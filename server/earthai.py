"""BeamNG Earth-only lane follower graph, independent of BeamNG's traffic navgraph.

The graph is built from OpenStreetMap ways.  Coordinates passed here are local metres in a
caller-selected tangent frame; the server owns projection and persistence.  This module stays
pure so route selection can be tested without BeamNG or the live OSM service.
"""
from __future__ import annotations

import math
import time

import numpy as np


LANE_M = 3.3
DEFAULT_SPEED_KMH = {
    "motorway": 110, "motorway_link": 60, "trunk": 90, "trunk_link": 55,
    "primary": 70, "primary_link": 50, "secondary": 60, "secondary_link": 45,
    "tertiary": 50, "tertiary_link": 40, "unclassified": 40, "residential": 35,
    "living_street": 15, "service": 20, "track": 25, "road": 30,
}
DRIVABLE = frozenset(DEFAULT_SPEED_KMH)
YES = frozenset(("yes", "1", "true"))
NO = frozenset(("no", "0", "false"))
QUERY_RADIUS_M = 4200.0
SPAWN_MIN_M = 70.0
SPAWN_MAX_M = 1200.0
ROUTE_M = 2400.0


def _count(value, default=None):
    """Parse an OSM lane count (including semicolon-separated values)."""
    try:
        result = int(str(value).split(";")[0].strip())
        return result if 1 <= result <= 12 else default
    except (TypeError, ValueError):
        return default


def _speed_kmh(value, highway):
    """Parse a mapped maxspeed or choose a conservative class default."""
    if value is not None:
        raw = str(value).strip().lower()
        try:
            amount = float(raw.split(";")[0].split()[0])
            if "mph" in raw:
                amount *= 1.609344
            if 5 <= amount <= 200:
                return amount
        except (ValueError, IndexError):
            pass
    return float(DEFAULT_SPEED_KMH.get(highway, 30))


def _line(points):
    p = np.asarray(points, dtype=np.float64)
    if p.ndim != 2 or p.shape[0] < 2 or p.shape[1] < 2 or not np.isfinite(p[:, :2]).all():
        return None
    p = p[:, :2].copy()
    keep = np.r_[True, np.hypot(*np.diff(p, axis=0).T) > 0.05]
    p = p[keep]
    return p if len(p) >= 2 else None


def _offset(points, distance):
    """Offset a polyline to its left by metres, using averaged vertex normals."""
    p = np.asarray(points, dtype=np.float64)
    d = np.diff(p, axis=0)
    lengths = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
    normals = np.column_stack((-d[:, 1] / lengths, d[:, 0] / lengths))
    out = np.empty_like(p)
    out[0] = p[0] + distance * normals[0]
    out[-1] = p[-1] + distance * normals[-1]
    if len(p) > 2:
        n = normals[:-1] + normals[1:]
        size = np.maximum(np.hypot(n[:, 0], n[:, 1]), 1e-6)
        n /= size[:, None]
        out[1:-1] = p[1:-1] + distance * n
    return out


def _tag(way, name, default=None):
    return way.get(name, default)


def _oneway(value):
    raw = str(value or "").strip().lower()
    if raw == "-1":
        return -1
    if raw in YES:
        return 1
    if raw in NO:
        return 0
    return None


def _junction_key(point, snap_m=0.75):
    """A stable local-metre key for connecting way endpoints in the lane graph."""
    return (round(float(point[0]) / snap_m), round(float(point[1]) / snap_m))


def geographic_bounds(lat, lon, radius_m):
    """One or two lon/lat boxes covering a player-centred circle with a small margin."""
    lat = max(-89.999, min(89.999, float(lat)))
    lon = float(lon)
    dlat = float(radius_m) / 110900.0
    dlon = min(180.0, float(radius_m) / (111000.0 * max(0.01, math.cos(math.radians(lat)))))
    south, north = max(-90.0, lat - dlat), min(90.0, lat + dlat)
    west, east = lon - dlon, lon + dlon
    if west < -180.0:
        return [(-180.0, south, east, north), (west + 360.0, south, 180.0, north)]
    if east > 180.0:
        return [(-180.0, south, east - 360.0, north), (west, south, 180.0, north)]
    return [(west, south, east, north)]


def project_ways(ways, frame):
    """Copy geographic OSM way rows into the local metre frame expected by this module."""
    out = []
    for way in ways:
        coords = np.asarray(_tag(way, "points", ()), dtype=np.float64)
        if coords.ndim != 2 or len(coords) < 2 or coords.shape[1] < 2:
            continue
        east, north, _up = frame.geodetic_to_enu(coords[:, 1], coords[:, 0], 0.0)
        row = dict(way)
        row["points"] = np.column_stack((east, north))
        out.append(row)
    return out


def geographic_plan(plan, frame, elevation_fn):
    """Convert a local spawn plan to serializable [lat, lon, height] routes."""
    result = []
    for item in plan:
        route = np.asarray(item["route"], dtype=np.float64)
        lat, lon, _h = frame.enu_to_geodetic(route[:, 0], route[:, 1], 0.0)
        ground = np.asarray(elevation_fn(np.asarray(lat), np.asarray(lon)), dtype=np.float64)
        points = [[float(a), float(b), float(c) + 0.35] for a, b, c in zip(lat, lon, ground)]
        spawn_lat, spawn_lon, _spawn_h = frame.enu_to_geodetic(item["x"], item["y"], 0.0)
        spawn_ground = float(elevation_fn(np.asarray([spawn_lat]), np.asarray([spawn_lon]))[0])
        result.append({"lat": float(spawn_lat), "lon": float(spawn_lon), "h": spawn_ground + 0.6,
                       "speed": float(item["speed"]), "distance": float(item["distance"]), "route": points})
    return result


def build_lane_graph(ways, *, left_hand=False, lane_width=LANE_M):
    """Expand OSM ways to lane-centre polylines and build straight-continuation links.

    Each input way is a mapping with ``id``, ``highway``, ``points`` (local x/y metres), and
    optional OSM ``oneway``, ``lanes``, ``lanes:forward``, ``lanes:backward``, ``maxspeed`` and
    ``name`` tags. Junction continuation always favours the smallest change in heading; equal
    candidates prefer the same named way, then the same road class and stable OSM way id.
    """
    ways_by_id = {}
    for way in ways:
        highway = str(_tag(way, "highway", ""))
        pts = _line(_tag(way, "points", ()))
        if highway not in DRIVABLE or pts is None:
            continue
        # Keep the follower on ordinary public motor roads. Do not send cars down private
        # access roads, driveways, parking aisles, or foot/cycle-only roads tagged as highways.
        access = str(_tag(way, "access", "")).strip().lower()
        motor = str(_tag(way, "motor_vehicle", "")).strip().lower()
        service = str(_tag(way, "service", "")).strip().lower()
        if access in {"no", "private"} and motor not in {"yes", "permissive", "designated", "destination"}:
            continue
        if motor in {"no", "private"}:
            continue
        if service in {"driveway", "parking_aisle", "emergency_access", "private"}:
            continue
        way_id = str(_tag(way, "id", ""))
        if not way_id:
            continue
        oneway = _oneway(_tag(way, "oneway"))
        total = _count(_tag(way, "lanes"))
        fwd = _count(_tag(way, "lanes:forward"))
        back = _count(_tag(way, "lanes:backward"))
        if oneway is None:
            oneway = 1 if highway in ("motorway", "motorway_link") else 0
        if oneway:
            lane_count = fwd or total or max(1, round(5.0 / lane_width))
            fwd, back = lane_count, 0
        else:
            if fwd is None and back is None:
                total = total or max(2, round(6.6 / lane_width))
                fwd = max(1, total // 2)
                back = max(1, total - fwd)
            elif fwd is None:
                fwd = max(1, (total or (back + 1)) - back)
            elif back is None:
                back = max(1, (total or (fwd + 1)) - fwd)
        ways_by_id[way_id] = (way, highway, pts, oneway, fwd, back)

    lanes = {}
    by_start = {}
    by_end = {}
    for way_id, (way, highway, pts, oneway, fwd, back) in sorted(ways_by_id.items()):
        if oneway == 1:
            directions = [(1, fwd)]
        elif oneway == -1:
            directions = [(-1, fwd)]
        else:
            directions = [(1, fwd), (-1, back)]
        speed = _speed_kmh(_tag(way, "maxspeed"), highway) / 3.6
        for direction, count in directions:
            base = pts if direction == 1 else pts[::-1]
            if oneway:
                offsets = [(i - (count - 1) / 2) * lane_width for i in range(count)]
            else:
                # Lane centres lie in each travel direction's side of the way centreline.
                offsets = []
                for i in range(count):
                    side = 1 if left_hand else -1
                    offsets.append(side * (i + 0.5) * lane_width)
            for index, offset in enumerate(offsets):
                # _offset uses positive left-of-the-provided-polyline.
                path = _offset(base, offset)
                lane_id = f"{way_id}:{'f' if direction == 1 else 'b'}:{index}"
                start_key, end_key = _junction_key(base[0]), _junction_key(base[-1])
                lane = {
                    "id": lane_id, "way": way_id, "highway": highway,
                    "name": str(_tag(way, "name", "") or ""), "direction": direction,
                    "lane": index, "speed": speed, "points": path,
                    "start": start_key, "end": end_key, "next": None,
                }
                lanes[lane_id] = lane
                by_start.setdefault(start_key, []).append(lane_id)
                by_end.setdefault(end_key, []).append(lane_id)

    for lane in lanes.values():
        incoming = lane["points"][-1] - lane["points"][-2]
        ilen = max(float(np.hypot(*incoming)), 1e-9)
        incoming /= ilen
        choices = []
        for candidate_id in by_start.get(lane["end"], ()):
            candidate = lanes[candidate_id]
            if candidate_id == lane["id"] or candidate["way"] == lane["way"]:
                continue
            outgoing = candidate["points"][1] - candidate["points"][0]
            olen = max(float(np.hypot(*outgoing)), 1e-9)
            dot = float(np.dot(incoming, outgoing / olen))
            if dot < 0.5:  # avoid turns and U-turns; allow only a near-straight continuation
                continue
            gap = float(np.hypot(*(candidate["points"][0] - lane["points"][-1])))
            score = (dot, candidate["name"] == lane["name"] and bool(lane["name"]),
                     candidate["highway"] == lane["highway"], -gap, candidate["way"])
            choices.append((score, candidate_id))
        if choices:
            # Largest heading agreement first; then same name/class; then stable way id.
            choices.sort(key=lambda item: (-item[0][0], -int(item[0][1]), -int(item[0][2]),
                                            -item[0][3], item[0][4]))
            lane["next"] = choices[0][1]
    return lanes


def follow_route(graph, lane_id, distance_m=1200.0, start_m=0.0):
    """Concatenate the current lane and straight continuations up to a metre budget."""
    if lane_id not in graph or distance_m <= 0:
        return []
    out = []
    seen = set()
    remaining = float(distance_m)
    current = lane_id
    skip = max(0.0, float(start_m))
    while current in graph and current not in seen and remaining > 0:
        seen.add(current)
        lane = graph[current]
        points = np.asarray(lane["points"], dtype=np.float64)
        start_index = 0
        while start_index < len(points) - 1 and skip > 0:
            segment = points[start_index + 1] - points[start_index]
            length = float(np.hypot(*segment))
            if skip < length:
                fraction = skip / max(length, 1e-9)
                points = np.vstack((points[start_index] + fraction * segment, points[start_index + 1:]))
                start_index = 0
                skip = 0
                break
            skip -= length
            start_index += 1
        if start_index >= len(points) - 1:
            nxt = lane.get("next")
            if nxt is None or nxt in seen:
                break
            current, skip = nxt, max(0.0, skip)
            continue
        start = points[start_index]
        if not out:
            out.append((float(start[0]), float(start[1])))
        elif math.hypot(float(start[0]) - out[-1][0], float(start[1]) - out[-1][1]) > 0.05:
            # Keep the actual next lane's start in the route, so the controller follows its
            # centreline through the short offset gap at an OSM way join.
            out.append((float(start[0]), float(start[1])))
        for a, b in zip(points[start_index:-1], points[start_index + 1:]):
            length = float(np.hypot(*(b - a)))
            if length <= remaining:
                out.append((float(b[0]), float(b[1])))
                remaining -= length
            else:
                t = remaining / max(length, 1e-9)
                out.append((float(a[0] + t * (b[0] - a[0])), float(a[1] + t * (b[1] - a[1]))))
                remaining = 0
                break
        if remaining <= 0:
            break
        nxt = lane.get("next")
        if nxt is None or nxt in seen:
            break
        current = nxt
    return out


def _closest_station(points, position):
    """(distance from line, along-line station, nearest point) for a local polyline."""
    p = np.asarray(points, dtype=np.float64)
    q = np.asarray(position, dtype=np.float64)[:2]
    d = np.diff(p, axis=0)
    length = np.hypot(d[:, 0], d[:, 1])
    l2 = np.maximum(length * length, 1e-12)
    t = np.clip(np.einsum("ij,ij->i", q - p[:-1], d) / l2, 0, 1)
    projected = p[:-1] + t[:, None] * d
    distance = np.hypot(*(projected - q).T)
    i = int(np.argmin(distance))
    station = float(length[:i].sum() + t[i] * length[i])
    return float(distance[i]), station, projected[i]


def spawn_plan(graph, position, amount, *, min_distance=70.0, max_distance=1200.0,
               min_separation=35.0, route_distance=1600.0, samples_per_lane=5):
    """Select spread-out, reachable lane spawn poses around the player.

    The order is deterministic for a graph snapshot. Each result carries a route that only
    continues straight through the OSM network. Lanes with no forward route are still valid
    spawn choices; their vehicle will despawn when that lane ends.
    """
    amount = max(0, int(amount))
    if amount == 0:
        return []
    candidates = []
    p = np.asarray(position, dtype=np.float64)[:2]
    for lane_id in sorted(graph):
        lane = graph[lane_id]
        points = lane["points"]
        line_distance, station, _nearest = _closest_station(points, p)
        if line_distance > max_distance:
            continue
        lengths = np.hypot(*np.diff(np.asarray(points), axis=0).T)
        total = float(lengths.sum())
        if total <= 0:
            continue
        # Candidate stations are spread along the whole local line, then ranked by distance.
        n = max(2, min(samples_per_lane, int(total // 80) + 1))
        stations = {max(0.0, min(total, station + offset)) for offset in
                    np.linspace(-min(max_distance, total), min(max_distance, total), n)}
        for at in sorted(stations):
            start = follow_route(graph, lane_id, 0.1, at)
            if not start:
                continue
            x, y = start[0]
            distance = math.hypot(x - p[0], y - p[1])
            if not min_distance <= distance <= max_distance:
                continue
            speed = float(lane["speed"])
            candidates.append({"lane": lane_id, "x": x, "y": y,
                               "station": at, "speed": speed, "distance": distance})
    # Stable radial buckets keep spawns distributed rather than taking all nearest options.
    candidates.sort(key=lambda c: (round(c["distance"] / 50.0), c["distance"], c["lane"], c["x"], c["y"]))
    chosen = []
    for candidate in candidates:
        if all((candidate["x"] - old["x"]) ** 2 + (candidate["y"] - old["y"]) ** 2
               >= min_separation ** 2 for old in chosen):
            chosen.append(candidate)
            if len(chosen) >= amount:
                break
    for candidate in chosen:
        route = follow_route(graph, candidate["lane"], route_distance, candidate["station"])
        if len(route) < 2:
            candidate["route"] = []
            continue
        tangent = np.asarray(route[1]) - np.asarray(route[0])
        candidate["heading"] = math.atan2(tangent[1], tangent[0])
        candidate["route"] = route
    chosen = [candidate for candidate in chosen if len(candidate["route"]) >= 2]
    return chosen


class Tracker:
    """Per-client, opt-in Earth AI graph refresh; separate from the traffic graph tracker."""

    REBUILD_M = 250.0
    REPLENISH_S = 10.0

    def __init__(self):
        self.enabled = False
        self.amount = 0
        self.aid = None
        self.centre = None
        self.building = False
        self.serial = 0
        self.last_build = -math.inf
        self.pool = None

    def close(self):
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def on_pose(self, conn, aid, lat, lon, enabled, amount, active, queued, loop, build):
        self.enabled = enabled is True
        self.amount = max(0, min(32, int(amount or 0)))
        if not self.enabled or self.amount == 0 or aid is None:
            if self.aid is not None or self.building:
                self.serial += 1
            self.centre = None
            self.aid = None
            return
        lat, lon = float(lat), float(lon)
        if self.building:
            return
        now = time.monotonic()
        needs_more = max(0, int(active or 0)) + max(0, int(queued or 0)) < self.amount
        moved = self.aid != aid or self.centre is None
        if self.aid == aid and self.centre is not None:
            dlat = math.radians(lat - self.centre[0])
            dlon = math.radians(lon - self.centre[1]) * math.cos(math.radians(lat))
            moved = (dlat * 6371000) ** 2 + (dlon * 6371000) ** 2 >= self.REBUILD_M ** 2
        if not moved and (not needs_more or now - self.last_build < self.REPLENISH_S):
            return
        self.building = True
        self.last_build = now
        # Record the attempted centre before dispatch. If OSM is offline or the query fails,
        # the next pose is throttled instead of submitting the same failed build every 0.5 s.
        self.aid, self.centre = aid, (lat, lon)
        self.serial += 1
        serial = self.serial
        amount = self.amount
        if self.pool is None:
            from runtimeprofiler import ThreadPoolExecutor
            self.pool = ThreadPoolExecutor(1, thread_name_prefix="earth-lane-ai")
        fut = loop.run_in_executor(self.pool, build, aid, lat, lon, amount)
        fut.add_done_callback(lambda f: self._built(conn, aid, (lat, lon), amount, serial, f))

    def _built(self, conn, aid, centre, amount, serial, future):
        self.building = False
        try:
            routes = future.result()
        except Exception as error:
            import logging
            logging.getLogger("earthd.earthai").debug("OSM lane graph %s: %s: %s", centre,
                                                       type(error).__name__, error)
            return
        if serial != self.serial or not self.enabled or amount != self.amount:
            return
        self.aid, self.centre = aid, centre
        conn.send({"type": "earth_ai_graph", "anchorId": aid, "lat": centre[0], "lon": centre[1],
                   "count": amount, "routes": routes, "serial": serial})


def speed_target(speed_mps, target_mps, *, kp=0.12, max_throttle=0.65, max_brake=0.8):
    """Simple bounded longitudinal command for the isolated lane follower."""
    error = float(target_mps) - max(0.0, float(speed_mps))
    if error >= 0:
        return min(max_throttle, max(0.0, kp * error)), 0.0
    return 0.0, min(max_brake, max(0.0, -kp * error))


def pure_pursuit_steering(position, heading, target, wheelbase=2.7, steer_limit=0.7):
    """Return normalized steering toward one lane-centre lookahead point."""
    px, py = map(float, position[:2])
    tx, ty = map(float, target[:2])
    dx, dy = tx - px, ty - py
    distance = math.hypot(dx, dy)
    if distance < 0.1:
        return 0.0
    angle = math.atan2(dy, dx) - float(heading)
    angle = (angle + math.pi) % (2 * math.pi) - math.pi
    curvature = 2 * math.sin(angle) / max(distance, 1.0)
    return max(-steer_limit, min(steer_limit, math.atan(wheelbase * curvature) / 0.55))
