"""French E52a signs on the median, using published motorway reference points."""

from collections import defaultdict
from functools import lru_cache
import gzip
import hashlib
from itertools import combinations
import json
import math
from pathlib import Path

import numpy as np
import dnp

import frroadmesh
import furniture
import geotiles
import guardrails
import signplacement

DATA_PATH = Path(__file__).with_name("data") / "fr_motorway_markers.json.gz"
DATA_SHA256 = "c576d0263eef6124e9d6c475bcb22c3c774b142b895c27ced6ab2b7a61e2fa4c"
SIGNATURE = "fr-motorway-pr-1:" + DATA_SHA256
WIDTH = .43
GRID = 1_000_000


@lru_cache(maxsize=1)
def _catalogue():
    raw = DATA_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DATA_SHA256:
        raise ValueError("French motorway reference checksum mismatch")
    doc = json.loads(gzip.decompress(raw))
    if doc["format"] != 1:
        raise ValueError("Unsupported French motorway reference format")
    grid = defaultdict(list)
    for row in doc["rows"]:
        grid[row[0] // GRID, row[1] // GRID].append(row)
    return grid


def nearby(frame, box):
    x0, y0, x1, y1 = box
    lat, lon, _ = frame.enu_to_geodetic(
        np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4)
    )
    a, b = math.floor(min(lon) * 1e7), math.floor(min(lat) * 1e7)
    c, d = math.ceil(max(lon) * 1e7), math.ceil(max(lat) * 1e7)
    if c < -55_000_000 or a > 100_000_000 or d < 410_000_000 or b > 515_000_000:
        return []
    grid = _catalogue()
    return [
        row for x in range(a // GRID, c // GRID + 1) for y in range(b // GRID, d // GRID + 1)
        for row in grid.get((x, y), ())
        if a <= row[0] <= c and b <= row[1] <= d
    ]


def _median(highways, at):
    """Opposite ground mainlines must overlap abreast of the reference."""
    near = []
    for fid, spec, line, _ in highways:
        s = furniture._project(line, at[None, :2])
        points, tangents = furniture._at(line, s)
        q, tangent = points[0], tangents[0]
        delta = at[:2] - q[:2]
        if float(dnp.matmul(delta, delta)) <= 34.0**2 and abs(float(dnp.matmul(delta, tangent))) < 2.5:
            near.append((fid, q, tangent * spec.dir, spec.width / 2))
    pairs = []
    for (fa, a, ta, wa), (fb, b, tb, wb) in combinations(near, 2):
        cross = b[:2] - a[:2]
        gap = dnp.norm(cross) - wa - wb
        if not (-.25 <= gap <= 22.0 and abs(a[2] - b[2]) < 1.25
                and float(dnp.matmul(ta, tb)) < -.92 and abs(float(dnp.matmul(cross, ta))) < 3.2):
            continue
        # The median sits to the left of each carriageway on French motorways.
        if signplacement.cross(ta, cross) <= 0 or signplacement.cross(tb, -cross) <= 0:
            continue
        normal = cross / dnp.norm(cross)
        centre = (a + b) / 2
        centre[:2] += normal * (wa - wb) / 2
        distance = dnp.norm(centre[:2] - at[:2])
        if distance <= 22.0:
            pairs.append((distance, centre, ta, normal, gap, (fa, fb)))
    pairs.sort(key=lambda pair: pair[0])
    if not pairs:
        return None
    best = pairs[0]
    if any(p[0] - best[0] < 1.5 and dnp.norm(p[1][:2] - best[1][:2]) > 1.5 for p in pairs[1:]):
        return None
    return best[1:]


def _concrete(centre, travel, rows):
    """A bracket may sit on a parallel concrete module in a very narrow median."""
    for row in rows:
        if len(row) < 17 or int(row[0]) != guardrails.G_JERSEY:
            continue
        q = np.asarray(row[1:4], float)
        along = np.asarray(row[11:13], float)
        if abs(float(dnp.matmul(travel, along))) < .9:
            continue
        offset = float(dnp.matmul(centre[:2] - q[:2], along))
        q[:2] += along * offset
        if abs(offset) <= 1.40 and dnp.norm(q[:2] - centre[:2]) < .45:
            q[2] += row[6] + offset * row[13]
            return q
    return None


def add(frame, roads, box, french, mesh, pavement, obstacles, rails, guardrails_rows=(), decks=(), data=None):
    highways = [
        r for r in roads if r[1].cls == "motorway" and r[1].kind == "paved"
        and r[1].oneway and r[1].dir and not r[1].link and len(r[2]) > 1
    ]
    if len(highways) < 2:
        return []
    records = []
    for lon, lat, route, pr in nearby(frame, box) if data is None else data:
        at = np.asarray(frame.geodetic_to_enu(lat * 1e-7, lon * 1e-7, 0.))
        if not geotiles.box_owns(box, *at[:2]) or not french(*at):
            continue
        pair = _median(highways, at)
        if pair is None:
            continue
        centre, travel, normal, gap, carriageways = pair
        offsets = [s for s in (0., .42, -.42, .8, -.8) if abs(s) < gap / 2 - WIDTH / 2 - .15]
        ground_positions = [centre[:2] + normal * s for s in offsets]
        base = None
        for q in ground_positions:
            a, _, b = signplacement.span(q, travel, WIDTH)
            if not pavement.visible(a, b, .15) or not obstacles.visible(a, b, .14) or not rails.clear(q, .13):
                continue
            z = obstacles.z(q, centre[2])
            if abs(z - centre[2]) <= 1.25:
                base = np.r_[q, z]
                break
        mount = base is None
        if mount:
            base = _concrete(centre, travel, guardrails_rows)
            if base is None:
                continue
            a, _, b = signplacement.span(base, travel, WIDTH)
            # The two paved shoulders sometimes continue under the concrete.
            # The plate fits over that physical barrier. Other roads and decks
            # still need to clear its span.
            crossing = signplacement.pavement(
                [(sp.width / 2, line) for fid, sp, line, _ in roads if fid not in carriageways]
                + [(half, line) for line, half in decks]
            )
            if not crossing.visible(a, b, .15) or not obstacles.visible(a, b, .14):
                continue
        bottom = .15 if mount else max(.92, centre[2] - base[2] + .84)
        frroadmesh.motorway_marker(mesh, base, travel, route, str(pr), bottom, WIDTH)
        records.append({
            "id": f"pr/{route.replace(' ', '')}/{pr}/{lon},{lat}", "kind": "motorway-pr",
            "name": f"{route} {pr}", "route": route, "pr": pr, "pos": base.tolist(),
            "source": "RRN 2026", "support": "median-barrier" if mount else "median-post",
        })
    return records
