"""Marshal public context into the native matcher; no client road-mesh reads.

Intermediate server API for the Groenpol migration. Callers must explicitly
prove complete WKD coverage before an absent urban match can mean bubeko.
"""
import numpy as np
import dmath
import fast
from nlsignaldata import valid


def match(records, frame, queries, complete_wkd=False):
    lines, points, offsets, kinds, oneways, turns = [], [], [0], [], [], []
    for row in records:
        if not valid(row):
            raise ValueError('invalid signal context record')
        if row['kind'] == 'signal':
            continue
        tags = row.get('tags', {})
        if not isinstance(tags, dict):
            raise ValueError('invalid signal context tags')
        text = [tags.get(k, '') for k in ('turn:lanes:forward', 'turn:lanes:backward', 'turn:lanes')]
        if not all(isinstance(t, str) for t in text):
            raise ValueError('invalid lane-turn text')
        ow = tags.get('oneway')
        direction = -1 if ow == '-1' else 1 if ow in ('yes', '1', 'true') or (
            tags.get('junction') == 'roundabout' and ow != 'no') else 0
        lines.append(row)
        points.extend(row['geometry'])
        offsets.append(len(points))
        kinds.append(0 if row['kind'] == 'urban_section' else 1)
        oneways.append(direction)
        turns.append(text)
    lonlat = np.asarray(points, np.float64).reshape(-1, 2)
    if len(lonlat):
        x, y, _ = frame.geodetic_to_enu(lonlat[:, 1], lonlat[:, 0], 0.)
        xy = np.column_stack((x, y))
    else:
        xy = np.empty((0, 2))
    matches = fast.native(292, 'Dutch signal-context matching').nl_signal_context_match(
        np.asarray(queries, np.float64).reshape(-1, 5), xy, np.asarray(offsets, np.int64),
        np.asarray(kinds, np.uint8), np.asarray(oneways, np.int8), turns, bool(complete_wkd))
    out = []
    for m in matches:
        ctx = dict(zone={-1: 'unknown', 0: 'bubeko', 1: 'bibeko'}[int(m[0])],
                   zone_source='WKD_KOM_BORDEN_spatial_match' if m[0] >= 0 else 'context_unavailable',
                   turns_source='turn_lanes_unavailable')
        if m[1] >= 0:
            ctx['zone_wvk_id'] = lines[int(m[1])]['id']
            ctx['zone_distance_m'] = dmath.sqrt(float(m[6]))
        if m[2] >= 0:
            road = lines[int(m[2])]
            ctx.update(road_id=road['id'], road_class=road.get('tags', {}).get('highway'),
                       road_tags=road.get('tags', {}), road_forward=bool(m[4] > 0),
                       road_distance_m=dmath.sqrt(float(m[7])))
        if m[3] >= 0:
            i = int(m[3])
            raw = turns[i][2 if oneways[i] else 0 if m[5] > 0 else 1]
            ctx.update(turns=raw.split('|'), turns_source='OSM_turn_lanes_spatial_match', turns_way_id=lines[i]['id'])
        out.append(ctx)
    return out
