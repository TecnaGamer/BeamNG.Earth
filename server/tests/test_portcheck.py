"""The port's byte-identity tools (tools/portcheck, the Rust port plan's phase 0b) on made-up outputs: identical
builds compare as identical, a moved float, a -0.0, a changed pixel or a file one side lacks do not; the fields that only
say in which order things were sent are left out; the session plays the launcher's options and the mod's hello."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "portcheck"))
sys.path.insert(0, str(ROOT))
import factcmp  # noqa: E402
import msgs  # noqa: E402
import pcommon  # noqa: E402
import plancmp  # noqa: E402
import session  # noqa: E402


def quiet(fn, *a):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = fn(*a)
    return code, buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.a, self.b = os.path.join(self.tmp, "A"), os.path.join(self.tmp, "B")


class PlansFactsTest(Base):
    def plans(self, root, z):
        d = os.path.join(root, "plans", "p")
        os.makedirs(d, exist_ok=True)
        roads = [{"fid": "w1", "half": 3.5}] if z else []
        meta = np.frombuffer(json.dumps({"plans": roads}).encode(), np.uint8)
        np.savez(os.path.join(d, "14_1_2.npz"), meta=meta, z=np.asarray(z, np.float64),
                 p_n=np.array([len(z)] if z else [], np.int64))
        pcommon.write_json(os.path.join(d, "index.json"), {"cells": ["14/1/2"], "plans": {"14_1_2": {"complete": True}}})

    def test_plans(self):
        self.plans(self.a, [1.0, 2.0])
        self.plans(self.b, [1.0, 2.0])
        self.assertEqual(quiet(plancmp.main, [self.a, self.b, "--places", "p"])[0], 0)
        self.plans(self.b, [1.0, 2.0 + 2 ** -40])
        code, out = quiet(plancmp.main, [self.a, self.b, "--places", "p"])
        self.assertEqual(code, 1)
        self.assertIn("z: 1 of 2 values differ", out)
        self.plans(self.b, [1.0, -0.0 + 2.0])
        self.plans(self.a, [1.0, 2.0])
        os.remove(os.path.join(self.b, "plans", "p", "14_1_2.npz"))
        code, out = quiet(plancmp.main, [self.a, self.b, "--places", "p"])
        self.assertEqual(code, 1)
        self.assertIn("one side only", out)

    def test_missing_or_aliased_outputs(self):
        for compare in (plancmp.main, factcmp.main):
            self.assertNotEqual(quiet(compare, [self.a, self.b, "--places", "p"])[0], 0)
            with self.assertRaises(SystemExit):
                compare([self.a, self.a])
        for root in (self.a, self.b):
            self.plans(root, [1.0])
            os.remove(os.path.join(root, "plans", "p", "14_1_2.npz"))
        self.assertNotEqual(quiet(plancmp.main, [self.a, self.b, "--places", "p"])[0], 0)

    def test_unfinished_build_and_changed_metadata_bytes(self):
        for root in (self.a, self.b):
            self.plans(root, [1.0])
        mark = Path(self.b) / "build-p.pending"
        mark.touch()
        for compare in (factcmp.main, plancmp.main):
            with self.assertRaisesRegex(SystemExit, "incomplete build"):
                compare([self.a, self.b, "--places", "p"])
        mark.unlink()
        for root, data in ((self.a, b'{"a": 1}'), (self.b, b'{"a":1 }')):
            np.savez(Path(root) / "plans/p/14_1_2.npz", meta=np.frombuffer(data, np.uint8))
        self.assertNotEqual(quiet(plancmp.main, [self.a, self.b, "--places", "p"])[0], 0)

    def test_short_missing_and_verified_empty_cells(self):
        for root in (self.a, self.b):
            self.plans(root, [])
            (Path(root) / "facts/p").mkdir(parents=True)
        cell = "14/1/2"
        plan = {"14_1_2": {"complete": True}}
        for plans, cells, counts, ok in (
                (plan, [cell], {cell: 0}, True),
                ({"14_1_2": {"complete": False}}, [cell], {cell: 0}, False),
                (plan, [cell, "14/2/3"], {cell: 0, "14/2/3": 0}, False),
                (plan, [cell], {}, False)):
            for root in (self.a, self.b):
                pcommon.write_json(os.path.join(root, "plans/p/index.json"),
                                   {"cells": cells, "plans": plans, "road_features": counts})
            for compare in (plancmp.main, factcmp.main):
                with self.subTest(plans=plans, cells=cells, compare=compare.__module__):
                    self.assertEqual(quiet(compare, [self.a, self.b, "--places", "p"])[0] == 0, ok)

    def test_writer_excludes_other_writers_and_readers(self):
        for root in (self.a, self.b):
            self.plans(root, [1.0])
        with pcommon.output_lock(self.a, writing=True):
            with self.assertRaisesRegex(SystemExit, "cannot lock"):
                with pcommon.output_lock(self.a, writing=True):
                    self.fail("second writer acquired lock")
            with self.assertRaisesRegex(SystemExit, "cannot lock"):
                plancmp.main([self.a, self.b, "--places", "p"])
        with pcommon.comparing(self.a, self.b):
            with self.assertRaisesRegex(SystemExit, "cannot lock"):
                with pcommon.output_lock(self.a, writing=True):
                    self.fail("writer acquired reader's lock")
        self.assertEqual(quiet(plancmp.main, [self.a, self.b, "--places", "p"])[0], 0)

    def test_matching_omissions_from_both_inventories(self):
        cell = "14/1/2"
        for root in (self.a, self.b):
            self.plans(root, [2.0])
            pcommon.write_json(Path(root) / "plans/p/index.json",
                               {"cells": [cell], "plans": {"14_1_2": {"complete": True}}, "road_features": {cell: 1}})
            pcommon.write_json(Path(root) / "facts/p/specs_14_9_9_a.json", {"data": [2]})
        rc, why = quiet(factcmp.main, [self.a, self.b, "--places", "p"])
        self.assertNotEqual(rc, 0)
        self.assertIn("no facts stored", why)
        with mock.patch.object(pcommon, "corpus", return_value={"places": {"p": {"plan_cells": [cell, "14/2/3"]}}}):
            for compare in (plancmp.main, factcmp.main):
                rc, why = quiet(compare, [self.a, self.b])
                self.assertNotEqual(rc, 0)
                self.assertIn("frozen corpus", why)

    def test_facts(self):
        for r, v in ((self.a, 0.0), (self.b, 0.0)):
            self.plans(r, [v])
            pcommon.write_json(os.path.join(r, "facts", "p", "ranges_14_1_2_x.json"), {"data": [[v, 1.5]]})
        self.assertEqual(quiet(factcmp.main, [self.a, self.b, "--places", "p"])[0], 0)
        pcommon.write_json(os.path.join(self.b, "facts", "p", "ranges_14_1_2_x.json"), {"data": [[-0.0, 1.5]]})
        code, out = quiet(factcmp.main, [self.a, self.b, "--places", "p"])
        self.assertEqual(code, 1, out)
        pcommon.write_json(os.path.join(self.b, "facts", "p", "specs_14_1_2_x.json"), {"data": []})
        self.assertIn("only in B: 1", quiet(factcmp.main, [self.a, self.b, "--places", "p"])[1])


class SessionsTest(Base):
    def session(self, root, msgs_, blobs=()):
        d = os.path.join(root, "sessions", "p")
        os.makedirs(os.path.join(d, "blobs"), exist_ok=True)
        pcommon.write_json(os.path.join(d, "summary.json"), {"settled": True, "messages": len(msgs_)})
        for name, data in blobs:
            with open(os.path.join(d, "blobs", name), "wb") as f:
                f.write(data)
        with open(os.path.join(d, "messages.jsonl"), "w") as f:
            for i, m in enumerate(msgs_):
                f.write(json.dumps({"i": i, "t": 0.1 * i, "m": m}) + "\n")

    def png(self, v):
        buf = io.BytesIO()
        Image.fromarray(np.full((4, 4), v, np.uint16)).save(buf, "PNG")
        return buf.getvalue()

    def test_order_fields_ignored(self):
        far = {"type": "far_tile", "anchorId": 1, "key": "3_1_2", "level": 3, "slotSerial": 7}
        self.session(self.a, [{"type": "status", "text": "1 s"}, far, {"type": "far_swap", "id": 1, "add": ["x"]}])
        self.session(self.b, [dict(far, slotSerial=9), {"type": "status", "text": "2 s"},
                              {"type": "far_swap", "id": 7, "add": ["x"]}, {"type": "far_swap", "id": 8, "add": []}])
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "far", ["p"])[0], 0)
        code, out = quiet(msgs.compare, self.a, self.b, None, ["p"])
        self.assertEqual(code, 0)
        self.assertIn("far field bookkeeping", out)

    def test_files_by_content(self):
        pa, pb = self.png(100), self.png(101)
        ha, hb = pcommon.sha(pa), pcommon.sha(pb)
        blk = {"type": "terrain_ready", "anchorId": 1, "requestId": 1, "cx": 0, "cy": 0}
        self.session(self.a, [dict(blk, heightmap=f"@{ha}.png")], [(ha + ".png", pa)])
        self.session(self.b, [dict(blk, heightmap=f"@{ha}.png")], [(ha + ".png", pa)])
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "blocks", ["p"])[0], 0)
        self.session(self.b, [dict(blk, heightmap=f"@{hb}.png")], [(hb + ".png", pb)])
        code, out = quiet(msgs.compare, self.a, self.b, "blocks", ["p"])
        self.assertEqual(code, 1)
        self.assertIn("pixels: 16 of 16 differ, max 1", out)
        # the last copy of a block is what counts; a tile only one side sent is reported as such
        self.session(self.b, [dict(blk, heightmap=f"@{hb}.png"), dict(blk, heightmap=f"@{ha}.png"),
                              {"type": "roads_tile", "anchorId": 1, "key": "g17_1_2", "path": "@x.json"}],
                     [(ha + ".png", pa)])
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "blocks", ["p"])[0], 0)
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "tiles", ["p"])[0], 2)

    def test_incomplete_recordings(self):
        for root in (self.a, self.b):
            self.session(root, [{"type": "hello"}])
        self.assertNotEqual(quiet(msgs.compare, self.a, self.b, "tiles", ["p"])[0], 0)
        for root in (self.a, self.b):
            self.session(root, [{"type": "far_retex", "path": "@" + pcommon.sha(b"x") + ".dds"}])
        self.assertNotEqual(quiet(msgs.compare, self.a, self.b, "far", ["p"])[0], 0)
        for root in (self.a, self.b):
            self.session(root, [{"type": "far_tile", "key": "x"}])
        os.remove(os.path.join(self.b, "sessions", "p", "summary.json"))
        self.assertNotEqual(quiet(msgs.compare, self.a, self.b, "far", ["p"])[0], 0)
        self.assertFalse(session.loaded({"ground": [1, 1]}))

    def test_legacy_settlement_and_complete_message_count(self):
        progress = dict.fromkeys(("ground", "far", "trees", "roads"), [1, 1])
        events = [{"type": "far_tile", "key": "x"}, {"type": "far_done", "anchorId": 1},
                  dict(progress, type="load_progress", anchorId=1, net=[0])]
        summary = {"anchor": 1, "far_done": True, "seconds": 21., "messages": 3}
        for root in (self.a, self.b):
            self.session(root, events)
            pcommon.write_json(os.path.join(root, "sessions/p/summary.json"), summary)
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "far", ["p"])[0], 0)
        for overrides in ({"seconds": 2}, {"messages": 4}, {"anchor": 2}):
            pcommon.write_json(os.path.join(self.b, "sessions/p/summary.json"), dict(summary, **overrides))
            self.assertNotEqual(quiet(msgs.compare, self.a, self.b, "far", ["p"])[0], 0)

    def test_far_field_end_state(self):
        """The far field's swaps compared by what they leave shown and take away, not by their order or counter."""
        t = lambda k: {"type": "far_tile", "anchorId": 1, "key": k, "level": 3}
        a = [t("3_1_1"), t("3_1_2"), {"type": "far_swap", "anchorId": 1, "id": 1, "add": ["3_1_1", "3_1_2"], "remove": []}]
        b = [t("3_1_2"), t("3_1_1"), {"type": "far_swap", "anchorId": 1, "id": 4, "add": ["3_1_2"], "remove": []},
             {"type": "far_swap", "anchorId": 1, "id": 5, "add": ["3_1_1"], "remove": []}]
        self.session(self.a, a)
        self.session(self.b, b)
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "far", ["p"])[0], 0)
        self.session(self.b, b + [{"type": "far_remove", "anchorId": 1, "key": "3_1_1"}])
        code, out = quiet(msgs.compare, self.a, self.b, "far", ["p"])
        self.assertEqual(code, 1)
        self.assertIn("far_field", out)

    def test_slots_serials_ignored(self):
        shape = "@" + pcommon.sha(b"shape") + ".cdae"
        blob = [(shape[1:], b"shape")]
        t = {"type": "roads_tile", "anchorId": 1, "key": "g17_1_2", "slots": {"mesh": [shape, 4]}}
        self.session(self.a, [t], blob)
        self.session(self.b, [dict(t, slots={"mesh": [shape, 11]})], blob)
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "tiles", ["p"])[0], 0)


class RecorderTest(Base):
    def test_session_names_masked(self):
        """Two sessions name the same far tile's pooled material and file after their own id and slot: recorded the same."""
        got = []
        for sid, slot in (("20261116-193231-f328", 12), ("20261116-193755-e70c", 19)):
            out, user = os.path.join(self.tmp, sid, "out"), os.path.join(self.tmp, sid, "user")
            os.makedirs(os.path.join(user, "temp", "beamngEarth", sid))
            with open(os.path.join(user, "temp", "beamngEarth", sid, f"slot_{slot:05d}.json"), "w") as f:
                json.dump({"sid": sid, "material": f"bngEarthFar_{sid[-4:]}_{slot:05d}"}, f)
            rec = session.Recorder(out, user)
            rec.add({"type": "far_tile", "key": "3_1_2", "material": f"bngEarthFar_{sid[-4:]}_{slot:05d}",
                     "shape": f"/temp/beamngEarth/{sid}/slot_{slot:05d}.json"})
            rec.close()
            with open(os.path.join(out, "messages.jsonl")) as f:
                got.append(json.loads(f.readline())["m"])
        self.assertEqual(got[0], got[1])
        self.assertEqual(got[0]["material"], "bngEarthFar_xxxx_xxxxx")


class SetupTest(unittest.TestCase):
    def test_session_deadline_requires_settlement(self):
        client = mock.Mock()
        client.recv.side_effect = [{"type": "hello"}, {"type": "anchor_ready", "anchorId": 1}]
        with self.assertRaisesRegex(TimeoutError, "did not settle"):
            session.play(mock.Mock(server_dir=pcommon.SERVER, timeout=0), client, mock.Mock(progress=None),
                         {"lat": 0, "lon": 0, "view_m": 2000})
        counts = dict.fromkeys(("ground", "roads", "trees", "far"), [2, 2])
        self.assertTrue(session.loaded(dict(counts, net=[0, 0])))
        self.assertFalse(session.loaded(dict(counts, ground=["2", "2"], net=[0, 0])))

    def test_launcher_options(self):
        import launcher
        self.assertEqual(session.RECOMMENDED, launcher.RECOMMENDED)

    def test_season_pinned(self):
        """The date-graded season (--season auto, earthd's default) is pinned: sessions an hour apart across
        midnight compare equal."""
        self.assertNotIn("--season", session.RECOMMENDED, "pinned apart from the launcher's options")
        i = session.PINNED_ARGS.index("--season")
        self.assertNotEqual(session.PINNED_ARGS[i + 1], "auto")

    def test_mod_hello(self):
        hello = session.mod_hello(str(ROOT / "server"))
        self.assertEqual(hello["protocol"], 4)
        for field, _name, _pat in session.CAPS:
            self.assertIsInstance(hello[field], int, field)
        sys.path.insert(0, str(ROOT / "server" / "tests"))
        import test_protocol_agreement as pa
        self.assertLessEqual(set(hello) - {"type"}, set(pa.MOD_TO_SERVER["hello"]))

    def test_refuses_the_real_cache(self):
        with self.assertRaises(SystemExit):
            pcommon.check_cache(os.path.expanduser("~/.cache/beamng-earth"))
        with tempfile.TemporaryDirectory() as d:
            os.symlink(d, os.path.join(d, "link"))
            with self.assertRaises(SystemExit):
                pcommon.check_cache(d)

    def test_refuses_overlapping_work_copies(self):
        """A work copy equal to, inside or holding the corpus cache: refused before anything is deleted."""
        with tempfile.TemporaryDirectory() as d:
            corpus = os.path.join(d, "cache")
            os.makedirs(os.path.join(corpus, "dem"))
            with open(os.path.join(corpus, "dem", "t"), "w") as f:
                f.write("x")
            for dst in (corpus, os.path.join(corpus, "work"), d):
                with self.assertRaises(SystemExit, msg=dst):
                    pcommon.work_copy(corpus, dst)
                self.assertTrue(os.path.exists(os.path.join(corpus, "dem", "t")), "the corpus untouched")
            with self.assertRaises(SystemExit):
                pcommon.check_apart(corpus, os.path.join(corpus, "out"))
            pcommon.work_copy(corpus, os.path.join(d, "work"))      # (apart: copied)
            self.assertTrue(os.path.exists(os.path.join(d, "work", "dem", "t")))

    def test_refuses_the_configured_cache_and_game_folders(self):
        with tempfile.TemporaryDirectory() as d:
            chosen = os.path.join(d, "chosen")
            os.makedirs(chosen)
            with mock.patch.dict(os.environ, {"BNGE_CACHE_DIR": chosen}):
                for p in (chosen, os.path.join(chosen, "sub"), d):
                    with self.assertRaises(SystemExit, msg=p):
                        pcommon.check_cache(p)
            with mock.patch("apppaths.saved_cache_dir", return_value=chosen):
                with self.assertRaises(SystemExit):
                    pcommon.check_cache(os.path.join(chosen, "x"))
        with self.assertRaises(SystemExit):
            pcommon.check_cache(os.path.expanduser("~/.local/share/BeamNG/BeamNG.drive/current"))

    def test_corpus(self):
        names = [n for n, _ in pcommon.places()]
        self.assertEqual(names, ["fells_point", "manhattan_penn", "vienna_s1_rautenweg", "stelvio", "phoenix_i10",
                                 "zermatt", "tromso", "laramie"])
        for _n, p in pcommon.places():
            self.assertTrue({"lat", "lon", "plan_half_m", "view_m", "why"} <= set(p))


if __name__ == "__main__":
    unittest.main()
