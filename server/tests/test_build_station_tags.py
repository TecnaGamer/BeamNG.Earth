"""tools/build_station_tags.py, the weekly station shards (Bauer33333 on #716, 2026-11-04: "Almost 3/4 of
`amenity=charging_station` on OSM got a `capacity=*` tag, same for `man_made=charge_point`"): every fuel and charging
station with its capacity, and every charge point, from a small .osm file. Skipped without osmium (the builder's own
dependency: the data repo's job installs it)."""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools"))

try:
    import osmium  # noqa: F401
except ImportError:             # (no osmium here: the builder's own dependency)
    osmium = None
bst = None
if osmium is not None:
    import build_station_tags as bst

OSM = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" lat="34.849" lon="-117.085" version="1"><tag k="amenity" v="fuel"/><tag k="name" v="Arco"/></node>
  <node id="2" lat="34.8491" lon="-117.0854" version="1">
    <tag k="amenity" v="charging_station"/><tag k="operator" v="Tesla, Inc."/><tag k="capacity" v="4"/></node>
  <node id="3" lat="52.5190" lon="13.4140" version="1"><tag k="amenity" v="charging_station"/><tag k="capacity" v="yes"/></node>
  <node id="4" lat="52.5191" lon="13.4141" version="1"><tag k="man_made" v="charge_point"/></node>
  <node id="5" lat="52.5192" lon="13.4142" version="1"><tag k="man_made" v="charge_point"/></node>
  <node id="6" lat="52.5193" lon="13.4143" version="1"><tag k="shop" v="bakery"/></node>
  <node id="10" lat="48.0000" lon="11.0000" version="1"/>
  <node id="11" lat="48.0000" lon="11.0010" version="1"/>
  <node id="12" lat="48.0010" lon="11.0010" version="1"/>
  <node id="13" lat="48.0010" lon="11.0000" version="1"/>
  <way id="20" version="1">
    <nd ref="10"/><nd ref="11"/><nd ref="12"/><nd ref="13"/><nd ref="10"/>
    <tag k="amenity" v="charging_station"/><tag k="brand" v="Ionity"/><tag k="capacity" v="2;4"/></way>
</osm>
"""


@unittest.skipIf(osmium is None, "osmium not installed")
class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bnge-bst-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "in.osm")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write(OSM)

    def build(self, name="out"):
        out = os.path.join(self.tmp, name)
        bst.build(self.src, out)
        return out

    def shards(self, out):
        got = {}
        for x in os.listdir(out):
            if x == "manifest.json":
                continue
            for f in os.listdir(os.path.join(out, x)):
                with gzip.open(os.path.join(out, x, f), "rt", encoding="utf-8") as fh:
                    got[(int(x), int(f.split(".")[0]))] = json.load(fh)
        return got

    def test_capacity_of(self):
        self.assertEqual(bst.capacity_of("4"), 4)
        self.assertEqual(bst.capacity_of("2;4"), 2)
        self.assertEqual(bst.capacity_of("8 (4 dual)"), 8)
        self.assertIsNone(bst.capacity_of("yes"))
        self.assertIsNone(bst.capacity_of(""))
        self.assertIsNone(bst.capacity_of(None))

    def test_stations_and_points(self):
        out = self.build()
        recs = [r for s in self.shards(out).values() for r in s["s"]]
        by = {r[0]: r for r in recs}
        self.assertEqual(sorted(by), ["n1", "n2", "n3", "w20"], "fuel and charging stations only")
        self.assertEqual(by["n1"][1:3], ["fuel", -117.085])
        self.assertEqual((by["n1"][4], by["n1"][6]), ("Arco", None))
        self.assertEqual((by["n2"][1], by["n2"][5], by["n2"][6]), ("ev", "Tesla, Inc.", 4), "operator as the brand")
        self.assertIsNone(by["n3"][6], "capacity=yes: no number")
        w = by["w20"]
        self.assertEqual((w[1], w[5], w[6]), ("ev", "Ionity", 2))
        self.assertAlmostEqual(w[2], 11.0005, places=4)      # (its nodes' mean: the closing node counted once)
        self.assertAlmostEqual(w[3], 48.0005, places=4)
        pts = [p for s in self.shards(out).values() for p in s["p"]]
        self.assertEqual(sorted(pts), [[13.4141, 52.5191], [13.4142, 52.5192]])

    def test_shard_of_point(self):
        out = self.build()
        for (x, y), s in self.shards(out).items():
            for r in s["s"]:
                self.assertEqual(bst.shard_of(r[2], r[3]), (x, y))

    def test_manifest_and_same_bytes(self):
        a, b = self.build("a"), self.build("b")
        with open(os.path.join(a, "manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual((m["format"], m["zoom"], m["stations"], m["points"]), (bst.FORMAT, 7, 4, 2))
        self.assertIn("ODbL", m["license"])
        for x in os.listdir(a):
            if x == "manifest.json":
                continue
            for f in os.listdir(os.path.join(a, x)):
                with open(os.path.join(a, x, f), "rb") as fa, open(os.path.join(b, x, f), "rb") as fb:
                    self.assertEqual(fa.read(), fb.read(), "the same planet gives the same bytes")


if __name__ == "__main__":
    unittest.main()
