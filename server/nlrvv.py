"""Optional public NDW tiles, server placement and shared native atlas meshes."""
from collections import OrderedDict
import math
import threading

import numpy as np

import exitsigns
import fast
import nlrvvdata as data
import plazas
import railtags
import roads
import roadsigns
import speedsigns


def plate_layout(row, main_width):
    """Readable source text, in source order; never truncate or replace glyphs."""
    plates = data.text_plates(row.get('plates'), normalized=True)
    if plates is None:
        return None
    glyphs = exitsigns.lettering()['glyphs']
    layout = []
    for plate in plates:
        text = plate['text']
        if any(c not in glyphs for c in text):
            return None
        words = text.split()
        longest = max(exitsigns.guide_width(w, .10) for w in words)
        width = max(main_width, .8, min(1.1, longest + .12))
        font_height = .10 * min(1., (width - .12) / max(longest, 1e-9))
        if font_height < .065:
            return None
        lines, line = [], ''
        for word in words:
            candidate = (line + ' ' + word).strip()
            if line and exitsigns.guide_width(candidate, font_height) > width - .12:
                lines.append(line);line = word
            else:
                line = candidate
        lines.append(line)
        if len(lines) > 3:
            return None
        layout.append({'width': width, 'height': .10 + .14 * len(lines), 'lines': lines, 'font_height': font_height})
    return layout


class Source(railtags.Shards):
    def __init__(self, cache_dir, fetcher):
        super().__init__(cache_dir, fetcher, 'nl-rvv')
        self.shards = OrderedDict()
        self.query_lock = threading.Lock()

    def query(self, bounds):
        """Records in lon/lat bounds. Missing/unpublished data gives no records."""
        west, south, east, north = bounds
        if not roadsigns.NL_ON:
            return []
        # Pinning/versioning/failure backoff is the ordinary public-data path.
        if not (3 <= east and west <= 8 and 50 <= north and south <= 54):
            return []
        if self.version() is None:
            return []
        ax, ay = data.tile(min(54, north), max(3, west))
        bx, by = data.tile(max(50, south), min(8, east))
        if (bx - ax + 1) * (by - ay + 1) > 64:
            raise ValueError('NDW query exceeds tile neighbourhood')
        found = []
        with self.query_lock:
            for x in range(ax, bx + 1):
                for y in range(ay, by + 1):
                    shard = self.shard(x, y)
                    if not shard or shard.get('format') != data.FORMAT:
                        continue
                    rows = shard.get('records', ())
                    if not isinstance(rows, list) or len(rows) > 100000:
                        continue
                    for row in rows:
                        if (isinstance(row, dict) and isinstance(row.get('lat'), (int, float))
                                and isinstance(row.get('lon'), (int, float))
                                and south <= row['lat'] <= north and west <= row['lon'] <= east):
                            found.append(row)
                    with self.lock:
                        if (x, y) in self.shards:
                            self.shards.move_to_end((x, y))
                        while len(self.shards) > 16:
                            self.shards.popitem(last=False)
        return found


def place(records, frame, box, pavements, elev, decks=()):
    """Exact source coordinates only when the entire foot clears drawn pavement.

    Do not slide a restriction to another road to make it fit. Unsupported,
    corrupt and unsafe records are left out without suppressing any stock sign.
    """
    polygons = exitsigns._outlines(pavements)
    roles, _, by_id = roadsigns.tables()
    out = []
    seen = set()
    for row in records:
        try:
            ident = row['id']
            lat, lon, bearing = (float(row[k]) for k in ('lat', 'lon', 'bearing'))
            role, speed = row.get('role'), row.get('speed')
            if (ident in seen or not isinstance(ident, str) or not all(math.isfinite(v) for v in (lat, lon, bearing))
                    or not (50 <= lat <= 54 and 3 <= lon <= 8 and 0 <= bearing < 360)):
                continue
            if role not in set(data.ROLES.values()) | set(data.VARIANTS.values()) and not (
                    role is None and type(speed) is int and 5 <= speed <= 130):
                continue
            width = .6 if role is None else roadsigns.tables()[1]['sizes']['default'][by_id[role][1]['shape']][0]
            if plate_layout(row, width) is None:
                continue
            point = exitsigns._xy(frame, [[lon, lat]])[0]
            if not (box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]):
                continue
            if not exitsigns._clear(point, .22, polygons) or exitsigns._on_deck(point, decks, 1.):
                continue
            # Transform a short geodetic heading, rather than assume every anchor
            # has exactly the same north as the source's compass bearing.
            a = math.radians(bearing)
            ahead = exitsigns._xy(frame, [[lon + math.sin(a) / (111320 * math.cos(math.radians(lat))),
                                          lat + math.cos(a) / 111320]])[0]
            travel = ahead - point
            travel /= max(float(np.linalg.norm(travel)), 1e-9)
            z = exitsigns._ground_z(frame, elev, point)
            if not math.isfinite(z):
                continue
            seen.add(ident)
            out.append(dict(row, point=point, travel=travel, z=z))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return out


def render(placed, box):
    """One tile mesh, shared atlas/lettering and metal materials, no per-sign objects."""
    roles, systems, by_id = roadsigns.tables()
    ordinary, speed = [], []
    panels = exitsigns.Mesh((box[0], box[1], 0.))
    for row in placed:
        x, y = row['point']
        dx, dy = row['travel']
        z = row['z']
        main_width = .6 if row.get('role') is None else systems['sizes']['default'][by_id[row['role']][1]['shape']][0]
        layout = plate_layout(row, main_width)
        if layout is None:
            continue
        lift = sum(p['height'] + .08 for p in layout)
        if row.get('role') is not None:
            cell, role = by_id[row['role']]
            width = systems['sizes']['default'][role['shape']][0]
            ordinary.append([x, y, z, z, dx, dy, cell, roadsigns.SHAPES[role['shape']], width, 2.2 + lift])
        else:
            speed.append([x, y, z, z + .2 + lift, dx, dy, row['speed'], 0, 1])
        panels.centre = np.array([x, y, z])
        panels.face, panels.right = np.array([-dx, -dy, 0.]), np.array([dy, -dx, 0.])
        bottom = 2.2 + lift
        for plate in layout:
            w, h = plate['width'], plate['height']
            bottom -= h + .08
            panels.rounded(w, bottom, h, .025, .05, 2, back=True, front=False)
            outer = panels.outline(w, bottom, h, .025)
            inner = panels.outline(w - .024, bottom + .012, h - .024, .013)
            for a, b, c, d in zip(outer, np.roll(outer, -1, axis=0), np.roll(inner, -1, axis=0), inner):
                panels.quad([(.05, *a), (.05, *b), (.05, *c), (.05, *d)], 6)
            for i in range(1, len(inner) - 1):
                panels.triangle(np.c_[np.full(3, .05), inner[[0, i, i+1]]], 1)
            for i, text in enumerate(plate['lines']):
                panels.guide_text(text, 0., bottom + h - .15 - .14 * i, plate['font_height'], w - .12, black=True)
            for height in (bottom + .05, bottom + h - .05):
                panels.box((-.13, -.06, height - .015), (.02, .06, height + .015), 2)
    mesh = None
    if ordinary:
        p, t, uv, mi = fast._ef.road_sign_mesh(np.ascontiguousarray(ordinary, np.float64), roles['cols'], roles['rows'],
                                               [float(box[0]), float(box[1]), 0.])
        mesh = p, t, uv, ['bne_signs_vienna_w', roadsigns.METAL], mi
    if speed:
        cp, glyph, cap = speedsigns._glyphs()
        p, t, uv, mi = fast._ef.speed_sign_mesh(np.ascontiguousarray(speed, np.float64), speedsigns.STYLE_VIENNA,
                                                cp, glyph, cap, [float(box[0]), float(box[1]), 0.])
        mesh = plazas.merge(mesh, (p, t, uv, speedsigns.MATERIALS, mi))
    return plazas.merge(mesh, panels.result())


def keep_inferred(signs, claims, speed=False):
    """Only a successfully rendered, same-facing/same-meaning nearby sign wins.

    No dataset/missing tile/failed placement means no suppression at all.
    """
    if not len(signs) or not claims:
        return signs
    keep = []
    rules = roadsigns.tables()[1]['rules']
    for i, row in enumerate(signs):
        dx, dy = row[2:4]
        norm = math.hypot(dx, dy)
        meaning = row[4] if speed else rules[int(row[4])]
        duplicate = any(not c.get('plates') and (c.get('speed') == meaning if speed else c.get('role') == meaning)
                        and float(np.linalg.norm(c['point'] - row[:2])) <= 8.
                        and norm > 0 and float(c['travel'] @ (row[2:4] / norm)) >= math.cos(math.radians(5))
                        for c in claims)
        if not duplicate:
            keep.append(i)
    return signs[np.asarray(keep, np.int64)]


def build_tile(md, frame, tx, ty, elev, context, decks):
    source = getattr(md, 'nl_rvv', None)
    if source is None or not roadsigns.NL_ON:
        return None, []
    box = roads.tile_bbox(tx, ty)
    la, lo, _ = frame.enu_to_geodetic(np.array([box[0], box[2]]), np.array([box[1], box[3]]), 0.)
    records = source.query((float(lo.min()), float(la.min()), float(lo.max()), float(la.max())))
    if not records:
        return None, []
    pavement = context.pavements(frame, np.c_[lo, la], False)
    placed = place(records, frame, box, pavement, elev, decks)
    return render(placed, box), placed
