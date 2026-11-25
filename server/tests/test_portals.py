"""Tunnel entrances (2026-11-12, the user: "focus on tunnel entrances ... Many entrances are broken or ugly and look
nothing like the real version"): the structure at a mouth built from the ground round it (portals.fit / structure,
earthfast.portal_mesh), the tube's outside where the ground does not cover it (portals.cover), the look from the
road and the ground (earthfast.portal_style)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import portals  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402


def _tube(cls, elev, line=((-100, 0), (100, 0))):
    from test_roads import features, FRAME
    f = features([list(line)], [cls], flags=[(0, "is_tunnel")])
    tp = roads.TunnelProfiles(FRAME, lambda key: f, elev)
    ch = roads._tunnel_chunks(tp, [f], (-128.0, -128.0, 128.0, 128.0))
    return [c for c in ch if c.get("tunnel")]


def _ridge(peak=40.0, slope=0.8, half=30.0):
    """A ridge over x in (-half, half) rising `slope` per m from 10 m to 10 + peak, flat ground beyond."""
    from test_roads import LON, M_LON

    def elev(la, lo):
        e = (np.asarray(lo) - LON) * M_LON
        return 10.0 + np.clip((half + 20.0 - np.abs(e)) * slope, 0.0, peak)
    return elev


@unittest.skipUnless(portals.ON, "BNE_PORTALS=0")
class Entrance(unittest.TestCase):
    def test_bare_portal_exports_both_cliff_colours_with_the_same_mesh(self):
        from tempfile import TemporaryDirectory

        import cdae

        origin = np.array([10., 7., 0.])
        chunk = roads._chunk(roads.TUNNEL, 4., np.array([[10., 7., 5.], [40., 7., 5.]]))
        chunk.update(fixed=True, deck=True, tunnel=True, portal=[True, False],
                     norway_rock=True, norway_portal_style=["rock", None])
        palette = np.array([[60, 74, 82], [96, 88, 77]], dtype=np.uint8)

        def sample(point, left, outward, half_width):
            np.testing.assert_allclose(point, origin[:2])
            np.testing.assert_allclose(left, [0., 1.])
            np.testing.assert_allclose(outward, [-1., 0.])
            self.assertAlmostEqual(half_width, 2. + roadmesh.TUNNEL_SHOULDER)
            return palette / 255.

        before = roadmesh.tunnel([chunk], origin, rock_colour=lambda *_: None)
        self.assertEqual(len(before), 5)
        points, triangles, uv, materials, faces, _uv2, colours = roadmesh.tunnel([chunk], origin, rock_colour=sample)
        for old, new in zip(before[:3], (points, triangles, uv)):
            np.testing.assert_array_equal(old, new)
        vertices = np.unique(triangles[faces == materials.index(roadmesh.PORTAL_ROCK_LOCAL)])
        self.assertTrue(np.any(np.all(colours[vertices, :3] == palette[0], axis=1)))
        self.assertTrue(np.any(np.all(colours[vertices, :3] == palette[1], axis=1)))
        np.testing.assert_array_equal(colours[np.setdiff1d(np.arange(len(points)), vertices)], 255)
        with TemporaryDirectory() as work:
            path = Path(work) / "portal.cdae"
            roadmesh.write(str(path), (points, triangles, uv, materials, faces, None, colours), close=False)
            mesh = cdae.read(path)["meshes"][0]
        count, stride, raw = mesh["colors"]
        self.assertEqual(stride, 4)
        actual = np.frombuffer(raw, np.uint8).reshape(count, 4)
        for expected, got in zip(np.unique(colours, axis=0, return_counts=True),
                                 np.unique(actual, axis=0, return_counts=True)):
            np.testing.assert_array_equal(expected, got)

    def test_narrow_local_bore_uses_a_compact_full_vault(self):
        import json
        from unittest.mock import patch

        from test_roads import features, FRAME

        def export(cls, width):
            f = features([[(-100., 0.), (100., 0.)]], [cls], widths=[(0, width)], flags=[(0, "is_tunnel")])
            profiles = roads.TunnelProfiles(FRAME, lambda _key: f, _ridge())
            box = (-128., -128., 128., 128.)
            with (patch("norway_tunnels.in_norway", return_value=True),
                  patch("norway_tunnel_nvdb.portal_assessment", return_value=(None, "unknown"))):
                parts = profiles.parts([f], box)
                chunks = [c for c in roads._tunnel_chunks(profiles, [f], box) if c.get("tunnel")]
            self.assertTrue(chunks)
            return profiles, parts, chunks

        profiles, parts, chunks = export("unclassified", 3.1)
        self.assertTrue(all(c.get("compact_tunnel") and c["tunnel_h"] == 4.5 for c in chunks))
        self.assertTrue(all(c.get("norway_rock") for c in chunks))
        spec = roads.spec_from_json(json.loads(json.dumps(roads._spec_json(parts[0][1]))))
        self.assertIsInstance(spec, roads.CompactNorwegianTunnelSpec)
        sample = parts[0][2][len(parts[0][2]) // 2:][:1]
        ground = np.full(len(sample), 2.)
        np.testing.assert_array_equal(
            roads.arch_raise(parts, profiles.heights, *sample.T, ground), ground)
        for cls, width in (("unclassified", 5.5), ("primary", 3.1)):
            _, _, standard = export(cls, width)
            self.assertTrue(all(not c.get("compact_tunnel") for c in standard))

    def test_norwegian_raked_hoods_respect_the_mapped_cross_section(self):
        from unittest.mock import patch

        import norway_tunnels

        class Tunnels:
            frame = object()

        level = {"st": 1, "sl": 2.8, "sr": 2.8, "fg": [4., 7., 4.], "sb": 0.}
        hillside = {**level, "st": 0, "fg": [9., 7., 4.], "sb": 4.}
        chunk = {"tunnel": True, "concrete": True, "oneway": True, "n": [[0., 0., 0.], [70., 0., 0.]],
                 "portal": [True, True], "fit": [level, hillside]}
        with (patch("norway_tunnels.location", return_value=(63.0, 8.0)),
              patch("norway_tunnels.in_norway", return_value=True),
              patch("norway_tunnel_nvdb.portal_assessment") as survey):
            survey.return_value = ({"shape": "arch"}, "present")
            norway_tunnels.tag_chunks(Tunnels(), [chunk])
            self.assertEqual(chunk["norway_portal_style"], ["embankment", "swept"])
            chunk["oneway"] = False
            norway_tunnels.tag_chunks(Tunnels(), [chunk])
            self.assertEqual(chunk["norway_portal_style"], ["arch", "arch"])
            chunk["oneway"] = True
            chunk["fit"] = [level, {**hillside, "sr": 0.7}]
            norway_tunnels.tag_chunks(Tunnels(), [chunk])
            self.assertEqual(chunk["norway_portal_style"], ["embankment", "arch"])
            survey.return_value = ({"shape": "box"}, "present")
            norway_tunnels.tag_chunks(Tunnels(), [chunk])
            self.assertEqual(chunk["norway_portal_style"], ["box", "box"])
            survey.return_value = (None, "no_portal")
            norway_tunnels.tag_chunks(Tunnels(), [chunk])
            self.assertEqual(chunk["norway_portal_style"], ["rock", "rock"])

    def test_norwegian_hoods_extend_into_the_bank_outside_the_clearance(self):
        import norway_tunnels

        half, th = 4.9, roadmesh.TUNNEL_H
        section = roadmesh._tunnel_section(half, shoulder=0.4)
        fit = {"sl": 3., "sr": 3., "wl": [3.], "wr": [6.], "fg": [9., 7., 4.]}
        for style in ("embankment", "swept"):
            points, tris, _, _ = norway_tunnels.mouth_mesh(
                np.zeros(3), [0., 1.], [1., 0.], section, th, half, style, fit, (False, False))
            front = points[len(section) - 2:2 * (len(section) - 2)]
            self.assertGreater(np.max(front[:, 0]), 2.7)
            np.testing.assert_array_equal(points[:len(front), 1:], section[1:-1])
            if style == "embankment":
                self.assertAlmostEqual(front[0, 0], front[-1, 0])
            else:
                self.assertGreater(front[0, 0] - front[-1, 0], 1.3)
            faces = points[tris]
            self.assertFalse(((np.abs(faces[:, :, 1]) < half - 0.4).all(axis=1)
                              & (faces[:, :, 2] < 4.0).all(axis=1)).any())
        points, _, _, _ = norway_tunnels.mouth_mesh(
            np.zeros(3), [0., 1.], [1., 0.], section, th, half, "swept", fit, (True, False))
        self.assertLess(float(points[points[:, 1] > half, 0].max()), 1.05)

    def test_a_mountain_bore_gets_a_headwall_not_a_slab(self):
        tube = _tube("primary", _ridge())
        self.assertTrue(tube)
        fits = [f for c in tube for f in (c.get("fit") or []) if f is not None]
        self.assertTrue(fits, "each mouth has its ground fit")
        P, T, UV, mats, M = roadmesh.tunnel(tube, (0.0, 0.0, 0.0))
        # the old face stood PORTAL_FRAME (6 m) beside the opening at the vault's height + 6 m: a slab on the hillside.
        # Now nothing of the structure stands more than its parapet over the ground beside the opening ...
        th = max(float(c.get("tunnel_h") or roadmesh.TUNNEL_H) for c in tube)
        hw = float(tube[0]["w"]) / 2 + roadmesh.TUNNEL_SHOULDER
        self.assertLess(np.abs(P[:, 1]).max(), hw + portals.FACE_SIDE + 2.0, "no frame 6 m beside the opening")
        # ... and its top over the opening is no higher than the opening needs (th + top_max)
        z0 = min(float(np.asarray(c["n"])[:, 2].min()) for c in tube)
        self.assertLess(P[:, 2].max(), z0 + th + max(lk[6] for lk in portals.LOOKS.values()) + 0.5)

    def test_flat_ground_beside_the_mouth_brings_the_headwall_down(self):
        # the headwall's top: the parapet over the ground where it is high, sloping down to end_min where it is low
        fitd = {"st": 1, "fx": [-8.0, -5.0, 0.0, 5.0, 8.0], "fg": [0.0, 0.0, 9.0, 0.0, 0.0], "ws": [2.0, 4.0],
                "wl": None, "wr": None, "rz": [0.0, 0.0], "sl": 3.0, "sr": 3.0}
        sec = roadmesh._tunnel_section(5.0, 6.8)
        P, T, UV, C = portals.structure(fitd, np.zeros(3), np.array([0.0, 1.0]), np.array([1.0, 0.0]), sec, 6.8, 5.0,
                                        4.0)
        self.assertTrue(len(T))
        ends = P[np.abs(P[:, 1]) > 7.9]
        lk = portals.LOOKS[1]
        self.assertLessEqual(ends[:, 2].max(), lk[8] + 1e-6, "the wall's ends at end_min where the ground is low")
        mid = P[np.abs(P[:, 1]) < 0.2]
        self.assertGreaterEqual(mid[:, 2].max(), 6.8 + lk[5] - 1e-6, "over the opening at least th + top_min")

    def test_thin_ground_over_a_full_tube_is_covered(self):
        # a long tube under a low saddle (the ground 1 m over its vault in the middle): the outside built there
        mid = np.c_[np.arange(0.0, 50.0, 5.0), np.zeros(10)]
        left = np.tile([0.0, 1.0], (10, 1))
        z = np.zeros(10)
        g = np.full(10, 20.0)
        g[4:6] = 6.0                    # under th + COVER_NEED
        sec = roadmesh._tunnel_section(5.0, 6.8)
        P, T, UV, C = portals.cover(mid, left, z, g, 5.0, 6.8, (False, False), 4.0, 1.0, sec)
        self.assertTrue(len(T))
        self.assertGreaterEqual(P[:, 2].max(), 6.8 + portals.COVER_OVER - 1e-6)
        self.assertTrue((np.asarray(C) == 1).any(), "soil on its top")
        self.assertTrue(((P[:, 0] >= 10.0 - 1e-6) & (P[:, 0] <= 35.0 + 1e-6)).all(), "only round the thin rows")
        # its ends inside the hill leave the tube's opening clear (no wall across the road)
        tri = P[np.asarray(T)]
        cen = tri.mean(1)
        ends = np.all(np.abs(tri[:, :, 0] - tri[:, :1, 0]) < 1e-6, axis=1)
        inside = ends & (np.abs(cen[:, 1]) < 4.0) & (cen[:, 2] > 0.2) & (cen[:, 2] < 4.0)
        self.assertFalse(inside.any(), "an end cap across the tube's opening")

    def test_norway_portal_fills_the_hillside_gap_on_a_descending_bore(self):
        ch = roads._chunk(roads.TUNNEL, 9.6, np.array([[0., 0., 0.], [12., 0., -0.72], [30., 0., -1.8]]))
        ch.update(fixed=True, deck=True, tunnel=True, portal=[True, False], norway_portal_style=["arch", None],
                  ground=[1., 1., 20.], fit=[{"st": 0, "sb": 3., "fx": [-9., -6., 0., 6., 9.],
                                             "fg": [18., 17., 13.6, 9., 5.], "ws": [], "rz": [],
                                             "sl": 3., "sr": 3.}, None])
        points, triangles, _, materials, _ = roadmesh.tunnel([ch], np.zeros(3))
        self.assertIn(roadmesh.TUNNEL_SOIL, materials, "cover continues over the exposed bore")
        upper = points[points[:, 2] > 9.5]
        self.assertGreater(np.ptp(upper[:, 0]), 1.0, "the rock rises back into the hillside")
        a, b, c = points[triangles, 1:].transpose(1, 0, 2)
        edge_a, edge_b = b - a, c - a
        cross = lambda u, v: u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]
        det = cross(edge_a, edge_b)

        def intersects(y, z):
            rel = [y, z] - a
            with np.errstate(divide="ignore", invalid="ignore"):
                u, v = cross(rel, edge_b) / det, cross(edge_a, rel) / det
                return ((np.abs(det) > 1e-8) & (u >= 0.) & (v >= 0.) & (u + v <= 1.)).any()

        self.assertTrue(intersects(0.35, 10.), "the headwall fills the gap above the crown")
        for y in (-2.5, 0., 2.5):
            self.assertFalse(intersects(y, 1.7), "the headwall and cover leave the driving lane open")

    def test_norwegian_models_have_distinct_faces_and_the_same_rear_opening(self):
        import norway_tunnels

        half_width, height = 4.9, roadmesh.TUNNEL_H
        sec = roadmesh._tunnel_section(half_width, shoulder=0.4)
        opening = np.c_[np.zeros(len(sec) - 2), sec[1:-1]]
        models = {}
        for style in ("arch", "box", "rock"):
            points, tris, uvs, materials = norway_tunnels.mouth_mesh(
                np.zeros(3), [0., 1.], [1., 0.], sec, height, half_width, style, None, (False, False))
            self.assertTrue(np.isfinite(points).all() and np.isfinite(uvs).all())
            rear = points[np.abs(points[:, 0]) < 1e-8]
            distances = np.linalg.norm(opening[:, None] - rear[None], axis=2)
            self.assertLess(float(distances.min(axis=1).max()), 1e-8)
            faces = points[tris]
            # A jamb or roof must stay outside this full rectangle along the throat.
            self.assertFalse(((np.abs(faces[:, :, 1]) < 3.5).all(axis=1)
                              & (faces[:, :, 2] < 4.).all(axis=1)).any())
            models[style] = points
            self.assertEqual(2 in materials, style != "rock")
        self.assertGreater(models["arch"][:, 0].max(), models["box"][:, 0].max() + 0.4)
        self.assertLess(models["rock"][:, 0].max(), 0.4)
        self.assertLess(models["rock"][:, 0].min(), -0.2, "the rock edges recede into the hillside")
        self.assertGreater(np.count_nonzero(models["rock"][:, 2] > height + 0.65), 20)
        front = models["box"][:2 * len(opening)][len(opening):]
        flat = (np.abs(front[:, 1]) < 0.65 * half_width)
        self.assertLess(np.ptp(front[flat, 2]), 0.01, "the lintel is flat across the middle")

    def test_faceted_rock_mouth_preserves_the_road_and_headroom(self):
        import norway_tunnels

        for half_width in (2.6, 4.9, 6.4):
            section = roadmesh._tunnel_section(half_width, shoulder=0.4)
            for direction in (-1.0, 1.0):
                p, t, _, _ = norway_tunnels.mouth_mesh(
                    np.zeros(3), [0., direction], [direction, 0.], section, roadmesh.TUNNEL_H,
                    half_width, "rock", {"sl": 0.7, "sr": 2.2}, (True, False))
                a, b, c = p[t, 1:].transpose(1, 0, 2)
                ab, ac = b - a, c - a
                det = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
                for offset in (-half_width + 0.4, 0., half_width - 0.4):
                    for height in (0.4, 1.7, 4.0):
                        rel = [offset, height] - a
                        with np.errstate(divide="ignore", invalid="ignore"):
                            u = (rel[:, 0] * ac[:, 1] - rel[:, 1] * ac[:, 0]) / det
                            v = (ab[:, 0] * rel[:, 1] - ab[:, 1] * rel[:, 0]) / det
                        self.assertFalse(((np.abs(det) > 1e-9) & (u >= 0.) & (v >= 0.) & (u + v <= 1.)).any())

    def test_norwegian_bore_and_cut_follow_both_approach_widths(self):
        spec = roads.NorwegianTunnelSpec(*roads.Spec(8.0, "paved", False, "trunk", False)).with_widths(
            [0., 0.2, 0.8, 1.], [8., 8., 12., 12.])
        pts = np.c_[np.arange(-40., 41., 2.), np.zeros(41), np.zeros(41)]

        class Tun:
            def parts(self, cells, box):
                return [("test", spec, pts[:, :2], pts[:, 2], 0.)]

            def height(self, fid):
                return roadmesh.TUNNEL_H

        parts = Tun().parts([], None)
        chunks = roads._tunnel_chunks(Tun(), [], (-60., -10., 60., 10.))
        decks = [c for c in chunks if c.get("tunnel")]
        self.assertEqual(decks[0]["ws"][0], 8.)
        self.assertEqual(decks[-1]["ws"][-1], 12.)
        points, _, _, _, _ = roadmesh.tunnel(decks, np.zeros(3))
        for station, expected in ((-34., 4.4), (34., 6.4)):
            rows = points[(np.abs(points[:, 0] - station) < 2.1) & np.isclose(points[:, 2], 0.)]
            self.assertAlmostEqual(float(np.abs(rows[:, 1]).max()), expected, delta=0.08)
        self.assertIsNone(roads.tunnel_floor_at(parts, -30., 5.25))
        self.assertAlmostEqual(roads.tunnel_floor_at(parts, 30., 5.25), 0.)
        mask = roads.tunnel_hole_mask(parts, np.full((180, 180), 2.), 0., 0., 0.5)
        self.assertFalse(mask[102, 30])
        self.assertTrue(mask[102, 150])

    def test_styles_from_the_ground(self):
        import fast
        fast.require(portals.VERSION, "portal_style")
        ef = fast._ef
        self.assertEqual(ef.portal_style(True, 6.8, 10.0, 0.5), 0)    # a motorway bore into a mountain: a raked hood
        self.assertEqual(ef.portal_style(False, 6.8, 10.0, 0.5), 2)   # a lane's in a hillside
        self.assertEqual(ef.portal_style(True, 6.8, 1.0, 0.0), 1)     # under flat ground: cut-and-cover
        self.assertEqual(ef.portal_style(True, 6.8, 10.0, 4.0), 0)    # out of a rock cut into a hill: still a hood
        self.assertEqual(ef.portal_style(True, 6.8, 10.0, 9.5), 1)    # out of a trough deeper than the opening


class SurveyedApproach(unittest.TestCase):
    def test_matching_road_descends_into_the_tunnel_on_a_shared_height(self):
        import grading
        import norway_tunnels

        s = np.arange(0., 81., 2.)
        xy = np.c_[-s, np.zeros(len(s))]
        original = grading.Plan(xy, 6.7 - 0.06 * s, 5.0)
        adjacent = grading.Plan(xy + [0., 4.], original.z.copy(), 5.0)
        ends = {"trunk": [(np.zeros(2), np.array([1., 0.]), 0.85, 0.075)]}

        plans = norway_tunnels.join_surveyed_approaches([("trunk", original), ("local", adjacent)], ends)
        revised = plans[0][1].z
        self.assertAlmostEqual(revised[0], 0.85)
        self.assertTrue((np.diff(revised[:11]) > 0.).all(), "the final 20 m drops as the car approaches")
        np.testing.assert_array_equal(revised[s >= 60.], original.z[s >= 60.])
        self.assertAlmostEqual(original.z[0], 6.7, msg="cached road-stage plan remains intact")
        self.assertIs(plans[1][1], adjacent)


class Box(unittest.TestCase):
    def test_a_major_road_s_low_tube_is_a_box(self):
        sec = roadmesh._tunnel_section(9.0, 5.0, low=True, box=True)
        top = sec[sec[:, 1] > 4.9]
        self.assertGreaterEqual(len(top), 2)
        self.assertGreater(np.ptp(top[:, 0]), 15.0, "a flat roof across")
        self.assertAlmostEqual(sec[:, 1].max(), 5.0)


if __name__ == "__main__":
    unittest.main()
