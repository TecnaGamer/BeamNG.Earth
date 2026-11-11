"""Road surface wear (the user, 2026-11-10: "lets use as many assets as needed to give this world detail and
variety"): the stock maps' wear decals on our paved roads, as pieces of the tile's road mesh (no objects of their own).

Real roads first (Mapillary 2026-11-10, docs/research/streetview/wear_*): Iowa's concrete US-30 shows its transverse
slab joints every ~5 m, a joint between the lanes and darker slabs; Baltimore's streets new black asphalt with grey
utility patches, a manhole cover in a lane; Munich's streets darker wheel paths and patched rectangles. The stock maps
(game_assets/level_usage) draw this with 252 DecalRoad materials; a DecalRoad is an object each (a frame cost), so the
pieces here go into the tile's one road mesh instead, a handful of materials reused everywhere:

  bne_wear_tread    tyre polish along the lanes (tread_marks_worn), tertiary roads and up
  bne_wear_sealed   a tar-sealed crack network (AsphaltRoad_joint_01's), maintained roads of middling age
  bne_wear_cracks   alligator cracking in a wheel path (m_asphalt_cracks_02's), poor surfaces
  bne_crack_line    one crack / joint line (road_crack's): a concrete road's slab joints across and its lane joints,
                    the paving joint along an old two-way road, transverse cracks out of town
  bne_spot_patch    repair patches (the asphalt_patches atlas: 8 shapes)
  bne_spot_pothole  potholes (the m_asphalt_pothole_decal atlas: 4), the worst surfaces only
  bne_spot_manhole  manhole covers and small lids (Italy's ground parts atlas), town streets
  bne_spot_oil      oil spots (oilstains) in a parking lot's stalls and down service roads

Our materials (roadwear.materials.json) point at the stock textures under /assets. The strips fade in and out at
their ends through the fade uv the dirt strips use (roadmesh FADE_PREFIX, the opacity detail map on the second uv).

The layout is native (earthfast.wear_plan, roadwear.rs): deterministic per road (its id's seed, distances along its
whole centre line), cut at the other carriageways. The condition of a surface comes from OpenStreetMap's smoothness
where mapped, else a seeded value by class, varied per 250 m stretch.

Tiers (the settings' road detail, BNE_ROAD_WEAR overrides): 0 none, 1 tyre polish and concrete joints (two
materials at most: low-end machines), 2 everything.
"""
import hashlib
import os

import numpy as np

TIERS = {"off": 0, "basic": 1, "full": 2}
ENV_TIER = TIERS.get(os.environ.get("BNE_ROAD_WEAR", "").strip().lower())   # (None: the setting's)
DEFAULT_TIER = 2
CUT_MARGIN = 0.5            # m past another carriageway's edge a piece stops (roads.CUT_MARGIN)
NA = 13                     # roadwear.rs attrs columns

K_TREAD, K_SEALED, K_CRACKS, K_LINE, K_ACROSS, K_PATCH, K_POTHOLE, K_MANHOLE, K_OIL = range(9)
# kind -> look (layer: roadmesh.LIFT; "p": the DecalRoad priority, unused in a mesh); strips with "fade" carry
# s0 / s1 / slen / fends (roadmesh._fade_uv); "feather": 0 leaves out the strip's side rows (its texture is soft)
LOOKS = {
    K_TREAD: {"m": "bne_wear_tread", "p": 9, "layer": "wear", "fade": True, "feather": 0},
    K_SEALED: {"m": "bne_wear_sealed", "p": 9, "layer": "wear2", "fade": True},
    K_CRACKS: {"m": "bne_wear_cracks", "p": 9, "layer": "wear2", "fade": True},
    K_LINE: {"m": "bne_crack_line", "p": 8, "layer": "wline"},
    K_ACROSS: {"m": "bne_crack_line", "p": 8, "layer": "wline"},
    K_PATCH: {"m": "bne_spot_patch", "p": 8, "layer": "spot"},
    K_POTHOLE: {"m": "bne_spot_pothole", "p": 8, "layer": "spot"},
    K_MANHOLE: {"m": "bne_spot_manhole", "p": 8, "layer": "spot"},
    K_OIL: {"m": "bne_spot_oil", "p": 8, "layer": "spot"},
}
MATERIALS = tuple(sorted({v["m"] for v in LOOKS.values()}))     # (prewarm.py)


def _grid(cols, rows):
    """Atlas cells (u0, v0, u1, v1), row 0 at the top (the shape's v runs up from the sheet's bottom:
    markings.arrow_quad)."""
    out = []
    for r in range(rows):
        for c in range(cols):
            v1 = 1.0 - r / rows
            out.append((c / cols, v1 - 1.0 / rows, (c + 1) / cols, v1))
    return tuple(out)


# the manhole sheet: square cover, round cover, plate (three quarters), and the bottom right quarter's four lids
_SMALL = tuple((0.5 + u0 / 2, v0 / 2, 0.5 + u1 / 2, v1 / 2) for u0, v0, u1, v1 in _grid(2, 2))
CELLS = {
    K_PATCH: _grid(4, 2),
    K_POTHOLE: _grid(2, 2),
    K_MANHOLE: ((0.0, 0.5, 0.5, 1.0), (0.5, 0.5, 1.0, 1.0), (0.0, 0.0, 0.5, 0.5)) + _SMALL,
    K_OIL: ((0.0, 0.0, 1.0, 1.0),),
}
CLASS_CODE = {"motorway": 0, "trunk": 1, "primary": 2, "secondary": 3, "tertiary": 4, "residential": 5,
              "unclassified": 5, "living_street": 5, "service": 6}
# OpenStreetMap smoothness -> condition (0 ruined .. 1 new)
SMOOTHNESS = {"excellent": 0.95, "good": 0.75, "intermediate": 0.45, "bad": 0.2, "very_bad": 0.1, "horrible": 0.04,
              "very_horrible": 0.02, "impassable": 0.0}


def tier_of(setting):
    """The tier for a setting value ('off' / 'basic' / 'full', a number, None: DEFAULT_TIER); BNE_ROAD_WEAR wins."""
    if ENV_TIER is not None:
        return ENV_TIER
    if setting is None:
        return DEFAULT_TIER
    if isinstance(setting, str):
        return TIERS.get(setting.strip().lower(), DEFAULT_TIER)
    try:
        return int(np.clip(int(setting), 0, 2))
    except (TypeError, ValueError):
        return DEFAULT_TIER


def _seed(fid):
    return int.from_bytes(hashlib.blake2s(str(fid).encode(), digest_size=4).digest(), "little")


def _condition(feats, i):
    col = feats.attrs.get("osm_smoothness") if feats is not None else None
    if col is None:
        return -1.0
    return SMOOTHNESS.get(str(col[i]).strip().lower(), -1.0)


def tile_chunks(roads, seen, box, tier, exclude=None, aisles=(), skip=()):
    """The wear chunks of a tile's roads (build_tile's roads: (fid, spec, centreline (k, 3), lon)); seen: fid ->
    (feats, i, spec) (OpenStreetMap's smoothness); box: x0 y0 x1 y1 the pieces are laid in (the mesh is cut at the
    tile); exclude(fid, other fid): the other road does not cut this one's pieces (a straight continuation, a ramp's
    merge); aisles: a parking lot's aisles (their stalls' oil); skip: roads without wear of their own (a piece inside a
    junction). Chunks carry "wear": 1 (no DecalRoad fallback draws them)."""
    if tier <= 0 or not roads:
        return []
    import fast
    import roads as roadsmod
    fast.require(202, "wear_plan")
    pts, off, rows = [], [0], []
    for fid, spec, p, _lon in roads:
        p = np.asarray(p, np.float64)
        pts.append(p[:, :3])
        off.append(off[-1] + len(p))
        hw = float(spec.width) / 2
        wear = spec.kind == "paved" and fid not in skip and len(p) >= 2
        drive, lanes, doff = 2 * hw, 1, 0.0
        if wear:
            drive, lanes = roadsmod.lane_layout(spec)
            doff = roadsmod.driving_offset(spec)
        feats, i = (seen[fid][0], seen[fid][1]) if fid in seen else (None, 0)
        rows.append((hw, float(drive), float(doff), float(lanes), float(bool(spec.oneway)),
                     float(CLASS_CODE.get(spec.cls, 7)), float(spec.surf == "concrete"), float(bool(spec.urban)),
                     _condition(feats, i), float(_seed(fid)), float(wear), float(fid in aisles), hw + CUT_MARGIN))
    if not any(r[10] for r in rows):
        return []
    excl = []
    if exclude is not None:
        bxs = [(q[:, 0].min(), q[:, 1].min(), q[:, 0].max(), q[:, 1].max()) for q in pts]
        ids = [r[0] for r in roads]
        for a, ba in enumerate(bxs):
            if not rows[a][10]:
                continue
            for b, bb in enumerate(bxs):
                if a != b and ba[0] <= bb[2] + 20 and bb[0] <= ba[2] + 20 and ba[1] <= bb[3] + 20 \
                        and bb[1] <= ba[3] + 20 and exclude(ids[a], ids[b]):
                    excl.append((a, b))
    P, O, A = fast._ef.wear_plan(np.ascontiguousarray(np.concatenate(pts)), np.asarray(off, np.int64),
                                 np.ascontiguousarray(np.asarray(rows, np.float64).reshape(-1, NA)),
                                 np.asarray(excl, np.int64).reshape(-1, 2),
                                 np.asarray(box, np.float64)[:4], int(tier))
    out = []
    for k in range(len(A)):
        kind, width, tl, slen, cell = A[k]
        kind = int(kind)
        look = LOOKS[kind]
        q = P[O[k]:O[k + 1]]
        ch = {"m": look["m"], "p": look["p"], "layer": look["layer"], "wear": 1}
        if kind in CELLS:
            u0, v0, u1, v1 = CELLS[kind][max(0, int(cell)) % len(CELLS[kind])]
            ch.update(w=0.0, tl=1.0, n=[], poly=np.round(q[:, :3], 3).tolist(),
                      uvq=[[u0, v1], [u1, v1], [u1, v0], [u0, v0]])
        else:
            ch.update(w=round(float(width), 2), tl=round(float(tl), 2), n=np.round(q[:, :3], 3).tolist())
            if look.get("fade") and slen > 0:
                ch.update(s0=round(float(q[0, 3]), 3), s1=round(float(q[-1, 3]), 3), slen=round(float(slen), 3),
                          fends=[1, 1])
                if "feather" in look:
                    ch["feather"] = look["feather"]
        out.append(ch)
    return out
