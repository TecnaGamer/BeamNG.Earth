"""The trees' leaf cycle at a place (the user, 2026-11-27: "trees should gradually lose their leaves"; winter W4):
anchor_ready's "leaf", the place's leaf calendar from the Rust kernel (native/earthfast/src/phenology.rs: per species
group the days of bud break, full leaf, first colour, leaf fall, the midpoint and bare, by latitude, elevation and the
Koppen-Geiger class; sources in docs/research/2026-11-27-leaf-phenology.md). The mod (baretrees.lua) gives each
deciduous tree its own dates about it, so a wood colours and thins over weeks and leafs out in spring tree by tree.

    BNE_LEAF_CYCLE=on    the Experimental page's "Leaf cycle" (with Winter: the bare trees are winter's, BNE_WINTER)

Off (the default): no field, the bare trees by the season's amount as before. Never in plans, cell facts, tiles or the
decision archives (revisit.NOT_SWITCHES): a field of the anchor's message only. This module is glue: the kernel's
arguments and its result as the message's table.
"""
import os

import fast

ON = os.environ.get("BNE_LEAF_CYCLE", "").strip().lower() in ("on", "1", "true", "yes")
NATIVE = 645                # earthfast: phenology leaf_calendar
VERSION = 1                 # leaf.v: the field's layout


def anchor_leaf(lat, elev_m, koppen):
    """The anchor's leaf field: {v, share (the place's share of trees in the cycle), cal (4 species x [bud, full,
    colour, drop, half, bare] days of year, northern-equivalent), perM (days per metre higher: spring, autumn), spread
    (a tree's own days, one sd: spring, autumn), elev}. elev_m: the ground's height there (None: sea level)."""
    fast.require(NATIVE, "leaf_calendar")
    elev = float(elev_m) if elev_m is not None else 0.0
    share, cal, per_m, spread = fast._ef.leaf_calendar(float(lat), elev, int(koppen or 0))
    return {"v": VERSION, "share": share, "cal": [list(r) for r in cal], "perM": list(per_m), "spread": list(spread),
            "elev": round(elev, 1)}
