"""Pair physical geotiles before aggregation. Keep regressions visible even at net-improved places."""
import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path
import subprocess
import sys

from common import HERE, PRIVATE, REVAMPED, ROOT, digest, places, read, save

COVERAGE = {"lane_samples", "pavement_samples", "clearance_samples", "deck_ends_sampled", "outline_vertices"}
# One point per unit below. These weights order human review, not release acceptance.
UNITS = {"inLane": .25, "kerb_in_lane_lips": .25, "step4": 1, "deck_end_steps_5cm": .25,
         "clearance_under_4m": 1, "drive_hole_samples": 2, "missing_pavement_samples": 3,
         "outline_crossings": 1, "acute_corners": 4, "tiny_edges": 10, "marking_fragments": 4,
         "kerb_high_faces": 20, "kerb_overhang_faces": 5, "surface_gap_over_10cm": 30,
         "surface_buried_2cm": 30, "in_lane_overlap_m2": 2, "visual_base_overlap_m2": 5, "grade_over_15pct": 5,
         "bank_jump_5pct": 5, "ride_50mph_over_0.5g": 5, "step3": 5, "gapCandidate": 5,
         # the M0 audit (native/roadsys audit.rs; bridgerewrite/DESIGN.md section 10): m^2 of 0.5 m lane cells, metres,
         # 0.25 m edge samples, counts
         "double_owner_m2": 1, "envelope_intrusion_m2": 1, "opening_blocked_m2": 1, "pier_in_face_m2": 1,
         "parapet_in_cw_m": 1, "handover_lip_over_5mm": 4, "clearance_short_cells": 8, "clearance_excess_cells": 40,
         "tcracks_collider": 1, "tcracks_road": 5, "folds_in_lane": 5}
REVIEW_LIMIT = {"inLane": 1, "kerb_in_lane_lips": 1, "deck_end_steps_5cm": 1, "step4": 2,
                "outline_crossings": 1, "drive_hole_samples": 3, "clearance_under_4m": 3,
                "missing_pavement_samples": 5, "ride_50mph_over_0.5g": 5, "grade_over_15pct": 5,
                "bank_jump_5pct": 5, "in_lane_overlap_m2": 1, "visual_base_overlap_m2": 3,
                "kerb_overhang_faces": 3, "kerb_high_faces": 5, "marking_fragments": 10,
                "acute_corners": 25, "tiny_edges": 25, "surface_gap_over_10cm": 100,
                "surface_buried_2cm": 100,
                # drivability classes (D1, D2, D4, D8, D16): one new instance holds the change
                "double_owner_m2": 1, "envelope_intrusion_m2": 1, "opening_blocked_m2": 1, "pier_in_face_m2": 1,
                "parapet_in_cw_m": 1, "handover_lip_over_5mm": 1, "tcracks_collider": 1}
# These fields are emitted even when the measured count is zero. Other sparse mesh counts
# can legitimately be absent, e.g. when there are no bridge ends on a tile.
TILE_FIELDS = {"marking_fragments", "source_faults", "lidar_waits"}
LANE_FIELDS = {"drive_hole_samples", "pavement_samples", "missing_pavement_samples",
               "clearance_samples", "clearance_under_4m", "grade_over_15pct", "bank_jump_5pct",
               *(f"ride_{mph}mph_{measure}" for mph in (30,50,70) for measure in ("max_g","over_0.5g"))}


def pair(p, default, candidate, across_builds=False):
    if default["snapshot"] != candidate["snapshot"] or (not across_builds and default["code"] != candidate["code"]):
        raise ValueError("different input snapshots or code")
    a, b = default["tiles"], candidate["tiles"]
    if not a or a.keys() != b.keys():
        raise ValueError("tile coverage mismatch")
    for tiles in (a,b):
        if sum(t["metrics"].get("lane_samples",0) for t in tiles.values())<=0:
            raise ValueError("no lane coverage")
        if any(not math.isfinite(v) or v<0 for t in tiles.values() for v in (*t["metrics"].values(),t["seconds"])):
            raise ValueError("invalid tile measurement")
    fields = sorted(set(UNITS) | COVERAGE | {k for tree in (a,b) for t in tree.values() for k in t["metrics"]})
    per_tile = {}
    for key in sorted(a):
        d = {k:round(b[key]["metrics"].get(k,0)-a[key]["metrics"].get(k,0),6) for k in fields}
        d["build_seconds"] = round(b[key]["seconds"]-a[key]["seconds"],3)
        per_tile[key] = d

    def total(tree, k):
        vals = [t["metrics"].get(k,0) for t in tree.values()]
        return max(vals) if "_max_" in k else sum(vals)
    delta = {k:round(total(b,k)-total(a,k),6) for k in fields}
    delta["build_seconds"] = round(sum(t["build_seconds"] for t in per_tile.values()),3)
    scores = [round(sum(total(t,k)/unit for k,unit in UNITS.items()),3) for t in (a,b)]
    alerts = [[key,k] for key,d in per_tile.items() for k,limit in REVIEW_LIMIT.items() if d[k]>=limit]
    for key in a:
        for field in ("lane_samples","pavement_samples"):
            if b[key]["metrics"].get(field,0)<a[key]["metrics"].get(field,0)*.98:
                alerts.append([key,field+"_loss"])
        old, new = a[key]["metrics"].get("triangles",0), b[key]["metrics"].get("triangles",0)
        if new>old*1.5 and new>old+10000:
            alerts.append([key,"triangle_cost"])
        if per_tile[key].get("source_faults",0)>0 or per_tile[key].get("lidar_waits",0)>0:
            alerts.append([key,"source_coverage"])
    # Rank the sum of positive per-tile changes. One improvement cannot erase another tile's failure.
    worse = sum(max(0,d[k])/u for d in per_tile.values() for k,u in UNITS.items())
    better = sum(max(0,-d[k])/u for d in per_tile.values() for k,u in UNITS.items())
    return dict(id=p["id"], score_default=scores[0], score_candidate=scores[1], regression=round(worse,3),
                improvement=round(better,3), delta=delta, per_tile=per_tile, alerts=alerts)


def record(run, p, label, env, code, radius):
    root = run / p["id"]
    path = root / (label+".json")
    if not path.exists():
        raise ValueError(f"missing {label}")
    data = read(path)
    if (data["identity"]!=digest([digest([p,radius,code]),env]) or data["env"]!=env or
            data["id"]!=p["id"] or data["code"]!=code):
        raise ValueError(f"stale {label}")
    validate_metrics(data,label)
    return data


def validate_metrics(data,label):
    for key,tile in data["tiles"].items():
        metrics=tile["metrics"]
        required=TILE_FIELDS | LANE_FIELDS if metrics.get("lane_samples",0)>0 else TILE_FIELDS
        if required-metrics.keys():
            raise ValueError(f'{label} {key} missing measurements: {", ".join(sorted(required-metrics.keys()))}')


def confirmed_failure(run, p, configs, code, radius):
    """A crash counts as examined only with successful default and replayed switch trials on one input."""
    folder=run/"failures"/p["id"]
    path=folder/"attribution.json"
    if configs.get("revamped")!=REVAMPED or not path.is_file():
        return None
    evidence=read(path)
    identity=digest([p,radius,code])
    if evidence.get("status")!="confirmed" or evidence.get("identity")!=identity or evidence.get("code")!=code:
        return None
    token=evidence["snapshot"]
    def trial(env):
        result=read(folder/"trials"/(digest(env)[:16]+".json"))
        if (result["env"],result["snapshot"],result["identity"],result["code"])!=(env,token,identity,code):
            raise ValueError("incompatible failure trial")
        return result
    from failures import signature
    baseline,full=trial({}),trial(REVAMPED)
    validate_metrics(baseline,"default")
    pair(p,baseline,baseline)
    sufficient={k:REVAMPED[k] for k in evidence["sufficient_set"]}
    error=signature(evidence["candidate_error"])
    reduced=trial(sufficient)
    without=trial({k:v for k,v in REVAMPED.items() if k not in sufficient})
    finished=set(baseline["finished"])
    if (baseline["error"] or not error or finished!=baseline["tiles"].keys()
            or baseline["finished"]!=evidence["default_finished"] or full["finished"]!=evidence["candidate_finished"]
            or evidence.get("failed_tile") not in finished-set(full["finished"])
            or signature(full["error"])!=error or signature(evidence["initial_error"])!=error
            or signature(reduced["error"])!=error or reduced["finished"]!=full["finished"]
            or without["error"]!=evidence["without_set_error"]
            or (not without["error"] and set(without["finished"])!=finished)):
        raise ValueError("unverified production failure")
    return evidence


def ranking(run, ps, configs, code, radius):
    rows, missing, failures = [], {}, {}
    label = next(k for k in configs if k!="default")
    for p in ps:
        try:
            a = record(run,p,"default",{},code,radius)
            b = record(run,p,label,configs[label],code,radius)
            rows.append(pair(p,a,b))
        except (KeyError, ValueError, OSError) as exc:
            error=run/p["id"]/"error.txt"
            missing[p["id"]] = f"{exc}. {error.read_text().strip()}" if error.exists() else str(exc)
            try:
                evidence=confirmed_failure(run,p,configs,code,radius)
            except (OSError,KeyError,ValueError) as failure:
                missing[p["id"]]+=f". Failure evidence: {failure}"
            else:
                if evidence:
                    failures[p["id"]]=evidence
                    del missing[p["id"]]
    rows.sort(key=lambda r:(-r["regression"],r["id"]))
    return dict(candidate=label, expected=len(ps), complete=len(rows), missing=missing,
                build_failures=failures, places=rows)


def worst_places(result, count):
    return (sorted(result.get("build_failures",{}))+[r["id"] for r in result["places"]])[:count]


def minimize_switches(keys, passes):
    """Reduce a reproducing configuration by testing subsets and their complements."""
    remaining=sorted(keys)
    granularity=2
    while len(remaining)>1:
        width=math.ceil(len(remaining)/granularity)
        parts=[remaining[i:i+width] for i in range(0,len(remaining),width)]
        candidates=parts+[[k for k in remaining if k not in part] for part in parts]
        reduced=False
        for subset in candidates:
            if subset and subset!=remaining and passes(subset):
                remaining=subset
                granularity=2
                reduced=True
                break
        if not reduced:
            if granularity>=len(remaining):
                break
            granularity=min(len(remaining),granularity*2)
    return remaining


def isolate(p, row, run, radius, code, builder):
    """Delta debugging of the largest weighted tile/check regression, then removal from the full bundle."""
    ranked = [(d[k]/unit,key,k,d[k]) for key,d in row["per_tile"].items() for k,unit in UNITS.items() if d[k]>0]
    if not ranked:
        return dict(status="no regression")
    _,tile,metric,full_change=max(ranked)
    a=record(run,p,"default",{},code,radius)
    original_token=a["snapshot"]
    ablations=run/"bisect"
    for label in ("default","revamped"):
        path=ablations/p["id"]/(label+".json")
        if not path.exists():
            save(path,read(run/p["id"]/(label+".json")))
    trials=[]
    measured={}
    def check(keys):
        env={k:REVAMPED[k] for k in sorted(keys)}
        key=tuple(sorted(keys))
        if key in measured:
            return measured[key]
        label="ablate-"+digest(env)[:16]
        conf={"default":{}, "revamped":REVAMPED, label:env}
        if not builder(p,radius,conf,ablations,code):
            raise ValueError("ablation build failed")
        control=record(ablations,p,"default",{},code,radius)
        full=record(ablations,p,"revamped",REVAMPED,code,radius)
        newfull=pair(p,control,full)["per_tile"][tile][metric]
        if newfull!=full_change:
            raise ValueError("full regression did not reproduce on reacquired input")
        test=record(ablations,p,label,env,code,radius)
        delta=pair(p,control,test)["per_tile"][tile][metric]
        if trials and test["snapshot"]!=trials[0]["snapshot"]:
            raise ValueError("input snapshot changed between switch trials")
        trials.append(dict(keys=sorted(keys),delta=delta,snapshot=test["snapshot"]))
        measured[key]=delta
        return delta
    try:
        def reproduces(keys):
            return check(keys)>=full_change*.8
        # Dense urban tiles can take tens of minutes for one trial. Test the surface
        # switches together first when the target measures the road skin itself.
        search=REVAMPED
        skin=("BNE_ROADSURF_V3","BNE_SURFACE_V2")
        if metric in {"surface_buried_2cm","surface_gap_over_10cm","missing_pavement_samples"}:
            if reproduces(skin):
                search=skin
        remaining=minimize_switches(search,reproduces)
        removed=check(set(REVAMPED)-set(remaining))
        return dict(status="measured",tile=tile,metric=metric,target=full_change,
                    sufficient_set=remaining,delta_without_set=removed,original_snapshot=original_token,trials=trials)
    except (ValueError, KeyError, OSError) as exc:
        return dict(status="incomplete",reason=str(exc),tile=tile,metric=metric,trials=trials)


def store_definition(run, code):
    files=[HERE/name for name in ("build.py","checks.py","scan.cpp")]+[HERE.parent/"probes/markprobe.py"]
    signature=digest([hashlib.sha256(p.read_bytes()).hexdigest() for p in files])
    save(run/"measurement.json",{"code":code,"definition":signature})


def compare(run, ps, configs, code, radius, bisect=False, jobs=1, builder=None):
    result=ranking(run,ps,configs,code,radius)
    store_definition(run,code)
    save(run/"places-used.json",ps)
    save(run/"comparison.json",result)
    if bisect:
        if result["candidate"]!="revamped":
            raise ValueError("--bisect needs the full Revamped configuration")
        from failures import exception
        for pid in result["missing"]:
            logs=[run/pid/name for name in ("revamped.log","warm.revamped.log")]
            if any(log.is_file() and exception(log) for log in logs):
                subprocess.run([sys.executable,HERE/"failures.py","--run",run.name,"--places",pid],check=False)
        result=ranking(run,ps,configs,code,radius)
        by={p["id"]:p for p in ps}
        def investigate(row):
            r=isolate(by[row["id"]],row,run,radius,code,builder)
            save(run/row["id"]/"attribution.json",r)
            return row["id"],r
        leaders=set(worst_places(result,10))
        with ThreadPoolExecutor(jobs) as pool:
            result["attribution"]=dict(pool.map(investigate,[r for r in result["places"] if r["regression"] and r["id"] in leaders]))
        save(run/"comparison.json", result)
    write_comparison(run,result)
    return result


def markdown(result):
    rows=result["places"]
    lines=["# Wide road comparison","",f'{result["complete"]} of {result["expected"]} places paired against {result["candidate"]}.']
    if result.get("build_failures"):
        lines+=["","## Release-blocking production failures",""]
        for pid,failure in sorted(result["build_failures"].items()):
            err=failure["candidate_error"]
            without=failure["without_set_error"]
            outcome=f'raised {without["type"]}' if without else "built successfully"
            lines.append(f'- {pid}, {failure["failed_tile"]}. Default built {len(failure["default_finished"])} tiles. '
                         f'Revamped raised {err["type"]} in {err["file"]} ({err["function"]}). '
                         f'The failure reproduced with {", ".join(failure["sufficient_set"])}. '
                         f'The remaining switches {outcome}. Geometry scores require a completed pair.')
    lines+=["", "| Place | Default score | Candidate score | Worse points | Better points | Largest weighted tile regression |",
           "|---|---:|---:|---:|---:|---|"]
    for r in rows:
        _, tile, worst, delta = max((d[k]/unit, tile, k, d[k])
                                    for tile,d in r["per_tile"].items() for k,unit in UNITS.items())
        observation = f"{worst} +{delta:g} ({tile})" if delta>0 else "—"
        lines.append(f'| {r["id"]} | {r["score_default"]} | {r["score_candidate"]} | {r["regression"]} | '
                     f'{r["improvement"]} | {observation} |')
    lines+=["","## Per-check changes","",
            "A place can appear in both counts when different tiles move in opposite directions.","",
            "| Check | Places with an increase | Places with a decrease | Three largest increases, one tile per place |",
            "|---|---:|---:|---|"]
    for key in sorted({k for r in rows for k in r["delta"]}-COVERAGE):
        increases = [(d.get(key,0),r["id"],tile) for r in rows for tile,d in r["per_tile"].items()
                     if d.get(key,0)>0]
        decreases = sum(any(d.get(key,0)<0 for d in r["per_tile"].values()) for r in rows)
        peaks = {}
        for value, pid, tile in sorted(increases, key=lambda t: (-t[0], t[1], t[2])):
            peaks.setdefault(pid,(value,tile))
        leaders = [f"{pid} +{value:g} ({tile})" for pid,(value,tile) in list(peaks.items())[:3]]
        lines.append(f'| {key} | {len(peaks)} | {decreases} | {", ".join(leaders) or "—"} |')
    improvements = sorted((r for r in rows if r["score_candidate"] < r["score_default"]),
                          key=lambda r: (r["score_candidate"]-r["score_default"],r["id"]))
    lines += ["", "## Places with lower net defect scores", ""]
    lines.extend(f'- {r["id"]}, down {r["score_default"]-r["score_candidate"]:.3f} points, '
                 f'{r["regression"]:.3f} points of local regressions.' for r in improvements)
    if not improvements:
        lines.append("None in the completed pairs.")
    if result.get("attribution"):
        lines+=["","## Switch attribution",""]
        for pid,t in result["attribution"].items():
            if t["status"]=="measured":
                removed=t["delta_without_set"]
                verdict=("The complement also reproduced the threshold." if removed>=.8*t["target"]
                         else "Removing the set dropped the change below the reproduction threshold.")
                lines.append(f'- {pid}, {t["metric"]} in {t["tile"]}. Full delta {t["target"]:+g}, '
                             f'sufficient set {", ".join(t["sufficient_set"])}, full bundle without it {removed:+g}. {verdict}')
            else:
                lines.append(f'- {pid}, {t["status"]}. {t.get("reason","")}')
    if result["missing"]:
        lines+=["","## Missing pairs",""]+[f"- {k}, {v}" for k,v in result["missing"].items()]
    return "\n".join(lines)+"\n"


def matching_sheets(run,result):
    from visuals import BROKER_PROTOCOL
    manifest=read(run/"manifest.json")
    for index in sorted((run/"visuals").glob("*/sheets.md")):
        path=index.parent/"index.json"
        if not path.is_file():
            continue
        plan=read(path)
        ids=[e["id"] for e in plan["entries"]]
        if (not ids or plan.get("protocol")!=BROKER_PROTOCOL or plan.get("code")!=manifest["code"] or
                not set(ids).issubset(manifest["places"])):
            continue
        if plan.get("population"):
            examined=result["complete"]+len(result.get("build_failures",{}))
            if plan["population"]!=[examined,result["expected"]] or ids!=worst_places(result,len(ids)):
                continue
        anchors=places(",".join(ids), extra=manifest.get("extra", ()))
        if all((e["shots"][0]["lat"],e["shots"][0]["lon"])==(p["lat"],p["lon"]) for e,p in zip(plan["entries"],anchors)):
            yield index


def write_comparison(run, result):
    sections=[markdown(result)]
    sections.extend(index.read_text() for index in matching_sheets(run,result))
    (run/"comparison.md").write_text("\n".join(sections))


def historical(run, baseline, manifest):
    old = read(baseline/"manifest.json")
    try:
        a,b=(read(d/"measurement.json") for d in (run,baseline))
        if a["code"]!=manifest["code"] or b["code"]!=old["code"] or a["definition"]!=b["definition"]:
            raise ValueError("measurement definitions differ between runs")
    except (FileNotFoundError,KeyError) as exc:
        raise ValueError("missing recorded measurement definition") from exc
    if (set(old["places"]) != set(manifest["places"]) or old["radius"] != manifest["radius"]
            or old["configs"] != manifest["configs"]):
        raise ValueError("reference and candidate must have the same places, radius and configurations")
    reports = {}
    ps = places(",".join(manifest["places"]), extra=manifest.get("extra", ()))
    for label, env in manifest["configs"].items():
        rows, missing = [], {}
        for p in ps:
            try:
                a = record(baseline, p, label, env, old["code"], old["radius"])
                b = record(run, p, label, env, manifest["code"], manifest["radius"])
                rows.append(pair(p, a, b, across_builds=True))
            except (ValueError, KeyError, OSError) as exc:
                missing[p["id"]] = str(exc)
        reports[label] = dict(candidate=label, expected=len(ps), complete=len(rows), missing=missing,
                              places=sorted(rows,key=lambda r:(-r["regression"],r["id"])))
    return reports


def run_name(name):
    if Path(name).name != name or name in ("", ".", ".."):
        raise argparse.ArgumentTypeError("invalid run name")
    return name


def check_population(manifest, catalog):
    if catalog=="all" and (not PRIVATE.is_file() or not read(PRIVATE)):
        raise ValueError("the full nightly requires its private catalogue")
    expected={p["id"] for p in places(public_only=catalog=="public")}
    actual=manifest["places"]
    if len(actual)!=len(expected) or set(actual)!=expected:
        raise ValueError(f"{catalog} catalogue differs from the run selection")


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--run",default="current",type=run_name)
    ap.add_argument("--against",type=run_name,help="compare each configuration with the same one in an accepted run")
    ap.add_argument("--gate",action="store_true")
    ap.add_argument("--expect-catalog",choices=("public","all"),help="require the exact public or public-plus-private nightly population")
    args=ap.parse_args()
    run=ROOT/"runs"/args.run
    m=read(run/"manifest.json")
    if args.expect_catalog:
        try:
            check_population(m,args.expect_catalog)
        except (OSError,KeyError,ValueError) as exc:
            ap.error(str(exc))
    if args.gate:
        from run import fingerprint
        if fingerprint()!=m["code"]:
            ap.error("the recorded build differs from the current checkout")
        store_definition(run,m["code"])
    if args.against:
        if args.run==args.against:
            ap.error("the accepted run must be a separate run")
        try:
            reports=historical(run,ROOT/"runs"/args.against,m)
        except (KeyError,ValueError,OSError) as exc:
            ap.error(f"incompatible or missing reference data: {exc}")
        save(run/"historical.json",reports)
        (run/"historical.md").write_text("\n".join(markdown(v) for v in reports.values()))
        results=list(reports.values())
    else:
        try:
            expected=places(",".join(m["places"]), extra=m.get("extra", ()))
        except (KeyError,ValueError,OSError) as exc:
            ap.error(f"manifest place data unavailable: {exc}")
        result=ranking(run,expected,m["configs"],m["code"],m["radius"])
        if not result["missing"]:
            save(run/"places-used.json",expected)
        previous = read(run/"comparison.json") if (run/"comparison.json").exists() else {}
        if previous.get("places")==result["places"] and "attribution" in previous:
            result["attribution"]=previous["attribution"]
        save(run/"comparison.json",result)
        write_comparison(run,result)
        results=[result]
    for result in results:
        print(f'{result["candidate"]} {result["complete"]}/{result["expected"]} pairs, '
              f'{len(result.get("build_failures",{}))} confirmed build failures, '
              f'{sum(bool(r["alerts"]) for r in result["places"])} with alerts')
    if any(r["missing"] for r in results):
        raise SystemExit(2)
    blocked=any(r.get("build_failures") or any(p["alerts"] for p in r["places"]) for r in results)
    raise SystemExit(int(args.gate and blocked))
