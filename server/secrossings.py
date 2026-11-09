"""Sweden's crossings as the map has them (roads.build_tile, country SE), and its priority signs.

junctions.find draws a crosswalk on every arm with two corners and a stop line behind it, as West Coast USA's streets
look. A Swedish street is painted by rule (TSFS 2010:171): M15 only where a B3 stands or signals are (4 kap. 8 §), M13
only at a B2 or at signals (3 kap. 1, 3 §§), so nothing at a junction under the right-hand rule. OpenStreetMap maps
these nodes (Overture's infrastructure layer) and most say whether there is paint. Tallied 2026-11-02, within 1.5 km of
the middle of Borås, Göteborg, Stockholm, Malmö and Umeå: 7427 crossings, 95% within 2 m of a road's centreline.

A crossing with no word on its paint counts as painted. crossing=uncontrolled is OpenStreetMap's word for a marked
crossing without signals; of the signal crossings that say, 614 of 722 have a zebra; of the bare ones that say, 296 of
492. A guess, but the map's majority in each case, and all of them are mapped crossings where a kerb is lowered.

Germany reads the same nodes when its junction signs are on (wanted): its own reading of the crossings (de_paint, for
designs.junction_paint's Zeichen 293) and the stop and give-way points (designs.mapped_rows).
"""
import json
import logging
import math

import numpy as np

import furniture
import junctions

M15, M16 = 1, 2                 # paint: bits of paint()
CLASSES = ("crossing", "give_way", "stop", "traffic_signals")
UNPAINTED = ("unmarked", "informal", "no")
NODE_NEAR_M = 2.0               # m from a carriageway's centreline a crossing is on that road (95% of them, above)
SIGNALS_NEAR_M = 15.0           # m from a set of signals a crossing is under them, whatever its own tags say
ONE_CROSSING_M = 5.0            # m along a road within which two nodes are one crossing
ON_JUNCTION_M = 1.0             # m from where three roads meet a node has no road to cross, nor a way to lie across it
                                # (16 of 649 near Göteborg stood on another road, 10 of them exactly on the junction)

# M15: bars 0.50 m wide, 0.50 m apart; at least 2.5 m long up to 60 km/h, at least 4.0 m above (TSFS 2010:171 4 kap. 9 §).
# The stock texture has four bars to a repeat: 4.0 m of it are bars of 0.50 m (junctions.CROSSWALK's 4.8: 0.60 m).
M15_LOOK = dict(junctions.CROSSWALK, tl=4.0)
M15_L, M15_FAST_L, M15_FAST_KMH = junctions.CROSSWALK["w"], 4.0, 60.0
# M16: 0.50 m squares, 0.50 m apart (TSFS 2010:171 4 kap. 10 §), a row each side of the cycle crossing (VGU 2024 9.3.8.3)
# that is 2.5 m wide (its råd for a two-way cycle crossing, 9.3.8.1)
M16_SIDE, M16_GAP, M16_RIDE = 0.50, 0.50, 2.5


def wanted(country, de=False):
    """Whether a place's road tiles and road graph read the map's crossing, stop and give-way nodes (read): Sweden's
    always (its paint and signs are the map's), Germany's when its junction signs are on (de: the setting on and the
    junctions the tile decides, or the graph's window, reaching into Germany: designs.reads_here; the country code
    alone is the nearest airport's). Nowhere else: no read, the guesses as before."""
    return str(country or "").upper() == "SE" or bool(de)


def _marks(tags):
    return set(str(tags.get("crossing:markings", "")).replace(":", ";").split(";")) - {""}


def paint(tags):
    """M15 | M16 | 0: what a crossing node's OpenStreetMap tags say is on the road. crossing:markings=dots (and dashes) is
    a row of squares each side, a cycle crossing; surface alone is a coloured or paved crossing with no white on it."""
    marks = _marks(tags)
    what = (M15 if marks & {"zebra", "yes"} else 0) | (M16 if marks & {"dots", "dashes"} else 0)
    if what or marks:
        return what
    return 0 if tags.get("crossing") in UNPAINTED else M15


DE_MARKED = ("zebra", "marked", "uncontrolled")     # crossing=: a marked crossing without signals


def de_paint(tags):
    """M15 (a Fußgängerüberweg, Zeichen 293) | 0: Germany's reading of a crossing node's tags (Sweden's majority guess
    in paint() is not Germany's: a Fußgängerüberweg is rare, and the design paints "only where OpenStreetMap maps
    a marked crossing ... unmarked crossings get none"). None at signals, whatever the markings
    (crossing=traffic_signals or crossing:signals=yes; VwV-StVO zu § 26 Rn. 5: "In der Nähe von Lichtzeichenanlagen ...
    dürfen Fußgängerüberwege nicht angelegt werden"); else crossing:markings decides where mapped (zebra, or yes: an
    unsignalised marked crossing is a Fußgängerüberweg in Germany; no, dots, lines, surface ...: none); else
    crossing=zebra / marked / uncontrolled, or crossing_ref=zebra. A bare highway=crossing, crossing=island (a refuge,
    no markings), unmarked, informal: none. The road it is on decides the rest (designs.junction_paint: in town and at
    most 50 km/h, zu § 26 Rn. 1: "Fußgängerüberwege dürfen nur innerhalb geschlossener Ortschaften und nicht auf
    Straßen angelegt werden, auf denen schneller als 50 km/h gefahren werden darf")."""
    if tags.get("crossing") == "traffic_signals" or tags.get("crossing:signals") == "yes":
        return 0
    marks = _marks(tags)
    if marks:
        return M15 if marks & {"zebra", "yes"} else 0
    return M15 if tags.get("crossing") in DE_MARKED or tags.get("crossing_ref") == "zebra" else 0


def nodes(frame, infra_list):
    """{"crossings": [(x, y, paint)], "zebras": [(x, y)] of those painted M15 and no signal's (sesigns' B3),
    "de_crossings": [(x, y, de_paint)] Germany's reading of the same crossings, M15 none within SIGNALS_NEAR_M of a
    mapped signal (designs.junction_paint), "stop_lines": (n, 2) xy of the mapped stop lines (road_marking=stop_line on
    a signal or stop node), "priority": (n, 3) x, y, 1 at a stop sign and 0 at a give-way sign} of infrastructure
    Features of map cells, each node once; None when none could be read or they have no tags (the road tiles then
    guess, as elsewhere)."""
    if not infra_list or all(f is None for f in infra_list):
        return None
    seen, found = set(), []
    for f in infra_list:
        if f is None or not len(f):
            continue
        cls, sub, tags = f.attrs.get("class"), f.attrs.get("subtype"), f.attrs.get("tags")
        if cls is None:
            continue
        if tags is None:
            return None
        classes = np.asarray(cls).astype(str)
        hit = np.isin(classes, CLASSES)         # (whole arrays, as furniture.mapped_signals)
        if sub is not None:
            hit &= np.asarray(sub).astype(str) == "transportation"
        for i in np.flatnonzero(hit):
            fid = str(f.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            a = f.coord_off[f.ring_off[f.part_off[i]]]
            b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
            try:
                tag = json.loads(str(tags[i]))
            except ValueError:
                tag = {}
            found.append((str(classes[i]), f.lonlat[a:b].mean(axis=0), tag if isinstance(tag, dict) else {}))
    if found:
        ll = np.array([q[1] for q in found], np.float64)
        e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
        xy = np.column_stack([np.asarray(e), np.asarray(n)])
    crossings, lines, priority, zebras, signals, de = [], [], [], [], [], []
    for (cls, _, tag), p in zip(found, xy if found else ()):
        if cls == "crossing":
            crossings.append((float(p[0]), float(p[1]), paint(tag)))
            de.append((float(p[0]), float(p[1]), de_paint(tag)))
            if paint(tag) & M15 and tag.get("crossing") != "traffic_signals":
                zebras.append((float(p[0]), float(p[1])))
        elif cls == "traffic_signals":
            signals.append((float(p[0]), float(p[1])))
        elif cls in ("give_way", "stop"):
            priority.append((float(p[0]), float(p[1]), float(cls == "stop")))
        if cls != "crossing" and tag.get("road_marking") == "stop_line":
            lines.append((float(p[0]), float(p[1])))

    def lit(z):
        return any(math.hypot(z[0] - q[0], z[1] - q[1]) < SIGNALS_NEAR_M for q in signals)
    zebras = [z for z in zebras if not lit(z)]
    de = [(x, y, 0 if w and lit((x, y)) else w) for x, y, w in de]
    return {"crossings": crossings, "zebras": zebras, "de_crossings": de,
            "stop_lines": np.array(lines, np.float64).reshape(-1, 2),
            "priority": np.array(priority, np.float64).reshape(-1, 3)}


def read(md, frame, keys):
    """nodes of the map cells keys (md: mapdata.MapData), None when their infrastructure cannot be read."""
    try:
        return nodes(frame, [md.cell(k, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
                             for k in keys])
    except Exception as e:      # guessed crossings rather than no road tile
        logging.getLogger("earthd.crossings").debug("crossings: %s: %s", type(e).__name__, e)
        return None


def _squares(mid, u, n, half, z):
    """M16's row across a road of half width half with its middle at mid: the squares' corners (4, 3)."""
    pitch = M16_SIDE + M16_GAP
    count = int((2 * half + M16_GAP) // pitch)
    out = []
    for k in range(count):
        c = mid + n * (k - (count - 1) / 2) * pitch
        corners = [c + (u * a + n * b) * M16_SIDE / 2 for a, b in ((1, 1), (-1, 1), (-1, -1), (1, -1))]
        out.append(np.c_[np.array(corners), np.full(4, z)])
    return out


def place(roads, found, on=None):
    """(crosswalks [{"a", "b", "w"[, "paint": False]}], squares [(4, 3)], ramps {(fid, side): [(point, u, t0, t1)]}) of the
    crossing nodes in found (nodes) on the paved roads in roads [(fid, spec, centreline (k, 3), lon)]: square to the road
    from kerb to kerb, a crosswalk without paint standing for M16's band so the lines stop at it too. Crossing and cycle
    crossing side by side go along the road's own direction, the same from every tile: the tags don't say which side
    each is on. A crossing with no paint still gets its lowered kerbs; a node on a junction is left out. on: a list
    given, each crosswalk's road (fid, spec) appended to it in the crosswalks' order (Germany's: designs.junction_paint
    asks where a Fußgängerüberweg may be)."""
    lines = [(fid, spec, np.asarray(pts, np.float64)) for fid, spec, pts, _ in roads
             if spec.kind in ("paved", "cobble") and spec.cls != "motorway" and len(pts) >= 2]
    walks, squares, ramps, taken = [], [], {}, []
    if not lines:
        return walks, squares, ramps
    a = np.vstack([pts[:-1, :3] for _, _, pts in lines])
    b = np.vstack([pts[1:, :3] for _, _, pts in lines])
    own = np.repeat(np.arange(len(lines)), [len(pts) - 1 for _, _, pts in lines])
    d = b[:, :2] - a[:, :2]
    l2 = np.maximum((d * d).sum(1), 1e-9)
    meet = np.array([arms[0][3][:2] for arms in furniture._arms(roads).values() if len(arms) >= 3]).reshape(-1, 2)
    for x, y, what in found["crossings"]:
        t = np.clip(((np.array([x, y]) - a[:, :2]) * d).sum(1) / l2, 0.0, 1.0)
        q = a[:, :2] + d * t[:, None]
        off = np.hypot(q[:, 0] - x, q[:, 1] - y)
        i = int(np.argmin(off))
        if off[i] > NODE_NEAR_M:
            continue                    # (on a footway, or a road not in this tile's list)
        fid, spec, _ = lines[own[i]]
        c = q[i]
        if len(meet) and np.hypot(*(meet - c).T).min() < ON_JUNCTION_M:
            continue
        if any(f == fid and np.hypot(*(c - e)) < ONE_CROSSING_M for f, e in taken):
            continue
        taken.append((fid, c))
        u = d[i] / float(np.sqrt(l2[i]))
        n = np.array([-u[1], u[0]])
        z = float(a[i, 2] + t[i] * (b[i, 2] - a[i, 2]))
        half = spec.width / 2
        for side in (1, -1):
            ramps.setdefault((fid, side), []).append((c.copy(), u, -M15_L / 2, M15_L / 2))
        long_ = M15_FAST_L if (spec.kmh or 0.0) > M15_FAST_KMH else M15_L
        band = M16_RIDE + 2 * M16_SIDE              # M16's: from the outer edge of one row to the other's
        at_m15 = -band / 2 if what == M15 | M16 else 0.0
        at_m16 = long_ / 2 if what == M15 | M16 else 0.0
        if what & M15:
            m = c + u * at_m15
            walks.append({"a": np.r_[m + n * half, z], "b": np.r_[m - n * half, z], "w": long_})
            if on is not None:
                on.append((fid, spec))
        if what & M16:
            m = c + u * at_m16
            walks.append({"a": np.r_[m + n * half, z], "b": np.r_[m - n * half, z], "w": band, "paint": False})
            if on is not None:
                on.append((fid, spec))
            squares += _squares(m + u * (band - M16_SIDE) / 2, u, n, half, z)
            squares += _squares(m - u * (band - M16_SIDE) / 2, u, n, half, z)
    return walks, squares, ramps
