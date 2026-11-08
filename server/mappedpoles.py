"""Mapped utility poles; no roadside presence or spacing guesses.

Read the existing Overture infrastructure layer (OSM source tags). Only actual
pole points produce props. A mapped minor/overhead cable or telecom line may
connect those points; a road or an isolated line never invents supports.

BNE_MAPPED_POLES (on by default) uses the audited electric_pole_wood_old_01
and the installed wire_30m_01 / wire_40m_01 shapes in every country, including
France. With it off, the older opt-in French kit path remains available.
A road with mapped poles along it gets no guessed ones (furniture.place).
"""
import json
import math
import os

import numpy as np

import franceroads
import furniture
import poleprofiles

POLES = {"power_pole", "utility_pole", "communication_pole"}
LINES = {"minor_line", "cable", "communication_line"}
MATCH_M = 1.5
MAX_SPAN_M = 80.0                 # furniture.wire_lines' existing limit
MARGIN_M = MAX_SPAN_M + 5.0
# (everywhere: the lines OpenStreetMap calls minor_line are mostly the 10-30 kV ones across the fields, 90-160 m
# between their mapped poles in a German village's, up to 300; a guessed roadside run keeps 80)
EVERYWHERE_MAX_SPAN_M = 250.0
# Mapped poles outside the French scenery (the user, 2026-11-07: "for everywhere where it exists"): a village whose
# poles are in OpenStreetMap got its guessed ones on the wrong roads and sides, or none (furniture.BURIED_LV)
EVERYWHERE = os.environ.get("BNE_MAPPED_POLES", "on").lower() not in {"off", "0", "false", "no"}


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


def _drawable_pole(cls, tags):
    if not _active(tags) or tags.get("power") == "tower":
        return False
    if (str(tags.get("communication:mobile_phone", "")).strip().lower() == "yes"
            and tags.get("power") != "pole" and cls != "power_pole"):
        return False  # owned by the communications tower renderer
    if tags.get("man_made") in {"mast", "tower"} and tags.get("power") != "pole":
        return False
    return not (cls == "utility_pole" and tags.get("utility") and not (
        set(str(tags["utility"]).split(";")) & {"power", "telecom"}))


def _records(layers):
    """Stable source identity, including features repeated in adjacent map cells."""
    import mappedinfra
    rows = {}
    for f in layers:
        if f is None or "class" not in f.attrs:
            continue
        selected = np.flatnonzero(np.isin(f.attrs["class"], tuple(POLES | LINES | {"power_line"})))
        for i in selected:
            cls = str(f.attrs["class"][i])
            source = str(f.attrs["src_record"][i]) if "src_record" in f.attrs else ""
            key = mappedinfra.source_id(source, f.ids[i])
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


def _french_kit(pole_profile):
    return franceroads.ON and poleprofiles.kit_enabled(pole_profile)


def place(frame, box, layers, elev, props, wires, pole_profile, urban_fn=None, everywhere=False, building_rings=(),
          connect=True, wanted=None):
    """Append mapped poles and wires. The global path emits named infrastructure records;
    the legacy French path emits the existing Forest rows and wire polylines.

    Points retain their mapped side and coordinates. Missing mapping is unknown,
    not evidence of a buried network. No photo/location-specific exception.
    everywhere uses stock shapes globally, ignores urban_fn and preserves mapped height metadata.
    """
    if not everywhere and not _french_kit(pole_profile):
        return
    import countrybounds
    index = countrybounds.bundled() if not everywhere else None
    import mappedinfra
    choose = poleprofiles.selector(pole_profile, frame) if not everywhere else None
    max_span = EVERYWHERE_MAX_SPAN_M if everywhere else MAX_SPAN_M
    margin = max_span + 5.
    x0, y0, x1, y1 = box
    nodes, lines, high, supports = {}, [], [], []
    for key, f, i, cls, tags in _records(layers):
        if cls in POLES and int(f.kind[i]) == 1:
            ll = f.parts(i)[0][0][0]
            if not np.isfinite(ll).all():
                continue
            lon, lat = map(float, ll)
            if not everywhere and not (-5.3 < lon < 9.8 and 41.3 < lat < 51.2):
                continue
            x, y, _ = frame.geodetic_to_enu(lat, lon, 0.)
            if not (x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin):
                continue
            supports.append([x, y])
            if wanted is not None and key not in wanted:
                continue
            if not _drawable_pole(cls, tags):
                continue
            if not everywhere and index.at(lat, lon) != "FR":
                continue
            height = f.attrs["height"][i] if "height" in f.attrs else None
            wire_tags = dict(tags)
            if cls == "communication_pole":
                wire_tags.setdefault("utility", "telecom")
            nodes[key] = (np.array([x, y], float), lat, lon, wire_tags, height)
        elif int(f.kind[i]) == 2 and _active(tags) and (cls == "power_line" or _wire_line(cls, tags)):
            for part_index, part in enumerate(f.parts(i)):
                for ring_index, ll in enumerate(part):
                    if len(ll) < 2 or not np.isfinite(ll).all():
                        continue
                    x, y, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
                    xy = np.c_[x, y]
                    # Keep neighbouring segments, not an entire 100 km line.
                    a, b = xy[:-1], xy[1:]
                    near = ((np.maximum(a[:, 0], b[:, 0]) >= x0 - margin)
                            & (np.minimum(a[:, 0], b[:, 0]) <= x1 + margin)
                            & (np.maximum(a[:, 1], b[:, 1]) >= y0 - margin)
                            & (np.minimum(a[:, 1], b[:, 1]) <= y1 + margin))
                    if cls == "power_line" and not everywhere:
                        high.extend(xy[np.r_[near, False] | np.r_[False, near]])
                        continue
                    # Separate runs avoid bridging an excursion outside the query.
                    starts = np.flatnonzero(near & ~np.r_[False, near[:-1]])
                    ends = np.flatnonzero(near & ~np.r_[near[1:], False]) + 2
                    lines.extend(((key, part_index, ring_index, int(a)), xy[a:b], cls) for a, b in zip(starts, ends))
    if not nodes:
        return
    if urban_fn is not None and not everywhere:
        keys = list(nodes)
        lat = np.array([nodes[k][1] for k in keys])
        lon = np.array([nodes[k][2] for k in keys])
        urban = np.broadcast_to(np.asarray(urban_fn(lat, lon), bool), lat.shape)
        nodes = {k: nodes[k] for k, is_urban in zip(keys, urban) if not is_urban}
    # The legacy French path still delegates high-line supports. The global path
    # emits actual mapped poles; powerlines replaces its own placeholders by source ID.
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
    for line_key, line, line_cls in sorted(lines, key=lambda r: r[0]):
        dist, station, segment = _project(points, line)
        all_dist, all_station, _ = _project(supports, line)
        matched = np.flatnonzero(dist <= MATCH_M)
        if everywhere:
            # Actual way vertices establish connectivity. A nearby pole along a segment, or on a crossing
            # unrelated way, is not evidence of membership in this circuit.
            matched = np.array([j for j in matched if np.linalg.norm(line - points[j], axis=1).min() <= .25], int)
            matched = np.array([j for j in matched if not (
                line_cls != "communication_line" and unique[keys[j]][3].get("utility") == "telecom")], int)
        for j in matched:
            d = line[segment[j] + 1] - line[segment[j]]
            yaw.setdefault(keys[j], round(furniture._yaw(-d[1], d[0]) % 180., 6) % 180.)
        if not connect or line_cls == "power_line":
            continue  # powerlines owns these conductors, but their mapped poles use this same pose
        matched = sorted(matched, key=lambda j: (station[j], keys[j]))
        for a, b in zip(matched[:-1], matched[1:]):
            length = float(np.linalg.norm(points[b] - points[a]))
            if not 5. <= length <= max_span or station[b] - station[a] > max_span:
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
        # Global poles use measured model anchors, scaled with the mapped height and origin offset.
        if everywhere:
            h = mappedinfra.height(tags, height, "pole")
            scale = h / mappedinfra.MODEL["pole"][0]
            style = poleprofiles.PoleStyle("mapped_power_pole", h,
                tuple(tuple(v * scale for v in p) for p in mappedinfra.POLE_ANCHORS), .008)
            ground_z = float(z[k])
            pole_z = ground_z - mappedinfra.MODEL["pole"][1] * scale
        else:
            style = _style(choose(key, *xy, float(z[k])), tags, height)
            pole_z = float(z[k])
        if not math.isfinite(pole_z):
            continue
        pole = (*map(float, xy), pole_z, yaw.get(key, 0.), style)
        poles[key] = pole
        if furniture._owns(box, *xy):
            if everywhere:
                props.append(mappedinfra.record("pole", mappedinfra.source_id(key, key), *xy, ground_z,
                                                tags, height, pole[3], float(lat[k]), float(lon[k])))
            else:
                props.append([style.group, round(pole[0], 2), round(pole[1], 2), round(pole[2], 2),
                              round(pole[3], 1), 1., style.height, 0.])
    for a, b in sorted(spans):
        # Stable source-id ownership, including reversed/repeated line geometry.
        if a not in poles or b not in poles:
            continue
        pa, pb = poles[a], poles[b]
        if not furniture._owns(box, pa[0], pa[1]):
            continue
        aa, ab = pa[4].wire_anchors, pb[4].wire_anchors
        if aa is None and ab is None:   # two stock poles: the stock wires, as between guessed ones
            wires.extend(furniture.wire_lines([pa[:4], pb[:4]], max_span=max_span))
            continue
        if aa is None or ab is None or len(aa) != len(ab):
            continue
        def attachment(p, anchor):
            angle = math.radians(p[3])
            c, s = math.cos(angle), math.sin(angle)
            x, y, z = anchor
            return (p[0] + c * x - s * y, p[1] + s * x + c * y, p[2] + z, 0.)
        for conductor, (ac, bc) in enumerate(zip(aa, ab)):
            start, end = attachment(pa, ac)[:3], attachment(pb, bc)[:3]
            if everywhere:
                if mappedinfra.span_clear(frame, start, end, elev, building_rings):
                    wires.append(mappedinfra.wire_record(mappedinfra.source_id(a, a), mappedinfra.source_id(b, b),
                                                        start, end, conductor))
            else:
                wires.extend(furniture.wire_lines([(*start, 0.), (*end, 0.)],
                             ((0., 0., 0.),), max(pa[4].wire_radius or .012, pb[4].wire_radius or .012)))


def _layers(md, frame, tx, ty, margin=MARGIN_M):
    import roads
    return [md.cell(k, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
            for k in roads.cells_of_tile(frame, tx, ty, margin=margin)]


def tile_poles(frame, tx, ty, md, elev, props, wires, pole_profile, urban_fn=None, infrastructure=None):
    if md is None or not (EVERYWHERE or _french_kit(pole_profile)):
        return
    import roads
    layers = _layers(md, frame, tx, ty, EVERYWHERE_MAX_SPAN_M + 5. if EVERYWHERE else MARGIN_M)
    if _french_kit(pole_profile) and not EVERYWHERE:
        place(frame, roads.tile_bbox(tx, ty), layers, elev, props, wires, pole_profile, urban_fn)
    if EVERYWHERE:
        import worldprops as wp
        arrays, which, starts, ends, _ = wp._building_rings(frame, wp._layer(md,
            roads.cells_of_tile(frame, tx, ty, margin=EVERYWHERE_MAX_SPAN_M + 5.), "buildings"))
        rings = [arrays[int(i)][int(a):int(b)] for i, a, b in zip(which, starts, ends)]
        place(frame, roads.tile_bbox(tx, ty), layers, elev, infrastructure if infrastructure is not None else props,
              wires, pole_profile, everywhere=True, building_rings=rings)


def road_points(md, frame, tx, ty, pole_profile=None):
    """(n, 2) ENU points of the mapped poles near a road tile that place(everywhere=True) draws (furniture.place's
    mapped_poles: a road along them guesses none), or None: the mode off, no map data."""
    if md is None or not EVERYWHERE:
        return None
    out = []
    occupied = set()
    records = list(_records(_layers(md, frame, tx, ty)))
    for _key, f, i, cls, tags in records:
        if cls not in POLES or int(f.kind[i]) != 1 or not _drawable_pole(cls, tags):
            continue
        ll = f.parts(i)[0][0][0]
        if not np.isfinite(ll).all():
            continue
        lon, lat = map(float, ll)
        x, y, _ = frame.geodetic_to_enu(lat, lon, 0.)
        point = (round(lat, 7), round(lon, 7))
        if point in occupied:
            continue
        occupied.add(point)
        out.append((x, y))
    return np.asarray(out, float).reshape(-1, 2)
