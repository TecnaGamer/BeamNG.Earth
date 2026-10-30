"""Place mapped Overture guard rails using the stock Italy guardrail Forest models.

Mapped guard rails, jersey barriers (the stock jersey barrier) and cable barriers (the mod's model) are rendered.
Their Forest rows reuse the game's model batching and near-field collision path instead of creating a custom mesh
for every road tile. medians.py adds the median barriers where none is mapped, in the same rows.
"""
import hashlib
import json

import geotiles
import logging
import math

import numpy as np

import mapcells
import mapgeom

log = logging.getLogger("earthd.guardrails")

BBOX_PAD_M = 2.0


# The Forest groups of the rows (trees.lua's CATALOG): Italy's rail, its caps and the reverse-facing three; the stock
# jersey barrier and its sloped end (they run along their model's y axis: the rows' first two columns swapped); the
# mod's cable barrier (tools/build_median_props.py). medians.py's rows use the same groups.
GROUPS = ("guardrail", "guardrail_start", "guardrail_end", "guardrail_back", "guardrail_back_start",
          "guardrail_back_end", "jersey", "jersey_end", "cable")
G_JERSEY, G_JERSEY_END, G_CABLE = 6, 7, 8
KINDS = {"guard_rail": 0, "jersey_barrier": G_JERSEY, "cable_barrier": G_CABLE}    # mapped class -> its module group
HEIGHTS = {G_JERSEY: 0.88, G_JERSEY_END: 0.88, G_CABLE: 1.0}                       # (the rest: MODULE_HEIGHT_M)


def _barrier_kind(feats, i):
    """A mapped barrier line's kind among KINDS, or None: Overture's normalized class, falling back to the preserved
    OSM tags."""
    classes = feats.attrs.get("class")
    subtypes = feats.attrs.get("subtype")
    cls = str(classes[i]).strip().lower() if classes is not None else ""
    subtype = str(subtypes[i]).strip().lower() if subtypes is not None else ""
    if subtype == "barrier" and cls in KINDS:
        return cls
    tags = feats.attrs.get("tags")
    if tags is None:
        return None
    try:
        values = json.loads(str(tags[i]))
    except (TypeError, ValueError):
        return None
    # source_tags is optional in Overture and may be absent, JSON null, or another
    # JSON value. Only an object can contain OSM tag keys.
    if not isinstance(values, dict):
        return None
    v = str(values.get("barrier", "")).strip().lower()
    return v if v in KINDS else None


def _is_guardrail(feats, i):
    """Whether a mapped line is a guard rail (Overture's class, else the OSM tags)."""
    return _barrier_kind(feats, i) == "guard_rail"


def _deck_level_hint(feats, i):
    """Return True for a mapped bridge-level barrier, False for lower/ground level, else None.

    These tags belong to the barrier way itself. A negative layer must not be overwritten by a
    nearby bridge deck merely because both features overlap in XY.
    """
    tags = feats.attrs.get("tags")
    if tags is None:
        return None
    try:
        values = json.loads(str(tags[i]))
    except (TypeError, ValueError):
        return None
    if not isinstance(values, dict):
        return None

    norm = {str(k).strip().lower(): str(v).strip().lower() for k, v in values.items()}
    yes = {"yes", "true", "1"}
    no = {"no", "false", "0"}
    if norm.get("tunnel", "") in yes:
        return False

    layer = None
    try:
        layer = float(norm.get("layer", ""))
    except (TypeError, ValueError):
        pass
    if layer is not None and layer < 0:
        return False

    bridge = norm.get("bridge", "")
    if bridge in yes or bridge in {"viaduct", "movable", "swing", "drawbridge", "covered"}:
        return True
    if norm.get("location", "") in {"bridge", "on_bridge"}:
        return True
    if layer is not None:
        return layer > 0
    if bridge in no or norm.get("location", "") in {"underground", "underwater", "underground_passage"}:
        return False
    return None


# Mapped rails on a bridge deck's edge are dropped: bridge_rows() builds the deck's own rails from its
# geometry, so a mapped rail there would only double them (and z-fight with them).
BRIDGE_SIDE_CLEARANCE_M = 1.5   # m beyond the deck's road edge (its 0.45 m parapet strip is part of that)


def _at(line, arc, distance):
    """Point and unit tangent at arc length distance along a 2D polyline."""
    k = min(max(int(np.searchsorted(arc, distance, side="right") - 1), 0), len(line) - 2)
    span = max(float(arc[k + 1] - arc[k]), 1e-9)
    t = float(np.clip((distance - arc[k]) / span, 0.0, 1.0))
    d = line[k + 1] - line[k]
    length = max(float(np.hypot(d[0], d[1])), 1e-9)
    return line[k] + t * d, d / length


DECK_AUTO_SNAP_TOLERANCE_M = 1.5  # Untagged rails snap to a deck only when terrain agrees.



DECK_WIDE_REACH_M = 12.0     # m: the nearest deck's height is also looked up this far out (between two decks)


def _surface_info(frame, points, elev, decks, deck_preference=None):
    """Return (z, ground_z, deck_wide_z) for XY points.

    z: terrain first; snapped to a deck only when tags identify its level or heights agree.
    OSM barrier lines are 2-D. XY-only snapping is wrong at an overpass: a lower road's guardrail
    can otherwise be lifted onto the bridge above it. Explicit bridge/layer tags disambiguate levels;
    for untagged ways the terrain must be within a small tolerance of the candidate deck.
    ground_z: the terrain alone. deck_wide_z: the nearest deck's height within DECK_WIDE_REACH_M of its
    edge (NaN if none), for rails the caller knows stand on a deck (centre rails, medians).
    """
    points = np.asarray(points, np.float64).reshape(-1, 2)
    if not len(points):
        empty = np.zeros(0, np.float64)
        return empty, empty, empty

    la, lo, _ = frame.enu_to_geodetic(points[:, 0], points[:, 1], np.zeros(len(points), np.float64))
    ground = np.asarray(elev(la, lo), np.float64)
    ground_z = np.asarray(frame.geodetic_to_enu(la, lo, ground)[2], np.float64)

    deck_z = np.full(len(points), np.nan, np.float64)
    best = np.full(len(points), np.inf, np.float64)
    wide_z = np.full(len(points), np.nan, np.float64)
    wide_best = np.full(len(points), np.inf, np.float64)
    for deck in decks or ():
        line = np.asarray(deck.get("n", ()), np.float64)
        if len(line) < 2 or line.shape[1] < 3:
            continue
        a, b = line[:-1, :2], line[1:, :2]
        d = b - a
        l2 = np.maximum(np.sum(d * d, axis=1), 1e-12)
        v = points[:, None, :] - a[None, :, :]
        t = np.clip(np.sum(v * d[None, :, :], axis=2) / l2[None, :], 0.0, 1.0)
        near = a[None, :, :] + t[:, :, None] * d[None, :, :]
        dist = np.hypot(points[:, None, 0] - near[:, :, 0],
                        points[:, None, 1] - near[:, :, 1])
        k = np.argmin(dist, axis=1)
        row = np.arange(len(points))
        dmin = dist[row, k]
        half = float(deck.get("w", 0.0)) / 2.0
        seg_z = line[k, 2] + t[row, k] * (line[k + 1, 2] - line[k, 2])
        use = (dmin <= half + 2.0) & (dmin < best)
        if use.any():
            deck_z[use], best[use] = seg_z[use], dmin[use]
        wide = (dmin <= half + DECK_WIDE_REACH_M) & (dmin < wide_best)
        if wide.any():
            wide_z[wide], wide_best[wide] = seg_z[wide], dmin[wide]

    if deck_preference is None:
        preference = np.full(len(points), -1, np.int8)  # unknown: infer conservatively from terrain
    else:
        preference = np.asarray([
            -1 if value is None else (1 if bool(value) else 0)
            for value in deck_preference
        ], np.int8)
        if len(preference) != len(points):
            raise ValueError("deck_preference must contain one value per sample point")

    found = np.isfinite(deck_z)
    explicit_bridge = found & (preference > 0)
    terrain_matches = found & (preference < 0) & (
        np.abs(deck_z - ground_z) <= DECK_AUTO_SNAP_TOLERANCE_M)
    use_deck = explicit_bridge | terrain_matches
    return np.where(use_deck, deck_z, ground_z), ground_z, wide_z


def _surface_z(frame, points, elev, decks, deck_preference=None):
    """Heights of XY points (see _surface_info)."""
    return _surface_info(frame, points, elev, decks, deck_preference)[0]



# Stock Italy guardrail modules are 3 m long and overlap by 0.2 m at 2.8 m spacing.
# They are Forest items: the engine batches repeated shapes and caches their collision mesh.
MODULE_LENGTH_M = 3.0
MODULE_STEP_M = 2.8
MODULE_HEIGHT_M = 0.9
# The standard and reverse-facing models sit beside one another, rather than on the same line.
# This is the distance between their centre lines (0.25 m to either side of the mapped way).
TWO_SIDED_SPACING_M = 0.5


def _is_two_sided(feats, i):
    """OSM's two_sided=yes means the guardrail has a traffic-facing rail on both sides."""
    tags = feats.attrs.get("tags")
    if tags is None:
        return False
    try:
        values = json.loads(str(tags[i]))
    except (TypeError, ValueError):
        return False
    if not isinstance(values, dict):
        return False
    return str(values.get("two_sided", "")).strip().lower() in ("yes", "true", "1")


# Fallback duplicate suppression when a bridge profile is incomplete.
# The deck footprint test normally removes these models first; if no usable deck
# profile exists, tight inner-wall matching avoids deleting a central divider between decks.
PARAPET_DUPLICATE_M = 0.7
OUTER_PARAPET_DUPLICATE_M = 2.0
PARAPET_LEVEL_TOLERANCE_M = 1.0
PARAPET_PARALLEL_DOT = 0.75
PARAPET_MATCH_SHARE = 0.75
PARAPET_MATCH_SAMPLES = 7
OUTER_DECK_SEARCH_M = 30.0


def _deck_is_outward_of_parapet(mid, tangent, outward, z, decks):
    """True when another same-level, parallel bridge deck lies outside this concrete parapet."""
    for deck in decks or ():
        line = np.asarray(deck.get("n", ()), np.float64)
        if len(line) < 2 or line.shape[1] < 3:
            continue
        a, b = line[:-1, :2], line[1:, :2]
        v = b - a
        l2 = np.maximum(np.sum(v * v, axis=1), 1e-12)
        t = np.clip(np.sum((mid - a) * v, axis=1) / l2, 0.0, 1.0)
        near = a + t[:, None] * v
        delta = near - mid
        signed = delta @ outward
        dist = np.hypot(delta[:, 0], delta[:, 1])
        length = np.maximum(np.linalg.norm(v, axis=1), 1e-9)
        parallel = np.abs((v / length[:, None]) @ tangent) >= PARAPET_PARALLEL_DOT
        deck_z = line[:-1, 2] + t * (line[1:, 2] - line[:-1, 2])
        level_ok = np.abs(deck_z - z) <= PARAPET_LEVEL_TOLERANCE_M
        if np.any(parallel & level_ok & (signed >= 1.0) &
                  (signed <= OUTER_DECK_SEARCH_M) & (dist <= OUTER_DECK_SEARCH_M)):
            return True
    return False


def _parapet_segments(parapets, decks=()):
    """Flatten generated bridge-parapet centre lines and identify inner versus outer walls.

    The parapet's outward normal points away from its own deck. If another parallel, same-level
    bridge deck lies in that direction, this is an inner parapet facing the central divider; only
    the tighter duplicate tolerance may match it. Outer parapets may replace mapped roadside rails
    that were mapped a little farther from the wall than its centre line.
    """
    starts, ends, z_starts, z_ends, outer_segments = [], [], [], [], []
    for parapet in parapets or ():
        line = np.asarray(parapet.get("line", ()) if isinstance(parapet, dict) else parapet,
                          np.float64).reshape(-1, 2)
        if len(line) < 2:
            continue
        z = np.asarray(parapet.get("z", ()) if isinstance(parapet, dict) else (), np.float64).reshape(-1)
        if len(z) != len(line):
            z = np.full(len(line), np.nan, np.float64)
        out = np.asarray(parapet.get("out", ()) if isinstance(parapet, dict) else (),
                         np.float64).reshape(-1, 2)
        if len(out) != len(line):
            out = np.zeros_like(line)
        a, b = line[:-1], line[1:]
        d = b - a
        length = np.hypot(d[:, 0], d[:, 1])
        keep = length > 1e-6
        if not keep.any():
            continue
        unit = d[keep] / length[keep, None]
        mids = (a[keep] + b[keep]) * 0.5
        normals = out[:-1][keep] + out[1:][keep]
        nl = np.linalg.norm(normals, axis=1)
        normals[nl > 1e-9] /= nl[nl > 1e-9, None]
        is_outer = []
        segment_z = (z[:-1][keep] + z[1:][keep]) * 0.5
        for mid, tangent, outward, level in zip(mids, unit, normals, segment_z):
            if np.linalg.norm(outward) < 1e-9 or not np.isfinite(level):
                is_outer.append(False)
                continue
            inner = _deck_is_outward_of_parapet(mid, tangent, outward, float(level), decks)
            is_outer.append(not inner)
        starts.append(a[keep])
        ends.append(b[keep])
        z_starts.append(z[:-1][keep])
        z_ends.append(z[1:][keep])
        outer_segments.append(np.asarray(is_outer, bool))
    if not starts:
        return None
    a, b = np.concatenate(starts), np.concatenate(ends)
    d = b - a
    length = np.hypot(d[:, 0], d[:, 1])
    return {
        "a": a,
        "b": b,
        "unit": d / length[:, None],
        "lo": np.minimum(a, b),
        "hi": np.maximum(a, b),
        "za": np.concatenate(z_starts),
        "zb": np.concatenate(z_ends),
        "outer": np.concatenate(outer_segments),
    }


def _overlaps_bridge_parapet(a, b, segments, level=None, distance_limit=PARAPET_DUPLICATE_M,
                             outer_only=False):
    """Whether a module runs along a generated concrete parapet, not merely across it."""
    if segments is None:
        return False
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    d = b - a
    length = float(np.hypot(d[0], d[1]))
    if length < 1e-6:
        return False
    tangent = d / length
    lo = np.minimum(a, b) - distance_limit
    hi = np.maximum(a, b) + distance_limit
    box = ((segments["hi"][:, 0] >= lo[0]) & (segments["lo"][:, 0] <= hi[0]) &
           (segments["hi"][:, 1] >= lo[1]) & (segments["lo"][:, 1] <= hi[1]))
    if outer_only:
        box &= segments["outer"]
    if not box.any():
        return False
    pa, pb, unit = segments["a"][box], segments["b"][box], segments["unit"][box]
    v = pb - pa
    v2 = np.maximum(np.sum(v * v, axis=1), 1e-12)
    samples = a[None, :] + np.linspace(0.1, 0.9, PARAPET_MATCH_SAMPLES)[:, None] * d[None, :]
    delta = samples[:, None, :] - pa[None, :, :]
    t = np.clip(np.sum(delta * v[None, :, :], axis=2) / v2[None, :], 0.0, 1.0)
    nearest = pa[None, :, :] + t[:, :, None] * v[None, :, :]
    dist = np.hypot(samples[:, None, 0] - nearest[:, :, 0],
                    samples[:, None, 1] - nearest[:, :, 1])
    parallel = np.abs(unit @ tangent) >= PARAPET_PARALLEL_DOT
    matches = (dist <= distance_limit) & parallel[None, :]
    if level is not None:
        parapet_z = segments["za"][box][None, :] + t * (segments["zb"][box] - segments["za"][box])[None, :]
        matches &= np.isfinite(parapet_z) & (np.abs(parapet_z - float(level)) <= PARAPET_LEVEL_TOLERANCE_M)
    return float(matches.any(axis=1).mean()) >= PARAPET_MATCH_SHARE


def _overlaps_bridge_approach(a, b, run):
    """Whether a mapped rail continues along a deck-edge line just beyond a real run end.

    Approach rails are evidence for the adjoining bridge edge too; they are not finite-overlap
    matches because their modules begin outside the parapet run. Direction and lateral distance
    checks avoid treating a crossing road's rail as a bridge approach.
    """
    line = np.asarray(run.get("line", ()), np.float64).reshape(-1, 2)
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    d = b - a
    length = float(np.hypot(d[0], d[1]))
    if len(line) < 2 or length < 1e-6:
        return False
    tangent = d / length
    samples = a[None, :] + np.linspace(0.1, 0.9, PARAPET_MATCH_SAMPLES)[:, None] * d[None, :]
    for at, inside in ((0, 1), (-1, -2)):
        edge = line[at]
        outward = edge - line[inside]
        edge_length = float(np.hypot(outward[0], outward[1]))
        if edge_length < 1e-6:
            continue
        outward /= edge_length
        if abs(float(np.dot(tangent, outward))) < PARAPET_PARALLEL_DOT:
            continue
        delta = samples - edge
        along = delta @ outward
        lateral = np.abs(delta[:, 0] * outward[1] - delta[:, 1] * outward[0])
        near = ((along >= -SPAN_TOL_M) & (along <= JOIN_LENGTH_M) &
                (lateral <= JOIN_LATERAL_M))
        if float(near.mean()) >= PARAPET_MATCH_SHARE:
            return True
    return False


# --- Rails around a bridge -------------------------------------------------------------------------------
# A mapped rail near a deck is one of: a centre rail (on the deck, inside its edges: kept, at deck height), an
# edge rail (duplicate of the rail the bridge builds on its own edge: dropped), a median between two decks (kept,
# at deck height), a rail beside the deck (kept unless it hangs far under the deck: that is the bridge's own
# rail, mapped a little outside), or an approach rail beyond a deck end (kept and joined to the bridge's rail).
BRIDGE_EDGE_OFFSET_M = 0.225    # m: the bridge rail's line outside the road edge (roadmesh.PARAPET_W / 2)
INTERIOR_MARGIN_M = 1.0         # m: a rail this far inside the road edge, or deeper, is a centre rail
ALONGSIDE_REACH_M = 4.0         # m beyond BRIDGE_SIDE_CLEARANCE_M: rails here are checked against the ground
HANGING_DROP_M = 1.0            # m: ground this far under the deck there: not a ground rail
MEDIAN_REACH_M = 12.0           # m: another deck this near, on the other side of the rail: a median
SPAN_TOL_M = 0.5                # m past a deck end along the road still counted as beside the deck
JOIN_LENGTH_M = 24.0            # m an approach rail takes to leave the bridge rail's line / height
JOIN_LATERAL_M = 2.5            # m: only rails this near the bridge rail's line are drawn onto it
JOIN_MAX_GRADE = 0.08           # the deck's grade carried on past its end (while the ground takes over)
JOIN_AXIS_TOL_M = 2.0           # m: the road's height beyond the end (ground on the deck's axis) is trusted this near the deck's own
END_JOIN_SKIP_M = 2.5           # a deck end this near another deck's end is a joint, not an end


def _deck_segments(decks):
    """Deck centre-line segments of all decks with the deck index, half width, unit, length."""
    cols = {"a": [], "unit": [], "len": [], "half": [], "idx": []}
    for k, deck in enumerate(decks or ()):
        line = np.asarray(deck.get("n", ()), np.float64)
        if line.ndim != 2 or len(line) < 2 or line.shape[1] < 2:
            continue
        a, b = line[:-1, :2], line[1:, :2]
        d = b - a
        length = np.hypot(d[:, 0], d[:, 1])
        keep = length > 1e-6
        if not keep.any():
            continue
        width = float(deck.get("w", 0.0))
        if not np.isfinite(width):
            width = 0.0
        n = int(keep.sum())
        cols["a"].append(a[keep])
        cols["unit"].append(d[keep] / length[keep, None])
        cols["len"].append(length[keep])
        cols["half"].append(np.full(n, max(0.0, width) / 2.0, np.float64))
        cols["idx"].append(np.full(n, k, np.int64))
    if not cols["a"]:
        return None
    return {key: np.concatenate(val) for key, val in cols.items()}


def _relate(mid, segs, direction=None):
    """Relate a rail module to a nearby deck only when it runs along that deck.

    A point-only test misclassifies a guardrail on a lower road crossing under a bridge as a
    centre/median rail on the bridge. Direction alignment prevents perpendicular roads from
    borrowing the bridge's height or being shifted onto its edge.
    """
    v = mid - segs["a"]
    along = np.sum(v * segs["unit"], axis=1)
    lateral = v[:, 0] * segs["unit"][:, 1] - v[:, 1] * segs["unit"][:, 0]
    span = (along >= -SPAN_TOL_M) & (along <= segs["len"] + SPAN_TOL_M)
    past = np.abs(lateral) - segs["half"]
    aligned = np.ones(len(segs["unit"]), bool)
    if direction is not None:
        tangent = np.asarray(direction, np.float64)
        tangent_len = float(np.hypot(tangent[0], tangent[1]))
        if tangent_len < 1e-6:
            return None
        aligned = np.abs(segs["unit"] @ (tangent / tangent_len)) >= PARAPET_PARALLEL_DOT
    near = span & aligned & (past <= BRIDGE_SIDE_CLEARANCE_M + ALONGSIDE_REACH_M)
    if not near.any():
        return None
    cand = np.flatnonzero(near)
    k = int(cand[np.argmin(past[cand])])
    deck = int(segs["idx"][k])
    rel = {"past": float(past[k]), "lat": float(abs(lateral[k])), "half": float(segs["half"][k]),
           "deck": deck, "median": False}
    other = (segs["idx"] != deck) & aligned
    if other.any():
        a2, u2, l2 = segs["a"][other], segs["unit"][other], segs["len"][other]
        t2 = np.clip(np.sum((mid - a2) * u2, axis=1), 0.0, l2)
        n2 = a2 + t2[:, None] * u2
        d2 = np.hypot(*(mid - n2).T) - segs["half"][other]
        j = int(np.argmin(d2))
        if d2[j] <= MEDIAN_REACH_M:
            t1 = float(np.clip(along[k], 0.0, segs["len"][k]))
            n1 = segs["a"][k] + t1 * segs["unit"][k]
            rel["median"] = float(np.dot(mid - n1, mid - n2[j])) < 0.0
    return rel


def _deck_ends(decks):
    """Real ends of the decks: XY point, outward unit tangent and normal, height, grade outward, half width.
    A deck end that meets another deck's end (a chain's joint) is no end."""
    ends, all_points = [], []
    for k, deck in enumerate(decks or ()):
        line = np.asarray(deck.get("n", ()), np.float64)
        if line.ndim != 2 or len(line) < 2 or line.shape[1] < 3:
            continue
        all_points.append((k, line[0, :2]))
        all_points.append((k, line[-1, :2]))
    for k, deck in enumerate(decks or ()):
        line = np.asarray(deck.get("n", ()), np.float64)
        if line.ndim != 2 or len(line) < 2 or line.shape[1] < 3:
            continue
        arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line[:, :2], axis=0).T))]
        if arc[-1] < 3.0:
            continue
        width = float(deck.get("w", 0.0))
        half = max(0.0, width if np.isfinite(width) else 0.0) / 2.0
        for at_start in (True, False):
            pts = line if at_start else line[::-1]
            dist = arc if at_start else arc[-1] - arc[::-1]
            e = pts[0]
            if any(j != k and float(np.hypot(*(e[:2] - q))) < END_JOIN_SKIP_M for j, q in all_points):
                continue
            i = min(int(np.searchsorted(dist, 3.0)), len(pts) - 1)
            inward = pts[i, :2] - e[:2]
            length = float(np.hypot(*inward))
            if length < 1e-6:
                continue
            t = -inward / length
            grade = float(np.clip((e[2] - pts[i, 2]) / length, -JOIN_MAX_GRADE, JOIN_MAX_GRADE))
            ends.append({"e": e[:2].copy(), "t": t, "n": np.array([-t[1], t[0]]),
                         "z": float(e[2]), "g": grade, "half": half})
    if not ends:
        return None
    return {key: np.array([x[key] for x in ends], np.float64) for key in ("e", "t", "n", "z", "g", "half")}


def _smooth_weight(s):
    t = np.clip(np.asarray(s, np.float64) / JOIN_LENGTH_M, 0.0, 1.0)
    return 1.0 - t * t * (3.0 - 2.0 * t)


def _join_module(sa, sb, ends, direction=None):
    """Join a rail module that lies beyond a deck end to the bridge: returns None, or
    [(shift, w, ze, axis_xy)] for a / b / the middle: XY shift, the pull toward the road's height (1 at the end,
    0 JOIN_LENGTH_M out), the deck's height carried on past the end, and the point on the deck's axis at the same
    distance (the road's graded ground there tells the road's height; the rail's own ground is the verge's).

    The module's middle picks the end (its distance beyond the end, 0 up to JOIN_LENGTH_M). A rail near the
    bridge rail's line is drawn onto it at the end and leaves it smoothly over JOIN_LENGTH_M; its height is
    the road's at the end (a rail on a fill stands at the road's level, not on the slope under it) and the
    ground's further out. A centre rail keeps its place and only takes the height.
    """
    if ends is None:
        return None
    mid = (sa + sb) * 0.5
    d = mid[None, :] - ends["e"]
    s = np.sum(d * ends["t"], axis=1)
    lat = np.sum(d * ends["n"], axis=1)
    zone = (s >= 0.0) & (s <= JOIN_LENGTH_M) & (np.abs(lat) <= ends["half"] + BRIDGE_SIDE_CLEARANCE_M + JOIN_LATERAL_M)
    if direction is not None:
        tangent = np.asarray(direction, np.float64)
        tangent_len = float(np.hypot(tangent[0], tangent[1]))
        if tangent_len < 1e-6:
            return None
        aligned = np.abs(ends["t"] @ (tangent / tangent_len)) >= PARAPET_PARALLEL_DOT
        zone &= aligned
    if not zone.any():
        return None
    k = int(np.flatnonzero(zone)[np.argmin(s[zone])])
    half = float(ends["half"][k])
    sign = 1.0 if lat[k] >= 0.0 else -1.0
    target = sign * (half + BRIDGE_EDGE_OFFSET_M)
    edge_type = abs(lat[k]) >= half - INTERIOR_MARGIN_M and abs(lat[k] - target) <= JOIN_LATERAL_M
    out = []
    for q in (sa, sb, mid):
        dq = q - ends["e"][k]
        sq = float(np.dot(dq, ends["t"][k]))
        lq = float(np.dot(dq, ends["n"][k]))
        w = float(_smooth_weight(max(sq, 0.0)))
        shift = ends["n"][k] * ((target - lq) * w) if edge_type else np.zeros(2, np.float64)
        ze = float(ends["z"][k] + ends["g"][k] * max(sq, 0.0))
        axis = ends["e"][k] + ends["t"][k] * max(sq, 0.0)
        out.append((shift, w, ze, axis))
    return out


def _drop_doubles(rows, reference, dist=0.9, dot=0.8, dz=1.5):
    """Rows of `rows` that do not lie on a row of `reference` (same place, same direction, same height)."""
    if not reference:
        return rows
    cell = 1.0
    grid = {}
    for r in reference:
        grid.setdefault((int(math.floor(r[1] / cell)), int(math.floor(r[2] / cell))), []).append(r)
    kept = []
    for r in rows:
        cx, cy = int(math.floor(r[1] / cell)), int(math.floor(r[2] / cell))
        double = False
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for q in grid.get((gx, gy), ()):
                    if (math.hypot(r[1] - q[1], r[2] - q[2]) <= dist and abs(r[3] - q[3]) <= dz and
                            abs(r[8] * q[8] + r[9] * q[9]) >= dot):
                        double = True
                        break
                if double:
                    break
            if double:
                break
        if not double:
            kept.append(r)
    return kept


def _self_doubles(rows, dist=0.9, dot=0.8, dz=1.5):
    """rows without the later of any two that lie on each other."""
    kept = []
    for r in rows:
        if not _drop_doubles([r], kept, dist, dot, dz):
            continue
        kept.append(r)
    return kept



def forest_rows(frame, infra_fn, exact, elev, decks=(), parapets=(), bridge_rails=False,
                supported_decks_out=None):
    """Return Forest-item rows for the stock Italy guardrail model.

    Row layout matches trees.lua:
      [group, x, y, z, yaw, unused, height, rank, forwardXYZ, backXYZ, upXYZ]
    The three basis vectors are stored consecutively and installed as MatrixF columns. This lets
    each 3 m module follow the surface's pitch rather than looking like stairs on a grade.
    Groups: rail, start/end caps, then their reverse-facing equivalents for two_sided=yes.

    Near bridges (decks: [{"n": (k, 3) XYZ, "w": road width}]): mapped edge rails are replaced by generated
    bridge rails only when mapped guardrails support that deck (on an edge or approach). Centre rails and medians
    aligned with the bridge deck stay at deck height. Explicit lower-level/tunnel tags keep underpass rails at
    ground level, and perpendicular crossings are not treated as bridge rails. Rails hanging far under a deck
    are dropped, and aligned approach rails join the bridge over JOIN_LENGTH_M.
    When supplied, supported_decks_out receives the indices of decks supported by mapped rails.
    """
    if supported_decks_out is not None:
        supported_decks_out.clear()
    # Bridge rails must be supported by mapped guardrail evidence on the corresponding deck edge;
    # never infer that every bridge needs railings just because a bridge mesh exists.
    rails = []
    if infra_fn is None:
        return []
    segs = _deck_segments(decks)
    ends = _deck_ends(decks)
    parapet_segments = _parapet_segments(parapets, decks)
    support_segments = [_parapet_segments([run], decks) for run in parapets] if bridge_rails else []
    support_hits = [set() for _ in support_segments]
    x0, y0, x1, y1 = map(float, exact)
    corners_x = np.array([x0, x0, x1, x1], np.float64)
    corners_y = np.array([y0, y1, y0, y1], np.float64)
    la, lo, _ = frame.enu_to_geodetic(corners_x, corners_y, np.zeros(4, np.float64))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return rails

    seen = set()
    placements = []
    for key in keys:
        try:
            feats = infra_fn(key)
        except Exception as ex:
            log.debug("guardrails: infrastructure cell %s unavailable: %s", key, ex)
            continue
        if feats is None or not len(feats):
            continue
        if feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        boxes = feats.boxes(frame)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen or int(feats.kind[i]) != mapgeom.LINE:
                continue
            kind = _barrier_kind(feats, i)
            if kind is None:
                continue
            module = KINDS[kind]
            rail = kind == "guard_rail"
            b = boxes[i]
            if (b[2] < x0 - BBOX_PAD_M or b[0] > x1 + BBOX_PAD_M or
                    b[3] < y0 - BBOX_PAD_M or b[1] > y1 + BBOX_PAD_M):
                continue
            seen.add(fid)
            two_sided = rail and _is_two_sided(feats, i)
            deck_hint = _deck_level_hint(feats, i)
            for part_no, part in enumerate(feats.parts(i)):
                if not part:
                    continue
                lonlat = np.asarray(part[0], np.float64)
                if len(lonlat) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, lonlat)
                line = np.column_stack((e, n))
                seglen = np.hypot(*np.diff(line, axis=0).T)
                line = line[np.r_[True, seglen > 1e-4]]
                if len(line) < 2:
                    continue
                arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
                total = float(arc[-1])
                if total < 1.0:
                    continue

                # Fit all modules inside the mapped line. Neighbouring 3 m shapes overlap by 0.2 m;
                # the unused margins are shared between the two ends, avoiding gaps at either cap.
                count = max(1, int(math.floor((total - MODULE_LENGTH_M) / MODULE_STEP_M)) + 1)
                covered = MODULE_LENGTH_M + (count - 1) * MODULE_STEP_M
                start = 0.5 * (total - covered)

                spans = []
                for k in range(count):
                    sa = start + k * MODULE_STEP_M
                    sb = sa + MODULE_LENGTH_M
                    a, _ = _at(line, arc, float(sa))
                    b, _ = _at(line, arc, float(sb))
                    if float(np.hypot(*(b - a))) < 0.05:
                        continue
                    p = 0.5 * (a + b)
                    rank_bytes = hashlib.blake2b(f"{fid}:{part_no}:{k}".encode("utf-8"), digest_size=4).digest()
                    rank = int.from_bytes(rank_bytes, "big") / 4294967296.0
                    spans.append((k, a, b, p, rank))

                if not spans:
                    continue
                _, first_a, first_b, _, first_rank = spans[0]
                _, last_a, last_b, _, last_rank = spans[-1]
                candidates = []
                for k, a, b, p, rank in spans:
                    # Record only mapped rails that actually run beside this concrete deck edge.
                    # A bridge is not given generated rails merely because another road has barriers nearby.
                    # Explicitly lower-level rails are never evidence for a bridge edge.
                    if bridge_rails and rail and deck_hint is not False:
                        for run_no, run_segments in enumerate(support_segments):
                            if run_segments is None:
                                continue
                            supports = _overlaps_bridge_parapet(a, b, run_segments)
                            if not supports:
                                supports = _overlaps_bridge_parapet(
                                    a, b, run_segments,
                                    distance_limit=OUTER_PARAPET_DUPLICATE_M, outer_only=True)
                            if not supports:
                                supports = _overlaps_bridge_approach(a, b, parapets[run_no])
                            if supports:
                                support_hits[run_no].add((fid, part_no, k))
                    if geotiles.box_owns(exact, p[0], p[1]):
                        candidates.append((module, p, a, b, rank, False))
                        if two_sided:
                            candidates.append((3, p, a, b, rank, True))
                # Caps extend outwards from the ends. Reverse-facing cap types are swapped so they
                # also flare outwards rather than into the run. (A rail's only: a jersey barrier or a
                # cable barrier ends with its last module.)
                if not rail:
                    pass
                elif geotiles.box_owns(exact, first_a[0], first_a[1]):
                    candidates.append((1, first_a, first_a, first_b, first_rank, False))
                    if two_sided:
                        candidates.append((4, first_a, first_a, first_b, first_rank, True))
                if rail and geotiles.box_owns(exact, last_b[0], last_b[1]):
                    candidates.append((2, last_b, last_a, last_b, last_rank, False))
                    if two_sided:
                        candidates.append((5, last_b, last_a, last_b, last_rank, True))

                for group, p, a, b, rank, reverse in candidates:
                    # Everything below works on the mapped line itself, before the offsets of two-sided
                    # models: a central separator near the inner parapets must not be shifted onto the match band.
                    source_a, source_b = np.asarray(a, np.float64), np.asarray(b, np.float64)
                    mid = 0.5 * (source_a + source_b)
                    mode, shifts = "ground", None
                    if deck_hint is not False:
                        direction = source_b - source_a
                        rel = _relate(mid, segs, direction) if segs is not None else None
                        if rel is not None:
                            edge_band = rel["lat"] > rel["half"] - INTERIOR_MARGIN_M
                            if rel["median"]:
                                mode = "median"                # between two aligned decks: stays at deck height
                            elif not edge_band:
                                mode = "deck"                  # a centre rail on the deck
                            elif rel["past"] <= BRIDGE_SIDE_CLEARANCE_M:
                                continue                       # on the deck's edge: the bridge's own rail is there
                            else:
                                mode = "beside"                # beside the deck: ground rail unless it hangs
                        else:
                            shifts = _join_module(source_a, source_b, ends, direction)
                            if shifts is not None:
                                mode = "join"
                    parapet_xy_match = _overlaps_bridge_parapet(source_a, source_b, parapet_segments)
                    if not parapet_xy_match:
                        parapet_xy_match = _overlaps_bridge_parapet(
                            source_a, source_b, parapet_segments,
                            distance_limit=OUTER_PARAPET_DUPLICATE_M, outer_only=True)
                    placements.append({"group": group, "a": source_a, "b": source_b, "rank": rank,
                                       "reverse": reverse, "two_sided": two_sided, "hint": deck_hint,
                                       "mode": mode, "join": shifts, "match": parapet_xy_match})

    # Build rails only for parapet runs supported by at least two mapped modules (about 5.8 m
    # of mapped guardrail). This filters out incidental crossings and bridges with no mapped rail data.
    supported_decks = set()
    if bridge_rails:
        # A mapped rail on either edge or its approach supports the whole deck: roads commonly
        # have two physical sides even when OSM only mapped one of them. Do not enable unrelated decks.
        supported_decks = {
            run.get("deck", i)
            for i, (run, hits) in enumerate(zip(parapets, support_hits))
            if len(hits) >= 2
        }
        supported = [run for i, run in enumerate(parapets)
                     if run.get("deck", i) in supported_decks]
        rails = _self_doubles(bridge_rows(supported, frame)) if supported else []

    if not placements:
        if supported_decks_out is not None:
            supported_decks_out.update(supported_decks)
        return rails

    # Join first (moves the module's ends), then the two-sided offset on the moved line.
    for pl in placements:
        a, b = pl["a"], pl["b"]
        if pl["join"] is not None:
            (sh_a, _, _, _), (sh_b, _, _, _), _ = pl["join"]
            a, b = a + sh_a, b + sh_b
        p = 0.5 * (a + b)
        if pl["two_sided"]:
            # The model's visible side is opposite its back axis. Keep the backs toward the centre
            # gap and the visible faces outward on both sides.
            tangent = b - a
            tangent_len = float(np.hypot(tangent[0], tangent[1]))
            if tangent_len >= 1e-6:
                normal = np.array([-tangent[1], tangent[0]], np.float64) / tangent_len
                side = 1.0 if pl["reverse"] else -1.0
                offset = normal * (0.5 * TWO_SIDED_SPACING_M * side)
                p, a, b = p + offset, a + offset, b + offset
        if pl["group"] in (1, 4):       # a cap sits on its end point
            p = a
        elif pl["group"] in (2, 5):
            p = b
        pl["p"], pl["ma"], pl["mb"] = p, a, b

    # One batched terrain/deck query for all module origins and their grade endpoints.
    sample_points, sample_deck_hints = [], []
    for pl in placements:
        sample_points.extend((pl["p"], pl["ma"], pl["mb"]))
        hint = True if pl["mode"] in ("deck", "median") else pl["hint"]
        sample_deck_hints.extend((hint, hint, hint))
    for pl in placements:       # joined rails also sample the ground on the deck's axis (the road's height)
        if pl["mode"] == "join":
            pl["axis_at"] = len(sample_points)
            for _, _, _, axis in pl["join"]:
                sample_points.append(axis)
                sample_deck_hints.append(None)
    heights, ground_z, deck_z = _surface_info(frame, np.asarray(sample_points, np.float64), elev, decks,
                                              deck_preference=sample_deck_hints)
    rows = []
    for i, pl in enumerate(placements):
        j = slice(3 * i, 3 * i + 3)
        z, za, zb = (float(heights[3 * i]), float(heights[3 * i + 1]), float(heights[3 * i + 2]))
        mode = pl["mode"]
        if mode == "beside":
            # Beside a deck: a rail mapped a little outside the bridge's edge hangs far under the deck.
            if pl["hint"] is True:
                continue
            gz, dz = ground_z[j], deck_z[j]
            if np.isfinite(dz).any() and float(np.nanmax(dz - gz)) > HANGING_DROP_M:
                continue
        elif mode in ("deck", "median"):
            dz = deck_z[j]
            # Direction and deck position already classified this as a centre/median rail.
            # Do not override that classification with terrain height: the terrain under a bridge
            # can be many metres below the actual rail. Explicit lower-level tags bypass this mode.
            z, za, zb = (float(dz[0]) if np.isfinite(dz[0]) else z,
                         float(dz[1]) if np.isfinite(dz[1]) else za,
                         float(dz[2]) if np.isfinite(dz[2]) else zb)
        elif mode == "join":
            gz = ground_z[j]
            at = pl["axis_at"]
            zs = []
            for n, (_, w, ze, _axis) in enumerate(pl["join"]):
                own = float(gz[(1, 2, 0)[n]])                    # a, b, middle: the order of the join tuple
                road = float(ground_z[at + n])
                if not np.isfinite(road) or abs(road - ze) > JOIN_AXIS_TOL_M:
                    road = ze                                    # the axis left the road (a bend): the deck's height
                zs.append(own + (road - own) * w)
            za, zb, z = zs
            if pl["group"] in (1, 4):      # a cap stands on its end point, not the module's middle
                z = za
            elif pl["group"] in (2, 5):
                z = zb
        if pl["match"] and mode == "ground" and (
                _overlaps_bridge_parapet(pl["a"], pl["b"], parapet_segments, level=z) or
                _overlaps_bridge_parapet(pl["a"], pl["b"], parapet_segments, level=z,
                                         distance_limit=OUTER_PARAPET_DUPLICATE_M, outer_only=True)):
            continue
        p, a, b = pl["p"], pl["ma"], pl["mb"]
        forward = np.array([float(b[0] - a[0]), float(b[1] - a[1]), zb - za], np.float64)
        length = float(np.linalg.norm(forward))
        if length < 1e-6:
            continue
        forward /= length
        up = np.array([0.0, 0.0, 1.0], np.float64) - forward[2] * forward
        up_len = float(np.linalg.norm(up))
        if up_len < 1e-6:
            up = np.array([0.0, 1.0, 0.0], np.float64)
        else:
            up /= up_len
        back = np.cross(up, forward)
        back_len = float(np.linalg.norm(back))
        if back_len < 1e-6:
            continue
        back /= back_len
        if pl["reverse"]:
            forward, back = -forward, -back
        yaw = math.degrees(math.atan2(float(forward[1]), float(forward[0])))
        c0, c1 = forward, back
        if pl["group"] in (G_JERSEY, G_JERSEY_END):     # (the jersey model runs along its y axis)
            c0, c1 = -back, forward
        rows.append([pl["group"], float(p[0]), float(p[1]), z, yaw, 0.0,
                     HEIGHTS.get(pl["group"], MODULE_HEIGHT_M), pl["rank"],
                     float(c0[0]), float(c0[1]), float(c0[2]),
                     float(c1[0]), float(c1[1]), float(c1[2]),
                     float(up[0]), float(up[1]), float(up[2])])
    # Whatever still lies on a rail the bridge built itself is a double.
    result = rails + _drop_doubles(rows, rails)
    if supported_decks_out is not None:
        supported_decks_out.update(supported_decks)
    return result


def bridge_rows(parapets, frame=None):
    """Forest rows (same layout as forest_rows) for the rails a bridge builds along its own deck edges.

    `parapets` are roadmesh.parapet_runs(): per free deck edge the strip centre line ("line"), the deck's
    heights ("z") and the unit normal pointing away from the deck ("out"). Heights come from the deck itself,
    so nothing is guessed from terrain or matched against mapped data (the cause of the doubled and missing
    rails before). The rail's visible face is opposite its back axis, i.e. on the right of its travel
    direction, so each run is walked with its outside on the left and the face looks onto the road.

    Modules are spread over the whole run (step at most MODULE_STEP_M) so they reach both ends of the run;
    runs are clipped at the tile box, so neighbouring tiles meet without a gap. No end caps: the rail goes
    on into the approach's mapped rail. frame: the tile's (its modules' ranks hashed from their lat / lon: the same
    module the same rank whatever frame or tile builds it); None: from x, y.
    """
    rows = []
    for run in parapets or ():
        if not isinstance(run, dict):
            continue
        line = np.asarray(run.get("line", ()), np.float64).reshape(-1, 2)
        z = np.asarray(run.get("z", ()), np.float64).reshape(-1)
        out = np.asarray(run.get("out", ()), np.float64).reshape(-1, 2)
        if len(line) < 2 or len(z) != len(line) or len(out) != len(line) or not np.isfinite(z).all():
            continue
        keep = np.r_[True, np.hypot(*np.diff(line, axis=0).T) > 1e-4]
        line, z, out = line[keep], z[keep], out[keep]
        if len(line) < 2:
            continue
        tang = np.diff(line, axis=0)
        left = np.column_stack((-tang[:, 1], tang[:, 0]))
        if float(np.sum(left * (out[:-1] + out[1:]) * 0.5)) < 0.0:
            line, z, out = line[::-1], z[::-1], out[::-1]
        arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
        total = float(arc[-1])
        if total < 1.0:
            continue
        if total <= MODULE_LENGTH_M:
            count, step = 1, 0.0
            length = total
        else:
            count = int(math.ceil((total - MODULE_LENGTH_M) / MODULE_STEP_M)) + 1
            step = (total - MODULE_LENGTH_M) / (count - 1) if count > 1 else 0.0
            length = MODULE_LENGTH_M
        for k in range(count):
            sa = k * step
            sb = sa + length
            a, _ = _at(line, arc, sa)
            b, _ = _at(line, arc, sb)
            if float(np.hypot(*(b - a))) < 0.05:
                continue
            za, zb = float(np.interp(sa, arc, z)), float(np.interp(sb, arc, z))
            p = 0.5 * (a + b)
            zp = float(np.interp(0.5 * (sa + sb), arc, z))
            if frame is not None:
                pla, plo, _ = frame.enu_to_geodetic(float(p[0]), float(p[1]), 0.0)
                key = f"bridge:{float(pla):.6f}:{float(plo):.6f}"
            else:
                key = f"bridge:{p[0]:.1f}:{p[1]:.1f}"
            rank = int.from_bytes(hashlib.blake2b(key.encode("utf-8"), digest_size=4).digest(), "big") / 4294967296.0
            forward = np.array([float(b[0] - a[0]), float(b[1] - a[1]), zb - za], np.float64)
            norm = float(np.linalg.norm(forward))
            if norm < 1e-6:
                continue
            forward /= norm
            up = np.array([0.0, 0.0, 1.0], np.float64) - forward[2] * forward
            up_len = float(np.linalg.norm(up))
            if up_len < 1e-6:
                continue
            up /= up_len
            back = np.cross(up, forward)
            back_len = float(np.linalg.norm(back))
            if back_len < 1e-6:
                continue
            back /= back_len
            yaw = math.degrees(math.atan2(float(forward[1]), float(forward[0])))
            rows.append([0, float(p[0]), float(p[1]), zp, yaw, 0.0, MODULE_HEIGHT_M, rank,
                         float(forward[0]), float(forward[1]), float(forward[2]),
                         float(back[0]), float(back[1]), float(back[2]),
                         float(up[0]), float(up[1]), float(up[2])])
    return rows
