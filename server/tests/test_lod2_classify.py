"""tools/lod2_classify.py: a roof's shape from its faces, as the AdV roofType code the German states give; and the
builder's readers for models without a roofType (Estonia's untyped faces, northing-first coordinates)."""
import importlib.util
import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
spec = importlib.util.spec_from_file_location("lod2_classify", ROOT / "tools" / "lod2_classify.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


def gabled(w=10.0, d=8.0, h=3.0):
    r = d / 2
    return [[(0, 0, 0), (w, 0, 0), (w, r, h), (0, r, h)], [(0, r, h), (w, r, h), (w, d, 0), (0, d, 0)]]


def hipped(w=12.0, d=8.0, h=3.0):
    r = d / 2
    return [[(0, 0, 0), (w, 0, 0), (w - r, r, h), (r, r, h)], [(w, d, 0), (0, d, 0), (r, r, h), (w - r, r, h)],
            [(w, 0, 0), (w, d, 0), (w - r, r, h)], [(0, d, 0), (0, 0, 0), (r, r, h)]]


def pyramid(w=10.0, h=4.0):
    a = (w / 2, w / 2, h)
    return [[(0, 0, 0), (w, 0, 0), a], [(w, 0, 0), (w, w, 0), a], [(w, w, 0), (0, w, 0), a], [(0, w, 0), (0, 0, 0), a]]


def half_hipped(w=10.0, inset=1.5):
    k = 0.75                    # rise per metre of the main planes: eave 0 at y 0 / 8, ridge 3 at y 4
    y2, y3 = 2 / k, 8 - 2 / k   # where the gables stop (z 2) and the small hips start
    return [[(0, 0, 0), (w, 0, 0), (w, y2, 2), (w - inset, 4, 3), (inset, 4, 3), (0, y2, 2)],
            [(w, 8, 0), (0, 8, 0), (0, y3, 2), (inset, 4, 3), (w - inset, 4, 3), (w, y3, 2)],
            [(w, y2, 2), (w, y3, 2), (w - inset, 4, 3)], [(0, y3, 2), (0, y2, 2), (inset, 4, 3)]]


def gambrel(w=10.0):
    return [[(0, 0, 0), (w, 0, 0), (w, 1, 2.5), (0, 1, 2.5)], [(0, 1, 2.5), (w, 1, 2.5), (w, 4, 4), (0, 4, 4)],
            [(0, 4, 4), (w, 4, 4), (w, 7, 2.5), (0, 7, 2.5)], [(0, 7, 2.5), (w, 7, 2.5), (w, 8, 0), (0, 8, 0)]]


def barrel(w=20.0, r=6.0, n=10):
    pts = [(r - r * math.cos(math.pi * i / n), r * math.sin(math.pi * i / n)) for i in range(n + 1)]
    return [[(0, y0, z0), (w, y0, z0), (w, y1, z1), (0, y1, z1)] for (y0, z0), (y1, z1) in zip(pts, pts[1:])]


def triangulated(rings, clockwise=False):
    out = []
    for r in rings:
        for i in range(1, len(r) - 1):
            t = [r[0], r[i], r[i + 1]]
            out.append(t[::-1] if clockwise else t)
    return out


def box_faces(w=10.0, d=8.0, wall=6.0):
    """A gabled house as untyped faces (Estonia's lod2MultiSurface): ground, four walls, the roof."""
    g = [(0, 0, 0), (0, d, 0), (w, d, 0), (w, 0, 0)]
    walls = [[(0, 0, 0), (w, 0, 0), (w, 0, wall), (0, 0, wall)], [(w, d, 0), (0, d, 0), (0, d, wall), (w, d, wall)],
             [(w, 0, 0), (w, d, 0), (w, d, wall + 3), (w, d / 2, wall + 3), (w, 0, wall)],
             [(0, d, 0), (0, 0, 0), (0, 0, wall), (0, d / 2, wall + 3), (0, d, wall)]]
    roof = [[(x, y, z + wall) for x, y, z in r] for r in gabled(w, d)]
    return [g] + walls + roof


class ClassifyTest(unittest.TestCase):
    def test_the_basic_shapes(self):
        self.assertEqual(C.classify([[(0, 0, 5), (10, 0, 5), (10, 8, 5), (0, 8, 5)]]), C.FLAT)
        self.assertEqual(C.classify([[(0, 0, 0), (10, 0, 0), (10, 8, 1.5), (0, 8, 1.5)]]), C.SKILLION)
        self.assertEqual(C.classify(gabled()), C.GABLED)
        self.assertEqual(C.classify(hipped()), C.HIPPED)
        self.assertEqual(C.classify(pyramid()), C.PYRAMIDAL)
        self.assertEqual(C.classify(half_hipped()), C.HALF_HIPPED)
        self.assertEqual(C.classify(gambrel()), C.MANSARD)
        self.assertEqual(C.classify(barrel()), C.ROUND)

    def test_triangulated_and_written_clockwise(self):
        """swissBUILDINGS3D's roofs come as triangles, written clockwise (facing down)."""
        self.assertEqual(C.classify(triangulated(gabled(), clockwise=True)), C.GABLED)
        self.assertEqual(C.classify(triangulated(hipped(), clockwise=True)), C.HIPPED)

    def test_a_slab_with_soffits_and_edges_reads_as_its_top(self):
        top = triangulated(gabled())
        soffit = [[(x, y, z - 0.25) for x, y, z in t[::-1]] for t in top]      # (facing down)
        edge = [[(0, 0, 0), (0, 0, -0.25), (10, 0, -0.25), (10, 0, 0)]]     # (upright)
        self.assertEqual(C.classify(top + soffit + edge), C.GABLED)

    def test_orientation_and_place_do_not_matter(self):
        a = math.radians(33)
        rot = [[(x * math.cos(a) - y * math.sin(a) + 2.6e6, x * math.sin(a) + y * math.cos(a) + 1.2e6, z + 500)
                for x, y, z in r] for r in hipped()]
        self.assertEqual(C.classify(rot), C.HIPPED)

    def test_a_dormer_leaves_a_gabled_roof_gabled(self):
        dormer = [[(4, 1.5, 1.6), (6, 1.5, 1.6), (6, 2.5, 1.9), (4, 2.5, 1.9)]]
        self.assertEqual(C.classify(gabled() + dormer), C.GABLED)

    def test_the_rise_is_the_shapes_not_the_span_of_all_faces(self):
        """A gabled block (3 m rise, eaves at 12 m) over a low flat-roofed wing (4 m): the roof is 3 m, not 11."""
        block = [[(x, y, z + 12) for x, y, z in r] for r in gabled()]
        wing = [[(10, 0, 4), (16, 0, 4), (16, 2, 4), (10, 2, 4)]]
        code, rise = C.classify_rise(block + wing)
        self.assertEqual(code, C.GABLED)
        self.assertAlmostEqual(rise, 3.0, 2)
        self.assertEqual(C.classify_rise([[(0, 0, 5), (10, 0, 5), (10, 8, 5), (0, 8, 5)]]), (C.FLAT, 0.0))

    def test_nothing_usable(self):
        self.assertIsNone(C.classify([]))
        self.assertIsNone(C.classify([[(0, 0, 0), (1, 0, 0), (1, 0, 3)]]))           # (upright only)

    def test_every_code_is_one_the_runtime_knows(self):
        sys.path.insert(0, str(ROOT / "server"))
        import lod2roofs
        for code in (C.FLAT, C.SKILLION, C.GABLED, C.HIPPED, C.HALF_HIPPED, C.MANSARD, C.PYRAMIDAL, C.DOME, C.ROUND):
            self.assertIn(code, lod2roofs.SHAPES)
        self.assertNotIn(C.MIXED, lod2roofs.SHAPES)         # (mixed: the height only)


class UntypedTest(unittest.TestCase):
    def test_untyped_faces_sorted_into_ground_walls_and_roof(self):
        ground, walls, roofs = C.split_untyped(box_faces())
        self.assertEqual((len(ground), len(walls), len(roofs)), (1, 4, 2))
        self.assertEqual(C.classify(roofs), C.GABLED)


class BuilderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import lxml  # noqa: F401
            import pyproj  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("the builder needs lxml and pyproj")
        s = importlib.util.spec_from_file_location("build_lod2_roofs", ROOT / "tools" / "build_lod2_roofs.py")
        cls.B = importlib.util.module_from_spec(s)
        s.loader.exec_module(cls.B)

    def gml(self, kind="10"):
        """An Estonian building: untyped faces, coordinates northing first (EPSG:3301), no roofType."""
        e0, n0 = 538000.0, 6584000.0
        polys = "".join(
            "<gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>"
            + " ".join(f"{n0 + y} {e0 + x} {z + 30}" for x, y, z in r + [r[0]])
            + "</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>"
            for r in box_faces())
        return (f'<bldg:Building xmlns:bldg="http://www.opengis.net/citygml/building/2.0" '
                f'xmlns:gml="http://www.opengis.net/gml" xmlns:gen="http://www.opengis.net/citygml/generics/2.0">'
                f'<gen:intAttribute name="tyyp"><gen:value>{kind}</gen:value></gen:intAttribute>'
                f'<bldg:measuredHeight uom="m">9.0</bldg:measuredHeight>'
                f'<bldg:lod2MultiSurface><gml:MultiSurface>{polys}</gml:MultiSurface></bldg:lod2MultiSurface>'
                f'</bldg:Building>')

    def test_an_estonian_building_gets_its_shape_from_the_faces(self):
        from lxml import etree
        r = self.B.row(etree.fromstring(self.gml()), 3301, False)
        self.assertEqual(r["roof"], "3100")
        self.assertAlmostEqual(r["x"], 538005.0, 1)         # (easting first again)
        self.assertAlmostEqual(r["y"], 6584004.0, 1)
        self.assertEqual(r["height"], 9.0)
        self.assertAlmostEqual(r["zt"] - r["zr"], 3.0, 2)

    def test_foundations_and_ruins_are_left_out(self):
        from lxml import etree
        self.assertIsNone(self.B.row(etree.fromstring(self.gml("30")), 3301, False))

    def test_a_point_inside_a_u_shaped_footprint(self):
        u = np.array([(0, 0), (30, 0), (30, 20), (20, 20), (20, 5), (10, 5), (10, 20), (0, 20)], float)
        x, y, _ = self.B._point_in(u)
        self.assertTrue(self.B._inside(x, y, u))
        self.assertFalse(self.B._inside(15, 12, u))


if __name__ == "__main__":
    unittest.main()
