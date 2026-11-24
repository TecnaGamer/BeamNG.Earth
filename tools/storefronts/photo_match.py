"""Match a photographed wall using camera coordinates and the bearing to the selected crop."""

import math
from xml.etree import ElementTree

import numpy as np
from whole_store import simplify


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def match_wall(extract, camera, bearing):
    """Return a way ID and wall endpoints if nearby sight lines agree on the first facade."""
    lon, lat = camera
    if not (-180 <= lon <= 180 and -89 < lat < 89 and math.isfinite(bearing)):
        raise ValueError(
            "Expected camera longitude, latitude and a finite true-north bearing"
        )
    origin = np.array(camera)
    scale = np.array([111320 * math.cos(math.radians(lat)), 111132])
    osm = ElementTree.parse(extract)
    nodes = {
        n.get("id"): (float(n.get("lon")), float(n.get("lat")))
        for n in osm.findall("node")
    }
    walls = []
    for way in osm.findall("way"):
        building = way.find("tag[@k='building']")
        if building is None or building.get("v") == "no":
            continue
        refs = [n.get("ref") for n in way.findall("nd")]
        if (
            len(refs) < 4
            or refs[0] != refs[-1]
            or any(ref not in nodes for ref in refs)
        ):
            continue
        ring = (np.array([nodes[r] for r in refs[:-1]]) - origin) * scale
        if np.any(ring.min(axis=0) > 80) or np.any(ring.max(axis=0) < -80):
            continue
        ring = ring[np.any(ring != np.roll(ring, 1, axis=0), axis=1)]
        if len(ring) < 3:
            continue
        # A camera inside a mapped building has no reliable exterior sight line.
        crossing_edges = (ring[:, 1] > 0) != (np.roll(ring[:, 1], -1) > 0)
        starts, ends = ring[crossing_edges], np.roll(ring, -1, axis=0)[crossing_edges]
        x_crossings = starts[:, 0] - starts[:, 1] * (ends[:, 0] - starts[:, 0]) / (
            ends[:, 1] - starts[:, 1]
        )
        if np.count_nonzero(x_crossings > 0) % 2:
            raise ValueError("Camera falls inside a mapped building")
        ring = simplify(ring)
        for a, b in zip(ring, np.roll(ring, -1, axis=0)):
            walls.append((way.get("id"), a, b))

    selected = []
    for degrees in (bearing - 5, bearing, bearing + 5):
        angle = math.radians(degrees)
        direction = np.array([math.sin(angle), math.cos(angle)])
        hits = []
        for index, (_, a, b) in enumerate(walls):
            edge = b - a
            divisor = cross(direction, edge)
            if abs(divisor) < 1e-8:
                continue
            distance = cross(a, edge) / divisor
            fraction = cross(a, direction) / divisor
            if 0 <= distance <= 80 and 0 <= fraction <= 1:
                hits.append((distance, index, fraction))
        if not hits:
            raise ValueError(
                "No mapped facade within 80 metres along the photo bearing"
            )
        hits.sort()
        distance, index, fraction = hits[0]
        _, a, b = walls[index]
        length = np.linalg.norm(b - a)
        if (
            distance < 1
            or min(fraction, 1 - fraction) * length < 1
            or abs(cross(direction, b - a)) / length < 0.5
            or (len(hits) > 1 and hits[1][0] - distance < 1)
        ):
            raise ValueError(
                "Ambiguous facade near a corner, overlapping wall or shallow viewing angle"
            )
        selected.append(index)
    if len(set(selected)) != 1:
        raise ValueError("Photo bearing uncertainty crosses multiple walls")
    way, a, b = walls[selected[0]]
    return way, [(a / scale + origin).tolist(), (b / scale + origin).tolist()]
