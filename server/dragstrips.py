"""Drag strips for the stock drag system (the mod's dragstrips.lua; design: BeamMaps
earth-dragstrip-functional-2026-11-03.md). OpenStreetMap's highway=raceway ways that are drag strips, from their data
only (no list of tracks):

- a name or tag that says so: "*dragway*", "*drag strip*", "*drag racing*", "*raceway park*", or sport / raceway
  tags containing "drag" (Layercake's highways file has no sport column today; read when a source carries it);
- or the shape: a straight of at least SHAPE_MIN_M that starts at a dead end (no other raceway way at that end) on a
  one-way way or a way that is all straight (Bandimere: "Bandimere Speedway", one straight one-way way of 1.1 km).
  A name with "pit" never counts by shape (pit lanes are straight and dead-ended too).

Per strip the tile carries (anchor frame, m; heights on the ground): the two lanes' stage and finish points (finish
= stage + 402.336 m, or 201.168 m on a strip too short for a quarter mile), the end of the straight (the shutdown's),
the lanes' half spacing. The mod builds the tree, boards, waypoints, zones and the stage POIs from them.

Direction: a one-way way's sense; else from the dead-end start; else the way's own order (flagged "wayorder")."""
import logging
import re

import numpy as np

log = logging.getLogger("bne.dragstrips")

QUARTER_M = 402.336
EIGHTH_M = 201.168
FIT = 0.98                  # a straight this share of a distance still runs it (the way mapped start to finish only)
STRAIGHT_TOL_M = 2.0        # a straight's points within this of its chord
SHAPE_MIN_M = 380.0         # shape-only detection: the straight at least this long
STAGE_PAD_M = 30.0          # the stage line this far into the straight (the burnout box behind it)
MIN_SHUTDOWN_M = 100.0      # kept past the finish before any pad is taken
LANE_HALF_M = 4.4           # lane centre off the strip's centre (Alder: 4.3 to 4.8)
LANE_HALF_RANGE = (3.0, 5.0)
ACCESS_M = 600.0          # the public / pit roads this near a strip's end: that end is its staging end
END_SNAP_M = 3.0            # another raceway way's point this near an end: that end is joined, not a dead end

NAME_RE = re.compile(r"drag\s*-?\s*(way|strip|racing|race)|dragway|raceway\s+park|quarter[\s-]*mile|eighth[\s-]*mile",
                     re.I)
NOT_BY_SHAPE_RE = re.compile(r"\bpit", re.I)
EIGHTH_RE = re.compile(r"1/8|eighth", re.I)
ONEWAY_YES = ("yes", "1", "true")


def by_tags(name, tags=None):
    """Whether a raceway's name or tags call it a drag strip."""
    if name and NAME_RE.search(str(name)):
        return True
    for k in ("sport", "raceway", "racing"):
        v = (tags or {}).get(k)
        if v and "drag" in str(v).lower():
            return True
    return False


def longest_straight(xy, tol=STRAIGHT_TOL_M, near=0.85):
    """(i, j, length m) of polyline xy's first run i..j (in the way's order) at least near x the longest whose points
    all lie within tol of the chord i-j (a way drawn as strip, turnaround and return road: the strip first, Island
    Dragway). Pure."""
    runs = _runs(np.asarray(xy, np.float64), tol)
    top = max(r[2] for r in runs)
    return next(r for r in runs if r[2] >= near * top)


def _runs(xy, tol):
    """[(i, j, length)] the longest straight from each start i."""
    n = len(xy)
    out = []
    for i in range(n - 1):
        best = (i, i + 1, float(np.hypot(*(xy[i + 1] - xy[i]))))
        for j in range(n - 1, i, -1):
            d = xy[j] - xy[i]
            L = float(np.hypot(*d))
            if L <= best[2]:
                break
            if j > i + 1:
                u = d / L
                mid = xy[i + 1:j] - xy[i]
                off = np.abs(mid[:, 0] * u[1] - mid[:, 1] * u[0])
                along = mid @ u
                if off.max() > tol or along.min() < -tol or along.max() > L + tol:
                    continue
            best = (i, j, L)
            break
        out.append(best)
    return out


def _near(p, lines):
    """m from point p to the nearest point of lines ((k, 2) arrays); inf without any."""
    return min((float(np.min(np.hypot(*(o - p).T))) for o in lines if len(o)), default=float("inf"))


def _joined(p, others):
    return any(len(o) and float(np.min(np.hypot(*(o - p).T))) <= END_SNAP_M for o in others)


def strip_of(line, others=(), access=()):
    """The drag strip of one raceway, or None. line: {"id", "name", "xy" (k, 2) m, "oneway", "width", "tags"};
    others: the other raceway ways' points ((k, 2) arrays, same frame) for the dead-end test. Pure."""
    xy = np.asarray(line["xy"], np.float64)
    if len(xy) < 2:
        return None
    name = line.get("name") or ""
    tagged = by_tags(name, line.get("tags"))
    i, j, L = longest_straight(xy)
    n = len(xy)
    closed = n > 2 and float(np.hypot(*(xy[0] - xy[-1]))) < 1.0
    ow = str(line.get("oneway") or "").lower()
    dead_i = i == 0 and not closed and not _joined(xy[0], others)
    dead_j = j == n - 1 and not closed and not _joined(xy[-1], others)
    if ow == "-1":
        fwd, conf = False, "oneway"
    elif ow in ONEWAY_YES:
        fwd, conf = True, "oneway"
    elif dead_j and not dead_i:
        fwd, conf = False, "deadend"
    elif dead_i and not dead_j:
        fwd, conf = True, "deadend"
    else:
        fwd, conf = True, "wayorder"
        # no sense in the data: the staging end is the one the other roads come to (the pits, the entrance; the
        # shutdown runs out into the field: Keystone Raceway Park, an unnamed two-node way)
        da, db = _near(xy[i], access), _near(xy[j], access)
        if min(da, db) <= ACCESS_M and abs(da - db) > 10.0:
            fwd, conf = da < db, "access"
    if not tagged:
        start_dead = dead_i if fwd else dead_j
        whole = i == 0 and j == n - 1
        if L < SHAPE_MIN_M or NOT_BY_SHAPE_RE.search(name) or not start_dead or not (conf == "oneway" or whole):
            return None
    if EIGHTH_RE.search(name) or L < QUARTER_M * FIT:
        dist = EIGHTH_M
    else:
        dist = QUARTER_M
    if L < dist * FIT:
        return None
    a, b = (xy[i], xy[j]) if fwd else (xy[j], xy[i])
    h = (b - a) / L
    pad = min(STAGE_PAD_M, max(0.0, L - dist - MIN_SHUTDOWN_M))
    s = a + pad * h
    f = s + dist * h
    r = np.array([h[1], -h[0]])
    try:
        half = float(line.get("width")) / 4.0
        half = min(max(half, LANE_HALF_RANGE[0]), LANE_HALF_RANGE[1]) if np.isfinite(half) else LANE_HALF_M
    except (TypeError, ValueError):
        half = LANE_HALF_M
    lanes = [(s + half * r, f + half * r), (s - half * r, f - half * r)]     # lane 1 right, lane 2 left (Alder's)
    return {"id": f"earth_{int(line['id'])}", "name": name or "Drag strip", "dist": dist, "conf": conf,
            "half": round(half, 2), "lanes": lanes, "centre": s, "end": b}


def strips_of(lines, access=()):
    """Every drag strip of raceway lines (strip_of's dicts), each judged against the others. Pure."""
    out = []
    for k, ln in enumerate(lines):
        others = [np.asarray(o["xy"], np.float64) for m, o in enumerate(lines) if m != k]
        st = strip_of(ln, others, access)
        if st is not None:
            out.append(st)
    return out


def raceway_lines(frame, feats_list):
    """strip_of's line dicts of the raceways in road Features (osmtags.raceway_features, joined), deduplicated."""
    out, seen = [], set()
    for feats in feats_list:
        if feats is None or len(feats) == 0 or "class" not in feats.attrs:
            continue
        cls = feats.attrs["class"]
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if str(cls[i]) != "raceway" or fid in seen or not fid.startswith("osm-raceway-"):
                continue
            seen.add(fid)
            parts = feats.parts(i) or []
            if not parts or not parts[0] or len(parts[0][0]) < 2:
                continue
            ll = np.asarray(parts[0][0], np.float64)
            e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)

            def attr(k, i=i):
                col = feats.attrs.get(k)
                return None if col is None else col[i]
            w = attr("osm_width")
            out.append({"id": int(fid.rsplit("-", 1)[1]), "name": str(attr("name") or ""),
                        "xy": np.stack([np.asarray(e).ravel(), np.asarray(n).ravel()], 1),
                        "oneway": str(attr("osm_oneway") or ""), "width": None if w is None else float(w),
                        "tags": {}})
    return out


def road_points(frame, feats_list):
    """(k, 2) point arrays of the cells' roads other than raceways (strip_of's access), in the frame."""
    out = []
    for feats in feats_list:
        if feats is None or len(feats) == 0 or "class" not in feats.attrs:
            continue
        cls = feats.attrs["class"]
        for i in range(len(feats)):
            if str(cls[i]) == "raceway":
                continue
            for part in feats.parts(i) or []:
                if part and len(part[0]):
                    ll = np.asarray(part[0], np.float64)
                    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
                    out.append(np.stack([np.asarray(e).ravel(), np.asarray(n).ravel()], 1))
    return out


def record(st, frame, elev):
    """A strip as the road tile's JSON record: points [x, y, z] in the frame, z on the ground (elev(lat, lon))."""
    pts = [st["lanes"][0][0], st["lanes"][0][1], st["lanes"][1][0], st["lanes"][1][1], st["end"]]
    xs = np.array([p[0] for p in pts], np.float64)
    ys = np.array([p[1] for p in pts], np.float64)
    la, lo, _ = frame.enu_to_geodetic(xs, ys, np.zeros(len(xs)))
    z = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64).ravel()
    p3 = [[round(float(xs[k]), 3), round(float(ys[k]), 3), round(float(z[k]), 3)] for k in range(len(pts))]
    return {"id": st["id"], "name": st["name"], "dist": st["dist"], "conf": st["conf"], "half": st["half"],
            "lanes": [{"stage": p3[0], "finish": p3[1]}, {"stage": p3[2], "finish": p3[3]}], "end": p3[4]}


def tile_strips(frame, tx, ty, md, elev):
    """The JSON records of the drag strips whose stage line lies in road tile (tx, ty). md: mapdata.MapData. Any
    failure costs the strips, not the tile."""
    import mapcells
    import roads
    box = roads.tile_bbox(tx, ty)
    x0, y0, x1, y1 = box
    try:
        la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1], float), np.array([y0, y0, y1, y1], float), 0.0)
        keys = mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
        feats = [md.cell(k, layers=("roads",), kind="demand").layers.get("roads") for k in keys]
        out = []
        for st in strips_of(raceway_lines(frame, feats), road_points(frame, feats)):
            if roads.box_owns(box, float(st["centre"][0]), float(st["centre"][1])):
                out.append(record(st, frame, elev))
        return out
    except Exception as e:
        log.debug("drag strips of road tile %s_%s: %s", tx, ty, e)
        return []
