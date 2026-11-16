"""Offline Ed25519 release signer. The private key NEVER enters CI or a ZIP.

Usage:
  python scripts/sign_release.py dist/VoicePilot-v0.24.0.zip --key C:/secure/private.pem
The key file must be generated offline and kept outside this repository.
Do not upload its contents or the password to GitHub/ChatGPT.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
from pathlib import Path
import re
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voicepilot_bootstrap import _signed_payload, version_tuple


def sign_archive(archive: Path, key_file: Path, password: bytes | None) -> tuple[Path, str]:
    archive, key_file = archive.resolve(strict=True), key_file.resolve(strict=True)
    if key_file.is_relative_to(ROOT):
        raise ValueError("秘密鍵をソースリポジトリの外に移動してください")
    if not archive.is_file() or not key_file.is_file():
        raise ValueError("ZIPまたは秘密鍵がファイルではありません")
    match = re.fullmatch(r"VoicePilot-(v[0-9]+\.[0-9]+\.[0-9]+)\.zip", archive.name)
    if not match:
        raise ValueError("ZIP名はVoicePilot-vX.Y.Z.zipの形式にしてください")
    tag = match.group(1)
    version_tuple(tag)
    private = serialization.load_pem_private_key(key_file.read_bytes(), password=password)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("Ed25519の秘密鍵のみ使用できます")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    signature = private.sign(_signed_payload(tag, digest))
    sidecar = archive.with_name(archive.name + ".sig")
    sidecar.write_text(base64.b64encode(signature).decode("ascii"), encoding="ascii")
    public_key = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    # Public-key fingerprint is a *display aid*, not proof of signer identity.
    fingerprint = hashlib.sha256(public_key).hexdigest()
    print("ZIP SHA-256:", digest)
    print("Signer PUBLIC key (Base64):", base64.b64encode(public_key).decode("ascii"))
    print("Signer PUBLIC key SHA-256 fingerprint:", fingerprint)
    return sidecar, fingerprint


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign VoicePilot release ZIP with an offline Ed25519 private key")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--key", type=Path, required=True,
                        help="Encrypted PEM Ed25519 private key stored OUTSIDE the repository")
    args = parser.parse_args()
    # Never accept password via command-line args, environment variables,
    # or persistent plaintext files: those often leak into process lists/logs.
    raw_password = getpass.getpass("Private key password (blank for unencrypted legacy key): ")
    password = raw_password.encode("utf-8") if raw_password else None
    try:
        output, _ = sign_archive(args.archive, args.key, password)
    except (OSError, ValueError, TypeError) as exc:
        print("Signing failed: verify archive, key type and passphrase.", file=sys.stderr)
        return 1
    finally:
        del raw_password
        del password
    print("Created detached signature:", output)
    print("Upload the ZIP and .sig together to the matching GitHub Release.")
    print("A trusted PUBLIC key still must be pinned out-of-band in the immutable bootstrap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
