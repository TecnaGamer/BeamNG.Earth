"""Tram signals (plan 046 stage H, BNE_TRAM_SIGNALS; on unless set to "off"): thin glue to the kernel's tram::signals.

Where a ground tram track crosses a carriageway at a junction with OSM highway=traffic_signals within 30 m (or passes an
OSM railway=signal node), a signal stands before the stop line facing the approaching tram, on its own post beside the
track, at the kerb side of the traffic. The mod's tramsignals.lua makes them (TSStatic shapes with switchable lamps, the
rows of the tile's "tsig") and runs their phases: a cycle hashed from the junction's signal node (so every tile and client
agrees), or the car signals' own sequence where the navgraph has one at the junction. Rows: [x, y, z, yaw, style, cycle,
offset, green, branch] (the frame's metres, yaw degrees counter-clockwise of the model's +x axis, the right of the tram's travel;
style 0 the bars of white lamps, 1 the BOStrab head; seconds; branch 1 / -1 / 0: a track leaves to the left / right / not at all
ahead of the stop line: a switch, the head then has the diagonal bar of that side). Kept as a thin layer: the rules are tram/signals.rs."""
import os

import numpy as np

ENABLED = os.environ.get("BNE_TRAM_SIGNALS", "on") != "off"
REACH_M = 130.0             # m round the tile the tram tracks and roads are read (tramcat's, so the junction sees the same lines)
KERNEL = 310                # the kernel signature this calls (fast.require)
RAIL_SIGNALS = np.zeros((0, 2), np.float64)     # (no railway=signal nodes: the rail tag shards have them only once rebuilt with
                                                # tools/build_rail_tags.py's NODE_RAILWAY "signal", docs/new/046 SIGNALS part 2)


def _flat(lines):
    """(points (n, 2), offsets (k + 1,)) of a list of polylines."""
    xy = [np.asarray(ln, np.float64)[:, :2] for ln in lines]
    off = np.r_[0, np.cumsum([len(a) for a in xy])].astype(np.int64)
    return np.ascontiguousarray(np.concatenate(xy) if xy else np.zeros((0, 2)), np.float64), off


def candidates(parts, roads, signals, country=None, left_hand=False, rail=None):
    """The kernel's signals (s, 10) for ground tram tracks parts ([xy (k, 2)]), carriageways roads ([(centreline, half
    width)]) and the mapped traffic signal points signals (n, 2) (None or empty: none): x, y, yaw, style, cycle, offset,
    green, key x, key y, rule, branch."""
    import fast
    fast.require(KERNEL, "tram signals")
    pts, off = _flat(parts)
    rpts, roff = _flat([ln for ln, _hw in roads])
    hw = np.array([float(h) for _ln, h in roads], np.float64)
    sig = np.zeros((0, 2), np.float64) if signals is None else np.ascontiguousarray(signals, np.float64).reshape(-1, 2)
    rl = RAIL_SIGNALS if rail is None else np.ascontiguousarray(rail, np.float64).reshape(-1, 2)
    return fast._ef.tram_signals(pts, off, rpts, roff, hw, sig, rl, bool(left_hand), str(country or ""))


def rail_signal_points(frame, cells):
    """(n, 2) frame xy of the OSM railway=signal nodes of the cells (railtags' _rail_nodes), none when the shards carry none."""
    pts = [(lo, la) for f in cells if f is not None for lo, la, t in f.__dict__.get("_rail_nodes", ())
           if t.get("railway") == "signal"]
    if not pts:
        return RAIL_SIGNALS
    a = np.array(sorted(set(pts)), np.float64)
    e, n, _ = frame.geodetic_to_enu(a[:, 1], a[:, 0], np.zeros(len(a)))
    return np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)])


def tile_rows(frame, box, cells, ground, signals, country=None, left_hand=False):
    """The "tsig" rows of the tile box (roads.tile_bbox's or a geotile's): the signals whose post the tile owns, with the
    ground's height under each. cells: the road/rail layers round it, ground(xy (n, 2)) -> z (n,), signals: the mapped
    traffic signals (furniture.signals_for's points, None where the map has no layer)."""
    if not ENABLED:
        return []
    import catenary as cat
    import roads as roadsmod
    import tramcat
    parts = [xy for _fid, xy, kind in tramcat.tram_parts(frame, cells, box, REACH_M) if kind == "ground"]
    if not parts:
        return []
    if callable(signals):
        signals = signals()
    roads = cat._road_lines(frame, cells, box, REACH_M)
    got = candidates(parts, roads, signals, country, left_hand, rail_signal_points(frame, cells))
    if not len(got):
        return []
    got = got[np.asarray(roadsmod.box_owns(box, got[:, 0], got[:, 1]), bool)]
    if not len(got):
        return []
    z = np.asarray(ground(got[:, :2]), np.float64).ravel()
    return [[round(float(r[0]), 2), round(float(r[1]), 2), round(float(h), 2), round(float(r[2]), 1), int(r[3]),
             int(r[4]), int(r[5]), int(r[6]), int(r[10])] for r, h in zip(got, z)]
