"""Roof colours with the generated ground on (the user, 2026-11-15: "The switch should effect roof colors too"): no
regional / 1 m photo is fetched or read for roofs, squares or street fronts (buildings._photo_sources empty,
roof_photo_final's final None, no Imagery made), the roofs take their mapped colour or the style's hashed pick; with
the switch off, the photo sources are as before."""
import os
import sys
import unittest

import numpy as np
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import buildings  # noqa: E402
import genground  # noqa: E402
import geodesy  # noqa: E402
import imagery  # noqa: E402
import streetfront  # noqa: E402

FELLS = (39.2826, -76.5927)


class RoofsTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(*FELLS)
        self.box = (-128.0, -128.0, 128.0, 128.0)

    def test_off_unchanged(self):
        with mock.patch.object(genground, "ON", False):
            self.assertIn("md6in", buildings._photo_sources(self.frame, *self.box),
                          "switch off: Maryland's photo for the roofs, as before")

    def test_on_asks_no_photo_host(self):
        def no_imagery(*a, **k):
            raise AssertionError("an imagery reader was made for the roofs")
        with mock.patch.object(genground, "ON", True), \
                mock.patch.object(imagery.Imagery, "__init__", no_imagery), \
                mock.patch.object(imagery.Imagery, "render", no_imagery), \
                mock.patch.object(imagery.Imagery, "fetch_lazy", no_imagery):
            self.assertEqual(buildings._photo_sources(self.frame, *self.box), ())
            self.assertEqual(buildings.roof_photo_final(self.frame, *self.box, "/nonexistent-cache", online=True),
                             (None, True, None), "final: the procedural roofs, no revisit fault")
            fut = buildings.RoofPhotoFuture(self.frame, *self.box, "/nonexistent-cache")
            self.assertIsNone(fut.fut, "no fetch thread")
            self.assertIsNone(fut.colours([[(0, 0), (10, 0), (10, 10)]]))
            self.assertIsNone(streetfront.cached_photo(self.frame, self.box, "/nonexistent-cache"))

    def test_roof_from_tags_or_style(self):
        ring = np.array([(0.0, 0.0), (8.0, 0.0), (8.0, 12.0), (0.0, 12.0)])
        base = {"class": "house", "roof_shape": "gabled"}
        with mock.patch.object(genground, "ON", True):
            a = buildings.plan(ring, dict(base, id="b1"), 0, *FELLS).roof_mat
            b = buildings.plan(ring, dict(base, id="b1"), 0, *FELLS).roof_mat
            self.assertEqual(a, b, "the same building, the same roof")
            red = buildings.plan(ring, dict(base, id="b1", roof_color="#aa2211"), 0, *FELLS).roof_mat
            self.assertIn("|c_", red, "a mapped roof colour wins")

    def test_flat_roofs_light_membranes(self):
        # (2026-11-16, Fells Point against the 3-inch photo: flat roofs mostly white / light grey, some black; the
        # asphalt greys drew them all dark) a rowhouse with a neighbour is flat; with the switch on its membrane is
        # picked by its seed, light for most; switch off, the old asphalt tints
        ring = np.array([(0.0, 0.0), (5.0, 0.0), (5.0, 14.0), (0.0, 14.0)])
        light, looks = 0, set()
        for k in range(400):
            at = {"class": "house", "seed_id": f"w{k}"}
            with mock.patch.object(genground, "ON", True):
                on = buildings.plan(ring, at, 2, *FELLS, None, False, True)
                again = buildings.plan(ring, dict(at), 2, *FELLS, None, False, True)
            with mock.patch.object(genground, "ON", False):
                off = buildings.plan(ring, dict(at), 2, *FELLS, None, False, True)
            self.assertEqual(on.roof, "flat")
            self.assertEqual(on.roof_mat, again.roof_mat, "the building's own pick")
            self.assertRegex(off.roof_mat, r"^bne_bld_roof_flat\|\d+$", "switch off: as before")
            mat, tint = buildings.LOOKS[on.roof_mat]
            looks.add(on.roof_mat)
            rgb = np.array(buildings.ROOF_TEX_MEAN[mat]) * np.array(tint)
            light += rgb.mean() > 0.6
        self.assertGreater(light / 400, 0.55, "mostly white and light grey")
        self.assertLess(light / 400, 0.85, "some gravel and black")
        self.assertLessEqual(len(looks), 4)


if __name__ == "__main__":
    unittest.main()
