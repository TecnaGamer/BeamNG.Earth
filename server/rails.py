"""Railway and tram tracks on the road mesh.

Overture's rail segments (subtype "rail", in the roads layer; each OSM way one track) on the ground (bridge and tunnel
ranges left out) as draped chunks of the road tile: on open ground a ballast bed, the sleepers across it and two rails;
where the track runs on a carriageway (a street tram, a level crossing) West Coast USA's street track instead: its
road_tramline decal, both rails in one 1.8 m ribbon (textureLength 30, as in its DecalRoads). Standard gauge. Flat: no
collision, as the markings. Subways are left out.
The classes railways.py builds as a colliding 3D track on the ground and on bridges (standard and narrow gauge, light
rail: mpsteppe's PR #287) get only their street track here, where they cross a carriageway (railways.py stops
its track at the road's edge). Trams get all their ground track here; mapped tram bridges use railways.py's embedded slab.
"""
import math

import numpy as np
import dnp

import railxing

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
# a level crossing's deck (BNE_RAIL_XING, railxing.py): a rubber panel strip along each track across the carriageway, its
# two rails on it (from ingest_rail_textures.py's rubber tile)
DECK = {"m": "bne_rail_xing_rubber", "tl": 3.2, "p": 2, "layer": "gutter"}
DECK_EXT = 3                    # samples (m) the deck's rails run on past the carriageway's edge, each side
DECK_RAIL_W = 0.13              # m: a rail's ribbon on the deck
DECK_W = 3.2                    # m: the rails' gauge and 0.9 m of panel outside each
STREET_TRACK = {"m": "road_tramline", "tl": 30.0, "p": 1, "layer": "mark", "w": 1.8}
ON_ROAD_INSET_M = 0.3           # a track point this far inside a carriageway's edge is on the street
SAMPLE_M = 1.0                  # m between the points tested for that
MIN_RUN_M = 2.0                 # shorter open / street runs join their neighbours
MARGIN_M = 4.0                  # ribbons run this far past the tile box (the road mesh cuts them at it)
# Sweden's town trams. A reserved track keeps its middle at least 3.5 m from the road's edge (VGU 2024 K235543, at 40 km/h),
# so one nearer lies in the street, VGU's gatuspår, not on a bed of its own. OpenStreetMap's embedded tag isn't in the data.
GATUSPAR_M = 3.5
# (3.4 m wide: Göteborg's double tracks lie 3.24-4.19 m apart, half of them, so a pair's paving mostly meets; no official width)
EMBEDDED = {"m": "road_asphalt_2lane", "tl": 11.0, "p": 19, "layer": "base", "w": 3.4}


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
    L = np.maximum(dnp.hypot(d[:, 0], d[:, 1]), 1e-9)
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


def _crossing_windows(xy, lines, extra=()):
    """The stretches of track xy (k, 2) within CROSS_WINDOW_M of where it crosses a carriageway centre line (lines), and
    the arc ranges extra [(a, b)] (railways.street_spans: where it runs down a street), overlapping ones joined:
    [(k, 2)]. Pure."""
    import roads
    cr = roads._crossings([xy], lines) if lines else []
    if not cr and not extra:
        return []
    s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
    spans = []
    ranges = [(max(0.0, arc - CROSS_WINDOW_M), min(float(s[-1]), arc + CROSS_WINDOW_M)) for arc in (c[1] for c in cr)]
    for a, b in sorted(ranges + [(float(a), float(b)) for a, b in extra]):
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    out = []
    for a, b in spans:
        inner = xy[(s > a) & (s < b)]
        out.append(np.vstack([[np.interp(a, s, xy[:, 0]), np.interp(a, s, xy[:, 1])], inner,
                              [np.interp(b, s, xy[:, 0]), np.interp(b, s, xy[:, 1])]]))
    return out


def tram_lines(frame, tx, ty, cells):
    """[(centreline (k, 2), half the track's width)] of the trams on the ground near road tile (tx, ty): what a raised
    sidewalk or median stops at (Sweden's: roads.build_tile)."""
    import roads
    box = roads.tile_bbox(tx, ty)
    out, seen = [], set()
    for feats in cells:
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs:
            continue
        hit = (np.asarray(feats.attrs["subtype"]).astype(str) == "rail") & (np.asarray(feats.attrs["class"]).astype(str) == "tram")
        for i in np.flatnonzero(hit):
            if str(feats.ids[i]) in seen:
                continue
            seen.add(str(feats.ids[i]))
            for part in roads.ground_parts(feats, i):
                xy = _enu(frame, np.asarray(part, np.float64))
                if len(xy) >= 2 and _near(xy, box, 60.0):
                    out.append((xy, EMBEDDED["w"] / 2))
    return out


def tile_chunks(frame, tx, ty, cells, country=None, urban_fn=None):
    """The track chunks of road tile (tx, ty) (anchor frame): ballast, sleepers, then the rails (material order: the
    road mesh draws them in that order). cells: the roads Features of the cells touching the tile. Sweden (country),
    in town (urban_fn, as road_specs): a tram beside a carriageway is street track on paving (GATUSPAR_M)."""
    import railways
    import roads
    box = roads.tile_bbox(tx, ty)
    reach = max(t[0] for t in TRACK.values()) + MARGIN_M
    carriageways, town_ways, tracks, seen = {}, {}, [], set()
    road_classes = {}
    sweden = str(country or "").upper() == "SE"
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
            if str(sub[i]) == "rail" and not meshed and str(cls[i]) != "tram":
                continue
            kind = KINDS.get(got[0] if meshed else str(cls[i])) if str(sub[i]) == "rail" else None
            if str(sub[i]) == "road":
                if specs is None:
                    specs = roads.road_specs(feats, urban_fn if sweden else None)
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
                    road_classes[(fid, k)] = str(cls[i]).removesuffix("_link")
                    if sweden and specs[i].urban:
                        town_ways[(fid, k)] = carriageways[(fid, k)]
                else:
                    tracks.append((kind, xy, meshed, got[1] if meshed else 0))
    if not tracks:
        return []

    def on_road(pts, eligible):
        return roads._on_carriageways(pts, eligible, -ON_ROAD_INSET_M)

    def in_street(pts):
        return roads._on_carriageways(pts, town_ways, GATUSPAR_M) if town_ways else np.zeros(len(pts), bool)

    def no_sleeper(pts, eligible):
        return on_road(pts, eligible) | in_street(pts)

    ballast, sleepers, rails = [], [], []
    todo = []
    for kind, xy, meshed, wear in tracks:
        # (a worn line's street track only across the roads its 3D track is not cut at: railways.WORN_CUT_CLASSES,
        # #918; else its old bed came back as street rails across the motorway, wyo-backroads' PR #1117)
        excluded = railways.WORN_CUT_CLASSES.get(wear, ())
        eligible = {key: value for key, value in carriageways.items() if road_classes[key] not in excluded}
        cw_lines = [ln for ln, _hw in eligible.values()]
        if meshed and kind != "tram":
            # (a main line's only street track is where it crosses a road: just those stretches, every metre of every
            # line in reach tested against every carriageway cost a tile ~0.1 s, 2026-10-25)
            # (and where it runs down a street: railways.street_along cuts its 3D track there, Fells Point's trolley
            # tracks down Thames Street, the user's report 16)
            todo += [(kind, w, meshed, eligible)
                     for w in _crossing_windows(xy, cw_lines, railways.street_spans(xy, list(eligible.values())))]
        else:
            todo.append((kind, xy, meshed, eligible))
    for kind, xy, meshed, eligible in todo:
        bw, sl, sw, step = TRACK[kind]
        off = GAUGE_M[kind] / 2 + HEAD_M / 2
        full = roads.densify(xy, SAMPLE_M)
        s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(full, axis=0).T))]
        gap = (lambda q, e=eligible: no_sleeper(q, e)) if kind == "tram" else (lambda q, e=eligible: on_road(q, e))
        for a, b in (() if meshed else _sleepers(full, s, step, sl, box, gap)):
            sleepers.append(_chunk(SLEEPER, sw, (a, b)))
        for piece in roads.clip_polyline(full, box[0] - MARGIN_M, box[1] - MARGIN_M, box[2] + MARGIN_M,
                                         box[3] + MARGIN_M):
            piece = roads.densify(piece, SAMPLE_M)
            if len(piece) < 2:
                continue
            for a, b, street in _runs(on_road(piece, eligible)):
                run = piece[a:b + 1]
                if len(run) < 2:
                    continue
                if street:
                    if railxing.ENABLED and meshed and kind in ("rail", "narrow"):
                        # (past the carriageway's edge: the 3D track stops a sample (2 m) or so short of it, the deck's
                        # panels reach a metre beyond the road and its rails (wider than the open track's: they read
                        # as steel on the dark rubber) run on until they meet it)
                        lo, hi = max(0, a - DECK_EXT), min(len(piece) - 1, b + DECK_EXT)
                        sleepers.append(_chunk(DECK, DECK_W, piece[max(0, a - 1):min(len(piece) - 1, b + 1) + 1]))
                        for side in (off, -off):
                            rails.append(_chunk(RAIL, DECK_RAIL_W, roads.offset_polyline(piece[lo:hi + 1], side)))
                        continue
                    rails.append(_chunk(STREET_TRACK, STREET_TRACK["w"], run))
                    continue
                if meshed:
                    continue
                for c, d, paved in (_runs(in_street(run)) if kind == "tram" else [(0, len(run) - 1, False)]):
                    part = run[c:d + 1]
                    if len(part) < 2:
                        continue
                    if paved:
                        ballast.append(_chunk(EMBEDDED, EMBEDDED["w"], part))
                        rails.append(_chunk(STREET_TRACK, STREET_TRACK["w"], part))
                        continue
                    ballast.append(_chunk(BALLAST, bw, part))
                    for side in (off, -off):
                        rails.append(_chunk(RAIL, HEAD_M, roads.offset_polyline(part, side)))
    return ballast + sleepers + rails
