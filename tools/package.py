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
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FILES = ["install.py", "start.py", "launcher.py", "bmplaunch.py", "apppaths.py", "run_server.bat", "run_server.sh", "README.md", "LICENSE", "CREDITS.md", "TELEMETRY.md",
         "VERSION", "docs/QUICKSTART.md", "docs/MODERNIZATION.md", "docs/MODERNIZATION_VALIDATION.md",
         "docs/SKETCHFAB_ASSETS.md", "docs/SHARED_WORLD.md", "docs/RELAUNCH_TROUBLESHOOTING.md", "docs/STABILITY_VALIDATION.md",
         "docs/INITIAL_LOADING_VALIDATION.md",
         "docs/CLASS_GRID_CACHE.md",
         "docs/SUBSEA_TUNNEL_VALIDATION.md", "docs/licenses/Clarabel-LICENSE.txt",
         "docs/PLATFORM_STATUS.md", "docs/RELEASE_1_7_4.md", "docs/RELEASE_1_8_1.md",
         "docs/research/2026-11-02-range-cache.md", "docs/research/2026-11-02-world-bundle.md",
         "docs/research/2026-11-02-world-bundle.json", "docs/research/2026-11-02-release-1.8.1-world-bundle.json",
         "beammp/MODERNIZATION.md"]
TREES = ["mod/beamng_earth", "server"]
TOOLS = ["tools/reports.py", "tools/sketchfab_assets.py", "tools/world_cache.py"]  # no stored credentials
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


def past_stamp(mtime):
    """A zip entry's time for the game's mod zip: mtime - 14 h as UTC wall time. Zip times have no time zone and the
    game reads them as local time; built on a UTC machine (the release workflow) they were hours in the future west
    of UTC, and the game re-packed every terrain texture whose source looked newer than its cache at every block swap
    (17-25 s freezes in a BeamMP session right after the 1.8.0 release, 2026-11-04). 14 h back is in the past in every
    time zone (UTC-12 .. +14) and still newer than the last release's caches."""
    import time
    return time.gmtime(mtime - 14 * 3600)[:6]


def beammp_zip(out_dir, ver):
    """The BeamMP server's drop-in zip: the mod as Resources/Client/beamng_earth.zip, the plugin, the README."""
    import io
    mod = io.BytesIO()
    with zipfile.ZipFile(mod, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in tree_files("mod/beamng_earth"):
            src = os.path.join(ROOT, "mod/beamng_earth", rel)
            info = zipfile.ZipInfo(rel, past_stamp(os.path.getmtime(src)))
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(src, "rb") as f:
                z.writestr(info, f.read())
        # (the release this copy is: its hello names it, and a player's app whose version differs says so: a BeamMP
        # server's mod replaces the players' own, and a mismatch loaded the world the slow way unseen, 2026-11-07)
        info = zipfile.ZipInfo("lua/ge/extensions/beamngEarth/release.lua", past_stamp(time.time()))
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, f"return {ver!r}\n")
    path = os.path.join(out_dir, f"BeamNG.Earth-BeamMP-Server-{ver}.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("Resources/Client/beamng_earth.zip", mod.getvalue(), zipfile.ZIP_STORED)
        for rel in tree_files("beammp/Server"):
            z.write(os.path.join(ROOT, "beammp/Server", rel), f"Resources/Server/{rel}")
        z.write(os.path.join(ROOT, "beammp/README.md"), "BeamNG.Earth-README.md")
        z.write(os.path.join(ROOT, "LICENSE"), "LICENSE-Earth.txt")
        z.write(os.path.join(ROOT, "CREDITS.md"), "CREDITS-Earth.md")
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
            info = zipfile.ZipInfo(f"{top}/{rel}", past_stamp(os.path.getmtime(src)))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if os.path.basename(rel) in EXECUTABLE else 0o100644) << 16
            with open(src, "rb") as f:
                z.writestr(info, f.read())
    print(f"{path}: {len(files)} files, {os.path.getsize(path) / 1e6:.1f} MB")
    if args.beammp:
        beammp_zip(args.out, ver)


if __name__ == "__main__":
    main()
