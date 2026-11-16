"""Updater tests run offline and do not require Windows or a GitHub account."""
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import voicepilot_bootstrap as updater

from voicepilot_bootstrap import (
    UpdateError, apply_pending, check_and_stage, extract_checked,
    recover_interrupted, rollback, version_tuple, _safe_name,
)

BOOTSTRAP_PATH = Path(__file__).resolve().parents[1] / "voicepilot_bootstrap.py"


def archive(version="0.17.0", *, extra=None):
    contents = {
        "VERSION": version,
        "voicepilot_bootstrap.py": "# stable updater\\n",
        "setup_and_run.bat": "@echo off\\n",
        "requirements.txt": "new requirement\\n",
        "app/main.py": "new main\\n",
        "app/__init__.py": "",
        "chrome-extension/manifest.json": '{"manifest_version":3}',
    }
    contents.update(extra or {})
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, value in contents.items():
            zf.writestr(name, value)
    return stream.getvalue()


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "installed"
        self.cache = self.base / "cache"
        self.root.mkdir()
        (self.root / "app").mkdir()
        (self.root / "VERSION").write_text("0.16.0\n")
        (self.root / "app/main.py").write_text("old main")
        (self.root / "voicepilot_bootstrap.py").write_text("old stable bootstrap")
        (self.root / "requirements.txt").write_text("old deps")
        # Ephemeral local key only: no production signer key or network.
        self.key = Ed25519PrivateKey.generate()
        pub = self.key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        pin = patch.object(updater, "PINNED_SIGNER_PUBLIC_KEY_B64",
                           base64.b64encode(pub).decode("ascii"))
        pin.start()
        self.addCleanup(pin.stop)

    def signature(self, version, digest):
        payload = updater._signed_payload("v" + version, digest)
        return base64.b64encode(self.key.sign(payload)).decode("ascii")

    def release(self, payload, version="0.17.0", sha=None):
        digest = sha or hashlib.sha256(payload).hexdigest()
        url = f"https://github.com/ryotkkr-del/VoicePilot/releases/download/v{version}/VoicePilot-v{version}.zip"
        return {"tag_name": "v" + version, "draft": False, "prerelease": False,
                "assets": [
                    {"name": f"VoicePilot-v{version}.zip", "state": "uploaded",
                     "digest": "sha256:" + digest, "browser_download_url": url},
                    {"name": f"VoicePilot-v{version}.zip.sig", "state": "uploaded",
                     "browser_download_url": url + ".sig"},
                ]}

    def check(self, payload, version="0.17.0", sha=None):
        digest = sha or hashlib.sha256(payload).hexdigest()
        return check_and_stage(self.root, self.cache, force=True,
            fetch_release=lambda _: self.release(payload, version, sha),
            download=lambda _url, output: (output.write_bytes(payload), hashlib.sha256(payload).hexdigest())[1],
            download_signature=lambda _url: self.signature(version, digest))

    def test_semver(self):
        self.assertTrue(version_tuple("0.18.0") > version_tuple("0.17.9"))
        with self.assertRaises(UpdateError):
            version_tuple("v01.1.0")

    def test_valid_download_apply_and_rollback(self):
        payload = archive(extra={"app/new_feature.py": "print('x')"})
        self.assertEqual(self.check(payload), "downloaded")
        self.assertEqual((self.root / "app/main.py").read_text(), "old main")
        self.assertEqual(apply_pending(self.root, self.cache), "applied")
        self.assertEqual((self.root / "VERSION").read_text(), "0.17.0")
        self.assertEqual((self.root / "app/main.py").read_text(), "new main\\n")
        self.assertEqual((self.root / "voicepilot_bootstrap.py").read_text(), "old stable bootstrap")
        self.assertTrue((self.root / "app/new_feature.py").exists())
        self.assertEqual(rollback(self.root, self.cache), "rolled-back")
        self.assertEqual((self.root / "VERSION").read_text(), "0.16.0\n")
        self.assertEqual((self.root / "app/main.py").read_text(), "old main")
        self.assertFalse((self.root / "app/new_feature.py").exists())

    def test_digest_mismatch_never_stages(self):
        with self.assertRaises(UpdateError):
            self.check(archive(), sha="0" * 64)
        self.assertFalse((self.cache / "pending.json").exists())

    def test_current_version_has_no_download(self):
        self.assertEqual(self.check(archive("0.16.0"), "0.16.0"), "current")
        self.assertFalse((self.cache / "pending.json").exists())

    def test_invalid_release_url_rejected(self):
        a = archive()
        release = self.release(a)
        release["assets"][0]["browser_download_url"] = "https://evil.example/update.zip"
        with self.assertRaises(UpdateError):
            check_and_stage(self.root, self.cache, force=True, fetch_release=lambda _: release)

    def test_invalid_archive_paths(self):
        # Windows ZipFile normalizes backslashes; check raw validation directly.
        for name in ("../app/evil.py", "/etc/passwd", "app/../app/main.py", "app/x\\evil.py", "app/.hidden.py"):
            with self.subTest(name=name), self.assertRaises(UpdateError):
                _safe_name(name)
        # This path stays intact when ZipFile writes the archive on Windows.
        with self.assertRaises(UpdateError):
            self.check(archive(extra={"app/.hidden.py": "bad"}))

    def test_missing_required_file(self):
        path = self.base / "invalid.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("VERSION", "0.17.0")
        with self.assertRaises(UpdateError):
            extract_checked(path, self.base / "extract", "0.17.0")

    def test_corrupted_stage_does_not_modify_live_code(self):
        self.assertEqual(self.check(archive()), "downloaded")
        info = json.loads((self.cache / "pending.json").read_text())
        (self.cache / info["stage"] / "app/main.py").write_text("tampered")
        with self.assertRaises(UpdateError):
            apply_pending(self.root, self.cache)
        self.assertEqual((self.root / "app/main.py").read_text(), "old main")

    def test_interrupted_install_restores_backup(self):
        self.cache.mkdir()
        backup = self.cache / ("backup-" + "a"*32)
        (backup / "app").mkdir(parents=True)
        (backup / "app/main.py").write_text("old main")
        self.root.joinpath("app/main.py").write_text("broken main")
        (self.cache / "journal.json").write_text(json.dumps({"backup": backup.name,
            "files": [{"name": "app/main.py", "existed": True}]}))
        self.assertTrue(recover_interrupted(self.root, self.cache))
        self.assertEqual((self.root / "app/main.py").read_text(), "old main")

    def test_older_release_no_downgrade(self):
        self.assertEqual(self.check(archive("0.15.0"), "0.15.0"), "current")

if __name__ == "__main__":
    unittest.main()