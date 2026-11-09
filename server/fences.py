"""Mapped chain-link fences and noise walls (Overture infrastructure lines).

OpenStreetMap's barrier=fence with fence_type=chain_link, and barrier=wall with wall=noise_barrier (or
barrier=noise_barrier), kept on the feature's class and in source_tags; stone walls (wall=dry_stone ...) as
stone meshes (stone_walls). Other fences and walls are left out. Straight stock panels along the line: West Coast USA's chain-link panel (s_chainlink_old, 2.04 m,
origin at its forward end) and its concrete noise blocker (s_concrete_noiseblocker, 4 m along local +y,
origin at one end). A panel is kept when its centre is in the tile, so a line split across tiles is not
drawn twice.
"""
import json
import logging
import math
import os

import numpy as np

import mapcells
import mapgeom
from guardrails import DECK_AUTO_SNAP_TOLERANCE_M, _deck_level_hint, _surface_info

log = logging.getLogger("earthd.fences")

G_CHAIN, G_WALL = "chainlink", "noisewall"
# s_chainlink_old: x -2.02..0.02, z -0.16..1.60. The centre is 1 m behind the origin along local +x.
CHAIN_LEN, CHAIN_OX, CHAIN_H, CHAIN_LIFT = 2.04, 1.0, 1.76, 0.16
# s_concrete_noiseblocker: x 0..2.5 (thickness), y 0..4 (along the wall), z 0..2.4. Centre (1.25, 2).
WALL_LEN, WALL_ALONG, WALL_ACROSS, WALL_H = 4.0, 2.0, 1.25, 2.4
# its flat face is the full-height one at local x = 2.5; the other side has a 0.48 m base and a slope up
# (the mesh: 9.6 m2 of face at +x against 2.0 m2 at -x), so a panel is laid with local +x toward the road.
# A noise wall stands off the carriageway it runs along, not on the mapped line the way happens to have: the line
# follows the real deck's outer edge, which is a shoulder's width outside the carriageway drawn here. It is laid
# along the carriageway, WALL_EDGE_M beyond its painted edge, with the flat face toward it.
WALL_EDGE_M = 0.5           # m between the carriageway's edge and the wall's face (its inner side)
WALL_FIT_REACH_M = 20.0     # m: a wall farther from every carriageway keeps its mapped line
WALL_FIT_MAX_DEG = 40.0     # a wall this far off the carriageway's direction is not laid along it
WALL_ACROSS_DEG = 50.0      # ... and over the carriageway: never drawn, the wall must not cross the road
WALL_LEVEL_M = 3.0          # m: a carriageway this far above or below the wall is not the one it runs along
WALL_DECK_REACH_M = 4.0     # m past a deck's edge a bridge wall (its own layer/bridge tags) still stands on it
                            # (a road passing under a rail viaduct)
CHAIN_TYPES = {"chain_link", "chainlink", "chain-link", "chain"}
NOISE_TYPES = {"noise_barrier", "noisebarrier", "sound_barrier"}
BBOX_PAD_M = 8.0
OUTLINE_PAD_M = 1.0     # m past a bridge outline's edge a barrier along it still counts as on it (both mapped by hand)


def _tags(feats, i):
    """source_tags of feature i as a lower-case dict, or {}."""
    tags = feats.attrs.get("tags")
    if tags is None:
        return {}
    try:
        values = json.loads(str(tags[i]))
    except (TypeError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip().lower() for k, v in values.items()}


def kind_of(feats, i):
    """'chainlink' or 'noisewall' when the mapped line is one, else None."""
    classes = feats.attrs.get("class")
    cls = str(classes[i]).strip().lower() if classes is not None else ""
    tags = _tags(feats, i)
    barrier = tags.get("barrier", "")
    fence_type = tags.get("fence_type") or tags.get("fence:type") or ""
    wall = tags.get("wall") or ""
    if cls in CHAIN_TYPES or barrier in CHAIN_TYPES or fence_type in CHAIN_TYPES:
        return G_CHAIN
    if cls in ("fence",) or barrier == "fence":
        return G_CHAIN if fence_type in CHAIN_TYPES else None
    if cls in NOISE_TYPES or barrier in NOISE_TYPES or wall in NOISE_TYPES:
        return G_WALL
    return None


def _yaw(dx, dy):
    """Degrees that turn a Forest item's local +x toward (dx, dy)."""
    return math.degrees(math.atan2(dy, dx)) % 360.0


def _centres(line, length):
    """(centre (2,), unit tangent) every `length` metres along a polyline, the first half a panel in."""
    line = np.asarray(line, np.float64)
    if len(line) < 2:
        return
    step = np.diff(line, axis=0)
    seglen = np.hypot(step[:, 0], step[:, 1])
    total = float(seglen.sum())
    if total < length * 0.5:
        return
    arc = np.concatenate(([0.0], np.cumsum(seglen)))
    s = length * 0.5
    while s <= total - length * 0.25:
        k = min(max(int(np.searchsorted(arc, s, side="right") - 1), 0), len(line) - 2)
        span = max(float(arc[k + 1] - arc[k]), 1e-9)
        t = (s - float(arc[k])) / span
        p = line[k] + t * (line[k + 1] - line[k])
        yield p[:2], step[k] / span
        s += length


def _place(group, centre, tangent):
    """(x, y, yaw) of one panel whose centre sits on the mapped line."""
    tx, ty = float(tangent[0]), float(tangent[1])
    cx, cy = float(centre[0]), float(centre[1])
    if group == G_CHAIN:
        return cx + CHAIN_OX * tx, cy + CHAIN_OX * ty, _yaw(tx, ty)
    # the noise wall's long axis is local +y, so local +x points to the right of the line
    return (cx - WALL_ACROSS * ty - WALL_ALONG * tx, cy + WALL_ACROSS * tx - WALL_ALONG * ty,
            _yaw(ty, -tx))


def _in_outlines(rings, pts):
    """Which points lie in one of the rings ((k, 2) bridge outlines) or within OUTLINE_PAD_M of its edge."""
    from polygon import contains_points
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    m = np.zeros(len(pts), bool)
    for ring in rings:
        m |= np.asarray(contains_points(ring, pts), bool)
        a, b = ring[:-1], ring[1:]
        d = b - a
        l2 = np.maximum(np.sum(d * d, axis=1), 1e-12)
        t = np.clip(np.sum((pts[:, None, :] - a[None]) * d[None], axis=2) / l2[None], 0.0, 1.0)
        near = a[None] + t[:, :, None] * d[None]
        m |= np.hypot(pts[:, None, 0] - near[:, :, 0], pts[:, None, 1] - near[:, :, 1]).min(axis=1) <= OUTLINE_PAD_M
    return m


def rows(frame, infra_fn, box, elev, decks=(), roads=()):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of the chain-link fences and noise walls whose
    centre is inside box (x0, y0, x1, y1). decks ([{"n": (k, 3) XYZ, "w": road width}], as guardrails.forest_rows
    takes them): a wall or fence on a bridge stands on its deck, not on the ground under it (GitHub #775: a noise
    wall tagged layer=1 on the B 29's bridge stood on the road under it and blocked it). Which panels are on a deck
    is decided as for the mapped guardrails (guardrails._deck_level_hint / _surface_info): the way's own bridge /
    layer tags, else only where the ground already agrees with the deck. A way tagged on a bridge past its road
    deck's edge but in the bridge's mapped outline (man_made=bridge, the infrastructure's class bridge) stands at
    the nearest deck's height (guardrails.DECK_WIDE_REACH_M): the Stelling Road Overcrossing's noise wall and
    chain-link fence (OSM ways 921088133 / 921088134, layer=1) run along its outline (way 921088131) about 10 m
    from the 6.6 m road deck's centreline, its sidewalks mapped as footways (no deck), and stood on the road
    under it. roads ((fid, spec, centreline (k, 3), lon), roads.build_tile's roads_out) and the decks: a noise
    wall is laid along the carriageway it runs along, WALL_EDGE_M beyond its painted edge and with its flat face
    toward it, whichever way the way is drawn (a wall farther from every carriageway than WALL_FIT_REACH_M keeps
    its mapped line; one drawn across the carriageway is left out)."""
    if infra_fn is None:
        return []
    x0, y0, x1, y1 = map(float, box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return []
    pending = []
    seen, outlines, seen_outlines = set(), [], set()
    for key in keys:
        try:
            feats = infra_fn(key)
        except Exception as ex:
            log.debug("fences: infrastructure cell %s unavailable: %s", key, ex)
            continue
        if feats is None or not len(feats) or feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        boxes = feats.boxes(frame)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen or int(feats.kind[i]) not in (mapgeom.LINE, mapgeom.POLYGON):
                continue
            classes = feats.attrs.get("class")
            if (decks and int(feats.kind[i]) == mapgeom.POLYGON and classes is not None
                    and str(classes[i]).strip().lower() == "bridge" and fid not in seen_outlines):
                seen_outlines.add(fid)
                for part in feats.parts(i):
                    if part and len(part[0]) >= 4:
                        e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                        outlines.append(np.column_stack((e, n)))
                continue
            group = kind_of(feats, i)
            if group is None:
                continue
            b = boxes[i]
            if b[2] < x0 - BBOX_PAD_M or b[0] > x1 + BBOX_PAD_M or b[3] < y0 - BBOX_PAD_M or b[1] > y1 + BBOX_PAD_M:
                continue
            seen.add(fid)
            hint = _deck_level_hint(feats, i) if decks else None
            if hint is None and decks:
                # Overture keeps a way's OSM layer in the feature's own level, not always in its tags
                # (rail-viaduct noise walls seen with tags without layer, level=2.0): that counts as on a bridge.
                try:
                    lv = float(np.asarray(feats.attrs.get("level"))[i])
                except (TypeError, ValueError, IndexError):
                    lv = float("nan")
                if lv == lv:
                    hint = True if lv > 0 else (False if lv < 0 else None)
            length = CHAIN_LEN if group == G_CHAIN else WALL_LEN
            for pi, part in enumerate(feats.parts(i)):
                if not part or len(part[0]) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                for centre, tangent in _centres(np.column_stack((e, n)), length):
                    px, py, yaw = _place(group, centre, tangent)
                    # (a panel is kept when its centre is in the tile; the part's panels outside it are still
                    # made, for the bridge-continuity test below, and only not drawn)
                    keep = x0 <= centre[0] < x1 and y0 <= centre[1] < y1
                    pending.append((group, px, py, yaw, float(centre[0]), float(centre[1]), hint, fid, pi,
                                    keep, float(tangent[0]), float(tangent[1])))
    if not pending:
        return []
    near = np.zeros(len(pending), bool)     # (set below: walls laid along a carriageway)
    pz = np.zeros(len(pending))             # (that carriageway's own height there)
    if roads or decks:
        # A noise wall is laid along the carriageway it runs along, WALL_EDGE_M beyond its painted edge, with
        # its flat face (the stock model's local +x) toward it. The mapped line follows the real deck's outer
        # edge, a shoulder beyond the carriageway drawn here, and a way drawn at an angle laid panels over
        # the road; the carriageway decides both.
        fits = []                   # (polyline, half width): the carriageways a wall can be laid along
        deck_fits = []              # ... the decks alone: a wall tagged on a bridge keeps its own level
        for r in roads:
            try:
                pts = np.asarray(r[2] if len(r) > 2 else r, np.float64)
                spec = r[1] if len(r) > 1 else None
            except (TypeError, ValueError):
                continue
            if pts.ndim == 2 and pts.shape[1] >= 2 and len(pts) >= 2:
                try:
                    wide = float(getattr(spec, "width", 0.0) or 0.0)
                except (TypeError, ValueError):
                    wide = 0.0
                if wide > 0:
                    fits.append((pts, wide / 2.0, False))
        for d in decks:
            pts = np.asarray(d.get("n", ()), np.float64)
            if pts.ndim == 2 and pts.shape[1] >= 2 and len(pts) >= 2:
                try:
                    wide = float(d.get("w", 0.0) or 0.0)
                except (TypeError, ValueError):
                    wide = 0.0
                if wide > 0:
                    fits.append((pts, wide / 2.0, True))
                    deck_fits.append((pts, wide / 2.0))
        walls = np.array([p[0] == G_WALL for p in pending], bool)
        if fits and walls.any():
            # A wall stands off the edge of the carriageway it runs along and is laid along it, its flat face
            # toward it. The mapped line follows the real deck's outer edge (a shoulder beyond the carriageway
            # drawn here), and a way drawn at an angle put panels across the road.
            mid = np.array([[p[4], p[5]] for p in pending], np.float64)
            row = np.arange(len(pending))
            ang = np.radians([p[3] for p in pending])
            wt = np.stack([-np.sin(ang), np.cos(ang)], axis=1)      # (wall tangent, local +y)
            par_min = math.cos(math.radians(WALL_FIT_MAX_DEG))
            inside = _in_outlines(outlines, mid) if outlines else None   # (a wall on a mapped bridge's plan)
            # The wall's own level. One tagged on a bridge stands on its deck, not on the ground under it: a
            # carriageway at another level (a road passing under a rail viaduct) is not the one it runs along.
            la, lo, _ = frame.enu_to_geodetic(mid[:, 0], mid[:, 1], np.zeros(len(mid)))
            ref = np.asarray(elev(la, lo), np.float64).reshape(-1)
            if deck_fits and any(p[6] is True for p in pending):
                dd = np.full(len(pending), np.inf)
                dz = np.zeros(len(pending))
                for line, _half in deck_fits:
                    xy = line[:, :2]
                    a, b = xy[:-1], xy[1:]
                    d = b - a
                    l2 = np.maximum((d * d).sum(1), 1e-12)
                    v = mid[:, None, :] - a[None, :, :]
                    t = np.clip((v * d[None]).sum(2) / l2[None], 0.0, 1.0)
                    q = a[None, :, :] + t[:, :, None] * d[None, :, :]
                    dm = np.hypot(q[:, :, 0] - mid[:, None, 0], q[:, :, 1] - mid[:, None, 1])
                    k = np.argmin(dm, axis=1)
                    dm = dm[row, k]
                    if line.shape[1] >= 3:
                        zq = line[k, 2] + t[row, k] * (line[k + 1, 2] - line[k, 2])
                    else:
                        zq = ref
                    take = dm < dd
                    dd[take] = dm[take]
                    dz[take] = zq[take]
                on = np.array([p[6] is True for p in pending], bool) & np.isfinite(dd) & (dd <= WALL_FIT_REACH_M)
                ref = np.where(on, dz, ref)
            # An untagged wall's level is the road it runs along too: the ground beside it can be a bridge's
            # approach fill, higher than the road the wall stands next to (the wall climbed it at the abutments).
            untagged = np.array([p[0] == G_WALL and p[6] is None for p in pending], bool)
            if untagged.any():
                rd = np.full(len(pending), np.inf)
                rz = np.zeros(len(pending))
                for line, _half, is_deck in fits:
                    if is_deck:
                        continue
                    xy = line[:, :2]
                    a, b = xy[:-1], xy[1:]
                    d = b - a
                    l2 = np.maximum((d * d).sum(1), 1e-12)
                    v = mid[:, None, :] - a[None, :, :]
                    t = np.clip((v * d[None]).sum(2) / l2[None], 0.0, 1.0)
                    q = a[None, :, :] + t[:, :, None] * d[None, :, :]
                    dm = np.hypot(q[:, :, 0] - mid[:, None, 0], q[:, :, 1] - mid[:, None, 1])
                    k = np.argmin(dm, axis=1)
                    dm = dm[row, k]
                    seg = d[k]
                    seg = seg / np.maximum(np.hypot(seg[:, 0], seg[:, 1]), 1e-12)[:, None]
                    if line.shape[1] >= 3:
                        zq = line[k, 2] + t[row, k] * (line[k + 1, 2] - line[k, 2])
                    else:
                        zq = ref
                    along = np.abs((wt * seg).sum(1))
                    take = (dm < rd) & (along >= par_min) & (dm <= WALL_FIT_REACH_M)
                    rd[take] = dm[take]
                    rz[take] = zq[take]
                ref = np.where(untagged & np.isfinite(rd), rz, ref)
            fd = np.full(len(pending), np.inf)      # (nearest carriageway of any direction, at its level)
            fv = np.zeros((len(pending), 2))
            ft = np.zeros((len(pending), 2))
            fh = np.zeros(len(pending))
            pd = np.full(len(pending), np.inf)      # (nearest one the wall runs along)
            pv = np.zeros((len(pending), 2))
            pt = np.zeros((len(pending), 2))
            ph = np.zeros(len(pending))
            for line, half, is_deck in fits:
                xy = line[:, :2]
                a, b = xy[:-1], xy[1:]
                d = b - a
                l2 = np.maximum((d * d).sum(1), 1e-12)
                v = mid[:, None, :] - a[None, :, :]
                t = np.clip((v * d[None]).sum(2) / l2[None], 0.0, 1.0)
                q = a[None, :, :] + t[:, :, None] * d[None, :, :]
                dm = np.hypot(q[:, :, 0] - mid[:, None, 0], q[:, :, 1] - mid[:, None, 1])
                k = np.argmin(dm, axis=1)
                dm = dm[row, k]
                seg = d[k]
                seg = seg / np.maximum(np.hypot(seg[:, 0], seg[:, 1]), 1e-12)[:, None]
                if line.shape[1] >= 3:
                    zq = line[k, 2] + t[row, k] * (line[k + 1, 2] - line[k, 2])
                else:
                    zq = ref
                lev = np.abs(zq - ref) <= WALL_LEVEL_M      # (not a road under a viaduct)
                if is_deck:
                    # (a wall leaves a bridge at its mapped outline: an approach past it keeps its line; without
                    # an outline only the deck's own footprint counts, as in guardrails._surface_info)
                    plan = (dm <= half + WALL_DECK_REACH_M) | (inside if inside is not None else False)
                else:
                    plan = np.ones(len(pending), bool)
                take = (dm < fd) & lev & plan
                if take.any():
                    fd[take] = dm[take]
                    fv[take] = q[take, k[take]] - mid[take]
                    ft[take] = seg[take]
                    fh[take] = half
                along = np.abs((wt * seg).sum(1))
                take = (dm < pd) & (along >= par_min) & lev & plan   # (a crossing way must not steal the fit)
                if take.any():
                    pd[take] = dm[take]
                    pv[take] = q[take, k[take]] - mid[take]
                    pt[take] = seg[take]
                    ph[take] = half
                    pz[take] = zq[take]
            near = walls & np.isfinite(pd) & (pd <= WALL_FIT_REACH_M)
            across = walls & np.isfinite(fd) & (fd <= WALL_FIT_REACH_M) & (fd <= fh + 1.0) \
                & (np.abs((wt * ft).sum(1)) < math.cos(math.radians(WALL_ACROSS_DEG)))
            for i in np.flatnonzero(near).tolist():
                p = pending[i]
                T = pt[i]
                left = np.array([-T[1], T[0]])
                side = 1.0 if float((pv[i] * left).sum()) < 0.0 else -1.0
                here = mid[i] + pv[i] + side * left * (ph[i] + WALL_EDGE_M + WALL_ACROSS)
                t_new = side * T
                px, py, yaw = _place(p[0], (float(here[0]), float(here[1])),
                                     (float(t_new[0]), float(t_new[1])))
                keep = x0 <= here[0] < x1 and y0 <= here[1] < y1
                pending[i] = ((p[0], px, py, yaw, float(here[0]), float(here[1])) + p[6:9]
                              + (keep, float(t_new[0]), float(t_new[1])))
            for i in np.flatnonzero(across).tolist():
                p = pending[i]
                pending[i] = p[:9] + (False,) + p[10:]      # (never a panel across the road)
    xy = np.array([[p[1], p[2]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    z = np.asarray(z, np.float64)
    if decks:
        # (the deck's height under the panel's centre on the mapped line: the panel's origin is beside it)
        mid = np.array([[p[4], p[5]] for p in pending], np.float64)
        surf, ground, wide = _surface_info(frame, mid, elev, decks, [p[6] for p in pending])
        on = np.abs(surf - ground) > 1e-6
        if outlines:
            edge = ~on & np.isfinite(wide) & np.array([p[6] is True for p in pending])
            if edge.any():
                edge[edge] = _in_outlines(outlines, mid[edge])
            surf, on = np.where(edge, wide, surf), on | edge
        z = np.where(on, surf, z)
        if walls.any():
            # A wall laid along a carriageway follows that carriageway's own height: the ground beside it can be a
            # bridge's approach fill, and the wall climbed it to the deck's height at the abutments (two end panels
            # stood at the deck where a wall crossed under the Galgenbergstraße bridge).
            z = np.where(near, pz, z)
        # A wall running onto a bridge keeps to its deck: a stretch of its panels that would stand on the
        # ground under the deck, with the panels on both sides of that stretch at the deck's height, is on
        # the bridge (OSM often leaves layer / bridge off a noise wall along one: the wall broke into the
        # road under the bridge mid-span, 7.5 m down). A wall really under a bridge has no such panels.
        groups = {}
        for i, p in enumerate(pending):
            if p[6] is None:            # (its own tags/level say nothing about the bridge)
                groups.setdefault((p[7], p[8]), []).append(i)
        for idx in groups.values():
            if len(idx) < 3:
                continue
            idx = np.asarray(idx, np.int64)     # (in the order the part's panels were made: along the line)
            near_deck = np.isfinite(wide[idx])
            at_deck = near_deck & (np.abs(wide[idx] - ground[idx]) <= DECK_AUTO_SNAP_TOLERANCE_M)
            off_deck = near_deck & ~at_deck
            i0 = 0
            while i0 < len(idx):
                if not off_deck[i0]:
                    i0 += 1
                    continue
                i1 = i0
                while i1 + 1 < len(idx) and off_deck[i1 + 1]:
                    i1 += 1
                # (an end of the part: the panels on that side stop there, no bridge to run onto)
                before = at_deck[i0 - 1] if i0 > 0 else None
                after = at_deck[i1 + 1] if i1 + 1 < len(idx) else None
                if (before is None or before) and (after is None or after) and (before or after):
                    z[idx[i0:i1 + 1]] = wide[idx[i0:i1 + 1]]
                i0 = i1 + 1
    out = []
    for p, zz in zip(pending, z):
        if not p[9]:
            continue
        group, px, py, yaw = p[0], p[1], p[2], p[3]
        lift = CHAIN_LIFT if group == G_CHAIN else 0.0
        height = CHAIN_H if group == G_CHAIN else WALL_H
        out.append([group, round(px, 2), round(py, 2), round(float(zz) + lift, 2), round(yaw, 1) % 360.0,
                    1.0, height, 0.0])
    return out


# Mapped stone walls (GitHub #938: the field walls along Ireland's and Britain's country roads). OSM's barrier=wall with
# wall=dry_stone (or another stone wall type, or material=stone and no wall=), drawn as a stone solid along the mapped
# line: STONE_THICK_M thick, its own height= tag within STONE_H_RANGE else STONE_H_M over the ground, its foot
# STONE_FOOT_M into it (the ground between the line's points is not flat). The retaining walls' mesh draws it
# (roadmesh._retaining_walls, a parapet: stone on both faces down to its foot). Left out: a wall on a bridge (its
# bridge / layer tags or Overture's level), and the metres of it within STONE_ROAD_GAP_M of a ground road's edge (a
# wall mapped to the road's centre line at a gate or a junction would block it). BNE_STONE_WALLS=off: none.
STONE_ON = os.environ.get("BNE_STONE_WALLS", "on") != "off"
STONE_TYPES = {"dry_stone", "drystone", "dry_stone_wall", "stone", "field_stone", "fieldstone", "flint", "rubble",
               "stone_wall"}
STONE_MATERIALS = {"stone", "dry_stone", "granite", "limestone", "sandstone", "slate", "flint"}
STONE_H_M = 1.1
STONE_H_RANGE = (0.4, 2.5)
STONE_THICK_M = 0.5         # (roadmesh.WALL_COPING_M: all of its top is stone, no strip of soil)
STONE_FOOT_M = 0.3
STONE_STEP_M = 1.0
STONE_ROAD_GAP_M = 0.6


def stone_height(feats, i):
    """The height (m) of mapped stone wall i, or None when it is not one (STONE_TYPES / STONE_MATERIALS)."""
    classes = feats.attrs.get("class")
    cls = str(classes[i]).strip().lower() if classes is not None else ""
    tags = _tags(feats, i)
    if tags.get("barrier", cls) != "wall" and cls != "wall":
        return None
    wall = tags.get("wall", "")
    if not (wall in STONE_TYPES or (not wall and tags.get("material", "") in STONE_MATERIALS)):
        return None
    try:                            # Overture's height, else the OSM tag
        h = float(np.asarray(feats.attrs.get("height"))[i])
    except (TypeError, ValueError, IndexError):
        h = float("nan")
    if not h == h:
        try:
            h = float(tags.get("height", "").replace("m", "").strip())
        except ValueError:
            h = float("nan")
    return h if STONE_H_RANGE[0] <= h <= STONE_H_RANGE[1] else STONE_H_M


def _resample(line, step):
    """Points every ~step m along a polyline, its own end points kept."""
    seg = np.hypot(*np.diff(line, axis=0).T)
    arc = np.concatenate(([0.0], np.cumsum(seg)))
    if arc[-1] < 1e-6:
        return line[:1]
    n = max(int(math.ceil(arc[-1] / step)), 1)
    s = np.linspace(0.0, arc[-1], n + 1)
    return np.column_stack((np.interp(s, arc, line[:, 0]), np.interp(s, arc, line[:, 1])))


def _near_road(xy, roads):
    """(n,) bool: the points within a ground road's half width + STONE_ROAD_GAP_M of its centre line."""
    hit = np.zeros(len(xy), bool)
    lo, hi = xy.min(0), xy.max(0)
    for _fid, spec, pts, *_r in roads:
        line = np.asarray(pts, np.float64)[:, :2]
        if len(line) < 2:
            continue
        reach = float(getattr(spec, "width", 0.0) or 0.0) / 2 + STONE_ROAD_GAP_M
        if (line.min(0) > hi + reach).any() or (line.max(0) < lo - reach).any():
            continue
        a, d = line[:-1], np.diff(line, axis=0)
        dd = np.maximum((d * d).sum(1), 1e-12)
        t = np.clip(((xy[:, None, :] - a[None]) * d[None]).sum(2) / dd[None], 0.0, 1.0)
        foot = a[None] + t[..., None] * d[None]
        dist = np.hypot(foot[..., 0] - xy[:, None, 0], foot[..., 1] - xy[:, None, 1]).min(1)
        hit |= dist < reach
    return hit


def stone_walls(frame, infra_fn, box, elev, roads=()):
    """The mapped stone walls in box (x0, y0, x1, y1) as roads.retaining_walls' wall dicts (base, top, foot, toward,
    thick, back_top, parapet), for roadmesh.tunnel's stone: each mapped line's pieces whose segments' middles are in
    the box (a wall split across tiles meets at the edge, not drawn twice), cut where a ground road (roads: build_tile's
    roads_out, (fid, spec, pts, ...)) runs. elev(lat, lon) -> the ground's height."""
    if infra_fn is None or not STONE_ON:
        return []
    x0, y0, x1, y1 = map(float, box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return []
    runs, seen = [], set()
    for key in keys:
        try:
            feats = infra_fn(key)
        except Exception as ex:
            log.debug("stone walls: infrastructure cell %s unavailable: %s", key, ex)
            continue
        if feats is None or not len(feats) or feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        boxes = feats.boxes(frame)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen or int(feats.kind[i]) not in (mapgeom.LINE, mapgeom.POLYGON):
                continue
            b = boxes[i]
            if b[2] < x0 - BBOX_PAD_M or b[0] > x1 + BBOX_PAD_M or b[3] < y0 - BBOX_PAD_M or b[1] > y1 + BBOX_PAD_M:
                continue
            h = stone_height(feats, i)
            if h is None:
                continue
            seen.add(fid)
            if _deck_level_hint(feats, i):
                continue
            try:
                lv = float(np.asarray(feats.attrs.get("level"))[i])
            except (TypeError, ValueError, IndexError):
                lv = 0.0
            if lv > 0:
                continue
            for part in feats.parts(i):
                if not part or len(part[0]) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                xy = _resample(np.column_stack((e, n)), STONE_STEP_M)
                if len(xy) < 2:
                    continue
                mid = (xy[:-1] + xy[1:]) / 2
                keep = (mid[:, 0] >= x0) & (mid[:, 0] < x1) & (mid[:, 1] >= y0) & (mid[:, 1] < y1)
                if not keep.any():
                    continue
                if roads:
                    blocked = _near_road(xy, roads)
                    keep &= ~(blocked[:-1] | blocked[1:])
                j = 0
                while j < len(keep):            # each run of kept segments is a piece
                    if not keep[j]:
                        j += 1
                        continue
                    k = j
                    while k < len(keep) and keep[k]:
                        k += 1
                    runs.append((xy[j:k + 1], h))
                    j = k
    if not runs:
        return []
    allxy = np.concatenate([r for r, _h in runs])
    lat, lon, _ = frame.enu_to_geodetic(allxy[:, 0], allxy[:, 1], np.zeros(len(allxy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    z = np.asarray(z, np.float64)
    out, k = [], 0
    for xy, h in runs:
        zz = z[k:k + len(xy)]
        k += len(xy)
        d = np.gradient(xy, axis=0)
        d /= np.maximum(np.hypot(*d.T), 1e-9)[:, None]
        nl = np.column_stack((-d[:, 1], d[:, 0]))
        top = zz + h
        out.append({"base": xy + nl * (STONE_THICK_M / 2), "toward": nl, "top": top, "back_top": top.copy(),
                    "foot": zz - STONE_FOOT_M, "thick": np.full(len(xy), STONE_THICK_M), "parapet": True,
                    "concrete": False, "mountain": False, "rock": False})
    return out


def tile_rows(frame, tx, ty, md, elev):
    """rows() for one road tile."""
    import roads
    box = roads.tile_bbox(tx, ty)

    def infra_fn(key):
        return md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")

    return rows(frame, infra_fn, box, elev)
