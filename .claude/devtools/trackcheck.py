"""trackcheck.py LAT LON [R]: the racetracks (OpenStreetMap highway=raceway, osmtags.raceway_features) within R m
(default 1500) of the point, their road tiles built offline (as rampcheck.py; the revisit cache skipped), and what a
driver finds on them measured (2026-11-03, racetracks: Indianapolis, Interlagos, Road America looked lumpy, stepped
and built over):

  rough     the racing line's height (the drawn surface along each way's centre line, 1 m steps: the terrain where
            it is on the ground, a deck's road where it is on one) against a smooth fit (a 25 m running mean): RMS and
            the largest deviation; the largest grade change from one 10 m stretch to the next (% per 10 m); the
            cross slope's largest change per 10 m (the "tilted flat patches"); bumps (second difference > 0.1 m)
  decks     every bridge range on a raceway (roads.level_ranges), where it came from (the map's own bridge / layer,
            gradesep.py's underpass rule, the lidar's elevated stretches, a deck gap, a skewed reach), its length
            and the height step at each end against the surface going on (an open slab edge when > 0.05 m)
  under     every road or waterway crossing under a raceway: the surface over it, the floor under it, the clearance
            (deck top - DECK_MIN_DEPTH - floor), and the trench dug for it under the track's ground height
  buildings footprints (buildings / building_parts) the carriageway runs through, their bottom (min_height) and how
            far the track runs inside them (buildings.road_lines leaves raceways out: none are lifted)
  gaps      plan gaps between a way's end and the nearest raceway end (0.3-15 m: a strip of grass between pieces)
  width     the drawn width (Spec.width) against OSM's width tag
  extras    road markings, sidewalks (walks) and curb lines drawn on or along the track

Prints one row per track (raceways joined end to end, named by their most common name) and the worst items with
lat/lon. Measures what the tiles draw (blockworker.roads_tile -> roads.build_tile's chunks, ground function, roads_out,
walks)."""
import os, sys, tempfile, logging, collections, math
os.environ.setdefault("BNE_REVISIT_SKIP", "roads")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..", "server")))
logging.basicConfig(level=logging.ERROR)
import numpy as np
import blockworker as bw
import roads as roadsmod
import geodesy
import mapgeom

SMOOTH_M = 25.0         # m: the running mean a racing line is compared with
GRADE_M = 10.0          # m: the stretches grades are taken over
BUMP_M = 0.10           # m: a second difference this large at 1 m steps is a bump / step
STEP_M = 0.05           # m: a deck end this far off the surface going on is an open edge / step
GAP_MIN, GAP_MAX = 0.3, 15.0
DECK_DEPTH = roadsmod.DECK_MIN_DEPTH
LANES = (-0.45, -0.3, -0.15, 0.0, 0.15, 0.3, 0.45)   # of the width: the lanes measured at speed
CHORD_M = 10.0          # m: the chord a car at speed rides over
CHORD_BAR_M = 0.006     # m: a paved oval's 10 m chord stays within a few mm
SPEED_MS = 62.6         # m/s (140 mph)

args = [a for a in sys.argv[1:] if not a.startswith("--")]
LAT, LON = float(args[0]), float(args[1])
R = float(args[2]) if len(args) > 2 else 1500.0
st = bw._State(os.path.expanduser("~/.cache/beamng-earth"), False, True, True, True, False, True, True)
bw._s = st
frame = geodesy.EnuFrame(LAT, LON)


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def latlon(p):
    la, lo, _ = frame.enu_to_geodetic(np.array([float(p[0])]), np.array([float(p[1])]), 0.0)
    return round(float(la[0]), 6), round(float(lo[0]), 6)


def enu(ll):
    e, n, _ = frame.geodetic_to_enu(np.asarray(ll)[:, 1], np.asarray(ll)[:, 0], 0.0)
    return np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], 1)


def resample(xy, step=1.0):
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if s[-1] <= 0:
        return xy[:1], s[:1]
    t = np.arange(0.0, s[-1] + 1e-9, step)
    return np.stack([np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])], 1), t


def near_line(p, n):
    """(plan distance, z there, segment) of point p to polyline n (k, 3)."""
    a, b = n[:-1, :2], n[1:, :2]
    ab = b - a
    t = np.clip(((p[:2] - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    q = a + t[:, None] * ab
    d = np.hypot(*(p[:2] - q).T)
    k = int(np.argmin(d))
    return float(d[k]), float(n[k, 2] + t[k] * (n[k + 1, 2] - n[k, 2])), k


def seg_cross(a, b):
    """[(point, s along a (m))] where polylines a, b (k, 2) cross."""
    out = []
    sa = np.r_[0.0, np.cumsum(np.hypot(*np.diff(a, axis=0).T))]
    if (a.min(0) > b.max(0)).any() or (b.min(0) > a.max(0)).any():
        return out
    for i in range(len(a) - 1):
        p, r = a[i], a[i + 1] - a[i]
        for j in range(len(b) - 1):
            q, s_ = b[j], b[j + 1] - b[j]
            den = r[0] * s_[1] - r[1] * s_[0]
            if abs(den) < 1e-12:
                continue
            qp = q - p
            t = (qp[0] * s_[1] - qp[1] * s_[0]) / den
            u = (qp[0] * r[1] - qp[1] * r[0]) / den
            if 0 <= t <= 1 and 0 <= u <= 1:
                out.append((p + t * r, sa[i] + t * (sa[i + 1] - sa[i])))
    return out


# ---- the map: raceways, the other lines, buildings, from the cells round the point
keys = set()
for tx, ty in roadsmod.tiles_near(0.0, 0.0, R):
    keys.update(roadsmod.cells_of_tile(frame, tx, ty))
ways, others, blds, layers = {}, [], [], []
seen_o = set()
for key in sorted(keys, key=str):
    cell = st.md.cell(key, layers=("roads", "buildings", "building_parts", "water"), kind="demand")
    f = cell.layers.get("roads")
    if f is not None and len(f):
        layers.append(f)
        cls = [str(c) for c in f.attrs["class"]]
        specs = roadsmod.road_specs(f, st.surface.urban if getattr(st, "surface", None) else None, st.widths)
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in ways or fid in seen_o:
                continue
            a = f.coord_off[f.ring_off[f.part_off[i]]]
            b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
            if b - a < 2:
                continue
            xy = enu(f.lonlat[a:b])
            if cls[i] == "raceway":
                if np.hypot(*xy.T).min() > R:
                    continue
                ow = f.attrs["osm_width"][i] if "osm_width" in f.attrs else None
                ways[fid] = dict(fid=fid, xy=xy, f=f, i=i, spec=specs[i], name=str(f.attrs["name"][i] or ""),
                                 osm_width=ow, rngs=roadsmod.level_ranges(f, i),
                                 tag=roadsmod.tag_level_ranges(f, i), raw=roadsmod.raw_level_ranges(f, i))
            else:
                seen_o.add(fid)
                sub = str(f.attrs["subtype"][i]) if "subtype" in f.attrs else ""
                others.append(dict(fid=fid, xy=xy, cls=cls[i], sub=sub, rngs=roadsmod.level_ranges(f, i),
                                   spec=specs[i], osm_width=_num(f.attrs["osm_width"][i]) if "osm_width" in f.attrs
                                   else None))
    wl = cell.layers.get("water")
    if wl is not None and len(wl):
        import riverlines
        for i in range(len(wl)):
            if wl.kind[i] != mapgeom.LINE:
                continue
            fid = str(wl.ids[i])
            if fid in seen_o:
                continue
            seen_o.add(fid)
            a = wl.coord_off[wl.ring_off[wl.part_off[i]]]
            b = wl.coord_off[wl.ring_off[wl.part_off[i + 1]]]
            if b - a >= 2:
                others.append(dict(fid=fid, xy=enu(wl.lonlat[a:b]), cls="water:" + str(wl.attrs["class"][i]),
                                   sub="water", rngs=[], spec=None, culvert=riverlines.underground(wl, i)))
    for ln in ("buildings", "building_parts"):
        bl = cell.layers.get(ln)
        if bl is None or not len(bl):
            continue
        for i in range(len(bl)):
            if bl.kind[i] == mapgeom.LINE:
                continue
            a = bl.coord_off[bl.ring_off[bl.part_off[i]]]
            b = bl.coord_off[bl.ring_off[bl.part_off[i] + 1]]
            if b - a < 3:
                continue
            ll = bl.lonlat[a:b]
            if str(bl.ids[i]) in seen_o:
                continue                    # (a footprint in two cells)
            seen_o.add(str(bl.ids[i]))
            blds.append(dict(fid=str(bl.ids[i]), layer=ln, ll=ll,
                             cls=str(bl.attrs["class"][i]) if "class" in bl.attrs else "",
                             h=float(bl.attrs["height"][i]) if "height" in bl.attrs else float("nan"),
                             mh=float(bl.attrs["min_height"][i]) if "min_height" in bl.attrs else float("nan"),
                             src=str(bl.attrs["src_dataset"][i]) if "src_dataset" in bl.attrs else ""))
if not ways:
    print(f"no raceways within {R:.0f} m of {LAT}, {LON}")
    sys.exit(0)

# ---- the tiles the raceways pass through, built
need = set()
for w in ways.values():
    pts, _ = resample(w["xy"], 20.0)
    for p in np.r_[pts, w["xy"][-1:]]:
        need.add((int(math.floor(p[0] / roadsmod.TILE)), int(math.floor(p[1] / roadsmod.TILE))))
tiles, grounds, ground_roads, walks = [], [], [], []
orig_build = roadsmod.build_tile


def grab_build(frame_, tx, ty, cells, elev, *a, **kw):
    r = orig_build(frame_, tx, ty, cells, elev, *a, **kw)
    tiles.append(r)
    if kw.get("ground") is not None:
        grounds.append((roadsmod.tile_bbox(tx, ty), kw["ground"]))
    ground_roads.extend(kw.get("roads_out") or [])
    walks.extend(kw.get("walks") or [])
    return r


roadsmod.build_tile = grab_build
CAP_PLANS = {}
TRACE = []
if os.environ.get("TC_TRACE"):          # TC_TRACE=lat,lon: every grading layer's value at the point (Grader.apply)
    import grading as _gtr
    _tla, _tlo = map(float, os.environ["TC_TRACE"].split(","))
    _ctx = {}

    def _near_idx(self_, e, n):
        x, y, _ = self_.frame.geodetic_to_enu(np.array([_tla]), np.array([_tlo]), 0.0)
        d = np.hypot(np.asarray(e) - float(x[0]), np.asarray(n) - float(y[0]))
        return np.flatnonzero(d < 0.75)
    _o_on_kept = _gtr._Planner._on_kept

    def _t_on_kept(self_, e, n):
        r = _o_on_kept(self_, e, n)
        k = _near_idx(self_, e, n)
        if len(k):
            _ctx["k"], _ctx["self"] = k, self_
            TRACE.append(("on_kept(race band)", None if r is None else bool(np.asarray(r)[k[0]])))
        else:
            _ctx.pop("k", None)
        return r
    _gtr._Planner._on_kept = _t_on_kept
    _o_gp = _gtr.grade_points

    def _t_gp(px, py, base, plans, lift=0.0):
        r = _o_gp(px, py, base, plans, lift)
        self_ = _ctx.get("self")
        if self_ is not None:
            k = _near_idx(self_, px, py)
            if len(k):
                TRACE.append(("grade_points n=%d" % len(px), round(float(np.asarray(base)[k[0]]), 3), round(float(r[k[0]]), 3)))
        return r
    _gtr.grade_points = _t_gp
    for _nm in ("_race_designed",):
        _o = getattr(_gtr._Planner, _nm)

        def _w(self_, out, race, e, n, plans, lift, _o=_o, _nm=_nm):
            k = _near_idx(self_, e, n)
            b = round(float(out[k[0]]), 3) if len(k) else None
            r = _o(self_, out, race, e, n, plans, lift)
            if len(k):
                TRACE.append((_nm, b, round(float(out[k[0]]), 3), "race", bool(np.asarray(race)[k[0]])))
            return r
        setattr(_gtr._Planner, _nm, _w)
    _o_emb = _gtr._Planner._race_embank

    def _t_emb(self_, e, n, keys, lift):
        r = _o_emb(self_, e, n, keys, lift)
        k = _near_idx(self_, e, n)
        if len(k) and r is not None:
            idx, ez, d = r
            j = np.flatnonzero(np.isin(idx, k))
            TRACE.append(("race_embank", [(round(float(ez[x]), 3), round(float(d[x]), 2)) for x in j[:2]]))
        return r
    _gtr._Planner._race_embank = _t_emb
    _o_apply = _gtr._Planner.apply

    def _t_apply(self_, lat, lon, base, *a, **kw):
        r = _o_apply(self_, lat, lon, base, *a, **kw)
        x, y, _ = self_.frame.geodetic_to_enu(np.asarray(lat).ravel(), np.asarray(lon).ravel(), 0.0)
        tx, ty, _ = self_.frame.geodetic_to_enu(np.array([_tla]), np.array([_tlo]), 0.0)
        d = np.hypot(np.asarray(x) - float(tx[0]), np.asarray(y) - float(ty[0]))
        kk_ = np.flatnonzero(d < 3.0)
        if len(kk_) and os.environ.get("TC_TRACE_NODES"):
            TRACE.append(("apply nodes", sorted((round(float(np.asarray(x)[i] - float(tx[0])), 1), round(float(np.asarray(y)[i] - float(ty[0])), 1),
                                                 round(float(np.asarray(r).ravel()[i]), 3)) for i in kk_)))
        k = np.flatnonzero(d < 0.75)
        if len(k):
            TRACE.append(("apply args", a, {kk: vv for kk, vv in kw.items() if not hasattr(vv, "__len__") or isinstance(vv, str)},
                          "base", round(float(np.asarray(base).ravel()[k[0]]), 3), "-> out", round(float(np.asarray(r).ravel()[k[0]]), 3)))
            TRACE.append(("----",))
        _ctx.pop("self", None)
        return r
    _gtr._Planner.apply = _t_apply
if os.environ.get("TC_PLANS"):          # TC_PLANS=lat,lon: the race plans read near it, printed at the end
    import grading as _gcap
    _orig_rs = _gcap.plan_roads

    def _cap_rs(*a, **k):
        r_ = _orig_rs(*a, **k)
        for p_, (_ll, sp_) in zip(r_, a[1]):
            if p_ is not None and getattr(sp_, "cls", "") == "raceway":
                CAP_PLANS[id(p_)] = (p_, a[0])
        return r_
    _gcap.plan_roads = _cap_rs
if os.environ.get("TC_CONFORM"):        # TC_CONFORM=path: the largest plan set race_conform was handed, pickled there
    import grading as _gcf
    import pickle as _pk
    _orig_cf = _gcf.race_conform
    _cf_best = [0]

    def _cap_cf(plans):
        if len(plans) > _cf_best[0]:
            _cf_best[0] = len(plans)
            with open(os.environ["TC_CONFORM"], "wb") as fh_:
                _pk.dump([{a: getattr(p_, a, None) for a in _gcf.Plan.__slots__} for p_ in plans], fh_)
        return _orig_cf(plans)
    _gcf.race_conform = _cap_cf
import buildings as buildingsmod
built_blds = []
orig_items = buildingsmod.road_tile_items


def grab_items(frame_, tx, ty, *a, **kw):
    r = orig_items(frame_, tx, ty, *a, **kw)
    x0, y0, _x1, _y1 = roadsmod.tile_bbox(tx, ty)
    items = r[0] if isinstance(r, tuple) else r
    for it in items or ():
        sp = it[0] if isinstance(it, tuple) else it
        ring = getattr(sp, "ring", None)
        if ring is not None and len(ring) >= 3:
            built_blds.append((np.asarray(ring, np.float64)[:, :2] + [x0, y0], float(getattr(sp, "min_h", 0) or 0),
                               str(getattr(sp, "cls", ""))))
    return r


buildingsmod.road_tile_items = grab_items
# what the tile streams to the game: its Forest rows (furniture.py's poles and signs, street trees, fences.py,
# racefurniture.py's catch fences, tyre walls and grandstands) and the guard rails, in the frame (trees.lua)
streamed_props, streamed_rails = [], []
orig_write = roadsmod.write_tile


def grab_write(out_dir_, vfs_dir_, aid_, tx_, ty_, chunks_, *a, **kw):
    streamed_props.extend(list(kw.get("props", a[4] if len(a) > 4 else None) or ()))
    streamed_rails.extend(list(kw.get("guardrail_rows") or ()))
    return orig_write(out_dir_, vfs_dir_, aid_, tx_, ty_, chunks_, *a, **kw)


roadsmod.write_tile = grab_write
out = tempfile.mkdtemp()
import atexit, shutil
atexit.register(shutil.rmtree, out, True)
print(f"{len(ways)} raceway ways within {R:.0f} m; building {len(need)} road tiles", flush=True)
for tx, ty in sorted(need):
    try:
        bw.roads_tile("p", LAT, LON, 15, tx, ty, out, "/x", True, True, buildings=True)
    except Exception as e:
        print("tile", tx, ty, "failed:", type(e).__name__, e)


def ground_z(p):
    for b, fn in grounds:
        if b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3]:
            return float(np.asarray(fn(np.array([p[0]]), np.array([p[1]])), np.float64).ravel()[0])
    return np.nan


def ground_zs(P):
    z = np.full(len(P), np.nan)
    for b, fn in grounds:
        m = (P[:, 0] >= b[0]) & (P[:, 0] <= b[2]) & (P[:, 1] >= b[1]) & (P[:, 1] <= b[3]) & np.isnan(z)
        if m.any():
            z[m] = np.asarray(fn(P[m, 0], P[m, 1]), np.float64).ravel()
    return z


seen, decks, lines, tunnels = set(), [], [], []
road_decks = {}         # fid -> [(nodes, width)]: every deck chunk, for a road's own driven surface (road_zs)
for chunks in tiles:
    for c in chunks:
        n = np.asarray(c.get("n") or [], np.float64)
        if n.ndim != 2 or n.shape[1] < 3 or len(n) < 2:
            continue
        k = (c.get("fid"), c.get("m"), len(n), round(float(n[0, 0]), 2), round(float(n[0, 1]), 2))
        if k in seen:
            continue
        seen.add(k)
        if c.get("deck") and not c.get("tunnel"):
            road_decks.setdefault(c.get("fid"), []).append((n, float(c.get("w", 0) or 0)))
        if c.get("tunnel"):
            tunnels.append((n, c))
        elif c.get("deck") and c.get("fixed") and not c.get("ctx"):
            decks.append((n, float(c.get("w", 0)), c))
        elif str(c.get("m", "")).startswith("line_") or c.get("layer") == "edge":
            lines.append((n, c))
race_decks = [(n, w, c) for n, w, c in decks if str(c.get("fid", "")).startswith("osm-raceway-")]


def surface_z(P, fid=None):
    """The drawn driving surface at points P (k, 2): a raceway deck's road where one is under the point (within its
    half width), else the terrain."""
    z = ground_zs(P)
    on = np.zeros(len(P), bool)
    for n, w, c in race_decks:
        if fid is not None and c.get("fid") != fid:
            continue
        lo, hi = n[:, :2].min(0) - w, n[:, :2].max(0) + w
        idx = np.flatnonzero((P[:, 0] >= lo[0]) & (P[:, 0] <= hi[0]) & (P[:, 1] >= lo[1]) & (P[:, 1] <= hi[1]))
        for k in idx:
            d, zz, _ = near_line(P[k], n)
            if d <= w / 2 and (not on[k] or zz > z[k]):
                z[k], on[k] = zz, True
    return z, on


def beside_line(p, n, w):
    """Does point p lie within w of polyline n's line, beside one of its segments (not past either end)?"""
    a, d = n[:-1, :2], np.diff(n[:, :2], axis=0)
    ll = np.maximum((d * d).sum(1), 1e-12)
    t = ((p - a) * d).sum(1) / ll
    perp = np.abs((p - a)[:, 0] * d[:, 1] - (p - a)[:, 1] * d[:, 0]) / np.sqrt(ll)
    return bool(np.any((t >= 0) & (t <= 1) & (perp <= w)))


def road_zs(P, fid):
    """The driven surface of road fid at points P (k, 2): its own deck where one of its bridge chunks is under the
    point (within its half width), else the terrain."""
    z = ground_zs(P)
    for n, w in road_decks.get(fid, ()):
        lo, hi = n[:, :2].min(0) - w, n[:, :2].max(0) + w
        for k in np.flatnonzero((P[:, 0] >= lo[0]) & (P[:, 0] <= hi[0]) & (P[:, 1] >= lo[1]) & (P[:, 1] <= hi[1])):
            d, zz, _ = near_line(P[k], n)
            if d <= max(w / 2, 1.0):
                z[k] = zz
    return z


def running_mean(z, n):
    k = max(1, int(n) | 1)
    if len(z) < k:          # (a way shorter than the window: its straight-line fit, not its mean)
        x = np.arange(len(z), dtype=np.float64)
        return np.polyval(np.polyfit(x, z, 1), x) if len(z) >= 2 else z.copy()
    pad = k // 2
    # (odd reflection at the ends: a steady grade stays a straight line to a way's end; a flat pad made every way end on
    # a 16 % climb read as a 0.5 m deviation, Interlagos' pit straight)
    zp = np.r_[2 * z[0] - z[pad:0:-1], z, 2 * z[-1] - z[-2:-pad - 2:-1]]
    c = np.cumsum(np.r_[0.0, zp])
    return (c[k:] - c[:-k]) / k


def range_source(w, a, b):
    """Where the bridge range (a, b) of a raceway came from."""
    def hit(rr):
        return any(k == "bridge" and ra < b - 1e-4 and rb > a + 1e-4 for ra, rb, k in rr)
    if hit(w["tag"]):
        return "map bridge/layer tag"
    hook = w["f"].__dict__.get("_bnge_gradesep")
    extra = hook(w["f"]).get(w["i"]) if hook is not None else None
    if extra and hit([r for r in extra if r[2] == "bridge"]):
        return "gradesep (over a short tunnel / grade separation)"
    try:
        lg = roadsmod._lidar_gaps(w["f"], w["i"], [])
        if hit(lg):
            return "lidar elevated stretch"
    except Exception:
        pass
    if hit(w["raw"]):
        return "raw level range (deck gap / skew)"
    return "skew / deck gap / home-cell reach"


APPROACH_M = 200.0      # m of a tunnel road checked out from where it crosses under a track, either way
STEEP_PCT = 18.0        # % over 4 m: an approach steeper than this is flagged when its use is unknown; else its use's
STEEP_TOL_PCT = 2.0     # grade (grading.race_tunnel_use: foot 20 %, cars 15 %, trucks 8 %) + this
WALL_M = 2.0            # m the ground 3 m outside the road stands over it, past the tube, is a wall / pit side


def _arc_at(xy, p):
    """Arc length along polyline xy of its point nearest p."""
    a, d = xy[:-1], np.diff(xy, axis=0)
    ll = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((p - a) * d).sum(1) / ll, 0.0, 1.0)
    dist = np.hypot(*(p - (a + t[:, None] * d)).T)
    g = int(np.argmin(dist))
    s0 = np.r_[0.0, np.cumsum(np.sqrt(ll))]
    return float(s0[g] + t[g] * math.sqrt(ll[g]))


def approach_check(o, sa_o, tube):
    """Flags of a road crossing under a track in a tunnel, from where it crosses out APPROACH_M either way along it:
    no_tube (none drawn), steep (STEEP_PCT over 4 m), wall (the ground beside it, outside the tube, WALL_M over it)
    and pit (the road's end reached while still walled in)."""
    P_, s_ = resample(o["xy"], 1.0)
    flags, steep, wall = [], 0.0, 0.0
    if not tube:
        flags.append("no_tube")
    t_ = np.gradient(P_, axis=0)
    t_ /= np.maximum(np.hypot(*t_.T), 1e-9)[:, None]
    nrm = np.stack([-t_[:, 1], t_[:, 0]], 1)
    hw = (o["spec"].width / 2 if o.get("spec") is not None else 3.0) + 3.0
    tun = [(a * s_[-1], b * s_[-1]) for a, b, k in o["rngs"] if k == "tunnel"]
    for sgn in (-1, 1):
        m = (sgn * (s_ - sa_o) >= 0) & (np.abs(s_ - sa_o) <= APPROACH_M)
        idx = np.flatnonzero(m)
        if sgn < 0:
            idx = idx[::-1]
        if len(idx) < 6:
            continue
        out_tube = np.array([not any(a <= s_[i] <= b for a, b in tun) for i in idx])
        # (and under a drawn tube: its portal carried past the mapped range, roads._clear_portals; the ground there is
        # the tube's roof, not the road)
        for t_i, i in enumerate(idx):
            if out_tube[t_i]:
                # (within the portal face's half width of its line (roads._clear_portals'): a portal carried straight out of a band
                # (roads._clear_portals) while the road curves away from it left the road's line 1-3 m off the tube's
                # inside its mouth, and the portal face read as the approach, Darlington 246 %, 2026-11-07)
                for tn, _c in tunnels:
                    import roadmesh as _rmc
                    tw_ = max(1.0, float(_c.get("w", 0) or 0) / 2 + _rmc.TUNNEL_SHOULDER + _rmc.PORTAL_FRAME)
                    lo_, hi_ = tn[:, :2].min(0) - tw_, tn[:, :2].max(0) + tw_
                    if lo_[0] <= P_[i, 0] <= hi_[0] and lo_[1] <= P_[i, 1] <= hi_[1] and beside_line(P_[i], tn, tw_):
                        out_tube[t_i] = False
                        break
        # (the road's own deck where it is on a bridge: Darlington's infield tunnel road goes on onto a 38 m bridge
        # at its portal, and the ground under the deck read as a 70 % climb, 2026-11-07)
        zc = road_zs(P_[idx], o["fid"])
        zl = ground_zs(P_[idx] + nrm[idx] * hw)
        zr = ground_zs(P_[idx] - nrm[idx] * hw)
        g4 = np.abs(zc[4:] - zc[:-4]) / 4.0 * 100.0
        if os.environ.get("TC_PROFILE"):
            print("profile", o["fid"][-12:], sgn, [(round(float(s_[i] - sa_o)), round(float(z), 2), int(ot),
                                                     latlon(P_[i]) if os.environ.get("TC_PROFILE") == "ll" else 0)
                                                    for i, z, ot in zip(idx[::2], zc[::2], out_tube[::2])])
        if len(g4):
            g4 = g4[out_tube[4:] & out_tube[:-4]]
            steep = max(steep, float(np.nanmax(g4)) if len(g4) else 0.0)
        side = np.fmin(zl, zr) - zc
        side = np.where(out_tube, side, np.nan)
        if np.isfinite(side).any():
            wall = max(wall, float(np.nanmax(side)))
        # at the road's own end (within the checked stretch), still walled in: a pit
        reaches_end = (idx[-1] in (0, len(P_) - 1))
        if reaches_end and np.isfinite(side[-3:]).any() and float(np.nanmin(side[-3:])) > WALL_M:
            flags.append("pit")
    lim = STEEP_PCT
    if o.get("spec") is not None:
        import grading as _gr
        lim = _gr.race_tunnel_use(o["spec"], o.get("osm_width"))[1] * 100.0 + STEEP_TOL_PCT
    if steep > lim:
        flags.append("steep")
    # (a wall beside a continuous ramp is the cut's retaining wall: fine, the user 2026-11-04. A fault only where the
    # driven road itself meets it: its profile steeper than its use allows there, or its end walled in, a pit)
    retaining = wall > WALL_M and "pit" not in flags and "steep" not in flags
    if wall > WALL_M and "pit" not in flags and "steep" in flags:
        flags.append("wall")
    return dict(flags=flags, steep=round(steep, 1), wall=round(wall, 2), lim=round(lim, 1), retaining=retaining)


# ---- per way
rows, details = {}, collections.defaultdict(list)
# road-course bars (Jon driving Spa, 2026-11-07: three spots he felt, ray-cast from the drawn surface: a 4.8 cm
# wheelbase bump at Malmedy, a 0.115 m join at Les Combes, a 10 cm grade plateau over 20 m): each a fault
WB_BAR_M = 0.03          # m: a 4 m chord's middle off the surface
PLATEAU_CHORD_M = 20.0   # m: the chord a grade plateau / dip is read over ...
PLATEAU_BAR_M = 0.05     # m: ... its middle this far off it
EVENT_GAP_M = 10.0       # m: samples over a bar this close are one event


def events(mask, step=1.0):
    """[(start, end)] runs of mask, runs within EVENT_GAP_M joined."""
    idx = np.flatnonzero(mask)
    if not len(idx):
        return []
    out, a, b = [], idx[0], idx[0]
    for i in idx[1:]:
        if (i - b) * step <= EVENT_GAP_M:
            b = i
        else:
            out.append((a, b))
            a = b = i
    out.append((a, b))
    return out
# FIA Appendix O 7.4 (vertical radii adequate for the cars) and its splitter / cross-slope rules, per lane: the
# local vertical radius R over a car-length window and the vertical acceleration v^2 / R a car feels at the speed the
# corner allows there (v = sqrt(mu g r) of the horizontal radius r, at most FIA_V_ROAD / FIA_V_OVAL): a crest over
# FIA_CREST_G lifts it light, a sag over FIA_SAG_G bottoms it; a step >= FIA_STEP_M within 0.5 m hits a low splitter;
# straights' cross slope FIA_XS_LO..FIA_XS_HI (drainage), banking at most FIA_BANK_MAX but on a speedway, changing at
# most FIA_XS_RATE per 10 m. Ranked by the worst g and the worst step.
FIA_CAR_M = 5.0              # m: the window a car's wheelbase and overhangs ride over
FIA_V_ROAD, FIA_V_OVAL = 60.0, 80.0     # m/s where no corner limits the speed
FIA_MU = 1.6                 # a racing tyre's grip with downforce at corner speed (v = sqrt(mu g r))
FIA_CREST_G, FIA_SAG_G = 0.5, 1.0
FIA_STEP_M = 0.03
FIA_XS_LO, FIA_XS_HI, FIA_BANK_MAX, FIA_XS_RATE = 0.015, 0.03, 0.10, 0.02
FIA_STRAIGHT_R = 1000.0      # m: a horizontal radius over this is a straight


def fia_measure(fid, w, sp, P, Ps, nrm, lanes):
    """FIA_* above on one way's lanes (LANES across sp.width, 1 m along Ps): events in details["fia_g"] (fid, g,
    crest / sag, lat/lon, v m/s, R m, name), details["fia_step"] (fid, cm, lat/lon, name), details["fia_xs"]
    (fid, bank / rate, %, lat/lon, name) and details["fia_xs_len"] (fid, straight m, of it off the drainage range)."""
    n = lanes.shape[1]
    hw = int(round(FIA_CAR_M / 2))
    if n < 2 * hw + 3:
        return
    oval = getattr(sp, "race", None) == "oval"
    vmax = FIA_V_OVAL if oval else FIA_V_ROAD
    hk = 10                  # (the horizontal radius over 20 m: the corner's, not a vertex's)
    tn = np.gradient(Ps, axis=0)
    ang = np.unwrap(np.arctan2(tn[:, 1], tn[:, 0]))
    dang = np.full(n, 1e-9)
    if n > 2 * hk:
        dang[hk:-hk] = np.abs(ang[2 * hk:] - ang[:-2 * hk]) / (2.0 * hk)
    r_h = 1.0 / np.maximum(dang, 1e-6)
    v = np.minimum(vmax, np.sqrt(FIA_MU * 9.81 * r_h))
    # (vertical curvature per lane over the window: (z[i-h] - 2 z[i] + z[i+h]) / h^2; + a sag, - a crest)
    kap = (lanes[:, :-2 * hw] - 2.0 * lanes[:, hw:-hw] + lanes[:, 2 * hw:]) / float(hw * hw)
    kap = np.where(np.isfinite(kap), kap, 0.0)
    vv = v[hw:-hw] ** 2
    for sign, bar, kind in ((-1.0, FIA_CREST_G, "crest"), (1.0, FIA_SAG_G, "sag")):
        gk = (sign * kap).max(0) * vv / 9.81
        for a_, b_ in events(gk > bar):
            j_ = a_ + int(np.argmax(gk[a_:b_ + 1]))
            kk = float((sign * kap[:, j_]).max())
            details["fia_g"].append((fid, round(float(gk[j_]), 2), kind, latlon(P[min(j_ + hw, len(P) - 1)]),
                                     round(float(v[j_ + hw]), 1), int(round(1.0 / max(kk, 1e-9))), w["name"]))
    # (splitter steps: the surface every 0.5 m along three lanes, a step against its neighbours' grade)
    S, _ = resample(Ps, 0.5)
    if len(S) > 6:
        tS = np.gradient(S, axis=0)
        tS /= np.maximum(np.hypot(*tS.T), 1e-9)[:, None]
        nS = np.stack([-tS[:, 1], tS[:, 0]], 1)
        worst = np.zeros(len(S) - 3)
        for f_ in (-0.3, 0.0, 0.3):
            zz, _ = surface_z(S + nS * (f_ * sp.width), fid)
            d1 = np.diff(zz)
            st = np.abs(d1[1:-1] - 0.5 * (d1[:-2] + d1[2:]))
            worst = np.maximum(worst, np.where(np.isfinite(st), st, 0.0))
        for a_, b_ in events(worst >= FIA_STEP_M, 0.5):
            j_ = a_ + int(np.argmax(worst[a_:b_ + 1]))
            details["fia_step"].append((fid, round(float(worst[j_]) * 100, 1), latlon(S[j_ + 1]), w["name"]))
    # (cross slope edge lane to edge lane, banking, and how fast it changes)
    xs = (lanes[-1] - lanes[0]) / ((LANES[-1] - LANES[0]) * sp.width)
    if np.isfinite(xs).all():
        straight_ = r_h > FIA_STRAIGHT_R
        off = straight_ & ((np.abs(xs) < FIA_XS_LO) | (np.abs(xs) > FIA_XS_HI))
        details["fia_xs_len"].append((fid, int(straight_.sum()), int(off.sum())))
        if not oval:
            for a_, b_ in events(np.abs(xs) > FIA_BANK_MAX):
                j_ = a_ + int(np.argmax(np.abs(xs[a_:b_ + 1])))
                details["fia_xs"].append((fid, "bank", round(float(abs(xs[j_])) * 100, 1), latlon(P[j_]), w["name"]))
        if n > 12:
            rate = np.abs(xs[10:] - xs[:-10])
            for a_, b_ in events(rate > FIA_XS_RATE):
                j_ = a_ + int(np.argmax(rate[a_:b_ + 1]))
                details["fia_xs"].append((fid, "rate", round(float(rate[j_]) * 100, 1),
                                          latlon(P[min(j_ + 5, len(P) - 1)]), w["name"]))


LANE_DUMP = {}
for fid, w in ways.items():
    sp = w["spec"]
    # (along the map's line with its spikes put back on it, as grading reads the surface: a vertex 1.3 m off the line
    # swung the lanes laid across it back and forth along the track, Road America, 2026-11-05)
    import grading as _gsp
    P, s = resample(_gsp._race_eval_line(w["xy"]) if hasattr(_gsp, "_race_eval_line") else w["xy"], 1.0)
    if len(P) < 5:
        continue
    z, on = surface_z(P, fid)
    ok = np.isfinite(z)
    if ok.sum() < 5:
        continue
    P, s, z, on = P[ok], s[ok], z[ok], on[ok]
    sm = running_mean(z, SMOOTH_M)
    dev = z - sm
    k = int(GRADE_M)
    g = (z[k:] - z[:-k]) / GRADE_M if len(z) > k else np.zeros(1)
    dg = np.abs(g[k:] - g[:-k]) * 100.0 if len(g) > k else np.zeros(1)
    d2 = np.abs(z[2:] - 2 * z[1:-1] + z[:-2]) if len(z) > 2 else np.zeros(1)
    # cross slope: the surface at the carriageway's two edges (0.4 of the width out)
    t = np.gradient(P, axis=0)
    t /= np.maximum(np.hypot(*t.T), 1e-9)[:, None]
    nrm = np.stack([-t[:, 1], t[:, 0]], 1)
    hw = 0.4 * (sp.width if sp is not None else 12.0)
    zl, _ = surface_z(P + nrm * hw, fid)
    zr, _ = surface_z(P - nrm * hw, fid)
    xs = (zl - zr) / (2 * hw) * 100.0
    dxs = np.abs(xs[k:] - xs[:-k]) if len(xs) > k else np.zeros(1)
    dxs = dxs[np.isfinite(dxs)] if np.isfinite(dxs).any() else np.zeros(1)
    r = dict(fid=fid, name=w["name"], L=float(s[-1]), rms=float(np.sqrt(np.mean(dev ** 2))),
             maxdev=float(np.abs(dev).max()), maxdg=float(dg.max()), bumps=int((d2 > BUMP_M).sum()),
             maxd2=float(d2.max()), maxdxs=float(dxs.max()), width=sp.width if sp is not None else None,
             osm_width=w["osm_width"], decks=[], under=[], blds=[], gaps=[], extras=collections.Counter())
    j = int(np.argmax(np.abs(dev)))
    details["rough"].append((round(float(np.abs(dev[j])), 2), latlon(P[j]), fid[-9:], w["name"]))
    details["profile"].append((float(np.abs(dev[j])), fid[-9:], int(j), len(z),
                               np.round(z[max(0, j - 30):j + 31:3] - z[j], 2).tolist(),
                               np.round(ground_zs(P[max(0, j - 30):j + 31:3]) - z[j], 2).tolist()))
    j = int(np.argmax(dg)) + k
    details["grade"].append((round(float(dg.max()), 1), latlon(P[min(j, len(P) - 1)]), fid[-9:], w["name"]))
    # wheelbase: a 4 m chord's midpoint off the surface under it (|z(s) - (z(s-2) + z(s+2)) / 2|, the 1 m samples'
    # nearest to a car's ~3 m wheelbase): the bump or dip its two axles straddle
    if len(z) > 4:
        wb = np.abs(z[2:-2] - 0.5 * (z[:-4] + z[4:]))
        jw = int(np.argmax(wb))
        details["wheelbase"].append((round(float(wb[jw]) * 100, 1), latlon(P[jw + 2]), fid[-9:], w["name"]))
        for a_, b_ in events(wb >= WB_BAR_M):
            j_ = a_ + int(np.argmax(wb[a_:b_ + 1]))
            details["bump_ev"].append((fid, round(float(wb[j_]) * 100, 1), latlon(P[j_ + 2]), w["name"]))
    if len(dxs) and np.isfinite(xs).any():
        j = int(np.nanargmax(np.abs(xs[k:] - xs[:-k]))) if len(xs) > k else 0
        details["tilt"].append((round(float(dxs.max()), 1), latlon(P[j]), fid[-9:], w["name"]))
    # decks on it
    L = r["L"]
    for a, b, kind in w["rngs"]:
        if kind != "bridge":
            continue
        src = range_source(w, a, b)
        mine = [(n, dw, c) for n, dw, c in race_decks if c.get("fid") == fid]
        steps = []
        for sa in (a * L, b * L):
            q = P[min(int(sa), len(P) - 1)]
            gz = ground_z(q)
            dzs = [near_line(q, n)[1] for n, dw, c in mine if near_line(q, n)[0] < 1.5]
            if dzs and np.isfinite(gz):
                steps.append(round(min(abs(d - gz) for d in dzs), 2))
        caps = [c.get("caps") for n, dw, c in mine]
        r["decks"].append(dict(src=src, len=round((b - a) * L, 1), steps=steps, chunks=len(mine),
                               at=latlon(P[min(int((a + b) / 2 * L), len(P) - 1)])))
        details["deck"].append((max(steps) if steps else None, round((b - a) * L, 1), src,
                                latlon(P[min(int((a + b) / 2 * L), len(P) - 1)]), fid[-9:], caps[:1]))
    # what crosses under it
    for o in others:
        under = any(k2 == "tunnel" for _, _, k2 in o["rngs"]) or o.get("culvert")
        cr = seg_cross(w["xy"], o["xy"])
        Lo = float(np.hypot(*np.diff(o["xy"], axis=0).T).sum()) if under and not o.get("culvert") else 0.0
        for p, sa in cr:
            ondeck = any(a * L <= sa <= b * L for a, b, kk in w["rngs"] if kk == "bridge")
            if Lo > 0 and not ondeck:
                # (under only where its tunnel range is: a road with a tunnel elsewhere crossing the track at grade, or
                # one whose tunnel starts at the track (joined there), is a level crossing / junction, not a missing tube:
                # Michigan's infield service roads, 2026-11-03)
                so = _arc_at(o["xy"], p)
                if so < 1.5 or so > Lo - 1.5 or not any(a * Lo - 2.0 <= so <= b * Lo + 2.0
                                                        for a, b, k2 in o["rngs"] if k2 == "tunnel"):
                    continue
            if not (under or ondeck or o["sub"] == "water"):
                continue                    # (a level crossing with a road: a junction, not under)
            top, _ = surface_z(p[None], fid)
            top = float(top[0])
            # the floor under: the terrain at the crossing (an underpass made ground is dug there), or a tunnel chunk's
            floor = ground_z(p)
            tube = False
            for n, c in tunnels:
                d, zz, _ = near_line(p, n)
                if d < 2.0:
                    floor = min(floor, zz) if np.isfinite(floor) else zz
                    tube = True
            # the track's own ground beside the crossing (its undisturbed height, 15 m along either way)
            q = [P[max(0, min(len(P) - 1, int(sa + dd)))] for dd in (-15, 15)]
            side = np.nanmean([ground_z(x) for x in q])
            # (nothing drawn for it: no deck over it, no tube under: the line under is simply left out)
            clear = top - (DECK_DEPTH if ondeck else 0.0) - floor if (ondeck or tube) else float("nan")
            it = dict(what=f"{o['cls']}{' culvert' if o.get('culvert') else ''}{' tunnel' if under and not o.get('culvert') else ''}",
                      ofid=o["fid"][-12:], deck=ondeck, top=round(top, 2), floor=round(float(floor), 2),
                      clear=round(float(clear), 2), trench=round(float(side - floor), 2), at=latlon(p))
            if under and not o.get("culvert") and not ondeck:
                it.update(approach_check(o, sa_o=_arc_at(o["xy"], p), tube=tube))
            r["under"].append(it)
            details["under"].append((it["clear"], it["trench"], it["what"], it["deck"], it["at"], fid[-9:]))
            if it.get("flags"):
                details["tunnel"].append((it["flags"], it.get("steep"), it.get("wall"), it["at"], o["fid"][-12:]))
    # extras: markings / curb lines along it, sidewalks along it
    c0 = w["xy"]
    lo, hi = c0.min(0) - 20, c0.max(0) + 20
    for n, c in lines:
        mid = n[len(n) // 2, :2]
        if not (lo[0] <= mid[0] <= hi[0] and lo[1] <= mid[1] <= hi[1]):
            continue
        d, _, _ = near_line(np.r_[mid, 0.0], np.c_[c0, np.zeros(len(c0))])
        if d <= (sp.width / 2 + 1.5 if sp is not None else 7.5):
            r["extras"]["edge/curb line" if c.get("layer") == "edge" else str(c.get("m"))] += 1
    for wk in walks:
        pts = np.asarray(wk.get("pts"), np.float64)
        if pts.ndim != 2 or len(pts) < 2:
            continue
        if str(wk.get("fid")) == fid:
            r["extras"]["sidewalk run"] += 1
    # at speed (the owner 2026-11-05: superspeedways "smooth is not super speedway smooth"): lanes across the band,
    # the drawn ground (the physics terrain) along each: the 10 m chord's middle off the surface (mm; heave a car at
    # SPEED_MS feels as SPEED_MS^2 * 8 dev / 10^2 vertical), and the bank between neighbouring lanes against its 60 m
    # running mean (bank jitter, %)
    if sp is not None and r["L"] > 200:
        # (along the line a car drives: the plan's own, grading.race_line of the map's centre line; the map's chords
        # put a lane 0.3 m in and out across the banked plane every chord, a wave of the measuring line, not the surface)
        import grading as _g0
        Ps = _g0.race_line(P, 1.0) if _g0.RACE_LINE_SIGMA_M > 0 else P
        # (and a 4 m Gaussian over it whatever the bend: a spike in the map's line (Road America, 1.2 m off in 4 m)
        # kept by race_line's bend rule folded the lanes laid across it back on themselves; a car's line has no spike.
        # A 30 m hairpin's line moves 0.27 m)
        if len(Ps) > 25:
            _k = np.exp(-0.5 * (np.arange(-12, 13) / 4.0) ** 2)
            _k /= _k.sum()
            _pad = np.r_[2 * Ps[:1] - Ps[12:0:-1], Ps, 2 * Ps[-1:] - Ps[-2:-14:-1]]
            Ps = np.stack([np.convolve(_pad[:, 0], _k, "valid"), np.convolve(_pad[:, 1], _k, "valid")], 1)
        # (across the line driven: the map's line's normals swung 30 deg at a spiked vertex, Road America, and the
        # lanes laid on them ran 1-2 m unevenly along the surface, read as a 0.3 m chord)
        tn_ = np.gradient(Ps, axis=0)
        tn_ /= np.maximum(np.hypot(*tn_.T), 1e-9)[:, None]
        nrm = np.stack([-tn_[:, 1], tn_[:, 0]], 1)
        lanes = []
        for f_ in LANES:
            zz, _ = surface_z(Ps + nrm * (f_ * sp.width), fid)
            lanes.append(zz)
        lanes = np.array(lanes)
        LANE_DUMP[fid] = np.r_[Ps.T, nrm.T, lanes]
        # grade plateau / dip: each lane's 20 m chord, its middle off the surface (a crest or a sag a car's
        # suspension feels as a float or a compression across the whole width)
        hc = int(PLATEAU_CHORD_M / 2)
        if lanes.shape[1] > 2 * hc + 1:
            dvp = np.abs(lanes[:, hc:-hc] - 0.5 * (lanes[:, :-2 * hc] + lanes[:, 2 * hc:]))
            dvp = np.where(np.isfinite(dvp), dvp, 0.0).max(0)
            for a_, b_ in events(dvp >= PLATEAU_BAR_M):
                j_ = a_ + int(np.argmax(dvp[a_:b_ + 1]))
                details["plateau_ev"].append((fid, round(float(dvp[j_]) * 100, 1), latlon(P[min(j_ + hc, len(P) - 1)]), w["name"]))
        fia_measure(fid, w, sp, P, Ps, nrm, lanes)
        ch = int(CHORD_M / 2)
        worst_c, worst_b, at_c, at_b = 0.0, 0.0, None, None
        for li, zz in enumerate(lanes):
            if len(zz) > 2 * ch + 1:
                dv = np.abs(zz[ch:-ch] - 0.5 * (zz[:-2 * ch] + zz[2 * ch:]))
                if np.isfinite(dv).any():
                    j = int(np.nanargmax(dv))
                    if dv[j] > worst_c:
                        worst_c, at_c = float(dv[j]), (latlon(P[j + ch] + nrm[j + ch] * LANES[li] * sp.width), LANES[li])
                    r["chord_over"] = r.get("chord_over", 0) + int((dv > CHORD_BAR_M).sum())
        dl = (LANES[1] - LANES[0]) * sp.width
        for li in range(len(LANES) - 1):
            bk = (lanes[li + 1] - lanes[li]) / dl
            if np.isfinite(bk).all() and len(bk) > 61:
                jit = np.abs(bk - running_mean(bk, 61))
                j = int(np.argmax(jit))
                if jit[j] > worst_b:
                    worst_b, at_b = float(jit[j]), latlon(P[j])
        r["chord_mm"] = round(worst_c * 1000, 1)
        r["accel_g"] = round(SPEED_MS ** 2 * 8 * worst_c / CHORD_M ** 2 / 9.81, 2)
        r["bankjit"] = round(worst_b * 100, 2)
        details["speed"].append((r["chord_mm"], r["accel_g"], r["bankjit"], r.get("chord_over", 0), at_c, at_b,
                                 fid[-9:], w["name"]))
    rows[fid] = r

# holes under the band (the owner 2026-11-05: no terrain hole inside a track's band, ever): every tube drawn under a
# raceway's carriageway + shoulder (grading.RACE_SHOULDER_M), the 1 m lattice samples over its footprint
# (roads.tunnel_hole_mask's: width / 2 + TUNNEL_SHOULDER + TUNNEL_HOLE_MARGIN) inside the band, and the ground there over
# the tube's floor: under the vault + TUNNEL_HOLE_MARGIN the block cuts a hole (a hole cell in the band: a fault)
import grading as _grm
import roadmesh as _rmm
HOLE_UP = _rmm.TUNNEL_H + roadsmod.TUNNEL_HOLE_MARGIN
bands = [(w["xy"], (w["spec"].width if w["spec"] is not None else 12.0) / 2 + _grm.RACE_SHOULDER_M + _grm.RACE_GRID_M)
         for w in ways.values()]


def in_band(Q):
    m = np.zeros(len(Q), bool)
    for xy, hb in bands:
        lo, hi = xy.min(0) - hb, xy.max(0) + hb
        k = np.flatnonzero(((Q >= lo) & (Q <= hi)).all(1) & ~m)
        if not len(k):
            continue
        a, d = xy[:-1], np.diff(xy, axis=0)
        ll = np.maximum((d * d).sum(1), 1e-12)
        for g in range(len(a)):
            t = np.clip(((Q[k] - a[g]) @ d[g]) / ll[g], 0.0, 1.0)
            m[k[np.hypot(*(Q[k] - a[g] - t[:, None] * d[g]).T) <= hb]] = True
    return m


for tn, c in tunnels:
    xy_t = tn[:, :2]
    if len(xy_t) < 2:
        continue
    hw = float(c.get("w", 6.0) or 6.0) / 2 + _rmm.TUNNEL_SHOULDER + roadsmod.TUNNEL_HOLE_MARGIN
    Pt, st_ = resample(xy_t, 1.0)
    s0 = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy_t, axis=0).T))]
    zt = np.interp(st_, s0, tn[:, 2])
    tg = np.gradient(Pt, axis=0)
    tg /= np.maximum(np.hypot(*tg.T), 1e-9)[:, None]
    nm = np.stack([-tg[:, 1], tg[:, 0]], 1)
    offs = np.arange(-hw, hw + 1e-9, 1.0)
    Q = (Pt[:, None, :] + offs[None, :, None] * nm[:, None, :]).reshape(-1, 2)
    Q = np.round(Q)                         # (the lattice's samples)
    Z = np.repeat(zt, len(offs))
    m = in_band(Q)
    if not m.any():
        continue
    cover = ground_zs(Q[m]) - Z[m]
    # (FAULT: the vault through the band's ground; under HOLE_UP the block would cut a hole, roads.spare_carriageways
    # keeps the band's squares: counted as "spared")
    holes = int(np.sum(cover < _rmm.TUNNEL_H))
    spared = int(np.sum(cover < HOLE_UP))
    j = int(np.nanargmin(cover)) if np.isfinite(cover).any() else 0
    details["band_holes"].append((holes, round(float(np.nanmin(cover)), 2), spared, round(HOLE_UP, 2),
                                  latlon(Q[m][j]), str(c.get("fid", ""))[-12:]))

# gaps: a way's end not on another raceway's end or line
ends = [(fid, w["xy"][0]) for fid, w in ways.items()] + [(fid, w["xy"][-1]) for fid, w in ways.items()]
for fid, p in ends:
    best = np.inf
    for fid2, w2 in ways.items():
        if fid2 == fid:
            continue
        d, _, _ = near_line(np.r_[p, 0.0], np.c_[w2["xy"], np.zeros(len(w2["xy"]))])
        best = min(best, d)
    if GAP_MIN <= best <= GAP_MAX and fid in rows:
        rows[fid]["gaps"].append(round(best, 2))
        details["gap"].append((round(best, 2), latlon(p), fid[-9:]))

# buildings the carriageways run through
from polygon import Path
for b in blds:
    ring = enu(b["ll"])
    bb0, bb1 = ring.min(0), ring.max(0)
    for fid, w in ways.items():
        if fid not in rows:
            continue
        hw = (w["spec"].width if w["spec"] is not None else 12.0) / 2
        if (w["xy"].min(0) > bb1 + hw).any() or (w["xy"].max(0) < bb0 - hw).any():
            continue
        P, _ = resample(w["xy"], 1.0)
        t = np.gradient(P, axis=0)
        t /= np.maximum(np.hypot(*t.T), 1e-9)[:, None]
        nrm = np.stack([-t[:, 1], t[:, 0]], 1)
        pth = Path(ring)
        ins = np.zeros(len(P), bool)
        for off in (-0.9 * hw, 0.0, 0.9 * hw):
            ins |= np.asarray(pth.contains_points(P + nrm * off), bool)
        m = int(ins.sum())
        if m:
            it = dict(bfid=b["fid"][:12], layer=b["layer"], cls=b["cls"], src=b["src"], inside=m,
                      h=None if not np.isfinite(b["h"]) else round(b["h"], 1),
                      mh=None if not np.isfinite(b["mh"]) else round(b["mh"], 1), at=latlon(P[np.flatnonzero(ins)[0]]))
            rows[fid]["blds"].append(it)
            details["bld"].append((m, it["layer"], it["cls"], it["src"], it["h"], it["mh"], it["at"], fid[-9:]))

# buildings as the tiles build them: a solid one (bottom under SOLID_UNDER_M) the carriageway runs through, or lifted
SOLID_UNDER_M = 4.5
built_on = collections.Counter()
seen_b = set()
for ring, mh, cls in built_blds:
    kb = (round(float(ring[0, 0]), 1), round(float(ring[0, 1]), 1), len(ring), round(mh, 1))
    if kb in seen_b:
        continue
    seen_b.add(kb)
    bb0, bb1 = ring.min(0), ring.max(0)
    pth = Path(ring)
    for fid, w in ways.items():
        if fid not in rows:
            continue
        hw = (w["spec"].width if w["spec"] is not None else 12.0) / 2
        if (w["xy"].min(0) > bb1 + hw).any() or (w["xy"].max(0) < bb0 - hw).any():
            continue
        P, _ = resample(w["xy"], 1.0)
        t = np.gradient(P, axis=0)
        t /= np.maximum(np.hypot(*t.T), 1e-9)[:, None]
        nrm = np.stack([-t[:, 1], t[:, 0]], 1)
        ins = np.zeros(len(P), bool)
        for off in (-0.9 * hw, 0.0, 0.9 * hw):
            ins |= np.asarray(pth.contains_points(P + nrm * off), bool)
        m = int(ins.sum())
        if m:
            kind = "solid" if mh < SOLID_UNDER_M else "lifted"
            rows[fid].setdefault("built", collections.Counter())[kind] += 1
            details["built"].append((kind, m, round(mh, 1), cls, latlon(P[np.flatnonzero(ins)[0]]), fid[-9:]))

# ---- rules on what the server streams (trackrules.py; 2026-11-06): joins, stands, props, buildings, holes, walls
import trackrules as TR
import racefurniture as RF
RL = [w["xy"] for w in ways.values()]
RH = [(w["spec"].width if w["spec"] is not None else RF.DEFAULT_RACE_W) / 2 for w in ways.values()]
CLEAR = RF.CLEAR_M                      # m: the shoulder nothing stands on (racefurniture, grading.RACE_SHOULDER_M)
JOIN_STEP_M = STEP_M                    # m: a 1 m step a tyre feels as an edge (the deck ends' open-edge bar)
TWIST_M = BUMP_M                        # m: one edge lifted against the other across a join (a wheel unloaded)
flags = collections.Counter()
rules = collections.defaultdict(list)
# the racing line's pieces (trackrules.main_pieces: each network's longest cycle, else longest path) keep the shoulder
# clear; a side piece (pit lane, paddock road, link) its carriageway only: garages and the pit wall front it
MAIN = TR.main_pieces({f: w["xy"] for f, w in ways.items()}, tol=GAP_MAX, beside_m=RF.GRANDSTAND_REACH_M,
                      pits=[f for f, w in ways.items() if roadsmod.pit_lane(w["f"], w["i"])])   # (ends within a plan gap join)
RHC = [h + (CLEAR if MAIN[f] else 0.0) for f, h in zip(ways, RH)]      # the band each piece keeps clear
# (for buildings: a piece without a mapped width is drawn at its type's default (12, 18, 20 m), a guess; a building
# beside it is a fault only within the narrowest raceway the server draws (roads.PIT_LANE_M) + its shoulder, else the
# guessed width is the suspect: "width", info)
HAS_W = [_num(w["osm_width"]) is not None and _num(w["osm_width"]) > 0 for w in ways.values()]
RHB = [(h if hw else min(h, roadsmod.PIT_LANE_M / 2)) + (CLEAR if MAIN[f] else 0.0)
       for f, h, hw in zip(ways, RH, HAS_W)]
rules["main"] = [sum(MAIN.values()), len(MAIN)]
if os.environ.get("TC_WAYS"):
    for f, w in ways.items():
        print("way", f[-9:], MAIN[f], repr(w["name"]), round(float(np.hypot(*np.diff(w["xy"], axis=0).T).sum())),
              latlon(w["xy"][0]), latlon(w["xy"][-1]), w["spec"].width if w["spec"] else None)
# road-course bars on the racing line: wheelbase bumps, grade plateaus / dips
for k_, fl_ in (("bump_ev", "bump"), ("plateau_ev", "plateau")):
    for fid_, v_, at_, nm_ in details[k_]:
        if MAIN.get(fid_, True):
            flags[fl_] += 1
            rules[fl_].append((v_, at_, fid_[-9:], nm_))
# joins and seams
for fid, p, t, kind in TR.joins({f: w["xy"] for f, w in ways.items()}):
    h = (ways[fid]["spec"].width if ways[fid]["spec"] is not None else RF.DEFAULT_RACE_W) / 2
    stp, tw = TR.join_profile(lambda Q: surface_z(Q)[0], p, t, h)
    if os.environ.get("TC_JOIN") and fid.endswith(tuple(os.environ["TC_JOIN"].split(","))):
        tt = np.asarray(t, float) / np.hypot(*t)
        nn = np.array([-tt[1], tt[0]])
        for f_ in (-0.8, 0.0, 0.8):
            ss = np.arange(-6, 6.01, 0.5)
            Q = np.asarray(p)[None] + ss[:, None] * tt[None] + f_ * h * nn[None]
            zq, onq = surface_z(Q)
            print("joinprof", fid[-9:], kind, f_, np.round(zq - zq[12], 2).tolist(), onq.astype(int).tolist())
    if stp >= JOIN_STEP_M or tw > TWIST_M:
        flags["seam" if kind == "seam" else "join"] += 1
    rules["join"].append((kind, stp, tw, latlon(p), fid[-9:], ways[fid]["name"]))
# grandstands as streamed
stand_rows_ = [r for r in streamed_props if r[0] == RF.G_STAND]
fps = []
for b in blds:
    c = b["cls"].strip().lower()
    ring = enu(b["ll"])
    if c in RF.STAND_CLASSES or (c in ("", "yes", "none") and TR.ring_area(ring) >= RF.STAND_AREA_M2):
        if float(TR.edge_dist(ring, RL, RH)[0].min()) <= RF.GRANDSTAND_REACH_M:
            fps.append(ring)
built_rings = [r for r, _mh, _c in built_blds]
st_res, cover = ([], []) if not getattr(RF, "STANDS_ON", True) else TR.stand_check([(float(r[1]), float(r[2]), float(r[4]), float(r[6]) / RF.STAND_H) for r in stand_rows_],
                               RL, RH, fps, RF.STAND_LEN, RF.STAND_DEPTH, CLEAR - 0.5, RF.STAND_PIECE_MIN)
for s_ in st_res:
    for fl in s_["faults"]:
        flags["stand_" + fl] += 1
    rules["stand"].append((s_["faults"], s_["faces"], s_["gap"], s_["on_fp"], latlon([s_["x"], s_["y"]]), s_["yaw"]))
for r_, cv in zip(fps if getattr(RF, "STANDS_ON", True) else [], cover):
    # (a footprint left a building, not seats: buildings.py draws it, nothing missing)
    kept_bld = any(TR.inside(r_, br.mean(0)[None])[0] for br in built_rings)
    if cv < 0.4 and not kept_bld:
        flags["stand_cover"] += 1
    rules["cover"].append((round(cv, 2), kept_bld, round(TR.ring_area(r_)), latlon(r_.mean(0))))
# props (every Forest row but the seats and barriers placed along the edge by design) and vegetation on the band
EDGE_GROUPS = {RF.G_STAND}
for g_, x_, y_, d_ in TR.props_on_band(streamed_props, RL, RHB, 0.0, EDGE_GROUPS):
    flags["prop"] += 1
    rules["prop"].append((g_, d_, latlon([x_, y_])))
try:
    g_anchor, _ = st.anchor("p", LAT, LON)
except Exception:
    g_anchor = None
try:
    import vegetation as _veg
    import geotiles as _gt
    v_ = _veg.Vegetation(st.md, st.cache_dir if hasattr(st, "cache_dir") else os.path.expanduser("~/.cache/beamng-earth"))
    v_.dem = getattr(st, "dem", None)
    v_.surface, v_.widths = getattr(st, "surface", None), st.widths
    pts = np.concatenate([resample(x, 50.0)[0] for x in RL])
    la_, lo_, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
    gx_, gy_ = _gt.tile_of(np.asarray(la_), np.asarray(lo_))
    # (the rows the game gets: vegetation.geo_tile_items, as blockworker.veg_geo streams them, after its own clearing
    # of the drawn roads and the graded ground (_off_graded); the cell's raw list counted thousands on Monza's band)
    def _raw(la, lo):
        return bw.ground(None, la, lo, st.dem.sample(la, lo, 15))

    def _gelev(la, lo):
        z_ = _raw(la, lo)
        return g_anchor.apply(la, lo, z_, _grm.ROAD_LIFT) if g_anchor is not None else z_
    vrows = []
    for gk in sorted(set(zip(np.atleast_1d(gx_).tolist(), np.atleast_1d(gy_).tolist()))):
        got_ = _veg.geo_tile_items(gk[0], gk[1], v_, lambda k: True, _gelev, getattr(st, "sea", None),
                                   as_json=False, raw=_raw if g_anchor is not None else None)
        if not got_ or not got_[0] or not isinstance(got_[0], list):
            continue
        gf_ = _gt.frame(*gk)
        a_ = np.array([[r[1], r[2]] for r in got_[0]], np.float64)
        la2, lo2, _ = gf_.enu_to_geodetic(a_[:, 0], a_[:, 1], np.zeros(len(a_)))
        e_, n_, _ = frame.geodetic_to_enu(np.asarray(la2), np.asarray(lo2), np.zeros(len(a_)))
        vrows += [["veg%d" % int(r[0]), float(a), float(b)] for r, a, b in zip(got_[0], np.atleast_1d(e_), np.atleast_1d(n_))]
    seen_v = set()
    for g_, x_, y_, d_ in TR.props_on_band(vrows, RL, RHB, 0.0):
        k_ = (round(x_, 1), round(y_, 1))
        if k_ not in seen_v:
            seen_v.add(k_)
            flags["prop"] += 1
            rules["prop"].append((g_, d_, latlon([x_, y_])))
    rules["veg_checked"] = [len(vrows)]
except Exception as e:
    rules["veg_checked"] = [f"failed: {type(e).__name__}: {e}"]
# buildings as built: on the band, or round the race line (a lid over the oval)
seen_b2 = set()
for ring, mh, cls in built_blds:
    kb = (round(float(ring[0, 0]), 1), round(float(ring[0, 1]), 1), len(ring))
    if kb in seen_b2:
        continue
    seen_b2.add(kb)
    on_, under_ = TR.building_on_band(ring, RL, RHB, 0.0)
    if not (on_ or under_):
        on2_, _u2 = TR.building_on_band(ring, RL, RHC, 0.0)
        if on2_:
            rules["width"].append((on2_, round(TR.ring_area(ring)), latlon(ring.mean(0))))
        continue
    if on_ or under_:
        flags["building"] += 1
        dd_ = [float(TR.edge_dist(TR.densify(np.vstack([ring, ring[:1]]), 1.0), [x], [h])[0].min()) for x, h in zip(RL, RH)]
        kw_ = int(np.argmin(dd_))
        w_ = list(ways.values())[kw_]
        rules["building"].append((on_, under_, round(mh, 1), cls, round(TR.ring_area(ring)), latlon(ring.mean(0)),
                                  round(dd_[kw_], 1), w_["fid"][-9:], w_["name"], w_["spec"].width if w_["spec"] else None,
                                  "main" if MAIN[w_["fid"]] else "side"))
# tunnels: the terrain block's hole mask (blockworker._tunnel_holes, the server's own) over each tube near the band;
# and the tube's vault against grading.race_tube_depth under the band
DEPTH = _grm.race_tube_depth()
import seatiles as _seat


def _elev(la, lo):
    z_ = bw.ground(frame, la, lo, st.dem.sample(la, lo, 15))
    return g_anchor.apply(la, lo, z_, _grm.ROAD_LIFT) if g_anchor is not None else z_


for tn, c in tunnels:
    xy_t = tn[:, :2]
    if len(xy_t) < 2:
        continue
    near_ = TR.edge_dist(xy_t, RL, RH)[0]
    if near_.min() > CLEAR + 20:
        continue
    # vault: the tube's floor under the band, cover = surface - floor
    Pt = TR.densify(xy_t, 1.0)
    s0 = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy_t, axis=0).T))]
    st_ = np.r_[0.0, np.cumsum(np.hypot(*np.diff(Pt, axis=0).T))]
    zt = np.interp(st_, s0, tn[:, 2])
    ib = TR.edge_dist(Pt, RL, RHC)[0] < 0.0          # (the racing line's band, a side piece's carriageway)
    if ib.any():
        surf_ = surface_z(Pt[ib])[0]
        cov_ = surf_ - zt[ib]
        # (the deepest the server may dig: its floor kept SEA_E + UNDER_SEA_M over the sea, or the sea fills the tube;
        # Monaco, Zandvoort, Homestead, Daytona: a cover that limit sets is reported, not a fault)
        need_ = np.minimum(DEPTH, surf_ - (_seat.SEA_E + _grm.UNDER_SEA_M))
        short_ = cov_ - need_
        if np.isfinite(short_).any() and np.nanmin(short_) < -0.05:
            jv = int(np.nanargmin(short_))
            flags["vault"] += 1
            rules["vault"].append((round(float(cov_[jv]), 2), round(float(need_[jv]), 2), latlon(Pt[ib][jv]),
                                   str(c.get("fid", ""))[-12:]))
        elif np.isfinite(cov_).any() and np.nanmin(cov_) < DEPTH - 0.05:
            rules["vault_sea"].append((round(float(np.nanmin(cov_)), 2), latlon(Pt[ib][int(np.nanargmin(cov_))])))
    # the holes the block would cut, on a 1 m lattice round the tube's stretch near the band
    cen_ = Pt[ib].mean(0) if ib.any() else xy_t[int(np.argmin(near_))]
    n_ = 128
    cx_, cy_ = float(np.round(cen_[0])), float(np.round(cen_[1]))
    ii = np.arange(n_) - n_ / 2
    X, Y = np.meshgrid(cx_ + ii, cy_ + ii)
    la_, lo_, _ = frame.enu_to_geodetic(X.ravel(), Y.ravel(), np.zeros(X.size))
    la_, lo_ = np.asarray(la_).reshape(X.shape), np.asarray(lo_).reshape(X.shape)
    try:
        u_ = np.asarray(_elev(la_.ravel(), lo_.ravel()), np.float64).reshape(X.shape)
        keep_ = dict(lat=la_, lon=lo_, u=u_)
        info_ = {}
        idx_ = bw._tunnel_holes("p", frame, _elev, keep_, cx_, cy_, None, info_, g_anchor)
        if idx_ is not None:
            import heightmap as _hm
            hole = np.asarray(idx_) == _hm.TER_HOLE
            if hole.any():
                hq = np.c_[X[hole], Y[hole]]
                hb = int((TR.edge_dist(hq, RL, RHC)[0] < 0.0).sum())
                rules["holes"].append((hb, int(hole.sum()), latlon([cx_, cy_]), str(c.get("fid", ""))[-12:]))
                if hb:
                    flags["holes"] += hb
    except Exception as e:
        rules["holes"].append((f"failed {type(e).__name__}: {e}", latlon([cx_, cy_])))
# walls (info): closed loops' share of length with a barrier within reach of the edge
BARRIER_GROUPS = {RF.G_CATCH, RF.G_WALL, RF.G_TYRE}
bar_rows = [r for r in streamed_props if r[0] in BARRIER_GROUPS] + list(streamed_rails)
for fid, w in ways.items():
    if len(w["xy"]) > 2 and np.hypot(*(w["xy"][0] - w["xy"][-1])) < 1.0 and w["spec"] is not None:
        rules["walls"].append((round(TR.barrier_share(w["xy"], bar_rows, w["spec"].width / 2, 15.0), 2),
                               round(rows[fid]["L"]) if fid in rows else 0, fid[-9:], w["name"]))
SEVERITY = TR.severity(flags)
if os.environ.get("TC_BLD"):
    la0, lo0 = map(float, os.environ["TC_BLD"].split(","))
    q0 = enu(np.array([[lo0, la0]]))[0]
    for ring, mh, cls in built_blds:
        if np.hypot(*(ring.mean(0) - q0)) < 15:
            lines_ = [w["xy"] for w in ways.values()]
            print("bld test", len(ring), "race_stand", RF.race_stand(ring, cls, lines_), "on_band", RF.on_band(ring, lines_),
                  "edge", round(float(TR.edge_dist(ring, RL, RH)[0].min()), 2))
for t_ in TRACE[:80]:
    print("trace", t_)
if os.environ.get("TC_PROBE"):          # TC_PROBE=lat,lon,dx,dy: what is at a point (and along the line dx,dy from it)
    vals = list(map(float, os.environ["TC_PROBE"].split(",")))
    q0 = enu(np.array([[vals[1], vals[0]]]))[0]
    dirv = np.array(vals[2:4]) if len(vals) >= 4 else np.array([1.0, 0.0])
    dirv = dirv / np.hypot(*dirv)
    T = roadsmod.TILE
    print("probe tile", np.floor(q0 / T).astype(int).tolist(), "to tile edges", np.round(np.minimum(q0 % T, T - q0 % T), 1).tolist())
    import mapcells as _mc
    for dd in np.arange(-6, 6.01, 1.0):
        q = q0 + dirv * dd
        la_, lo_ = latlon(q)
        tiles_ = [k for k, (b, fn) in enumerate(grounds) if b[0] <= q[0] <= b[2] and b[1] <= q[1] <= b[3]]
        zs_ = [round(float(np.asarray(grounds[k][1](np.array([q[0]]), np.array([q[1]]))).ravel()[0]), 3) for k in tiles_]
        print("probe", dd, (la_, lo_),
              "tile grounds", zs_, "surface", round(float(surface_z(q[None])[0][0]), 3))
    for o in others:
        d_, _, _ = near_line(np.r_[q0, 0.0], np.c_[o["xy"], np.zeros(len(o["xy"]))])
        if d_ < 25:
            print("probe other", o["cls"], o["sub"], o["fid"][-12:], round(d_, 1), o["rngs"])
    for n_, w_, c_ in decks:
        if near_line(np.r_[q0, 0.0], n_)[0] < 25:
            print("probe deck", c_.get("fid"), round(w_, 1))
    for tn, c_ in tunnels:
        if near_line(np.r_[q0, 0.0], tn)[0] < 25:
            print("probe tunnel", c_.get("fid"))
    for p_, fr_ in CAP_PLANS.values():
        xy_ = np.asarray(p_.xy, float)[:, :2]
        la2, lo2, _ = fr_.enu_to_geodetic(xy_[:, 0], xy_[:, 1], np.zeros(len(xy_)))
        xy_ = enu(np.c_[np.asarray(lo2), np.asarray(la2)])
        d_ = np.hypot(*(xy_ - q0).T)
        if d_.min() < 15:
            k_ = int(np.argmin(d_))
            for dd in np.arange(-6, 6.01, 1.0):
                q = q0 + dirv * dd
                dl, zl, kl = near_line(np.r_[q, 0.0], np.c_[xy_, np.asarray(p_.z, float)])
                print("probe planline", dd, "dist", round(dl, 2), "seg", kl, "plan z", round(zl, 3), "bank", None if p_.bank is None else np.round(np.asarray(p_.bank)[[kl, (kl + 1) % len(p_.bank)]], 3).tolist())
            dd_ = np.diff(xy_, axis=0)
            hd_ = np.degrees(np.arctan2(dd_[:, 1], dd_[:, 0]))
            tn_ = (np.diff(hd_) + 180) % 360 - 180
            print("probe plan xy end", np.round(xy_[-7:] - q0, 2).tolist(), "start", np.round(xy_[:4] - q0, 2).tolist())
            print("probe plan turns (deg at vertex 1..n-2)", [(i + 1, round(float(t), 1), round(float(np.hypot(*dd_[i])), 1)) for i, t in enumerate(tn_) if i + 1 >= len(xy_) - 8 or i + 1 <= 6],
                  "seam turn", round(float((hd_[0] - hd_[-1] + 180) % 360 - 180), 1))
            print("probe plan", len(xy_), "nearest", round(float(d_.min()), 1), "idx", k_,
                  [(round(float(np.hypot(*(xy_[i] - q0))), 1), round(float(p_.z[i]), 3)) for i in range(max(0, k_ - 3), min(len(xy_), k_ + 4))])
if os.environ.get("TC_PLANS"):
    la0, lo0 = map(float, os.environ["TC_PLANS"].split(","))
    q0 = enu(np.array([[lo0, la0]]))[0]
    for p_, fr_ in CAP_PLANS.values():
        xy_ = np.asarray(p_.xy, float)[:, :2]
        la_, lo_, _ = fr_.enu_to_geodetic(xy_[:, 0], xy_[:, 1], np.zeros(len(xy_)))
        xy_ = enu(np.c_[np.asarray(lo_), np.asarray(la_)])
        d_ = np.hypot(*(xy_ - q0).T)
        if d_.min() < 30:
            k_ = int(np.argmin(d_))
            print("plan", len(xy_), "half", round(float(p_.road_half), 1), "near", round(float(d_.min()), 1), "at", k_,
                  "z", np.round(np.asarray(p_.z)[max(0, k_ - 3):k_ + 4], 2).tolist(),
                  "ends", latlon(xy_[0]), latlon(xy_[-1]), "z0/z1", round(float(p_.z[0]), 2), round(float(p_.z[-1]), 2))
print(f"\nrules: severity {SEVERITY}, flags {dict(flags)}")
_fg = sorted([x for x in details["fia_g"] if MAIN.get(x[0], True)], key=lambda x: -x[1])
_fs = sorted([x for x in details["fia_step"] if MAIN.get(x[0], True)], key=lambda x: -x[1])
_fx = [x for x in details["fia_xs"] if MAIN.get(x[0], True)]
_fl = [x for x in details["fia_xs_len"] if MAIN.get(x[0], True)]
print(f"fia: vertical g over the bar {len(_fg)} (worst {_fg[0][1] if _fg else 0} g), splitter steps {len(_fs)} "
      f"(worst {_fs[0][1] if _fs else 0} cm), banking / cross-slope rate {len(_fx)}, straights off 1.5-3 % "
      f"{sum(x[2] for x in _fl)} of {sum(x[1] for x in _fl)} m")
for x_ in _fg[:10]:
    print("  fia_g", x_[1], x_[2], x_[3], "v", x_[4], "R", x_[5], x_[6])
for x_ in _fs[:10]:
    print("  fia_step", x_[1], "cm", x_[2], x_[3])
for x_ in sorted(_fx, key=lambda x: -x[2])[:6]:
    print("  fia_xs", x_[1], x_[2], "%", x_[3], x_[4])

for k_ in ("bump", "plateau", "join", "stand", "cover", "prop", "building", "width", "holes", "vault", "vault_sea", "walls", "veg_checked", "main"):
    xs = rules.get(k_, [])
    if k_ == "join":
        xs = sorted(xs, key=lambda x: -max(x[1], x[2]))
    print(f"  {k_} ({len(xs)}):")
    for x in xs[:12]:
        print("    ", x)

# ---- tracks: ways joined end to end
parent = {f: f for f in rows}


def find(x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


fl = list(rows)
for a_ in range(len(fl)):
    for b_ in range(a_ + 1, len(fl)):
        A, B = ways[fl[a_]]["xy"], ways[fl[b_]]["xy"]
        if min(np.hypot(*(p - q)) for p in (A[0], A[-1]) for q in (B[0], B[-1])) < 1.0:
            parent[find(fl[a_])] = find(fl[b_])
groups = collections.defaultdict(list)
for f in rows:
    groups[find(f)].append(rows[f])

print()
hdr = ("track", "ways", "km", "rms m", "maxdev m", "dgrade %/10m", "dxslope %/10m", "bumps", "decks", "deck steps m",
       "under", "min clear m", "max trench m", "bldgs (map)", "built solid/lifted", "gaps", "width m", "osm width", "extras")
print(" | ".join(hdr))
for g in sorted(groups.values(), key=lambda g: -sum(r["L"] for r in g)):
    names = collections.Counter(r["name"] for r in g if r["name"])
    name = names.most_common(1)[0][0] if names else "(unnamed)"
    Lt = sum(r["L"] for r in g)
    rms = math.sqrt(sum(r["rms"] ** 2 * r["L"] for r in g) / max(Lt, 1e-9))
    dk = [d for r in g for d in r["decks"]]
    un = [u for r in g for u in r["under"]]
    bl = [b for r in g for b in r["blds"]]
    gp = [x for r in g for x in r["gaps"]]
    ws = sorted({r["width"] for r in g if r["width"]})
    ows = sorted({str(r["osm_width"]) for r in g if r["osm_width"] not in (None, "", "None")})
    ex = collections.Counter()
    for r in g:
        ex.update(r["extras"])
    stp = [s for d in dk for s in d["steps"]]
    print(" | ".join(str(v) for v in (
        name, len(g), round(Lt / 1000, 2), round(rms, 3), round(max(r["maxdev"] for r in g), 2),
        round(max(r["maxdg"] for r in g), 1), round(max(r["maxdxs"] for r in g), 1), sum(r["bumps"] for r in g),
        len(dk), max(stp) if stp else "-", len(un), min([u["clear"] for u in un if np.isfinite(u["clear"])] or [float('nan')]) if un else "-",
        max(u["trench"] for u in un) if un else "-", len(bl),
        f"{sum(r.get('built', {}).get('solid', 0) for r in g)}/{sum(r.get('built', {}).get('lifted', 0) for r in g)}",
        len(gp), ws, ows or "-",
        dict(ex) or "-")))

print("\nworst roughness (|height - 25 m mean|):")
for x in sorted(details["rough"], reverse=True)[:8]:
    print("  ", x)
if "--profiles" in sys.argv:
    print("profiles at the worst points (way, index, of, surface - there every 3 m from -30 to +30 m):")
    for x in sorted(details["profile"], reverse=True)[:4]:
        print("  ", x[1:4], x[4])
print("worst grade change per 10 m (%):")
for x in sorted(details["grade"], reverse=True)[:8]:
    print("  ", x)
print("worst wheelbase bump (cm: a 4 m chord's middle off the surface):")
for x in sorted(details["wheelbase"], reverse=True)[:6]:
    print("  ", x)
print("worst cross-slope change per 10 m (%):")
for x in sorted(details["tilt"], reverse=True)[:8]:
    print("  ", x)
print("decks on the track (end step m, length m, source, at, way, caps):")
for x in sorted(details["deck"], key=lambda x: -(x[0] or 0)):
    print("  ", x)
print("(clearance nan: no deck over it and no tube drawn: the line under is left out)")
print("crossings under the track (clearance m, trench m, what, on a deck, at, way):")
for x in sorted(details["under"], key=lambda x: (not np.isfinite(x[0]), x[0])):
    print("  ", x)
print("buildings on the track (m inside, layer, class, source, height, min_height, at, way):")
for x in sorted(details["bld"], key=lambda x: (x[0], x[4] if x[4] is not None else -1), reverse=True)[:20]:
    print("  ", x)
print("buildings as built on the track (solid / lifted, m inside, bottom m, class, at, way):")
for x in sorted(details["built"], key=lambda x: (x[0] != "solid", -x[1]))[:20]:
    print("  ", x)
print("gaps between pieces (m, at, way):")
for x in sorted(details["gap"], reverse=True)[:20]:
    print("  ", x)

print("at speed (10 m chord mm, vertical g at 140 mph, bank jitter %, samples over the bar, at (lane), bank at, way):")
for x in sorted(details["speed"], key=lambda x: -x[0])[:10]:
    print("  ", x)
print("tubes under a band (samples with the vault through the band, least cover m over the floor, samples a hole would be cut at (spared on the band), cover a hole needs under, at, road):")
for x in sorted(details["band_holes"], key=lambda x: -x[0]):
    print("  ", ("FAULT" if x[0] else "ok"), x)
print("tunnel approaches flagged (flags, steepest %, tallest wall m, at, road):")
for x in details["tunnel"]:
    print("  ", x)

# ---- --json OUT: the summary for batch runs (trackbatch.py); --dump OUT: the racing surface samples (dig diffs)
for a in sys.argv[1:]:
    if a.startswith("--json="):
        import json as _json
        wb = sorted(details["wheelbase"], reverse=True)
        summary = dict(
            lat=LAT, lon=LON, ways=len(ways),
            maxdev=max([x[0] for x in details["rough"]] or [0]),
            dgrade=max([x[0] for x in details["grade"]] or [0]),
            dxslope=max([x[0] for x in details["tilt"]] or [0]),
            wheelbase_cm=wb[0][0] if wb else 0, wheelbase_at=wb[0][1] if wb else None,
            decks=len(details["deck"]), under=len(details["under"]),
            no_tube=sum(1 for u in details["under"] if "tunnel" in u[2] and not np.isfinite(u[0])),
            tunnel_flags=[[x[0], x[1], x[2], x[3]] for x in details["tunnel"]],
            built_solid=sum(1 for x in details["built"] if x[0] == "solid"),
            gaps=len(details["gap"]),
            band_holes=sum(x[0] for x in details["band_holes"]),
            chord_mm=max([x[0] for x in details["speed"]] or [0]),
            bankjit=max([x[2] for x in details["speed"]] or [0]),
            severity=SEVERITY, flags=dict(flags),
            bumps_on_line=[list(x) for x in sorted(rules["bump"], key=lambda x: -x[0])[:20]],
            fia_g=[list(x[1:]) for x in _fg[:20]], fia_g_n=len(_fg), fia_g_worst=_fg[0][1] if _fg else 0.0,
            fia_step=[list(x[1:]) for x in _fs[:20]], fia_step_n=len(_fs), fia_step_worst=_fs[0][1] if _fs else 0.0,
            fia_xs=[list(x[1:]) for x in _fx[:20]], fia_straight_m=sum(x[1] for x in _fl),
            fia_straight_off_m=sum(x[2] for x in _fl),
            plateaus_on_line=[list(x) for x in sorted(rules["plateau"], key=lambda x: -x[0])[:20]],
            join_step=max([x[1] for x in rules["join"]] or [0]), join_twist=max([x[2] for x in rules["join"]] or [0]),
            worst_join=max(rules["join"], key=lambda x: max(x[1], x[2]))[3] if rules["join"] else None,
            stands=len(rules["stand"]), stand_faults=[[x[0], x[4]] for x in rules["stand"] if x[0]][:20],
            props_on=[list(x) for x in rules["prop"][:20]], buildings_on=[list(x) for x in rules["building"][:20]],
            holes=[list(x) for x in rules["holes"]], width_guess=len(rules["width"]), vault=[list(x) for x in rules["vault"]],
            walls=[list(x) for x in rules["walls"]], veg_checked=rules["veg_checked"][0] if rules["veg_checked"] else None)
        with open(a.split("=", 1)[1], "w") as fh:
            _json.dump(summary, fh, default=float)
    if a.startswith("--lanes="):
        np.savez_compressed(a.split("=", 1)[1], **{"w" + f: v for f, v in LANE_DUMP.items()},
                            **{"s" + f: np.array([ways[f]["spec"].width]) for f in LANE_DUMP})
    if a.startswith("--dump="):
        np.savez_compressed(a.split("=", 1)[1], **{f"w{f}": np.c_[resample(w["xy"], 1.0)[0], surface_z(resample(w["xy"], 1.0)[0], f)[0]]
                                                   for f, w in ways.items()})
