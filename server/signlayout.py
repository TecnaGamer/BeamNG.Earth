"""Resolve guide-sign records and panel conflicts before assigning tile ownership."""
import numpy as np

import exitsigns

MARGIN = 100.                 # contains neighbouring panels, supports and fallback moves


def label_key(label):
    norm = lambda s: ' '.join(str(s).upper().split())
    return (norm(label.get('ref', '')), tuple(sorted(norm(s) for s in label.get('names', ()))),
            tuple(sorted(norm(s) for s in label.get('routes', ()))), norm(label.get('direction', '')))


def groups(frame, objects):
    """Separate junctions/carriageways; collapse repeated records for the same branch.

    A common exit number alone is insufficient. The junction, incoming direction
    and incoming pavement must agree, including on opposite carriageways.
    """
    candidates = []
    for obj in objects:
        if not obj.get('exit'):
            continue
        path = exitsigns._xy(frame, obj['exit']['approach'])
        end = exitsigns._station(path, 1e12)
        if end is None:
            continue
        before = exitsigns._station(path, max(0., end[2]-50.))[0]
        candidates.append((obj, end[0], end[1], before, end[2]))
    # The longest source approach retains every available advance station.
    candidates.sort(key=lambda row: (-row[4], row[0]['id']))
    clusters = []
    def related(a, b):
        return (a[2]@b[2] > .98 and np.linalg.norm(a[1]-b[1]) < 25. and
                abs(float((a[3]-b[3])@np.array([a[2][1], -a[2][0]]))) < 3.)
    for row in candidates:
        cluster = next((c for c in clusters if all(related(row, other) for other in c)), None)
        if cluster is None:
            clusters.append([row])
        else:
            cluster.append(row)
    out = []
    for cluster in clusters:
        seen, unique = set(), []
        for obj, *_ in cluster:
            extra = obj['exit']
            key = label_key(extra['label']), extra['side']
            if key not in seen:
                seen.add(key)
                unique.append(obj)
        out.append(unique)
    return out


def owns(box, sign):
    x, y = sign['centre'][:2]
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def overlap(a, b, clearance=.5):
    """Panel envelopes, including exit tabs; use both local axes on a bend."""
    def extent(s):
        if 'panels' in s:
            return max(abs(p['offset'])+p['width']/2 for p in s['panels']), max(p['height'] for p in s['panels'])
        return s['width']/2, s['height']
    wa, ha = extent(a)
    wb, hb = extent(b)
    if a['bottom'] > b['bottom']+hb+.8 or b['bottom'] > a['bottom']+ha+.8:
        return False
    da, db = np.asarray(a['travel']), np.asarray(b['travel'])
    ra, rb = np.array([da[1], -da[0]]), np.array([db[1], -db[0]])
    delta = np.asarray(a['centre'][:2])-b['centre'][:2]
    for axis in (da, db, ra, rb):
        reach = wa*abs(ra@axis)+wb*abs(rb@axis)+.25*(abs(da@axis)+abs(db@axis))+clearance
        if abs(delta@axis) > reach:
            return False
    return True


def arrange(frame, roadside, overheads, contexts, objects, elev, chunks):
    """One assembly per station; stagger residual roadside conflicts on clear ground.

    Input covers the tile plus MARGIN; ownership is deliberately tested afterwards.
    No labels are inferred. A panel with no safe position is omitted.
    """
    import throughsigns
    overheads = sorted(overheads, key=lambda s: (s['centre'], s['id']))
    def board_key(panel, sign):
        return (label_key(panel['label']), panel.get('side', sign.get('side', 1)) if panel['exit'] else 0,
                panel.get('kind', 'advance'))
    gantries = []
    for sign in overheads:
        signature = sign['distance'], tuple(board_key(p, sign) for p in sign['panels'])
        if any(signature == key and np.linalg.norm(np.asarray(sign['centre'])-other['centre']) < 25.
               and np.dot(sign['travel'], other['travel']) > .98 for key, other in gantries):
            continue
        collided = next((i for i, (_, other) in enumerate(gantries) if overlap(sign, other)), None)
        if collided is not None:
            _, other = gantries[collided]
            travel = np.asarray(other['travel'])
            lateral = np.array([travel[1], -travel[0]])
            delta = np.asarray(sign['centre'][:2])-other['centre'][:2]
            panels, keys = [], set()
            compatible_distance = (sign['distance'] == other['distance'] or
                not any(p['exit'] for p in sign['panels']) or not any(p['exit'] for p in other['panels']))
            distance = other['distance'] if any(p['exit'] for p in other['panels']) else sign['distance']
            if (np.dot(sign['travel'], travel) > .98 and abs(delta@lateral) < 3.
                    and compatible_distance):
                for assembly in (other, sign):
                    for p in assembly['panels']:
                        key = board_key(p, assembly)
                        if key not in keys:
                            keys.add(key)
                            panels.append(dict(p, side=p.get('side', assembly.get('side', 1))))
                panels.sort(key=lambda p: (p['offset'], board_key(p, other)))
            # Try combining compatible boards; otherwise move the assembly along
            # its approach and recheck both support locations and height.
            result = None
            for shift in ([0.] if panels else []) + list(np.arange(8., 65., 8.)):
                base = other if shift == 0 else sign
                q = np.asarray(base['centre'][:2])-np.asarray(base['travel'])*shift
                result = throughsigns._assembly(frame, objects[base['id']], contexts[base['id']], q,
                    np.asarray(base['travel']), (0., 0., 0., 0.), elev, chunks, None,
                    side=base.get('side', 1), distance=distance if shift == 0 else base['distance'], allow_outside=True,
                    branch_panels=panels if shift == 0 else [dict(p, side=p.get('side', sign.get('side', 1)))
                                                          for p in sign['panels']])
                if result and not any(overlap(result, existing) for i, (_, existing) in enumerate(gantries)
                                      if not (shift == 0 and i == collided)):
                    if shift == 0:
                        gantries.pop(collided)
                    break
                result = None
            if result is None:
                continue
            sign = result
            signature = sign['distance'], tuple(board_key(p, sign) for p in sign['panels'])
        gantries.append((signature, sign))
    fixed = [s for _, s in gantries]
    out = []
    for sign in sorted(roadside, key=lambda s: (s['id'], s['kind'], s['advance_m'])):
        # Small gore panels display only EXIT/ref and the arrow, not destination names.
        key = (sign['label']['ref'], sign['side']) if sign['kind'] == 'ramp' else label_key(sign['label'])
        if any(key == k and sign['kind'] == other['kind'] and sign['distance'] == other['distance']
               and sign['side'] == other['side'] and np.dot(sign['travel'], other['travel']) > .98
               and np.linalg.norm(np.asarray(sign['centre'])-other['centre']) < 25. for k, other in out):
            continue
        trial = sign
        if any(overlap(trial, other) for other in fixed):
            pavements = contexts[sign['id']]
            for shift in np.arange(8., 65., 8.):
                centre = np.asarray(sign['centre'][:2])-np.asarray(sign['travel'])*shift
                if (not exitsigns._clear(centre, sign['width']/2.+.5, exitsigns._outlines(pavements))
                        or exitsigns._on_deck(centre, chunks, sign['width']/2.+1.)):
                    continue
                trial = exitsigns._make_sign(objects[sign['id']], sign['kind'], centre,
                    np.asarray(sign['travel']), np.r_[centre, exitsigns._ground_z(frame, elev, centre)],
                    frame, elev, sign['side'], sign['width'], sign['height'],
                    sign['distance'], sign['advance_m'], sign['state'])
                if not any(overlap(trial, other) for other in fixed):
                    break
            else:
                continue
        out.append((key, trial))
        fixed.append(trial)
    return [s for _, s in out], [s for _, s in gantries]
