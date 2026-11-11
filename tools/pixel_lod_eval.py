#!/usr/bin/env python3
"""Offline evaluation of the far field's screen-space LOD (farfield.View, pixel_lod_plan steps 2-3; no game): the
steady-state far selection round a camera on real terrain (the DEM through dem.Dem, a scratch cache) under today's
distance rule (tau 0) and the pixel rule at several tau, at several screens, with every built tile's error known
(the best case: in game a node's error is known once its tile was built). Reports per run the tiles per level and the
pop each shown tile makes when it switches to its children, in px: its error along the normal (Built.err_n, capped at
SSE_SPACING_CAP vertex spacings, View.pixels) seen from the distance it splits at (the distance rule: its switch
distance; the pixel rule: where its pop reaches tau, between its floor and its reach cap: View.decide).

    python3 tools/pixel_lod_eval.py --cache ~/bnge-tests/pixellod/cache \\
        --place yosemite=37.72,-119.6 --place kansas=38.5,-98.0 --place baltimore=39.283,-76.593 \\
        --tau 0,1,2,4 --screen 1440 --screen 2160 --fov 60

Levels <= 0 (the near rings, 128 / 256 m tiles within ~400 m) count as unknown errors (the distance rule): their
reference is the 1 m DEM. The anchor quadtree (farfield.select) stands in for the global tiles (the same rule in
farglobal.rs; their sizes match the levels within 0.7-1.4x)."""
import argparse
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import dem as demmod  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy  # noqa: E402

LAY = ff.Layout(64, 1024, True)
RADIUS = 80000.0
DZ = 2.0                # the camera over the ground under the player (m): a car's


class Errors:
    """err_n (and the vertical err) of anchor nodes, built on demand (farfield.build_tile) and kept."""

    def __init__(self, frame, dem, lat):
        self.frame, self.dem, self.lat = frame, dem, lat
        self.zooms = {lvl: LAY.dem_zoom(lat, lvl) for lvl in range(0, ff.MAX_LEVEL + 1)}
        self.known = {}
        self.built_s = 0.0

    def __call__(self, node):
        if node[0] < 1:
            return None
        if node not in self.known:
            t0 = time.perf_counter()
            lvl = node[0]
            b = ff.build_tile(self.frame, self.dem, ff.Tile(*node), self.zooms[lvl], None, self.zooms[lvl - 1],
                              with_sink=False, layout=LAY)
            self.known[node] = (b.err, b.lod_err)
            self.built_s += time.perf_counter() - t0
        return self.known[node][1]


def steady(px, py, view, rounds=6):
    """The selection a camera standing at (px, py) settles on (each round's refined nodes the next one's previous)."""
    prev = None
    out = ref = None
    for _ in range(rounds):
        out, ref = ff.select(px, py, prev_refined=prev or frozenset(), radius=RADIUS, min_level=LAY.min_level,
                             forced_level=LAY.min_level, layout=LAY, view=view)
        if ref == prev:
            break
        prev = ref
    return out, ref


def switch_pops(tiles, px, py, errs, ppr, tau):
    """The pop (px) of each shown tile of level >= 1 when it splits, approaching it: e x ppr / d_split."""
    pops = []
    for t in tiles.values():
        if t.level < 1:
            continue
        e = errs(t.node)
        e = min(e, ff.SSE_SPACING_CAP * t.size / LAY.cells)
        lim = ff.switch_distance(t.level, min_level=LAY.min_level, layout=LAY)
        if tau > 0:
            lvl = t.level
            lim_f = (max(ff.switch_distance(lvl - 1, min_level=LAY.min_level, layout=LAY),
                         max([r for m, r in LAY.near_radii.items() if LAY.min_level <= m < lvl] or [0.0]))
                     if lvl - 1 > LAY.min_level else lim)
            d_tau = e * ppr / tau                       # where its pop reaches tau
            d = min(max(d_tau, lim_f), ff.SSE_REACH * lim)
        else:
            d = lim
        pops.append(e * ppr / max(math.hypot(d, DZ), 1.0))
    return np.array(pops)


def shown_pops(tiles, px, py, errs, ppr):
    """The error each shown tile (level >= 1) has on screen now (px)."""
    out = []
    for t in tiles.values():
        if t.level < 1:
            continue
        e = min(errs(t.node), ff.SSE_SPACING_CAP * t.size / LAY.cells)
        out.append(e * ppr / max(math.hypot(t.distance(px, py), DZ), 1.0))
    return np.array(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", required=True, help="a scratch DEM cache (never the player's)")
    ap.add_argument("--place", action="append", required=True, help="name=lat,lon")
    ap.add_argument("--tau", default="0,1,2,4")
    ap.add_argument("--screen", action="append", type=int, help="screen heights (px)")
    ap.add_argument("--fov", type=float, default=60.0, help="vertical field of view (deg)")
    a = ap.parse_args()
    taus = [float(v) for v in a.tau.split(",")]
    screens = a.screen or [1440, 2160]
    os.makedirs(a.cache, exist_ok=True)
    dem = demmod.Dem(a.cache)
    for spec in a.place:
        name, ll = spec.split("=")
        lat, lon = (float(v) for v in ll.split(","))
        frame = geodesy.EnuFrame(lat, lon)
        errs = Errors(frame, dem, lat)
        px, py = 37.0, -91.0            # (off the node corners)
        for h in screens:
            ppr = h / (2 * math.tan(math.radians(a.fov) / 2))
            rows = []
            for tau in taus:
                view = ff.View(px, py, DZ, ppr, errs, tau=tau, cells=LAY.cells) if tau > 0 else None
                tiles, _ = steady(px, py, view)
                lv = {}
                for t in tiles.values():
                    lv[t.level] = lv.get(t.level, 0) + 1
                sw = switch_pops(tiles, px, py, errs, ppr, tau)
                sh = shown_pops(tiles, px, py, errs, ppr)
                rows.append((tau, len(tiles), dict(sorted(lv.items())), sw, sh))
            print(f"\n== {name} ({lat}, {lon}), screen {h} px, vfov {a.fov:g} deg (ppr {ppr:.0f}); "
                  f"{len(errs.known)} tiles built for their errors ({errs.built_s:.0f} s)")
            print("tau  tiles  per level (L: n)" + " " * 40 + "pop at switch px: p50 / p90 / p99 / max   "
                  "shown now px: p99 / max")
            for tau, n, lv, sw, sh in rows:
                lvs = " ".join(f"{k}:{v}" for k, v in lv.items())
                print(f"{tau:<4g} {n:5d}  {lvs:<56s} {np.percentile(sw, 50):5.2f} / {np.percentile(sw, 90):5.2f} / "
                      f"{np.percentile(sw, 99):5.2f} / {sw.max():5.2f}   {np.percentile(sh, 99):5.2f} / {sh.max():5.2f}")
    dem.pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
