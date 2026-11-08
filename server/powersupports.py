"""Explicit mapped support nodes override a power way's inferred hardware.

Keep the existing native transmission layout for towers. Only mapped power=pole
vertices get the audited wood model/anchors, even on 115/230 kV or mixed ways.
"""
import math

import numpy as np

import mappedinfra
import mappedpoles

# Additional real anchors on the stock pole, for a mapped six-conductor circuit.
SIX_ANCHORS = mappedinfra.POLE_ANCHORS + ((0.6190262, -0.1304009, 6.192549),
                                        (0.2690277, -0.1304009, 6.192549),
                                        (-0.2309723, -0.1304009, 6.192549))
MATCH_M = .25


class Lookup:
    """One tile build's cached infrastructure reads, including both ends of boundary spans."""
    def __init__(self, frame, md, layers=()):
        self.frame, self.md = frame, md
        self.layers = dict(layers)

    def for_vertices(self, ll, elev):
        if not mappedpoles.EVERYWHERE:
            return {}
        import mapcells
        for lon, lat in ll:
            key = mapcells.cell_of(float(lat), float(lon))
            if key not in self.layers:
                try:
                    self.layers[key] = self.md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
                except (OSError, ValueError):
                    self.layers[key] = None
        layers = list({id(f): f for f in self.layers.values() if f is not None}.values())
        ex, ny, _ = self.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
        vertices = np.c_[ex, ny]
        wanted = set()
        for key, f, i, cls, tags in mappedpoles._records(layers):
            if int(f.kind[i]) != 1 or cls not in mappedpoles.POLES or not mappedpoles._drawable_pole(cls, tags):
                continue
            # A telecom/lighting point close to a transmission way is not an electrical support.
            if tags.get("power", "pole" if cls == "power_pole" else "") != "pole":
                continue
            llp = f.parts(i)[0][0][0]
            if not np.isfinite(llp).all():
                continue
            x, y, _ = self.frame.geodetic_to_enu(float(llp[1]), float(llp[0]), 0.)
            if np.linalg.norm(vertices - [x, y], axis=1).min() <= MATCH_M:
                wanted.add(key)
        if not wanted:
            return {}
        lo = vertices.min(0) - 1.
        hi = vertices.max(0) + 1.
        records = []
        # Exactly the same measured origin, height and heading as the ordinary mapped-pole renderer.
        # No extra wire layout or terrain reads for irrelevant supports.
        mappedpoles.place(self.frame, (*lo, *hi), layers, elev, records, [], None,
                          everywhere=True, connect=False, wanted=wanted)
        out = {}
        for r in records:
            distance = np.linalg.norm(vertices - [r["x"], r["y"]], axis=1)
            for index in np.flatnonzero(distance <= MATCH_M):
                out[int(index)] = r
        return out

    def tower_ids(self, ll):
        """Source identity for mapped tower vertices; for_vertices has warmed these same cells."""
        ex, ny, _ = self.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
        vertices = np.c_[ex, ny]
        out = {}
        for f in {id(f): f for f in self.layers.values() if f is not None}.values():
            classes = f.attrs.get("class")
            if classes is None:
                continue
            for i in np.flatnonzero((f.kind == 1) & (classes == "power_tower")):
                point = f.parts(i)[0][0][0]
                if not np.isfinite(point).all():
                    continue
                x, y, _ = self.frame.geodetic_to_enu(float(point[1]), float(point[0]), 0.)
                source = f.attrs.get("src_record", [""] * len(f.kind))[i]
                fid = mappedinfra.source_id(source, f.ids[i])
                for index in np.flatnonzero(np.linalg.norm(vertices - [x, y], axis=1) <= MATCH_M):
                    out.setdefault(int(index), fid)
        return out


def retained_vertices(pts, offsets, min_span):
    for line, (a, b) in enumerate(zip(offsets[:-1], offsets[1:])):
        kept = []
        for i in range(int(a), int(b)):
            if kept and np.linalg.norm(pts[i, :2] - pts[kept[-1], :2]) < min_span:
                continue
            kept.append(i)
        yield line, kept


def segments(pts, offsets, stop, box, min_span, max_span):
    """Source vertex indices for the native kernel's emitted spans, in exactly its order.

    Native tower deduplication does not change wire emission. Retain its minimum
    vertex separation, substation exclusions and midpoint tile ownership.
    """
    for line, kept in retained_vertices(pts, offsets, min_span):
        for i, j in zip(kept[:-1], kept[1:]):
            if stop[i] and stop[j]:
                continue
            if np.linalg.norm(pts[j, :2] - pts[i, :2]) > max_span:
                continue
            mid = (pts[i, :2] + pts[j, :2]) * .5
            if box[0] <= mid[0] < box[2] and box[1] <= mid[1] < box[3]:
                yield line, i, j


def support_records(frame, ll, pts, offsets, stop, overrides, tower_ids, box, group, height,
                    model_height, clearance, min_span, max_span):
    """One visible support per wire junction, owned also by tiles showing its incident spans.

    Native coarse deduplication can remove a distinct support or let an off-tile
    point claim an in-tile support. Rebuild the required poses from the same retained
    way vertices, deduplicating only identical source identities/locations.
    """
    def inside(x, y):
        return box[0] <= x < box[2] and box[1] <= y < box[3]
    needed = set()
    for _, a, b in segments(pts, offsets, stop, box, min_span, max_span):
        if not stop[a]: needed.add(a)
        if not stop[b]: needed.add(b)
    records = {}
    for _, kept in retained_vertices(pts, offsets, min_span):
        if len(kept) < 2:
            continue
        for k, index in enumerate(kept):
            x, y, z = map(float, pts[index])
            own = inside(x, y)
            if stop[index] or (not own and index not in needed):
                continue
            if index in overrides:
                r = dict(overrides[index])
                # Nearby ownership supplies collision; a wire-only owner retains the visible pole.
                r["collide"] = bool(inside(r["x"], r["y"]))
            else:
                direction = np.zeros(2)
                for a, b in ((kept[k - 1], index) if k else (index, index),
                             (index, kept[k + 1]) if k + 1 < len(kept) else (index, index)):
                    d = pts[b, :2] - pts[a, :2]
                    direction += d / max(float(np.linalg.norm(d)), 1e-9)
                yaw = round(math.degrees(math.atan2(direction[1], direction[0])) % 360., 1)
                row = [group, round(x, 2), round(y, 2), round(z, 2), yaw, 1., height, 0.]
                collide = own and (clearance is None or clearance.clear(row))
                lat, lon = float(ll[index, 1]), float(ll[index, 0])
                source = tower_ids.get(index)
                location = f"{((lon + 180.) % 360.) - 180.:.7f}:{lat:.7f}"
                # Identity remains stable when a neighboring tile has richer/missing node metadata.
                r = {"id": f"power:{group}:{location}", "asset": group,
                     "x": x, "y": y, "z": z, "yaw": yaw, "scale": height / model_height,
                     "height": height, "lat": lat, "lon": lon, "collide": bool(collide),
                     "forestGroup": group}
                if source:
                    r["sourceIds"] = [source]
            old = records.get(r["id"])
            if old is None or (r["collide"] and not old["collide"]):
                records[r["id"]] = r
    return [records[key] for key in sorted(records)]


def attachment(record, conductor, count):
    anchors = mappedinfra.POLE_ANCHORS if count <= 3 else SIX_ANCHORS
    local = np.asarray(anchors[conductor], float) * record["scale"]
    angle = math.radians(record["yaw"])
    c, s = math.cos(angle), math.sin(angle)
    return np.array([record["x"] + c * local[0] - s * local[1],
                     record["y"] + s * local[0] + c * local[1], record["z"] + local[2]])


def wires(wp, offsets, pts, point_offsets, stop, overrides, box, arms_count, cables, min_span, max_span, radius):
    """Keep untouched tower spans byte-for-byte; fit only spans touching an explicit wooden support."""
    spans = list(segments(pts, point_offsets, stop, box, min_span, max_span))
    if len(offsets) - 1 != len(spans) * arms_count:
        raise ValueError("native power-line span order changed; cannot safely fit mapped support anchors")
    out = []
    for index, (start, end) in enumerate(zip(offsets[:-1], offsets[1:])):
        line, i, j = spans[index // arms_count]
        pa = overrides.get(i) if not stop[i] else None
        pb = overrides.get(j) if not stop[j] else None
        polyline = np.asarray(wp[start:end])
        if pa is None and pb is None:
            out.append((polyline, radius))
            continue
        conductor = index % arms_count
        count = cables.get(line, arms_count)
        count = count if count in (3, 6) else arms_count
        count = min(count, arms_count)
        if conductor >= count:
            continue
        a = attachment(pa, conductor, count) if pa is not None else polyline[0]
        b = attachment(pb, conductor, count) if pb is not None else polyline[-1]
        t = np.linspace(0., 1., len(polyline))
        fitted = a[None] * (1. - t[:, None]) + b[None] * t[:, None]
        # A transmission pylon's span-proportional sag can bury the lowest wire when
        # its actual support is a ten-metre wooden pole. Use the audited stock-wire sag.
        fitted[:, 2] -= 4. * 1.278578 * t * (1. - t)
        fitted[0], fitted[-1] = a, b
        out.append((fitted, .012 if pa is not None and pb is not None else radius))
    return out
