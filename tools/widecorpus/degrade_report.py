"""The degradation test's tables (degrade_run.py --report): per step and per road class, median and p90 errors against
the full-data build, the corpus checks' defect counts and the build times; report.md and report.json in the run."""
import math
from collections import Counter, defaultdict

import numpy as np

from common import read, save
import degrade
import degrade_metrics as dm

ROAD_COLS = (("width_med", "width p50 m"), ("width_p90", "p90"), ("width_mapped_med", "where D0 had a lane count: p50"),
             ("width_mapped_p90", "p90"), ("lanes_exact", "lanes exact"), ("lanes_exact_mapped", "where D0 had a lane count"),
             ("mapped_share", "share with a lane count"),
             ("edge_med", "edge p50 m"), ("edge_p90", "p90"), ("drawn_width_med", "drawn width p50 m"),
             ("drawn_width_p90", "p90"), ("surface_lost", "surface lost"))
VERT_COLS = (("z_med", "height p50 m"), ("z_p90", "p90"), ("grade_med", "grade p50 %"), ("grade_p90", "p90"),
             ("ride_med", "ride p50 g"), ("ride_p90", "p90"))


def _fmt(v, pct=False):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "-"
    if pct:
        return f"{100 * v:.1f} %"
    return f"{v:.2f}" if abs(v) < 100 else f"{v:.0f}"


def collect(run, ps, steps):
    """Pooled rows per step, per-place summaries, building rows, defect pairs, times."""
    pooled = {s: defaultdict(list) for s in steps}
    blds = {s: defaultdict(list) for s in steps}
    pairs = {s: [] for s in steps}
    times = {s: [] for s in steps}
    per_place = {}
    done = []
    excluded = {s: [] for s in steps}
    for p in ps:
        out = run / p["id"]
        if not (out / "D0.json").is_file():
            continue
        ok = all((out / f"rows_{s}.npz").is_file() for s in steps if s != "D0")
        if not ok:
            continue
        done.append(p["id"])
        per_place[p["id"]] = {}
        faults0 = _faults(out / "D0.log")
        for s in steps:
            row = read(out / f"{s}.json")
            if s != "D0" and _faults(out / f"{s}.log") > faults0:
                # (inputs the snapshot lacked for this step, a map cell read from a stand-in: its build is not the
                # step's data removal alone)
                excluded[s].append(p["id"])
                continue
            times[s].append((row.get("build_wall_s"), row.get("measure_s")))
            if s == "D0":
                if (out / "rows_D0.npz").is_file():
                    r = dm.load_rows(out / "rows_D0.npz")
                    for k, v in r.items():
                        pooled[s][k].append(v)
                continue
            r = _with_areas(dm.load_rows(out / f"rows_{s}.npz"))
            for k, v in r.items():
                pooled[s][k].append(v)
            per_place[p["id"]][s] = dm.summarize_roads(r).get("all", {})
            b = read(out / f"bld_{s}.json")
            for k, v in b.items():
                blds[s][k].append(np.asarray(v))
            pairs[s].append(read(out / f"cmp_{s}.json"))
    cat = lambda d: {k: np.concatenate(v) for k, v in d.items() if v}     # noqa: E731
    return done, {s: cat(pooled[s]) for s in steps}, {s: cat(blds[s]) for s in steps}, pairs, times, per_place, excluded


def _with_areas(r):
    """A place's rows with the mapped-area columns (NaN, off) where it has no mapped road areas: pooled columns of one
    length."""
    n = len(r["cls"])
    if "La" not in r:
        r = dict(r, La=np.full(n, np.nan), Ra=np.full(n, np.nan), area_centre=np.zeros(n, bool))
    return r


def _faults(log):
    """Map data the offline build could not read (a cell's layer from a stand-in), from its log. (The tiles'
    source_faults count optional offline refusals too: the corpus's own default builds have them by the thousand.)"""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if "tiles failed" in line or "unreachable" in line)


def defect_summary(pairs):
    """Summed per-check changes against D0 (worse, better), places with a hold alert and the commonest alerts."""
    worse, better = Counter(), Counter()
    alerted = 0
    kinds = Counter()
    for pr in pairs:
        for k, v in pr["delta"].items():
            if k in ("build_seconds",) or k.endswith("_max_g") or k.endswith("_max_m") or k in dm_COVERAGE:
                continue
            if v > 0:
                worse[k] += v
            elif v < 0:
                better[k] -= v
        a = {tuple(x)[1] for x in pr.get("alerts", [])}
        if a:
            alerted += 1
        kinds.update(a)
    return dict(places=len(pairs), places_alerted=alerted, alerts=dict(kinds.most_common()),
                worse={k: round(v, 3) for k, v in worse.most_common()},
                better={k: round(v, 3) for k, v in better.most_common()})


dm_COVERAGE = {"lane_samples", "pavement_samples", "clearance_samples", "deck_ends_sampled", "outline_vertices",
               "triangles", "draw_calls", "kerb_faces", "source_faults", "lidar_waits"}


def _clean(v):
    """NaN (no observations) as None: JSON without NaN."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, (float, np.floating)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, np.integer):
        return int(v)
    return v


def write(run, ps, steps):
    done, pooled, blds, pairs, times, per_place, excluded = collect(run, ps, steps)
    report = dict(places=done, steps={s: degrade.NAMES[s] for s in steps}, roads={}, buildings={}, defects={},
                  times={}, areas={}, per_place=per_place, excluded=excluded)
    for s in steps:
        r = pooled[s]
        if s != "D0" and r:
            report["roads"][s] = dm.summarize_roads(r)
        elif s == "D0" and r and "La" in r:
            report["areas"]["D0"] = dm.summarize_roads(r)
        if s != "D0" and blds[s]:
            report["buildings"][s] = dm.summarize_buildings(blds[s])
        if s != "D0":
            report["defects"][s] = defect_summary(pairs[s])
        wall = [t[0] for t in times[s] if t[0] is not None]
        meas = [t[1] for t in times[s] if t[1] is not None]
        report["times"][s] = dict(builds=len(wall), wall_med_s=dm.q(wall, 50), wall_p90_s=dm.q(wall, 90),
                                  wall_sum_s=float(sum(wall)), measure_med_s=dm.q(meas, 50))
    report = _clean(report)
    save(run / "report.json", report)
    (run / "report.md").write_text(markdown(report, steps))
    return report


def markdown(rep, steps):
    L = [f"# Degradation test: {len(rep['places'])} places", ""]
    L += ["Errors are against the same place's full-data build (D0), at its stations every 2 m along each drivable "
          "road inside the measured tiles. Width: the resolved carriageway width; lanes: the painted lane count; edge: "
          "each side's drawn surface edge along the normal (stations where the full build's drawn width is not the "
          "road's own left out); surface lost: such stations with no drawn surface in the degraded build.", ""]
    for s in steps:
        if s == "D0" or s not in rep["roads"]:
            continue
        L += [f"## {s}: {rep['steps'][s]}", ""]
        if rep["excluded"].get(s):
            L += [f"Left out (more source faults than D0): {', '.join(rep['excluded'][s])}.", ""]
        hdr = ["class", "km"] + [h for _, h in ROAD_COLS]
        L += ["| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
        for g, v in rep["roads"][s].items():
            L.append("| " + " | ".join([g, f"{v['metres'] / 1000:.1f}"] + [
                _fmt(v.get(k), pct=k in ("lanes_exact", "surface_lost", "lanes_exact_mapped", "mapped_share"))
                for k, _ in ROAD_COLS]) + " |")
        L += ["", "Vertical (drivable surface at the stations):", ""]
        hdr = ["class"] + [h for _, h in VERT_COLS] + ["grade>15 % D0 / step", "ride>0.5 g D0 / step"]
        L += ["| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
        for g, v in rep["roads"][s].items():
            L.append("| " + " | ".join([g] + [_fmt(v.get(k)) for k, _ in VERT_COLS] + [
                f"{v.get('grade15_0', '-')} / {v.get('grade15_1', '-')}",
                f"{v.get('ride05_0', '-')} / {v.get('ride05_1', '-')}"]) + " |")
        a = rep["roads"][s].get("all", {}).get("area_vs")
        if a and a.get("stations"):
            L += ["", f"Against mapped area:highway ({a['stations']} stations): full p50 {_fmt(a['full_med'])} m, "
                      f"p90 {_fmt(a['full_p90'])} m; {s} p50 {_fmt(a.get('deg_med'))} m, p90 {_fmt(a.get('deg_p90'))} m"]
        b = rep["buildings"].get(s)
        if b:
            L += ["", "Buildings (eave height of the drawn building against D0):", "",
                  "| set | buildings | p50 m | p90 m | p50 storeys | p90 storeys | floors exact | roof same |",
                  "|---|---|---|---|---|---|---|---|"]
            for k, v in b.items():
                L.append(f"| {k} | {v['buildings']} | {_fmt(v['err_med_m'])} | {_fmt(v['err_p90_m'])} | "
                         f"{_fmt(v['err_med_storeys'])} | {_fmt(v['err_p90_storeys'])} | "
                         f"{_fmt(v['floors_exact'], True)} | {_fmt(v['roof_same'], True)} |")
        d = rep["defects"].get(s)
        if d:
            top = ", ".join(f"{k} +{v:g}" for k, v in list(d["worse"].items())[:8]) or "none"
            al = ", ".join(f"{k} {v}" for k, v in list(d["alerts"].items())[:8]) or "none"
            L += ["", f"Corpus checks: {d['places_alerted']} of {d['places']} places over a hold threshold "
                      f"(alerts: {al}). Largest summed increases: {top}."]
        t = rep["times"].get(s)
        if t:
            L += ["", f"Build wall time: median {_fmt(t['wall_med_s'])} s, p90 {_fmt(t['wall_p90_s'])} s, "
                      f"{t['wall_sum_s'] / 60:.0f} min over {t['builds']} places."]
        L.append("")
    if "D0" in rep["times"]:
        t = rep["times"]["D0"]
        L += [f"D0 (full data) build wall time: median {_fmt(t['wall_med_s'])} s, p90 {_fmt(t['wall_p90_s'])} s, "
              f"{t['wall_sum_s'] / 60:.0f} min over {t['builds']} places.", ""]
    a = rep["areas"].get("D0", {}).get("all", {}).get("area_vs")
    if a:
        L += [f"Full build against mapped area:highway: {a['stations']} stations, p50 {_fmt(a['full_med'])} m, "
              f"p90 {_fmt(a['full_p90'])} m.", ""]
    return "\n".join(L) + "\n"
