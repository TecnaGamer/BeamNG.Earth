"""Bridge structure types and the steel trusses drawn over (or under) a bridge's decks.

The map data says where a bridge is, not what it is: the Queensboro Bridge, a steel cantilever truss, was a plain
concrete deck (the user, 2026-10-17: "Bridges need a lot of work in general"; Mapillary: the truss all along it).
A bridge outline in the map tagged bridge:structure (OSM, carried in Overture's infrastructure "tags") decides
first, anywhere. Where the US National Bridge Inventory covers (bridges_us.npz, tools/build_bridge_db.py: FHWA, public domain) its
main span type decides (item 43B: through or deck truss, arch, suspension, cable-stayed, movable); a bridge there
with no such record is a plain deck. Elsewhere a long span over water is taken for a through truss (the commonest
long steel span of the 20th century; the user's choice, 2026-10-18: "OSM tag + size rule").

truss_members() builds the truss of one chain of deck parts as beams (p, q, width, height) in the anchor frame:
two vertical Warren trusses (chords, verticals, alternating diagonals) at the outer edges of the decks running side
by side, cross beams over the top for a through truss; roads.BridgeProfiles hands each tile those whose middle lies
in it, roadmesh.deck draws them. Pure given the inputs."""
import math
import os

import numpy as np

# NBI item 43B, the main span's design
NBI_DECK_TRUSS, NBI_THROUGH_TRUSS = 9, 10
NBI_DECK_ARCH, NBI_THROUGH_ARCH, NBI_SUSPENSION, NBI_STAYED = 11, 12, 13, 14
NBI_LIFT, NBI_BASCULE, NBI_SWING = 15, 16, 17
ARCH_ANY = 111          # (not NBI) an arch over or under its deck as its height allows: OSM's bridge:structure=arch
PLAIN = 2               # NBI's stringer / girder: a plain deck (Wikidata's beam and girder bridges)
TRUSS_TYPES = (NBI_DECK_TRUSS, NBI_THROUGH_TRUSS)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridges_us.npz")
COVER_M = 25000.0       # an NBI record (any notable type) this close: the inventory covers here (no size rule)
MATCH_M = 200.0         # an NBI record's point this close to a chain's deck lines may be that chain's (the points are
                        # rough: Golden Gate's lies 140 m off its deck) ...
MATCH_LEN_M = 150.0     # ... the best by distance + this many m per factor e of length mismatch
WET_SPAN_M = 300.0      # (size rule, outside the inventory) a run over water this long: a through truss (150 m made
                        # trusses of Tokyo's expressways over the bay, girders in life, 2026-10-18)
PANEL_M = 14.0          # m between a truss's verticals
ANCHOR_ARM = 0.75       # a big truss reaches this share of its main span past the water (the Queensboro Bridge's
                        # anchor arms: 143 and 140 m for its 182 and 299 m channel spans)
ROAD_GAP_M = 3.0        # m an anchor pier keeps off a road's centre line under it
ANCHOR_T = 6.0          # m: the thickness along the bridge of the solid pier under each end of a big truss
MIN_RUN_M = 24.0        # a truss shorter than this is left out
HIGH_M = 9.0            # m of deck over the ground: the long spans a big truss stands over (its approaches are plain)
LOW_SPAN_M = 60.0       # an NBI span shorter than this: a small truss, over all of its bridge (HIGH_LOW_M)
HIGH_LOW_M = 2.0
SIDE_GAP_M = 0.8        # m from the outermost deck's edge (parapet) to a truss's plane
PARALLEL_COS = 0.94     # decks within ~20 degrees of each other run side by side
SIDE_REACH_M = 30.0     # m sideways a deck is taken as beside another
HEADROOM_M = 6.5        # m from the highest deck inside a through truss to its top chord
PONY_SPAN_M = 35.0      # a through truss with a shorter span is a pony truss: low, no cross beams
CHORD = (0.9, 1.1)      # (width, height) m of the chords
POST = (0.6, 0.7)       # verticals
DIAG = (0.45, 0.6)      # diagonals
BRACE = (0.4, 0.5)      # cross beams over the top

OSM_TYPES = {"truss": NBI_THROUGH_TRUSS, "cantilever": NBI_THROUGH_TRUSS, "arch": ARCH_ANY,
             "suspension": NBI_SUSPENSION, "cable-stayed": NBI_STAYED}    # bridge:structure; others: a plain deck
# Painted steel (roadmesh.STEEL_PAINTS: one material each): the tagged colour (OSM colour=, a name or #rrggbb) as
# the nearest of these; grey without a tag (the Golden Gate Bridge is tagged orange / #f04a00)
PAINTS = {"grey": (150, 152, 150), "orange": (240, 74, 0), "red": (150, 25, 20), "green": (60, 100, 70),
          "blue": (50, 80, 130), "black": (25, 25, 25), "white": (225, 225, 220), "yellow": (220, 170, 30),
          "brown": (95, 60, 40)}
COLOUR_NAMES = {"gray": "grey", "silver": "grey", "darkgreen": "green", "navy": "blue", "gold": "yellow",
                "maroon": "red", "darkred": "red", "orangered": "orange", "rust": "brown", "beige": "white",
                "cream": "white"}
OSM_INSIDE = 0.2        # (osm_colour) this share of the chain's points in the outline
OSM_MIN_M = 80.0        # a tagged outline holding this much of the chain's deck is a structure's site
NETWORK_M = 3000.0      # the size rule: a chain no longer than this ...
NETWORK_SHARE = 4.0     # ... or this many times its run over water (not a city's elevated network)        # this share of a chain's deck points inside a tagged outline: the chain is that bridge

_db = None
_wd = None
WD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridges_wd.npz")
WD_MATCH_M = 150.0      # a Wikidata bridge's point this close to the decks is theirs (its points sit on the bridge)


def _load_wd():
    global _wd
    if _wd is None:
        try:
            d = np.load(WD_PATH)
            _wd = {k: d[k] for k in d.files}
        except OSError:
            _wd = {"lat": np.zeros(0, np.float32)}
    return _wd


def wd_near(lat, lon, r_m):
    """True when a Wikidata typed bridge (bridges_wd.npz) lies within r_m of (lat, lon) (the credits)."""
    wd = _load_wd()
    la = wd["lat"]
    if not len(la):
        return False
    kx = 111320.0 * np.cos(np.radians(lat))
    d = np.hypot((wd["lon"] - lon) * kx, (la - lat) * 110540.0)
    return bool((d <= r_m).any())


def wd_matches(frame, lines):
    """[(distance m, index, point (2,) in the frame)] of Wikidata's typed bridges (bridges_wd.npz,
    tools/build_bridge_wikidata.py) within WD_MATCH_M of the deck lines, nearest first."""
    lines = [np.asarray(ln, np.float64)[:, :2] for ln in lines if len(ln) >= 2]
    wd = _load_wd()
    if not lines or not len(wd["lat"]):
        return []
    pts = np.concatenate(lines)
    c = pts.mean(0)
    la, lo, _ = frame.enu_to_geodetic(np.array([c[0]]), np.array([c[1]]), 0.0)
    reach = float(np.hypot(*(pts.max(0) - pts.min(0)))) / 2 + WD_MATCH_M
    dlat = reach / 110540.0
    i0, i1 = np.searchsorted(wd["lat"], float(la[0]) - dlat), np.searchsorted(wd["lat"], float(la[0]) + dlat)
    idx = np.arange(i0, i1)
    if not len(idx):
        return []
    e, n, _ = frame.geodetic_to_enu(wd["lat"][idx].astype(np.float64), wd["lon"][idx].astype(np.float64), 0.0)
    out = []
    for i, q in zip(idx, np.c_[e, n]):
        if abs(q[0] - c[0]) > reach or abs(q[1] - c[1]) > reach:
            continue
        d = min(_seg_dist(q, ln) for ln in lines)
        if d <= WD_MATCH_M:
            out.append((d, int(i), q))
    return sorted(out, key=lambda r: r[0])


def wd_record(i):
    wd = _load_wd()
    out = {"type": int(wd["type"][i]), "span": float(wd["span"][i]), "length": float(wd["length"][i]), "width": 0.0}
    if "paint" in wd and int(wd["paint"][i]) < len(wd.get("paints", ())):
        out["wd_paint"] = str(wd["paints"][int(wd["paint"][i])])   # (its colour, P462: the Rainbow Bridge's white)
    return out


def paint(colour):
    """The PAINTS name nearest to an OSM colour value (a name or #rrggbb), None if it is neither."""
    c = str(colour or "").strip().lower().replace(" ", "")
    c = COLOUR_NAMES.get(c, c)
    if c in PAINTS:
        return c
    if c.startswith("#") and len(c) in (4, 7):
        try:
            rgb = [int(c[1 + i] * 2, 16) for i in range(3)] if len(c) == 4 else \
                [int(c[1 + 2 * i:3 + 2 * i], 16) for i in range(3)]
        except ValueError:
            return None
        return min(PAINTS, key=lambda k: sum((a - b) ** 2 for a, b in zip(PAINTS[k], rgb)))
    return None


def osm_colour(frame, parts, feats_list):
    """The paint (PAINTS name) of the bridge outline tagged colour= covering the chain (OSM_INSIDE), else None."""
    got = _osm_tagged(frame, parts, feats_list, "colour")
    return paint(got) if got is not None else None


def _osm_tagged(frame, parts, feats_list, key, with_outline=False):
    """The value of tag key on the bridge outline (infrastructure, kind 3) covering most of the chain, else None;
    with_outline: (value, outline (k, 2) in the frame)."""
    import json
    from polygon import Path
    pts = np.concatenate([np.asarray(xy, np.float64) for xy, _, _ in parts])
    best = None
    for f in feats_list:
        if f is None or not len(f):
            continue
        tags, sub = f.attrs.get("tags"), f.attrs.get("subtype")
        if tags is None:
            continue
        for i in range(len(f.kind)):
            t = str(tags[i])
            if f'"{key}"' not in t or (sub is not None and str(sub[i]) != "bridge") or f.kind[i] != 3:
                continue
            try:
                val = json.loads(t).get(key)
            except ValueError:
                continue
            for part in f.parts(i):
                ring = np.asarray(part[0], np.float64)
                if len(ring) < 4:
                    continue
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                share = float(Path(np.c_[e, n]).contains_points(pts).mean())
                if share >= OSM_INSIDE and (best is None or share > best[0]):
                    best = (share, val, np.c_[e, n])
    if best is None:
        return None
    return (best[1], best[2]) if with_outline else best[1]


def osm_structure(frame, parts, feats_list):
    """The NBI-style type code of a chain from the bridge outlines tagged bridge:structure among the infrastructure
    Features (feats_list), 0 for one tagged a plain kind (beam, ...), None if none covers the chain."""
    return _osm_structure(frame, parts, feats_list)[0]


def _osm_structure(frame, parts, feats_list):
    """(osm_structure's code, the tagged outline (k, 2) in the frame) or (None, None)."""
    got = _osm_tagged(frame, parts, feats_list, "bridge:structure", with_outline=True)
    if got is None:
        return None, None
    return OSM_TYPES.get(str(got[0]), 0), got[1]


def _load():
    global _db
    if _db is None:
        try:
            d = np.load(DB_PATH)
            _db = {k: d[k] for k in d.files}
        except OSError:
            _db = {"lat": np.zeros(0, np.float32)}
    return _db


def nbi_near(lat, lon, r_m):
    """Indices into the inventory of the records within r_m of (lat, lon) and their distances (m)."""
    db = _load()
    la = db["lat"]
    if not len(la):
        return np.zeros(0, np.int64), np.zeros(0)
    dlat = r_m / 110540.0
    i0, i1 = np.searchsorted(la, lat - dlat), np.searchsorted(la, lat + dlat)
    idx = np.arange(i0, i1)
    if not len(idx):
        return idx, np.zeros(0)
    kx = 111320.0 * np.cos(np.radians(lat))
    d = np.hypot((db["lon"][idx] - lon) * kx, (la[idx] - lat) * 110540.0)
    keep = d <= r_m
    return idx[keep], d[keep]


def matches(frame, lines):
    """[(score, index)] of the inventory's records within MATCH_M of the deck lines (k, 2+), best first: the distance
    plus MATCH_LEN_M per factor e between the record's length and the longest line's."""
    lines = [np.asarray(ln, np.float64)[:, :2] for ln in lines if len(ln) >= 2]
    if not lines:
        return []
    pts = np.concatenate(lines)
    c = pts.mean(0)
    la, lo, _ = frame.enu_to_geodetic(np.array([c[0]]), np.array([c[1]]), 0.0)
    reach = float(np.hypot(*(pts.max(0) - pts.min(0)))) / 2 + MATCH_M
    idx, _ = nbi_near(float(la[0]), float(lo[0]), reach)
    if not len(idx):
        return []
    db = _load()
    e, n, _ = frame.geodetic_to_enu(db["lat"][idx].astype(np.float64), db["lon"][idx].astype(np.float64), 0.0)
    longest = max(float(np.hypot(*np.diff(ln, axis=0).T).sum()) for ln in lines)
    out = []
    for i, q in zip(idx, np.c_[e, n]):
        d = min(_seg_dist(q, ln) for ln in lines)
        if d <= MATCH_M:
            rl = float(db["length"][i])
            mis = abs(math.log(max(rl, 5.0) / max(longest, 5.0))) if rl > 0 else 1.0
            out.append((d + MATCH_LEN_M * mis, int(i)))
    return sorted(out)


def nav_clearance(frame, lines):
    """m: the navigation clearance (NBI item 39) of the best record with one on these deck lines, 0 if none."""
    db = _load()
    if "nav" not in db:
        return 0.0
    for _, i in matches(frame, lines):
        if db["nav"][i] > 0 and nav_plausible(float(db["nav"][i]), float(db["span"][i]), int(db["type"][i])):
            return float(db["nav"][i])
    return 0.0


NAV_SPAN_MIN = 0.06         # a navigation clearance under this share of the record's main span (15 m at most) is not ...
NAV_SPAN_MAX_MOVABLE = 0.30  # ... and a movable bridge's over this share is its span raised
MOVABLE = (15, 16, 17)       # NBI 43B: lift, bascule, swing


def nav_plausible(nav, span, kind):
    """Whether NBI item 39 (m) fits the closed bridge: 3-80 m; for a main span of 40 m or more, at least
    min(NAV_SPAN_MIN x span, 15 m); a movable one's at most NAV_SPAN_MAX_MOVABLE x span. Portland, 2026-10-19: the
    Interstate Bridge's 4.3 m under a 162 m lift span (its deck is 22 m up), the Hawthorne Bridge's 33.5 m (the
    lift span raised) over 74 m; the Golden Gate Bridge's 67 m over its 1280 m span stays. Pure."""
    if not 3.0 <= nav <= 80.0:
        return False
    if span < 40.0:
        return True
    if kind in MOVABLE and nav > NAV_SPAN_MAX_MOVABLE * span:
        return False
    return nav >= min(NAV_SPAN_MIN * span, 15.0)


def record(i):
    db = _load()
    return {"type": int(db["type"][i]), "span": float(db["span"][i]), "length": float(db["length"][i]),
            "width": float(db["width"][i])}


def _seg_dist(q, xy):
    """Distance (m) from point q (2,) to the polyline xy (k, 2)."""
    a, b = xy[:-1], xy[1:]
    ab = b - a
    t = np.clip(((q - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-9), 0, 1)
    return float(np.min(np.hypot(*(a + t[:, None] * ab - q).T)))


def chain_structure(frame, parts, wet_fn=None, infra=()):
    """The first of chain_structures, or None (a plain deck)."""
    got = chain_structures(frame, parts, wet_fn, infra)
    return got[0] if got else None


def chain_structures(frame, parts, wet_fn=None, infra=()):
    """The structures on a chain of deck parts [(xy (k, 2), z (k,), width)]: [{"type": NBI code, "span": m, "length":
    m, "width": m, "src": "osm" | "osm+nbi" | "nbi" | "size", "paint": a PAINTS name, "outline": (k, 2) or absent}].
    A chain can be a city's whole elevated network (Tokyo's: 163 parts, 76 km), so each structure keeps to its site:
    a bridge outline tagged bridge:structure covering OSM_MIN_M of deck (OSM, in Overture's infrastructure: its
    colour= its paint, the inventory's record inside it its span), else each notable inventory record on the decks
    (a circle round its point), else, outside the inventory, a run of WET_SPAN_M over water on a chain no longer
    than NETWORK_M or NETWORK_SHARE times that run: a through truss. wet_fn(xy) -> bool per point."""
    from polygon import Path
    lines = [np.asarray(xy, np.float64) for xy, _, _ in parts if len(xy) >= 2]
    if not lines:
        return []
    out, taken, used, osm_sites, wd_used = [], [], set(), [], set()
    wdm = wd_matches(frame, lines)
    # Wikidata's type where it has the bridge: the map's tags can be wrong (OSM calls Tokyo's Rainbow Bridge, a
    # suspension bridge, cable-stayed: drawn with stays, 2026-10-18); a bridge's outlines that hold the same Wikidata
    # bridge are one site (the Rainbow Bridge has a second, unnamed one)
    groups = {}
    for tags, outline in (_osm_outlines(frame, parts, infra) if infra else []):
        taken.append(outline)
        rings = outline if isinstance(outline, list) else [outline]
        inner = [ln[_in_rings(rings, ln)] for ln in lines]
        wdi = [w for w in wdm if _in_rings(rings, w[2][None])[0] or any(
            len(ln) >= 2 and _seg_dist(w[2], ln) <= WD_MATCH_M for ln in inner)]
        key = ("wd", wdi[0][1]) if wdi else ("osm", len(groups))
        g = groups.setdefault(key, {"tags": tags, "rings": [], "wd": wdi[0][1] if wdi else None})
        g["rings"] += rings
        if tags.get("colour") and not g["tags"].get("colour"):
            g["tags"] = {**g["tags"], "colour": tags["colour"]}
    for g in groups.values():
        tags, rings = g["tags"], g["rings"]
        code = OSM_TYPES.get(str(tags.get("bridge:structure")), 0)
        wdr = wd_record(g["wd"]) if g["wd"] is not None else None
        wd_agrees = True
        if wdr is not None:
            wd_used.add(g["wd"])
            # Wikidata corrects the map within a kind of structure (suspension / stayed: the Rainbow Bridge) or where
            # the map gives none; between kinds the map is kept (Wikidata's Port Mann Bridge still had the 1964 tied
            # arch's type and span over the 2012 cable-stayed bridge: an arch drawn over its lanes, GitHub #65)
            wd_agrees = code in (0, PLAIN) or _family(code) == _family(wdr["type"])
            if wd_agrees and not (wdr["type"] == ARCH_ANY and code in (NBI_DECK_ARCH, NBI_THROUGH_ARCH)):
                code = wdr["type"]          # (an arch the map places over or under its deck keeps that)
        tagged = code
        inner = [ln[_in_rings(rings, ln)] for ln in lines]
        found = matches(frame, [ln for ln in inner if len(ln) >= 2])
        if not found:                       # (the record's point can lie outside the outline: Golden Gate's)
            found = matches(frame, [ln for ln in lines if _in_rings(rings, ln).any()])
        if found and found[0][1] in used and int(_load()["type"][found[0][1]]) in MOVABLE_TYPES:
            continue                        # (a movable bridge's other outlines: one structure, the Burnside's three)
        nbi_t = int(_load()["type"][found[0][1]]) if found else 0
        code, shape = resolve_type(code, nbi_t, tags)
        if code in (0, PLAIN):
            continue                        # a plain kind: nothing drawn there
        info = ({**record(found[0][1]), "src": "osm+nbi"} if found else
                {"span": 0.0, "length": 0.0, "width": 0.0, "src": "osm"})
        if shape:
            info["shape"] = shape
        if tagged in TRUSS_TYPES or shape == "arched" or str(tags.get("bridge:structure")) in ("truss", "cantilever"):
            info["truss"] = True            # (a movable span the map or Wikidata calls a truss: steel over it)
        if found:
            used.add(found[0][1])
        elif wdr is not None and wdr["span"] > 0 and wd_agrees:
            info.update(span=wdr["span"], length=wdr["length"], src="osm+wd")
        info.update(type=code, paint=paint(tags.get("colour")) or (wdr or {}).get("wd_paint") or "grey", outline=rings)
        if found and (code in MOVABLE_TYPES or shape) and _held(rings, lines) < 0.5 * float(info["length"]):
            # an outline holding a small part of the inventory's bridge (the Hawthorne Bridge's: its east end) does not
            # bound its trusses: the record's circle does (the river spans: truss_members keeps them to the water)
            info["outline"] = rings + [_circle(frame, found[0][1])]
        out.append(info)
        osm_sites.append((np.concatenate(rings).mean(0), max(float(info["length"]), 300.0)))
    chain_paint = (osm_colour(frame, parts, infra) if infra else None) or "grey"
    db = _load()
    seen, nbi_sites = set(used), []
    for _, i in matches(frame, lines):
        t = int(db["type"][i])
        if not NBI_DECK_TRUSS <= t <= NBI_SWING or i in seen:
            continue
        seen.add(i)
        e, n, _ = frame.geodetic_to_enu(np.array([float(db["lat"][i])]), np.array([float(db["lon"][i])]), 0.0)
        c = np.array([float(e[0]), float(n[0])])
        if any(_in_rings(o, c[None])[0] for o in taken) or \
                any(float(np.hypot(*(c - oc))) < oL / 2 for oc, oL in osm_sites):
            continue                        # (a tagged bridge's own record)
        L = float(db["length"][i])
        if any(t == t2 and float(np.hypot(*(c - c2))) < max(L, L2) / 2 for t2, c2, L2 in nbi_sites):
            continue                        # a bridge's second record (the Queensboro Bridge's upper and lower
                                            # roadways: two trusses drawn)
        nbi_sites.append((t, c, L))
        r = max(L / 2, 100.0) + MATCH_M
        ang = np.linspace(0, 2 * np.pi, 33)
        out.append({**record(i), "src": "nbi", "paint": chain_paint,
                    "outline": np.c_[c[0] + r * np.cos(ang), c[1] + r * np.sin(ang)]})
    for d, i, c in wdm:                     # Wikidata's typed bridges the map and the inventory did not give
        if i in wd_used or any(_in_rings(o, c[None])[0] for o in taken) or \
                any(float(np.hypot(*(c - oc))) < oL / 2 for oc, oL in osm_sites) or \
                any(float(np.hypot(*(c - c2))) < max(L2 / 2, 150.0) for _, c2, L2 in nbi_sites):
            continue
        wd_used.add(i)
        r = wd_record(i)
        L = r["length"]
        ang = np.linspace(0, 2 * np.pi, 33)
        rad = max(L / 2, 150.0) + 60.0
        circle = np.c_[c[0] + rad * np.cos(ang), c[1] + rad * np.sin(ang)]
        taken.append(circle)
        if r["type"] == PLAIN or not (NBI_DECK_TRUSS <= r["type"] <= NBI_SWING or r["type"] == ARCH_ANY):
            continue                        # a girder bridge: a plain deck (and no size rule there)
        out.append({**r, "src": "wd", "paint": r.get("wd_paint") or chain_paint, "outline": circle})
    if out or taken:
        return _drop_strays(out)
    pts = np.concatenate(lines)
    cc = pts.mean(0)
    la, lo, _ = frame.enu_to_geodetic(np.array([cc[0]]), np.array([cc[1]]), 0.0)
    if len(nbi_near(float(la[0]), float(lo[0]), COVER_M)[0]):
        return []                           # inside the inventory: no notable record, a plain deck
    longest = _longest_wet(parts, wet_fn)
    total = sum(float(np.hypot(*np.diff(ln, axis=0).T).sum()) for ln in lines)
    if longest >= WET_SPAN_M and total <= max(NETWORK_M, NETWORK_SHARE * longest):
        return [{"type": NBI_THROUGH_TRUSS, "span": float(min(longest, 250.0)), "length": 0.0, "width": 0.0,
                 "src": "size", "paint": chain_paint}]
    return []


MOVABLE_TYPES = (NBI_LIFT, NBI_BASCULE, NBI_SWING)
LENS_TAGS = ("lenticular", "lens", "pauli")       # OSM bridge:truss / truss:type values of a lens truss


def resolve_type(code, nbi_t, tags=None):
    """(type code, shape None | "lens" | "arched") of a bridge from the map's / Wikidata's code and its inventory record's
    type nbi_t (0: none). The inventory knows what moves: a bascule or lift span the map tags `arch` (Portland's
    Broadway Bridge, a bascule between curved-chord Petit trusses) or `beam` was drawn as an arch hanging under its
    road (the user's screenshots, 2026-10-26), so a movable record decides the type; the arch tag then says the
    truss's top chord is curved. A truss record the map or Wikidata calls an arch is a lens truss (Pittsburgh's
    Smithfield Street Bridge: Wikidata's arch, the inventory's through truss), as is one tagged bridge:truss=lenticular.
    Pure."""
    tags = tags or {}
    lens_tag = any(str(tags.get(k, "")).lower() in LENS_TAGS for k in ("bridge:truss", "truss:type", "truss"))
    archy = code in (ARCH_ANY, NBI_DECK_ARCH, NBI_THROUGH_ARCH)
    if nbi_t in MOVABLE_TYPES and code not in (NBI_SUSPENSION, NBI_STAYED):
        return nbi_t, ("arched" if archy else None)
    if code in MOVABLE_TYPES:
        return code, None
    if nbi_t in TRUSS_TYPES and (code == ARCH_ANY or lens_tag):
        return NBI_THROUGH_TRUSS, "lens"
    if code in TRUSS_TYPES and lens_tag:
        return NBI_THROUGH_TRUSS, "lens"
    return code, None


def _held(rings, lines):
    """m of the deck line holding most inside the rings (decks side by side count once)."""
    held = 0.0
    for ln in lines:
        m = _in_rings(rings, ln)
        held = max(held, float(np.hypot(*np.diff(ln, axis=0).T)[m[1:] & m[:-1]].sum()))
    return held


def _circle(frame, i):
    """The circle (33, 2) in the frame round inventory record i that chain_structures gives a record of its own."""
    db = _load()
    e, n, _ = frame.geodetic_to_enu(np.array([float(db["lat"][i])]), np.array([float(db["lon"][i])]), 0.0)
    r = max(float(db["length"][i]) / 2, 100.0) + MATCH_M
    ang = np.linspace(0, 2 * np.pi, 33)
    return np.c_[float(e[0]) + r * np.cos(ang), float(n[0]) + r * np.sin(ang)]


def _drop_strays(out):
    """The structures of a chain without a second site of a kind that has no size from any source where one of the same
    kind has one: a second outline of the Fremont Bridge (tagged arch, no record of its own) put a 40 m arch on an
    approach ramp beside the real 383 m one (2026-10-27)."""
    sized = {_family(s["type"]) for s in out if s.get("span")}
    out = [s for s in out if s.get("span") or _family(s["type"]) not in sized]
    # one main span per site: an arch or a cable span from a record whose circle holds a bigger one's centre is the
    # same crossing (the Fort Pitt Bridge's 229 m through arch got a 56 m deck arch drawn under it from a record of
    # a ramp beside it, 2026-10-27; both go on the chain's one main span)
    keep = []
    for s in sorted(out, key=lambda s: -float(s.get("span") or 0)):
        if s["type"] in ARCH_TYPES + (NBI_SUSPENSION, NBI_STAYED) and s.get("src") in ("nbi", "wd"):
            c = np.asarray(s["outline"]).mean(0) if isinstance(s.get("outline"), np.ndarray) else None
            if c is not None and any(k["type"] in ARCH_TYPES + (NBI_SUSPENSION, NBI_STAYED) and
                                     isinstance(k.get("outline"), np.ndarray) and
                                     _in_rings(k["outline"], c[None])[0] for k in keep):
                continue
        keep.append(s)
    return [s for s in out if any(s is k for k in keep)]


FAMILIES = {NBI_SUSPENSION: "cable", NBI_STAYED: "cable", NBI_DECK_ARCH: "arch", NBI_THROUGH_ARCH: "arch",
            ARCH_ANY: "arch", NBI_DECK_TRUSS: "truss", NBI_THROUGH_TRUSS: "truss", NBI_BASCULE: "movable",
            NBI_LIFT: "movable", NBI_SWING: "movable"}


def _family(code):
    """The kind of structure of a type code (chain_structures: where Wikidata may correct the map)."""
    return FAMILIES.get(code, "plain")


def _osm_outlines(frame, parts, feats_list):
    """[(tags dict, outline (k, 2) in the frame)] of the bridge outlines tagged bridge:structure (infrastructure, kind
    3) holding at least OSM_MIN_M of the chain's deck."""
    import json
    from polygon import Path
    lines = [np.asarray(xy, np.float64) for xy, _, _ in parts if len(xy) >= 2]
    out, seen, groups = [], set(), {}
    for f in feats_list:
        if f is None or not len(f):
            continue
        tags, sub = f.attrs.get("tags"), f.attrs.get("subtype")
        if tags is None:
            continue
        for i in range(len(f.kind)):
            t = str(tags[i])
            if "bridge:structure" not in t or (sub is not None and str(sub[i]) != "bridge") or f.kind[i] != 3:
                continue
            fid = str(f.ids[i]) if hasattr(f, "ids") else (id(f), i)
            if fid in seen:
                continue
            seen.add(fid)
            try:
                td = json.loads(t)
            except ValueError:
                continue
            name = str(f.attrs["name"][i]) if "name" in f.attrs else ""
            key = (name, str(td.get("bridge:structure"))) if name else ("#" + str(fid), "")
            for part in f.parts(i):
                ring = np.asarray(part[0], np.float64)
                if len(ring) < 4:
                    continue
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                groups.setdefault(key, (td, []))[1].append(np.c_[e, n])
    for td, rings in groups.values():   # one site per bridge: its outline's parts, its namesakes' (Sydney Harbour
        held = 0.0                      # Bridge's three outlines were three arches)
        for ln in lines:
            m = _in_rings(rings, ln)
            seg = np.hypot(*np.diff(ln, axis=0).T)
            held += float(seg[m[1:] & m[:-1]].sum())
        if held >= OSM_MIN_M:
            out.append((td, rings))
    return out


def _in_rings(rings, pts):
    """Which points lie in any of the rings (an outline: one ring (k, 2) or a list of them)."""
    from polygon import Path
    if isinstance(rings, np.ndarray):
        rings = [rings]
    m = np.zeros(len(pts), bool)
    for r in rings:
        m |= np.asarray(Path(r).contains_points(pts), bool)
    return m


def _longest_wet(parts, wet_fn):
    """The longest run (m) of any part over water."""
    if wet_fn is None:
        return 0.0
    longest = 0.0
    for xy, z, w in parts:
        xy = np.asarray(xy, np.float64)
        if len(xy) < 2:
            continue
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        wet = np.asarray(wet_fn(xy), bool)
        i = 0
        while i < len(wet):
            if not wet[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(wet) and wet[j + 1]:
                j += 1
            longest = max(longest, s[j] - s[i])
            i = j + 1
    return longest


def _runs(mask, s, min_len):
    """[(i, j)] index runs of mask at least min_len long (arc s)."""
    out, i = [], 0
    while i < len(mask):
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(mask) and mask[j + 1]:
            j += 1
        if s[j] - s[i] >= min_len:
            out.append((i, j))
        i = j + 1
    return out


def _clear_of_roads(foot, road_lines, gap):
    """Whether no road line (k, 2) comes within gap m of any of the points foot (n, 2)."""
    for ln in road_lines:
        ln = np.asarray(ln, np.float64)[:, :2]
        if len(ln) >= 2 and min(_seg_dist(q, ln) for q in foot) < gap:
            return False
    return True


SHAPED_ARM = 0.12       # (a lens / curved-chord / movable truss) this share of a span past the water
LENS_RISE = 1 / 14.0    # a lens truss's top chord over its portals at mid-span: a share of its span (Smithfield
                        # Street: 110 m spans, ~8 m)
LENS_BELLY_M = 1.2      # ... its lower chord falls to this over the road at mid-span (BeamMaps' Smithfield)
ARCHED_RISE = 1 / 12.0  # a curved-chord (Petit / Parker) truss's top chord rise over its ends: a share of its span
PORTAL_M = 7.0          # m: a shaped through truss's ends (portals) over the road


LOW_WATER_M = 2.0       # low_water: ground within this of a chain's lowest point is its river ...
LOW_DIP_M = 5.0         # ... when that lies this far under both of its ends


def low_water(P, ground_fn, inside=None):
    """wet(xy) -> bool for a river the sea depth does not know (one above sea level: Pittsburgh's rivers, ~218 m; the
    Willamette under Portland's bridges), or None: the ground within LOW_WATER_M of the lowest point under the chain's
    part dipping deepest (inside the bridge's outline, else all of it), if that lies LOW_DIP_M under its higher end (the
    Smithfield Street Bridge's outline stops over the low south bank; the Hawthorne Bridge's holds the river only).
    Pure given ground_fn."""
    for use in ((inside, None) if inside is not None else (None,)):
        lines = [np.asarray(p[0], np.float64)[m] if use is not None else np.asarray(p[0], np.float64)
                 for p, m in zip(P, use if use is not None else [None] * len(P))]
        lines = [ln for ln in lines if len(ln) >= 2]
        if not lines:
            continue
        best = None
        for ln in lines:                                # the part dipping deepest between its ends: the crossing
            g = np.asarray(ground_fn(ln), np.float64)
            dip = max(g[0], g[-1]) - float(g.min())
            if best is None or dip > best[0]:
                best = (dip, float(g.min()))
        if best[0] >= LOW_DIP_M:
            lo = best[1]
            return lambda xy: np.asarray(ground_fn(np.asarray(xy, np.float64)), np.float64) <= lo + LOW_WATER_M
    return None


def _span_axis(groups):
    """(origin (2,), unit axis (2,), x0, x1): the line all of a structure's truss runs lie along (their stations'
    principal direction) and their extent on it, so their spans fall in one place: the runs of the decks side by side
    each took their own (three lenses over the Smithfield Street Bridge's two spans, 2026-10-27)."""
    c = np.array([(L + R) / 2 for run in groups for L, R, *_ in run])
    o = c.mean(0)
    _, _, vt = np.linalg.svd(c - o, full_matrices=False)
    ax = vt[0]
    x = (c - o) @ ax
    return o, ax, float(x.min()), float(x.max())


def _spans_of(run, span, axis):
    """Per station of a truss run: (span index, t 0..1 along that span, kept). The structure (axis: _span_axis) holds
    the whole number of the inventory's main spans that fits over its water (its item 48 is the longest span), centred
    on it; the stations past them are approaches (no truss). The Smithfield Street Bridge's 305 m of river holds 2 of
    its 110 m spans (in life, 2 lenses and approach spans): BeamMaps' hand-made one had 3, a fixed 78 m span."""
    o, ax, lo, hi = axis
    c = np.array([(L + R) / 2 for L, R, *_ in run])
    a = (c - o) @ ax - lo
    length = hi - lo
    span = max(float(span), 1.0)
    # (the run reaches SHAPED_ARM of a span past the water each side: the spans that fit over the water itself)
    n = max(1, int(math.floor(max(length - 2 * SHAPED_ARM * span, 0.0) / span + 0.05)))
    sl = min(span, length / n)
    x0 = (length - n * sl) / 2
    u = (a - x0) / sl
    keep = (u >= -0.02) & (u <= n + 0.02)
    u = np.clip(u, 0.0, n)
    k = np.minimum(np.floor(u), n - 1)
    return k.astype(int), u - k, keep


def _shaped(run, span, shape, pony, axis):
    """A through truss's stations [(L, R, bot, top, t)] given a curved top chord rising span by span (shape "arched")
    or a lens (shape "lens": the lower chord also curved, falling from the portals to LENS_BELLY_M over the road, the
    road hung from it), plus the extra members (the lens's hangers and its portal posts). Pure."""
    from roadmesh import DECK_DEPTH
    k, t, keep = _spans_of(run, span, axis)
    run = [r for r, m in zip(run, keep) if m]
    k, t = k[keep], t[keep]
    bump = 4.0 * t * (1.0 - t)
    out, extra = [], []
    rise = (LENS_RISE if shape == "lens" else ARCHED_RISE) * span
    for a, (L, R, bot, top, tt) in enumerate(run):
        road = bot + DECK_DEPTH                     # the road's height at this station (the lowest deck)
        end = road + (2.5 if pony else PORTAL_M)
        top2 = end + rise * bump[a]
        if shape == "lens":
            low = end - (end - road - LENS_BELLY_M) * bump[a]
            out.append((L, R, low, top2, tt))
            for e in (L, R):                        # the road hung from the lens
                if low - road > 0.8:
                    extra.append((np.r_[e, road - 0.3], np.r_[e, low], 0.25, 0.25))
            if bump[a] < 0.05:                      # a portal post from the deck through the chords' meeting point
                for e in (L, R):
                    extra.append((np.r_[e, bot], np.r_[e, end + 2.5], 0.9, 0.9))
        else:
            out.append((L, R, bot, max(top2, road + 5.0), tt))
    return out, extra


def truss_members(parts, info, ground_fn, wet_fn=None, with_piers=False, road_lines=()):
    """The truss beams [(p (3,), q (3,), w, h)] of a chain of deck parts [(xy (k, 2), z (k,), width)] whose structure
    (chain_structure) is a truss. ground_fn(xy) -> heights, wet_fn(xy) -> bool. Stations every PANEL_M along each
    part's high runs (longest parts first; a station beside one already placed is skipped: decks side by side share
    one truss); at each, the decks beside it (parallel, within SIDE_REACH_M) give the truss's width and heights. A big
    truss over water stands over the water and a span either side (the Queensboro Bridge's chain reached Queens
    Plaza's ramps: trussed too), one elsewhere over the longest parts' high runs. with_piers: (beams, piers): the solid
    piers under a big truss's ends (roads.pier_sites tuples + their thickness, ANCHOR_T), moved along the bridge off
    the roads under it (road_lines: [(k, 2)], or a function of the pier's foot points (n, 2) giving them) or left
    out."""
    piers = []
    if info is None or info["type"] not in TRUSS_TYPES:
        return ([], []) if with_piers else []
    span = info["span"] or 100.0
    shape = info.get("shape")
    # a lens, a curved-chord truss or a movable bridge's trusses stand over the river spans only (BeamMaps' river
    # bridges: the truss from bank to bank, two stations past the water); a big cantilever's reach its anchor arms
    arm = SHAPED_ARM if (shape or info.get("movable")) else ANCHOR_ARM
    small = span < LOW_SPAN_M
    high_m = HIGH_LOW_M if small else HIGH_M
    deck = info["type"] == NBI_DECK_TRUSS
    pony = not deck and span < PONY_SPAN_M
    depth = float(np.clip(span / 8.0, 3.0, 24.0))
    P = [(np.asarray(xy, np.float64), np.asarray(z, np.float64), float(w)) for xy, z, w in parts if len(xy) >= 2]
    if not P:
        return ([], []) if with_piers else []
    arcs = [np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))] for xy, _, _ in P]
    tans = []
    for xy, _, _ in P:
        t = np.gradient(xy, axis=0)
        tans.append(t / np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None])
    order = sorted(range(len(P)), key=lambda k: -arcs[k][-1])
    ins_all = _inside(P, info)
    wets = [np.asarray(wet_fn(xy), bool) if wet_fn is not None else np.zeros(len(xy), bool) for xy, _, _ in P]
    if shape or info.get("movable"):
        # the river: the sea's depth where it has one, and the low ground under the bridge (a river the sea does not
        # reach, Pittsburgh's, ~218 m; the Willamette under the Hawthorne Bridge, which the sea's mask held only by its
        # east bank: one tower and a 40 m truss, 2026-10-27)
        low = low_water(P, ground_fn, ins_all)
        if low is not None:
            wets = [w | np.asarray(low(xy), bool) for w, (xy, _, _) in zip(wets, P)]
    any_wet = any(w.any() for w in wets)
    longest = max(a[-1] for a in arcs)
    placed = []                                 # (centre (2,), unit along (2,), half width)
    groups = []                                 # per run: the stations [(L (3,) bottom, R bottom, top z)]
    from roadmesh import DECK_DEPTH, PARAPET_W
    for k in order:
        xy, z, w = P[k]
        s = arcs[k]
        g = np.asarray(ground_fn(xy), np.float64)
        mask = z - g >= high_m
        if ins_all is not None:                 # a tagged truss: on its mapped bridge only
            mask &= ins_all[k]
        if not small:
            if any_wet:
                ws = s[wets[k]]
                near = (np.min(np.abs(s[:, None] - ws[None, :]), 1) <= arm * span if len(ws)
                        else np.zeros(len(s), bool))
                mask &= near
            elif s[-1] < 0.5 * longest:
                continue
        for i, j in _runs(mask, s, MIN_RUN_M):
            n_st = max(int(round((s[j] - s[i]) / PANEL_M)), 1)
            run = []
            for sc in np.linspace(s[i], s[j], n_st + 1):
                p = np.array([np.interp(sc, s, xy[:, 0]), np.interp(sc, s, xy[:, 1])])
                ii = min(int(np.searchsorted(s, sc)), len(s) - 1)
                t = tans[k][ii]
                nrm = np.array([-t[1], t[0]])
                if any(abs((p - c) @ u) < PANEL_M / 2 and abs((p - c) @ np.array([-u[1], u[0]])) < hw
                       for c, u, hw in placed):
                    if run:
                        groups.append(run)
                        run = []
                    continue
                lo, hi = -w / 2, w / 2
                zlo = zhi = float(np.interp(sc, s, z))
                for k2, (xy2, z2, w2) in enumerate(P):
                    if k2 == k:
                        continue
                    d = np.hypot(*(xy2 - p).T)
                    m = int(np.argmin(d))
                    if d[m] > SIDE_REACH_M or abs(tans[k2][m] @ t) < PARALLEL_COS:
                        continue
                    off = float((xy2[m] - p) @ nrm)
                    if abs(float((xy2[m] - p) @ t)) > PANEL_M:
                        continue
                    lo, hi = min(lo, off - w2 / 2), max(hi, off + w2 / 2)
                    zlo, zhi = min(zlo, float(z2[m])), max(zhi, float(z2[m]))
                lo -= PARAPET_W + SIDE_GAP_M
                hi += PARAPET_W + SIDE_GAP_M
                L, R = p + nrm * hi, p + nrm * lo
                if deck:
                    top, bot = zhi - DECK_DEPTH, zlo - DECK_DEPTH - depth
                elif pony:
                    top, bot = zhi + float(np.clip(span / 10.0, 1.8, 3.5)), zlo - DECK_DEPTH
                else:
                    top, bot = max(zlo - DECK_DEPTH + depth, zhi + HEADROOM_M), zlo - DECK_DEPTH
                run.append((L, R, bot, top, t))
                placed.append((p, t, (hi - lo) / 2))
            if run:
                groups.append(run)
    beams = []
    axis = _span_axis([r for r in groups if len(r) >= 2]) if shape and not deck and any(len(r) >= 2 for r in groups)         else None
    for gi, run in enumerate(groups):
        if len(run) < 2:
            continue
        if axis is not None:
            run, extra = _shaped(run, span, shape, pony, axis)
            if len(run) < 2:
                continue
            beams += extra
        if not small:                           # a solid pier under each end (the anchor piers)
            for (L, R, bot, top, t), inward in ((run[0], 1.0), (run[-1], -1.0)):
                wd = float(np.hypot(*(L - R)))
                for off in (0.0, 0.5, 1.0, 1.5, 2.0):              # panels toward the truss's middle
                    c = (L + R) / 2 + t * inward * off * PANEL_M
                    nrm = (L - R) / max(wd, 1e-9)
                    foot = np.array([c + nrm * a * wd / 2 + t * b * ANCHOR_T / 2
                                     for a in (-1, 0, 1) for b in (-1, 0, 1)])
                    if not _clear_of_roads(foot, road_lines(foot) if callable(road_lines) else road_lines,
                                           ROAD_GAP_M):
                        continue
                    gz = float(np.asarray(ground_fn(c[None]), np.float64)[0])
                    if bot - gz > 2.0:
                        piers.append((float(c[0]), float(c[1]), float(t[0]), float(t[1]), float(bot), gz - 2.0, wd,
                                      ANCHOR_T))
                    break
        for side in (0, 1):
            for a in range(len(run)):
                e = run[a][side]
                bot, top = run[a][2], run[a][3]
                beams.append((np.r_[e, bot], np.r_[e, top], *POST))
                if a + 1 < len(run):
                    e2, bot2, top2 = run[a + 1][side], run[a + 1][2], run[a + 1][3]
                    beams.append((np.r_[e, top], np.r_[e2, top2], *CHORD))
                    beams.append((np.r_[e, bot], np.r_[e2, bot2], *CHORD))
                    if a % 2 == 0:
                        beams.append((np.r_[e, bot], np.r_[e2, top2], *DIAG))
                    else:
                        beams.append((np.r_[e, top], np.r_[e2, bot2], *DIAG))
        if not pony and not deck:               # cross beams over the decks, X bracing between them
            for a in range(len(run)):
                Lt, Rt = np.r_[run[a][0], run[a][3]], np.r_[run[a][1], run[a][3]]
                beams.append((Lt, Rt, *BRACE))
                if a + 1 < len(run) and a % 2 == 0:
                    Lt2, Rt2 = np.r_[run[a + 1][0], run[a + 1][3]], np.r_[run[a + 1][1], run[a + 1][3]]
                    beams.append((Lt, Rt2, *BRACE))
                    beams.append((Rt, Lt2, *BRACE))
    return (beams, piers) if with_piers else beams


# --- arches, suspension and cable-stayed spans ------------------------------------------------------------------
# One main span per chain, on its longest part: centred on its longest run over water (else where the deck stands
# highest over the ground), as long as the inventory's span (else most of that run). Sydney Harbour Bridge was a
# plain deck between the city's towers where the photos show its steel arch (Mapillary, 2026-10-18).
ARCH_TYPES = (NBI_DECK_ARCH, NBI_THROUGH_ARCH, ARCH_ANY)
ARCH_SPAN_SHARE = 1.05  # (no inventory span) an arch springs from the banks: a little more than the water
SPAN_SHARE = 0.85       # (no inventory span) the main span: this share of the run over water it stands over
SPAN_MAX = 1300.0       # m
SPAN_MIN = 40.0         # m: shorter main spans are left plain
ARCH_RISE = 1 / 5.0     # a deck arch's rise from its springings: a share of its span
THROUGH_TOP = 1 / 6.5   # a through arch's crown over the deck: a share of its span (Sydney: 503 m, ~75 m)
ARCH_TRUSS_M = 200.0    # an arch at least this long has trussed ribs ...
ARCH_DEPTH_CROWN = 1 / 28.0     # ... this deep at the crown (a share of the span: Sydney 18 m) ...
ARCH_DEPTH_END = 1 / 9.0        # ... and at the bearings (Sydney 57 m)
TIED_SPAN_M = 300.0     # a through arch shorter than this is tied: it springs at the deck (Pittsburgh's)
SUSP_TOWER = 1 / 9.0    # a suspension tower's top over the deck: a share of the main span (Golden Gate: 1280, 150)
SUSP_SIDE = 0.35        # the side spans: a share of the main span past each tower
SUSP_LOW_M = 3.0        # m: the main cable's lowest point over the deck
STAYED_TOWER = 1 / 4.0  # a cable-stayed pylon's top over the deck: a share of the main span
HANGER_M = 12.0         # m between hangers / stays / spandrel posts


def _station(P, tans, k, sc, arcs):
    """At arc sc of part k: (point (2,), unit along, unit left, lo, hi (offsets of the outermost decks' edges beside it,
    PARAPET_W and SIDE_GAP_M outside), z low, z high of those decks)."""
    from roadmesh import PARAPET_W
    xy, z, w = P[k]
    s = arcs[k]
    p = np.array([np.interp(sc, s, xy[:, 0]), np.interp(sc, s, xy[:, 1])])
    ii = min(int(np.searchsorted(s, sc)), len(s) - 1)
    t = tans[k][ii]
    nrm = np.array([-t[1], t[0]])
    lo, hi = -w / 2, w / 2
    zlo = zhi = float(np.interp(sc, s, z))
    for k2, (xy2, z2, w2) in enumerate(P):
        if k2 == k:
            continue
        # the nearest point on its segments, not its nearest node: a suspension span's way can run a kilometre
        # between nodes, and its twin carriageway was missed there, a tower leg standing in its lanes (25 de Abril,
        # the Bay Bridge, 2026-10-28)
        a, b = xy2[:-1], xy2[1:]
        ab = b - a
        f = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
        q = a + f[:, None] * ab
        d = np.hypot(*(q - p).T)
        m = int(np.argmin(d))
        if d[m] > SIDE_REACH_M or abs(tans[k2][m] @ t) < PARALLEL_COS or abs(float((q[m] - p) @ t)) > PANEL_M:
            continue
        off = float((q[m] - p) @ nrm)
        zq = float(z2[m] + f[m] * (z2[m + 1] - z2[m]))
        lo, hi = min(lo, off - w2 / 2), max(hi, off + w2 / 2)
        zlo, zhi = min(zlo, zq), max(zhi, zq)
    g = PARAPET_W + SIDE_GAP_M
    return p, t, nrm, lo - g, hi + g, zlo, zhi


def _prep(parts):
    P = [(np.asarray(xy, np.float64), np.asarray(z, np.float64), float(w)) for xy, z, w in parts if len(xy) >= 2]
    arcs = [np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))] for xy, _, _ in P]
    tans = []
    for xy, _, _ in P:
        t = np.gradient(xy, axis=0)
        tans.append(t / np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None])
    return P, arcs, tans


def _inside(P, info):
    """Per part, which points lie in the structure's tagged outline (info["outline"]); None without one."""
    ol = info.get("outline") if info else None
    if ol is None:
        return None
    return [_in_rings(ol, xy) for xy, _, _ in P]


def main_span(P, arcs, info, ground_fn, wet_fn=None):
    """(part index, centre arc, span m) of a chain's main span, or None."""
    if not P:
        return None
    ins = _inside(P, info)
    if ins is not None:         # a tagged bridge: on the part running furthest inside its outline
        k = int(np.argmax([float(np.sum(np.diff(a)[m[1:] & m[:-1]])) for a, m in zip(arcs, ins)]))
        if not ins[k].any():
            return None
    else:
        k = int(np.argmax([a[-1] for a in arcs]))
    xy, z, _ = P[k]
    s = arcs[k]
    inside = ins[k] if ins is not None else np.ones(len(s), bool)
    run = None
    if wet_fn is not None:
        runs = _runs(np.asarray(wet_fn(xy), bool) & inside, s, 1.0)
        if runs:
            i, j = max(runs, key=lambda r: s[r[1]] - s[r[0]])
            run = (s[i], s[j])
    if run is None and ins is not None:
        i, j = max(_runs(inside, s, 0.0), key=lambda r: s[r[1]] - s[r[0]])
        run = (s[i], s[j])
    if run is None:
        h = z - np.asarray(ground_fn(xy), np.float64)
        m = int(np.argmax(h))
        if h[m] < 8.0:
            return None
        i = j = m                                   # the run around the highest point over half its height
        while i > 0 and h[i - 1] >= 0.5 * h[m]:
            i -= 1
        while j < len(h) - 1 and h[j + 1] >= 0.5 * h[m]:
            j += 1
        run = (s[i], s[j])
    c = (run[0] + run[1]) / 2
    share = ARCH_SPAN_SHARE if info["type"] in ARCH_TYPES else SPAN_SHARE
    span = (info["span"] if str(info.get("src", "")).endswith(("nbi", "wd")) and info.get("span")
            else share * (run[1] - run[0]))
    span = float(min(span, SPAN_MAX, s[-1] * 0.9))
    if span < SPAN_MIN:
        return None
    c = float(np.clip(c, span / 2, s[-1] - span / 2))
    return k, c, span


def _ribs(P, tans, arcs, k, s0, s1, n):
    """n + 1 stations from arc s0 to s1 on part k: [(s, p, t, nrm, lo, hi, zlo, zhi)]."""
    out = []
    for sc in np.linspace(s0, s1, n + 1):
        out.append((float(sc),) + _station(P, tans, k, float(sc), arcs))
    return out


def _sz(span, a, lo, hi):
    return float(np.clip(span * a, lo, hi))


def arch_members(P, arcs, tans, ms, info, ground_fn):
    """The ribs, hangers / spandrel posts and bracing of an arch over (through) or under (deck) its main span."""
    k, c, L = ms
    n = max(int(round(L / HANGER_M)), 4)
    st = _ribs(P, tans, arcs, k, c - L / 2, c + L / 2, n)
    deck_c = st[n // 2][7]
    ends = np.array([st[0][1], st[-1][1]])
    g_ends = np.asarray(ground_fn(ends), np.float64)
    g_mid = float(np.asarray(ground_fn(st[n // 2][1][None]), np.float64)[0])
    through = info["type"] == NBI_THROUGH_ARCH or (info["type"] != NBI_DECK_ARCH and deck_c - g_mid < 0.9 * ARCH_RISE * L)
    rw, rh = _sz(L, 1 / 120, 1.0, 4.5), _sz(L, 1 / 60, 1.2, 9.0)        # rib (width, depth)
    beams = []
    if through:
        tied = L < TIED_SPAN_M
        z0 = [st[0][6] - 1.0, st[-1][6] - 1.0] if tied else [max(float(g), st[0][6] - L / 8) for g in g_ends]
        top = deck_c + THROUGH_TOP * L
    else:
        z0 = [float(g) for g in g_ends]
        top = deck_c - 1.5 - rh / 2
    x = np.linspace(-1, 1, n + 1)
    base = z0[0] + (z0[1] - z0[0]) * (x + 1) / 2
    zr = base + (top - base) * (1 - x ** 2)
    trussed = L >= ARCH_TRUSS_M
    dz_t = (ARCH_DEPTH_CROWN + (ARCH_DEPTH_END - ARCH_DEPTH_CROWN) * x ** 2) * L if trussed else np.zeros(n + 1)
    if not through:                                 # a deck arch's upper chord stays under the deck
        dz_t = np.clip(np.minimum(dz_t, np.array([s[6] for s in st]) - 1.5 - rh / 2 - zr), 0.0, None)
    for side in (0, 1):
        pts = [np.r_[(s[1] + s[3] * (s[5] if side else s[4])), z] for s, z in zip(st, zr)]
        for a in range(n):
            beams.append((pts[a], pts[a + 1], rw, rh))
        if trussed:                                 # a trussed rib (Sydney's): an upper chord, posts, diagonals
            up = [p + np.r_[0.0, 0.0, d] for p, d in zip(pts, dz_t)]
            for a in range(n):
                beams.append((up[a], up[a + 1], rw, rh))
                beams.append((pts[a], up[a], rw * 0.6, rw * 0.6))
                beams.append((pts[a], up[a + 1]) if a % 2 == 0 else (up[a], pts[a + 1]))
                beams[-1] = beams[-1] + (rw * 0.5, rw * 0.5)
            beams.append((pts[n], up[n], rw * 0.6, rw * 0.6))
        for a in range(1, n):
            s = st[a]
            e = s[1] + s[3] * (s[5] if side else s[4])
            dz = zr[a] - s[7]
            if through and dz > 1.0:                    # a hanger down to the deck
                beams.append((np.r_[e, zr[a] - rh / 2], np.r_[e, s[6] - 0.8], 0.25, 0.25))
            elif not through and s[6] - zr[a] > 1.5:    # a spandrel post up to the deck
                beams.append((np.r_[e, zr[a] + rh / 2], np.r_[e, s[6] - 1.2], 0.8, 0.8))
    for a in range(1, n):                                # bracing between the ribs, clear of the traffic
        s = st[a]
        if not through or zr[a] - s[7] > 7.0:
            zb = zr[a] + dz_t[a]
            Lp = np.r_[s[1] + s[3] * s[5], zb]
            Rp = np.r_[s[1] + s[3] * s[4], zb]
            beams.append((Lp, Rp, 0.5, 0.7))
    return beams


def suspension_members(P, arcs, tans, ms, info, ground_fn, stayed=False):
    """Towers, main cables and hangers of a suspension span; with stayed, pylons and fans of stays."""
    k, c, L = ms
    s_part = arcs[k]
    towers = [c - L / 2, c + L / 2]
    beams = []
    H = (STAYED_TOWER if stayed else SUSP_TOWER) * L
    leg = (_sz(L, 1 / 250, 2.5, 8.0), _sz(L, 1 / 180, 3.0, 10.0))
    tops = []
    for sc in towers:
        p, t, nrm, lo, hi, zlo, zhi = _station(P, tans, k, sc, arcs)
        gz = float(np.asarray(ground_fn(p[None]), np.float64)[0])
        top = zhi + H
        legs = []
        for off in (lo - leg[0] / 2, hi + leg[0] / 2):
            e = p + nrm * off
            beams.append((np.r_[e, gz - 2.0], np.r_[e, top], leg[0], leg[1]))
            legs.append(e)
        for zz in (top - 1.5, top - 0.3 * H, zlo - 3.0):     # portal beams (one under the decks)
            if zz > gz + 1.0:
                beams.append((np.r_[legs[0], zz], np.r_[legs[1], zz], leg[0] * 0.8, 2.5))
        tops.append((p, t, nrm, lo, hi, top))
    if not stayed:
        side = SUSP_SIDE * L
        s0, s1 = max(towers[0] - side, 0.0), min(towers[1] + side, s_part[-1])
        n = max(int(round((s1 - s0) / HANGER_M)), 4)
        st = _ribs(P, tans, arcs, k, s0, s1, n)
        for which in (0, 1):
            prev = None
            for s in st:
                sc, p, t, nrm, lo, hi, zlo, zhi = s
                e = p + nrm * ((hi + leg[0] / 2) if which else (lo - leg[0] / 2))
                if sc < towers[0]:                      # side span: straight down to the anchorage at the deck
                    f = (sc - s0) / max(towers[0] - s0, 1e-9)
                    zc = (zhi + 1.0) + (tops[0][5] - zhi - 1.0) * f ** 1.3
                elif sc > towers[1]:
                    f = (s1 - sc) / max(s1 - towers[1], 1e-9)
                    zc = (zhi + 1.0) + (tops[1][5] - zhi - 1.0) * f ** 1.3
                else:
                    x = (sc - c) / (L / 2)
                    low = zhi + SUSP_LOW_M
                    zc = low + (min(tops[0][5], tops[1][5]) - low) * x ** 2
                q = np.r_[e, zc]
                if prev is not None:
                    beams.append((prev, q, 0.9, 0.9))
                if zc - zhi > 1.5 and abs(sc - towers[0]) > 2 and abs(sc - towers[1]) > 2:
                    beams.append((q, np.r_[e, zhi - 0.6], 0.12, 0.12))
                prev = q
    else:
        for ti, sc_t in enumerate(towers):
            p, t, nrm, lo, hi, top = tops[ti]
            reach = L / 2
            for sc in np.arange(sc_t - reach + HANGER_M / 2, sc_t + reach, HANGER_M):
                if abs(sc - sc_t) < 6.0 or sc < 0 or sc > s_part[-1]:
                    continue
                s = _station(P, tans, k, float(sc), arcs)
                f = min(abs(sc - sc_t) / reach, 1.0)
                anchor_z = top - 0.3 * H * f
                for which, off in ((0, lo), (1, hi)):
                    e_t = p + nrm * off
                    e_d = s[0] + s[2] * (s[4] if which else s[3])
                    beams.append((np.r_[e_t, anchor_z], np.r_[e_d, s[6] - 0.2], 0.25, 0.25))
    return beams


def structure_members(parts, info, ground_fn, wet_fn=None, road_lines=()):
    """(beams, piers) of a chain's structure (chain_structure): a truss (truss_members, a lens or a curved top chord
    where info["shape"] says), an arch, a suspension or a cable-stayed span (one main span: main_span, centred on the
    river: _river), a movable span (movable_members); ([], []) for a plain deck. Each beam carries its paint (p, q, w,
    h, paint)."""
    if info is None:
        return [], []
    beams, piers = _structure_members(parts, info, ground_fn, wet_fn, road_lines)
    pt = info.get("paint") or "grey"
    return [tuple(b[:4]) + (pt,) for b in beams], piers


def _structure_members(parts, info, ground_fn, wet_fn, road_lines):
    if info["type"] in TRUSS_TYPES:
        return truss_members(parts, info, ground_fn, wet_fn, with_piers=True, road_lines=road_lines)
    if info["type"] in MOVABLE_TYPES:
        return movable_members(parts, info, ground_fn, wet_fn, road_lines)
    if info["type"] not in ARCH_TYPES + (NBI_SUSPENSION, NBI_STAYED):
        return [], []
    P, arcs, tans = _prep(parts)
    ms = main_span(P, arcs, info, ground_fn, _river(P, info, ground_fn, wet_fn))
    if ms is None:
        return [], []
    if info["type"] in ARCH_TYPES:
        return _clear_of_decks(arch_members(P, arcs, tans, ms, info, ground_fn), P), []
    return _clear_of_decks(suspension_members(P, arcs, tans, ms, info, ground_fn, stayed=info["type"] == NBI_STAYED),
                           P), []


LIFT_TOWER = 0.42       # a lift span's towers: their tops over the deck, a share of the span (the Hawthorne Bridge:
                        # 74 m span, towers 32 m over its deck)
LIFT_TOWER_MIN, LIFT_TOWER_MAX = 18.0, 70.0
TOWER_LEG = (1.4, 1.4)  # m, a lift tower's legs
HOUSE = (5.0, 5.0)      # m, a bascule's control house (plan)
HOUSE_UP_M = 7.0        # ... its roof over the deck
LEAF_GIRDER = (0.8, 2.6)    # m, the steel girders along a bascule's leaves under the deck's edges
BASCULE_PIER_T = 12.0   # m along the bridge: a bascule pier (it holds the leaves' counterweights)


def _river(P, info, ground_fn, wet_fn):
    """wet(xy) -> bool: the sea's depth (wet_fn) or the low ground under the structure (low_water), either; None
    without both. Where the sea knows no river (Pittsburgh's, Portland's Willamette) main_span centred an arch or a
    tower pair on the bridge's highest point or its outline instead of the water (the Fort Pitt Bridge's arch on its
    approach, 2026-10-27)."""
    low = low_water(P, ground_fn, _inside(P, info))
    if low is None:
        return wet_fn
    if wet_fn is None:
        return low
    return lambda xy: np.asarray(wet_fn(xy), bool) | np.asarray(low(xy), bool)


def _river_span(P, arcs, info, river):
    """(part, centre arc, span) of a movable bridge's opening: the inventory's span (else SPAN_SHARE of the water)
    centred on the longest run over the river (river(xy) -> bool) of any part inside the bridge's outline; None
    without one (the opening was put on an interchange ramp by main_span's longest-inside part, the Morrison
    Bridge's, and over no water on an approach chain, the Burnside's, 2026-10-27)."""
    if river is None:
        return None
    ins = _inside(P, info)
    best = None
    for k, (xy, _, _) in enumerate(P):
        s = arcs[k]
        m = np.asarray(river(xy), bool) & (ins[k] if ins is not None else True)
        for i, j in _runs(m, s, 1.0):
            if best is None or s[j] - s[i] > best[0]:
                best = (s[j] - s[i], k, (s[i] + s[j]) / 2)
    if best is None or best[0] < SPAN_MIN:
        return None
    run, k, c = best
    L = float(info.get("span") or SPAN_SHARE * run)
    L = min(L, run, arcs[k][-1] * 0.9)
    c = float(np.clip(c, L / 2, arcs[k][-1] - L / 2))
    return k, c, L


def movable_members(parts, info, ground_fn, wet_fn=None, road_lines=()):
    """(beams, piers) of a movable bridge's main span (NBI 15-17; its span from the inventory, centred on the water by
    main_span). A lift or swing span, or a bascule the map or Wikidata calls a truss (info["truss"]), is a through truss
    over the river spans (its top chord curved where the map says arch: info["shape"]); a lift span gets its two
    towers, LIFT_TOWER of its span over the deck. A bascule without one is a deck girder: its leaves' steel girders
    under the deck's edges, a control house beside the road at each end of the opening and the solid bascule piers
    under them (the Burnside and Morrison bridges were plain flat decks, the user's screenshots 2026-10-26)."""
    from roadmesh import DECK_DEPTH, GIRDER_D
    beams, piers = [], []
    kind = info["type"]
    if kind in (NBI_LIFT, NBI_SWING) or info.get("truss"):
        beams, piers = truss_members(parts, {**info, "type": NBI_THROUGH_TRUSS, "movable": True}, ground_fn, wet_fn,
                                     with_piers=True, road_lines=road_lines)
    P, arcs, tans = _prep(parts)
    ms = _river_span(P, arcs, info, _river(P, info, ground_fn, wet_fn))
    if ms is None:
        return beams, piers                     # an opening over no water: another chain's (the approach ramps')
    k, c, L = ms
    ends = [_station(P, tans, k, float(sc), arcs) for sc in (c - L / 2, c + L / 2)]
    if kind == NBI_LIFT:
        top = float(np.clip(LIFT_TOWER * L, LIFT_TOWER_MIN, LIFT_TOWER_MAX))
        for p, t, nrm, lo, hi, zlo, zhi in ends:
            z0, z1 = zlo - DECK_DEPTH, zhi + top
            legs = [p + nrm * off + t * a for off in (lo - 0.8, hi + 0.8) for a in (-2.0, 2.0)]
            for e in legs:
                beams.append((np.r_[e, z0], np.r_[e, z1], *TOWER_LEG))
            for zz in np.arange(zhi + 8.0, z1 + 1e-6, max(6.0, (z1 - zhi - 8.0) / 4.0)):
                for a, b in ((0, 2), (1, 3), (0, 1), (2, 3)):
                    beams.append((np.r_[legs[a], zz], np.r_[legs[b], zz], 0.5, 0.6))
            mid = p + nrm * (lo + hi) / 2
            beams.append((np.r_[mid - nrm * (hi - lo) / 2, z1 + 1.5], np.r_[mid + nrm * (hi - lo) / 2, z1 + 1.5],
                          4.5, 3.0))                # the machinery house on top
    elif kind == NBI_BASCULE and not info.get("truss"):
        sc0, sc1 = c - L / 2, c + L / 2
        n = max(int(round(L / PANEL_M)), 2)
        st = _ribs(P, tans, arcs, k, sc0, sc1, n)
        for which in (4, 5):                        # the leaves' girders under each edge
            for a in range(n):
                e0 = st[a][1] + st[a][3] * (st[a][which] + (1.0 if which == 4 else -1.0))
                e1 = st[a + 1][1] + st[a + 1][3] * (st[a + 1][which] + (1.0 if which == 4 else -1.0))
                beams.append((np.r_[e0, st[a][6] - DECK_DEPTH - LEAF_GIRDER[1] / 2],
                              np.r_[e1, st[a + 1][6] - DECK_DEPTH - LEAF_GIRDER[1] / 2], *LEAF_GIRDER))
        for p, t, nrm, lo, hi, zlo, zhi in ends:
            for off in (lo - HOUSE[0] / 2, hi + HOUSE[0] / 2):
                e = p + nrm * off
                beams.append((np.r_[e, zlo - DECK_DEPTH - GIRDER_D], np.r_[e, zhi + HOUSE_UP_M], *HOUSE))
            gz = float(np.asarray(ground_fn(p[None]), np.float64)[0])
            z_top = zlo - DECK_DEPTH - GIRDER_D
            if z_top - gz > 2.0:
                piers.append((float(p[0]), float(p[1]), float(t[0]), float(t[1]), float(z_top), gz - 2.0,
                              float(hi - lo + HOUSE[0] * 2), BASCULE_PIER_T))
    return beams, piers


HEADROOM_M = 5.0        # _clear_of_decks: a member this little over a carriageway is in the traffic's way
HEADROOM_STEP_M = 2.0


def _clear_of_decks(beams, P):
    """The members [(p, q, w, h, ...)] that keep HEADROOM_M over every deck of P (xy, z, width) they pass over: a stay
    whose fan reached over the other carriageway where the two part on a curve came down to car height across its
    lanes (an autodrive hit one at 49 mph on the Port Mann Bridge's south approach, GitHub #65). Members beside the
    decks or under them stay. Pure."""
    if not beams or not P:
        return beams
    segs = [(xy[:-1], xy[1:], z[:-1], z[1:], w / 2.0) for xy, z, w in P if len(xy) >= 2]
    A = np.concatenate([a for a, _, _, _, _ in segs])
    B = np.concatenate([b for _, b, _, _, _ in segs])
    ZA = np.concatenate([za for _, _, za, _, _ in segs])
    ZB = np.concatenate([zb for _, _, _, zb, _ in segs])
    HW = np.concatenate([np.full(len(a), h) for a, _, _, _, h in segs])
    AB = B - A
    L2 = np.maximum((AB * AB).sum(1), 1e-12)
    out = []
    for b in beams:
        p, q = np.asarray(b[0], np.float64), np.asarray(b[1], np.float64)
        n = max(int(np.hypot(*(q[:2] - p[:2])) / HEADROOM_STEP_M), 1) + 1
        pts = p[None] + (q - p)[None] * np.linspace(0.0, 1.0, n)[:, None]
        lo_b, hi_b = pts[:, :2].min(0) - 30.0, pts[:, :2].max(0) + 30.0
        near = ~((np.maximum(A, B) < lo_b).any(1) | (np.minimum(A, B) > hi_b).any(1))
        if not near.any():
            out.append(b)
            continue
        a, ab, l2 = A[near], AB[near], L2[near]
        t = np.clip(((pts[:, None, :2] - a[None]) * ab[None]).sum(2) / l2[None], 0.0, 1.0)
        d = np.hypot(*(pts[:, None, :2] - (a[None] + t[..., None] * ab[None])).transpose(2, 0, 1))
        dz = pts[:, 2:3] - (ZA[near][None] + (ZB[near] - ZA[near])[None] * t)
        over = (d < HW[near][None] - 0.3) & (dz > -0.5) & (dz < HEADROOM_M)
        if not over.any():
            out.append(b)
    return out
