"""Fetch the selected CC0 scan sources, retaining hashes and provenance.

Source meshes stay in ignored build/cliff-scans. Only derived runtime assets
should be packaged. Powered by Poly Haven (https://polyhaven.com).
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse
import hashlib
import json
import requests

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/cliff-scans'
ASSETS = ('coastal_cliff_01', 'coastal_cliff_02', 'coastal_cliff_04',
          'rock_face_01', 'rock_face_02')


def download(job):
    path, metadata = job
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and hashlib.md5(path.read_bytes()).hexdigest() == metadata['md5']:
        return
    temporary = path.with_suffix(path.suffix + '.partial')
    digest = hashlib.md5()
    with requests.get(metadata['url'], stream=True, timeout=120) as response:
        response.raise_for_status()
        with temporary.open('wb') as output:
            for chunk in response.iter_content(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
    if digest.hexdigest() != metadata['md5']:
        raise ValueError(f'Checksum mismatch: {path}')
    temporary.replace(path)
    print(f'{path.name}: {path.stat().st_size / 1e6:.1f} MB verified', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resolution', default='8k', choices=('2k', '4k', '8k'))
    options = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    jobs, provenance = [], []
    for name in ASSETS:
        records = {}
        for kind in ('info', 'files'):
            path = WORK / f'{name}-{kind}.json'
            if not path.exists():
                response = requests.get(f'https://api.polyhaven.com/{kind}/{name}', timeout=60)
                response.raise_for_status()
                path.write_text(json.dumps(response.json(), indent=2), encoding='utf-8')
            records[kind] = json.loads(path.read_text(encoding='utf-8'))
        package = records['files']['gltf'][options.resolution]['gltf']
        folder = WORK / name
        files = {Path(package['url']).name: package, **package['include']}
        for relative, metadata in files.items():
            jobs.append((folder / relative, metadata))
        provenance.append({'id': name, 'source': f'https://polyhaven.com/a/{name}',
                           'license': 'CC0-1.0', 'license_url': 'https://polyhaven.com/license',
                           'authors': records['info']['authors'],
                           'dimensions_mm': records['info']['dimensions'],
                           'files': {key: {k: v[k] for k in ('url', 'md5', 'size')}
                                     for key, v in files.items()}})
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(download, jobs))
    (WORK / 'sources.json').write_text(json.dumps(provenance, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
