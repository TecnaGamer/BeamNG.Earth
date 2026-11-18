"""Norway-only tunnel geometry adaptation for BeamNG.Earth.

Replaces the unfinished Norway experiment.  It uses the existing road, terrain,
and tunnel geometry hooks; it does not fetch data or require additional assets.
Unknown elevations and unconnected roads are deliberately left alone.
"""

import logging
import math

import numpy as np


LOG = logging.getLogger("earthd.norway_tunnels")
AURS = (62.4145045, 8.3222186)
MAJOR = frozenset(("motorway", "trunk", "primary"))
MAX_GRADE = 0.16
MAX_GAP_M = 12.0


def at_aurs(lat, lon):
    return math.hypot((float(lat) - AURS[0]) * 111320.0,
                      (float(lon) - AURS[1]) * 51550.0) < 265.0


def in_norway(lat, lon):
    """Do not apply Norway rules to neighbouring countries near the border."""
    if not (57.9 <= lat <= 71.4 and 4.0 <= lon <= 31.5):
        return False
    import countrybounds
    return countrybounds.bundled().at(float(lat), float(lon)) == "NO"


def location(frame, xy):
    lat, lon, _ = frame.enu_to_geodetic(float(xy[0]), float(xy[1]), 0.0)
    return float(lat), float(lon)


def arc(xy):
    xy = np.asarray(xy, dtype=float)
    return np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(xy[:, :2], axis=0), axis=1))]


def unit(v):
    v = np.asarray(v, dtype=float)
    return v / max(float(np.linalg.norm(v)), 1e-9)


def near_bore(frame, xy):
    """Retain the site's known gravel-road surface override at Aursjøvegen."""
    if not len(xy):
        return False
    return at_aurs(*location(frame, np.asarray(xy)[len(xy) // 2]))


def _observed(frame, xy, spec, level, name=""):
    """Use only elevations positively identified as NVDB, never inferred DEM Z."""
    try:
        import roadz
        provider = roadz.PROVIDER
        if provider is None or not provider.enabled or len(xy) < 3:
            return None
        info = {}
        values = provider.observed(frame, xy, float(spec.width) / 2.0,
                                   level, name, info=info)
        if values is None or info.get("source") != "nvdb":
            return None
        return np.asarray(values, dtype=float)
    except Exception as exc:
        LOG.debug("Tunnel elevation query failed (stock fallbacks remain): %s", exc)
        return None


def _survey(s, z):
    if z is None or len(z) != len(s) or len(s) < 3 or s[-1] < 5:
        return None
    valid = np.isfinite(z) & (z > -500) & (z < 2500)
    if not valid[0] or not valid[-1] or valid.mean() < 0.85:
        return None
    x, y = s[valid], z[valid]
    ds = np.diff(x)
    if np.any(ds <= 0) or np.any(ds > 30) or np.all(np.abs(y) < 0.01):
        return None
    if np.any(np.abs(np.diff(y)) > MAX_GRADE * ds + 0.08):
        return None
    return np.interp(s, x, y)


def _close_micro_openings(s, kinds, ground, floor):
    """Close sub-12m DEM gaps *only* inside a genuinely deep full rock bore."""
    result = np.array(kinds, copy=True)
    i = 0
    while i < len(result):
        if result[i] != 0:
            i += 1
            continue
        j = i
        while j + 1 < len(result) and result[j + 1] == 0:
            j += 1
        enclosed = i > 0 and j + 1 < len(result) and result[i - 1] == result[j + 1] == 2
        if enclosed and s[j + 1] - s[i - 1] <= MAX_GAP_M:
            # A one-sample DEM valley should not create a portal.  Actual
            # daylight gaps have too little cover and remain untouched.
            margins = np.asarray(ground[i - 1:j + 2]) - np.asarray(floor[i - 1:j + 2])
            if np.isfinite(margins).all() and float(np.min(margins)) >= 6.5:
                result[i:j + 1] = 2
        i = j + 1
    return result


def bore_profile(profile, xy, s, spec, name, ground, floor, kinds):
    """Use a validated measured profile; otherwise keep the original floor.

    Aursjøvegen is the only optional straight-grade fallback: a mapped short
    rocky tunnel whose sampled cliff height can be an extreme false spike.
    """
    if spec is None or len(xy) < 3 or s[-1] < 8:
        return floor, kinds, False
    lat, lon = location(profile.frame, xy[len(xy) // 2])
    if not in_norway(lat, lon):
        return floor, kinds, False
    local = at_aurs(lat, lon) and getattr(spec, "cls", "") not in MAJOR
    covered = _close_micro_openings(s, kinds, ground, floor)
    z = _survey(s, _observed(profile.frame, xy, spec, -1, name))
    height = None
    if z is not None:
        try:
            la, lo, _ = profile.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            height = np.asarray(profile.frame.geodetic_to_enu(la, lo, z)[2], dtype=float)
            offset = np.array((ground[0] - height[0], ground[-1] - height[-1]))
            if not np.isfinite(height).all() or np.max(np.abs(offset)) > 10.0:
                height = None
            else:
                height = height + np.interp(s, (s[0], s[-1]), offset)
        except Exception as exc:
            LOG.debug("NVDB bore height conversion failed: %s", exc)
            height = None
    if height is None and local:
        height = np.interp(s, (s[0], s[-1]), (ground[0], ground[-1]))
    if height is not None:
        grade = np.abs(np.diff(height)) / np.maximum(np.diff(s), 0.01)
        if np.isfinite(height).all() and np.max(grade) <= MAX_GRADE + 0.01:
            # Only force the known Aurs rocky mapped bore; we don't promote
            # arbitrary Norwegian roads into tunnels.
            if local and np.count_nonzero(covered) >= max(2, len(covered) // 4):
                covered = np.full_like(covered, 2)
            return height, covered, True
    return floor, covered, False


def entrances(roads, feats, frame, cell):
    """Road ends to reconcile during grading; never treat an entire cell as NO."""
    lat, lon = cell.center()
    if not (57.8 <= lat <= 71.5 and 3.8 <= lon <= 31.7):
        return []
    ends = []
    for i, spec in enumerate(roads.tunnel_specs(feats)):
        if spec is None:
            continue
        for ring in roads.tunnel_parts(feats, i):
            ll = np.asarray(ring, dtype=float)
            if len(ll) < 2:
                continue
            for a, b in ((ll[0], ll[1]), (ll[-1], ll[-2])):
                if not in_norway(a[1], a[0]):
                    continue
                east, north, _ = frame.geodetic_to_enu(np.array((a[1], b[1])),
                                                       np.array((a[0], b[0])), 0.0)
                p, q = np.column_stack((east, north))
                if np.linalg.norm(q - p) > 0.1:
                    ends.append((p, unit(q - p), at_aurs(a[1], a[0])))
    return ends


def approach_grade(frame, plan, spec, ends):
    """Conservative smoothing of a verified connection, never an unrelated road."""
    if not ends or spec is None or len(plan.xy) < 4:
        return
    for side in (0, 1):
        if plan.abut[side]:
            continue
        sl = slice(None, None, -1 if side else 1)
        xy, z = np.asarray(plan.xy)[sl], np.asarray(plan.z)[sl]
        heading = unit(xy[0] - xy[1])
        local = [aurs for p, inward, aurs in ends
                 if np.linalg.norm(xy[0] - p) < 3.0 and np.dot(heading, inward) > 0.7]
        if not local:
            continue
        s = arc(xy)
        n = min(len(s), int(np.searchsorted(s, 64.0, side="right")))
        if n < 4 or s[n - 1] < 30:
            continue
        q = s[:n]
        original = z[:n].copy()
        measured = _survey(q, _observed(frame, xy[:n], spec, 0))
        if measured is not None:
            try:
                # NVDB heights are geographic elevations, but plans are local
                # ENU elevations; mixing them creates giant floating ramps.
                la, lo, _ = frame.enu_to_geodetic(xy[:n, 0], xy[:n, 1], 0.0)
                measured = np.asarray(frame.geodetic_to_enu(la, lo, measured)[2], float)
                if (not np.isfinite(measured).all() or
                        abs(measured[-1] - original[-1]) > 10.0):
                    measured = None
                else:
                    measured += original[-1] - measured[-1]
            except Exception as exc:
                LOG.debug("Approach elevation conversion failed: %s", exc)
                measured = None
        if measured is None:
            # For the Aurs example only, address a blatant near-portal DEM
            # cliff spike; don't invent a grade elsewhere.
            if not any(local):
                continue
            remote = q >= 0.55 * q[-1]
            gradients = np.diff(original[remote]) / np.maximum(np.diff(q[remote]), 0.01)
            if not len(gradients):
                continue
            slope = float(np.clip(np.median(gradients), -MAX_GRADE, MAX_GRADE))
            measured = np.median(original[remote] - slope * q[remote]) + slope * q
            if abs(measured[0] - original[0]) < 1.75:
                continue
        weight = np.clip((q[-1] - q) / max(1, q[-1] * 0.55), 0, 1)
        weight = weight * weight * (3 - 2 * weight)
        updated = original + (measured - original) * weight
        before = np.max(np.abs(np.diff(original)) / np.maximum(np.diff(q), 0.01))
        after = np.max(np.abs(np.diff(updated)) / np.maximum(np.diff(q), 0.01))
        if np.isfinite(updated).all() and after <= max(MAX_GRADE + 0.02, before):
            z[:n] = updated


def _style(chunk, fit):
    """A realistic small portal only where the world already has a mouth."""
    th = float(chunk.get("tunnel_h") or 6.8)
    st = fit.get("st") if isinstance(fit, dict) else None
    major = chunk.get("concrete") or chunk.get("cls") in MAJOR
    length = float(chunk.get("tunnel_len") or 0.0)
    if th < 5.9 or (length and length < 28):
        return None  # existing compact underpasses remain intact
    if major:
        return "box" if st in (1, 3) else "arch"
    return "rock"


def tag_chunks(tunnels, chunks):
    """Apply uniform rock lining + portal choices to each Norwegian bore."""
    frame = getattr(tunnels, "frame", None)
    if frame is None:
        return
    for c in chunks:
        if not c.get("tunnel") or c.get("no_tube") or len(c.get("n", ())) < 2:
            continue
        pts = np.asarray(c["n"], dtype=float)
        if not in_norway(*location(frame, pts[len(pts) // 2])):
            continue
        fits = c.get("fit") or (None, None)
        portals = c.get("portal") or (False, False)
        rock = (_style(c, None) == "rock")
        c["norway_rock"] = rock
        styles = []
        for i, mouth in enumerate(portals):
            if not mouth or not in_norway(*location(frame, pts[-1 if i else 0])):
                styles.append(None)
            else:
                styles.append(_style(c, fits[i] if i < len(fits) else None))
        c["norway_portal_style"] = styles


def mouth_mesh(p, left, out, sec, th, hw, style, fit, near):
    """Narrow textured collar outside the exact roadmesh opening (no road obstruction)."""
    p, left, out = np.asarray(p, float), unit(left), unit(out)
    profile = np.asarray(sec, float)[1:-1]
    if len(profile) < 2:
        return np.empty((0, 3)), np.empty((0, 3), int), np.empty((0, 2)), np.empty(0, int)
    x, z = profile.T
    h = np.clip(z / max(float(th), 0.01), 0, 1)
    rock = style == "rock"
    rectangular = style == "box"
    thickness = (0.34 + 0.07 * np.sin(3.0 * h + x) ** 2) if rock else 0.42
    exterior_x = x + np.sign(x) * thickness
    exterior_z = z + thickness * h
    if rectangular:
        exterior_z = np.where(h > 0.88, float(th) + 0.46, exterior_z)
    # Extends towards the road by at most 0.8m: far less than generic headwalls.
    projection = (0.25 + 0.15 * h) if rock else (0.55 + 0.25 * h)
    def xyz(a, b, depth):
        depth = np.broadcast_to(depth, np.shape(a))
        return p + np.c_[a[:, None] * left + depth[:, None] * out, b]
    inner = xyz(x, z, projection)
    outer = xyz(exterior_x, exterior_z, projection)
    back = xyz(exterior_x, exterior_z, np.zeros(len(x)))
    rim = xyz(x, z, np.zeros(len(x)))
    vertices, uv, tris, codes = [], [], [], []
    def strip(a, b, code):
        start = len(vertices)
        vertices.extend(a)
        vertices.extend(b)
        t = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(a, axis=0), axis=1))] / 2.5
        uv.extend(np.c_[t, np.zeros(len(a))])
        uv.extend(np.c_[t, np.linalg.norm(b - a, axis=1) / 2.5])
        for j in range(len(a) - 1):
            i = start + j
            k = len(a)
            tris.extend(((i, i + 1, i + k), (i + 1, i + k + 1, i + k)))
            codes.extend((code, code))
    strip(rim, inner, 0)
    strip(inner, outer, 0 if rock else 2)
    strip(outer, back, 0)
    v = np.asarray(vertices, float)
    t = np.asarray(tris, int)
    normals = np.cross(v[t[:, 1]] - v[t[:, 0]], v[t[:, 2]] - v[t[:, 0]])
    # Explicit winding: the narrow inner lip faces the driving envelope,
    # the visible frame faces the approaching driver, and outside faces rock.
    count = 2 * (len(profile) - 1)
    for i, triangle in enumerate(t):
        centre = v[triangle].mean(axis=0) - p
        radial = np.r_[left * np.dot(centre[:2], left), centre[2] - 0.4 * th]
        expected = -radial if i < count else (np.r_[out, 0.0] if i < 2 * count else radial)
        if np.dot(normals[i], expected) < 0:
            t[i] = triangle[::-1]
    good = np.linalg.norm(normals, axis=1) > 1e-10
    return v, t[good], np.asarray(uv), np.asarray(codes, int)[good]


def _apron_heights(s, start_z, terrain_z):
    """End at natural approach height without exceeding road-legal ramp grade."""
    if len(s) < 2 or s[-1] < 4 or not np.isfinite(terrain_z).all():
        return None
    end_z = float(terrain_z[-1]) - 0.035
    mean = abs(end_z - start_z) / s[-1]
    if mean > MAX_GRADE * 0.7:
        return None
    # Cosine-smooth endpoints: 1.5*average maximum derivative.
    t = np.clip(s / s[-1], 0, 1)
    ease = t * t * (3.0 - 2.0 * t)
    zz = start_z + (end_z - start_z) * ease
    if np.max(np.abs(np.diff(zz)) / np.maximum(np.diff(s), 1e-4)) > MAX_GRADE:
        return None
    return zz


def aprons(roads, tunnels, cells, parts, box):
    """Add a short colliding entrance ramp only for an actual matching road.

    No road match = no synthetic road. No safe ramp gradient = no change.
    """
    if not parts or getattr(tunnels, "frame", None) is None or not hasattr(tunnels, "_ground"):
        return []
    frame = tunnels.frame
    mouths = []
    for fid, spec, xy, z, _ in parts:
        xy, z = np.asarray(xy, float), np.asarray(z, float)
        if len(xy) < 2 or len(z) != len(xy) or not np.isfinite(z).all():
            continue
        for side, present in enumerate(tunnels.mouth(fid)):
            if not present:
                continue
            a, b = ((0, 1) if side == 0 else (-1, -2))
            p, q = xy[a, :2], xy[b, :2]
            if (np.linalg.norm(p - q) < .1 or
                not (box[0] - 2 <= p[0] <= box[2] + 2 and box[1] - 2 <= p[1] <= box[3] + 2)):
                continue
            if in_norway(*location(frame, p)):
                mouths.append((p, unit(q - p), float(z[a]), spec))
    if not mouths:
        return []
    paths = []
    for feats in cells:
        if feats is None or not len(feats):
            continue
        for i, spec in enumerate(roads.road_specs(feats)):
            if spec is None:
                continue
            for ring in roads.ground_parts(feats, i):
                ll = np.asarray(ring, float)
                if len(ll) < 2:
                    continue
                e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
                path = np.c_[e, n]
                if arc(path)[-1] >= 10:
                    paths.extend(((path, spec), (path[::-1], spec)))
    made, out = set(), []
    for p, inward, z0, spec in mouths:
        key = (round(float(p[0]), 1), round(float(p[1]), 1))
        if key in made:
            continue
        made.add(key)
        best = None
        for line, other_spec in paths:
            dist = float(np.linalg.norm(line[0] - p))
            if dist > 3.5:
                continue
            ss = arc(line)
            t = min(6.0, ss[-1])
            far = np.array([np.interp(t, ss, line[:, j]) for j in (0, 1)])
            alignment = float(np.dot(unit(far - line[0]), -inward))
            if alignment < 0.8:
                continue
            score = dist + 5.0 * (1.0 - alignment)
            if best is None or score < best[0]:
                best = (score, line, ss, other_spec)
        if best is None:
            continue
        _, line, ss, asp = best
        # Longer ramps are permitted where terrain is reliable, no more than 24m.
        length = min(24.0, ss[-1])
        stations = np.linspace(0.0, length, max(9, 1 + math.ceil(length / 1.5)))
        xy = np.c_[np.interp(stations, ss, line[:, 0]), np.interp(stations, ss, line[:, 1])]
        offset = p - xy[0]
        xy += offset * (1.0 - stations / length)[:, None]
        try:
            ground = np.asarray(tunnels._ground(xy), float)
        except Exception:
            continue
        if ground.shape != (len(stations),):
            continue
        heights = _apron_heights(stations, z0, ground)
        if heights is None:
            continue
        points = np.c_[xy, heights]
        for segment in roads.clip_polyline(points, *box):
            if len(segment) < 2:
                continue
            look = (roads.DIRT_LOOK["gravel"] if at_aurs(*location(frame, p))
                    else roads._deck_look(spec))
            piece = roads._chunk(look, min(float(spec.width), float(asp.width)), segment)
            piece.update(fixed=True, deck=True, tunnel=True, no_tube=True,
                         portal=[False, False], norway_apron=True)
            out.append(piece)
    return out


def clear_lips(mask, parts, frame, u, cx, cy, square, heights, mouths, full_height):
    """Remove a few steep terrain squares in front of a *real* tunnel entrance."""
    n = u.shape[0]
    origin = np.array((cx, cy), float) - n * square / 2.0
    for fid, spec, xy, z, _ in parts:
        xy, z = np.asarray(xy, float), np.asarray(z, float)
        if len(xy) < 2 or heights.get(fid, full_height) < full_height - 0.05:
            continue
        for side, present in enumerate(mouths.get(fid, (True, True))):
            if not present:
                continue
            k, j = ((0, 1) if side == 0 else (-1, -2))
            p = xy[k, :2]
            if not in_norway(*location(frame, p)):
                continue
            inward = unit(xy[j, :2] - p)
            half_width = float(spec.width) / 2.0
            radius = half_width + 3.0 + square
            lo = np.maximum(0, np.floor((p - origin - radius) / square).astype(int))
            hi = np.minimum(n, np.ceil((p - origin + radius) / square).astype(int))
            if np.any(hi <= lo):
                continue
            row, col = np.mgrid[lo[1]:hi[1], lo[0]:hi[0]]
            dx = origin[0] + (col + 0.5) * square - p[0]
            dy = origin[1] + (row + 0.5) * square - p[1]
            along = dx * inward[0] + dy * inward[1]
            across = np.abs(dx * inward[1] - dy * inward[0])
            x2, y2 = np.minimum(col + 1, n - 1), np.minimum(row + 1, n - 1)
            corners = np.array((u[row, col], u[row, x2], u[y2, col], u[y2, x2]))
            steep = (np.ptp(corners, axis=0) > max(1.2, 0.6 * square))
            above = corners.min(axis=0) > z[k] + 0.05
            # Just inside the portal: never excavate several metres into a hillside.
            mask[row, col] |= ((along >= -0.35) & (along <= min(2.0, square)) &
                               (across <= half_width) & steep & above)


def shell(mid, left, z, sec, hw, th, origin):
    """Subtle outside-of-bore skin; uses the project's existing stone texture."""
    lateral, up = np.asarray(sec, float)[1:-1].T
    middle, left, z = np.asarray(mid, float), np.asarray(left, float), np.asarray(z, float)
    along = lateral * (1.0 + 0.24 / max(hw, 0.1))
    roof = up * (1.0 + 0.24 / max(th, 0.1))
    xy = middle[None, :, :] + along[:, None, None] * left[None, :, :]
    vertex = np.concatenate((xy, (z[None, :] + roof[:, None])[:, :, None]), axis=2)
    vertex -= np.asarray(origin, float)
    nrow, ncol = vertex.shape[:2]
    if nrow < 2 or ncol < 2:
        return np.empty((0, 3)), np.empty((0, 3), int), np.empty((0, 2)), np.empty(0, int)
    s, u = arc(middle), arc(np.c_[along, roof])
    uv = np.stack(np.broadcast_arrays(s[None, :], u[:, None]), axis=-1).reshape(-1, 2) / 3.0
    row, col = np.mgrid[0:nrow - 1, 0:ncol - 1]
    a = (row * ncol + col).ravel()
    triangles = np.r_[np.c_[a, a + ncol, a + 1],
                      np.c_[a + ncol, a + ncol + 1, a + 1]]
    verts = vertex.reshape(-1, 3)
    axis = np.c_[middle, z + 0.45 * th] - origin
    aim = verts[triangles].mean(axis=1) - axis[triangles % ncol].mean(axis=1)
    normal = np.cross(verts[triangles[:, 1]] - verts[triangles[:, 0]],
                      verts[triangles[:, 2]] - verts[triangles[:, 0]])
    reverse = (normal * aim).sum(axis=1) < 0
    triangles[reverse] = triangles[reverse, ::-1]
    return verts, triangles, uv, np.zeros(len(triangles), dtype=int)
