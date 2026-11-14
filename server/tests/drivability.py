"""Drivability of the roads round reported places: the heights a car's wheels meet, measured offline on the replays
(fixtures/drive_*.npz and fixtures/interchange_*.npz, tools/capture_interchange_fixture.py; interchanges.load).

    python server/tests/drivability.py [NAME ...]             # each site's score against the baseline's
    python server/tests/drivability.py --baseline [NAME ...]  # the baseline's scores, without measuring
    python server/tests/drivability.py --json [NAME ...]      # one JSON line per site
    python server/tests/drivability.py --update [NAME ...]    # write those sites' measurements to the baseline
    (--jobs N or BNE_DRIVE_JOBS: sites measured at once, each in its own process; default: one per CPU)

measure(name) reports, for the roads within RADIUS_M of the place:
  steps   metres of wheel track with a step over its limit
  parts   {"<surface>:<feature id>": {metric: value, metric_at: [x, y], "class"}} of every part with a metric over its
          limit (METRICS); x / y: m east / north of the place where that metric is largest
Metrics (1 m samples z):
  step_m  |z(s - 1) - 2 z(s) + z(s + 1)| on every wheel track: a ledge or a kink, not a hill (a steady 30 % grade has
          none, a 1 m ledge is a 1 m step)
  grade   |z(s + 4) - z(s)| / 4 on a major road's centre line: a ramp no car climbs
  lift_g  v^2 |z(s - 5) - 2 z(s) + z(s + 5)| / 25 / g on a major road's centre line, v its class's speed (SPEED_MS):
          the vertical acceleration of a car at that speed, a crest that throws it or a dip that bottoms it out
Surfaces: a road on the ground on the graded physics ground (Grader.apply with ROAD_LIFT, as the terrain blocks) along
three wheel tracks; a bridge deck or a tunnel floor on its profile (centre line) with LEAD_M of what joins it before
and after: the deck or tunnel that meets it there, else the road on the ground; a deck where the ground is higher on
that ground (the car meets the higher surface).

score(): how far a site's roads are from drivable, one number: per bad part and metric log2(value / limit), so halving
a defect takes a point off and fixing it takes all of its points; 0 when every road is within the limits.

test_drivability.py holds every site to fixtures/drivability_baseline.json (steps and each part's metrics): no part may
get worse and no part may become bad, and an improvement must be written back (--update) so that it cannot get worse
again. The fixtures never change, so a part's id names the same road in every run.
"""
import argparse
import json
import math
from concurrent.futures import ProcessPoolExecutor
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import interchanges  # noqa: E402
import dmath  # noqa: E402
import dnp  # noqa: E402
import mapgeom  # noqa: E402

BASELINE = Path(__file__).parent / "fixtures" / "drivability_baseline.json"
SITES = {       # name: (fixture prefix, what was reported)
    "torbole_adige_tunnel": ("drive", "Torbole: the mountainside reaching onto the street over the Adige-Garda tunnel"),
    "riva_pontera_tunnel": ("drive", "Riva del Garda, Pontera nuova: a steep hill after a small tunnel"),
    "riva_gardesana": ("drive", "Riva del Garda, Via Gardesana: something on top of the road"),
    "casagranda_bridge": ("drive", "Galleria Casagranda: a large incline between the tunnels"),
    "casagranda_portal": ("drive", "Galleria Casagranda: a large incline before the tunnel"),
    "a5_underpass": ("drive", "A5 near Heidelberg: a gap in the autobahn over an underpass"),
    "compiegne_roundabout": ("drive", "Compiègne: a roundabout the railway crosses twice on bridges; every version a different mess"),
    "perth_mount_henry_520": ("drive", "GitHub #520, Perth: Mount Henry Bridge's crests launch the car"),
    "tamar_bridge_1112": ("drive", "GitHub #1112, Tamar Bridge: the ends droop, a drop and a bump onto the deck"),
    "vienna_suedtiroler_1083": ("drive", "GitHub #1083, Vienna: Südtiroler Platz falls into the trench of its lid"),
    "vienna_s1_rautenweg_1083": ("drive", "GitHub #1083, Vienna: Rautenweg over the S1's cover section"),
    "vienna_s1_breitenleer_1083": ("drive", "GitHub #1083, Vienna: Breitenleer Straße over the S1's cover section"),
    "vienna_a23_1083": ("drive", "GitHub #1083, Vienna: the A23 mapped on the ground over Erzherzog-Karl-Straße"),
    "vienna_kennedy_1083": ("drive", "GitHub #1083, Vienna: Kennedybrücke mapped on the ground over its underpass"),
    "vienna_mooslacken_1083": ("drive", "GitHub #1083, Vienna: Alois-Mock-Gasse over the Mooslackengasse lid"),
    "paris_maillot_1083": ("drive", "GitHub #1083, Paris: Avenue de Neuilly over the Périphérique at Porte Maillot"),
    "paris_maillot_ax_1083": ("drive", "GitHub #1083, Paris: Rue du Général Anselin over Voie AX at Porte Maillot"),
    "dc_capitol_crossing_1083": ("drive", "GitHub #1083, Washington DC: F Street over I-395 at Capitol Crossing"),
    "boston_bowker": ("drive", "Boston: the Bowker Overpass over Storrow Drive under its 3.2-3.35 m height limits"),
    "boston_storrow_massave": ("drive", "Boston: Storrow and Memorial Drives under Mass Ave, 2.74-3.66 m height limits"),
    **{name: ("interchange", what) for name, (_issue, what) in interchanges.SITES.items()},
}
RADIUS_M = 200.0
KEEP_M = 600.0          # m of roads round a place the replay plans (interchanges.load keep_m): the planner plans whole
                        # cells (1.6 km and more), most of the time a site took; every site but these measured the same
                        # with only these roads as with all of them (within the tolerances), at a third of the time
KEEP_M_SITE = {         # ... these differed (2026-11-11, against the whole capture):
    "m1_ireland_960": 800.0,        # (691x the tolerance at 600 m, the same at 800 m)
    "tamar_bridge_1112": 1600.0,    # (12x at 600 m, 2.2x at 800 m, 1.2x at 1200 m)
    "a61_928": None,                # (144x even at 1600 m: a road there depends on map data farther off)
}
SPEED_MS = {"motorway": 36.0, "trunk": 28.0, "primary": 25.0, "secondary": 22.0}   # 130, 100, 90, 80 km/h
G = 9.81
GRADE_RUN = 4           # m
LIFT_HALF = 5           # m: half the chord of lift_g (shorter: the DEM's metre noise, not the road's shape)
TRACK_IN_M = 0.8        # m from the carriageway's edge to the outer wheel tracks
LEAD_M = 10             # m of the road measured before and after a deck or a tunnel
JOIN_M = 5.0            # m: a deck or tunnel end this near another's end goes on onto it (a deck runs ~3 m past its map end)
JOIN_SAME_M = 10.0      # ... this near for the same road's next part (Casagranda's tunnel ends 5.5 m from its bridge; a
                        # parallel carriageway's twin structure is another feature, 10 m or more off)


def _step(z, cls):
    return np.abs(z[:-2] - 2 * z[1:-1] + z[2:]), 1


def _grade(z, cls):
    return np.abs(z[GRADE_RUN:] - z[:-GRADE_RUN]) / GRADE_RUN, 0


def _lift(z, cls):
    h = LIFT_HALF
    return (SPEED_MS[cls] * SPEED_MS[cls]) * np.abs(z[:-2 * h] - 2 * z[h:-h] + z[2 * h:]) / (h * h) / G, h


# name: (limit, tolerance, f(z, class) -> (values, index of the first value's sample), on wheel tracks / centre only)
METRICS = {
    "step_m": (0.10, 0.02, _step, "tracks"),    # m: a smooth road's vertical curve bends a few mm per metre
    "grade": (0.15, 0.005, _grade, "centre"),   # steeper than any but the rarest real major road
    "lift_g": (0.20, 0.01, _lift, "centre"),    # a motorway crest built to the design rules: about 0.01 g
}


def measure(name, threads=1, approach=None):
    """The site's drivability (the module's docstring). threads: cells planned at once (grading.PLAN_THREADS); approach:
    (east, north) m of a place graded first, as a session arriving from there would (test_drivability: neither may
    change the result)."""
    import grading
    import revisit
    import roads
    import roadsurf
    prefix = SITES[name][0]
    # One planning thread by default. On main (1.9.1-beta) cells planned at once gave Silistra's bridge a 1.9 m step in
    # some runs only; dev gave the same result with 1 and 4 threads, but a race coming back must not make the ratchet
    # flaky: test_drivability checks threads and approach on their own.
    # The photo road-surface verdicts off: they live in the machine's own cache (~/.cache/beamng-earth/roadsurf, written by
    # whatever session last drove there), so with them the replay planned other roads on a machine that had visited a
    # site (2026-11-12: Vienna's and DC's cells got verdicts from a game session; the gate's test_drivability failed on
    # every commit, its steps 0.26 -> 0.34 m at S1 Rautenweg, while BNE_ROADSURF=off runs passed).
    with mock.patch.object(revisit, "ON", False), mock.patch.object(grading, "PLAN_THREADS", threads), \
            mock.patch.object(roadsurf, "ON", False):
        site = interchanges.load(name, prefix, keep_m=KEEP_M_SITE.get(name, KEEP_M))
        md = SimpleNamespace(cell=lambda key, **kw: SimpleNamespace(layers={"roads": site.cells.get(str(key))}),
                             dem=SimpleNamespace(sample=site.sample))
        _hook_cells(site, md, name)
        store = grading.PlanStore(md, site.sample)
        store.frame_of = lambda region: site.frame
        grader = grading.Grader(site.frame, md, site.sample, store=store)
        if approach is not None:
            _grade_around(grader, site.frame, approach)

        def elev(la, lo):
            return grader.apply(la, lo, site.sample(la, lo), grading.ROAD_LIFT)

        def ground(xy):
            la, lo, _ = site.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            return np.asarray(elev(la, lo), np.float64)
        cells = list(site.cells.values())
        box = (-RADIUS_M, -RADIUS_M, RADIUS_M, RADIUS_M)
        tunnels = roads.TunnelProfiles(site.frame, lambda k: site.cells.get(str(k)), elev)
        tunnels.raw = site.sample
        tunnels.ends = lambda la, lo: grader.apply(la, lo, site.sample(la, lo), grading.ROAD_LIFT, roads_only=True)
        report = Report()
        _measure_ground(report, cells, site.frame, ground, roads)
        structures = [s for surface, profiles in
                      (("bridge", roads.BridgeProfiles(site.frame, lambda k: site.cells.get(str(k)), elev)), ("tunnel", tunnels))
                      for fid, spec, xy, z, _lon in profiles.parts(cells, box)
                      for s in [_structure(surface, fid, spec, np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64), ground)]
                      if s is not None]
        for st in structures:
            _measure_structure(report, st, structures, ground)
    return {"site": name, **report.result()}


def _hook_cells(site, md, name):
    """Each replayed roads cell as MapData.cell hands it to the game (mapdata._decoded_layer): its grade separations
    (gradesep.hook: the bridges the map leaves out, at BNE_GRADESEP's mode, the DEM read from the replay's ground) and
    its eight neighbours (gradesep.neighbours_hook: lane flow, a road's home cell, regional classes, the other cells'
    separations). Without them a replay planned other roads than the game builds (2026-11-12: S1 Rautenweg 176.9
    against the game's 212.6). The cell id (_bnge_cell: roads._FACTS_MEM keeps a cell's facts by it) names the site and
    its kept radius: a pool process measures one site after another."""
    import gradesep
    import mapcells
    tag = f"drivability:{name}:{KEEP_M_SITE.get(name, KEEP_M)}"
    for key, f in site.cells.items():
        k = mapcells.CellKey(*(int(v) for v in key.split("/")))
        f.__dict__.update(_bnge_cell=(key, "roads", tag), _bnge_gradesep=gradesep.hook(md, k),
                          _bnge_neighbours=gradesep.neighbours_hook(md, k))


APPROACH_HALF_M = 150.0  # m: the square graded first round an approach point


def _grade_around(grader, frame, at):
    """Grades a square round `at` (east, north m) first, so its cells are planned before the place's."""
    axis = np.arange(-APPROACH_HALF_M, APPROACH_HALF_M + 1, 25.0)
    x, y = np.meshgrid(axis + at[0], axis + at[1])
    la, lo, _ = frame.enu_to_geodetic(x.ravel(), y.ravel(), 0.0)
    grader.apply(la, lo, np.zeros(la.shape))


def measure_all(names, jobs=None):
    """measure() of each site, yielded in order; up to `jobs` sites at once, each in its own process (a site plans on
    one thread, so the results are the same at any number of jobs)."""
    names = list(names)
    jobs = min(len(names), jobs or int(os.environ.get("BNE_DRIVE_JOBS") or 0) or os.cpu_count() or 1)
    if jobs <= 1:
        yield from map(measure, names)
        return
    with ProcessPoolExecutor(jobs) as pool:
        yield from pool.map(measure, names)


class Report:
    """Per part the largest value of each metric and where; the metres of wheel track over the step limit."""

    def __init__(self):
        self.steps, self.parts = 0, {}

    def add(self, where, xy, z, inside, centre):
        """A profile: z at xy (1 m apart) along a wheel track (centre: the centre line), inside: which samples lie
        within RADIUS_M."""
        for name, (limit, _tol, fn, on) in METRICS.items():
            if (on == "centre" and not centre) or (fn is not _step and where["class"] not in SPEED_MS):
                continue
            values, first = fn(z, where["class"])       # (empty when the profile is shorter than the metric's window)
            values = np.where(inside[first:first + len(values)] & np.isfinite(values), values, 0.0)
            if not len(values):
                continue
            if fn is _step:
                self.steps += int((values > limit).sum())
            k = int(np.argmax(values))
            part = self.parts.setdefault(f"{where['surface']}:{where['part']}", {"class": where["class"]})
            if values[k] > part.get(name, 0.0):
                part[name] = float(values[k])
                part[name + "_at"] = [round(float(xy[first + k][0]), 1), round(float(xy[first + k][1]), 1)]

    def result(self):
        bad = {}
        for key, part in sorted(self.parts.items()):
            if any(part.get(m, 0.0) > limit for m, (limit, *_r) in METRICS.items()):
                bad[key] = {k: round(v, 4) if isinstance(v, float) else v for k, v in part.items()}
        return {"steps": self.steps, "parts": bad}


def _resample(xy):
    """Polyline xy every metre ((k, 2)), None when shorter than 3 m."""
    s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
    if s[-1] < 3.0:
        return None
    ss = np.arange(0.0, s[-1], 1.0)
    return np.c_[np.interp(ss, s, xy[:, 0]), np.interp(ss, s, xy[:, 1])]


def _inside(xy):
    return dnp.hypot(xy[:, 0], xy[:, 1]) <= RADIUS_M


def _measure_ground(report, cells, frame, ground, roads):
    """Every road on the ground near the place along three wheel tracks."""
    seen = set()
    for feats in cells:
        for i, spec in enumerate(roads.road_specs(feats)):
            if spec is None:
                continue
            fid = str(feats.ids[i])
            for j, ring in enumerate(roads.ground_parts(feats, i)):
                if (fid, j) in seen:
                    continue
                seen.add((fid, j))
                e, n = mapgeom.enu_xy(feats, frame, ring)
                centre = _resample(np.c_[e, n])
                if centre is None or dnp.hypot(centre[:, 0], centre[:, 1]).min() > RADIUS_M + spec.width:
                    continue
                inside = _inside(centre)
                d = np.gradient(centre, axis=0)
                normal = np.c_[-d[:, 1], d[:, 0]] / np.maximum(dnp.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
                side = max(spec.width / 2 - TRACK_IN_M, 0.0)
                where = {"part": fid, "class": spec.cls, "surface": "ground"}
                for off in (-side, 0.0, side) if side else (0.0,):
                    p = centre + off * normal
                    report.add(where, p, ground(p), inside, centre=off == 0.0)


def _structure(surface, fid, spec, xy, z, ground):
    """A bridge deck or a tunnel floor every metre: SimpleNamespace(surface, fid, spec, xy, z), z the surface a car meets
    (a deck where the ground is higher: that ground). None when shorter than 3 m."""
    centre = _resample(xy)
    if centre is None:
        return None
    s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
    arc = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(centre, axis=0).T))]
    zz = np.interp(arc, s, z)
    if surface == "bridge":
        zz = np.maximum(zz, ground(centre))
    return SimpleNamespace(surface=surface, fid=fid, spec=spec, xy=centre, z=zz)


def _feature(fid):
    """The map feature of a part id: a tunnel's sections are "<feature>.<chain>#<k>"."""
    return re.split(r"[.#]", fid, maxsplit=1)[0]


def _lead(st, start, structures, ground):
    """(xy, z) of LEAD_M before (start) or after st, ordered along the drive: on the nearest deck or tunnel that joins st
    there and goes on outwards, else on the ground straight on."""
    end = st.xy[0] if start else st.xy[-1]
    d = st.xy[0] - st.xy[1] if start else st.xy[-1] - st.xy[-2]
    out = d / dnp.hypot(*d)                                  # (outwards from st's end)
    feature = _feature(st.fid)
    best = None
    for other in structures:
        if other is st:
            continue
        reach = JOIN_SAME_M if _feature(other.fid) == feature else JOIN_M
        for oxy, oz in ((other.xy, other.z), (other.xy[::-1], other.z[::-1])):     # (from its start, from its end)
            gap = float(dnp.hypot(*(oxy[0] - end)))
            ahead = dnp.matmul(oxy - end, out) > 0       # (not back over st: a fork's other deck, a deck past its map end)
            if gap <= reach and ahead.any() and (best is None or gap < best[0]):
                best = (gap, oxy[ahead][:LEAD_M], oz[ahead][:LEAD_M])
    if best is not None:
        xy, z = best[1], best[2]
    else:
        xy = end + np.arange(1, LEAD_M + 1)[:, None] * out
        z = ground(xy)
    return (xy[::-1], z[::-1]) if start else (xy, z)        # (from the joined end outwards: reversed before st)


def _every_metre(xy, z):
    """xy and z again every metre along the path: a joined structure's end can be several metres from st's (the gap
    between them is a slope over its length, not a ledge over one metre)."""
    s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
    ss = np.arange(0.0, s[-1] + 1e-9, 1.0)
    return np.c_[np.interp(ss, s, xy[:, 0]), np.interp(ss, s, xy[:, 1])], np.interp(ss, s, z)


def _measure_structure(report, st, structures, ground):
    """A bridge deck or a tunnel floor on its profile, with LEAD_M of what joins it before and after."""
    (bxy, bz), (axy, az) = _lead(st, True, structures, ground), _lead(st, False, structures, ground)
    path, profile = _every_metre(np.concatenate([bxy, st.xy, axy]), np.concatenate([bz, st.z, az]))
    report.add({"part": st.fid, "class": st.spec.cls, "surface": st.surface}, path, profile, _inside(path), centre=True)


def score(entry):
    """The module's docstring; entry: a measurement or a baseline entry."""
    return sum(dmath.log2(part[m] / limit) for part in entry["parts"].values()
               for m, (limit, *_r) in METRICS.items() if part.get(m, 0.0) > limit)


def _bad(entry):
    return sum(part.get(m, 0.0) > limit for part in entry["parts"].values() for m, (limit, *_r) in METRICS.items())


def score_table(rows):
    """Lines of a table of (site, entry, baseline entry or None) and their total."""
    lines = [f"{'site':28} {'score':>7} {'baseline':>9} {'change':>7} {'steps':>6} {'bad':>4}"]
    total, total_was = 0.0, 0.0
    for name, entry, was in rows:
        now = score(entry)
        total += now
        if was is None:
            lines.append(f"{name:28} {now:7.1f} {'':>9} {'':>7} {entry['steps']:6} {_bad(entry):4}")
            continue
        total_was += score(was)
        lines.append(f"{name:28} {now:7.1f} {score(was):9.1f} {now - score(was):+7.1f} {entry['steps']:6} {_bad(entry):4}")
    lines.append(f"{'total':28} {total:7.1f}" + (f" {total_was:9.1f} {total - total_was:+7.1f}" if total_was else ""))
    return lines


def load_baseline():
    return json.loads(BASELINE.read_text()) if BASELINE.exists() else {}


def baseline_entry(result):
    """What the baseline keeps of a measurement: the steps and each bad part's metrics."""
    return {"steps": result["steps"],
            "parts": {k: {m: p.get(m, 0.0) for m in METRICS} for k, p in result["parts"].items()}}


def write_baseline(results):
    base = load_baseline()
    for r in results:
        base[r["site"]] = baseline_entry(r)
    BASELINE.write_text(json.dumps(dict(sorted(base.items())), indent=1) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="sites (default: all)")
    ap.add_argument("--update", action="store_true", help="write the measurements to the baseline")
    ap.add_argument("--baseline", action="store_true", help="the baseline's scores, without measuring")
    ap.add_argument("--json", action="store_true", help="one JSON line per site instead of the scores")
    ap.add_argument("--jobs", type=int, help="sites measured at once (default: BNE_DRIVE_JOBS, else one per CPU)")
    a = ap.parse_args()
    unknown = sorted(set(a.names) - set(SITES))
    if unknown:
        ap.error(f"unknown sites: {', '.join(unknown)} (known: {', '.join(SITES)})")
    names, baseline = a.names or list(SITES), load_baseline()
    if a.baseline:
        print("\n".join(score_table((n, baseline[n], None) for n in names if n in baseline)))
        return
    results = []
    for result in measure_all(names, a.jobs):
        results.append(result)
        if a.json:
            print(json.dumps(result), flush=True)
    if not a.json:
        print("\n".join(score_table((r["site"], r, baseline.get(r["site"])) for r in results)))
    if a.update:
        write_baseline(results)


if __name__ == "__main__":
    main()
