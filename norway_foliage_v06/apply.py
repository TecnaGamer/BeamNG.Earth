"""Safe installer for Norway foliage v0.6 (natural forest-floor patches and fuller birch).

Run from F:\\BeamNG.Earth in PowerShell:
    py .\\norway_foliage_v06\\apply.py --check
    py .\\norway_foliage_v06\\apply.py --install
    py .\\norway_foliage_v06\\apply.py --undo

Upgrades v0.4 or v0.5 directly; the older backups are left untouched.
If v0.3 is still installed instead, first run its --undo.
Nothing is overwritten unless the file is exactly one of the supplied versions.
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
PATCHED = {
    "server/vegetation.py": {
        "c27aac537e92408c8ce93e69a869a4952104845467a80023c6f25993d6b85c76",
        "fdcf956c1244f9546f49b8ec20bf6402a5f3b52c529641b715a4a9e51e52174b",
        "659df0800a8a5e39445597f119d2077329edd7efffcbe71815ea4abc77bc5372",
        "1f3d25c9b8f529a6e81e9d734bb75592cabf091692d1271c186fc6bbea9aac47",  # v0.4
        "6921961a15fa683b71cceaea1287ae059fe451ddd080ea3abe7d1bf7dc75b289",  # v0.5
    },
    "mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua": {
        "5dd021e4904ab833e138ea85094c12c3a025a543f24e74f156235c1add12c775",
        "c2e0cc6bd101fad61048211d71a0f5be152a3f5fdb3a71347f7f8c7f171df742",
        "51fbe5335b195d3bbdcede5f08e48151faab4d3561bc3097f7c22b771822cbac",  # v0.4
        "379a84a41c43c84017904b1b7e4e8535bede4850727fbe335b82605b6ee4dbc4",  # v0.5
    },
    "server/norway_sr16.py": {"8a132220c2875c481376b01c8a4f75a6d74b1a3b0e41789950f0e390265b3137",
                              "6140c646ee932b4db13976f3f2372682d0489b3c7c6bcbbe8425a4b40e39c6e9"},
    "server/norway_outline.py": {"660423dae58073e715301022e5493b406a9041b6fd2dba21e106e310a3acdd7a"},
    "server/norway_outline.json": {"3259541053aac2b537ef1c1f76a5dc751a3368bbdf096becb0b137a55355fe6e"},
}


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def atomic_copy(src,dst):
    dst.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.norway-v06-',dir=str(dst.parent))
    os.close(fd)
    temp=Path(tmp)
    try:
        shutil.copy2(src,temp)
        os.replace(temp,dst)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path,content):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.norway-v06-',dir=str(path.parent))
    os.close(fd)
    temp=Path(tmp)
    try:
        temp.write_text(json.dumps(content,indent=2)+'\n',encoding='utf-8')
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def manifest_of(backup):
    p=backup/'manifest.json'
    return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else None


def status(root,backed):
    print('Norway foliage v0.6 — root:',root)
    print('Installer:',backed['state'] if backed else 'not installed')
    for rel,supported in PATCHED.items():
        dest=root/rel
        curr=digest(dest) if dest.is_file() else None
        mine=digest(PACK/rel)
        state=('v0.6' if curr==mine else 'absent' if curr is None else
               'earlier supported version' if curr in supported else 'MODIFIED / UNKNOWN')
        print(' ',rel,':',state)


def install(root,backup,manifest):
    if manifest and manifest['state']=='installed':
        print('Already installed; nothing changed')
        return
    if manifest and manifest['state']!='restored':
        raise ValueError('Interrupted install. Run --undo first to recover.')
    if backup.exists() and not manifest:
        raise ValueError('Existing v0.6 backup folder has no manifest; inspect it before continuing')
    originals={}
    for rel,supported in PATCHED.items():
        dest=root/rel
        old=digest(dest) if dest.is_file() else None
        if rel in list(PATCHED)[:2] and old is None:
            raise ValueError(f'Not a BeamNG.Earth root: {rel} is missing')
        if old is not None and old not in supported:
            raise ValueError(f'Cannot overwrite modified/unknown {rel}; if v0.3 is installed, run v0.3 --undo first')
        originals[rel]=old
    backup.mkdir(parents=True,exist_ok=True)
    for rel,h in originals.items():
        if h is None:
            continue
        saved=backup/'originals'/rel
        if saved.exists() and digest(saved)!=h:
            raise ValueError(f'Existing backup differs from current source: {rel}')
        if not saved.exists():
            atomic_copy(root/rel,saved)
    info={'state':'installing','originals':originals,
          'patched':{rel:digest(PACK/rel) for rel in PATCHED}}
    atomic_json(backup/'manifest.json',info)
    for rel in PATCHED:
        atomic_copy(PACK/rel,root/rel)
    info['state']='installed'
    atomic_json(backup/'manifest.json',info)
    print('Installed v0.6: Norway-only SR16, fuller stock birch crowns and grouped roadside groundcover.')
    print('No custom DAE models or textures. Existing grading/signs/roads unchanged.')
    print('Backups:',backup)
    print('Restart the companion server and BeamNG.drive.')


def undo(root,backup,manifest):
    if not manifest:
        raise ValueError('No v0.6 installation record to undo')
    if manifest['state']=='restored':
        print('Already restored')
        return
    if manifest['state'] not in ('installed','installing'):
        raise ValueError('Unrecognized installation state')
    # Check everything before changing anything. User edits after install win.
    for rel in PATCHED:
        dest=root/rel
        if dest.is_file():
            h=digest(dest)
            if h not in (manifest['patched'][rel],manifest['originals'][rel]):
                raise ValueError(f'Changed since install: {rel}; preserving it')
        original_hash=manifest['originals'][rel]
        if original_hash:
            saved=backup/'originals'/rel
            if not saved.is_file() or digest(saved)!=original_hash:
                raise ValueError(f'Missing/modified backup: {saved}')
    for rel in PATCHED:
        dest=root/rel
        previous=manifest['originals'][rel]
        if previous:
            atomic_copy(backup/'originals'/rel,dest)
        elif dest.is_file():
            parked=backup/'disabled_added'/rel
            parked.parent.mkdir(parents=True,exist_ok=True)
            suffix=0
            while parked.exists():
                suffix+=1
                parked=backup/'disabled_added'/(rel+f'.{suffix}')
            dest.rename(parked)
    manifest['state']='restored'
    atomic_json(backup/'manifest.json',manifest)
    print('Restored your exact pre-v0.6 files. The added modules are parked in the backup.')
    print('Restart the companion server and BeamNG.drive.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path.cwd(),help='BeamNG.Earth root')
    modes=p.add_mutually_exclusive_group()
    modes.add_argument('--check',action='store_true')
    modes.add_argument('--install',action='store_true')
    modes.add_argument('--undo',action='store_true')
    args=p.parse_args()
    root=args.root.resolve()
    backup=root/'norway_foliage_v06_backup'
    try:
        for rel in PATCHED:
            if not (PACK/rel).is_file():
                raise ValueError(f'Extracted package incomplete: {rel}')
        info=manifest_of(backup)
        status(root,info)
        if args.install:
            install(root,backup,info)
        elif args.undo:
            undo(root,backup,info)
        else:
            print('Preflight complete. Run --install to apply or --undo to revert.')
        return 0
    except (OSError,ValueError,KeyError,json.JSONDecodeError) as e:
        print('STOPPED safely:',e,file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())
