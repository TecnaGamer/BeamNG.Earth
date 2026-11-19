"""Step 3 (SPEC.md): per system each crossing's upper and lower, the clearance it asks (clearance_need) and the
carriageway pairs it holds over (under_pairs), the members' signs and their level order, as the Order artifact."""
import numpy as np

import gradeshapes as _gsh

from . import artifacts, cache

FORMAT = 1
LAWS = (("gradeshapes", ["HEADROOM_M", "RAIL_HEADROOM_M", "HEADROOM_MARGIN_M", "CLEAR_PATH_M", "DECK_MIN_DEPTH_M",
                         "DECK_FULL_DEPTH_M", "DECK_SPAN_RATIO", "RAIL_DECK_DEPTH_M", "COVER_M"]),)
LAWS_DIGEST = cache.laws_digest(*LAWS)
STEP = 3

SIGN = {"tunnel": -1, "ground": 0, "rail": 0, "deck": 1, "rail_deck": 1}


def levels(net, members, edges):
    """({member: int level}, findings): a longest-path layering of 'd below u', level[u] > level[d] at every crossing
    (each member starts at its sign's band: 0 on the ground). A tunnel over something pushes that down, anything else
    rises over what it crosses, so the ground stays at 0 as far as the order lets it. A cycle (a road over itself) is a
    finding, never an exception: the last pass' levels stand."""
    lv = {i: SIGN[net.m[i].kind] for i in members}
    for _ in range(len(members) + 1):
        changed = False
        for u, d in edges:
            if lv[u] > lv[d]:
                continue
            if SIGN[net.m[u].kind] < 0:
                lv[d] = lv[u] - 1
            else:
                lv[u] = lv[d] + 1
            changed = True
        if not changed:
            return lv, []
    names = ", ".join(sorted({str(net.m[i].key[0]) for i in members})[:8])
    return lv, [artifacts.Finding(STEP, "fail", "order_cycle", "the crossings of %s loop (a road over itself)" % names)]


def run(planner, net, sysm, system, scope):
    """The Order artifact of one system: today's crossings (their under_pairs and clearance_need), the members' signs
    and levels."""
    crossings = [artifacts.Crossing(int(c["u"]), float(c["su"]), int(c["d"]), float(c["sd"]),
                                     np.asarray(c["q"], np.float64),
                                     float(_gsh.clearance_need(net, c["u"], c["d"], c["q"])),
                                     np.asarray(c["under"], np.float64).reshape(-1, 2))
                 for c in sysm["crossings"]]
    members = sorted(set(int(i) for i in sysm["seeds"]) | {c.u for c in crossings} | {c.d for c in crossings})
    lv, findings = levels(net, members, [(int(c["u"]), int(c["d"])) for c in sysm["crossings"]])
    return artifacts.Order(artifacts.Meta(STEP, FORMAT, "", (), LAWS_DIGEST, scope), system, crossings,
                           {str(i): SIGN[net.m[i].kind] for i in members},
                           {str(i): int(lv[i]) for i in members}, findings)
