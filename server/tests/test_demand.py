"""Far-field demand: tiles added per second while driving (docs/V1_4_SPEC.md §2.1, milestone M0).

The [M] rows of table 2.1 come from the real farfield.select driven the way the server drives it
(earthd.Connection.update_far): a pose every 0.25 s (4 Hz), the 25 % hysteresis on the previous
selection's refined nodes, and HOLD_S (a node that changed state less than 2 s ago keeps it), in
mesh near-visual mode (min level and NEAR_RADII as the server passes them, no forced boxes). A
straight drive at heading 37° from (1234, -777) is measured for 60 s after a 10 s warm-up; every
key in a selection that was not in the previous one is an add. "tiles at a pose" is the size of
the first selection after the warm-up.

Each row must reproduce the spec within ±10 % (adds/s and tiles), and per level within ±10 % or
±0.3 tiles/s, whichever is larger (the coarse levels add a handful of tiles a minute).

The M1 rows (64-cell layout, --cells 64: the K' table, NEAR_RADII {-1: 150, 0: 400}, min level -1)
run with the same driver. Hooks for later milestones (clearly marked below): M3a the capsule /
speed-scale rows and D(s) against the simulation, M5 the flight rows.

NetDemandTest (docs/research/2026-10-10-rate-limits.md §2.2 and §5.4 case 12): the same driver
counts the upstream requests the added tiles cause (every imagery or DEM source tile not seen
before is one HTTP request: a cold disk cache), per minute and host, for the default CLI layout (64
cells) at 30 and 134 m/s with a rebase every 10 km, starting in the US (USGS first) and in Europe
(EOX first). The busiest minute of every host must stay below its netfetch policy rate, so a layout
change that raises network demand fails here instead of against a server. The 250 m/s flight is
left out: §2.2 measured ~21 req/s to USGS there (the far field selects by horizontal distance), so
the limiter caps it (ROADMAP: select by 3D distance in flight).
"""
import math
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dem as demmod  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy  # noqa: E402
import imagery  # noqa: E402
import netfetch  # noqa: E402

import numpy as np  # noqa: E402

HZ = 4.0                    # pose rate of the drive (above 30 m/s from M3a; the review's model used 4 Hz)
SECS, WARM = 60.0, 10.0
HEADING = 37.0
START = (1234.0, -777.0)
TOL = 0.10                  # ±10 %
TOL_ABS = 0.3               # tiles/s: the floor of the per-level tolerance
COARSE = 4                  # levels >= this are one column of the table ("L>=4")


def straight(v, heading=HEADING, start=START):
    hx, hy = math.sin(math.radians(heading)), math.cos(math.radians(heading))
    return lambda t: (start[0] + v * hx * t, start[1] + v * hy * t, v * hx, v * hy)


def layout_select(layout):
    """The selection earthd.Server makes in mesh mode with this layout (Connection.update_far)."""
    def select(px, py, vx, vy, prev, hold):
        return ff.select(px, py, (), prev, layout.k, ff.HYSTERESIS, ff.RADIUS, hold=hold,
                         min_level=layout.min_level, radii=layout.near_radii, layout=layout)
    return select


# The v1.3.1 selection (32 cells, the server's default) and M1's (docs/V1_4_SPEC.md §3.1: --cells 64;
# M4 makes the block part of the layout 512, which does not change the selection).
select_v131 = layout_select(ff.V131)
select_m1 = layout_select(ff.Layout(64))


def demand(select, path, secs=SECS, warm=WARM, hz=HZ):
    """(tiles at the first pose after the warm-up, adds/s, {level: adds/s}) of `select` along
    `path` (t -> (x, y, vx, vy)), with the server's hysteresis and HOLD_S bookkeeping."""
    prev, changed, cur, tiles = set(), {}, None, None
    adds = Counter()
    for k in range(int(round((secs + warm) * hz))):
        t = k / hz
        px, py, vx, vy = path(t)
        hold = {n for n, tc in changed.items() if t - tc < ff.HOLD_S}
        new, refined = select(px, py, vx, vy, prev, hold)
        if cur is not None:                 # the first selection of an anchor is not a change
            for n in refined ^ prev:
                changed[n] = t
            if t >= warm:
                for key, tile in new.items():
                    if key not in cur:
                        adds[tile.level] += 1
        if t >= warm and tiles is None:
            tiles = len(new)
        prev, cur = refined, new
    per = Counter()
    for lvl, n in adds.items():
        per[min(lvl, COARSE)] += n / secs
    return tiles, sum(adds.values()) / secs, dict(per)


# Table 2.1, v1.3.1 rows: v (m/s) -> (tiles at a pose, adds/s, {level: adds/s}); level COARSE = L>=4.
ROWS_V131 = {
    40: (811, 14.4, {-2: 4.5, -1: 4.0, 0: 3.1, 1: 1.6, 2: 0.7, 3: 0.4, COARSE: 0.3}),
    90: (811, 33.4, {-2: 10.4, -1: 9.1, 0: 6.8, 1: 4.0, 2: 1.6, 3: 0.9, COARSE: 0.7}),
    134: (811, 49.9, {-2: 15.6, -1: 13.3, 0: 10.3, 1: 5.8, 2: 2.7, 3: 1.2, COARSE: 1.1}),
}
# Table 2.1, "v1.4 M1 (64 cells, same vertex density, no look-ahead)" rows, for select_m1.
ROWS_M1 = {
    40: (300, 4.6, {-1: 1.6, 0: 1.3, 1: 0.9, 2: 0.5, 3: 0.2, COARSE: 0.1}),
    90: (300, 10.6, {-1: 3.6, 0: 3.0, 1: 2.0, 2: 1.1, 3: 0.4, COARSE: 0.5}),
    134: (300, 16.0, {-1: 5.4, 0: 4.5, 1: 3.1, 2: 1.8, 3: 0.8, COARSE: 0.5}),
}
# ---- M3a HOOK: the capsule rows (s = 1 at 134 m/s; s·v = 90 at 40/67/90/134; s·v = 60 at 134) and
# D(s) within ±15 % of the simulated rate at 40/90/134 m/s, s in {0.3, 0.5, 1}.
# ---- M5 HOOK: the flight rows (250 m/s at 300 m / 1 km / 3 km / 10 km AGL) with the disc and
# horizon cull.


class DemandTest(unittest.TestCase):
    def check_rows(self, select, rows):
        for v, (want_tiles, want_rate, want_levels) in rows.items():
            with self.subTest(v=v):
                tiles, rate, levels = demand(select, straight(v))
                print(f"\n  v={v:3d} m/s: {tiles} tiles, {rate:.1f} adds/s (spec {want_tiles}, {want_rate}); "
                      + " ".join(f"L{'>=' if lvl == COARSE else ''}{lvl}:{levels.get(lvl, 0):.1f}"
                                 for lvl in sorted(want_levels)), file=sys.stderr)
                self.assertLessEqual(abs(tiles - want_tiles), TOL * want_tiles, f"tiles {tiles} vs {want_tiles}")
                self.assertLessEqual(abs(rate - want_rate), TOL * want_rate, f"adds/s {rate:.2f} vs {want_rate}")
                self.assertEqual(set(levels) - set(want_levels), set(), "a level the table does not have")
                for lvl, want in want_levels.items():
                    got = levels.get(lvl, 0.0)
                    self.assertLessEqual(abs(got - want), max(TOL * want, TOL_ABS), f"L{lvl}: {got:.2f} vs {want}")

    def test_v131_rows(self):
        """Table 2.1, v1.3.1 layout (32 cells, L-2 exact within 150 m, L-1 within 400 m)."""
        self.check_rows(select_v131, ROWS_V131)

    def test_demand_scales_with_speed(self):
        """The ring model of §2.1: the rate is proportional to v (the tile set is not)."""
        r40 = demand(select_v131, straight(40.0))[1]
        r134 = demand(select_v131, straight(134.0))[1]
        self.assertAlmostEqual(r134 / r40, 134.0 / 40.0, delta=0.1 * 134.0 / 40.0)

    def test_stationary_adds_nothing(self):
        tiles, rate, _ = demand(select_v131, straight(0.0))
        self.assertEqual(rate, 0.0)
        self.assertGreater(tiles, 700)

    def test_m1_rows(self):
        """Table 2.1, the 64-cell layout (M1): ~1/3 of the v1.3.1 demand and objects."""
        self.check_rows(select_m1, ROWS_M1)

    def test_m1_against_v131(self):
        """M1's point (§3.1 key number): at 134 m/s the demand drops 49.9 -> 16.0 tiles/s and the
        objects 811 -> ~300 (each within ±10 % above; here the ratios)."""
        t32, r32, _ = demand(select_v131, straight(134.0))
        t64, r64, _ = demand(select_m1, straight(134.0))
        self.assertLess(r64 / r32, 0.4)
        self.assertLess(t64 / t32, 0.45)
        tiles, rate, _ = demand(select_m1, straight(0.0))
        self.assertEqual(rate, 0.0)
        self.assertTrue(280 <= tiles <= 330, tiles)


# --- network demand per host (rate-limit report §2.2 / §5.4 case 12) ---------------------------------
NET_LAYOUT = ff.Layout(64)          # the default CLI layout (mesh, 64 cells)
REBASE_M = 10000.0                  # the mod's default rebaseDistance
NET_STARTS = {"US": (39.2904, -76.6122, "usgs"), "EU": (48.1374, 11.5755, "eox2023")}   # Baltimore, Munich


def imagery_keys(frame, tile, px, src):
    """Source tile keys of one far tile's texture from src (imagery.render's zoom and mosaic rules)."""
    x0, y0, _, _ = tile.bbox
    gx0, gy0, size = imagery.gutter_square(x0, y0, tile.size, px)
    c = np.array([0.0, 0.5, 1.0]) * size
    E, N = np.meshgrid(gx0 + c, gy0 + c)
    lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
    z = min(imagery.zoom_for(float(lat[1, 1]), size / px), src.max_zoom)
    while True:
        n = 2 ** z
        fx, fy = imagery.mercator_px(lat.ravel(), lon.ravel(), z)
        tx0, tx1 = int(math.floor(fx.min() - 1)) // imagery.TILE, int(math.floor(fx.max() + 1)) // imagery.TILE
        ty0 = max(0, int(math.floor(fy.min() - 1)) // imagery.TILE)
        ty1 = min(n - 1, int(math.floor(fy.max() + 1)) // imagery.TILE)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= imagery.MAX_MOSAIC or z == 0:
            break
        z -= 1
    return {(z, tx % n, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)}


def dem_keys(frame, tile, zoom):
    """Mapterhorn 512 px tile keys a far tile's build samples at this zoom."""
    x0, y0, x1, y1 = tile.bbox
    c = np.linspace(0, 1, 5)
    E, N = np.meshgrid(x0 + c * (x1 - x0), y0 + c * (y1 - y0))
    lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
    n = 2 ** zoom
    fx = (lon / 360.0 + 0.5) * n * demmod.TILE
    fy = (0.5 - np.arcsinh(np.tan(np.radians(lat))) / (2 * np.pi)) * n * demmod.TILE
    tx0, tx1 = int(math.floor(fx.min())) // demmod.TILE, int(math.floor(fx.max()) + 1) // demmod.TILE
    ty0, ty1 = int(math.floor(fy.min())) // demmod.TILE, int(math.floor(fy.max()) + 1) // demmod.TILE
    return {(zoom, tx % n, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)}


def net_demand(lat, lon, src, v, minutes, heading=HEADING, hz=HZ):
    """(requests of the first selection, [per minute after it]) as {host: requests}, for a straight
    drive at v m/s from (lat, lon) with a rebase every REBASE_M. Mapterhorn counts every new DEM key;
    the AWS row is the worst case where Mapterhorn has no lidar (its 404s above GLOBAL_ZOOM fall back
    to AWS z+1, 4 tiles each)."""
    img_host = netfetch.host_of(src.url)
    mt, aws = netfetch.host_of(demmod.MAPTERHORN["url"]), netfetch.host_of(demmod.AWS["url"])
    seen_img, seen_dem = set(), set()
    hx, hy = math.sin(math.radians(heading)), math.cos(math.radians(heading))

    def anchor(la, lo):
        zooms = {lvl: NET_LAYOUT.dem_zoom(la, lvl) for lvl in range(1, ff.MAX_LEVEL + 2)}
        zooms[0] = demmod.zoom_for(la, 2.5)
        return geodesy.EnuFrame(la, lo), zooms

    frame, zooms = anchor(lat, lon)
    prev, changed, cur = set(), {}, None
    ox = oy = 0.0
    first, per_min, acc = None, [], Counter()
    for k in range(int(minutes * 60 * hz) + 1):
        t = k / hz
        px, py = ox + v * hx * t, oy + v * hy * t
        if math.hypot(px, py) > REBASE_M:           # the mod rebases: a new anchor at the player
            la, lo, _ = frame.enu_to_geodetic(px, py, 0.0)
            frame, zooms = anchor(float(la), float(lo))
            prev, changed, cur = set(), {}, None
            ox, oy, px, py = -v * hx * t, -v * hy * t, 0.0, 0.0
        hold = {n for n, tc in changed.items() if t - tc < ff.HOLD_S}
        new, refined = ff.select(px, py, (), prev, NET_LAYOUT.k, ff.HYSTERESIS, ff.RADIUS, hold=hold,
                                 min_level=NET_LAYOUT.min_level, radii=NET_LAYOUT.near_radii, layout=NET_LAYOUT)
        if cur is not None:
            for n in refined ^ prev:
                changed[n] = t
        req = Counter()
        for key, tile in new.items():
            if cur is not None and key in cur:
                continue
            new_img = imagery_keys(frame, tile, NET_LAYOUT.tex_size(tile.level), src) - seen_img
            seen_img |= new_img
            req[img_host] += len(new_img)
            for z in {zooms[max(tile.level, 0)]} | ({zooms[tile.level - 1]} if tile.level > 0 else set()):
                new_dem = dem_keys(frame, tile, z) - seen_dem
                seen_dem |= new_dem
                req[mt] += len(new_dem)
                if z > demmod.GLOBAL_ZOOM:
                    req[aws] += len(new_dem) * 4 ** (min(demmod.MAX_ZOOM, z + 1) - z)
        prev, cur = refined, new
        if first is None:
            first = req
            continue
        acc.update(req)
        if k % int(60 * hz) == 0:
            per_min.append(acc)
            acc = Counter()
    return first, per_min


class NetDemandTest(unittest.TestCase):
    MINUTES = 3

    def test_steady_demand_below_the_host_rates(self):
        """The busiest minute of every host, cold cache, stays below its policy rate (§5.2)."""
        for start, (lat, lon, name) in NET_STARTS.items():
            for v in (30, 134):
                with self.subTest(start=start, v=v):
                    first, per_min = net_demand(lat, lon, imagery.SOURCES[name], v, self.MINUTES)
                    self.assertEqual(len(per_min), self.MINUTES)
                    peak = Counter()
                    for m in per_min:
                        for host, n in m.items():
                            peak[host] = max(peak[host], n)
                    print(f"\n  {start} {v:3d} m/s: start burst " + ", ".join(f"{h} {n}" for h, n in sorted(first.items()))
                          + "; peak/min " + ", ".join(f"{h} {n} ({n / 60:.1f}/s)" for h, n in sorted(peak.items())),
                          file=sys.stderr)
                    for host, n in peak.items():
                        rate = netfetch.POLICIES[host].rate
                        self.assertLess(n / 60.0, rate, f"{host}: {n} requests in a minute, policy {rate}/s")

    def test_start_burst_is_what_the_limiter_spreads(self):
        """The first selection's requests (a cold start or a teleport, §2.2: ~1,430 USGS, ~1,190 EOX)
        exceed every imagery host's burst many times over: they are what the token bucket spreads out
        (test_netfetch.ColdStartTest), not something the pools alone may send."""
        for start, (lat, lon, name) in NET_STARTS.items():
            with self.subTest(start=start):
                first, _ = net_demand(lat, lon, imagery.SOURCES[name], 0.0, 0)
                host = netfetch.host_of(imagery.SOURCES[name].url)
                # (5x since the 2026-10-16 raise: USGS burst 150, ~1,430 requests; 4x since 2026-11-07's: burst 300)
                self.assertGreater(first[host], 4 * netfetch.POLICIES[host].burst)


if __name__ == "__main__":
    unittest.main()
