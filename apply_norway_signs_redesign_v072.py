#!/usr/bin/env python3
"""Safe BeamNG.Earth Norway v0.7.1 -> v0.7.2 sign-design update.

  py apply_norway_signs_redesign_v072.py --check
  py apply_norway_signs_redesign_v072.py --apply
  py apply_norway_signs_redesign_v072.py --undo

Changes only the NVDB destination module and its targeted tests, with verified
backups. No roads, speed signs, native library, bridge or furniture edits.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path.cwd()
BUNDLE = Path(__file__).resolve().parent
BRANCH = 'feature/norway-nvdb-roads'
STATE = ROOT / '.git' / 'norway_signs_redesign_v072'
REPLACEMENTS = {
    'server/norway_nvdb_destinations_v07.py': 'norway_nvdb_destinations_v07.py',
    'server/tests/test_nvdb_destinations_v071.py': 'test_nvdb_destinations_v071.py',
    'server/tests/test_nvdb_destinations_v072.py': 'test_nvdb_destinations_v072.py',
}
# The actual v0.7.1 files from the user's last hotfix package.
KNOWN_BEFORE = {
    'server/norway_nvdb_destinations_v07.py': 'cea10217e1d7fe756c80ecc1af1365046b181ba21927863ac9b4c2722e6784ea',
    'server/tests/test_nvdb_destinations_v071.py': 'd67faea045c6cb637cdec45d8623f7f05da77a994602788152fee271dbcf9986',
}


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def canonical(data):
    if data is None:
        return None
    if b'\r\n' in data and b'\n' in data.replace(b'\r\n', b''):
        raise ValueError('Mixed LF/CRLF line endings; refusing to overwrite.')
    return data.replace(b'\r\n', b'\n')


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.norway_redesign_v072_', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def prepare():
    if not (ROOT / 'run_server.bat').is_file() or not (ROOT / '.git').is_dir():
        raise ValueError('Run from your BeamNG.Earth checkout beside run_server.bat.')
    try:
        branch = subprocess.run(['git', '-C', str(ROOT), 'branch', '--show-current'],
                                check=True, text=True, capture_output=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        raise ValueError('Git not available or checkout invalid: ' + str(e)) from e
    if branch != BRANCH:
        raise ValueError('Wrong branch: %s (expected %s)' % (branch, BRANCH))
    if STATE.exists():
        raise ValueError('v0.7.2 backup already exists. Applied once; use --undo if needed.')
    stream = ROOT / 'server' / 'signstream.py'
    if not stream.is_file() or b'BNE_NVDB_DESTINATIONS_V07' not in stream.read_bytes():
        raise ValueError('v0.7 signstream integration missing. Install prior v0.7/v0.7.1 first.')
    result = {}
    for rel, src_name in REPLACEMENTS.items():
        src = BUNDLE / src_name
        dst = ROOT / rel
        if not src.is_file():
            raise ValueError('Package file missing: ' + src_name)
        original = dst.read_bytes() if dst.is_file() else None
        norm = canonical(original)
        if rel in KNOWN_BEFORE and digest(norm) != KNOWN_BEFORE[rel]:
            raise ValueError('Unexpected/edited v0.7.1 file: %s. Refusing to overwrite.' % rel)
        if rel not in KNOWN_BEFORE and original is not None:
            if canonical(original) != src.read_bytes():
                raise ValueError('New test file already exists and differs: ' + rel)
        new_lf = src.read_bytes()
        compile(new_lf.decode('utf-8'), rel, 'exec')
        new = new_lf.replace(b'\n', b'\r\n') if original is not None and b'\r\n' in original else new_lf
        if original != new:
            result[rel] = (original, new)
    if not result:
        raise ValueError('Nothing to change. Already current or wrong package.')
    return result


def apply():
    changes = prepare()
    STATE.mkdir(parents=True, exist_ok=False)
    manifest = {}
    try:
        for i, (rel, (old, new)) in enumerate(changes.items()):
            backup = f'{i:02d}.bak' if old is not None else None
            if backup:
                atomic_write(STATE / backup, old)
            manifest[rel] = {'old': digest(old), 'new': digest(new), 'backup': backup}
        atomic_write(STATE / 'manifest.json', json.dumps(manifest, indent=2).encode('utf-8'))
        for rel, (old, new) in changes.items():
            dst = ROOT / rel
            got = dst.read_bytes() if dst.exists() else None
            if digest(got) != digest(old):
                raise RuntimeError('File changed while applying: ' + rel)
            atomic_write(dst, new)
    except BaseException:
        for rel, (old, new) in changes.items():
            dst = ROOT / rel
            got = dst.read_bytes() if dst.is_file() else None
            if digest(got) == digest(new):
                if old is None:
                    dst.unlink(missing_ok=True)
                else:
                    atomic_write(dst, old)
        shutil.rmtree(STATE)
        raise
    print('APPLIED v0.7.2: New Norwegian destination faces and route-number shields.')
    print('Only server/norway_nvdb_destinations_v07.py and the v071/v072 tests changed.')
    print('Restart the server with BNE_REVISIT=off; new textures use new cache names.')


def undo():
    manifest = STATE / 'manifest.json'
    if not manifest.is_file():
        raise ValueError('No v0.7.2 rollback available.')
    entries = json.loads(manifest.read_text('utf-8'))
    for rel, rec in entries.items():
        if rel not in REPLACEMENTS:
            raise ValueError('Unknown backup entry; refusing undo')
        dst = ROOT / rel
        current = dst.read_bytes() if dst.exists() else None
        if digest(current) != rec['new']:
            raise ValueError('File modified after patch. Refusing to overwrite: ' + rel)
        if rec['backup'] and digest((STATE / rec['backup']).read_bytes()) != rec['old']:
            raise ValueError('Backup hash mismatch: ' + rel)
    for rel, rec in entries.items():
        dst = ROOT / rel
        if rec['old'] is None:
            dst.unlink(missing_ok=True)
        else:
            atomic_write(dst, (STATE / rec['backup']).read_bytes())
    shutil.rmtree(STATE)
    print('UNDONE: exact previous v0.7.1 files restored.')


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('--check', '--apply', '--undo'):
        print('Usage: py apply_norway_signs_redesign_v072.py --check|--apply|--undo')
        return 2
    try:
        if sys.argv[1] == '--check':
            changes = prepare()
            print('CHECK PASSED: ' + ', '.join(changes))
            print('No files changed. Original v0.7.1 files and branch verified.')
        elif sys.argv[1] == '--apply':
            apply()
        else:
            undo()
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, UnicodeError, TypeError, SyntaxError) as e:
        print('ERROR:', e)
        return 1

if __name__ == '__main__':
    sys.exit(main())
