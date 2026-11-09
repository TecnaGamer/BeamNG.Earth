"""The original NumPy roads._merge_overlaps and roads._ground_pins (bridge chain solving), kept only as test oracles
for their Rust ports (native/earthfast/src/deckmerge.rs: deck_merge_round, deck_ground_pins): the ported functions
must give the same heights and pins bit for bit (test_deckmerge.py)."""
import numpy as np

from roads import (DECK_MAX_GRADE, DUAL_DECK_M, MERGE_TOUCH_M, PIN_M, PIN_ROAD_M, DECK_SIGMA, _chain_graph,
                   _dijkstra, _ground_envelope, _pin, _smooth_pinned)


def _ground_pins(lines, ground_lines):
    """The joints of decks (lines' ends shared by two or more) a ground road of PIN_ROAD_M or more goes on from (an
    end of it within PIN_M): _pin's points. Pure."""
    ends = np.array([p[:2] for ln in lines for p in (ln[0], ln[-1])], np.float64).reshape(-1, 2)
    if not len(ends) or not ground_lines:
        return []
    # (every end against every end at once, in row blocks: one at a time was ~1 s of a Queensboro Bridge tile)
    keep = np.zeros(len(ends), bool)
    for r0 in range(0, len(ends), 512):
        blk = ends[r0:r0 + 512]
        d = np.hypot(ends[None, :, 0] - blk[:, None, 0], ends[None, :, 1] - blk[:, None, 1])
        keep[r0:r0 + 512] = (d < 0.5).sum(1) > 1
    shared = ends[keep]
    if not len(shared):
        return []
    out = []
    for gl in ground_lines:
        gl = np.asarray(gl, np.float64)[:, :2]
        if len(gl) < 2 or float(np.hypot(*np.diff(gl, axis=0).T).sum()) < PIN_ROAD_M:
            continue
        for e in (gl[0], gl[-1]):
            out.extend(shared[np.hypot(e[0] - shared[:, 0], e[1] - shared[:, 1]) < PIN_M])
    return out


def _merge_overlaps(lines, zs, widths, levels, rounds=3, pins=None):
    """Heights of decks lying over each other (a ramp leaving the mainline, parallel carriageways of
    one level) pulled to their mean where their surfaces overlap, so a split is one surface until the
    decks part (2026-10-11 the user: exits and splits not clean). Parts are related when they share an
    end node or a level and run within ~30 degrees of each other. Abutment ends stay (pins: deck_profile's). Pure."""
    nodes, edges, adj = _chain_graph(lines)
    deg = np.zeros(len(nodes), int)
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    deg = _pin(nodes, deg, pins)
    n = len(lines)
    pairs = []
    # the related pairs (i, j), i then j ascending, as a double loop testing each would find them, one line against
    # all others at a time (that loop was ~4 s of a Fells Point teleport, on the event loop)
    if n:
        ea = np.array([e[0] for e in edges[:n]])
        eb = np.array([e[1] for e in edges[:n]])
        lo = np.array([np.asarray(ln)[:, :2].min(0) for ln in lines])
        hi = np.array([np.asarray(ln)[:, :2].max(0) for ln in lines])
        lv = None if levels is None else np.empty(n, object)
        if lv is not None:
            lv[:] = list(levels)[:n]
        idx = np.arange(n)
        for i in range(n):
            ok = (ea[i] == ea) | (ea[i] == eb) | (eb[i] == ea) | (eb[i] == eb)
            if lv is not None:
                ok |= ~(lv != lv[i]).astype(bool)          # elementwise, the same != as levels[i] != levels[j]
            ok &= ~(hi[i] < lo - 30).any(1) & ~(hi < lo[i] - 30).any(1)
            ok &= idx != i
            pairs.extend((i, int(j)) for j in np.flatnonzero(ok))
    if not pairs:
        return zs
    floors = [np.array(z, np.float64) for z in zs]
    zs = [z.copy() for z in floors]
    # never raised faster than DECK_MAX_GRADE from any abutment of the chain, along it (a lift at a joint reached the
    # parts beyond it: Spaghetti Junction's ramps, lifted to a higher deck of their level beside them, stood 21 m up at
    # the joint of a 48 m part ending on the ground, 40 %)
    reach = [np.full(len(z), np.inf) for z in zs]
    for k0, (a0, b0, _s) in enumerate(edges[:n]):
        for node, zend in ((a0, floors[k0][0]), (b0, floors[k0][-1])):
            if deg[node] != 1 or not np.isfinite(zend):
                continue
            d = _dijkstra(adj, {node: 0.0})
            for k, (ea, eb, es) in enumerate(edges[:n]):
                reach[k] = np.minimum(reach[k], zend + DECK_MAX_GRADE * np.minimum(d[ea] + es, d[eb] + (es[-1] - es)))
    for _ in range(rounds):
        new = [z.copy() for z in zs]
        acc = [np.zeros(len(z)) for z in zs]
        cnt = [np.zeros(len(z)) for z in zs]
        up = [np.zeros(len(z)) for z in zs]     # twin carriageways: how far up to the other's height
        for i, j in pairs:
            A, B = lines[i][:, :2], lines[j][:, :2]
            P, Q = B[:-1], B[1:]
            ab = Q - P
            L2 = np.maximum((ab ** 2).sum(1), 1e-12)
            t = np.clip(((A[:, None, :] - P[None]) * ab[None]).sum(2) / L2[None], 0, 1)
            near = P[None] + t[..., None] * ab[None]
            d = np.hypot(*(A[:, None, :] - near).transpose(2, 0, 1))
            m = np.argmin(d, axis=1)
            dmin = d[np.arange(len(A)), m]
            tj = t[np.arange(len(A)), m]
            zj = zs[j][m] + tj * (zs[j][np.minimum(m + 1, len(zs[j]) - 1)] - zs[j][m])
            ta = np.gradient(A, axis=0)
            tb = ab[m]
            cos = np.abs((ta * tb).sum(1)) / (np.hypot(*ta.T) * np.hypot(*tb.T) + 1e-12)
            # soft: full weight 2 m into the overlap and when parallel, none at its edge (a hard mask
            # made 4 m steps mid-viaduct where a neighbour's footprint started)
            depth = np.clip(((widths[i] + widths[j]) / 2 + MERGE_TOUCH_M - dmin) / 2.0, 0.0, 1.0)
            w = depth * np.clip((cos - 0.85) / 0.1, 0.0, 1.0) * np.isfinite(zj)
            acc[i] += w * np.nan_to_num(zj)
            cnt[i] += w
            if levels is not None and levels[i] == levels[j]:
                # one level's carriageways side by side are one deck (the Tsing Ma Bridge's two directions stood 15 m
                # apart in height, the one over the lower deck lifted by the stacking, 2026-10-18): the lower lifted
                # to the higher, fading out from DUAL_DECK_M apart
                w2 = np.clip((DUAL_DECK_M - dmin) / 10.0, 0.0, 1.0) * np.clip((cos - 0.85) / 0.1, 0.0, 1.0)
                lift = np.where(np.isfinite(zj), w2 * np.maximum(np.nan_to_num(zj) - zs[i], 0.0), 0.0)
                up[i] = np.maximum(up[i], lift)
        for i in range(n):
            m = cnt[i] > 0
            new[i][m] = (zs[i][m] + acc[i][m]) / (1 + cnt[i][m])
            new[i] = np.maximum(new[i], zs[i] + up[i])
            a, b, es = edges[i]
            # a lift fades along the deck no faster than DECK_MAX_GRADE: it ended where the parts drew apart, over a
            # few metres of their spacing (a High Five ramp lifted 11 m to its twin fell back over ~60 m, 35 %)
            lifted = new[i] > zs[i] + 1e-6
            if lifted.any():
                new[i] = np.maximum(new[i], _ground_envelope(es, np.where(lifted, new[i], -np.inf), DECK_MAX_GRADE))
            # never raised faster than DECK_MAX_GRADE from an abutment (a road's bridge forking off uphill near one,
            # taken for a carriageway beside it, pulled US-24's deck 1.1 m up in the first 3 m off its abutment near
            # Buena Vista CO, 2026-10-27)
            # (fading out toward a joint with other parts, where the parts must meet: capped right up to it, an 8 m
            # ramp part of a Calgary diverging diamond stood 0.7 m under the joint it leaves, #369)
            rise = np.full(len(es), np.inf)
            to_joint = np.full(len(es), np.inf)
            for end, d, at in ((a, es, 0), (b, es[-1] - es, -1)):
                if deg[end] == 1:
                    rise = np.minimum(rise, floors[i][at] + DECK_MAX_GRADE * d)
                else:
                    to_joint = np.minimum(to_joint, d)
            capped = np.minimum(new[i], np.maximum(floors[i], rise))     # (from the unmerged heights: no creep per round)
            if np.isfinite(to_joint).any() and np.isfinite(rise).any():
                w = np.clip(to_joint / max(float(es[-1]), 1e-6), 0.0, 1.0)
                capped = w * capped + (1.0 - w) * new[i]
            new[i] = np.minimum(capped, np.maximum(reach[i], floors[i]))
            if deg[a] == 1:
                new[i][0] = zs[i][0]
            if deg[b] == 1:
                new[i][-1] = zs[i][-1]
        zs = new
    out = []
    for i, z in enumerate(zs):                  # re-smoothed, ends kept
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(lines[i][:, :2], axis=0).T))]
        sm = _smooth_pinned(z, s[-1] / max(len(s) - 1, 1), DECK_SIGMA) if len(z) > 3 else z
        out.append(np.maximum(sm, floors[i]))
    return out
