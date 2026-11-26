"""roadscan's validation on builds the corpora already kept (no new builds): does the scanner find the known problems
and stay quiet where there are none?

1. Regression mode on the tunnel corpus (tools/tunnelcorpus, 52 mouths, tiles kept per label): two labels scanned
   and compared issue by issue; the truth is the list of mouths the corpus's audit sums called worse between them
   (a `tcquick` output: "worse ID [(metric, a, b), ...]" lines). Recall: losers with a new or worse major/critical
   issue of the metric's kind in their tiles; quiet: the other mouths without one.
2. Single-state mode on the road corpus (the 66 places of the road rewrite's M5 runs, one label): per place and
   drivability class (double owner, envelope intrusion, blocked opening, short clearance, hand-over lips over
   20 mm) the audit's own sums against the scanner's located issues: the plumbing (scenes, seams, ownership) shown
   to keep the audit's answer, place by place.
3. Known spots: an issue of severity 2+ of the expected kinds within RADIUS_M of each.

    python3 tools/roadscan/validate.py [--cpus N] [--tunnel-base revE --tunnel-new x10 --losers FILE]
                                       [--m5-label D7] [--only tunnel,m5]

Results under ~/bnge-tests/roadscan/validate/ (validation.json, validation.txt)."""
import argparse
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[1]
sys.path[:0] = [str(HERE), str(TREE / "tools" / "widecorpus")]
import roadscan  # noqa: E402

T = Path.home() / "bnge-tests"
OUT = roadscan.BASE / "validate"
TUNNEL = T / "tunnelcorpus"
M5 = T / "roadsys" / "m5"
M5RUN = M5 / "fh" / "bnge-tests" / "widecorpus" / "cache" / "m5" / "run"
RADIUS_M = 150.0
# audit metric -> the scanner's issue kinds (the same check, located)
CLASSES = {
    "double_owner_m2": ("double_surface",),
    "envelope_intrusion_m2": ("obstruction", "sidewalk_in_lane", "rail_in_lane"),
    "opening_blocked_m2": ("blocked_opening",),
    "clearance_short_cells": ("low_clearance", "blocked_opening"),
    "handover_lip_over_20mm": ("lip",),
    "handover_lip_over_5mm": ("lip",),
}
# (name, lat, lon, kinds, where): problems seen in game or by the agents
SPOTS = [
    ("Fort McHenry tunnel west approach: portal frame and lids over the open road (playtest 2026-11-26)",
     39.2642, -76.5894, ("obstruction", "tunnel_roofless", "ground_in_structure", "blocked_opening", "low_clearance",
                         "tunnel_mouth_step", "step", "deck_end_step"), "tunnel"),
]


def tiles_of(d):
    f = Path(d) / "tiles.json"
    return [t["key"] for t in json.loads(f.read_text())] if f.exists() else []


def scan_label(name, sources, jobs, threads):
    out = OUT / name
    roadscan.scan_sources(out, sources, jobs, threads, force=True)
    return out


def issues(out):
    return json.loads((out / "issues.json").read_text())["issues"]


def tunnel(args, jobs, threads, rep):
    places = {p["id"]: p for p in json.loads((TUNNEL / "places.json").read_text())}
    ids = sorted(places)
    outs, keys = {}, {}
    for label in (args.tunnel_base, args.tunnel_new):
        src = [dict(dir=str(TUNNEL / "tiles" / i / label), lat=places[i]["lat"], lon=places[i]["lon"]) for i in ids
               if (TUNNEL / "tiles" / i / label / "tiles.json").exists()]
        outs[label] = scan_label(f"tunnel-{label}", src, jobs, threads)
        for i in ids:
            keys.setdefault(i, set()).update(tiles_of(TUNNEL / "tiles" / i / label))
    exe = roadscan.scanner(jobs)
    cmp_dir = OUT / f"tunnel-{args.tunnel_base}-vs-{args.tunnel_new}"
    import subprocess
    subprocess.run([str(exe), "compare", str(outs[args.tunnel_base]), str(outs[args.tunnel_new]), str(cmp_dir)],
                   stdin=subprocess.DEVNULL)
    pairs = json.loads((cmp_dir / "compare.json").read_text())["pairs"]
    losers = {}
    for ln in Path(args.losers).read_text().splitlines():
        ln = ln.strip()
        if ln.startswith("worse "):
            _w, pid, rest = ln.split(" ", 2)
            import ast
            losers[pid] = [m for m, _a, _b in ast.literal_eval(rest)]
    by_tile = {}
    for i, ks in keys.items():
        for k in ks:
            by_tile.setdefault(k, []).append(i)
    flagged = {}     # mouth -> [(status, kind, severity)]
    for p in pairs:
        x = p["b"] or p["a"]
        if p["status"] not in ("new", "worse") or not p["b"] or p["b"]["severity"] < 2:
            continue
        for i in by_tile.get(x["tile"], []):
            flagged.setdefault(i, []).append((p["status"], x["kind"], x["severity"]))
    rows, hit, quiet = [], 0, 0
    for i in ids:
        kinds_expected = {k for m in losers.get(i, ()) for k in CLASSES.get(m, ())}
        got = flagged.get(i, [])
        match = [g for g in got if g[1] in kinds_expected]
        drive = [g for g in got if any(g[1] in v for v in CLASSES.values())]
        if i in losers:
            hit += bool(match)
        else:
            quiet += not drive
        rows.append(dict(id=i, loser=i in losers, metrics=losers.get(i, []), flagged=len(got),
                         flagged_drive=len(drive), matched=len(match),
                         kinds=sorted({g[1] for g in got})))
    nl, nq = len(losers), len(ids) - len(losers)
    rep["tunnel"] = dict(base=args.tunnel_base, new=args.tunnel_new, mouths=len(ids), losers=nl,
                         recall=round(hit / max(nl, 1), 3), losers_found=hit,
                         quiet=round(quiet / max(nq, 1), 3), quiet_mouths=quiet, others=nq, rows=rows)
    # known spots in the new label
    found = issues(outs[args.tunnel_new])
    spots = []
    for name, la, lo, kinds, _w in SPOTS:
        near = [i for i in found if i["severity"] >= 2 and i["kind"] in kinds and
                roadscan._dist((la, lo), (i["lat"], i["lon"])) <= RADIUS_M]
        spots.append(dict(spot=name, lat=la, lon=lo, found=len(near),
                          top=[dict(rank=i["rank"], kind=i["kind"], sev=i["severity"], mag=i["magnitude"],
                                    ft=round(roadscan._dist((la, lo), (i["lat"], i["lon"])) * 3.28084))
                               for i in near[:8]]))
    rep["spots"] = spots


def m5(args, jobs, threads, rep):
    import common
    places = {p["id"]: p for p in common.places(None, False, [T / "roadsys" / "study_places.json"])}
    ids = sorted(p.name for p in (M5 / "tiles").iterdir() if (p / args.m5_label / "tiles.json").exists())
    src = [dict(dir=str(M5 / "tiles" / i / args.m5_label), lat=places[i]["lat"], lon=places[i]["lon"]) for i in ids
           if i in places]
    out = scan_label(f"m5-{args.m5_label}", src, jobs, threads)
    found = issues(out)
    # every finding too (the lips' sizes), by tile
    recs = {}
    for f in (out / "tiles").glob("*.tsv"):
        for ln in f.read_text().splitlines()[1:]:
            w = ln.split("\t")
            if w[0] == "LIP" and float(w[4]) > 20.0:
                recs[w[10]] = recs.get(w[10], 0) + 1
    by_tile = {}
    for i in found:
        by_tile.setdefault(i["tile"], []).append(i)
    conf = {m: dict(tp=0, fn=0, fp=0, tn=0) for m in CLASSES}
    rows = []
    for i in ids:
        res = M5RUN / i / f"{args.m5_label}.json"
        if not res.exists():
            continue
        tiles = json.loads(res.read_text())["tiles"]
        # (the audit run's own tile metrics, over the place's tiles: the truth for each class)
        sums = {m: sum(float(t["metrics"].get(m, 0.0)) for t in tiles.values()) for m in CLASSES}
        mine = [x for k in tiles for x in by_tile.get(k, [])]
        row = dict(id=i, audit={m: round(v, 1) for m, v in sums.items() if v}, issues=len(mine),
                   by_kind={})
        for x in mine:
            row["by_kind"][x["kind"]] = row["by_kind"].get(x["kind"], 0) + 1
        for m, kinds in CLASSES.items():
            if m == "handover_lip_over_20mm":
                got = sum(recs.get(k, 0) for k in tiles) > 0
            elif m == "handover_lip_over_5mm":
                continue
            else:
                got = any(x["kind"] in kinds for x in mine)
            truth = sums[m] > 0
            conf[m]["tp" if truth and got else "fn" if truth else "fp" if got else "tn"] += 1
        rows.append(row)
    conf.pop("handover_lip_over_5mm")
    for m, c in conf.items():
        c["recall"] = round(c["tp"] / max(1, c["tp"] + c["fn"]), 3)
        c["precision"] = round(c["tp"] / max(1, c["tp"] + c["fp"]), 3)
    # the scanner's own checks (no audit sum to stand against): issues per place, majors and criticals
    own = ("step", "deck_end_step", "tunnel_mouth_step", "crest", "dip", "steep_grade", "terrain_over_road",
           "road_floating", "missing_road", "marking_off_road", "width_mismatch", "hole", "deck_missing")
    per = {k: [0, 0, 0] for k in own}
    for x in found:
        if x["kind"] in per:
            per[x["kind"]][x["severity"] - 1] += 1
    rep["m5"] = dict(label=args.m5_label, places=len(rows), classes=conf, own_checks=per, rows=rows,
                     lips_0bff=[r for r in rows if r["id"].endswith("0bff")])


def text(rep):
    s = []
    if "tunnel" in rep:
        t = rep["tunnel"]
        s.append(f"Tunnel corpus, {t['base']} -> {t['new']}: {t['mouths']} mouths, {t['losers']} losers by the audit sums")
        s.append(f"  recall {t['recall']} ({t['losers_found']}/{t['losers']} losers with a new or worse major+ issue of "
                 f"their metric's kind); quiet {t['quiet']} ({t['quiet_mouths']}/{t['others']} other mouths without a "
                 f"new or worse major+ drivability issue)")
        for r in t["rows"]:
            if r["loser"] or r["flagged_drive"]:
                s.append(f"    {r['id']:28s} loser={int(r['loser'])} {','.join(r['metrics']) or '-':40s} "
                         f"matched {r['matched']} drive {r['flagged_drive']} all {r['flagged']} {','.join(r['kinds'])}")
    for sp in rep.get("spots", []):
        s.append(f"Spot: {sp['spot']}: {sp['found']} major+ issues within {RADIUS_M:.0f} m")
        for x in sp["top"]:
            s.append(f"    #{x['rank']} {x['kind']} sev {x['sev']} {x['mag']} at {x['ft']} ft")
    if "m5" in rep:
        m = rep["m5"]
        s.append(f"Road corpus {m['label']}: {m['places']} places; audit sums vs located issues per place")
        for k, c in m["classes"].items():
            s.append(f"  {k:28s} tp {c['tp']:3d} fn {c['fn']:3d} fp {c['fp']:3d} tn {c['tn']:3d}  recall {c['recall']} "
                     f"precision {c['precision']}")
        s.append("  scanner's own checks (minor / major / critical issues over all places):")
        for k, v in m["own_checks"].items():
            s.append(f"    {k:22s} {v[0]:5d} {v[1]:5d} {v[2]:5d}")
        for r in m["lips_0bff"]:
            s.append(f"  0bff: audit {r['audit']}; issues {r['by_kind']}")
    return "\n".join(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpus", type=int, default=0)
    ap.add_argument("--tunnel-base", default="revE")
    ap.add_argument("--tunnel-new", default="x10")
    ap.add_argument("--losers", default=str(T / "tunnelwork" / "t1b" / "tcq_x10.txt"))
    ap.add_argument("--m5-label", default="D7")
    ap.add_argument("--only", default="tunnel,m5")
    a = ap.parse_args()
    n = roadscan.budget(a.cpus)
    jobs, threads = min(roadscan.MAX_JOBS, max(1, n // 2)), min(n, roadscan.MAX_JOBS)
    rep = {}
    only = a.only.split(",")
    if "tunnel" in only:
        tunnel(a, jobs, threads, rep)
    if "m5" in only:
        m5(a, jobs, threads, rep)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "validation.json").write_text(json.dumps(rep, indent=1))
    t = text(rep)
    (OUT / "validation.txt").write_text(t + "\n")
    print(t)


if __name__ == "__main__":
    main()
