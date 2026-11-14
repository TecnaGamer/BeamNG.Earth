"""Build immutable public signal-context tiles from WKD and saved Overpass results.

Offline producer only; pyproj and shapely are needed for the WKD GeoPackage.
No national download or GeoPackage parsing belongs in the game client.
"""
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import sqlite3

FORMAT, REVISION, ZOOM = 1, 1, 14
ORIGIN = 20037508.342789244
CAR_CLASSES = set(('motorway trunk primary secondary tertiary unclassified residential '
                   'living_street service motorway_link trunk_link primary_link secondary_link tertiary_link').split())
LANE_KEYS = ('oneway', 'junction', 'highway', 'lanes', 'lanes:forward', 'lanes:backward',
             'turn:lanes', 'turn:lanes:forward', 'turn:lanes:backward')
TURN_QUERY = ('[out:json][timeout:180];area["ISO3166-1"="NL"]["admin_level"="2"]->.a;'
              'way["highway"][~"^turn:lanes(:forward|:backward)?$"~"."](area.a);out tags geom;')
SIGNAL_QUERY = ('[out:json][timeout:180];area["ISO3166-1"="NL"]["admin_level"="2"]->.a;'
                'node["highway"="traffic_signals"](area.a)->.s;way(bn.s)["highway"]->.w;'
                '(.w;node(w.w)["highway"="traffic_signals"](area.a););out body;')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def motor_way(tags):
    return (tags.get('highway') in CAR_CLASSES
            and tags.get('motor_vehicle') not in ('no', 'agricultural', 'forestry')
            and (tags.get('access') != 'no' or tags.get('motor_vehicle') in ('yes', 'designated')))


def point(lon, lat):
    lon, lat = float(lon), float(lat)
    if not (math.isfinite(lon) and math.isfinite(lat) and 3 <= lon <= 8 and 50 <= lat <= 54):
        raise ValueError('context outside European Netherlands')
    return [round(lon, 8), round(lat, 8)]


def osm_records(turns, signals, verified_nodes=False):
    """Preserve 64-bit source IDs; signal nodes require a motor-road parent."""
    for element in turns.get('elements', ()):
        tags = element.get('tags', {})
        if element.get('type') != 'way' or not motor_way(tags):
            continue
        try:
            geometry = [point(p['lon'], p['lat']) for p in element.get('geometry', ())]
        except (ValueError, KeyError, TypeError):
            continue
        if len(geometry) >= 2:
            yield dict(kind='road', id=str(element['id']), geometry=geometry,
                       tags={k: tags[k] for k in LANE_KEYS if k in tags})
    parents = {}
    for element in signals.get('elements', ()):
        if element.get('type') == 'way' and motor_way(element.get('tags', {})):
            for node in element.get('nodes', ()):
                parents.setdefault(node, element['tags']['highway'])
    for element in signals.get('elements', ()):
        tags = element.get('tags', {})
        if (element.get('type') != 'node' or tags.get('highway') != 'traffic_signals'
                or not (verified_nodes or element.get('id') in parents)
                or tags.get('traffic_signals') in ('bicycle', 'pedestrian')
                or tags.get('motor_vehicle') == 'no'):
            continue
        try:
            geometry = [point(element['lon'], element['lat'])]
        except (ValueError, KeyError, TypeError):
            continue
        yield dict(kind='signal', id=str(element['id']), geometry=geometry,
                   tags={k: tags[k] for k in ('direction', 'traffic_signals:direction') if k in tags},
                   road_class=parents.get(element['id']))


def wkd_records(path):
    from pyproj import Transformer
    from shapely import from_wkb
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        metadata = db.execute("select srs_id from gpkg_geometry_columns where table_name='WKD_KOM_BORDEN'").fetchone()
        if not metadata or metadata[0] != 28992:
            raise ValueError('expected WKD_KOM_BORDEN in EPSG:28992')
        transform = Transformer.from_crs(28992, 4326, always_xy=True)
        for ident, blob in db.execute('select WVK_ID,geom from WKD_KOM_BORDEN order by WVK_ID'):
            if not blob or blob[:2] != b'GP':
                raise ValueError('invalid GeoPackage geometry')
            envelope = (blob[3] >> 1) & 7
            size = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(envelope)
            if size is None:
                raise ValueError('invalid GeoPackage envelope')
            line = from_wkb(blob[8 + size:])
            if line.geom_type != 'LineString' or line.is_empty:
                raise ValueError('expected WKD LineString')
            # One metre in the source's metric CRS, before projection.
            xy = list(line.simplify(1., preserve_topology=False).coords)
            geometry = [point(*transform.transform(x, y)) for x, y, *_ in xy]
            if len(geometry) >= 2:
                yield dict(kind='urban_section', id=str(ident), geometry=geometry)


def tile_slots(geometry):
    """Halo covers the existing 20 m match radius after Mercator scale correction."""
    merc = [(lon * ORIGIN / 180, math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * ORIGIN / math.pi)
            for lon, lat in geometry]
    span = ORIGIN * 2 / 2 ** ZOOM
    xs, ys = zip(*merc)
    ax, bx = (math.floor((v + ORIGIN) / span) for v in (min(xs) - 45, max(xs) + 45))
    ay, by = (math.floor((ORIGIN - v) / span) for v in (max(ys) + 45, min(ys) - 45))
    if (bx - ax + 1) * (by - ay + 1) > 256:
        raise ValueError('context record spans too many tiles')
    return [(x, y) for x in range(ax, bx + 1) for y in range(ay, by + 1)]


def write_tiles(records, target, sources):
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError('output directory must be empty')
    tiles, identities = defaultdict(list), {}
    for row in records:
        key = row['kind'], row['id']
        if key in identities:
            if identities[key] != row:
                raise ValueError('conflicting context identity')
            continue
        identities[key] = row
        for slot in tile_slots(row['geometry']):
            tiles[slot].append(row)
    manifest = dict(format=FORMAT, producer_revision=REVISION, zoom=ZOOM,
                    license='ODbL-1.0', attribution='OpenStreetMap contributors; Rijkswaterstaat / NDW WKD KOM_BORDEN',
                    sources=sources, records=len(identities),
                    records_by_kind=dict(sorted(Counter(k[0] for k in identities).items())), tiles={})
    target.mkdir(parents=True, exist_ok=True)
    for (x, y), rows in sorted(tiles.items()):
        rows.sort(key=lambda r: (r['kind'], int(r['id'])))
        raw = json.dumps(dict(format=FORMAT, records=rows), sort_keys=True, separators=(',', ':')).encode()
        packed = gzip.compress(raw, mtime=0)
        name = f'{x}/{y}.json.gz'
        path = target / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(packed)
        manifest['tiles'][name] = dict(bytes=len(packed), sha256=hashlib.sha256(packed).hexdigest())
    manifest['built'] = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return manifest


def build(wkd, turns, signals, output):
    paths = dict(wkd=Path(wkd), turns=Path(turns), signals=Path(signals))
    sources = {k: dict(file=p.name, sha256=digest(p)) for k, p in paths.items()}
    source_note = paths['signals'].with_name(paths['signals'].name + '.source.json')
    note = json.loads(source_note.read_text()) if source_note.exists() else {}
    verified = (note.get('filter') == 'nodes_on_motor_vehicle_road_ways'
                and note.get('sha256') == sources['signals']['sha256'])
    turn_note_path = paths['turns'].with_name(paths['turns'].name + '.source.json')
    turn_note = json.loads(turn_note_path.read_text()) if turn_note_path.exists() else {}
    if turn_note.get('sha256') == sources['turns']['sha256'] and turn_note.get('query'):
        sources['turns']['query'] = turn_note['query']
    else:
        sources['turns']['suggested_query'] = TURN_QUERY
    if note.get('sha256') == sources['signals']['sha256'] and note.get('query'):
        sources['signals']['query'] = note['query']
    else:
        sources['signals']['suggested_query'] = SIGNAL_QUERY
    if verified:
        sources['signals']['filter'] = note['filter']
    turn_rows = json.loads(paths['turns'].read_text())
    signal_rows = json.loads(paths['signals'].read_text())
    for kind, raw in (('turns', turn_rows), ('signals', signal_rows)):
        sources[kind]['osm_base_timestamp'] = raw.get('osm3s', {}).get('timestamp_osm_base')
    def records():
        yield from wkd_records(paths['wkd'])
        yield from osm_records(turn_rows, signal_rows, verified)
    return write_tiles(records(), output, sources)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('wkd', 'turns', 'signals', 'output'):
        parser.add_argument(name)
    args = parser.parse_args()
    result = build(args.wkd, args.turns, args.signals, args.output)
    print(json.dumps(dict(built=result['built'], records=result['records'], tiles=len(result['tiles']))))
