"""Road comparison selection: geographic clipping, level ranges and stable cameras."""
import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import benchmark
import road_audit as audit
from test_roads import features, rules


def layer(lines, flags=(), oneway=()):
    f = features([[(0, 0), (100, 0)]] * len(lines), ["motorway"] * len(lines), flags=flags)
    f.lonlat = np.asarray([p for line in lines for p in line], np.float64)
    f.attrs["src_record"] = np.array(["r42@3"] * len(lines))
    f.rules["oneway"] = rules(oneway, "float")
    return f


class RoadAuditTest(unittest.TestCase):
    def test_midpoint_uses_distance_not_vertex_count(self):
        lat, lon, hd = audit.midpoint([[0, 0], [0.01, 0], [0.1, 0]])
        self.assertAlmostEqual(lon, 0.05)
        self.assertEqual((lat, hd), (0, 90))
        self.assertIsNone(audit.midpoint([[1, 2], [1, 2]]))

    def test_mixed_level_camera_stays_on_ground_and_respects_oneway(self):
        f = layer([[(12, 49), (12.01, 49)]], flags=[(0, "is_bridge", .4, .6)], oneway=[(0, -1)])
        rows, geo = audit.inspect_features(f, (48.9, 11.9, 49.1, 12.1), "motorway", "r42")
        self.assertTrue(rows[0]["mixed_ground_and_structure"])
        self.assertEqual(rows[0]["ground_parts"], 2)
        self.assertEqual(rows[0]["camera"]["heading"], 270)
        self.assertLess(rows[0]["camera"]["lon"], 12.004)
        self.assertEqual([g["properties"]["kind"] for g in geo], ["ground", "ground", "bridge"])

    def test_bbox_line_intersection_and_source_version_filter(self):
        f = layer([[(0, 0), (2, 2)]])
        self.assertEqual(audit.inspect_features(f, (1.5, 0, 2, .5))[0], [], "bbox overlap alone is insufficient")
        self.assertEqual(audit.inspect_features(f, (.5, .5, 1.5, 1.5), source_record="r43")[0], [])
        self.assertEqual(len(audit.inspect_features(f, (.5, .5, 1.5, 1.5), source_record="r42")[0]), 1)

    def test_bridge_only_never_gets_a_ground_height_camera(self):
        f = layer([[(12, 49), (12.01, 49)]], flags=[(0, "is_bridge")])
        rows, _ = audit.inspect_features(f, (48.9, 11.9, 49.1, 12.1))
        self.assertNotIn("camera", rows[0])

    def test_no_cross_cell_duplicate_features_and_missing_coverage_is_explicit(self):
        f = layer([[(12, 49), (12.01, 49)]])
        md = Mock()
        md.fetcher.offline = True
        md.has.side_effect = [True, True, False, False]
        cd = Mock(layers={"roads": f}, provenance=[])
        cd.digest.return_value = "digest"
        md.cell.return_value = cd
        md.versions.return_value = {"overture": "fixed"}
        # Bbox spanning exactly four adjacent cells.
        key = audit.mapcells.cell_of(49, 12)
        w, s, e, n = key.bounds()
        report, geo = audit.audit(md, (s, w, n + (n - s) / 2, e + (e - w) / 2))
        self.assertEqual(report["summary"]["roads"], 1)
        self.assertEqual(len(geo["features"]), 1)
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(len(report["coverage"]["missing"]), 2)
        self.assertEqual(md.cell.call_count, 2)

    def test_benchmark_is_valid_deterministic_and_filters_travel_direction(self):
        f = layer([[(12, 49), (12.01, 49)], [(12, 49.01), (12.01, 49.01)]], oneway=[(1, -1)])
        rows, _ = audit.inspect_features(f, (48.9, 11.9, 49.1, 12.1))
        s = audit.benchmark_set(rows, 12, 90)
        benchmark.validate_set(s)
        self.assertEqual(len(s["places"]), 1)
        self.assertTrue(s["defaults"]["views"][0]["under"], "ground cameras stay below crossing overpasses")
        self.assertEqual(s, audit.benchmark_set(rows[::-1], 12, 90))
        self.assertEqual(audit.benchmark_set(rows, 12, 0)["places"], [])

    def test_offline_corrupt_cache_never_rebuilds_or_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = audit.AuditMapData(tmp, audit.netfetch.Fetcher(offline=True))
            key = audit.mapcells.cell_of(49, 12)
            path = Path(md.path(key, "roads"))
            path.parent.mkdir(parents=True)
            path.write_bytes(b"corrupt npz")
            md._build = Mock(side_effect=AssertionError("must not rebuild"))
            md._store = Mock(side_effect=AssertionError("must not write"))
            try:
                w, s, e, n = key.bounds()
                with self.assertLogs("earthd.mapdata", level="WARNING"):
                    report, _ = audit.audit(md, (s, w, n, e))
                self.assertEqual(report["coverage"]["missing"], [str(key)])
                self.assertIn(str(key), report["coverage"]["unreadable"])
                self.assertEqual(path.read_bytes(), b"corrupt npz")
                md._build.assert_not_called()
                md._store.assert_not_called()
            finally:
                md.close()

    def test_bad_bbox_rejected(self):
        for value in ("nan,1,2,3", "2,1,1,2", "-90,0,2,3", "1,2,3"):
            with self.assertRaises(argparse.ArgumentTypeError):
                audit.bbox_arg(value)


if __name__ == "__main__":
    unittest.main()
