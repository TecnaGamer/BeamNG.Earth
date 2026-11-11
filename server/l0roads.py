"""L0 roads: "fake it until it loads" for the ground under the painted roads (docs: the retro optimisation study's
section I, the fast world-gen study's proposal 4; the user, 2026-11-11: "I like the 'Fake it until it loads' idea, as
long as it doesn't slow down actual world gen").

A teleport's provisional block (earthd make_provisional / blockworker.build provisional) is the DEM's ground with the
roads painted in from the cached map cells, no grading: the painted roads carried the elevation data's bumps, a
hillside's side slope and a car or deck left in a lidar model, for the 20-120 s until the graded block (grading.py's
road plans, which wait on the neighbouring cells) replaces it. Here the same roads' corridors are flattened into the
provisional heights from those cells alone (no plans, no neighbour cells): each road's centreline heights in the block
smoothed with the grading's own per-class sigma (grading.profile_sigma, after a running median), its carriageway and
shoulder laid flat across, the ground eased back beyond (grading's T_MIN..T_MAX at 1:SLOPE_RUN). The Rust kernel
(earthfast.l0_flatten) costs tens of ms a block; the corridors' Python here about as much as the road paint's.

Bridges and tunnels are left out (roads.ground_parts, as the paint): a deck over a valley is not laid into it.
Replaced cleanly: it only changes the provisional block's heights, which the graded block replaces as a whole.
BNGE_L0_ROADS=0 turns it off (the provisional block as before).
"""
import logging
import math
import os
import time

import numpy as np

import fast

log = logging.getLogger("earthd.l0roads")

ON = os.environ.get("BNGE_L0_ROADS", "1") != "0"
# A teleport's first block whose aerial imagery is not on disk (a cold place) goes with a stand-in photo made from its
# own ground layers (ground_photo) instead of waiting up to earthd.BASE_NO_STANDIN_MAX_S more for the photo; the photo
# follows as block_base. BNGE_L0_GROUND=0: waited for as before
GROUND_ON = os.environ.get("BNGE_L0_GROUND", "1") != "0"
NEED = 219                  # earthfast.l0_flatten, earthfast.ground_photo
STEP_M = 2.0                # m between a road's profile stations
MARGIN_M = 160.0            # m round the block whose roads are read (a road's reach + its smoothing along it)
BLEND_POW = float(os.environ.get("BNGE_L0_BLEND_POW", "2"))   # roads of one class blend by 1 / (1 + (d / BLEND_R)^this)
BLUR_M = float(os.environ.get("BNGE_L0_BLUR_M", "3"))   # m: the targets rounded over this (kinks at junction edges)
JOIN_MAX_DZ_M = 1.5         # m: a road end this near a main road's height takes it (a bigger step: another level, a driveway
                            # ramp, left as it is; the grading's plans know more and snap up to JUNCTION_MAX_M)


def _params(spec):
    """(core half width m, sigma m, median window m, rank) of a road spec: the grading's own rules (grading.py)."""
    import grading
    import roads as roadsmod
    core = float(spec.width) / 2 + grading.shoulder(spec)
    sigma = grading.profile_sigma(spec)
    median = grading.MEDIAN_M if spec.kind in roadsmod.BUILT else grading.DIRT_MEDIAN_M
    rank = float(grading.JUNCTION_RANK.get(spec.cls, 0)) - (0.5 if getattr(spec, "link", False) else 0.0)
    return core, float(sigma), float(median), max(rank, 0.0)


def corridors(frame, feats_list, urban_fn, lat, lon):
    """The ground roads of feats_list (the "roads" Features of the cells round the block, as surface.paint_roads takes
    them) near the block's points (lat, lon): (pts (m, 2) ENU of frame, starts (k + 1,), core, sigma, median, rank (k,)),
    or None when there are none."""
    import roads as roadsmod
    import surface as surfacemod
    la0, la1 = float(np.min(lat)), float(np.max(lat))
    lo0, lo1 = float(np.min(lon)), float(np.max(lon))
    m_lat = MARGIN_M / 111320.0
    m_lon = m_lat / max(math.cos(math.radians((la0 + la1) / 2)), 1e-6)
    la0, la1, lo0, lo1 = la0 - m_lat, la1 + m_lat, lo0 - m_lon, lo1 + m_lon
    rings, params = [], []
    seen = set()
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        specs = roadsmod.road_specs(feats, urban_fn, None)
        box = surfacemod._lonlat_boxes(feats)
        near = np.flatnonzero((box[:, 1] >= lo0) & (box[:, 0] <= lo1) & (box[:, 3] >= la0) & (box[:, 2] <= la1))
        ids = feats.ids
        for i in near.tolist():
            sp = specs[i]
            if sp is None:
                continue
            fid = str(ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            prm = None
            for ring in roadsmod.ground_parts(feats, i):     # (not bridges / tunnels)
                ring = np.asarray(ring, np.float64)
                if len(ring) < 2:
                    continue
                if prm is None:
                    prm = _params(sp)
                rings.append(ring[:, :2])
                params.append(prm)
    if not rings:
        return None
    ll = np.concatenate(rings)
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
    pts = np.ascontiguousarray(np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1))
    starts = np.zeros(len(rings) + 1, np.int64)
    starts[1:] = np.cumsum([len(r) for r in rings])
    p = np.asarray(params, np.float64)
    return (pts, starts, np.ascontiguousarray(p[:, 0]), np.ascontiguousarray(p[:, 1]), np.ascontiguousarray(p[:, 2]),
            np.ascontiguousarray(p[:, 3]))


def flatten_grid(u, xs, ys, cor):
    """u ([row = ys index, col = xs index], heights on the regular lattice xs, ys) with the corridors cor (corridors())
    flattened in, in place. Returns (samples changed, largest change m, road ends joined to a higher class's road)."""
    import grading
    fast.require(NEED, "l0_flatten")
    import earthfast
    if cor is None:
        return 0, 0.0, 0
    pts, starts, core, sigma, median, rank = cor
    sq = float(xs[1] - xs[0]) if len(xs) > 1 else 1.0
    return earthfast.l0_flatten(u, float(xs[0]), float(ys[0]), sq, pts, starts, core, sigma, median, rank,
                                float(grading.T_MIN), float(grading.T_MAX), float(grading.SLOPE_RUN), STEP_M,
                                float(grading.JUNCTION_REACH_M), JOIN_MAX_DZ_M,
                                float(grading.JUNCTION_EASE_GRADE), float(grading.JUNCTION_TAPER_M),
                                float(grading.JUNCTION_EASE_MIN_M), float(grading.BLEND_R), BLEND_POW, BLUR_M)


def post_for(frame, surface, cx, cy, size, stages=None):
    """heightmap.generate's post hook of a provisional block: its roads (surface.Surface.roads: the cached cells only,
    never waits) flattened into its heights u. stages (a dict) gets "l0roads" (s) and "l0Samples". None when L0 roads
    are off or there is no surface (no roads read)."""
    if not ON or surface is None:
        return None
    import heightmap

    def post(u, keep):
        t0 = time.perf_counter()
        try:
            feats, _ = surface.roads(keep["lat"], keep["lon"])
            cor = corridors(frame, feats, surface.urban, keep["lat"], keep["lon"]) if feats else None
            xs, ys = heightmap.sample_coords(cx, cy, size)
            n, dmax, joins = flatten_grid(u, xs, ys, cor) if cor is not None else (0, 0.0, 0)
        except fast.NativeMissing:
            raise
        except Exception as e:      # the block as before rather than none
            log.warning("L0 roads of block (%s, %s): %s: %s", cx, cy, type(e).__name__, e)
            n, dmax, joins = 0, 0.0, 0
        if stages is not None:
            stages["l0roads"] = time.perf_counter() - t0
            stages["l0Samples"] = float(n)
            stages["l0MaxDz"] = float(dmax)
            stages["l0Joins"] = float(joins)
        return u
    return post


# The ground stand-in's colours by terrain material (aerial-photo tones: a summer photo's grass is olive, not the
# material's own green); the level's dirt by climate where the land cover is not on disk yet (all of it dirt then:
# "the entire ground now turns brown", the imagery's fallback over a whole block, 2026-10-30)
GROUND_RGB = {
    "bngEarthGroundAsphalt": (78, 78, 80), "bngEarthGroundGrass": (92, 106, 64), "bngEarthGroundDusty": (156, 141, 112),
    "bngEarthGroundLoose": (146, 136, 118), "bngEarthGroundRock": (122, 118, 110), "bngEarthGroundSnow": (232, 236, 240),
    "bngEarthGroundMud": (96, 88, 72), "bngEarthGroundCobble": (116, 110, 102), "bngEarthGroundLawn": (88, 112, 62),
    "bngEarthGroundGolfRough": (86, 108, 58), "bngEarthGroundGolfFairway": (96, 128, 66),
    "bngEarthGroundGolfGreen": (100, 136, 70), "bngEarthGroundGolfBunker": (214, 200, 160),
    "beamngEarthDirt": (126, 118, 102),
}
DIRT_BY_CLIMATE = {"A": (86, 104, 62), "B": (170, 148, 112), "C": (100, 108, 72), "D": (98, 104, 74), "E": (220, 224, 228)}
GROUND_SHADE = 0.6          # the slope shading's strength (1: plain Lambert)


def ground_palette(names, lat, lon, land_known):
    """(k, 3) uint8: the stand-in colour of each of a .ter's materials (names); the level's dirt by the Koppen class at
    (lat, lon) when the land cover is not known (land_known False)."""
    import climate
    dirt = GROUND_RGB["beamngEarthDirt"]
    if not land_known:
        k = climate.koppen_at(lat, lon)
        dirt = DIRT_BY_CLIMATE["A" if 1 <= k <= 3 else "B" if k <= 7 else "C" if k <= 16 else "D" if k <= 28 else
                               "E" if k <= 30 else "C"]
    out = [dirt if (nm == "beamngEarthDirt") else GROUND_RGB.get(nm, dirt) for nm in names]
    return np.asarray(out or [dirt], np.uint8)


def read_ter(path):
    """(heights u16 (n, n), layer indices u8 (n, n), material names) of a .ter (heightmap.write_ter), rows as there
    (row 0 = south)."""
    import struct
    with open(path, "rb") as f:
        data = f.read()
    n = struct.unpack_from("<I", data, 1)[0]
    h = np.frombuffer(data, np.uint16, n * n, 5).reshape(n, n)
    off = 5 + 2 * n * n
    lay = np.frombuffer(data, np.uint8, n * n, off).reshape(n, n)
    o = off + n * n
    names = []
    cnt = struct.unpack_from("<I", data, o)[0]
    o += 4
    for _ in range(cnt):
        ln = data[o]
        names.append(data[o + 1:o + 1 + ln].decode())
        o += 1 + ln
    return h, lay, names


def ground_photo(ter_path, q, lat, lon, land_known, px, square):
    """The stand-in photo (px, px, 3) uint8, row 0 = north, of a block from its .ter: each sample's material colour,
    slope-shaded (earthfast.ground_photo). The .ter's holes (its edge ring) take their neighbour's colour."""
    fast.require(NEED, "ground_photo")
    import earthfast
    h, lay, names = read_ter(ter_path)
    pal = ground_palette(names, lat, lon, land_known)
    edge = np.array(lay)
    edge[0, :], edge[-1, :], edge[:, 0], edge[:, -1] = edge[1, :], edge[-2, :], edge[:, 1], edge[:, -2]
    z = np.ascontiguousarray(h.astype(np.float32) * np.float32(q))
    return earthfast.ground_photo(np.ascontiguousarray(edge), z, pal, float(square), int(px), GROUND_SHADE)
