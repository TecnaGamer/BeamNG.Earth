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
