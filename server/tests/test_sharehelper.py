"""sharehelper.py (the BeamMP server's helper): which cells it plans for the players (theirs of its own code, nearest
first, ahead of their headings, the missed ones sooner), and its store files read back as the plugin serves them."""
import json
import os
import shutil
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mapcells  # noqa: E402
import mpshare  # noqa: E402
import sharehelper  # noqa: E402

CODE = "a" * 16


def player(lat, lon, h=None, code=CODE):
    return {"pid": 1, "lat": lat, "lon": lon, "h": h, "codes": {"plans": code}}


class WantedTest(unittest.TestCase):
    def test_nearest_first_and_only_our_code(self):
        got = sharehelper.wanted_cells([player(40.75, -73.98), player(45.4, 11.9, code="b" * 16)], [], CODE,
                                       lambda c: False)
        self.assertTrue(got)
        self.assertEqual([d for d, _c in got], sorted(d for d, _c in got), "nearest first")
        here = mapcells.cell_of(40.75, -73.98)
        self.assertEqual(got[0][1], (here.z, here.x, here.y), "the player's own cell first")
        for _d, c in got:
            la, lo = mapcells.CellKey(*c).center()
            self.assertLess(abs(la - 40.75), 0.1, "not the other code's player (Italy)")

    def test_ahead_and_had(self):
        from unittest import mock
        self.enterContext(mock.patch.object(sharehelper, "RADIUS_M", 700.0))      # (ahead: past the circle,
        self.enterContext(mock.patch.object(sharehelper, "AHEAD_V", 100.0))       # 3 km ahead: other cells)
        still = sharehelper.wanted_cells([player(40.75, -73.98)], [], CODE, lambda c: False)
        north = sharehelper.wanted_cells([player(40.75, -73.98, h=0.0)], [], CODE, lambda c: False)
        self.assertGreater(len(north), len(still), "ahead of its heading too")
        self.assertGreater(max(mapcells.CellKey(*c).center()[0] for _d, c in north),
                           max(mapcells.CellKey(*c).center()[0] for _d, c in still), "north of it")
        none = sharehelper.wanted_cells([player(40.75, -73.98)], [], CODE, lambda c: True)
        self.assertEqual(none, [], "all had: nothing")

    def test_misses_sooner(self):
        far = mapcells.cell_of(40.75 + 0.02, -73.98)
        key = f"plans/w1s1/{far.z}_{far.x}_{far.y}"
        base = {c: dist for dist, c in sharehelper.wanted_cells([player(40.75, -73.98)], [], CODE, lambda c: False)}
        got = sharehelper.wanted_cells([player(40.75, -73.98)], [{"key": key, "c": CODE}], CODE, lambda c: False)
        d = {c: dist for dist, c in got}
        self.assertIn((far.z, far.x, far.y), d)
        self.assertEqual(sharehelper.wanted_cells([player(40.75, -73.98)], [{"key": key, "c": "x" * 16}], CODE,
                                                  lambda c: False), sorted((v, c) for c, v in base.items()),
                         "another code's miss: not ours")


class StoreTest(unittest.TestCase):
    def test_store_file_as_the_plugin_serves_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        data = os.urandom(70_000) + b"Zp" * 1000
        iid = mpshare.item_id(CODE, "plans/w1s1/14_1_2")
        sharehelper.store_put(d, iid, "plans/w1s1/14_1_2", CODE, data, mpshare.CHUNK)
        with open(os.path.join(d, "store", iid[:2], iid), encoding="ascii") as f:
            lines = f.read().split("\n")
        head = json.loads(lines[0])
        self.assertEqual((head["iid"], head["c"], head["z"], head["n"]), (iid, CODE, 1, len(lines) - 1))
        self.assertFalse(any("Zp" in ln for ln in lines[1:]), "never Zp (BeamMP's launcher)")
        back = zlib.decompress(b"".join(mpshare.b64d(ln) for ln in lines[1:]))
        self.assertEqual(back, data)


if __name__ == "__main__":
    unittest.main()
