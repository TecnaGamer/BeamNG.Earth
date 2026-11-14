"""Offline replays of reported interchanges (fixtures/interchange_*.npz, tools/capture_interchange_fixture.py).

Each fixture holds the real Overture roads cells round a reported place and the raw elevation there (two grids); the
grading, the carriageway solver (BNE_INTERCHANGE) and the bridge profiles run afresh on them, with no network.

    python server/tests/interchanges.py [NAME ...]      # one JSON line per site: its decks' measurements

measure() reports, for the decks within RADIUS_M of the place:
  road_max_grade  the steepest graded grade of a motorway or trunk on the ground (per STEP_M)
  decks           how many bridge parts
  max_grade       the steepest deck grade (rise over run, per STEP_M)
  max_grade_jump  the largest grade change between two steps (a crest or a kink)
  min_gap         the least height of a deck's road over a ground road it crosses (graded ground there)
  low             the crossings whose gap is under LOW_GAP_M (a deck that blocks, or lies on, the road under it)
These are measurements, not expectations: test_interchanges.py checks what holds today and that the replay runs.
"""
import ast
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dnp  # noqa: E402
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import mapgeom  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
SITES = {       # name: (GitHub issue, what was reported)
    "a61_928": (928, "A61 bridge near Ahrweiler: a hole in the ground where the bridge over the road under it is"),
    "bologna_929": (929, "Autostrada overpasses round Bologna: crossing roads at the same height"),
    "prague_924": (924, "Prague: a bridge not generated correctly"),
    "a20_921": (921, "A20 near Rostock: a motorway bridge far too steep for its length"),
    "castlegar_939": (939, "Castlegar BC, after the Kinnaird bridge: an underpass too low for a car"),
    "silistra_936": (936, "Aydemir, Silistra: a weird impassable bridge"),
    "italy_967": (967, "Italian freeway near Orte: an overpass at the same level as the road under it"),
    "m1_ireland_960": (960, "M1 Newry to Dublin: bridges too low, clipping into the road below"),
    "perth_915": (915, "Tonkin Highway x Morley Drive, Perth: the underpass and the overpass both at ground level"),
}
RADIUS_M = 400.0
STEP_M = 4.0
MAJOR = {"motorway", "trunk"}
END_M = 2.0             # m: a road met this near a deck's end is the junction it starts from
LOW_GAP_M = 4.5         # m of road surface over the road under it: less leaves no headroom under any real deck


def unpack_heights(first, steps):
    q = np.concatenate([np.asarray(first, np.int64)[:, None], np.asarray(steps, np.int64)], axis=1)
    return np.cumsum(q, axis=1) / 100.0


def _bilinear(axis, z, x, y):
    """z (rows y, columns x on the same axis) at x, y; inside: mask of the points within the grid."""
    a0, step, n = float(axis[0]), float(axis[1] - axis[0]), len(axis)
    fx, fy = (x - a0) / step, (y - a0) / step
    inside = (fx >= 0) & (fx <= n - 1) & (fy >= 0) & (fy <= n - 1)
    fx, fy = np.clip(fx, 0, n - 1), np.clip(fy, 0, n - 1)
    i, j = np.minimum(fx.astype(np.int64), n - 2), np.minimum(fy.astype(np.int64), n - 2)
    u, v = fx - i, fy - j
    out = (z[j, i] * (1 - u) * (1 - v) + z[j, i + 1] * u * (1 - v) + z[j + 1, i] * (1 - u) * v
           + z[j + 1, i + 1] * u * v)
    return out, inside


STRUCTURED = (".attr.osm_lane_parts", ".attr.lane_section_profile")     # object columns the capture keeps as text (Python literals: no pickles)


def _structured(key, values):
    """A fixture column as Features holds it: a STRUCTURED one's text read back ("" -> None)."""
    if not key.endswith(STRUCTURED):
        return values
    out = np.empty(len(values), object)
    out[:] = [ast.literal_eval(str(v)) if str(v) else None for v in values]
    return out


def _keep_near(data, prefix, frame, keep_m):
    """The features of one cell's arrays (prefix) within keep_m of the frame's origin (their bounding box), the rest
    left out: the planner plans whole cells, and a replay measured near its place need not plan a city 1 km away."""
    ids = data[prefix + "ids"]
    part_off, ring_off, coord_off = (np.asarray(data[prefix + k], np.int64) for k in ("part_off", "ring_off", "coord_off"))
    lonlat = np.asarray(data[prefix + "lonlat"], np.float64)
    e, n, _ = frame.geodetic_to_enu(lonlat[:, 1], lonlat[:, 0], 0.0)
    xy = np.c_[e, n]
    keep = []
    for i in range(len(ids)):
        c0, c1 = coord_off[ring_off[part_off[i]]], coord_off[ring_off[part_off[i + 1]]]
        if c1 > c0:
            lo, hi = xy[c0:c1].min(axis=0), xy[c0:c1].max(axis=0)
            if dnp.hypot(*np.maximum(np.maximum(lo, -hi), 0.0)) <= keep_m:
                keep.append(i)
    new_part, new_ring, new_coord, coords = [0], [0], [0], []
    for i in keep:
        for p in range(part_off[i], part_off[i + 1]):
            for r in range(ring_off[p], ring_off[p + 1]):
                coords.append(lonlat[coord_off[r]:coord_off[r + 1]])
                new_coord.append(new_coord[-1] + len(coords[-1]))
            new_ring.append(len(new_coord) - 1)
        new_part.append(len(new_ring) - 1)
    index = np.full(len(ids), -1, np.int64)
    index[keep] = np.arange(len(keep))
    out = dict(data)
    out.update({prefix + "part_off": np.array(new_part, np.int64), prefix + "ring_off": np.array(new_ring, np.int64),
                prefix + "coord_off": np.array(new_coord, np.int64),
                prefix + "lonlat": np.concatenate(coords) if coords else np.zeros((0, 2))})
    for k, v in data.items():
        rest = k[len(prefix):] if k.startswith(prefix) else None
        if rest in ("ids", "kind", "own", "clipped") or (rest or "").startswith("attr."):
            out[k] = v[keep]
        elif (rest or "").startswith("rule.") and rest.endswith(".feature"):
            rows = index[np.asarray(v, np.int64)] >= 0
            base = k[:-len("feature")]
            for col in ("feature", "value", "start", "end"):
                out[base + col] = data[base + col][rows]
            out[k] = index[np.asarray(v, np.int64)[rows]].astype(v.dtype)
    return out


def load(name, prefix="interchange", keep_m=None):
    """SimpleNamespace(frame, cells {key str: Features}, sample(lat, lon) -> raw z, origin (lat, lon)) of
    fixtures/PREFIX_NAME.npz; keep_m: only the roads within this of the place (_keep_near)."""
    with np.load(FIXTURES / f"{prefix}_{name}.npz", allow_pickle=False) as a:
        data = {k: _structured(k, a[k]) for k in a.files}
    lat, lon = (float(v) for v in data["origin"])
    frame = geodesy.EnuFrame(lat, lon)
    cells = {}
    for i, key in enumerate(data["cells"]):
        if f"cell{i}.ids" not in data:
            continue
        if keep_m is not None:
            data = _keep_near(data, f"cell{i}.", frame, keep_m)
        k = mapcells.CellKey(*(int(v) for v in str(key).split("/")))
        clat, clon = k.center()
        cells[str(k)] = mapgeom.Features.from_arrays("roads", data, f"cell{i}.").localize(geodesy.EnuFrame(clat, clon))
    fine = (data["fine_axis"], unpack_heights(data["fine_first"], data["fine_steps"]))
    coarse = (data["coarse_axis"], unpack_heights(data["coarse_first"], data["coarse_steps"]))

    def sample(la, lo, *_zoom):
        a, b = np.broadcast_arrays(np.asarray(la, np.float64), np.asarray(lo, np.float64))
        e, n, _ = frame.geodetic_to_enu(a.ravel(), b.ravel(), 0.0)
        e, n = np.asarray(e), np.asarray(n)
        z, _ = _bilinear(*coarse, e, n)
        zf, inside = _bilinear(*fine, e, n)
        z = np.where(inside, zf, z)
        return z.reshape(a.shape)
    return SimpleNamespace(frame=frame, cells=cells, sample=sample, origin=(lat, lon))


def _crossings(a, b):
    """[(t along a's arc m, point (2,))] where polylines a and b (k, 2) cross."""
    out = []
    sa = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(a, axis=0).T))]
    for i in range(len(a) - 1):
        p, r = a[i], a[i + 1] - a[i]
        q, s = b[:-1], b[1:] - b[:-1]
        rxs = r[0] * s[:, 1] - r[1] * s[:, 0]
        ok = np.abs(rxs) > 1e-12
        qp = q - p
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (qp[:, 0] * s[:, 1] - qp[:, 1] * s[:, 0]) / rxs
            u = (qp[:, 0] * r[1] - qp[:, 1] * r[0]) / rxs
        for k in np.flatnonzero(ok & (t >= 0) & (t <= 1) & (u >= 0) & (u <= 1)):
            out.append((float(sa[i] + t[k] * dnp.hypot(*r)), p + t[k] * r))
    return out


def measure(name):
    """The site's deck measurements (the module's docstring) with this process's BNE_INTERCHANGE."""
    import grading
    import revisit
    import roads
    with mock.patch.object(revisit, "ON", False):
        site = load(name)
        md = SimpleNamespace(cell=lambda key, **kw: SimpleNamespace(layers={"roads": site.cells.get(str(key))}))
        store = grading.PlanStore(md, site.sample)
        store.frame_of = lambda region: site.frame
        grader = grading.Grader(site.frame, md, site.sample, store=store)

        def elev(la, lo):
            return grader.apply(la, lo, site.sample(la, lo), grading.ROAD_LIFT)
        bridges = roads.BridgeProfiles(site.frame, lambda k: site.cells.get(str(k)), elev)
        home = mapcells.cell_of(*site.origin)
        near = [site.cells[k] for k in sorted(site.cells)
                if k == str(home) or _cell_near(k, site, RADIUS_M)]
        R = RADIUS_M
        parts = bridges.parts(near, (-R, -R, R, R))
        ground = []                                 # ground roads near the place, in the frame
        for f in near:
            specs = roads.road_specs(f)
            for i, sp in enumerate(specs):
                if sp is None:
                    continue
                for ring in roads.ground_parts(f, i):
                    e, n = mapgeom.enu_xy(f, site.frame, ring)
                    xy = np.c_[e, n]
                    if len(xy) >= 2 and np.abs(xy).min(axis=1).min() < R + 200:
                        ground.append((str(f.ids[i]), sp.cls, xy))
        decks, grades, jumps, gaps, low = 0, [], [], [], []
        for fid, spec, xy, z, _lon in parts:
            xy = np.asarray(xy, np.float64)[:, :2]
            if dnp.hypot(xy[:, 0], xy[:, 1]).min() > R:
                continue
            decks += 1
            s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
            if s[-1] > STEP_M:
                ss = np.arange(0.0, s[-1], STEP_M)
                zz = np.interp(ss, s, z)
                g = np.diff(zz) / STEP_M
                if len(g):
                    grades.append(float(np.abs(g).max()))
                if len(g) > 1:
                    jumps.append(float(np.abs(np.diff(g)).max()))
            for gid, gcls, gxy in ground:
                if gid == fid:
                    continue
                for t, p in _crossings(xy, gxy):
                    if min(t, s[-1] - t) < END_M:       # the deck's end on a road: a junction, not a crossing over it
                        continue
                    zd = float(np.interp(t, s, z))
                    la, lo, _ = site.frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
                    zg = float(elev(la, lo)[0])
                    gap = zd - zg
                    gaps.append(gap)
                    if gap < LOW_GAP_M:
                        low.append({"deck": fid, "deck_class": spec.cls, "under": gid, "under_class": gcls,
                                    "gap_m": round(gap, 2), "at": [round(float(p[0]), 1), round(float(p[1]), 1)]})
        # the motorways' and trunks' own ground near the place: their graded profile (GitHub #921 is a ground stretch)
        road_grades = []
        for gid, gcls, gxy in ground:
            if gcls not in MAJOR:
                continue
            s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(gxy, axis=0).T))]
            if s[-1] < 3 * STEP_M:
                continue
            ss = np.arange(0.0, s[-1], STEP_M)
            px, py = np.interp(ss, s, gxy[:, 0]), np.interp(ss, s, gxy[:, 1])
            keep = dnp.hypot(px, py) <= R
            if keep.sum() < 3:
                continue
            la, lo, _ = site.frame.enu_to_geodetic(px, py, 0.0)
            zz = np.asarray(elev(la, lo), np.float64)
            g = np.diff(zz) / STEP_M
            g = g[keep[1:] & keep[:-1]]
            if len(g):
                road_grades.append((float(np.abs(g).max()), gid))
    worst = max(road_grades) if road_grades else None
    return {"site": name, "issue": SITES.get(name, (None,))[0], "interchange": _interchange_on(),
            "road_max_grade": round(worst[0], 4) if worst else None, "road_max_grade_id": worst[1] if worst else None,
            "decks": decks,
            "max_grade": round(max(grades), 4) if grades else None,
            "max_grade_jump": round(max(jumps), 4) if jumps else None,
            "min_gap": round(min(gaps), 2) if gaps else None,
            "low": [dict(t) for t in dict.fromkeys(tuple((k, tuple(v) if isinstance(v, list) else v)
                                                         for k, v in x.items()) for x in low)]}


def _cell_near(k, site, m):
    key = mapcells.CellKey(*(int(v) for v in k.split("/")))
    lo0, la0, lo1, la1 = (float(v) for v in key.bounds())      # (lon0, lat0, lon1, lat1)
    lat, lon = site.origin
    dlat = max(la0 - lat, 0.0, lat - la1)
    dlon = max(lo0 - lon, 0.0, lon - lo1)
    d = dnp.hypot(dlat * 111320.0, dlon * 111320.0 * dnp.cos(np.radians(lat)))
    return d <= m


def _interchange_on():
    import crossings
    return bool(crossings.ON)


if __name__ == "__main__":
    for nm in sys.argv[1:] or sorted(SITES):
        print(json.dumps(measure(nm)), flush=True)
