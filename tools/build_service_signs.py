"""Build a service-sign index from Overpass JSON (out body geom), optionally gzip.

Input must contain complete connected drivable ways, junctions, restrictions,
barriers and service POIs/areas. Use a buffered extract; missing roads cause
omissions, never guessed access. Output is independent of running server caches.
"""
import argparse
import gzip
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from servicesigns_index import build, apply_verifications, POLICY


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--policy', type=Path, default=POLICY)
    parser.add_argument('--verified', type=Path, help='Audited participation evidence JSON list; never grants logo rights')
    args = parser.parse_args()
    opener = gzip.open if args.input.suffix == '.gz' else open
    with opener(args.input, 'rt', encoding='utf-8') as f:
        result = build(json.load(f), args.policy)
    if args.verified:
        apply_verifications(result, json.loads(args.verified.read_text(encoding='utf-8')))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix('.tmp')
    tmp.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')), encoding='utf-8')
    tmp.replace(args.output)
    print(json.dumps(result['diagnostics'], sort_keys=True))


if __name__ == '__main__': main()
