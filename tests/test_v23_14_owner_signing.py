"""P0-4 offline owner signing workflow regressions: ephemeral keys only.

No GitHub publishing, network requests, actual developer keys, app launch
or modification of the user's existing files.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization

from app import signed_migration
from scripts import owner_release


VERSION = "0.23.14"


class OwnerSigningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.repo = self.home / "checkout"
        (self.repo / "dist").mkdir(parents=True)
        self.key = self.home / "private-ed25519.pem"
        self.output = self.home / "new-signed-candidate"
        self.password = b"ephemeral-test-key-passphrase-24!"
        self.source = self.repo / "dist" / f"VoicePilot-v{VERSION}.zip"
        self.old = self.home / "existing-install"
        self.old.mkdir()
        (self.old / "settings.json").write_text("DO NOT TOUCH", encoding="utf-8")
        self.mock_repo = patch.object(owner_release, "ROOT", self.repo)
        self.mock_repo.start()
        self.addCleanup(self.mock_repo.stop)
        self.make_archive()
        self.pub, self.fingerprint = owner_release.generate_encrypted_key(self.key, self.password)

    def make_archive(self, *, bootstrap_rev=3, boot_pin="", manifest_version=VERSION,
                     missing=None, filename=None):
        target = self.source if filename is None else self.repo / "dist" / filename
        files = {
            "VERSION": VERSION + "\n",
            "voicepilot_bootstrap.py": (
                f'BOOTSTRAP_SECURITY_REVISION = {bootstrap_rev}\n'
                f'PINNED_ED25519_PUBLIC_KEY_HEX = "{boot_pin}"\n'
                "raise RuntimeError('Never execute ZIP contents while signing')\n"
            ),
            "setup_and_run.bat": "@echo off\n",
            "requirements.txt": "cryptography>=43,<49\n",
            "app/__init__.py": "",
            "app/main.py": "# dummy app entry\n",
            "chrome-extension/manifest.json": json.dumps({
                "manifest_version": 3, "version": manifest_version,
            }),
        }
        if missing is not None:
            del files[missing]
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, value in files.items():
                zf.writestr(name, value)

    def build(self, **kwargs):
        return owner_release.prepare_signed_candidate(
            kwargs.pop("source", self.source),
            kwargs.pop("key_path", self.key),
            kwargs.pop("password", self.password),
            kwargs.pop("output_dir", self.output),
        )

    def test_generated_key_is_encrypted_and_not_in_repository(self):
        data = self.key.read_bytes()
        self.assertIn(b"BEGIN ENCRYPTED PRIVATE KEY", data)
        self.assertNotIn(b"BEGIN PRIVATE KEY-----", data)
        self.assertEqual(len(self.pub), 64)
        self.assertEqual(len(self.fingerprint), 64)
        key = owner_release._private_key(self.key, self.password)
        self.assertEqual(owner_release.public_identity(key), (self.pub, self.fingerprint))
        self.assertFalse(self.key.is_relative_to(self.repo))

    def test_full_pinned_signed_candidate_passes_independent_migration_check(self):
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self.build()
        self.assertEqual(result.public_key_hex, self.pub)
        self.assertEqual(result.public_key_fingerprint, self.fingerprint)
        self.assertTrue(result.archive.parent.samefile(self.output))
        self.assertEqual(len(list(self.output.iterdir())), 2)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original)
        self.assertEqual((self.old / "settings.json").read_text(), "DO NOT TOUCH")
        verified, placed = signed_migration.prepare_signed_migration(
            result.archive, result.signature, self.pub, self.fingerprint
        )
        self.assertEqual(verified.version, VERSION)
        self.assertIsNone(placed)
        with zipfile.ZipFile(result.archive) as archive:
            boot = archive.read("voicepilot_bootstrap.py").decode()
            self.assertIn('PINNED_ED25519_PUBLIC_KEY_HEX = "' + self.pub + '"', boot)
            self.assertNotIn("ephemeral-test-key-passphrase", str(archive.namelist()))
            self.assertNotIn("PRIVATE KEY", boot)
        self.assertFalse((self.output / self.key.name).exists())
        self.assertFalse((self.repo / "private-ed25519.pem").exists())

    def test_issued_signed_candidate_never_overwritten(self):
        first = self.build()
        with self.assertRaisesRegex(owner_release.OfflineSignerError, "既存"):
            self.build()
        self.assertTrue(first.archive.is_file())
        self.assertTrue(first.signature.is_file())

    def test_wrong_passphrase_creates_no_candidate(self):
        with self.assertRaises(owner_release.OfflineSignerError):
            self.build(password=b"definitely-wrong")
        self.assertFalse(self.output.exists())

    def test_tampered_signature_rejected_after_preparation(self):
        result = self.build()
        envelope = json.loads(result.signature.read_text(encoding="utf-8"))
        envelope["sha256"] = "a" * 64
        result.signature.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(signed_migration.MigrationError):
            signed_migration.prepare_signed_migration(
                result.archive, result.signature, self.pub, self.fingerprint
            )

    def test_existing_pinned_bootstrap_is_not_silently_rekeyed(self):
        self.make_archive(boot_pin="a" * 64)
        with self.assertRaisesRegex(owner_release.OfflineSignerError, "公開鍵"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_legacy_bootstrap_not_approved_for_signing(self):
        self.make_archive(bootstrap_rev=2)
        with self.assertRaisesRegex(owner_release.OfflineSignerError, "旧bootstrap"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_chrome_manifest_mismatch_rejected_without_output(self):
        self.make_archive(manifest_version="0.23.13")
        with self.assertRaisesRegex(owner_release.OfflineSignerError, "Chrome"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_missing_required_file_rejected_without_output(self):
        self.make_archive(missing="app/main.py")
        with self.assertRaises((signed_migration.MigrationError, ValueError)):
            self.build()
        self.assertFalse(self.output.exists())

    def test_source_zip_version_name_must_match(self):
        other = self.repo / "dist" / "renamed.zip"
        other.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(owner_release.OfflineSignerError, "名前"):
            self.build(source=other)

    def test_key_placed_in_repository_is_refused(self):
        in_repo = self.repo / "signing-secret.pem"
        with self.assertRaisesRegex(owner_release.OfflineSignerError, "リポジトリ"):
            owner_release.generate_encrypted_key(in_repo, self.password)
        self.assertFalse(in_repo.exists())

    def test_short_passphrase_and_existing_key_are_rejected(self):
        another = self.home / "new-key.pem"
        with self.assertRaisesRegex(owner_release.OfflineSignerError, "16バイト"):
            owner_release.generate_encrypted_key(another, b"weak")
        self.assertFalse(another.exists())
        with self.assertRaisesRegex(owner_release.OfflineSignerError, "既に"):
            owner_release.generate_encrypted_key(self.key, self.password)

    def test_output_cannot_be_inside_repository(self):
        with self.assertRaisesRegex(owner_release.OfflineSignerError, "リポジトリ"):
            self.build(output_dir=self.repo / "test-release")
        self.assertFalse((self.repo / "test-release").exists())

    def test_source_and_existing_installation_remain_untouched_when_output_is_invalid(self):
        protected = self.home / "already-existing"
        protected.mkdir()
        (protected / "content.txt").write_text("original", encoding="utf-8")
        with self.assertRaisesRegex(owner_release.OfflineSignerError, "既存"):
            self.build(output_dir=protected)
        self.assertEqual((protected / "content.txt").read_text(), "original")
        self.assertEqual((self.old / "settings.json").read_text(), "DO NOT TOUCH")

    def test_bad_private_key_type_is_rejected(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        data = rsa_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(self.password),
        )
        self.key.write_bytes(data)
        with self.assertRaisesRegex(owner_release.OfflineSignerError, "Ed25519"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_private_key_symlink_rejected_where_supported(self):
        link = self.home / "alias-key.pem"
        try:
            link.symlink_to(self.key)
        except (NotImplementedError, OSError):
            self.skipTest("No symlink permission on Windows CI")
        with self.assertRaises(owner_release.OfflineSignerError):
            self.build(key_path=link)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
