#!/usr/bin/env python3
"""Offline roadside-elevation JSON and SVG engineering profile.

  python -B tools/roadside_profile.py --output build/roundtop-investigation/profile
  python -B tools/roadside_profile.py --record saved-diagnostic.json --output build/profile

The default uses captured genuine Roundtop inputs and built-world grading plans.
It never creates MapData/Fetcher/server objects or changes active
caches. A saved runtime record preserves that record's actual generation scope.
Plots use matplotlib Agg; these are computed engineering profiles, not engine screenshots.
"""
import argparse
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "server/tests"))


def isolated_record():
    import furniture
    import roadside_debug
    import roads
    from roundtop_fixture import CapturedRoundtop, CapturedRoundtopPlans, ROUND_TOP

    capture = CapturedRoundtop()
    try:
        feats = capture.roads()
        i = list(feats.ids).index(ROUND_TOP)
        frame = capture.frame
        spec = roads.road_specs(feats)[i]
        parts = roads.ground_parts(feats, i)
        context = CapturedRoundtopPlans(capture)
        elev = context.final

        def ground(x, y):
            lat, lon, _ = frame.enu_to_geodetic(x, y, np.zeros(np.shape(x)))
            return np.asarray(frame.geodetic_to_enu(lat, lon, elev(lat, lon))[2])

        road_rows = []
        for part in parts:
            x, y, _ = frame.geodetic_to_enu(part[:, 1], part[:, 0], np.zeros(len(part)))
            z = frame.geodetic_to_enu(part[:, 1], part[:, 0], elev(part[:, 1], part[:, 0]))[2]
            road_rows.append((ROUND_TOP, spec, np.column_stack([x, y, z]), float(part[:, 0].mean())))
        box = (-300., -300., 300., 300.)
        walls = roads.retaining_walls(frame, road_rows, elev, box)
        props = furniture.place(road_rows, [], box, region="na:sparse", ground=ground)
        record = roadside_debug.explain(feats, i, spec, frame, elev=elev, raw_elev=capture.raw,
            walls=walls, props=props, tile={"geographic_source": "14/3418/6133", "isolated_box_enu": box},
            dem_source=capture.manifest["dem"], cache_status="offline_captured_sources")
        record["replay_scope"] = "Captured built-world plans; selected ground-road geometry; no fresh whole-world rebuild or live collision measurement"
        record["cached_plan_provenance"] = context.manifest
        record["limitations"].append("captured production planner context is used; live rendered terrain grid and engine collision are not observed")
        return record
    finally:
        capture.close()


def svg_profile(record, path):
    """Render a standalone engineering profile using standard matplotlib."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    profiles = record.get("profiles", [])
    if not profiles:
        raise ValueError("record has no sampled profile")
    p = max(profiles, key=lambda q: len(q["station_m"]))
    station = np.asarray(p["station_m"], float)
    fig = plt.figure(figsize=(13, 9), constrained_layout=True)
    grid = fig.add_gridspec(2, 2, height_ratios=(1.3, 1.))
    ax = fig.add_subplot(grid[0, :])
    err = fig.add_subplot(grid[1, 0])
    plan = fig.add_subplot(grid[1, 1])
    for name, values, color, style in (
        ("Raw DEM", p.get("raw_z_m"), "#718096", "-"),
        ("Final graded", p["graded_z_m"], "#087e8b", "-"),
        ("Old source chord", p["source_chord_z_m"], "#c53030", "--"),
        ("Drawn terrain grid", p.get("drawn_z_m"), "#6b46c1", ":")):
        if values is not None:
            ax.plot(station, values, style, color=color, label=name, linewidth=1.8)
    ax.set(xlabel="Station along source road (m)", ylabel="Elevation in diagnostic ENU frame (m)")
    ax.set_title(str(record.get("name") or record.get("feature_id")) + " - roadside elevation", loc="left", fontsize=16)
    east, north = np.asarray(p["east_m"]), np.asarray(p["north_m"])
    for n, part in enumerate(profiles):
        plan.plot(part["east_m"], part["north_m"], color="#087e8b", label="Ground road center" if n == 0 else None, linewidth=2)
    for n, wall in enumerate(record.get("walls", [])):
        plan.plot(wall["east_m"], wall["north_m"], color="#d97706", linewidth=3,
                  label="Nearby wall" if n == 0 else None)
    shown_poles = 0
    for n, pole in enumerate(record.get("poles", [])):
        index = int(np.argmin((east-pole["east_m"])**2 + (north-pole["north_m"])**2))
        if np.hypot(east[index]-pole["east_m"], north[index]-pole["north_m"]) < 25.:
            ax.scatter([station[index]], [pole["object_origin_z_m"]], s=25, c="#111827", zorder=5,
                       label="Nearby pole base/origin" if shown_poles == 0 else None)
            shown_poles += 1
        plan.scatter([pole["east_m"]], [pole["north_m"]], s=20, c="#111827", zorder=5,
                     label="Pole base" if n == 0 else None)
    delta = np.asarray(p["source_chord_minus_graded_m"], float)
    err.axhline(0., color="#718096", linewidth=.8)
    err.plot(station, delta, color="#c53030", linewidth=1.8)
    err.set(xlabel="Station (m)", ylabel="Old chord minus final road (m)", title="Sparse-source elevation error")
    plan.set(xlabel="East (m)", ylabel="North (m)", title="Diagnostic plan view")
    plan.set_aspect("equal", adjustable="datalim")
    for axes in (ax, err, plan):
        axes.grid(True, alpha=.22)
    ax.legend(loc="best", ncol=3)
    plan.legend(loc="best")
    fig.suptitle("Longest selected ground part; computed geometry, not live engine/collision measurements", fontsize=10)
    note = str(record.get("replay_scope") or "Runtime diagnostic: provenance and limitations are in the adjacent JSON")
    fig.supxlabel(note, fontsize=8)
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=160)
    plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--record", type=Path, help="saved JSON diagnostic record; default captured built-world Roundtop replay")
    parser.add_argument("--output", type=Path, default=ROOT / "build/roundtop-investigation/profile", help="output prefix for .json and .svg")
    args = parser.parse_args(argv)
    record = json.loads(args.record.read_text(encoding="utf-8-sig")) if args.record else isolated_record()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(json.dumps(record, indent=2, allow_nan=False), encoding="utf-8")
    svg_profile(record, args.output.with_suffix(".svg"))
    print(args.output.with_suffix(".json"))
    print(args.output.with_suffix(".svg"))
    print(args.output.with_suffix(".png"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
