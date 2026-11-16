"""Ground tram track (rails.py's "tram" kind): glue to the kernel's tram module (native/earthfast/src/tram/, plan 046).

BNE_TRAM=1 (off: rails.py draws as before). Before it, a tram was drawn by the same rule as a main line: street track only
inside a carriageway's drawn width, else a gravel bed with white concrete sleepers. A tram lane or a parking strip is not in
that width (Prague, Na Slupi, 2026-11-06: one track of a pair across the road as white stripes on the asphalt), the rails
were always 1.435 m apart (Vinnytsia's are 1.0 m), and the sleepers were as white as concrete. Now:
  - a tram within STREET_MARGIN_M of a carriageway's edge lies in the street: a paved strip (paving: asphalt, slabs in the
    ex-USSR) with two rails on it, no sleepers, no bed;
  - the rails' gauge is the line's OSM gauge (railways.osm_gauge), the decal's width scaled with it;
  - open track gets a bed and rails of our own texture, no sleepers: a tram's sleepers are under the ballast or paving.
The rules and numbers live in the kernel (tram/track.rs); this module reads its constants and passes the features' arrays.
Pure given its inputs."""
import os

import numpy as np

import fast

ENABLED = os.environ.get("BNE_TRAM", "0") == "1"
STREET_TEX = os.environ.get("BNE_TRAM_STREET_TEX", "on") != "off"      # the user's strips with the rails baked in (off: the old
                                                                       # asphalt / slab paving with a separate rail ribbon)
SWITCH3D = os.environ.get("BNE_TRAM_SWITCH3D", "on") != "off"       # turnouts of the 3D track drawn as turnouts (stage F)
TRACK3D = os.environ.get("BNE_TRAM_TRACK3D", "on") != "off"           # open track as a 3D mesh (railways.tile_mesh, stage D)
CORRIDOR = os.environ.get("BNE_TRAM_CORRIDOR", "on") != "off"           # nothing stands on the rails: poles, signs, benches, walls
RAIL_PHYS = os.environ.get("BNE_TRAM_RAIL_PHYS", "on") != "off"          # the street rails raised in the collision mesh (stage C)
MINOR_CLASSES = frozenset(("residential", "living_street", "service", "unclassified", "pedestrian"))   # setts streets
KERNEL = 310                # the kernel version with the tram track (tram/track.rs: the parts chained, plan 046 stage F)
BED_KERNEL = 310            # ... and with its bed (tram/profile.rs)
PHYS_KERNEL = 310           # ... and the street rails' physics strips (tram/physmesh.rs)
MESH_KERNEL = 310           # ... and the 3D track's mesh, deck paving, road deck height (tram/mesh.rs)
CORRIDOR_KERNEL = 310        # ... and the corridor (tram/corridor.rs)
FOLLOW_KERNEL = 310         # ... and the side streets that follow it (tram_follow)
_CONSTS = {}


def __getattr__(name):
    """The kernel's constants (STREET_MARGIN_M, GAUGE_DEFAULT_M, GAUGE_MIN_M, GAUGE_MAX_M, DECAL_W_M, PAVING_EXTRA_M,
    RAIL_W_M, and the looks RAIL, ASPHALT, SLAB), read once on first use."""
    if name.startswith("__"):
        raise AttributeError(name)
    if not _CONSTS:
        fast.require(BED_KERNEL, "tram_track_consts")
        _CONSTS.update(fast._ef.tram_track_consts())
        _CONSTS.update(fast._ef.tram_bed_consts())
        _CONSTS.update(fast._ef.tram_corridor_consts())
    if name in _CONSTS:
        return _CONSTS[name]
    raise AttributeError(f"module 'tramtrack' has no attribute {name!r}")


def gauge(osm=None):
    """Gauge in m: osm (railways.osm_gauge's) inside GAUGE_MIN_M..GAUGE_MAX_M, else GAUGE_DEFAULT_M."""
    fast.require(KERNEL, "tram_gauge")
    return fast._ef.tram_gauge(None if osm is None else float(osm))


def su(country=None):
    """Whether a street track lies in concrete slabs: the ex-USSR (railxing.style_of "su": Vinnytsia's track is in
    slabs); elsewhere in the road's asphalt (the data has no surface tag)."""
    import railxing
    return railxing.style_of(country) == "su"


def paving(country=None):
    """What a track in the street lies in: the look {m, tl, p, layer} of the slabs or the road's asphalt."""
    fast.require(KERNEL, "tram_paving")
    return dict(fast._ef.tram_paving(su(country)))


def paving_width(g):
    fast.require(KERNEL, "tram_paving_width")
    return fast._ef.tram_paving_width(float(g))


def segments(lines_hw):
    """(a (m, 2), b (m, 2), hw (m,)): the segments of [(polyline (k, 2), half width)], as the kernel reads them."""
    segs, hw = fast.segments(lines_hw)
    return np.ascontiguousarray(segs[:, :2]), np.ascontiguousarray(segs[:, 2:]), hw


def in_street(pts, ways, on_ways=None):
    """Per point of pts (n, 2): in the street. ways: {key: (centreline (m, 2), half width)} of the carriageways (on_ways: the
    old callback of the Python version, unused: the kernel tests them)."""
    pts = np.ascontiguousarray(np.asarray(pts, np.float64).reshape(-1, 2))
    if not ways or not len(pts):
        return np.zeros(len(pts), bool)
    fast.require(KERNEL, "tram_in_street")
    a, b, hw = segments(ways.values())
    return np.asarray(fast._ef.tram_in_street(pts, a, b, hw), bool)


def ground_chunks(parts, box, carriageways, eligible, bw, country=None, minor=(), cobble=()):
    """(bed chunks, rail chunks) of the ground tram parts [(xy (k, 2), gauge)] in road tile box (x0, y0, x1, y1): the paved
    strip or the bed, and two rails on each run (none on the strips that have them in the picture), parts that meet end
    to end as one line. carriageways, eligible: {key: (centreline (m, 2), half width)} (eligible: the ones whose drawn
    width counts as the street itself); minor: the keys of carriageways that are minor streets (MINOR_CLASSES: setts in
    Central Europe); cobble: the keys paved with sets or cobbles by the map (setts anywhere). bw: the open track's bed width."""
    fast.require(KERNEL, "tram_ground_chunks")
    xys = [np.asarray(p[0], np.float64)[:, :2] for p in parts]
    off = np.r_[0, np.cumsum([len(x) for x in xys], dtype=np.int64)].astype(np.int64)
    a, b, hw = segments(carriageways.values())
    ea, eb, ehw = segments(eligible.values())
    minor, cobble = set(minor), set(cobble)
    tags = np.concatenate([np.full(len(ln) - 1, (1 if key in minor else 0) | (2 if key in cobble else 0), np.uint8)
                           for key, (ln, _h) in carriageways.items() if len(ln) >= 2] or [np.zeros(0, np.uint8)])
    return fast._ef.tram_ground_chunks(np.ascontiguousarray(np.vstack(xys)), off,
                                       np.array([float(p[1]) for p in parts], np.float64), [float(v) for v in box],
                                       a, b, hw, ea, eb, ehw, np.ascontiguousarray(tags), float(bw), su(country),
                                       str(country or ""), STREET_TEX, TRACK3D, SWITCH3D)


def corridor_parts(frame, cells, box, reach=12.0):
    """[(xy, gauge)] of the ground tram parts within reach of box that are in use (not abandoned): the corridor's lines."""
    if not (ENABLED and CORRIDOR):
        return []
    import railways
    worn = {}
    return [(xy, GAUGE_DEFAULT) for fid, _c, kind, xy in railways.parts(frame, cells, box, reach=reach, worn=worn, trams="only")
            if kind == "ground" and fid not in worn]


GAUGE_DEFAULT = 1.435       # (the corridor's lines only chain parts: the gauge is not used)


def _c(name):
    """One of the kernel's constants."""
    return __getattr__(name)


def corridor_lines(parts):
    """[line (k, 2)]: the corridor lines of the ground tram parts [(xy, gauge)]: ways that meet end to end one line, every 2 m
    (tram/corridor.rs)."""
    if not parts:
        return []
    fast.require(CORRIDOR_KERNEL, "tram_corridor_lines")
    xys = [np.asarray(p[0], np.float64)[:, :2] for p in parts]
    off = np.r_[0, np.cumsum([len(x) for x in xys], dtype=np.int64)].astype(np.int64)
    return [np.asarray(l, np.float64) for l in fast._ef.tram_corridor_lines(
        np.ascontiguousarray(np.vstack(xys)), off, np.array([float(p[1]) for p in parts], np.float64))]


def corridor_hits(pts, parts, half=None):
    """(n,) bool: which points pts (n, 2) lie within half (default KEEP_M) of a ground tram's axis (tram/corridor.rs)."""
    pts = np.ascontiguousarray(np.asarray(pts, np.float64).reshape(-1, 2)[:, :2])
    if not parts or not len(pts):
        return np.zeros(len(pts), bool)
    fast.require(CORRIDOR_KERNEL, "tram_corridor_hits")
    xys = [np.asarray(p[0], np.float64)[:, :2] for p in parts]
    off = np.r_[0, np.cumsum([len(x) for x in xys], dtype=np.int64)].astype(np.int64)
    return np.asarray(fast._ef.tram_corridor_hits(pts, np.ascontiguousarray(np.vstack(xys)), off,
                                                  np.array([float(p[1]) for p in parts], np.float64),
                                                  float(_c("KEEP_M") if half is None else half)), bool)


def street_runs(parts, box, carriageways, eligible):
    """[(run (k, 2), gauge)]: the stretches of the ground tram parts [(xy, gauge)] in road tile box that lie in the street
    (the paving's rule), parts that meet end to end as one line (tram/physmesh.rs)."""
    fast.require(PHYS_KERNEL, "tram_street_runs")
    xys = [np.asarray(p[0], np.float64)[:, :2] for p in parts]
    off = np.r_[0, np.cumsum([len(x) for x in xys], dtype=np.int64)].astype(np.int64)
    a, b, hw = segments(carriageways.values())
    ea, eb, ehw = segments(eligible.values())
    return [(np.asarray(r, np.float64), float(g), bool(ts), bool(te)) for r, g, ts, te in fast._ef.tram_street_runs(
        np.ascontiguousarray(np.vstack(xys)), off, np.array([float(p[1]) for p in parts], np.float64),
        [float(v) for v in box], a, b, hw, ea, eb, ehw)]


def open_runs(parts, near, carriageways, eligible, country=None, minor=(), cobble=()):
    """[(run (k, 2), gauge)]: the open (unpaved) stretches of the ground tram parts [(xy, gauge)] that the 3D track draws
    (tile_mesh), within near (x0, y0, x1, y1): none where BNE_TRAM_TRACK3D is off or the track is a green one."""
    if not TRACK3D or not parts:
        return []
    fast.require(PHYS_KERNEL, "tram_open_runs")
    xys = [np.asarray(p[0], np.float64)[:, :2] for p in parts]
    off = np.r_[0, np.cumsum([len(x) for x in xys], dtype=np.int64)].astype(np.int64)
    a, b, hw = segments(carriageways.values())
    ea, eb, ehw = segments(eligible.values())
    return [(np.asarray(r, np.float64), float(g), bool(rs), bool(re), info) for r, g, rs, re, info in fast._ef.tram_open_runs(
        np.ascontiguousarray(np.vstack(xys)), off, np.array([float(p[1]) for p in parts], np.float64),
        [float(v) for v in near], a, b, hw, ea, eb, ehw, su(country), str(country or ""), STREET_TEX, TRACK3D, SWITCH3D)]


def open_heights(s, zg, zs, ramp_start, ramp_end):
    """(ballast top, rail head top, sleeper top, first, last) of a 3D open run at arcs s (n,) over the ground zg and its
    smoothing zs: the track on the ground, ramped to the ground where an end meets a strip (tram/track.rs open_heights);
    first / last: the arcs sleepers may lie between."""
    fast.require(PHYS_KERNEL, "tram_open_heights")
    top, rail, sleeper, lo, hi = fast._ef.tram_open_heights(
        np.ascontiguousarray(s, np.float64), np.ascontiguousarray(zg, np.float64), np.ascontiguousarray(zs, np.float64),
        bool(ramp_start), bool(ramp_end))
    return np.asarray(top), np.asarray(rail), np.asarray(sleeper), float(lo), float(hi)


def sleeper_reach(centres, axes, branch, reach=8.0):
    """(n,) how far each sleeper (centre, unit long axis) must reach along its axis to a branch's line: the signed distance to
    the nearest crossing, 0 where none (tram/switches.rs ray_reach)."""
    fast.require(PHYS_KERNEL, "tram_sleeper_reach")
    return np.asarray(fast._ef.tram_sleeper_reach(np.ascontiguousarray(centres, np.float64), np.ascontiguousarray(axes, np.float64),
                                                  np.ascontiguousarray(branch, np.float64), float(reach)), np.float64)


def _lines_off(lines):
    """(points (m, 2), offsets (k + 1,)) of polylines end to end."""
    xys = [np.asarray(l, np.float64).reshape(-1, 2)[:, :2] for l in lines]
    off = np.r_[0, np.cumsum([len(x) for x in xys], dtype=np.int64)].astype(np.int64)
    return (np.ascontiguousarray(np.vstack(xys)) if xys else np.zeros((0, 2))), off


def road_deck_z(xy, decks):
    """(n,) the surface height of the road deck under each point of xy (n, 2) of decks [(xy (k, 2), z (k,)[, width])], NaN
    where none is within DECK_NEAR_M (tram/mesh.rs)."""
    fast.require(MESH_KERNEL, "tram_road_deck_z")
    good = [(np.asarray(d[0], np.float64).reshape(-1, 2), np.asarray(d[1], np.float64).ravel()) for d in decks]
    good = [(p, z[:len(p)]) for p, z in good if len(p) >= 2 and len(z) >= len(p)]
    pts, off = _lines_off([p for p, _z in good])
    z = np.ascontiguousarray(np.concatenate([z for _p, z in good])) if good else np.zeros(0)
    return np.asarray(fast._ef.tram_road_deck_z(np.ascontiguousarray(np.asarray(xy, np.float64).reshape(-1, 2)), pts, z,
                                                off), np.float64)


def rides_deck(line, decks, share, step):
    """Whether a tram's bridge piece (line (n, 2)) lies on a road deck: at least share of it, sampled every step m, within
    DECK_NEAR_M of one of decks [line (m, 2+)] (tram/mesh.rs)."""
    fast.require(MESH_KERNEL, "tram_rides_deck")
    pts, off = _lines_off([np.asarray(d, np.float64)[:, :2] for d in decks if len(d) >= 2])
    return bool(fast._ef.tram_rides_deck(np.ascontiguousarray(np.asarray(line, np.float64)[:, :2]), pts, off, float(share),
                                         float(step)))


def open_bed(zg):
    """(n,) the bed line of a 3D open tram part over the ground zg at its points (tram/mesh.rs)."""
    fast.require(MESH_KERNEL, "tram_open_bed")
    return np.asarray(fast._ef.tram_open_bed(np.ascontiguousarray(zg, np.float64)), np.float64)


def deck_paving(p, uu, t, v, half_g):
    """(P, T, UV, tag) of the paving of a street tram on a road bridge's deck (tram/mesh.rs)."""
    fast.require(MESH_KERNEL, "tram_deck_paving")
    return fast._ef.tram_deck_paving(np.ascontiguousarray(p, np.float64)[:, :2], np.ascontiguousarray(uu, np.float64)[:, :2],
                                     np.ascontiguousarray(t, np.float64), np.ascontiguousarray(v, np.float64), float(half_g))


def open_mesh(xy, gauge, zg, cut, inside, sw, ramps, info):
    """The 3D track of one open tram part (tram/mesh.rs open_part): ((P, T, UV, tag) its rails, [(centres, along, across, z0,
    z1, hx, hy, tag)] its sleepers and frogs, (P, T, UV, tag) its collider). cut / inside / sw: per point, not drawn / in the
    tile / in a switch; ramps: (start, end) meet a strip; info: tram_open_runs' turnout dict."""
    fast.require(MESH_KERNEL, "tram_open_mesh")
    b = info.get("branch")
    marks = [(float(m["s0"]), float(m["s1"]), None if m["frog"] is None else (float(m["frog"][0]), float(m["frog"][1])),
              float(m["frog_s"]), np.ascontiguousarray(m["branch"], np.float64)) for m in info.get("marks") or ()]
    return fast._ef.tram_open_mesh(np.ascontiguousarray(xy, np.float64)[:, :2], float(gauge),
                                   np.ascontiguousarray(zg, np.float64), [bool(v) for v in cut], [bool(v) for v in inside],
                                   [bool(v) for v in sw], bool(ramps[0]), bool(ramps[1]),
                                   None if b is None else (float(b["zone"]), float(b["div"])), marks)


def physics_mesh(run, gauge, ground, taper_start=False, taper_end=False):
    """(vertices (n, 3), triangles (m, 3)) of the rails' physics strips along a street run (xy (k, 2)): the sample points,
    ground(xy (n, 2)) -> the road surface's heights there, the strips over them, rising over 0.3 m from a true end
    (tram/physmesh.rs)."""
    fast.require(PHYS_KERNEL, "tram_physics_mesh")
    pts = np.ascontiguousarray(fast._ef.tram_physics_points(np.ascontiguousarray(run, np.float64), bool(taper_start),
                                                            bool(taper_end)))
    z = np.ascontiguousarray(np.asarray(ground(pts), np.float64).ravel())
    v, t = fast._ef.tram_physics_mesh(pts, z, float(gauge), bool(taper_start), bool(taper_end))
    return np.asarray(v, np.float64), np.asarray(t, np.int64)


def beds(parts, roads):
    """The bed heights of a cell's ground tram parts: one array per part of parts [(xy (n, 2), zd (n,))] (zd: the DEM's
    heights under it), among roads [(centreline (k, 2), plan heights (k,), carriageway half width)]: held to the roads'
    surface near them, the DEM's trend (smoothed) away from them, at a tram's grade at most, and the parts that meet at one
    height there (tram/profile.rs)."""
    if not parts:
        return []
    fast.require(BED_KERNEL, "tram_beds")
    xys = [np.asarray(p[0], np.float64)[:, :2] for p in parts]
    poff = np.r_[0, np.cumsum([len(x) for x in xys], dtype=np.int64)].astype(np.int64)
    lines = [(np.asarray(r[0], np.float64)[:, :2], np.asarray(r[1], np.float64)[:len(r[0])], float(r[2])) for r in roads
             if len(r[0]) >= 2]
    off = np.r_[0, np.cumsum([len(r[0]) for r in lines], dtype=np.int64)].astype(np.int64)
    pts = np.ascontiguousarray(np.vstack([r[0] for r in lines])) if lines else np.zeros((0, 2))
    zs = np.ascontiguousarray(np.concatenate([r[1] for r in lines])) if lines else np.zeros(0)
    hw = np.array([r[2] for r in lines], np.float64)
    z = np.asarray(fast._ef.tram_beds(np.ascontiguousarray(np.vstack(xys)),
                                      np.ascontiguousarray(np.concatenate([np.asarray(p[1], np.float64) for p in parts])),
                                      poff, pts, zs, off, hw), np.float64)
    return [z[poff[k]:poff[k + 1]] for k in range(len(parts))]


def follow(roads, beds):
    """The heights (one array per road) of roads [(centreline (k, 2), plan heights (k,), class)] brought to the tram beds
    [(line (n, 2), bed heights (n,))] they cross at grade: a ramp (4-6 % by class) to the track and from it, never a crest
    or a sag (tram/profile.rs follow_roads). A road that crosses none keeps its heights."""
    if not roads or not beds:
        return [np.asarray(r[1], np.float64) for r in roads]
    fast.require(FOLLOW_KERNEL, "tram_follow")
    rl = [(np.asarray(r[0], np.float64)[:, :2], np.asarray(r[1], np.float64)[:len(r[0])], str(r[2])) for r in roads]
    ro = np.r_[0, np.cumsum([len(r[0]) for r in rl], dtype=np.int64)].astype(np.int64)
    bl = [(np.asarray(b[0], np.float64)[:, :2], np.asarray(b[1], np.float64)[:len(b[0])]) for b in beds]
    bo = np.r_[0, np.cumsum([len(b[0]) for b in bl], dtype=np.int64)].astype(np.int64)
    z = np.asarray(fast._ef.tram_follow(np.ascontiguousarray(np.vstack([r[0] for r in rl])),
                                        np.ascontiguousarray(np.concatenate([r[1] for r in rl])), ro,
                                        [r[2] for r in rl], np.ascontiguousarray(np.vstack([b[0] for b in bl])),
                                        np.ascontiguousarray(np.concatenate([b[1] for b in bl])), bo), np.float64)
    return [z[ro[k]:ro[k + 1]] for k in range(len(rl))]
