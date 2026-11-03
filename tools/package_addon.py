#!/usr/bin/env python3
"""Package a BeamNG.Earth addon directory (e.g., addons/beamng_earth_provence) into dist/<name>.zip
as a valid drop-in BeamNG mod archive and print its canonical SHA-256 approval digest.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import addons as addons_mod  # noqa: E402


def package_addon(addon_dir: str | Path, out_dir: str | Path | None = None) -> tuple[Path, str]:
    """Package `addon_dir` into `<out_dir>/<name>.zip` and return `(zip_path, canonical_sha256_digest)`."""
    src_dir = Path(addon_dir).resolve()
    if not src_dir.is_dir():
        candidate = REPO_ROOT / "addons" / str(addon_dir)
        if candidate.is_dir():
            src_dir = candidate.resolve()
        else:
            raise FileNotFoundError(f"Addon directory not found: {addon_dir}")

    payload = addons_mod.load_addon_from_dir(src_dir)
    valid, errors = addons_mod.validate_addon_manifest(payload)
    if not valid:
        raise ValueError(f"Invalid addon manifest in {src_dir}: {'; '.join(errors)}")

    digest = addons_mod.compute_addon_digest(payload)

    dest_root = Path(out_dir).resolve() if out_dir is not None else (REPO_ROOT / "dist")
    dest_root.mkdir(parents=True, exist_ok=True)
    zip_path = dest_root / f"{src_dir.name}.zip"

    files_to_pack: list[tuple[Path, str]] = []
    for root, dirs, files in os.walk(src_dir):
        dirs.sort()
        for fname in sorted(files):
            if fname.startswith(".") or fname.endswith((".pyc", ".pyo")):
                continue
            fpath = Path(root) / fname
            rel_arc = fpath.relative_to(src_dir).as_posix()
            files_to_pack.append((fpath, rel_arc))

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fpath, rel_arc in files_to_pack:
            zinfo = zipfile.ZipInfo(rel_arc, date_time=(2026, 1, 1, 0, 0, 0))
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.external_attr = 0o644 << 16
            zf.writestr(zinfo, fpath.read_bytes())

    return zip_path, digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package a BeamNG.Earth native addon into dist/<name>.zip")
    parser.add_argument(
        "addon",
        nargs="?",
        default="beamng_earth_provence",
        help="Addon folder name under addons/ or path to addon directory (default: beamng_earth_provence)",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for the packaged .zip (default: dist/)",
    )
    args = parser.parse_args(argv)
    zip_path, digest = package_addon(args.addon, args.out_dir)
    print(f"Packaged addon: {zip_path}")
    print(f"Approval SHA-256 digest: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
