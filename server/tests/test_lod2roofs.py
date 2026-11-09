"""lod2roofs: a measured record fills what the map leaves out of the footprint it stands in; the map's own values stay,
nothing is asked for outside Germany, and a missing shard means the predicted roofs."""
import io
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import buildings as bld  # noqa: E402
import geodesy  # noqa: E402
import lod2roofs as lr  # noqa: E402
import netfetch  # noqa: E402

LAT, LON = 50.9, 9.1


def shard(rows):
    """npz bytes of [(lat, lon, code, height m, roof m)]."""
    a = np.array(rows, np.float64)
    buf = io.BytesIO()
    np.savez_compressed(buf, lat=np.round(a[:, 0] * 1e6).astype(np.int32), lon=np.round(a[:, 1] * 1e6).astype(np.int32),
                        code=a[:, 2].astype(np.int16), height=np.round(a[:, 3] * 10).astype(np.uint16),
                        roof=np.round(a[:, 4] * 10).astype(np.uint16))
    return buf.getvalue()


class Body:
    def __init__(self, body):
        self.body = body


class Fetcher:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.urls = body, error, []

    def get(self, url, kind="demand", **_):
        self.urls.append(url)
        if self.error:
            raise self.error
        return Body(self.body)


def ring(cx, cy, r=6.0):
    return np.array([cx - r, cx + r, cx + r, cx - r]), np.array([cy - r, cy - r, cy + r, cy + r])


class Base(unittest.TestCase):
    def records(self, rows):
        frame = geodesy.EnuFrame(LAT, LON)
        src = lr.Source(None, Fetcher(shard(rows)))
        return src.tile(frame, -200, -200, 200, 200), src


class FillTest(Base):
    def test_a_measured_roof_fills_an_unknown_building(self):
        rec, _ = self.records([(LAT, LON, 3300, 9.4, 3.2)])
        at = rec.fill({"roof_shape": None, "height": None}, *ring(0, 0))
        self.assertEqual(at["roof_shape"], "half-hipped")
        self.assertAlmostEqual(at["height"], 9.4, 3)
        self.assertAlmostEqual(at["roof_height"], 3.2, 3)

    def test_the_maps_own_values_stay(self):
        rec, _ = self.records([(LAT, LON, 3100, 9.4, 3.2)])
        at = rec.fill({"roof_shape": "hipped", "height": 12.0, "roof_height": 2.0}, *ring(0, 0))
        self.assertEqual((at["roof_shape"], at["height"], at["roof_height"]), ("hipped", 12.0, 2.0))

    def test_a_mixed_roof_gives_its_height_but_no_shape(self):
        rec, _ = self.records([(LAT, LON, 5000, 8.0, 3.0)])
        at = rec.fill({"roof_shape": None, "height": None}, *ring(0, 0))
        self.assertIsNone(at["roof_shape"])
        self.assertAlmostEqual(at["height"], 8.0, 3)

    def test_a_record_beside_the_footprint_does_not_join(self):
        rec, _ = self.records([(LAT, LON, 3100, 9.4, 3.2)])
        at = rec.fill({"roof_shape": None, "height": None}, *ring(40, 0))
        self.assertIsNone(at["roof_shape"])

    def test_the_tallest_part_says_what_the_building_is(self):
        d = 2e-5
        rec, _ = self.records([(LAT, LON, 1000, 4.0, 0.0), (LAT + d, LON, 3200, 11.0, 4.0)])
        at = rec.fill({"roof_shape": None, "height": None}, *ring(0, 0))
        self.assertEqual(at["roof_shape"], "hipped")
        self.assertAlmostEqual(at["height"], 11.0, 3)

    def test_plan_draws_the_filled_shape(self):
        rec, _ = self.records([(LAT, LON, 3400, 9.0, 3.5)])
        e, n = ring(0, 0)
        at = rec.fill({"class": "house", "roof_shape": None, "height": None, "seed_id": "a"}, e, n)
        spec = bld.plan(np.stack([e, n], 1), at)
        self.assertEqual(spec.roof, "mansard")


class ShardTest(Base):
    def test_every_code_in_the_table_is_a_shape_plan_knows(self):
        known = {"gabled", "hipped", "flat", "pyramidal", "half-hipped", "mansard", "skillion", "round"} | set(bld.DOME_ROOFS)
        self.assertLessEqual(set(lr.SHAPES.values()), known)

    def test_nothing_is_asked_for_outside_germany(self):
        self.assertEqual(lr.tiles_over(40.0, -3.0, 40.2, -2.8), [])
        f = Fetcher(shard([(LAT, LON, 3100, 9.0, 3.0)]))
        self.assertIsNone(lr.Source(None, f).tile(geodesy.EnuFrame(40.0, -3.0), -200, -200, 200, 200))
        self.assertEqual(f.urls, [])

    def test_a_missing_shard_means_the_predicted_roofs_and_is_remembered(self):
        f = Fetcher(error=netfetch.NotFound("none"))
        src = lr.Source(None, f)
        frame = geodesy.EnuFrame(LAT, LON)
        self.assertIsNone(src.tile(frame, -200, -200, 200, 200))
        asked = len(f.urls)
        self.assertGreater(asked, 0)
        self.assertIsNone(src.tile(frame, -200, -200, 200, 200))
        self.assertEqual(len(f.urls), asked)

    def test_a_missing_shard_kept_on_disk_is_asked_again_after_a_week(self):
        """A 404 is kept as an empty file (no asking again every session), but not for good: before the shards were
        published every German tile answered 404, and those players would never have got the roofs."""
        import os, tempfile, time
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(error=netfetch.NotFound("none"))
            frame = geodesy.EnuFrame(LAT, LON)
            self.assertIsNone(lr.Source(d, f).tile(frame, -200, -200, 200, 200))
            kept = [os.path.join(dp, n) for dp, _, ns in os.walk(d) for n in ns if n.endswith(".npz")]
            self.assertTrue(kept and all(os.path.getsize(p) == 0 for p in kept))
            asked = len(f.urls)
            lr.Source(d, f).tile(frame, -200, -200, 200, 200)
            self.assertEqual(len(f.urls), asked, "a fresh miss on disk: not asked again")
            old = time.time() - lr.MISS_TTL_S - 60
            for p in kept:
                os.utime(p, (old, old))
            lr.Source(d, f).tile(frame, -200, -200, 200, 200)
            self.assertGreater(len(f.urls), asked, "a week-old miss: asked again")

    def test_offline_asks_again_only_after_a_while(self):
        f = Fetcher(error=OSError("offline"))
        src = lr.Source(None, f)
        frame = geodesy.EnuFrame(LAT, LON)
        self.assertIsNone(src.tile(frame, -200, -200, 200, 200))
        asked = len(f.urls)
        self.assertIsNone(src.tile(frame, -200, -200, 200, 200))
        self.assertEqual(len(f.urls), asked)

    def test_a_shard_on_disk_is_not_fetched_again(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(shard([(LAT, LON, 3100, 9.0, 3.0)]))
            frame = geodesy.EnuFrame(LAT, LON)
            self.assertIsNotNone(lr.Source(d, f).tile(frame, -200, -200, 200, 200))
            asked = len(f.urls)
            self.assertIsNotNone(lr.Source(d, f).tile(frame, -200, -200, 200, 200))
            self.assertEqual(len(f.urls), asked)

    def test_a_broken_shard_reads_as_none(self):
        self.assertEqual(lr.decode(b"not a shard"), {})


if __name__ == "__main__":
    unittest.main()
