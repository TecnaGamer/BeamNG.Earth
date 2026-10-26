#!/usr/bin/env python3
"""Refresh the bundled whole-mile Interstate references from public state DOT services.

Downloads are a development step, never a dependency of a road-tile worker. OSM
milestones supplement this snapshot at runtime through the regional roadside index.
"""
import argparse
from collections import defaultdict
from datetime import date
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    'MI': dict(url='https://gisagomdot.state.mi.us/arcgis/rest/services/MDOT/MdotMileMarker/FeatureServer/0',
               where="Route LIKE 'I-%' AND MileNumber = FLOOR(MileNumber)", fields='OBJECTID,Route,Direction,MileNumber',
               route='Route', mile='MileNumber', direction='Direction', credit='Michigan DOT',
               kind='sign inventory'),
    'KY': dict(url='https://kygisserver.ky.gov/arcgis/rest/services/WGS84WM_Services/Ky_State_Road_Mileposts_WGS84WM/MapServer/0',
               where="RTNAME LIKE 'I-%' AND MILEPOINT = FLOOR(MILEPOINT)",
               fields='OBJECTID,RTNAME,MILEPOINT,RT_UNIQUE', route='RTNAME', mile='MILEPOINT',
               credit='Kentucky Transportation Cabinet', kind='calculated route milepoints'),
}


def fetch(source):
    offset = 0
    while True:
        params = dict(f='json', where=source['where'], outFields=source['fields'],
                      outSR=4326, orderByFields='OBJECTID', resultRecordCount=1000,
                      resultOffset=offset, returnGeometry='true')
        req = Request(source['url']+'/query?'+urlencode(params), headers={
            'User-Agent': 'BeamNG.Earth (+https://github.com/TecnaGamer/BeamNG.Earth)'})
        with urlopen(req, timeout=30) as response:
            data = json.load(response)
        if 'error' in data:
            raise ValueError(data['error'])
        features = data.get('features', [])
        yield from features
        if not data.get('exceededTransferLimit'):
            break
        if not features or offset > 100000:
            raise ValueError('Incomplete DOT response')
        offset += len(features)


def normalize(state, source, features):
    rows = []
    for feature in features:
        a, p = feature['attributes'], feature.get('geometry') or {}
        match = re.fullmatch(r'I[- ]0*(\d{1,3})', str(a.get(source['route'], '')).strip(), re.I)
        try:
            mile, lon, lat = float(a[source['mile']]), float(p['x']), float(p['y'])
        except (KeyError, TypeError, ValueError):
            continue
        if not match or not all(math.isfinite(v) for v in (mile, lon, lat)):
            continue
        if abs(mile-round(mile)) > .001 or mile < 0 or not (-180 <= lon <= 180 and -90 <= lat <= 90):
            continue
        # KY's auxiliary ramps/branches have a non-zero final route-unique code.
        # The 000 primary route is the statewide Interstate's mileage reference.
        if state == 'KY' and not str(a.get('RT_UNIQUE', '')).endswith('000'):
            continue
        direction = str(a.get(source.get('direction', ''), '') or '').strip().upper()
        direction = direction if direction in ('N', 'S', 'E', 'W') else ''
        rows.append([state, int(match[1]), int(round(mile)), direction, round(lon, 7), round(lat, 7)])
    return sorted(set(map(tuple, rows)))


def fill_gaps(state, rows, pbf):
    """Calibrate absent whole miles along the connected OSM route, never a straight chord."""
    import osmium
    sys.path.insert(0,str(ROOT/'server'))
    import milemarkers
    main, needed = [], set()
    for w in osmium.FileProcessor(str(pbf),osmium.osm.WAY).with_filter(osmium.filter.TagFilter(('highway','motorway'))):
        refs = [n.ref for n in w.nodes]
        main.append((w.id,refs,{k:v for k,v in w.tags if k in ('highway','ref','direction','oneway','access')}))
        needed.update(refs)
    coords = {n.id:(n.location.lon,n.location.lat) for n in
              osmium.FileProcessor(str(pbf),osmium.osm.NODE).with_filter(osmium.filter.IdFilter(needed))
              if n.location.valid()}
    relations = []
    for r in osmium.FileProcessor(str(pbf),osmium.osm.RELATION).with_filter(osmium.filter.KeyFilter('network')):
        if r.tags.get('network') != 'US:I' or not re.fullmatch(r'\d{1,3}',r.tags.get('ref','')):
            continue
        relations.extend((m.ref,int(r.tags['ref']),milemarkers.cardinal(r.tags.get('direction')),
                          r.tags.get('is_in:state',state)) for m in r.members if m.type == 'w')
    anchors = {i:(lon,lat,dict(distance=f'{mile} mi',carriageway_ref=f'I {route} {direction}'))
               for i,(_,route,mile,direction,lon,lat) in enumerate(rows,1)}
    known = defaultdict(set)
    for _,route,mile,*_ in rows:
        known[route].add(mile)
    added = []
    for obj in milemarkers.osm_records(main,coords,anchors,relations):
        info = obj['mile_marker']
        route,mile = info['route'],info['mile']
        if route not in known or not min(known[route]) < mile < max(known[route]) or mile in known[route]:
            continue
        if info.get('state') not in ('',state):
            continue
        added.append((state,route,mile,info['direction'],round(obj['lon'],7),round(obj['lat'],7)))
    return sorted(set(added))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT/'server'/'mile_markers_us.json')
    parser.add_argument('--osm-root',type=Path,help='cached regional PBF folder, for route-calibrated gaps')
    args = parser.parse_args()
    rows, meta, interpolated = [], {}, []
    for state, source in SOURCES.items():
        normalized = normalize(state, source, fetch(source))
        if not normalized:
            raise ValueError(f'{state}: no whole-mile Interstate references')
        rows.extend(normalized)
        meta[state] = {k: source[k] for k in ('url', 'credit', 'kind')}
        print(f'{state}: {len(normalized)} whole-mile references', flush=True)
        if args.osm_root:
            region = {'MI':'us_michigan','KY':'us_kentucky'}[state]
            pbf = args.osm_root/(region+'.osm.pbf')
            added = fill_gaps(state,normalized,pbf)
            rows.extend(added)
            interpolated.extend(row[:4] for row in added)
            meta[state]['gap_calibration'] = 'Connected OpenStreetMap geometry; '+pbf.name
            print(f'{state}: {len(added)} calibrated directional references in inventory gaps',flush=True)
    data = dict(version=1, updated=date.today().isoformat(),
                columns=['state', 'interstate', 'mile', 'direction', 'longitude', 'latitude'],
                sources=meta, interpolated=interpolated, markers=sorted(rows))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # One point per line keeps refreshed datasets reviewable in a source diff.
    head = json.dumps({k:v for k,v in data.items() if k != 'markers'}, indent=2)
    args.out.write_text(head[:-2]+',\n  "markers": [\n'+',\n'.join(
        '    '+json.dumps(row) for row in rows)+'\n  ]\n}\n', encoding='utf-8')


if __name__ == '__main__':
    main()
