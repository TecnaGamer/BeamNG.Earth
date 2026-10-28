"""Railway and tram tracks on the road mesh.

Overture's rail segments (subtype "rail", in the roads layer; each OSM way one track) on the ground (bridge and tunnel
ranges left out) as draped chunks of the road tile: on open ground a ballast bed, the sleepers across it and two rails;
where the track runs on a carriageway (a street tram, a level crossing) West Coast USA's street track instead: its
road_tramline decal, both rails in one 1.8 m ribbon (textureLength 30, as in its DecalRoads). Standard gauge. Flat: no
collision, as the markings. Subways are left out.
The classes railways.py builds as a colliding 3D track on the ground and on bridges (standard and narrow gauge, light
rail: mpsteppe's PR #287) get only their street track here, where they cross a carriageway (railways.py stops
its track at the road's edge); trams get it all.
"""
import math

import numpy as np

KINDS = {"standard_gauge": "rail", "narrow_gauge": "narrow", "light_rail": "tram", "tram": "tram"}   # (no "unknown":
# OpenStreetMap's proposed and unbuilt lines, railways.DRAWN)
GAUGE_M = {"rail": 1.435, "narrow": 1.0, "tram": 1.435}
HEAD_M = 0.075                  # a rail head's width; the rail lines lie half of it outside the gauge
TRACK = {                       # ballast top width, sleeper length, sleeper width, sleeper spacing (m)
    "rail": (3.4, 2.6, 0.25, 0.6),
    "narrow": (2.4, 1.7, 0.2, 0.6),
    "tram": (2.6, 2.4, 0.22, 0.6),
}
# stock materials the mod loads (roads.lua MATERIAL_FILES): West Coast USA's road decals, Gridmap v2's grid
BALLAST = {"m": "road_gravel", "tl": 8.0, "p": 19, "layer": "base"}
SLEEPER = {"m": "m_gm_concrete_plain", "tl": 2.0, "p": 4, "layer": "gutter"}
RAIL = {"m": "m_gm_metal_hotspotting", "tl": 4.0, "p": 1, "layer": "mark"}
STREET_TRACK = {"m": "road_tramline", "tl": 30.0, "p": 1, "layer": "mark", "w": 1.8}
ON_ROAD_INSET_M = 0.3           # a track point this far inside a carriageway's edge is on the street
SAMPLE_M = 1.0                  # m between the points tested for that
MIN_RUN_M = 2.0                 # shorter open / street runs join their neighbours
MARGIN_M = 4.0                  # ribbons run this far past the tile box (the road mesh cuts them at it)


def _chunk(look, w, pts):
    return {"m": look["m"], "w": round(float(w), 3), "tl": look["tl"], "p": look["p"], "layer": look["layer"],
            "n": [[round(float(p[0]), 3), round(float(p[1]), 3), 0.0] for p in pts]}


def _enu(frame, ll):
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
    return np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)


def _lonlat_box(frame, box, m):
    """(lon0, lat0, lon1, lat1) round the frame box (x0, y0, x1, y1) grown by m, and 100 m more (the frame's axes turn
    against the meridians away from its origin): every point within m of the box lies inside it."""
    x0, y0, x1, y1 = box[0] - m - 100.0, box[1] - m - 100.0, box[2] + m + 100.0, box[3] + m + 100.0
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
    return float(np.min(lo)), float(np.min(la)), float(np.max(lo)), float(np.max(la))


def _near(xy, box, m):
    x0, y0, x1, y1 = box
    return xy[:, 0].max() >= x0 - m and xy[:, 0].min() <= x1 + m and xy[:, 1].max() >= y0 - m and xy[:, 1].min() <= y1 + m


def _runs(on):
    """[(start, end, on)] index ranges (end inclusive) of equal values, runs under MIN_RUN_M / SAMPLE_M points
    merged into the one before; each run starts where the one before ends (no gap in the rails). Pure."""
    out = []
    for k, v in enumerate(on):
        if out and out[-1][2] == bool(v):
            out[-1][1] = k
        else:
            out.append([k, k, bool(v)])
    short = int(math.ceil(MIN_RUN_M / SAMPLE_M))
    merged = []
    for r in out:
        if merged and (r[1] - r[0] < short or merged[-1][1] - merged[-1][0] < short):
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    for k in range(1, len(merged)):
        merged[k][0] = merged[k - 1][1]
    return [tuple(r) for r in merged]


def _sleepers(xy, s, step, length, box, on_road):
    """Sleeper ribbons [(a, b)] across the track xy (arcs s) every step m of its arc from its start, the centre within
    the box grown by half a sleeper (each tile's mesh cuts at its edge: the halves meet), none on a carriageway."""
    x0, y0, x1, y1 = box
    g = length / 2 + 0.1
    at = np.arange(step / 2, s[-1], step)
    if not len(at):
        return []
    k = np.clip(np.searchsorted(s, at, side="right") - 1, 0, len(xy) - 2)
    d = xy[k + 1] - xy[k]
    L = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
    t = d / L[:, None]
    c = xy[k] + t * (at - s[k])[:, None]
    inside = (c[:, 0] >= x0 - g) & (c[:, 0] <= x1 + g) & (c[:, 1] >= y0 - g) & (c[:, 1] <= y1 + g)
    c, t = c[inside], t[inside]
    if not len(c):
        return []
    keep = ~on_road(c)
    n = np.c_[-t[keep, 1], t[keep, 0]] * (length / 2)
    return list(zip(c[keep] - n, c[keep] + n))


CROSS_WINDOW_M = 30.0           # m of track either side of where it crosses a carriageway's centre line (_crossing_windows)


def _crossing_windows(xy, lines):
    """The stretches of track xy (k, 2) within CROSS_WINDOW_M of where it crosses a carriageway centre line (lines),
    overlapping ones joined: [(k, 2)]. Pure."""
    import roads
    cr = roads._crossings([xy], lines) if lines else []
    if not cr:
        return []
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    spans = []
    for arc in sorted(c[1] for c in cr):
        a, b = max(0.0, arc - CROSS_WINDOW_M), min(float(s[-1]), arc + CROSS_WINDOW_M)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = b
        else:
            spans.append([a, b])
    out = []
    for a, b in spans:
        inner = xy[(s > a) & (s < b)]
        out.append(np.vstack([[np.interp(a, s, xy[:, 0]), np.interp(a, s, xy[:, 1])], inner,
                              [np.interp(b, s, xy[:, 0]), np.interp(b, s, xy[:, 1])]]))
    return out


def tile_chunks(frame, tx, ty, cells):
    """The track chunks of road tile (tx, ty) (anchor frame): ballast, sleepers, then the rails (material order: the
    road mesh draws them in that order). cells: the roads Features of the cells touching the tile."""
    import railways
    import roads
    box = roads.tile_bbox(tx, ty)
    reach = max(t[0] for t in TRACK.values()) + MARGIN_M
    carriageways, tracks, seen = {}, [], set()
    llb = _lonlat_box(frame, box, reach + 20.0)
    for feats in cells:
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs:
            continue
        sub, cls = feats.attrs["subtype"], feats.attrs["class"]
        fb = roads._feature_boxes(feats)        # (lon0, lat0, lon1, lat1) per feature
        far = (fb[:, 2] < llb[0]) | (fb[:, 0] > llb[2]) | (fb[:, 3] < llb[1]) | (fb[:, 1] > llb[3])
        specs = None
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            # the lines railways.py builds as a 3D track (mpsteppe's PR #287; railways.drawn, worn ones too): only where
            # they cross a carriageway (its street track across the road, which railways.py leaves to the road),
            # nothing on open ground
            got = railways.drawn(feats, i) if str(sub[i]) == "rail" else None
            meshed = got is not None
            kind = KINDS.get(got[0] if meshed else str(cls[i])) if str(sub[i]) == "rail" else None
            if str(sub[i]) == "road":
                if specs is None:
                    specs = roads.road_specs(feats)
                if specs[i] is None:
                    continue
            elif kind is None:
                continue
            seen.add(fid)
            if far[i]:
                continue                        # (no part of it near the tile: converting each to find that out was
                                                # most of this, 2026-10-25)
            for k, part in enumerate(roads.ground_parts(feats, i)):
                xy = _enu(frame, np.asarray(part, np.float64))
                if len(xy) < 2 or not _near(xy, box, reach + 20.0):
                    continue
                if kind is None:
                    carriageways[(fid, k)] = (xy, specs[i].width / 2)
                else:
                    tracks.append((kind, xy, meshed))
    if not tracks:
        return []

    def on_road(pts):
        return roads._on_carriageways(pts, carriageways, -ON_ROAD_INSET_M)

    ballast, sleepers, rails = [], [], []
    cw_lines = [ln for ln, _hw in carriageways.values()]
    todo = []
    for kind, xy, meshed in tracks:
        if meshed and kind != "tram":
            # (a main line's only street track is where it crosses a road: just those stretches, every metre of every
            # line in reach tested against every carriageway cost a tile ~0.1 s, 2026-10-25)
            todo += [(kind, w, meshed) for w in _crossing_windows(xy, cw_lines)]
        else:
            todo.append((kind, xy, meshed))
    for kind, xy, meshed in todo:
        bw, sl, sw, step = TRACK[kind]
        off = GAUGE_M[kind] / 2 + HEAD_M / 2
        full = roads.densify(xy, SAMPLE_M)
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(full, axis=0).T))]
        for a, b in (() if meshed else _sleepers(full, s, step, sl, box, on_road)):
            sleepers.append(_chunk(SLEEPER, sw, (a, b)))
        for piece in roads.clip_polyline(full, box[0] - MARGIN_M, box[1] - MARGIN_M, box[2] + MARGIN_M,
                                         box[3] + MARGIN_M):
            piece = roads.densify(piece, SAMPLE_M)
            if len(piece) < 2:
                continue
            for a, b, street in _runs(on_road(piece)):
                run = piece[a:b + 1]
                if len(run) < 2:
                    continue
                if street:
                    rails.append(_chunk(STREET_TRACK, STREET_TRACK["w"], run))
                    continue
                if meshed:
                    continue
                ballast.append(_chunk(BALLAST, bw, run))
                for side in (off, -off):
                    rails.append(_chunk(RAIL, HEAD_M, roads.offset_polyline(run, side)))
    return ballast + sleepers + rails
