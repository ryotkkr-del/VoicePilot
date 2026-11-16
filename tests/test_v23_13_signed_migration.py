"""Offline P0-4 migration regression tests; only ephemeral folders and keys.

No real user files, admin permissions, network, release uploads or execution.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from app import signed_migration as migration
from voicepilot_bootstrap import REPO, _signing_message


class OfflineSignedMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old = self.base / "existing-VoicePilot"
        self.old.mkdir()
        (self.old / "VERSION").write_text("0.23.12", encoding="utf-8")
        (self.old / "user-secret.txt").write_text("untouched user data", encoding="utf-8")
        self.key = Ed25519PrivateKey.generate()  # test-only, no actual signer
        public = self.key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
        self.pub_hex = public.hex()
        self.fingerprint = hashlib.sha256(public).hexdigest()
        self.version = "0.23.13"
        self.archive = self.base / f"VoicePilot-v{self.version}.zip"
        self.sig = self.base / f"VoicePilot-v{self.version}.sig.json"
        self.make_bundle()
        self.root_patch = patch.object(migration, "SOURCE_ROOT", self.old)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def make_bundle(self, *, key_hex=None, revision=3, manifest_version=None,
                    version=None, missing=None):
        v = version or self.version
        self.archive = self.base / f"VoicePilot-v{v}.zip"
        pinned = self.pub_hex if key_hex is None else key_hex
        files = {
            "VERSION": v + "\n",
            "voicepilot_bootstrap.py":
                f"BOOTSTRAP_SECURITY_REVISION = {revision}\n"
                f"PINNED_ED25519_PUBLIC_KEY_HEX = {pinned!r}\n"
                "raise RuntimeError('MIGRATION MUST NOT EXECUTE ZIP FILES')\n",
            "setup_and_run.bat": "@echo off\n",
            "requirements.txt": "cryptography>=43,<50\n",
            "app/main.py": "# New signed VoicePilot\n",
            "app/__init__.py": "",
            "chrome-extension/manifest.json": json.dumps(
                {"manifest_version": 3, "version": manifest_version or v}
            ),
        }
        if missing:
            files.pop(missing)
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, body in files.items():
                zf.writestr(name, body)
        self.sig = self.base / f"VoicePilot-v{v}.sig.json"
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.envelope = {
            "schema": 1,
            "repository": REPO,
            "version": v,
            "sha256": digest,
            "signature": base64.b64encode(
                self.key.sign(_signing_message(v, digest))
            ).decode("ascii"),
        }
        self.sig.write_text(json.dumps(self.envelope), encoding="utf-8")

    def verify(self, **kwargs):
        return migration.prepare_signed_migration(
            self.archive, self.sig,
            kwargs.pop("public_key_hex", self.pub_hex),
            kwargs.pop("fingerprint", self.fingerprint),
            **kwargs
        )

    def assert_old_intact(self):
        self.assertEqual((self.old / "VERSION").read_text(), "0.23.12")
        self.assertEqual((self.old / "user-secret.txt").read_text(), "untouched user data")

    def test_valid_offline_check_creates_no_installation(self):
        answer, folder = self.verify()
        self.assertEqual(answer.version, "0.23.13")
        self.assertEqual(answer.public_key_fingerprint, self.fingerprint)
        self.assertIsNone(folder)
        self.assertEqual(sorted(p.name for p in self.base.iterdir()),
                         ["VoicePilot-v0.23.13.sig.json", "VoicePilot-v0.23.13.zip",
                          "existing-VoicePilot"])
        self.assert_old_intact()

    def test_real_signer_key_is_never_required_for_verification(self):
        answer, _ = self.verify()
        self.assertEqual(answer.zip_digest, self.envelope["sha256"])
        # Only public bytes/fingerprint are used; no private key file exists.
        self.assertEqual(len(self.pub_hex), 64)

    def test_wrong_independent_fingerprint_rejected_without_unzipping(self):
        with self.assertRaisesRegex(migration.MigrationError, "指紋"):
            self.verify(fingerprint="a" * 64)
        self.assert_old_intact()

    def test_wrong_public_key_cannot_use_true_fingerprint(self):
        with self.assertRaisesRegex(migration.MigrationError, "指紋"):
            self.verify(public_key_hex="a" * 64)

    def test_detached_signature_required(self):
        self.sig.unlink()
        with self.assertRaises(migration.MigrationError):
            self.verify()
        self.assert_old_intact()

    def test_forged_signature_rejected(self):
        forged = dict(self.envelope)
        forged["signature"] = base64.b64encode(
            Ed25519PrivateKey.generate().sign(
                _signing_message(self.version, self.envelope["sha256"])
            )).decode("ascii")
        self.sig.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaisesRegex(migration.MigrationError, "署名"):
            self.verify()

    def test_modified_zip_is_rejected_even_with_original_signature(self):
        with self.archive.open("ab") as output:
            output.write(b"tampered")
        with self.assertRaisesRegex(migration.MigrationError, "ハッシュ"):
            self.verify()

    def test_rejects_duplicate_signature_keys(self):
        original = json.dumps(self.envelope)
        duplicate = original.replace('"schema": 1', '"schema": 1, "schema": 1')
        self.sig.write_text(duplicate, encoding="utf-8")
        with self.assertRaises(migration.MigrationError):
            self.verify()

    def test_rejects_unsigned_bootstrap_inside_signed_archive(self):
        self.make_bundle(key_hex="")
        with self.assertRaisesRegex(migration.MigrationError, "bootstrap"):
            self.verify()

    def test_rejects_different_embedded_trust_anchor(self):
        self.make_bundle(key_hex="f" * 64)
        with self.assertRaisesRegex(migration.MigrationError, "bootstrap"):
            self.verify()

    def test_rejects_legacy_bootstrap_even_when_archive_is_signed(self):
        self.make_bundle(revision=2)
        with self.assertRaisesRegex(migration.MigrationError, "bootstrap"):
            self.verify()

    def test_rejects_chrome_manifest_version_mismatch(self):
        self.make_bundle(manifest_version="0.23.12")
        with self.assertRaisesRegex(migration.MigrationError, "Chrome"):
            self.verify()

    def test_missing_required_file_rejected_without_side_effects(self):
        self.make_bundle(missing="app/main.py")
        with self.assertRaises(migration.MigrationError):
            self.verify()
        self.assert_old_intact()

    def test_no_consent_cannot_create_destination(self):
        target = self.base / "VoicePilot-Signed"
        with patch.object(migration, "_assert_windows_nonadmin"):
            with self.assertRaisesRegex(migration.MigrationError, "承認"):
                self.verify(destination=target, confirmed=False)
        self.assertFalse(target.exists())

    def test_nonwindows_or_admin_machine_cannot_install(self):
        target = self.base / "VoicePilot-Signed"
        with patch.object(migration, "_assert_windows_nonadmin",
                          side_effect=migration.MigrationError("Windows安全条件が不一致")):
            with self.assertRaisesRegex(migration.MigrationError, "Windows安全"):
                self.verify(destination=target, confirmed=True)
        self.assertFalse(target.exists())

    def test_existing_destination_is_never_overwritten(self):
        target = self.base / "VoicePilot-Signed"
        target.mkdir()
        protected = target / "private.txt"
        protected.write_text("DO NOT TOUCH", encoding="utf-8")
        with patch.object(migration, "_assert_windows_nonadmin"):
            with self.assertRaisesRegex(migration.MigrationError, "既に存在"):
                self.verify(destination=target, confirmed=True)
        self.assertEqual(protected.read_text(), "DO NOT TOUCH")

    def test_existing_install_child_is_rejected(self):
        target = self.old / "VoicePilot-Signed"
        with patch.object(migration, "_assert_windows_nonadmin"):
            with self.assertRaisesRegex(migration.MigrationError, "上書き"):
                self.verify(destination=target, confirmed=True)
        self.assertFalse(target.exists())

    def test_new_sibling_install_does_not_touch_original_or_copy_secrets(self):
        target = self.base / "VoicePilot-Signed"
        with patch.object(migration, "_assert_windows_nonadmin"):
            answer, placed = self.verify(destination=target, confirmed=True)
        # Windows may spell the same TEMP path using 8.3 short names.
        self.assertTrue(placed.samefile(target))
        self.assertEqual((placed / "VERSION").read_text().strip(), "0.23.13")
        self.assertIn(self.pub_hex, (placed / "voicepilot_bootstrap.py").read_text())
        self.assertFalse((placed / "user-secret.txt").exists())
        self.assertFalse((placed / ".venv").exists())
        self.assert_old_intact()
        self.assertEqual(answer.public_key_fingerprint, self.fingerprint)
        with patch.object(migration, "_assert_windows_nonadmin"):
            with self.assertRaises(migration.MigrationError):
                self.verify(destination=target, confirmed=True)

    def test_invalid_signature_never_creates_new_folder(self):
        target = self.base / "VoicePilot-Signed"
        changed = dict(self.envelope)
        changed["sha256"] = "0" * 64
        self.sig.write_text(json.dumps(changed), encoding="utf-8")
        with patch.object(migration, "_assert_windows_nonadmin"):
            with self.assertRaises(migration.MigrationError):
                self.verify(destination=target, confirmed=True)
        self.assertFalse(target.exists())
        self.assert_old_intact()

    def test_symlinked_zip_is_not_followed(self):
        linked = self.base / "alias.zip"
        try:
            linked.symlink_to(self.archive)
        except (OSError, NotImplementedError):
            self.skipTest("No symlink privileges on this Windows runner")
        with self.assertRaises(migration.MigrationError):
            migration.prepare_signed_migration(
                linked, self.sig, self.pub_hex, self.fingerprint
            )

    def test_destination_name_cannot_be_hidden_or_traverse(self):
        with patch.object(migration, "_assert_windows_nonadmin"):
            for folder in (".hidden", "bad name", "VoicePilot-Signed."):
                with self.subTest(folder=folder):
                    with self.assertRaises(migration.MigrationError):
                        self.verify(destination=self.base / folder, confirmed=True)


if __name__ == "__main__":
    unittest.main()
