"""Germany's junction signs as they stand: the catalogue designs.py (which re-exports it) and furniture.py (the
collision boxes) both import, data and pure geometry only, so neither waits on the other. One mesh per group
(tools/build_median_props.py's DE_SIGNS: bne_<group>.dae), its faces on bne_de_signs.png (tools/build_de_signs.py),
its height in trees.lua's DE_SIGNS (HEIGHTS: rows carry it, so the mesh is drawn at scale 1, its plate and lower edge
as built). A group is a face alone or over its 1002 plate (_p<k>, k an index into PLATES), in one of three sizes
(SIZES: a suffix, Größe 1, 2 and 3)."""
import math

# the kernel's 1002 pictures in its order (native dejunction.rs PATTERNS): the other arms' slots, then the priority
# pair's; D the approach (down on the plate), L left, A ahead, R right
PLATES = ("LR:DL", "LR:DR", "LA:DL", "LA:LA", "RA:DR", "RA:RA", "LAR:DL", "LAR:DR", "LAR:LA", "LAR:AR")
FACES = ("z205", "z206", "z301", "z306", "z307")
# the faces a 1002 plate goes under: 306 (StVO Anlage 3 lfd. Nr. 2.1), 205 and 206 (Anlage 2 zu Nr. 2 und 3); never
# 301 (VwV-StVO zu Zeichen 301 Rn. 3: "darf nicht zusammen mit dem Zeichen 301 angeordnet werden")
PLATED = ("z205", "z206", "z306")
SIZES = ("_g1", "", "_g3")
ASSEMBLIES = tuple("de_" + f for f in FACES) + tuple(f"de_{f}_p{k}" for f in PLATED for k in range(len(PLATES)))
GROUPS = tuple(a + s for a in ASSEMBLIES for s in SIZES)
# m at Größe 1 / 2 / 3: VwV-StVO zu den §§ 39 bis 43 Rn. 13, triangle side 630 / 900 / 1260 (205, 301), square side
# 420 / 600 / 840 (306, 307: on its corner), Zusatzzeichen Höhe 3 420 x 420 / 600 x 600 / 750 x 750 (the 1002 plate:
# square in the VzKat's picture, Teil 7 Zeichen 1002). The octagon (206), across its flats: the VzKat's own size (Rn.
# 14: "Größenangaben für Sonderformen ... sind im VzKat festgelegt"; Teil 3 Zeichen 206: "h = 900mm (Standardgröße), h =
# 1050mm (Übergröße)"): it has no smaller one, so Größe 1 (a road under 50 km/h, size_suffix) is the 900 too.
SIZE = {"z205": (0.63, 0.90, 1.26), "z301": (0.63, 0.90, 1.26), "z206": (0.90, 0.90, 1.05),
        "z306": (0.42, 0.60, 0.84), "z307": (0.42, 0.60, 0.84), "plate": (0.42, 0.60, 0.75)}
# each shape as a regular polygon: corners, the first corner's direction (degrees from +x, the face seen from the
# front), the sides' distance from the middle per size, the corners' radius per size (measured off the public-domain
# renderings on Wikimedia Commons: 37.7 mm at 900, 42 at 900, 39 at 600, 40 at 600; not construction dimensions)
SHAPES = {"z205": (3, -90.0, 1 / (2 * 3 ** 0.5), 0.0419), "z301": (3, 90.0, 1 / (2 * 3 ** 0.5), 0.0419),
          "z206": (8, 22.5, 0.5, 0.0467), "z306": (4, 90.0, 0.5, 0.0653), "z307": (4, 90.0, 0.5, 0.0653),
          "plate": (4, 45.0, 0.5, 0.0667)}
LOW_M = 2.0         # the lowest edge over the ground (VwV-StVO zu den §§ 39 bis 43 Rn. 42: "in der Regel 2 m über
                    # Straßenniveau"; the same out of town)
GAP_M = 0.05        # between the face and the plate under it (an assumption: the VwV gives none)
POST_R_M = 0.0302   # the post: a galvanised 60.3 mm tube
DEPTH_M = 0.02      # the plates' folded edge, in front of the post
SINK_M = 0.3        # the post's foot below the ground


def rounded(n, first, r_in, radius, segs):
    """A regular n-gon's edge, (x, z) counter-clockwise seen from the front round its middle: corners at first + k *
    360 / n degrees, its sides r_in from the middle, each corner an arc of radius in segs pieces (radius or segs 0:
    sharp)."""
    half = math.pi / n
    out = []
    for k in range(n):
        a = math.radians(first) + 2 * half * k
        if radius <= 0 or segs <= 0:
            d = r_in / math.cos(half)
            out.append((d * math.cos(a), d * math.sin(a)))
            continue
        d = (r_in - radius) / math.cos(half)            # (the arc's centre: radius in from both sides)
        out += [(d * math.cos(a) + radius * math.cos(a - half + 2 * half * j / segs),
                 d * math.sin(a) + radius * math.sin(a - half + 2 * half * j / segs)) for j in range(segs + 1)]
    return out


def edge(shape, size, segs=4):
    """The edge of SHAPES' shape at size (m), round its middle (an even segs puts a point on each extreme)."""
    n, first, r_in, radius = SHAPES[shape]
    return rounded(n, first, r_in * size, radius * size, segs)


def extent(shape, size):
    """(x0, z0, x1, z1) of edge(shape, size) round its middle."""
    pts = edge(shape, size)
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _parts(group):
    name = group[3:]
    size = 0 if name.endswith("_g1") else 2 if name.endswith("_g3") else 1
    name = name[:-3] if size != 1 else name
    face, _, plate = name.partition("_p")
    return face, (int(plate) if plate else None), size


PARTS = {g: _parts(g) for g in GROUPS}          # group -> (face, plate index or None, size index into SIZES)


def panels(group):
    """[(picture, shape, size m, the middle's height over the ground)] of a group, bottom up: its 1002 plate
    (picture "p<k>") with its lower edge LOW_M, then GAP_M over it the face; or the face alone, its lowest point
    LOW_M. The plate is of the face's size class: where a face has no smaller size (Z 206 at Größe 1 is the 900 mm
    octagon of Größe 2) its plate is that larger class's too."""
    face, plate, i = PARTS[group]
    j = max(k for k in range(len(SIZES)) if SIZE[face][k] == SIZE[face][i])     # (the plate's size index)
    out, z = [], LOW_M
    for pic, shape in (((f"p{plate}", "plate"),) if plate is not None else ()) + ((face, face),):
        size = SIZE[shape][j if shape == "plate" else i]
        x0, z0, x1, z1 = extent(shape, size)
        out.append((pic, shape, size, z - z0))
        z += z1 - z0 + GAP_M
    return out


def _top(group):
    _, shape, size, mid = panels(group)[-1]
    return mid + extent(shape, size)[3]


HEIGHTS = {g: round(_top(g), 3) for g in GROUPS}                                # m: the top (trees.lua's DE_SIGNS)
WIDTHS = {g: round(max(extent(s, m)[2] - extent(s, m)[0] for _, s, m, _ in panels(g)), 3) for g in GROUPS}
# furniture.COLLISION_BOXES' entries (what a car hits and what must clear the carriageway, furniture.RoadClearance):
# the widest panel from the face's front to the post's back, the post's foot to the top, at the catalog height
BOXES = {g: (HEIGHTS[g], ((-WIDTHS[g] / 2, -(POST_R_M + DEPTH_M), -SINK_M, WIDTHS[g] / 2, POST_R_M, HEIGHTS[g]),))
         for g in GROUPS}


def size_suffix(kmh):
    """The suffix of SIZES for a road's speed limit (km/h): VwV-StVO zu den §§ 39 bis 43 Rn. 15, triangles, squares
    and rectangles, Größe 1 from 20 to less than 50, 2 from 50 to 100, 3 above 100. No limit known (None, 0): Größe
    2."""
    if not kmh or kmh != kmh:
        return ""
    return "_g1" if kmh < 50 else "_g3" if kmh > 100 else ""
