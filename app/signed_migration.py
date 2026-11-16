"""Offline, opt-in preparation of a NEW trusted VoicePilot installation.

This tool does not download releases, provision a key, modify the existing
installation, copy personal settings, install dependencies or launch programs.
The caller must have authenticated the signing public-key fingerprint using
a channel independent of GitHub. Never trust a fingerprint merely because
it is printed in the ZIP, signature sidecar or GitHub release notes.
"""
from __future__ import annotations

import argparse
import ast
import base64
import binascii
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass

SOURCE_ROOT = Path(__file__).resolve().parents[1]
# Keep this verifier self-contained: legacy bootstraps predate Ed25519 and
# must not be imported/executed just to inspect a signed migration package.
REPO = "ryotkkr-del/VoicePilot"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
MAX_FILES = 400
_SIGNED_KEYS = frozenset({"schema", "repository", "version", "sha256", "signature"})
_REQUIRED = frozenset({
    "VERSION", "voicepilot_bootstrap.py", "setup_and_run.bat", "app/main.py",
    "app/__init__.py", "chrome-extension/manifest.json", "requirements.txt",
})
_ALLOWED_FILES = frozenset({
    "VERSION", "requirements.txt", "README.md", "README_JA.md",
    "run_tests.bat", "try_voicepilot.bat", "voicepilot_bootstrap.py", "setup_and_run.bat",
    "rollback_update.bat",
})
_ALLOWED_DIRS = ("app/", "chrome-extension/", "tests/", "tests_js/")
_HEX_KEY = re.compile(r"[0-9a-f]{64}\Z")
_VERSIONED_ZIP = re.compile(r"VoicePilot-v(\d+\.\d+\.\d+)\.zip\Z")
_NEW_DIRECTORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,75}\Z")
_MAX_SIGNATURE_SIZE = 2048


class MigrationError(ValueError):
    """A failed precondition; do not create or modify any installation."""


@dataclass(frozen=True)
class VerifiedBundle:
    version: str
    zip_digest: str
    public_key_fingerprint: str


def _canonical_message(version: str, digest: str) -> bytes:
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise MigrationError("署名対象バージョンが不正です")
    if _HEX_KEY.fullmatch(digest) is None:
        raise MigrationError("署名対象ハッシュが不正です")
    body = {"schema": 1, "repository": REPO, "version": version, "sha256": digest}
    return (b"VoicePilot independent release signature v1\n" +
            json.dumps(body, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True).encode("ascii"))


def _validated_name(name: str) -> str:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise MigrationError("ZIPに不正なパスがあります")
    path = PurePosixPath(name)
    if (path.as_posix() != name or
            any(part in {"", ".", ".."} for part in name.split("/")) or
            len(path.parts) > 8):
        raise MigrationError("ZIPのパスが安全ではありません")
    for part in path.parts:
        if (part.startswith(".") or part.endswith((" ", ".")) or
                any(ord(ch) < 32 or ch in '<>:"|?*' for ch in part) or
                re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)):
            raise MigrationError("ZIPのWindows予約パスを拒否しました")
    if not (name in _ALLOWED_FILES or name.startswith(_ALLOWED_DIRS)):
        raise MigrationError("ZIPに許可外のファイルがあります")
    if not (name == "VERSION" or name.endswith((".py", ".js", ".json", ".html", ".md", ".txt", ".bat"))):
        raise MigrationError("ZIPの拡張子が不正です")
    return name


def _extract_signed_archive(archive: Path, destination: Path, version: str) -> None:
    """Reject dangerous ZIP metadata before any source file is unpacked."""
    seen: set[str] = set()
    names: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise MigrationError("ZIPのファイル数が上限を超えています")
        total = 0
        for entry in infos:
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or entry.flag_bits & 1:
                raise MigrationError("ZIPのリンク・暗号化エントリを拒否しました")
            if entry.is_dir():
                continue
            name = _validated_name(entry.filename)
            folded = unicodedata.normalize("NFC", name).casefold()
            if folded in seen:
                raise MigrationError("ZIPのファイル名が重複しています")
            seen.add(folded)
            names.append(name)
            total += entry.file_size
            if total > MAX_UNPACKED_BYTES:
                raise MigrationError("ZIPの展開サイズが上限を超えています")
        if not _REQUIRED.issubset(names):
            raise MigrationError("ZIPの必須ファイルが不足しています")
        destination.mkdir()
        for name in sorted(names):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=64 * 1024)
    if (destination / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise MigrationError("署名済みZIPのVERSIONが一致しません")


def _read_exact_manifest(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_SIGNATURE_SIZE:
        raise MigrationError("署名ファイルが欠落、またはサイズ上限を超えています")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise MigrationError("署名ファイルに重複キーがあります")
            result[key] = value
        return result

    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, ValueError, OSError) as exc:
        raise MigrationError("署名ファイルを解析できません") from exc
    if not isinstance(data, dict) or set(data) != _SIGNED_KEYS:
        raise MigrationError("署名ファイルの形式が不正です")
    return data


def _literal_constant(file: Path, name: str):
    if file.is_symlink() or not file.is_file() or file.stat().st_size > 128 * 1024:
        raise MigrationError("署名されたbootstrapファイルが不正です")
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"))
    except (UnicodeError, SyntaxError) as exc:
        raise MigrationError("bootstrapを安全に解析できません") from exc
    matches = [
        stmt.value.value for stmt in tree.body
        if isinstance(stmt, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name
                for target in stmt.targets)
        and isinstance(stmt.value, ast.Constant)
    ]
    if len(matches) != 1:
        raise MigrationError("bootstrapの信頼鍵設定を特定できません")
    return matches[0]


def _verify_archive_copy(local_zip: Path, signature: Path, key_hex: str,
                         expected_fingerprint: str, extracted: Path) -> VerifiedBundle:
    """Validate a *local immutable copy* of the ZIP before exposing its files."""
    if not isinstance(key_hex, str) or _HEX_KEY.fullmatch(key_hex) is None:
        raise MigrationError("信頼済み公開鍵は64桁の小文字16進数で指定してください")
    if (not isinstance(expected_fingerprint, str)
            or _HEX_KEY.fullmatch(expected_fingerprint) is None):
        raise MigrationError("別経路で確認した公開鍵指紋が不正です")
    public_bytes = bytes.fromhex(key_hex)
    fingerprint = hashlib.sha256(public_bytes).hexdigest()
    if not hmac.compare_digest(fingerprint, expected_fingerprint):
        raise MigrationError("公開鍵指紋が一致しません。GitHubの情報だけで判断しないでください")
    match = _VERSIONED_ZIP.fullmatch(local_zip.name)
    if match is None:
        raise MigrationError("ZIPの名前が正規のリリース形式ではありません")
    version = match.group(1)
    _canonical_message(version, "0" * 64)
    if local_zip.stat().st_size > MAX_ARCHIVE_BYTES:
        raise MigrationError("ZIPがサイズ上限を超えています")

    # Verify a detached signature against the caller's independently
    # authenticated public key, never a key learned from the ZIP itself.
    with local_zip.open("rb") as zip_stream:
        digest = hashlib.file_digest(zip_stream, "sha256").hexdigest()
    envelope = _read_exact_manifest(signature)
    if (type(envelope.get("schema")) is not int or envelope["schema"] != 1
            or envelope["repository"] != REPO or envelope["version"] != version
            or envelope["sha256"] != digest):
        raise MigrationError("署名されたリポジトリ・バージョン・ZIPハッシュが一致しません")
    signature_text = envelope["signature"]
    if not isinstance(signature_text, str) or len(signature_text) > 128:
        raise MigrationError("署名データが不正です")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise MigrationError("署名ライブラリが利用できません。移行を拒否します") from exc
    try:
        raw_signature = base64.b64decode(signature_text, validate=True)
        if (len(raw_signature) != 64 or
                base64.b64encode(raw_signature).decode("ascii") != signature_text):
            raise ValueError("not canonical Base64 Ed25519")
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            raw_signature, _canonical_message(version, digest)
        )
    except (ValueError, binascii.Error, InvalidSignature) as exc:
        raise MigrationError("開発者の独立署名を検証できません。移行を拒否します") from exc

    try:
        _extract_signed_archive(local_zip, extracted, version)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise MigrationError("署名済みZIPの内容が安全な配布形式ではありません") from exc

    bootstrap = extracted / "voicepilot_bootstrap.py"
    revision = _literal_constant(bootstrap, "BOOTSTRAP_SECURITY_REVISION")
    pinned = _literal_constant(bootstrap, "PINNED_ED25519_PUBLIC_KEY_HEX")
    if type(revision) is not int or revision < 3 or pinned != key_hex:
        raise MigrationError("配布されたbootstrapに同じ信頼鍵が固定されていません")
    manifest = _read_extension_manifest(extracted / "chrome-extension" / "manifest.json")
    if manifest.get("manifest_version") != 3 or manifest.get("version") != version:
        raise MigrationError("Chrome拡張とリリースのバージョンが一致しません")

    return VerifiedBundle(version, digest, fingerprint)


def _read_extension_manifest(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise MigrationError("Chrome拡張の設定が不正です")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise MigrationError("Chrome拡張の設定を解析できません") from exc
    return value if isinstance(value, dict) else {}


def _assert_windows_nonadmin() -> None:
    if os.name != "nt":
        raise MigrationError("別フォルダへの導入はWindows 11の通常権限でのみ実施してください")
    try:
        import ctypes
        build = int(sys.getwindowsversion().build)
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (OSError, AttributeError, ValueError) as exc:
        raise MigrationError("Windows環境または権限を確認できません") from exc
    if build < 22000 or admin:
        raise MigrationError("Windows 11の管理者ではないアカウントで実施してください")


def _safe_destination(destination: Path) -> Path:
    raw = Path(destination).absolute()
    if not _NEW_DIRECTORY.fullmatch(raw.name) or raw.name.endswith("."):
        raise MigrationError("新しいフォルダ名が不正です")
    if raw.exists() or raw.is_symlink():
        raise MigrationError("導入先が既に存在します。上書きは行いません")
    if not raw.parent.is_dir() or raw.parent.is_symlink():
        raise MigrationError("既存の安全な親フォルダを指定してください")
    parent = raw.parent.resolve(strict=True)
    source = SOURCE_ROOT.resolve(strict=True)
    target = parent / raw.name
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise MigrationError("VoicePilotの既存インストールを上書きできません")
    if target.exists() or target.is_symlink():
        raise MigrationError("導入先が既に存在します")
    return target


def prepare_signed_migration(zip_path: Path, signature_path: Path,
                             public_key_hex: str, fingerprint: str,
                             *, destination: Path | None = None,
                             confirmed: bool = False) -> tuple[VerifiedBundle, Path | None]:
    """Verify offline by default; prepare a NEW folder only with opt-in consent.

    Copy source ZIP to a private scratch location first, then authenticate
    that copy. A source file swap cannot invalidate the extracted bytes.
    """
    source_zip = Path(zip_path)
    signature = Path(signature_path)
    if (source_zip.is_symlink() or signature.is_symlink() or
            not source_zip.is_file() or not signature.is_file()):
        raise MigrationError("署名済みZIPと署名ファイルを指定してください")
    if source_zip.stat().st_size > MAX_ARCHIVE_BYTES:
        raise MigrationError("ZIPがサイズ上限を超えています")
    if destination is not None:
        _assert_windows_nonadmin()
        if not confirmed:
            raise MigrationError("明示的な導入承認が必要です")
        target = _safe_destination(destination)
        parent = target.parent
    else:
        target = None
        parent = None

    with tempfile.TemporaryDirectory(prefix="vp-signed-migration-", dir=parent) as scratch:
        temporary = Path(scratch)
        # Copy to the expected canonical name, avoiding user-controlled paths
        # inside the ZIP. We do not execute any file from the candidate.
        name = source_zip.name
        if _VERSIONED_ZIP.fullmatch(name) is None:
            raise MigrationError("ZIP名が正規形式ではありません")
        local = temporary / name
        shutil.copyfile(source_zip, local)
        if local.stat().st_size > MAX_ARCHIVE_BYTES:
            raise MigrationError("ZIPがサイズ上限を超えています")
        unpacked = temporary / "prepared"
        result = _verify_archive_copy(local, signature, public_key_hex,
                                      fingerprint, unpacked)
        if target is None:
            return result, None
        # No upgrade of the old folder, no copied preferences or credentials,
        # no pip install or Python execution. On Windows os.rename fails if
        # the destination already exists; fail rather than overwrite.
        if target.exists() or target.is_symlink():
            raise MigrationError("導入先が作成されました。上書きを拒否します")
        try:
            os.rename(unpacked, target)
        except OSError as exc:
            raise MigrationError("別フォルダへの配置に失敗しました。既存環境は未変更です") from exc
        return result, target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline verification of an owner-signed VoicePilot ZIP; new-folder migration only by consent")
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--public-key-hex", required=True,
                        help="32-byte Ed25519 public key obtained from an independently trusted channel")
    parser.add_argument("--fingerprint", required=True,
                        help="SHA256 of the public key confirmed INDEPENDENTLY of the GitHub release")
    parser.add_argument("--install-to", type=Path, default=None,
                        help="New, currently nonexistent folder; never overwrites an installation")
    args = parser.parse_args(argv)
    try:
        if args.install_to is not None:
            print("既存VoicePilotは変更しません。新しい別フォルダだけを作成します。")
            print("署名鍵の指紋はGitHubとは別経路で本人確認しましたか？")
            if input("続行する場合だけ MIGRATE と入力: ").strip() != "MIGRATE":
                print("移行は中止しました。")
                return 2
        result, placed = prepare_signed_migration(
            args.zip, args.signature, args.public_key_hex, args.fingerprint,
            destination=args.install_to, confirmed=args.install_to is not None)
    except (MigrationError, OSError, ValueError) as exc:
        print("署名検証または安全な移行条件が満たされません。既存インストールは変更しません。", file=sys.stderr)
        return 2
    print("INDEPENDENT_SIGNATURE_VERIFIED", result.version)
    print("PUBLIC_KEY_FINGERPRINT_VERIFIED", result.public_key_fingerprint)
    if placed is not None:
        print("新しい別フォルダへの配置が完了しました。起動・依存関係・Chrome連携は未検証です。")
    else:
        print("検証のみ完了。インストール先は作成していません。")
    print("正式運用の合格判定ではありません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
