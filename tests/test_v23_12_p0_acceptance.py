"""P0-5: offline tests of the evidence harness. No hardware or user files."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import p0_acceptance as acceptance


class P0AcceptanceEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "chrome-extension").mkdir()
        (self.root / "VERSION").write_text("0.23.12\n", encoding="utf-8")
        (self.root / "chrome-extension/manifest.json").write_text(
            '{"manifest_version":3,"version":"0.23.12"}', encoding="utf-8"
        )
        (self.root / "voicepilot_bootstrap.py").write_text(
            "BOOTSTRAP_SECURITY_REVISION = 3\n"
            "PINNED_ED25519_PUBLIC_KEY_HEX = ''\n"
            "if False:\n    raise RuntimeError('THIS MUST NEVER EXECUTE')\n",
            encoding="utf-8",
        )

    def examine(self, **kwargs):
        return acceptance.preflight(
            self.root, platform_name="windows", admin=False,
            windows_build=22631, python_version=(3, 11), **kwargs
        )

    def test_pure_preflight_identifies_unsigned_bootstrap_and_never_approves(self):
        result = self.examine()
        self.assertEqual(result["checks"]["windows_11"], "PASS")
        self.assertEqual(result["checks"]["chrome_manifest_match"], "PASS")
        self.assertEqual(result["checks"]["developer_key_pinned"], "BLOCKED")
        self.assertEqual(result["public_key_status"], "not_configured")
        self.assertFalse(result["production_approved"])
        self.assertEqual(result["decision"], "P0_INCOMPLETE")
        self.assertEqual(set(result["manual_results"].values()), {"NOT_RUN"})

    def test_does_not_execute_untrusted_bootstrap_code(self):
        # The bootstrap contains a top-level statement that would throw if
        # executed. Pure AST parsing must not import or run it.
        result = self.examine()
        self.assertEqual(result["checks"]["signed_bootstrap_revision"], "PASS")

    def test_public_key_presence_is_not_identity_attestation(self):
        (self.root / "voicepilot_bootstrap.py").write_text(
            "BOOTSTRAP_SECURITY_REVISION = 3\n"
            "PINNED_ED25519_PUBLIC_KEY_HEX = '" + ("b" * 64) + "'\n",
            encoding="utf-8"
        )
        result = self.examine()
        self.assertEqual(result["checks"]["developer_key_pinned"], "UNVERIFIED")
        self.assertEqual(result["public_key_status"], "present_identity_unverified")
        self.assertFalse(result["production_approved"])
        self.assertNotIn("b" * 64, json.dumps(result))

    def test_invalid_component_versions_and_admin_execution_are_blocked(self):
        (self.root / "chrome-extension/manifest.json").write_text(
            '{"manifest_version":3,"version":"0.23.11"}', encoding="utf-8"
        )
        result = acceptance.preflight(
            self.root, platform_name="windows", admin=True,
            windows_build=22631, python_version=(3, 11)
        )
        self.assertEqual(result["checks"]["non_admin"], "BLOCKED")
        self.assertEqual(result["checks"]["chrome_manifest_match"], "BLOCKED")

    def test_nonwindows_default_will_not_be_mistaken_for_real_hardware(self):
        result = acceptance.preflight(
            self.root, platform_name="non-windows", admin=False,
            windows_build=None, python_version=(3, 11)
        )
        self.assertEqual(result["checks"]["windows_11"], "UNVERIFIED")
        self.assertEqual(result["checks"]["non_admin"], "BLOCKED")

    def test_bad_bootstrap_and_manifest_never_claim_safe(self):
        (self.root / "voicepilot_bootstrap.py").write_text("not valid python (", encoding="utf-8")
        (self.root / "chrome-extension/manifest.json").write_text("invalid JSON", encoding="utf-8")
        result = self.examine()
        self.assertEqual(result["checks"]["signed_bootstrap_revision"], "BLOCKED")
        self.assertEqual(result["checks"]["developer_key_pinned"], "BLOCKED")
        self.assertEqual(result["checks"]["chrome_manifest_match"], "BLOCKED")
        self.assertFalse(result["production_approved"])

    def test_operator_marks_only_fixed_statuses_and_never_saves_text(self):
        result = self.examine()
        answers = iter(["PASS", "FAIL", "SKIP", "my bank password is FAKE_SECRET"] +
                       ["PASS"] * (len(acceptance.MANUAL_CASES) - 4))
        printed = []
        updated = acceptance.collect_manual(result, ask=lambda _: next(answers),
                                            out=lambda text: printed.append(text))
        self.assertEqual(updated["manual_results"]["P0-1_focus_guard"], "PASS")
        self.assertEqual(updated["manual_results"]["P0-2_gui_stop"], "FAIL")
        self.assertEqual(updated["manual_results"]["P0-2_hotkey_stop"], "SKIP")
        self.assertEqual(updated["manual_results"]["P0-2_voice_stop"], "NOT_RUN")
        self.assertNotIn("FAKE_SECRET", json.dumps(updated))
        self.assertFalse(updated["production_approved"])

    def test_cancellation_leaves_remaining_cases_unverified(self):
        def interrupt(_):
            raise KeyboardInterrupt()
        result = acceptance.collect_manual(self.examine(), ask=interrupt,
                                           out=lambda _: None)
        self.assertEqual(set(result["manual_results"].values()), {"NOT_RUN"})
        self.assertEqual(result["decision"], "P0_INCOMPLETE")

    def test_even_all_manual_passes_cannot_authorize_production(self):
        result = acceptance.collect_manual(
            self.examine(), ask=lambda _: "PASS", out=lambda _: None
        )
        self.assertEqual(set(result["manual_results"].values()), {"PASS"})
        self.assertFalse(result["production_approved"])
        self.assertEqual(result["decision"], "P0_INCOMPLETE")

    def test_report_is_explicit_and_create_only(self):
        path = self.root / "reports" / "P0-result.json"
        report = self.examine()
        acceptance.save_report(report, path)
        self.assertEqual(json.loads(path.read_text("utf-8"))["decision"], "P0_INCOMPLETE")
        with self.assertRaises(FileExistsError):
            acceptance.save_report({"decision": "forged"}, path)
        self.assertEqual(json.loads(path.read_text("utf-8"))["decision"], "P0_INCOMPLETE")

    def test_main_prints_report_but_creates_no_file_by_default(self):
        with patch.object(acceptance, "ROOT", self.root), patch.object(
            acceptance, "_platform_evidence", return_value=("non-windows", None, None)
        ), redirect_stdout(io.StringIO()) as capture:
            exit_code = acceptance.main([])
        self.assertEqual(exit_code, 0)
        rendered = json.loads(capture.getvalue())
        self.assertFalse(rendered["production_approved"])
        self.assertFalse((self.root / "reports").exists())

    def test_interactive_refused_on_nonwindows_and_elevated(self):
        with patch.object(acceptance, "ROOT", self.root), patch.object(
            acceptance, "_platform_evidence", return_value=("non-windows", None, None)
        ), redirect_stdout(io.StringIO()):
            code = acceptance.main(["--interactive"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
