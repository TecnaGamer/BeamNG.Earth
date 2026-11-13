#!/usr/bin/env python3
"""Compile official NDW current-state CSV[.gz] into public attribute-only tiles.

No image URLs/photos survive normalization. Publish the output as nl-rvv in
BeamNG.Earth-data; never fetch the national source from gameplay.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import nlrvvdata as data


def build(source, target):
    source, target = Path(source), Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError('Use an empty output directory; publish an immutable build atomically')
    with source.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    accepted, reasons = [], Counter()
    opener = gzip.open if source.suffix == '.gz' else open
    with opener(source, 'rt', encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        required = {'id', 'rvvCode', 'blackCode', 'status', 'latitude', 'longitude', 'placement', 'bearing'}
        if not required <= set(reader.fieldnames or ()):
            raise ValueError('Not an NDW current-state CSV')
        for raw in reader:
            row, reason = data.normalize(raw)
            if row is None:
                reasons[reason] += 1
            else:
                accepted.append(row)
    records = data.deduplicate(accepted, maths=math)
    tiles = defaultdict(list)
    for row in records:
        tiles[data.tile(row['lat'], row['lon'], maths=math)].append(row)
    built = hashlib.sha256(f'{data.FORMAT}:{data.REVISION}:{digest}'.encode()).hexdigest()
    manifest = {'format': data.FORMAT, 'zoom': data.ZOOM, 'built': built, 'producer_revision': data.REVISION,
                'source': data.SOURCE, 'source_sha256': digest, 'license': 'CC0-1.0', 'terms': data.TERMS,
                'generated': datetime.now(timezone.utc).isoformat(), 'bearing': 'travel_clockwise_from_north',
                'records': len(records), 'duplicates': len(accepted) - len(records), 'rejected': dict(reasons),
                'tiles': [f'{x}/{y}' for x, y in sorted(tiles)]}
    target.mkdir(parents=True, exist_ok=True)
    for (x, y), rows in sorted(tiles.items()):
        folder = target / str(x)
        folder.mkdir(exist_ok=True)
        raw = json.dumps({'format': data.FORMAT, 'records': rows}, separators=(',', ':'), ensure_ascii=False).encode()
        (folder / f'{y}.json.gz').write_bytes(gzip.compress(raw, mtime=0))
    # Manifest last: a failed build must never be mistaken for a published one.
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', help='Official verkeersborden_actueel_beeld.csv[.gz]')
    parser.add_argument('output', help='Empty nl-rvv dataset directory')
    args = parser.parse_args()
    report = build(args.source, args.output)
    report['tiles'] = len(report['tiles'])
    print(json.dumps(report, indent=2))
