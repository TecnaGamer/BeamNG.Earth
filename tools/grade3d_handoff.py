"""Does the build lay the roads where the 3D solve put them? For each drivability site (server/tests/drivability.py),
each centre-line profile drivability measures (a road on the ground, a deck, a tunnel floor, with its leads) is read
twice: as built (drivability's own reading) and on the solve's line along the same points (_Planner._shape_at of the
part's feature). A part bad as built is

    handoff   the solve's line there is within drivability's limits: a step after the solve laid it elsewhere
    solve     the solve's line is itself over a limit there: a law or a coupling of the solve
    missing   the solve has no height at the bad spot: the road is not one of its pieces there (the old plans' height)

For each handoff part: where the built surface is farthest from the solve's line, by how much, and the metric there.

    python tools/grade3d_handoff.py [SITE ...] [--jobs N] [--top K] [--json OUT]

Runs with BNE_SHAPES=1 (the 3D solve's switch with it). Exit 0; the last line is the summary."""
import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from unittest import mock

os.environ.setdefault("BNE_SHAPES", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "server", "tests"))

import numpy as np  # noqa: E402

GAP_M = 0.05        # m: built this near the solve's line is on it


def _modes(surface):
    return {"ground": ("set", "fill", "cut"), "bridge": ("deck", "set", "fill", "cut"),
            "tunnel": ("cut", "set", "fill", "deck")}[surface]


def _pieces(pl):
    """[(a (k, 2), b (k, 2), za (k,), zb (k,))] of every solved piece the planner's cells hold (its region's shapes)."""
    got = pl.__dict__.get("_handoff_pieces")
    if got is not None:
        return got
    out = []
    seen = set()
    for key in list(getattr(pl, "cells", {}).keys()):
        try:
            reg = pl._shape_region(key)
        except Exception:
            continue
        for fid, lst in (reg or {}).items():
            for xy, z, _mode in lst:
                xy = np.asarray(xy, np.float64)[:, :2]
                z = np.asarray(z, np.float64)
                if len(xy) < 2 or (fid, round(float(xy[0, 0]), 2), round(float(xy[0, 1]), 2)) in seen:
                    continue
                seen.add((fid, round(float(xy[0, 0]), 2), round(float(xy[0, 1]), 2)))
                out.append((xy[:-1], xy[1:], z[:-1], z[1:]))
    pl.__dict__["_handoff_pieces"] = out
    return out


def _others(pl, xy, zs, near=3.0):
    """zs with its unknown points read on any solved piece within `near` m whose direction follows the profile's
    (a deck's lead onto another feature's road or deck), the height nearest the profile's last known one."""
    miss = np.flatnonzero(~np.isfinite(zs))
    if not len(miss):
        return zs
    zs = zs.copy()
    tan = np.gradient(xy, axis=0)
    tan /= np.maximum(np.hypot(tan[:, 0], tan[:, 1]), 1e-9)[:, None]
    pcs = _pieces(pl)
    for k in miss:
        q = xy[k]
        prev = zs[np.flatnonzero(np.isfinite(zs))]
        ref = None
        if len(prev):
            kk = np.flatnonzero(np.isfinite(zs))
            ref = zs[kk[np.argmin(np.abs(kk - k))]]
        best = None
        for a, b, za, zb in pcs:
            ab = b - a
            L2 = np.maximum((ab ** 2).sum(1), 1e-12)
            t = np.clip(((q - a) * ab).sum(1) / L2, 0.0, 1.0)
            d = np.hypot(*(a + ab * t[:, None] - q).T)
            ok = (d <= near) & (np.abs((ab * tan[k]).sum(1)) / np.sqrt(L2) >= 0.7) & np.isfinite(za) & np.isfinite(zb)
            for i in np.flatnonzero(ok):
                z = za[i] + (zb[i] - za[i]) * t[i]
                key = abs(z - ref) if ref is not None else d[i]
                if best is None or key < best[0]:
                    best = (key, z)
        if best is not None:
            zs[k] = best[1]
    return zs


def _metrics(dv, z, cls, inside):
    """{metric: (worst value over its limit's ratio, index of the sample)} of profile z (nan where unknown: skipped)."""
    out = {}
    for name, (limit, _tol, fn, on) in dv.METRICS.items():
        if fn is not dv._step and cls not in dv.SPEED_MS:
            continue
        v, first = fn(z, cls)
        if not len(v):
            continue
        ok = inside[first:first + len(v)] & np.isfinite(v)
        v = np.where(ok, v, 0.0)
        k = int(np.argmax(v))
        out[name] = (float(v[k]) / limit, first + k)
    return out


def check(name):
    """{site, parts: [...], counts}: every part drivability finds bad, classed (the module's docstring)."""
    import drivability as dv
    import grading
    records, stores = [], []
    add0 = dv.Report.add
    init0 = grading.PlanStore.__init__

    def add(self, where, xy, z, inside, centre):
        if centre:
            records.append((dict(where), np.asarray(xy, np.float64)[:, :2].copy(), np.asarray(z, np.float64).copy(),
                            np.asarray(inside, bool).copy()))
        return add0(self, where, xy, z, inside, centre)

    def init(self, *a, **k):
        init0(self, *a, **k)
        stores.append(self)

    with mock.patch.object(dv.Report, "add", add), mock.patch.object(grading.PlanStore, "__init__", init):
        dv.measure(name)
    store = stores[-1]
    frame = None
    parts = []
    for where, xy, zb, inside in records:
        cls = where["class"]
        built = _metrics(dv, zb, cls, inside)
        bad = {m: v for m, v in built.items() if v[0] > 1.0}
        if not bad:
            continue
        if frame is None:
            frame = store.frame_of(None)
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        pl = store.planners_of(la[:1], lo[:1])[0][0]
        fid = dv._feature(where["part"]).split(":")[0]
        zs, _m = pl._shape_at(xy, xy, fid, _modes(where["surface"]))
        zs = _others(pl, xy, np.asarray(zs, np.float64))
        worst = max(bad, key=lambda m: bad[m][0])
        ratio, at = bad[worst]
        lo_i, hi_i = max(0, at - 6), min(len(zs), at + 7)
        if not np.isfinite(zs[lo_i:hi_i]).all():
            kind = "missing"
            solve_ratio = None
        else:
            sol = _metrics(dv, zs, cls, inside)
            solve_ratio = sol.get(worst, (0.0, 0))[0]
            kind = "solve" if solve_ratio > 1.0 and solve_ratio >= 0.5 * ratio else "handoff"
        d = np.abs(zb - zs)
        d = np.where(np.isfinite(d) & inside, d, -1.0)
        g = int(np.argmax(d))
        parts.append(dict(part=f"{where['surface']}:{where['part']}", cls=cls, kind=kind, metric=worst,
                          built=round(ratio, 2), solve=None if solve_ratio is None else round(solve_ratio, 2),
                          at=[round(float(xy[at, 0]), 1), round(float(xy[at, 1]), 1)],
                          z_built=round(float(zb[at]), 2),
                          z_solve=None if not np.isfinite(zs[at]) else round(float(zs[at]), 2),
                          gap_max=round(float(d[g]), 2) if d[g] >= 0 else None,
                          gap_at=[round(float(xy[g, 0]), 1), round(float(xy[g, 1]), 1)],
                          solve_known=round(float(np.isfinite(zs[inside]).mean()), 2) if inside.any() else 0.0))
    counts = {}
    for p in parts:
        counts[p["kind"]] = counts.get(p["kind"], 0) + 1
    return dict(site=name, parts=parts, counts=counts)


def main():
    import drivability as dv
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sites", nargs="*")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--top", type=int, default=8, help="parts listed per site, worst built first")
    ap.add_argument("--json", help="write every part here")
    a = ap.parse_args()
    names = a.sites or list(dv.SITES)
    out, total = [], {}
    with ProcessPoolExecutor(max(1, min(a.jobs, len(names)))) as pool:
        for r in pool.map(check, names):
            out.append(r)
            for k, v in r["counts"].items():
                total[k] = total.get(k, 0) + v
            print(f"{r['site']}: " + ", ".join(f"{k} {v}" for k, v in sorted(r["counts"].items())), flush=True)
            for p in sorted(r["parts"], key=lambda p: -p["built"])[:a.top]:
                print(f"   {p['kind']:8s} {p['part'][:60]:60s} {p['cls']:12s} {p['metric']:7s} built x{p['built']:<7} "
                      f"solve x{p['solve']}  at {p['at']} z {p['z_built']} vs {p['z_solve']}  "
                      f"gap max {p['gap_max']} at {p['gap_at']} (solve known {p['solve_known']})", flush=True)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    print("handoff: " + ", ".join(f"{k} {v}" for k, v in sorted(total.items())) + f" over {len(out)} sites")


if __name__ == "__main__":
    main()
