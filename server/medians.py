"""Median barriers on divided highways (the user, 2026-10-28: "Do the median barriers, they're mostly missing anyway").

Mapped barriers come first (guardrails.forest_rows draws OpenStreetMap's guard_rail / jersey_barrier / cable_barrier
lines); they are rare along medians (none along I-95 in Baltimore, the A2 near Utrecht or the E4 near Uppsala,
2026-10-28), so where none is mapped a rule puts one down the median of every motorway and expressway trunk whose two
one-way carriageways run side by side: its type by the median's width (pavement edge to edge, as drawn) and the
country's practice (STYLES, from aerials and street photos of real places), none where the median is wide.
Openings: no module within OPENING_M of any other road's pavement (median crossovers, ramps and their gores, crossing
roads), none in any carriageway, none in tunnels (only ground parts and decks carry one). On a bridge only where its
two decks are one slab (roadmesh.MEDIAN_JOIN_M: no parapets between them), at the decks' height; decks further apart
have their own parapets.

The geometry is native/earthfast's median_modules / median_rows (Rust); this is the glue. The rows are the guard
rails' Forest rows (guardrails.GROUPS): the stock jersey barrier and its sloped end, the mod's cable barrier
(tools/build_median_props.py), Italy's guard rail (back to back, or one along each edge); they collide as the guard
rails do (trees.lua's near items).
"""
import hashlib

import numpy as np

import fast

# barrier types (medians.rs): none, a concrete New Jersey wall, a cable barrier (four ropes on posts), a double-sided
# steel guard rail (two W-beams back to back at the middle), two single steel rails (one along each carriageway's edge,
# 0.75 m outside it, their backs to the median)
NONE, JERSEY, CABLE, STEEL, RAILS = 0, 1, 2, 3, 4
MAX_GAP_M = 22.0        # m: no pair looked for past this median width
OPENING_M = 4.0         # m kept clear around any other road's pavement (crossovers, ramps, gores, crossing roads)
MAPPED_REACH_M = 1.5    # m: a mapped barrier this near the median's barrier line (or within half the median) stands for it
MODULE_M = 3.0          # m: the models' length (the stock jersey barrier, the cable module, Italy's rail)
EDGE_ALLOW_M = 0.15     # m of a paired carriageway's drawn edge (outside its edge line) the barrier may stand on ...
SHOULDER_KEEP_M = 0.5   # ... or of its shoulder less this
WIDE_LANES = 3          # lanes a direction (or, unmapped, WIDE_M of carriageway): a rebuilt six-lane motorway
WIDE_M = 14.0

# Rows (max median width m: the first row the median, pavement edge to edge as drawn, is not wider than; past the last
# none) -> the type of: a rural motorway, an urban motorway, a rural motorway of WIDE_LANES or more, a rural trunk, an
# urban trunk (an expressway: a trunk with sidewalks is a street with a curbed median, none). From aerials (ortho_hi)
# and Mapillary photos of 22 places, 2026-10-28 (widths edge to edge):
#   US/Canada: I-65 / I-75 Kentucky rebuilt to six lanes (Bonnieville, Bowling Green, Berea): a tall concrete wall in
#     ~8 m of paved median (our gap reads 12-17 m there: the drawn carriageways are narrower than three lanes and
#     shoulders); I-65 Louisville 2.7 m, I-95 Baltimore 3-8 m paved, I-10 Phoenix ~6 m paved: concrete; I-95 Harford Co.
#     60 m, I-95 Virginia 29 m, I-10 Picacho 36 m, I-80 Laramie 38 m: none; US-13 Salisbury (divided trunk, 11 m
#     grass): a single W-beam on one side (most US rural divided trunks have none: none here). Cable barriers in grass
#     medians of four-lane interstates up to 50 ft (15 m: AASHTO's Roadside Design Guide warrants a median barrier
#     under 30 ft and weighs one to 50 ft): US practice, not seen in the sample.
#   Germany / Netherlands / UK (the north): A9 Denkendorf 3-4 m grass, A12 Arnhem ~6.5 m, M1 Leicester ~4 m: a double
#     steel rail at the middle; A8 Burgau (six lanes, rebuilt 2015) 3-5 m: a concrete step barrier.
#   France: A10 Blois ~3 m paved: low concrete (GBA). Spain: A-357 Malaga ~7 m and A-4 Almuradiel 4-7 m: a single W-beam
#     along each carriageway's edge. Italy: A1 Modena (narrow, paved): New Jersey concrete.
#   Japan: Tomei Gotemba 3-4 m: steel rail with shrubs. Australia: M1 Pacific Motorway ~10 m: a four-rope cable barrier.
#   No barrier seen anywhere past ~20 m.
STYLES = {
    "na": ((4.0, JERSEY, JERSEY, JERSEY, JERSEY, JERSEY),
           (12.0, CABLE, JERSEY, JERSEY, NONE, JERSEY),
           (15.0, CABLE, NONE, JERSEY, NONE, NONE),
           (20.0, NONE, NONE, JERSEY, NONE, NONE)),
    "north": ((4.0, STEEL, JERSEY, JERSEY, STEEL, JERSEY),
              (12.0, STEEL, STEEL, STEEL, STEEL, NONE)),
    "fr": ((4.0, JERSEY, JERSEY, JERSEY, JERSEY, JERSEY),
           (12.0, RAILS, JERSEY, RAILS, RAILS, NONE)),
    "es": ((3.0, JERSEY, JERSEY, JERSEY, JERSEY, JERSEY),
           (14.0, RAILS, RAILS, RAILS, RAILS, NONE)),
    "it": ((5.0, JERSEY, JERSEY, JERSEY, JERSEY, JERSEY),
           (12.0, RAILS, JERSEY, RAILS, RAILS, NONE)),
    "jp": ((8.0, STEEL, STEEL, STEEL, STEEL, NONE),),
    "au": ((5.0, JERSEY, JERSEY, JERSEY, JERSEY, JERSEY),
           (15.0, CABLE, JERSEY, CABLE, CABLE, NONE)),
    "default": ((4.0, JERSEY, JERSEY, JERSEY, JERSEY, JERSEY),
                (12.0, STEEL, STEEL, STEEL, STEEL, NONE)),
}
REGIONS = {c: r for r, cs in (
    ("na", "US CA PR"),
    ("north", "DE NL BE LU AT CH LI DK SE NO FI IS PL CZ SK HU SI EE LV LT GB IE IM JE GG"),
    ("fr", "FR MC"),
    ("es", "ES PT AD GI"),
    ("it", "IT SM VA MT GR CY"),
    ("jp", "JP"),
    ("au", "AU NZ"),
) for c in cs.split()}


def style(country):
    """The style table (STYLES) of a country (ISO alpha-2, or None)."""
    return STYLES[REGIONS.get((country or "").upper(), "default")]


def _key(fid, pts):
    """A carriageway piece's order, the same in every tile: its id and first point."""
    h = hashlib.blake2b(f"{fid}:{pts[0, 0]:.1f}:{pts[0, 1]:.1f}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big", signed=True)


def _mapped_segments(rows):
    """[ax, ay, bx, by] of the mapped barrier modules among guard rail rows (guardrails.GROUPS: the jersey groups run
    along their second column, the rest along their first)."""
    out = []
    for r in rows or ():
        g = int(r[0])
        along = (r[11], r[12]) if g in (6, 7) else (r[8], r[9])
        h = MODULE_M / 2
        out.append((r[1] - along[0] * h, r[2] - along[1] * h, r[1] + along[0] * h, r[2] + along[1] * h))
    return np.asarray(out, np.float64).reshape(-1, 4)


def flags_of(spec, deck=False):
    """medians.rs's flags of a carriageway: bit 0 a candidate (a motorway / trunk one-way carriageway that is no ramp
    and no street with sidewalks), 1 a ramp, 2 travel against its geometry, 3 urban, 4 WIDE_LANES or more, 5 a
    motorway, 6 a bridge deck."""
    import roads as roadsmod
    cand = (spec.cls in ("motorway", "trunk") and bool(spec.oneway) and not spec.link
            and spec.kind in roadsmod.BUILT and not roadsmod.has_sidewalks(spec))
    wide = spec.lanes >= WIDE_LANES if spec.lanes else float(spec.width) >= WIDE_M
    return ((1 if cand else 0) | (2 if spec.link else 0) | (4 if (spec.dir or 1) < 0 else 0) |
            (8 if spec.urban else 0) | (16 if wide else 0) | (32 if spec.cls == "motorway" else 0) | (64 if deck else 0))


def modules(roads_near, box, left_hand=False, country=None, mapped_rows=(), decks=()):
    """medians.rs median_modules' (k, 11) for the ground carriageways roads_near ((fid, spec, centreline (k, >= 2),
    ...)) and the bridge decks ((fid, spec, xy (k, 2), z (k,), ...): BridgeProfiles.parts)."""
    import roads as roadsmod
    import roadmesh
    pts, zs, off, half, flags, keys, allow = [], [], [0], [], [], [], []
    lines = [(item[0], item[1], np.asarray(item[2], np.float64)[:, :2], None) for item in roads_near]
    lines += [(item[0], item[1], np.asarray(item[2], np.float64)[:, :2], np.asarray(item[3], np.float64))
              for item in decks]
    for fid, spec, xy, z in lines:
        if len(xy) < 2 or spec is None:
            continue
        f = flags_of(spec, z is not None)
        pts.append(xy)
        zs.append(np.zeros(len(xy)) if z is None else z.reshape(-1))
        off.append(off[-1] + len(xy))
        half.append(float(spec.width) / 2)
        flags.append(f)
        keys.append(_key(fid, xy))
        allow.append(max(EDGE_ALLOW_M, roadsmod.edge_margin(spec) - SHOULDER_KEEP_M) if f & 1 else 0.0)
    if not any(f & 1 for f in flags):
        return np.zeros((0, 11), np.float64)
    fast.require(99, "median_modules")
    return np.asarray(fast._ef.median_modules(
        np.ascontiguousarray(np.concatenate(pts), np.float64), np.ascontiguousarray(np.concatenate(zs), np.float64),
        np.asarray(off, np.int64),
        np.asarray(half, np.float64), np.asarray(flags, np.int32), np.asarray(keys, np.int64),
        np.asarray(allow, np.float64), np.ascontiguousarray(_mapped_segments(mapped_rows)),
        np.asarray(style(country), np.float64).reshape(-1, 6),
        [0.0 if left_hand else 1.0, MAX_GAP_M, OPENING_M, MAPPED_REACH_M, MODULE_M, roadmesh.MEDIAN_JOIN_M],
        [float(v) for v in box]), np.float64)


def tile_rows(frame, roads_near, box, elev, left_hand=False, country=None, mapped_rows=(), decks=(), custom=()):
    """Forest rows (guardrails.forest_rows' layout and GROUPS) of the median barriers in road tile box (x0, y0, x1,
    y1): roads_near the tile's ground carriageways (roads.build_tile roads_out: (fid, spec, centreline (k, 3), lon)),
    decks its bridges' (BridgeProfiles.parts: (fid, spec, xy, z, lon)), elev(lat, lon) the ground, mapped_rows the
    tile's mapped barrier rows (none of ours along them)."""
    mods = modules(roads_near, box, left_hand, country, mapped_rows, decks)
    if custom:
        import medianbarriers
        mods = medianbarriers.native_modules(mods, custom)
    if not len(mods):
        return []
    ends = np.concatenate([mods[:, 2:4], mods[:, 4:6]])
    z = np.concatenate([mods[:, 9], mods[:, 10]])           # (a deck's own height)
    ground = ~np.isfinite(z)
    if ground.any():
        la, lo, _ = frame.enu_to_geodetic(ends[ground, 0], ends[ground, 1], np.zeros(int(ground.sum()), np.float64))
        z[ground] = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)
    n = len(mods)
    rows = fast._ef.median_rows(np.ascontiguousarray(mods), np.ascontiguousarray(z[:n]),
                                np.ascontiguousarray(z[n:]))
    return np.round(np.asarray(rows, np.float64), 4).tolist()
