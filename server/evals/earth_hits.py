"""Every junction of a set drawn from one Earth tree, its geometry checks counted: the vehicles hitting what isn't their own
(yellow) and the crossings whose meshes cut each other (red), and each hit's place, what it cuts and how deep. One tree, no
before / after (server/evals/earth_eval.py compares two): the list of what is still wrong, to work through.

    python server/evals/earth_hits.py --earth . --out hits-out
    python server/evals/earth_hits.py --earth . --out hits-out --only yokohama,barcelona_trench --jobs 2

<out>/<name>/ is each junction's earth_view output (view.png, view.log, view_checks.json); <out>/hits.md the table, the
junctions with most hits first, and every hit listed; <out>/hits.json the same. --reuse draws from the data a former run left.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import earth_eval as ee  # noqa: E402


def build(j, a):
    out = os.path.join(a.out, j["name"])
    os.makedirs(out, exist_ok=True)
    cmd = [sys.executable, os.path.join(HERE, "earth_view.py"), "--earth", a.earth, "--out", out, "--name", "view", "--dump",
           "--views", a.views] + ee.junction_args(j)
    if a.reuse:
        cmd.append("--reuse")
    env = dict(os.environ, BNGE_PLANCACHE="off", BNGE_BLOCKCACHE="off", BNE_LIDAR_GAPS_WAIT=ee.LIDAR_WAIT_S)
    t = time.time()
    with open(os.path.join(out, "view.log"), "w", encoding="utf-8") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env, cwd=ee.REPO)
    ck = ee.load_checks(os.path.join(out, "view_checks.json"))
    return {"name": j["name"], "what": j.get("what", ""), "seconds": round(time.time() - t), "error": r.returncode or None,
            **{k: ck.get(k) for k in ("vehicles", "vehicles_hit", "crossings", "crossings_cut", "overview_crossings_cut")},
            "hits": ck.get("hits", [])}


def table(results):
    rows = ["| junction | vehicles hitting | crossings cutting | cutting in 1 km |", "|---|---|---|---|"]
    for r in sorted(results, key=lambda r: -((r["vehicles_hit"] or 0) + (r["crossings_cut"] or 0))):
        if r["error"]:
            rows.append(f"| {r['name']} | error {r['error']} (see {r['name']}/view.log) | | |")
            continue
        rows.append(f"| {r['name']} | {r['vehicles_hit']} of {r['vehicles']} | {r['crossings_cut']} of {r['crossings']} | "
                    f"{r['overview_crossings_cut']} |")
    out = rows + [""]
    for r in results:
        for h in r["hits"]:
            out.append(f"- {r['name']}: {h['kind']} at x {h['x']} y {h['y']} z {h['z']} heading {h['heading']:g}: hits "
                       + ", ".join(f"{n} by {d} m" for n, d in h["hits"].items()))
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--earth", required=True, help="the Earth tree")
    ap.add_argument("--out", default="earth-hits")
    ap.add_argument("--set", default=ee.DEFAULT_SET)
    ap.add_argument("--only", help="comma list of the set's junction names")
    ap.add_argument("--views", default="overview,top,SW")
    ap.add_argument("--jobs", type=int, default=3, help="junctions built at once")
    ap.add_argument("--reuse", action="store_true")
    a = ap.parse_args(argv)
    with open(a.set, encoding="utf-8") as f:
        js = json.load(f)["junctions"]
    if a.only:
        want = set(a.only.split(","))
        js = [j for j in js if j["name"] in want]
    os.makedirs(a.out, exist_ok=True)
    with ThreadPoolExecutor(max(1, a.jobs)) as ex:
        results = list(ex.map(lambda j: build(j, a), js))
    with open(os.path.join(a.out, "hits.json"), "w", encoding="utf-8") as f:
        json.dump({"earth": a.earth, "junctions": results}, f, indent=1)
    t = table(results)
    with open(os.path.join(a.out, "hits.md"), "w", encoding="utf-8") as f:
        f.write(f"# Geometry checks: {a.earth}\n\n" + t)
    print(t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
