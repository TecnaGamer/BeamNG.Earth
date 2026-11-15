#!/usr/bin/env python3
"""Build the Windows app, source/player ZIP and Earth + standalone autosave BeamMP bundle.

Uses the existing build_app.py and package.py release builders. Run with a Python
containing their dependencies and lupa. The official native module must be present.
The standalone autosave project defaults to ../BeamNG.Earth.Autosave.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run([sys.executable, *map(str, args)], cwd=ROOT, check=True)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--autosave-project', type=Path, default=ROOT.parent / 'BeamNG.Earth.Autosave')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('This combined build targets Windows. Use the standard release builders for Linux.')
    version = (ROOT / 'VERSION').read_text().strip()
    addon = args.autosave_project.resolve()
    if not (addon / 'tools/build.py').is_file():
        parser.error(f'Missing standalone autosave project: {addon}')
    out = (args.out or ROOT.parent / 'builds' / f'{version}-autosave').resolve()
    out.mkdir(parents=True, exist_ok=True)
    # build_app.py clears this scratch directory; verify it stays within this checkout.
    work = (ROOT / 'build/app').resolve()
    if not work.is_relative_to(ROOT.resolve()):
        parser.error('The app build directory resolves outside this checkout.')

    run(addon / 'tools/test.py')
    run(addon / 'tools/build.py')
    run(ROOT / 'tools/build_app.py', '--require-own-bootloader', '--out', out)
    run(ROOT / 'tools/package.py', '--beammp', '--out', out)

    client = addon / 'build/earth_beammp_autosave-1.0.0.zip'
    stock = out / f'BeamNG.Earth-BeamMP-Server-{version}.zip'
    combined = out / f'BeamNG.Earth-BeamMP-Server-{version}-autosave.zip'
    with zipfile.ZipFile(stock) as source, zipfile.ZipFile(combined, 'w') as target:
        for entry in source.infolist():
            target.writestr(entry, source.read(entry.filename))
        target.write(client, 'Resources/Client/' + client.name, compress_type=zipfile.ZIP_STORED)
        target.write(addon / 'README.md', 'Earth-BeamMP-Autosave-README.md', compress_type=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(combined) as archive:
        assert archive.testzip() is None
        assert archive.read('Resources/Client/' + client.name) == client.read_bytes()
    shutil.copyfile(client, out / client.name)
    # Keep the independent add-on's sources with the build as it lives outside the Earth git repository.
    addon_source = out / 'Earth-BeamMP-Autosave-source.zip'
    with zipfile.ZipFile(addon_source, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(addon.rglob('*')):
            relative = path.relative_to(addon)
            if path.is_file() and not any(part in {'.git', '.test-deps', '__pycache__', 'build'} for part in relative.parts):
                archive.write(path, relative.as_posix())

    readme = out / 'INSTALL.txt'
    readme.write_text(
        f'BeamNG.Earth {version}: saved-location fix + standalone BeamMP vehicle autosave\n\n'
        f'SERVER: Extract {combined.name} into your existing BeamMP server folder and restart it.\n'
        'This replaces Resources/Client/beamng_earth.zip and includes the standalone autosave client ZIP.\n'
        'Keep your existing ServerConfig.toml and AuthKey. No server executable is included.\n\n'
        f'PLAYERS: Run BeamNG.Earth-{version}.exe, then reconnect with BeamMP.\n'
        'Returning players keep their last Earth location; delayed invitations to another player are ignored.\n'
        'The separate autosave mod restores your vehicle/configuration, fuel/battery and heading there.\n'
        'If no previous/chosen location exists, Earth retains the join-other-players fallback.\n\n'
        'Existing saves are reused. If a previous session already saved the wrong destination, travel to the\n'
        'desired location once and let Earth save it before testing another reconnect.\n\n'
        'Local custom build, not an official upstream release. Automated checks are offline; test a real\n'
        'two-player reconnect before rolling out widely. See the autosave README inside the server ZIP.\n', encoding='utf-8')
    products = [out / f'BeamNG.Earth-{version}.exe', out / f'BeamNG.Earth-{version}.zip',
                combined, out / client.name, addon_source, readme]
    manifest = out / 'SHA256SUMS.txt'
    manifest.write_text(''.join(f'{sha(path)}  {path.name}\n' for path in products), encoding='ascii')
    provenance = out / 'BUILD.json'
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()
    provenance.write_text(json.dumps({'earthVersion': version, 'branch': git('branch', '--show-current'),
        'baseCommit': git('rev-parse', 'HEAD'), 'workingTree': git('status', '--short'),
        'files': {path.name: {'sha256': sha(path), 'bytes': path.stat().st_size} for path in products}}, indent=2) + '\n', encoding='utf-8')
    package = out / f'BeamNG.Earth-{version}-autosave-combined.zip'
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in products + [manifest, provenance]:
            archive.write(path, path.name)
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
    package.with_suffix('.zip.sha256').write_text(f'{sha(package)}  {package.name}\n', encoding='ascii')
    print(f'Combined build: {package} ({package.stat().st_size / 1e6:.1f} MB)', flush=True)


if __name__ == '__main__':
    main()
