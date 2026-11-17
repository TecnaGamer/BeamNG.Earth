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
        meta = np.frombuffer(json.dumps({"plans": [{"fid": "w1", "half": 3.5}]}).encode(), np.uint8)
        np.savez(os.path.join(d, "14_1_2.npz"), meta=meta, z=np.asarray(z, np.float64), p_n=np.array([2], np.int64))
        pcommon.write_json(os.path.join(d, "index.json"), {"plans": {"14_1_2": {"complete": True}}})

    def test_plans(self):
        self.plans(self.a, [1.0, 2.0])
        self.plans(self.b, [1.0, 2.0])
        self.assertEqual(quiet(plancmp.main, [self.a, self.b])[0], 0)
        self.plans(self.b, [1.0, 2.0 + 2 ** -40])
        code, out = quiet(plancmp.main, [self.a, self.b])
        self.assertEqual(code, 1)
        self.assertIn("z: 1 of 2 values differ", out)
        self.plans(self.b, [1.0, -0.0 + 2.0])
        self.plans(self.a, [1.0, 2.0])
        os.remove(os.path.join(self.b, "plans", "p", "14_1_2.npz"))
        code, out = quiet(plancmp.main, [self.a, self.b])
        self.assertEqual(code, 1)
        self.assertIn("one side only", out)

    def test_facts(self):
        for r, v in ((self.a, 0.0), (self.b, 0.0)):
            pcommon.write_json(os.path.join(r, "facts", "p", "ranges_14_1_2_x.json"), {"data": [[v, 1.5]]})
        self.assertEqual(quiet(factcmp.main, [self.a, self.b])[0], 0)
        pcommon.write_json(os.path.join(self.b, "facts", "p", "ranges_14_1_2_x.json"), {"data": [[-0.0, 1.5]]})
        code, out = quiet(factcmp.main, [self.a, self.b])
        self.assertEqual(code, 1, out)
        pcommon.write_json(os.path.join(self.b, "facts", "p", "specs_14_1_2_x.json"), {"data": []})
        self.assertIn("only in B: 1", quiet(factcmp.main, [self.a, self.b])[1])


class SessionsTest(Base):
    def session(self, root, msgs_, blobs=()):
        d = os.path.join(root, "sessions", "p")
        os.makedirs(os.path.join(d, "blobs"), exist_ok=True)
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
        far = {"type": "far_tile", "anchorId": 1, "key": "3_1_2", "level": 3, "shape": "@aa.cdae", "slotSerial": 7}
        self.session(self.a, [{"type": "status", "text": "1 s"}, far, {"type": "far_swap", "id": 1, "add": ["x"]}])
        self.session(self.b, [dict(far, slotSerial=9), {"type": "status", "text": "2 s"},
                              {"type": "far_swap", "id": 7, "add": ["x"]}, {"type": "far_swap", "id": 8, "add": []}])
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "far")[0], 0)
        code, out = quiet(msgs.compare, self.a, self.b, None)
        self.assertEqual(code, 0)
        self.assertIn("far field bookkeeping", out)

    def test_files_by_content(self):
        pa, pb = self.png(100), self.png(101)
        ha, hb = pcommon.sha(pa), pcommon.sha(pb)
        blk = {"type": "terrain_ready", "anchorId": 1, "requestId": 1, "cx": 0, "cy": 0}
        self.session(self.a, [dict(blk, heightmap=f"@{ha}.png")], [(ha + ".png", pa)])
        self.session(self.b, [dict(blk, heightmap=f"@{ha}.png")], [(ha + ".png", pa)])
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "blocks")[0], 0)
        self.session(self.b, [dict(blk, heightmap=f"@{hb}.png")], [(hb + ".png", pb)])
        code, out = quiet(msgs.compare, self.a, self.b, "blocks")
        self.assertEqual(code, 1)
        self.assertIn("pixels: 16 of 16 differ, max 1", out)
        # the last copy of a block is what counts; a tile only one side sent is reported as such
        self.session(self.b, [dict(blk, heightmap=f"@{hb}.png"), dict(blk, heightmap=f"@{ha}.png"),
                              {"type": "roads_tile", "anchorId": 1, "key": "g17_1_2", "path": "@x.json"}],
                     [(ha + ".png", pa)])
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "blocks")[0], 0)
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "tiles")[0], 2)

    def test_far_field_end_state(self):
        """The far field's swaps compared by what they leave shown and take away, not by their order or counter."""
        t = lambda k: {"type": "far_tile", "anchorId": 1, "key": k, "level": 3}
        a = [t("3_1_1"), t("3_1_2"), {"type": "far_swap", "anchorId": 1, "id": 1, "add": ["3_1_1", "3_1_2"], "remove": []}]
        b = [t("3_1_2"), t("3_1_1"), {"type": "far_swap", "anchorId": 1, "id": 4, "add": ["3_1_2"], "remove": []},
             {"type": "far_swap", "anchorId": 1, "id": 5, "add": ["3_1_1"], "remove": []}]
        self.session(self.a, a)
        self.session(self.b, b)
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "far")[0], 0)
        self.session(self.b, b + [{"type": "far_remove", "anchorId": 1, "key": "3_1_1"}])
        code, out = quiet(msgs.compare, self.a, self.b, "far")
        self.assertEqual(code, 1)
        self.assertIn("far_field", out)

    def test_slots_serials_ignored(self):
        t = {"type": "roads_tile", "anchorId": 1, "key": "g17_1_2", "slots": {"mesh": ["@s.cdae", 4]}}
        self.session(self.a, [t])
        self.session(self.b, [dict(t, slots={"mesh": ["@s.cdae", 11]})])
        self.assertEqual(quiet(msgs.compare, self.a, self.b, "tiles")[0], 0)


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
    def test_launcher_options(self):
        import launcher
        self.assertEqual(session.RECOMMENDED, launcher.RECOMMENDED)

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
