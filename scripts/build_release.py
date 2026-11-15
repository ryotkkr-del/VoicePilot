"""Build an update archive containing source, extension, and fixed bootstrap.

Usage: python scripts/build_release.py  (outputs dist/VoicePilot-vX.Y.Z.zip)
"""
from pathlib import Path
import json
import zipfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voicepilot_bootstrap import _ALLOWED_DIRS, _ALLOWED_FILES, _REQUIRED, _safe_name


def build(output_dir: Path | None = None) -> Path:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    from voicepilot_bootstrap import version_tuple
    version_tuple(version)
    # A mismatched extension version causes repeated reloads, stale protocol
    # assumptions, and misleading compatibility checks on the user's PC.
    # Never ship or auto-update a build with inconsistent components.
    manifest = json.loads((ROOT / "chrome-extension" / "manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("manifest_version") != 3
            or manifest.get("version") != version):
        raise RuntimeError("Chrome拡張のバージョンとVERSIONが一致しません")
    files = sorted(
        p for p in ROOT.rglob("*") if p.is_file()
        and not p.is_symlink() and (
            p.relative_to(ROOT).as_posix() in _ALLOWED_FILES or
            p.relative_to(ROOT).as_posix().startswith(_ALLOWED_DIRS)
        ) and not any(part.startswith(".") or part in {"__pycache__", "node_modules"}
                      for part in p.relative_to(ROOT).parts)
        and p.suffix in {".py", ".js", ".json", ".html", ".md", ".txt", ".bat"} or p == ROOT / "VERSION"
    )
    included = {p.relative_to(ROOT).as_posix() for p in files}
    if not _REQUIRED.issubset(included):
        raise RuntimeError(f"Missing files: {_REQUIRED - included}")
    output = output_dir or ROOT / "dist"
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"VoicePilot-v{version}.zip"
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            name = _safe_name(p.relative_to(ROOT).as_posix())
            zf.write(p, arcname=name)
    print(artifact)
    return artifact


if __name__ == "__main__":
    build()