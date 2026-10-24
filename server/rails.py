"""Railway and tram tracks on the road mesh.

Overture's rail segments (subtype "rail", in the roads layer; each OSM way one track) on the ground (bridge and tunnel
ranges left out) as draped chunks of the road tile: on open ground a ballast bed, the sleepers across it and two rails;
where the track runs on a carriageway (a street tram, a level crossing) West Coast USA's street track instead: its
road_tramline decal, both rails in one 1.8 m ribbon (textureLength 30, as in its DecalRoads). Standard gauge. Flat: no
collision, as the markings. Subways and the rail classes Overture leaves unknown are left out.
"""
import math

import numpy as np

KINDS = {"standard_gauge": "rail", "narrow_gauge": "narrow", "light_rail": "tram", "tram": "tram"}
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
    return np.c_[e, n]


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


def tile_chunks(frame, tx, ty, cells):
    """The track chunks of road tile (tx, ty) (anchor frame): ballast, sleepers, then the rails (material order: the
    road mesh draws them in that order). cells: the roads Features of the cells touching the tile."""
    import roads
    box = roads.tile_bbox(tx, ty)
    reach = max(t[0] for t in TRACK.values()) + MARGIN_M
    carriageways, tracks, seen = {}, [], set()
    for feats in cells:
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs:
            continue
        sub, cls = feats.attrs["subtype"], feats.attrs["class"]
        specs = None
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            kind = KINDS.get(str(cls[i])) if str(sub[i]) == "rail" else None
            if str(sub[i]) == "road":
                if specs is None:
                    specs = roads.road_specs(feats)
                if specs[i] is None:
                    continue
            elif kind is None:
                continue
            seen.add(fid)
            for k, part in enumerate(roads.ground_parts(feats, i)):
                xy = _enu(frame, np.asarray(part, np.float64))
                if len(xy) < 2 or not _near(xy, box, reach + 20.0):
                    continue
                if kind is None:
                    carriageways[(fid, k)] = (xy, specs[i].width / 2)
                else:
                    tracks.append((kind, xy))
    if not tracks:
        return []

    def on_road(pts):
        return roads._on_carriageways(pts, carriageways, -ON_ROAD_INSET_M)

    ballast, sleepers, rails = [], [], []
    for kind, xy in tracks:
        bw, sl, sw, step = TRACK[kind]
        off = GAUGE_M[kind] / 2 + HEAD_M / 2
        full = roads.densify(xy, SAMPLE_M)
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(full, axis=0).T))]
        for a, b in _sleepers(full, s, step, sl, box, on_road):
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
                ballast.append(_chunk(BALLAST, bw, run))
                for side in (off, -off):
                    rails.append(_chunk(RAIL, HEAD_M, roads.offset_polyline(run, side)))
    return ballast + sleepers + rails
