r"""Reversible Norway NVDB tunnel integration, invoked from BeamNG.Earth root.

    py .\norway_tunnels_nvdb_v025\apply.py --check
    py .\norway_tunnels_nvdb_v025\apply.py --install
    py .\norway_tunnels_nvdb_v025\apply.py --undo
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent
TARGET = ROOT / "server" / "norway_tunnels.py"
NEW = ROOT / "server" / "norway_tunnel_nvdb.py"
SOURCE = PACKAGE / "server" / "norway_tunnels.py"
NEW_SOURCE = PACKAGE / "server" / "norway_tunnel_nvdb.py"
BACKUP_DIR = ROOT / "norway_tunnels_nvdb_v025_backup"
BACKUP = BACKUP_DIR / "norway_tunnels.py"
STATE = BACKUP_DIR / "state.json"
SOURCES = {
    "unfinished v0": "7515d0757c4ccdbb2e02fb1be8654a4b33adbc6f810e721b9daa2e4a2d25d7ef",
    "rebuild v0.1": "7a7177c1fe6da1901f069f1ecf2b6184e6f0f7881ac8ab890c0c28d8b2133f3c",
}
PREVIOUS_V024 = "fed50a0babec23574485a4adb4c8241aab97ea3ee2ab3b4c860e44e47379f123"
CORE = {
    "roads.py": "501826505f7970d4c382729acf875b416002e579d994d841ebe354ffd0583e31",
    "roadmesh.py": "c698ec830e187d95e78dca97230e886ce565be2577ae2b88c8a471da38860fd2",
    "portals.py": "6fc6673ae82bf5098f9ade97e2cd9c12ea0f3b346d547a32b89cafe5a9b85009",
    "grading.py": "4b6f6cda796f6bf13b74460836b1b9aeb884c28fb43bd0bc141915ba8b921bd2",
}


def digest(file):
    hasher = hashlib.sha256()
    with file.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def atom_copy(source, destination):
    fd, temp = tempfile.mkstemp(prefix="nvdb_tunnel_", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as f, source.open("rb") as original:
            shutil.copyfileobj(original, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, destination)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def checks():
    mismatches = []
    for name, expected in CORE.items():
        f = ROOT / "server" / name
        if not f.is_file():
            mismatches.append("missing server/" + name)
        elif digest(f) != expected:
            mismatches.append("server/" + name + " differs from the supplied snapshot")
    return mismatches


def installed():
    return TARGET.is_file() and NEW.is_file() and digest(TARGET) == digest(SOURCE) and digest(NEW) == digest(NEW_SOURCE)


def run():
    parser = argparse.ArgumentParser(description="Norwegian NVDB V4 tunnel integration")
    options = parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--check", action="store_true")
    options.add_argument("--install", action="store_true")
    options.add_argument("--undo", action="store_true")
    args = parser.parse_args()
    print("BeamNG.Earth root:", ROOT)
    if not SOURCE.is_file() or not NEW_SOURCE.is_file():
        raise ValueError("Incomplete ZIP extraction; missing package source")
    version = next((label for label, hash_ in SOURCES.items()
                    if TARGET.is_file() and digest(TARGET) == hash_), None)
    conflicts = checks()
    current = digest(TARGET) if TARGET.is_file() else None
    print("Current tunnel module:", "NVDB v0.2.5" if installed() else
          ("NVDB v0.2.4 (undo v0.2.4 first)" if current == PREVIOUS_V024 else (version or "unknown")))
    if conflicts:
        for item in conflicts:
            print("Compatibility warning:", item)
    if args.check:
        if installed():
            print("Installed. Automatic rollback available:", BACKUP.is_file() and STATE.is_file())
        elif version and not NEW.exists() and not conflicts and not BACKUP.exists():
            print("Ready to install; exact source version and core modules match.")
        else:
            print("Not ready to install. No files changed.")
        return 0
    if args.install:
        if installed():
            print("Already installed; nothing changed.")
            return 0
        if not version or NEW.exists() or BACKUP.exists() or STATE.exists() or conflicts:
            raise ValueError("Refusing to overwrite unknown or modified files; nothing changed.")
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TARGET, BACKUP)
        STATE.write_text(json.dumps({"previous_sha256": digest(TARGET),
                                     "current_sha256": digest(SOURCE),
                                     "new_sha256": digest(NEW_SOURCE)}, indent=2) + "\n", encoding="utf-8")
        # Write the helper first; the active tunnel module only changes last.
        atom_copy(NEW_SOURCE, NEW)
        atom_copy(SOURCE, TARGET)
        if not installed():
            raise RuntimeError("Post-install hashes did not match; use --undo")
        print("Installed NVDB tunnel integration. Backup:", BACKUP)
        print("Run prefetch.py before loading a fresh tunnel tile.")
        return 0
    if not BACKUP.is_file() or not STATE.is_file():
        raise ValueError("Backup manifest missing; refusing to alter files.")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    if not TARGET.is_file() or digest(TARGET) != state["current_sha256"]:
        raise ValueError("The active tunnel module changed; refusing to overwrite it.")
    if not NEW.is_file() or digest(NEW) != state["new_sha256"]:
        raise ValueError("The NVDB module changed; refusing to overwrite it.")
    if digest(BACKUP) != state["previous_sha256"]:
        raise ValueError("Backup integrity check failed; no files changed.")
    # Preserve the exact integrated helper inside backup rather than delete it.
    retired = BACKUP_DIR / "retired_norway_tunnel_nvdb.py"
    if retired.exists():
        raise ValueError("Rollback staging file already exists; no files changed.")
    os.replace(NEW, retired)
    atom_copy(BACKUP, TARGET)
    print("Restored previous module. New NVDB helper kept at:", retired)
    print("Cached NVDB data was not deleted. Restart BeamNG.Earth server.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print("STOP:", exc, file=sys.stderr)
        sys.exit(1)
