"""The revisit cache (revisit.py): a tile's files kept and written back under another session's names; builds that
read something missing are not kept."""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import netfetch  # noqa: E402
import revisit  # noqa: E402


class RevisitTest(unittest.TestCase):
    def test_an_item_comes_back_and_names_swap(self):
        with tempfile.TemporaryDirectory() as d:
            text = json.dumps({"road": {"shape": "/temp/s1/road_3_1_-2.cdae"},
                               "bld": {"shape": "/temp/s1/bld_3_1_-2.cdae"}}, separators=(",", ":"))
            self.assertEqual(revisit.shapes_in(text, "/temp/s1"), ["bld_3_1_-2.cdae", "road_3_1_-2.cdae"])
            mine = [("/temp/s1/", "@V@/"), ("_3_1_-2", "_@A@_1_-2")]
            k = revisit.key("roads", 39.28, -76.56, 1, -2)
            self.assertTrue(revisit.store(d, "roads", k, {revisit.swap("roads_3_1_-2.json", mine):
                                                          revisit.swap(text, mine).encode(), "x.bin": b"mesh"},
                                          {"n": 5}))
            blobs, extra = revisit.load(d, "roads", k)
            self.assertEqual(extra, {"n": 5})
            self.assertEqual(blobs["x.bin"], b"mesh")
            back = [("@V@/", "/temp/s2/"), ("_@A@_1_-2", "_7_1_-2")]
            self.assertIn("roads_@A@_1_-2.json", blobs)
            self.assertEqual(revisit.swap("roads_@A@_1_-2.json", back), "roads_7_1_-2.json")
            tile = json.loads(revisit.swap(blobs["roads_@A@_1_-2.json"].decode(), back))
            self.assertEqual(tile["road"]["shape"], "/temp/s2/road_7_1_-2.cdae")
            self.assertIsNone(revisit.load(d, "roads", revisit.key("roads", 0, 0, 1, -2)))

    def test_failed_fetches_mark_builds_incomplete_but_missing_data_does_not(self):
        n = revisit.faults()
        netfetch.NotFound("404")                # (no data there: as it should be)
        netfetch.Deferred("dropped")
        self.assertEqual(revisit.faults(), n)
        netfetch.FetchFailed("timed out")
        self.assertEqual(revisit.faults(), n + 1)

    def test_a_cells_road_facts_are_shared(self):
        import roads
        from test_roads import features
        with tempfile.TemporaryDirectory() as d:
            old = revisit.CACHE_DIR
            revisit.CACHE_DIR = d
            try:
                def cell():
                    f = features([[(0, 0), (100, 0)], [(0, 50), (100, 50)]], ["residential", "primary"])
                    f.__dict__["_bnge_cell"] = ("15/1/2", "roads", "v")
                    return f
                a = roads.road_specs(cell())
                ra = [roads.level_ranges(cell(), i) for i in range(2)]
                b_feats = cell()            # (another process: its own decoded cell)
                from unittest import mock
                with mock.patch.object(roads, "_road_specs", side_effect=AssertionError("made again")), \
                        mock.patch.object(roads, "_level_ranges", side_effect=AssertionError("made again")):
                    self.assertEqual(roads.road_specs(b_feats), a)
                    self.assertEqual([roads.level_ranges(b_feats, i) for i in range(2)], ra)
                self.assertEqual(b_feats.__dict__["_road_specs"][(None, None, False)], a)
                self.assertEqual([roads.level_ranges(b_feats, i) for i in range(2)], ra)
                self.assertEqual(b_feats.__dict__.get("_ranges_state"), "done")
            finally:
                revisit.CACHE_DIR = old

    def test_the_key_holds_the_code(self):
        self.assertEqual(revisit.key("roads", 1, 2), revisit.key("roads", 1, 2))
        self.assertNotEqual(revisit.key("roads", 1, 2), revisit.key("roads", 1, 3))
        self.assertEqual(len(revisit.code_key()), 16)

    def test_packaged_solver_change_invalidates_cached_collision_meshes(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, mock.patch.object(revisit, "_code", []), \
                mock.patch.object(revisit, "__file__", os.path.join(d, "revisit.py")):
            # A frozen app has no loose .py sources. Its native module can remain
            # unchanged between Python-only bridge fixes.
            with open(os.path.join(d, "earthfast.pyd"), "wb") as f:
                f.write(b"same native module")
            fingerprint = os.path.join(d, "CODE_ID")
            with open(fingerprint, "w") as f:
                f.write("original-1.6.3")
            old = revisit.key("roads", 47.57282, -122.31967)
            revisit.store(d, "roads", old, {"bridge.cdae": b"old collision mesh"})
            with open(fingerprint, "w") as f:
                f.write("bridge-clearance-fix")
            revisit._code.clear()
            new = revisit.key("roads", 47.57282, -122.31967)
            self.assertNotEqual(old, new)
            self.assertIsNone(revisit.load(d, "roads", new))

    def test_frozen_build_fingerprint_invalidates_old_geometry(self):
        import peerwork
        source = compile(Path(revisit.__file__).read_text(encoding='utf-8'), 'revisit.py', 'exec')
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            # PyInstaller has bytecode in PYZ and a native module on disk, no .py files.
            (folder / 'earthfast.pyd').write_bytes(b'unchanged-native-module')

            def load(build_id, name):
                (folder / 'CODE_ID').write_text(build_id, encoding='ascii')
                module = types.ModuleType(name)
                module.__file__ = str(folder / 'revisit.pyc')
                exec(source, module.__dict__)
                with mock.patch.object(peerwork, '_ROOT', temp), mock.patch.object(peerwork, '_CODE_ID', None):
                    module.code_key()
                return module

            old = load('build-a', 'old')
            same = load('build-a', 'same')
            new = load('build-b', 'new')
            self.assertEqual(old.code_key(), same.code_key())
            self.assertNotEqual(old.code_key(), new.code_key())
            for kind in ('roads', 'plans', 'cellfacts', 'far'):
                key = old.key(kind, 'public-square')
                self.assertTrue(old.store(temp, kind, key, {'geometry': b'old-layout'}))
                self.assertIsNone(new.load(temp, kind, new.key(kind, 'public-square')))
                self.assertIsNotNone(old.load(temp, kind, key), 'invalidation does not delete old data')
            (folder / 'earthfast.pyd').write_bytes(b'changed-native-module')
            self.assertNotEqual(new.code_key(), load('build-b', 'native_changed').code_key())


if __name__ == "__main__":
    unittest.main()
