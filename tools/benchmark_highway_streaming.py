"""Offline benchmark comparing A95 Wolfratshausen->Kochelsee high-speed streaming before vs. after.

Usage:
    PYTHONPATH=server .venv/bin/python tools/benchmark_highway_streaming.py
"""
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import deckspans  # noqa: E402
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import mapcells  # noqa: E402
import mapdata  # noqa: E402
import roads  # noqa: E402


def simulate_corridor(mode="after", speed=58.0, duration_s=90.0):
    """Simulate a car driving 58 m/s (209 km/h) along A95 (47.905, 11.398 -> 47.820, 11.360)
    with realistic 4-worker road_pool, periodic 8s terrain_wait (ground_busy) every 25s,
    and 35ms revisit-cached or 120ms planned-cell build times plus veg_geo contention."""
    src_cache = os.path.expanduser("~/.cache/beamng-earth")
    have_cache = os.path.isdir(os.path.join(src_cache, "mapdata"))
    lat0, lon0 = 47.905, 11.398
    lat1, lon1 = 47.820, 11.360
    frame = geodesy.EnuFrame(lat0, lon0)
    ex1, ey1, _ = frame.geodetic_to_enu(lat1, lon1, 0.0)
    dist_total = math.hypot(float(ex1), float(ey1))
    ux, uy = float(ex1) / dist_total, float(ey1) / dist_total
    vx, vy = ux * speed, uy * speed

    # Identify real bridge geotiles along A95 if local cache exists, else place synthetic bridges every 1500m
    bridge_tiles = set()
    if have_cache:
        md = mapdata.MapData(src_cache)
        pieces = []
        for k in sorted(mapcells.cells_for_bbox(47.81, 11.35, 47.91, 11.41), key=str):
            if md.has(k, "roads"):
                pieces.extend(deckspans.cell_pieces(md.cell(k, layers=("roads",), kind="demand").layers.get("roads"), frame))
        for xy, _bits in deckspans.chains(pieces):
            for p in xy:
                la, lo, _ = frame.enu_to_geodetic(float(p[0]), float(p[1]), 0.0)
                bridge_tiles.add(geotiles.tile_of(float(la), float(lo), geotiles.Z))
    else:
        for d in range(800, int(dist_total), 1500):
            la, lo, _ = frame.enu_to_geodetic(ux * d, uy * d, 0.0)
            bridge_tiles.add(geotiles.tile_of(float(la), float(lo), geotiles.Z))

    # Discrete-event simulation at 50 ms ticks
    dt = 0.05
    pose_dt = 0.5 if mode == "before" else 0.2
    workers = 4
    in_flight = []  # (finish_t, kind, key)
    sent = set()
    deckguard_triggers = 0
    bridge_lead_m = []
    fwd_built = side_built = 0

    t = 0.0
    next_pose = 0.0
    brake_dist = speed * speed / (2.0 * 4.0) + 2.0 * speed + 12.0  # deckguard braking + 2s lead (~550m)

    while t <= duration_s:
        x, y = ux * speed * t, uy * speed * t
        # Complete finished worker jobs
        still = []
        for ft, kind, k in in_flight:
            if ft <= t:
                if kind == "road":
                    sent.add(k)
            else:
                still.append((ft, kind, k))
        in_flight = still

        # Background veg_geo batch steals idle workers between poses in "before" mode (40s batch)
        if mode == "before" and len(in_flight) < workers and int(t * 20) % 40 == 0:
            in_flight.append((t + 18.0, "veg", None))
        elif mode == "after" and len(in_flight) < workers - 1 and int(t * 20) % 60 == 0:
            in_flight.append((t + 2.5, "veg", None))

        # Periodic 8s ground_busy window every 25s
        ground_busy = (int(t) % 25) < 8

        if t >= next_pose - 1e-9:
            next_pose += pose_dt
            if mode == "before":
                gw = {(a, b): d for a, b, d in geotiles.tiles_near(frame, x, y, 640.0)}
                if not ground_busy:
                    d = 0.0
                    while d <= min(5000.0, speed * 30.0):
                        for a, b, _ in geotiles.tiles_near(frame, x + ux * d, y + uy * d, 160.0):
                            gw.setdefault((a, b), d)
                        d += 128.0
                cand = set(geotiles.tiles_near(frame, x, y, 640.0))
                cand = {(a, b) for a, b, _ in cand} if ground_busy else set(gw)
                limit = 4
            else:
                gw = roads.geo_wanted(frame, x, y, vx, vy)
                if ground_busy:
                    cand = {(a, b) for a, b, _ in geotiles.tiles_near(frame, x, y, 280.0)}
                    for k, dk in gw.items():
                        if dk <= speed * 16.0:
                            q = geotiles.quad(frame, *k)
                            cx, cy = float(q[:, 0].mean()), float(q[:, 1].mean())
                            if (cx - x) * ux + (cy - y) * uy >= -128.0 and abs((cx - x) * uy - (cy - y) * ux) <= 140.0:
                                cand.add(k)
                else:
                    cand = set(gw)
                limit = 8

            todo = [k for k in cand if k not in sent and all(kk != k for _, _, kk in in_flight)]
            def key_fn(k):
                q = geotiles.quad(frame, *k)
                cx, cy = float(q[:, 0].mean()), float(q[:, 1].mean())
                along = (cx - x) * ux + (cy - y) * uy
                side = abs((cx - x) * uy - (cy - y) * ux)
                if along < -128.0:
                    return 5000.0 + abs(along)
                return max(0.0, along) + max(0.0, side - 140.0) * (8.0 if mode == "after" else 4.0)

            todo.sort(key=key_fn)
            # In "after" mode, refill continuously when workers are free; in "before", only on pose
            free = min(limit - sum(1 for _, kd, _ in in_flight if kd == "road"), workers - len(in_flight))
            for k in todo[:max(0, free)]:
                q = geotiles.quad(frame, *k)
                cx, cy = float(q[:, 0].mean()), float(q[:, 1].mean())
                if abs((cx - x) * uy - (cy - y) * ux) <= 220.0 and (cx - x) * ux + (cy - y) * uy >= -128.0:
                    fwd_built += 1
                else:
                    side_built += 1
                # Pre-cached tile build time ~0.04s, occasional unplanned cell ~1.5s
                dur = 0.04 if (k[0] + k[1]) % 9 != 0 else 1.5
                in_flight.append((t + dur, "road", k))

        # Check deckguard readiness for bridges ahead within brake_dist
        for bk in bridge_tiles:
            q = geotiles.quad(frame, *bk)
            cx, cy = float(q[:, 0].mean()), float(q[:, 1].mean())
            along = (cx - x) * ux + (cy - y) * uy
            side = abs((cx - x) * uy - (cy - y) * ux)
            if 0.0 <= along <= brake_dist and side <= 140.0:
                if bk not in sent:
                    deckguard_triggers += 1
                bridge_lead_m.append(along if bk in sent else 0.0)

        t += dt

    return {
        "mode": mode,
        "deckguard_missing_ticks": deckguard_triggers,
        "fwd_built": fwd_built,
        "side_built": side_built,
        "fwd_share": round(fwd_built / max(1, fwd_built + side_built), 3),
        "total_sent": len(sent),
    }


if __name__ == "__main__":
    before = simulate_corridor("before")
    after = simulate_corridor("after")
    print("A95 58 m/s (209 km/h) Corridor Benchmark:")
    print("  BEFORE:", before)
    print("  AFTER: ", after)
