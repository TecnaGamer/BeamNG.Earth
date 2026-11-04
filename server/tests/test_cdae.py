"""server/cdae.py against engine-written golden files.

tests/fixtures/ holds pairs copied from a game session: a Collada file we wrote and the .cdae
the engine compiled from it (<user>/temp/temp/beamngearth/..., same base name, lower-cased):
  lodtest_16_0  16x16-cell grid, no normals in the DAE (the engine computed them), regular UVs
  far_7_0_0_3   a v1.1 far tile: 32x32 cells + skirts, normals, palette UVs
cdae.encode() on the same geometry must reproduce the engine's file: identical structure,
identical vertex arrays, the same triangles (the engine reorders them for its vertex cache, so
they are compared as a set), same size."""
import os
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdae  # noqa: E402
import dae  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
NS = "{http://www.collada.org/2005/11/COLLADASchema}"


def load_dae(path):
    root = ET.parse(path).getroot()
    arrs = {fa.get("id"): np.array(fa.text.split(), float) for fa in root.iter(NS + "float_array")}
    tri = root.find(f".//{NS}triangles")
    ninp = len(tri.findall(NS + "input"))
    p = np.array(tri.find(NS + "p").text.split(), int).reshape(-1, ninp)[:, 0].reshape(-1, 3)
    nrm = arrs.get("g0-nrm-a")
    return {"positions": arrs["g0-pos-a"].reshape(-1, 3), "uvs": arrs["g0-uv-a"].reshape(-1, 2),
            "normals": None if nrm is None else nrm.reshape(-1, 3), "tris": p,
            "material": root.find(f".//{NS}material").get("name")}


def canon(tris):
    """Triangles as a set of index triples, rotated to start at the smallest index."""
    k = np.argmin(tris, 1)
    return {tuple(np.roll(t, -s)) for t, s in zip(tris.tolist(), k)}


class GoldenTest(unittest.TestCase):
    def compare(self, name, tangents_exact):
        src = load_dae(FIX / f"{name}.dae")
        gold = cdae.read(FIX / f"{name}.cdae")
        ours_bytes = cdae.encode(src["positions"], src["tris"], normals=src["normals"], uvs=src["uvs"],
                                 material=src["material"])
        ours = cdae.decode(ours_bytes)
        self.assertEqual(len(ours_bytes), os.path.getsize(FIX / f"{name}.cdae"))
        self.assertEqual(ours["version"], gold["version"])
        self.assertEqual(gold["version"], cdae.VERSION_WORD)
        self.assertEqual(ours["header"], gold["header"])
        for k in ("smallestVisibleSize", "smallestVisibleDL", "names", "numSequences", "materials", "bounds"):
            self.assertEqual(ours[k], gold[k], k)
        for k in ("radius", "tubeRadius"):
            self.assertAlmostEqual(ours[k], gold[k], places=4)
        np.testing.assert_allclose(ours["center"], gold["center"], atol=1e-6)
        for k in cdae.SHAPE_VECTORS:
            self.assertEqual(ours[k], gold[k], k)
        self.assertEqual(len(ours["meshes"]), 1)
        om, gm = ours["meshes"][0], gold["meshes"][0]
        for k in ("type", "numFrames", "numMatFrames", "parentMesh", "bounds", "vertsPerFrame", "flags",
                  "primitives", "tverts2", "colors", "encodedNorms"):
            self.assertEqual(om[k], gm[k], k)
        self.assertAlmostEqual(om["radius"], gm["radius"], places=4)
        np.testing.assert_allclose(om["center"], gm["center"], atol=1e-6)
        for k in ("verts", "tverts", "indices", "tangents"):
            self.assertEqual(om[k][:2], gm[k][:2], k)          # count, element size
        oa, ga = cdae.mesh_arrays(om), cdae.mesh_arrays(gm)
        np.testing.assert_array_equal(oa["verts"], ga["verts"])
        np.testing.assert_array_equal(oa["tverts"], ga["tverts"])
        np.testing.assert_allclose(oa["norms"], ga["norms"], atol=2e-7)
        self.assertEqual(canon(oa["indices"].reshape(-1, 3)), canon(ga["indices"].reshape(-1, 3)))
        if tangents_exact:
            np.testing.assert_allclose(oa["tangents"], ga["tangents"], atol=1e-6)
        t = oa["tangents"]
        np.testing.assert_allclose(np.linalg.norm(t[:, :3], axis=1), 1.0, atol=1e-5)
        self.assertTrue(set(np.unique(t[:, 3])) <= {-1.0, 1.0})
        return ours

    def test_lodtest_16(self):
        self.compare("lodtest_16_0", tangents_exact=True)

    def test_far_tile(self):
        # Tangents: the engine derives them from the palette UVs (height/slope), which are
        # ill-conditioned; ours follow the same algorithm, so only their form is checked.
        self.compare("far_7_0_0_3", tangents_exact=False)

    def test_engine_winding_and_v_flip(self):
        """Explicitly: stored triangles are clockwise seen from +Z and v is flipped."""
        pos, uv, tri = dae.grid(4, 2.0, lambda x, y: 0.1 * x)
        d = cdae.decode(cdae.encode(pos, tri, uvs=uv, material="m"))
        a = cdae.mesh_arrays(d["meshes"][0])
        V, I = a["verts"].astype(float), a["indices"].reshape(-1, 3)
        n = np.cross(V[I[:, 1]] - V[I[:, 0]], V[I[:, 2]] - V[I[:, 0]])
        self.assertTrue((n[:, 2] < 0).all())
        self.assertTrue((a["norms"][:, 2] > 0.9).all())
        uvd = np.asarray(uv).reshape(-1, 2)
        self.assertAlmostEqual(float(a["tverts"][0, 1]), 1.0 - uvd[0, 1])


class FormatTest(unittest.TestCase):
    def test_msgpack_roundtrip_and_int_floats(self):
        vals = [0, 1, 127, 128, 255, 256, 65535, 65536, 2 ** 32 - 1, -1, -32, -33, -129, -40000,
                1.5, -2.25, 3.0, -4.0, "x" * 40, b"\x00" * 300, [1, 2.5], {"a": False}]
        b = cdae.packb(*vals)
        i, out = 0, []
        while i < len(b):
            v, i = cdae.unpack_one(b, i)
            out.append(v)
        self.assertEqual(out[:14], vals[:14])
        self.assertEqual(out[14:16], [1.5, -2.25])
        self.assertEqual(out[16:18], [3, -4])               # integral floats become ints, like the engine's
        self.assertIsInstance(out[16], int)
        self.assertEqual(out[18:], vals[18:])
        self.assertEqual(cdae.packb(128.0), b"\xcc\x80")
        self.assertEqual(cdae.packb(0.1), b"\xca" + struct.pack(">f", 0.1))

    def test_version_word_and_rejects(self):
        with tempfile.TemporaryDirectory() as d:
            pos, uv, tri = dae.grid(2, 1.0)
            p = os.path.join(d, "x.cdae")
            n = cdae.write(p, pos, tri, uvs=uv)
            self.assertEqual(os.path.getsize(p), n)
            self.assertFalse(os.path.exists(p + ".tmp"))
            self.assertEqual(cdae.read_version(p), 0x007C001F)
            with open(p, "rb") as f:
                data = bytearray(f.read())
            with self.assertRaises(cdae.CdaeError):
                cdae.decode(bytes(data[:-3]))
            data[0] = 30
            with self.assertRaises(cdae.CdaeError):
                cdae.decode(bytes(data))

    def test_engine_version_scan(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(cdae.engine_version(d))
            sub = os.path.join(d, "temp", "temp", "beamngearth", "s")
            os.makedirs(sub)
            pos, uv, tri = dae.grid(2, 1.0)
            cdae.write(os.path.join(sub, "a.cdae"), pos, tri)
            self.assertEqual(cdae.engine_version(d), cdae.VERSION_WORD)
            with open(os.path.join(sub, "b.cdae"), "wb") as f:
                f.write(struct.pack("<II", 0x007D0020, 0))
            with open(os.path.join(sub, "c.cdae"), "wb") as f:
                f.write(struct.pack("<II", 0x007D0020, 0))
            self.assertEqual(cdae.engine_version(d), 0x007D0020)

    def test_uv2(self):
        pos, uv, tri = dae.grid(2, 1.0)
        d = cdae.decode(cdae.encode(pos, tri, uvs=uv, uvs2=uv))
        a = cdae.mesh_arrays(d["meshes"][0])
        np.testing.assert_array_equal(a["tverts2"], a["tverts"])

    def test_only_used_materials_listed(self):
        # the engine makes a material instance for every material a shape lists when it loads it: a shape lists the
        # ones its levels use, in the given order, each primitive pointing at its own (2026-10-20)
        pos, uv, tri = dae.grid(2, 1.0)
        tri = np.asarray(tri).reshape(-1, 3)
        tm = np.array([0, 3] * (len(tri) // 2) + [0] * (len(tri) % 2))
        lod = {"positions": pos, "tris": tri, "uvs": uv, "tri_materials": np.full(len(tri), 2)}
        d = cdae.decode(cdae.encode(pos, tri, uvs=uv, material=["a", "b", "c", "d", "e"], tri_materials=tm,
                                    lods=[(100.0, lod)]))
        self.assertEqual([m["name"] for m in d["materials"]], ["a", "c", "d"])
        prims = [struct.unpack_from("<3i", cdae.mesh_arrays(d["meshes"][0])["primitives"], k * 12)
                 for k in range(2)]
        self.assertEqual(sorted(p[2] & cdae.PRIM_MATERIAL_MASK for p in prims), [0, 2], "a and d")
        lod_prims = cdae.mesh_arrays(d["meshes"][1])["primitives"]
        self.assertEqual(struct.unpack_from("<3i", lod_prims, 0)[2] & cdae.PRIM_MATERIAL_MASK, 1, "c")
        same = cdae.encode(pos, tri, uvs=uv, material=["a", "b"], tri_materials=np.array([0, 1] * (len(tri) // 2)
                                                                                             + [0] * (len(tri) % 2)))
        self.assertEqual([m["name"] for m in cdae.decode(same)["materials"]], ["a", "b"], "all used: as given")

    def test_collision_only_object_is_colmesh(self):
        # the engine collides only with collision-detail objects named "Col..." (in-game test, 2026-10-16)
        pos, uv, tri = dae.grid(2, 1.0)
        d = cdae.decode(cdae.encode(pos, tri, uvs=uv, object_name="buildings", collision_only=True))
        self.assertEqual(d["names"], ["Colmesh", "detail2", "Collision-1"])
        self.assertEqual(d["header"]["objectNames"], ["Colmesh"])
        # a visible detail with a null mesh first (no "Shape with invalid detail levels" in the log, 2026-10-17), the
        # triangles in the collision detail's mesh
        self.assertEqual([m["type"] for m in d["meshes"]], [4, 0])
        n, size, raw = d["details"]
        dets = [struct.unpack_from("<3i3fi", raw, k * size) for k in range(n)]
        self.assertEqual([(x[1], x[2], x[3]) for x in dets], [(0, 0, 2.0), (0, 1, -1.0)])
        self.assertEqual(len(cdae.mesh_arrays(d["meshes"][1])["indices"]), np.asarray(tri).size)
        d = cdae.decode(cdae.encode(pos, tri, uvs=uv, object_name="buildings"))
        self.assertEqual(d["names"], ["buildings", "detail2"])


class SplitCollisionTest(unittest.TestCase):
    def test_drawn_mesh_and_collision_meshes(self):
        """collisions=: the drawn object and an object "Colmesh<k>" per collision mesh, in a "Collision-1" detail
        (Windows builds each collision mesh in time ~quadratic in its triangles: a big shape's collision in small
        meshes, fine ground 2026-11-01)."""
        P = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], float)
        T = np.array([[0, 1, 2], [0, 2, 3]])
        cols = [(P[:3], np.array([[0, 1, 2]]), None), (P[[0, 2, 3]], np.array([[0, 1, 2]]), None)]
        d = cdae.decode(cdae.encode(P, T, material="m", object_name="fine", collisions=cols))
        self.assertEqual(d["names"][:3], ["fine", "Colmesh0", "Colmesh1"])
        self.assertIn("Collision-1", d["names"])
        types = [m["type"] for m in d["meshes"]]
        self.assertEqual(types, [0, 4, 4, 0, 4, 0], "drawn, null; then each collision object's null and its own")
        cm = [cdae.mesh_arrays(m) for m in d["meshes"] if m["type"] == 0]
        self.assertEqual([len(m["indices"]) // 3 for m in cm], [2, 1, 1])
        with self.assertRaises(cdae.CdaeError):
            cdae.encode(P, T, collision_only=True, collisions=cols)
        # (a material list the drawn mesh uses part of: the collisions' indices follow its pruning)
        mats = ["a", "b", "c"]
        d = cdae.decode(cdae.encode(P, T, material=mats, tri_materials=[2, 2], object_name="fine",
                                    collisions=[(P[:3], np.array([[0, 1, 2]]), [2])]))
        self.assertEqual(len(d["meshes"]), 4)


if __name__ == "__main__":
    unittest.main()


class LodTest(unittest.TestCase):
    def test_coarser_details(self):
        # the engine's camera LOD: the full mesh from 300 px up, a coarser one below, drawn down to 2 px
        P = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 3]], float)
        b = cdae.encode(P, [[0, 1, 2], [1, 3, 2]], lods=[(300, {"positions": P[:3] * 2, "tris": [[0, 1, 2]]})])
        d = cdae.decode(b)
        self.assertEqual(d["names"], ["tile", "detail300", "detail2"])
        self.assertEqual((d["smallestVisibleSize"], d["smallestVisibleDL"]), (2.0, 1))
        n, _, raw = d["details"]
        sizes = [struct.unpack_from("<3i3fi", raw, 52 * k) for k in range(n)]
        self.assertEqual([(s[2], s[3], s[6]) for s in sizes], [(0, 300.0, 2), (1, 2.0, 1)])
        self.assertEqual(len(d["meshes"]), 2)
        self.assertEqual(d["bounds"][3:5], [2.0, 2.0], "the bounds cover every detail")
