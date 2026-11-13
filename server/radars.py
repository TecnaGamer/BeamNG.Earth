"""Surveyed French fixed speed cameras. The catalogue is offline and is the only source of sites."""
from dataclasses import dataclass
from functools import lru_cache
import gzip
import hashlib
import json
import logging
import math
from pathlib import Path

import numpy as np
import dmath
import dnp

import franceroads
import roads
import signroads

DATA = Path(__file__).with_name("data") / "fr_speed_cameras.json.gz"
HARDWARE = {
    "ETF": ("fr_speed_camera_box", 1.57, 1.22),
    "ETD": ("fr_speed_camera_pole", 2.82, 2.39),
    "ETT": ("fr_speed_camera_tower", 4.12, 3.71),
    "ETU": ("fr_speed_camera_urban", 2.94, 2.68),
}
SIGN_HEIGHT = {"fr_speed_warning_sr3a": 4.85, "fr_speed_warning_sr3b": 3.05}
ROAD_WARNING_M, EXPRESS_WARNING_M = 100., 500.
MATCH_M = 34.
APPROACH_M = EXPRESS_WARNING_M + MATCH_M + 18.
EXPRESS = {"motorway", "trunk"}
DRIVABLE = EXPRESS | {"primary", "secondary", "tertiary", "unclassified", "residential", "living_street"}
log = logging.getLogger("earthd.radars")


@lru_cache(maxsize=1)
def _catalog():
    try:
        data = DATA.read_bytes()
        cameras = json.loads(gzip.decompress(data))["cameras"]
    except (OSError, ValueError, KeyError) as exc:
        log.warning("French speed camera catalogue unavailable: %s", exc)
        return {}, None
    grid = {}
    for row in cameras:
        lat, lon = row[3:5]
        key = math.floor(lon * 10), math.floor(lat * 10)
        grid.setdefault(key, []).append(row)
    return grid, hashlib.sha256(data).hexdigest()


def signature():
    """Included in the retained and anchor road cache keys when the feature is enabled."""
    return _catalog()[1] if franceroads.ON else None


def _nearby(frame, box):
    x0, y0, x1, y1 = box
    corners = np.array([[x0, y0], [x0, y1], [x1, y0], [x1, y1]], dtype=float)
    corners += APPROACH_M * np.array([[-1., -1.], [-1., 1.], [1., -1.], [1., 1.]])
    lat, lon, _ = frame.enu_to_geodetic(*corners.T, 0.)
    grid, _ = _catalog()
    south, north = lat.min(), lat.max()
    west, east = lon.min(), lon.max()
    return [row for x in range(math.floor(west * 10), math.floor(east * 10) + 1)
            for y in range(math.floor(south * 10), math.floor(north * 10) + 1)
            for row in grid.get((x, y), ())
            if south <= row[3] <= north and west <= row[4] <= east]


@dataclass
class Road:
    fid: str
    spec: signroads.Spec
    xy: np.ndarray
    arc: np.ndarray
    width: float

    def at(self, station):
        i = min(len(self.arc) - 2, max(0, int(np.searchsorted(self.arc, station, side="right") - 1)))
        d = (self.xy[i + 1] - self.xy[i]) / (self.arc[i + 1] - self.arc[i])
        return self.xy[i] + d * (station - self.arc[i]), d


def _network(pavements):
    out = []
    for fid, spec, line, width in pavements:
        xy = np.asarray(line)[:, :2]
        lengths = dnp.hypot(*np.diff(xy, axis=0).T)
        keep = lengths > 1e-5
        if keep.any():
            out.append(Road(fid, spec, xy[np.r_[True, keep]], np.r_[0., lengths[keep].cumsum()], float(width)))
    return out


class Network:
    def __init__(self, pavements):
        self.roads = _network(pavements)
        self.low = np.array([r.xy.min(axis=0) for r in self.roads]).reshape(-1, 2)
        self.high = np.array([r.xy.max(axis=0) for r in self.roads]).reshape(-1, 2)

    def near(self, point, radius):
        hit = ((point + radius >= self.low) & (point - radius <= self.high)).all(axis=1)
        return (self.roads[i] for i in np.flatnonzero(hit))

    def clear(self, point, margin):
        return all(_project(road, point)[2] >= road.width / 2 + margin for road in self.near(point, 25.))


def _project(road, point):
    a = road.xy[:-1]
    d = np.diff(road.xy, axis=0)
    length = np.diff(road.arc)
    u = np.clip(((point - a) * d).sum(axis=1) / (length * length), 0., 1.)
    projected = a + u[:, None] * d
    dist2 = (dnp.sq(point - projected)).sum(axis=1)
    i = int(dist2.argmin())
    return road.arc[i] + u[i] * length[i], d[i] / length[i], math.sqrt(dist2[i]), projected[i]


def _right(tangent):
    return np.array([tangent[1], -tangent[0]])


def _match(network, point, limit):
    choices = []
    for road in network.near(point, MATCH_M):
        sp = road.spec
        if sp.cls not in DRIVABLE or sp.link or (limit >= 120 and sp.cls != "motorway"):
            continue
        s, tangent, distance, centre = _project(road, point)
        if distance > MATCH_M:
            continue
        side = float(dnp.matmul(point - centre, _right(tangent)))
        sense = (sp.dir or 1) if sp.oneway else (1 if side >= 0 else -1)
        # A surveyed point may be on the centreline. Near parallel roads,
        # prefer the matching limit and the traffic side nearest the cabinet.
        score = distance + (5. if sp.kmh is not None and abs(sp.kmh - limit) > 2 else 0.)
        score += 6. if sp.oneway and side * sense < -1. else 0.
        choices.append((score, str(road.fid), road, s, sense))
    if choices:
        return min(choices, key=lambda c: c[:2])[2:]
    return None


def _upstream(network, road, s, sense, length):
    """Follow the same approach through connected road parts, staying off ramps."""
    seen = {id(road)}
    express = road.spec.cls in EXPRESS
    while True:
        available = s if sense > 0 else road.arc[-1] - s
        if length <= available:
            p, d = road.at(s - length * sense)
            return road, p, d * sense
        length -= available
        endpoint, old_direction = road.at(0. if sense > 0 else road.arc[-1])
        incoming = []
        for next_road in network.near(endpoint, 3.):
            sp = next_road.spec
            if (id(next_road) in seen or sp.link or sp.cls not in DRIVABLE
                    or (express and sp.cls not in EXPRESS)):
                continue
            for sign in (1, -1):
                if sp.oneway and (sp.dir or 1) != sign:
                    continue
                station = next_road.arc[-1] if sign > 0 else 0.
                p, d = next_road.at(station)
                gap, alignment = dnp.norm(p - endpoint), dnp.matmul(d * sign, old_direction * sense)
                if gap <= 3. and alignment >= .68:
                    score = gap + 5 * (1 - alignment) + (sp.cls != road.spec.cls)
                    incoming.append((score, str(next_road.fid), next_road, station, sign))
        if not incoming:
            return None
        _, _, road, s, sense = min(incoming, key=lambda c: c[:2])
        seen.add(id(road))


def _shoulder(network, centre, tangent, width, setback, margin):
    for extra in (0., .8, 1.6, 3.):
        p = centre + _right(tangent) * (width / 2 + setback + extra)
        if network.clear(p, margin):
            return p
    return None


def _yaw(tangent):
    return math.degrees(dmath.atan2(tangent[1], tangent[0])) % 360.


def place(frame, box, sites, pavements, elev):
    """Radar rows and gates. Ownership of each sign and cabinet follows its final position."""
    network = Network(pavements)
    props, gates = [], []

    def ground(p):
        la, lo, _ = frame.enu_to_geodetic(*p, 0.)
        return float(frame.geodetic_to_enu(la, lo, elev(la, lo))[2])

    for ident, kind, limit, lat, lon in sites:
        x, y, _ = frame.geodetic_to_enu(lat, lon, 0.)
        point = np.array([x, y])
        match = _match(network, point, limit)
        if match is None:
            continue
        road, s, sense = match
        centre, axis = road.at(s)
        tangent = axis * sense
        camera = point if network.clear(point, .7) else _shoulder(network, centre, tangent, road.width, 1.1, .7)
        if camera is None:
            continue
        group, height, lens = HARDWARE[kind]
        if roads.box_owns(box, *camera):
            camera_z = ground(camera)
            yaw = _yaw(tangent)
            props.append([group, *map(float, camera), camera_z, (yaw - 90) % 360., 1., height, 0.])
            half = road.width / (2. if road.spec.oneway else 4.)
            gate = centre if road.spec.oneway else centre + half * _right(tangent)
            gates.append([ident, *map(float, gate), ground(gate), yaw, half - .12, limit,
                          *map(float, camera), camera_z + lens])

        express = road.spec.cls in EXPRESS
        base = EXPRESS_WARNING_M if express else ROAD_WARNING_M
        for extra in (0., 8., 16.):
            approach = _upstream(network, road, s, sense, base + extra)
            if approach is None:
                break
            upstream, p, heading = approach
            sign = _shoulder(network, p, heading, upstream.width, 2.4 if express else 1.15, 1.25 if express else .7)
            if sign is None:
                continue
            if roads.box_owns(box, *sign):
                group = "fr_speed_warning_sr3a" if express else "fr_speed_warning_sr3b"
                props.append([group, *map(float, sign), ground(sign), (_yaw(heading) - 90) % 360.,
                              1., SIGN_HEIGHT[group], 0.])
            break
    return props, gates


def tile_rows(frame, box, md, elev, widths=None, urban=None):
    if not franceroads.ON or md is None:
        return [], []
    try:
        sites = _nearby(frame, box)
        if not sites:
            return [], []
        context = getattr(md, "_radar_roads", None)
        if context is None:
            def cell(key):
                return md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
            context = md._radar_roads = signroads.Context(cell, urban, widths)
        points = [(row[4], row[3]) for row in sites]
        return place(frame, box, sites, context.pavements(frame, points, margin=APPROACH_M), elev)
    except Exception as exc:
        log.warning("French speed cameras unavailable in this tile: %s", exc)
        return [], []
