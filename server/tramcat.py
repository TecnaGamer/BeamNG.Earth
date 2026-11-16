"""Tram overhead line: poles and the contact wire along electrified street trams (BNE_TRAM=1 with BNE_CATENARY=1).

catenary.py's lines are the main-line kinds (railways.parts leaves the ground trams to rails.py); a tram has no messenger
wire and, in a street, no mast of its own beside the track. The way a street tram is wired (Prague, Vinnytsia: the user,
2026-11-06, "where the tracks are in the middle of the road the poles are usually on the pavements or the buildings"):
  - the track lies in the street (within tramtrack.STREET_MARGIN_M of a carriageway): a pole on the pavement each side, or
    where a building stands there a fixing on its wall (no pole), a span wire across between them and the contact wire
    hung from it over the track's axis (a hanger). One side only where the other has no room: the span wire from there;
  - the track runs on its own, beside a road or not: the railway's cantilever mast (catenary.GROUP) at its axis' side with
    room, the contact wire at the arm's tip, as the railway's.
The poles stand every SPAN_M (shorter on a curve) from a phase hashed from the line, the same in every tile; a pole near
another (SHARE_M) is that one (two tracks side by side share their poles). Spans are drawn by the tile that holds their
axis point. Plan 046: the poles and wires are the kernel's (native/earthfast/src/tram/: poles.rs plans them, wires.rs hangs the wires);
this module reads the map's features into arrays, calls `tram_overhead` once a tile and keeps the tile's own items.
Pure given its inputs; the heights are one call of ground."""
import numpy as np
import dmath
import dnp

import catenary as cat
import fast
import tramtrack

ENABLED = tramtrack.ENABLED and cat.ENABLED
KERNEL = 310                # the kernel version that has tram_overhead
REACH_M = 130.0             # m round the tile the lines are read


def tram_parts(frame, cells, box, reach=REACH_M):
    """[(fid, xy (k, 2), kind)] ("ground" or "bridge") of the electrified trams near box, sorted (the same in every tile). A tram's flags: none
    (disused, abandoned, unbuilt) or an OSM electrified value that is not a contact line leaves it out; no value is a
    contact line."""
    import mapgeom
    import railways
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    out, seen = [], set()
    for feats in cells:
        if feats is None or not len(feats) or "subtype" not in feats.attrs:
            continue
        sub, cls = feats.attrs["subtype"], feats.attrs.get("class")
        osm, el = feats.attrs.get("osm_railway"), feats.attrs.get("osm_electrified")
        fb = feats.boxes(frame)
        for i in range(len(feats)):
            if str(sub[i]) != "rail":
                continue
            tram = (cls is not None and str(cls[i]) == "tram") or (osm is not None and osm[i] and str(osm[i]) == "tram")
            fid = str(feats.ids[i])
            if not tram or fid in seen or not railways._box_hit(fb[i], near):
                continue
            seen.add(fid)
            if railways.wear(feats, i) != 0:
                continue
            if el is not None and el[i] and str(el[i]).split(";")[0].strip() not in cat.ELECTRIC:
                continue
            for kind, rings in railways._pieces(feats, i):
                for ring in rings:
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    xy = np.c_[e, n]
                    if len(xy) >= 2 and railways._box_hit((xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(),
                                                            xy[:, 1].max()), near):
                        xy = railways._resample(xy, 2.0)
                        if len(xy) >= 2:
                            out.append((fid, xy, kind))
    out.sort(key=lambda p: (p[0], p[2], float(p[1][0, 0]), float(p[1][0, 1])))
    return out


def _rail_axes(frame, cells, box):
    """[xy (k, 2)] of the railway tracks (not trams) near box: the poles stay off them and a tram wire over one is insulated."""
    import railways
    return [np.asarray(xy, np.float64) for _fid, _c, kind, xy in railways.parts(frame, cells, box, reach=REACH_M)
            if kind == "ground" and len(xy) >= 2]


def _track_lines(frame, cells, box):
    """[line (k, 2)] of every ground tram track near box, electrified or not, ways that meet end to end one line (the tram
    corridor's, tram/corridor.rs): no pole of the overhead line stands within POLE_TRACK_CLEAR_M of one."""
    import tramtrack
    return tramtrack.corridor_lines(tramtrack.corridor_parts(frame, cells, box, REACH_M))


def consts():
    """{name: value} of the kernel's rule numbers (SPAN_M, SIDEWALK_OFF_M, CONTACT_Z, ...)."""
    fast.require(KERNEL, "tram overhead line")
    return dict(fast._ef.tram_consts())


def _flat(arrays, width=2):
    """(points (n, width), offsets (k + 1,)) of a list of (m_i, >=width) arrays."""
    off = np.zeros(len(arrays) + 1, np.int64)
    for i, a in enumerate(arrays):
        off[i + 1] = off[i] + len(a)
    pts = np.concatenate([np.asarray(a, np.float64)[:, :width] for a in arrays]) if arrays else np.zeros((0, width))
    return np.ascontiguousarray(pts, np.float64), off


def overhead(frame, cells, box, ground, rings=(), country=None, decks=()):
    """(rows, row owners (n, 2), wires [(xyz (k, 3), half width)], wire owners (w, 2), poles [(x, y, wall, z or None)], gaps
    [(x, y, m)]: the stretches of contact wire with no support for more than MAX_UNSUPPORTED_M) of
    the trams round box, not yet cut to a tile (the owner points say whose each item is). decks: [(xy, z, width)] the road
    bridge decks near. ground(xy (n, 2)) -> z."""
    fast.require(KERNEL, "tram overhead line")
    empty = [], np.zeros((0, 2)), [], np.zeros((0, 2)), [], []
    parts = tram_parts(frame, cells, box)
    if not parts:
        return empty
    roads = cat._road_lines(frame, cells, box, REACH_M)
    pp, po = _flat([xy for _f, xy, _k in parts])
    rp, ro = _flat([ln for ln, _hw in roads])
    gp, go = _flat([np.asarray(r, np.float64) for r in rings])
    dp, do = _flat([np.asarray(d[0], np.float64) for d in decks])
    dz = np.ascontiguousarray(np.concatenate([np.asarray(d[1], np.float64).ravel()[:len(d[0])] for d in decks])
                              if decks else np.zeros(0), np.float64)
    rail = _rail_axes(frame, cells, box)
    ap, ao = _flat(rail)
    tp, to = _flat(_track_lines(frame, cells, box))
    return fast._ef.tram_overhead(
        [str(f) for f, _x, _k in parts], np.array([k == "bridge" for _f, _x, k in parts], bool), pp, po,
        rp, ro, np.array([hw for _ln, hw in roads], np.float64), gp, go, dp, dz, do,
        np.array([float(d[2]) for d in decks], np.float64), ap, ao, tp, to, ground, str(country or "").upper())


def build(frame, tx, ty, cells, ground, rings=(), country=None, decks=()):
    """(prop rows, wire lines) of road tile (tx, ty): the tram overhead lines round it (catenary.build adds them).
    decks: [(xy, z, width)] the road bridge decks round it (blockworker._road_decks)."""
    import roads as roadsmod
    box = roadsmod.tile_bbox(tx, ty)
    rows, rxy, wires, wxy, _poles, _gaps = overhead(frame, cells, box, ground, rings, country, decks)
    if not rows and not wires:
        return [], []
    own_r = roadsmod.box_owns(box, rxy[:, 0], rxy[:, 1]) if len(rows) else []
    own_w = roadsmod.box_owns(box, wxy[:, 0], wxy[:, 1]) if len(wires) else []
    rows = [r for r, o in zip(rows, own_r) if o]
    import furniture
    return rows + furniture.lamp_lenses([r for r in rows if r[0] in LAMP_BRACKETS]), [w for w, o in zip(wires, own_w) if o]


LAMP_BRACKETS = frozenset({"tram_lamp_g", "tram_lamp_k", "tram_lamp_t"})       # the lamp brackets on the street poles (tram/wires.rs)
POLE_GROUPS = frozenset(f"tram_{fam}_{kind}" for fam in ("tube", "square", "lattice", "concrete")
                        for kind in ("span", "arm", "centre")) | {"tram_rosette"}


def drop_lamps(props, tram_rows):
    """props without the street lamps (and their lenses and glows) within the kernel's LAMP_CLEAR_M of a tram pole that
    carries a lamp bracket: the bracket is that street's lamp (plan 046). tram_rows: build's rows."""
    if not props or not tram_rows:
        return props
    import furniture
    at = [r[1:3] for r in tram_rows if r[0] in LAMP_BRACKETS]
    if not at:
        return props
    lamps = [i for i, r in enumerate(props) if r[0] in furniture.LAMP_ROW_GROUPS]
    if not lamps:
        return props
    fast.require(KERNEL, "tram lamps")
    near = fast._ef.tram_near(np.array([props[i][1:3] for i in lamps], np.float64), np.array(at, np.float64),
                              consts()["LAMP_CLEAR_M"])
    gone = {i for i, n in zip(lamps, near) if n}
    return [r for i, r in enumerate(props) if i not in gone] if gone else props
