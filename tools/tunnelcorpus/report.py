"""The tunnel corpus's numbers per label: the tunnel checks summed over every mouth's tiles, the drivability classes,
the built portal's offset from the real one (refs.json) and the mapped tag end's, and per mouth which label is worse.

    python3 tools/tunnelcorpus/report.py LABEL [LABEL ...] [--json OUT]

A mouth's built portal: the nearest PORTAL line of its .portals.txt within NEAR_M of the real face (the audit's: where
a tunnel lane's centre row goes from no vault to a vault); its tag end: the nearest TAGEND."""
import argparse
import json
import math
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TUNNEL_CORPUS_ROOT") or Path.home() / "bnge-tests/tunnelcorpus")
NEAR_M = 150.0
TUNNEL = ("hole_squares", "hole_open_squares", "hole_edge_open", "opening_blocked_tunnel_m2", "tube_breaks_upper_m2",
          "tunnel_roofless_cells", "lane_step_cells_tunnel", "handover_lip_tunnel_over_5mm", "tunnel_mouth_steps_over_10cm",
          "tunnel_mouth_holes", "envelope_intrusion_tunnel_m2", "envelope_intrusion_terrain_m2")
DRIVE = ("double_owner_m2", "envelope_intrusion_m2", "opening_blocked_m2", "clearance_short_cells", "handover_lip_over_5mm")
MAXES = ("handover_lip_tunnel_max_mm", "tunnel_mouth_step_max_m")


def nearest(path, kind):
    best = None
    if path.exists():
        for ln in path.read_text().splitlines():
            w = ln.split()
            if w and w[0] == kind:
                d = math.hypot(float(w[1]), float(w[2]))
                if d <= NEAR_M and (best is None or d < best):
                    best = d
    return best


def pct(v, q):
    if not v:
        return float("nan")
    v = sorted(v)
    return v[min(len(v) - 1, int(round((len(v) - 1) * q)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", nargs="+")
    ap.add_argument("--json")
    a = ap.parse_args()
    rows = json.loads((HERE / "refs.json").read_text())["mouths"]
    out = {"labels": a.labels, "sums": {}, "maxes": {}, "portal": {}, "mouths": {}}
    for lab in a.labels:
        sums = {k: 0.0 for k in TUNNEL + DRIVE}
        mx = {k: 0.0 for k in MAXES}
        offs, tags, built, failed = [], [], 0, 0
        for r in rows:
            pid = "tun-" + r["id"]
            f = ROOT / "run" / pid / (lab + ".json")
            if not f.exists():
                failed += 1
                continue
            built += 1
            m = {}
            for t in json.loads(f.read_text())["tiles"].values():
                for k, v in t["metrics"].items():
                    if k in MAXES:
                        m[k] = max(m.get(k, 0.0), v)
                    else:
                        m[k] = m.get(k, 0.0) + v
            for k in sums:
                sums[k] += m.get(k, 0.0)
            for k in mx:
                mx[k] = max(mx[k], m.get(k, 0.0))
            po = nearest(ROOT / "run" / pid / (lab + ".portals.txt"), "PORTAL")
            te = nearest(ROOT / "run" / pid / (lab + ".portals.txt"), "TAGEND")
            if po is not None:
                offs.append(po)
            if te is not None:
                tags.append(te)
            out["mouths"].setdefault(pid, {})[lab] = dict(m, portal_m=po, tagend_m=te)
        out["sums"][lab] = dict(sums, built=built, failed=failed)
        out["maxes"][lab] = mx
        out["portal"][lab] = dict(n=len(offs), p50=pct(offs, 0.5), p95=pct(offs, 0.95), within_30m=sum(1 for o in offs if o <= 30.0),
                                  tag_n=len(tags), tag_p50=pct(tags, 0.5), tag_p95=pct(tags, 0.95))
    w = max(len(k) for k in TUNNEL + DRIVE + MAXES) + 2
    print("metric".ljust(w) + "".join(lab.rjust(12) for lab in a.labels))
    for k in ("built", "failed") + TUNNEL + DRIVE:
        print(k.ljust(w) + "".join(f"{out['sums'][lab][k]:12.1f}" for lab in a.labels))
    for k in MAXES:
        print(k.ljust(w) + "".join(f"{out['maxes'][lab][k]:12.3f}" for lab in a.labels))
    for k in ("n", "p50", "p95", "within_30m", "tag_n", "tag_p50", "tag_p95"):
        print(("portal_" + k).ljust(w) + "".join(f"{out['portal'][lab][k]:12.1f}" for lab in a.labels))
    if len(a.labels) >= 2:
        base, new = a.labels[0], a.labels[-1]
        worse = []
        for pid, by in sorted(out["mouths"].items()):
            if base in by and new in by:
                d = [k for k in DRIVE if by[new].get(k, 0) > by[base].get(k, 0) + 1e-9]
                if d:
                    worse.append((pid, d))
        print(f"drivability no worse ({new} vs {base}): {sum(1 for pid, by in out['mouths'].items() if base in by and new in by) - len(worse)}"
              f"/{sum(1 for by in out['mouths'].values() if base in by and new in by)}")
        for pid, d in worse:
            print("  worse", pid, " ".join(f"{k} {out['mouths'][pid][base].get(k, 0):.1f}->{out['mouths'][pid][new].get(k, 0):.1f}" for k in d))
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
