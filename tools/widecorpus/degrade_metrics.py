"""The degradation test's measurements (degrade.py): a degraded build of a place against its full-data build (D0).

Every measurement is taken at the full build's stations: points every STATION_M along the centre line of each
drivable road (ground and bridge levels) inside the tile, so the two builds are read at the same places.

- cross-section: the resolved carriageway width and painted lane count of the road under the station (road_specs,
  matched by position and direction: a degraded build may split or join features differently);
- edges: the drawn road surface (the road meshes' base layer, the deck tops and the terrain's carriageway paint, which
  carries trunk's ground roads: 1 m squares) rasterised at RES_M; from each station
  out along the normal to the first cell without surface, each side. Stations where the full build's drawn width is
  not that road's own (a junction, a merge, a parallel road against it) are left out;
- vertical: the drivable surface's height at the stations (the physics terrain on the ground, the deck tops on a
  bridge), its grade over GRADE_M and its vertical acceleration at 50 mph;
- buildings: per footprint the highest eave of the drawn pieces inside it, their floors and the main roof.

The same functions measure the full build against OSM's mapped road areas (area:highway) where a place has them."""
import json
import math
from pathlib import Path

import numpy as np

from common import TREE, read
import checks

import sys
sys.path[:0] = [str(TREE / "server")]
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import roadmesh  # noqa: E402

STATION_M = 2.0
RES_M = 0.2             # surface raster cell
EDGE_MAX_M = 25.0       # an edge farther than this from the centre line: none (an open area)
MATCH_M = 1.5           # a degraded road's centre line within this of the station, ...
MATCH_COS = 0.9         # ... and as near parallel as this
GRADE_M = 10.0
G = 9.80665
MPH50 = 50 * 0.44704
STOREY_M = 3.0
NOT_DRIVEN = {"footway", "cycleway", "path", "steps", "pedestrian", "bridleway", "sidewalk", "crossing", "platform",
              "corridor", "elevator", "escalator"}
GROUPS = (("motorway/trunk", ("motorway", "trunk", "motorway_link", "trunk_link")),
          ("primary", ("primary", "primary_link")),
          ("secondary/tertiary", ("secondary", "tertiary", "secondary_link", "tertiary_link")),
          ("residential/minor", ("residential", "unclassified", "living_street", "road", "unknown", "street")),
          ("service/track", ("service", "track", "parking_aisle", "driveway", "alley")))
TERTIARY_UP = {"motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link",
               "primary_link", "secondary_link", "tertiary_link"}


def group(cls):
    for name, members in GROUPS:
        if cls in members:
            return name
    return "other"


# --- reading a build -----------------------------------------------------------------------------------------------
class Build:
    """One configuration's tile outputs (a build.py output folder run with BNE_DEGRADE)."""

    def __init__(self, directory, place):
        self.dir = Path(directory)
        self.place = place
        self.frame = geodesy.EnuFrame(place["lat"], place["lon"])
        self.tiles = {}
        for tile in read(self.dir / "tiles.json"):
            key = tile["key"]
            ctx = read(self.dir / (key + ".source.json"))
            self.tiles[key] = dict(ctx=ctx, quad=np.asarray(ctx["quad"]), tile=tile,
                                   specs=read(self.dir / (key + ".specs.json")))
        path = self.dir / "buildings.json"
        self.buildings = read(path) if path.is_file() else None

    def surface(self, key):
        """(upward road base-layer and deck-top triangles (n, 3, 3), deck tops, deck floors, the terrain, the markings
        [(material, triangles)]) of a tile."""
        t = self.tiles[key]
        if "surf" in t:
            return t["surf"]
        ctx = t["ctx"]
        terrain = checks.Terrain(self.dir / (key + ".ter"), ctx["block"])
        src = geotiles.frame(*geotiles.parse(key)[1:])
        surf, tops, floors, marks = [], [], [], []
        for file, src_frame in ((f"rg_{key}.json", src), (f"rs_1_{key}.json", None)):
            data = read(self.dir / file)
            for what, entry in data.items():
                if not isinstance(entry, dict) or "shape" not in entry or what not in ("road", "deck"):
                    continue
                for mat, tri in checks.groups(self.dir, entry, self.frame, src_frame, t["tile"]["exact"].get(what)):
                    n = checks.normal(tri)
                    up = tri[n[:, 2] > .45 * np.linalg.norm(n, axis=1)]
                    if what == "deck" and mat == roadmesh.DECK_TOP:
                        tops.append(up)
                        surf.append(up)
                    elif what == "deck" and mat == roadmesh.DECK_SIDE:
                        floors.append(up)
                    elif what == "road" and ctx["layers"].get(mat) == "base":
                        surf.append(up)
                    elif what == "road" and (ctx["layers"].get(mat) == "mark" or mat.startswith(("line_", "crossing"))):
                        marks.append((mat, up))
        cat = lambda xs: np.concatenate(xs) if xs else np.zeros((0, 3, 3))     # noqa: E731
        t["surf"] = (cat(surf), cat(tops), cat(floors), terrain, marks)
        return t["surf"]


# --- stations ------------------------------------------------------------------------------------------------------
def stations(lines, quad, step=STATION_M):
    """Points every step along each drivable line inside quad: dict of arrays pt (n, 2), tan (n, 2), line (n,), s (n,)
    (distance along the line), level (n,) and the line's cls, width, lanes."""
    pts, tans, idx, ss = [], [], [], []
    for li, road in enumerate(lines):
        if road["level"] == "tunnel" or road["cls"] in NOT_DRIVEN:
            continue
        xy = np.asarray(road["xy"], np.float64)
        seg = np.diff(xy, axis=0)
        ln = np.linalg.norm(seg, axis=1)
        keep = ln > 1e-3
        if not keep.any():
            continue
        cum = np.r_[0.0, np.cumsum(ln)]
        s = np.arange(step / 2, cum[-1], step)
        j = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(seg) - 1)
        good = ln[j] > 1e-3
        s, j = s[good], j[good]
        u = (s - cum[j]) / ln[j]
        p = xy[j] + seg[j] * u[:, None]
        t = seg[j] / ln[j][:, None]
        ok = checks.inside(p, quad)
        pts.append(p[ok])
        tans.append(t[ok])
        idx.append(np.full(int(ok.sum()), li))
        ss.append(s[ok])
    if not pts:
        return dict(pt=np.zeros((0, 2)), tan=np.zeros((0, 2)), line=np.zeros(0, int), s=np.zeros(0))
    return dict(pt=np.concatenate(pts), tan=np.concatenate(tans), line=np.concatenate(idx), s=np.concatenate(ss))


def match_lines(st, lines):
    """For each station the index of the line of lines (the same level, drivable) whose centre is nearest within
    MATCH_M and runs within MATCH_COS of its direction, or -1."""
    n = len(st["pt"])
    best = np.full(n, -1)
    dist = np.full(n, np.inf)
    for li, road in enumerate(lines):
        if road["level"] == "tunnel" or road["cls"] in NOT_DRIVEN:
            continue
        xy = np.asarray(road["xy"], np.float64)
        lo, hi = xy.min(0) - MATCH_M, xy.max(0) + MATCH_M
        near = np.flatnonzero(((st["pt"] >= lo) & (st["pt"] <= hi)).all(1))
        if not len(near):
            continue
        p = st["pt"][near]
        a, b = xy[:-1], xy[1:]
        ab = b - a
        L2 = np.maximum((ab * ab).sum(1), 1e-12)
        # (point x segment: projection, distance)
        u = np.clip(((p[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0, 1)
        q = a[None] + u[..., None] * ab[None]
        d = np.linalg.norm(p[:, None, :] - q, axis=2)
        k = d.argmin(1)
        dk = d[np.arange(len(near)), k]
        dirs = ab[k] / np.sqrt(L2[k])[:, None]
        cos = np.abs((dirs * st["tan"][near]).sum(1))
        ok = (dk <= MATCH_M) & (cos >= MATCH_COS) & (dk < dist[near])
        best[near[ok]] = li
        dist[near[ok]] = dk[ok]
    return best


# --- the drawn surface ---------------------------------------------------------------------------------------------
def raster(tris, quad, res=RES_M, pad=EDGE_MAX_M + 2):
    """(mask (h, w) bool, origin (2,), res) of triangles' xy union over quad's box plus pad."""
    from PIL import Image, ImageDraw
    lo = np.floor(np.asarray(quad).min(0) - pad)
    hi = np.ceil(np.asarray(quad).max(0) + pad)
    w, h = (np.ceil((hi - lo) / res)).astype(int)
    im = Image.new("1", (int(w), int(h)), 0)
    draw = ImageDraw.Draw(im)
    if len(tris):
        px = (np.asarray(tris)[:, :, :2] - lo) / res
        for t in px:
            draw.polygon([tuple(v) for v in t.tolist()], fill=1)
    return np.asarray(im, bool), lo, res


def paint_mask(terrain, grid):
    """grid's mask with the terrain block's carriageway paint added (asphalt and cobbles: surface.paint_roads, the
    ground roads' pavement under or instead of road meshes; one lattice square per vertex, its lower-left)."""
    import surface
    mask, lo, res = grid
    k = int(round(1.0 / res))
    h, w = mask.shape
    paint = np.isin(terrain.mat, [surface.ASPHALT, surface.COBBLE])
    # (the 1 m squares over the grid: x0 = lo, both on whole metres)
    n = len(terrain.z)
    gx0, gy0 = int(lo[0]) - int(terrain.origin[0]), int(lo[1]) - int(terrain.origin[1])
    W, H = -(-w // k), -(-h // k)
    sub = np.zeros((H, W), bool)
    xs, ys = np.arange(W) + gx0, np.arange(H) + gy0
    okx, oky = (xs >= 0) & (xs < n - 1), (ys >= 0) & (ys < n - 1)
    if okx.any() and oky.any():
        sub[np.ix_(oky, okx)] = paint[np.ix_(ys[oky], xs[okx])]
    up = np.repeat(np.repeat(sub, k, axis=0), k, axis=1)[:h, :w]
    return mask | up, lo, res


def polygons_raster(rings, quad, res=RES_M, pad=EDGE_MAX_M + 2):
    """The same raster of polygons [(outer, [holes])] (anchor frame)."""
    from PIL import Image, ImageDraw
    lo = np.floor(np.asarray(quad).min(0) - pad)
    hi = np.ceil(np.asarray(quad).max(0) + pad)
    w, h = (np.ceil((hi - lo) / res)).astype(int)
    im = Image.new("1", (int(w), int(h)), 0)
    draw = ImageDraw.Draw(im)
    for outer, holes in rings:
        draw.polygon([tuple(v) for v in ((np.asarray(outer) - lo) / res).tolist()], fill=1)
        for hole in holes:
            draw.polygon([tuple(v) for v in ((np.asarray(hole) - lo) / res).tolist()], fill=0)
    return np.asarray(im, bool), lo, res


def _at(mask, lo, res, xy):
    ij = np.floor((xy - lo) / res).astype(int)
    h, w = mask.shape
    ok = (ij[..., 0] >= 0) & (ij[..., 0] < w) & (ij[..., 1] >= 0) & (ij[..., 1] < h)
    out = np.zeros(ij.shape[:-1], bool)
    out[ok] = mask[ij[..., 1][ok], ij[..., 0][ok]]
    return out


def edges(grid, pt, tan, step=RES_M / 2):
    """(left, right, centre) offsets (m) of the surface's edges from each station along its normal: NaN where the
    surface runs on past EDGE_MAX_M; centre: the station itself is on the surface."""
    mask, lo, res = grid
    perp = np.column_stack((-tan[:, 1], tan[:, 0]))
    off = np.arange(step, EDGE_MAX_M + step, step)
    centre = _at(mask, lo, res, pt)
    out = []
    for sign in (1, -1):
        q = pt[:, None, :] + sign * off[None, :, None] * perp[:, None, :]
        on = _at(mask, lo, res, q)
        first = np.argmin(on, axis=1)               # (the first cell off the surface)
        found = ~on[np.arange(len(pt)), first]
        out.append(np.where(found & centre, off[first] - step / 2, np.nan))
    return out[0], out[1], centre


# --- heights -------------------------------------------------------------------------------------------------------
def heights(build, key, pt, level):
    """The drivable surface's height at stations: the deck tops on a bridge, the physics terrain on the ground."""
    surf, tops, floors, terrain, _ = build.surface(key)
    z = terrain.sample(pt)[0]
    br = level == "bridge"
    if br.any():
        z[br] = checks.ray_height(tops, pt[br])
    return z


def profiles(z, line, s):
    """Per station (grade % over GRADE_M, vertical acceleration g at 50 mph) along its own line, NaN at the ends."""
    grade = np.full(len(z), np.nan)
    ride = np.full(len(z), np.nan)
    k = int(round(GRADE_M / STATION_M / 2))
    for li in np.unique(line):
        idx = np.flatnonzero(line == li)
        idx = idx[np.argsort(s[idx])]
        zz, sv = z[idx], s[idx]
        run = np.r_[0, np.cumsum(np.abs(np.diff(sv) - STATION_M) > 1e-6)]  # (contiguous stations only)
        for r in np.unique(run):
            seg = np.flatnonzero(run == r)
            if len(seg) > 2 * k:
                g = (zz[seg[2 * k:]] - zz[seg[:-2 * k]]) / (2 * k * STATION_M) * 100
                grade[idx[seg[k:-k]]] = g
            if len(seg) > 2:
                c = (zz[seg[2:]] - 2 * zz[seg[1:-1]] + zz[seg[:-2]]) / STATION_M ** 2
                ride[idx[seg[1:-1]]] = c * MPH50 ** 2 / G
    return grade, ride


# --- one tile, one degraded build ----------------------------------------------------------------------------------
def tile_rows(full, deg, key, areas=None):
    """The per-station measurements of tile key: a dict of arrays (cls code via the line, errors)."""
    lines0 = full.tiles[key]["specs"]
    lines1 = deg.tiles[key]["specs"] if deg is not None else None
    quad = full.tiles[key]["quad"]
    st = stations(lines0, quad)
    n = len(st["pt"])
    cls = np.array([lines0[i]["cls"] for i in st["line"]], dtype=object) if n else np.zeros(0, object)
    level = np.array([lines0[i]["level"] for i in st["line"]]) if n else np.zeros(0, "<U6")
    w0 = np.array([lines0[i]["width"] for i in st["line"]], float) if n else np.zeros(0)
    l0 = np.array([lines0[i]["lanes"] for i in st["line"]], float) if n else np.zeros(0)
    mapped_l0 = np.array([lines0[i].get("mapped_lanes", 0) for i in st["line"]], float) if n else np.zeros(0)
    out = dict(cls=cls, level=level, w0=w0, l0=l0, mapped_l0=mapped_l0)
    g0 = paint_mask(full.surface(key)[3], raster(full.surface(key)[0], quad))
    L0, R0, c0 = edges(g0, st["pt"], st["tan"])
    # (the full build's drawn width is that road's own: not a junction or a merge)
    own = c0 & np.isfinite(L0) & np.isfinite(R0) & (L0 + R0 <= np.maximum(w0 + 4.0, 1.3 * w0))
    out.update(L0=L0, R0=R0, own=own)
    z0 = heights(full, key, st["pt"], level)
    gr0, rd0 = profiles(z0, st["line"], st["s"])
    out.update(z0=z0, grade0=gr0, ride0=rd0)
    if areas is not None:
        ga = polygons_raster(areas, quad)
        La, Ra, ca = edges(ga, st["pt"], st["tan"])
        out.update(La=La, Ra=Ra, area_centre=ca)
    if deg is None:
        return out
    m = match_lines(st, lines1)
    w1 = np.array([lines1[i]["width"] if i >= 0 else np.nan for i in m], float)
    l1 = np.array([lines1[i]["lanes"] if i >= 0 else np.nan for i in m], float)
    g1 = paint_mask(deg.surface(key)[3], raster(deg.surface(key)[0], quad))
    L1, R1, c1 = edges(g1, st["pt"], st["tan"])
    z1 = heights(deg, key, st["pt"], level)
    gr1, rd1 = profiles(z1, st["line"], st["s"])
    out.update(matched=m >= 0, w1=w1, l1=l1, L1=L1, R1=R1, centre1=c1, z1=z1, grade1=gr1, ride1=rd1)
    return out


def place_rows(full, deg, areas_by_tile=None):
    """tile_rows of every tile, concatenated."""
    parts = [tile_rows(full, deg, key, (areas_by_tile or {}).get(key)) for key in sorted(full.tiles)]
    keys = set.intersection(*(set(p) for p in parts)) if parts else set()
    return {k: np.concatenate([p[k] for p in parts]) for k in sorted(keys)}


# --- buildings -----------------------------------------------------------------------------------------------------
def building_table(data):
    """{footprint id: (eave m, floors, main roof)} of a build's buildings.json: the highest drawn piece whose ring's
    centre is inside the footprint, the roof of the largest piece."""
    from matplotlib.path import Path as MPath
    if not data or not data["footprints"]:
        return {}
    pieces = data["pieces"]
    cent = np.array([np.asarray(p[0], float).mean(0) for p in pieces]) if pieces else np.zeros((0, 2))
    area = np.array([abs(_area(np.asarray(p[0], float))) for p in pieces]) if pieces else np.zeros(0)
    out = {}
    for fid, ring in data["footprints"]:
        r = np.asarray(ring, float)
        lo, hi = r.min(0), r.max(0)
        cand = np.flatnonzero(((cent >= lo) & (cent <= hi)).all(1)) if len(cent) else np.zeros(0, int)
        if not len(cand):
            continue
        inside = cand[MPath(r).contains_points(cent[cand])]
        if not len(inside):
            continue
        top = max(inside, key=lambda k: (pieces[k][2], area[k]))
        big = max(inside, key=lambda k: (area[k], pieces[k][2]))
        out[fid] = (float(pieces[top][2]), int(pieces[top][3]), str(pieces[big][4]))
    return out


def _area(r):
    return 0.5 * float(np.dot(r[:-1, 0], r[1:, 1]) - np.dot(r[1:, 0], r[:-1, 1]))


def building_rows(full_data, deg_data, mapped):
    """Per footprint present in both: eave error m, storey error, floors equal, roof equal; mapped: the footprint ids
    whose full data had a mapped height or levels."""
    a, b = building_table(full_data), building_table(deg_data)
    ids = sorted(set(a) & set(b))
    if not ids:
        return dict(err_m=np.zeros(0), floors_eq=np.zeros(0, bool), roof_eq=np.zeros(0, bool), mapped=np.zeros(0, bool))
    e = np.array([abs(b[i][0] - a[i][0]) for i in ids])
    return dict(err_m=e, floors_eq=np.array([a[i][1] == b[i][1] for i in ids]),
                roof_eq=np.array([a[i][2] == b[i][2] for i in ids]), mapped=np.array([i in mapped for i in ids]))


# --- summaries -----------------------------------------------------------------------------------------------------
def q(values, p):
    v = np.asarray(values, float)
    v = v[np.isfinite(v)]
    return float(np.percentile(v, p)) if len(v) else math.nan


def summarize_roads(rows):
    """{group: stats} of place_rows (or several places' concatenated): width, lane, edge, drawn width, vertical."""
    out = {}
    if not len(rows.get("cls", ())):
        return out
    grp = np.array([group(c) for c in rows["cls"]])
    for name in [g for g, _ in GROUPS] + ["other", "all"]:
        sel = np.ones(len(grp), bool) if name == "all" else grp == name
        if not sel.any():
            continue
        r = {k: v[sel] for k, v in rows.items()}
        s = dict(stations=int(sel.sum()), metres=round(float(sel.sum() * STATION_M), 1))
        if "w1" in r:
            m = r["matched"]
            werr = np.abs(r["w1"] - r["w0"])[m]
            s.update(matched=round(float(m.mean()), 4), width_med=q(werr, 50), width_p90=q(werr, 90),
                     lanes_exact=round(float((r["l1"] == r["l0"])[m].mean()), 4) if m.any() else math.nan)
            mapped = m & (r["mapped_l0"] > 0)
            s["lanes_exact_mapped"] = round(float((r["l1"] == r["l0"])[mapped].mean()), 4) if mapped.any() else math.nan
            # (on the roads whose full data mapped a lane count: what removing the tags changes where there were any)
            wm = np.abs(r["w1"] - r["w0"])[mapped]
            s.update(width_mapped_med=q(wm, 50), width_mapped_p90=q(wm, 90))
            s["mapped_share"] = round(float(mapped.sum() / max(m.sum(), 1)), 4)
            own = r["own"]
            hole = own & ~r["centre1"]
            e = np.concatenate([np.abs(r["L1"] - r["L0"])[own], np.abs(r["R1"] - r["R0"])[own]])
            dw = np.abs((r["L1"] + r["R1"]) - (r["L0"] + r["R0"]))[own]
            s.update(edge_stations=int(own.sum()), edge_med=q(e, 50), edge_p90=q(e, 90), drawn_width_med=q(dw, 50),
                     drawn_width_p90=q(dw, 90), surface_lost=round(float(hole.sum() / max(own.sum(), 1)), 4))
            fin = np.isfinite(r["z0"]) & np.isfinite(r["z1"])
            dz = np.abs(r["z1"] - r["z0"])[fin]
            dg = np.abs(r["grade1"] - r["grade0"])
            dr = np.abs(r["ride1"] - r["ride0"])
            s.update(z_med=q(dz, 50), z_p90=q(dz, 90), grade_med=q(dg, 50), grade_p90=q(dg, 90), ride_med=q(dr, 50),
                     ride_p90=q(dr, 90),
                     grade15_0=int((np.abs(r["grade0"]) > 15).sum()), grade15_1=int((np.abs(r["grade1"]) > 15).sum()),
                     ride05_0=int((np.abs(r["ride0"]) > .5).sum()), ride05_1=int((np.abs(r["ride1"]) > .5).sum()))
            if "La" in r:
                s.update(area_vs=_area_stats(r))
        if "La" in r and "w1" not in r:
            s.update(area_vs=_area_stats(r))
        out[name] = s
    return out


def _area_stats(r):
    """Edge error against the mapped road areas (area:highway), full and degraded, at the stations where both the
    full build and the mapped area give the road's own edges."""
    ok = r["own"] & r["area_centre"] & np.isfinite(r["La"]) & np.isfinite(r["Ra"]) & (
        r["La"] + r["Ra"] <= np.maximum(r["w0"] + 4.0, 1.3 * r["w0"]))
    e0 = np.concatenate([np.abs(r["L0"] - r["La"])[ok], np.abs(r["R0"] - r["Ra"])[ok]])
    out = dict(stations=int(ok.sum()), full_med=q(e0, 50), full_p90=q(e0, 90))
    if "L1" in r:
        e1 = np.concatenate([np.abs(r["L1"] - r["La"])[ok], np.abs(r["R1"] - r["Ra"])[ok]])
        out.update(deg_med=q(e1, 50), deg_p90=q(e1, 90))
    return out


def summarize_buildings(rows):
    out = {}
    rows = {k: (np.asarray(v, float) if k == "err_m" else np.asarray(v).astype(bool)) for k, v in rows.items()}
    for name, sel in (("all", np.ones(len(rows["err_m"]), bool)), ("mapped", rows["mapped"])):
        if not sel.any():
            continue
        e = rows["err_m"][sel]
        out[name] = dict(buildings=int(sel.sum()), err_med_m=q(e, 50), err_p90_m=q(e, 90),
                         err_med_storeys=q(e / STOREY_M, 50), err_p90_storeys=q(e / STOREY_M, 90),
                         floors_exact=round(float(rows["floors_eq"][sel].mean()), 4),
                         roof_same=round(float(rows["roof_eq"][sel].mean()), 4))
    return out


def save_rows(path, rows):
    np.savez_compressed(path, **{k: (v.astype(str) if v.dtype == object else v) for k, v in rows.items()})


def load_rows(path):
    with np.load(path, allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def mapped_heights(full_dir):
    path = Path(full_dir) / "buildings.json"
    if not path.is_file():
        return set()
    return set(read(path).get("mapped", []))


def dumps(obj):
    return json.dumps(obj, sort_keys=True, indent=1, default=float)
