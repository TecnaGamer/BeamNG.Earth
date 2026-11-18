"""Racetracks: the track surface wins (road revamp R1k, .claude/work/road-revamp-gaps-2026-11-17.md G3; PR #992's rule,
GitHub #1220, #1352, #1350, #588). BNE_RACEWAY_DIG=on:

- no grade separation bridge on a raceway, and a mapped tunnel crossing under a raceway is never taken for an
  underpass (gradesep: the oval's surface was cut by a 16-19 m deck over its own infield tunnel, opened to the ground,
  at Daytona and Talladega);
- a road tunnel under a raceway is dug under the track's band: its floor lowered so the tube, its soil and a margin fit
  under the track's surface as drawn (grading.raceway_surface, smoothed along the track), the tube covered across the
  band and PORTAL_OUT_M past it, its ramps inside the tube at DIG_GRADE (DIG_MAX_GRADE at most from its portals, which
  stay where the approaches are): roads.TunnelProfiles._solve, dig_part; its portals carried off the band
  (roads._clear_portals); the ground over the tube on the band is the track's, never the bare ground kept over a
  tunnel (grading.Grader.apply);
- no props (poles, lamps, signs on posts, delineators, fences, ...) and no wires ending inside a raceway's carriageway
  + PROP_CLEAR_M (props on a deck over it kept), and trees kept PROP_CLEAR_M off it (vegetation).

A raceway is OpenStreetMap's highway=raceway, appended to the roads layer with ids "osm-raceway-<way id>"
(osmtags.raceway_features). Off by default while it is measured (tools/roadcorpus, category 19); every BNE_* switch is
in the plans' and cells' keys (revisit.switches_key). Not here (later road revamp phases): raceways on the AI graph
(decision D20), generated kerbs and run-off (D17, only as mapped by default), raceway profiles in the road model."""
import logging
import os

import numpy as np

import dnp

log = logging.getLogger("earthd.racedig")
ON = os.environ.get("BNE_RACEWAY_DIG", "off") == "on"
FID_PREFIX = "osm-raceway-"
BAND_MARGIN_M = 2.0         # m past a raceway's carriageway that is its band (kerbs, apron: grading.UNGRADED_MARGIN_M)
PROP_CLEAR_M = 2.0          # m past the carriageway no prop, pole or tree stands
PROP_DECK_M = 3.0           # m: a prop this far over the ground under it stands on a deck over the track (kept)
PORTAL_OUT_M = 3.0          # m past the band (and the tube's half width) the tube stays covered: its portal off the band
DIG_SOIL_M = 1.0            # m of soil over the vault under the track (roads.TUNNEL_SOIL_M)
DIG_SLACK_M = 0.5           # m more: the drawn surface vs the elevation the dig reads (zooms, the smoothing's grid)
DIG_GRADE = 0.12            # the dug floor's ramps in the tube (a car park ramp's)
DIG_MAX_GRADE = 0.25        # ... at most, down from a portal that cannot move (a tube too short keeps what fits),
DIG_EASE_M = 12.0           # its grade growing over this far from the portal (no kink where the road meets the ramp)
DIG_MAX_DROP_M = 12.0       # m the floor is lowered at most (bad data: rather a shallow tube than a pit)
MIN_TUBE_H = 2.5            # m: a tube under a track is no lower (a low arch's under the band when the dig falls short)
DIG_GAP_M = 30.0            # m: an open gap this short between covered stretches of a dug tube is covered too (track and
                            # pit lane side by side: no light well between)
MAX_PART_M = 400.0          # m: longer tunnel parts are tunnels of their own (a road through a hill under a circuit)
WINDOW_M = 150.0            # m of track either side of a tube the surface is read from (3 sigmas are 30 m)


def is_raceway(fid):
    return str(fid).startswith(FID_PREFIX)


def segments(lines):
    """(a (m, 2), b (m, 2), half width (m,)) of [(line (k, 2), half width)]."""
    a, b, h = [], [], []
    for xy, hw in lines:
        xy = np.asarray(xy, np.float64)[:, :2]
        if len(xy) < 2:
            continue
        a.append(xy[:-1])
        b.append(xy[1:])
        h.append(np.full(len(xy) - 1, float(hw)))
    if not a:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0)
    return np.vstack(a), np.vstack(b), np.concatenate(h)


def near(pts, lines, extra=0.0):
    """(n,) bool: points (n, 2) within their half width + extra of any of lines ([(line, half width)])."""
    pts = np.ascontiguousarray(np.asarray(pts, np.float64).reshape(-1, 2))
    a, b, h = segments(lines)
    if not len(pts) or not len(a):
        return np.zeros(len(pts), bool)
    import fast
    fast.require(0, "points_near_segments")
    return np.asarray(fast._ef.points_near_segments(pts, np.ascontiguousarray(a), np.ascontiguousarray(b),
                                                    np.ascontiguousarray(h + float(extra))), bool)


def bands(frame, cells):
    """[(line (k, 2) in frame, half width m)] of the raceways' carriageways on the ground in the roads layers cells,
    each way once (the first copy)."""
    import mapgeom
    import roads as roadsmod
    out, seen = [], set()
    for feats in cells:
        if feats is None or not len(feats) or "class" not in feats.attrs:
            continue
        cls = feats.attrs["class"]
        idx = [i for i in range(len(feats)) if str(cls[i]) == "raceway"]
        if not idx:
            continue
        specs = roadsmod.road_specs(feats)
        for i in idx:
            fid = str(feats.ids[i])
            if fid in seen or specs[i] is None:
                continue
            seen.add(fid)
            for ring in roadsmod.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                xy = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)
                if len(xy) >= 2:
                    out.append((xy, specs[i].width / 2))
    return out


# An assembly goes as one (furniture.assemblies; the user's reports 56, 59, 66, 70 at Daytona and Atlanta, 2026-11-17:
# a lit raceway's utility pole 0.7 m inside the band went, its cobra-head arm 7 m up was taken for a prop on a deck
# over the track and stayed, and of its three wires the one ending 0.8 m farther out stayed: an arm and one wire in the
# air). A wire with an end within a dropped support's reach, or whose own end support goes (this tile's or the
# next's: furniture.with_ends), goes with it (furniture.wire_cull; in Rust).


def clear_props(props, wires, lines, ground=None):
    """props (furniture rows [group, x, y, z, ...] in the frame) and wires (lists of [x, y, z] points, or (points,
    radius)) without those inside a raceway's carriageway + PROP_CLEAR_M (lines: bands()), in place; a prop standing
    PROP_DECK_M or more over the ground there (ground(x (n,), y (n,)) -> z, the graded ground; None: none kept) is on
    a deck over the track and stays. A row mounted on another (furniture.assemblies) goes or stays with it. Wires:
    those with an end on the band, at a dropped support or whose end's own support goes (furniture.wire_cull) go."""
    if not lines:
        return 0
    dropped = 0
    culled = []
    if props:
        xy = np.array([[float(r[1]), float(r[2])] for r in props], np.float64).reshape(-1, 2)
        inside = near(xy, lines, PROP_CLEAR_M)
        had = bool(inside.any())
        if inside.any() and ground is not None:
            k = np.flatnonzero(inside)
            gz = np.asarray(ground(xy[k, 0], xy[k, 1]), np.float64)
            pz = np.array([float(props[j][3]) for j in k], np.float64)
            inside[k[pz - gz >= PROP_DECK_M]] = False
        if had:                     # (an assembly as one: a pole's arm, a post's lens go or stay with it)
            import furniture
            inside = furniture.assembly_drop(inside, props)
        if inside.any():
            keep = [r for r, bad in zip(props, inside) if not bad]
            culled = [r for r, bad in zip(props, inside) if bad]
            dropped += len(props) - len(keep)
            props[:] = keep
    if wires:
        import furniture
        ends, which, sup = [], [], []
        for n_, w in enumerate(wires):
            pts = w[0] if isinstance(w, tuple) else w
            if len(pts) >= 1:
                own, mounted = furniture.wire_ends(w), furniture.wire_mounted(w)
                for k, p in enumerate((pts[0], pts[-1])):
                    if own and own[k] is not None:
                        sup.append((len(ends), own[k]))
                        if mounted[k] is not None:      # (its arm: the assembly as one, assembly_cull)
                            sup.append((len(ends), mounted[k]))
                    ends.append([float(p[0]), float(p[1])])
                which += [n_, n_]
        if ends:
            ends = np.array(ends, np.float64)
            bad = near(ends, lines, PROP_CLEAR_M)
            cut = np.zeros(len(ends), bool)
            if sup:                 # (each span's own supports judged as the props are, the next tile's pole too)
                q = np.array([[float(r[1]), float(r[2])] for _, r in sup], np.float64)
                gone = near(q, lines, PROP_CLEAR_M)
                if gone.any() and ground is not None:
                    k = np.flatnonzero(gone)
                    gz = np.asarray(ground(q[k, 0], q[k, 1]), np.float64)
                    pz = np.array([float(sup[j][1][3]) for j in k], np.float64)
                    gone[k[pz - gz >= PROP_DECK_M]] = False
                for (j, _), g in zip(sup, gone):
                    cut[j] |= bool(g)
            if culled or cut.any():
                bad |= furniture.wire_cull(ends, cut, culled)
            drop = {which[j] for j in np.flatnonzero(bad).tolist()}
            if drop:
                wires[:] = [w for n_, w in enumerate(wires) if n_ not in drop]
                dropped += len(drop)
    return dropped


def _window(line, lo, hi, reach):
    """The run of line (k, 2) resampled every grading.RACEWAY_STEP_M from its start (the stations grading's own
    smoothing reads) with its samples within reach m of the box lo..hi (one more each side), or None. By the resampled
    line, not its vertices: a long straight with its vertices far off still passes the box (Codex review of 47edf660)."""
    import grading
    dense, _st = grading.resample(np.asarray(line, np.float64)[:, :2], grading.RACEWAY_STEP_M)
    d = np.maximum(np.maximum(lo - dense, dense - hi), 0.0)
    ok = np.flatnonzero(dnp.hypot(d[:, 0], d[:, 1]) <= reach)
    if not len(ok):
        return None
    i0, i1 = max(0, int(ok[0]) - 1), min(len(dense), int(ok[-1]) + 2)
    return dense[i0:i1] if i1 - i0 >= 2 else None


def track_surface(frame, elev, races, pts, base):
    """(n,) geodetic m: the drawn surface of the raceways races ([(line, half width)]) at points pts (n, 2) on their
    bands, as grading.Grader.apply lays it (grading.raceway_surface, each raceway's own, read over WINDOW_M of track
    round the points: 3 sigmas are 30 m, so the window's own ends change nothing there), base (n,) elsewhere.
    elev(lat, lon) -> geodetic m (the elevation the tunnels read)."""
    import grading
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    base = np.asarray(base, np.float64)
    if not len(pts):
        return base.copy()
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    zones = []
    for line, hw in races:
        win = _window(line, lo, hi, WINDOW_M)
        if win is None:
            continue
        got = grading.raceway_surface(frame, elev, win, float(hw) + BAND_MARGIN_M)
        if got:
            zones.append(got)
    if not zones:
        return base.copy()
    # (no fade at the band's edge: the surface itself on the band; apply fades it to the ground over its last
    # BAND_MARGIN_M, between the two, so the lower of the two is the least the track can be)
    return np.asarray(grading.raceway_ground(pts[:, 0], pts[:, 1], base, zones, margin=1e-6), np.float64)


def _track_low(frame, elev, races, pts):
    """(n,) frame z: the least of the bare ground and the track's drawn surface at points pts (n, 2)."""
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    la, lo = np.asarray(la), np.asarray(lo)
    raw = np.asarray(elev(la, lo), np.float64)
    zg = np.minimum(raw, track_surface(frame, elev, races, pts, raw))
    return np.asarray(frame.geodetic_to_enu(la, lo, zg)[2], np.float64)


def dig_part(frame, elev, xy, s, floor, kinds, races, tube_hw, tube_h, full_h):
    """A tunnel part under a raceway dug (module doc): (floor, kinds, cap) or None when no raceway's band meets it.
    xy (k, 2), s (k,) its line and arc lengths, floor (k,) its solved floor (frame z, its ends the portals' or joints'),
    kinds its sections (0 open, 1 low arch, 2 full); races [(line, half width)] the raceways round it; tube_hw: its half
    width to the wall; tube_h: its low tube's least height (roads.TUNNEL_LOW_H / MAJOR_LOW_H), full_h a full tube's
    (roadmesh.TUNNEL_H); elev(lat, lon) -> geodetic m. cap (k,): the frame z the vault's soil must stay under (the
    track's surface less DIG_SOIL_M) on the band, +inf elsewhere.
    A dig that cannot give MIN_TUBE_H under the track everywhere on its band (a part too short for its depth from
    portals or joints that cannot move, the drop limit) builds no tube at all: the part is a road on the ground, the
    track's surface over the band (open, cap +inf): the road crosses the track at grade, as an opened underpass did
    before (Codex review of 47edf660: a 20 m part kept a 2.5 m vault 1.46 m over the track)."""
    xy = np.asarray(xy, np.float64)[:, :2]
    s = np.asarray(s, np.float64)
    floor = np.asarray(floor, np.float64)
    if not races or len(xy) < 2 or s[-1] > MAX_PART_M:
        return None
    on = near(xy, races, BAND_MARGIN_M + tube_hw)
    if not on.any():
        return None
    out = near(xy, races, BAND_MARGIN_M + tube_hw + PORTAL_OUT_M)
    # the track over the tube's whole footprint at each sample and half way to its neighbours: centre and both walls
    t = np.gradient(xy, axis=0)
    t /= np.maximum(dnp.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
    nrm = np.stack([-t[:, 1], t[:, 0]], axis=1)
    k = np.flatnonzero(on)
    prv = np.maximum(k - 1, 0)
    nxt = np.minimum(k + 1, len(xy) - 1)
    cen = np.concatenate([xy[k], (xy[k] + xy[prv]) / 2, (xy[k] + xy[nxt]) / 2])
    nr = np.concatenate([nrm[k], nrm[k], nrm[k]])
    foot = np.concatenate([cen, cen + nr * tube_hw, cen - nr * tube_hw])
    zt = _track_low(frame, elev, races, foot).reshape(-1, len(k)).min(axis=0)
    kinds = np.asarray(kinds, np.int64)
    th = np.where(kinds[k] == 2, float(full_h), float(tube_h))
    need = np.full(len(xy), np.inf)
    need[k] = np.maximum(zt - th - DIG_SOIL_M - DIG_SLACK_M, floor[k] - DIG_MAX_DROP_M)
    import fast
    fast.require(384, "race_dig_floor")
    f2, k2 = fast._ef.race_dig_floor(np.ascontiguousarray(s), np.ascontiguousarray(floor),
                                     np.ascontiguousarray(need), np.ascontiguousarray(out),
                                     np.ascontiguousarray(kinds), DIG_GRADE, DIG_MAX_GRADE, DIG_GAP_M,
                                     DIG_EASE_M)
    f2, k2 = np.asarray(f2, np.float64), np.asarray(k2, np.int64)
    cap = np.full(len(xy), np.inf)
    cap[k] = zt - DIG_SOIL_M
    if float(np.min(cap[k] - f2[k])) >= MIN_TUBE_H:
        return f2, k2, cap
    # (no room for a tube: the road on the ground, the track's own surface over the band)
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    la, lo = np.asarray(la), np.asarray(lo)
    raw = np.asarray(elev(la, lo), np.float64)
    ground = np.asarray(frame.geodetic_to_enu(la, lo, track_surface(frame, elev, races, xy, raw))[2], np.float64)
    ground[0], ground[-1] = floor[0], floor[-1]
    return ground, np.zeros(len(xy), np.int64), np.full(len(xy), np.inf)


def tube_height(th, cap, floor, a, b):
    """A section's tube height th (samples a..b inclusive of a part with dig_part's cap and the floor as dug) no taller
    than the room under the track (cap - floor) where the section passes under one: never through the track (dig_part
    leaves at least MIN_TUBE_H there, or no tube)."""
    room = np.asarray(cap[a:b + 1], np.float64) - np.asarray(floor[a:b + 1], np.float64)
    room = room[np.isfinite(room)]
    if not len(room):
        return th
    return float(min(th, float(room.min())))


SUPPORT_CELL_M = 60.0       # m round a span's support whose road cells are read for its bands (a raceway's half width,
                            # the band's margin and PROP_CLEAR_M, with room)


def support_cells(frame, wires):
    """{(x, y) of a support at the ends of wires (furniture.wire_ends): the map cells (keys) within SUPPORT_CELL_M of
    it}: a span is drawn by one tile and its far support may stand 1 km off, near a raceway in no road cell of this
    tile (Codex's review of 26580e0c)."""
    import furniture
    import mapcells
    pts = [(float(r[1]), float(r[2])) for w in wires or () for e in (furniture.wire_ends(w),) if e
           for r in e if r is not None]
    if not pts:
        return {}
    xy = np.array(sorted(set(pts)), np.float64)
    m = SUPPORT_CELL_M
    corners = [frame.enu_to_geodetic(xy[:, 0] + dx, xy[:, 1] + dy, 0.0)[:2]
               for dx, dy in ((-m, -m), (m, -m), (m, m), (-m, m))]
    la = np.stack([np.atleast_1d(np.asarray(c[0], np.float64)) for c in corners], 1)
    lo = np.stack([np.atleast_1d(np.asarray(c[1], np.float64)) for c in corners], 1)
    _, lo_mid, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    out = {}
    for k, (x, y) in enumerate(xy.tolist()):
        # (the box's longitudes round the support's own, wrapped: west of it to east of it, west > east across the
        # antimeridian, as cells_for_bbox takes them; min / max of +179.99 and -179.99 was the whole world)
        mid = float(np.atleast_1d(lo_mid)[k])
        rel = (lo[k] - mid + 180.0) % 360.0 - 180.0
        west = (mid + float(rel.min()) + 180.0) % 360.0 - 180.0
        east = (mid + float(rel.max()) + 180.0) % 360.0 - 180.0
        try:
            out[(x, y)] = mapcells.cells_for_bbox(float(la[k].min()), west, float(la[k].max()), east)
        except ValueError:          # (no sane box: its spans are judged unread, tile_clear)
            out[(x, y)] = [None]
    return out


def tile_clear(frame, cells, props, wires, elev, md=None):
    """clear_props for a road tile (frame; cells: its roads layers; elev(lat, lon) -> geodetic m, the graded ground):
    in place, nothing when off or the tile has no raceway. md: the map data, for the road cells round the supports at
    the far ends of the tile's spans (support_cells: the bands that cull them in their own tiles)."""
    if not ON or not (props or wires):
        return 0
    unread = set()
    if md is not None and wires:
        near_cells = support_cells(frame, wires)
        keys = sorted({k for ks in near_cells.values() for k in ks}, key=lambda k: (k is not None, str(k)))
        if keys:
            import mapcells
            got, bad = [], set()
            for k in keys:              # (one by one: a cell that cannot be read costs only the spans that need it)
                if k is None:
                    bad.add(k)
                    continue
                try:
                    got += [cd.layers.get("roads") for cd in mapcells.load_cells(md, [k], ("roads",))]
                except Exception as ex:     # (OSError, a decode error: the tile is still built, not kept)
                    bad.add(k)
                    log.warning("racedig: road cell %s round a span's support unread: %s: %s", k, type(ex).__name__,
                                ex)
            cells = list(cells) + got
            if bad:
                import revisit
                revisit.fault(here=True)
                unread = {xy for xy, ks in near_cells.items() if any(k in bad for k in ks)}
    lines = bands(frame, cells)
    dropped = 0
    if unread:                          # (a span whose support's band could not be read: left out, as if culled)
        import furniture
        keep = [w for w in wires if not any(r is not None and (float(r[1]), float(r[2])) in unread
                                            for r in (furniture.wire_ends(w) or ()))]
        dropped = len(wires) - len(keep)
        wires[:] = keep
    if not lines:
        return dropped

    def ground(x, y):
        la, lo, _ = frame.enu_to_geodetic(np.asarray(x, np.float64), np.asarray(y, np.float64), 0.0)
        la, lo = np.asarray(la), np.asarray(lo)
        return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)
    return dropped + clear_props(props, wires, lines, ground)

