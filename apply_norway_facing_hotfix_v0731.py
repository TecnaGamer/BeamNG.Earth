#!/usr/bin/env python3
"""Repair the missing facing.py import in Norway modular signs v0.7.3.

Safely installs only server/norway_signs_v073/facing.py and the offline test.
Run from the cloned BeamNG.Earth root:
    py apply_norway_facing_hotfix_v0731.py --check
    py apply_norway_facing_hotfix_v0731.py --apply
    py apply_norway_facing_hotfix_v0731.py --undo
"""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile

ROOT = Path.cwd()
PAYLOAD = Path(__file__).resolve().parent
STATE = ROOT / '.git' / 'norway_facing_hotfix_v0731'
BRANCH = 'feature/norway-nvdb-roads'
TARGET = 'server/norway_signs_v073/facing.py'
TEST = 'server/tests/test_nvdb_sign_facing_v0731.py'
FILES = {TARGET: 'norway_signs_v073/facing.py',
         TEST: 'test_nvdb_sign_facing_v0731.py'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.facing_v0731_', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def preflight():
    if not (ROOT / '.git').is_dir() or not (ROOT / 'run_server.bat').is_file():
        raise ValueError('Run from F:\\BeamNG.Earth, beside run_server.bat.')
    try:
        p = subprocess.run(['git', '-C', str(ROOT), 'branch', '--show-current'],
                           capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ValueError('Git is unavailable or checkout cannot be verified.') from exc
    if p.stdout.strip() != BRANCH:
        raise ValueError('Switch to branch ' + BRANCH + ' in GitHub Desktop.')
    if STATE.exists():
        raise ValueError('This hotfix has already been applied (backup state exists).')
    mod = ROOT / 'server/norway_nvdb_destinations_v07.py'
    if not mod.is_file() or 'from norway_signs_v073 import catalog, coverage, facing as facing_module' not in mod.read_text('utf-8'):
        raise ValueError('Installed source does not match v0.7.3 missing-facing import.')
    if not (ROOT / 'server/norway_signs_v073/catalog.py').is_file() or not (ROOT / 'server/norway_signs_v073/coverage.py').is_file():
        raise ValueError('v0.7.3 modular catalogue/coverage files are missing.')
    patch = {}
    for dest, src in FILES.items():
        source = PAYLOAD / src
        if not source.is_file():
            raise ValueError('Hotfix file is missing: ' + src)
        raw = source.read_bytes()
        compile(raw.decode('utf-8'), dest, 'exec')
        target = ROOT / dest
        if target.exists():
            if target.read_bytes() == raw:
                continue
            raise ValueError('Existing file differs; refusing overwrite: ' + dest)
        patch[dest] = raw
    if not patch:
        raise ValueError('Facing hotfix is already present; no changes needed.')
    return patch


def apply():
    patch = preflight()
    STATE.mkdir(parents=True, exist_ok=False)
    manifest = {dest: sha(raw) for dest, raw in patch.items()}
    try:
        atomic(STATE / 'manifest.json', json.dumps(manifest, indent=2).encode('utf-8'))
        for dest, raw in patch.items():
            path = ROOT / dest
            if path.exists():
                raise RuntimeError('File appeared after preflight: ' + dest)
            atomic(path, raw)
    except BaseException:
        for dest, raw in patch.items():
            path = ROOT / dest
            if path.is_file() and sha(path.read_bytes()) == sha(raw):
                path.unlink()
        import shutil
        shutil.rmtree(STATE)
        raise
    print('APPLIED v0.7.3.1: restored missing modular sign-facing logic.')
    print('Changed files: ' + ', '.join(patch))
    print('No road, texture, or NVDB cache files changed.')


def undo():
    manifest = STATE / 'manifest.json'
    if not manifest.is_file():
        raise ValueError('No v0.7.3.1 installation state found.')
    entries = json.loads(manifest.read_text('utf-8'))
    if not entries or not set(entries).issubset(FILES):
        raise ValueError('Unexpected hotfix manifest; refusing undo.')
    for dest, digest in entries.items():
        path = ROOT / dest
        if not path.is_file() or sha(path.read_bytes()) != digest:
            raise ValueError('File modified since installation; refusing undo: ' + dest)
    for dest in entries:
        (ROOT / dest).unlink()
    manifest.unlink()
    STATE.rmdir()
    print('UNDONE v0.7.3.1: files created by hotfix removed.')


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('--check', '--apply', '--undo'):
        print('Usage: py apply_norway_facing_hotfix_v0731.py --check|--apply|--undo')
        return 2
    try:
        if sys.argv[1] == '--check':
            patch = preflight()
            print('CHECK PASSED: will add ' + ', '.join(patch))
            print('No files changed.')
        elif sys.argv[1] == '--apply':
            apply()
        else:
            undo()
    except (ValueError, OSError, RuntimeError, KeyError, UnicodeError, SyntaxError) as exc:
        print('ERROR:', exc)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
