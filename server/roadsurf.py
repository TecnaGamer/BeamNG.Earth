"""Each untagged minor road's surface from the aerial photo (the user, 2026-11-12: "fix any instances of the road material
being incorrect"): residential, service, unclassified and unknown roads and tracks without a surface value are judged
paved or unpaved by their colour in the z17 aerial tiles already on the disk for the ground photo (NAIP, the state and
national orthophotos; never fetched here), against the same photo's own references: the roads round them the map says
are paved (a paved surface value, or primary .. tertiary) and unpaved. The regional rule (roads.regional_unpaved) can
only call all of a window's untagged roads gravel or all paved: round GitHub #834 (Hunt County, Texas) ~60 % of them
are paved by the photo and ~40 % gravel, and either call was wrong for a third or more.

The measuring and the verdict are earthfast.road_photo_surface's (native 231: the tiles read and decoded there, no GIL;
tools/roadscore.py's method): per road the median luma, saturation, excess green and luma spread of its centre band
(shade and an export's no-data left out, the first and last END_M of each piece left out: the other road of a
junction), its distance to the paved and to the unpaved references' medians in SCALE units, a verdict only when one is
nearer by MARGIN of their separation and the references stand MIN_SEP apart (MIN_REFS of each measured).

When: a block's photo rendered with all its imagery tiles on the disk (earthd._base_kept) has the cells under it
measured in a block process (blockworker.roadsurf_area): the home cell's candidates not judged yet (roads._home_cells:
a road's verdict lives in the cell of its first point, one for every copy of it), its references from it and the eight
cells round it. The verdicts are kept per cell on the disk (<cache>/roadsurf/v<FORMAT>/z/x/y.json), each with the time
it was made, and are used from the next server run on (T0: earthd's start, BNGE_ROADSURF_T0, handed to its processes):
a run never changes a road's surface while it runs (its tiles, blocks and photos agree with each other; on a first
visit the tiles come after the roads' facts anyway), and a road changes at most once, from the regional rule's guess to
the photo's verdict, at a later start. Whatever is kept across runs and built from the specs carries the verdicts'
digest of the cells it covers in its key (roads.road_specs' facts, the plans, the road and far tiles: area_tags /
cell_tags), so a cached item made before a verdict is not taken for one made after it. A road the photo cannot tell
(too short, in shade, no references) keeps the regional rule.

BNE_ROADSURF=off: none of it; BNE_ROADSURF_NOW=1 (tools, tests): verdicts used as soon as they are made."""
import hashlib
import json
import logging
import math
import os
import threading
import time

import numpy as np
import dnp

log = logging.getLogger("earthd.roadsurf")

ON = os.environ.get("BNE_ROADSURF", "on").lower() not in ("off", "0", "false")
FORMAT = 1                  # the store's folder (v1): bump when a verdict would come out otherwise
ZOOM = 17
CANDIDATE_CLASSES = {"residential", "service", "unclassified", "unknown", "track"}
PAVED_REF_CLASSES = {"primary", "secondary", "tertiary"}    # (untagged: paved references; motorways and trunks are wider
                                                            # and often concrete: another look)
REF_CLASSES = PAVED_REF_CLASSES | CANDIDATE_CLASSES
STEP_M = 2.0                # m between a road's samples along it
END_M = 12.0                # m left out at each end of a piece (a junction: the other road's surface)
OFFSETS_M = (-0.8, 0.0, 0.8)    # m across its centre line (the centre lines sit within ~1 m in the US, roadscore)
DARK_L = 55.0               # luma under this: shade or canopy, left out
MIN_SAMPLES = 30            # samples kept for a road to be measured (~20 m of road)
MIN_HAVE = 0.6              # ... and this share of its samples with their tile on the disk
MIN_REFS = 3                # references of each kind measured, else no verdict
# each kind of reference: the medians of its roads' (luma, saturation, excess green, luma spread) and their spread
# (1.4826 MAD over its roads, at least FLOOR); a road's -2 log-likelihood under each (independent normals)
FLOOR = (6.0, 0.02, 0.012, 4.0)
MARGIN = 2.0                # -2 log-likelihood the nearer kind wins by (a likelihood ratio of e)
OUTLIER = 16.0              # ... and the road's squared distance in that kind's spreads at most (like neither: no verdict)
# a road that does not stand out from its verges (VERGE_M out either side) where they are vegetation (saturation
# VERGE_SAT or excess green VERGE_EXG and up): a field track the photo shows as field, unpaved (VIS_MIN in SCALE units)
VERGE_M = 7.0
VIS_MIN = 0.8
VERGE_SAT = 0.2
VERGE_EXG = 0.04
VERGE_L = 115.0             # ... both the road and its verges this bright at least (under a canopy both are the trees)
SCALE = (25.0, 0.04, 0.03, 6.0)     # luma, saturation, excess green, luma spread: one unit each (roadscore)
MAX_REFS = 80               # references of each kind measured at most (the nearest to the cell's centre)
PAVED, UNPAVED = 1, 2

_T0_ENV = "BNGE_ROADSURF_T0"
NOW = os.environ.get("BNE_ROADSURF_NOW") == "1"
_lock = threading.Lock()
_entries = {}               # cell str -> {fid: verdict} used this run (made before T0)
_digests = {}               # cell str -> digest str ("" none)


def t0():
    """The time before which a verdict is used in this run: earthd's start (BNGE_ROADSURF_T0, set at its start and
    inherited by its processes), else this process's first ask; NOW: any."""
    if NOW:
        return float("inf")
    v = os.environ.get(_T0_ENV)
    if v is None:
        v = os.environ[_T0_ENV] = repr(time.time())
    try:
        return float(v)
    except ValueError:
        return 0.0


def mark_run_start():
    """earthd's start: this run's T0 (its processes inherit it)."""
    os.environ[_T0_ENV] = repr(time.time())


def cache_dir():
    import revisit
    if revisit.CACHE_DIR:
        return revisit.CACHE_DIR
    import mapdata
    return os.environ.get("BNGE_CACHE_DIR") or mapdata.DEFAULT_CACHE


def _path(cell):
    z, x, y = str(cell).split("/")
    return os.path.join(cache_dir(), "roadsurf", f"v{FORMAT}", z, x, f"{y}.json")


def _read(cell):
    try:
        with open(_path(cell), encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def entry(cell):
    """{fid: PAVED / UNPAVED} of the roads whose home cell is `cell`, as this run uses them (made before t0()); read once
    a process (NOW: again each time)."""
    cell = str(cell)
    if not NOW:
        with _lock:
            got = _entries.get(cell)
        if got is not None:
            return got
    lim = t0()
    got = {}
    for fid, rec in (_read(cell).get("v") or {}).items():
        try:
            v, t = int(rec[0]), float(rec[1])
        except (TypeError, ValueError, IndexError):
            continue
        if v in (PAVED, UNPAVED) and t < lim:
            got[fid] = v
    with _lock:
        _entries[cell] = got
        _digests.pop(cell, None)
    return got


def digest(cell):
    """A short hash of the verdicts cell holds this run ("" for none)."""
    cell = str(cell)
    with _lock:
        got = _digests.get(cell)
    if got is not None and not NOW:
        return got
    e = entry(cell)
    got = "" if not e else hashlib.blake2s(json.dumps(sorted(e.items())).encode(), digest_size=6).hexdigest()
    with _lock:
        _digests[cell] = got
    return got


def _tags(cells):
    if not ON:
        return ()
    parts = [f"{c}:{d}" for c in sorted(set(map(str, cells))) for d in (digest(c),) if d]
    if not parts:
        return ()
    return ("rs" + hashlib.blake2s("|".join(parts).encode(), digest_size=6).hexdigest(),)


def cell_tags(key):
    """() or (tag,): the verdicts of cell key and the eight round it (a cache key's part: plans)."""
    import mapcells
    k = mapcells.CellKey(*[int(v) for v in (key if not isinstance(key, str) else key.split("/"))])
    return _tags([k] + k.neighbours())


def area_tags(lat0, lon0, lat1, lon1):
    """() or (tag,): the verdicts of the cells over the box and one ring round them (a road's home cell can be the
    next one: a cache key's part, road and far tiles)."""
    if not ON:
        return ()
    import mapcells
    try:
        a = mapcells.cell_of(max(min(lat0, lat1), -85.0), min(lon0, lon1))
        b = mapcells.cell_of(min(max(lat0, lat1), 85.0), max(lon0, lon1))
    except ValueError:
        return ()
    cells = []
    n = 1 << a.z
    if (abs(b.x - a.x) + 1) * (abs(b.y - a.y) + 1) > 64:     # (a far tile kilometres wide: none of this matters there)
        return ()
    for x in range(min(a.x, b.x) - 1, max(a.x, b.x) + 2):
        for y in range(min(a.y, b.y) - 1, max(a.y, b.y) + 2):
            if 0 <= y < n:
                cells.append(mapcells.CellKey(a.z, x % n, y))
    return _tags(cells)


def candidates(feats, vals=None):
    """bool (n,): feats' roads the photo may judge: CANDIDATE_CLASSES, no surface value, no mapped lane count or good
    smoothness (a mapper who added those looked at a paved road: roads' regional rule leaves them paved too)."""
    import roads as roadsmod
    n = len(feats)
    cls = feats.attrs.get("class")
    if cls is None or not n:
        return np.zeros(n, bool)
    vals = roadsmod._map_surface_values(feats) if vals is None else vals
    out = np.fromiter((str(c) in CANDIDATE_CLASSES and v is None for c, v in zip(cls.tolist(), vals)), bool, n)
    lanes = roadsmod._osm_values(feats, "lanes", *roadsmod.OSM_LANES)
    sm = feats.attrs.get("osm_smoothness")
    good = np.zeros(n, bool) if sm is None else np.fromiter(
        (str(v).strip().lower() in roadsmod.GOOD_SMOOTHNESS for v in sm.tolist()), bool, n)
    return out & np.isnan(lanes) & ~good


_store_seen = []        # [bool]: the store's folder was there at this process's first look (made later: no verdict of
                        # it is older than this run's start, NOW aside)


def _store_exists():
    if NOW:
        return os.path.isdir(os.path.join(cache_dir(), "roadsurf", f"v{FORMAT}"))
    if not _store_seen:
        _store_seen.append(os.path.isdir(os.path.join(cache_dir(), "roadsurf", f"v{FORMAT}")))
    return _store_seen[0]


def layer_verdicts(feats, vals=None):
    """{feature index: PAVED / UNPAVED} of feats' candidates the photo judged (this run's), kept on feats."""
    if not ON or not len(feats):
        return {}
    got = feats.__dict__.get("_roadsurf")
    if got is not None:
        return got
    if not _store_exists():         # (no verdict anywhere yet: nothing to look up, the layer's candidates not listed)
        feats.__dict__["_roadsurf"] = {}
        return {}
    import roads as roadsmod
    out = {}
    cand = candidates(feats, vals)
    if cand.any():
        homes = roadsmod._home_cells(feats)
        ids = feats.ids
        for i in np.flatnonzero(cand).tolist():
            v = entry(homes[i]).get(str(ids[i]))
            if v:
                out[i] = v
    feats.__dict__["_roadsurf"] = out
    return out


def layer_tags(feats):
    """() or (tag,): the digest of the verdicts layer_verdicts used (road_specs' cache key)."""
    v = layer_verdicts(feats)
    if not v:
        return ()
    ids = feats.ids
    return ("rs" + hashlib.blake2s(json.dumps(sorted((str(ids[i]), k) for i, k in v.items())).encode(),
                                   digest_size=6).hexdigest(),)


# --- measuring (a block process, after a photo) ---

def z17_sources(lat, lon):
    """The default chain's sources with z17 tiles at a point, in its order: [(name, tile folder)]."""
    import imagery as imagerymod
    out = []
    for name in imagerymod.DEFAULT_CHAIN:
        s = imagerymod.SOURCES.get(name)
        if s is None or s.min_zoom > ZOOM or s.max_zoom < ZOOM or s.pack or name == "bcortho":
            continue
        if s.bounds and not (s.bounds[0] <= lat <= s.bounds[2] and s.bounds[1] <= lon <= s.bounds[3]):
            continue
        out.append((name, os.path.join(cache_dir(), "imagery", name)))
    return out


def _tile_xy(lat, lon, z=ZOOM):
    n = 2.0 ** z
    lat = np.radians(np.asarray(lat, np.float64))
    x = (np.asarray(lon, np.float64) + 180.0) / 360.0 * n
    y = (1.0 - dnp.arcsinh(dnp.tan(lat)) / math.pi) / 2.0 * n
    return np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)


def _has_data(path):
    """Is the tile at path a picture (not an export's white / black outside its data: a state's service answers its
    whole box, Ontario's reaches down over Iowa)? Read on a 16 x 16 thumbnail."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            a = np.asarray(im.convert("RGB").resize((16, 16)), np.int16)
    except (OSError, ValueError):
        return False
    blank = (a.min(axis=2) > 245) | (a.max(axis=2) < 12)
    return float(blank.mean()) < 0.5


def _pick_source(lat, lon, pts):
    """(name, folder) of the chain's first z17 source with half of pts' (lon / lat (k, 2)) tiles on the disk (and a
    picture in a few of them), else the one with the most, or None."""
    if not len(pts):
        return None
    tx, ty = _tile_xy(pts[:, 1], pts[:, 0])
    tiles = sorted(set(zip(tx.tolist(), ty.tolist())))
    best, best_n = None, 0
    for name, root in z17_sources(lat, lon):
        have = [os.path.join(root, str(ZOOM), str(x), f"{y}.jpg") for x, y in tiles]
        have = [p for p in have if os.path.exists(p)]
        if not have:
            continue
        step = max(1, len(have) // 3)
        if sum(_has_data(p) for p in have[::step][:3]) < 2 and len(have) >= 2:
            continue                # (the source's blank outside its data)
        if len(have) >= 0.5 * len(tiles):
            return name, root
        if len(have) > best_n:
            best, best_n = (name, root), len(have)
    return best


def measure_cell(key, layer, neighbours=()):
    """Judge cell key's candidates (its layer's roads whose home cell it is, not judged yet) from the z17 tiles on the
    disk, against the references of layer and neighbours (roads layers); the verdicts kept in the store. Returns a
    summary dict (or None: nothing to judge, no references, no tiles)."""
    import fast
    import roads as roadsmod
    if not ON or layer is None or not len(layer):
        return None
    t_start = time.perf_counter()
    cell = str(key)
    stored = _read(cell)
    known = set((stored.get("v") or {}).keys()) | set((stored.get("u") or {}).keys())
    homes = roadsmod._home_cells(layer)
    cand = candidates(layer)
    ids = layer.ids
    cand_rows = [i for i in np.flatnonzero(cand).tolist() if homes[i] == cell and str(ids[i]) not in known]
    if not cand_rows:
        return None
    # references: the layers' roads with a paved / unpaved value of a car class, or primary .. tertiary untagged
    lat_c, lon_c = _centre(key)
    refs = {PAVED: [], UNPAVED: []}
    seen = set()
    for f in [layer] + [g for g in neighbours if g is not None and len(g)]:
        cls = f.attrs.get("class")
        if cls is None:
            continue
        vals = roadsmod._map_surface_values(f)
        sub = f.attrs.get("subtype")
        for i, c in enumerate(cls.tolist()):
            c = str(c)
            fid = str(f.ids[i])
            if c not in REF_CLASSES or fid in seen or (sub is not None and str(sub[i]) != "road"):
                continue
            v = vals[i]
            if v is None:
                kind = PAVED if c in PAVED_REF_CLASSES else None
            else:
                k = roadsmod.surface_kind(c, v)
                kind = PAVED if k == "paved" else UNPAVED if k in ("gravel", "dirt") else None
            if kind is None:
                continue
            seen.add(fid)
            a = f.coord_off[f.ring_off[f.part_off[i]]]
            d = math.hypot(float(f.lonlat[a, 0]) - lon_c, float(f.lonlat[a, 1]) - lat_c)
            refs[kind].append((d, f, i))
    if len(refs[PAVED]) < MIN_REFS or len(refs[UNPAVED]) < MIN_REFS:
        return {"cell": cell, "skipped": "refs", "n_paved_refs": len(refs[PAVED]), "n_unpaved_refs": len(refs[UNPAVED])}
    rows = [(layer, i, 0) for i in cand_rows]
    for kind in (PAVED, UNPAVED):
        rows += [(f, i, kind) for _, f, i in sorted(refs[kind], key=lambda r: r[0])[:MAX_REFS]]
    pts, poly_off, poly_road = [], [0], []
    for r, (f, i, _) in enumerate(rows):
        for part in f.parts(i):
            for ring in part:
                ring = np.asarray(ring, np.float64)[:, :2]
                if len(ring) >= 2:
                    pts.append(ring)
                    poly_off.append(poly_off[-1] + len(ring))
                    poly_road.append(r)
    if not pts:
        return None
    pts = np.ascontiguousarray(np.concatenate(pts))
    cpts = np.concatenate([np.asarray(r, np.float64)[:, :2] for (f, i, k) in rows if k == 0
                           for part in f.parts(i) for r in part])
    src = _pick_source(lat_c, lon_c, cpts)
    if src is None:
        return {"cell": cell, "skipped": "tiles"}
    fast.require(231, "road surfaces from the photo")
    role = np.array([k for _, _, k in rows], np.int8)
    feats_out, verdict, rinfo, n_read = fast._ef.road_photo_surface(
        src[1], ZOOM, pts, np.asarray(poly_off, np.int64), np.asarray(poly_road, np.int64), role, list(OFFSETS_M),
        {"step": STEP_M, "end": END_M, "dark": DARK_L, "min_n": MIN_SAMPLES, "min_have": MIN_HAVE,
         "min_refs": MIN_REFS, "margin": MARGIN, "outlier": OUTLIER, "verge": VERGE_M, "vis_min": VIS_MIN,
         "verge_sat": VERGE_SAT, "verge_exg": VERGE_EXG, "verge_l": VERGE_L}, list(FLOOR), list(SCALE))
    np_refs, nu_refs = divmod(int(rinfo[16]), 1000)
    out = {"cell": cell, "src": src[0], "candidates": len(cand_rows), "tiles": int(n_read),
           "n_paved_refs": np_refs, "n_unpaved_refs": nu_refs,
           "refs": None if rinfo[0] != rinfo[0] else [round(float(v), 3) for v in rinfo[:16]],
           "paved": 0, "unpaved": 0, "unsure": 0, "unmeasured": 0}
    now = time.time()
    new_v, new_u = {}, {}
    if np_refs >= MIN_REFS and nu_refs >= MIN_REFS:
        for r, i in enumerate(cand_rows):
            v = int(verdict[r])
            fid = str(ids[i])
            if v in (PAVED, UNPAVED):
                new_v[fid] = [v, now]
                out["paved" if v == PAVED else "unpaved"] += 1
            elif v == 0:
                new_u[fid] = now        # (measured, the photo cannot tell: the regional rule stays; not asked again)
                out["unsure"] += 1
            else:
                out["unmeasured"] += 1
    else:
        out["unmeasured"] = len(cand_rows)
    if new_v or new_u:
        _merge(cell, new_v, new_u, src[0])
    out["s"] = round(time.perf_counter() - t_start, 4)
    out["feats"] = feats_out
    out["verdict"] = verdict
    out["rows"] = [(str(f.ids[i]), k) for f, i, k in rows]
    return out


def _centre(key):
    import mapcells
    k = key if isinstance(key, mapcells.CellKey) else mapcells.CellKey(*[int(v) for v in str(key).split("/")])
    return k.center()


def _merge(cell, new_v, new_u, src):
    """Add verdicts to cell's store file (read again and replaced: another process's additions kept; a verdict once
    made is never changed)."""
    import staging
    path = _path(cell)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        got = _read(cell)
        v = got.get("v") or {}
        u = got.get("u") or {}
        for fid, rec in new_v.items():
            v.setdefault(fid, rec)
        for fid, t in new_u.items():
            if fid not in v:
                u.setdefault(fid, t)
        tmp = staging.tmp_for(path)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"format": FORMAT, "src": src, "v": v, "u": u}, f)
        staging.replace(tmp, path)
    except OSError as e:
        log.debug("road surfaces of %s not kept: %s", cell, e)
    if NOW:
        with _lock:
            _entries.pop(str(cell), None)
            _digests.pop(str(cell), None)


_area_done = set()


def measure_area(md, lat0, lon0, lat1, lon1):
    """measure_cell for the cells under the box (a block's photo with all its imagery on the disk), each once a
    process run; map data from md (a MapData; the cells it has on the disk or in memory). Returns the summaries."""
    if not ON or md is None:
        return []
    import mapcells
    out = []
    try:
        a = mapcells.cell_of(min(lat0, lat1), min(lon0, lon1))
        b = mapcells.cell_of(max(lat0, lat1), max(lon0, lon1))
    except ValueError:
        return out
    for x in range(min(a.x, b.x), max(a.x, b.x) + 1):
        for y in range(min(a.y, b.y), max(a.y, b.y) + 1):
            key = mapcells.CellKey(a.z, x, y)
            with _lock:
                if str(key) in _area_done:
                    continue
                _area_done.add(str(key))
            try:
                layer = md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
                hook = layer.__dict__.get("_bnge_neighbours") if layer is not None else None
                nb = [g for g in hook() if g is not None] if hook is not None else []
                got = measure_cell(key, layer, nb)
            except Exception as e:      # (the photo's verdicts are a refinement: never the build's failure)
                log.info("road surfaces of %s: %s: %s", key, type(e).__name__, e)
                with _lock:
                    _area_done.discard(str(key))
                continue
            if got is not None:
                if got.get("skipped") == "tiles" or got.get("unmeasured"):
                    with _lock:
                        _area_done.discard(str(key))     # (asked again after the next photo: more of its tiles)
                got = {k: v for k, v in got.items() if k not in ("feats", "verdict", "rows")}
                if got.get("paved") or got.get("unpaved"):
                    log.info("road surfaces of %s from the photo (%s): %d paved, %d unpaved, %d unsure, %d unmeasured "
                             "of %d (refs %d / %d) in %.0f ms, %d tiles; used from the next start",
                             key, got.get("src"), got["paved"], got["unpaved"], got["unsure"], got["unmeasured"],
                             got["candidates"], got["n_paved_refs"], got["n_unpaved_refs"], got["s"] * 1000, got["tiles"])
                out.append(got)
    return out
