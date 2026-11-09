"""threedmr.py: the 3dmr=<id> tag -> 3dmr.eu GLB -> .cdae/.png/.materials.json pipeline.

The glTF decoding tests use small GLBs built in the tests themselves (_glb, _textured_glb): textured
multi-material models and the extensions 3dmr.eu models use (KHR_draco_mesh_compression,
KHR_mesh_quantization, non-finite vertices) - no model from the library is shipped with the tests."""

import inspect
import json
import logging
import math
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blockworker  # noqa: E402
import cdae  # noqa: E402
import revisit  # noqa: E402
import staging  # noqa: E402
import threedmr  # noqa: E402


def _glb(gltf, bin_bytes=b""):
    """A minimal .glb (JSON chunk + BIN chunk) of a hand-built gltf dict, for the extensions neither real
    fixture happens to exercise (Draco, quantization, non-finite vertices)."""
    js = json.dumps(gltf).encode()
    body = (
        struct.pack("<I4s", len(js), b"JSON")
        + js
        + struct.pack("<I4s", len(bin_bytes), b"BIN\x00")
        + bin_bytes
    )
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


class TagTest(unittest.TestCase):
    def test_tag_present(self):
        self.assertEqual(threedmr.tag_3dmr('{"3dmr": "24"}'), "24")

    def test_none(self):
        self.assertIsNone(threedmr.tag_3dmr(None))

    def test_garbage_json(self):
        self.assertIsNone(threedmr.tag_3dmr("{not json"))
        self.assertIsNone(threedmr.tag_3dmr(""))

    def test_tag_absent(self):
        self.assertIsNone(threedmr.tag_3dmr('{"other": 1}'))


def _image(fmt, rgb, size=8):
    """A small solid-colour image (bytes) in fmt ("JPEG"/"PNG"), for a synthetic model's texture."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (size, size), rgb).save(buf, fmt)
    return buf.getvalue()


def _textured_glb(parts, translation=(0.0, 0.0, 0.0), scale=1.0):
    """A .glb of axis-aligned boxes, one primitive each with its own material and embedded image: parts
    [(min (x, y, z), max (x, y, z), image format "JPEG"/"PNG", rgb)] in glTF's Y-up frame, all on one node
    with translation/scale (a stand-in for a textured model as 3dmr.eu serves it, without shipping one)."""
    blob = bytearray()
    views, accessors, prims, materials, textures, images = [], [], [], [], [], []

    def view(data):
        while len(blob) % 4:
            blob.append(0)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)})
        blob.extend(data)
        return len(views) - 1

    for k, (lo, hi, fmt, rgb) in enumerate(parts):
        corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
        uvs = [((i >> 2) & 1, (i >> 1) & 1) for i in range(8)]
        faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
                 (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
        accessors.append({"bufferView": view(b"".join(struct.pack("<3f", *c) for c in corners)),
                          "componentType": 5126, "count": 8, "type": "VEC3",
                          "min": list(lo), "max": list(hi)})
        accessors.append({"bufferView": view(b"".join(struct.pack("<2f", *t) for t in uvs)),
                          "componentType": 5126, "count": 8, "type": "VEC2"})
        accessors.append({"bufferView": view(b"".join(struct.pack("<H", i) for f in faces for i in f)),
                          "componentType": 5123, "count": 36, "type": "SCALAR"})
        images.append({"bufferView": view(_image(fmt, rgb)), "mimeType": f"image/{fmt.lower()}"})
        textures.append({"source": k})
        materials.append({"pbrMetallicRoughness": {"baseColorTexture": {"index": k}}})
        prims.append({"attributes": {"POSITION": 3 * k, "TEXCOORD_0": 3 * k + 1}, "indices": 3 * k + 2,
                      "material": k})
    while len(blob) % 4:
        blob.append(0)
    gltf = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "translation": list(translation), "scale": [scale] * 3}],
        "meshes": [{"primitives": prims}],
        "materials": materials,
        "textures": textures,
        "images": images,
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(blob)}],
    }
    return _glb(gltf, bytes(blob))


class GltfMeshTest(unittest.TestCase):
    def test_textured_model(self):
        # a monument-like model: a wide base and a tall shaft, Y-up, moved and scaled by its node
        glb = _textured_glb([((-10, 0, -5), (10, 2, 5), "JPEG", (200, 180, 150)),
                             ((-2, 2, -2), (2, 20, 2), "JPEG", (90, 160, 200))],
                            translation=(5.0, 0.0, 0.0), scale=2.0)
        positions, uvs, tris, normals, textures, tri_materials = threedmr.gltf_mesh(glb, model_id=0)
        self.assertEqual(len(positions), 16)
        self.assertEqual(len(tris), 24)
        self.assertEqual(uvs.shape, (len(positions), 2))
        self.assertEqual(tris.shape[1], 3)
        pmin, pmax = positions.min(0), positions.max(0)
        self.assertAlmostEqual(pmin[0], -15.0)          # node scale, then translation
        self.assertAlmostEqual(pmax[0], 25.0)
        self.assertAlmostEqual(pmin[1], -10.0)          # glTF -z is BeamNG's y
        self.assertAlmostEqual(pmax[1], 10.0)
        self.assertAlmostEqual(pmin[2], 0.0)            # glTF y (up) is BeamNG's z
        self.assertAlmostEqual(pmax[2], 40.0)
        self.assertEqual(tri_materials.shape, (len(tris),))
        self.assertTrue(any(tb is not None for tb, _, _f, _ha, _ac, _ds in textures))
        self.assertIn("jpeg", [te for _, te, _f, _ha, _ac, _ds in textures if te is not None])
        self.assertIsNone(normals)                      # (the boxes carry no NORMAL)


class GltfMeshSmallTest(unittest.TestCase):
    PARTS = [((0, 0, 0), (4, 3, 4), "PNG", (180, 40, 40)),
             ((0, 3.5, 0), (4, 5, 4), "JPEG", (40, 40, 180)),
             ((5, 0, 0), (7, 8, 2), "PNG", (40, 160, 40))]

    def test_slots_in_range(self):
        positions, uvs, tris, normals, textures, tri_materials = threedmr.gltf_mesh(
            _textured_glb(self.PARTS), model_id=0
        )
        self.assertEqual(len(tris), 36)
        self.assertEqual(uvs.shape, (len(positions), 2))
        self.assertEqual(tri_materials.shape, (len(tris),))
        self.assertTrue(
            (tri_materials >= 0).all() and (tri_materials < len(textures)).all()
        )

    def test_several_materials_keep_their_own_texture(self):
        # a model with several materials/images (3dmr.eu id 20 is one): every primitive must end up pointing at
        # its own material's texture, not all sharing the first textured primitive's (the pre-fix behaviour)
        _positions, _uvs, tris, _normals, textures, tri_materials = threedmr.gltf_mesh(
            _textured_glb(self.PARTS), model_id=0
        )
        self.assertEqual(len(textures), 3)
        used = sorted(set(tri_materials.tolist()))
        self.assertEqual(used, [0, 1, 2], "every material slot is used by its own triangles")
        distinct_images = {tb for tb, _, _f, _ha, _ac, _ds in textures if tb is not None}
        self.assertEqual(len(distinct_images), 3, "each slot references its own image")
        self.assertEqual(sorted(te for _, te, _f, _ha, _ac, _ds in textures), ["jpeg", "png", "png"])


def _tri_mesh(positions, triangles, normalized=False, component_type=5126):
    """A minimal valid one-primitive gltf dict (+ its BIN bytes) for POSITION (float32 unless component_type/
    normalized say otherwise) + SCALAR ubyte indices, mesh 0 on node 0, identity transform. Callers add their
    own "scale"/"matrix" to nodes[0] or swap in componentType/normalized for KHR_mesh_quantization."""
    dtype = {5126: "f", 5123: "H", 5121: "B"}[component_type]
    pos_bytes = b"".join(struct.pack(f"<3{dtype}", *v) for v in positions)
    idx_bytes = b"".join(struct.pack("<B", i) for tri in triangles for i in tri)
    gltf = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "accessors": [
            {
                "componentType": component_type,
                "type": "VEC3",
                "count": len(positions),
                "bufferView": 0,
                **({"normalized": True} if normalized else {}),
            },
            {
                "componentType": 5121,
                "type": "SCALAR",
                "count": len(triangles) * 3,
                "bufferView": 1,
            },
        ],
        "bufferViews": [
            {"byteOffset": 0, "byteLength": len(pos_bytes)},
            {"byteOffset": len(pos_bytes), "byteLength": len(idx_bytes)},
        ],
    }
    return gltf, pos_bytes + idx_bytes


class DracoTest(unittest.TestCase):
    def test_all_primitives_draco_is_skipped_not_a_crash(self):
        gltf = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "extensionsUsed": ["KHR_draco_mesh_compression"],
            "meshes": [
                {
                    "primitives": [
                        {
                            "attributes": {"POSITION": 0},
                            "indices": 0,
                            "extensions": {
                                "KHR_draco_mesh_compression": {
                                    "bufferView": 0,
                                    "attributes": {"POSITION": 0},
                                }
                            },
                        },
                    ]
                }
            ],
        }
        with self.assertLogs(threedmr.log, level="WARNING") as cm:
            with self.assertRaises(threedmr.UnsupportedModelError):
                threedmr.gltf_mesh(_glb(gltf), model_id=49)
        self.assertTrue(any("DRACO" in m and "skipped" in m for m in cm.output))

    def test_missing_buffer_view_is_treated_as_draco_without_the_extension_flag(self):
        # a POSITION accessor with no bufferView at all (the actual data sits in an extension this decoder
        # does not read) must not raise a raw KeyError up through gltf_mesh
        gltf = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "accessors": [{"componentType": 5126, "type": "VEC3", "count": 3}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 0}]}],
        }
        with self.assertRaises(threedmr.UnsupportedModelError):
            threedmr.gltf_mesh(_glb(gltf), model_id=49)

    def test_draco_primitives_skipped(self):
        # extensionsUsed declares Draco, and the primitive's own POSITION accessor carries no bufferView (the
        # real vertex data sits inside the KHR_draco_mesh_compression extension, which this decoder does not
        # read): gltf_mesh must not raise a raw KeyError/exception for this, and since every primitive in the
        # model is Draco-only, the model as a whole is unsupported
        gltf = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "extensionsUsed": ["KHR_draco_mesh_compression"],
            "accessors": [{"componentType": 5126, "type": "VEC3", "count": 3}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 0}]}],
        }
        with self.assertRaises(threedmr.UnsupportedModelError):
            threedmr.gltf_mesh(_glb(gltf), model_id=49)

    def test_one_draco_primitive_among_valid_ones_is_only_that_one_skipped(self):
        good, bin_bytes = _tri_mesh([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)])
        good["meshes"][0]["primitives"].append(
            {
                "attributes": {"POSITION": 2},
                "indices": 0,
                "extensions": {
                    "KHR_draco_mesh_compression": {
                        "bufferView": 0,
                        "attributes": {"POSITION": 0},
                    }
                },
            }
        )
        good["accessors"].append({"componentType": 5126, "type": "VEC3", "count": 3})
        with self.assertLogs(threedmr.log, level="WARNING"):
            positions, _uvs, tris, _normals, _textures, _tm = threedmr.gltf_mesh(
                _glb(good, bin_bytes), model_id=49
            )
        self.assertEqual(len(positions), 3)
        self.assertEqual(len(tris), 1)


class QuantizationTest(unittest.TestCase):
    def test_normalized_u16_position_is_scaled_by_the_node(self):
        # KHR_mesh_quantization: POSITION is normalized u16 ([0, 65535] -> [0, 1]); the real-world size comes
        # from the node's own scale, applied on top - not from the raw integer (observed on 3dmr.eu id 41:
        # raw u16 read directly as metres gave a ~5000 m wide "building")
        gltf, bin_bytes = _tri_mesh(
            [(0, 0, 0), (65535, 0, 0), (0, 65535, 0)],
            [(0, 1, 2)],
            normalized=True,
            component_type=5123,
        )
        gltf["nodes"][0]["scale"] = [0.1, 0.1, 0.1]
        positions, _uvs, _tris, _normals, _textures, _tm = threedmr.gltf_mesh(
            _glb(gltf, bin_bytes)
        )
        self.assertLess(
            float(np.abs(positions).max()),
            1.0,
            "scaled down to sane metres, not raw-u16-sized",
        )
        self.assertAlmostEqual(float(np.abs(positions).max()), 0.1, places=5)

    def test_quantization_scale_applied(self):
        # same KHR_mesh_quantization scenario, phrased as a general raw-value * node-scale check rather than
        # one hardcoded pair of numbers: the real-world size must track the node's scale, not the raw u16 range
        raw_max = 40000
        gltf, bin_bytes = _tri_mesh(
            [(0, 0, 0), (raw_max, 0, 0), (0, raw_max, 0)],
            [(0, 1, 2)],
            normalized=True,
            component_type=5123,
        )
        gltf["nodes"][0]["scale"] = [0.1, 0.1, 0.1]
        positions, _uvs, _tris, _normals, _textures, _tm = threedmr.gltf_mesh(
            _glb(gltf, bin_bytes)
        )
        expected = (raw_max / 65535.0) * 0.1
        self.assertAlmostEqual(float(np.abs(positions).max()), expected, places=5)
        self.assertLess(
            float(np.abs(positions).max()),
            1.0,
            "metres, not thousands of raw quantized units",
        )

    def test_node_matrix_scale_also_applies(self):
        gltf, bin_bytes = _tri_mesh([(0, 0, 0), (10, 0, 0), (0, 10, 0)], [(0, 1, 2)])
        gltf["nodes"][0]["matrix"] = [
            0.5,
            0,
            0,
            0,
            0,
            0.5,
            0,
            0,
            0,
            0,
            0.5,
            0,
            0,
            0,
            0,
            1,
        ]
        positions, _uvs, _tris, _normals, _textures, _tm = threedmr.gltf_mesh(
            _glb(gltf, bin_bytes)
        )
        self.assertAlmostEqual(float(np.abs(positions).max()), 5.0, places=5)


class NonFiniteVertexTest(unittest.TestCase):
    def test_nan_vertex_and_its_triangle_are_dropped(self):
        positions = [
            (0, 0, 0),
            (1, 0, 0),
            (0, 1, 0),
            (float("nan"), float("nan"), float("nan")),
        ]
        gltf, bin_bytes = _tri_mesh(positions, [(0, 1, 2), (1, 2, 3)])
        with self.assertLogs(threedmr.log, level="WARNING") as cm:
            pos, _uvs, tris, _normals, _textures, _tm = threedmr.gltf_mesh(
                _glb(gltf, bin_bytes), model_id=28
            )
        self.assertEqual(len(pos), 3, "the NaN vertex is dropped")
        self.assertEqual(len(tris), 1, "the triangle that referenced it is dropped too")
        self.assertTrue(np.isfinite(pos).all())
        self.assertTrue(any("non-finite" in m for m in cm.output))


class WedgeMaskTest(unittest.TestCase):
    """_wedge_mask: drops an author's stray giant flat "wedge" triangle (3dmr.eu id 3, Petronas) without
    touching real ground-plate/mockup geometry (id 6) or ordinary sloped/building geometry."""

    def _box_mesh(self):
        # an 8-vertex, 12-triangle unit cube scaled to 10x10x10 m, every vertex shared by several triangles
        # (not isolated) - the "real building geometry" a wedge check must never touch
        c = [[x, y, z] for x in (0.0, 10.0) for y in (0.0, 10.0) for z in (0.0, 10.0)]
        idx = {tuple(v): i for i, v in enumerate(c)}

        def i(x, y, z):
            return idx[(x, y, z)]

        tris = [
            (i(0, 0, 0), i(10, 0, 0), i(10, 10, 0)),
            (i(0, 0, 0), i(10, 10, 0), i(0, 10, 0)),  # bottom
            (i(0, 0, 10), i(10, 10, 10), i(10, 0, 10)),
            (i(0, 0, 10), i(0, 10, 10), i(10, 10, 10)),  # top
            (i(0, 0, 0), i(10, 0, 10), i(10, 0, 0)),
            (i(0, 0, 0), i(0, 0, 10), i(10, 0, 10)),  # sides
            (i(10, 0, 0), i(10, 0, 10), i(10, 10, 10)),
            (i(10, 0, 0), i(10, 10, 10), i(10, 10, 0)),
            (i(10, 10, 0), i(10, 10, 10), i(0, 10, 10)),
            (i(10, 10, 0), i(0, 10, 10), i(0, 10, 0)),
            (i(0, 10, 0), i(0, 10, 10), i(0, 0, 10)),
            (i(0, 10, 0), i(0, 0, 10), i(0, 0, 0)),
        ]
        return np.array(c, np.float64), np.array(tris, np.int64)

    def test_wedge_bbox_corners(self):
        positions, tris = self._box_mesh()
        # two new, otherwise-unused vertices at bbox corners (0,0,0)-(200,150,0): a giant flat rectangle,
        # split into 2 triangles, used nowhere else - id 3's own signature
        wedge_verts = np.array(
            [[0.0, 0.0, 0.0], [200.0, 0.0, 0.0], [200.0, 150.0, 0.0], [0.0, 150.0, 0.0]]
        )
        positions = np.concatenate([positions, wedge_verts], 0)
        base = len(positions) - 4
        wedge_tris = np.array([[base, base + 1, base + 2], [base, base + 2, base + 3]])
        tris = np.concatenate([tris, wedge_tris], 0)

        mask = threedmr._wedge_mask(positions, tris)
        self.assertEqual(mask.sum(), 2, "both wedge triangles dropped")
        self.assertTrue(mask[-2] and mask[-1])
        self.assertFalse(mask[:-2].any(), "the cube's own triangles are untouched")

    def test_keeps_ground_plate(self):
        positions, tris = self._box_mesh()
        # a 27x28 m plate (775 sq m, > the 500 area threshold), flat, far from the cube's bbox corners, whose
        # 4 corners are each also used by one more big (> 10 sq m) triangle elsewhere - real mockup geometry
        # touching it, unlike a stray wedge
        plate = np.array(
            [[50.0, 50.0, 0.0], [77.0, 50.0, 0.0], [77.0, 78.0, 0.0], [50.0, 78.0, 0.0]]
        )
        positions = np.concatenate([positions, plate], 0)
        base = len(positions) - 4
        plate_tris = np.array([[base, base + 1, base + 2], [base, base + 2, base + 3]])
        tris = np.concatenate([tris, plate_tris], 0)

        neighbor = np.array(
            [[200.0, 50.0, 0.0], [227.0, 50.0, 0.0], [227.0, 78.0, 0.0]]
        )
        positions = np.concatenate([positions, neighbor], 0)
        nbase = len(positions) - 3
        # shares vertex `base` (one plate corner) with a real, sizeable (> 10 sq m) triangle
        neighbor_tri = np.array([[base, nbase, nbase + 1]])
        tris = np.concatenate([tris, neighbor_tri], 0)

        mask = threedmr._wedge_mask(positions, tris)
        plate_tri_idx = [len(tris) - 3, len(tris) - 2]
        self.assertFalse(mask[plate_tri_idx[0]])
        self.assertFalse(mask[plate_tri_idx[1]])

    def test_keeps_slope(self):
        positions, tris = self._box_mesh()
        # a large, isolated, bbox-corner-ish triangle but NOT flat (16 m of Z range) - a roof/wall slope, not
        # a wedge
        slope = np.array([[0.0, 0.0, 0.0], [200.0, 0.0, 0.0], [200.0, 150.0, 16.0]])
        positions = np.concatenate([positions, slope], 0)
        base = len(positions) - 3
        tris = np.concatenate([tris, np.array([[base, base + 1, base + 2]])], 0)

        mask = threedmr._wedge_mask(positions, tris)
        self.assertFalse(mask[-1])

    def test_keeps_normal_mesh(self):
        positions, tris = self._box_mesh()
        mask = threedmr._wedge_mask(positions, tris)
        self.assertEqual(mask.sum(), 0)


class DedupTrisTest(unittest.TestCase):
    """_dedup_tris / gltf_mesh: 3dmr.eu id 22 (Dragon Bridge) carries each of its triangles twice, under two
    unrelated sets of vertex indices but identical world-space coordinates - the GPU then rasterizes a
    random one of each coincident pair every frame, so the bridge flickers on its own turntable with no stub
    road anywhere in the picture. Ordinary vertex dedup never catches this: the coordinates match but the
    indices don't, so nothing ever merges the vertices themselves."""

    def _three_prim_mesh(self, tri_a, tri_b, tri_c, materials=(0, 1, 0)):
        """3 primitives, one triangle each, with per-primitive materials - like MultiTextureTest's mesh, but
        materials only (no textures), so a surviving duplicate's own material slot can be checked."""
        tris = [tri_a, tri_b, tri_c]
        chunks = []
        for tri in tris:
            chunks.append(b"".join(struct.pack("<3f", *v) for v in tri))
            chunks.append(b"".join(struct.pack("<B", i) for i in (0, 1, 2)))
        offsets, off = [], 0
        for c in chunks:
            offsets.append(off)
            off += len(c)
        bin_bytes = b"".join(chunks)
        buffer_views = [
            {"byteOffset": offsets[i], "byteLength": len(chunks[i])}
            for i in range(len(chunks))
        ]
        accessors = []
        for p in range(3):
            accessors.append(
                {
                    "componentType": 5126,
                    "type": "VEC3",
                    "count": 3,
                    "bufferView": 2 * p,
                }
            )
            accessors.append(
                {
                    "componentType": 5121,
                    "type": "SCALAR",
                    "count": 3,
                    "bufferView": 2 * p + 1,
                }
            )
        gltf = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "meshes": [
                {
                    "primitives": [
                        {
                            "attributes": {"POSITION": 2 * p},
                            "indices": 2 * p + 1,
                            "material": materials[p],
                        }
                        for p in range(3)
                    ]
                }
            ],
            "accessors": accessors,
            "bufferViews": buffer_views,
            "materials": [{}, {}],
        }
        return _glb(gltf, bin_bytes)

    def test_duplicate_same_winding_is_dropped(self):
        tri_a = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        tri_b_dup = [
            (0, 0, 0),
            (1, 0, 0),
            (0, 1, 0),
        ]  # same geometry, different indices, material 0
        tri_c_unique = [(5, 5, 5), (6, 5, 5), (5, 6, 5)]
        glb = self._three_prim_mesh(tri_a, tri_b_dup, tri_c_unique, materials=(0, 0, 0))
        with self.assertLogs(threedmr.log, level="INFO") as cm:
            _pos, _uvs, tris, _normals, _textures, tri_materials = threedmr.gltf_mesh(
                glb, model_id=22
            )
        self.assertEqual(len(tris), 2, "the duplicate triangle is dropped")
        self.assertEqual(
            tri_materials.tolist(),
            [0, 0],
            "the survivor keeps the FIRST occurrence's own material (slot 0), not the dropped one's",
        )
        self.assertTrue(any("duplicate" in m for m in cm.output))

    def test_duplicate_other_material_same_facing_is_dropped(self):
        # two coincident faces facing ONE way are drawn into one pixel twice whatever their materials: z-fight
        tri_a = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        tri_b_dup_other_mat = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        tri_c_unique = [(5, 5, 5), (6, 5, 5), (5, 6, 5)]
        glb = self._three_prim_mesh(
            tri_a, tri_b_dup_other_mat, tri_c_unique, materials=(0, 1, 0)
        )
        _pos, _uvs, tris, _normals, _textures, tri_materials = threedmr.gltf_mesh(
            glb, model_id=22
        )
        self.assertEqual(len(tris), 2)
        self.assertEqual(tri_materials.tolist(), [0, 0], "the first occurrence survives")

    def test_back_face_other_material_is_kept_two_sided_wall(self):
        # the same 3 points facing the OTHER way under another material: a deck's own top and its underside
        # (the FV15 report's transparent, collision-less road surface) - both stay, each slot one-sided
        tri_a = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        tri_b_back = [(0, 1, 0), (1, 0, 0), (0, 0, 0)]
        tri_c_unique = [(5, 5, 5), (6, 5, 5), (5, 6, 5)]
        glb = self._three_prim_mesh(tri_a, tri_b_back, tri_c_unique, materials=(0, 1, 0))
        _pos, _uvs, tris, _normals, textures, tri_materials = threedmr.gltf_mesh(glb, model_id=22)
        self.assertEqual(len(tris), 3, "the two-sided wall's both sides are kept")
        self.assertEqual(tri_materials.tolist(), [0, 1, 0])
        self.assertEqual(
            [t[5] for t in textures], [False, False],
            "both slots one-sided: drawn doubleSided, the two faces z-fight (the Dragon Bridge's flicker)",
        )

    def test_back_face_same_material_is_kept_when_one_sided(self):
        # 3dmr.eu id 22: 27,665 such pairs - a closed two-sided model, not duplicates
        tri_a = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        tri_b_back = [(0, 1, 0), (1, 0, 0), (0, 0, 0)]
        tri_c_back = [(0, 0, 0), (0, 1, 0), (1, 0, 0)]  # (a third face: same points, same side as tri_b)
        glb = self._three_prim_mesh(tri_a, tri_b_back, tri_c_back, materials=(0, 0, 0))
        _pos, _uvs, tris, _normals, textures, tri_materials = threedmr.gltf_mesh(glb, model_id=22)
        self.assertEqual(len(tris), 2, "front and back kept, the same-facing repeat dropped")
        self.assertEqual(textures[0][5], False)

    def test_back_face_of_a_double_sided_material_is_dropped(self):
        # the glTF material is doubleSided: it draws each face from both sides already - its back face is the
        # same surface twice
        tri_a = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        tri_b_back = [(0, 1, 0), (1, 0, 0), (0, 0, 0)]
        tri_c_unique = [(5, 5, 5), (6, 5, 5), (5, 6, 5)]
        glb = self._three_prim_mesh(tri_a, tri_b_back, tri_c_unique, materials=(0, 0, 0))
        n = struct.unpack("<I", glb[12:16])[0]
        gltf = json.loads(glb[20:20 + n])
        gltf["materials"][0]["doubleSided"] = True
        bin_len = struct.unpack("<I", glb[20 + n:24 + n])[0]
        glb2 = _glb(gltf, glb[28 + n:28 + n + bin_len])
        _pos, _uvs, tris, _normals, textures, tri_materials = threedmr.gltf_mesh(glb2, model_id=22)
        self.assertEqual(len(tris), 2)
        self.assertEqual(textures[0][5], True)

    def test_few_back_faces_keep_the_slot_double_sided(self):
        # a stray back face or two in a big slot (3dmr.eu id 8: 120 of 1,137,789) is no two-sided model:
        # the slot stays doubleSided, the stray back face is dropped (no z-fight either way)
        P = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        T = [(0, 1, 2), (2, 1, 0)]
        for k in range(4):
            P += [(10 + k, 0, 0), (11 + k, 0, 0), (10 + k, 1, 0)]
            T.append((3 + 3 * k, 4 + 3 * k, 5 + 3 * k))
        gltf, bin_bytes = _tri_mesh(P, T)
        _pos, _uvs, tris, _normals, textures, _tm = threedmr.gltf_mesh(_glb(gltf, bin_bytes))
        self.assertEqual(textures[0][5], True, "2 of 6 triangles (33%) < BACKFACE_SHARE_MIN")
        self.assertEqual(len(tris), 5)

    def test_mirroring_node_flips_winding(self):
        # glTF: a node whose matrix mirrors (negative determinant) flips which winding is the front face
        gltf, bin_bytes = _tri_mesh([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)])
        P, _uv, T, _n, _tex, _tm = threedmr.gltf_mesh(_glb(gltf, bin_bytes))
        gltf["nodes"][0]["scale"] = [-1, 1, 1]
        Pm, _uv, Tm, _n, _tex, _tm = threedmr.gltf_mesh(_glb(gltf, bin_bytes))
        n = np.cross(P[T[0, 1]] - P[T[0, 0]], P[T[0, 2]] - P[T[0, 0]])
        nm = np.cross(Pm[Tm[0, 1]] - Pm[Tm[0, 0]], Pm[Tm[0, 2]] - Pm[Tm[0, 0]])
        self.assertGreater(float(n @ nm), 0, "the mirrored triangle still faces the same way")

    def test_dragon_bridge_has_no_z_fighting_pair(self):
        # the real 3dmr.eu id 22, when its GLB is on this machine: no two faces the engine draws into one pixel
        path = os.environ.get("BNGE_3DMR_TEST_GLB_22", "")      # (a downloaded https://3dmr.eu/model/22)
        if not os.path.exists(path):
            self.skipTest("3dmr.eu id 22 GLB not downloaded (BNGE_3DMR_TEST_GLB_22)")
        P, _uv, T, _n, textures, TM = threedmr.gltf_mesh(open(path, "rb").read(), model_id=22)
        ds = np.array([t[5] for t in textures], bool)
        keys, side = threedmr._tri_sides(P, T)
        _, inv = np.unique(keys, axis=0, return_inverse=True)
        inv = inv.ravel()
        fights = 0
        for g in np.split(np.argsort(inv, kind="stable"), np.cumsum(np.bincount(inv))[:-1]):
            for i in range(len(g)):
                for j in range(i + 1, len(g)):
                    a, b = g[i], g[j]
                    fights += side[a] == side[b] or ds[TM[a]] or ds[TM[b]]
        self.assertEqual(fights, 0)
        self.assertFalse(ds.any(), "every slot of the closed two-sided bridge is one-sided")

    def test_duplicate_reversed_winding_is_dropped_double_sided(self):
        tri_a = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        tri_b_dup_reversed = [(0, 1, 0), (1, 0, 0), (0, 0, 0)]
        P = np.array(tri_a + tri_b_dup_reversed, np.float64)
        _p, _u, _n, tris, tm = threedmr._dedup_tris(
            P, None, None, np.array([[0, 1, 2], [3, 4, 5]]), np.zeros(2, np.int64), double_sided=[True]
        )
        self.assertEqual(len(tris), 1, "a doubleSided slot's back face is still a duplicate")

    def test_no_duplicates_mesh_is_unchanged(self):
        gltf, bin_bytes = _tri_mesh(
            [(0, 0, 0), (1, 0, 0), (0, 1, 0), (5, 5, 5), (6, 5, 5), (5, 6, 5)],
            [(0, 1, 2), (3, 4, 5)],
        )
        _pos, _uvs, tris, _normals, _textures, _tm = threedmr.gltf_mesh(
            _glb(gltf, bin_bytes)
        )
        self.assertEqual(len(tris), 2, "no duplicates: triangle count is untouched")


class MultiTextureTest(unittest.TestCase):
    def test_multi_texture_per_material(self):
        # two primitives, each with its own material/baseColorTexture pointing at a distinct embedded image:
        # gltf_mesh must keep them as two separate texture slots and split triangles between them correctly,
        # rather than every primitive ending up pointing at the first textured material's image
        pos0 = b"".join(
            struct.pack("<3f", *v) for v in [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        )
        idx0 = b"".join(struct.pack("<B", i) for i in (0, 1, 2))
        pos1 = b"".join(
            struct.pack("<3f", *v) for v in [(2, 0, 0), (3, 0, 0), (2, 1, 0)]
        )
        idx1 = b"".join(struct.pack("<B", i) for i in (0, 1, 2))
        img_a, img_b = b"PNG_A_BYTES", b"PNG_B_BYTES"

        chunks = [pos0, idx0, pos1, idx1, img_a, img_b]
        offsets, off = [], 0
        for c in chunks:
            offsets.append(off)
            off += len(c)
        bin_bytes = b"".join(chunks)
        buffer_views = [
            {"byteOffset": offsets[i], "byteLength": len(chunks[i])}
            for i in range(len(chunks))
        ]

        gltf = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "meshes": [
                {
                    "primitives": [
                        {"attributes": {"POSITION": 0}, "indices": 1, "material": 0},
                        {"attributes": {"POSITION": 2}, "indices": 3, "material": 1},
                    ]
                }
            ],
            "accessors": [
                {"componentType": 5126, "type": "VEC3", "count": 3, "bufferView": 0},
                {"componentType": 5121, "type": "SCALAR", "count": 3, "bufferView": 1},
                {"componentType": 5126, "type": "VEC3", "count": 3, "bufferView": 2},
                {"componentType": 5121, "type": "SCALAR", "count": 3, "bufferView": 3},
            ],
            "bufferViews": buffer_views,
            "images": [
                {"bufferView": 4, "mimeType": "image/png"},
                {"bufferView": 5, "mimeType": "image/png"},
            ],
            "textures": [{"source": 0}, {"source": 1}],
            "materials": [
                {"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}},
                {"pbrMetallicRoughness": {"baseColorTexture": {"index": 1}}},
            ],
        }
        positions, _uvs, tris, _normals, textures, tri_materials = threedmr.gltf_mesh(
            _glb(gltf, bin_bytes)
        )
        self.assertEqual(len(positions), 6)
        self.assertEqual(len(tris), 2)
        self.assertEqual(len(textures), 2)
        self.assertEqual([tb for tb, _, _f, _ha, _ac, _ds in textures], [img_a, img_b])
        self.assertEqual(tri_materials.tolist(), [0, 1])


class BaseColorFactorTest(unittest.TestCase):
    """3dmr.eu ids 47 (Times Tower) and 10 (Brunei mosque): materials with a baseColorFactor and no
    baseColorTexture at all must not fall back to the plain-grey "untextured" colour - a user otherwise
    sees them as "materials didn't load" in-game."""

    def _one_material_mesh(self, material):
        gltf, bin_bytes = _tri_mesh([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)])
        gltf["meshes"][0]["primitives"][0]["material"] = 0
        gltf["materials"] = [material]
        return _glb(gltf, bin_bytes)

    def test_factor_without_texture_is_kept(self):
        # id 47, m0: baseColorFactor (0.269, 0.274, 0.291), no baseColorTexture - kept as its sRGB conversion
        # (_linear_to_srgb: the glTF value is linear-light, materials.json diffuseColor reads as sRGB)
        glb = self._one_material_mesh(
            {"pbrMetallicRoughness": {"baseColorFactor": [0.269, 0.274, 0.291, 1]}}
        )
        _pos, _uvs, _tris, _normals, textures, _tm = threedmr.gltf_mesh(glb)
        self.assertEqual(len(textures), 1)
        tex, ext, factor, has_alpha, alpha_cutoff, _double_sided = textures[0]
        self.assertIsNone(tex)
        self.assertIsNone(ext)
        expected = tuple(threedmr._linear_to_srgb(c) for c in (0.269, 0.274, 0.291))
        for got, want in zip(factor, expected):
            self.assertAlmostEqual(got, want, places=6)
        self.assertFalse(
            has_alpha, "a colourless slot (factor, no texture) is never alpha"
        )
        self.assertIsNone(alpha_cutoff)

    def test_white_factor_without_texture_is_dropped(self):
        # the glTF-default baseColorFactor (white): nothing to tint away from, factor stays None so the
        # plain-grey fallback (update_materials/_material_stage) still applies
        glb = self._one_material_mesh(
            {"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1]}}
        )
        _pos, _uvs, _tris, _normals, textures, _tm = threedmr.gltf_mesh(glb)
        self.assertIsNone(textures[0][2])

    def test_absent_factor_without_texture_is_none(self):
        glb = self._one_material_mesh({})
        _pos, _uvs, _tris, _normals, textures, _tm = threedmr.gltf_mesh(glb)
        self.assertIsNone(textures[0][2])


class LinearToSrgbTest(unittest.TestCase):
    """glTF baseColorFactor is linear-light (spec); materials.json diffuseColor is read as sRGB - without
    _linear_to_srgb a dark linear factor (3dmr.eu id 4, Tour_Eiffel: linear ~0.06,0.04,0.03, 83% of the
    model's triangles) bakes in almost black in-game instead of its intended dark bronze."""

    def test_factor_linear_to_srgb(self):
        gltf = {
            "materials": [
                {
                    "pbrMetallicRoughness": {
                        "baseColorFactor": [0.061262, 0.044687, 0.031797, 1]
                    }
                }
            ]
        }
        _tex, _ext, factor, _has_alpha, _alpha_cutoff = threedmr._resolve_texture(
            gltf, b"", 0, {}
        )
        for got, want in zip(factor, (0.275, 0.234, 0.196)):
            self.assertAlmostEqual(got, want, delta=0.001)

    def test_factor_alpha_channel_not_converted(self):
        # baseColorFactor's own 4th component (alpha) is never part of factor (RGB only) - a Stage built
        # from it always carries diffuseColor's 4th element as plain opacity 1, untouched by the sRGB fix
        gltf = {
            "materials": [
                {"pbrMetallicRoughness": {"baseColorFactor": [0.5, 0.5, 0.5, 0.3]}}
            ]
        }
        _tex, _ext, factor, _has_alpha, _alpha_cutoff = threedmr._resolve_texture(
            gltf, b"", 0, {}
        )
        stage, _translucency = threedmr._material_stage(None, factor)
        self.assertEqual(stage["diffuseColor"][3], 1)


class MaterialStageAlphaTest(unittest.TestCase):
    """Torque3D/BeamNG does not draw a colorMap's alpha channel unless the material opts into alphaTest -
    observed on 3dmr.eu id 4 (Eiffel Tower): its lattice textures carry a cutting alpha channel and every
    material is alphaMode BLEND, but without these fields the gaps painted opaque and the tower read
    in-game as a solid mass. BeamNG reads these fields ONLY off a material's top level - never off a Stage
    - so _material_stage returns them separately from the Stage itself. translucent/translucentBlendOp are
    deliberately absent: this mod's materials are the classic format (no "version": 1.5), which rejects the
    whole material if those two fields appear at the top level (observed after the 2026-10-25 test build:
    every 3dmr material broke, "NO MATERIAL" in-game)."""

    def test_material_stage_no_alpha(self):
        stage, translucency = threedmr._material_stage(
            "/temp/beamngEarth/s1/x.png", None
        )
        self.assertIsNone(translucency)
        self.assertNotIn("translucent", stage)
        self.assertNotIn("alphaTest", stage)

    def test_material_stage_alpha(self):
        stage, translucency = threedmr._material_stage(
            "/temp/beamngEarth/s1/x.png", None, has_alpha=True
        )
        self.assertTrue(translucency["alphaTest"])
        self.assertEqual(translucency["alphaRef"], 64)
        self.assertNotIn("translucent", translucency)
        self.assertNotIn("translucentBlendOp", translucency)
        self.assertNotIn("translucent", stage)
        self.assertNotIn("alphaTest", stage)

    def test_material_stage_alpha_cutoff(self):
        _stage, translucency = threedmr._material_stage(
            "/temp/beamngEarth/s1/x.png", None, has_alpha=True, alpha_cutoff=0.5
        )
        self.assertEqual(translucency["alphaRef"], 128)

    def test_material_stage_is_matte(self):
        # a user's report: a local model's fire-tower STL "shines like it's painted" in-game - the old
        # specular [0.2]*3/specularPower 12/pixelSpecular True combination put a shiny highlight on every
        # 3dmr material (photographic textures included, which should never show one of their own)
        stage, _translucency = threedmr._material_stage(
            "/temp/beamngEarth/s1/x.png", None
        )
        self.assertEqual(stage["specular"], [0.02, 0.02, 0.02, 1.0])
        self.assertEqual(stage["specularPower"], 4)
        self.assertFalse(stage["pixelSpecular"])


class WriteModelAlphaFlagTest(unittest.TestCase):
    def _png(self, mode, pixel):
        from io import BytesIO
        from PIL import Image

        img = Image.new(mode, (2, 2), pixel)
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def _rgba_png(self, pixel_a, pixel_b):
        from io import BytesIO
        from PIL import Image

        img = Image.new("RGBA", (2, 1), pixel_a)
        img.putpixel((1, 0), pixel_b)
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def test_write_model_alpha_flag(self):
        # one slot's texture actually cuts (RGBA, one pixel alpha 0, the other 255), the other is opaque RGB
        positions = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        )
        tris = np.array([[0, 1, 2], [0, 2, 3]])
        uvs = np.zeros((4, 2))
        tri_materials = np.array([0, 1])
        alpha_png = self._rgba_png((255, 0, 0, 0), (255, 0, 0, 255))
        opaque_png = self._png("RGB", (10, 20, 30))
        with tempfile.TemporaryDirectory() as d:
            result = threedmr.write_model(
                d,
                "4",
                positions,
                tris,
                uvs,
                None,
                [alpha_png, opaque_png],
                ground_z=0.0,
                tri_materials=tri_materials,
            )
        self.assertEqual(result["has_alpha"], [True, False])
        self.assertEqual(result["alpha_cutoff"], [None, None])


class UpdateMaterialsAlphaTest(unittest.TestCase):
    def test_translucent_written(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, *threedmr.MOD_MATERIALS_REL[:3])
            os.makedirs(root, exist_ok=True)
            threedmr.update_materials(
                d,
                "threedmr_4_0",
                "/temp/beamngEarth/s1/threedmr_4_0.png",
                has_alpha=True,
                alpha_cutoff=0.5,
            )
            path = os.path.join(d, *threedmr.MOD_MATERIALS_REL, threedmr.MATERIALS_FILE)
            with open(path) as f:
                data = json.load(f)
            entry = data["threedmr_4_0"]
            # BeamNG reads alphaTest/alphaRef off the material's top level only, never off a Stage
            self.assertTrue(entry["alphaTest"])
            self.assertEqual(entry["alphaRef"], 128)
            self.assertNotIn("translucent", entry)
            self.assertNotIn("translucentBlendOp", entry)
            self.assertNotIn("alphaTest", entry["Stages"][0])
            self.assertNotIn("alphaRef", entry["Stages"][0])


class FetchModelTest(unittest.TestCase):
    def test_from_cache(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "threedmr_cache")
            os.makedirs(cache)
            payload = b"x" * 2000
            with open(os.path.join(cache, "999.glb"), "wb") as f:
                f.write(payload)
            data = threedmr.fetch_model("999", d)
            self.assertEqual(data, payload)


class RegionIdsTest(unittest.TestCase):
    """region_ids: one cached, paged search per REGION_DEG square (not two per road tile)."""

    def pages(self, *pages):
        got = []

        def fetch(url, timeout):
            got.append(url)
            n = int(url.rsplit("/", 1)[1])
            return json.dumps(pages[n - 1] if n <= len(pages) else []).encode()
        return got, fetch

    def test_paged_once_per_region_then_cached(self):
        with tempfile.TemporaryDirectory() as d:
            urls, fetch = self.pages([4, 9], [12], [])
            with mock.patch("threedmr._urllib_fetch", side_effect=fetch):
                a = threedmr.region_ids(48.85, 2.35, d)
                b = threedmr.region_ids(48.95, 2.45, d)          # (the same 0.5-degree square)
            self.assertEqual(a, [4, 9, 12])
            self.assertEqual(b, a)
            self.assertEqual(len(urls), 3, "pages 1-3 once, the second tile from the cache")
            self.assertTrue(urls[0].startswith("https://3dmr.eu/api/search/48.750000/2.250000/"))
            rng = int(urls[0].split("/")[-2])
            self.assertGreater(rng, threedmr.SEARCH_CORRECT_R_M, "reaches past the square's corners")

    def test_stale_list_kept_when_its_refresh_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _u, fetch = self.pages([7])
            with mock.patch("threedmr._urllib_fetch", side_effect=fetch):
                threedmr.region_ids(10.1, 20.1, d)
            path = os.path.join(d, "threedmr_cache", "region_20_40.json")
            old = time.time() - threedmr.REGION_TTL_S - 10
            os.utime(path, (old, old))
            faults = []
            with mock.patch("threedmr._urllib_fetch", side_effect=OSError("down")), \
                    mock.patch("revisit.fault", side_effect=lambda: faults.append(1)):
                self.assertEqual(threedmr.region_ids(10.1, 20.1, d), [7])
            self.assertEqual(faults, [], "an old list stands in: no fault")

    def test_first_search_failing_faults(self):
        faults = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("threedmr._urllib_fetch", side_effect=OSError("down")), \
                mock.patch("revisit.fault", side_effect=lambda: faults.append(1)):
            self.assertEqual(threedmr.region_ids(1.0, 2.0, d), [])
        self.assertEqual(faults, [1])

    def test_off_no_request(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"BNE_3DMR": "off"}), \
                mock.patch("threedmr._urllib_fetch", side_effect=AssertionError("no request")):
            self.assertEqual(threedmr.region_ids(1.0, 2.0, d), [])


class CapsTest(unittest.TestCase):
    def test_download_over_the_cap_refused(self):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return b"x" * n

        with mock.patch.object(threedmr, "MAX_DOWNLOAD", 1000), \
                mock.patch("urllib.request.urlopen", return_value=Resp()):
            with self.assertRaises(OSError):
                threedmr._urllib_fetch("https://3dmr.eu/api/model/1", 5)


class SearchIdsTest(unittest.TestCase):
    def test_successful_search_caches(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("threedmr._urllib_fetch", return_value=b"[24, 25]"):
                ids = threedmr.search_ids(1.0, 2.0, 100, d)
            self.assertEqual(ids, [24, 25])
            cache = os.path.join(d, "threedmr_cache", "search_1.0_2.0_100.json")
            self.assertTrue(os.path.exists(cache))

    def test_failed_fetch_returns_empty_without_caching(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("threedmr._urllib_fetch", side_effect=OSError("boom")):
                ids = threedmr.search_ids(1.0, 2.0, 100, d)
            self.assertEqual(ids, [])
            cache = os.path.join(d, "threedmr_cache")
            self.assertFalse(
                os.path.isdir(cache), "a failed search leaves nothing cached"
            )

    def test_failed_fetch_faults_the_revisit_build(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            with (
                mock.patch("threedmr._urllib_fetch", side_effect=OSError("boom")),
                mock.patch("revisit.fault", side_effect=lambda: calls.append(1)),
            ):
                threedmr.search_ids(1.0, 2.0, 100, d)
            self.assertEqual(len(calls), 1)


    def test_models_switched_off(self):
        # the settings page's "3D models from 3dmr.eu" off (BNE_3DMR=off, cpupin.apply_player_settings): no request,
        # no model anywhere
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"BNE_3DMR": "off"}), \
                mock.patch("threedmr._urllib_fetch", side_effect=AssertionError("no request")):
            self.assertFalse(threedmr.enabled())
            self.assertEqual(threedmr.search_ids(1.0, 2.0, 100, d), [])
        with mock.patch.dict(os.environ, {"BNE_3DMR": "on"}):
            self.assertTrue(threedmr.enabled())


class ModelInfoTest(unittest.TestCase):
    def test_failed_fetch_raises_without_caching(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("threedmr._urllib_fetch", side_effect=OSError("boom")):
                with self.assertRaises(ValueError):
                    threedmr.model_info("24", d)
            cache = os.path.join(d, "threedmr_cache")
            self.assertFalse(
                os.path.isdir(cache), "a failed info fetch leaves nothing cached"
            )


class DescPositionTest(unittest.TestCase):
    """threedmr.desc_position: id 22's own desc (its api point sits 26 km off, in the mountains: the
    procedural bridge stayed in Da Nang and the model went to the api point) still carries an osmgo.org link to
    the real bridge."""

    def test_parses_osmgo_link(self):
        info = {
            "desc": "Dragon Bridge in Da Nang. View: http://osmgo.org/?km=1&lat=16.05716901&lon=108.2318281"
        }
        self.assertEqual(threedmr.desc_position(info), (16.05716901, 108.2318281))

    def test_no_link_is_none(self):
        self.assertIsNone(
            threedmr.desc_position(
                {"desc": "just a plain description, no coordinates here"}
            )
        )

    def test_no_desc_is_none(self):
        self.assertIsNone(threedmr.desc_position({}))
        self.assertIsNone(threedmr.desc_position({"desc": None}))


class ModelPositionTest(unittest.TestCase):
    """threedmr.model_position: trusts the api point unless desc's own osmgo.org link disagrees by more than
    POSITION_CORRECT_M, for a model with no POSITION_OVERRIDES entry of its own (id 99: a stand-in id, since
    id 22 now has one - PositionOverrideTest below)."""

    def test_close_desc_keeps_api_point(self):
        info = {
            "lat": 16.05716901,
            "lon": 108.2318281,
            "desc": "View: http://osmgo.org/?km=1&lat=16.05720000&lon=108.2318300",
        }
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch("threedmr.model_info", return_value=info),
            mock.patch("threedmr.osm_polygon_position", return_value=None),
        ):
            lat, lon, corrected, source = threedmr.model_position(99, d)
        self.assertAlmostEqual(lat, 16.05716901)
        self.assertAlmostEqual(lon, 108.2318281)
        self.assertFalse(corrected)
        self.assertEqual(source, "api")

    def test_far_desc_overrides_the_api_point(self):
        # id 22's own real numbers (api point ~26 km from desc's osmgo.org link), reused here on a plain id
        # without an override, to exercise the generic desc-correction path on its own
        info = {
            "lat": 15.99797,
            "lon": 107.99513,
            "desc": "Dragon Bridge in Da Nang. View: http://osmgo.org/?km=1&lat=16.05716901&lon=108.2318281",
        }
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch("threedmr.model_info", return_value=info),
            mock.patch("threedmr.osm_polygon_position", return_value=None),
        ):
            with self.assertLogs(threedmr.log, level="WARNING") as cm:
                lat, lon, corrected, source = threedmr.model_position(99, d)
        self.assertAlmostEqual(lat, 16.05716901)
        self.assertAlmostEqual(lon, 108.2318281)
        self.assertTrue(corrected)
        self.assertEqual(source, "desc")
        self.assertTrue(any("desc coords" in m for m in cm.output))


class PositionOverrideTest(unittest.TestCase):
    """threedmr.model_position/position_override: id 22 (Dragon Bridge) has a POSITION_OVERRIDES entry
    because 3dmr.eu's own api point AND desc link are both wrong - model_position must return it without
    ever consulting model_info (no network call, no cache needed)."""

    def test_model_position_uses_override_without_network(self):
        with mock.patch(
            "threedmr.model_info", side_effect=AssertionError("should not be called")
        ):
            lat, lon, corrected, source = threedmr.model_position(22, "/nonexistent")
        self.assertAlmostEqual(lat, 16.06120)
        self.assertAlmostEqual(lon, 108.22790)
        self.assertTrue(corrected)
        self.assertEqual(source, "override")

    def test_position_override_known_id(self):
        self.assertEqual(threedmr.position_override(22), (16.06120, 108.22790, 0.0))

    def test_position_override_unknown_id_is_none(self):
        self.assertIsNone(threedmr.position_override(99))


def _disjoint_mesh(n_verts, index_encode=False, seed=0):
    """A mesh of n_verts // 3 triangles, each 3 brand-new vertices (no vertex shared by two triangles) - makes
    split_mesh's section boundaries land at exactly predictable triangle counts (limit // 3 triangles/section)
    instead of depending on how much random triangles happen to overlap. index_encode: positions/uvs/normals'
    own column 0 is set to the vertex's own global index (and the rest zero) - lets a test recover which
    original vertex a section-local row came from with a plain round(), instead of needing its own bookkeeping."""
    rng = np.random.default_rng(seed)
    if index_encode:
        positions = np.zeros((n_verts, 3))
        positions[:, 0] = np.arange(n_verts)
        uvs = np.stack([np.arange(n_verts, dtype=np.float64), np.zeros(n_verts)], 1)
        normals = np.stack(
            [
                np.arange(n_verts, dtype=np.float64),
                np.zeros(n_verts),
                np.zeros(n_verts),
            ],
            1,
        )
    else:
        positions = rng.random((n_verts, 3)) * 10.0
        uvs = rng.random((n_verts, 2))
        normals = rng.random((n_verts, 3))
    n_tris = n_verts // 3
    tris = np.arange(n_tris * 3).reshape(n_tris, 3)
    tri_materials = rng.integers(0, 4, size=n_tris)
    return positions, tris, uvs, normals, tri_materials


class SplitMeshTest(unittest.TestCase):
    """threedmr.split_mesh: Torque3D's IBO is 16-bit (TSShape::initVertexBuffers/TSMesh::dumpPrimitives) - a
    mesh over SPLIT_VERTEX_TRIGGER vertices must be broken into sections of at most `limit` unique vertices
    each before it is written, or its indices wrap mod 65536 in-game (observed on 3dmr.eu ids 3, 4, 7, 8, 14,
    25, 26, 30 - users' "glitchy mesh" reports)."""

    def test_every_section_under_the_limit(self):
        positions, tris, uvs, normals, tri_materials = _disjoint_mesh(70000)
        sections = threedmr.split_mesh(positions, tris, uvs, normals, tri_materials)
        self.assertGreater(len(sections), 1, "70000 verts must not fit in one section")
        for sec in sections:
            self.assertLessEqual(len(sec["positions"]), threedmr.SPLIT_VERTEX_LIMIT)
            self.assertEqual(len(sec["uvs"]), len(sec["positions"]))
            self.assertEqual(len(sec["normals"]), len(sec["positions"]))

    def test_every_triangle_present_exactly_once_with_its_material_in_order(self):
        positions, tris, uvs, normals, tri_materials = _disjoint_mesh(
            70000, index_encode=True
        )
        sections = threedmr.split_mesh(positions, tris, uvs, normals, tri_materials)
        got = []
        for sec in sections:
            global_tris = (
                sec["positions"][sec["tris"]][:, :, 0].round().astype(np.int64)
            )
            got.extend(
                zip(
                    (tuple(t) for t in global_tris.tolist()),
                    sec["tri_materials"].tolist(),
                )
            )
        want = list(zip((tuple(t) for t in tris.tolist()), tri_materials.tolist()))
        self.assertEqual(
            got,
            want,
            "every original triangle, in order, exactly once, with its own material",
        )

    def test_small_mesh_is_not_split(self):
        positions, tris, uvs, normals, tri_materials = _disjoint_mesh(100)
        sections = threedmr.split_mesh(positions, tris, uvs, normals, tri_materials)
        self.assertEqual(len(sections), 1)
        self.assertEqual(
            len(sections[0]["positions"]), 99
        )  # 33 tris x 3 verts, the unused 100th dropped
        self.assertEqual(len(sections[0]["tris"]), 33)

    def test_uv_and_normal_rows_match_their_section_vertex(self):
        positions, tris, uvs, normals, tri_materials = _disjoint_mesh(
            70000, index_encode=True
        )
        sections = threedmr.split_mesh(positions, tris, uvs, normals, tri_materials)
        for sec in sections:
            for local in (0, len(sec["positions"]) - 1):
                global_idx = int(round(sec["positions"][local, 0]))
                self.assertEqual(int(round(sec["uvs"][local, 0])), global_idx)
                self.assertEqual(int(round(sec["normals"][local, 0])), global_idx)


class WriteModelSplitTest(unittest.TestCase):
    def test_large_mesh_writes_several_sections(self):
        positions, tris, uvs, _normals, _tm = _disjoint_mesh(70000)
        with tempfile.TemporaryDirectory() as d:
            result = threedmr.write_model(
                d, "8", positions, tris, uvs, None, b"\x89PNGfake", ground_z=0.0
            )
            self.assertIn("sections", result)
            self.assertGreaterEqual(len(result["sections"]), 2)
            self.assertEqual(result["shape"], "threedmr_8.cdae")
            self.assertEqual(result["sections"][0]["shape"], "threedmr_8.cdae")
            self.assertEqual(result["sections"][1]["shape"], "threedmr_8_s1.cdae")
            for sec in result["sections"]:
                self.assertTrue(os.path.exists(os.path.join(d, sec["shape"])))
                decoded = cdae.read(os.path.join(d, sec["shape"]))
                self.assertLessEqual(
                    len(cdae.mesh_arrays(decoded["meshes"][0])["verts"]),
                    threedmr.SPLIT_VERTEX_LIMIT,
                )

    def test_small_mesh_keeps_the_old_single_section_format(self):
        positions, tris, uvs, _normals, _tm = _disjoint_mesh(100)
        with tempfile.TemporaryDirectory() as d:
            result = threedmr.write_model(
                d, "9", positions, tris, uvs, None, None, ground_z=0.0
            )
        self.assertNotIn("sections", result)
        self.assertEqual(result["shape"], "threedmr_9.cdae")


class WriteModelTest(unittest.TestCase):
    def test_write_and_read_back(self):
        positions = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        )
        tris = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 1], [1, 3, 2]])
        uvs = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])
        with tempfile.TemporaryDirectory() as d:
            result = threedmr.write_model(
                d,
                "42",
                positions,
                tris,
                uvs,
                None,
                b"\x89PNGfake",
                ground_z=10.0,
                offset_xy=(5.0, -3.0),
            )
            self.assertEqual(result["shape"], "threedmr_42.cdae")
            self.assertEqual(result["texture"], "threedmr_42.png")
            self.assertEqual(result["material"], "threedmr_42")
            shape_path = os.path.join(d, result["shape"])
            tex_path = os.path.join(d, result["texture"])
            self.assertTrue(os.path.exists(shape_path))
            self.assertTrue(os.path.exists(tex_path))
            with open(tex_path, "rb") as f:
                self.assertEqual(f.read(), b"\x89PNGfake")

            decoded = cdae.read(shape_path)
            self.assertEqual(decoded["version"], cdae.VERSION_WORD)
            self.assertEqual(decoded["materials"][0]["name"], "threedmr_42")
            verts = cdae.mesh_arrays(decoded["meshes"][0])["verts"]
            self.assertAlmostEqual(float(verts[:, 2].min()), 10.0 - 0.2, places=3)

    def test_no_texture(self):
        positions = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        )
        tris = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 1], [1, 3, 2]])
        uvs = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])
        with tempfile.TemporaryDirectory() as d:
            result = threedmr.write_model(
                d, "7", positions, tris, uvs, None, None, ground_z=0.0
            )
            self.assertIsNone(result["texture"])
            self.assertFalse(os.path.exists(os.path.join(d, "threedmr_7.png")))

    def test_texture_dedup(self):
        # 3dmr.eu id 10 (Brunei mosque): 234 materials, several byte-identical textures among them - two
        # slots sharing the same source image must write one PNG file, not two, and both slots' "textures"
        # entries must point at that one file
        positions = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        )
        tris = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 1], [1, 3, 2]])
        uvs = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])
        tri_materials = np.array([0, 1, 2, 0])
        same = b"\x89PNGsame"
        pngs = [same, b"\x89PNGdifferent", same]
        with tempfile.TemporaryDirectory() as d:
            result = threedmr.write_model(
                d,
                "10",
                positions,
                tris,
                uvs,
                None,
                pngs,
                ground_z=0.0,
                tri_materials=tri_materials,
            )
            self.assertEqual(
                result["textures"][0],
                result["textures"][2],
                "identical bytes share one file",
            )
            self.assertNotEqual(result["textures"][0], result["textures"][1])
            files = sorted(f for f in os.listdir(d) if f.endswith(".png"))
            self.assertEqual(
                len(files), 2, "one file per distinct image, not one per slot"
            )
            with open(os.path.join(d, result["textures"][0]), "rb") as f:
                self.assertEqual(f.read(), same)


class SupportZTest(unittest.TestCase):
    """threedmr._support_z/write_model's bedding fix: a user's report at 3dmr.eu id 31 (an Izhevsk house)
    - a mapper modeled a terrace/plaza plate around the building, but left a couple of stray vertices tens
    of metres below it (georeferencing debris). The old write_model bedded the mesh's bare z.min() (that
    debris) onto the ground, launching the whole building into the air. _support_z instead looks for the
    lowest Z layer that actually holds a real share of the mesh's vertices - the plate itself - and beds
    that on the ground; the stray vertices end up buried, not the building floating."""

    def _grid_plate(self, n, z):
        # an n x n flat grid at height z, quad-triangulated: enough vertices for its own Z layer to
        # dominate a mesh that also carries a couple of outlier vertices far below it
        xs, ys = np.meshgrid(
            np.arange(n, dtype=np.float64), np.arange(n, dtype=np.float64)
        )
        pos = np.stack([xs.ravel(), ys.ravel(), np.full(xs.size, float(z))], axis=1)
        tris = []
        for r in range(n - 1):
            for c in range(n - 1):
                i00, i10 = r * n + c, r * n + c + 1
                i01, i11 = (r + 1) * n + c, (r + 1) * n + c + 1
                tris.append((i00, i10, i11))
                tris.append((i00, i11, i01))
        return pos, np.array(tris, np.int64)

    def test_support_z_prefers_the_dense_plate_over_stray_low_vertices(self):
        z = np.array([10.0] * 98 + [-16.0] * 2)
        # cumulative: the debris layer (2 of 100) never reaches SUPPORT_SHARE, the plate's own
        # layer does - within SUPPORT_DZ_CAP of the minimum, so the plate is the support plane
        self.assertEqual(threedmr._support_z(z), 10.0)

    def test_support_z_falls_back_to_zmin_without_a_dominant_layer(self):
        # a rounded bottom (e.g. a sphere/cylinder cap) spread thin over a tall mesh: every 0.5 m layer
        # holds only ~1 of the 2000 vertices (~0.05%, well under SUPPORT_SHARE) - zmin is the only sensible
        # choice left
        z = np.linspace(0.0, 1000.0, 2000)
        # one vertex per 0.5 m layer: the cumulative SUPPORT_SHARE share is reached 50 m up the mesh -
        # far past SUPPORT_DZ_CAP, so the bare minimum stands (an openwork tower's legs, not debris)
        self.assertAlmostEqual(threedmr._support_z(z), float(z.min()), places=6)

    def test_write_model_beds_the_plate_not_the_debris_below_it(self):
        plate, plate_tris = self._grid_plate(
            11, 10.0
        )  # 121 vertices, dense enough to dominate
        debris = np.array([[500.0, 500.0, -16.0], [500.0, 501.0, -16.0]])
        positions = np.concatenate([plate, debris], 0)
        # a sliver triangle tying the 2 debris vertices to the plate (so cdae's own first-use vertex
        # renumbering keeps them in the written mesh at all, same as any real stray/degenerate triangle a
        # mapper leaves behind) - appended after plate_tris so it does not perturb the plate's own ordering
        debris_tri = np.array([[0, 121, 122]])
        tris = np.concatenate([plate_tris, debris_tri], 0)
        uvs = positions[:, :2]
        with tempfile.TemporaryDirectory() as d:
            result = threedmr.write_model(
                d,
                "31",
                positions,
                tris,
                uvs,
                None,
                None,
                ground_z=0.0,
            )
            self.assertAlmostEqual(result["support_dz"], 26.0, places=3)
            decoded = cdae.read(os.path.join(d, result["shape"]))
            verts = cdae.mesh_arrays(decoded["meshes"][0])["verts"]
            # the plate (the first 121 vertices) sits bedded on the ground - ground_z - 0.2, not floating
            self.assertAlmostEqual(float(verts[:121, 2].max()), -0.2, places=3)
            self.assertAlmostEqual(float(verts[:121, 2].min()), -0.2, places=3)
            # the debris (the last 2) ends up buried well under the ground, not pulling the plate down to it
            self.assertLess(float(verts[121:, 2].max()), -0.2)

    def test_write_model_without_a_plate_keeps_the_old_zmin_behaviour(self):
        # a cylinder-like mesh with no dominant bottom layer: write_model must still bed its bare z.min(),
        # same as before this fix (a regular model with an ordinary floor also falls in this case: its own
        # floor slab IS its z.min(), so _support_z picks the same layer zmin already names)
        n = 24
        t = np.linspace(0.0, 2 * math.pi, n, endpoint=False)
        bottom = np.stack([np.cos(t), np.sin(t), np.full(n, 0.0)], axis=1)
        top = np.stack([np.cos(t), np.sin(t), np.full(n, 5.0)], axis=1)
        positions = np.concatenate([bottom, top], 0)
        tris = []
        for i in range(n):
            j = (i + 1) % n
            tris.append((i, j, n + j))
            tris.append((i, n + j, n + i))
        tris = np.array(tris, np.int64)
        uvs = positions[:, :2]
        with tempfile.TemporaryDirectory() as d:
            result = threedmr.write_model(
                d,
                "99",
                positions,
                tris,
                uvs,
                None,
                None,
                ground_z=0.0,
            )
            self.assertAlmostEqual(result["support_dz"], 0.0, places=3)
            decoded = cdae.read(os.path.join(d, result["shape"]))
            verts = cdae.mesh_arrays(decoded["meshes"][0])["verts"]
            self.assertAlmostEqual(float(verts[:, 2].min()), -0.2, places=3)


class WriteModelTexCacheDirTest(unittest.TestCase):
    """write_model's tex_cache_dir: a texture written there (threedmr._persistent_png_dir) instead of out_dir
    survives out_dir being wiped (buildings._custom_tile_entries' real use: out_dir is a session folder swept
    on an anchor change, while tex_cache_dir's own threedmr_cache is not) - the fix for the "NO MATERIAL" bug
    (a revisit cache hit restores a tile's .cdae alone, never a texture that lived only in the session dir)."""

    def test_texture_goes_to_the_persistent_dir_not_out_dir(self):
        positions = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        )
        tris = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 1], [1, 3, 2]])
        uvs = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])
        with (
            tempfile.TemporaryDirectory() as out_dir,
            tempfile.TemporaryDirectory() as tex_cache_dir,
        ):
            result = threedmr.write_model(
                out_dir,
                "42",
                positions,
                tris,
                uvs,
                None,
                b"\x89PNGfake",
                ground_z=0.0,
                tex_cache_dir=tex_cache_dir,
            )
            self.assertEqual(result["texture"], "threedmr_42.png")
            self.assertTrue(
                os.path.exists(os.path.join(out_dir, result["shape"])),
                "the .cdae stays in out_dir",
            )
            self.assertFalse(
                os.path.exists(os.path.join(out_dir, result["texture"])),
                "the texture must not land in out_dir",
            )
            persisted = os.path.join(
                threedmr._persistent_png_dir(tex_cache_dir), result["texture"]
            )
            self.assertTrue(os.path.exists(persisted))
            with open(persisted, "rb") as f:
                self.assertEqual(f.read(), b"\x89PNGfake")

    def test_tex_cache_dir_none_keeps_the_old_behaviour(self):
        positions = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        )
        tris = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 1], [1, 3, 2]])
        uvs = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])
        with tempfile.TemporaryDirectory() as out_dir:
            result = threedmr.write_model(
                out_dir, "42", positions, tris, uvs, None, b"\x89PNGfake", ground_z=0.0
            )
            self.assertTrue(
                os.path.exists(os.path.join(out_dir, result["texture"])),
                "tex_cache_dir=None: the texture goes into out_dir, same as always",
            )

    def test_rewriting_identical_bytes_does_not_touch_the_file(self):
        positions = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        )
        tris = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 1], [1, 3, 2]])
        uvs = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])
        with (
            tempfile.TemporaryDirectory() as out_dir,
            tempfile.TemporaryDirectory() as tex_cache_dir,
        ):
            result = threedmr.write_model(
                out_dir,
                "42",
                positions,
                tris,
                uvs,
                None,
                b"\x89PNGfake",
                ground_z=0.0,
                tex_cache_dir=tex_cache_dir,
            )
            persisted = os.path.join(
                threedmr._persistent_png_dir(tex_cache_dir), result["texture"]
            )
            mtime_before = os.path.getmtime(persisted)

            threedmr.write_model(
                out_dir,
                "42",
                positions,
                tris,
                uvs,
                None,
                b"\x89PNGfake",
                ground_z=0.0,
                tex_cache_dir=tex_cache_dir,
            )
            self.assertEqual(
                os.path.getmtime(persisted),
                mtime_before,
                "the same bytes under the same name must not be rewritten",
            )

    def test_rewriting_different_bytes_replaces_the_file(self):
        positions = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        )
        tris = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 1], [1, 3, 2]])
        uvs = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])
        with (
            tempfile.TemporaryDirectory() as out_dir,
            tempfile.TemporaryDirectory() as tex_cache_dir,
        ):
            result = threedmr.write_model(
                out_dir,
                "42",
                positions,
                tris,
                uvs,
                None,
                b"\x89PNGfake",
                ground_z=0.0,
                tex_cache_dir=tex_cache_dir,
            )
            persisted = os.path.join(
                threedmr._persistent_png_dir(tex_cache_dir), result["texture"]
            )

            threedmr.write_model(
                out_dir,
                "42",
                positions,
                tris,
                uvs,
                None,
                b"\x89PNGchanged",
                ground_z=0.0,
                tex_cache_dir=tex_cache_dir,
            )
            with open(persisted, "rb") as f:
                self.assertEqual(f.read(), b"\x89PNGchanged")


class UpdateMaterialsTest(unittest.TestCase):
    def _mod_dir(self, d):
        root = os.path.join(d, *threedmr.MOD_MATERIALS_REL[:3])
        os.makedirs(root, exist_ok=True)
        return root

    def _materials_path(self, d):
        return os.path.join(d, *threedmr.MOD_MATERIALS_REL, threedmr.MATERIALS_FILE)

    def test_creates_entry(self):
        with tempfile.TemporaryDirectory() as d:
            self._mod_dir(d)
            threedmr.update_materials(
                d, "threedmr_24", "/temp/beamngEarth/s1/threedmr_24.png"
            )
            with open(self._materials_path(d)) as f:
                data = json.load(f)
            self.assertIn("threedmr_24", data)
            entry = data["threedmr_24"]
            self.assertEqual(entry["name"], "threedmr_24")
            self.assertEqual(entry["mapTo"], "threedmr_24")
            self.assertEqual(entry["class"], "Material")
            self.assertTrue(entry["doubleSided"])
            self.assertEqual(
                entry["Stages"][0]["colorMap"], "/temp/beamngEarth/s1/threedmr_24.png"
            )

    def test_one_sided_entry_rewrites_an_old_double_sided_one(self):
        # a model baked before FV25 left its slot doubleSided in the shared materials.json: the same stage with
        # double_sided False must still be written (the flicker stays otherwise)
        with tempfile.TemporaryDirectory() as d:
            self._mod_dir(d)
            threedmr.update_materials(d, "threedmr_22_2", None, (0.8, 0.8, 0.8))
            threedmr.update_materials(d, "threedmr_22_2", None, (0.8, 0.8, 0.8), double_sided=False)
            with open(self._materials_path(d)) as f:
                data = json.load(f)
            self.assertIs(data["threedmr_22_2"]["doubleSided"], False)

    def test_fallback_grey_without_texture(self):
        with tempfile.TemporaryDirectory() as d:
            self._mod_dir(d)
            threedmr.update_materials(d, "threedmr_7", None)
            with open(self._materials_path(d)) as f:
                data = json.load(f)
            stage0 = data["threedmr_7"]["Stages"][0]
            self.assertNotIn("colorMap", stage0)
            self.assertEqual(stage0["diffuseColor"], [0.6, 0.6, 0.6, 1])

    def _touch_vfs(self, d, texture_vfs):
        """Creates the real disk file a game-path colorMap (e.g. "/temp/beamngEarth/s1/x.png") resolves to,
        so update_materials' stale-entry check (its backing session folder is still there) sees it as alive."""
        p = os.path.join(d, texture_vfs.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"\x89PNGfake")

    def test_merges_and_keeps_existing(self):
        with tempfile.TemporaryDirectory() as d:
            self._mod_dir(d)
            self._touch_vfs(d, "/temp/beamngEarth/s1/threedmr_1.png")
            self._touch_vfs(d, "/temp/beamngEarth/s1/threedmr_2.png")
            threedmr.update_materials(
                d, "threedmr_1", "/temp/beamngEarth/s1/threedmr_1.png"
            )
            threedmr.update_materials(
                d, "threedmr_2", "/temp/beamngEarth/s1/threedmr_2.png"
            )
            with open(self._materials_path(d)) as f:
                data = json.load(f)
            self.assertIn("threedmr_1", data)
            self.assertIn("threedmr_2", data)

    def test_second_call_same_texture_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            self._mod_dir(d)
            self._touch_vfs(d, "/temp/beamngEarth/s1/threedmr_24.png")
            threedmr.update_materials(
                d, "threedmr_24", "/temp/beamngEarth/s1/threedmr_24.png"
            )
            path = self._materials_path(d)
            mtime1 = os.path.getmtime(path)
            with open(path) as f:
                before = f.read()
            threedmr.update_materials(
                d, "threedmr_24", "/temp/beamngEarth/s1/threedmr_24.png"
            )
            with open(path) as f:
                after = f.read()
            self.assertEqual(before, after)
            self.assertEqual(mtime1, os.path.getmtime(path))

    def test_no_mod_installed_returns_none_quietly(self):
        with tempfile.TemporaryDirectory() as d:
            result = threedmr.update_materials(
                d, "threedmr_1", "/temp/beamngEarth/s1/threedmr_1.png"
            )
            self.assertIsNone(result)
            self.assertFalse(os.path.exists(os.path.join(d, "mods")))

    def test_factor_material(self):
        # id 47: a material with no texture at all gets its baseColorFactor as a plain diffuseColor,
        # instead of the grey fallback (which a user saw as "the materials didn't load")
        with tempfile.TemporaryDirectory() as d:
            self._mod_dir(d)
            threedmr.update_materials(
                d, "threedmr_47_0", None, factor=(0.269, 0.274, 0.291)
            )
            with open(self._materials_path(d)) as f:
                data = json.load(f)
            stage0 = data["threedmr_47_0"]["Stages"][0]
            self.assertNotIn("colorMap", stage0)
            self.assertEqual(stage0["diffuseColor"], [0.269, 0.274, 0.291, 1])

    def test_factor_tints_a_textured_material_too(self):
        # id 10: some textured materials also carry a non-white baseColorFactor, which must still tint the
        # texture (diffuseColor multiplies colorMap in-engine) rather than being dropped because a texture
        # is present
        with tempfile.TemporaryDirectory() as d:
            self._mod_dir(d)
            threedmr.update_materials(
                d,
                "threedmr_10_1",
                "/temp/beamngEarth/s1/threedmr_10_1.png",
                factor=(0.643, 0.698, 0.733),
            )
            with open(self._materials_path(d)) as f:
                data = json.load(f)
            stage0 = data["threedmr_10_1"]["Stages"][0]
            self.assertEqual(
                stage0["colorMap"], "/temp/beamngEarth/s1/threedmr_10_1.png"
            )
            self.assertEqual(stage0["diffuseColor"], [0.643, 0.698, 0.733, 1])

    def test_stale_entry_dropped(self):
        # threedmr_9's own colorMap names a session folder that is long gone (an anchor change wipes
        # /temp/beamngEarth/<sid>/...): a later update_materials call for an unrelated material must drop it,
        # instead of letting it sit in the file forever pointing a revisit-cached cdae at a dead texture
        with tempfile.TemporaryDirectory() as d:
            self._mod_dir(d)
            self._touch_vfs(d, "/temp/beamngEarth/s1/threedmr_1.png")
            threedmr.update_materials(
                d, "threedmr_9", "/temp/beamngEarth/OLD_SID/threedmr_9.png"
            )
            with self.assertLogs(threedmr.log, level="INFO") as cm:
                threedmr.update_materials(
                    d, "threedmr_1", "/temp/beamngEarth/s1/threedmr_1.png"
                )
            with open(self._materials_path(d)) as f:
                data = json.load(f)
            self.assertNotIn("threedmr_9", data)
            self.assertIn("threedmr_1", data)
            self.assertTrue(any("dropped" in m and "stale" in m for m in cm.output))

    def test_alive_entry_kept(self):
        # threedmr_8's colorMap names a file that still exists on disk: a later call for an unrelated
        # material must leave it alone
        with tempfile.TemporaryDirectory() as d:
            self._mod_dir(d)
            self._touch_vfs(d, "/temp/beamngEarth/s1/threedmr_8.png")
            self._touch_vfs(d, "/temp/beamngEarth/s1/threedmr_1.png")
            threedmr.update_materials(
                d, "threedmr_8", "/temp/beamngEarth/s1/threedmr_8.png"
            )
            threedmr.update_materials(
                d, "threedmr_1", "/temp/beamngEarth/s1/threedmr_1.png"
            )
            with open(self._materials_path(d)) as f:
                data = json.load(f)
            self.assertIn("threedmr_8", data)

    def test_stale_same_name_overwritten(self):
        # threedmr_5's own entry is itself dead (old session), but a fresh update_materials call for that
        # same material (a new session's texture) must still replace it with the new, live path
        with tempfile.TemporaryDirectory() as d:
            self._mod_dir(d)
            threedmr.update_materials(
                d, "threedmr_5", "/temp/beamngEarth/OLD_SID/threedmr_5.png"
            )
            self._touch_vfs(d, "/temp/beamngEarth/s2/threedmr_5.png")
            threedmr.update_materials(
                d, "threedmr_5", "/temp/beamngEarth/s2/threedmr_5.png"
            )
            with open(self._materials_path(d)) as f:
                data = json.load(f)
            self.assertEqual(
                data["threedmr_5"]["Stages"][0]["colorMap"],
                "/temp/beamngEarth/s2/threedmr_5.png",
            )

    def test_dead_colormap_same_stage_not_noop(self):
        # an existing entry whose Stage 0 already matches what this call would write (same data), but whose
        # colorMap file is missing on disk: must not be treated as a no-op (data-equality alone isn't enough -
        # the stale-record bug is exactly a file going missing underneath an otherwise-unchanged record)
        with tempfile.TemporaryDirectory() as d:
            self._mod_dir(d)
            threedmr.update_materials(
                d, "threedmr_6", "/temp/beamngEarth/s1/threedmr_6.png"
            )
            with mock.patch(
                "threedmr.staging.replace", wraps=threedmr.staging.replace
            ) as m:
                threedmr.update_materials(
                    d, "threedmr_6", "/temp/beamngEarth/s1/threedmr_6.png"
                )
            m.assert_called_once()
            with open(self._materials_path(d)) as f:
                data = json.load(f)
            self.assertEqual(
                data["threedmr_6"]["Stages"][0]["colorMap"],
                "/temp/beamngEarth/s1/threedmr_6.png",
            )


def _osm_square_xml(model_id, half_diag_e, half_diag_n, lat0=0.0, lon0=0.0, tag=True):
    """A minimal OSM API 0.6 map response (xml.etree-parseable) holding one closed way: a square centred on
    (lat0, lon0), its corners half_diag_e/half_diag_n local metres east/north/west/south of it (a diamond:
    edges at 45 degrees mod 90) - optionally tagged 3dmr=<model_id> (_osm_building_ring's own lookup)."""
    corners_en = [
        (half_diag_e, 0.0),
        (0.0, half_diag_n),
        (-half_diag_e, 0.0),
        (0.0, -half_diag_n),
    ]
    coslat = math.cos(math.radians(lat0))
    nodes_xml, refs_xml = [], []
    for i, (e, n) in enumerate(corners_en, start=1):
        lat = lat0 + n / 111132.0
        lon = lon0 + e / (111320.0 * coslat)
        nodes_xml.append(f'<node id="{i}" lat="{lat:.9f}" lon="{lon:.9f}"/>')
        refs_xml.append(f'<nd ref="{i}"/>')
    refs_xml.append('<nd ref="1"/>')  # closed way: repeats its first node
    tag_xml = f'<tag k="3dmr" v="{model_id}"/>' if tag else ""
    return (
        "<?xml version='1.0'?><osm version='0.6'>"
        + "".join(nodes_xml)
        + '<way id="100">'
        + "".join(refs_xml)
        + '<tag k="building" v="yes"/>'
        + tag_xml
        + "</way></osm>"
    ).encode()


class OsmYawTest(unittest.TestCase):
    """threedmr.osm_yaw: a model's own baked-in rotation recovered by comparing its lowest tier's dominant
    edge direction (mod 90) against its OSM building way's - not a per-model lookup table (rejected by the
    in review: "too easy to miss one" as new models are uploaded)."""

    def _square_model_points(self, half_side=5.0, step=1.0):
        # an axis-aligned square footprint's perimeter, densely sampled: kNN edges all land at 0/90 (mod 90: 0)
        xs = np.arange(-half_side, half_side + step, step)
        top = np.stack([xs, np.full_like(xs, half_side)], 1)
        bottom = np.stack([xs, np.full_like(xs, -half_side)], 1)
        left = np.stack([np.full_like(xs, -half_side), xs], 1)
        right = np.stack([np.full_like(xs, half_side), xs], 1)
        return np.concatenate([top, bottom, left, right], 0)

    def test_yaw_perfect_alignment(self):
        # OSM: a diamond (edges at 45 degrees mod 90). Model: an axis-aligned square (edges at 0 mod 90).
        # The 90-degree-apart correction that squares things up is -45 (equivalently +45: _yaw_from_angles'
        # own symmetry at the normalization's edge).
        xml = _osm_square_xml(4, 10.0, 10.0)
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch("threedmr._urllib_fetch", return_value=xml),
        ):
            yaw = threedmr.osm_yaw(0.0, 0.0, 4, self._square_model_points(), d)
        self.assertAlmostEqual(abs(yaw), 45.0, delta=0.5)

    def test_yaw_zero_when_no_osm(self):
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch("threedmr._urllib_fetch", side_effect=OSError("network down")),
        ):
            yaw = threedmr.osm_yaw(0.0, 0.0, 4, self._square_model_points(), d)
        self.assertEqual(yaw, 0.0)

    def test_yaw_cache(self):
        xml = _osm_square_xml(4, 10.0, 10.0)
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch("threedmr._urllib_fetch", return_value=xml) as m,
        ):
            first = threedmr.osm_yaw(0.0, 0.0, 4, self._square_model_points(), d)
            second = threedmr.osm_yaw(0.0, 0.0, 4, self._square_model_points(), d)
        self.assertEqual(first, second)
        self.assertEqual(
            m.call_count, 1, "the second call reads the cache, no repeat OSM request"
        )

    def test_yaw_norm_minus45_to_45(self):
        self.assertAlmostEqual(threedmr._yaw_from_angles(5.0, 50.0), -45.0, places=6)

    def test_no_yaw_env_skips_osm_entirely(self):
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch("threedmr._urllib_fetch") as m,
            mock.patch.dict(os.environ, {threedmr.NO_YAW_ENV: "1"}),
        ):
            yaw = threedmr.osm_yaw(0.0, 0.0, 4, self._square_model_points(), d)
        self.assertEqual(yaw, 0.0)
        m.assert_not_called()


class RotateBeforeCenterTest(unittest.TestCase):
    def test_rotate_before_center(self):
        # an off-centre mesh (its bbox centred on (100, 50), not the origin), rotated by rotate_xz and then
        # written: write_model's own centering must still land it on (0, 0) regardless of the rotation
        base = np.array(
            [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 6.0, 0.0], [0.0, 6.0, 1.0]]
        )
        positions = base + [100.0, 50.0, 0.0]
        tris = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 1], [1, 3, 2]])
        uvs = np.zeros((4, 2))
        rotated = threedmr.rotate_xz(positions, 37.0)
        self.assertFalse(
            np.allclose(rotated, positions), "rotate_xz actually changed the XY columns"
        )
        with tempfile.TemporaryDirectory() as d:
            result = threedmr.write_model(
                d, "99", rotated, tris, uvs, None, None, ground_z=0.0
            )
            verts = cdae.mesh_arrays(
                cdae.read(os.path.join(d, result["shape"]))["meshes"][0]
            )["verts"]
        cx = (float(verts[:, 0].min()) + float(verts[:, 0].max())) / 2.0
        cy = (float(verts[:, 1].min()) + float(verts[:, 1].max())) / 2.0
        self.assertAlmostEqual(cx, 0.0, places=3)
        self.assertAlmostEqual(cy, 0.0, places=3)


class FeatureVersionTest(unittest.TestCase):
    """FEATURE_VERSION in the roads revisit-cache key: a tile built before 3dmr geo-search existed must not
    come back from the cache without it running."""

    def test_version_is_28(self):
        self.assertEqual(threedmr.FEATURE_VERSION, 28)

    def test_revisit_key_changes_with_feature_version(self):
        base = (
            "roads",
            1.0,
            2.0,
            16,
            3,
            4,
            False,
            0.0,
            False,
            False,
            True,
            None,
            False,
            None,
        )
        self.assertNotEqual(revisit.key(*base, 0), revisit.key(*base, 1))

    def test_blockworker_roads_key_includes_feature_version(self):
        src = inspect.getsource(blockworker._roads_tile_cached)
        self.assertIn("threedmr.FEATURE_VERSION", src)


def _binary_stl(triangles):
    """A minimal binary STL (80-byte header, 4-byte triangle count, 50 bytes/triangle: 12-byte normal - left
    zero, _load_stl computes its own - 36-byte vertices, 2-byte attribute) of triangles ([(v0, v1, v2), ...],
    each vi a 3-float xyz)."""
    body = b"\x00" * 80 + struct.pack("<I", len(triangles))
    for v0, v1, v2 in triangles:
        body += struct.pack(
            "<3f", 0.0, 0.0, 0.0
        )  # normal: left zero on purpose (exporters often do)
        body += struct.pack("<9f", *v0, *v1, *v2)
        body += struct.pack("<H", 0)
    return body


_TETRAHEDRON = [
    ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
    ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
]


class LoadLocalModelTest(unittest.TestCase):
    """load_local_model: the local catalog's own counterpart of gltf_mesh, for the player's own .stl/.glb
    files (no 3dmr.eu base at all - the repository's local_models/ example)."""

    def test_binary_stl_decodes(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "threedmr_cache")
            os.makedirs(cache)
            with open(os.path.join(cache, "tetra.stl"), "wb") as f:
                f.write(_binary_stl(_TETRAHEDRON))
            entry = {
                "id": "local_tetra",
                "file": "tetra.stl",
                "factor": [0.784, 0.063, 0.18],
            }
            pos, uvs, tris, nrm, textures, tri_mat = threedmr.load_local_model(d, entry)
        self.assertEqual(
            pos.shape, (12, 3), "4 triangles, each owning its own 3 vertices"
        )
        self.assertEqual(tris.shape, (4, 3))
        np.testing.assert_array_equal(tris, np.arange(12).reshape(4, 3))
        self.assertEqual(uvs.shape, (12, 2))
        np.testing.assert_array_equal(uvs, np.zeros((12, 2)))
        self.assertIsNotNone(nrm)
        self.assertEqual(nrm.shape, (12, 3))
        self.assertTrue(np.all(np.isfinite(nrm)))
        self.assertAlmostEqual(
            float(np.linalg.norm(nrm[0])), 1.0, places=5, msg="normalized"
        )
        self.assertEqual(len(textures), 1)
        _tex, _ext, factor, has_alpha, alpha_cutoff, _double_sided = textures[0]
        self.assertEqual(factor, (0.784, 0.063, 0.18))
        self.assertFalse(has_alpha)
        self.assertIsNone(alpha_cutoff)
        self.assertEqual(tri_mat.tolist(), [0, 0, 0, 0])

    def test_binary_stl_without_factor(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "threedmr_cache")
            os.makedirs(cache)
            with open(os.path.join(cache, "tetra.stl"), "wb") as f:
                f.write(_binary_stl(_TETRAHEDRON))
            entry = {"id": "local_tetra", "file": "tetra.stl"}
            _pos, _uvs, _tris, _nrm, textures, _tri_mat = threedmr.load_local_model(
                d, entry
            )
        self.assertIsNone(textures[0][2], "no factor in the catalog entry: no tint")

    def test_ascii_stl_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "threedmr_cache")
            os.makedirs(cache)
            with open(os.path.join(cache, "ascii.stl"), "w") as f:
                f.write("solid ascii\nendsolid ascii\n")
            entry = {"id": "local_ascii", "file": "ascii.stl"}
            with self.assertRaises(threedmr.UnsupportedModelError):
                threedmr.load_local_model(d, entry)

    def test_glb_dispatches_to_gltf_mesh(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "threedmr_cache")
            os.makedirs(cache)
            glb_path = os.path.join(cache, "thing.glb")
            with open(glb_path, "wb") as f:
                f.write(b"fake glb bytes")
            entry = {
                "id": "local_thing",
                "file": "thing.glb",
                "factor": [1.0, 0.0, 0.0],
            }
            sentinel = object()
            with mock.patch("threedmr.gltf_mesh", return_value=sentinel) as gltf_mesh:
                result = threedmr.load_local_model(d, entry)
            self.assertIs(result, sentinel)
            gltf_mesh.assert_called_once_with(b"fake glb bytes", model_id="local_thing")

    def test_unsupported_extension_raises(self):
        with tempfile.TemporaryDirectory() as d:
            entry = {"id": "local_x", "file": "thing.obj"}
            with self.assertRaises(threedmr.UnsupportedModelError):
                threedmr.load_local_model(d, entry)


class LocalModelsTest(unittest.TestCase):
    def test_reads_catalog_and_adds_id(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "threedmr_cache")
            os.makedirs(cache)
            catalog = [
                {
                    "file": "fire_tower_20m.stl",
                    "lat": 50.24378,
                    "lon": 137.93470,
                    "factor": [0.784, 0.063, 0.18],
                    "title": "Fire lookout tower",
                }
            ]
            with open(os.path.join(cache, threedmr.LOCAL_MODELS_FILE), "w") as f:
                json.dump(catalog, f)
            models = threedmr.local_models(d)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["id"], "local_fire_tower_20m")
        self.assertEqual(models[0]["lat"], 50.24378)
        self.assertEqual(models[0]["file"], "fire_tower_20m.stl")

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(threedmr.local_models(d), [])

    def test_signature_follows_the_catalog_and_its_files(self):
        # the road tiles' revisit keys (blockworker): a local model added, moved or replaced is built in a place
        # visited before
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(threedmr.local_signature(d), ())
            self.assertEqual(threedmr.local_signature(None), ())
            cache = os.path.join(d, "threedmr_cache")
            os.makedirs(cache)
            with open(os.path.join(cache, "tower.stl"), "wb") as f:
                f.write(b"x" * 84)
            entry = {"file": "tower.stl", "lat": 1.0, "lon": 2.0}
            with open(os.path.join(cache, threedmr.LOCAL_MODELS_FILE), "w") as f:
                json.dump([entry], f)
            first = threedmr.local_signature(d)
            self.assertEqual(len(first), 1)
            self.assertEqual(threedmr.local_signature(d), first, "the same catalog: the same signature")
            with open(os.path.join(cache, threedmr.LOCAL_MODELS_FILE), "w") as f:
                json.dump([dict(entry, lat=1.001)], f)
            moved = threedmr.local_signature(d)
            self.assertNotEqual(moved, first, "a model moved")
            with open(os.path.join(cache, "tower.stl"), "wb") as f:
                f.write(b"x" * 134)
            self.assertNotEqual(threedmr.local_signature(d), moved, "a model's file replaced")

    def test_unparseable_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "threedmr_cache")
            os.makedirs(cache)
            with open(os.path.join(cache, threedmr.LOCAL_MODELS_FILE), "w") as f:
                f.write("not json")
            self.assertEqual(threedmr.local_models(d), [])


if __name__ == "__main__":
    unittest.main()


def _bridge_mesh(length=200.0, width=20.0, plinth=6.0):
    """A model bridge as 3dmr.eu's id 22 is drawn: a 2 m slab whose every face is doubled with the opposite
    normal (two-sided), its top an arch (z 5 at the ends, 15 mid-span), a 0.6 m raised median 4 m wide, a box
    (the dragon) 4 m over the median, and a lower plinth 1 m down reaching `plinth` m past each end."""
    P, T = [], []

    def quad(a, b, c, d):
        n = len(P)
        P.extend([a, b, c, d])
        T.extend([[n, n + 1, n + 2], [n, n + 2, n + 3], [n, n + 2, n + 1], [n, n + 3, n + 2]])   # both sides

    def top(x):
        return 5.0 + 10.0 * (1.0 - (2.0 * x / length) ** 2)
    xs = np.linspace(-length / 2, length / 2, 41)
    h = width / 2
    for x0, x1 in zip(xs[:-1], xs[1:]):
        for (y0, y1, dz) in ((-h, -2.0, 0.0), (-2.0, 2.0, 0.6), (2.0, h, 0.0)):
            quad([x0, y0, top(x0) + dz], [x1, y0, top(x1) + dz], [x1, y1, top(x1) + dz], [x0, y1, top(x0) + dz])
            quad([x0, y0, top(x0) - 2], [x1, y0, top(x1) - 2], [x1, y1, top(x1) - 2], [x0, y1, top(x0) - 2])
        quad([x0, -2.0, top(x0) + 4.6], [x1, -2.0, top(x1) + 4.6], [x1, 2.0, top(x1) + 4.6], [x0, 2.0, top(x0) + 4.6])
    for s in (-1, 1):                       # the plinths
        xa, xb = s * length / 2, s * (length / 2 + plinth)
        quad([xa, -h, 4.0], [xb, -h, 4.0], [xb, h, 4.0], [xa, h, 4.0])
    return np.array(P, np.float64), np.array(T, np.int64)


class BridgeDeckTest(unittest.TestCase):
    """A model bridge carries the road (FV=24): bridge_deck_surface traces the top of its deck (not its slab's lower
    skin, not the dragon, not the plinths past its ends), bridge_tilt leans it onto the road's ends, write_bridge_deck
    writes a closed collision slab with sinking aprons, bridge_footprint is cached per model, under_footprint."""

    def setUp(self):
        self.P, self.T = _bridge_mesh()
        self.surf = threedmr.bridge_deck_surface(self.P, self.T)

    def _z(self, x, y):
        s = self.surf
        i = int(round((x - s["u0"]) / s["du"]))
        j = int(round((y - s["v0"]) / s["dv"]))
        return s["z"][i, j]

    def test_traces_the_top_of_the_deck(self):
        self.assertEqual(self.surf["ax"], 0)
        self.assertAlmostEqual(self._z(0.0, -6.0), 15.0, delta=0.15)       # the lane: the slab's top
        self.assertAlmostEqual(self._z(0.0, 0.0), 15.6, delta=0.15)        # the raised median stays
        self.assertAlmostEqual(self._z(-60.0, 6.0), 5.0 + 10.0 * (1 - 0.36), delta=0.15)
        Z = self.surf["z"]
        self.assertLess(np.nanmax(Z), 16.0, "the dragon's back is no floor")
        self.assertGreater(np.nanmin(Z), 4.5, "nor the slab's lower skin or a plinth")

    def test_ends_where_the_level_steps(self):
        Z = self.surf["z"]
        rows = np.nonzero(np.isfinite(Z).any(1))[0]
        us = self.surf["u0"] + self.surf["du"] * rows
        self.assertGreaterEqual(us.min(), -100.5)
        self.assertLessEqual(us.max(), 100.5, "the plinths past the ends are cut off")

    def test_tilt_lands_the_deck_on_the_road_ends(self):
        tilt = threedmr.bridge_tilt(self.surf, lambda x, y: 0.0, 22, road_ends=[(-90.0, -5.0, 1.0), (-90.0, 5.0, 1.0),
                                                                                 (90.0, -5.0, 3.0), (90.0, 5.0, 3.0)])
        self.assertEqual(tilt[4], (-90.0, 90.0))
        for u, want in ((-90.0, 1.0), (90.0, 3.0)):
            level = threedmr._deck_level_at(self.surf, u)[1] + threedmr.tilt_dz(tilt, [[u, 0.0]])[0]
            self.assertAlmostEqual(level, want + threedmr.BRIDGE_END_EPS_M, delta=0.35)   # (the median in the median)

    def test_tilt_without_road_ends_uses_the_ground_at_the_deck_ends(self):
        tilt = threedmr.bridge_tilt(self.surf, lambda x, y: 2.0, 22)
        (ua, _v, za), (ub, _v2, zb) = threedmr._deck_ends(self.surf)
        self.assertAlmostEqual(za + threedmr.tilt_dz(tilt, [[ua, 0.0]])[0], 2.0 + threedmr.BRIDGE_END_EPS_M, places=6)
        self.assertAlmostEqual(zb + threedmr.tilt_dz(tilt, [[ub, 0.0]])[0], 2.0 + threedmr.BRIDGE_END_EPS_M, places=6)

    def test_far_off_ends_do_not_lean(self):
        tilt = threedmr.bridge_tilt(self.surf, lambda x, y: -100.0, 22)
        self.assertEqual(tilt[2:4], (0.0, 0.0))

    def test_slab_is_closed_wound_outwards_and_sinks_past_its_ends(self):
        tilt = threedmr.bridge_tilt(self.surf, lambda x, y: 0.0, 22, road_ends=[(-90.0, 0.0, 1.0), (90.0, 0.0, 1.0)])
        with tempfile.TemporaryDirectory() as d:
            got = threedmr.write_bridge_deck(d, 22, self.surf, tilt)
            self.assertEqual(got["shape"], "threedmr_22_deck.cdae")
            shape = cdae.read(os.path.join(d, got["shape"]))
            self.assertTrue(any(str(n).startswith("Col") for n in shape["names"]), "a collision detail (Colmesh)")
        V, T = got["verts"], got["tris"]
        edges = {}
        for t in T:
            for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                edges[(a, b)] = edges.get((a, b), 0) + 1
        self.assertTrue(all(edges.get((b, a), 0) == 1 and n == 1 for (a, b), n in edges.items()),
                        "closed and consistently wound: every edge once each way")
        n = np.cross(V[T[:, 1]] - V[T[:, 0]], V[T[:, 2]] - V[T[:, 0]])
        vol = float(np.einsum("ij,ij->i", V[T[:, 0]], n).sum()) / 6.0
        self.assertGreater(vol, 0.0, "wound counter-clockwise seen from outside (cdae's)")
        x = V[:, 0]
        self.assertAlmostEqual(x.min(), -90.0 - threedmr.BRIDGE_APRON_M, delta=threedmr.BRIDGE_CELL_U_M)
        self.assertAlmostEqual(x.max(), 90.0 + threedmr.BRIDGE_APRON_M, delta=threedmr.BRIDGE_CELL_U_M)
        tip = V[(x <= x.min() + 0.01)][:, 2].max()
        end = [e[2] for e in got["ends"]]
        self.assertLess(tip, min(end) - threedmr.BRIDGE_APRON_DROP_M + 0.05, "the apron sinks under its road")

    def test_footprint_cached_per_model_and_under_footprint(self):
        calls = []

        def mesh():
            calls.append(1)
            return self.P, self.T, True
        with tempfile.TemporaryDirectory() as d:
            fp = threedmr.bridge_footprint(22, d, mesh)
            self.assertTrue(fp["bridge"])
            self.assertEqual(threedmr.bridge_footprint(22, d, mesh), fp)
            self.assertEqual(len(calls), 1, "the second tile reads the cache")
            self.assertEqual(threedmr.bridge_footprint(4, d, lambda: (self.P, self.T, False)), {"bridge": False})
        inside = threedmr.under_footprint(fp, (1000.0, 2000.0), [[1000.0, 2000.0], [1090.0, 2009.0], [1000.0, 2015.0],
                                                                  [1120.0, 2000.0]])
        self.assertEqual(inside.tolist(), [True, True, False, False])

    def test_write_model_leans_by_the_tilt(self):
        tilt = (0, -100.0, -2.0, 0.01, (-100.0, 100.0))
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("threedmr._write_section", side_effect=lambda out, mid, P, *a, **k: {"P": P}):
                got = threedmr.write_model(d, 22, self.P, self.T, None, None, None, 0.0, tilt=tilt)
        P = got["P"]
        base = self.P[:, 2] - threedmr._support_z(self.P[:, 2]) - 0.2
        np.testing.assert_allclose(P[:, 2] - base, -2.0 + 0.01 * (P[:, 0] + 100.0), atol=1e-9)


class DownloadTlsTest(unittest.TestCase):
    """3dmr.eu's requests take the system's certificate check (netfetch.windows_ssl_context), as the gravity download
    (GitHub #567): Python's own chain picked an expired intermediate for its Let's Encrypt YE1 certificate on Windows."""

    def test_the_platform_tls_context_is_used(self):
        import io
        from unittest import mock
        import netfetch
        import threedmr
        seen = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None, context=None):
            seen.append(context)
            return Resp(b"[1, 2]")

        ctx = object()
        with mock.patch.object(netfetch, "windows_ssl_context", return_value=ctx), \
                mock.patch.object(threedmr.urllib.request, "urlopen", urlopen):
            self.assertEqual(threedmr._urllib_fetch("https://3dmr.eu/api/search/0/0/100", 5), b"[1, 2]")
        self.assertEqual(seen, [ctx])
