"""Fetch buffered Wyoming validation inputs; never touches active map caches."""
import gzip
import argparse
import json
from pathlib import Path
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--supplement', action='store_true', help='Only fetch military areas and roadside barriers, merging into existing sample')
    args = parser.parse_args()
    boxes = ['41.07,-104.96,41.24,-104.68', '41.24,-105.70,41.39,-105.46']
    query = '[out:json][timeout:90];('
    for box in boxes:
        selectors = ('way[highway]', 'nwr[amenity~"^(restaurant|fast_food|cafe|fuel|hospital)$"]',
                         'nwr[tourism~"^(hotel|motel|guest_house|camp_site|caravan_site)$"]',
                         'node[highway=motorway_junction]', 'node[barrier]', 'relation[type=restriction]')
        supplement = ('nwr[landuse=military]', 'nwr[military]', 'way[barrier]')
        for selector in supplement if args.supplement else selectors+supplement:
            query += selector+'('+box+');'
    query += ');out body geom;'
    req = urllib.request.Request('https://overpass-api.de/api/interpreter?'+
                                 urllib.parse.urlencode({'data': query}),
                                 headers={'User-Agent': 'BeamNG-Earth-service-sign-validation/1.0'})
    raw = urllib.request.urlopen(req, timeout=55).read()
    path = Path(__file__).resolve().parents[1]/'probes/service-signs/wyoming-services.osm.json.gz'
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(raw)
    if data.get('remark') or 'elements' not in data: raise ValueError('Incomplete Overpass response')
    if args.supplement:
        with gzip.open(path, 'rt', encoding='utf-8') as f: original = json.load(f)
        elements = {(e['type'],e['id']): e for e in original['elements']}
        elements.update({(e['type'],e['id']): e for e in data['elements']})
        original['elements'] = list(elements.values())
        original['supplement_timestamp'] = data.get('osm3s', {}).get('timestamp_osm_base')
        raw = json.dumps(original, ensure_ascii=False).encode('utf-8')
    tmp = path.with_suffix('.partial')
    tmp.write_bytes(gzip.compress(raw, mtime=0))
    tmp.replace(path)
    print(f'{path}: {len(raw)} bytes')


if __name__ == '__main__': main()
