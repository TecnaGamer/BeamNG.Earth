"""Snowy roads, sidewalks and snowbanks (winter W3, ~/bnge-tests/winterplan/PLAN.md 2.7, 2.8, 3.3, 3.4). Glue around
earthfast's snowroad.rs (the lane sheet: zones, classes, banks, the state's look); BNE_WINTER=on turns it on with the
rest of winter, BNE_WINTER_ROADS=off leaves it out (W1 and W2 as before). Off: nothing here is read, keyed or built.

What it changes, all keyed by the snow state that made it:
  - the ground photos (snowcover.road_classes): the carriageways' texels by plow class AND zone (lanes, wheel tracks,
    edge), so a plowed main road shows wet black lanes with slush at its edges and a side street packed snow with grey
    tracks; the shares come from W1's road rows (snow.py: Table.road, 15 slots);
  - the geotile skins (blockworker._geo_skin_build, skin_key): the markings, crossings and bridge deck asphalt drawn with
    our winter copies per plow class (bne_snow_<material>_p<class>: the mod fades the paint under the snow and whitens
    the decks live, winterroad.lua), the sidewalk slabs by street kind (bne_snow_walk_<slot>), and the snowbanks along
    the plowed roads' edges (bne_snowbank_p<class>) at the state's bank bin per class;
  - the snowstate message's "look" (snow.py): what the mod sets on those materials.

The skins' winter part is the wear argument's "snow" (earthd.Server.wear_arg: {"q": 1, "banks": [bin per class]}):
roadwear.wear_key never reads it (the geotile content stays season-free), skin_key does.
"""
import os

import numpy as np

import fast

NATIVE = 640
_W = os.environ.get("BNE_WINTER", "").strip().lower()
ON = _W in ("on", "1", "yes", "true", "real", "light", "heavy", "ice") \
    and os.environ.get("BNE_WINTER_ROADS", "on").strip().lower() not in ("off", "0", "no", "false")
VERSION = 1                 # the skins' winter part (bump with any change of what the skin draws from it)
CLASSES = 5
# the paint and the deck asphalt that get winter copies per class (winterroad.lua makes them from the stock ones)
MARK_MATERIALS = ("line_white", "line_yellow", "crossing_white")
DECK_MATERIALS = ("road_asphalt_2lane",)
WALK_MATERIALS = ("sidewalk1",)
WALK_SLOTS = ("s0", "s1c", "s1u", "s2")     # main streets, residential cleared, residential left, unmaintained
CROSSING_MATERIALS = ("crossing_white", "crossings")
CLASS_EXTRA_M = 2.5         # a chunk this far past a road's half width still takes its class
WALK_EXTRA_M = 7.0          # a sidewalk slab this far past it
LINE_MARGIN_M = 60.0        # the roads read round a tile
CORNER_TEXEL_M = 0.6        # photos this fine take the junctions' curb corners into their paved mask


def mark_name(m, c):
    return f"bne_snow_{m}_p{int(c)}"


def bank_name(c):
    return f"bne_snowbank_p{int(c)}"


def walk_name(slot):
    return f"bne_snow_walk_{slot}"


def _ef():
    fast.require(NATIVE, "snowy roads")
    return fast._ef


def snow_of(wear):
    """The wear argument's winter part ({"q", "banks"}) or None."""
    if not ON or not isinstance(wear, dict):
        return None
    s = wear.get("snow")
    if not isinstance(s, dict) or not s.get("q"):
        return None
    return s


def skin_key(wear):
    """A skin's revisit key part: () with no winter on its roads (the key as it was)."""
    s = snow_of(wear)
    if s is None:
        return ()
    return (("snowroad", VERSION, int(s["q"]), tuple(int(b) for b in s.get("banks", ())[:CLASSES])),)


WALK_CLEAR = 3              # snowroad.rs W_CLEAR: a sidewalk with no snow on it


def bare(look):
    """Is a state's look one with no winter on the roads (snowroad.rs look under no ground cover: every class's paint
    shown whole, no deck under snow, no bank, every sidewalk clear)? Its skins are then drawn as without winter: a
    wet or dry road with no snow gives the same skins (and keys) as winter off. Missing parts count as bare."""
    if any(int(b) > 0 for b in look.get("bank_bin") or ()):
        return False
    if any(int(m) < 1000 for m in look.get("marking") or ()):
        return False
    if any(int(d) > 0 for d in look.get("deck_cover") or ()):
        return False
    return all(int(w[1]) <= 0 and int(w[0]) == WALK_CLEAR for w in look.get("walks") or ())


def wear_snow(look):
    """The wear argument's winter part from a state's look (snow.py: snowroad_look), or None (no snow on the roads,
    bare: the skins as they were). Baltimore, 2026-11-28: a bare look's {"q": 1, banks 0} differed from None, so a
    teleport and the place's first state (no snow before or after) each sent every road tile again."""
    if not ON or not look or bare(look):
        return None
    return {"q": 1, "banks": [int(b) for b in look["bank_bin"]]}


# ---------------------------------------------------------------------------------------------------------------------
# The lane sheet from the map's road features

def road_lines(feats_list, frame, urban=None, widths=None, ground_only=True, bounds=None, bridges=False):
    """The roads of feats_list as a line set in frame m: (pts (n, 2), off (L + 1,) int64, hw (L,), cls (L,) uint8,
    lanes (L,) uint8, oneway (L,) bool, fids [L]); ground_only: their ground parts (no deck, no tunnel; the banks'),
    else every part (any point's class); bridges: their bridge parts alone (the banks' cut lines). bounds (x0, y0,
    x1, y1): lines whose box, grown by their half width, misses it are left out. Each feature once (its first copy)."""
    import mapgeom
    import roads as roadsmod
    pts, off, hw, names, lanes, oneway, fids = [], [0], [], [], [], [], []
    seen = set()
    for feats in feats_list or ():
        if feats is None or len(feats) == 0:
            continue
        specs = roadsmod.road_specs(feats, urban, widths)
        ids = feats.ids.tolist()
        for i in range(len(feats)):
            sp = specs[i]
            fid = str(ids[i])
            if sp is None or fid in seen:
                continue
            seen.add(fid)
            parts = roadsmod.bridge_parts(feats, i) if bridges else roadsmod.ground_parts(feats, i) if ground_only \
                else feats.parts(i)
            for ring in parts:
                ring = np.asarray(ring[0] if len(ring) and np.ndim(ring[0]) == 2 else ring, np.float64)
                if len(ring) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, ring)
                xy = np.c_[np.asarray(e, np.float64), np.asarray(n, np.float64)]
                h = float(sp.width) / 2.0
                # (the ring's box against the bounds: a long straight piece crosses them with no vertex inside)
                if bounds is not None and (xy[:, 0].max() < bounds[0] - h or xy[:, 0].min() > bounds[2] + h
                                           or xy[:, 1].max() < bounds[1] - h or xy[:, 1].min() > bounds[3] + h):
                    continue
                pts.append(xy)
                off.append(off[-1] + len(xy))
                hw.append(h)
                names.append(str(sp.cls or ""))
                lanes.append(max(1, min(int(sp.lanes or 2), 12)))
                oneway.append(bool(sp.oneway))
                fids.append(fid)
    cls = np.frombuffer(bytes(_ef().snowroad_classes(names)), np.uint8).copy() if names else np.zeros(0, np.uint8)
    return (np.ascontiguousarray(np.concatenate(pts) if pts else np.zeros((0, 2)), np.float64),
            np.asarray(off, np.int64), np.asarray(hw, np.float64), cls, np.asarray(lanes, np.uint8),
            np.asarray(oneway, bool), fids)


def class_at(points, lines, extra=CLASS_EXTRA_M):
    """(line, class, side) arrays of each point's nearest road (class 255: none)."""
    pts, off, hw, cls = lines[:4]
    p = np.ascontiguousarray(np.asarray(points, np.float64).reshape(-1, 2))
    if len(hw) == 0 or len(p) == 0:
        n = len(p)
        return np.full(n, -1, np.int32), np.full(n, 255, np.uint8), np.zeros(n, np.int8)
    return _ef().snowroad_class_at(p, pts, off, hw, cls, float(extra))


# ---------------------------------------------------------------------------------------------------------------------
# The ground photos

def photo_mask(frame, x0, y0, size, px, surface, widths=None):
    """The ground photo's road mask (px x px uint8: 0 none, 1 + zone * 5 + class) of [x0, x0 + size]^2: the painted
    carriageways (surface._road_segments: the same segments the photo's asphalt is painted with) by the class, lanes and
    one-way of the nearest mapped road and the zone across it; parking lots, pads and corners the paved mask's (P2
    lanes). None: no surface, texels coarser than surface.ROAD_MASK_MAX_TEXEL, no roads."""
    import surface as surfacemod
    if surface is None or size / max(px, 1) > surfacemod.ROAD_MASK_MAX_TEXEL:
        return None
    feats = surfacemod._square_roads(frame, x0, y0, size, surface)
    # (the junctions' curb corners too where the texels are fine (a block's photo): left out, every corner of a plowed
    # junction was a white wedge; a far tile's coarser texels go without them, their time)
    paved = surfacemod.road_mask(frame, x0, y0, size, px, surface, widths, corners=size / max(px, 1) <= CORNER_TEXEL_M)
    code = None
    if feats:
        roads_of = []
        got = surfacemod._road_segments(frame, x0, y0, size, px, px, feats, surface.urban, widths, roads_of=roads_of)
        if got is not None:
            segs, hws, kind = (np.asarray(a) for a in got)
            road = np.asarray(roads_of[0][0], np.int64) if roads_of else np.arange(len(hws), dtype=np.int64)
            names = list(surfacemod.PHOTO_ROAD_RGBA)
            ok = np.array([names[int(k)] in surfacemod.ROAD_MASK_KINDS if int(k) < len(names) else False
                           for k in kind], bool)
            hws = np.where(ok, np.asarray(hws, np.float64), 0.0)
            k = size / px
            mid = np.c_[x0 + 0.5 * (segs[:, 0] + segs[:, 2]) * k, y0 + size - 0.5 * (segs[:, 1] + segs[:, 3]) * k]
            lines = road_lines(feats, frame, surface.urban, widths, ground_only=False,
                               bounds=(x0 - 20.0, y0 - 20.0, x0 + size + 20.0, y0 + size + 20.0))
            li, cls, _side = class_at(mid, lines, 6.0)
            li = np.asarray(li)
            lanes = np.where(li >= 0, lines[4][np.maximum(li, 0)] if len(lines[4]) else 2, 2).astype(np.uint8)
            oneway = np.where(li >= 0, lines[5][np.maximum(li, 0)] if len(lines[5]) else False, False).astype(bool)
            cls = np.where(np.asarray(cls) == 255, 2, cls).astype(np.uint8)
            code = np.asarray(_ef().snowroad_zones(np.ascontiguousarray(segs, np.float64), np.ascontiguousarray(hws),
                                                   np.ascontiguousarray(road), np.ascontiguousarray(cls),
                                                   np.ascontiguousarray(lanes), np.ascontiguousarray(oneway),
                                                   int(px), float(k)), np.uint8)
    if paved is not None:
        lot = (np.asarray(paved) >= 128).astype(np.uint8) * 3      # (1 + P2: lanes)
        code = lot if code is None else np.where(code > 0, code, lot).astype(np.uint8)
    if code is None or not code.any():
        return None
    return code


# ---------------------------------------------------------------------------------------------------------------------
# The skins

def _chunk_mid(c):
    n = c.get("n")
    if n is None or len(n) == 0:
        return None
    a = np.asarray(n, np.float64)
    return a[len(a) // 2, :2] if len(a) % 2 else 0.5 * (a[len(a) // 2 - 1, :2] + a[len(a) // 2, :2])


def remap_chunks(chunks, lines):
    """The skin's chunks with the paint and the deck asphalt on their winter copies per class (new dicts where
    changed; the class of the nearest road; none found: P2)."""
    idx, mids = [], []
    for i, c in enumerate(chunks):
        m = c.get("m")
        if m in MARK_MATERIALS or (m in DECK_MATERIALS and c.get("deck")):
            p = _chunk_mid(c)
            if p is not None:
                idx.append(i)
                mids.append(p)
    if not idx:
        return chunks
    _li, cls, _s = class_at(np.asarray(mids), lines)
    out = list(chunks)
    for i, k in zip(idx, np.asarray(cls).tolist()):
        c = dict(out[i])
        k = 2 if k == 255 else int(k)
        c["m"] = mark_name("deck" if c["m"] in DECK_MATERIALS else c["m"], k)
        out[i] = c
    return out


def crossing_cuts(chunks):
    """Discs (x, y, r) where the crossings are: no bank across them (snowroad.rs crossing_discs)."""
    lines, widths = [], []
    for c in chunks:
        m = c.get("m")
        if m in CROSSING_MATERIALS or (isinstance(m, str) and m.startswith("bne_snow_crossing")):
            n = c.get("n")
            if n is None:
                continue
            a = np.asarray(n, np.float64)
            if a.ndim == 2 and len(a) and a.shape[1] >= 2:
                lines.append(np.ascontiguousarray(a[:, :2]))
                widths.append(float(c.get("w") or 3.0))
    if not lines:
        return np.zeros((0, 3), np.float64)
    return np.asarray(_ef().snowroad_crossing_discs(lines, widths), np.float64).reshape(-1, 3)


def bank_mesh(snow, lines, cuts, bbox, ground, origin, decks=None):
    """The snowbanks of a skin: (P relative to origin, tris, uvs, materials, tri material) or None. snow: the wear's
    winter part; ground(xy (n, 2)) -> z of the drawn ground; decks: the bridge parts' line set (road_lines bridges=True:
    no bank at an abutment or under a deck)."""
    draw = _ef().SNOWROAD_BANK_DRAW_MM
    bins = list(snow.get("banks") or ())[:CLASSES] + [0] * CLASSES
    heights = [draw[min(max(int(b), 0), 4)] / 1000.0 for b in bins[:CLASSES]]
    if not any(heights) or len(lines[2]) == 0:
        return None
    over = None if decks is None or len(decks[2]) == 0 else (decks[0], decks[1], decks[2])
    got = _ef().snowroad_banks(lines[0], lines[1], lines[2], lines[3], heights,
                               np.ascontiguousarray(cuts, np.float64).reshape(-1, 3), tuple(float(v) for v in bbox),
                               over)
    if got is None:
        return None
    xy, h, tris, uv, tcls = got
    g = np.asarray(ground(np.asarray(xy, np.float64)), np.float64)
    bad = ~np.isfinite(g)
    if bad.all():
        return None
    if bad.any():                   # (a point the drawn ground cannot answer: its triangles left out)
        tris = np.asarray(tris)
        keep = ~bad[tris].any(axis=1)
        tris, tcls = tris[keep], np.asarray(tcls)[keep]
        g = np.where(bad, 0.0, g)
        if not len(tris):
            return None
    z = g + np.asarray(h, np.float64)
    P = np.c_[np.asarray(xy) - np.asarray(origin[:2], np.float64), z - float(origin[2])]
    mats = [bank_name(c) for c in range(CLASSES)]
    return P, np.asarray(tris, np.int64), np.asarray(uv, np.float64), mats, np.asarray(tcls, np.int64)


def remap_walks(mesh, lines, origin):
    """A sidewalk mesh (P rel. origin, tris, uvs, materials, tri material) with its slabs (WALK_MATERIALS) on the
    winter copies by street kind: main streets (P0-P1) S0, residential (P2-P3) cleared or left by a draw per block
    face (the street's feature and side), the rest S2."""
    P, T, UV, mats, M = mesh
    mats = list(mats)
    M = np.asarray(M, np.int64).copy()
    slab = [i for i, m in enumerate(mats) if m in WALK_MATERIALS]
    if not slab or len(T) == 0:
        return mesh
    sel = np.isin(M, slab)
    if not sel.any():
        return mesh
    P = np.asarray(P, np.float64)
    T = np.asarray(T, np.int64)
    cen = P[T[sel]].mean(axis=1)[:, :2] + np.asarray(origin[:2], np.float64)
    li, cls, side = class_at(cen, lines, WALK_EXTRA_M)
    base = len(mats)
    mats += [walk_name(s) for s in WALK_SLOTS]
    fids = lines[6]
    li = np.asarray(li)
    slot = np.frombuffer(bytes(_ef().snowroad_walk_slots(
        bytes(np.asarray(cls, np.uint8)), [fids[i] if i >= 0 else "" for i in li.tolist()],
        np.asarray(side, np.int8).tolist())), np.uint8).astype(np.int64)
    M[np.where(sel)[0]] = base + slot
    return P, T, UV, mats, M
