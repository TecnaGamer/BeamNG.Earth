"""How far ahead of a fast car its streamed tiles reach the mod: the pop-in's measure.

A tile's build time alone does not say whether the player saw it appear: one built in 12 s for a place 30 s ahead is
in time, one built in 2 s for the place the car is at is not. Each tile sent while the car is fast (FAST_V) is noted
with where it is from the car: behind it, under it (the car inside the tile), or ahead, with the seconds of driving
left to it at the car's speed (its lead). Every EVERY_S the connection's notes go to the log, one line per kind:

    arrival trees at 87 m/s: 42 sent, 6 behind, 3 under the car, 33 ahead: lead median 4.2 s (365 m), 9 under 2 s

The trackers call note_geo for the geotiles they send (vegetation.Tracker._built_geo, roads.GeoTracker._built).
"""
import logging
import math
import time

log = logging.getLogger("earthd.arrival")

FAST_V = 40.0           # m/s (90 mph): slower, nothing is noted
EVERY_S = 20.0          # s between two reports
SHORT_S = 2.0           # a lead shorter than this: the tile appeared right in front of the car


class Arrival:
    """One connection's notes."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.rows = {}          # kind -> {"behind", "under", "leads": [(s, m)], "v": [m/s]}
        self.at = clock()
        self.total = {}         # kind -> [sent, behind, under, ahead, short]: the session's

    def note(self, kind, x, y, vx, vy, cx, cy, dist):
        """A tile of kind sent while the car at (x, y) moves at (vx, vy) m/s: (cx, cy) the tile's middle, dist m from
        the car to its nearest point (0: the car is in it)."""
        v = math.hypot(vx, vy)
        if v < FAST_V:
            return
        r = self.rows.setdefault(kind, {"behind": 0, "under": 0, "leads": [], "v": []})
        t = self.total.setdefault(kind, [0, 0, 0, 0, 0])
        r["v"].append(v)
        t[0] += 1
        if dist <= 0.0:
            r["under"] += 1
            t[2] += 1
        elif (cx - x) * vx + (cy - y) * vy <= 0.0:
            r["behind"] += 1
            t[1] += 1
        else:
            r["leads"].append((dist / v, dist))
            t[3] += 1
            t[4] += dist / v < SHORT_S
        if self.clock() - self.at >= EVERY_S:
            self.flush()

    def note_geo(self, kind, frame, key, z, focus, vel):
        """note for zoom-z geotile key sent in frame's world (an anchor's EnuFrame)."""
        if frame is None or focus is None or math.hypot(*vel) < FAST_V:
            return
        import geotiles
        la, lo = geotiles.centre(key[0], key[1], z)
        cx, cy, _ = frame.geodetic_to_enu(la, lo, 0.0)
        dist = float(geotiles.dists(frame, [key], z, [focus])[0, 0])
        self.note(kind, focus[0], focus[1], vel[0], vel[1], float(cx), float(cy), dist)

    def lines(self):
        """The report's lines, one per kind (most tiles first)."""
        out = []
        for kind, r in sorted(self.rows.items(), key=lambda kv: -len(kv[1]["v"])):
            n = len(r["v"])
            v = sum(r["v"]) / n
            leads = sorted(r["leads"])
            s = f"arrival {kind} at {v:.0f} m/s: {n} sent, {r['behind']} behind, {r['under']} under the car, " \
                f"{len(leads)} ahead"
            if leads:
                ls, lm = leads[len(leads) // 2]
                s += f": lead median {ls:.1f} s ({lm:.0f} m), {sum(1 for a, _ in leads if a < SHORT_S)} under " \
                     f"{SHORT_S:.0f} s"
            out.append(s)
        return out

    def flush(self):
        for s in self.lines():
            log.info("%s", s)
        self.rows = {}
        self.at = self.clock()


def note_geo(conn, kind, frame, key, z):
    """A connection's tracker sent geotile key: noted in conn.arrival (never the tile's send)."""
    arr = getattr(conn, "arrival", None)
    if arr is None:
        return
    try:
        arr.note_geo(kind, frame, key, z, getattr(conn, "focus", None), getattr(conn, "vel", (0.0, 0.0)))
    except Exception as e:
        log.debug("arrival %s %s: %s: %s", kind, key, type(e).__name__, e)
