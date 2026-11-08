"""Mapped rural French utility poles; no roadside presence or spacing guesses.

Read the existing Overture infrastructure layer (OSM source tags). Only actual
pole points produce props. A mapped minor/overhead cable or telecom line may
connect those points; a road or an isolated line never invents supports.
Urban scenery, other countries and the mode off retain their previous rules.
"""
import json
import math
import re

import numpy as np

import franceroads
import furniture
import poleprofiles

POLES = {"power_pole", "utility_pole", "communication_pole"}
LINES = {"minor_line", "cable", "communication_line"}
MATCH_M = 1.5
MAX_SPAN_M = 80.0                 # furniture.wire_lines' existing limit
MARGIN_M = MAX_SPAN_M + 5.0


def _tags(value):
    if isinstance(value, dict):
        return value
    try:
        out = json.loads(str(value))
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}


def _active(tags):
    if str(tags.get("location", "")).lower() in {"underground", "underwater", "indoor", "tunnel"}:
        return False
    return not any(str(tags.get(k, "no")).lower() not in {"no", "false", "0", "", "none"}
                   for k in ("disused", "abandoned", "demolished", "razed", "construction", "proposed"))


def _wire_line(cls, tags):
    if not _active(tags):
        return False
    # Insulated power cables and telecom lines must explicitly be aerial.
    return cls == "minor_line" or (cls in {"cable", "communication_line"}
                                   and tags.get("location") in {"overhead", "overground"})


def _records(layers):
    """Stable source identity, including features repeated in adjacent map cells."""
    rows = {}
    for f in layers:
        if f is None or "class" not in f.attrs:
            continue
        selected = np.flatnonzero(np.isin(f.attrs["class"], tuple(POLES | LINES | {"power_line"})))
        for i in selected:
            cls = str(f.attrs["class"][i])
            source = str(f.attrs["src_record"][i]) if "src_record" in f.attrs else ""
            key = re.sub(r"@.*$", "", source) if re.match(r"^[nw]\d+", source) else str(f.ids[i])
            record = (str(f.ids[i]), f, i, cls)
            if key not in rows or record[0] < rows[key][0]:
                rows[key] = record
    for key, (_, f, i, cls) in sorted(rows.items()):
        tags = _tags(f.attrs["tags"][i]) if "tags" in f.attrs else {}
        yield key, f, i, cls, tags


def _project(points, line):
    """Closest distance and station on a line for each point (ENU metres)."""
    a, d = line[:-1], np.diff(line, axis=0)
    lengths = np.linalg.norm(d, axis=1)
    t = np.clip(np.sum((points[:, None] - a) * d, axis=2)
                / np.maximum(lengths * lengths, 1e-12), 0., 1.)
    distance = np.linalg.norm(points[:, None] - (a + t[:, :, None] * d), axis=2)
    k = np.argmin(distance, axis=1)
    stations = np.r_[0., np.cumsum(lengths)]
    return distance[np.arange(len(points)), k], stations[k] + t[np.arange(len(points)), k] * lengths[k], k


def _style(base, tags, height):
    material = str(tags.get("material", "")).lower()
    chosen = {"wood": poleprofiles.WOOD, "concrete": poleprofiles.RECESSED}.get(material, base)
    try:
        h = float(height)
    except (TypeError, ValueError):
        h = float("nan")
    if not math.isfinite(h):
        try:
            h = float(tags.get("height", ""))
        except (ValueError, TypeError):
            h = chosen.height
    if not 3. <= h <= 20.:
        h = chosen.height
    anchors = chosen.wire_anchors
    if anchors is not None:
        anchors = tuple(tuple(v * h / chosen.height for v in p) for p in anchors)
    return poleprofiles.PoleStyle(chosen.group, h, anchors, chosen.wire_radius)


def place(frame, box, layers, elev, props, wires, pole_profile, urban_fn=None):
    """Append mapped rural FR props and wires in the existing furniture formats.

    Points retain their mapped side and coordinates. Missing mapping is unknown,
    not evidence of a buried network. No photo/location-specific exception.
    """
    if not franceroads.ON or not poleprofiles.kit_enabled(pole_profile):
        return
    import countrybounds
    index = countrybounds.bundled()
    choose = poleprofiles.selector(pole_profile, frame)
    x0, y0, x1, y1 = box
    nodes, lines, high, supports = {}, [], [], []
    for key, f, i, cls, tags in _records(layers):
        if cls in POLES and int(f.kind[i]) == 1:
            ll = f.parts(i)[0][0][0]
            if not np.isfinite(ll).all():
                continue
            lon, lat = map(float, ll)
            if not (-5.3 < lon < 9.8 and 41.3 < lat < 51.2):
                continue
            x, y, _ = frame.geodetic_to_enu(lat, lon, 0.)
            if not (x0 - MARGIN_M <= x <= x1 + MARGIN_M and y0 - MARGIN_M <= y <= y1 + MARGIN_M):
                continue
            supports.append([x, y])
            if not _active(tags):
                continue
            if index.at(lat, lon) != "FR":
                continue
            if cls == "utility_pole" and tags.get("utility") and not (
                    set(str(tags["utility"]).split(";")) & {"power", "telecom"}):
                continue
            height = f.attrs["height"][i] if "height" in f.attrs else None
            nodes[key] = (np.array([x, y], float), lat, lon, tags, height)
        elif int(f.kind[i]) == 2 and _active(tags) and (cls == "power_line" or _wire_line(cls, tags)):
            for part_index, part in enumerate(f.parts(i)):
                for ring_index, ll in enumerate(part):
                    if len(ll) < 2 or not np.isfinite(ll).all():
                        continue
                    x, y, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
                    xy = np.c_[x, y]
                    # Keep neighbouring segments, not an entire 100 km line.
                    a, b = xy[:-1], xy[1:]
                    near = ((np.maximum(a[:, 0], b[:, 0]) >= x0 - MARGIN_M)
                            & (np.minimum(a[:, 0], b[:, 0]) <= x1 + MARGIN_M)
                            & (np.maximum(a[:, 1], b[:, 1]) >= y0 - MARGIN_M)
                            & (np.minimum(a[:, 1], b[:, 1]) <= y1 + MARGIN_M))
                    if cls == "power_line":
                        high.extend(xy[np.r_[near, False] | np.r_[False, near]])
                        continue
                    # Separate runs avoid bridging an excursion outside the query.
                    starts = np.flatnonzero(near & ~np.r_[False, near[:-1]])
                    ends = np.flatnonzero(near & ~np.r_[near[1:], False]) + 2
                    lines.extend(((key, part_index, ring_index, int(a)), xy[a:b]) for a, b in zip(starts, ends))
    if not nodes:
        return
    if urban_fn is not None:
        keys = list(nodes)
        lat = np.array([nodes[k][1] for k in keys])
        lon = np.array([nodes[k][2] for k in keys])
        urban = np.broadcast_to(np.asarray(urban_fn(lat, lon), bool), lat.shape)
        nodes = {k: nodes[k] for k, is_urban in zip(keys, urban) if not is_urban}
    # Existing powerlines.py already places a pylon at these high-line vertices.
    # Do not stack a second prop where that pipeline renders the same support.
    high = np.asarray(high, float).reshape(-1, 2)
    unique, occupied = {}, set()
    for key, node in sorted(nodes.items()):
        xy, lat, lon, *_ = node
        coordinate = (round(lat, 7), round(lon, 7))
        if coordinate in occupied or (len(high) and np.min(np.linalg.norm(high - xy, axis=1)) <= MATCH_M):
            continue
        occupied.add(coordinate)
        unique[key] = node
    if not unique:
        return
    keys = list(unique)
    points = np.array([unique[key][0] for key in keys])
    yaw, spans = {}, {}
    supports = np.asarray(supports, float)
    for line_key, line in sorted(lines, key=lambda r: r[0]):
        dist, station, segment = _project(points, line)
        all_dist, all_station, _ = _project(supports, line)
        matched = np.flatnonzero(dist <= MATCH_M)
        for j in matched:
            d = line[segment[j] + 1] - line[segment[j]]
            yaw.setdefault(keys[j], furniture._yaw(-d[1], d[0]) % 180.)
        matched = sorted(matched, key=lambda j: (station[j], keys[j]))
        for a, b in zip(matched[:-1], matched[1:]):
            length = float(np.linalg.norm(points[b] - points[a]))
            if not 5. <= length <= MAX_SPAN_M or station[b] - station[a] > MAX_SPAN_M:
                continue
            if np.any((all_dist <= MATCH_M) & (all_station > station[a] + .01)
                      & (all_station < station[b] - .01)):
                continue                  # never jump over an excluded mapped support
            # A mapped bend without a mapped pole does not get a straight chord.
            vertices = np.r_[0., np.cumsum(np.linalg.norm(np.diff(line, axis=0), axis=1))]
            between = line[(vertices >= station[a]) & (vertices <= station[b])]
            if len(between) and np.max(_project(between, points[[a, b]])[0]) > MATCH_M:
                continue
            pair = tuple(sorted((keys[a], keys[b])))
            spans[pair] = pair
    lat = np.array([unique[k][1] for k in keys])
    lon = np.array([unique[k][2] for k in keys])
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), float))
    poles = {}
    for k, key in enumerate(keys):
        xy, _, _, tags, height = unique[key]
        style = _style(choose(key, *xy, float(z[k])), tags, height)
        pole = (*map(float, xy), float(z[k]), yaw.get(key, 0.), style)
        poles[key] = pole
        if furniture._owns(box, *xy):
            props.append([style.group, round(pole[0], 2), round(pole[1], 2), round(pole[2], 2),
                          round(pole[3], 1), 1., style.height, 0.])
    for a, b in sorted(spans):
        # Stable source-id ownership, including reversed/repeated line geometry.
        pa, pb = poles[a], poles[b]
        if not furniture._owns(box, pa[0], pa[1]):
            continue
        aa, ab = pa[4].wire_anchors, pb[4].wire_anchors
        if aa is None or ab is None or len(aa) != len(ab):
            continue
        def attachment(p, anchor):
            angle = math.radians(p[3])
            c, s = math.cos(angle), math.sin(angle)
            x, y, z = anchor
            return (p[0] + c * x - s * y, p[1] + s * x + c * y, p[2] + z, 0.)
        for ac, bc in zip(aa, ab):
            wires.extend(furniture.wire_lines([attachment(pa, ac), attachment(pb, bc)],
                         ((0., 0., 0.),), max(pa[4].wire_radius or .012, pb[4].wire_radius or .012)))


def tile_poles(frame, tx, ty, md, elev, props, wires, pole_profile, urban_fn=None):
    if md is None or not franceroads.ON or not poleprofiles.kit_enabled(pole_profile):
        return
    import roads
    layers = [md.cell(k, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
              for k in roads.cells_of_tile(frame, tx, ty, margin=MARGIN_M)]
    place(frame, roads.tile_bbox(tx, ty), layers, elev, props, wires, pole_profile, urban_fn)
