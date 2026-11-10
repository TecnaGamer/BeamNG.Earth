"""New York's measured rooftop water tanks (server/nyctanks.py, tools/build_nyc_tanks.py, tools/tankdetect.py): the
shard encoding, the manifest gate (nothing asked before it is published), a tank on the topmost roof under its point,
no procedural tank in a covered cell, the procedural ones outside, the builder's shards read back in place, and the
detector finding a cedar disc on a roof (not off the roofs, not along a tan strip)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "tools"))

import buildings as bld  # noqa: E402
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import netfetch  # noqa: E402
import nyctanks as nt  # noqa: E402
import build_nyc_tanks as builder  # noqa: E402
import tankdetect as td  # noqa: E402

LAT, LON = 40.7430, -73.9940            # Chelsea
KEY = mapcells.cell_of(LAT, LON)
CELL = geodesy.EnuFrame(*KEY.center(), 0.0)


def shard(tmp, key, xy_m, d_m, kind=None, h_m=None):
    n = len(d_m)
    arrs = nt.encode(np.round(np.asarray(xy_m, float).reshape(-1, 2) * 10), np.round(np.asarray(d_m) * 10),
                     kind if kind is not None else [0] * n, np.round(np.asarray(h_m if h_m is not None else [30] * n) * 10))
    d = os.path.join(tmp, str(key.z), str(key.x))
    os.makedirs(d, exist_ok=True)
    np.savez_compressed(os.path.join(d, f"{key.y}.npz"), **arrs)


def manifest(tmp, keys):
    with open(os.path.join(tmp, "manifest.json"), "w") as fh:
        json.dump({"cells": {str(k): {} for k in keys}}, fh)


def spec_at(ring, floors=12, seed="a"):
    sp = bld.plan(np.asarray(ring, float), {"class": "apartments", "num_floors": floors, "seed_id": seed}, 1, LAT, LON)
    sp.min_h = 0.0
    return sp


class ShardTest(unittest.TestCase):
    def test_round_trip(self):
        import io
        buf = io.BytesIO()
        np.savez_compressed(buf, **nt.encode([[12, -340], [5000, 7]], [36, 52], [0, 1], [301, 455]))
        got = nt.decode(buf.getvalue())
        np.testing.assert_allclose(got["xy"], [[1.2, -34.0], [500.0, 0.7]])
        np.testing.assert_allclose(got["d"], [3.6, 5.2])
        self.assertEqual(got["kind"].tolist(), [0, 1])
        np.testing.assert_allclose(got["h"], [30.1, 45.5])
        self.assertEqual(nt.decode(b""), {})
        self.assertEqual(nt.decode(b"not a zip"), {})
        buf = io.BytesIO()
        np.savez_compressed(buf, xy=np.zeros((3, 2), np.int32), d=np.zeros(2, np.uint8), kind=np.zeros(2, np.uint8),
                            h=np.zeros(2, np.uint16))
        self.assertEqual(nt.decode(buf.getvalue()), {}, "malformed")


class GateTest(unittest.TestCase):
    def test_nothing_asked_before_the_manifest(self):
        asked = []

        class F:
            def get(self, url, kind=None):
                asked.append(url)
                raise netfetch.NotFound(url)
        with tempfile.TemporaryDirectory() as tmp:
            src = nt.Source(tmp, fetcher=F(), base="https://example.invalid/nyctanks/v1")
            self.assertIs(src.cells(), False)
            self.assertEqual(asked, ["https://example.invalid/nyctanks/v1/manifest.json"])
            self.assertIs(src.cells(), False, "not asked again at once")
            self.assertEqual(len(asked), 1)
            self.assertEqual(os.listdir(tmp), [], "nothing kept")

    def test_local_folder_and_unlisted_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            shard(tmp, KEY, [(1.0, 2.0)], [3.5])
            manifest(tmp, [KEY])
            with mock.patch.dict(os.environ, {"BNE_NYC_TANKS_DIR": tmp}):
                nt._SOURCES.clear()
                x, y = 0.0, 0.0
                frame = geodesy.EnuFrame(LAT, LON, 0.0)
                got = nt.Fetch(frame, x - 50, y - 50, x + 50, y + 50, None).result()
                self.assertIsNotNone(got)
                self.assertEqual(len(got), 1)
                # the point is where the shard put it (cell frame -> anchor frame)
                la, lo, _ = CELL.enu_to_geodetic(np.array([1.0]), np.array([2.0]), 0.0)
                e, n, _ = frame.geodetic_to_enu(la, lo, 0.0)
                np.testing.assert_allclose(got.pts[0], [float(e[0]), float(n[0])], atol=1e-6)
                self.assertTrue(got.covered([LAT], [LON])[0])
                self.assertFalse(got.covered([40.6], [-73.9])[0])
                manifest(tmp, [])
                nt._SOURCES.clear()
                self.assertIsNone(nt.Fetch(frame, -50, -50, 50, 50, None).result(), "a cell not listed: procedural")
            nt._SOURCES.clear()

    def test_off_and_outside(self):
        frame = geodesy.EnuFrame(48.85, 2.35, 0.0)
        self.assertIsNone(nt.Fetch(frame, -50, -50, 50, 50, "/nonexistent").result(), "Paris")
        with mock.patch.object(nt, "ON", False):
            self.assertIsNone(nt.Fetch(geodesy.EnuFrame(LAT, LON, 0.0), -50, -50, 50, 50, "/x").result())


class Tanks:
    """A stand-in for nyctanks.Tanks."""

    def __init__(self, pts, d):
        self.pts, self.d = np.asarray(pts, float).reshape(-1, 2), np.asarray(d, float)

    def __len__(self):
        return len(self.d)


class RowsTest(unittest.TestCase):
    def setUp(self):
        base = np.array([[0, 0], [30, 0], [30, 20], [0, 20]], float)
        self.low = spec_at(base, 6, "low")
        self.top = spec_at(base[[0, 1, 2, 3]] * [0.5, 1.0], 12, "top")      # (the taller part over the west half)
        self.other = spec_at(base + [60, 0], 12, "other")
        self.low.min_h, self.top.min_h = 0.0, 0.0
        self.items = [(self.low, 10.0, 10.0), (self.top, 10.0, 10.0), (self.other, 10.0, 10.0)]

    def test_on_the_topmost_roof(self):
        tanks = Tanks([[100 + 5, 200 + 10]], [4.2])
        rows = bld.measured_tank_rows(self.items, 100.0, 200.0, tanks, np.array([True, True, True]))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r[0], "rooftank")
        self.assertAlmostEqual(r[1], 105.0, places=2)
        self.assertAlmostEqual(r[2], 210.0, places=2)
        zt = bld.base_z(self.top, 10.0, 10.0) + self.top.height
        zl = bld.base_z(self.low, 10.0, 10.0) + self.low.height
        self.assertAlmostEqual(r[3], max(zt, zl), places=1)
        self.assertAlmostEqual(r[6], bld.WATER_TANK_MODEL_H * 4.2 / bld.WATER_TANK_MODEL_D, places=1)

    def test_covered_roofs_without_a_point_have_none(self):
        for sp, _a, _b in self.items:
            sp.tank = True
        rows = bld.measured_tank_rows(self.items, 0.0, 0.0, Tanks(np.zeros((0, 2)), []), np.array([True] * 3))
        self.assertEqual(rows, [], "the photo answers: no tank there")
        rows = bld.measured_tank_rows(self.items, 0.0, 0.0, Tanks(np.zeros((0, 2)), []),
                                      np.array([True, True, False]))
        self.assertEqual(len(rows), 1, "outside the covered cells: the procedural tank")
        self.assertGreater(rows[0][1], 59.0)

    def test_a_point_on_no_roof(self):
        rows = bld.measured_tank_rows(self.items, 0.0, 0.0, Tanks([[45.0, 10.0]], [3.0]), np.array([True] * 3))
        self.assertEqual(rows, [])

    def test_tile_rows_without_data(self):
        for sp, _a, _b in self.items:
            sp.tank = True
        self.assertEqual(len(nt.tile_rows(None, None, self.items, 0.0, 0.0)), len(bld.tank_rows(self.items, 0, 0)))


class BuilderTest(unittest.TestCase):
    def test_pixels_round_trip(self):
        url, mbox, px = builder.chip_request(*builder.chip_of(LAT, LON))
        self.assertIn("orthos.its.ny.gov", url)
        self.assertLess(abs(px - (builder.CHIP_M + 2 * builder.MARGIN_M) / builder.MPP), 30)
        xy = builder.to_px(mbox, px, np.array([LON]), np.array([LAT]))
        la, lo = builder.from_px(mbox, px, xy[:, 0], xy[:, 1])
        self.assertAlmostEqual(float(la[0]), LAT, places=7)
        self.assertAlmostEqual(float(lo[0]), LON, places=7)

    def test_dedup(self):
        t = [{"lat": LAT, "lon": LON, "p": 0.9}, {"lat": LAT + 0.5 / 110540, "lon": LON, "p": 0.95},
             {"lat": LAT + 10 / 110540, "lon": LON, "p": 0.85}]
        got = builder.dedup(t)
        self.assertEqual(sorted(g["p"] for g in got), [0.85, 0.95])

    def test_build_reads_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = os.path.join(tmp, "root"), os.path.join(tmp, "out")
            os.makedirs(os.path.join(root, "det"))
            i, j = builder.chip_of(LAT, LON)
            chip = f"{i}_{j}"
            s, w, n, e = builder.chip_box(i, j)
            lat, lon = 0.5 * (s + n), 0.5 * (w + e)
            with open(os.path.join(root, "chips.json"), "w") as fh:
                json.dump({"min_h": 12.0, "chips": {chip: 3}}, fh)
            with open(os.path.join(root, "det", chip + ".json"), "w") as fh:
                json.dump({"tanks": [{"lat": lat, "lon": lon, "d": 3.8, "kind": 0, "p": 0.9, "h": 41.2}]}, fh)
            builder.build(root, out)
            m = json.load(open(os.path.join(out, "manifest.json")))
            key = mapcells.cell_of(lat, lon)
            self.assertIn(str(key), m["cells"])
            self.assertEqual(m["tanks"], 1)
            got = nt.decode(open(os.path.join(out, str(key.z), str(key.x), f"{key.y}.npz"), "rb").read())
            cf = geodesy.EnuFrame(*key.center(), 0.0)
            la, lo, _ = cf.enu_to_geodetic(got["xy"][:, 0], got["xy"][:, 1], 0.0)
            self.assertLess(abs(float(np.atleast_1d(la)[0]) - lat) * 110540, 0.1)
            self.assertLess(abs(float(np.atleast_1d(lo)[0]) - lon) * 84000, 0.1)
            self.assertAlmostEqual(float(got["d"][0]), 3.8)
            self.assertAlmostEqual(float(got["h"][0]), 41.2)


class DetectTest(unittest.TestCase):
    """A cedar disc (tan, in the dark square of its platform) on a grey roof is found; a tan strip (a deck) is not."""

    def picture(self, shape):
        rng = np.random.default_rng(1)
        img = np.full((300, 300, 3), (150, 150, 152), np.float32) + rng.normal(0, 3, (300, 300, 3))
        yy, xx = np.mgrid[0:300, 0:300]
        img[(abs(xx - 150) < 30) & (abs(yy - 150) < 30)] = (60, 62, 70)       # (the platform's shadow)
        if shape == "disc":
            m = np.hypot(xx - 150, yy - 150) <= 19
        else:
            img[(abs(xx - 150) < 88) & (abs(yy - 150) < 30)] = (60, 62, 70)
            m = (abs(xx - 150) <= 80) & (abs(yy - 150) <= 14)
        img[m] = (178, 152, 130)                                            # (cedar)
        img[m & (xx > 150)] *= 0.85                                         # (the cone's shaded half)
        return np.clip(img, 0, 255).astype(np.uint8)

    def test_disc_found_strip_not(self):
        roof = [([np.array([[10, 10], [290, 10], [290, 290], [10, 290]], float)], 30.0)]
        dets, v = td.detect(self.picture("disc"), roof, lean=(0.0, 0.0))
        self.assertEqual(len(dets), 1, dets)
        self.assertLess(np.hypot(dets[0]["x"] - 150, dets[0]["y"] - 150), 5)
        self.assertEqual(dets[0]["kind"], "tan")
        self.assertEqual(td.detect(self.picture("strip"), roof, lean=(0.0, 0.0))[0], [], "a tan strip")
        self.assertEqual(td.detect(self.picture("disc"), [], lean=(0.0, 0.0))[0], [], "not on a roof")


if __name__ == "__main__":
    unittest.main()
