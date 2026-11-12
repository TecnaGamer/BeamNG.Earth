"""Public NDW attribute tiles. No photographs or vendor artwork, no private store.

Shared with tools/build_nl_rvv.py; standard library only so the data repository
can run the producer independently of the world server.
"""
import math

FORMAT, ZOOM = 1, 12
SOURCE = 'https://opendata.ndw.nu/verkeersborden_actueel_beeld.csv.gz'
TERMS = 'https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden/'
# Only mappings whose pictogram exists in our vector-generated atlas. Never
# substitute a generic sign for a qualified/unknown RVV code.
ROLES = {
    'B1': 'prio.main_road', 'B2': 'prio.main_road_end', 'B6': 'prio.give_way', 'B7': 'prio.stop',
    'C1': 'reg.no_vehicles', 'C2': 'reg.no_entry', 'D1': 'mand.roundabout', 'D4': 'mand.ahead',
    'E1': 'reg.no_parking', 'E2': 'reg.no_stopping', 'E4': 'info.parking',
    'F1': 'reg.no_overtaking', 'F2': 'reg.no_overtaking_end', 'F5': 'prio.oncoming', 'F6': 'prio.over_oncoming',
    'G1': 'info.motorway', 'G2': 'info.motorway_end',
    'G7': 'mand.pedestrian', 'G11': 'mand.bicycle', 'L8': 'info.dead_end',
    'J2': 'warn.curve_right', 'J3': 'warn.curve_left', 'J4': 'warn.double_right', 'J5': 'warn.double_left',
    'J8': 'warn.crossroads', 'J9': 'warn.roundabout', 'J10': 'warn.level_gate', 'J11': 'warn.level_nogate',
    'J16': 'warn.roadworks', 'J17': 'warn.narrow_both', 'J18': 'warn.narrow_right', 'J19': 'warn.narrow_left',
    'J20': 'warn.slippery', 'J21': 'warn.children', 'J23': 'warn.pedestrians', 'J24': 'warn.cyclists',
    'J27': 'warn.animals', 'J32': 'warn.signals', 'J37': 'warn.danger', 'J38': 'warn.bump',
}
VARIANTS = {
    ('D2', 'RO'): 'mand.keep_right', ('D2', 'LO'): 'mand.keep_left',
    ('D5', 'R'): 'mand.right', ('D5', 'L'): 'mand.left',
    ('D6', 'R'): 'mand.ahead_right', ('D6', 'L'): 'mand.ahead_left',
}
COMPASS = {s: i * 22.5 for i, s in enumerate(
    ('N', 'NNO', 'NO', 'ONO', 'O', 'OZO', 'ZO', 'ZZO', 'Z', 'ZZW', 'ZW', 'WZW', 'W', 'WNW', 'NW', 'NNW'))}


def tile(lat, lon):
    n = 1 << ZOOM
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def normalize(row):
    """(public row, rejection reason). Input is the documented current-state CSV.

    Qualifiers matter: a sign with an unrendered exception must not become an
    unconditional restriction. Such records are counted as unsupported.
    """
    if row.get('status') != 'PLACED':
        return None, 'not_placed'
    if row.get('placement') != 'L':
        return None, 'not_roadside'
    if row.get('zoneCode') or row.get('textSigns') not in (None, '', '[]'):
        return None, 'qualifier_not_supported'
    code, black = str(row.get('rvvCode') or '').upper(), str(row.get('blackCode') or '').upper()
    role = VARIANTS.get((code, black)) if black else ROLES.get(code)
    speed = None
    if code == 'A1' and black.isdecimal() and 5 <= int(black) <= 130:
        speed = int(black)
    if role is None and speed is None:
        return None, 'artwork_not_supported'
    try:
        ident = str(row['id']).strip()
        lat, lon = float(row['latitude']), float(row['longitude'])
        raw = row.get('bearing')
        bearing = float(raw) if raw not in (None, '') else COMPASS[str(row.get('side', '')).upper()]
        if not ident or len(ident) > 128 or not (50 <= lat <= 54 and 3 <= lon <= 8 and 0 <= bearing < 360):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, 'invalid_location_or_bearing'
    # George's bearing is the road user's viewing/travel direction (0 = north),
    # not the sign face's outward normal. Preserve it explicitly in the schema.
    return {'id': ident, 'lat': lat, 'lon': lon, 'bearing': bearing, 'code': code,
            'role': role, 'speed': speed}, None


def deduplicate(records):
    """Keep opposite faces/different meanings; collapse <=.5m / <=5 degree copies.

    Use a fixed representative, not transitive chaining, and preserve source IDs.
    """
    grid, result, ids = {}, [], set()
    for row in sorted(records, key=lambda r: r['id']):
        if row['id'] in ids:
            raise ValueError('duplicate source id: ' + row['id'])
        ids.add(row['id'])
        x = row['lon'] * 111320 * math.cos(math.radians(52))
        y = row['lat'] * 111320
        cx, cy = math.floor(x), math.floor(y)
        match = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((cx + dx, cy + dy), ()):
                    distance = math.hypot((row['lon'] - other['lon']) * 111320 * math.cos(math.radians(row['lat'])),
                                          (row['lat'] - other['lat']) * 111320)
                    if (distance <= .5 and (row['role'], row['speed']) == (other['role'], other['speed'])
                            and abs((row['bearing'] - other['bearing'] + 180) % 360 - 180) <= 5):
                        match = other
                        break
        if match is not None:
            match['source_ids'].append(row['id'])
        else:
            item = {**row, 'source_ids': [row['id']]}
            result.append(item)
            grid.setdefault((cx, cy), []).append(item)
    return result
