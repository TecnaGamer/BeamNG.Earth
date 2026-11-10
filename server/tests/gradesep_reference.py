"""The Python gradesep._spans / roads._deck_gaps had before their rows, lines and look round were moved to earthfast
(gradesep_select / gradesep_gather_rows, first_ring_ends / deck_gaps_round, 2026-11-08), kept only as test oracles:
the server's must give exactly these answers."""
import math

import numpy as np

import fast
import gradesep as G
import roads as R


def pieces(feats, i, kind):
    """gradesep._pieces as it was (the length measured for every feature with a range)."""
    rngs = R.tag_level_ranges(feats, i)
    L = R._feature_length_m(feats, i) if rngs else 0.0
    out, at = [], 0.0
    for a, b, k in rngs:
        if a - at > R.MIN_PART:
            out.append((kind, at, a, False))
        if k == "bridge":
            out.append((None, a, b, True))
        elif kind == 1 and (b - a) * L < G.railways.SHORT_TUNNEL_M:
            out.append((2, a, b, False))
        elif kind == 0:
            out.append((3, a, b, False))
        at = max(at, b)
    if 1.0 - at > R.MIN_PART:
        out.append((kind, at, 1.0, False))
    return out


def gather(frame, rows, own):
    """gradesep._gather as it was (earthfast.gradesep_gather over the layers joined)."""
    layers = {}
    xy, co, ro, po, pof, pk, pa, pb, ha = [], [], [], [], [], [], [], [], []
    nc = nr = npp = nf = nq = 0
    gi = np.empty(len(rows), np.int64)
    for r, (_fid, f, i) in enumerate(rows):
        b = layers.get(id(f))
        if b is None:
            kinds, halves, poff, lk, la_, lb_, _ids = G._layer_rows(f)
            e, n, _ = frame.geodetic_to_enu(f.lonlat[:, 1], f.lonlat[:, 0], 0.0)
            xy.append(np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], 1).reshape(-1, 2))
            co.append(np.asarray(f.coord_off, np.int64)[:-1] + nc)
            ro.append(np.asarray(f.ring_off, np.int64)[:-1] + nr)
            po.append(np.asarray(f.part_off, np.int64)[:-1] + npp)
            pof.append(poff[:-1] + nq)
            pk.append(lk)
            pa.append(la_)
            pb.append(lb_)
            ha.append(halves)
            b = layers[id(f)] = (nf, kinds)
            nc += len(f.lonlat)
            nr += len(f.coord_off) - 1
            npp += len(f.ring_off) - 1
            nf += len(f)
            nq += len(lk)
        gi[r] = b[0] + i if b[1][i] >= 0 else -1
    if not layers:
        return [], [], [], [], [], np.zeros((0, 3))
    cat = lambda v, t: np.ascontiguousarray(np.concatenate(v), t)       # noqa: E731
    keep = gi >= 0
    pts, starts, line_row, line_piece, de, _deck_row = fast._ef.gradesep_gather(
        cat(xy, np.float64).reshape(-1, 2), np.r_[cat(co, np.int64), nc], np.r_[cat(ro, np.int64), nr],
        np.r_[cat(po, np.int64), npp], np.ascontiguousarray(np.where(keep, gi, 0), np.int64)[keep],
        np.r_[cat(pof, np.int64), nq], cat(pk, np.int64), cat(pa, np.float64), cat(pb, np.float64))
    rid = np.flatnonzero(keep)[line_row] if len(line_row) else np.zeros(0, np.int64)
    lines = np.split(pts, starts[1:-1]) if len(line_row) else []
    allk, alla, allb, allh = cat(pk, np.int64), cat(pa, np.float64), cat(pb, np.float64), cat(ha, np.float64)
    kinds = allk[line_piece].tolist()
    a_l, b_l = alla[line_piece].tolist(), allb[line_piece].tolist()
    halves = allh[gi[rid]].tolist() if len(rid) else []
    groups = rid.tolist()
    owner = [(own.get(rows[r][0]), a, b) for r, a, b in zip(groups, a_l, b_l)]
    return lines, kinds, halves, groups, owner, np.asarray(de, np.float64).reshape(-1, 3)


def underpasses(md, frame, lines, kinds, groups):
    """gradesep._underpasses as it was (the lines as a list)."""
    keep = [True] * len(lines)
    cand = [n for n, k in enumerate(kinds) if k == 3]
    if not cand:
        return keep
    # (the whole tunnel: Overture splits one at every node, a 2.6 km motorway tunnel into 50 m pieces)
    lens = {c: float(np.hypot(*np.diff(lines[c], axis=0).T).sum()) for c in cand}
    ends = np.array([[lines[c][0], lines[c][-1]] for c in cand], np.float64)       # (n, 2, 2)
    parent = list(range(len(cand)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for p in range(len(cand)):
        d = np.hypot(ends[:, :, None, 0] - ends[p, None, None, :, 0], ends[:, :, None, 1] - ends[p, None, None, :, 1])
        for q in np.flatnonzero((d < G.TOUCH_M).any(axis=(1, 2))):
            parent[find(int(q))] = find(p)
    total = {}
    for p, c in enumerate(cand):
        total[find(p)] = total.get(find(p), 0.0) + lens[c]
    for p, c in enumerate(cand):
        if total[find(p)] >= G.SHORT_ROAD_TUNNEL_M:
            keep[c] = False
    cand = [c for c in cand if keep[c]]
    if not cand:
        return keep
    over = [n for n, k in enumerate(kinds) if k in (0, 1)]
    hits = R._crossings_by([lines[n] for n in over], [lines[c] for c in cand]) if over else [[] for _ in cand]
    ask, at = [], []                # (DEM points, (candidate, crossings' slice, portals' slice))
    for c, cr in zip(cand, hits):
        pts = []
        for pi, _arc, pt in cr:
            o = over[pi]
            ends = (lines[o][0], lines[o][-1])
            if groups[o] != groups[c] and min(np.hypot(*(e - pt)) for e in ends) >= G.END_M:
                pts.append(np.asarray(pt, np.float64))
        if not pts:
            keep[c] = False
            continue
        xy = lines[c]
        u0 = xy[0] - xy[1]
        u1 = xy[-1] - xy[-2]
        portals = [xy[0] + G.PORTAL_OUT_M * u0 / max(np.hypot(*u0), 1e-9),
                   xy[-1] + G.PORTAL_OUT_M * u1 / max(np.hypot(*u1), 1e-9)]
        at.append((c, len(ask), len(ask) + len(pts)))
        ask += pts + portals
    if ask:
        P = np.asarray(ask, np.float64)
        la, lo, _ = frame.enu_to_geodetic(P[:, 0], P[:, 1], 0.0)
        z = np.asarray(G._dem(md).sample(np.asarray(la), np.asarray(lo), G.DEM_ZOOM), np.float64)
        for c, i0, i1 in at:
            if not (np.nanmax(z[i0:i1]) - np.nanmin(z[i1:i1 + 2]) < G.UNDERPASS_COVER_M):
                keep[c] = False
    return keep


def spans(md, key, feats):
    """gradesep._spans as it was."""
    if feats is None or not len(feats) or "subtype" not in feats.attrs:
        return {}
    if not G.ON and not any(p[0] in (2, 3) for i in range(len(feats)) for p in (G._row(feats, i)[2] or ())):
        return {}
    import revisit
    try:
        others = G._neighbours(md, key)
    except Exception:
        revisit.fault()
        return {}
    lat_c, lon_c = key.center()
    import geodesy
    frame = geodesy.EnuFrame(lat_c, lon_c, 0.0)
    own = {str(f): i for i, f in enumerate(feats.ids)}
    fb = feats.boxes(frame)
    ok = np.isfinite(fb).all(axis=1)
    if not ok.any():
        return {}
    box = (fb[ok, 0].min() - G.NEAR_M, fb[ok, 1].min() - G.NEAR_M, fb[ok, 2].max() + G.NEAR_M,
           fb[ok, 3].max() + G.NEAR_M)
    seen, rows = set(), []
    for f in [feats] + others:
        if f is None or not len(f) or "subtype" not in f.attrs:
            continue
        bx = f.boxes(frame)
        ids = G._layer_rows(f)[6]
        for i in np.flatnonzero(~((bx[:, 2] < box[0]) | (bx[:, 0] > box[2]) | (bx[:, 3] < box[1])
                                  | (bx[:, 1] > box[3]))).tolist():
            fid = ids[i]
            if fid in seen:
                continue
            seen.add(fid)
            rows.append((fid, f, i))
    rows.sort(key=lambda r: r[0])
    lines, kinds, halves, groups, owner, de = gather(frame, rows, own)
    try:
        keep = underpasses(md, frame, lines, kinds, groups)
    except Exception:
        revisit.fault()
        return {}
    lines, kinds, halves, groups, owner = ([v[n] for n in range(len(lines)) if keep[n]]
                                           for v in (lines, kinds, halves, groups, owner))
    if not lines:
        return {}
    pts = np.ascontiguousarray(np.vstack(lines))
    starts = np.r_[0, np.cumsum([len(x) for x in lines])].astype(np.int64)
    kinds = np.asarray(kinds, np.int64)
    halves = np.asarray(halves, np.float64)
    cand, smp = fast._ef.gradesep_candidates(pts, starts, kinds, halves, np.asarray(groups, np.int64),
                                             np.ascontiguousarray(de), np.array(
        [G.END_M, G.SAMPLE_GAP_M, G.SAMPLE_STEP_M, G.MIN_SIN, G.TOUCH_M, G.GAP_MAX_M, G.TURN_COS, G.GAP_SHARE],
        np.float64))
    if not G.ON and len(cand):
        one = cand[:, 5] == 1.0
        cand, smp = cand[one], smp.reshape(-1, 8, 2)[one].reshape(-1, 2)
    out, why = {}, []
    for ln, k in enumerate(kinds):
        i, a, b = owner[ln]
        if k == 3 and i is not None:
            out.setdefault(i, []).append((round(a, 6), round(b, 6), "ground"))
            why.append((i, round(a, 6), round(b, 6), 3))
    if not len(cand):
        return G._merged(feats, out, why)
    z = np.full(len(smp), np.nan)
    good = np.isfinite(smp).all(axis=1)
    if good.any():
        try:
            la, lo, _ = frame.enu_to_geodetic(smp[good, 0], smp[good, 1], 0.0)
            z[good] = np.asarray(G._dem(md).sample(np.asarray(la), np.asarray(lo), G.DEM_ZOOM), np.float64)
        except Exception:
            revisit.fault()
            return {}
    sp = fast._ef.gradesep_spans(pts, starts, kinds, halves, cand, np.ascontiguousarray(z.reshape(-1, 8)), np.array(
        [G.SEP_M, G.SIDE_M, G.CLEAR_M, G.MIN_HALF_M, G.MIN_SIN, G.TOUCH_M, G.TURN_COS], np.float64))
    for ln, s0, s1, how in sp:
        ln = int(ln)
        i, a, b = owner[ln]
        L = float(np.hypot(*np.diff(lines[ln], axis=0).T).sum())
        if i is None or L <= 0 or s1 - s0 <= 0:
            continue
        fa = round(a + (b - a) * float(s0) / L, 6)
        fbb = round(a + (b - a) * float(s1) / L, 6)
        out.setdefault(i, []).append((fa, fbb, "bridge"))
        why.append((i, fa, fbb, int(how)))
    return G._merged(feats, out, why)


def first_ring_ends(feats):
    """roads._first_ring_ends as it was (not kept on feats)."""
    n = len(feats)
    got = np.full((n, 2, 2), np.nan)
    for i in range(n):
        if feats.part_off[i + 1] <= feats.part_off[i]:
            continue
        r = feats.ring_off[feats.part_off[i]]
        a, b = feats.coord_off[r], feats.coord_off[r + 1]
        if b - a >= 2:
            got[i, 0], got[i, 1] = feats.lonlat[a][:2], feats.lonlat[b - 1][:2]
    return got


def deck_gaps(feats, i, rngs, ends=None):
    """roads._deck_gaps as it was (ends: first_ring_ends(feats), made here when not given)."""
    if not R.GAP_DECK_ON:
        return rngs
    kinds = [r[2] for r in rngs] if rngs else ()
    if "tunnel" in kinds:
        return rngs
    if "bridge" not in kinds:
        near = R._near_bridge_ends(feats)
        if near is not None and not near[i]:
            return rngs
    if R._structure_width(feats, i) is None:
        return rngs
    rings = [np.asarray(r, np.float64) for part in feats.parts(i) for r in part if len(r) >= 2]
    if not rings:
        return rngs
    ring = rings[0][:, :2]
    kx, ky = 111320.0 * math.cos(math.radians(float(ring[0, 1]))), 111320.0
    m = (ring - ring[0]) * [kx, ky]
    L = float(np.hypot(*np.diff(m, axis=0).T).sum())
    if L <= 0:
        return rngs
    specs = R.road_specs(feats)
    ends = first_ring_ends(feats) if ends is None else ends
    boxes = R._feature_boxes(feats)
    end_deck = []
    for e, p in ((0, ring[0]), (1, ring[-1])):
        d = np.hypot((ends[:, :, 0] - p[0]) * kx, (ends[:, :, 1] - p[1]) * ky)
        d[i] = np.inf
        hit = [(int(j), int(k)) for j, k in zip(*np.nonzero(d < R.GAP_JOIN_M))]
        ok = len(hit) == 1
        if ok:
            j, k = hit[0]
            rj = R.raw_level_ranges(feats, j)
            ok = any(kk == "bridge" and (a <= 1e-6 if k == 0 else b >= 1.0 - 1e-6) for a, b, kk in rj)
        end_deck.append(ok)
    bridges = [(a * L, b * L) for a, b, k in rngs if k == "bridge"]
    pad = (R.GAP_JUNCTION_M + 1.0) / np.array([kx, ky])
    lo, hi = ring.min(0) - pad, ring.max(0) + pad
    near = np.flatnonzero((boxes[:, 0] <= hi[0]) & (boxes[:, 2] >= lo[0]) & (boxes[:, 1] <= hi[1]) &
                          (boxes[:, 3] >= lo[1]))
    others = [(ends[j, k] - ring[0]) * [kx, ky] for j in near if j != i and specs[j] is not None for k in (0, 1)
              if np.isfinite(ends[j, k]).all()]
    got = np.asarray(fast._ef.deck_gaps(np.ascontiguousarray(m), np.asarray(bridges, np.float64).reshape(-1, 2),
                                        np.asarray(end_deck, np.int64),
                                        np.asarray(others, np.float64).reshape(-1, 2), R.GAP_DECK_M,
                                        R.GAP_JUNCTION_M),
                     np.float64).reshape(-1, 2)
    if len(got) == len(bridges) and np.allclose(got, np.asarray(bridges, np.float64).reshape(-1, 2)):
        return rngs
    out = [(float(min(max(a / L, 0.0), 1.0)), float(min(max(b / L, 0.0), 1.0)), "bridge") for a, b in got]
    out[0] = (0.0, out[0][1], "bridge") if got[0, 0] <= 1e-6 else out[0]
    out[-1] = (out[-1][0], 1.0, "bridge") if got[-1, 1] >= L - 1e-6 else out[-1]
    return out
