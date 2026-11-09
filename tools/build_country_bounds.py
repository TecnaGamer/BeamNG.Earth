"""Build a worldwide, lazy country archive from the pinned Overture extraction.

Input directory: *.geojson.gz, territorial country AND dependency features.
Preserves all source vertices, holes and islands without simplification. Each
ZIP member is one whole polygon, not a clipped map tile. Shapely is build-only.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import zipfile
import numpy as np


def build(source, output):
    from shapely.geometry import shape
    source, output = Path(source), Path(output)
    files = sorted(source.glob('*.geojson.gz')) if source.is_dir() else [source]
    meta = {'format': 2, 'overture_release': '2026-09-23.1',
            'selection': "subtype IN ('country','dependency') AND is_territorial=true",
            'license': 'ODbL-1.0', 'parts': [], 'features': []}
    seen = set(); points = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    def put(z, name, obj):
        info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        z.writestr(info, json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode(), compresslevel=9)
    with zipfile.ZipFile(output, 'w') as z:
        for path in files:
            raw = path.read_bytes()
            data = json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw)
            for f in sorted(data['features'], key=lambda x: x['properties']['country']):
                p = f['properties']; code = p['country']
                if code in seen: raise ValueError('Duplicate territory: ' + code)
                seen.add(code)
                geom = shape(f['geometry'])
                if geom.is_empty or not geom.is_valid: raise ValueError('Invalid source geometry: ' + code)
                meta['features'].append(p)
                parts = f['geometry']['coordinates']
                if f['geometry']['type'] == 'Polygon': parts = [parts]
                for i, rings in enumerate(parts):
                    a = np.array(rings[0]); a[:, 0] = np.degrees(np.unwrap(np.radians(a[:, 0])))
                    if not np.allclose(a[0], a[-1], rtol=0, atol=1e-10):
                        raise ValueError('Uncut polar ring: ' + code)
                    name = f'{code}/{i}.json'
                    meta['parts'].append({'country': code, 'member': name,
                                          'bounds': [*a.min(0), *a.max(0)]})
                    points += sum(map(len, rings))
                    put(z, name, rings)
        meta['codes'] = sorted(seen); meta['point_count'] = points
        put(z, 'index.json', meta)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(json.dumps({'sha256': digest, 'bytes': output.stat().st_size,
                      'territories': len(seen), 'parts': len(meta['parts']), 'points': points}, indent=2))
    return digest


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('source'); p.add_argument('output')
    a = p.parse_args(); build(a.source, a.output)
