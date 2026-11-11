"""The revisit cache (revisit.py): a tile's files kept and written back under another session's names; builds that
read something missing are not kept."""
import json
import os
import sys
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
import unittest

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
                        mock.patch.object(roads, "_level_ranges_many", side_effect=AssertionError("made again")):
                    self.assertEqual(roads.road_specs(b_feats), a)
                    self.assertEqual([roads.level_ranges(b_feats, i) for i in range(2)], ra)
                self.assertEqual(b_feats.__dict__["_road_specs"][(None, None, False)], a)
                self.assertEqual([roads.level_ranges(b_feats, i) for i in range(2)], ra)
                self.assertEqual(b_feats.__dict__.get("_ranges_state"), "done")
            finally:
                revisit.CACHE_DIR = old

    def test_sidecar_lines_are_written_compacted_and_dropped(self):
        """store(where=...) appends one line to this process's sidecar; compact() folds older files into one, the
        last line per item, without the items whose zip is gone; a file written after `before` stays as it is."""
        import time
        with tempfile.TemporaryDirectory() as d:
            sig = revisit.sig(sidewalks=True, md={"overture": "2026-09-23.1"})
            ks = [revisit.key("roads", 39.28, -76.56, i) for i in range(3)]
            for i, k in enumerate(ks):
                self.assertTrue(revisit.store(d, "roads", k, {"a": b"x"},
                                              where=revisit.where(39.28, -76.56, 39.28 + i * 0.01, -76.56, 39.29, -76.55,
                                                                  sig)))
            self.assertTrue(revisit.store(d, "far", revisit.key("far", 1), {"a": b"x"}))     # (no where: no line)
            wd = os.path.join(d, revisit.SUBDIR, revisit.WHERE_DIR)
            mine = os.listdir(wd)
            self.assertEqual(len(mine), 1)
            with open(os.path.join(wd, mine[0])) as f:
                rows = [revisit.parse_where(line) for line in f]
            self.assertEqual([r[3] for r in rows], ks)
            self.assertEqual(rows[0][:3], (revisit.code_key(), sig, "roads"))
            self.assertAlmostEqual(rows[2][6], 39.30)
            self.assertIsNone(revisit.parse_where("damaged\tline\n"))
            # an older run's file: two lines for one item (the last wins), one item whose zip is gone
            old = os.path.join(wd, "1_1.tsv")
            with open(os.path.join(wd, mine[0])) as f:
                lines = f.readlines()
            gone = revisit.key("roads", 0, 0, 9)
            with open(old, "w") as f:
                f.writelines(lines + [lines[0], lines[0].replace(ks[0], gone)])
            past = time.time() - 100
            os.utime(old, (past, past))
            self.assertEqual(revisit.compact(d, time.time() - 50), 3)
            self.assertTrue(revisit.COMPACTED.is_set())
            names = sorted(os.listdir(wd))
            self.assertIn(mine[0], names, "this process's own file stays")
            self.assertNotIn("1_1.tsv", names)
            comp = [n for n in names if n.startswith("c")]
            with open(os.path.join(wd, comp[0])) as f:
                kept = [revisit.parse_where(line)[3] for line in f]
            self.assertEqual(sorted(kept), sorted(ks), "the line whose zip is gone is dropped")

    def test_settings_signature(self):
        cur = {"sidewalks": True, "md": {"overture": "2026-09-23.1"}, "tone": 1.0}
        s = revisit.sig(sidewalks=True, md={"overture": "2026-09-23.1"})
        self.assertTrue(revisit.sig_current(s, cur))
        self.assertFalse(revisit.sig_current(s, dict(cur, sidewalks=False)), "a setting changed")
        self.assertFalse(revisit.sig_current(revisit.sig(lanes=1), cur), "a setting the server does not name")
        self.assertTrue(revisit.sig_current("", cur))
        self.assertEqual(revisit.sig(b=1, a=2), revisit.sig(a=2, b=1))

    def test_sidecar_descriptor_is_closed_on_success_and_write_error(self):
        from unittest import mock
        real_open = os.open
        opened = []

        def record_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        with tempfile.TemporaryDirectory() as d:
            wh = revisit.where(39.28, -76.56, 39.28, -76.56, 39.29, -76.55, "")
            with mock.patch.object(revisit.os, "open", side_effect=record_open):
                revisit._note(d, "roads", "first", wh)
                self.assertEqual(len(opened), 1)
                with self.assertRaises(OSError):
                    os.fstat(opened[-1])
                with mock.patch.object(revisit.os, "write", side_effect=OSError("disk write failed")):
                    revisit._note(d, "roads", "failed", wh)
                self.assertEqual(len(opened), 2)
                with self.assertRaises(OSError):
                    os.fstat(opened[-1])
            wd = os.path.join(d, revisit.SUBDIR, revisit.WHERE_DIR)
            with open(os.path.join(wd, os.listdir(wd)[0]), encoding="utf-8") as f:
                self.assertEqual([revisit.parse_where(line)[3] for line in f], ["first"])

    def test_concurrent_sidecar_appends_keep_every_row(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as d:
            wh = revisit.where(39.28, -76.56, 39.28, -76.56, 39.29, -76.55, "")
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda i: revisit._note(d, "roads", str(i), wh), range(100)))
            wd = os.path.join(d, revisit.SUBDIR, revisit.WHERE_DIR)
            with open(os.path.join(wd, os.listdir(wd)[0]), encoding="utf-8") as f:
                rows = [revisit.parse_where(line) for line in f]
            self.assertEqual(len(rows), 100)
            self.assertTrue(all(r is not None for r in rows))
            self.assertEqual({r[3] for r in rows}, {str(i) for i in range(100)})

    def test_sidecar_reopens_after_cache_directory_is_replaced(self):
        wh = revisit.where(39, -76, 39, -76, 40, -75, "")
        with tempfile.TemporaryDirectory() as d:
            revisit._note(d, "roads", "first", wh)
            wd = os.path.join(d, revisit.SUBDIR, revisit.WHERE_DIR)
            shutil.rmtree(wd)                 # Windows must not retain an open handle here.
            revisit._note(d, "roads", "second", wh)
            names = os.listdir(wd)            # Unix must not keep writing the removed inode.
            self.assertEqual(len(names), 1)
            with open(os.path.join(wd, names[0])) as f:
                self.assertEqual([revisit.parse_where(line)[3] for line in f], ["second"])

    def test_sidecar_closes_handle_after_write_failure(self):
        wh = revisit.where(39, -76, 39, -76, 40, -75, "")
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(revisit.os, "write", side_effect=OSError("full disk")), \
                    mock.patch.object(revisit.os, "close", wraps=os.close) as close:
                revisit._note(d, "roads", "failed", wh)
                close.assert_called_once()
                with self.assertRaises(OSError):
                    os.fstat(close.call_args.args[0])
            revisit._note(d, "roads", "recovered", wh)
            wd = os.path.join(d, revisit.SUBDIR, revisit.WHERE_DIR)
            with open(os.path.join(wd, os.listdir(wd)[0])) as f:
                self.assertEqual([revisit.parse_where(line)[3] for line in f], ["recovered"])

    def test_concurrent_sidecar_appends_remain_complete(self):
        wh = revisit.where(39, -76, 39, -76, 40, -75, "")
        with tempfile.TemporaryDirectory() as d:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda k: revisit._note(d, "roads", str(k), wh), range(200)))
            wd = os.path.join(d, revisit.SUBDIR, revisit.WHERE_DIR)
            with open(os.path.join(wd, os.listdir(wd)[0])) as f:
                rows = [revisit.parse_where(line) for line in f]
            self.assertEqual(len(rows), 200)
            self.assertNotIn(None, rows)
            self.assertEqual({r[3] for r in rows}, {str(k) for k in range(200)})

    def test_the_key_holds_the_code(self):
        self.assertEqual(revisit.key("roads", 1, 2), revisit.key("roads", 1, 2))
        self.assertNotEqual(revisit.key("roads", 1, 2), revisit.key("roads", 1, 3))
        self.assertEqual(len(revisit.code_key()), 16)

    def test_format_keys_survive_code_changes(self):
        """Road plans and cell facts (the user, 2026-11-02): keyed by their format number, not the code, so an update
        keeps them; another format number or build switch is another key; the map's cache view counts their sidecar
        rows as current."""
        from unittest import mock
        import plancache
        k = revisit.format_key("plans", "1.1", (14, 1, 2))
        with mock.patch.object(revisit, "_code", ["another-code-key"]):
            self.assertEqual(revisit.format_key("plans", "1.1", (14, 1, 2)), k, "the code changed: the same key")
            self.assertNotEqual(revisit.key("plans", (14, 1, 2)), revisit.format_key("plans", "1.1", (14, 1, 2)))
        self.assertNotEqual(revisit.format_key("plans", "2.1", (14, 1, 2)), k, "another format")
        with mock.patch.object(revisit, "_switches", ["other-switches"]):
            self.assertNotEqual(revisit.format_key("plans", "1.1", (14, 1, 2)), k, "other BNE_* switches")
        codes = revisit.current_codes()
        self.assertIn(revisit.code_key(), codes)
        self.assertIn(revisit.format_marker("plans", plancache.plans_format()), codes)


if __name__ == "__main__":
    unittest.main()
