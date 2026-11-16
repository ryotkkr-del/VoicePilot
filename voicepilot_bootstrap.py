"""VoicePilot v1 bootstrap: GitHub Release downloader and recoverable installer.

This small, dependency-free module is intentionally NOT updated in-place.  It
runs before app.main and can recover even if a downloaded Python module fails.
Only *published* releases from one pinned GitHub repository are eligible.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import unicodedata
import zipfile

BOOTSTRAP_SECURITY_REVISION = 3  # Ed25519-signed update gate (unconfigured until developer pins their key)
# The signing key must be authenticated OFFLINE and embedded in this immutable
# bootstrap during an explicit, trusted installation. Never load a key from
# the GitHub release, unsigned config, environment variable or updated app.
PINNED_SIGNER_PUBLIC_KEY_B64 = ""
# Keep an authenticated ZIP outside the application update file list so the
# staged file hashes can be reconstructed at apply time without trusting a
# user-writable pending.json manifest.
_SIGNED_ARCHIVE_NAME = "_signed_release.zip"

REPO = "ryotkkr-del/VoicePilot"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
MAX_FILES = 400
CHECK_INTERVAL = 6 * 60 * 60
_REQUIRED = {"VERSION", "voicepilot_bootstrap.py", "setup_and_run.bat", "app/main.py", "app/__init__.py", "chrome-extension/manifest.json", "requirements.txt"}
_ALLOWED_DIRS = ("app/", "chrome-extension/", "tests/", "tests_js/")
_ALLOWED_FILES = {"VERSION", "requirements.txt", "README.md", "README_JA.md", "run_tests.bat",
                  "voicepilot_bootstrap.py", "setup_and_run.bat", "rollback_update.bat"}
_BOOTSTRAP_FILES = {"voicepilot_bootstrap.py", "setup_and_run.bat", "rollback_update.bat"}
_TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA = re.compile(r"^sha256:([0-9a-f]{64})$")


class UpdateError(Exception):
    """Nonfatal updater error. VoicePilot should still start using local files."""


def version_tuple(value: str) -> tuple[int, int, int]:
    value = value.strip()
    match = _TAG.fullmatch(value if value.startswith("v") else "v" + value)
    if not match:
        raise UpdateError("バージョン番号が不正です")
    return tuple(map(int, match.groups()))


def app_version(root: Path) -> str:
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    return base / "VoicePilot" / "updates"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise UpdateError("更新情報が不正です")
    return result


def _api_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "VoicePilot-Updater/1",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status != 200:
            raise UpdateError("GitHubの応答が不正です")
        payload = resp.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise UpdateError("更新情報が大きすぎます")
    obj = json.loads(payload)
    if not isinstance(obj, dict):
        raise UpdateError("更新情報が不正です")
    return obj


def _signed_payload(tag: str, digest: str) -> bytes:
    """Canonical and domain-separated Ed25519 message (no external JSON)."""
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise UpdateError("署名対象のバージョンが不正です")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError("署名対象のSHA-256が不正です")
    asset = f"VoicePilot-{tag}.zip"
    return (f"VoicePilot signed release v1\n{REPO}\n{tag}\n{asset}\n{digest}\n").encode("ascii")


def _verify_signed_release(tag: str, digest: str, signature_b64: str) -> str:
    """Reject updates unless their digest was signed by the PINNED developer key.

    GitHub's asset digest is a transport checksum, not this trust anchor.
    The bootstrap is intentionally immutable; empty/invalid pin fails closed.
    """
    if not isinstance(PINNED_SIGNER_PUBLIC_KEY_B64, str) or not PINNED_SIGNER_PUBLIC_KEY_B64:
        raise UpdateError("開発者公開鍵が未設定です。署名済み更新を検証できないため自動更新を停止しました")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise UpdateError("署名検証の依存ライブラリがありません。自動更新を停止しました") from exc
    try:
        public = base64.b64decode(PINNED_SIGNER_PUBLIC_KEY_B64, validate=True)
        if len(public) != 32:
            raise ValueError("wrong public key length")
        if not isinstance(signature_b64, str):
            raise ValueError("no signature")
        sig = base64.b64decode(signature_b64, validate=True)
        if len(sig) != 64 or base64.b64encode(sig).decode("ascii") != signature_b64:
            raise ValueError("noncanonical signature")
        Ed25519PublicKey.from_public_bytes(public).verify(sig, _signed_payload(tag, digest))
    except (InvalidSignature, binascii.Error, UnicodeError, ValueError) as exc:
        raise UpdateError("リリースの開発者署名が無効です。更新を拒否しました") from exc
    return signature_b64


def _download_signature(url: str) -> str:
    """Read only the fixed GitHub signature sidecar, max 128 ASCII bytes."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or not parsed.path.startswith(f"/{REPO}/releases/download/"):
        raise UpdateError("開発者署名の配信元が不正です")
    request = urllib.request.Request(url, headers={"User-Agent": "VoicePilot-Updater/1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = response.read(129)
    if len(payload) > 128:
        raise UpdateError("開発者署名が大きすぎます")
    try:
        return payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise UpdateError("開発者署名の形式が不正です") from exc


def _download(url: str, destination: Path) -> str:
    # Only a release asset belonging to the pinned repository is accepted.
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or not parsed.path.startswith(f"/{REPO}/releases/download/"):
        raise UpdateError("許可されていない配信元です")
    request = urllib.request.Request(url, headers={"User-Agent": "VoicePilot-Updater/1"})
    h = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=15) as response, open(destination, "wb") as out:
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise UpdateError("更新ファイルが大きすぎます")
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest()


def _safe_name(name: str) -> str:
    if "\\" in name or "\x00" in name or not name or name.startswith("/"):
        raise UpdateError("更新アーカイブのパスが不正です")
    path = PurePosixPath(name)
    if any(p in (".", "..", "") for p in name.split("/")) or path.as_posix() != name:
        raise UpdateError("更新アーカイブのパスが不正です")
    if len(path.parts) > 8 or any(p.startswith(".") for p in path.parts):
        raise UpdateError("隠しファイルや深すぎるパスを含む更新は許可しません")
    # ZIP path rules must be validated with Windows semantics even when
    # running unit tests on Linux. NTFS supports case-insensitive names and
    # alternative data streams (the colon is not an ordinary filename char).
    # Reject Win32 reserved devices and other ambiguous paths *before*
    # extraction, so nothing can alias or redirect a release file.
    for part in path.parts:
        if (part.endswith((" ", ".")) or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or re.fullmatch(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)):
            raise UpdateError("Windowsで安全に扱えない更新パスです")
    if not (name in _ALLOWED_FILES or name.startswith(_ALLOWED_DIRS)):
        raise UpdateError(f"未許可の更新ファイルです: {name[:80]}")
    if not (name.endswith((".py", ".js", ".json", ".html", ".md", ".txt", ".bat")) or name == "VERSION"):
        raise UpdateError("未許可のファイル形式です")
    return name


def extract_checked(archive: Path, folder: Path, expected_version: str) -> dict[str, str]:
    """Reject archive tricks before writing any extracted file."""
    hashes = {}
    seen_windows_names: set[str] = set()
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise UpdateError("更新ファイル数が上限を超えています")
        total = 0
        for entry in infos:
            if entry.is_dir():
                continue
            name = _safe_name(entry.filename)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise UpdateError("シンボリックリンクは許可していません")
            normalized = unicodedata.normalize("NFC", name).casefold()
            if name in hashes or normalized in seen_windows_names:
                raise UpdateError("Windows上で重複する更新ファイル名です")
            seen_windows_names.add(normalized)
            total += entry.file_size
            if total > MAX_UNPACKED_BYTES:
                raise UpdateError("展開後のサイズが大きすぎます")
            hashes[name] = ""
        if not _REQUIRED.issubset(hashes):
            raise UpdateError("更新パッケージの必要ファイルが不足しています")
        for name in sorted(hashes):
            destination = folder / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            hasher = hashlib.sha256()
            with zf.open(name) as src, open(destination, "xb") as dst:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    dst.write(chunk)
            hashes[name] = hasher.hexdigest()
    if (folder / "VERSION").read_text(encoding="utf-8").strip() != expected_version:
        raise UpdateError("パッケージのバージョンが一致しません")
    return hashes


def check_and_stage(root: Path, cache: Path | None = None, *, force: bool = False,
                    fetch_release=None, download=None, download_signature=None) -> str:
    """Network only at idle: check, validate, and stage. Never modify live code."""
    cache = cache or data_dir()
    root = Path(root).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    state = cache / "checked.json"
    if not force and state.exists():
        try:
            if 0 <= time.time() - float(_read_json(state)["timestamp"]) < CHECK_INTERVAL:
                return "skipped"
        except (ValueError, KeyError, OSError, json.JSONDecodeError, UpdateError):
            pass
    try:
        release = (fetch_release or _api_get)(API)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _atomic_json(state, {"timestamp": time.time()})
            return "no-release"
        raise UpdateError("GitHubへ接続できません") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise UpdateError("GitHubへ接続できません") from exc
    if release.get("draft") or release.get("prerelease"):
        return "no-release"
    tag = release.get("tag_name", "")
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise UpdateError("リリースのバージョンが不正です")
    version = tag[1:]
    current = app_version(root)
    blocked_path = cache / "blocked.json"
    if blocked_path.exists():
        try:
            if _read_json(blocked_path).get("version") == version:
                return "blocked"
        except (OSError, ValueError, UpdateError):
            pass
    if version_tuple(version) <= version_tuple(current):
        _atomic_json(state, {"timestamp": time.time()})
        return "current"
    pending_path = cache / "pending.json"
    if pending_path.exists():
        try:
            prior = _read_json(pending_path)
            if version_tuple(prior["version"]) >= version_tuple(version):
                # Never trust a pending stage prepared by an older unsigned
                # bootstrap or by an interrupted/unknown updater.
                _verify_signed_release("v" + prior["version"], prior["sha256"],
                                       prior["signature_b64"])
                _atomic_json(state, {"timestamp": time.time()})
                return "staged"
        except (ValueError, KeyError, UpdateError):
            pass
    asset_name = f"VoicePilot-v{version}.zip"
    assets = [x for x in release.get("assets", []) if x.get("name") == asset_name and x.get("state") == "uploaded"]
    if len(assets) != 1:
        raise UpdateError("対応する更新パッケージがありません")
    asset = assets[0]
    digest = asset.get("digest")
    match = _SHA.fullmatch(digest if isinstance(digest, str) else "")
    if not match:
        raise UpdateError("GitHub発行のSHA-256ハッシュがありません")
    expected = match.group(1)
    asset_url = asset.get("browser_download_url", "")
    if not isinstance(asset_url, str) or asset_url != f"https://github.com/{REPO}/releases/download/{tag}/{asset_name}":
        raise UpdateError("更新ファイルのURLが不正です")
    # Verify the independent developer signature BEFORE downloading/placing
    # executable release bytes. Never fallback to GitHub SHA-256 alone.
    signature_name = asset_name + ".sig"
    sig_assets = [x for x in release.get("assets", [])
                  if x.get("name") == signature_name and x.get("state") == "uploaded"]
    if len(sig_assets) != 1:
        raise UpdateError("署名ファイルがありません。署名のない更新は拒否します")
    expected_sig_url = f"https://github.com/{REPO}/releases/download/{tag}/{signature_name}"
    if sig_assets[0].get("browser_download_url") != expected_sig_url:
        raise UpdateError("開発者署名のURLが不正です")
    signature_b64 = (download_signature or _download_signature)(expected_sig_url)
    signature_b64 = _verify_signed_release(tag, expected, signature_b64)
    previous_stage = None
    if pending_path.exists():
        try:
            old = _read_json(pending_path).get("stage")
            if isinstance(old, str) and re.fullmatch(r"stage-[0-9a-f]{32}", old):
                previous_stage = cache / old
        except (OSError, ValueError):
            pass
    temp_folder = Path(tempfile.mkdtemp(prefix="download-", dir=cache))
    try:
        archive = temp_folder / "release.zip"
        observed = (download or _download)(asset_url, archive)
        if observed != expected:
            raise UpdateError("更新ファイルのSHA-256が一致しません")
        extracted = temp_folder / "files"
        extracted.mkdir()
        hashes = extract_checked(archive, extracted, version)
        # Use an opaque stage identifier and don't trust any path stored in metadata.
        stage_id = "stage-" + uuid.uuid4().hex
        stage = cache / stage_id
        os.replace(extracted, stage)
        # Preserve the EXACT ZIP whose digest is covered by the developer's
        # signature. On apply, reconstruct the trusted file hashes from this
        # ZIP to prevent altering staged files and pending.json together.
        shutil.copyfile(archive, stage / _SIGNED_ARCHIVE_NAME)
        _atomic_json(pending_path, {"version": version, "stage": stage_id,
                                   "sha256": expected, "signature_b64": signature_b64,
                                   "files": hashes})
    finally:
        shutil.rmtree(temp_folder, ignore_errors=True)
    _atomic_json(state, {"timestamp": time.time()})
    if previous_stage is not None and previous_stage != stage and not previous_stage.is_symlink():
        shutil.rmtree(previous_stage, ignore_errors=True)
    return "downloaded"


def _ensure_inside(root: Path, relative: str) -> Path:
    name = _safe_name(relative)
    target = root / name
    # Do not overwrite symlinks or traverse symlinked parents.
    for obj in [root, *[root.joinpath(*PurePosixPath(name).parts[:i]) for i in range(1, len(PurePosixPath(name).parts) + 1)]]:
        if obj.is_symlink():
            raise UpdateError("シンボリックリンクをたどる更新は許可できません")
    return target


def _restore(root: Path, backup: Path, files: list[dict]) -> None:
    """Validate *every* backup before restoring any installed file.

    A corrupt or missing backup late in the list must not cause a partial
    rollback of earlier files. New-format snapshots bind each existing file
    to its SHA-256; legacy snapshots without hashes remain readable so an
    interrupted older installation is still recoverable.
    """
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise UpdateError("復元ファイル一覧が不正です")
    if backup.is_symlink() or not backup.is_dir():
        raise UpdateError("バックアップ保存先が不正です")
    prepared = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or type(item.get("existed")) is not bool:
            raise UpdateError("復元ファイル情報が不正です")
        name = _safe_name(item["name"])
        folded = unicodedata.normalize("NFC", name).casefold()
        if folded in seen:
            raise UpdateError("復元ファイル名が重複しています")
        seen.add(folded)
        target = _ensure_inside(root, name)
        original = _ensure_inside(backup, name)
        if target.exists() and not target.is_file():
            raise UpdateError("復元先がファイルではありません")
        expected = item.get("backup_sha256")
        if expected is not None and (not isinstance(expected, str)
                                     or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise UpdateError("復元ファイルの検証値が不正です")
        if item["existed"]:
            if not original.is_file():
                raise UpdateError("バックアップファイルがありません")
            if expected is not None and hashlib.sha256(original.read_bytes()).hexdigest() != expected:
                raise UpdateError("バックアップの内容が変わっています")
        elif expected is not None:
            raise UpdateError("存在しないファイルに検証値は設定できません")
        prepared.append((target, original, item["existed"], expected))
    # All source files and destinations have passed preflight. Verify again
    # after copying each backup to a temporary file, before os.replace.
    for target, original, existed, expected in reversed(prepared):
        if existed:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".vp-restore-", delete=False) as tmp:
                tmpfile = Path(tmp.name)
            try:
                shutil.copyfile(original, tmpfile)
                if expected is not None and hashlib.sha256(tmpfile.read_bytes()).hexdigest() != expected:
                    raise UpdateError("復元中にバックアップが変更されました")
                os.replace(tmpfile, target)
            finally:
                tmpfile.unlink(missing_ok=True)
        else:
            target.unlink(missing_ok=True)


def recover_interrupted(root: Path, cache: Path | None = None) -> bool:
    cache = cache or data_dir()
    journal = cache / "journal.json"
    if not journal.exists():
        return False
    data = _read_json(journal)
    backup_id = data.get("backup", "")
    if not re.fullmatch(r"backup-[0-9a-f]{32}", backup_id):
        raise UpdateError("回復情報が不正です")
    backup = cache / backup_id
    files = data.get("files", [])
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise UpdateError("回復情報が不正です")
    _restore(root, backup, files)
    # If a crash happened after the new last_good record was written but
    # before journal removal, that record refers to a version just reverted.
    # Never leave it advertised as a valid rollback point.
    state = cache / "last_good.json"
    if state.exists():
        current = _read_json(state)
        if current.get("backup") == backup_id:
            state.unlink()
    journal.unlink()
    (cache / "pending.json").unlink(missing_ok=True)
    return True


def apply_pending(root: Path, cache: Path | None = None) -> str:
    """Run only in the launcher before starting the VoicePilot GUI."""
    root = Path(root).resolve()
    cache = cache or data_dir()
    cache.mkdir(parents=True, exist_ok=True)
    if recover_interrupted(root, cache):
        return "recovered"
    pending_file = cache / "pending.json"
    if not pending_file.exists():
        return "none"
    # The local browser bridge is held by the running desktop app. Never
    # replace source files while another VoicePilot instance is alive.
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.2):
            raise UpdateError("VoicePilotが起動中です。終了してから更新してください")
    except (ConnectionRefusedError, TimeoutError, OSError):
        pass
    pending = _read_json(pending_file)
    version = pending.get("version", "")
    stage_id = pending.get("stage", "")
    if not isinstance(version, str) or not _TAG.fullmatch("v" + version) or not re.fullmatch(r"stage-[0-9a-f]{32}", stage_id):
        pending_file.unlink(missing_ok=True)
        raise UpdateError("更新ステージ情報が壊れています")
    if version_tuple(version) <= version_tuple(app_version(root)):
        pending_file.unlink(missing_ok=True)
        return "current"
    # This is a second signature gate: an older bootstrap may have staged an
    # unsigned package. Refuse before touching live code.
    signed_digest = pending.get("sha256")
    _verify_signed_release("v" + version, signed_digest,
                           pending.get("signature_b64"))
    stage = cache / stage_id
    if stage.is_symlink():
        raise UpdateError("更新ステージのリンクは許可できません")
    hashes = pending.get("files")
    if not isinstance(hashes, dict) or len(hashes) > MAX_FILES or not _REQUIRED.issubset(hashes):
        raise UpdateError("更新ファイル一覧が不正です")
    # The cache is user-writable. Both staged bytes and pending.json hashes
    # can be modified together; a signature on the ZIP digest alone would
    # not protect the staged directory. Reconstruct from the signed archive.
    signed_archive = stage / _SIGNED_ARCHIVE_NAME
    if (signed_archive.is_symlink() or not signed_archive.is_file()
            or signed_archive.stat().st_size > MAX_ARCHIVE_BYTES):
        raise UpdateError("署名付きの更新ZIPが見つからないか不正です")
    if hashlib.sha256(signed_archive.read_bytes()).hexdigest() != signed_digest:
        raise UpdateError("署名済みZIPのSHA-256が一致しません")
    with tempfile.TemporaryDirectory(prefix="signed-check-", dir=cache) as temp_dir:
        trusted_hashes = extract_checked(signed_archive, Path(temp_dir), version)
    if hashes != trusted_hashes:
        raise UpdateError("更新ファイル一覧が署名済みZIPと一致しません")
    # Verify the complete staged package before touching a single installed file.
    for name, digest in hashes.items():
        target = _ensure_inside(root, name)
        staged = _ensure_inside(stage, name)
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not staged.is_file() or hashlib.sha256(staged.read_bytes()).hexdigest() != digest):
            raise UpdateError(f"更新用ファイルの検証に失敗しました: {name}")
        if target.exists() and not target.is_file():
            raise UpdateError(f"更新先がファイルではありません: {name}")
    backup_id = "backup-" + uuid.uuid4().hex
    backup = cache / backup_id
    backup.mkdir()
    ordered = sorted((n for n in hashes if n not in _BOOTSTRAP_FILES and n != "VERSION")) + ["VERSION"]
    journal = {"backup": backup_id, "files": []}
    prior = cache / "last_good.json"
    previous_backup = None
    if prior.exists():
        try:
            old = _read_json(prior).get("backup")
            if isinstance(old, str) and re.fullmatch(r"backup-[0-9a-f]{32}", old):
                previous_backup = cache / old
        except (OSError, ValueError):
            pass
    try:
        # Prepare *all* backups before modifying any installed file.
        # Store a digest of each backup in the recovery journal so broken
        # or replaced snapshots are caught before a future rollback.
        for name in ordered:
            target = _ensure_inside(root, name)
            existed = target.is_file()
            entry = {"name": name, "existed": existed}
            if existed:
                saved = _ensure_inside(backup, name)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
                entry["backup_sha256"] = hashlib.sha256(saved.read_bytes()).hexdigest()
            journal["files"].append(entry)
        _atomic_json(cache / "journal.json", journal)
        for name in ordered:
            target = _ensure_inside(root, name)
            staged = _ensure_inside(stage, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".vp-update-", delete=False) as handle:
                temp_path = Path(handle.name)
            try:
                shutil.copyfile(staged, temp_path)
                # Another local process could alter the stage after its first
                # hash check. Never install bytes that differ from the digest.
                if hashlib.sha256(temp_path.read_bytes()).hexdigest() != hashes[name]:
                    raise UpdateError("適用直前に更新ファイルが変更されました")
                os.replace(temp_path, target)
            finally:
                temp_path.unlink(missing_ok=True)
        # Commit the rollback metadata *before* removing the journal. If this
        # write fails, the journal still exists and we can restore old code.
        _atomic_json(prior, {"backup": backup_id, "files": journal["files"],
                             "from": app_version(backup), "to": version})
    except Exception as exc:
        try:
            _restore(root, backup, journal["files"])
        except Exception as restore_error:
            # Never discard the journal when recovery is incomplete.
            raise UpdateError("更新後の復元に失敗しました。起動せず復旧してください") from restore_error
        # An interrupted or faulty metadata write might have replaced
        # last_good just before reporting an error. After restoring the old
        # source, that new record must never remain a valid rollback marker.
        if prior.exists():
            try:
                if _read_json(prior).get("backup") == backup_id:
                    prior.unlink()
            except (OSError, ValueError, UpdateError):
                # Keep the journal if recovery metadata cannot be inspected.
                raise UpdateError("更新記録が不正なため起動を停止しました") from exc
        (cache / "journal.json").unlink(missing_ok=True)
        raise UpdateError("更新失敗のため旧版を復元しました") from exc
    (cache / "journal.json").unlink(missing_ok=True)
    if previous_backup and previous_backup != backup and not previous_backup.is_symlink():
        shutil.rmtree(previous_backup, ignore_errors=True)
    pending_file.unlink(missing_ok=True)
    shutil.rmtree(stage, ignore_errors=True)
    return "applied"

def rollback(root: Path, cache: Path | None = None) -> str:
    root = Path(root).resolve()
    cache = cache or data_dir()
    if recover_interrupted(root, cache):
        return "recovered"
    state_file = cache / "last_good.json"
    if not state_file.exists():
        return "none"
    state = _read_json(state_file)
    backup_id = state.get("backup", "")
    if not re.fullmatch(r"backup-[0-9a-f]{32}", backup_id):
        raise UpdateError("復元情報が不正です")
    # Keep the failed version for diagnosis, but do not execute it.
    _restore(root, cache / backup_id, state["files"])
    _atomic_json(cache / "blocked.json", {"version": state["to"]})
    state_file.unlink()
    (cache / "pending.json").unlink(missing_ok=True)
    return "rolled-back"


def main() -> int:
    parser = argparse.ArgumentParser(description="VoicePilot safe Release updater")
    parser.add_argument("mode", choices=["apply", "check", "rollback", "deps-needed", "deps-mark"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    try:
        if args.mode in {"deps-needed", "deps-mark"}:
            digest = hashlib.sha256((root / "requirements.txt").read_bytes()).hexdigest()
            mark = root / ".venv" / "requirements.sha256"
            if args.mode == "deps-needed":
                return 0 if mark.exists() and mark.read_text(encoding="utf-8").strip() == digest else 1
            mark.parent.mkdir(parents=True, exist_ok=True)
            mark.write_text(digest + "\n", encoding="utf-8")
            return 0
        if args.mode == "apply":
            result = apply_pending(root)
        elif args.mode == "rollback":
            result = rollback(root)
        else:
            result = check_and_stage(root, force=True)
        print(f"[VoicePilot] updater: {result}")
        return 0
    except (UpdateError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"[VoicePilot] updater: {exc}")
        if args.mode == "apply" and not (data_dir() / "journal.json").exists():
            # A rejected/invalid download did not modify live code; discard it
            # and keep VoicePilot usable offline. Do not loop on the same stage.
            (data_dir() / "pending.json").unlink(missing_ok=True)
            return 0
        # A journal means we may have partially written code. Fail closed.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())