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
        self.assertEqual(len(cdae.plain(ours_bytes)), os.path.getsize(FIX / f"{name}.cdae"))   # (written packed)
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


class DrawnPartsTest(unittest.TestCase):
    def test_oversized_draw_preserves_every_triangle_and_vertex_channel(self):
        from unittest.mock import patch
        p = np.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.], [1., 1., 1.], [2., 1., 0.]])
        t = np.array([[0, 1, 2], [2, 1, 3], [3, 1, 4]])
        kwargs = dict(material=["paint", "asphalt"], tri_materials=[1, 0, 0], uvs=p[:, :2],
                      tangent_uvs=p[:, 1:], uvs2=p[:, :2] / 2, colors=np.arange(20, dtype=np.uint8).reshape(-1, 4))
        whole = cdae.decode(cdae.encode(p, t, **kwargs))
        with patch.object(cdae, "VERTS_MAX", 3):
            split = cdae.decode(cdae.encode(p, t, split_drawn=True, **kwargs))
        self.assertEqual(whole["bounds"], split["bounds"])
        self.assertEqual(len(split["meshes"]), 3)
        def expanded(shape):
            attrs, materials = [], []
            for mesh in shape["meshes"]:
                a = cdae.mesh_arrays(mesh)
                rgba = np.frombuffer(mesh["colors"][2], np.uint8).reshape(-1, 4)
                vertex = np.column_stack([a[k] for k in ("verts", "norms", "tangents", "tverts", "tverts2")] + [rgba])
                attrs.append(vertex[a["indices"]])
                for _, count, mat in a["primitives"]:
                    materials.extend([mat] * (count // 3))
            return np.concatenate(attrs), materials
        expected, mats = expanded(whole)
        actual, split_mats = expanded(split)
        np.testing.assert_array_equal(actual, expected)
        self.assertEqual(split_mats, mats)

    def test_parts_are_objects_of_the_one_detail(self):
        """parts=: further drawn meshes, each its own object ("<name>2", ...) with one mesh in the one detail: a shape
        past the engine's 16-bit indices as several meshes under them (the far building packs, 2026-11-14)."""
        P = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], float)
        T = np.array([[0, 1, 2], [0, 2, 3]])
        mats = ["a", "b", "c"]
        parts = [{"positions": P + [20, 0, 0], "tris": T[:1], "tri_materials": [2]},
                 {"positions": P + [0, 0, 5], "tris": T, "tri_materials": [0, 2], "uvs": np.zeros((4, 2))}]
        d = cdae.decode(cdae.encode(P, T, material=mats, tri_materials=[0, 0], object_name="buildings", parts=parts))
        self.assertEqual(d["names"], ["buildings", "buildings2", "buildings3", "detail2"])
        self.assertEqual(d["header"]["objectNames"], ["buildings", "buildings2", "buildings3"])
        self.assertEqual([m["type"] for m in d["meshes"]], [0, 0, 0])
        self.assertEqual(d["objects"][0], 3)
        self.assertEqual(d["details"][0], 1)
        self.assertEqual([mt["name"] for mt in d["materials"]], ["a", "c"], "the materials the meshes use")
        ms = [cdae.mesh_arrays(m) for m in d["meshes"]]
        self.assertEqual([len(m["indices"]) // 3 for m in ms], [2, 1, 2])
        self.assertEqual([len(m["verts"]) for m in ms], [4, 3, 4])
        self.assertEqual([int(p[2]) & cdae.PRIM_MATERIAL_MASK for p in ms[1]["primitives"]], [1])
        self.assertAlmostEqual(d["bounds"][3], 30.0)
        self.assertAlmostEqual(d["bounds"][5], 5.0)
        with self.assertRaises(cdae.CdaeError):
            cdae.encode(P, T, material="m", parts=parts[:1], collision_only=True)
        # without parts: the shape as before
        self.assertEqual(cdae.encode(P, T, material="m", parts=None), cdae.encode(P, T, material="m"))


class VertexLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # All 65,535 vertices used. The last triangle shares an edge with the first,
        # and introduces just one more vertex in a different plane.
        n = cdae.VERTS_MAX
        i = np.arange(n + 1)
        cls.p = np.column_stack((i // 3, i % 3 == 1, i % 3 == 2)).astype(float)
        cls.t = np.vstack((i[:n].reshape(-1, 3), [1, 0, n]))
        cls.uv = cls.p[:, 1:].copy()
        cls.colors = np.column_stack((i % 256, i % 79, i % 41, i % 19)).astype(np.uint8)

    def test_attributes_and_materials_cross_the_cut(self):
        n = cdae.VERTS_MAX
        self.assertEqual(n, cdae.fast._ef.MESH_VERTICES_MAX)
        tm = np.zeros(len(self.t), np.int64)
        tm[-1] = 1
        shape = cdae.decode(cdae.encode(self.p, self.t, material=["a", "b"], tri_materials=tm,
                                        uvs=self.uv, uvs2=self.uv * 3, colors=self.colors))
        first, second = shape["meshes"]
        self.assertEqual((first["vertsPerFrame"], second["vertsPerFrame"]), (n, 3))
        for m, original, material in [(first, self.t[:-1], 0), (second, self.t[-1:], 1)]:
            a = cdae.mesh_arrays(m)
            inds = a["indices"].reshape(-1, 3)
            np.testing.assert_array_equal(a["verts"][inds], self.p[original[:, ::-1]])
            self.assertEqual(a["primitives"].tolist(), [[0, original.size, cdae.PRIM_INDEXED | material]])
        for field in ("verts", "norms", "tangents", "tverts", "tverts2", "colors"):
            def at(mesh, source_id):
                a = cdae.mesh_arrays(mesh)
                idx, = np.flatnonzero((a["verts"] == self.p[source_id]).all(axis=1))
                _, width, data = mesh[field]
                return data[idx * width:(idx + 1) * width]
            for i in (0, 1):
                self.assertEqual(at(first, i), at(second, i), field)

    def test_each_detail_gets_all_its_meshes(self):
        p, t = self.p, self.t
        one = dict(positions=p[:3], tris=t[:1])
        two = dict(positions=p, tris=t)
        for fine, lods, counts in [(two, [(300, one)], [0, 0, 0, 4]),
                                   (one, [(300, two)], [0, 0, 4, 0])]:
            d = cdae.decode(cdae.encode(**fine, lods=lods))
            self.assertEqual([m["type"] for m in d["meshes"]], counts)
            self.assertEqual(d["smallestVisibleDL"], 1)
        d = cdae.decode(cdae.encode(p, t, collisions=[(p, t, None)]))
        self.assertEqual(d["header"]["objectNames"], ["tile", "tile2", "Colmesh0", "Colmesh1"])
        self.assertEqual([m["type"] for m in d["meshes"]], [0, 4, 0, 4, 4, 0, 4, 0])
        d = cdae.decode(cdae.encode(p, t, collision_only=True))
        self.assertEqual(d["header"]["objectNames"], ["Colmesh", "Colmesh2"])
        self.assertEqual([m["type"] for m in d["meshes"]], [4, 0, 4, 0])
        # A large source array with a single addressable triangle still uses one object.
        self.assertEqual(len(cdae.decode(cdae.encode(p, t[:1]))["meshes"]), 1)

    def test_shell_closed_before_partition(self):
        # A long open ribbon. Its top fits, its skirts and bottom take it over the limit.
        import shells
        n = 17000
        i = np.arange(n)
        pos = np.column_stack((np.repeat(i, 2), np.tile([0, 1], n), np.zeros(2 * n)))
        j = i[:-1] * 2
        tri = np.column_stack((j, j + 2, j + 1, j + 1, j + 2, j + 3)).reshape(-1, 3)
        closed, faces, _, _ = shells.close(pos, tri)
        self.assertGreater(len(closed), cdae.VERTS_MAX)
        d = cdae.decode(cdae.encode(pos, tri, close=True))
        meshes = [cdae.mesh_arrays(m) for m in d["meshes"]]
        self.assertGreater(len(meshes), 1)
        actual = np.concatenate([a["verts"][a["indices"].reshape(-1, 3)] for a in meshes])
        np.testing.assert_array_equal(actual, closed.astype(np.float32)[faces[:, ::-1]])

    def test_packaged_shape_geometry_limits(self):
        # Authored props are written by offline asset tools, outside the tile exporter.
        # A Collada vertex can split at a normal/UV seam. Count complete corner tuples
        # per primitive, including material boundaries, across the entire geometry.
        mod = FIX.parents[2] / "mod"
        paths = list(mod.rglob("*.dae"))
        self.assertTrue(paths)
        for path in paths:
            for mesh in ET.parse(path).iter(NS + "mesh"):
                for pos in mesh.findall(f"{NS}vertices/{NS}input[@semantic='POSITION']"):
                    source = mesh.find(f"{NS}source[@id='{pos.get('source')[1:]}']")
                    accessor = source.find(f"{NS}technique_common/{NS}accessor")
                    self.assertLessEqual(int(accessor.get("count")), dae.IMPORT_VERTS_MAX, path)
                used = 0
                for prim in mesh:
                    indices = prim.findall(".//" + NS + "p")
                    if not indices:
                        continue
                    stride = 1 + max(int(i.get("offset", 0)) for i in prim.findall(NS + "input"))
                    corners = np.fromstring(" ".join(p.text or "" for p in indices), sep=" ", dtype=int)
                    used += len(np.unique(corners.reshape(-1, stride), axis=0))
                self.assertLessEqual(used, dae.IMPORT_VERTS_MAX, path)
        for path in mod.rglob("*.cdae"):
            for mesh in cdae.read(path)["meshes"]:
                if mesh["type"] == 0:
                    arrays = cdae.mesh_arrays(mesh)
                    self.assertLessEqual(len(arrays["verts"]), cdae.VERTS_MAX, path)
                    indices = arrays["indices"]
                    self.assertTrue(np.all((indices >= 0) & (indices < len(arrays["verts"]))), path)

    def test_grid_dae_has_separate_local_geometries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "road.dae")
            dae.write_grid_dae(path, self.p.ravel(), self.uv.ravel(), self.t.ravel(), material="road",
                               uvs2=self.uv.ravel())
            root = ET.parse(path)
        geos = root.findall(".//" + NS + "geometry")
        self.assertEqual(len(geos), 2)
        nodes = root.findall(".//" + NS + "node")
        self.assertEqual([n.find(NS + "instance_geometry").get("url") for n in nodes],
                         ["#" + g.get("id") for g in geos])
        for g, nv in zip(geos, [cdae.VERTS_MAX - 3, 6]):
            self.assertTrue(all(int(a.get("count")) == nv for a in g.iter(NS + "accessor")))
            tri = g.find(".//" + NS + "triangles")
            idx = np.fromstring(tri.find(NS + "p").text, dtype=int, sep=" ")
            self.assertLess(idx.max(), nv)
            self.assertEqual(len(idx), int(tri.get("count")) * 9)


class PackedTest(unittest.TestCase):
    """Shapes are written with their body as one zstd frame (the game reads them as its own compressed shapes,
    earthfast cdaez.rs); they decode as the plain file does, and plain() gives that file back."""

    def test_packed_round_trip(self):
        P = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 1], [10, 10, 2]], float)
        T = np.array([[0, 1, 2], [1, 3, 2]])
        data = cdae.encode(P, T, uvs=np.zeros((4, 2)), material="m")
        h, end = cdae.unpack_one(data, 8)
        self.assertTrue(h["compression"])
        self.assertEqual(data[end:end + 4], b"\x28\xb5\x2f\xfd")
        self.assertEqual(h["bodysize"], len(data) - end)
        flat = cdae.plain(data)
        self.assertFalse(cdae.unpack_one(flat, 8)[0]["compression"])
        a, b = cdae.decode(data), cdae.decode(flat)
        self.assertEqual(a["materials"], b["materials"])
        np.testing.assert_array_equal(cdae.mesh_arrays(a["meshes"][0])["verts"],
                                      cdae.mesh_arrays(b["meshes"][0])["verts"])
        self.assertEqual(cdae.plain(flat), flat)

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
