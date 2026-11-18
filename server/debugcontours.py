"""Union bounded detector rectangles into contours without reading render meshes.

A sweep removes shared cell edges. Rings retain real gaps and holes; no smoothing
or convex hull expands the recorded coverage into areas that were not selected.
"""
from collections import Counter, defaultdict


def _merged(intervals):
    out = []
    for west, east in sorted(intervals):
        if out and west <= out[-1][1]:
            out[-1][1] = max(east, out[-1][1])
        else:
            out.append([west, east])
    return out


def _difference(left, right):
    """Disjoint sorted intervals in left but not right."""
    j = 0
    for west, east in left:
        while j < len(right) and right[j][1] <= west:
            j += 1
        k, cursor = j, west
        while k < len(right) and right[k][0] < east:
            a, b = right[k]
            if a > cursor:
                yield cursor, min(a, east)
            cursor = max(cursor, b)
            if cursor >= east:
                break
            k += 1
        if cursor < east:
            yield cursor, east


def rectangle_rings(rectangles):
    """Return boundary rings of (west, south, east, north) rectangles, including holes."""
    events = defaultdict(list)
    for west, south, east, north in rectangles:
        if west < east and south < north:
            events[south].append(((west, east), 1))
            events[north].append(((west, east), -1))
    active, edges, previous, last_y = Counter(), defaultdict(set), [], None
    for y, changes in sorted(events.items()):
        if last_y is not None:
            for west, east in previous:
                edges[(west, y)].add((west, last_y))
                edges[(east, last_y)].add((east, y))
        for interval, delta in changes:
            active[interval] += delta
            if not active[interval]:
                del active[interval]
        current = _merged(active)
        for west, east in _difference(current, previous):
            edges[(west, y)].add((east, y))
        for west, east in _difference(previous, current):
            edges[(east, y)].add((west, y))
        previous, last_y = current, y

    def direction(a, b):
        return (0 if b[0] > a[0] else 2) if b[0] != a[0] else (1 if b[1] > a[1] else 3)

    rings = []
    while edges:
        start = next(iter(edges))
        ring, point, incoming = [], start, None
        while True:
            ring.append(point)
            options = edges[point]
            # At corner-only contacts turn left to retain separate patches.
            if incoming is None or len(options) == 1:
                nxt = min(options)
            else:
                priority = {1: 3, 0: 2, 3: 1, 2: 0}
                nxt = max(options, key=lambda p: priority[(direction(point, p)-incoming) % 4])
            options.remove(nxt)
            if not options:
                del edges[point]
            incoming, point = direction(point, nxt), nxt
            if point == start:
                break
        # Drop collinear sweep vertices, retaining every actual coverage corner.
        corners = [p for i, p in enumerate(ring)
                   if direction(ring[i-1], p) != direction(p, ring[(i+1) % len(ring)])]
        if len(corners) >= 3:
            rings.append([list(p) for p in corners])
    return rings


def features(rectangles, labels=None):
    """One even-odd contour feature per material, with no internal row outlines."""
    grouped = defaultdict(list)
    for family, west, south, east, north in rectangles:
        grouped[int(family)].append((west, south, east, north))
    out = []
    for family, boxes in sorted(grouped.items()):
        rings = rectangle_rings(boxes)
        if rings:
            feature = {"type": family, "rings": rings, "unwrapped": True}
            if labels and family in labels:
                feature["label"] = labels[family]
            out.append(feature)
    return out
