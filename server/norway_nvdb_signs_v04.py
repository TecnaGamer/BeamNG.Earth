"""Norwegian real Vikeplikt sign test v0.4 — eight surveyed positions around Batnfjordsora.

Off unless BNE_NVDB_SIGN_TEST_V04=on. Uses the existing sign atlas and native mesh
builder, *not* a new model or an automatic sign-placement rule.

The position and road reference come from NVDB 95/96. The facing is a
provisional geometry-derived test, because NVDB's 'Parallelt fra veg' is
relative to its registered main road. Verify the actual faces in-game before
extending coverage. No networking or disk writes in the server worker.
"""
from functools import lru_cache
from pathlib import Path
import json
import math
import os

import numpy as np

SOURCE = Path(__file__).with_name('data') / 'norway_nvdb_signpoints_v04.json'
ENV = 'BNE_NVDB_SIGN_TEST_V04'
MIN_OFFSET_M = 2.5
MAX_OFFSET_M = 20.0
SUPPRESS_INFERRED_WITHIN_M = 65.0


def enabled():
    return os.environ.get(ENV, 'off').strip().lower() in {'on', 'true', '1'}


@lru_cache(maxsize=1)
def records():
    """Fixed local survey fixture; no internet at runtime."""
    raw = json.loads(SOURCE.read_text(encoding='utf8'))
    out, seen = [], set()
    for item in raw.get('signs', ()):  # fail closed on malformed/unsupported records
        try:
            number = str(item['number'])
            if number != '202' or item['orientation'] != 'parallelt_fra_veg':
                continue
            plate_id = int(item['plate_id'])
            if plate_id in seen:
                continue
            qlon, qlat = map(float, item['point_lonlat'])
            rlon, rlat = map(float, item['road_ref_lonlat'])
            if not (all(math.isfinite(v) for v in (qlon, qlat, rlon, rlat))
                    and 57.0 < qlat < 72.0 and 4.0 < qlon < 32.0
                    and 57.0 < rlat < 72.0 and 4.0 < rlon < 32.0):
                continue
            dx = (qlon - rlon) * (111320.0 * math.cos(math.radians(qlat)))
            dy = (qlat - rlat) * 111320.0
            dist = math.hypot(dx, dy)
            if not MIN_OFFSET_M <= dist <= MAX_OFFSET_M:
                continue
            seen.add(plate_id)
            out.append((plate_id, qlat, qlon, rlat, rlon))
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return tuple(out)


def in_box(x, y, box):
    """Half-open road tile ownership: each pole belongs to exactly one tile."""
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def projected(frame, source=None):
    """(plate ID, sign XY, registered reference XY, unit face XY) in the anchor frame.

    Native road_sign_mesh points its face OPPOSITE its travel vector. This
    helper provides the desired facing (outward from the registered E39 road
    reference, the provisional interpretation of 'Parallelt fra veg').
    """
    items = records() if source is None else source
    if not items:
        return []
    la = np.asarray([v for _, qlat, _, rlat, _ in items for v in (qlat, rlat)], np.float64)
    lo = np.asarray([v for _, _, qlon, _, rlon in items for v in (qlon, rlon)], np.float64)
    e, n, _ = frame.geodetic_to_enu(la, lo, np.zeros(len(la), np.float64))
    e = np.asarray(e, np.float64).reshape(-1)
    n = np.asarray(n, np.float64).reshape(-1)
    out = []
    for i, row in enumerate(items):
        plate_id = row[0]
        q = np.array([e[2*i], n[2*i]])
        ref = np.array([e[2*i+1], n[2*i+1]])
        delta = q-ref
        length = float(np.linalg.norm(delta))
        if not (np.isfinite(delta).all() and MIN_OFFSET_M <= length <= MAX_OFFSET_M):
            continue
        out.append((plate_id, q, ref, delta / length))
    return out


def suppress_inferred(signs, frame):
    """Remove only inferred Vikeplikt rows close to surveyed real sign positions.

    The old inferred STOP / No Entry signs, speed signs, and all other sign
    roles remain. Suppression spans tile boundaries to avoid duplicates.
    """
    if not enabled() or len(signs) == 0:
        return signs
    import roadsigns
    rules = roadsigns.tables()[1]['rules']
    role_index = rules.index('prio.give_way')
    coords = np.array([q for _, q, _ref, _face in projected(frame)])
    if len(coords) == 0:
        return signs
    out = np.ones(len(signs), dtype=bool)
    for i, row in enumerate(signs):
        if int(row[4]) != role_index:
            continue
        dist = np.linalg.norm(coords - np.asarray(row[:2], np.float64), axis=1)
        if float(dist.min()) <= SUPPRESS_INFERRED_WITHIN_M:
            out[i] = False
    return signs[out]


def suppress_furniture_yields(rows, frame):
    """Hide only duplicate old Forest `giveway` props near real surveyed signs.

    The old furniture pipeline places Nordic give-way signs independently of
    roadsigns.py. Never touch its STOP, signal, roads or AI priority rules.
    """
    # Never hide the original Forest sign when the sign renderer itself is OFF.
    if (not enabled() or os.environ.get('BNE_ROADSIGNS', 'off').strip().lower() != 'on'
            or not rows or frame is None):
        return rows
    coords = np.array([q for _, q, _ref, _face in projected(frame)])
    if len(coords) == 0:
        return rows
    result = []
    for row in rows:
        if row[0] != 'giveway':
            result.append(row)
            continue
        q = np.asarray(row[1:3], np.float64)
        if float(np.linalg.norm(coords - q, axis=1).min()) > SUPPRESS_INFERRED_WITHIN_M:
            result.append(row)
    return result


def build_tile(frame, box, elev, decks, sys_row):
    """Render the surveyed signpoints within this road-sign tile (else None).

    Uses real NVDB signpole XY, current game's terrain heights (not NVDB's
    inconsistent vertical datum), and the built-in Vienna white atlas.
    """
    if not enabled() or sys_row is None or sys_row.get('atlas') != 'vienna_w':
        return None
    picks = [(pid, q, ref, face) for pid, q, ref, face in projected(frame)
             if in_box(float(q[0]), float(q[1]), box)]
    if not picks:
        return None
    import exitsigns
    import roadsigns
    import fast
    roles, systems, by_id = roadsigns.tables()
    index, role = by_id['prio.give_way']
    if role['shape'] != 'tri_down':
        return None
    size = float(systems['sizes']['default']['tri_down'][1])  # normal sign size
    n = len(picks)
    wanted = [(pid, q, ref, face) for pid, q, ref, face in picks
              if not exitsigns._on_deck(q, decks, 1.5)]
    if not wanted:
        return None
    q = np.array([t[1] for t in wanted], np.float64)
    r = np.array([t[2] for t in wanted], np.float64)
    both = np.vstack((q, r))
    lat, lon, _ = frame.enu_to_geodetic(both[:,0], both[:,1], np.zeros(len(both)))
    lat, lon = np.asarray(lat,np.float64), np.asarray(lon,np.float64)
    h = np.asarray(elev(lat,lon),np.float64).reshape(-1)
    if len(h) != len(both) or not np.isfinite(h).all():
        return None
    z = np.asarray(frame.geodetic_to_enu(lat,lon,h)[2],np.float64).reshape(-1)
    if len(z) != len(both) or not np.isfinite(z).all():
        return None
    table = np.zeros((len(wanted),10),np.float64)
    for i, (_id, xy, _ref, outward) in enumerate(wanted):
        table[i,:] = [float(xy[0]),float(xy[1]),float(z[i]),float(z[len(wanted)+i]),
                      -float(outward[0]),-float(outward[1]),float(index),
                      float(roadsigns.SHAPES['tri_down']),size,
                      float(sys_row['mount'][1])]
    p,t,uv,mi = fast._ef.road_sign_mesh(table,int(roles['cols']),int(roles['rows']),
                                        [float(box[0]),float(box[1]),0.0])
    if len(t) == 0:
        return None
    return p,t,uv,['bne_signs_vienna_w', roadsigns.METAL],mi
