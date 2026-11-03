#!/usr/bin/env python3
"""Road-following in-engine benchmark harness for BeamNG.Earth.

Follows the real road geometry (OSM GPS route) from a designated start waypoint (default: START)
towards an endpoint (default: TOP) with exact road banking, heading, and curved spline tracking.

Supports:
  - Distances: 1.0 km, 2.0 km, 3.0 km (or custom --km)
  - Speed sets: e.g. 20 m/s (72 km/h / 45 mph), 25 m/s (90 km/h / 56 mph), 30 m/s (108 km/h / 67 mph), 35.8 m/s (80 mph)
  - Staging modes:
      --mode cold: Teleport to START, settle 5 seconds, immediately begin driving.
      --mode intermediate: Teleport to START, settle 20 seconds, begin driving.
      --mode warm: Teleport to START, wait until far-field is fully ready (queued == 0, serverBacklog == 0), begin driving.

Usage:
  python tools/road_bench.py --mode cold --km 1.0 --speed 25.0 --out runs/bench_cold_1km
  python tools/road_bench.py --mode intermediate --km 2.0 --speed 25.0 --out runs/bench_inter_2km
  python tools/road_bench.py --mode warm --km 3.0 --speed 25.0 --out runs/bench_warm_3km
"""

import argparse
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import drive
import geocode
import mcp
import routing


def get_place(name, default_coords=None):
    """Retrieve latitude and longitude for a saved place name from beamngEarth_settings."""
    try:
        raw = mcp.call("run_lua", {"code": "return jsonEncode(beamngEarth_settings and beamngEarth_settings.uiPlaces and beamngEarth_settings.uiPlaces())"})
        data = json.loads(raw)
        for p in data.get("places", []):
            if p.get("name") and p["name"].strip().upper() == name.strip().upper():
                return float(p["lat"]), float(p["lon"])
        for r in data.get("recent", []):
            if r.get("name") and r["name"].strip().upper() == name.strip().upper():
                return float(r["lat"]), float(r["lon"])
    except Exception as e:
        pass
    return default_coords


def resolve_coords(val, default_name, fallback_coords):
    """Resolve coordinates from 'lat,lon' string, saved place name, or geocoding query."""
    if not val:
        val = default_name
    val = str(val).strip()

    # 1. Literal lat,lon coordinates
    if "," in val:
        parts = [float(x.strip()) for x in val.split(",")]
        return parts[0], parts[1], f"Coordinates ({parts[0]:.6f}, {parts[1]:.6f})"

    # 2. Saved place in BeamNG settings (e.g. START, TOP, bookmarks)
    saved = get_place(val, None)
    if saved:
        return saved[0], saved[1], f"{val} (in-game bookmark)"

    # 3. Online geocoding via Photon / OpenStreetMap
    try:
        results = geocode.search(val)
        if results:
            top = results[0]
            desc = top.get("name", val)
            if top.get("detail"):
                desc += f", {top['detail']}"
            return float(top["lat"]), float(top["lon"]), f"{desc} [{top.get('kind', 'place')}]"
    except Exception as e:
        print(f"  Geocoding search for '{val}' warning: {e}")

    # 4. Fallback default coordinates
    return fallback_coords[0], fallback_coords[1], f"{default_name} (default fallback)"


def geodetic_to_enu(lat, lon, lat0, lon0):
    """Convert geodetic (lat, lon) to local ENU (East, North) in meters relative to (lat0, lon0)."""
    m = 111320.0
    rad_lat0 = math.radians(lat0)
    e = (lon - lon0) * m * math.cos(rad_lat0)
    n = (lat - lat0) * m
    return e, n


def plan_road_profile(start_lat, start_lon, top_lat, top_lon, target_km, speed_mps, step_m=2.0):
    """Fetch unsimplified OSM road route and fit a smooth centripetal Catmull-Rom spline to track the exact centerline."""
    print(f"Planning route from ({start_lat:.6f}, {start_lon:.6f}) to ({top_lat:.6f}, {top_lon:.6f})...", flush=True)
    plan = routing.plan(start_lat, start_lon, top_lat, top_lon, simplify_pts=False)
    route_pts = plan.get("points", [])
    if not route_pts or len(route_pts) < 2:
        raise RuntimeError(f"Routing failed or returned insufficient points ({len(route_pts)} points)")

    lat0, lon0 = route_pts[0]
    enu_pts = [geodetic_to_enu(la, lo, lat0, lon0) for la, lo in route_pts]

    # Clean near-duplicate points (< 0.5m)
    clean = [enu_pts[0]]
    for p in enu_pts[1:]:
        if math.hypot(p[0] - clean[-1][0], p[1] - clean[-1][1]) > 0.5:
            clean.append(p)

    n = len(clean)
    p_pre = (2 * clean[0][0] - clean[1][0], 2 * clean[0][1] - clean[1][1])
    p_post = (2 * clean[-1][0] - clean[-2][0], 2 * clean[-1][1] - clean[-2][1])
    ext = [p_pre] + clean + [p_post]

    # Generate dense centripetal Catmull-Rom spline points (~0.5m density)
    dense_pts = []
    for i in range(1, n):
        p0, p1, p2, p3 = ext[i-1], ext[i], ext[i+1], ext[i+2]
        chord = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        num_sub = max(2, int(math.ceil(chord / 0.5)))

        t0 = 0.0
        t1 = t0 + math.hypot(p1[0] - p0[0], p1[1] - p0[1]) ** 0.5
        t2 = t1 + math.hypot(p2[0] - p1[0], p2[1] - p1[1]) ** 0.5
        t3 = t2 + math.hypot(p3[0] - p2[0], p3[1] - p2[1]) ** 0.5

        for k in range(num_sub if i < n - 1 else num_sub + 1):
            t = t1 + (t2 - t1) * (k / num_sub)
            a1_x = (t1 - t)/(t1 - t0)*p0[0] + (t - t0)/(t1 - t0)*p1[0]
            a1_y = (t1 - t)/(t1 - t0)*p0[1] + (t - t0)/(t1 - t0)*p1[1]
            a2_x = (t2 - t)/(t2 - t1)*p1[0] + (t - t1)/(t2 - t1)*p2[0]
            a2_y = (t2 - t)/(t2 - t1)*p1[1] + (t - t1)/(t2 - t1)*p2[1]
            a3_x = (t3 - t)/(t3 - t2)*p2[0] + (t - t2)/(t3 - t2)*p3[0]
            a3_y = (t3 - t)/(t3 - t2)*p2[1] + (t - t2)/(t3 - t2)*p3[1]

            b1_x = (t2 - t)/(t2 - t0)*a1_x + (t - t0)/(t2 - t0)*a2_x
            b1_y = (t2 - t)/(t2 - t0)*a1_y + (t - t0)/(t2 - t0)*a2_y
            b2_x = (t3 - t)/(t3 - t1)*a2_x + (t - t1)/(t3 - t1)*a3_x
            b2_y = (t3 - t)/(t3 - t1)*a2_y + (t - t1)/(t3 - t1)*a3_y

            cx = (t2 - t)/(t2 - t1)*b1_x + (t - t1)/(t2 - t1)*b2_x
            cy = (t2 - t)/(t2 - t1)*b1_y + (t - t1)/(t2 - t1)*b2_y
            dense_pts.append((cx, cy))

    # Calculate cumulative distance along spline
    cum_s = [0.0]
    for (x1, y1), (x2, y2) in zip(dense_pts, dense_pts[1:]):
        cum_s.append(cum_s[-1] + math.hypot(x2 - x1, y2 - y1))

    total_route_m = cum_s[-1]
    if target_km is not None and target_km > 0:
        limit_m = min(total_route_m, target_km * 1000.0)
    else:
        limit_m = total_route_m

    print(f"Full road route: {total_route_m / 1000.0:.2f} km ({len(route_pts)} raw OSM nodes). Drive distance: {limit_m / 1000.0:.2f} km.", flush=True)

    # Resample at step_m intervals along the continuous spline
    samples = []
    curr_s = 0.0
    idx = 0

    while curr_s <= limit_m:
        while idx < len(cum_s) - 2 and cum_s[idx + 1] < curr_s:
            idx += 1

        s0, s1 = cum_s[idx], cum_s[idx + 1]
        x0, y0 = dense_pts[idx]
        x1, y1 = dense_pts[idx + 1]
        f = (curr_s - s0) / max(1e-6, s1 - s0)
        rx = x0 + f * (x1 - x0)
        ry = y0 + f * (y1 - y0)

        # Smooth tangent from surrounding dense samples (+- 2m window)
        i_fwd = min(len(dense_pts) - 1, idx + 4)
        i_bwd = max(0, idx - 4)
        tx = dense_pts[i_fwd][0] - dense_pts[i_bwd][0]
        ty = dense_pts[i_fwd][1] - dense_pts[i_bwd][1]
        heading = math.degrees(math.atan2(tx, ty)) % 360.0

        samples.append({
            "s": round(curr_s, 2),
            "speed": float(speed_mps),
            "heading": round(heading, 2),
            "x": round(rx, 3),
            "y": round(ry, 3)
        })
        curr_s += step_m

    # Ensure exact final boundary point
    if samples[-1]["s"] < limit_m:
        while idx < len(cum_s) - 2 and cum_s[idx + 1] < limit_m:
            idx += 1
        s0, s1 = cum_s[idx], cum_s[idx + 1]
        x0, y0 = dense_pts[idx]
        x1, y1 = dense_pts[idx + 1]
        f = (limit_m - s0) / max(1e-6, s1 - s0)
        rx = x0 + f * (x1 - x0)
        ry = y0 + f * (y1 - y0)
        i_fwd = min(len(dense_pts) - 1, idx + 4)
        i_bwd = max(0, idx - 4)
        tx = dense_pts[i_fwd][0] - dense_pts[i_bwd][0]
        ty = dense_pts[i_fwd][1] - dense_pts[i_bwd][1]
        heading = math.degrees(math.atan2(tx, ty)) % 360.0
        samples.append({
            "s": round(limit_m, 2),
            "speed": float(speed_mps),
            "heading": round(heading, 2),
            "x": round(rx, 3),
            "y": round(ry, 3)
        })

    return {
        "points": samples,
        "km": round(limit_m / 1000.0, 3),
        "lat0": lat0,
        "lon0": lon0,
        "enu_start": (samples[0]["x"], samples[0]["y"]),
        "initial_heading": samples[0]["heading"]
    }


def teleport_and_stage(start_lat, start_lon, mode):
    """Teleport to start position and apply cold, intermediate, or warm settling."""
    print(f"\n[1/3] Teleporting player to START ({start_lat:.6f}, {start_lon:.6f})...", flush=True)
    mcp.quiet_ui()
    cmd = f"return beamngEarth_core.teleport({start_lat}, {start_lon}, 'road_bench_start')"
    res = mcp.call("run_lua", {"code": cmd})
    print(f"Teleport response: {res.strip()}", flush=True)

    # Wait for anchor to initialize
    print("Waiting for world anchor to acknowledge...", flush=True)
    time.sleep(3.0)

    print(f"\n[2/3] Staging mode: '{mode.upper()}'")
    if mode == "cold":
        print("Cold start: waiting 5 seconds before driving...", flush=True)
        time.sleep(5.0)
    elif mode == "intermediate":
        print("Intermediate start: waiting 20 seconds before driving...", flush=True)
        time.sleep(20.0)
    elif mode == "warm":
        print("Warm start: waiting for full map readiness (status Far field ready, queued == 0, backlog == 0)...", flush=True)
        t_start = time.time()
        while time.time() - t_start < 120.0:
            try:
                code = """
local raw = beamngEarth_core.status()
local f = loadstring('return ' .. raw)
local s = f and f() or {}
local far = s.far or {}
return jsonEncode({
  status = s.status or '',
  queued = far.queued or 0,
  backlog = s.serverBacklog or 0,
  done = far.done or false,
  anchorId = s.anchorId or 0
})"""
                status_raw = mcp.call("run_lua", {"code": code})
                info = json.loads(status_raw)
                status_text = info.get("status", "")
                queued = info.get("queued", 0)
                backlog = info.get("backlog", 0)
                done = info.get("done", False)
                print(f"  Status: {status_text} | Queued: {queued} | Backlog: {backlog}", flush=True)
                if ("Far field ready" in status_text or done) and queued == 0 and backlog == 0:
                    print("Map is fully loaded and warm!", flush=True)
                    break
            except Exception as e:
                print(f"  polling status: {e}", flush=True)
            time.sleep(1.0)
        else:
            print("Warning: Timed out waiting for full readiness, proceeding anyway.", flush=True)


def snap_vehicle_to_road(enu_x, enu_y, heading):
    """Snap vehicle directly onto the road start point facing along the road heading."""
    snap_lua = f"""
local veh = getPlayerVehicle(0)
if not veh then return 'no vehicle' end
local z = be:getSurfaceHeightBelow(vec3({enu_x}, {enu_y}, 2500))
if not z or z < -1000 then z = veh:getPosition().z end
local rot = quatFromDir(vec3(math.sin(math.rad({heading})), math.cos(math.rad({heading})), 0), vec3(0, 0, 1))
veh:setPositionRotation({enu_x}, {enu_y}, z + 0.5, rot.x, rot.y, rot.z, rot.w)
veh:applyClusterVelocityScaleAdd(-1, 0, 0, 0, 0)
return string.format('snapped to (%.2f, %.2f, %.2f) heading %.1f', {enu_x}, {enu_y}, z + 0.5, {heading})
"""
    res = mcp.call("run_lua", {"code": snap_lua})
    print(f"Vehicle placement: {res.strip()}", flush=True)
    time.sleep(2.0)


def format_results_table(summary, mode, km, speed_mps):
    """Format and print an executive benchmark comparison table."""
    mean_fps = summary.get("fpsMean", 0.0)
    p50_ms = summary.get("p50", 0.0)
    p95_ms = summary.get("p95", 0.0)
    p99_ms = summary.get("p99", 0.0)
    peak_commit = summary.get("commitFrameMaxMs", 0.0)
    peak_hitch = summary.get("maxMs", 0.0)
    eff_speed = summary.get("effectiveSpeed", 0.0)
    nom_speed = summary.get("nominalSpeed", speed_mps)
    backlog_max = summary.get("backlogMax", 0)
    passed = summary.get("pass", False)
    falls = summary.get("falls", 0)
    resets = summary.get("resets", 0)

    print("\n" + "=" * 78)
    print(f"  BEAMNG.EARTH ROAD-FOLLOWING BENCHMARK REPORT: {mode.upper()} ({km:.1f} km)")
    print("=" * 78)
    print(f"  Route Profile:        OSM Mont Ventoux D974 GPS Track (Exact Curve Following)")
    print(f"  Staging Mode:         {mode.upper()}")
    print(f"  Test Distance:        {km:.2f} km")
    print(f"  Target Speed:         {speed_mps:.1f} m/s ({speed_mps * 3.6:.1f} km/h | {speed_mps * 2.23694:.1f} mph)")
    print(f"  Effective Speed:      {eff_speed:.2f} m/s ({eff_speed / max(1e-3, nom_speed) * 100.0:.1f}% nominal)")
    print("-" * 78)
    print(f"  Mean Frame Rate:      {mean_fps:.1f} FPS")
    print(f"  Frame Time (p50):     {p50_ms:.2f} ms")
    print(f"  Frame Time (p95):     {p95_ms:.2f} ms")
    print(f"  Frame Time (p99):     {p99_ms:.2f} ms")
    print(f"  Peak Commit Hitch:    {peak_commit:.1f} ms" if peak_commit else "  Peak Commit Hitch:    None")
    print(f"  Peak Engine Hitch:    {peak_hitch:.1f} ms")
    print(f"  Peak Server Backlog:  {backlog_max} tiles")
    print(f"  Physics Falls/Resets: {falls} falls / {resets} resets")
    print("-" * 78)
    print(f"  OVERALL RESULT:       {'PASS (Nominal Speed Achieved)' if passed else 'FAIL (Below 95% Speed or Driver Interrupted)'}")
    print("=" * 78 + "\n")


def main():
    parser = argparse.ArgumentParser(description="BeamNG.Earth GPS Road-Following In-Game Autonomous Test Driver")
    parser.add_argument("--from", "--start", dest="start", default="START",
                        help="Starting waypoint name, city/address, or 'lat,lon' (default: START)")
    parser.add_argument("--to", "--top", dest="top", default="TOP",
                        help="Ending waypoint name, city/address, or 'lat,lon' (default: TOP)")
    parser.add_argument("--km", type=float, default=None,
                        help="Distance to drive in km (default: full route to destination)")
    parser.add_argument("--speed", type=float, default=None,
                        help="Target driving speed in m/s (e.g. 25.0, 30.0, 40.2)")
    parser.add_argument("--mph", type=float, default=None,
                        help="Target driving speed in mph (e.g. 45, 60, 80, 90)")
    parser.add_argument("--kmh", type=float, default=None,
                        help="Target driving speed in km/h (e.g. 70, 90, 110, 130)")
    parser.add_argument("--mode", choices=("cold", "intermediate", "warm"), default="warm",
                        help="cold (5s settle), intermediate (20s settle), warm (wait for full load)")
    parser.add_argument("--step", type=float, default=2.0,
                        help="Sampling interval in meters along the road centerline (default: 2.0m)")
    parser.add_argument("--out", default=None,
                        help="Output directory for frames, samples, and summary")
    parser.add_argument("--use-player-vehicle", action="store_true",
                        help="Drive user's active vehicle instead of spawning stock test car")
    parser.add_argument("--dry-run", "--plan-only", dest="dry_run", action="store_true",
                        help="Plan and inspect the route without starting the game drive")
    args = parser.parse_args()

    # Determine driving speed
    if args.mph is not None:
        speed_mps = args.mph * 0.44704
    elif args.kmh is not None:
        speed_mps = args.kmh / 3.6
    elif args.speed is not None:
        speed_mps = args.speed
    else:
        speed_mps = 25.0  # default 90 km/h / 56 mph

    # 1. Resolve coordinates
    start_lat, start_lon, start_desc = resolve_coords(args.start, "START", (44.171279, 5.139169))
    top_lat, top_lon, top_desc = resolve_coords(args.top, "TOP", (44.173433, 5.277728))

    print("\n" + "=" * 78)
    print("  BEAMNG.EARTH ROAD TEST DRIVER ROUTE CONFIGURATION")
    print("=" * 78)
    print(f"  Start:        {start_desc}")
    print(f"                Coordinates: ({start_lat:.6f}, {start_lon:.6f})")
    print(f"  Destination:  {top_desc}")
    print(f"                Coordinates: ({top_lat:.6f}, {top_lon:.6f})")
    print(f"  Target Speed: {speed_mps:.1f} m/s ({speed_mps * 3.6:.1f} km/h | {speed_mps * 2.23694:.1f} mph)")
    print(f"  Staging Mode: {args.mode.upper()}")
    print("=" * 78)

    # 2. Plan road-following profile along OSM GPS path
    profile_data = plan_road_profile(start_lat, start_lon, top_lat, top_lon, args.km, speed_mps, step_m=args.step)
    effective_km = profile_data["km"]

    # Determine output directory
    if not args.out:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        args.out = os.path.join(ROOT, "runs", f"drive_{args.mode}_{effective_km:.1f}km_{timestamp}")
    os.makedirs(args.out, exist_ok=True)

    profile_path = os.path.join(args.out, "road_profile.json")
    with open(profile_path, "w") as f:
        json.dump(profile_data, f, indent=1)
    print(f"Saved road profile with {len(profile_data['points'])} curve points to {profile_path}")

    if args.dry_run:
        print(f"\n[DRY RUN COMPLETE] Route planned successfully ({effective_km:.2f} km, {len(profile_data['points'])} centerline samples).")
        return 0

    # 3. Teleport and apply staging delay
    teleport_and_stage(start_lat, start_lon, args.mode)

    # 4. Snap vehicle to start of road
    snap_vehicle_to_road(profile_data["enu_start"][0], profile_data["enu_start"][1], profile_data["initial_heading"])

    # 5. Execute benchmark via drive.py
    print(f"\n[3/3] Commencing road-following drive for {effective_km:.2f} km at {speed_mps:.1f} m/s ({speed_mps * 3.6:.1f} km/h)...", flush=True)
    drive_args = argparse.Namespace(
        out=args.out,
        profile=profile_path,
        speed=None,
        heading=None,
        km=effective_km,
        label=f"{args.mode}_{effective_km:.1f}km",
        poll=1.0,
        shots=0.0,
        max_s=7200.0,
        exact_level=None,
        use_player_vehicle=args.use_player_vehicle,
        step_mode="ge"
    )

    polls = drive.run_ge(drive_args, profile_data)
    summary = drive.summarize(polls, profile_data, label=drive_args.label, visual=False)
    summary_path = os.path.join(args.out, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=1)

    format_results_table(summary, args.mode, effective_km, speed_mps)
    return 0 if summary.get("pass") else 1


if __name__ == "__main__":
    sys.exit(main())

