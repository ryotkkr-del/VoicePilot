"""P0-4: independent Ed25519 release signatures, entirely offline.

Ephemeral Ed25519 test keys exist only in memory. No developer signing keys,
real updater folders, outbound requests, microphone or Windows GUI are used.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import voicepilot_bootstrap as updater
from app.main import bootstrap_signing_key_configured


def make_archive(version="0.23.10", *, malicious=False):
    files = {
        "VERSION": version,
        "voicepilot_bootstrap.py": "# immutable bootstrap\n",
        "setup_and_run.bat": "@echo off\n",
        "requirements.txt": "dependencies\n",
        "app/main.py": "signed main code\n" if not malicious else "evil main\n",
        "app/__init__.py": "",
        "chrome-extension/manifest.json": '{"manifest_version":3}',
    }
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return result.getvalue()


class SignedUpdaterTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.root, self.cache = base / "install", base / "cache"
        (self.root / "app").mkdir(parents=True)
        (self.root / "app/main.py").write_text("existing main\n", encoding="utf-8")
        (self.root / "VERSION").write_text("0.23.9", encoding="utf-8")
        (self.root / "voicepilot_bootstrap.py").write_text("old trusted loader", encoding="utf-8")
        (self.root / "requirements.txt").write_text("old requirements", encoding="utf-8")
        self.user_data = self.root / "user-private-note.txt"
        self.user_data.write_text("keep me", encoding="utf-8")
        self.private_key = Ed25519PrivateKey.generate()
        pub = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        self.key_hex = pub.hex()
        self.key_patcher = patch.object(updater, "PINNED_ED25519_PUBLIC_KEY_HEX", self.key_hex)
        self.key_patcher.start()
        self.addCleanup(self.key_patcher.stop)
        self.payload = make_archive()

    def signed_release(self, payload=None, *, signed_version="0.23.10",
                       signed_digest=None, include_sig_asset=True):
        payload = self.payload if payload is None else payload
        digest = hashlib.sha256(payload).hexdigest()
        signature_digest = digest if signed_digest is None else signed_digest
        sig = base64.b64encode(self.private_key.sign(
            updater._signing_message(signed_version, signature_digest)
        )).decode("ascii")
        envelope = {
            "schema": 1,
            "repository": updater.REPO,
            "version": signed_version,
            "sha256": signature_digest,
            "signature": sig,
        }
        base = f"https://github.com/{updater.REPO}/releases/download/v0.23.10/"
        assets = [{
            "name": "VoicePilot-v0.23.10.zip", "state": "uploaded",
            "digest": "sha256:" + digest,
            "browser_download_url": base + "VoicePilot-v0.23.10.zip",
        }]
        if include_sig_asset:
            assets.append({
                "name": "VoicePilot-v0.23.10.sig.json", "state": "uploaded",
                "browser_download_url": base + "VoicePilot-v0.23.10.sig.json",
            })
        release = {"tag_name": "v0.23.10", "draft": False, "prerelease": False,
                   "assets": assets}
        return release, envelope

    def stage(self, release, envelope, payload=None):
        payload = self.payload if payload is None else payload
        return updater.check_and_stage(
            self.root, self.cache, force=True,
            fetch_release=lambda _: release,
            fetch_signature=lambda _: envelope,
            download=lambda _url, output: (
                output.write_bytes(payload), hashlib.sha256(payload).hexdigest()
            )[1],
        )

    def assert_unchanged(self):
        self.assertEqual((self.root / "VERSION").read_text(), "0.23.9")
        self.assertEqual((self.root / "app/main.py").read_text(), "existing main\n")
        self.assertEqual((self.root / "voicepilot_bootstrap.py").read_text(), "old trusted loader")
        self.assertEqual(self.user_data.read_text(), "keep me")

    def test_signature_canonical_message_has_fixed_prefix_and_fields(self):
        digest = hashlib.sha256(self.payload).hexdigest()
        expected = (b"VoicePilot independent release signature v1\n" +
                    json.dumps({"schema": 1, "repository": updater.REPO,
                                "version": "0.23.10", "sha256": digest},
                               sort_keys=True, separators=(",", ":")).encode("ascii"))
        self.assertEqual(updater._signing_message("0.23.10", digest), expected)

    def test_valid_signed_download_apply_and_rollback(self):
        release, envelope = self.signed_release()
        self.assertEqual(self.stage(release, envelope), "downloaded")
        pending = json.loads((self.cache / "pending.json").read_text())
        self.assertEqual(pending["signed_release"], envelope)
        self.assertTrue((self.cache / pending["stage"] / "_signed_release.zip").exists())
        self.assertEqual(updater.apply_pending(self.root, self.cache), "applied")
        self.assertEqual((self.root / "VERSION").read_text(), "0.23.10")
        self.assertEqual((self.root / "app/main.py").read_text(), "signed main code\n")
        self.assertEqual((self.root / "voicepilot_bootstrap.py").read_text(),
                         "old trusted loader", "New unsigned/updater code never self-overwrites bootstrap")
        self.assertEqual(self.user_data.read_text(), "keep me")
        self.assertEqual(updater.rollback(self.root, self.cache), "rolled-back")
        self.assert_unchanged()

    def test_signature_absent_rejects_without_downloading(self):
        release, envelope = self.signed_release(include_sig_asset=False)
        downloader = Mock()
        with self.assertRaisesRegex(updater.UpdateError, "署名がない"):
            updater.check_and_stage(self.root, self.cache, force=True,
                                    fetch_release=lambda _: release,
                                    fetch_signature=lambda _: envelope, download=downloader)
        downloader.assert_not_called()
        self.assert_unchanged()

    def test_forged_signature_does_not_download(self):
        release, envelope = self.signed_release()
        fake = dict(envelope)
        fake["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
        downloader = Mock()
        with self.assertRaisesRegex(updater.UpdateError, "署名の照合"):
            updater.check_and_stage(self.root, self.cache, force=True,
                                    fetch_release=lambda _: release,
                                    fetch_signature=lambda _: fake, download=downloader)
        downloader.assert_not_called()
        self.assert_unchanged()

    def test_mismatched_signed_version_repository_and_digest_rejected(self):
        release, envelope = self.signed_release()
        for changed in [
            {**envelope, "version": "0.23.11"},
            {**envelope, "repository": "attacker/fork"},
            {**envelope, "sha256": "1" * 64},
            {**envelope, "schema": True},
            {**envelope, "unexpected": "extra"},
        ]:
            with self.subTest(changed=changed):
                with self.assertRaises(updater.UpdateError):
                    self.stage(release, changed)
                self.assert_unchanged()

    def test_wrong_pinned_key_rejects_correct_signature(self):
        _, envelope = self.signed_release()
        another_key = Ed25519PrivateKey.generate().public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        ).hex()
        with patch.object(updater, "PINNED_ED25519_PUBLIC_KEY_HEX", another_key):
            with self.assertRaisesRegex(updater.UpdateError, "署名の照合"):
                updater._verify_signed_release(
                    envelope, "0.23.10", hashlib.sha256(self.payload).hexdigest()
                )

    def test_tampered_pending_signature_fails_before_any_installation(self):
        release, envelope = self.signed_release()
        self.assertEqual(self.stage(release, envelope), "downloaded")
        pending_file = self.cache / "pending.json"
        pending = json.loads(pending_file.read_text())
        pending["signed_release"]["signature"] = base64.b64encode(b"\xFF" * 64).decode()
        pending_file.write_text(json.dumps(pending))
        with self.assertRaises(updater.UpdateError):
            updater.apply_pending(self.root, self.cache)
        self.assert_unchanged()

    def test_attacker_editing_stage_and_hash_list_cannot_bypass_signature(self):
        release, envelope = self.signed_release()
        self.assertEqual(self.stage(release, envelope), "downloaded")
        pending_file = self.cache / "pending.json"
        pending = json.loads(pending_file.read_text())
        stage = self.cache / pending["stage"]
        tampered = b"untrusted main from tampered stage"
        (stage / "app/main.py").write_bytes(tampered)
        pending["files"]["app/main.py"] = hashlib.sha256(tampered).hexdigest()
        pending_file.write_text(json.dumps(pending))
        with self.assertRaisesRegex(updater.UpdateError, "署名付きZIPと一致"):
            updater.apply_pending(self.root, self.cache)
        self.assert_unchanged()

    def test_modified_archived_zip_fails_closed(self):
        release, envelope = self.signed_release()
        self.assertEqual(self.stage(release, envelope), "downloaded")
        pending = json.loads((self.cache / "pending.json").read_text())
        archive = self.cache / pending["stage"] / "_signed_release.zip"
        archive.write_bytes(make_archive(malicious=True))
        with self.assertRaisesRegex(updater.UpdateError, "ZIPのハッシュが一致"):
            updater.apply_pending(self.root, self.cache)
        self.assert_unchanged()

    def test_unconfigured_signer_rejects_new_updates_before_download(self):
        release, envelope = self.signed_release()
        downloader = Mock()
        with patch.object(updater, "PINNED_ED25519_PUBLIC_KEY_HEX", ""):
            with self.assertRaisesRegex(updater.UpdateError, "公開鍵が未設定"):
                updater.check_and_stage(
                    self.root, self.cache, force=True,
                    fetch_release=lambda _: release, fetch_signature=lambda _: envelope,
                    download=downloader,
                )
        downloader.assert_not_called()
        self.assertFalse((self.cache / "pending.json").exists())
        self.assert_unchanged()

    def test_legacy_unsigned_pending_is_rejected_after_key_provisioning(self):
        release, envelope = self.signed_release()
        self.assertEqual(self.stage(release, envelope), "downloaded")
        pending_file = self.cache / "pending.json"
        pending = json.loads(pending_file.read_text())
        # Simulate an older unsigned bootstrap's on-disk format without
        # ever enabling unsigned staging in the revision-3 updater itself.
        pending.pop("signed_release", None)
        (self.cache / pending["stage"] / "_signed_release.zip").unlink()
        pending_file.write_text(json.dumps(pending))
        with self.assertRaises(updater.UpdateError):
            updater.apply_pending(self.root, self.cache)
        self.assert_unchanged()
        with self.assertRaises(updater.UpdateError):
            self.stage(release, envelope)

    def test_missing_crypto_library_fails_closed(self):
        _, envelope = self.signed_release()
        original_import = __import__

        def missing_crypto(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("not installed")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing_crypto):
            with self.assertRaisesRegex(updater.UpdateError, "ライブラリ"):
                updater._verify_signed_release(
                    envelope, "0.23.10", hashlib.sha256(self.payload).hexdigest()
                )

    def test_offline_signing_helper_uses_external_key_and_matches_verifier(self):
        from scripts.sign_release import sign_archive
        from cryptography.hazmat.primitives.serialization import (
            BestAvailableEncryption, Encoding, PrivateFormat,
        )
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            key_path = home / "offline-private.pem"
            password = b"mock-only-secret"
            key_path.write_bytes(self.private_key.private_bytes(
                encoding=Encoding.PEM, format=PrivateFormat.PKCS8,
                encryption_algorithm=BestAvailableEncryption(password),
            ))
            archive = home / "VoicePilot-v0.23.10.zip"
            archive.write_bytes(self.payload)
            result = sign_archive(archive, key_path, password=password)
            envelope = json.loads(result.read_text(encoding="utf-8"))
            updater._verify_signed_release(
                envelope, "0.23.10", hashlib.sha256(self.payload).hexdigest()
            )
            self.assertFalse(key_path.is_relative_to(Path(__file__).resolve().parents[1]))
            with self.assertRaises(FileExistsError):
                sign_archive(archive, key_path, password=password)
            # Public signature contains no private PEM material.
            self.assertNotIn("PRIVATE KEY", result.read_text(encoding="utf-8"))

    def test_signer_rejects_private_key_in_repository(self):
        from scripts import sign_release
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat,
        )
        with tempfile.TemporaryDirectory() as directory:
            fake_repo = Path(directory)
            key_path = fake_repo / "test_private.pem"
            key_path.write_bytes(self.private_key.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
            ))
            archive = fake_repo / "VoicePilot-v0.23.10.zip"
            archive.write_bytes(self.payload)
            with patch.object(sign_release, "ROOT", fake_repo):
                with self.assertRaisesRegex(ValueError, "リポジトリ内"):
                    sign_release.sign_archive(archive, key_path)

    def test_signature_status_never_inferred_from_revision_alone(self):
        self.assertTrue(bootstrap_signing_key_configured())
        with patch.object(updater, "PINNED_ED25519_PUBLIC_KEY_HEX", ""):
            self.assertFalse(bootstrap_signing_key_configured())
            self.assertEqual(updater.BOOTSTRAP_SECURITY_REVISION, 3)
        # A higher revision number by itself is not an authenticated anchor.


if __name__ == "__main__":
    unittest.main()
