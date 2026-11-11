"""Streets mapped as pedestrian ways across a railway: a vehicle level crossing node makes them roads (plan 035).

OpenStreetMap tags a level crossing for vehicles railway=level_crossing and one for people railway=crossing. Where a
level_crossing node lies on a way of a walking class (pedestrian, footway, path, living street's pedestrian piece ...) and
a drivable road ends at or near that way, the way is a street the cars use across the tracks: Nachod's Kladska runs as a
residential road up to the tracks and on past them as a "pedestrian" way, while Street View shows a two-lane asphalt street
with gates and lights. Such a way takes the class of the drivable road that leads into it, so the road mesh, the grading,
the level crossing's deck and its gates all follow as for any street. Pure per cell (its own features and rail nodes); the
roads layer is changed in place as it is loaded (mapdata.py), before anything reads it. BNE_LEVEL_WAYS=off leaves the map
as it is."""
import os

import numpy as np

VERSION = 1                 # (mapdata.versions(): the derived caches are built again when it changes)
ON = os.environ.get("BNE_LEVEL_WAYS", "on") != "off"
WALK_CLASSES = {"pedestrian", "footway", "path", "cycleway", "steps", "bridleway"}
DRIVE_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
                 "living_street", "service"}
ON_WAY_M = 2.0              # m: a node this near a way's line is on it
JOIN_M = 15.0               # m: a drivable road's end this near the way (or the node) leads into it
NODE_REACH_M = 40.0         # m: a drivable road ending this near the crossing node leads into it
MAX_WAY_M = 250.0           # m: a longer walking way is a promenade with a crossing in it, not a street
LINK_M = 40.0               # m: a short walking piece between the promoted way and a road (its two ends on them) goes too
END_M = 2.0                 # m: an end this near a line is on it


def _seg_dist(p, line):
    """Distance from p (2,) to the polyline line (k, 2)."""
    a, b = line[:-1], line[1:]
    ab = b - a
    t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    return float(np.min(np.hypot(*(a + t[:, None] * ab - p).T)))


def _box(lines, pad):
    """The (x0, y0, x1, y1) box round lines [(k, 2)], pad metres out."""
    xy = np.vstack(lines)
    return (float(xy[:, 0].min()) - pad, float(xy[:, 1].min()) - pad, float(xy[:, 0].max()) + pad,
            float(xy[:, 1].max()) + pad)


def _inside(pts, box):
    """(n,) bool: points pts (n, 2) in box."""
    return (pts[:, 0] >= box[0]) & (pts[:, 0] <= box[2]) & (pts[:, 1] >= box[1]) & (pts[:, 1] <= box[3])


def promote(feats, frame):
    """feats (the roads layer, localized in frame) with each walking-class way that carries a vehicle level crossing node
    and has a drivable road leading into it given that road's class, in place; returns it."""
    if not ON or feats is None or not len(feats) or "class" not in feats.attrs or feats.en is None:
        return feats
    nodes = [(lo, la) for lo, la, t in feats.__dict__.get("_rail_nodes", ()) if t.get("railway") == "level_crossing"]
    if not nodes:
        return feats
    e, n, _ = frame.geodetic_to_enu(np.array([q[1] for q in nodes]), np.array([q[0] for q in nodes]), 0.0)
    pts = np.c_[np.asarray(e, np.float64), np.asarray(n, np.float64)]
    sub = feats.attrs.get("subtype")
    cls = np.asarray(feats.attrs["class"]).astype(object)
    orig = [str(c) for c in cls]
    po, ro, co = feats.part_off, feats.ring_off, feats.coord_off

    def lines(i):
        out = []
        for r in range(int(ro[po[i]]), int(ro[po[i + 1]])):
            xy = np.asarray(feats.en[co[r]:co[r + 1]], np.float64)
            if len(xy) >= 2:
                out.append(xy)
        return out
    roads = [i for i in range(len(feats)) if (sub is None or str(sub[i]) == "road")]
    drive_ends = [(str(cls[i]), ln[k]) for i in roads if str(cls[i]) in DRIVE_CLASSES for ln in lines(i) for k in (0, -1)]
    if not drive_ends:
        return feats
    # (only the nodes and road ends that can pass the tests below are tried: near a way's box. Every walking way against
    # every crossing node and every road end was 63 s of a Baltimore cell's 120 s, 2.8 M _seg_dist calls)
    end_xy = np.array([end for _c, end in drive_ends], np.float64)
    reach = max(JOIN_M, NODE_REACH_M + ON_WAY_M) + 1.0
    changed, promoted = False, []
    for i in roads:
        if str(cls[i]) not in WALK_CLASSES:
            continue
        ls = lines(i)
        if not ls:
            continue
        near = np.flatnonzero(_inside(pts, _box(ls, ON_WAY_M + 1.0)))
        on = [pts[j] for j in near if any(_seg_dist(pts[j], ln) < ON_WAY_M for ln in ls)]
        if not on:
            continue
        best = None                     # (of the roads leading into it, the one ending nearest the crossing: the street
        for j in np.flatnonzero(_inside(end_xy, _box(ls, reach))):     # that goes on over the tracks, not the main
            c, end = drive_ends[j]                                      # road at its far end)
            near_way = min(_seg_dist(end, ln) for ln in ls)
            to_node = min(float(np.hypot(*(end - p))) for p in on)
            key = (c == "service", to_node)     # (a street over a service road's end beside it: Kladska's yard way)
            if (near_way < JOIN_M or to_node < NODE_REACH_M) and (best is None or key < best[0]):
                best = (key, c)
        if best is not None and sum(float(np.hypot(*np.diff(ln, axis=0).T).sum()) for ln in ls) <= MAX_WAY_M:
            cls[i] = best[1]
            promoted.append(i)
            changed = True
    if changed:                 # (the short walking pieces linking a promoted way to a road: Kladska's 14 m to its end)
        drive_lines = [(str(cls[i]), ln) for i in promoted for ln in lines(i)] +             [(str(cls[i]), ln) for i in roads if str(cls[i]) in DRIVE_CLASSES and i not in promoted for ln in lines(i)]
        boxes = np.array([_box([ln], END_M + 1.0) for _c, ln in drive_lines], np.float64).reshape(-1, 4)
        for i in roads:
            if str(cls[i]) not in WALK_CLASSES:
                continue
            ls = lines(i)
            if len(ls) != 1 or float(np.hypot(*np.diff(ls[0], axis=0).T).sum()) > LINK_M:
                continue
            ends = [ls[0][0], ls[0][-1]]
            got = [[drive_lines[k][0] for k in np.flatnonzero((boxes[:, 0] <= p[0]) & (boxes[:, 2] >= p[0])
                                                              & (boxes[:, 1] <= p[1]) & (boxes[:, 3] >= p[1]))
                    if _seg_dist(p, drive_lines[k][1]) < END_M] for p in ends]
            if got[0] and got[1]:           # (the promoted way's class first: drive_lines lists them first)
                pc = {str(cls[k]) for k in promoted}
                cls[i] = next((c for c in got[0] + got[1] if c in pc), got[0][0])
    if changed:
        feats.attrs["class"] = np.array([str(c) for c in cls])     # (a fresh array: wide enough for the new names)
        gone = {i for i in range(len(feats)) if str(cls[i]) != orig[i]}
        wt = feats.rules.get("width") if getattr(feats, "rules", None) else None
        if wt is not None and len(wt):          # (a plaza's mapped width is not its street's: the class's own instead)
            import mapgeom
            keep = ~np.isin(np.asarray(wt.feature), np.fromiter(gone, np.int64, len(gone)))
            feats.rules["width"] = mapgeom.RuleTable(np.asarray(wt.feature)[keep], np.asarray(wt.value)[keep],
                                                     np.asarray(wt.start)[keep], np.asarray(wt.end)[keep])
    return feats
