"""The highway sign shards: their reader (railtags.SignTags) with no data, an unpublished dataset and a tiny shard set
written by tools/build_sign_tags.py's own functions (no pyosmium needed: the planet passes are left out)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))

import netfetch  # noqa: E402
import railtags  # noqa: E402
import signshards  # noqa: E402
import build_sign_tags as builder  # noqa: E402

LAT, LON = 39.30, -76.55


def ll(x, y):
    """lon, lat of a point x m east, y m north of (LAT, LON)."""
    return LON + x / (111320.0 * np.cos(np.radians(LAT))), LAT + y / 110540.0


def tiny_network():
    """A motorway (way 10, nodes 1-3) and an exit ramp (way 20, nodes 3-5) with destination tags, junction node 3."""
    pts = {1: ll(-2000, 0), 2: ll(-1000, 0), 3: ll(0, 0), 4: ll(80, -40), 5: ll(160, -120)}
    main = [(10, np.array([1, 2, 3], np.int64), {"highway": "motorway", "oneway": "yes", "ref": "I 95"})]
    links = [(20, np.array([3, 4, 5], np.int64), {"highway": "motorway_link", "oneway": "yes",
                                                   "destination": "Essex", "destination:ref": "MD 702"})]
    junctions = {3: {"highway": "motorway_junction", "ref": "64"}}
    ids = np.array(sorted(pts), np.int64)
    places = builder.Places(ids, np.array([pts[i][0] for i in ids]), np.array([pts[i][1] for i in ids]))
    return main, links, junctions, places


class NotPublished:
    """A fetcher whose every request is a 404 (the data repo has no signs/ yet)."""

    def __init__(self):
        self.calls = 0

    def get(self, url, kind="demand"):
        self.calls += 1
        raise netfetch.NotFound(url)


class SignTagsTest(unittest.TestCase):
    def test_no_fetcher_and_no_local_set_gives_nothing(self):
        tags = railtags.SignTags(None, None, local=None)
        tags.local = None
        self.assertEqual(tags.query(LON - .1, LAT - .1, LON + .1, LAT + .1), [])
        self.assertIsNone(tags.version())

    def test_unpublished_dataset_gives_nothing_and_asks_once(self):
        with tempfile.TemporaryDirectory() as cache:
            fetcher = NotPublished()
            tags = railtags.SignTags(cache, fetcher)
            tags.local = None
            for _ in range(5):
                self.assertEqual(tags.query(LON - .1, LAT - .1, LON + .1, LAT + .1), [])
            self.assertEqual(fetcher.calls, 1, "the manifest is asked again only after RETRY_S")
            self.assertIsNone(tags.known())

    def test_local_folder_without_a_manifest_gives_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tags = railtags.SignTags(None, None, local=tmp)
            self.assertEqual(tags.query(LON - .1, LAT - .1, LON + .1, LAT + .1), [])

    def test_paths_round_trip_to_a_micro_degree(self):
        path = [list(ll(-1000, 3)), list(ll(0, 0)), list(ll(55.5, -12.25))]
        flat = signshards.encode_path(path, *ll(0, 0))
        self.assertTrue(all(isinstance(v, int) for v in flat))
        np.testing.assert_allclose(signshards.decode_path(flat, *ll(0, 0)), path, atol=6e-7)

    def test_built_shards_read_back_as_the_sign_modules_records(self):
        main, links, junctions, places = tiny_network()
        found = builder.exits(main, links, junctions, {}, places)
        self.assertEqual(len(found), 1)
        records = builder.exit_records(found)
        with tempfile.TemporaryDirectory() as tmp:
            builder.write(tmp, records, {"exits": 1, "through": 0, "miles": 0})
            self.assertTrue(os.path.exists(os.path.join(tmp, "manifest.json")))
            tags = railtags.SignTags(None, None, local=tmp)
            got = tags.query(LON - .05, LAT - .05, LON + .05, LAT + .05)
            self.assertEqual(len(got), 1)
            exit_ = got[0]["exit"]
            self.assertEqual(exit_["label"], {"ref": "64", "names": ["Essex"], "routes": ["MD 702"]})
            self.assertEqual(exit_["main_ids"], [10])
            self.assertEqual(exit_["ramp_id"], 20)
            want = found[0]["ways"][0]
            np.testing.assert_allclose(exit_["approach"][0], want["approach"][0], atol=1e-6)
            np.testing.assert_allclose(exit_["approach"][-1], want["approach"][-1], atol=1e-6)
            self.assertEqual(exit_["main_tags"], {"highway": "motorway", "ref": "I 95"})
            self.assertEqual(tags.query(LON + 1, LAT + 1, LON + 1.1, LAT + 1.1), [])
            # the same input gives the same shard bytes
            again = os.path.join(tmp, "again")
            builder.write(again, records, {"exits": 1, "through": 0, "miles": 0})
            x, y = signshards.tile(LAT, LON)
            with open(os.path.join(tmp, str(x), f"{y}.json.gz"), "rb") as a, \
                    open(os.path.join(again, str(x), f"{y}.json.gz"), "rb") as b:
                self.assertEqual(a.read(), b.read())

    def test_mile_references_from_mapped_milestones(self):
        pts = {i: ll(0, i * 1609.344) for i in range(6)}
        ids = np.array(sorted(pts), np.int64)
        places = builder.Places(ids, np.array([pts[i][0] for i in ids]), np.array([pts[i][1] for i in ids]))
        main = [(30, ids.copy(), {"highway": "motorway", "oneway": "yes", "ref": "I 95"})]
        milestones = {100: (pts[1][0], pts[1][1], {"highway": "milestone", "distance": "21 mi"}),
                      101: (pts[4][0], pts[4][1], {"highway": "milestone", "distance": "24 mi"})}
        rows = builder.miles(main, milestones, [(30, 95, "N", "")], places)
        miles = sorted(r["d"]["mile"] for r in rows)
        self.assertEqual(miles, [21, 22, 23, 24])
        self.assertEqual({r["k"] for r in rows}, {"mile"})


if __name__ == "__main__":
    unittest.main()
