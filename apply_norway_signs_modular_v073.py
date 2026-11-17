#!/usr/bin/env python3
"""Install / undo modular nationwide Norwegian NVDB signs v0.7.3.

Run from the root of your BeamNG.Earth clone on feature/norway-nvdb-roads:
    py apply_norway_signs_modular_v073.py --check
    py apply_norway_signs_modular_v073.py --apply
    py apply_norway_signs_modular_v073.py --undo

Inputs are included in the same extracted folder as this installer; the whole
folder norway_signs_v073 must be copied alongside it. Only Norway sign code,
a standalone survey, and an offline test are modified. Source checks are strict.
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
STATE = ROOT / '.git' / 'norway_signs_modular_v073'
REPLACEMENTS = {
    'server/norway_nvdb_destinations_v07.py':'norway_nvdb_destinations_v07.py',
    'server/norway_signs_v073/__init__.py':'norway_signs_v073/__init__.py',
    'server/norway_signs_v073/catalog.py':'norway_signs_v073/catalog.py',
    'server/norway_signs_v073/coverage.py':'norway_signs_v073/coverage.py',
    'server/tests/test_nvdb_signs_modular_v073.py':'test_nvdb_signs_modular_v073.py',
    'nvdb_sign_coverage_v073.py':'nvdb_sign_coverage_v073.py',
}
KNOWN_V072_HASH = '7e847c67c45bfac6cde7f53d96c954b87f25f3a51942327612dee2c0b7deeed4'


def sha(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def canonical(raw):
    if raw is None: return None
    if b'\r\n' in raw and b'\n' in raw.replace(b'\r\n',b''):
        raise ValueError('Source has mixed line endings; refusing to overwrite.')
    return raw.replace(b'\r\n',b'\n')


def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,temp = tempfile.mkstemp(prefix='.no_signs_modular_',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def prepare():
    if not (ROOT/'run_server.bat').is_file() or not (ROOT/'.git').is_dir():
        raise ValueError('Run inside your BeamNG.Earth checkout (beside run_server.bat).')
    try:
        p=subprocess.run(['git','-C',str(ROOT),'branch','--show-current'],
                         capture_output=True,text=True,check=True)
    except (OSError,subprocess.CalledProcessError) as exc:
        raise ValueError('Git unavailable or checkout invalid: '+str(exc)) from exc
    if p.stdout.strip()!=BRANCH:
        raise ValueError('Switch to Git branch '+BRANCH+' first.')
    if STATE.exists():
        raise ValueError('v0.7.3 already installed; backup state exists.')
    stream=ROOT/'server'/'signstream.py'
    if not stream.is_file() or b'BNE_NVDB_DESTINATIONS_V07' not in stream.read_bytes():
        raise ValueError('v0.7 integration missing; install the previous sign builds first.')
    patch={}
    for rel,src in REPLACEMENTS.items():
        dest=ROOT/rel
        srcp=BUNDLE/src
        if not srcp.is_file():
            raise ValueError('Missing installer source: '+src)
        data=srcp.read_bytes()
        if dest.is_file():
            original=dest.read_bytes()
        else:
            original=None
        if rel=='server/norway_nvdb_destinations_v07.py':
            if sha(canonical(original)) != KNOWN_V072_HASH:
                raise ValueError('Installed destination renderer is not the tested v0.7.2 version. No files changed.')
        elif original is not None and canonical(original) != data:
            raise ValueError('New module/test already exists with different content: '+rel)
        if src.endswith('.py'):
            compile(data.decode('utf-8'),rel,'exec')
        new=data.replace(b'\n',b'\r\n') if original is not None and b'\r\n' in original else data
        if original != new:
            patch[rel]=(original,new)
    if not patch:
        raise ValueError('No files require changing.')
    return patch


def apply():
    patch=prepare()
    STATE.mkdir(parents=True, exist_ok=False)
    manifest={}
    try:
        for i,(rel,(old,new)) in enumerate(patch.items()):
            backup='%02d.bak'%i if old is not None else None
            if backup: atomic(STATE/backup,old)
            manifest[rel]={'old':sha(old),'new':sha(new),'backup':backup}
        atomic(STATE/'manifest.json',json.dumps(manifest,indent=2).encode('utf-8'))
        for rel,(old,new) in patch.items():
            dest=ROOT/rel
            current=dest.read_bytes() if dest.is_file() else None
            if sha(current)!=sha(old):
                raise RuntimeError('File changed after preflight: '+rel)
            atomic(dest,new)
    except BaseException:
        for rel,(old,new) in patch.items():
            dest=ROOT/rel
            current=dest.read_bytes() if dest.is_file() else None
            if sha(current)==sha(new):
                if old is None: dest.unlink(missing_ok=True)
                else: atomic(dest,old)
        shutil.rmtree(STATE)
        raise
    print('APPLIED v0.7.3 — modular NVDB loader, improved 713 variants, two-sided 723 markers, coverage audit.')
    print('No roads, speed signs, bridges or furniture files were edited.')
    print('Use py nvdb_sign_coverage_v073.py --lat 62.89 --lon 7.67 --radius-m 500 to test API coverage.')


def undo():
    manifest=STATE/'manifest.json'
    if not manifest.is_file(): raise ValueError('No v0.7.3 backup found.')
    entries=json.loads(manifest.read_text('utf-8'))
    for rel,rec in entries.items():
        if rel not in REPLACEMENTS: raise ValueError('Unknown backup destination: '+rel)
        dest=ROOT/rel
        data=dest.read_bytes() if dest.is_file() else None
        if sha(data)!=rec['new']:
            raise ValueError('File edited since install; refusing unsafe undo: '+rel)
        if rec['backup'] and sha((STATE/rec['backup']).read_bytes())!=rec['old']:
            raise ValueError('Backup corrupted for: '+rel)
    for rel,rec in entries.items():
        dest=ROOT/rel
        if rec['old'] is None: dest.unlink(missing_ok=True)
        else: atomic(dest,(STATE/rec['backup']).read_bytes())
    shutil.rmtree(STATE)
    print('UNDONE v0.7.3: previous source restored; other sign builds untouched.')


def main():
    if len(sys.argv)!=2 or sys.argv[1] not in ('--check','--apply','--undo'):
        print('Usage: py apply_norway_signs_modular_v073.py --check|--apply|--undo')
        return 2
    try:
        if sys.argv[1]=='--check':
            patch=prepare()
            print('CHECK PASSED: '+', '.join(patch))
            print('No files changed.')
        elif sys.argv[1]=='--apply': apply()
        else: undo()
        return 0
    except (OSError,ValueError,RuntimeError,KeyError,UnicodeError,SyntaxError) as exc:
        print('ERROR:',exc)
        return 1

if __name__=='__main__':sys.exit(main())
