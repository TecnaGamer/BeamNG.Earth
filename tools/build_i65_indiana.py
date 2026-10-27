"""Extend the bundled median inventory through Indiana Exit 9, using local inputs.

Reference input: JSON from INDOT DOT/ReferencePosts/MapServer/0/query, POST_NAME
I_65_0 through I_65_11, outSR=4326. Reference-post labels define displayed mileage;
their MEASURE field is a separate linear-reference measure and is not the label.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import osmium

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server'))
import medianbarriers
import milemarkers
import signstates

SOURCE = 'https://gis.indot.in.gov/ro/rest/services/DOT/ReferencePosts/MapServer/0'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pbf', type=Path)
    parser.add_argument('references', type=Path)
    parser.add_argument('--out', type=Path, default=medianbarriers.DATA)
    args = parser.parse_args()
    ways, needed = [], set()
    for w in osmium.FileProcessor(str(args.pbf), osmium.osm.WAY).with_filter(
            osmium.filter.TagFilter(('highway', 'motorway'))):
        if not re.search(r'\bI[- ]*65\b', w.tags.get('ref', '')):
            continue
        nodes = [n.ref for n in w.nodes]
        ways.append((w.id, nodes, dict(w.tags)))
        needed.update(nodes)
    coords = {n.id: (n.location.lon, n.location.lat) for n in
              osmium.FileProcessor(str(args.pbf), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(needed))
              if n.location.valid()}
    ways = [(w,ns,t) for w,ns,t in ways if all(n in coords for n in ns)
            and min(coords[n][1] for n in ns) < 38.44 and max(coords[n][1] for n in ns) > 38.26]
    refs = json.loads(args.references.read_text())['features']
    anchors = {f['attributes']['OBJECTID']: (f['geometry']['x'], f['geometry']['y'],
               dict(distance=f['attributes']['POST_NAME'].split('_')[-1]+' mi', carriageway_ref='I 65'))
               for f in refs if re.fullmatch(r'I_65_\d+', f['attributes']['POST_NAME'])}
    metadata, paths = [], []
    for wid, ns, tags in ways:
        oriented = ns[::-1] if tags.get('oneway') == '-1' else ns
        direction = 'N' if coords[oriented[-1]][1] >= coords[oriented[0]][1] else 'S'
        metadata.append((wid,65,direction,'IN'))
        paths.append([65,wid,[[round(v,7) for v in coords[n]] for n in oriented]])
    # INDOT's named posts 1 and 2 are ~0.85 physical miles apart. Preserve those
    # authoritative labels and interpolate fifths along the connected road.
    objects = milemarkers.osm_records(ways,coords,anchors,metadata,mile_step=.2,
                                     route_directions={65:'N'},calibration_range=(.75,1.25))
    chosen = {}
    for obj in sorted(objects,key=lambda o:o['mile_marker']['direction'] != 'N'):
        if obj['lat'] > medianbarriers.INDIANA_END or str(signstates.states(obj['lat'],obj['lon'])) != 'IN':
            continue
        info = obj['mile_marker']
        chosen.setdefault(info['mile'],obj)
    stations = []
    for mile,obj in sorted(chosen.items()):
        info = obj['mile_marker']
        axis = info['axis'] if info['direction']=='N' else [-v for v in info['axis']]
        stations.append([65,mile,round(obj['lon'],7),round(obj['lat'],7),*axis])
    assert [round(row[1]*5) for row in stations] == list(range(1,46)), 'Incomplete Indiana fifth-mile inventory'
    data = json.loads(args.out.read_text())
    data.update(version=2,indiana_paths=paths,indiana_stations=stations,
                indiana_source=SOURCE,indiana_reference_sha256=hashlib.file_digest(args.references.open('rb'),'sha256').hexdigest(),
                indiana_osm_sha256=hashlib.file_digest(args.pbf.open('rb'),'sha256').hexdigest(),
                indiana_reference_posts=refs,
                indiana_endpoint=dict(exit=9,latitude=medianbarriers.INDIANA_END,longitude=-85.7626))
    args.out.write_text(json.dumps(data,separators=(',',':'))+'\n',encoding='utf-8')
    print('Indiana:',len(paths),'mainline paths,',len(stations),'stations (0.2 through 9.0)')


if __name__ == '__main__':
    main()
