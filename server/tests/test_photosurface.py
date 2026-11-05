"""Paved or not from the regional photo's colour (photosurface.py; task 010): a grey lane across green fields is asphalt;
a brown or green track, a dark (shaded) road, a road with its tile not on disk yet, one in no regional box, are not
called; what the map says is never overruled; a road's specs are not kept while a tile it needs is missing."""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import photosurface  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402

LAT, LON = 50.52754, 16.27370      # Suchy Dul, inside imagery_regional's "czechia" box


def line_east(lat, lon, metres, n=2):
    """A west-east polyline of `metres` m from (lat, lon), lon / lat rows."""
    dlon = metres / (111320.0 * np.cos(np.radians(lat)))
    return np.array([[lon + dlon * k / (n - 1), lat] for k in range(n)])


class Tiles:
    """A temporary imagery cache with the z17 tiles around the test point: grass everywhere, a road colour across the
    middle of them (a horizontal band 6 px = ~4.5 m tall through the tile row of the point)."""

    def __init__(self, road_rgb, field=(90, 130, 60), name="czechia", with_road=True):
        self.dir = tempfile.TemporaryDirectory()
        fx, fy = photosurface.tile_of(LAT, LON)
        self.tx, self.ty = int(fx), int(fy)
        self.py = int((fy - self.ty) * 256)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                arr = np.zeros((256, 256, 3), np.uint8)
                arr[:] = field
                if with_road and dy == 0:
                    arr[self.py - 3:self.py + 3, :] = road_rgb
                d = os.path.join(self.dir.name, name, "17", str(self.tx + dx))
                os.makedirs(d, exist_ok=True)
                Image.fromarray(arr).save(os.path.join(d, f"{self.ty + dy}.jpg"), quality=100, subsampling=0)

    def road(self, metres=60.0):
        """The road along the band's middle row."""
        fx, fy = photosurface.tile_of(LAT, LON)
        lat = LAT                       # (its row py: the point's own)
        return line_east(lat, LON - 0.0003, metres, 4)

    def close(self):
        self.dir.cleanup()


class ClassifyTest(unittest.TestCase):
    def st(self, **kw):
        return dict({"sat": 0.03, "exg": 0.0, "lum": 150.0, "n": 100, "mad": 5.0}, **kw)

    def test_grey_is_paved(self):
        self.assertEqual(photosurface.classify(self.st()), "paved")

    def test_not_paved(self):
        self.assertIsNone(photosurface.classify(self.st(sat=0.15)))        # warm: dirt / gravel
        self.assertIsNone(photosurface.classify(self.st(exg=0.08)))        # a grass lane
        self.assertIsNone(photosurface.classify(self.st(lum=60.0)))        # shade
        self.assertIsNone(photosurface.classify(self.st(lum=240.0)))       # glare / a bare field
        self.assertIsNone(photosurface.classify(self.st(n=5)))             # too little to say
        self.assertIsNone(photosurface.classify(None))

    def test_a_little_warmer_needs_green_verges(self):
        warm = self.st(sat=0.055, exg=0.0)
        self.assertNotEqual(photosurface.classify(warm), "paved")                         # (no verge: not enough)
        self.assertNotEqual(photosurface.classify(dict(warm, vexg=0.01)), "paved")        # (verges as grey as it)
        self.assertEqual(photosurface.classify(dict(warm, vexg=0.08)), "paved")           # (set against green)
        self.assertNotEqual(photosurface.classify(dict(warm, sat=0.07, vexg=0.08)), "paved")   # (too warm still)

    def test_pale_neutral_is_gravel(self):
        pale = self.st(sat=0.09, lum=170.0, vexg=0.10)                  # (grey-beige, not green, not asphalt-grey)
        self.assertEqual(photosurface.classify(pale), "gravel")
        self.assertIsNone(photosurface.classify(self.st(sat=0.09, lum=120.0)))      # (darker: dirt)
        self.assertIsNone(photosurface.classify(self.st(sat=0.15, lum=170.0)))      # (warmer: dirt)
        self.assertIsNone(photosurface.classify(self.st(sat=0.09, lum=170.0, exg=0.05)))   # (greenish, not grass-matched)

    def test_asphalt_and_grass_come_first(self):
        self.assertEqual(photosurface.classify(self.st(sat=0.04, lum=170.0)), "paved")
        self.assertEqual(photosurface.classify(self.st(sat=0.15, exg=0.1, vexg=0.1, lum=170.0)), "grass")

    def test_the_limit(self):
        self.assertEqual(photosurface.classify(self.st(sat=photosurface.PAVED_MAX_SAT)), "paved")
        self.assertNotEqual(photosurface.classify(self.st(sat=photosurface.PAVED_MAX_SAT + 0.01)), "paved")


class MeasureTest(unittest.TestCase):
    def measure(self, tiles, ring=None):
        return photosurface.measure(tiles.road() if ring is None else ring, root=tiles.dir.name)

    def test_grey_lane_over_green(self):
        t = Tiles((165, 163, 160))
        self.addCleanup(t.close)
        st, missing = self.measure(t)
        self.assertFalse(missing)
        self.assertEqual(photosurface.classify(st), "paved")
        self.assertGreater(st["vexg"], st["exg"] + 0.05)           # (the verges are green, the road is not)

    def test_brown_track_is_not(self):
        t = Tiles((150, 120, 85))
        self.addCleanup(t.close)
        st, _ = self.measure(t)
        self.assertIsNone(photosurface.classify(st))

    def test_green_lane_is_grass(self):
        t = Tiles((105, 118, 92), field=(100, 114, 88))                # (the field's own colour, a real meadow's dull green)
        self.addCleanup(t.close)
        st, _ = self.measure(t)
        self.assertEqual(photosurface.classify(st), "grass")

    def test_green_lane_in_a_brown_field_is_not_grass(self):
        t = Tiles((105, 118, 92), field=(120, 95, 70))                 # (set against bare soil: not a grass lane's blend)
        self.addCleanup(t.close)
        st, _ = self.measure(t)
        self.assertIsNone(photosurface.classify(st))

    def test_dark_road_is_not(self):
        t = Tiles((40, 40, 40))
        self.addCleanup(t.close)
        st, _ = self.measure(t)
        self.assertIsNone(photosurface.classify(st))

    def test_tile_not_on_disk_is_missing(self):
        with tempfile.TemporaryDirectory() as empty:
            st, missing = photosurface.measure(line_east(LAT, LON, 40.0), root=empty)
        self.assertIsNone(st)
        self.assertTrue(missing)                                     # (not known yet: the caller keeps nothing)

    def test_a_recorded_404_is_known(self):
        with tempfile.TemporaryDirectory() as d:
            fx, fy = photosurface.tile_of(LAT, LON)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    p = os.path.join(d, "czechia", "17", str(int(fx) + dx))
                    os.makedirs(p, exist_ok=True)
                    open(os.path.join(p, f"{int(fy) + dy}.404"), "w").close()
            st, missing = photosurface.measure(line_east(LAT, LON, 40.0), root=d)
        self.assertIsNone(st)
        self.assertFalse(missing)

    def test_outside_every_regional_box(self):
        with tempfile.TemporaryDirectory() as empty:
            st, missing = photosurface.measure(line_east(49.03376, 28.71176, 60.0), root=empty)   # Vinnytsia
        self.assertIsNone(st)
        self.assertFalse(missing)                                    # (nothing to wait for there)

    def test_too_short_to_judge(self):
        t = Tiles((165, 163, 160))
        self.addCleanup(t.close)
        st, _ = self.measure(t, line_east(LAT, LON - 0.0003, 6.0))
        self.assertIsNone(photosurface.classify(st))


class Feats:
    """A roads layer reduced to what the surface lookup reads."""
    def __init__(self, classes, osm):
        self.attrs = {"class": np.array(classes), "osm_surface": np.array(osm)}
        self.rules = {}
        self.ids = np.array([f"id{i}" for i in range(len(classes))])
        self.__dict__["_bnge_cell"] = None

    def __len__(self):
        return len(self.ids)


class HookTest(unittest.TestCase):
    def setUp(self):
        self.on, self.verdicts = photosurface.ON, photosurface.verdicts
        photosurface.ON = True

    def tearDown(self):
        photosurface.ON, photosurface.verdicts = self.on, self.verdicts

    def test_only_the_unmapped_road_takes_the_photo(self):
        photosurface.verdicts = lambda feats, surface_of, root=None: ({0: "paved", 1: "paved", 2: "grass", 3: "gravel"}, False)
        f = Feats(["track", "track", "track", "track"], ["", "gravel", "", ""])
        self.assertEqual(roads._surface_value(f, 3), "gravel")
        self.assertEqual(roads.surface_kind("track", roads._surface_value(f, 3)), "gravel")
        self.assertEqual(roads._surface_value(f, 2), "grass")           # (a grass lane: the ground layer's, no decal)
        self.assertEqual(roads.terrain_surface(types.SimpleNamespace(surf="grass")), "grass")
        self.assertEqual(roads._surface_value(f, 0), "asphalt")          # nothing mapped: the photo's
        self.assertEqual(roads._surface_value(f, 1), "gravel")           # mapped: never overruled
        self.assertEqual(roads.surface_kind("track", roads._surface_value(f, 0)), "paved")

    def test_off_is_the_map_alone(self):
        photosurface.ON = False
        photosurface.verdicts = lambda *a, **k: self.fail("the photo was read while off")
        f = Feats(["track"], [""])
        self.assertIsNone(roads._surface_value(f, 0))
        self.assertEqual(roads.surface_kind("track", None), "dirt")

    def test_missing_tile_is_a_fault_and_asked_again(self):
        calls = []

        def verdicts(feats, surface_of, root=None):
            calls.append(1)
            return {}, len(calls) == 1                                    # (missing the first time only)
        photosurface.verdicts = verdicts
        f = Feats(["track"], [""])
        before = revisit.faults()
        self.assertIsNone(roads._surface_value(f, 0))
        self.assertGreater(revisit.faults(), before)                      # (nothing built on it is kept)
        self.assertIsNone(roads._surface_value(f, 0))                     # (inside FACTS_SHORT_S: not asked again)
        self.assertEqual(len(calls), 1)
        f.__dict__["_photo_surface"] = (f.__dict__["_photo_surface"][0], 0.0)     # (expired)
        roads._surface_value(f, 0)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(f.__dict__["_photo_surface"][1])                # (complete now: remembered for good)

    def test_the_regional_share_counts_the_map_only(self):
        photosurface.verdicts = lambda *a, **k: self.fail("the regional share read the photo")
        f = Feats(["unclassified"], ["gravel"])
        old = roads._lengths
        roads._lengths = lambda feats: np.array([100.0])      # (the fake layer has no geometry)
        try:
            roads._regional_rows(f)
        finally:
            roads._lengths = old


class SpecsKeyTest(unittest.TestCase):
    def test_tag(self):
        self.assertTrue(photosurface.TAG)


if __name__ == "__main__":
    unittest.main()
