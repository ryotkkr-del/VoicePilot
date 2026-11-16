"""P0-4 signed release validation with ephemeral test-only Ed25519 keys.

No developer private key, network, real PC data, or current installation used.
"""
from __future__ import annotations

import base64
from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import voicepilot_bootstrap as updater
from scripts.sign_release import sign_archive


VERSION = "0.23.10"
URL = f"https://github.com/ryotkkr-del/VoicePilot/releases/download/v{VERSION}/VoicePilot-v{VERSION}.zip"


def sample_zip() -> bytes:
    files = {
        "VERSION": VERSION, "voicepilot_bootstrap.py": "stable updater",
        "setup_and_run.bat": "@echo off", "requirements.txt": "new",
        "app/main.py": "signed new app", "app/__init__.py": "",
        "chrome-extension/manifest.json": '{"manifest_version": 3}',
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buf.getvalue()


class SignedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "installed"
        self.cache = base / "cache"
        (self.root / "app").mkdir(parents=True)
        (self.root / "VERSION").write_text("0.23.9\n", encoding="utf-8")
        (self.root / "app/main.py").write_text("old app", encoding="utf-8")
        self.payload = sample_zip()
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.key = Ed25519PrivateKey.generate()
        pub = self.key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        self.public_b64 = base64.b64encode(pub).decode("ascii")
        self.pin = patch.object(updater, "PINNED_SIGNER_PUBLIC_KEY_B64", self.public_b64)
        self.pin.start()
        self.addCleanup(self.pin.stop)

    def sig(self, tag=None, digest=None, key=None):
        return base64.b64encode(
            (key or self.key).sign(updater._signed_payload(
                tag or f"v{VERSION}", digest or self.digest
            ))
        ).decode("ascii")

    def release(self, with_signature=True, signature_url=URL + ".sig", digest=None):
        assets = [{"name": f"VoicePilot-v{VERSION}.zip", "state": "uploaded",
                   "digest": "sha256:" + (digest or self.digest),
                   "browser_download_url": URL}]
        if with_signature:
            assets.append({"name": f"VoicePilot-v{VERSION}.zip.sig",
                           "state": "uploaded",
                           "browser_download_url": signature_url})
        return {"tag_name": f"v{VERSION}", "draft": False, "prerelease": False,
                "assets": assets}

    def stage(self, sig=None, release=None):
        return updater.check_and_stage(
            self.root, self.cache, force=True,
            fetch_release=lambda _: release or self.release(),
            download=lambda _url, destination: (
                destination.write_bytes(self.payload), self.digest
            )[1],
            download_signature=lambda _: self.sig() if sig is None else sig,
        )

    def assert_old(self):
        self.assertEqual((self.root / "VERSION").read_text(), "0.23.9\n")
        self.assertEqual((self.root / "app/main.py").read_text(), "old app")

    def test_valid_signature_stages_then_applies(self):
        self.assertEqual(self.stage(), "downloaded")
        self.assert_old()
        self.assertEqual(self.stage(), "staged")
        self.assertEqual(updater.apply_pending(self.root, self.cache), "applied")
        self.assertEqual((self.root / "VERSION").read_text(), VERSION)
        self.assertEqual((self.root / "app/main.py").read_text(), "signed new app")

    def test_no_pinned_key_refuses_even_correct_github_checksum(self):
        with patch.object(updater, "PINNED_SIGNER_PUBLIC_KEY_B64", ""):
            with self.assertRaisesRegex(updater.UpdateError, "公開鍵が未設定"):
                self.stage()
        self.assertFalse((self.cache / "pending.json").exists())
        self.assert_old()

    def test_missing_detached_signature_never_stages(self):
        with self.assertRaisesRegex(updater.UpdateError, "署名ファイルがありません"):
            self.stage(release=self.release(with_signature=False))
        self.assertFalse((self.cache / "pending.json").exists())

    def test_signature_from_other_key_rejected(self):
        with self.assertRaisesRegex(updater.UpdateError, "署名が無効"):
            self.stage(sig=self.sig(key=Ed25519PrivateKey.generate()))
        self.assert_old()

    def test_signature_bound_to_exact_release_version_and_digest(self):
        for invalid in (self.sig(tag="v0.23.11"), self.sig(digest="a" * 64)):
            with self.subTest(signature=invalid[:12]), self.assertRaisesRegex(updater.UpdateError, "署名が無効"):
                self.stage(sig=invalid)
        self.assert_old()

    def test_malformed_or_noncanonical_signature_rejected(self):
        for invalid in ("not_base64@", self.sig() + "\n", "", "A" * 88):
            with self.subTest(value=invalid[:12]), self.assertRaises(updater.UpdateError):
                self.stage(sig=invalid)
        self.assert_old()

    def test_signature_asset_from_foreign_location_rejected(self):
        with self.assertRaisesRegex(updater.UpdateError, "署名のURL"):
            self.stage(release=self.release(signature_url="https://example.org/release.sig"))
        self.assert_old()

    def test_unsigned_pending_from_old_bootstrap_never_applies(self):
        self.stage()
        path = self.cache / "pending.json"
        import json
        info = json.loads(path.read_text(encoding="utf-8"))
        info.pop("signature_b64")
        path.write_text(json.dumps(info), encoding="utf-8")
        with self.assertRaises(updater.UpdateError):
            updater.apply_pending(self.root, self.cache)
        self.assert_old()

    def test_modified_pending_signature_never_applies(self):
        self.stage()
        path = self.cache / "pending.json"
        import json
        info = json.loads(path.read_text(encoding="utf-8"))
        info["signature_b64"] = self.sig(key=Ed25519PrivateKey.generate())
        path.write_text(json.dumps(info), encoding="utf-8")
        with self.assertRaisesRegex(updater.UpdateError, "署名が無効"):
            updater.apply_pending(self.root, self.cache)
        self.assert_old()

    def test_rejected_staged_package_is_not_trusted_as_already_staged(self):
        self.stage()
        import json
        p = self.cache / "pending.json"
        value = json.loads(p.read_text(encoding="utf-8"))
        value.pop("signature_b64")
        p.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.stage(), "downloaded")
        self.assertEqual(updater.apply_pending(self.root, self.cache), "applied")

    def test_tampered_staged_files_and_pending_hashes_cannot_bypass_signature(self):
        self.stage()
        import json
        pending_path = self.cache / "pending.json"
        metadata = json.loads(pending_path.read_text(encoding="utf-8"))
        staged = self.cache / metadata["stage"] / "app/main.py"
        staged.write_text("attacker modified code", encoding="utf-8")
        metadata["files"]["app/main.py"] = hashlib.sha256(staged.read_bytes()).hexdigest()
        pending_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(updater.UpdateError, "署名済みZIPと一致"):
            updater.apply_pending(self.root, self.cache)
        self.assert_old()

    def test_missing_or_tampered_signed_archive_rejected(self):
        for modification in ("missing", "tampered"):
            with self.subTest(modification=modification):
                self.stage()
                import json
                info = json.loads((self.cache / "pending.json").read_text(encoding="utf-8"))
                archived = self.cache / info["stage"] / updater._SIGNED_ARCHIVE_NAME
                if modification == "missing":
                    archived.unlink()
                else:
                    archived.write_bytes(b"forged, unsigned replacement")
                with self.assertRaises(updater.UpdateError):
                    updater.apply_pending(self.root, self.cache)
                self.assert_old()

    def test_unsigned_cache_metadata_without_archive_is_not_installable(self):
        self.stage()
        import json
        info = json.loads((self.cache / "pending.json").read_text(encoding="utf-8"))
        archived = self.cache / info["stage"] / updater._SIGNED_ARCHIVE_NAME
        archived.unlink()
        with self.assertRaises(updater.UpdateError):
            updater.apply_pending(self.root, self.cache)
        self.assert_old()

    def test_offline_signer_refuses_invalid_archive(self):
        base = Path(self.tmp.name)
        invalid_archive = base / f"VoicePilot-v{VERSION}.zip"
        with zipfile.ZipFile(invalid_archive, "w") as archive:
            archive.writestr("VERSION", VERSION)
            archive.writestr("app/main.py", "missing everything else")
        key_file = base / "offline-private.pem"
        key_file.write_bytes(self.key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(b"test-password-123"),
        ))
        with self.assertRaises(updater.UpdateError):
            sign_archive(invalid_archive, key_file, b"test-password-123")
        self.assertFalse((base / (invalid_archive.name + ".sig")).exists())

    def test_offline_signer_works_with_encrypted_key_outside_repo(self):
        base = Path(self.tmp.name)
        archive = base / f"VoicePilot-v{VERSION}.zip"
        archive.write_bytes(self.payload)
        key_file = base / "offline-private.pem"
        key_file.write_bytes(self.key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(b"test-password-123"),
        ))
        with redirect_stdout(io.StringIO()):
            sidecar, fingerprint = sign_archive(archive, key_file, b"test-password-123")
        self.assertEqual(sidecar.name, archive.name + ".sig")
        self.assertEqual(len(fingerprint), 64)
        updater._verify_signed_release(f"v{VERSION}", self.digest, sidecar.read_text("ascii"))
        self.assertNotIn("PRIVATE KEY", sidecar.read_text("ascii"))
        self.assertTrue(key_file.exists())


if __name__ == "__main__":
    unittest.main()
