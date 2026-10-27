"""Bundle KY I-65/I-75 mainlines and route-calibrated 0.2-mile stations from a local OSM extract.

No downloads at runtime. DOT statewide mile references calibrate connected OSM
paths; coordinates between references follow curves, never straight chords.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

import osmium
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server'))
import milemarkers
import signstates


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pbf', type=Path)
    ap.add_argument('--out', type=Path, default=ROOT/'server/ky_interstate_medians.json')
    args = ap.parse_args()
    ways, needed, routes = [], set(), {}
    for w in osmium.FileProcessor(str(args.pbf), osmium.osm.WAY).with_filter(
            osmium.filter.TagFilter(('highway', 'motorway'))):
        refs = {int(r) for r in re.findall(r'\bI[- ]*(\d+)\b', w.tags.get('ref', ''))} & {65, 75}
        if not refs:
            continue
        tags = dict(w.tags)
        if tags.get('oneway') not in ('yes', '1', '-1', 'true'):
            continue
        nodes = [n.ref for n in w.nodes]
        ways.append((w.id, nodes, tags))
        routes[w.id] = sorted(refs)
        needed.update(nodes)
    coords = {n.id: (n.location.lon, n.location.lat) for n in
              osmium.FileProcessor(str(args.pbf), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(needed))
              if n.location.valid()}
    # Both carriageways use the same statewide chainage. Posted direction is N/S
    # even where a bend travels east/west; source way orientation is independent.
    metadata = []
    paths = []
    for wid, nodes, tags in ways:
        if any(n not in coords for n in nodes):
            continue
        oriented = nodes[::-1] if tags.get('oneway') == '-1' else nodes
        direction = 'N' if coords[oriented[-1]][1] >= coords[oriented[0]][1] else 'S'
        for route in routes[wid]:
            metadata.append((wid, route, direction, 'KY'))
            paths.append([route, wid, [[round(v, 7) for v in coords[n]] for n in oriented]])
    data = json.loads(milemarkers.DATA.read_text(encoding='utf-8'))
    anchors = {i: (lon, lat, dict(distance=f'{mile} mi', carriageway_ref=f'I {route}'))
               for i, (state, route, mile, direction, lon, lat) in enumerate(data['markers'], 1)
               if state == 'KY' and route in (65, 75)}
    objects = milemarkers.osm_records(ways, coords, anchors, metadata, mile_step=.2, extend_miles=1.,
                                      route_directions={65:'N', 75:'N'})
    # Prefer one northbound reference per station; one physical post has two faces.
    chosen = {}
    for obj in sorted(objects, key=lambda o: o['mile_marker']['direction'] != 'N'):
        info = obj['mile_marker']
        if str(signstates.states(obj['lat'], obj['lon'])) != 'KY':
            continue
        chosen.setdefault((info['route'], info['mile']), obj)
    stations = []
    for (route, mile), obj in sorted(chosen.items()):
        info = obj['mile_marker']
        axis = info['axis'] if info['direction'] == 'N' else [-v for v in info['axis']]
        stations.append([route, mile, round(obj['lon'], 7), round(obj['lat'], 7), *axis])
    output = dict(version=1, source='OpenStreetMap contributors, ODbL; Kentucky Transportation Cabinet mile references',
                  osm_sha256=hashlib.file_digest(args.pbf.open('rb'), 'sha256').hexdigest(),
                  mile_reference_sha256=hashlib.file_digest(milemarkers.DATA.open('rb'), 'sha256').hexdigest(),
                  interval_miles=.2, paths=paths, stations=stations)
    args.out.write_text(json.dumps(output, separators=(',', ':'))+'\n', encoding='utf-8')
    print('Paths:', dict(Counter(row[0] for row in paths)))
    print('Stations:', dict(Counter(row[0] for row in stations)))
    for route in (65, 75):
        ticks = {round(m*5) for r,m,*_ in stations if r == route}
        print(route, 'range', min(ticks)/5, max(ticks)/5, 'missing',
              [t/5 for t in range(min(ticks), max(ticks)+1) if t not in ticks])


if __name__ == '__main__':
    main()
