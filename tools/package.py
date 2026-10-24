#!/usr/bin/env python3
"""Build dist/BeamNG.Earth-<VERSION>.zip, the download for players.

    python3 tools/package.py [--out DIR] [--beammp]

The zip has one top folder, BeamNG.Earth-<VERSION>/, with the mod, the server (no tests, no
__pycache__), the installer and launchers, and the player docs. The version is the VERSION file.

--beammp also builds dist/BeamNG.Earth-BeamMP-Server-<VERSION>.zip: unzipped into a BeamMP server's folder it adds
Resources/Client/beamng_earth.zip (the mod, sent to every player who joins), Resources/Server/BeamNGEarth (the
plugin, beammp/Server) and the server's README (beammp/README.md).
"""

import argparse
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FILES = ["install.py", "start.py", "launcher.py", "bmplaunch.py", "run_server.bat", "run_server.sh", "README.md", "LICENSE", "CREDITS.md",
         "VERSION", "docs/QUICKSTART.md"]
TREES = ["mod/beamng_earth", "server"]
TOOLS = ["tools/reports.py"]                # user-facing helpers from tools/ (the in-game reports -> GitHub issues)
SKIP_DIRS = {"__pycache__", "tests", ".git", "backups", "probes", "research", ".venv", "dist"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".swp"}
EXECUTABLE = {"run_server.sh", "install.py", "start.py"}


def version():
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def collect():
    out = []
    for rel in FILES + TOOLS:
        if not os.path.isfile(os.path.join(ROOT, rel)):
            sys.exit(f"missing: {rel}")
        out.append(rel)
    for tree in TREES:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, tree)):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if os.path.splitext(name)[1] in SKIP_EXT or name == ".DS_Store":
                    continue
                out.append(os.path.relpath(os.path.join(dirpath, name), ROOT).replace(os.sep, "/"))
    return out


def tree_files(tree):
    """The files of a folder of the repo (relative to it), as collect() skips them."""
    out = []
    base = os.path.join(ROOT, tree)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if os.path.splitext(name)[1] in SKIP_EXT or name == ".DS_Store":
                continue
            out.append(os.path.relpath(os.path.join(dirpath, name), base).replace(os.sep, "/"))
    return out


def beammp_zip(out_dir, ver):
    """The BeamMP server's drop-in zip: the mod as Resources/Client/beamng_earth.zip, the plugin, the README."""
    import io
    mod = io.BytesIO()
    with zipfile.ZipFile(mod, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in tree_files("mod/beamng_earth"):
            z.write(os.path.join(ROOT, "mod/beamng_earth", rel), rel)
    path = os.path.join(out_dir, f"BeamNG.Earth-BeamMP-Server-{ver}.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("Resources/Client/beamng_earth.zip", mod.getvalue(), zipfile.ZIP_STORED)
        for rel in tree_files("beammp/Server"):
            z.write(os.path.join(ROOT, "beammp/Server", rel), f"Resources/Server/{rel}")
        z.write(os.path.join(ROOT, "beammp/README.md"), "BeamNG.Earth-README.md")
    print(f"{path}: {os.path.getsize(path) / 1e6:.1f} MB")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"), help="output folder (default: dist/)")
    ap.add_argument("--beammp", action="store_true", help="also the BeamMP server's drop-in zip")
    args = ap.parse_args()
    ver = version()
    top = f"BeamNG.Earth-{ver}"
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, top + ".zip")
    files = collect()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            src = os.path.join(ROOT, rel)
            info = zipfile.ZipInfo.from_file(src, f"{top}/{rel}")
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if os.path.basename(rel) in EXECUTABLE else 0o100644) << 16
            with open(src, "rb") as f:
                z.writestr(info, f.read())
    print(f"{path}: {len(files)} files, {os.path.getsize(path) / 1e6:.1f} MB")
    if args.beammp:
        beammp_zip(args.out, ver)


if __name__ == "__main__":
    main()
