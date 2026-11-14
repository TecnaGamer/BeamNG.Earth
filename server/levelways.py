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


def promote(feats, frame):
    """feats (the roads layer, localized in frame) with each walking-class way that carries a vehicle level crossing node
    and has a drivable road leading into it given that road's class, in place; returns it. The tests are native
    (earthfast level_ways_promote, levelways.rs): the way's nodes within ON_WAY_M of its line, of the drivable roads
    ending within JOIN_M of it or NODE_REACH_M of such a node the one ending nearest the node (a service road's last),
    a way up to MAX_WAY_M long; then a walking piece up to LINK_M long with both ends within END_M of the promoted
    ways or other drivable roads takes their class (a promoted way's first)."""
    if not ON or feats is None or not len(feats) or "class" not in feats.attrs or feats.en is None:
        return feats
    nodes = [(lo, la) for lo, la, t in feats.__dict__.get("_rail_nodes", ()) if t.get("railway") == "level_crossing"]
    if not nodes:
        return feats
    import fast
    fast.require(235, "level_ways_promote")
    e, n, _ = frame.geodetic_to_enu(np.array([q[1] for q in nodes]), np.array([q[0] for q in nodes]), 0.0)
    pts = np.c_[np.asarray(e, np.float64), np.asarray(n, np.float64)]
    sub = feats.attrs.get("subtype")
    orig = np.array([str(c) for c in feats.attrs["class"]], dtype=object)
    names, codes = np.unique(orig.astype(str), return_inverse=True)
    names = [str(c) for c in names]
    road = np.ones(len(feats), bool) if sub is None else np.array([str(v) == "road" for v in sub], bool)
    got = fast._ef.level_ways_promote(
        np.ascontiguousarray(codes.reshape(-1), np.int64), road, np.array([c in WALK_CLASSES for c in names], bool),
        np.array([c in DRIVE_CLASSES for c in names], bool), np.array([c == "service" for c in names], bool),
        np.ascontiguousarray(feats.part_off, np.int64), np.ascontiguousarray(feats.ring_off, np.int64),
        np.ascontiguousarray(feats.coord_off, np.int64), np.ascontiguousarray(feats.en, np.float64).reshape(-1, 2),
        np.ascontiguousarray(pts, np.float64).reshape(-1, 2),
        np.array([ON_WAY_M, JOIN_M, NODE_REACH_M, MAX_WAY_M, LINK_M, END_M], np.float64))
    if got is None:
        return feats
    cls = [names[k] for k in got.tolist()]
    feats.attrs["class"] = np.array(cls)        # (a fresh array: wide enough for the new names)
    gone = np.flatnonzero(np.array(cls, dtype=object) != orig)
    wt = feats.rules.get("width") if getattr(feats, "rules", None) else None
    if wt is not None and len(wt):              # (a plaza's mapped width is not its street's: the class's own instead)
        import mapgeom
        keep = ~np.isin(np.asarray(wt.feature), gone.astype(np.int64))
        feats.rules["width"] = mapgeom.RuleTable(np.asarray(wt.feature)[keep], np.asarray(wt.value)[keep],
                                                 np.asarray(wt.start)[keep], np.asarray(wt.end)[keep])
    return feats
