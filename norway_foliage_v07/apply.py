r"""Reversible Norway foliage v0.7 addon for an existing v0.6 installation.

Open PowerShell in the BeamNG.Earth main directory:
    py .\norway_foliage_v07\apply.py --check
    py .\norway_foliage_v07\apply.py --install
    py .\norway_foliage_v07\apply.py --undo

Only mod/.../trees.lua is ever replaced. The server's v0.6 vegetation.py is
checked for compatibility, not modified. Unknown local modifications stop the
installer rather than being overwritten.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


PACK = Path(__file__).resolve().parent
TREE_REL = Path("mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua")
SERVER_REL = Path("server/vegetation.py")
V06_TREE_SHA = "eb345f3a80168039ff62d03bf7ae9910d1b6a76d94b5681cc637f3b470bb12ab"
V06_SERVER_SHA = "1a5fd024271ee49b29b5b00ff20b13f5653136ba4e8b29f20ac371671e605cb0"


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_copy(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".norway-v07-", dir=str(dest.parent))
    os.close(fd)
    temp = Path(name)
    try:
        shutil.copy2(source, temp)
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)


def atomic_manifest(dest, data):
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".norway-v07-", dir=str(dest.parent))
    os.close(fd)
    temp = Path(name)
    try:
        temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)


def status(root, backup, manifest):
    dest = root / TREE_REL
    server = root / SERVER_REL
    current = sha(dest) if dest.is_file() else None
    installed = sha(PACK / TREE_REL)
    server_ok = server.is_file() and sha(server) == V06_SERVER_SHA
    state = ("v0.7 installed" if current == installed else
             "v0.6 — ready" if current == V06_TREE_SHA else
             "file missing" if current is None else "modified / unsupported")
    print("Norway dense forest v0.7 —", root)
    print("  Trees:", state)
    print("  Server v0.6 compatible:", "yes" if server_ok else "NO")
    print("  v0.7 backup:", manifest["state"] if manifest else "not yet created")
    print("  Only this file will be changed:", TREE_REL)


def install(root, backup, manifest):
    dest = root / TREE_REL
    server = root / SERVER_REL
    if not server.is_file() or sha(server) != V06_SERVER_SHA:
        raise ValueError("Expected Norway foliage v0.6 server/vegetation.py; refusing to patch a different version")
    patched_sha = sha(PACK / TREE_REL)
    current = sha(dest) if dest.is_file() else None
    if manifest and manifest["state"] == "installed":
        if current != patched_sha:
            raise ValueError("v0.7 was modified after installation; preserving user changes")
        print("Already installed; no changes made")
        return
    if manifest and manifest["state"] != "restored":
        raise ValueError("Interrupted v0.7 installation; run --undo first")
    if backup.exists() and not manifest:
        raise ValueError("v0.7 backup directory exists without a manifest; investigate before installing")
    if current != V06_TREE_SHA:
        raise ValueError("Expected unmodified v0.6 trees.lua; refusing to overwrite custom or older files")

    saved = backup / "originals" / TREE_REL
    if saved.exists() and sha(saved) != current:
        raise ValueError("Previous backup differs from source; preserving backup")
    if not saved.exists():
        atomic_copy(dest, saved)
    info = {"state": "installing", "original_tree_sha": current,
            "patched_tree_sha": patched_sha, "server_sha": V06_SERVER_SHA}
    atomic_manifest(backup / "manifest.json", info)
    atomic_copy(PACK / TREE_REL, dest)
    info["state"] = "installed"
    atomic_manifest(backup / "manifest.json", info)
    print("Installed v0.7: bounded extra stock forest groups on distant Norwegian slopes.")
    print("No new assets, collisions, server changes, or other countries affected.")
    print("Restart BeamNG.drive to reload trees.lua. Undo with this same installer.")


def undo(root, backup, manifest):
    if not manifest:
        raise ValueError("No v0.7 installation record found")
    if manifest["state"] == "restored":
        print("Already restored to v0.6")
        return
    if manifest["state"] not in ("installed", "installing"):
        raise ValueError("Unrecognized v0.7 installation state")
    dest = root / TREE_REL
    saved = backup / "originals" / TREE_REL
    if not saved.is_file() or sha(saved) != manifest["original_tree_sha"]:
        raise ValueError("The original backup is missing or modified; stopping")
    if not dest.is_file() or sha(dest) not in (manifest["patched_tree_sha"], manifest["original_tree_sha"]):
        raise ValueError("trees.lua changed since install; preserving user edits")
    atomic_copy(saved, dest)
    manifest["state"] = "restored"
    atomic_manifest(backup / "manifest.json", manifest)
    print("Restored the exact v0.6 trees.lua. No other files were changed.")
    print("Restart BeamNG.drive.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--install", action="store_true")
    modes.add_argument("--undo", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    backup = root / "norway_foliage_v07_backup"
    try:
        if not (PACK / TREE_REL).is_file():
            raise ValueError("Extracted ZIP is incomplete: trees.lua missing")
        record = backup / "manifest.json"
        manifest = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else None
        status(root, backup, manifest)
        if args.install:
            install(root, backup, manifest)
        elif args.undo:
            undo(root, backup, manifest)
        else:
            print("Preflight complete. Run --install or --undo as needed.")
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print("STOPPED safely:", error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
