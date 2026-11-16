"""Prepare an owner-signed, pinned-bootstrap VoicePilot ZIP entirely OFFLINE.

The real signing secret is created/kept only on the owner's machine, never on
GitHub, CI, ChatGPT, or inside a distributed ZIP. This tool does not publish
anything or mark an installation safe. An independently verified fingerprint
must still be delivered out-of-band to the person installing the release.

Examples:
  python scripts/owner_release.py new-key --key D:/private/vp-signing.pem
  python scripts/owner_release.py prepare --source-zip dist/VoicePilot-vX.Y.Z.zip \
      --key D:/private/vp-signing.pem --output-dir D:/release/vX.Y.Z-trusted
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import getpass
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import signed_migration
from scripts.sign_release import sign_archive

_VERSIONED_ZIP = re.compile(r"VoicePilot-v(\d+\.\d+\.\d+)\.zip\Z")
_OUTPUT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,75}\Z")


class OfflineSignerError(ValueError):
    """Operator action needed; no private key or original installation changed."""


@dataclass(frozen=True)
class SigningResult:
    archive: Path
    signature: Path
    public_key_hex: str
    public_key_fingerprint: str


def _external_private_key(path: Path, *, must_exist: bool) -> Path:
    value = Path(path)
    if value.is_symlink():
        raise OfflineSignerError("署名秘密鍵へのシンボリックリンクは使用できません")
    repo = ROOT.resolve(strict=True)
    try:
        parent = value.parent.resolve(strict=True)
    except OSError as exc:
        raise OfflineSignerError("秘密鍵は既存の安全な保存先フォルダを指定してください") from exc
    candidate = parent / value.name
    if candidate == repo or candidate.is_relative_to(repo):
        raise OfflineSignerError("秘密鍵をリポジトリ内に保存しないでください")
    if must_exist:
        if not candidate.is_file() or candidate.stat().st_size > 16 * 1024:
            raise OfflineSignerError("暗号化された秘密鍵ファイルがありません")
    elif candidate.exists() or candidate.is_symlink():
        raise OfflineSignerError("秘密鍵ファイルが既に存在します。上書きを拒否します")
    return candidate


def _private_key(path: Path, password: bytes) -> Ed25519PrivateKey:
    key_path = _external_private_key(path, must_exist=True)
    data = key_path.read_bytes()
    if not data.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----"):
        raise OfflineSignerError("暗号化されたPKCS8 Ed25519秘密鍵のみ使用できます")
    try:
        key = serialization.load_pem_private_key(data, password=password)
    except (TypeError, ValueError) as exc:
        raise OfflineSignerError("秘密鍵の形式かパスフレーズが正しくありません") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise OfflineSignerError("Ed25519形式の秘密鍵だけ使用できます")
    return key


def public_identity(key: Ed25519PrivateKey) -> tuple[str, str]:
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return public.hex(), hashlib.sha256(public).hexdigest()


def generate_encrypted_key(path: Path, password: bytes) -> tuple[str, str]:
    """Create-only, password-encrypted PKCS8 private key outside checkout."""
    if not isinstance(password, bytes) or len(password) < 16:
        raise OfflineSignerError("秘密鍵のパスフレーズは16バイト以上にしてください")
    key_path = _external_private_key(path, must_exist=False)
    key = Ed25519PrivateKey.generate()
    encrypted = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(password),
    )
    # O_EXCL, no following an existing symlink, and owner-only on POSIX.
    # Windows operators should also choose a user-protected private directory.
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        key_path.unlink(missing_ok=True)
        raise
    return public_identity(key)


def _pin_bootstrap(path: Path, public_key_hex: str) -> None:
    """Modify only one literal assignment in the extracted candidate ZIP."""
    if signed_migration._literal_constant(path, "BOOTSTRAP_SECURITY_REVISION") < 3:
        raise OfflineSignerError("旧bootstrapを署名付きリリースとして出せません")
    if signed_migration._literal_constant(path, "PINNED_ED25519_PUBLIC_KEY_HEX") != "":
        raise OfflineSignerError("既に公開鍵が設定されています。署名元を再確認してください")
    original = path.read_text(encoding="utf-8")
    pattern = re.compile(r'^PINNED_ED25519_PUBLIC_KEY_HEX = ""(?:[ \t]*#.*)?$', re.M)
    if len(pattern.findall(original)) != 1:
        raise OfflineSignerError("bootstrapの公開鍵設定行が一意ではありません")
    updated = pattern.sub(f'PINNED_ED25519_PUBLIC_KEY_HEX = "{public_key_hex}"', original)
    ast.parse(updated)
    path.write_text(updated, encoding="utf-8")
    if signed_migration._literal_constant(path, "PINNED_ED25519_PUBLIC_KEY_HEX") != public_key_hex:
        raise OfflineSignerError("bootstrapの公開鍵の固定に失敗しました")


def _fresh_output(directory: Path, key: Path) -> Path:
    raw = Path(directory).absolute()
    if not _OUTPUT_NAME.fullmatch(raw.name) or raw.name.endswith("."):
        raise OfflineSignerError("出力フォルダ名が不正です")
    if raw.exists() or raw.is_symlink():
        raise OfflineSignerError("既存の出力先を上書きできません")
    if not raw.parent.is_dir() or raw.parent.is_symlink():
        raise OfflineSignerError("出力先の親フォルダは既存の実ディレクトリにしてください")
    parent = raw.parent.resolve(strict=True)
    candidate = parent / raw.name
    repo = ROOT.resolve(strict=True)
    if candidate == repo or candidate.is_relative_to(repo) or repo.is_relative_to(candidate):
        raise OfflineSignerError("署名済み成果物はリポジトリの外に出してください")
    if key == candidate or key.is_relative_to(candidate):
        raise OfflineSignerError("秘密鍵と成果物の保存先を分離してください")
    if candidate.exists() or candidate.is_symlink():
        raise OfflineSignerError("出力フォルダが作成済みです")
    return candidate


def prepare_signed_candidate(source_zip: Path, key_path: Path, password: bytes,
                             output_directory: Path) -> SigningResult:
    """Create signed ZIP and sidecar in a NEW external folder after self-check.

    This never changes the unsigned source, repository, current app or key.
    No network requests or automatic publishing are performed.
    """
    source = Path(source_zip)
    key_file = _external_private_key(key_path, must_exist=True)
    if source.is_symlink() or not source.is_file():
        raise OfflineSignerError("検証用の元ZIPがありません")
    match = _VERSIONED_ZIP.fullmatch(source.name)
    if not match:
        raise OfflineSignerError("元ZIPのファイル名が不正です")
    if source.stat().st_size > signed_migration.MAX_ARCHIVE_BYTES:
        raise OfflineSignerError("元ZIPがサイズ上限を超えています")
    version = match.group(1)
    signed_migration._canonical_message(version, "0" * 64)
    target = _fresh_output(output_directory, key_file)
    signer = _private_key(key_file, password)
    public_hex, fingerprint = public_identity(signer)

    with tempfile.TemporaryDirectory(prefix="vp-offline-sign-", dir=target.parent) as scratch:
        scratch = Path(scratch)
        # Copy first so a concurrent change to the source cannot introduce
        # unauthenticated bytes between inspection and signing.
        original = scratch / source.name
        shutil.copyfile(source, original)
        if original.stat().st_size > signed_migration.MAX_ARCHIVE_BYTES:
            raise OfflineSignerError("元ZIPが検証中に変更されました")
        extracted = scratch / "unpacked"
        signed_migration._extract_signed_archive(original, extracted, version)
        _pin_bootstrap(extracted / "voicepilot_bootstrap.py", public_hex)
        manifest = signed_migration._read_extension_manifest(
            extracted / "chrome-extension" / "manifest.json"
        )
        if manifest.get("manifest_version") != 3 or manifest.get("version") != version:
            raise OfflineSignerError("Chrome拡張とVERSIONが一致しません")
        packaged = scratch / "packaged"
        packaged.mkdir()
        candidate = packaged / source.name
        with zipfile.ZipFile(candidate, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(extracted.rglob("*")):
                if item.is_file() and not item.is_symlink():
                    archive.write(item, item.relative_to(extracted).as_posix())
        if candidate.stat().st_size > signed_migration.MAX_ARCHIVE_BYTES:
            raise OfflineSignerError("署名済みZIPがサイズ上限を超えています")
        detached = sign_archive(candidate, key_file, password=password)
        # A second independently implemented read-only verifier reconstructs
        # the ZIP contents and compares their embedded trust anchor.
        verified, no_install = signed_migration.prepare_signed_migration(
            candidate, detached, public_hex, fingerprint
        )
        if verified.version != version or no_install is not None:
            raise OfflineSignerError("独立検証の結果が一致しません")
        # Destination is created exclusively, not renamed over an existing
        # folder. A mid-copy failure removes only our new files.
        target.mkdir(mode=0o700)
        made = []
        try:
            for file in (candidate, detached):
                placed = target / file.name
                with file.open("rb") as src, placed.open("xb") as dst:
                    made.append(placed)
                    shutil.copyfileobj(src, dst, length=64 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
            return SigningResult(target / candidate.name,
                                 target / detached.name, public_hex, fingerprint)
        except BaseException:
            for file in made:
                file.unlink(missing_ok=True)
            try:
                target.rmdir()
            except OSError:
                pass
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OFFLINE owner signer: encrypted key generation or pinned signed ZIP creation")
    commands = parser.add_subparsers(dest="command", required=True)
    new_key = commands.add_parser("new-key", help="Generate a new encrypted offline Ed25519 key")
    new_key.add_argument("--key", required=True, type=Path)
    prepare = commands.add_parser("prepare", help="Create new signed ZIP and detached signature")
    prepare.add_argument("--source-zip", required=True, type=Path)
    prepare.add_argument("--key", required=True, type=Path)
    prepare.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "new-key":
            first = getpass.getpass("New private signing key passphrase (16+ bytes): ").encode()
            repeat = getpass.getpass("Confirm passphrase: ").encode()
            if first != repeat:
                raise OfflineSignerError("パスフレーズが一致しません")
            public_key, fingerprint = generate_encrypted_key(args.key, first)
        else:
            password = getpass.getpass("Offline signing key passphrase: ").encode()
            result = prepare_signed_candidate(
                args.source_zip, args.key, password, args.output_dir
            )
            public_key, fingerprint = result.public_key_hex, result.public_key_fingerprint
            print("SIGNED_ZIP", result.archive)
            print("DETACHED_SIGNATURE", result.signature)
        print("PUBLIC_ED25519_KEY_HEX", public_key)
        print("PUBLIC_KEY_SHA256_FINGERPRINT", fingerprint)
        print("公開鍵の本人確認はGitHub以外の信頼できる経路で行ってください。")
        print("秘密鍵をGitHubやチャットへ送信しないでください。正式運用は未承認です。")
        return 0
    except (OfflineSignerError, OSError, ValueError, TypeError, zipfile.BadZipFile) as exc:
        print("鍵または署名付きZIPの作成を拒否しました。入力と保存先を確認してください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
