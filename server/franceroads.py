"""Opt-in French road scenery (BNE_FR_ROADS=on).

Mapped widths/lane rules win. Unmapped rural widths, edge paint and bend
restrictions are scenery estimates, not surveyed road markings or traffic law.
The flag is read at process start and included by revisit.code_key in caches.
"""
import math
import os

import numpy as np

ON = os.environ.get("BNE_FR_ROADS", "off").lower() in {"on", "1", "true"}
VERSION = "fr-roads-signs-v5"
PAINT_MATERIAL = "line_bne_fr_white"
RURAL_WIDTH = {"primary": 7.0, "secondary": 6.0, "tertiary": 5.0,
               "unclassified": 4.5, "residential": 5.0, "service": 3.5}
T1, TP1, T3, T2, T4 = (3., 10.), (1.5, 5.), (3., 1.33), (3., 3.5), (39., 13.)

# Appearance reference supplied by Alexandre for the forest road north of
# Haguenau (old D27, now D927). Scope is one identified OSM way, not all roads.
# The photos establish a mixed line without verges, not every change of passing
# permission along its length. This trial heading is therefore representative;
# explicit OSM restrictions always take precedence.
OBSERVED_MIXED = {15954261: 330.}


def observed_lane_tags(feats, i, tags):
    import osmtags
    tags = dict(tags or {})
    if not ON or any(k.startswith("overtaking") or k == "divider" for k in tags):
        return tags or None
    col = feats.attrs.get("src_record")
    way = osmtags.way_id(str(col[i])) if col is not None else None
    if way not in OBSERVED_MIXED:
        return tags or None
    a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
    b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
    ll = np.asarray(feats.lonlat[a:b])
    if len(ll) < 2:
        return tags or None
    delta = ll[-1] - ll[0]
    delta[0] *= math.cos(math.radians(float(ll[:, 1].mean())))
    if np.linalg.norm(delta) < 1e-10:
        return tags or None
    bearing = math.radians(OBSERVED_MIXED[way])
    forward = float(delta @ np.array([math.sin(bearing), math.cos(bearing)])) > 0
    tags["overtaking:forward"] = "no" if forward else "yes"
    tags["overtaking:backward"] = "yes" if forward else "no"
    tags["_fr_edge"] = "no"
    return tags


def french_feature(feats, i):
    """Conservative whole-feature gate, using its geography, never an airport.

    Border-crossing/unknown features keep upstream geometry and paint. Testing
    vertices and segment midpoints also catches a road crossing a narrow enclave.
    """
    import countrybounds
    a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
    b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
    ll = np.asarray(feats.lonlat[a:b])
    if len(ll) < 2 or not np.isfinite(ll).all():
        return False
    # Metropolitan France only; overseas territories keep their own codes.
    if not ((ll[:, 0] > -5.3) & (ll[:, 0] < 9.8) & (ll[:, 1] > 41.3) & (ll[:, 1] < 51.2)).all():
        return False
    idx = countrybounds.bundled()
    return all(idx.at(float(lat), float(lon)) == "FR"
               for lon, lat in np.vstack([ll, (ll[:-1] + ll[1:]) / 2]))


def adjust_specs(feats, specs):
    import roads
    if not ON:
        return specs
    out = list(specs)
    for i, sp in enumerate(out):
        if sp is None or sp.kind != "paved" or not french_feature(feats, i):
            continue
        sp = sp._replace(france=True)
        mapped = roads._rule_value(feats.rules.get("width"), i)
        if mapped is None:
            mapped = roads._osm(feats, "width", i, *roads.OSM_WIDTH_M)
        if (not sp.urban and not sp.link and not sp.median and not getattr(sp, "mountain", False)
                and not roads.is_ring(feats, i)
                and sp.cls in RURAL_WIDTH and mapped is None):
            if sp.lanes:
                # Two mapped lanes describe traffic directions, not a measured
                # six-metre width: narrow French roads can have two small lanes.
                width = RURAL_WIDTH[sp.cls] if int(sp.lanes) == 2 and not sp.oneway else \
                    max(3.0, int(sp.lanes) * 3.0)
            else:
                width = 3.5 if sp.oneway else RURAL_WIDTH[sp.cls]
            sp = sp._replace(width=width)
        out[i] = sp
    return out


def enabled(spec):
    return ON and bool(getattr(spec, "france", False)) and not getattr(spec, "mountain", False)


def narrow(spec):
    return (not spec.urban and not spec.oneway and not spec.link and not spec.median
            and spec.kind == "paved" and 4.0 <= spec.width < 5.2
            and spec.cls in {"primary", "secondary", "tertiary", "unclassified"})


def edge_pattern(spec):
    """None = no inferred edge paint, False = solid, tuple = dash/gap metres."""
    if spec.kind != "paved" or spec.urban or narrow(spec):
        return None
    if spec.cls == "motorway":
        return T4
    if spec.cls == "trunk" and spec.oneway:
        return T4
    if spec.cls in {"primary", "secondary", "trunk"} and spec.width >= 6.5:
        return T2
    return None


def unit(spec):
    return .075 if spec.cls == "motorway" or spec.median or (spec.lanes or 0) >= 4 else \
        .06 if spec.cls in {"trunk", "primary"} else .05


def continuations(rows):
    """Mutual straightest road ends; keep paint across an ordinary map split.

    Unlike the gravel-edge rule, crossing/T arms are not all continuations.
    Other junction arms still clip the markings normally.
    """
    import furniture
    result = set()
    for arms in furniture._arms(rows).values():
        best = {}
        for a in arms:
            options = [(float(a[2] @ b[2]), b[0]) for b in arms if a[0] != b[0]
                       and a[1].oneway == b[1].oneway
                       and (not a[1].oneway or furniture._outbound(a) != furniture._outbound(b))]
            if options:
                dot, other = min(options)
                if dot <= -math.cos(math.radians(20)):
                    best[a[0]] = other
        for a, b in best.items():
            if best.get(b) == a:
                result.add((a, b))
    return result


def stations(pts):
    return np.r_[0., np.cumsum(np.linalg.norm(np.diff(np.asarray(pts)[:, :2], axis=0), axis=1))]


def at(pts, s):
    """Position and horizontal forward tangent, with duplicate nodes removed."""
    p = np.asarray(pts, dtype=float)
    p = p[np.r_[True, np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1) > 1e-6]]
    if len(p) < 2:
        return p[0].copy(), np.array([0., 1.])
    ss = stations(p)
    s = float(np.clip(s, 0., ss[-1]))
    i = min(int(np.searchsorted(ss, s, side="right")) - 1, len(p) - 2)
    t = (s - ss[i]) / (ss[i + 1] - ss[i])
    d = p[i + 1, :2] - p[i, :2]
    return p[i] * (1 - t) + p[i + 1] * t, d / np.linalg.norm(d)


def sample(pts, values):
    """Batch version of at, so long features cost O(vertices + samples)."""
    p = np.asarray(pts, float)
    p = p[np.r_[True, np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1) > 1e-6]]
    values = np.asarray(values, float)
    if len(p) < 2:
        return np.tile(p[0], (len(values), 1)), np.tile([0., 1.], (len(values), 1))
    ss = stations(p)
    values = np.clip(values, 0., ss[-1])
    ii = np.clip(np.searchsorted(ss, values, side="right") - 1, 0, len(p) - 2)
    t = ((values - ss[ii]) / (ss[ii + 1] - ss[ii]))[:, None]
    q = p[ii] * (1 - t) + p[ii + 1] * t
    d = p[ii + 1, :2] - p[ii, :2]
    d /= np.linalg.norm(d, axis=1)[:, None]
    return q, d


def segment(pts, a, b):
    ss = stations(pts)
    return np.vstack([at(pts, a)[0], np.asarray(pts)[(ss > a) & (ss < b)], at(pts, b)[0]])


def bends(pts):
    """Pronounced bends, sampled on the whole road before any tile clipping.

    These are candidates, not a map of official dangerous-bend sign locations.
    Return (station m, signed turn radians); positive is left along the geometry.
    """
    length = stations(pts)[-1]
    ss = np.arange(30., length - 30., 10.)
    a, _ = sample(pts, ss - 30.)
    p, _ = sample(pts, ss)
    b, _ = sample(pts, ss + 30.)
    u, v = p[:, :2] - a[:, :2], b[:, :2] - p[:, :2]
    turns = np.arctan2(u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0], np.sum(u * v, axis=1))
    keep = (np.minimum(np.linalg.norm(u, axis=1), np.linalg.norm(v, axis=1)) >= 10) & (np.abs(turns) >= math.radians(25))
    candidates = list(zip(ss[keep].tolist(), turns[keep].tolist()))
    # Keep the strongest sample of each bend; no row of signs on one curve.
    groups = []
    for event in candidates:
        if groups and event[0] - groups[-1][-1][0] <= 40 and event[1] * groups[-1][-1][1] > 0:
            groups[-1].append(event)
        else:
            groups.append([event])
    return [max(g, key=lambda e: abs(e[1])) for g in groups]


def mre_polygons(pts):
    """IISR D9 MRE: 10 cm half-width, 25 cm sides, 8 cm step, 120° angle.

    Two parallelograms per mark, 15 m apart. Their true 20 cm width is preserved
    through clipping and mesh export; these are not a stretched dash texture.
    """
    length = stations(pts)[-1]
    shear = .1 / math.sqrt(3)
    positions, directions = sample(pts, np.arange(7.5, length - .5, 15.))
    for p, d in zip(positions, directions):
        left = np.array([-d[1], d[0]])
        for x0, x1, step in ((-.1, 0., 0.), (0., .1, .08 + shear)):
            xy = [(x0, step), (x1, step + shear), (x1, step + shear + .25), (x0, step + .25)]
            q = np.tile(p, (4, 1))
            for i, (x, y) in enumerate(xy):
                q[i, :2] += left * x + d * (y - (.25 + .08 + 2 * shear) / 2)
            yield q


def recall_polygons(pts, s, direction):
    """Approximate six-metre curved return arrow, bending to the driver's right.

    Convex pieces avoid concave-polygon triangulation ambiguities. Use only for
    an inferred end of an overtaking section, never as generic lane-turn arrows.
    """
    p, d = at(pts, s)
    d *= direction
    left = np.array([-d[1], d[0]])
    t = np.linspace(0., 1., 9)
    xy = np.c_[.55 - .85 * t * t, -3. + 5. * t]

    def world(local):
        q = np.tile(p, (len(local), 1))
        local = np.asarray(local)
        q[:, :2] += local[:, 0, None] * left + local[:, 1, None] * d
        # The arrow follows the road's grade rather than floating horizontally.
        for k, y in enumerate(local[:, 1]):
            q[k, 2] = at(pts, s + direction * y)[0][2]
        return q

    for a, b in zip(xy[:-1], xy[1:]):
        n = np.array([b[1] - a[1], a[0] - b[0]])
        n = n / np.linalg.norm(n) * .075
        yield world([a - n, b - n, b + n, a + n])
    yield world([(-.76, 1.45), (-.5, 3.), (.12, 1.85)])


def paint(spec, pts, osm=None):
    """roads.extras tuples. Whole-road phases; geometry-inferred fallback is explicit.

    Where OSM describes overtaking, it wins for the entire mapped feature. Without
    that evidence, only pronounced bends receive estimated no-passing approaches.
    Observed local references are supplied by observed_lane_tags; their limits
    are documented separately from mapped passing restrictions.
    """
    import markings
    import roads
    out = []
    osm = {k: v for k, v in (osm or {}).items() if v is not None}
    if osm.get("overtaking") in {"forward", "backward"}:
        allowed = osm.pop("overtaking")
        osm.setdefault("overtaking:forward", "yes" if allowed == "forward" else "no")
        osm.setdefault("overtaking:backward", "yes" if allowed == "backward" else "no")
    if osm.get("divider") == "solid_line" and not any(k.startswith("overtaking") for k in osm):
        osm["overtaking"] = "no"
    elif osm.get("divider") == "dashed_line" and not any(k.startswith("overtaking") for k in osm):
        osm["overtaking"] = "yes"
    if osm.get("overtaking") == "both":
        osm["overtaking"] = "yes"
    if narrow(spec):
        # MRE guides an undivided road: OSM lane_markings=no is compatible.
        return [({"m": PAINT_MATERIAL, "tl": 1., "p": 5, "kind": "mre", "polygon": True}, 0., q)
                for q in mre_polygons(pts)]
    no_lanes = str(osm.get("lane_markings", "")).lower() == "no"
    width = 2 * unit(spec)
    length = stations(pts)[-1]
    if length < .01:
        return out

    def line(kind, offset, pattern=None, a=0., b=None, side=None, arrows=None):
        run = segment(pts, a, length if b is None else b)
        if abs(offset) > 1e-8:
            run = roads.offset_polyline(run, offset)
        look = {"m": PAINT_MATERIAL, "tl": 1., "p": 5, "kind": kind}
        if side is not None:
            look["side"] = side
        for p in [run] if pattern is None else markings.dashes(run, pattern, phase=a):
            if arrows is not None and any(
                not arrows.visible(start, end, width / 2 + .025)
                for start, end in zip(p[:-1], p[1:])
            ):
                continue              # a recall arrow takes the place of the overlapping dash
            out.append((look.copy(), 3 * unit(spec) if kind == "edge" else width, p))

    drive, lanes = roads.lane_layout(spec)
    plan = markings.lane_plan(osm, not spec.oneway, drive, lanes, False)
    if plan is None:
        plan = markings.lane_plan({"lanes:forward": str(lanes if spec.oneway else max(1, lanes // 2))},
                                  not spec.oneway, drive, lanes, False)
    if no_lanes:
        plan = []                         # edge guidance is independent of lanes
    explicit = any(str(osm.get(k, "")).lower() in {"yes", "no", "both", "forward", "backward"}
                   for k in ("overtaking", "overtaking:forward", "overtaking:backward", "divider"))
    events = bends(pts) if not explicit and not spec.urban and not spec.oneway and spec.width >= 5.2 else []
    for off, role, how in plan or []:
        if role == "lane":
            line("lane", off, None if how else (TP1 if spec.urban else T1))
        elif role == "centre" and spec.width >= 5.2:
            gap = (width + .1) / 2
            pattern = TP1 if spec.urban else T1
            if how == "double":
                line("centre", off)
            elif how in {"solid_right", "solid_left"}:
                sign = -1 if how == "solid_right" else 1
                line("centre", off + sign * gap)
                line("centre", off - sign * gap, pattern)
            elif events:
                import signplacement

                recall = [
                    q
                    for s, _ in events
                    for direction in (1, -1)
                    for back in (210., 185., 160.)
                    if 4. <= s - direction * back <= length - 4.
                    for q in recall_polygons(pts, s - direction * back, direction)
                ]
                arrows = signplacement.Context(recall) if recall else None
                cuts = sorted({0., length} | {float(np.clip(s + d, 0, length))
                                             for s, _ in events for d in (-225., -150., -35., 35., 150., 225.)})
                for a, b in zip(cuts[:-1], cuts[1:]):
                    if b - a < .1:
                        continue
                    mid = (a + b) / 2
                    fwd = any(s - 150 <= mid <= s + 35 for s, _ in events)
                    bwd = any(s - 35 <= mid <= s + 150 for s, _ in events)
                    warning = any(s - 225 <= mid <= s - 150 or s + 150 <= mid <= s + 225 for s, _ in events)
                    if fwd and bwd:
                        line("centre", off, a=a, b=b)
                    elif fwd or bwd:
                        sign = -1 if fwd else 1
                        line("centre", off + sign * gap, a=a, b=b)
                        line("centre", off - sign * gap, T3, a, b, arrows=arrows)
                    else:
                        line("centre", off, T3 if warning else pattern, a, b, arrows=arrows)
                out.extend(({"m": PAINT_MATERIAL, "tl": 1., "p": 5, "kind": "recall", "polygon": True}, 0., q)
                           for q in recall)
            else:
                line("centre", off, pattern)
        elif role == "twltl":
            # Preserve upstream's mapped shared turn lane, in French white.
            gap = (width + .1) / 2
            line("centre", off + how * gap)
            line("centre", off - how * gap, TP1 if spec.urban else T1)
    ep = edge_pattern(spec)
    if ep is not None and not roads.has_sidewalks(spec) and osm.get("_fr_edge") != "no":
        inset = roads.motorway_shoulder(spec) if roads.rural_motorway(spec) else .25
        for side in (1, -1):
            # The central reservation's edge is continuous, the hard shoulder T4.
            pat = None if spec.median and side == (1 if spec.dir >= 0 else -1) else ep
            line("edge", side * (spec.width / 2 - inset), pat, side=side)
    return out
