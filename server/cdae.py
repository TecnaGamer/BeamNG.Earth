"""BeamNG binary shape (.cdae, TSShape v31) reader and writer.

BeamNG compiles every Collada file it loads into a .cdae cache; a TSStatic also accepts a
.cdae shapeName directly (probe P5, docs/V1_2_DESIGN.md §12), which loads ~5x faster than
the DAE because there is no XML to parse. This module writes our far tiles in that format.

File layout (decoded from engine-written files, see tests/fixtures/):
  u32 version word   0x007C001F: exporter version 0x7C (124) << 16 | shape version 31
  u32 header size
  header             msgpack map {info: str, compression: false, bodysize: int, objectNames: [str]}
  body               a flat sequence of msgpack values (no enclosing array), uncompressed

Body, in order (Torque3D TSShape::write, BeamNG's msgpack variant):
  smallestVisibleSize, smallestVisibleDL, radius, tubeRadius, center [3], bounds [6]
  19 vectors (each = count, element size, bin): nodes, objects, subShapeFirstNode,
    subShapeFirstObject, subShapeNumNodes, subShapeNumObjects, defaultRotations,
    defaultTranslations, nodeRotations, nodeTranslations, nodeUniformScales, nodeAlignedScales,
    nodeArbitraryScaleFactors, nodeArbitraryScaleRots, groundTranslations, groundRotations,
    objectStates, triggers, details
  names: count, then each name as a str
  meshes: count, then per mesh: type, numFrames, numMatFrames, parentMesh, bounds [6],
    center [3], radius, 10 vectors (verts, tverts, tverts2, colors, norms, encodedNorms,
    primitives, indices, tangents), vertsPerFrame, flags
  sequences: count (0 here)
  materials: count, then per material: name, flags, reflectanceMap, bumpMap, detailMap,
    detailScale, reflectionAmount

msgpack details that the engine's own files show: floats are float32 (0xca) unless they are
integral, in which case they are packed as the smallest int; ints use the smallest encoding;
binary blobs are little-endian arrays.

Geometry conventions of the engine's Collada import (verified against golden pairs):
  - triangle winding is reversed (the engine stores clockwise-from-outside triangles);
  - texture v is flipped (v_cdae = 1 - v_dae);
  - one primitive per material, indexed triangle list (matIndex | 0x20000000);
  - tangents are xyz + w (handedness), 16 bytes per vertex.
"""
import os
import struct

import numpy as np

import fast
import staging

SHAPE_VERSION = 31
EXPORTER_VERSION = 0x7C
VERSION_WORD = (EXPORTER_VERSION << 16) | SHAPE_VERSION        # 0x007C001F
INFO = ("\r\n\r\n\r\nWelcome! This is a binary file :D\r\nPlease read the docs at "
        "https://go.beamng.com/shapeMessagepackFileformat\r\n\r\n\r\n")

# Primitive matIndex bits (TSDrawPrimitive).
PRIM_TRIANGLES = 0x00000000
PRIM_INDEXED = 0x20000000
PRIM_NO_MATERIAL = 0x10000000
PRIM_MATERIAL_MASK = 0x0FFFFFFF
# TSMaterialList flags.
MAT_S_WRAP, MAT_T_WRAP = 0x1, 0x2

SHAPE_VECTORS = ("nodes", "objects", "subShapeFirstNode", "subShapeFirstObject", "subShapeNumNodes",
                 "subShapeNumObjects", "defaultRotations", "defaultTranslations", "nodeRotations",
                 "nodeTranslations", "nodeUniformScales", "nodeAlignedScales", "nodeArbitraryScaleFactors",
                 "nodeArbitraryScaleRots", "groundTranslations", "groundRotations", "objectStates",
                 "triggers", "details")
# Element sizes of the shape vectors (an empty vector still states its element size).
SHAPE_VECTOR_ELEM = dict(zip(SHAPE_VECTORS, (20, 24, 4, 4, 4, 4, 8, 12, 8, 12, 4, 12, 12, 8, 12, 8, 12, 8, 52)))
MESH_VECTORS = ("verts", "tverts", "tverts2", "colors", "norms", "encodedNorms", "primitives",
                "indices", "tangents")
MESH_VECTOR_ELEM = dict(zip(MESH_VECTORS, (12, 8, 8, 4, 12, 1, 12, 4, 16)))


class CdaeError(ValueError):
    pass


# --- msgpack (the subset the shape format uses) ---------------------------------------
def _pack_int(out, v):
    if 0 <= v <= 0x7F:
        out.append(v)
    elif -32 <= v < 0:
        out.append(v & 0xFF)
    elif 0 <= v <= 0xFF:
        out += b"\xcc" + struct.pack(">B", v)
    elif 0 <= v <= 0xFFFF:
        out += b"\xcd" + struct.pack(">H", v)
    elif 0 <= v <= 0xFFFFFFFF:
        out += b"\xce" + struct.pack(">I", v)
    elif v > 0:
        out += b"\xcf" + struct.pack(">Q", v)
    elif v >= -0x80:
        out += b"\xd0" + struct.pack(">b", v)
    elif v >= -0x8000:
        out += b"\xd1" + struct.pack(">h", v)
    elif v >= -0x80000000:
        out += b"\xd2" + struct.pack(">i", v)
    else:
        out += b"\xd3" + struct.pack(">q", v)


def _pack(out, v):
    if v is None:
        out.append(0xC0)
    elif v is True or v is False:
        out.append(0xC3 if v else 0xC2)
    elif isinstance(v, (int, np.integer)):
        _pack_int(out, int(v))
    elif isinstance(v, (float, np.floating)):
        f = float(np.float32(v))
        if f == int(f) and abs(f) < 2 ** 31:
            _pack_int(out, int(f))          # the engine packs integral floats as ints
        else:
            out += b"\xca" + struct.pack(">f", f)
    elif isinstance(v, str):
        b = v.encode("utf-8")
        n = len(b)
        if n < 32:
            out.append(0xA0 | n)
        elif n <= 0xFF:
            out += b"\xd9" + struct.pack(">B", n)
        elif n <= 0xFFFF:
            out += b"\xda" + struct.pack(">H", n)
        else:
            out += b"\xdb" + struct.pack(">I", n)
        out += b
    elif isinstance(v, (bytes, bytearray, memoryview)):
        n = len(v)
        if n <= 0xFF:
            out += b"\xc4" + struct.pack(">B", n)
        elif n <= 0xFFFF:
            out += b"\xc5" + struct.pack(">H", n)
        else:
            out += b"\xc6" + struct.pack(">I", n)
        out += v
    elif isinstance(v, (list, tuple)):
        n = len(v)
        if n < 16:
            out.append(0x90 | n)
        elif n <= 0xFFFF:
            out += b"\xdc" + struct.pack(">H", n)
        else:
            out += b"\xdd" + struct.pack(">I", n)
        for x in v:
            _pack(out, x)
    elif isinstance(v, dict):
        n = len(v)
        if n < 16:
            out.append(0x80 | n)
        else:
            out += b"\xde" + struct.pack(">H", n)
        for k, x in v.items():
            _pack(out, k)
            _pack(out, x)
    else:
        raise TypeError(f"cannot pack {type(v).__name__}")


def packb(*values):
    out = bytearray()
    for v in values:
        _pack(out, v)
    return bytes(out)


_FIXED = {0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q", 0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q",
          0xCA: ">f", 0xCB: ">d"}


def unpack_one(b, i):
    """Decode one msgpack value at b[i]; returns (value, next index)."""
    t = b[i]
    if t <= 0x7F:
        return t, i + 1
    if t >= 0xE0:
        return t - 256, i + 1
    if t in _FIXED:
        fmt = _FIXED[t]
        n = struct.calcsize(fmt)
        return struct.unpack_from(fmt, b, i + 1)[0], i + 1 + n
    if 0xA0 <= t <= 0xBF or t in (0xD9, 0xDA, 0xDB):
        if t <= 0xBF:
            n, s = t & 31, i + 1
        else:
            L = {0xD9: 1, 0xDA: 2, 0xDB: 4}[t]
            n, s = int.from_bytes(b[i + 1:i + 1 + L], "big"), i + 1 + L
        return bytes(b[s:s + n]).decode("utf-8", "replace"), s + n
    if t in (0xC4, 0xC5, 0xC6):
        L = {0xC4: 1, 0xC5: 2, 0xC6: 4}[t]
        n, s = int.from_bytes(b[i + 1:i + 1 + L], "big"), i + 1 + L
        return bytes(b[s:s + n]), s + n
    if 0x90 <= t <= 0x9F or t in (0xDC, 0xDD):
        if t <= 0x9F:
            n, i = t & 15, i + 1
        else:
            L = 2 if t == 0xDC else 4
            n, i = int.from_bytes(b[i + 1:i + 1 + L], "big"), i + 1 + L
        a = []
        for _ in range(n):
            v, i = unpack_one(b, i)
            a.append(v)
        return a, i
    if 0x80 <= t <= 0x8F or t in (0xDE, 0xDF):
        if t <= 0x8F:
            n, i = t & 15, i + 1
        else:
            L = 2 if t == 0xDE else 4
            n, i = int.from_bytes(b[i + 1:i + 1 + L], "big"), i + 1 + L
        d = {}
        for _ in range(n):
            k, i = unpack_one(b, i)
            v, i = unpack_one(b, i)
            d[k] = v
        return d, i
    if t == 0xC0:
        return None, i + 1
    if t in (0xC2, 0xC3):
        return t == 0xC3, i + 1
    raise CdaeError(f"unsupported msgpack type 0x{t:02x} at {i}")


# --- reader ---------------------------------------------------------------------------
class _Stream:
    def __init__(self, b, i):
        self.b, self.i = b, i

    def next(self):
        if self.i >= len(self.b):
            raise CdaeError("truncated body")
        v, self.i = unpack_one(self.b, self.i)
        return v

    def int(self):
        v = self.next()
        if isinstance(v, float) and v == int(v):
            v = int(v)
        if not isinstance(v, int) or isinstance(v, bool):
            raise CdaeError(f"expected int, got {v!r}")
        return v

    def float(self):
        v = self.next()
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise CdaeError(f"expected number, got {v!r}")
        return float(v)

    def floats(self, n):
        v = self.next()
        if not isinstance(v, list) or len(v) != n:
            raise CdaeError(f"expected {n} numbers, got {v!r}")
        return [float(x) for x in v]

    def vector(self):
        n, size, data = self.int(), self.int(), self.next()
        if not isinstance(data, bytes) or len(data) != n * size:
            raise CdaeError(f"bad vector ({n} x {size}, {type(data).__name__})")
        return n, size, data


def read_version(path):
    """The version word of a shape file (0x007C001F for the format written here)."""
    with open(path, "rb") as f:
        head = f.read(4)
    if len(head) < 4:
        raise CdaeError("file too short")
    return struct.unpack("<I", head)[0]


def read(path):
    """Decode a v31 shape into a dict of plain values (vectors as (count, elemSize, bytes))."""
    with open(path, "rb") as f:
        b = f.read()
    return decode(b)


def decode(b):
    if len(b) < 8:
        raise CdaeError("file too short")
    version, hsize = struct.unpack_from("<II", b)
    if version & 0xFF != SHAPE_VERSION:
        raise CdaeError(f"unsupported shape version word 0x{version:08x}")
    header, end = unpack_one(b, 8)
    if end != 8 + hsize or not isinstance(header, dict):
        raise CdaeError("bad header")
    if header.get("compression"):
        raise CdaeError("compressed bodies are not supported")
    if header.get("bodysize") != len(b) - end:
        raise CdaeError(f"body size {len(b) - end} != header bodysize {header.get('bodysize')}")
    s = _Stream(b, end)
    out = {"version": version, "header": header}
    out["smallestVisibleSize"] = s.float()
    out["smallestVisibleDL"] = s.int()
    out["radius"] = s.float()
    out["tubeRadius"] = s.float()
    out["center"] = s.floats(3)
    out["bounds"] = s.floats(6)
    for name in SHAPE_VECTORS:
        out[name] = s.vector()
    out["names"] = [s.next() for _ in range(s.int())]
    meshes = []
    for _ in range(s.int()):
        m = {"type": s.int()}
        if m["type"] == 4:                  # null mesh
            meshes.append(m)
            continue
        if m["type"] != 0:
            raise CdaeError(f"mesh type {m['type']} not supported")
        m["numFrames"], m["numMatFrames"], m["parentMesh"] = s.int(), s.int(), s.int()
        m["bounds"], m["center"], m["radius"] = s.floats(6), s.floats(3), s.float()
        for name in MESH_VECTORS:
            m[name] = s.vector()
        m["vertsPerFrame"], m["flags"] = s.int(), s.int()
        meshes.append(m)
    out["meshes"] = meshes
    out["numSequences"] = s.int()
    if out["numSequences"]:
        raise CdaeError("sequences are not supported")
    mats = []
    for _ in range(s.int()):
        mats.append({"name": s.next(), "flags": s.int(), "reflectanceMap": s.int(), "bumpMap": s.int(),
                     "detailMap": s.int(), "detailScale": s.float(), "reflectionAmount": s.float()})
    out["materials"] = mats
    if s.i != len(b):
        raise CdaeError(f"{len(b) - s.i} trailing bytes")
    return out


def mesh_arrays(m):
    """numpy views of a decoded standard mesh: verts (n,3), tverts (n,2), norms (n,3),
    tangents (n,4), indices (k,), primitives (p,3)."""
    def arr(name, dtype, cols):
        n, size, data = m[name]
        a = np.frombuffer(data, dtype=dtype)
        return a.reshape(n, cols) if cols else a
    return {"verts": arr("verts", "<f4", 3), "tverts": arr("tverts", "<f4", 2),
            "tverts2": arr("tverts2", "<f4", 2), "norms": arr("norms", "<f4", 3),
            "tangents": arr("tangents", "<f4", 4), "indices": arr("indices", "<u4", None),
            "primitives": arr("primitives", "<i4", 3)}


# --- writer ---------------------------------------------------------------------------
def face_normal_sum(positions, tris):
    """Vertex normals the way the engine derives them for a DAE without normals: the
    normalised sum of the unit normals of the adjacent faces (tris counter-clockwise = up)."""
    fast.require(74, "face_normal_sum")         # (the Rust loop: numpy's order of sums, bit for bit)
    P = np.ascontiguousarray(np.asarray(positions, np.float64).reshape(-1, 3))
    T = np.ascontiguousarray(np.asarray(tris, np.int64).reshape(-1, 3))
    return np.asarray(fast._ef.face_normal_sum(P, T))


def tangents(V, UV, N, I):
    """Per-vertex tangents (xyz, w) by Lengyel's method, as the engine computes them (matches
    engine output to 1e-7 for regular UVs). V, UV, N, I in the stored (engine) convention."""
    fast.require(1, "tangents")
    f64 = lambda a: np.ascontiguousarray(a, np.float64)        # noqa: E731
    return fast._ef.tangents(f64(V), f64(UV), f64(N), np.ascontiguousarray(I, np.int64))

def _vec(data, count, elem):
    return (count, elem, data)


def _empty(elem):
    return (0, elem, b"")


def _mesh_data(P, T, TM, nmat, normals, uvs, uvs2, tangent_uvs, colors):
    """One standard mesh's body bytes (after its type) and its (lo, hi) bounds; T sorted by TM."""
    N = face_normal_sum(P, T) if normals is None else np.asarray(normals, np.float64).reshape(-1, 3)
    UV = np.zeros((len(P), 2)) if uvs is None else np.asarray(uvs, np.float64).reshape(-1, 2)
    TUV = UV if tangent_uvs is None else np.asarray(tangent_uvs, np.float64).reshape(-1, 2)

    # The engine numbers vertices in order of first use by the triangle list and stores the
    # triangles with the opposite winding.
    fast.require(74, "first_use")
    order, idx = fast._ef.first_use(np.ascontiguousarray(T, np.int64), len(P))
    order, idx = np.asarray(order), np.asarray(idx)

    V = P[order].astype(np.float32)
    NN = N[order].astype(np.float32)
    UV32 = UV[order].astype(np.float32)
    TV = np.stack([UV32[:, 0], np.float32(1.0) - UV32[:, 1]], 1)     # flipped in float32, as the engine does
    TTV = np.stack([TUV[order, 0], 1.0 - TUV[order, 1]], 1)
    TG = tangents(V.astype(np.float64), TTV, NN.astype(np.float64), idx).astype(np.float32)
    nv = len(V)
    ntri = len(idx)

    lo, hi = V.min(0), V.max(0)
    center = ((lo + hi) * np.float32(0.5)).astype(np.float32)
    d = V - center
    mesh_radius = float(np.sqrt((d * d).sum(1, dtype=np.float32).max()))
    out = bytearray()
    for v in (0, 1, 1, -1, [float(x) for x in lo] + [float(x) for x in hi], [float(x) for x in center],
              mesh_radius):     # standard mesh, frames, matFrames, parent
        _pack(out, v)
    UV2 = None if uvs2 is None else np.asarray(uvs2, np.float64).reshape(-1, 2)
    mesh_vecs = {
        "verts": _vec(V.astype("<f4").tobytes(), nv, 12),
        "tverts": _vec(TV.astype("<f4").tobytes(), nv, 8),
        "tverts2": _empty(8) if UV2 is None else _vec(
            np.stack([UV2[order, 0].astype(np.float32),
                      np.float32(1.0) - UV2[order, 1].astype(np.float32)], 1).astype("<f4").tobytes(), nv, 8),
        "colors": _empty(4) if colors is None else _vec(
            np.asarray(colors, np.uint8).reshape(-1, 4)[order].tobytes(), nv, 4),
        "norms": _vec(NN.astype("<f4").tobytes(), nv, 12),
        "primitives": _primitives(TM, nmat),
        "indices": _vec(idx.astype("<u4").tobytes(), ntri * 3, 4),
        "tangents": _vec(TG.astype("<f4").tobytes(), nv, 16),
    }
    for name in MESH_VECTORS:
        n, size, data = mesh_vecs.get(name, _empty(MESH_VECTOR_ELEM[name]))
        _pack(out, n), _pack(out, size), _pack(out, data)
    _pack(out, nv)                                         # vertsPerFrame
    _pack(out, 0)                                          # mesh flags
    return bytes(out), lo, hi, ntri


def _sorted_tris(tris, tri_materials, nmat):
    T = np.asarray(tris, np.int64).reshape(-1, 3)
    if len(T) == 0:
        raise CdaeError("no triangles")
    if tri_materials is None:
        if nmat != 1:
            raise CdaeError("several materials need tri_materials")
        return T, np.zeros(len(T), np.int64)
    TM = np.asarray(tri_materials, np.int64).reshape(-1)
    if len(TM) != len(T) or TM.min() < 0 or TM.max() >= nmat:
        raise CdaeError("tri_materials: one index into material per triangle")
    return T[np.argsort(TM, kind="stable")], np.sort(TM, kind="stable")


LOD_TOP_SIZE = 2.0          # px: the size of the one (or the last, coarsest) detail level, as the engine's own files


def encode(positions, tris, normals=None, uvs=None, material="bngEarthDae", uvs2=None,
           tangent_uvs=None, object_name="tile", tri_materials=None, colors=None, collision_only=False, lods=None,
           collisions=None):
    """Shape bytes for one static mesh, laid out exactly as the engine writes a compiled
    Collada file with the same geometry.

    positions (n,3), tris (m,3) vertex indices counter-clockwise seen from outside (the
    Collada / dae.py convention), normals (n,3) (None: computed like the engine does),
    uvs (n,2) and optional uvs2 (n,2) in Collada convention (v up), tangent_uvs (n,2) the
    parametrisation the tangents follow (default: uvs).
    Several materials: material a list of names and tri_materials (m,) each triangle's index in
    it; the triangles are grouped into one primitive per material (in list order). colors (n, 4)
    uint8 RGBA per vertex (materials with vertColor multiply their base colour by it).
    collision_only: the one detail is a collision detail ("Collision-1", negative size): never drawn, used by a
    TSStatic with collisionType "Collision Mesh" (a hidden "Visible Mesh" object got no collision at all). Its object
    is named "Colmesh" whatever object_name says: the engine only collides with collision-detail objects whose name
    starts with "Col" (stock shapes' Colmesh-1); named "buildings" it had no collision at all (in-game test boxes,
    2026-10-16: cars drove through every building since 2026-10-15).
    lods: coarser detail levels of the same object, the engine's own camera LOD (TSShapeInstance picks the finest
    detail whose size is at most the shape's projected size in pixels): a list of (size_px, dict of positions, tris
    and optional uvs / tri_materials / colors, same material list), sizes falling, the last one drawn down to
    LOD_TOP_SIZE px. The main mesh is drawn from lods[0]'s size up: lods=[(300, lite)] draws the full mesh while the
    shape spans 300 px or more, the lite one below.
    collisions: the shape's collision as separate meshes beside the drawn one, [(positions, tris, tri_materials or
    None)] (the same material list): objects "Colmesh<k>" in a "Collision-1" detail (each a null mesh in the drawn
    detail), for a TSStatic with collisionType "Collision Mesh". Windows builds each collision mesh in time ~quadratic
    in its triangles (collision-engine-facts: 51.2k tris 3.8 s, as 16 meshes 0.09 s): a big piece's collision in small
    meshes. Not with collision_only or lods."""
    if collision_only:
        object_name = "Colmesh"
        lods = None
    materials = [material] if isinstance(material, str) else list(material)
    if tri_materials is not None and len(materials) > 1:
        # only the materials a level uses: the engine makes a material instance for every material a shape lists
        # when it loads it (MatInstance::init / processMaterial were most of a new shape's load, ~0.4 ms each here;
        # the buildings listed all 15 of theirs, a far tile used a few)
        used = np.zeros(len(materials), bool)
        used[np.unique(np.asarray(tri_materials, np.int64))] = True
        for _size, d in (lods or []):
            if d.get("tri_materials") is not None:
                used[np.unique(np.asarray(d["tri_materials"], np.int64))] = True
        for _Pc, _Tc, TMc in (collisions or []):
            if TMc is not None:
                used[np.unique(np.asarray(TMc, np.int64))] = True
        if not used.all():
            new = np.cumsum(used) - 1
            materials = [m for m, u in zip(materials, used) if u]
            tri_materials = new[np.asarray(tri_materials, np.int64)]
            lods = [(size, dict(d, tri_materials=new[np.asarray(d["tri_materials"], np.int64)])
                     if d.get("tri_materials") is not None else d) for size, d in (lods or [])] or None
            collisions = [(Pc, Tc, None if TMc is None else new[np.asarray(TMc, np.int64)])
                          for Pc, Tc, TMc in (collisions or [])] or None
    nmat = len(materials)
    P = np.asarray(positions, np.float64).reshape(-1, 3)
    T, TM = _sorted_tris(tris, tri_materials, nmat)
    body0, lo, hi, ntri = _mesh_data(P, T, TM, nmat, normals, uvs, uvs2, tangent_uvs, colors)
    levels = [(None, body0, ntri)]
    for size, d in (lods or []):
        Pl = np.asarray(d["positions"], np.float64).reshape(-1, 3)
        Tl, TMl = _sorted_tris(d["tris"], d.get("tri_materials"), nmat)
        bl, lol, hil, ntl = _mesh_data(Pl, Tl, TMl, nmat, None, d.get("uvs"), None, None, d.get("colors"))
        lo, hi = np.minimum(lo, lol), np.maximum(hi, hil)
        levels.append((float(size), bl, ntl))
    cols = []
    for Pc, Tc, TMc in (collisions or []):
        Pc = np.asarray(Pc, np.float64).reshape(-1, 3)
        Tc, TMc = _sorted_tris(Tc, TMc, nmat)
        bc, loc, hic, ntc = _mesh_data(Pc, Tc, TMc, nmat, None, None, None, None, None)
        lo, hi = np.minimum(lo, loc), np.maximum(hi, hic)
        cols.append((bc, ntc))
    if cols and (collision_only or lods):
        raise CdaeError("collisions: not with collision_only or lods")
    # each level is drawn from the next coarser level's size up: the finest from lods[0]'s size, the last from 2 px
    sizes = [float(lv[0]) for lv in levels[1:]] + [LOD_TOP_SIZE]

    center = ((lo + hi) * np.float32(0.5)).astype(np.float32)
    half = ((hi - lo) * np.float32(0.5)).astype(np.float32)
    radius = float(np.sqrt(np.float32((half * half).sum(dtype=np.float32))))
    bounds = [float(x) for x in lo] + [float(x) for x in hi]
    centre = [float(x) for x in center]

    # collision only: a visible detail "detail2" whose mesh is a null mesh (draws nothing), then "Collision-1": with the
    # collision detail alone the engine logged "Shape with invalid detail levels" for every such shape (2026-10-17;
    # the buildings' lean collision meshes filled players' logs)
    nobj = 1 + len(cols)
    if cols:
        # the drawn object, then the collision objects; the drawn detail, then the collision detail
        names = [object_name] + [f"Colmesh{k}" for k in range(len(cols))] + [f"detail{int(sizes[0])}", "Collision-1"]
        details = _vec(struct.pack("<3i3fi2i2IfI", nobj, 0, 0, sizes[0], -1.0, -1.0, levels[0][2], 0, 0, 0, 0, 0.0, 0)
                       + struct.pack("<3i3fi2i2IfI", nobj + 1, 0, 1, -1.0, -1.0, -1.0, sum(n for _b, n in cols),
                                     0, 0, 0, 0, 0.0, 0), 2, 52)
    elif collision_only:
        names = [object_name, "detail2", "Collision-1"]
        details = (_vec(struct.pack("<3i3fi2i2IfI", 1, 0, 0, 2.0, -1.0, -1.0, 0, 0, 0, 0, 0, 0.0, 0)
                        + struct.pack("<3i3fi2i2IfI", 2, 0, 1, -1.0, -1.0, -1.0, ntri, 0, 0, 0, 0, 0.0, 0), 2, 52))
    else:
        names = [object_name] + [f"detail{int(sz)}" for sz in sizes]
        # detail: name, subShape, objectDetail, size, avgError, maxError, polyCount,
        # billboard dimension/detailLevel/equatorSteps/polarSteps/polarAngle/includePoles
        details = _vec(b"".join(struct.pack("<3i3fi2i2IfI", 1 + k, 0, k, sizes[k], -1.0, -1.0, levels[k][2],
                                            0, 0, 0, 0, 0.0, 0) for k in range(len(levels))), len(levels), 52)
    nmesh = 2 if (collision_only or cols) else len(levels)
    # object: name, numMeshes, startMeshIndex, node, nextSibling, firstDecal (each object a mesh per detail)
    objects = b"".join(struct.pack("<6i", k, nmesh, k * nmesh, 0, k + 1 if k + 1 < nobj else -1, -1) for k in range(nobj))
    shape_vecs = {
        # node: name, parent, firstObject, firstChild, nextSibling
        "nodes": _vec(struct.pack("<5i", 0, -1, 0, -1, -1), 1, 20),
        "objects": _vec(objects, nobj, 24),
        "subShapeFirstNode": _vec(struct.pack("<i", 0), 1, 4),
        "subShapeFirstObject": _vec(struct.pack("<i", 0), 1, 4),
        "subShapeNumNodes": _vec(struct.pack("<i", 1), 1, 4),
        "subShapeNumObjects": _vec(struct.pack("<i", nobj), 1, 4),
        "defaultRotations": _vec(struct.pack("<4h", 0, 0, 0, 0x7FFF), 1, 8),     # identity Quat16
        "defaultTranslations": _vec(struct.pack("<3f", 0, 0, 0), 1, 12),
        # object state: visibility, frame, matFrame
        "objectStates": _vec(struct.pack("<fii", 1.0, 0, 0) * nobj, nobj, 12),
        "details": details,
    }
    out = bytearray()
    for v in (LOD_TOP_SIZE, len(levels) - 1 if not collision_only else 0, radius, radius, centre, bounds):
        _pack(out, v)       # smallestVisibleSize/DL, radius, tubeRadius
    for name in SHAPE_VECTORS:
        n, size, data = shape_vecs.get(name, _empty(SHAPE_VECTOR_ELEM[name]))
        _pack(out, n), _pack(out, size), _pack(out, data)
    _pack(out, len(names))
    for s_ in names:
        _pack(out, s_)
    if cols:
        _pack(out, nmesh * nobj)                           # meshes: the drawn one and a null one, then each collision
        out += levels[0][1]                                # object's null one and its own
        _pack(out, 4)
        for body, _n in cols:
            _pack(out, 4)
            out += body
    else:
        _pack(out, nmesh)                                  # meshes (collision only: first the null mesh, type 4)
        if collision_only:
            _pack(out, 4)
        for _, body, _ in levels:
            out += body
    _pack(out, 0)                                          # sequences
    _pack(out, len(materials))                             # materials
    for name in materials:
        for v in (name, MAT_S_WRAP | MAT_T_WRAP, 0, 0xFFFFFFFF, 0xFFFFFFFF, 1.0, 1.0):
            _pack(out, v)

    header = packb({"info": INFO, "compression": False, "bodysize": len(out),
                    "objectNames": [object_name] + [f"Colmesh{k}" for k in range(len(cols))]})
    return struct.pack("<II", VERSION_WORD, len(header)) + header + bytes(out)


def _primitives(TM, nmat):
    """One indexed-triangle primitive per material present (start, count in indices, matIndex)."""
    out, n = b"", 0
    for k in range(nmat):
        idx = np.nonzero(TM == k)[0]
        if len(idx):
            out += struct.pack("<3i", int(idx[0]) * 3, len(idx) * 3, PRIM_TRIANGLES | PRIM_INDEXED | k)
            n += 1
    return _vec(out, n, 12)


def write(path, positions, tris, **kw):
    """encode() to path atomically (tmp + rename: the engine never sees a partial file)."""
    data = encode(positions, tris, **kw)
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(data)
    staging.replace(tmp, path)
    return len(data)


def engine_version(user_dir, limit=20):
    """Version word of the .cdae files the engine itself wrote under <user_dir>/temp/temp
    (its Collada cache), or None if there are none. A different word than VERSION_WORD means
    the game's shape format moved on, and the DAE path is the safe choice."""
    root = os.path.join(user_dir, "temp", "temp")
    seen = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.endswith(".cdae"):
                try:
                    v = read_version(os.path.join(dirpath, f))
                except (OSError, CdaeError):
                    continue
                seen[v] = seen.get(v, 0) + 1
                if sum(seen.values()) >= limit:
                    return max(seen, key=seen.get)
    return max(seen, key=seen.get) if seen else None
