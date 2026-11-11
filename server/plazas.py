"""Squares and pedestrian areas as a paved mesh (the user's report #45, Fells Point 2026-10-17: "Paived or brick
terrain that isnt roads are too organic and blurry"; Broadway Square on Mapillary: crisp red brick pavers round its
pavilion, where the terrain showed the aerial photo's blur).

Each Overture land_use polygon of subtype "pedestrian" (surface.PLAZA_CLASSES) is triangulated, its triangles split
until no edge is longer than STEP_M (earthfast.subdivide_tris, Rust) so it follows the graded ground, and draped
LIFT_M over it with the brick pavers tiled in world metres. A road tile gets the triangles whose centre lies in it
(no overlap between tiles). Overlapping areas are paved once: the smaller (the more specific) keeps the ground it
shares with a larger one (GitHub #127: Melbourne's Federation Square, two pavings at one height flickered through
each other). Render only (the terrain under it collides); the road mesh draws over it (a road's lift is higher). Pure
given its inputs (elev)."""
import numpy as np

STEP_M = 2.0            # m: the longest edge after subdivision (the physics block's 2 m lattice)
LIFT_M = 0.04           # m over the graded ground (under the road mesh's asphalt: detail mode lifts it 0.05)
BRICK = "bne_sidewalk_brick"    # red brick pavers in herringbone (buildings.materials.json; sidewalks.BRICK)
BRICK_REPEAT_M = 1.2    # m per repeat (sidewalks.BRICK_REPEAT_M)
MIN_AREA_M2 = 60.0      # smaller polygons are left to the terrain
# The paving by the mapped surface (GitHub #70: every square was red brick, Paris's Place de la Concorde and Louvre
# courtyards among them, 56,000 and 71,000 m2 of it): (material, m per texture repeat) as the road mesh draws those
# surfaces (roads.LOOK); unpaved ones (the Tuileries' compacted sand and gravel) are left to the terrain's photo. No
# surface mapped or paving stones: red brick in the US (Fells Point's squares, the user's report #45), stone elsewhere.
ASPHALT = ("road_asphalt_2lane", 11.0)
STONE = ("italy_cobblestone", 2.5)
# Flat-topped pavers (OSM's paving_stones: "flat top surface", unlike sett and cobblestone): the stock grey rectangular
# sidewalk tiles (the game's art/main.materials.json, always loaded; its texture is 2:1, six by three pavers: 0.6 x 1.2 m
# at 3.6 m per repeat), as the World Trade Center's granite pavers (the user, 2026-11-11: "the textue is now cobblestone").
PAVERS = ("italy_sidewalk_tiles", 3.6)
PAVING = {"asphalt": ASPHALT, "concrete": ASPHALT, "concrete:plates": ASPHALT, "concrete_plates": ASPHALT,
          "paved": ASPHALT, "paving_stones": PAVERS, "sett": STONE, "cobblestone": STONE, "unhewn_cobblestone": STONE,
          "pebblestone": STONE, "bricks": (BRICK, BRICK_REPEAT_M)}
UNPAVED = {"compacted", "gravel", "fine_gravel", "unpaved", "dirt", "ground", "sand", "grass", "earth", "mud",
           "woodchips", "grass_paver"}


def _subdivide(pts, tris, step):
    import fast
    fast.require(1, "subdivide_tris")
    P, T = fast._ef.subdivide_tris(np.ascontiguousarray(pts, np.float64), np.ascontiguousarray(tris, np.int64),
                                   float(step))
    return np.asarray(P), np.asarray(T)

def _inside_many(ring, pts):
    """Which points pts (k, 2) lie inside the ring (m, 2) (even-odd; polygon.contains_points, Rust)."""
    import polygon
    return polygon.contains_points(np.asarray(ring, np.float64), np.asarray(pts, np.float64))


def _overlaps(a, b):
    """Do the boxes (x0, y0, x1, y1) a and b overlap?"""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def polygons(feats_list):
    """The squares and pedestrian areas among the land_use Features: [(outer ring lon/lat (k, 2), [holes' rings],
    surface or None)]."""
    import surface
    out = []
    for f in feats_list:
        if f is None or len(f) == 0:
            continue
        m = surface._plazas(f)
        if m is None:
            continue
        sf = f.attrs.get("surface")
        for i in np.flatnonzero(m):
            v = str(sf[i]).strip().lower() if sf is not None and sf[i] is not None else ""
            for part in f.parts(i):
                if len(part) and len(part[0]) >= 4:
                    out.append((np.asarray(part[0], np.float64), [np.asarray(h, np.float64) for h in part[1:]],
                                v or None))
    return out


# A US square mapped paving_stones (or nothing) was red brick (Fells Point's squares); New York's are mostly grey granite
# and concrete pavers (the user, 2026-11-11 in game: "several parts of new york get covered in this red brick
# texture": the World Trade Center's plaza and streets, 475 paving_stones pieces). Now red brick only where the tile's
# orthophoto shows the area brick red (its hue under BRICK_HUE_MAX degrees, saturation over BRICK_SAT_MIN: the NYS photo
# shows the WTC's granite warm tan, hue ~37: "red over green and blue" took that for brick), grey pavers (PAVERS)
# otherwise; an area the photo cannot read takes the tile's majority, and with no photo at all the old US rule (brick)
# stands.
BRICK_HUE_MAX = 25.0
BRICK_SAT_MIN = 0.18


def is_brick_red(rgb):
    """Is this sRGB colour (0..1) brick red: hue under BRICK_HUE_MAX (or over 345), saturation over BRICK_SAT_MIN?"""
    r, g, b = (float(v) for v in rgb[:3])
    mx, mn = max(r, g, b), min(r, g, b)
    if mx <= 0 or (mx - mn) / mx < BRICK_SAT_MIN or mx != r:
        return False
    hue = 60.0 * ((g - b) / (mx - mn)) % 360.0
    return hue < BRICK_HUE_MAX or hue > 345.0
US_AMBIGUOUS = "us_ambiguous"


# Tags that name a square's paving better than surface=paving_stones (rare: 40 surface:colour and 5
# paving_stones:material among 4,942 cached squares, but exact where set; the ground-sources survey, 2026-11-11)
TAG_BRICK_MATERIALS = {"brick", "bricks", "clay", "clinker", "terracotta"}
TAG_GREY_MATERIALS = {"concrete", "stone", "granite", "basalt", "limestone", "sandstone", "marble", "slate"}
TAG_RED_COLOURS = {"red", "brown", "darkred", "maroon", "orange", "terracotta", "brick"}
TAG_GREY_COLOURS = {"grey", "gray", "white", "black", "silver", "lightgrey", "darkgrey", "lightgray", "darkgray",
                    "beige"}


def tag_look(tags):
    """The look an OSM square's own tags give it (paving_stones:material / material, then surface:colour / colour:
    red brick or grey pavers), None when they say nothing about it. tags: {key: value} (streetfront.tags_of)."""
    if not tags:
        return None
    for key in ("paving_stones:material", "material"):
        v = str(tags.get(key) or "").strip().lower()
        if v in TAG_BRICK_MATERIALS:
            return (BRICK, BRICK_REPEAT_M)
        if v in TAG_GREY_MATERIALS:
            return PAVERS
    for key in ("surface:colour", "surface:color", "colour", "color"):
        v = str(tags.get(key) or "").strip().lower()
        if not v:
            continue
        if v in TAG_RED_COLOURS:
            return (BRICK, BRICK_REPEAT_M)
        if v in TAG_GREY_COLOURS:
            return PAVERS
        if v.startswith("#") and len(v) == 7:
            try:
                rgb = tuple(int(v[k:k + 2], 16) / 255.0 for k in (1, 3, 5))
            except ValueError:
                continue
            return (BRICK, BRICK_REPEAT_M) if is_brick_red(rgb) else PAVERS
    return None


def paving(surface_value, lat, lon):
    """(material, m per repeat) of a square with this mapped surface at (lat, lon), None for an unpaved one."""
    if surface_value in UNPAVED:
        return None
    import buildings
    us = buildings.region_style(lat, lon).name.startswith("us_")
    if us and surface_value in (None, "paving_stones"):     # (red brick or grey pavers by the photo: mesh's photo)
        return US_AMBIGUOUS
    # (no surface mapped elsewhere: flat stone pavers, the Zocalo's slabs, most squares' finish; setts and cobbles
    # only where mapped)
    return PAVING.get(surface_value, PAVERS) if surface_value is not None else PAVERS


# No photo read for a cell's squares (on a first visit the photos are fetched later, by the road tiles' roofs: 7 of a
# WTC block's 8 cells read nothing): grey pavers, the common US square (granite, concrete), not the red brick that
# covered the World Trade Center
US_UNREAD_BRICK = False


def us_looks(xys, photo):
    """The looks of US squares mapped paving_stones or nothing (paving's US_AMBIGUOUS), their outer rings xys [(k, 2)
    in the photo's frame]: red brick where the photo shows the area brick red, grey pavers otherwise; an area the
    photo cannot read (a thin footway, shade) takes the read areas' majority, none read (or no photo):
    US_UNREAD_BRICK. photo: buildings.roof_photo's (colours(rings) -> (n, 3) sRGB 0..1, nan unread) or None."""
    cols = None
    if photo is not None and len(xys):
        try:
            cols = photo.colours(list(xys))
        except Exception:
            cols = None
    votes = {}
    for j in range(len(xys)):
        if cols is not None and j < len(cols) and np.all(np.isfinite(cols[j])):
            votes[j] = is_brick_red(cols[j])
    default = (sum(votes.values()) * 2 >= len(votes)) if votes else US_UNREAD_BRICK
    return [(BRICK, BRICK_REPEAT_M) if votes.get(j, default) else PAVERS for j in range(len(xys))]


def _covered(cover, pts):
    """Which points (k, 2) lie on the triangles cover (n, 6: x0 y0 x1 y1 x2 y2; earthfast.points_on_tris, Rust)."""
    import fast
    fast.require(47, "points_on_tris")
    return np.asarray(fast._ef.points_on_tris(np.ascontiguousarray(cover, np.float64),
                                               np.ascontiguousarray(pts, np.float64), 8.0))


def mesh(frame, box, rings, elev, origin, cover=None, photo=None):
    """The paved mesh of the plaza rings (lon/lat) in the tile box (x0, y0, x1, y1) of the anchor frame:
    (positions relative to origin (k, 3), tris, uvs, [material], tri material index) or None. elev(lat, lon) -> m
    (the graded ground the physics block has). cover: the road mesh's triangles in the frame (n, 6: x0 y0 x1 y1 x2
    y2) or None: the ground they cover is left to them (a pedestrian street mapped over a square drew its cobbles
    1 cm over the paving, and the two flickered at a distance; GitHub #127). Triangles steeper than MAX_SLOPE are left
    out. The mesh near-visual mode's paving: in detail mode the terrain block draws the squares (surface.paint_lots,
    the PAVERS / BRICK layers)."""
    import buildings
    x0, y0, x1, y1 = box
    Ps, Ts, Ms, n0 = [], [], [], 0
    areas = []                          # (area, xy, holes, look) of the areas in the tile, the smallest first
    for item in rings:
        ring, holes, sv = (item, [], "bricks") if isinstance(item, np.ndarray) else item
        look = paving(sv, float(np.mean(ring[:, 1])), float(np.mean(ring[:, 0])))
        if look is None:
            continue
        e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
        xy = np.stack([np.asarray(e), np.asarray(n)], 1)
        if np.hypot(*(xy[0] - xy[-1])) < 1e-6:
            xy = xy[:-1]
        if (xy[:, 0].max() < x0 or xy[:, 0].min() > x1 or xy[:, 1].max() < y0 or xy[:, 1].min() > y1
                or len(xy) < 3):
            continue
        area2 = buildings._area2(xy)
        if abs(area2) / 2 < MIN_AREA_M2:
            continue
        if area2 < 0:
            xy = xy[::-1]
        areas.append((abs(area2) / 2, xy, holes, look))
    amb = [k for k, a in enumerate(areas) if a[3] == US_AMBIGUOUS]
    if amb:                             # (US squares: red brick where the photo shows red, grey pavers otherwise)
        for k, look in zip(amb, us_looks([areas[k][1] for k in amb], photo)):
            areas[k] = areas[k][:3] + (look,)
    areas.sort(key=lambda a: a[0])
    done = []                           # (ring xy, its box) of the areas paved so far (smaller ones)
    for _, xy, holes, look in areas:
        xbox = (xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max())
        tris = np.asarray(buildings.triangulate(xy), np.int64).reshape(-1, 3)
        if not len(tris):
            continue
        P, T = _subdivide(xy, tris, STEP_M)
        c = P[T].mean(1)
        keep = (c[:, 0] >= x0) & (c[:, 0] < x1) & (c[:, 1] >= y0) & (c[:, 1] < y1)
        for h in holes:                 # (a fountain, a lawn, a building in it: its triangles out, to STEP_M)
            if keep.any() and len(h) >= 3:
                he, hn, _ = frame.geodetic_to_enu(h[:, 1], h[:, 0], 0.0)
                hxy = np.stack([np.asarray(he), np.asarray(hn)], 1)
                if (hxy[:, 0].max() < c[keep, 0].min() or hxy[:, 0].min() > c[keep, 0].max()
                        or hxy[:, 1].max() < c[keep, 1].min() or hxy[:, 1].min() > c[keep, 1].max()):
                    continue
                k = np.flatnonzero(keep)
                keep[k[_inside_many(hxy, c[k])]] = False
        if cover is not None and len(cover) and keep.any():
            k = np.flatnonzero(keep)
            keep[k[_covered(cover, c[k])]] = False
        for dxy, dbox in done:          # (a smaller area over this one: its ground paved once, by it)
            if keep.any() and _overlaps(xbox, dbox):
                k = np.flatnonzero(keep)
                keep[k[_inside_many(dxy, c[k])]] = False
        done.append((xy, xbox))
        if not keep.any():
            continue
        T = T[keep]
        used = np.unique(T)
        remap = np.full(len(P), -1, np.int64)
        remap[used] = np.arange(len(used))
        Ps.append(P[used])
        Ts.append(remap[T] + n0)
        Ms.append((look, len(used), len(T)))
        n0 += len(used)
    if not Ps:
        return None
    P = np.concatenate(Ps)
    T = np.concatenate(Ts)
    mats = []
    UV = np.empty((len(P), 2))
    idx = np.empty(len(T), np.int64)
    v0 = t0 = 0
    for (mat, rep_m), nv, nt in Ms:
        if mat not in mats:
            mats.append(mat)
        UV[v0:v0 + nv] = P[v0:v0 + nv] / rep_m
        idx[t0:t0 + nt] = mats.index(mat)
        v0, t0 = v0 + nv, t0 + nt
    la, lo, _ = frame.enu_to_geodetic(P[:, 0], P[:, 1], np.zeros(len(P)))
    h = np.asarray(elev(la, lo), np.float64)
    _, _, z = frame.geodetic_to_enu(la, lo, h)
    V = np.c_[P, np.asarray(z) + LIFT_M] - np.asarray(origin, np.float64)
    # (the elevation's humps where buildings stand, the 9/11 museum pavilion's pyramid, and the pools' walls carried the
    # paving up and down them, its top-down tiling smeared into streaks: the user, 2026-11-11 at the WTC)
    e = V[T]
    n = np.cross(e[:, 1] - e[:, 0], e[:, 2] - e[:, 0])
    level = np.hypot(n[:, 0], n[:, 1]) <= MAX_SLOPE * np.abs(n[:, 2])
    if not level.all():
        if not level.any():
            return None
        T, idx = T[level], idx[level]
        used = np.unique(T)
        remap = np.full(len(V), -1, np.int64)
        remap[used] = np.arange(len(used))
        V, UV, T = V[used], UV[used], remap[T]
    return V, T, UV, mats, idx


# A paved square is near level (ramps 1:12, the lidar's ground 0.1-0.2 m from metre to metre); a triangle steeper than
# this (rise over run; the top-down tiling stretches 1.22x) lies on a building's hump or a wall in the elevation
MAX_SLOPE = 0.7


def merge(a, b):
    """Two meshes (positions, tris, uvs, materials, tri material index; either None) as one. A sixth element (the road
    mesh's fade uv: roadmesh.build with a dirt or gravel strip) is kept, the other mesh's points on its solid middle."""
    if a is None:
        return b
    if b is None:
        return a
    Pa, Ta, UVa, Ma, Ia = a[:5]
    Pb, Tb, UVb, Mb, Ib = b[:5]
    mats = list(Ma)
    idx = []
    for m in Mb:
        if m not in mats:
            mats.append(m)
        idx.append(mats.index(m))
    out = (np.concatenate([Pa, Pb]), np.concatenate([Ta, np.asarray(Tb) + len(Pa)]), np.concatenate([UVa, UVb]),
           mats, np.concatenate([Ia, np.asarray(idx, np.int64)[np.asarray(Ib)]]))
    Fa = a[5] if len(a) > 5 else None
    Fb = b[5] if len(b) > 5 else None
    if Fa is None and Fb is None:
        return out
    Fa = np.full((len(Pa), 2), 0.5) if Fa is None else np.asarray(Fa, np.float64)
    Fb = np.full((len(Pb), 2), 0.5) if Fb is None else np.asarray(Fb, np.float64)
    return out + (np.concatenate([Fa, Fb]),)
