"""The pinned Overture release's STAC index ships with the app (overture.BUNDLED, tools/build_stac_index.py): the files
of a box come without a request (stac.overturemaps.org was down for an hour on 2026-10-26, GitHub #357)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import overture  # noqa: E402


class NoNetwork:
    def get(self, url, kind="demand", **kw):
        raise AssertionError(f"no request expected: {url}")


class BundleTest(unittest.TestCase):
    def test_every_layer_without_a_request(self):
        self.assertTrue(os.path.exists(overture.BUNDLED.format(release=overture.RELEASE)),
                        "run tools/build_stac_index.py after a release bump")
        with tempfile.TemporaryDirectory() as raw:
            idx = overture.StacIndex(overture.RELEASE, raw, NoNetwork())
            for v in overture.LAYERS.values():
                got = idx.files(v["theme"], v["type"], (-76.7, 39.2, -76.5, 39.4))    # Baltimore
                self.assertTrue(got, v["type"])
                for name, urls, size in got:
                    self.assertTrue(name.endswith(".parquet") and urls[0].endswith(name) and size > 0)

    def test_another_release_falls_back_to_the_network(self):
        with tempfile.TemporaryDirectory() as raw:
            idx = overture.StacIndex("1999-01-01.0", raw, NoNetwork())
            with self.assertRaises(AssertionError):
                idx.files("buildings", "building", (-76.7, 39.2, -76.5, 39.4))


if __name__ == "__main__":
    unittest.main()
