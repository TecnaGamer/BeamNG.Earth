"""German Autobahn guide signs: the glue of the earthfast kernel autobahn_signs (native/earthfast/src/deautobahn/: what
each board says, where it stands, the boards and their gantries as one mesh; the rules and their sources are there).
The design: docs/research/2026-11-08-german-autobahn-signs-design.md.

Built in the sign tiles (signstream.py, field "autobahn") of tiles in Germany (real borders: germany.contains, behind a
lat/lon box so nobody outside it fetches the border data) whose sign shards carry the German block (tools/
build_sign_tags.py: "de" on the exit records and the shards; older shards: nothing changes). Per junction the glue reads
the record, finds the gore on the generated roads (exitsigns._compute_gore with the downstream carriageway), samples the
carriageway every STEP_M along the 2.5 km approach (pavement edges, the opposite carriageway, the mapped lanes, the
ground, Germany or not) and the decks and tunnels on it, and hands the arrays to the kernel. BNE_AUTOBAHN_SIGNS: off =
no German signs and no step-back of the direction boards (exactly as before), unset = on, on = also without the border
check (tests on synthetic places)."""
from collections import OrderedDict
import logging
import math
import os
import threading
import time

import numpy as np

import exitsigns
import fast
import roads
import signroads
import signshards
import deautobahnfont

log = logging.getLogger('earthd.deautobahn')

KERNEL = 173                     # earthfast VERSION with autobahn_signs / autobahn_columns

# the kernel's input columns (agreement pinned by test_deautobahn_kernel.test_columns_agree)
RAW_FIELDS = (
    "node_ref", "node_name", "node_noref",
    "lanes_destination", "lanes_destination_ref", "lanes_destination_symbol",
    "lanes_destination_colour", "lanes_destination_country", "lanes_turn",
    "link_destination", "link_destination_ref", "link_destination_symbol", "link_destination_colour",
    "link_destination_country", "link_destination_to", "link_destination_ref_to",
    "through_destination", "through_destination_ref", "through_destination_symbol",
    "through_destination_colour", "through_destination_country",
    "main_ref", "main_int_ref", "next_name", "next_ref",
    "branch0_destination", "branch0_destination_ref", "branch1_destination", "branch1_destination_ref",
)
JUNCTION_COLS = ("id", "side", "gore_s", "ref_s", "prev_exit_s", "fork_s", "other_motorway",
                 "fork_x", "fork_y", "fork_z", "fork_dx", "fork_dy", "b0_dx", "b0_dy", "b1_dx", "b1_dy",
                 "gore_x", "gore_y", "gore_z", "gore_dx", "gore_dy", "follow")
PROFILE_COLS = ("s", "x", "y", "dx", "dy", "edge_l", "edge_r", "opp_edge", "lanes", "ground_l", "ground_r",
                "road_z_max", "de", "through")

ENV = os.environ.get("BNE_AUTOBAHN_SIGNS", "").strip().lower()
BOX = (5.8, 47.2, 15.1, 55.1)    # lon0, lat0, lon1, lat1: Germany's box, checked before any border lookup
DE_MARGIN_M = 2600.0             # junctions this far round a tile (the 2000 m Kreuz board, 125 m before the gore)
STEP_M = 10.0                    # the approach samples
REFERENCE_M = 125.0              # the reference point before the painted gore (Hessen LWBA 4.2.1)
GORE_FALLBACK_M = 60.0           # the nose down the ramp when the gore search finds none (measured 44-83 m on the A3)
DECK_R = 3.0                     # a gantry's half depth plus margin: no assembly on or under a deck
SKIP_PAD_M = 5.0
SNAP_M = 40.0
CACHE = 256
_cache, _lock = OrderedDict(), threading.Lock()
_warned = -float('inf')


def _samples(frame, tx, ty):
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    xs = np.array([x0, x0, x1, x1, (x0 + x1) / 2])
    ys = np.array([y0, y1, y0, y1, (y0 + y1) / 2])
    la, lo, _ = frame.enu_to_geodetic(xs, ys, 0.)
    return np.atleast_1d(la), np.atleast_1d(lo)


def _in_box(la, lo):
    return (lo >= BOX[0]) & (lo <= BOX[2]) & (la >= BOX[1]) & (la <= BOX[3])


def german_tile(frame, tx, ty):
    """Whether sign tile (tx, ty) touches Germany: germany.contains at its corners and middle (the box first, real
    borders inside it; no border data: not Germany, today's output). The sign tiles are a session's own, so a session
    without border data builds today's tiles and the next one the German signs."""
    if ENV == "off":
        return False
    la, lo = _samples(frame, tx, ty)
    if not _in_box(la, lo).any():
        return False
    if ENV == "on":
        return True
    import germany
    return bool(germany.contains(lo, la).any())


def data_here(source, frame, tx, ty):
    """Whether a sign shard round the tile carries the German block (the shards' top-level "de")."""
    tags = getattr(source, "tags", None)
    shard = getattr(tags, "shard", None)
    if shard is None:
        return False
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - DE_MARGIN_M, x1 + DE_MARGIN_M]),
                                      np.array([y0 - DE_MARGIN_M, y1 + DE_MARGIN_M]), 0.)
    xa, ya = signshards.tile(float(np.max(la)), float(np.min(lo)))
    xb, yb = signshards.tile(float(np.min(la)), float(np.max(lo)))
    for x in range(xa, xb + 1):
        for y in range(ya, yb + 1):
            got = shard(x, y)
            if isinstance(got, dict) and got.get("de"):
                return True
    return False


def lanes_at(edges, s, through=True):
    """The main lanes at s (metres upstream of the junction) from the block's edges: the mapped `lanes` less the lanes
    turn:lanes marks as leaving only (an exit lane is not counted: the mounting's count); through=False: the mapped
    `lanes` as the road is drawn, the exit lane included (the layout's count); None when not mapped there."""
    for _wid, s0, s1, tags in edges:
        if s0 - 1e-6 <= s <= s1 + 1e-6:
            try:
                v = float(str(tags.get("lanes", "")).split(";")[0])
            except ValueError:
                return None
            if not math.isfinite(v):
                return None
            n = int(min(max(round(v), 1), 8))          # (mapped data: an absurd count saturates)
            turns = [t for t in str(tags.get("turn:lanes", "")).split("|")] if tags.get("turn:lanes") else []
            if through and len(turns) == n:
                n -= sum(1 for t in turns if ("right" in t or "left" in t) and "through" not in t and "none" not in t
                         and t)
            return min(max(1, n), 8)
    return None


def raw_fields(de):
    """The kernel's RAW_FIELDS of one German block (strings, "" = not mapped)."""
    edges = de.get("edges") or []
    src = edges[de["lanes_src"]][3] if 0 <= de.get("lanes_src", -1) < len(edges) else {}
    link, thr = de.get("link") or {}, de.get("through") or {}
    br = de.get("branches") or []
    node, main, nxt = de.get("node") or {}, de.get("main") or {}, de.get("next") or {}

    def b(i, key):
        return str((br[i].get("tags") or {}).get(key, "")) if i < len(br) else ""
    v = {"node_ref": node.get("ref") or "", "node_name": node.get("name") or "",
         "node_noref": "yes" if node.get("noref") else "",
         "lanes_destination": src.get("destination:lanes", ""), "lanes_destination_ref": src.get("destination:ref:lanes", ""),
         "lanes_destination_symbol": src.get("destination:symbol:lanes", ""),
         "lanes_destination_colour": src.get("destination:colour:lanes", ""),
         "lanes_destination_country": src.get("destination:country:lanes", ""), "lanes_turn": src.get("turn:lanes", ""),
         "link_destination": link.get("destination", ""), "link_destination_ref": link.get("destination:ref", ""),
         "link_destination_symbol": link.get("destination:symbol", ""),
         "link_destination_colour": link.get("destination:colour", ""),
         "link_destination_country": link.get("destination:country", ""), "link_destination_to": link.get("destination:to", ""),
         "link_destination_ref_to": link.get("destination:ref:to", ""),
         "through_destination": thr.get("destination", ""), "through_destination_ref": thr.get("destination:ref", ""),
         "through_destination_symbol": thr.get("destination:symbol", ""),
         "through_destination_colour": thr.get("destination:colour", ""),
         "through_destination_country": thr.get("destination:country", ""),
         "main_ref": main.get("ref") or "", "main_int_ref": main.get("int_ref") or "",
         "next_name": nxt.get("name") or "", "next_ref": nxt.get("ref") or "",
         "branch0_destination": b(0, "destination"), "branch0_destination_ref": b(0, "destination:ref"),
         "branch1_destination": b(1, "destination"), "branch1_destination_ref": b(1, "destination:ref")}
    return [str(v[k]) for k in RAW_FIELDS]


def _unit(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else np.array([1.0, 0.0])


def _arc(path):
    return np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(path, axis=0), axis=1))]


def _at(path, arc, s):
    """The point and unit direction of a polyline at arc length s (clamped)."""
    s = min(max(s, 0.0), arc[-1])
    k = int(min(max(np.searchsorted(arc, s) - 1, 0), len(path) - 2))
    t = (s - arc[k]) / max(arc[k + 1] - arc[k], 1e-9)
    return path[k] + t * (path[k + 1] - path[k]), _unit(path[k + 1] - path[k])


def _project(path, q):
    """The arc length of q's projection on a polyline."""
    arc = _arc(path)
    best = (math.inf, 0.0)
    for k in range(len(path) - 1):
        ab = path[k + 1] - path[k]
        t = float(np.clip((q - path[k]) @ ab / max(ab @ ab, 1e-12), 0.0, 1.0))
        d = float(np.linalg.norm(path[k] + t * ab - q))
        if d < best[0]:
            best = (d, arc[k] + t * (arc[k + 1] - arc[k]))
    return best[1]


GORE_SIGN_W = 1.9                # the Zeichen 333 board: its spot keeps this wide (plus the gore search's clearance) off both
FORK_BOARDS_W = 10.0             # the two fork boards side by side (about 4 m each and a metre between)
FORK_CLEAR_M = 1.0
FORK_SEARCH_M = 150.0
BLOCK_ALONG_M = 12.0             # another pavement blocks a sample when it passes within this along the road
BLOCK_LATERAL_M = 60.0
Z_MARGIN_M = 150.0               # samples this far round the tile get heights (a station moves up to 80 m to fit)
FOLLOW_LATERAL_M = 8.0           # a junction's earlier exit counts when it lies this near this exit's own carriageway
FOLLOW_ALIGN = 0.7               # ... and runs the same way


def _at_many(path, arc, s):
    """Points and unit directions of a polyline at arc lengths s (clamped)."""
    s = np.clip(np.asarray(s, float), 0.0, arc[-1])
    k = np.clip(np.searchsorted(arc, s) - 1, 0, len(path) - 2)
    seg = path[k + 1] - path[k]
    n = np.maximum(np.linalg.norm(seg, axis=1), 1e-9)
    t = (s - arc[k]) / np.maximum(arc[k + 1] - arc[k], 1e-9)
    return path[k] + t[:, None] * seg, seg / n[:, None]


def _proj(q, line):
    """Each point's nearest point on a polyline: (distance, segment, t, the point, the segment's unit tangent)."""
    a, b = line[:-1, :2], line[1:, :2]
    ab = b - a
    l2 = np.maximum((ab * ab).sum(1), 1e-12)
    t = np.clip((((q[:, None, :] - a[None]) * ab[None]).sum(-1)) / l2[None], 0.0, 1.0)
    p = a[None] + t[..., None] * ab[None]
    d = np.linalg.norm(q[:, None, :] - p, axis=-1)
    k = np.argmin(d, axis=1)
    idx = np.arange(len(q))
    tang = ab[k] / np.sqrt(l2[k])[:, None]
    return d[idx, k], k, t[idx, k], p[idx, k], tang


def _half_width(line, width, k, t, right):
    """A row's half width across `right` at its points (k, t): its mitred normals, as the placement helpers measure it."""
    cross = signroads.cross(line)
    c = cross[k] * (1.0 - t)[:, None] + cross[np.minimum(k + 1, len(cross) - 1)] * t[:, None]
    return np.abs((c * right).sum(1)) * width / 2


def _gore(d, app, ramp, pav):
    """(nose s down the ramp, the Zeichen 333 spot or None, the ramp's direction there). The nose (where the ramp's
    pavement leaves the carriageway's) sets the reference point; the board's spot keeps GORE_SIGN_W and the gore
    search's clearance off both pavements. Both with exitsigns._compute_gore and the downstream carriageway rows as
    ramp_mains (the mainline way is split at the junction node, so main_ids alone never reach it)."""
    mains_rows = [r for r in pav if not r[1].link and r[1].cls in exitsigns.MAIN_CLASSES and r[1].oneway]
    ramp_rows = [r for r in pav if r[1].link and exitsigns._way_id(r[0]) == d["ramp_id"]]
    rarc = _arc(ramp)
    direction = _unit(app[-1] - app[-2]) if len(app) >= 2 else np.array([1.0, 0.0])
    down = set()
    qs, _ = _at_many(ramp, rarc, np.arange(10.0, min(150.0, rarc[-1]) + 0.1, 20.0))
    for fid, sp, line, width in mains_rows:
        dist, _k, _t, at, tang = _proj(qs, np.asarray(line, float))
        travel = tang * (sp.dir or 1)
        ok = (dist <= 30.0) & ((travel * direction).sum(1) > .8) & (np.abs(((qs - at) * travel).sum(1)) <= 2.0)
        if ok.any():
            down.add(fid)
    mains = {r[0]: sorted(down) for r in ramp_rows}
    extra = {"ramp_id": d["ramp_id"], "main_ids": d.get("main_ids", []), "side": d.get("side", 1)}

    def search(width, **kw):
        try:
            return exitsigns._compute_gore(extra, app, ramp, pav, mains, width, **kw) if ramp_rows else None
        except Exception:          # (an odd ramp: none)
            return None
    nose, sign = search(0.0, clearance=.05), search(GORE_SIGN_W)
    nose_s = _project(ramp, np.asarray(nose[3][:2], float)) if nose is not None else min(GORE_FALLBACK_M, rarc[-1])
    if sign is None:
        return nose_s, None, None
    ss = _project(ramp, np.asarray(sign[3][:2], float))
    _, u = _at(ramp, rarc, ss)
    return nose_s, np.asarray(sign[3][:2], float), u


def fork_spot(p0, p1, w0, w1, need=FORK_BOARDS_W, clear=None):
    """(point, distance) along two branches from their fork (ENU paths, widths w0, w1) where their pavements are
    need + 2 FORK_CLEAR_M apart (and clear(point), if given: no other pavement across the boards there): the fork
    boards stand there, between them; None within FORK_SEARCH_M."""
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    if len(p0) < 2 or len(p1) < 2:
        return None
    a0, a1 = _arc(p0), _arc(p1)
    end = min(a0[-1], a1[-1], FORK_SEARCH_M)
    for s in np.arange(0.0, end + 0.1, 5.0):
        q0, _ = _at(p0, a0, s)
        q1, _ = _at(p1, a1, s)
        if float(np.linalg.norm(q0 - q1)) - (w0 + w1) / 2 >= need + 2 * FORK_CLEAR_M:
            mid = (q0 + q1) / 2
            if clear is None or clear(mid):
                return mid, float(s)
    return None


def _skips(context, app_ll, frame, path, arc):
    """The approach's deck and tunnel ranges in s (no assembly on, under or in them)."""
    feats = context.around(np.asarray(app_ll, float))
    chunks = []
    for specs, parts in ((roads.bridge_specs, roads.bridge_parts), (roads.tunnel_specs, roads.tunnel_parts)):
        for i, spec in enumerate(specs(feats)):
            if spec is None:
                continue
            for ring in parts(feats, i):
                if len(ring) >= 2:
                    pts = np.column_stack(frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.))
                    chunks.append({'deck': True, 'n': pts, 'w': spec.width})
    if not chunks:
        return []
    bad = []
    for s in np.arange(0.0, arc[-1] + 0.1, STEP_M):
        q, _ = _at(path, arc, s)
        bad.append((s, exitsigns._on_deck(q, chunks, DECK_R)))
    out, start = [], None
    for s, b in bad + [(arc[-1] + STEP_M, False)]:
        if b and start is None:
            start = s
        elif not b and start is not None:
            out.append((start - SKIP_PAD_M, s - STEP_M + SKIP_PAD_M))
            start = None
    return out


def _junction(rec, frame, context, left_hand):
    """One German record's geometry in the frame, heights left out (filled per tile: _fill): raw fields, junction
    columns, profile rows (heights NaN), the other pavements' lateral intervals per sample, the skip ranges, and the
    points that need heights (the gore board, the fork boards); None when the record cannot be placed."""
    d = rec["exit"]
    de = d["de"]
    app_ll = d.get("de_approach") or d.get("approach")
    if not app_ll or len(app_ll) < 2 or not d.get("ramp"):
        return None
    app = exitsigns._xy(frame, app_ll)               # far end first
    path = app[::-1]                                 # from the junction upstream
    arc = _arc(path)
    ramp = exitsigns._xy(frame, d["ramp"])
    pav = list(context.pavements(frame, np.vstack([np.asarray(app_ll, float), np.asarray(d["ramp"], float)]), left_hand))
    nose_s, sign_pt, sign_u = _gore(d, app, ramp, pav)
    s = np.arange(0.0, arc[-1] + 0.1, STEP_M)
    q, up = _at_many(path, arc, s)
    travel = -up
    right = np.c_[travel[:, 1], -travel[:, 0]]
    n = len(s)
    best_d = np.full(n, np.inf)
    at, edge, fid = q.copy(), np.full(n, np.nan), np.full(n, -1)
    rows_l = [(r[0], r[1], np.asarray(r[2], float), float(r[3])) for r in pav if len(r[2]) >= 2]
    for ri, (rf, sp, line, width) in enumerate(rows_l):
        if sp.link or sp.cls not in exitsigns.MAIN_CLASSES:
            continue
        dist, k, t, pt, tang = _proj(q, line)
        along = tang * (sp.dir or 1) if sp.dir else tang * np.sign((tang * travel).sum(1))[:, None]
        ok = (dist <= SNAP_M) & ((along * travel).sum(1) >= .45) & (dist < best_d)
        if ok.any():
            best_d[ok], at[ok], fid[ok] = dist[ok], pt[ok], ri
            edge[ok] = _half_width(line, width, k, t, right)[ok]
    # the lanes as drawn (the layout: the exit lane is pavement too) and the through lanes (the mounting)
    lanes = np.array([lanes_at(de.get("edges") or [], v, through=False) or 0 for v in s], float)
    through = np.array([lanes_at(de.get("edges") or [], v) or 0 for v in s], float)
    miss = ~np.isfinite(edge)
    edge[miss] = (np.where(lanes[miss] > 0, lanes[miss], 3) * 4.0 + 6.0) / 2
    lanes = np.where(lanes > 0, lanes, np.clip(np.round((2 * edge - 6.0) / 4.0), 1, 8))
    through = np.where(through > 0, through, lanes)
    opp = np.full(n, np.nan)
    blocks = [[] for _ in range(n)]
    polys = [exitsigns._outline(line, width) for _, _, line, width in rows_l]
    for ri, (rf, sp, line, width) in enumerate(rows_l):
        dist, k, t, pt, tang = _proj(at, line)
        if not sp.link and sp.cls in exitsigns.MAIN_CLASSES:
            off = ((pt - at) * right).sum(1)
            alongd = ((pt - at) * travel).sum(1)
            e = _half_width(line, width, k, t, right)
            heading = tang * (sp.dir or 1)
            other = (dist <= BLOCK_LATERAL_M) & (np.abs(alongd) <= BLOCK_ALONG_M) & (fid != ri) & \
                ((heading * travel).sum(1) <= -.45) & (off < 0)
            cand = off + e
            opp = np.where(other & (~np.isfinite(opp) | (cand > opp)), cand, opp)
        # the pavement's stretches on each sample's cross-section (its outline cut by the line across the road there)
        poly = polys[ri][:, :2]
        lo, hi = poly.min(axis=0), poly.max(axis=0)
        # (the carriageway's own outline too: where it widens past `edge`, a merge or a curve, its posts go past it)
        for i in np.flatnonzero(dist <= BLOCK_LATERAL_M + width):
            if (at[i] < lo - BLOCK_LATERAL_M).any() or (at[i] > hi + BLOCK_LATERAL_M).any():
                continue
            for a, b in exitsigns._spans(poly, at[i], travel[i], right[i]):
                if b >= -BLOCK_LATERAL_M and a <= BLOCK_LATERAL_M:
                    blocks[i].append((float(a), float(b)))
    rows = np.column_stack([s, at[:, 0], at[:, 1], travel[:, 0], travel[:, 1], -edge, edge, opp, lanes,
                            np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan), np.zeros(n), through])
    jcols = {c: np.nan for c in JUNCTION_COLS}
    prev = de.get("prev_exit_s")
    jcols.update(id=float(rec["id"]), side=float(d.get("side", 1)), gore_s=-nose_s, ref_s=REFERENCE_M - nose_s,
                 prev_exit_s=float(prev) if prev is not None else np.nan,
                 other_motorway=float(bool(de.get("joins")) and de.get("joins") != (de.get("main") or {}).get("ref")))
    points = {}
    if sign_pt is not None:
        jcols.update(gore_x=sign_pt[0], gore_y=sign_pt[1], gore_dx=sign_u[0], gore_dy=sign_u[1])
        points["gore"] = sign_pt
    if de.get("fork_s") is not None and len(de.get("branches") or []) == 2 and d.get("de_ramp"):
        rp = exitsigns._xy(frame, d["de_ramp"])
        fq, fu = _at(rp, _arc(rp), float(de["fork_s"]))
        paths = []
        for b in de["branches"]:
            raw_path = b.get("path") or []
            ll = signshards.decode_path(raw_path, rec["lon"], rec["lat"]) if raw_path and not isinstance(raw_path[0], list) \
                else raw_path
            paths.append(exitsigns._xy(frame, ll) if len(ll) >= 2 else np.array([fq, fq + fu]))
        widths = []
        for bp in paths:
            w = 8.3
            for rf, sp, line, width in rows_l:
                if sp.link and len(bp) >= 2 and _proj(bp[-1:], line)[0][0] <= 3.0:
                    w = width
                    break
            widths.append(w)
        def clear(point, direction):
            side = np.array([direction[1], -direction[0]])
            for (_, _, _, _), poly in zip(rows_l, polys):
                for a, b in exitsigns._spans(poly[:, :2], point, direction, side):
                    if a < FORK_BOARDS_W / 2 + FORK_CLEAR_M and b > -FORK_BOARDS_W / 2 - FORK_CLEAR_M:
                        return False
            return True
        spot = fork_spot(paths[0], paths[1], widths[0], widths[1], clear=lambda p: clear(p, fu))
        if spot is not None:
            dirs = [_unit(bp[min(3, len(bp) - 1)] - bp[0]) if len(bp) >= 2 else fu for bp in paths]
            jcols.update(fork_s=float(de["fork_s"]) + spot[1], fork_x=spot[0][0], fork_y=spot[0][1], fork_dx=fu[0],
                         fork_dy=fu[1], b0_dx=dirs[0][0], b0_dy=dirs[0][1], b1_dx=dirs[1][0], b1_dy=dirs[1][1])
            points["fork"] = spot[0]
    skips = _skips(context, app_ll, frame, path, arc)
    return {"raw": raw_fields(de), "jcols": jcols, "rows": rows, "right": right, "blocks": blocks,
            "skips": np.array(skips, np.float64).reshape(-1, 2), "points": points}


def _cached(rec, frame, context, left_hand, version):
    """_junction, kept per (frame, record, sign build, side of the road) for the road context it was measured on: a new
    context (other roads, another width setting) measures again."""
    key = (round(frame.lat0, 7), round(frame.lon0, 7), int(rec["id"]), version, bool(left_hand))
    with _lock:
        got = _cache.get(key)
        if got is not None and got[0] is context:
            _cache.move_to_end(key)
            return got[1]
    geo = _junction(rec, frame, context, left_hand)
    with _lock:
        _cache[key] = (context, geo)
        _cache.move_to_end(key)
        while len(_cache) > CACHE:
            _cache.popitem(last=False)
    return geo


def _fill(geos, frame, elev, box):
    """Per tile: the heights (and Germany or not) of the samples near the tile, Z_MARGIN_M round it, in one height and
    one border lookup for all junctions; the rest stay NaN (no station there can be the tile's). (raw, junction row,
    profile rows, skips, blocks, block offsets) per junction."""
    x0, y0, x1, y1 = box
    pts, owners = [], []
    masks = []
    for gi, g in enumerate(geos):
        r = g["rows"]
        m = (r[:, 1] >= x0 - Z_MARGIN_M) & (r[:, 1] <= x1 + Z_MARGIN_M) & (r[:, 2] >= y0 - Z_MARGIN_M) & \
            (r[:, 2] <= y1 + Z_MARGIN_M)
        masks.append(m)
        for i in np.flatnonzero(m):
            at, rt, e = r[i, 1:3], g["right"][i], r[i, 6]
            for p in (at - rt * (e + 1.5), at + rt * (e + 1.5), at - rt * e * .8, at, at + rt * e * .8):
                pts.append(p)
                owners.append((gi, "row", i))
        for name, p in g["points"].items():
            if x0 - Z_MARGIN_M <= p[0] <= x1 + Z_MARGIN_M and y0 - Z_MARGIN_M <= p[1] <= y1 + Z_MARGIN_M:
                pts.append(p)
                owners.append((gi, name, -1))
    z = de_flags = np.zeros(0)
    if pts:
        P = np.asarray(pts, float)
        la, lo, _ = frame.enu_to_geodetic(P[:, 0], P[:, 1], 0.)
        la, lo = np.atleast_1d(la), np.atleast_1d(lo)
        h = np.asarray(elev(la, lo), float).ravel()
        z = np.asarray(frame.geodetic_to_enu(la, lo, h)[2], float).ravel()
        if ENV == "on":
            de_flags = np.ones(len(P))
        else:
            import germany
            de_flags = germany.contains(lo, la).astype(float)
    out = []
    k = 0
    for gi, g in enumerate(geos):
        rows = g["rows"].copy()
        jc = dict(g["jcols"])
        for i in np.flatnonzero(masks[gi]):
            zz = z[k:k + 5]
            rows[i, 9], rows[i, 10], rows[i, 11] = zz[0], zz[1], zz[2:].max()
            rows[i, 12] = de_flags[k + 3]
            k += 5
        for name in g["points"]:
            if k < len(owners) and owners[k][0] == gi and owners[k][1] == name:
                jc["gore_z" if name == "gore" else "fork_z"] = z[k]
                k += 1
        blocks, boff = [], [0]
        for i in range(len(rows)):
            if masks[gi][i]:
                blocks += g["blocks"][i]
            boff.append(len(blocks))
        out.append((g["raw"], jc, rows, g["skips"], np.array(blocks, np.float64).reshape(-1, 2), np.array(boff, np.int64)))
    return out


def follow_ups(recs, frame):
    """{record id: s of the earlier exit}: the exits that follow an earlier exit of the same junction (same number and
    name) on their own carriageway (within FOLLOW_LATERAL_M of their approach, running the same way), a cloverleaf's
    second exit; the kernel gives them no chain of their own."""
    def ident(r):
        node = r["exit"]["de"].get("node") or {}
        return (node.get("ref") or "", node.get("name") or "")

    def heading(r):
        app = r["exit"].get("de_approach") or r["exit"].get("approach") or []
        if len(app) < 2:
            return None
        xy = exitsigns._xy(frame, app[-2:])
        return _unit(xy[1] - xy[0])
    pts = {r["id"]: exitsigns._xy(frame, [[r["lon"], r["lat"]]])[0] for r in recs}
    out = {}
    for b in recs:
        key = ident(b)
        if key == ("", ""):
            continue
        app = b["exit"].get("de_approach") or b["exit"].get("approach") or []
        if len(app) < 2:
            continue
        path = exitsigns._xy(frame, app)[::-1]
        arc = _arc(path)
        for a in recs:
            if a is b or ident(a) != key:
                continue
            ha = heading(a)
            if ha is None:
                continue
            dist, k, t, _pt, tang = _proj(pts[a["id"]][None], path)
            travel_b = -tang[0]
            at = arc[k[0]] + t[0] * (arc[k[0] + 1] - arc[k[0]])
            if dist[0] <= FOLLOW_LATERAL_M and float(travel_b @ ha) >= FOLLOW_ALIGN and 1.0 < at < arc[-1]:
                out[b["id"]] = min(out.get(b["id"], math.inf), float(at))
    return out


def build_tile(md, frame, tx, ty, elev, context, left_hand=False):
    """(mesh (positions, triangles, uvs, MATERIALS, material ids) or None, the boards as JSON lines) of the German
    Autobahn signs owned by sign tile (tx, ty). A junction that fails is left out (logged once a minute), never the
    tile."""
    global _warned
    source = getattr(md, "highway_signs", None)
    if source is None:
        return None, []
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - DE_MARGIN_M, x1 + DE_MARGIN_M]),
                                      np.array([y0 - DE_MARGIN_M, y1 + DE_MARGIN_M]), 0.)
    recs = [r for r in source.tags.query(float(np.min(lo)), float(np.min(la)), float(np.max(lo)), float(np.max(la)),
                                          kinds=("exit",)) if "de" in r.get("exit", {})]
    if not recs:
        return None, []
    if ENV != "on":
        import germany
        inside = germany.contains([r["lon"] for r in recs], [r["lat"] for r in recs])
        recs = [r for r, here in zip(recs, inside) if here]
    version = source.version() if hasattr(source, "version") else None
    try:
        follows = follow_ups(recs, frame)
    except Exception as error:
        follows = {}
        _warn("follow-ups", error)
    geos = []
    for r in recs:
        try:
            app = r["exit"].get("de_approach") or r["exit"].get("approach") or []
            if len(app) < 2:
                continue
            xy = exitsigns._xy(frame, app)
            lo_, hi_ = xy.min(axis=0) - 300.0, xy.max(axis=0) + 300.0
            if hi_[0] < x0 or lo_[0] > x1 or hi_[1] < y0 or lo_[1] > y1:
                continue                       # no station of this junction can stand in the tile
            g = _cached(r, frame, context, left_hand, version)
            if g is None:
                continue
            if r["id"] in follows:
                jc = dict(g["jcols"])
                prev = jc["prev_exit_s"]
                jc["prev_exit_s"] = min(prev if np.isfinite(prev) else math.inf, follows[r["id"]])
                jc["follow"] = 1.0
                g = dict(g, jcols=jc)
            geos.append(g)
        except Exception as error:
            _warn(f"junction {r.get('id')}", error)
    if not geos:
        return None, []
    filled = _fill(geos, frame, elev, (x0, y0, x1, y1))
    raws, jrows, profs, skips, blocks, boffs = [], [], [], [], [], []
    base = 0
    for raw, jc, rows, sk, bl, bo in filled:
        raws.append(raw)
        jrows.append([jc[c] for c in JUNCTION_COLS])
        profs.append(rows)
        skips.append(sk)
        blocks.append(bl)
        boffs.append(bo[(1 if boffs else 0):] + base)
        base += len(bl)
    cps, glyphs, faces, params, kern = deautobahnfont.tables()
    picto = sorted(deautobahnfont.PICTOGRAMS.items())
    p, t, uv, mi, boards = fast._ef.autobahn_signs(
        [v for r in raws for v in r], np.ascontiguousarray(np.array(jrows, np.float64)),
        np.ascontiguousarray(np.vstack(profs)), np.cumsum([0] + [len(x) for x in profs]).astype(np.int64),
        np.ascontiguousarray(np.vstack(skips).reshape(-1, 2)), np.cumsum([0] + [len(x) for x in skips]).astype(np.int64),
        np.ascontiguousarray(np.vstack(blocks).reshape(-1, 2)), np.ascontiguousarray(np.concatenate(boffs).astype(np.int64)),
        cps, glyphs, faces, params, kern, [k for k, _ in picto], np.array([v for _, v in picto], np.int64),
        [float(x0), float(y0), float(x1), float(y1)])
    if not len(t):
        return None, boards
    return (p, t, uv, list(deautobahnfont.MATERIALS), mi), boards


def _warn(what, error):
    global _warned
    now = time.monotonic()
    if now - _warned >= 60.:
        _warned = now
        log.warning('German Autobahn signs: %s left out: %s: %s', what, type(error).__name__, error)
