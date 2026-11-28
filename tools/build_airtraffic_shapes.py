"""build_airtraffic_shapes.py: the live air traffic's aircraft shapes (mod/.../art/shapes/airtraffic/*.dae,
main.materials.json) and the livery colours (mod/.../lua/ge/extensions/beamngEarth/airtraffic/palettes.lua), made from
devo343's Live Air Traffic add-on models (used with their permission; their generator made plain lambert-coloured
geometry with no textures).

    python3 tools/build_airtraffic_shapes.py [--src DIR]

DIR is the add-on's art/shapes/liveAirTraffic folder (default: ~/bnge-tests/pr1580src/src/live-air-traffic/mod_src/
levels/beamng_earth/art/shapes/liveAirTraffic). Its COLLADA files are read as XML data only.

What it writes, per airframe <m>:
  lat_<m>.dae        the body in three detail levels (the engine reads a level's size, its on-screen radius in px, from
                     the node name's trailing number: tools/build_rail_crossing_props.lod_name): L0 the full mesh, L1 a
                     vertex-clustered mesh (cell = length / 48; seams and the interior left out, the see-through windows
                     opaque dark: they fill holes in the skin), L2 a coarse cluster (cell = length / 14, the colours
                     folded into a few materials, the livery's fin and engine covers in the tail colour: the fin is
                     only in the livery file), drawn down to 1 px.
  lat_<m>_liv.dae    the livery overlay (the fin, the cheatline stripe, the engine covers: the add-on's 16 overlays have
                     the same geometry, only their colours differ) in ONE material, lat_liv, tinted per aircraft by the
                     TSStatic's instanceColor (palettes.lua: the add-on's tail colours). One detail level, sized so it
                     gives way exactly where the body takes its L2 (which has the livery's parts untinted).
  lat_<m>_gear.dae   the landing gear, one level, not drawn below GEAR_PX.
  lat_<m>_cabin.dae  the interior for the seat view, one level.
plus lat_rotor_<shape>.dae (one level, not drawn when small) and lat_glyph_<c>.dae (the lettering, one level).
Livery files that are no palette overlay go into the body: the fighter's (its fin, in lat_mil_dark: no lat_fighter_liv)
and the balloon's white gores (lat_body; its coloured gores are its livery).

Vertices are re-indexed (identical position + normal shared) and written in mm, normals to 3 decimals; texture
coordinates are left out (no material has a texture). Deterministic: the same input gives the same bytes.
"""
import argparse
import json
import os
import re
import sys
import uuid
import xml.etree.ElementTree as ET

try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:     # (loaded by its path into another process, a test: that process's entry point guards)
    pass
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
SRC = os.path.expanduser("~/bnge-tests/pr1580src/src/live-air-traffic/mod_src/levels/beamng_earth/art/shapes/"
                         "liveAirTraffic")
OUT = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "airtraffic")
LUA_OUT = os.path.join(ROOT, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "airtraffic",
                       "palettes.lua")
NS = "{http://www.collada.org/2005/11/COLLADASchema}"

# Detail sizes (px of on-screen radius; the engine, measured: about 7000 x bounding radius / distance at 1440p, and it
# draws nothing below a shape's smallest level). A 38 m narrowbody (radius 26.7 m with its fin) takes L1 at 1.25 km,
# L2 at 5.8 km; L2 down to 1 px keeps it on screen past 40 km (the game scales far aircraft up to 2.5x).
BODY_PX = (150, 32, 1)
CELLS = (None, 48.0, 14.0)      # each level's cluster cell: the body's length (or span) over this
GEAR_PX = 24                    # the gear, a propeller: gone below this
ROTOR_BIG_PX, ROTOR_BIG_M = 12, 3.0     # a rotor this wide in radius or more (a helicopter's main rotor) stays longer
CABIN_PX = 64
GLYPH_PX = 8
CREASE_COS = float(np.cos(np.radians(60.0)))    # clustered levels: faces further apart than this keep a crisp edge
# Materials left out of the clustered levels: small details (seams) and the interior seen through the windows
DETAIL_MATS = {"lat_seam", "lat_cabin", "lat_cabin_light", "lat_text"}
# The see-through windows and the cockpit glass fill holes in the skin (left out, the clustered fuselage shows gaps):
# a small window (a cabin window, smaller than two cells: clustered, a row of them reads as black dashes) in the
# skin's colour, a large one (a canopy, a windscreen) and the cockpit glass dark; L2 also has fewer materials (draw
# calls: one per material and aircraft)
SMALL_WINDOW = "lat_window_small"   # (a name for the split only: mapped before writing)
FAR_MAP = {"lat_window": "lat_dark", SMALL_WINDOW: "lat_body", "lat_glass": "lat_dark"}
COARSE_MAP = {"lat_window": "lat_metal", SMALL_WINDOW: "lat_body", "lat_glass": "lat_metal", "lat_dark": "lat_metal",
              "lat_mil_dark": "lat_mil", "lat_wicker": "lat_metal"}
LIV_FALLBACK = "lat_tail"       # the livery's fin and engine covers in the body's L2 (untinted)
LIV_FALLBACK_PARTS = ("tail", "engine")     # (the cheatline stripe is a thin overlay: the cluster smears it)
# The order a triangle the clustering doubled is kept in (the first material wins)
PRIORITY = ("lat_body", "lat_mil", "lat_tail", "lat_metal", "lat_dark", "lat_mil_dark", "lat_wicker")
LIV = "lat_liv"
LIV_PID = str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/airtraffic/lat_liv"))
PALETTES = 16
LIV_RE = re.compile(r"^lat_liv(\d+)_(tail|stripe|engine)$")


# ------------------------------------------------------------------------------------------------ reading
def read_dae(path):
    """{material: (P, N)}: the corners of every triangle (n, 3, 3) and their normals (n, 3, 3), or None without."""
    root = ET.parse(path).getroot()
    mat_names = {m.get("id"): m.get("name") or m.get("id") for m in root.iter(NS + "material")}
    for node in root.iter(NS + "node"):
        for tag in ("matrix", "translate", "rotate", "scale", "lookat", "skew"):
            if node.find(NS + tag) is not None:
                raise SystemExit(f"{path}: node {node.get('name')} has a transform ({tag}): not supported")
    binds = {}          # geometry id -> {symbol: material name}
    for ig in root.iter(NS + "instance_geometry"):
        gid = ig.get("url").lstrip("#")
        for im in ig.iter(NS + "instance_material"):
            binds.setdefault(gid, {})[im.get("symbol")] = mat_names[im.get("target").lstrip("#")]
    out = {}
    for g in root.iter(NS + "geometry"):
        gid = g.get("id")
        if gid not in binds:
            continue                    # (not in the scene)
        mesh = g.find(NS + "mesh")
        src = {}
        for s in mesh.findall(NS + "source"):
            fa = s.find(NS + "float_array")
            stride = int(s.find(f"{NS}technique_common/{NS}accessor").get("stride"))
            src[s.get("id")] = np.array(fa.text.split(), dtype=np.float64).reshape(-1, stride)
        verts = {v.get("id"): v.find(NS + "input").get("source").lstrip("#") for v in mesh.findall(NS + "vertices")}
        for c in mesh:
            if c.tag in (NS + "polylist", NS + "polygons", NS + "lines", NS + "trifans", NS + "tristrips"):
                raise SystemExit(f"{path}: {c.tag[len(NS):]} not supported")
        for t in mesh.findall(NS + "triangles"):
            ins = t.findall(NS + "input")
            stride = 1 + max(int(i.get("offset")) for i in ins)
            p = np.array(t.find(NS + "p").text.split(), dtype=np.int64).reshape(-1, stride)
            pos = nrm = None
            for i in ins:
                sid, off = i.get("source").lstrip("#"), int(i.get("offset"))
                if i.get("semantic") == "VERTEX":
                    pos = src[verts[sid]][p[:, off]]
                elif i.get("semantic") == "NORMAL":
                    nrm = src[sid][p[:, off]]
            n = int(t.get("count"))
            if pos is None or len(pos) != 3 * n:
                raise SystemExit(f"{path}: {gid}: bad triangles block")
            mat = binds[gid].get(t.get("material"), t.get("material"))
            P = pos.reshape(n, 3, 3)
            N = None if nrm is None else nrm.reshape(n, 3, 3)
            add(out, mat, P, N)
    return out


def add(mesh, mat, P, N):
    if mat in mesh:
        P0, N0 = mesh[mat]
        P = np.concatenate([P0, P])
        N = None if N0 is None or N is None else np.concatenate([N0, N])
    mesh[mat] = (P, N)


def bbox(*meshes):
    pts = np.concatenate([P.reshape(-1, 3) for m in meshes for P, _ in m.values()])
    return pts.min(0), pts.max(0)


def radius(*meshes):
    """The engine's bounding radius: half the diagonal of the box round all of a shape's geometry."""
    lo, hi = bbox(*meshes)
    return float(np.linalg.norm(hi - lo) / 2)


# ------------------------------------------------------------------------------------------------ clustering
def cluster(mesh, cell):
    """A vertex-clustered copy of mesh: positions snapped to a grid of cell (m) with its corner at the model's origin
    (the fuselage's axis: a tube splits into four quadrants and stays a tube), each cell's vertex the mean of the
    distinct positions in it; degenerate and doubled triangles dropped (the first in PRIORITY order kept, the materials
    are double-sided), normals from the faces smoothed within 60 degrees."""
    mats = sorted(mesh, key=lambda k: (PRIORITY.index(k) if k in PRIORITY else len(PRIORITY), k))
    P = np.concatenate([mesh[k][0] for k in mats])
    mat_of = np.concatenate([np.full(len(mesh[k][0]), i) for i, k in enumerate(mats)])
    upos, inv = np.unique(P.reshape(-1, 3), axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    keys = np.floor(upos / cell).astype(np.int64)
    ucell, cid = np.unique(keys, axis=0, return_inverse=True)
    cid = cid.reshape(-1)
    cnt = np.bincount(cid, minlength=len(ucell)).astype(np.float64)
    rep = np.stack([np.bincount(cid, weights=upos[:, k], minlength=len(ucell)) for k in range(3)], 1) / cnt[:, None]
    tri = cid[inv].reshape(-1, 3)
    ok = (tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])
    tri, mat_of = tri[ok], mat_of[ok]
    _, first = np.unique(np.sort(tri, 1), axis=0, return_index=True)
    first = np.sort(first)
    tri, mat_of = tri[first], mat_of[first]
    fn = np.cross(rep[tri[:, 1]] - rep[tri[:, 0]], rep[tri[:, 2]] - rep[tri[:, 0]])
    area2 = np.linalg.norm(fn, axis=1)
    ok = area2 > 1e-9
    tri, mat_of, fn, area2 = tri[ok], mat_of[ok], fn[ok], area2[ok]
    un = fn / area2[:, None]
    # the corners' normals: the area-weighted normals of the faces round their vertex that are within the crease angle
    m = len(tri)
    v = tri.reshape(-1)
    f = np.repeat(np.arange(m), 3)
    order = np.argsort(v, kind="stable")
    vs, fs = v[order], f[order]
    starts = np.searchsorted(vs, vs, "left")
    sizes = np.searchsorted(vs, vs, "right") - starts
    k = np.repeat(np.arange(len(vs)), sizes)
    partner = starts[k] + (np.arange(len(k)) - np.repeat(np.cumsum(sizes) - sizes, sizes))
    fa, fb = fs[k], fs[partner]
    keep = np.einsum("ij,ij->i", un[fa], un[fb]) > CREASE_COS
    acc = np.zeros((len(vs), 3))
    np.add.at(acc, k[keep], fn[fb[keep]])
    acc /= np.maximum(np.linalg.norm(acc, axis=1), 1e-12)[:, None]
    cn = np.zeros((len(vs), 3))
    cn[order] = acc
    cn = cn.reshape(-1, 3, 3)
    out = {}
    for i, name in enumerate(mats):
        sel = mat_of == i
        if sel.any():
            out[name] = (rep[tri[sel]], cn[sel])
    return out


def split_windows(mesh, cell):
    """mesh with its lat_window triangles in pieces (triangles joined by a shared corner) smaller than two cells
    renamed SMALL_WINDOW."""
    if "lat_window" not in mesh:
        return mesh
    P, N = mesh["lat_window"]
    _, inv = np.unique(P.reshape(-1, 3), axis=0, return_inverse=True)
    inv = inv.reshape(-1, 3)
    parent = list(range(int(inv.max()) + 1))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for a, b, c in inv.tolist():
        ra = find(a)
        for x in (b, c):
            rx = find(x)
            if rx != ra:
                parent[max(rx, ra)] = min(rx, ra)
                ra = min(rx, ra)
    comp = np.array([find(int(a)) for a in inv[:, 0]])
    small = np.zeros(len(P), bool)
    for c in np.unique(comp):
        sel = comp == c
        pts = P[sel].reshape(-1, 3)
        small[sel] = float(np.max(pts.max(0) - pts.min(0))) < 2.0 * cell
    out = {k: v for k, v in mesh.items() if k != "lat_window"}
    for name, sel in (("lat_window", ~small), (SMALL_WINDOW, small)):
        if sel.any():
            out[name] = (P[sel], None if N is None else N[sel])
    return out


def remap(mesh, table, drop=()):
    out = {}
    for k in sorted(mesh):
        if k in drop:
            continue
        P, N = mesh[k]
        add(out, table.get(k, k), P, N)
    return out


def tri_count(mesh):
    return sum(len(P) for P, _ in mesh.values())


# ------------------------------------------------------------------------------------------------ writing
def _mm(q):
    """A whole number of thousandths as a short decimal (no float formatting: the same text everywhere)."""
    s = "-" if q < 0 else ""
    a = abs(q)
    i, f = divmod(a, 1000)
    if f == 0:
        return "0" if a == 0 else f"{s}{i}"
    return f"{s}{i}.{f:03d}".rstrip("0")


def _floats(qs):
    return " ".join(_mm(int(q)) for q in qs.reshape(-1))


def _geom(gid, mesh):
    """One <geometry>: the vertices shared by all its materials (equal position + normal once), a <triangles> block per
    material (sorted by name)."""
    mats = sorted(mesh)
    has_n = all(mesh[k][1] is not None for k in mats)
    keys, idx_of = [], []
    for k in mats:
        P, N = mesh[k]
        q = np.rint(P.reshape(-1, 3) * 1000.0).astype(np.int64)
        if has_n:
            n = N.reshape(-1, 3)
            n = n / np.maximum(np.linalg.norm(n, axis=1), 1e-12)[:, None]
            q = np.concatenate([q, np.rint(n * 1000.0).astype(np.int64)], 1)
        keys.append(q)
    allk = np.concatenate(keys)
    uk, first, inv = np.unique(allk, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    order = np.argsort(first, kind="stable")        # the vertices in their first use's order
    rank = np.empty(len(order), np.int64)
    rank[order] = np.arange(len(order))
    uk = uk[order]
    idx = rank[inv]
    blocks, at = [], 0
    for k, q in zip(mats, keys):
        t = idx[at:at + len(q)].reshape(-1, 3)
        pq = q[:, :3].reshape(-1, 3, 3)
        at += len(q)
        ok = ~((pq[:, 0] == pq[:, 1]).all(1) | (pq[:, 1] == pq[:, 2]).all(1) | (pq[:, 0] == pq[:, 2]).all(1))
        t = t[ok]
        if len(t) == 0:
            continue
        inputs = f'<input semantic="VERTEX" source="#{gid}-v" offset="0"/>'
        if has_n:
            inputs += f'<input semantic="NORMAL" source="#{gid}-n" offset="0"/>'
        blocks.append((k, len(t), f'<triangles count="{len(t)}" material="{k}-mat">{inputs}'
                                  f'<p>{" ".join(str(int(i)) for i in t.reshape(-1))}</p></triangles>\n'))
    n = len(uk)
    acc = lambda sid: (f'<technique_common><accessor source="#{sid}-a" count="{n}" stride="3">'
                       '<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>'
                       '</accessor></technique_common>')
    xml = (f'<geometry id="{gid}"><mesh>\n'
           f'<source id="{gid}-p"><float_array id="{gid}-p-a" count="{n * 3}">{_floats(uk[:, :3])}</float_array>'
           f'{acc(gid + "-p")}</source>\n')
    if has_n:
        xml += (f'<source id="{gid}-n"><float_array id="{gid}-n-a" count="{n * 3}">{_floats(uk[:, 3:])}'
                f'</float_array>{acc(gid + "-n")}</source>\n')
    xml += (f'<vertices id="{gid}-v"><input semantic="POSITION" source="#{gid}-p"/></vertices>\n'
            + "".join(b for _, _, b in blocks) + '</mesh></geometry>\n')
    return xml, [k for k, _, _ in blocks], sum(c for _, c, _ in blocks)


def lod_name(name, size):
    """tools/build_rail_crossing_props.lod_name: a name ending in a digit gets "_L" before its size."""
    return f"{name}_L{size}" if name[-1:].isdigit() else f"{name}{size}"


def write_dae(path, name, lods, colours):
    """lods: [(size px, mesh)], nearest first. Returns the triangles written per level. Lists only the materials used."""
    geoms, nodes, used, counts = "", "", set(), []
    for size, mesh in lods:
        nn = lod_name(name, size)
        g, mats, c = _geom(f"{nn}-g", mesh)
        used |= set(mats)
        counts.append(c)
        geoms += g
        nodes += (f'<node id="{nn}" name="{nn}"><instance_geometry url="#{nn}-g"><bind_material><technique_common>'
                  + "".join(f'<instance_material symbol="{k}-mat" target="#{k}-mat"/>' for k in mats)
                  + '</technique_common></bind_material></instance_geometry></node>\n')
    mats = sorted(used)
    effects = "".join(f'<effect id="{k}-fx"><profile_COMMON><technique sid="common"><lambert><diffuse><color>'
                      f'{" ".join(_mm(int(round(c * 1000))) for c in colours[k])} 1</color></diffuse></lambert>'
                      '</technique></profile_COMMON></effect>\n' for k in mats)
    materials = "".join(f'<material id="{k}-mat" name="{k}"><instance_effect url="#{k}-fx"/></material>\n'
                        for k in mats)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><contributor><authoring_tool>BeamNG.Earth tools/build_airtraffic_shapes.py (models: devo343, Live Air Traffic)\
</authoring_tool></contributor><unit meter="1" name="meter"/><up_axis>Z_UP</up_axis></asset>
<library_effects>
{effects}</library_effects>
<library_materials>
{materials}</library_materials>
<library_geometries>
{geoms}</library_geometries>
<library_visual_scenes><visual_scene id="scene" name="scene">
<node id="base00" name="base00"><node id="start01" name="start01">
{nodes}</node><node id="collision-1" name="collision-1"/></node>
</visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene>
</COLLADA>
"""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(xml)
    return counts, mats


# ------------------------------------------------------------------------------------------------ the parts
def split_livery(liv):
    """A livery file's parts: (palette overlay {kind: (P, N)}, parts that belong to the body {material: (P, N)})."""
    over, body = {}, {}
    for k in sorted(liv):
        m = LIV_RE.match(k)
        if m:
            add(over, m.group(2), *liv[k])
        else:
            add(body, k, *liv[k])
    return over, body


def same_geometry(a, b):
    """Two livery overlays with the same geometry (their materials' palette numbers aside)."""
    if sorted(a) != sorted(b):
        return False
    for k in a:
        (Pa, Na), (Pb, Nb) = a[k], b[k]
        if Pa.shape != Pb.shape or not np.array_equal(Pa, Pb):
            return False
        if (Na is None) != (Nb is None) or (Na is not None and not np.array_equal(Na, Nb)):
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", default=SRC)
    args = ap.parse_args()
    src = args.src
    files = sorted(f for f in os.listdir(src) if f.endswith(".dae"))
    with open(os.path.join(src, "liveAirTraffic.materials.json"), encoding="utf-8") as fh:
        src_mats = json.load(fh)
    colours = {k: v["Stages"][0]["baseColorFactor"][:3] for k, v in src_mats.items()}
    colours[LIV] = [1.0, 1.0, 1.0]
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):           # (only this tool's files live here)
        if f.endswith(".dae") or f.endswith(".materials.json"):
            os.remove(os.path.join(OUT, f))

    models = [f[4:-4] for f in files if re.match(r"^lat_[a-z0-9_]+\.dae$", f)
              and not re.match(r"^lat_(glyph|rotor)_", f) and not re.search(r"_(gear|cabin|liv\d+)\.dae$", f)]
    used, total, rows, notes = set(), 0, [], []

    def put(fname, name, lods):
        nonlocal total
        path = os.path.join(OUT, fname)
        counts, mats = write_dae(path, name, lods, colours)
        used.update(mats)
        size = os.path.getsize(path)
        total += size
        return counts, size

    for m in models:
        body = read_dae(os.path.join(src, f"lat_{m}.dae"))
        livs = sorted((int(x.group(1)), f) for f in files
                      if (x := re.match(rf"^lat_{re.escape(m)}_liv(\d+)\.dae$", f)))
        over = {}
        if livs:
            liv0 = read_dae(os.path.join(src, livs[0][1]))
            over, to_body = split_livery(liv0)
            for i, f in livs[1:]:
                o, b = split_livery(read_dae(os.path.join(src, f)))
                if not (same_geometry(o, over) and same_geometry(b, to_body)):
                    raise SystemExit(f"{f}: its geometry differs from {livs[0][1]}'s")
            for k, (P, N) in to_body.items():
                add(body, k, P, N)
            if to_body:
                notes.append(f"{m}: {len(livs)} livery file(s); parts with no palette colour ({', '.join(sorted(to_body))}, "
                             f"{tri_count(to_body)} tris) moved into the body")
            if not over:
                notes.append(f"{m}: no palette overlay: no lat_{m}_liv.dae")
        else:
            notes.append(f"{m}: no livery files")
        liv_mesh = {}
        for kind in sorted(over):
            add(liv_mesh, LIV, *over[kind])
        fallback = {}
        for kind in LIV_FALLBACK_PARTS:
            if kind in over:
                add(fallback, LIV_FALLBACK, *over[kind])
        lo, hi = bbox(body, liv_mesh) if liv_mesh else bbox(body)
        length = float(max(hi[0] - lo[0], hi[1] - lo[1]))
        lods = [(BODY_PX[0], body)]
        c1, c2 = length / CELLS[1], length / CELLS[2]
        lods.append((BODY_PX[1], cluster(remap(split_windows(body, c1), FAR_MAP, DETAIL_MATS), c1)))
        coarse = remap(split_windows(body, c2), COARSE_MAP, DETAIL_MATS)
        for k, (P, N) in fallback.items():
            add(coarse, k, P, N)
        lods.append((BODY_PX[2], cluster(coarse, length / CELLS[2])))
        counts, size = put(f"lat_{m}.dae", f"lat_{m}", lods)
        r_body = radius(*[ms for _, ms in lods])
        row = [m, counts, size, r_body]
        if liv_mesh:
            r_liv = radius(liv_mesh)
            liv_px = max(1, min(1024, int(round(BODY_PX[1] * r_liv / r_body))))
            lc, ls = put(f"lat_{m}_liv.dae", f"lat_{m}_liv", [(liv_px, liv_mesh)])
            row += [(liv_px, lc[0], ls)]
        else:
            row += [None]
        for part, px in (("gear", GEAR_PX), ("cabin", CABIN_PX)):
            p = os.path.join(src, f"lat_{m}_{part}.dae")
            if os.path.exists(p):
                mesh = read_dae(p)
                c, s = put(f"lat_{m}_{part}.dae", f"lat_{m}_{part}", [(px, mesh)])
                row += [(px, c[0], s)]
            else:
                row += [None]
        rows.append(row)

    extra = []
    for f in files:
        if f.startswith("lat_rotor_") or f.startswith("lat_glyph_"):
            mesh = read_dae(os.path.join(src, f))
            if f.startswith("lat_rotor_"):
                lo, hi = bbox(mesh)
                big = float(np.max(np.abs(np.concatenate([lo, hi])))) >= ROTOR_BIG_M
                px = ROTOR_BIG_PX if big else GEAR_PX
            else:
                px = GLYPH_PX
            c, s = put(f, f[:-4], [(px, mesh)])
            extra.append((f, px, c[0], s))

    # the materials: the add-on's (as they are, their ids kept) that the shapes use, and the tinted livery
    out = {}
    for k in sorted(used):
        if k == LIV:
            out[k] = {"name": LIV, "mapTo": LIV, "class": "Material", "version": 1.5, "persistentId": LIV_PID,
                      "Stages": [{"baseColorFactor": [1.0, 1.0, 1.0, 1.0], "instanceDiffuse": True,
                                  "metallicFactor": src_mats["lat_liv0_tail"]["Stages"][0]["metallicFactor"],
                                  "roughnessFactor": src_mats["lat_liv0_tail"]["Stages"][0]["roughnessFactor"]},
                                 {}, {}, {}], "doubleSided": True}
        elif k in src_mats:
            out[k] = src_mats[k]
        else:
            raise SystemExit(f"material {k} is in no materials file")
    mpath = os.path.join(OUT, "main.materials.json")
    with open(mpath, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")
    total += os.path.getsize(mpath)

    # the palettes: each scheme's tail colour (what the tinted overlay takes)
    lines = ["-- Generated by tools/build_airtraffic_shapes.py: the livery overlay's colour per palette (the tail colour of",
             "-- devo343's Live Air Traffic scheme N, lat_livN_tail), for the TSStatic's instanceColor. Do not edit by hand.",
             "return {"]
    for i in range(PALETTES):
        r, g, b = src_mats[f"lat_liv{i}_tail"]["Stages"][0]["baseColorFactor"][:3]
        lines.append(f"  [{i}] = {{{r}, {g}, {b}}},")
    lines.append("}")
    os.makedirs(os.path.dirname(LUA_OUT), exist_ok=True)
    with open(LUA_OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"{'model':16s} {'L0':>6s} {'L1':>6s} {'L2':>6s} {'body KB':>8s} {'R m':>6s}  liv(px,tris,KB)  "
          f"gear(px,tris,KB)  cabin(px,tris,KB)")
    fmt = lambda x: "-" if x is None else f"{x[0]},{x[1]},{x[2] // 1024}"
    for m, c, s, r, liv, gear, cab in rows:
        print(f"{m:16s} {c[0]:6d} {c[1]:6d} {c[2]:6d} {s // 1024:8d} {r:6.1f}  {fmt(liv):16s} {fmt(gear):17s} "
              f"{fmt(cab)}")
    for f, px, c, s in extra:
        if f.startswith("lat_rotor_"):
            print(f"{f:32s} {px:3d} px {c:6d} tris {s // 1024:5d} KB")
    g = [e for e in extra if e[0].startswith("lat_glyph_")]
    print(f"{len(g)} glyphs at {GLYPH_PX} px: {sum(e[2] for e in g)} tris, {sum(e[3] for e in g) // 1024} KB")
    for n in notes:
        print("note:", n)
    print(f"{len(os.listdir(OUT))} files, {total / 1e6:.2f} MB in {os.path.relpath(OUT, ROOT)}; {os.path.relpath(LUA_OUT, ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
