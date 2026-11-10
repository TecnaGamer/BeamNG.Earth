"""Building-aware sign positions and sight lines, using rendered wall footprints.

No runtime network beyond the map cells already used by a road tile. Unknown
facades never become wall mounts. All coordinates are in the tile's ENU frame.
"""

from dataclasses import dataclass
import numpy as np


def ring_of(points):
    p = np.asarray(points, float)
    if p.ndim != 2 or len(p) < 3 or p.shape[1] < 2:
        return None
    p = p[:, :2]
    if not np.isfinite(p).all():
        return None
    if len(p) > 1:
        p = p[np.linalg.norm(p - np.roll(p, 1, axis=0), axis=1) > 1e-6]
    if len(p) < 3:
        return None
    area = float(np.sum(p[:, 0] * np.roll(p[:, 1], -1) - p[:, 1] * np.roll(p[:, 0], -1)))
    if abs(area) < 1e-5:
        return None
    return p if area > 0 else p[::-1]


def inside(p, ring):
    a = ring
    b = np.roll(ring, -1, axis=0)
    dy = b[:, 1] - a[:, 1]
    crossings = (a[:, 1] > p[1]) != (b[:, 1] > p[1])
    x = a[:, 0] + (p[1] - a[:, 1]) * (b[:, 0] - a[:, 0]) / np.where(abs(dy) > 1e-12, dy, 1.0)
    return bool(np.count_nonzero(crossings & (x > p[0])) % 2)


def distance(p, ring):
    a = ring
    d = np.roll(ring, -1, axis=0) - a
    t = np.clip(((p - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0, 1)
    return float(np.linalg.norm(p - a - t[:, None] * d, axis=1).min())


def cross(a, b):
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


@dataclass
class Wall:
    ring: np.ndarray
    bottom: float
    height: float
    floor_h: float
    key: str = ""


class Context:
    def __init__(self, rings=(), walls=(), ground=None, get_footprints=None):
        self.ground = ground
        self._get_footprints = get_footprints
        if get_footprints is None:
            self._set_footprints(rings, walls)

    def _set_footprints(self, rings, walls):
        self.rings = [r for p in rings if (r := ring_of(p)) is not None]
        self.lo = np.asarray([r.min(0) for r in self.rings]).reshape(-1, 2)
        self.hi = np.asarray([r.max(0) for r in self.rings]).reshape(-1, 2)
        self._walls = list(walls)
        self.wall_lo = np.asarray([w.ring.min(0) for w in self._walls]).reshape(-1, 2)
        self.wall_hi = np.asarray([w.ring.max(0) for w in self._walls]).reshape(-1, 2)

    def _load_footprints(self):
        if self._get_footprints is not None:
            self._set_footprints(*self._get_footprints())
            self._get_footprints = None

    @property
    def walls(self):
        self._load_footprints()
        return self._walls

    def _near(self, lo, hi):
        self._load_footprints()
        return np.flatnonzero(np.all(self.lo <= hi, axis=1) & np.all(self.hi >= lo, axis=1))

    def clear(self, point, pad=0.15):
        p = np.asarray(point)[:2]
        for i in self._near(p - pad, p + pad):
            r = self.rings[i]
            if inside(p, r) or distance(p, r) < pad:
                return False
        return True

    def visible(self, eye, target, pad=0.06):
        a = np.asarray(eye)[:2]
        b = np.asarray(target)[:2]
        v = b - a
        lo = np.minimum(a, b) - pad
        hi = np.maximum(a, b) + pad
        for i in self._near(lo, hi):
            r = self.rings[i]
            if inside(a, r) or inside(b, r):
                return False
            e = np.roll(r, -1, axis=0) - r
            den = cross(v, e)
            active = abs(den) > 1e-10
            t = cross(r - a, e) / np.where(active, den, 1.0)
            u = cross(r - a, v) / np.where(active, den, 1.0)
            if (active & (t >= 0) & (t <= 1) & (u >= 0) & (u <= 1)).any():
                return False
            # Includes collinear grazing and very close wall edges.
            along = np.clip(((r - a) * v).sum(1) / max(float(v @ v), 1e-12), 0, 1)
            edge_to_ray = float(np.linalg.norm(r - a - along[:, None] * v, axis=1).min())
            if min(distance(a, r), distance(b, r), edge_to_ray) < pad:
                return False
        return True

    def post(self, q, front, width, eyes):
        probes = span(q, front, width)
        return self.visible(probes[0], probes[-1], 0.16) and all(
            self.visible(eye, p) for eye in eyes for p in probes
        )

    def z(self, q, default):
        if self.ground is None:
            return float(default)
        return float(np.asarray(self.ground(np.array([q[0]]), np.array([q[1]]))).ravel()[0])

    def mounts(self, c, u, width, height, line=None):
        """Sorted blank upper-ground-floor strips on known rendered facades."""
        import furniture as f

        c = np.asarray(c)[:2]
        u = np.asarray(u)[:2]
        found = []
        walls = self.walls
        near = np.flatnonzero(
            np.all(self.wall_lo <= c + 25.0, axis=1) & np.all(self.wall_hi >= c - 25.0, axis=1)
        )
        for idx in near:
            wall = walls[idx]
            if wall.floor_h < 2.85 or wall.height < 2.85:
                continue
            # Above the first row of windows/trim, below the next floor's band.
            z = wall.bottom + 2.40 + height / 2
            if z + height / 2 > wall.bottom + min(wall.floor_h - 0.10, wall.height - 0.20):
                continue
            for i, a in enumerate(wall.ring):
                e = wall.ring[(i + 1) % len(wall.ring)] - a
                length = np.linalg.norm(e)
                if length < width + 0.8:
                    continue
                d = e / length
                out = np.array([d[1], -d[0]])
                for s in (width / 2 + 0.40, length - width / 2 - 0.40):
                    p = a + d * s + out * 0.034
                    along = float((p - c) @ u)
                    if not -3.0 < along < 24.0 or np.linalg.norm(p - c) > 25.0:
                        continue
                    if line is not None:
                        station = float(f._project(line, p[None, :])[0])
                        if station > 24.0:
                            continue
                        eyes, _ = f._at(
                            line, np.array([max(3.0, station), station + 9.0, station - 9.0])
                        )
                        eyes = eyes[:, :2]
                    else:
                        road = c + u * max(3.0, along)
                        eyes = [road, road + u * 9, road - u * 9]
                    view = eyes[0] - p
                    if float(view @ out) < 0.8:
                        continue
                    probes = [p + d * v for v in (-width / 2, 0.0, width / 2)]
                    if not self.visible(probes[0], probes[-1], 0.025):
                        continue
                    # Roadside sight line plus at least one driving approach.
                    seen = [all(self.visible(eye, q, 0.02) for q in probes) for eye in eyes]
                    if not seen[0] or sum(seen) < 2:
                        continue
                    found.append((float(np.linalg.norm(p - c)), wall.key, i, s, np.r_[p, z], out))
        return sorted(found, key=lambda v: v[:4])


def span(q, front, width):
    q = np.asarray(q)[:2]
    front = np.asarray(front)[:2]
    axis = np.array([front[1], -front[0]])
    return [q + axis * s for s in (-width / 2, 0.0, width / 2)]


def pavement(lines, rings=()):
    """Use the rendered ribbon's widths and mitres, including bridge decks."""
    import signroads

    outlines = [signroads.polygon(np.asarray(line), 2 * half) for half, line in lines if len(line) >= 2]
    return Context([*outlines, *rings])


def arm_line(lines, arm):
    """Polyline leading out of the junction on this exact feature part."""
    fid, _, u, c, start, *_ = arm
    for p in lines.get(fid, ()):
        line = p if start else p[::-1]
        if np.linalg.norm(line[0, :2] - c[:2]) > 1e-5:
            continue
        d = line[1, :2] - line[0, :2]
        norm = np.linalg.norm(d)
        if norm > 1e-5 and float(d @ u) / norm > 0.999:
            return line
    return None


def for_tile(frame, tx, ty, md, items, skip=frozenset()):
    # A motorway or rural tile may never need to look up any building outlines.
    return Context(get_footprints=lambda: _tile_footprints(frame, tx, ty, md, items, skip))


def _tile_footprints(frame, tx, ty, md, items, skip):
    import buildings
    import roads

    x0, y0, _, _ = roads.tile_bbox(tx, ty)
    rings = buildings.near_rings(frame, tx, ty, md, margin=56.0, skip=skip)
    cover = []
    walls = []
    drawn = set()
    key = lambda r: tuple(sorted(map(tuple, np.round(r, 2))))
    for spec, gmin, gmax in items or ():
        if (spec.min_h or 0.0) > 0.5 or spec.ship or spec.deck:
            continue
        raw = ring_of(spec.ring + [x0, y0])
        if raw is not None:
            drawn.add(key(raw))
        r = ring_of(buildings.drawn_ring(spec) + [x0, y0])
        if r is None:
            continue
        cover.append(r)
        # Projecting balconies/timber braces are not represented by a plain wall.
        if (
            spec.shop
            or spec.plain
            or spec.wall
            or spec.porch
            or spec.portal
            or (
                spec.fr_house
                and (
                    spec.fr_house.get("balcony")
                    or spec.fr_house.get("family") in {"chalet", "timber", "basque_frame"}
                )
            )
        ):
            continue
        base = buildings.base_z(spec, gmin, gmax) + (spec.min_h or 0.0)
        walls.append(Wall(r, float(base), float(spec.height), float(spec.floor_h), str(spec.seed)))
    # Nearby pitched houses may occupy their enclosing box. The actual drawn
    # footprint always wins when we have the matching local building plan.
    for r in rings:
        r = ring_of(r)
        if r is None or key(r) in drawn:
            continue
        c, u, L, W = buildings.oriented_box(r)
        v = np.array([-u[1], u[0]])
        cover.append(
            np.array([c - u * L - v * W, c + u * L - v * W, c + u * L + v * W, c - u * L + v * W])
        )
    return cover, walls
