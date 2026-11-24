"""cip.py / earthfast cip.rs / tools/build_cip.py: architecture v2's compact per-cell inputs. The codec gives every
array back bit for bit (dtype, shape, bytes, order: unicode with inner NULs and UUID columns, coordinates off the 1e-7
grid and on the cell's edges, float32-exact doubles, offsets, bools, 0-d records, empty arrays) and blobs as they are;
cells are deterministic bytes; a build's archive is read from a folder through the BNWT container; a build made by
other code is refused; MapData takes a layer's cell file from its CIP cell (the same arrays the sources gave) and falls
back to the sources for a cell or a section the build lacks; a weekly shard's cell slice joins exactly as the shard."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cip  # noqa: E402
import fast  # noqa: E402
import mapcells  # noqa: E402
import mapgeom as mg  # noqa: E402
import railtags  # noqa: E402

HAVE = getattr(fast._ef, "VERSION", 0) >= cip.NATIVE
KEY = mapcells.CellKey(14, 4705, 6244)


def sample_arrays(key=KEY):
    lon0, lat0, lon1, lat1 = key.bounds()
    ll = np.array([[lon0, lat0], [-76.5934567, 39.2834567], [1.0 / 3.0, lat1], [lon1, 39.28]], np.float64)
    return {
        "ids": mg.text_column(["0a1b2c3d-0000-4000-8000-00000000000a", "", "x\x00y", "ünï"]),
        "kind": np.array([1, 2, 3, 2], np.uint8),
        "part_off": np.arange(5, dtype=np.int64),
        "lonlat": ll,
        "own": np.array([True, False, True, True]),
        "attr.cx": np.array([0.1, np.nan, -0.0, 1e300]),
        "attr.width": np.array([3.5, 7.0, 0.25, 2.0]),
        "attr.f4": np.array([0.1, 2.0, np.nan, 1.0], np.float32),
        "attr.neg": np.array([-5, 7, -1, 2 ** 40], np.int64),
        "rule.connectors.value": mg.text_column(["0a1b2c3d-0000-4000-8000-00000000000a"] * 3 + [""]),
        "grid": np.arange(12, dtype=np.uint8).reshape(3, 4),
        "empty": np.zeros(0, np.int32),
        "provenance": np.array(json.dumps({"a": 1})),
    }


@unittest.skipUnless(HAVE, "needs earthfast with cip")
class Codec(unittest.TestCase):
    def test_round_trip_bit_identical(self):
        a = sample_arrays()
        cell = cip.encode_cell(KEY, {"m:roads.9": a, "z:lidar.gaps": b"[]", "f:osmfuel": b'[{"id": 1}]'})
        back = cip.decode_cell(KEY, cell)
        self.assertEqual(set(back), {"m:roads.9", "z:lidar.gaps", "f:osmfuel"})
        self.assertEqual(list(back["m:roads.9"]), list(a))
        for k, v in a.items():
            w = back["m:roads.9"][k]
            self.assertEqual((v.dtype, v.shape), (w.dtype, w.shape), k)
            self.assertEqual(v.tobytes(), w.tobytes(), k)
        self.assertEqual(back["z:lidar.gaps"], b"[]")
        only = cip.decode_cell(KEY, cell, ["f:osmfuel"])
        self.assertEqual(list(only), ["f:osmfuel"])

    def test_context_codings(self):
        """Connector references coded against the cell's connectors (which come first), bbox centres made again from
        the geometry: both give the arrays back exactly, also when the roads alone are decoded."""
        lon0, lat0, lon1, lat1 = KEY.bounds()
        ids = [f"{i:08x}-0000-4000-8000-{i:012x}" for i in range(1, 6)]
        conn = {"ids": mg.text_column(ids), "attr.cx": np.array([0.5] * 5)}
        f = mg.build("roads", mg.text_column(ids[:3]), [
            (mg.LINE, [[np.array([[lon0, lat0], [-76.59, 39.284]])]]),
            (mg.LINE, [[np.array([[-76.591, 39.2841], [-76.5912, 39.2849], [-76.59, 39.2843]])]]),
            (mg.POINT, [[np.array([[-76.5901, 39.28456]])]])])
        roads = f.to_arrays("")
        ll, co = roads["lonlat"], roads["coord_off"]
        cx = np.array([(ll[co[i]:co[i + 1], 0].min() + ll[co[i]:co[i + 1], 0].max()) / 2 for i in range(3)])
        cx[1] = 1.25                                        # (an exception)
        roads["attr.cx"] = cx
        roads["rule.connectors.value"] = mg.text_column([ids[0], ids[4], "", "ffffffff-0000-4000-8000-0000000000ff"])
        cell = cip.encode_cell(KEY, {"m:roads.9": roads, "m:connectors": conn})
        for names in (None, ["m:roads.9"]):
            back = cip.decode_cell(KEY, cell, names)["m:roads.9"]
            for k, v in roads.items():
                self.assertEqual(np.asarray(v).tobytes(), back[k].tobytes(), k)
                self.assertEqual(np.asarray(v).dtype, back[k].dtype, k)

    def test_deterministic_and_cell_free(self):
        a = sample_arrays()
        b1 = cip.encode_cell(KEY, {"m:x": a})
        self.assertEqual(b1, cip.encode_cell(KEY, {"m:x": dict(a)}))
        # (a section made of its cell's edges only: the same bytes in another cell)
        other = mapcells.CellKey(14, 4706, 6244)

        def edges(k):
            w, s, e, n = k.bounds()
            return {"lonlat": np.array([[e, s], [e, n], [w, n], [w, s], [e, s]])}
        self.assertEqual(cip.encode_cell(KEY, {"m:water": edges(KEY)}), cip.encode_cell(other, {"m:water": edges(other)}))
        back = cip.decode_cell(other, cip.encode_cell(KEY, {"m:water": edges(KEY)}))
        self.assertEqual(back["m:water"]["lonlat"].tobytes(), edges(other)["lonlat"].tobytes())

    def test_damaged_and_order(self):
        cell = cip.encode_cell(KEY, {"m:places": {"a": np.zeros(3)}, "m:roads.9": {"a": np.ones(2)}})
        self.assertEqual([s[0] for s in cip.cell_sections(cell)], ["m:roads.9", "m:places"])   # (cip.ORDER)
        with self.assertRaises(ValueError):
            cip.decode_cell(KEY, cell[:-5])
        with self.assertRaises(ValueError):
            cip.decode_cell(KEY, b"BNWT" + cell[4:])


def shard():
    ways = {"100": {"lanes": "2", "turn:lanes": "left|through"}, "101": {"lanes": "3"}, "205": {"lanes": "2"},
            "999": {"lanes": "4"}}
    return {"ways": railtags.Ways(ways),
            "nodes": [[1, -76.590, 39.285, {"railway": "level_crossing"}], [2, 10.0, 10.0, {}],
                      [3, -76.5869, 39.2860, {"x": 1}]],
            "rules": railtags.Rules({"priority_road": {"designated": [100, 1, 104], "yes_unposted": [999]},
                                     "zone:maxspeed": {"DE:30": [205]}})}


@unittest.skipUnless(HAVE, "needs earthfast with cip")
class Slices(unittest.TestCase):
    def test_slice_reads_back_as_the_shard_for_its_ids(self):
        sh = shard()
        ids = ["100", "101", "205", "7"]
        sl = json.loads(json.dumps(railtags.cell_slice(sh, KEY, ids)))
        got = railtags.shard_of_slice(sl)
        self.assertIsInstance(got["ways"], railtags.Ways)
        for i in ids:
            self.assertEqual(got["ways"].get(i), sh["ways"].get(i))
        self.assertIsNone(got["ways"].get("999"))
        for c in railtags.RULE_COLUMNS:
            self.assertEqual(got["rules"].get_many(c, ids), sh["rules"].get_many(c, ids))
        lon0, lat0, lon1, lat1 = KEY.bounds()
        want = [nd for nd in sh["nodes"] if lon0 - railtags.PAD <= nd[1] <= lon1 + railtags.PAD
                and lat0 - railtags.PAD <= nd[2] <= lat1 + railtags.PAD]
        self.assertEqual(got["nodes"], want)


@unittest.skipUnless(HAVE, "needs earthfast with cip")
class Archive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_cip_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _build(self, cells, inputs=None):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "build_bnwt", str(Path(__file__).resolve().parents[2] / "tools" / "build_bnwt.py"))
        bb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bb)
        release = "2026-10-21.0"
        inputs = inputs or {"code": cip.code_inputs(release), "data": {"rails": "r1", "lanes": "l1"}}
        build = cip.build_id(inputs)
        out = os.path.join(self.tmp, "out", f"cip{cip.FORMAT}", release, build)
        os.makedirs(out)
        meta = {"format": "cip", "version": cip.FORMAT, "release": release, "build": build}
        keys = sorted(cells, key=lambda k: fast._ef.pmt_tile_id(14, k.x, k.y))
        rects = [(k.x, k.y, k.x, k.y) for k in keys]
        pw = bb.PartWriter(out, cip.ARCHIVE, meta, rects, 1 << 20)
        for k in keys:
            pw.add(fast._ef.pmt_tile_id(14, k.x, k.y), cip.encode_cell(k, cells[k]), False)
        parts = pw.finish()
        with open(os.path.join(out, "cells.index.json"), "w") as f:
            json.dump(dict(meta, archive=cip.ARCHIVE, inputs=inputs, coverage=[list(r) for r in rects], parts=parts),
                      f)
        with open(os.path.join(self.tmp, "out", f"cip{cip.FORMAT}", release, "latest.json"), "w") as f:
            json.dump({"build": build}, f)
        return release, build

    def test_reader_from_a_folder(self):
        import netfetch
        a = sample_arrays()
        release, build = self._build({KEY: {"m:roads.9": a, "t:rail": b'{"ways": []}'}})
        r = cip.Reader(os.path.join(self.tmp, "cache", "mapdata", "raw"), netfetch.Fetcher(offline=True), release,
                       base=os.path.join(self.tmp, "out"))
        self.assertEqual(r.data_builds(), {"rails": "r1", "lanes": "l1"})
        self.assertEqual(r.build, build)
        got = cip.layer_arrays(r, KEY, "roads.9")
        self.assertEqual(list(got), list(a))
        self.assertEqual(got["lonlat"].tobytes(), a["lonlat"].tobytes())
        self.assertIsNone(cip.layer_arrays(r, KEY, "places"))                     # (a section it lacks)
        self.assertIsNone(cip.layer_arrays(r, mapcells.CellKey(14, 1, 1), "roads.9"))   # (outside its coverage)
        self.assertEqual(cip.side_json(r, "t:rail", KEY), {"ways": []})

    def test_mapdata_reads_the_build(self):
        """MapData._build gives a layer's cell file arrays from the build (no source asked), has() sees a fetched cell's
        section, and a layer the build does not hold is left to its source."""
        import mapdata
        import netfetch
        a = sample_arrays()
        lc = {"data": np.arange(16, dtype=np.uint8).reshape(4, 4), "meta": np.array("{}"), "provenance": np.array("null")}
        release, _ = self._build({KEY: {f"m:{mapdata.MapData.layer_name('roads')}": a, "m:landcover": lc}})
        r = cip.Reader(os.path.join(self.tmp, "cache", "mapdata", "raw"), netfetch.Fetcher(offline=True), release,
                       base=os.path.join(self.tmp, "out"))
        st = type("M", (), {})()
        st.cip = r
        st.layer_name = mapdata.MapData.layer_name
        st.path = lambda key, layer: os.path.join(self.tmp, "none.npz")
        self.assertFalse(mapdata.MapData.has(st, KEY, "roads"))           # (not fetched yet)
        got = mapdata.MapData._build(st, KEY, "roads", "demand")
        self.assertEqual(list(got), list(a))
        for k in a:
            self.assertEqual(got[k].tobytes(), a[k].tobytes(), k)
        self.assertTrue(mapdata.MapData.has(st, KEY, "roads"))
        self.assertEqual(mapdata.MapData._build(st, KEY, "landcover", "demand")["data"].tobytes(), lc["data"].tobytes())
        self.assertFalse(mapdata.MapData.has(st, KEY, "places"))

    def test_other_code_is_refused(self):
        import netfetch
        code = dict(cip.code_inputs("2026-10-21.0"), pipeline=-1)
        release, _ = self._build({KEY: {"m:roads.9": sample_arrays()}}, {"code": code, "data": {}})
        r = cip.Reader(os.path.join(self.tmp, "cache", "mapdata", "raw"), netfetch.Fetcher(offline=True), release,
                       base=os.path.join(self.tmp, "out"))
        self.assertIsNone(cip.layer_arrays(r, KEY, "roads.9"))
        self.assertEqual(r.data_builds(), {})

    def test_a_build_with_tags_baked_in_is_refused(self):
        """A build of the code before roads 10 (CONTENT 1: its m:roads.9 sections hold the OSM road tags of its own
        week, as the planet build started at b56aa907): refused whole, every section read from the sources, so its
        tags never mix with the run's own build's (mapdata joins those at the decode)."""
        import mapdata
        import netfetch
        old = dict(cip.code_inputs("2026-10-21.0"), cip=[cip.FORMAT, 1], layer_versions={"roads": 9})
        self.assertNotEqual(old, cip.code_inputs("2026-10-21.0"))
        release, _ = self._build({KEY: {"m:roads.9": sample_arrays(), "t:rail": b'{"ways": []}'}},
                                 {"code": old, "data": {"rails": "r1", "lanes": "l1", "osmtags": "o-older"}})
        r = cip.Reader(os.path.join(self.tmp, "cache", "mapdata", "raw"), netfetch.Fetcher(offline=True), release,
                       base=os.path.join(self.tmp, "out"))
        self.assertIsNone(cip.layer_arrays(r, KEY, "roads.9"))
        self.assertIsNone(cip.layer_arrays(r, KEY, mapdata.MapData.layer_name("roads")))
        self.assertIsNone(cip.side_json(r, "t:rail", KEY))
        self.assertEqual(r.data_builds(), {}, "no weekly pin of it either")


if __name__ == "__main__":
    unittest.main()
