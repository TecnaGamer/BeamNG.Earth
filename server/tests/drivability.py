"""Drivability of the roads round reported places: the heights a car's wheels meet, measured offline on the replays
(fixtures/drive_*.npz and fixtures/interchange_*.npz, tools/capture_interchange_fixture.py; interchanges.load).

    python server/tests/drivability.py [NAME ...]             # one JSON line per site
    python server/tests/drivability.py --update [NAME ...]    # write those sites' measurements to the baseline

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
three wheel tracks; a bridge deck or a tunnel floor on its profile (centre line) with LEAD_M of the road on the ground
before and after it, a deck where the ground is higher on that ground (the car meets the higher surface).

test_drivability.py holds every site to fixtures/drivability_baseline.json (steps and each part's metrics): no part may
get worse and no part may become bad, and an improvement must be written back (--update) so that it cannot get worse
again. The fixtures never change, so a part's id names the same road in every run.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import interchanges  # noqa: E402
import mapgeom  # noqa: E402

BASELINE = Path(__file__).parent / "fixtures" / "drivability_baseline.json"
SITES = {       # name: (fixture prefix, what was reported)
    "torbole_adige_tunnel": ("drive", "Torbole: the mountainside reaching onto the street over the Adige-Garda tunnel"),
    "riva_pontera_tunnel": ("drive", "Riva del Garda, Pontera nuova: a steep hill after a small tunnel"),
    "riva_gardesana": ("drive", "Riva del Garda, Via Gardesana: something on top of the road"),
    "casagranda_bridge": ("drive", "Galleria Casagranda: a large incline between the tunnels"),
    "casagranda_portal": ("drive", "Galleria Casagranda: a large incline before the tunnel"),
    "a5_underpass": ("drive", "A5 near Heidelberg: a gap in the autobahn over an underpass"),
    "mannheim_motorway_bridge": ("drive", "Mannheim: an autobahn bridge with a bump too large for high speeds"),
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
    **{name: ("interchange", what) for name, (_issue, what) in interchanges.SITES.items()},
}
RADIUS_M = 200.0
SPEED_MS = {"motorway": 36.0, "trunk": 28.0, "primary": 25.0, "secondary": 22.0}   # 130, 100, 90, 80 km/h
G = 9.81
GRADE_RUN = 4           # m
LIFT_HALF = 5           # m: half the chord of lift_g (shorter: the DEM's metre noise, not the road's shape)
TRACK_IN_M = 0.8        # m from the carriageway's edge to the outer wheel tracks
LEAD_M = 10             # m of the road on the ground measured before and after a deck or a tunnel


def _step(z, cls):
    return np.abs(z[:-2] - 2 * z[1:-1] + z[2:]), 1


def _grade(z, cls):
    return np.abs(z[GRADE_RUN:] - z[:-GRADE_RUN]) / GRADE_RUN, 0


def _lift(z, cls):
    h = LIFT_HALF
    return SPEED_MS[cls] ** 2 * np.abs(z[:-2 * h] - 2 * z[h:-h] + z[2 * h:]) / (h * h) / G, h


# name: (limit, tolerance, f(z, class) -> (values, index of the first value's sample), on wheel tracks / centre only)
METRICS = {
    "step_m": (0.10, 0.02, _step, "tracks"),    # m: a smooth road's vertical curve bends a few mm per metre
    "grade": (0.15, 0.005, _grade, "centre"),   # steeper than any but the rarest real major road
    "lift_g": (0.20, 0.01, _lift, "centre"),    # a motorway crest built to the design rules: about 0.01 g
}


def measure(name):
    """The site's drivability (the module's docstring)."""
    import grading
    import revisit
    import roads
    prefix = SITES[name][0]
    # one planning thread: cells planned at once give different heights in some places from run to run (Silistra's
    # bridge: clean in some runs, a 1.9 m step in others), and a measurement that moves cannot hold anything
    with mock.patch.object(revisit, "ON", False), mock.patch.object(grading, "PLAN_THREADS", 1):
        site = interchanges.load(name, prefix)
        md = SimpleNamespace(cell=lambda key, **kw: SimpleNamespace(layers={"roads": site.cells.get(str(key))}))
        store = grading.PlanStore(md, site.sample)
        store.frame_of = lambda region: site.frame
        grader = grading.Grader(site.frame, md, site.sample, store=store)

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
        for surface, profiles in (("bridge", roads.BridgeProfiles(site.frame, lambda k: site.cells.get(str(k)), elev)),
                                  ("tunnel", tunnels)):
            for fid, spec, xy, z, _lon in profiles.parts(cells, box):
                _measure_structure(report, surface, fid, spec, np.asarray(xy, np.float64)[:, :2],
                                   np.asarray(z, np.float64), ground)
    return {"site": name, **report.result()}


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
            values, first = fn(z, where["class"]) if len(z) > 2 * LIFT_HALF else (np.zeros(0), 0)
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
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if s[-1] < 3.0:
        return None
    ss = np.arange(0.0, s[-1], 1.0)
    return np.c_[np.interp(ss, s, xy[:, 0]), np.interp(ss, s, xy[:, 1])]


def _inside(xy):
    return np.hypot(xy[:, 0], xy[:, 1]) <= RADIUS_M


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
                if centre is None or np.hypot(centre[:, 0], centre[:, 1]).min() > RADIUS_M + spec.width:
                    continue
                inside = _inside(centre)
                d = np.gradient(centre, axis=0)
                normal = np.c_[-d[:, 1], d[:, 0]] / np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
                side = max(spec.width / 2 - TRACK_IN_M, 0.0)
                where = {"part": fid, "class": spec.cls, "surface": "ground"}
                for off in (-side, 0.0, side) if side else (0.0,):
                    p = centre + off * normal
                    report.add(where, p, ground(p), inside, centre=off == 0.0)


def _measure_structure(report, surface, fid, spec, xy, z, ground):
    """A bridge deck or a tunnel floor on its profile, with LEAD_M of ground road before and after it."""
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    centre = _resample(xy)
    if centre is None:
        return
    arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(centre, axis=0).T))]
    zz = np.interp(arc, s, z)
    lead = np.arange(LEAD_M, 0, -1)[:, None]
    d0, d1 = centre[1] - centre[0], centre[-1] - centre[-2]
    before = centre[0] - lead * d0 / np.hypot(*d0)
    after = centre[-1] + lead[::-1] * d1 / np.hypot(*d1)
    path = np.concatenate([before, centre, after])
    zg = ground(path)
    on = zz if surface == "tunnel" else np.maximum(zz, zg[LEAD_M:LEAD_M + len(zz)])
    profile = np.concatenate([zg[:LEAD_M], on, zg[LEAD_M + len(zz):]])
    report.add({"part": fid, "class": spec.cls, "surface": surface}, path, profile, _inside(path), centre=True)


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
    a = ap.parse_args()
    unknown = sorted(set(a.names) - set(SITES))
    if unknown:
        ap.error(f"unknown sites: {', '.join(unknown)} (known: {', '.join(SITES)})")
    results = []
    for name in a.names or SITES:
        results.append(measure(name))
        print(json.dumps(results[-1]), flush=True)
    if a.update:
        write_baseline(results)


if __name__ == "__main__":
    main()
