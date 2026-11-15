"""Inspect NBd public exports without inventing missing headings or panel layouts.

Writes attribute-only GeoJSON and a data-quality report for a declared bbox.
This is research output, not a gameplay dataset or asset licence approval.
"""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path

def classify(code):
    """NBd nomenclature 27-10-2026; no face geometry follows from a type."""
    code = (code or '').strip().upper()
    if code == 'NBA' or len(code) != 3 or code[0] not in 'AKLNOPTUVW':
        return {'traffic': 'unknown', 'support': 'unknown'}
    supports = {'A':'signal_post','B':'signal_cantilever','C':'lighting_post',
                'D':'lighting_signal_post','E':'lighting_signal_cantilever',
                'F':'small_post','G':'mushroom_base','H':'wood_posts','I':'column',
                'J':'aluminium_frame','K':'gantry_column','M':'mast','O':'unknown',
                'P':'gantry','S':'frame','T':'tourist_frame','U':'cantilever',
                'V':'viaduct','Z':'existing'}
    traffic = 'bicycle' if code[1] == 'F' else 'road_guide' if code[1] in 'ABDEHLMNOPRSVW' else 'unknown'
    return {'traffic': traffic, 'support': supports.get(code[2], 'unknown')}


def audit(source, output, bounds):
    from pyproj import Transformer  # offline audit dependency, not a server requirement
    transformer = Transformer.from_crs(28992, 4326, always_xy=True)
    goals = defaultdict(list)
    types = {}
    # Legacy table is not UTF-8. Only ASCII identifier/type columns are used;
    # latin-1 preserves source bytes without lossy replacement of other fields.
    with (source / 'WW.TXT').open(encoding='latin-1', newline='') as f:
        for row in csv.reader(f):
            if len(row) >= 5:
                types[(row[0], row[1])] = row[4].strip()
    with (source / 'doelen_nbd.csv').open(encoding='utf-8-sig', errors='strict', newline='') as f:
        for row in csv.DictReader(f):
            if row.get('nummer') and row.get('doel'):
                goals[row['nummer']].append(row['doel'])
    counts, populated, classes, features = Counter(), Counter(), Counter(), []
    seen = set()
    with (source / 'wwxy.csv').open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            counts['rows'] += 1
            for k, v in row.items():
                if k and v and str(v).strip(): populated[k] += 1
            try:
                x, y = float(row['x']), float(row['y'])
                lon, lat = transformer.transform(x, y, errcheck=True)
                if not (3 <= lon <= 8 and 50 <= lat <= 54): raise ValueError('outside NL envelope')
            except (ValueError, TypeError, KeyError):
                counts['invalid_position'] += 1
                continue
            key = (row['kpnr'], row['wwnr'])
            if key in seen: counts['duplicate_object_id'] += 1
            seen.add(key)
            classes[row.get('soort', '')] += 1
            if not (bounds[0] <= lon <= bounds[2] and bounds[1] <= lat <= bounds[3]): continue
            ident = ''.join(key)
            text = goals.get(ident, [])
            classification = classify(types.get(key))
            counts['selected_' + classification['traffic']] += 1
            counts['selected'] += 1
            counts['selected_with_goals'] += bool(text)
            counts['selected_with_heading'] += bool((row.get('richting') or '').strip())
            features.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                             'properties': {'id': ident, 'junction_id': key[0], 'object_id': key[1],
                                            'source_x': x, 'source_y': y, 'kind': row.get('soort'),
                                            'type_raw': types.get(key),
                                            'classification': classification,
                                            'goals_raw': text, 'heading_raw': row.get('richting') or None,
                                            'layout_status': 'unverified', 'source': 'NBd'}})
    report = {'counts': dict(counts), 'populated_fields': dict(populated), 'kinds': dict(classes),
              'bounds_crs84': bounds, 'source_crs': 'EPSG:28992',
              'source_files': {name: hashlib.sha256((source/name).read_bytes()).hexdigest()
                               for name in ('wwxy.csv', 'doelen_nbd.csv', 'WW.TXT')},
              'limitations': ['Goals are source strings, not validated face layouts.',
                              'No heading is inferred from neighbouring objects.',
                              'No photographs, contacts or vendor assets are exported.']}
    output.mkdir(parents=True, exist_ok=True)
    (output/'objects.geojson').write_text(json.dumps({'type':'FeatureCollection','features':features}, ensure_ascii=False), encoding='utf-8')
    (output/'quality.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--bbox', type=float, nargs=4, required=True, metavar=('WEST','SOUTH','EAST','NORTH'))
    args = parser.parse_args()
    if not (args.bbox[0] < args.bbox[2] and args.bbox[1] < args.bbox[3]): parser.error('Invalid bbox')
    audit(args.source, args.output, args.bbox)
