"""Developer-only OSM snapshot acquisition for the French sign experiment.

Players use the bundled snapshot, never the public Overpass API. This tool
queries serially, waits between requests, keeps completed responses and stops
on service errors. Run again to resume. Set --date to an Overpass attic timestamp
when reproducing a historical snapshot. Building is a separate offline step.
"""
import argparse
import gzip
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request

BOXES = {
    0: (41,-6,46,3), 1: (41,3,46,10), 4: (49,-6,52,3),
    20: (46,-6,49,-1.5), 21: (46,-1.5,49,3),
    30: (46,3,47.5,8.5), 31: (47.5,3,49,8.5),
    51: (50.2,3,51.2,6.5), 500: (49,3,50.2,5),
    501: (49,5,50.2,6.8), 502: (49,6.8,50.2,8.5),
}


def queries(kind, date=None):
    prefix = '[out:json][timeout:100]' + (f'[date:{json.dumps(date)}]' if date else '') + ';'
    if kind == 'directions':
        yield 'directions', prefix + ('relation["type"="destination_sign"](41,-5.5,51.2,9.8)->.r;'
                                     '.r out body;way(r.r)["highway"];out geom;node(r.r);out;')
    else:
        for ident, box in BOXES.items():
            b = ','.join(map(str, box))
            yield str(ident), prefix + (
                f'(node["highway"="city_limit"]({b});node["traffic_sign"="city_limit"]({b});'
                f'node["traffic_sign"~"FR:EB(10|20)"]({b});)->.signs;'
                '.signs out;way(bn.signs);out geom;'
                f'node["place"~"^(city|town|village|hamlet)$"]({b});out;')


def fetch(output, kind, endpoint, date=None):
    output.mkdir(parents=True, exist_ok=True)
    requested = False
    for ident, query in queries(kind, date):
        path = output / (ident + '.json.gz')
        if path.exists():
            print('Cached:', path, flush=True)
            continue
        if requested:
            time.sleep(25)
        requested = True
        print('Fetching:', ident, flush=True)
        req = urllib.request.Request(endpoint + '?' + urllib.parse.urlencode({'data': query}),
                                     headers={'User-Agent':'BeamNG.Earth-regional-prototype/0.1'})
        with urllib.request.urlopen(req, timeout=115) as response:
            raw = response.read()
        doc = json.loads(raw)
        if doc.get('remark'):
            raise RuntimeError(doc['remark'])
        if 'elements' not in doc:
            raise ValueError('No elements in response')
        # Atomic completion marker: an interrupted download is never a valid cache.
        temporary = path.with_suffix('.partial')
        temporary.write_bytes(gzip.compress(raw, mtime=0))
        temporary.replace(path)
        print('Saved:', path, len(doc['elements']), 'elements', flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('kind', choices=('towns', 'directions'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--date')
    p.add_argument('--endpoint', default='https://overpass-api.de/api/interpreter')
    args = p.parse_args()
    fetch(args.output, args.kind, args.endpoint, args.date)
