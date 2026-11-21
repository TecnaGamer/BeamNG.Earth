"""The way ahead of a driving player, for the work done before the player gets there.

A first-visit map cell's data (its roads, buildings, water, land use, the vegetation cell made from them) is mostly
downloads and DuckDB reads: seconds to a minute a cell. Asked for only when a road or trees tile near the car needed
it, most of it was done behind the car at motorway speeds.

points() is the path the work ahead follows: as far ahead as LEAD_S of driving at the current speed (MIN_M ..
MAX_M), bending with the player's current turn (the turn rate fading over TURN_FADE_S: a bend, not a circle), each
point with the time the player reaches it (most urgent first) and a lateral spread growing with the distance (the
road's own curves, a turn-off). Pure; Track measures the turn rate from the poses.
"""
import math

import dmath
from collections import deque

LEAD_S = 90.0           # s of driving ahead
MIN_M = 1200.0          # m ahead at least while moving (town speeds: the next cell)
MAX_M = 6000.0          # m ahead at most (motorway speeds: ~4 cells)
STEP_M = 300.0          # m between points
SPREAD = 0.1            # m of lateral reach per m ahead
SPREAD_MAX_M = 600.0
TURN_FADE_S = 8.0       # s over which the measured turn rate fades
TURN_MAX = 0.5          # rad/s at most (a hairpin; more is noise or a spin)
MIN_V = 2.0             # m/s: slower, the player is standing (no path)


def points(x, y, vx, vy, turn=0.0, lead_s=LEAD_S, min_m=MIN_M, max_m=MAX_M, step_m=STEP_M):
    """[(t s, x, y, spread m)] from the player (t 0) to the end of the lookahead, every step_m along the path bending
    with turn (rad/s, counter-clockwise positive). [] when not moving."""
    v = math.hypot(vx, vy)
    if v < MIN_V:
        return []
    dist = min(max_m, max(min_m, v * lead_s))
    h0 = dmath.atan2(vy, vx)
    w = max(-TURN_MAX, min(TURN_MAX, turn))
    out = [(0.0, x, y, 0.0)]
    px, py, s = x, y, 0.0
    sub = 25.0              # m per integration step
    while s < dist - 1e-6:
        ds = min(sub, dist - s)
        t_mid = (s + ds / 2) / v
        h = h0 + w * TURN_FADE_S * (1.0 - dmath.exp(-t_mid / TURN_FADE_S))
        px, py, s = px + ds * dmath.cos(h), py + ds * dmath.sin(h), s + ds
        if s >= len(out) * step_m - 1e-6 or s >= dist - 1e-6:
            out.append((s / v, px, py, min(SPREAD_MAX_M, SPREAD * s)))
    return out


END_M = 400.0           # m of a known road's end whose direction the path goes on in past it (along)


def along(line, v, lead_s=LEAD_S, min_m=MIN_M, max_m=MAX_M, step_m=STEP_M):
    """points() for a player on a known road: line ((k, 2), the road ahead from the player: roadahead.follow) walked
    every step_m, then, where the known road ends before the lookahead does (the map ahead not read yet, a fork),
    straight on in the direction of its last END_M, the spread growing from there (along the road itself none: it is
    the way, not a guess). A motorway's bends took a straight line off it within 2-3 km, and the map cells warmed
    10 km ahead were another road's. [] when not moving or without a line."""
    if v < MIN_V or line is None or len(line) < 2:
        return []
    dist = min(max_m, max(min_m, v * lead_s))
    out = [(0.0, float(line[0][0]), float(line[0][1]), 0.0)]
    s, due = 0.0, step_m
    px, py = float(line[0][0]), float(line[0][1])
    tail = [(0.0, px, py)]                  # (the walked points: the end's direction)
    for q in line[1:]:
        qx, qy = float(q[0]), float(q[1])
        seg = math.hypot(qx - px, qy - py)
        while seg > 1e-9 and s + seg >= due and due <= dist + 1e-6:
            f = (due - s) / seg
            out.append((due / v, px + (qx - px) * f, py + (qy - py) * f, 0.0))
            due += step_m
        s += seg
        px, py = qx, qy
        tail.append((s, px, py))
        if s >= dist:
            return out
    back = next(((bx, by) for bs, bx, by in reversed(tail) if s - bs >= END_M), tail[0][1:])
    d = math.hypot(px - back[0], py - back[1])
    if d < 1e-6:
        return out
    ux, uy = (px - back[0]) / d, (py - back[1]) / d
    end = s
    while due <= dist + 1e-6:
        out.append((due / v, px + ux * (due - end), py + uy * (due - end), min(SPREAD_MAX_M, SPREAD * (due - end))))
        due += step_m
    if out[-1][0] * v < dist - 1e-6:
        out.append((dist / v, px + ux * (dist - end), py + uy * (dist - end), min(SPREAD_MAX_M, SPREAD * (dist - end))))
    return out


class Track:
    """The player's recent poses: its turn rate (heading change per second over the last WINDOW_S)."""
    WINDOW_S = 3.0
    JUMP_M = 250.0          # a jump between two poses (teleport, rebase): the history starts again

    def __init__(self):
        self.poses = deque()

    def add(self, t, x, y, vx, vy):
        if self.poses:
            _, lx, ly, _ = self.poses[-1]
            if math.hypot(x - lx, y - ly) > self.JUMP_M:
                self.poses.clear()
        if math.hypot(vx, vy) < MIN_V:
            self.poses.clear()
            return
        self.poses.append((t, x, y, dmath.atan2(vy, vx)))
        while self.poses and t - self.poses[0][0] > self.WINDOW_S:
            self.poses.popleft()

    def reset(self):
        self.poses.clear()

    def turn(self):
        """rad/s (counter-clockwise positive), 0 without a second of history."""
        if len(self.poses) < 2:
            return 0.0
        t0, _, _, h0 = self.poses[0]
        t1, _, _, h1 = self.poses[-1]
        if t1 - t0 < 1.0:
            return 0.0
        dh = (h1 - h0 + math.pi) % (2 * math.pi) - math.pi
        return max(-TURN_MAX, min(TURN_MAX, dh / (t1 - t0)))
