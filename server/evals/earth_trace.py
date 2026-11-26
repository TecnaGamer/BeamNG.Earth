#!/usr/bin/env python3
"""Rebuild an area of a BeamNG.Earth terrain block one step at a time, and say which road plan moved the ground.

Earth's physics block (server/blockworker.py build) computes each height sample as:

  1. dem      the DEM at the block's zoom (dem.Dem.sample)
  2. ground   + terrain_bumps, runways (--airports on), the sea floor (--sea on)       (blockworker.ground)
  3. graded   + every road plan near the point blended in (grading.grade_points) ...   (grading._Planner.apply)
  4. kept     ... then put back to step 2 over a covered tunnel (_over_tunnels) where it moved 0.5 m or more
  5. arch     + the ground raised over low tunnel arches                                (roads.block_arch_raise)

and stores ENU z of that surface in hm_<anchor>_<cx>_<cy>.png. This tool imports Earth's own modules (read-only,
nothing in the Earth tree is changed), runs the same steps on the same disk cache over a box of the block's 1 m
lattice, checks the result against the session's PNG, and writes:

  stages.npz       x, y, u_<step> for every step, session (the PNG's heights), the plans' own share
  stages.png       each step and each step's change, the stage line and the tunnels' cover zones drawn over them
  plans.png        the change each road plan makes alone, the plans that move the ground most
  profile.png      along the stage line (--geometry): the ground before grading, graded, final; across it the final
                   ground's departure from the graded one
  report.txt       the steps at probe points, the plans that move the ground there, the check against the session

Each plan's share is grade_points over that plan alone (the blend of all of them is not a sum, so the shares say which
plan moves the ground, not by how much in the blend).

Example (Popoli SS1 over the A25, anchor 4 at 42.17, 13.82, the block centred at 640, 896):

  python server/evals/earth_trace.py --earth . --anchor 42.17,13.82 --block 640,896 \
      --box 470,780,680,960 --session "%LOCALAPPDATA%/BeamNG/BeamNG.drive/current/temp/beamngEarth/<session>" \
      --geometry server/evals/geometry/ss1.json --probe 559.3,861.1 --out trace-out
"""
import argparse
import json
import os
import sys

import numpy as np

HM_ZOOM = 16
AID = 4


def parse_pair(s):
    a, b = s.split(",")
    return float(a), float(b)


def load_earth(earth_dir):
    server = os.path.join(os.path.abspath(earth_dir), "server")
    if not os.path.isfile(os.path.join(server, "blockworker.py")):
        sys.exit(f"no Earth server at {server}")
    sys.path.insert(0, server)
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def session_heights(session, cx, cy, size=1024):
    """The session PNG's heights (z_base + q * max_height / 65536) and its zBase / maxHeight from the server log."""
    from PIL import Image
    path = os.path.join(session, f"hm_{AID}_{int(cx)}_{int(cy)}.png")
    if not os.path.isfile(path):
        return None, None
    q = np.asarray(Image.open(path), np.float64)[::-1]       # row 0 = south
    return q, path


def block_params_from_log(log_path, cx, cy):
    import re
    pat = re.compile(rf"terrain \({AID}, {cx:.1f}, {cy:.1f}\) z\d+ in .*zBase (\S+) maxHeight (\S+)")
    got = None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = pat.search(line)
                if m:
                    got = float(m.group(1)), float(m.group(2))
    except OSError:
        pass
    return got


def main():
    global AID
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--earth", default=os.environ.get("BEAMNG_EARTH_DIR"), help="Earth source tree (its server/)")
    ap.add_argument("--cache", default=os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth"))
    ap.add_argument("--anchor", required=True, type=parse_pair, help="lat,lon of the anchor (the server log's)")
    ap.add_argument("--aid", type=int, default=AID, help="anchor id (the session files' hm_<aid>_...)")
    ap.add_argument("--block", required=True, type=parse_pair, help="cx,cy of the block (anchor frame, m)")
    ap.add_argument("--box", required=True, help="x0,y0,x1,y1 of the area to rebuild (anchor frame, m)")
    ap.add_argument("--session", help="the game's session folder (temp/beamngEarth/<session>) to check against")
    ap.add_argument("--server-log", default=os.path.join(os.environ.get("LOCALAPPDATA", ""), "beamng-earth", "logs",
                                                         "server.log"))
    ap.add_argument("--geometry", help="a EarthRally stage geometry json (points: lat, lon, z) drawn over the maps")
    ap.add_argument("--probe", action="append", type=parse_pair, default=[], help="x,y to report (repeatable)")
    ap.add_argument("--no-airports", action="store_true")
    ap.add_argument("--no-sea", action="store_true")
    ap.add_argument("--online", action="store_true", help="fetch what the cache lacks")
    ap.add_argument("--core", type=float, default=4.0, help="m either side of the stage line counted as its core")
    ap.add_argument("--side", type=float, default=12.0, help="m either side of the stage line profiled")
    ap.add_argument("--top", type=int, default=8, help="plans drawn in plans.png")
    ap.add_argument("--out", default="earth-trace")
    a = ap.parse_args()
    AID = a.aid
    if not a.earth:
        sys.exit("--earth (or BEAMNG_EARTH_DIR) is needed")
    load_earth(a.earth)
    import blockworker
    import geodesy
    import geotiles
    import grading
    import roads as roadsmod

    os.makedirs(a.out, exist_ok=True)
    blockworker._s = blockworker._State(a.cache, not a.online, True, True, True, False,
                                        airports=not a.no_airports, sea=not a.no_sea)
    S = blockworker._s
    lat0, lon0 = a.anchor
    frame = geodesy.EnuFrame(lat0, lon0)
    g, _chunks = S.anchor(AID, lat0, lon0)
    if g is None:
        sys.exit("no grader (roads or grading off)")

    x0, y0, x1, y1 = (float(v) for v in a.box.split(","))
    xs = np.arange(np.floor(x0), np.floor(x1) + 1.0)
    ys = np.arange(np.floor(y0), np.floor(y1) + 1.0)
    X, Y = np.meshgrid(xs, ys)          # [row = y, col = x]
    la, lo, _ = frame.enu_to_geodetic(X, Y, 0.0)
    la, lo = np.asarray(la), np.asarray(lo)
    flat_la, flat_lo = la.ravel(), lo.ravel()

    def u_of(z):
        return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(z).reshape(la.shape))[2])

    print(f"rebuilding {X.size} samples of block ({a.block[0]:g}, {a.block[1]:g}) ...", flush=True)
    z_dem = np.asarray(S.dem.sample(la, lo, HM_ZOOM), np.float64)
    z_bumps = z_dem + grading.terrain_bumps(la, lo)
    z_ground = blockworker.ground(frame, la, lo, z_dem)

    # steps 3 and 4 as _Planner.apply makes them, region by region, keeping what it throws away
    z_graded = np.array(z_ground, np.float64, copy=True).ravel()
    z_kept = z_graded.copy()
    cover_mask = np.zeros(X.size, bool)
    revert_mask = np.zeros(X.size, bool)
    race_mask = np.zeros(X.size, bool)
    plan_rows = []                      # (planner, fid, plan)
    covers = []                         # (xy in the anchor frame, hw)
    base_flat = np.asarray(z_ground, np.float64).ravel()
    for pl, idx in g.store.planners_of(flat_la, flat_lo):
        idx = np.arange(X.size) if idx is None else np.asarray(idx)
        pla, plo, b = flat_la[idx], flat_lo[idx], base_flat[idx]
        keys = pl._keys(pla, plo)
        plans, pack = pl._plan_set(keys, False)
        got = {}
        for key in keys:
            for fid_part, p in pl._cell_plans(key):
                got.setdefault((fid_part, float(p.xy[0, 0]), float(p.xy[0, 1])), (fid_part, p))
        for k in sorted(got):
            plan_rows.append((pl, got[k][0], got[k][1]))
        e, n, _ = pl.frame.geodetic_to_enu(pla, plo, 0.0)
        e, n = np.asarray(e), np.asarray(n)
        out = grading.grade_points(e, n, b, pack, grading.ROAD_LIFT) if plans else b.copy()
        race = pl._on_kept(e, n)
        if race is not None:
            out[race] = pl._raceway_ground(e[race], n[race], b[race])
            race_mask[idx[race]] = True
        z_graded[idx] = out
        keep = pl._over_tunnels(e, n)
        kept = out.copy()
        if keep is not None:
            cover_mask[idx[keep]] = True
            rev = keep & (np.abs(out - b) >= grading.OVER_TUNNEL_SMALL_M)
            revert_mask[idx[rev]] = True
            kept[rev] = b[rev]
        z_kept[idx] = kept
        with pl.lock:
            zones = [zz for v in pl.covers.values() for zz in v]
        for xy, hw in zones:
            covers.append((geotiles.world_xy_map(pl.frame, frame, np.asarray(xy, np.float64)[:, :2]), float(hw)))
    z_graded = z_graded.reshape(la.shape)
    z_kept = z_kept.reshape(la.shape)
    z_apply = g.apply(la, lo, z_ground, grading.ROAD_LIFT)     # Earth's own call
    replica_err = float(np.nanmax(np.abs(z_apply - z_kept)))
    # the step's result is Earth's own (an Earth tree whose put-back differs from the replica's rule: the replica only
    # says where it would differ); what was changed over a tunnel is where Earth's result departs from the graded
    z_kept = z_apply
    revert_mask = cover_mask & (np.abs(z_apply - z_graded) > 1e-9).ravel()

    # step 5: the arches, over the whole block's box as build() passes it
    half = 512.0
    bx = (a.block[0] - half, a.block[1] - half, a.block[0] + half, a.block[1] + half)

    def elev(la_, lo_):
        z = S.dem.sample(la_, lo_, HM_ZOOM)
        z = blockworker.ground(frame, la_, lo_, z)
        return g.apply(la_, lo_, z, grading.ROAD_LIFT)
    tun = blockworker.tunnel_profiles(AID, frame, elev, g)
    cell_fn = lambda key: S.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731
    z_arch = roadsmod.block_arch_raise(tun, cell_fn, frame, la, lo, z_kept, bx)

    steps = [("dem", z_dem), ("bumps", z_bumps), ("ground", z_ground), ("graded", z_graded), ("kept", z_kept),
             ("arch", z_arch)]
    U = {name: u_of(z) for name, z in steps}

    # the session's block, where it covers the box
    sess = None
    sess_note = "no session given"
    if a.session:
        qg, path = session_heights(a.session, *a.block)
        params = block_params_from_log(a.server_log, *a.block)
        if qg is None:
            sess_note = f"no hm_{AID}_{int(a.block[0])}_{int(a.block[1])}.png in {a.session}"
        elif params is None:
            sess_note = f"no zBase / maxHeight for the block in {a.server_log}"
        else:
            zb, mh = params
            hz = zb + qg * (mh / 65536.0)
            n = qg.shape[0]
            ix = (xs - (a.block[0] - n / 2)).astype(int)
            iy = (ys - (a.block[1] - n / 2)).astype(int)
            ok_x, ok_y = (ix >= 0) & (ix < n), (iy >= 0) & (iy < n)
            sess = np.full(la.shape, np.nan)
            sess[np.ix_(ok_y, ok_x)] = hz[np.ix_(iy[ok_y], ix[ok_x])]
            d = sess - U["arch"]
            sess_note = (f"{os.path.basename(path)} (zBase {zb:g}, maxHeight {mh:g}): rebuilt - session "
                         f"median {np.nanmedian(-d):+.3f} m, largest |difference| {np.nanmax(np.abs(d)):.3f} m")

    # each plan alone
    shares = []
    for pl, fid, p in plan_rows:
        e, n, _ = pl.frame.geodetic_to_enu(flat_la, flat_lo, 0.0)
        e, n = np.asarray(e), np.asarray(n)
        try:
            alone = grading.grade_points(e, n, base_flat, [p], grading.ROAD_LIFT)
        except Exception as ex:
            print(f"plan {fid}: {type(ex).__name__}: {ex}")
            continue
        dz = (alone - base_flat).reshape(la.shape)
        if not np.any(np.abs(dz) > 0.02):
            continue
        xy = geotiles.world_xy_map(pl.frame, frame, np.asarray(p.xy, np.float64)[:, :2])
        shares.append({"fid": fid, "dz": dz, "xy": xy, "z": np.asarray(p.z, np.float64), "half": float(p.half),
                       "road_half": float(p.road_half) if p.road_half is not None else None,
                       "tunnel": grading.TUNNEL_TAG in fid, "rid": int(getattr(p, "rid", -1))})
    shares.sort(key=lambda s: -float(np.nanmax(np.abs(s["dz"]))))

    stage_xy = None
    if a.geometry:
        with open(a.geometry, encoding="utf-8") as f:
            pts = np.asarray(json.load(f)["points"], np.float64)
        e, n, _ = frame.geodetic_to_enu(pts[:, 0], pts[:, 1], 0.0)
        stage_xy = np.c_[np.asarray(e), np.asarray(n)]

    np.savez_compressed(os.path.join(a.out, "stages.npz"), x=xs, y=ys, **{f"u_{k}": v for k, v in U.items()},
                        session=sess if sess is not None else np.zeros(0), cover=cover_mask.reshape(la.shape),
                        reverted=revert_mask.reshape(la.shape),
                        **{f"plan_{i}": s["dz"] for i, s in enumerate(shares)})

    # the report
    lines = [f"block ({a.block[0]:g}, {a.block[1]:g}) of anchor {AID} at {lat0}, {lon0}; box {a.box}; "
             f"{X.size} samples, 1 m",
             f"the old put-back rule (any change >= {grading.OVER_TUNNEL_SMALL_M} m over a tunnel back to the ground) "
             f"against Earth's own Grader.apply: largest difference {replica_err:.4f} m (0: this tree has that rule; "
             f"the steps below are Earth's own result either way)",
             f"session check: {sess_note}", ""]
    names = [k for k, _ in steps]
    for i in range(1, len(names)):
        d = U[names[i]] - U[names[i - 1]]
        lines.append(f"{names[i - 1]:>6} -> {names[i]:<6}: change min {np.nanmin(d):+7.2f} max {np.nanmax(d):+7.2f} m, "
                     f"{int((np.abs(d) > 0.1).sum())} samples moved > 0.1 m")
    lines.append(f"over a covered tunnel (_over_tunnels): {int(cover_mask.sum())} samples, "
                 f"{int(revert_mask.sum())} of them changed by the put-back (Earth's result != the graded), "
                 f"{int(cover_mask.sum() - revert_mask.sum())} left as graded")
    lines.append(f"raceway ground: {int(race_mask.sum())} samples")
    lines.append("")
    lines.append(f"cover zones ({len(covers)}):")
    for xy, hw in covers:
        if ((xy[:, 0] > x0 - 50) & (xy[:, 0] < x1 + 50) & (xy[:, 1] > y0 - 50) & (xy[:, 1] < y1 + 50)).any():
            lines.append(f"  hw {hw:.2f} m (+ keep {grading.TUNNEL_KEEP_M:g}): {len(xy)} points, "
                         f"({xy[0, 0]:.0f}, {xy[0, 1]:.0f}) to ({xy[-1, 0]:.0f}, {xy[-1, 1]:.0f})")
    lines.append("")
    lines.append(f"plans moving the ground in the box ({len(shares)}), most first:")
    for s in shares:
        d = s["dz"]
        lines.append(f"  {s['fid']}{'  [tunnel plan]' if s['tunnel'] else ''}: alone {np.nanmin(d):+.2f} .. "
                     f"{np.nanmax(d):+.2f} m, {int((np.abs(d) > 0.1).sum())} samples > 0.1 m; half {s['half']:.1f} "
                     f"road_half {s['road_half']}; z {s['z'].min():.1f} .. {s['z'].max():.1f}; "
                     f"({s['xy'][0, 0]:.0f}, {s['xy'][0, 1]:.0f}) to ({s['xy'][-1, 0]:.0f}, {s['xy'][-1, 1]:.0f})")
    lines.append("")
    for px, py in a.probe:
        j = int(round(py - ys[0]))
        i = int(round(px - xs[0]))
        if not (0 <= j < len(ys) and 0 <= i < len(xs)):
            lines.append(f"probe ({px:g}, {py:g}) outside the box")
            continue
        vals = ", ".join(f"{k} {U[k][j, i]:.2f}" for k in names)
        sv = "" if sess is None else f", session {sess[j, i]:.2f}"
        lines.append(f"probe ({px:g}, {py:g}): {vals}{sv}; cover {bool(cover_mask.reshape(la.shape)[j, i])}, "
                     f"put back {bool(revert_mask.reshape(la.shape)[j, i])}")
        for s in shares:
            v = s["dz"][j, i]
            if abs(v) > 0.05:
                lines.append(f"    {s['fid']}: {v:+.2f} m alone")
    prof = None
    if stage_xy is not None:
        prof = line_profile(stage_xy, xs, ys, U, revert_mask.reshape(la.shape), a.core, a.side)
        lines.append("")
        lines.extend(profile_lines(prof, a.core, a.side))
    rep = "\n".join(lines)
    with open(os.path.join(a.out, "report.txt"), "w", encoding="utf-8") as f:
        f.write(rep + "\n")
    print(rep)

    draw(a, xs, ys, U, names, sess, covers, stage_xy, shares, cover_mask.reshape(la.shape),
         revert_mask.reshape(la.shape), prof)


def bilinear(u, xs, ys, x, y):
    fx, fy = np.asarray(x) - xs[0], np.asarray(y) - ys[0]
    i = np.clip(np.floor(fx).astype(int), 0, len(xs) - 2)
    j = np.clip(np.floor(fy).astype(int), 0, len(ys) - 2)
    wx, wy = fx - i, fy - j
    return (u[j, i] * (1 - wx) + u[j, i + 1] * wx) * (1 - wy) + (u[j + 1, i] * (1 - wx) + u[j + 1, i + 1] * wx) * wy


def line_profile(stage_xy, xs, ys, U, reverted, core, side):
    """Along the stage line (every 1 m of it in the box): the raw, graded and final ground on it, and across it (every
    0.5 m out to side m either way) the final ground's departure from the graded one, on the core (|offset| <= core)
    and beside it, and how many of those samples were put back."""
    seg = np.hypot(*np.diff(stage_xy, axis=0).T)
    s_all = np.r_[0.0, np.cumsum(seg)]
    S = np.arange(0.0, s_all[-1], 1.0)
    X = np.interp(S, s_all, stage_xy[:, 0])
    Y = np.interp(S, s_all, stage_xy[:, 1])
    inb = (X > xs[0] + side) & (X < xs[-1] - side) & (Y > ys[0] + side) & (Y < ys[-1] - side)
    if not inb.any():
        return None
    k = np.flatnonzero(inb)
    sl = slice(k[0], k[-1] + 1)         # (contiguous in s: out of the box in between is NaN)
    S, X, Y, inb = S[sl], X[sl], Y[sl], inb[sl]
    tx = np.gradient(X)
    ty = np.gradient(Y)
    tl = np.maximum(np.hypot(tx, ty), 1e-9)
    nx, ny = -ty / tl, tx / tl
    offs = np.arange(-side, side + 0.25, 0.5)
    PX = X[:, None] + nx[:, None] * offs[None, :]
    PY = Y[:, None] + ny[:, None] * offs[None, :]
    dev = bilinear(U["arch"], xs, ys, PX, PY) - bilinear(U["graded"], xs, ys, PX, PY)
    rv = bilinear(reverted.astype(float), xs, ys, PX, PY) > 0.5
    is_core = np.abs(offs) <= core
    dev[~inb] = np.nan
    rv[~inb] = False
    out = {"s": S, "x": X, "y": Y, "offs": offs, "dev": dev, "rv": rv, "core": is_core,
           "raw": bilinear(U["ground"], xs, ys, X, Y), "graded": bilinear(U["graded"], xs, ys, X, Y),
           "final": bilinear(U["arch"], xs, ys, X, Y)}
    for key in ("raw", "graded", "final"):
        out[key][~inb] = np.nan
    return out


def profile_lines(p, core, side):
    if p is None:
        return ["stage line: not in the box"]
    out = [f"along the stage line, where samples within {side:g} m of it were put back (core: within {core:g} m):"]
    hit = p["rv"].any(1)
    k = 0
    n = len(hit)
    while k < n:
        if not hit[k]:
            k += 1
            continue
        m = k
        while m + 1 < n and hit[m + 1]:
            m += 1
        sl = slice(k, m + 1)
        dc = p["dev"][sl][:, p["core"]]
        db = p["dev"][sl][:, ~p["core"]]
        out.append(f"  s {p['s'][k]:.0f} to {p['s'][m]:.0f} m ({p['x'][k]:.0f}, {p['y'][k]:.0f} to {p['x'][m]:.0f}, "
                   f"{p['y'][m]:.0f}): final - graded on the core {np.nanmin(dc):+.2f} .. {np.nanmax(dc):+.2f} m, "
                   f"beside it {np.nanmin(db):+.2f} .. {np.nanmax(db):+.2f} m; {int(p['rv'][sl].sum())} of "
                   f"{p['rv'][sl].size} samples "
                   f"put back")
        k = m + 1
    if len(out) == 1:
        out.append("  none")
    return out


def draw(a, xs, ys, U, names, sess, covers, stage_xy, shares, cover, reverted, prof=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ext = (xs[0] - 0.5, xs[-1] + 0.5, ys[0] - 0.5, ys[-1] + 0.5)

    def overlay(ax):
        for xy, hw in covers:
            ax.plot(xy[:, 0], xy[:, 1], color="magenta", lw=0.8)
        if stage_xy is not None:
            ax.plot(stage_xy[:, 0], stage_xy[:, 1], color="cyan", lw=0.8)
        for px, py in a.probe:
            ax.plot(px, py, "w+", ms=8)
        ax.set_xlim(ext[0], ext[1])
        ax.set_ylim(ext[2], ext[3])

    def shade(u):
        gy, gx = np.gradient(u)
        return np.hypot(gx, gy)

    panels = [(f"{k} (ENU z)", U[k], "terrain", None) for k in ("ground", "arch")]
    panels.append(("slope of the final ground (edges)", shade(U["arch"]), "gray_r", (0, 1.0)))
    for i in range(1, len(names)):
        d = U[names[i]] - U[names[i - 1]]
        lim = max(0.1, float(np.nanmax(np.abs(d))))
        panels.append((f"{names[i]} - {names[i - 1]}", d, "RdBu_r", (-lim, lim)))
    if sess is not None:
        d = sess - U["arch"]
        lim = max(0.1, float(np.nanmax(np.abs(d))))
        panels.append(("session PNG - rebuilt", d, "RdBu_r", (-lim, lim)))
        panels.append(("slope of the session PNG (edges)", shade(sess), "gray_r", (0, 1.0)))
    panels.append(("over a cover zone (1) / put back (2)", cover.astype(float) + reverted, "viridis", (0, 2)))
    cols = 3
    rows = (len(panels) + cols - 1) // cols
    fig, axs = plt.subplots(rows, cols, figsize=(6 * cols, 5 * rows), squeeze=False)
    for ax, (title, img, cmap, lim) in zip(axs.flat, panels):
        im = ax.imshow(img, origin="lower", extent=ext, cmap=cmap,
                       vmin=None if lim is None else lim[0], vmax=None if lim is None else lim[1])
        fig.colorbar(im, ax=ax, shrink=0.8)
        overlay(ax)
        ax.set_title(title, fontsize=10)
    for ax in list(axs.flat)[len(panels):]:
        ax.axis("off")
    fig.suptitle("cyan: stage line; magenta: tunnels' cover zones (grading._Planner.covers); +: probes")
    fig.tight_layout()
    fig.savefig(os.path.join(a.out, "stages.png"), dpi=90)
    plt.close(fig)

    if prof is not None:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True)
        ax1.plot(prof["s"], prof["raw"], color="0.5", label="ground before grading")
        ax1.plot(prof["s"], prof["graded"], "b-", label="graded by the road plans")
        ax1.plot(prof["s"], prof["final"], "r-", label="final (after the put-back and the arches)")
        ax1.set_ylabel("ENU z (m) on the stage line")
        ax1.legend()
        lim = max(0.1, float(np.nanmax(np.abs(prof["dev"])))) if np.isfinite(prof["dev"]).any() else 0.1
        im = ax2.imshow(prof["dev"].T, origin="lower", aspect="auto", cmap="RdBu_r", vmin=-lim, vmax=lim,
                        extent=(prof["s"][0] - 0.5, prof["s"][-1] + 0.5, prof["offs"][0] - 0.25, prof["offs"][-1] + 0.25))
        fig.colorbar(im, ax=ax2, orientation="horizontal", pad=0.15, shrink=0.6, label="final - graded (m)")
        ax2.axhline(a.core, color="k", lw=0.5)
        ax2.axhline(-a.core, color="k", lw=0.5)
        ax2.set_ylabel("offset across the line (m, + left)")
        ax2.set_xlabel("m along the stage")
        fig.tight_layout()
        fig.savefig(os.path.join(a.out, "profile.png"), dpi=90)
        plt.close(fig)

    top = shares[:a.top]
    if not top:
        return
    rows = (len(top) + cols - 1) // cols
    fig, axs = plt.subplots(rows, cols, figsize=(6 * cols, 5 * rows), squeeze=False)
    for ax, s in zip(axs.flat, top):
        lim = max(0.1, float(np.nanmax(np.abs(s["dz"]))))
        im = ax.imshow(s["dz"], origin="lower", extent=ext, cmap="RdBu_r", vmin=-lim, vmax=lim)
        fig.colorbar(im, ax=ax, shrink=0.8)
        ax.plot(s["xy"][:, 0], s["xy"][:, 1], "k-", lw=0.8)
        overlay(ax)
        ax.set_title(f"{s['fid']}{' [tunnel]' if s['tunnel'] else ''} alone", fontsize=9)
    for ax in list(axs.flat)[len(top):]:
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(os.path.join(a.out, "plans.png"), dpi=90)
    plt.close(fig)


if __name__ == "__main__":
    main()
