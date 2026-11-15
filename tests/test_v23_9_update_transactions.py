"""v0.23.9 P0-4: offline update transaction and rollback fault injection.

All paths live in TemporaryDirectory. No real Windows installation, browser,
microphone, network, credentials or self-modification is involved.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import voicepilot_bootstrap as updater


def archive(version="0.23.9"):
    files = {
        "VERSION": version,
        "voicepilot_bootstrap.py": "# stable updater\n",
        "setup_and_run.bat": "@echo off\n",
        "requirements.txt": "new dependencies\n",
        "app/main.py": "updated main\n",
        "app/__init__.py": "",
        "chrome-extension/manifest.json": '{"manifest_version":3}',
        "app/new_feature.py": "added new module\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        self.root = home / "installed"
        self.cache = home / "cache"
        (self.root / "app").mkdir(parents=True)
        self.old_version = "0.23.8\n"
        (self.root / "VERSION").write_text(self.old_version, encoding="utf-8")
        (self.root / "app/main.py").write_text("original main", encoding="utf-8")
        (self.root / "requirements.txt").write_text("old dependencies", encoding="utf-8")
        (self.root / "voicepilot_bootstrap.py").write_text("stable bootstrap", encoding="utf-8")
        self.keep = self.root / "local_user_settings.json"
        self.keep.write_text('{"private":"not part of release"}', encoding="utf-8")

    def stage(self):
        payload = archive()
        sha = hashlib.sha256(payload).hexdigest()
        release = {
            "tag_name": "v0.23.9", "draft": False, "prerelease": False,
            "assets": [{
                "name": "VoicePilot-v0.23.9.zip", "state": "uploaded",
                "digest": "sha256:" + sha,
                "browser_download_url": (
                    "https://github.com/ryotkkr-del/VoicePilot/"
                    "releases/download/v0.23.9/VoicePilot-v0.23.9.zip"
                ),
            }],
        }
        result = updater.check_and_stage(
            self.root, self.cache, force=True,
            fetch_release=lambda _: release,
            download=lambda _url, destination: (
                destination.write_bytes(payload), sha
            )[1],
        )
        self.assertEqual(result, "downloaded")

    def assert_old_intact(self):
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), self.old_version)
        self.assertEqual((self.root / "app/main.py").read_text(encoding="utf-8"), "original main")
        self.assertEqual((self.root / "requirements.txt").read_text(encoding="utf-8"), "old dependencies")
        self.assertEqual((self.root / "voicepilot_bootstrap.py").read_text(encoding="utf-8"), "stable bootstrap")
        self.assertFalse((self.root / "app/new_feature.py").exists())
        self.assertEqual(self.keep.read_text(encoding="utf-8"), '{"private":"not part of release"}')

    def test_full_apply_and_rollback_preserve_custom_files(self):
        self.stage()
        self.assertEqual(updater.apply_pending(self.root, self.cache), "applied")
        self.assertEqual((self.root / "VERSION").read_text(), "0.23.9")
        info = json.loads((self.cache / "last_good.json").read_text(encoding="utf-8"))
        for item in info["files"]:
            if item["existed"]:
                self.assertRegex(item["backup_sha256"], r"^[0-9a-f]{64}$")
            else:
                self.assertNotIn("backup_sha256", item)
        self.assertEqual(updater.rollback(self.root, self.cache), "rolled-back")
        self.assert_old_intact()

    def test_apply_failure_after_partial_replacement_recovers_original(self):
        self.stage()
        original_replace = os.replace
        failed = [False]

        def fail_once(source, target):
            if Path(target) == self.root / "requirements.txt" and not failed[0]:
                failed[0] = True
                raise OSError("simulated write failure")
            return original_replace(source, target)

        with patch.object(updater.os, "replace", side_effect=fail_once):
            with self.assertRaisesRegex(updater.UpdateError, "旧版を復元"):
                updater.apply_pending(self.root, self.cache)
        self.assertTrue(failed[0])
        self.assert_old_intact()
        self.assertFalse((self.cache / "journal.json").exists())

    def test_final_commit_record_write_failure_restores_old_installation(self):
        self.stage()
        original_atomic = updater._atomic_json

        def fail_only_commit(path, data):
            if Path(path).name == "last_good.json":
                raise OSError("simulated fsync/full disk on commit record")
            return original_atomic(path, data)

        with patch.object(updater, "_atomic_json", side_effect=fail_only_commit):
            with self.assertRaisesRegex(updater.UpdateError, "旧版を復元"):
                updater.apply_pending(self.root, self.cache)
        self.assert_old_intact()
        self.assertFalse((self.cache / "journal.json").exists())
        self.assertFalse((self.cache / "last_good.json").exists())

    def test_corrupt_backup_preflights_before_overwriting_any_live_file(self):
        self.stage()
        self.assertEqual(updater.apply_pending(self.root, self.cache), "applied")
        new_main = (self.root / "app/main.py").read_text()
        info = json.loads((self.cache / "last_good.json").read_text())
        backup = self.cache / info["backup"]
        (backup / "requirements.txt").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(updater.UpdateError, "バックアップ"):
            updater.rollback(self.root, self.cache)
        self.assertEqual((self.root / "app/main.py").read_text(), new_main)
        self.assertEqual((self.root / "VERSION").read_text(), "0.23.9")
        self.assertTrue((self.cache / "last_good.json").exists())
        self.assertEqual(self.keep.read_text(), '{"private":"not part of release"}')

    def test_missing_late_backup_preflights_before_restoring_earlier_files(self):
        self.stage()
        self.assertEqual(updater.apply_pending(self.root, self.cache), "applied")
        info = json.loads((self.cache / "last_good.json").read_text())
        backup = self.cache / info["backup"]
        (backup / "requirements.txt").unlink()
        with self.assertRaisesRegex(updater.UpdateError, "バックアップ"):
            updater.rollback(self.root, self.cache)
        self.assertEqual((self.root / "VERSION").read_text(), "0.23.9")
        self.assertEqual((self.root / "app/main.py").read_text(), "updated main\n")

    def test_crash_after_commit_before_journal_delete_invalidates_rollback(self):
        self.stage()
        self.assertEqual(updater.apply_pending(self.root, self.cache), "applied")
        info = json.loads((self.cache / "last_good.json").read_text())
        # Simulate a process crash between writing last_good and deleting
        # the still-durable journal: recovery must not keep stale metadata.
        (self.cache / "journal.json").write_text(json.dumps(info), encoding="utf-8")
        self.assertTrue(updater.recover_interrupted(self.root, self.cache))
        self.assert_old_intact()
        self.assertFalse((self.cache / "last_good.json").exists())
        self.assertFalse((self.cache / "journal.json").exists())

    def test_recovery_rejects_corrupt_snapshot_without_partial_restore(self):
        self.stage()
        self.assertEqual(updater.apply_pending(self.root, self.cache), "applied")
        info = json.loads((self.cache / "last_good.json").read_text())
        backup = self.cache / info["backup"]
        (backup / "requirements.txt").write_text("tampered", encoding="utf-8")
        (self.cache / "journal.json").write_text(json.dumps(info), encoding="utf-8")
        with self.assertRaisesRegex(updater.UpdateError, "バックアップ"):
            updater.recover_interrupted(self.root, self.cache)
        self.assertEqual((self.root / "app/main.py").read_text(), "updated main\n")
        self.assertTrue((self.cache / "journal.json").exists())

    def test_legacy_recovery_journal_without_hashes_remains_supported(self):
        self.cache.mkdir(parents=True, exist_ok=True)
        backup = self.cache / ("backup-" + "a" * 32)
        (backup / "app").mkdir(parents=True)
        (backup / "app/main.py").write_text("legacy original", encoding="utf-8")
        (self.root / "app/main.py").write_text("new code", encoding="utf-8")
        (self.cache / "journal.json").write_text(
            json.dumps({"backup": backup.name, "files": [
                {"name": "app/main.py", "existed": True}
            ]}), encoding="utf-8"
        )
        self.assertTrue(updater.recover_interrupted(self.root, self.cache))
        self.assertEqual((self.root / "app/main.py").read_text(), "legacy original")

    def test_recovery_rejects_untrusted_relative_names_without_modification(self):
        self.cache.mkdir(parents=True, exist_ok=True)
        backup = self.cache / ("backup-" + "b" * 32)
        backup.mkdir()
        for bad in ["../outside.py", "app/con.py", "app/a.py:evil.py"]:
            with self.subTest(bad=bad):
                with self.assertRaises(updater.UpdateError):
                    updater._restore(self.root, backup, [
                        {"name": bad, "existed": True}
                    ])
        self.assert_old_intact()

    def test_stage_modified_after_preflight_never_installs_tampered_bytes(self):
        self.stage()
        original_copy = updater.shutil.copyfile
        tampered = [False]

        def tamper_before_copy(source, destination, *args, **kwargs):
            source_path = Path(source)
            if (source_path.name == "main.py" and "stage-" in str(source_path)
                    and not tampered[0]):
                source_path.write_text("tampered after preflight", encoding="utf-8")
                tampered[0] = True
            return original_copy(source, destination, *args, **kwargs)

        with patch.object(updater.shutil, "copyfile", side_effect=tamper_before_copy):
            with self.assertRaisesRegex(updater.UpdateError, "旧版を復元"):
                updater.apply_pending(self.root, self.cache)
        self.assertTrue(tampered[0])
        self.assert_old_intact()

    def test_commit_written_then_reports_failure_does_not_leave_stale_rollback(self):
        self.stage()
        original_atomic = updater._atomic_json

        def commit_then_raise(path, data):
            original_atomic(path, data)
            if Path(path).name == "last_good.json":
                raise OSError("simulated exception immediately after commit rename")

        with patch.object(updater, "_atomic_json", side_effect=commit_then_raise):
            with self.assertRaisesRegex(updater.UpdateError, "旧版を復元"):
                updater.apply_pending(self.root, self.cache)
        self.assert_old_intact()
        self.assertFalse((self.cache / "last_good.json").exists())
        self.assertFalse((self.cache / "journal.json").exists())

    def test_app_exposes_old_updater_revision_without_silent_upgrade(self):
        from app.main import bootstrap_security_revision
        with patch.object(updater, "BOOTSTRAP_SECURITY_REVISION", 0):
            self.assertEqual(bootstrap_security_revision(), 0)
        self.assertGreaterEqual(bootstrap_security_revision(), 2)
        # Older installations can still run the app, but see the warning.
        # This check never modifies the bootstrap or auto-installs one.

    def test_backup_symlink_rejected_if_supported_by_runner(self):
        self.stage()
        self.assertEqual(updater.apply_pending(self.root, self.cache), "applied")
        info = json.loads((self.cache / "last_good.json").read_text())
        backup = self.cache / info["backup"]
        real = backup / "app/main.py"
        replacement = backup / "app/other_source.py"
        real.rename(replacement)
        try:
            real.symlink_to(replacement)
        except (OSError, NotImplementedError):
            self.skipTest("Windows runner has no symlink privilege")
        with self.assertRaises(updater.UpdateError):
            updater.rollback(self.root, self.cache)
        self.assertEqual((self.root / "VERSION").read_text(), "0.23.9")


if __name__ == "__main__":
    unittest.main()
