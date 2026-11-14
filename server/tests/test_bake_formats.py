"""Baked packs (bakepack.py): read without the code hash, so a change that makes other plans or facts must bump
plancache.PLANS_FORMAT / roads.FACTS_FORMAT, or players would get the old ones from a pack. This test holds a
fixture's plans (as the heights graded with them) and facts digests with the format numbers they were made at
(golden/bake_formats.json): output that changed under the same number fails. After bumping the number, run with
BNGE_UPDATE_GOLDEN=1 to store the new golden. The plans are kept as their heights (0.1 mm steps) and compared
within PLANS_TOL_M, not as a hash: a hash of rounded heights flips between machines on a value ~1e-8 m from a
rounding step (other CPUs, compilers, numpy builds), while a planning change moves heights by centimetres.
(A fixture covers the common paths, not every rule: bump on any deliberate change of planning or road facts
output even when this passes.)

Also: a pack's items round trip, are found only under a matching meta, and a run without plans reads them from it."""
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zlib

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import bakepack  # noqa: E402
import grading  # noqa: E402
import plancache  # noqa: E402
import roads  # noqa: E402
from test_grading import FakeMD  # noqa: E402
from test_roads import features, FRAME  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "bake_formats.json")
PLANS_TOL_M = 1e-3                      # (machines agree to ~1e-6 m; a planning change moves heights far more)


def fixture():
    """A cross of streets, a motorway with a bridge over a street and a ramp."""
    return features([[(-400, 20), (400, 20)], [(0, -400), (0, 400)], [(-400, -150), (400, 120)],
                     [(150, -300), (150, 300)], [(-300, 200), (-100, 60)]],
                    ["residential", "secondary", "motorway", "tertiary", "motorway"],
                    flags=[(2, "is_bridge", 0.45, 0.6)])


def plans_heights(source=None):
    source = fixture() if source is None else source
    g = grading.Grader(FRAME, FakeMD(source), lambda la, lo: 10.0 + 0.002 * np.asarray(lo) * 0
                       + 0.0 * np.asarray(la))
    e, n = np.meshgrid(np.linspace(-380, 380, 77), np.linspace(-380, 380, 77))
    la, lo, _ = FRAME.enu_to_geodetic(e.ravel(), n.ravel(), 0.0)
    # At slope 0.01, the x=200 grid column sits exactly at WALL_CUT_M below the base. Last-bit differences in the
    # nominally 10 m plans then choose a wall or a slope (0.21 m apart on Windows). Keep the golden off that cutoff.
    return np.asarray(g.apply(la, lo, 10.0 + 0.0101 * e.ravel()), np.float64)


def pack_heights(z):
    """The heights in 0.1 mm steps, compressed, as text for the golden json."""
    return base64.b64encode(zlib.compress(np.round(z * 1e4).astype("<i4").tobytes(), 9)).decode("ascii")


def unpack_heights(s):
    return np.frombuffer(zlib.decompress(base64.b64decode(s)), "<i4").astype(np.float64) / 1e4


def heights_differ(z, want):
    """None when the heights match the golden ones, else what differs."""
    if want is None or len(want) != len(z):
        return "another grid"
    d = np.abs(z - want)
    n = int(np.count_nonzero(d > PLANS_TOL_M))
    return f"{n} of {len(z)} heights moved, up to {d.max():.4f} m" if n else None


def facts_digest():
    f = fixture()
    rng = [roads.level_ranges(f, i) for i in range(len(f))]
    specs = [None if sp is None else roads._spec_json(sp) for sp in roads.road_specs(f, None, None, False)]
    data = json.dumps({"ranges": [[[round(a, 6), round(b, 6), k] for a, b, k in r] for r in rng], "specs": specs},
                      sort_keys=True, default=str)
    return hashlib.sha1(data.encode("utf-8")).hexdigest()


class FormatTest(unittest.TestCase):
    def setUp(self):
        roads._FACTS_MEM.clear()
        self.addCleanup(roads._FACTS_MEM.clear)

    def check(self, name, fmt, value, key="digest", differ=None):
        """value: what the golden holds under key; differ(value, stored): None when they match, else how not."""
        differ = differ or (lambda a, b: None if a == b else f"{a} != {b}")
        got = json.load(open(GOLDEN)) if os.path.exists(GOLDEN) else {}
        want = got.get(name)
        why = None if want is None or key not in want else differ(value, want[key])
        if os.environ.get("BNGE_UPDATE_GOLDEN") and (want is None or want["format"] != fmt or key not in want or why):
            if want is not None and want["format"] == fmt and key in want and why:
                self.fail(f"{name}: the output changed ({why}): bump its format number first")
            got[name] = {"format": fmt, key: value}
            os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
            with open(GOLDEN, "w") as fh:
                json.dump(got, fh, indent=1, sort_keys=True)
            return
        self.assertIsNotNone(want, f"no golden {name}: run with BNGE_UPDATE_GOLDEN=1")
        if want["format"] != fmt or key not in want:
            self.fail(f"{name}: format {fmt} has no golden yet: run with BNGE_UPDATE_GOLDEN=1")
        self.assertIsNone(why, f"{name} output changed under format {fmt}: bump it (plancache.PLANS_FORMAT / "
                               f"roads.FACTS_FORMAT), then run with BNGE_UPDATE_GOLDEN=1; baked packs of the old "
                               f"format are then ignored")

    def test_plans_format(self):
        z = plans_heights()
        self.check("plans", plancache.PLANS_FORMAT, pack_heights(z), "heights",
                   lambda _a, b: heights_differ(z, unpack_heights(b)))

    def test_plans_compare_within_a_millimetre(self):
        z = plans_heights()
        want = unpack_heights(pack_heights(z))
        self.assertIsNone(heights_differ(z, want), "rounded to 0.1 mm: the same")
        self.assertIsNone(heights_differ(z + 1e-6, want), "another machine's last bits: the same")
        moved = z.copy()
        moved[len(z) // 2] += 0.01
        self.assertIn("1 of", heights_differ(moved, want) or "", "one height 1 cm off: changed")

    def test_plans_golden_survives_coordinate_roundoff(self):
        with open(GOLDEN) as fh:
            want = unpack_heights(json.load(fh)["plans"]["heights"])
        for axis, direction in ((0, np.inf), (0, -np.inf), (1, np.inf), (1, -np.inf)):
            with self.subTest(axis=axis, direction=direction):
                f = fixture()
                f.lonlat[:, axis] = np.nextafter(f.lonlat[:, axis], direction)
                self.assertIsNone(heights_differ(plans_heights(f), want),
                                  "one-ULP coordinate noise must not switch the golden fixture's wall classification")

    def test_facts_format(self):
        self.check("facts", roads.FACTS_FORMAT, facts_digest())


class PackTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_round_trip_and_meta(self):
        os.makedirs(os.path.join(self.dir, bakepack.SUBDIR))
        w = bakepack.Writer(os.path.join(self.dir, bakepack.SUBDIR, "t" + bakepack.SUFFIX))
        w.add("a", b"hello")
        w.add("b", b"")
        w.add("c", bytes(range(256)))
        self.assertEqual(w.close({"bake_format": bakepack.BAKE_FORMAT, "x": 1}), 3)
        self.assertEqual(bakepack.find(self.dir, "a", {"x": 1}), b"hello")
        self.assertEqual(bakepack.find(self.dir, "c", {"x": 1}), bytes(range(256)))
        self.assertEqual(bakepack.find(self.dir, "b", {"x": 1}), b"")
        self.assertIsNone(bakepack.find(self.dir, "a", {"x": 2}), "another meta: not read")
        self.assertIsNone(bakepack.find(self.dir, "zz", {"x": 1}))

    def test_a_run_reads_baked_plans(self):
        f = fixture()
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        old = os.environ.get(plancache.RUN_ENV)
        self.addCleanup(lambda: os.environ.__setitem__(plancache.RUN_ENV, old) if old is not None
                        else os.environ.pop(plancache.RUN_ENV, None))
        bake = os.path.join(self.dir, "bake_run")
        os.environ[plancache.RUN_ENV] = "bake"
        g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: 10.0 + 0.0 * np.asarray(la))
        g.shared = plancache.for_grader(bake, g, None, None)
        za = g.apply(la, lo, np.zeros(50))
        files = []
        for root, _d, names in os.walk(os.path.join(bake, "plans", "bake")):
            files += [os.path.join(root, n) for n in names if n.endswith(".npz")]
        self.assertTrue(files, "planned")
        cache = os.path.join(self.dir, "player")
        os.makedirs(os.path.join(cache, bakepack.SUBDIR))
        w = bakepack.Writer(os.path.join(cache, bakepack.SUBDIR, "p" + bakepack.SUFFIX))
        sig = g.shared.signature
        for p in files:
            key = tuple(int(v) for v in os.path.basename(p)[:-4].split("_"))
            w.add(bakepack.plans_key(sig, key), open(p, "rb").read())
        w.close({**g.shared._bake_expect()})
        os.environ[plancache.RUN_ENV] = "player"         # (a player's first visit: nothing planned, the pack)
        md = FakeMD(f)
        b = grading.Grader(FRAME, md, lambda la, lo: 10.0 + 0.0 * np.asarray(la))
        b.shared = plancache.for_grader(cache, b, None, None)
        self.assertTrue(b.shared.has(key))
        zb = b.apply(la, lo, np.zeros(50))
        self.assertTrue(np.array_equal(za, zb), "the same heights")
        self.assertGreater(b.shared.stats.get("baked", 0), 0, "read from the pack")


class SyncTest(unittest.TestCase):
    """bakepack.sync: the release's packs near the player and made for this server, fetched once."""

    def test_near_and_matching_only(self):
        from unittest import mock
        src = tempfile.mkdtemp()
        cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, True)
        self.addCleanup(shutil.rmtree, cache, True)

        class MD:
            def versions(self):
                return {"overture": "r1", "lanes": "l1"}
        md = MD()
        good = dict(bakepack._expect_for(md, "w1s1"))
        packs = []
        for name, bbox, meta in (("near.bnge", [40.7, -74.0, 40.8, -73.9], good),
                                 ("far.bnge", [34.0, -118.5, 34.1, -118.4], good),
                                 ("old.bnge", [40.7, -74.0, 40.8, -73.9], {**good, "plans_format": -1})):
            w = bakepack.Writer(os.path.join(src, name))
            w.add("x", b"y")
            w.close({**meta, "bbox": bbox})
            packs.append(name)
        with open(os.path.join(src, "index.json"), "w") as fh:
            json.dump(bakepack.index_of(src), fh)
        fetched = []

        def get(net, url, path, cap):
            fetched.append(os.path.basename(url))
            shutil.copy(os.path.join(src, os.path.basename(url)), path)
        bakepack._sync.update(at=-1e9, busy=False)
        with mock.patch.object(bakepack, "_get", get):
            got = bakepack.sync(cache, object(), md, 40.75, -73.95, "w1s1", now=0.0)
            self.assertEqual(got, ["near.bnge"])
            self.assertEqual(bakepack.sync(cache, object(), md, 40.75, -73.95, "w1s1", now=1.0), [], "not again soon")
            bakepack._sync["at"] = -1e9
            self.assertEqual(bakepack.sync(cache, object(), md, 40.75, -73.95, "w1s1", now=0.0), [], "had it")
        self.assertEqual(fetched, ["index.json", "near.bnge"])
        self.assertEqual(bakepack.find(cache, "x", good), b"y")


if __name__ == "__main__":
    unittest.main()
