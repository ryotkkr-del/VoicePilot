"""Offline release signer. NEVER upload a signing private key to GitHub.

Prepare a PEM-encoded Ed25519 private key outside the VoicePilot repository,
then sign an already-built release ZIP on a trusted, offline machine.

Example (offline, after independently verifying the ZIP):
    python scripts/sign_release.py dist/VoicePilot-vX.Y.Z.zip \
        --key D:/secure-offline/voicepilot-ed25519.pem --ask-passphrase

Produces a public JSON signature beside the ZIP for manual publishing.
This script NEVER generates or uploads keys and NEVER contacts a network.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voicepilot_bootstrap import REPO, _signing_message, version_tuple


def sign_archive(archive: Path, key_path: Path, *, password: bytes | None = None,
                 output: Path | None = None) -> Path:
    """Sign one release with a user-owned key external to the code repository."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    zip_path = Path(archive).resolve(strict=True)
    key = Path(key_path).resolve(strict=True)
    if key.is_relative_to(ROOT):
        raise ValueError("署名秘密鍵をVoicePilotのリポジトリ内に保存しないでください")
    match = re.fullmatch(r"VoicePilot-v(\d+\.\d+\.\d+)\.zip", zip_path.name)
    if not match:
        raise ValueError("ZIPファイル名がVoicePilot-vX.Y.Z.zipの形式ではありません")
    version = match.group(1)
    version_tuple(version)
    with zipfile.ZipFile(zip_path) as zf:
        if zf.read("VERSION").decode("utf-8").strip() != version:
            raise ValueError("ZIPのVERSIONとファイル名が一致しません")
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    signer = serialization.load_pem_private_key(key.read_bytes(), password=password)
    if not isinstance(signer, Ed25519PrivateKey):
        raise ValueError("Ed25519の秘密鍵のみ使用できます")
    envelope = {
        "schema": 1,
        "repository": REPO,
        "version": version,
        "sha256": digest,
        "signature": base64.b64encode(signer.sign(_signing_message(version, digest))).decode("ascii"),
    }
    destination = (Path(output) if output is not None else zip_path.with_name(
        f"VoicePilot-v{version}.sig.json"
    )).resolve()
    if destination == key or destination == zip_path:
        raise ValueError("署名出力先が秘密鍵やZIPと重複しています")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Never silently overwrite an already-issued signature.
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=True, sort_keys=True, indent=2)
        handle.write("\n")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign a release ZIP with an offline Ed25519 private key")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--key", type=Path, required=True, help="Private key PEM outside this repository")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--ask-passphrase", action="store_true", help="Prompt without echo (for encrypted PEM)")
    args = parser.parse_args()
    password = getpass.getpass("Ed25519 private key password: ").encode() if args.ask_passphrase else None
    try:
        result = sign_archive(args.archive, args.key, password=password, output=args.output)
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        print("Signature creation refused; confirm the ZIP, key location and passphrase.", file=sys.stderr)
        return 1
    print(f"Public signature file created: {result}")
    print("Verify the trusted public-key fingerprint out of band before shipping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
