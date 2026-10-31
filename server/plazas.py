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
PAVING = {"asphalt": ASPHALT, "concrete": ASPHALT, "concrete:plates": ASPHALT, "concrete_plates": ASPHALT,
          "paved": ASPHALT, "paving_stones": STONE, "sett": STONE, "cobblestone": STONE, "unhewn_cobblestone": STONE,
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


def paving(surface_value, lat, lon):
    """(material, m per repeat) of a square with this mapped surface at (lat, lon), None for an unpaved one."""
    if surface_value in UNPAVED:
        return None
    import buildings
    us = buildings.region_style(lat, lon).name.startswith("us_")
    if us and surface_value in (None, "paving_stones"):     # (Fells Point's squares, mapped paving_stones: red brick)
        return BRICK, BRICK_REPEAT_M
    return PAVING.get(surface_value, STONE)


def _covered(cover, pts):
    """Which points (k, 2) lie on the triangles cover (n, 6: x0 y0 x1 y1 x2 y2; earthfast.points_on_tris, Rust)."""
    import fast
    fast.require(47, "points_on_tris")
    return np.asarray(fast._ef.points_on_tris(np.ascontiguousarray(cover, np.float64),
                                               np.ascontiguousarray(pts, np.float64), 8.0))


def mesh(frame, box, rings, elev, origin, cover=None):
    """The paved mesh of the plaza rings (lon/lat) in the tile box (x0, y0, x1, y1) of the anchor frame:
    (positions relative to origin (k, 3), tris, uvs, [material], tri material index) or None. elev(lat, lon) -> m
    (the graded ground the physics block has). cover: the road mesh's triangles in the frame (n, 6: x0 y0 x1 y1 x2
    y2) or None: the ground they cover is left to them (a pedestrian street mapped over a square drew its cobbles
    1 cm over the paving, and the two flickered at a distance; GitHub #127)."""
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
    la, lo, _ = frame.enu_to_geodetic(P[:, 0], P[:, 1], np.zeros(len(P)))
    h = np.asarray(elev(la, lo), np.float64)
    _, _, z = frame.geodetic_to_enu(la, lo, h)
    V = np.c_[P, np.asarray(z) + LIFT_M] - np.asarray(origin, np.float64)
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
    return V, T, UV, mats, idx


def merge(a, b):
    """Two meshes (positions, tris, uvs, materials, tri material index; either None) as one. A sixth element,
    the dirt strip's fade uv, is kept when either mesh has it (the other side stays on the solid texel)."""
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
    if Fa is None:
        Fa = np.full((len(Pa), 2), 0.5)
    if Fb is None:
        Fb = np.full((len(Pb), 2), 0.5)
    return out + (np.concatenate([np.asarray(Fa, np.float64), np.asarray(Fb, np.float64)]),)
