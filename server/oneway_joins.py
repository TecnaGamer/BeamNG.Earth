"""One-way roads meet a two-way road at their own lane, not its centre line (the user, 2026-10-25: one-way pairs ended in
the middle of the two-way road they join, and the reverse).

Overture (and OSM) join a one-way to a two-way at the two-way's centre line, so the one-way's carriageway, drawn about
its own centre, lands across the two-way's middle. Here each such end moves to the side of the two-way where traffic
goes the one-way's way: right of its direction of travel (left in left-hand countries), by a quarter of the two-way's
width, and the move fades out along the one-way over BLEND_M. Applied once to the roads layer when a cell is built
(mapdata), so the meshes, the grading and the AI graph all see the same lines.

BNGE_ONEWAY_JOINS=0 turns it off.
"""
import os

ON = os.environ.get("BNGE_ONEWAY_JOINS", "1") != "0"
BLEND_M = 20.0
TWO_WAY_W_M = 8.0         # a two-way's width when OpenStreetMap gives no lane count
LANE_M = 3.3
MAX_SHIFT_M = 5.0
KEY_DEG = 1e-6


def apply(feats, left_hand=False):
    """Moves one-way ends in place (feats.lonlat). Returns the number of ends moved (earthfast onewayjoin::
    oneway_join_ends). Input contract (R2a): feats.lonlat an exact C-contiguous float64 numpy array of 2+ columns
    (a read-only one is refused at the first move, as numpy refused the write), int64 offsets, osm_oneway a column of
    the contract, osm_lanes None, an int64 array (passed as float64) or a float64 array; anything else TypeError."""
    if not ON or len(feats) == 0 or "osm_oneway" not in feats.attrs:
        return 0
    import fast
    fast.require(452, "oneway_join_ends")
    lanes = feats.attrs.get("osm_lanes")
    if lanes is not None and getattr(lanes, "dtype", None) is not None and lanes.dtype.kind in "iu":
        # (osmtags.join makes an int64 column when every road has a lane count, as numpy did before the port; the
        # removed Python read float(lanes[j]): exact for any lane count, so the same widths; Codex on d4bd7eb4)
        import numpy as np
        lanes = lanes.astype(np.float64)
    return fast._ef.oneway_join_ends(feats.lonlat, feats.part_off, feats.ring_off, feats.coord_off,
                                     feats.attrs.get("osm_oneway"), lanes, bool(left_hand),
                                     len(feats))
