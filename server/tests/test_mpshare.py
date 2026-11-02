"""mpshare.py (BeamMP shared world generation v2, a player's app's side): hello, the uploads of what the road processes
kept, the prefetch of what is not here, and what comes in kept where the road processes read it, the checks an
untrusted server or player cannot pass. The plugin is a fake here (its own tests: mod/tests/test_beammp_share.lua)."""
import base64
import io
import json
import os
import pickle
import shutil
import sys
import tempfile
import time
import unittest
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mpshare  # noqa: E402
import plancache  # noqa: E402
import revisit  # noqa: E402

VERSIONS = {"overture": "2026-09-23.1", "lanes": "l1"}


def plan_store(cache, run="r1", versions=VERSIONS):
    pc = plancache.PlanCache(os.path.join(cache, "plans", run), "w1s1")
    pc.persist = (cache, (plancache.FRAMES, "w1s1", versions))
    return pc


class FakePlugin:
    """The plugin's store and serving in Python: uploads kept by id, wants answered or missed."""

    def __init__(self):
        self.store, self.ups, self.hellos = {}, {}, []

    def take(self, hub, msgs):
        """The hub's share_out messages: what the plugin does with each; its answers back into hub."""
        for m in msgs:
            assert m["type"] == "share_out" and m["to"] == "S"
            d = json.loads(m["d"])
            if d["t"] == "hello":
                self.hellos.append(d)
            elif d["t"] == "c":
                u = self.ups.setdefault(d["id"], {})
                u[d["i"]] = d
                if len(u) == d["n"]:
                    self.store[d["iid"]] = [u[i]["d"] for i in range(d["n"])]
            elif d["t"] == "w":
                missed = []
                for it in d["items"]:
                    chunks = self.store.get(it["iid"])
                    if chunks is None:
                        missed.append(it["iid"])
                        continue
                    for i, c in enumerate(chunks):
                        hub.on_data("S|" + json.dumps({"t": "c", "id": "s" + it["iid"][:6], "i": i, "n": len(chunks),
                                                       "iid": it["iid"], "key": it["key"], "c": it["c"], "d": c}))
                if missed:
                    hub.on_data("S|" + json.dumps({"t": "m", "items": missed}))


class HubTest(unittest.TestCase):
    def setUp(self):
        self.dirs = [tempfile.mkdtemp(), tempfile.mkdtemp()]
        for d in self.dirs:
            self.addCleanup(shutil.rmtree, d, True)
        self.wire = [[], []]
        self.hubs = [mpshare.Hub(self.wire[n].append, self.dirs[n], lambda n=n: plan_store(self.dirs[n]))
                     for n in range(2)]
        self.plugin = FakePlugin()
        old = revisit.ON
        self.addCleanup(setattr, revisit, "ON", old)
        revisit.ON = True

    def flush(self, n):
        h = self.hubs[n]
        while h.out:
            h.tokens = mpshare.BURST
            h.pump()
        msgs, self.wire[n][:] = list(self.wire[n]), []
        self.plugin.take(h, msgs)

    def build(self, n, cell):
        """Cell's plans planned and kept by player n's road processes (PlanCache.store persist)."""
        pc = plan_store(self.dirs[n])
        self.assertTrue(pc.store(cell, [], [], [], [], persist=True))
        return pc._kept(cell).read()

    def test_hello_once_the_versions_are_known(self):
        late = mpshare.Hub(lambda m: None, self.dirs[0], lambda: plan_store(self.dirs[0], versions={"lanes": None}))
        self.assertFalse(late.hello(), "the weekly tags' versions not known yet: no hello")
        h = self.hubs[0]
        self.assertTrue(h.hello())
        self.flush(0)
        (hello,) = self.plugin.hellos
        expect = plan_store(self.dirs[0])._bake_expect()
        self.assertEqual(hello["codes"]["plans"], mpshare.expect_code(expect))
        self.assertEqual(hello["expects"][hello["codes"]["plans"]], json.loads(json.dumps(expect)))
        self.assertTrue(h.hello())
        self.flush(0)
        self.assertEqual(len(self.plugin.hellos), 1, "the same expect: not again")

    def test_a_cell_built_by_one_player_reaches_the_other(self):
        a, b = self.hubs
        for h in (a, b):
            h.hello()
        cell = (14, 4824, 6159)
        data = self.build(0, cell)
        self.assertEqual(os.listdir(os.path.join(self.dirs[0], "share", "out")), ["plans_w1s1_14_4824_6159"],
                         "noted while the session shares")
        self.assertEqual(a.upload_built(), 1)
        self.flush(0)
        self.assertEqual(os.listdir(os.path.join(self.dirs[0], "share", "out")), [])
        iid = mpshare.item_id(a.code, "plans/w1s1/14_4824_6159")
        self.assertIn(iid, self.plugin.store, "uploaded to the server")
        la, lo = __import__("mapcells").CellKey(*cell).center()
        b.prefetch(la, lo)
        self.flush(1)
        got = plan_store(self.dirs[1], run="other")._kept(cell)
        self.assertIsNotNone(got, "kept where the other's road processes read it")
        self.assertEqual(got.read(), data)
        self.assertEqual(b.stats["kept"], 1)
        self.assertTrue(plan_store(self.dirs[1], run="other").has(cell))

    def test_prefetch_asks_only_for_what_is_missing_and_not_again_soon(self):
        b = self.hubs[1]
        b.hello()
        self.flush(1)
        here = (14, 4824, 6159)
        self.build(1, here)
        la, lo = __import__("mapcells").CellKey(*here).center()
        t0 = time.monotonic()
        n = b.prefetch(la, lo, now=t0)
        self.assertGreater(n, 4)
        asked = {v[1] for v in b.wanted.values()}
        self.assertNotIn(here, asked, "had: not asked")
        self.flush(1)
        self.assertEqual(b.stats["missed"], n, "the server had none")
        self.assertEqual(b.prefetch(la, lo, now=t0 + 5.0), 0, "missed just now: not asked again")
        self.assertEqual(b.prefetch(la, lo, now=time.monotonic() + mpshare.ASK_AGAIN_S + 1), n, "later: again")

    def test_ahead_of_the_car(self):
        b = self.hubs[1]
        b.hello()
        la, lo = 40.75, -73.98
        n0 = b.prefetch(la, lo, 0.0, 0.0, now=0.0)
        b.wanted.clear()
        n1 = b.prefetch(la, lo, 0.0, 60.0, now=0.0)       # (60 m/s north: 1.2 km ahead in AHEAD_S)
        self.assertGreater(n1, n0, "the cells ahead too")

    def test_what_is_refused(self):
        b = self.hubs[1]
        b.hello()
        cell = (14, 4824, 6159)
        key = "plans/w1s1/14_4824_6159"
        iid = mpshare.item_id(b.code, key)

        def offer(data, wanted=True, key_=key):
            if wanted:
                b.wanted[iid] = (key_, cell, time.monotonic())
            b.on_data("S|" + json.dumps({"t": "c", "id": "x" + str(len(data)), "i": 0, "n": 1, "iid": iid, "key": key,
                                         "c": b.code, "d": base64.b64encode(data).decode()}))
        good = self.build(0, cell)
        offer(good, wanted=False)
        self.assertEqual(b.stats["unasked"], 1, "not asked for: not taken")
        offer(b"not an npz")
        bad = io.BytesIO()
        np.savez(bad, meta=np.frombuffer(json.dumps({"plans": []}).encode(), np.uint8),
                 evil=np.array([object()], dtype=object))
        offer(bad.getvalue())
        offer(pickle.dumps({"plans": []}))
        offer(good, key_="plans/w0s1/14_4824_6159")      # (another signature than ours)
        self.assertEqual(b.stats["refused"], 4)
        self.assertIsNone(plan_store(self.dirs[1])._kept(cell))
        offer(good)
        self.assertIsNotNone(plan_store(self.dirs[1])._kept(cell), "and the real one is")

    def test_not_noted_outside_a_session(self):
        self.hubs[0].close()
        self.build(0, (14, 1, 2))
        self.assertEqual(os.listdir(os.path.join(self.dirs[0], "share", "out")), [])

    def test_paced(self):
        a = self.hubs[0]
        a.hello()
        a.send_item("plans/w1s1/14_1_2", os.urandom(10 * mpshare.CHUNK))
        a.tokens, a.t_tokens = 0.0, time.monotonic()
        a.out.popleft()                                     # (the hello)
        wait = a.pump()
        self.assertEqual(self.wire[0], [], "an empty bucket: nothing yet")
        self.assertGreater(wait, 0.0)


class OwnerTest(unittest.TestCase):
    def test_stable_and_weighted(self):
        two = [{"id": "1", "w": 1.0}, {"id": "2", "w": 1.0}]
        keys = [f"k{i}" for i in range(4000)]
        got = Counter(mpshare.owner(two, k) for k in keys)
        self.assertLess(abs(got["1"] - got["2"]), 300, "about half each")
        three = two + [{"id": "3", "w": 2.0}]
        moved = [k for k in keys if mpshare.owner(three, k) not in (mpshare.owner(two, k), "3")]
        self.assertEqual(moved, [], "a joiner takes items only for itself")
        self.assertIsNone(mpshare.owner([], "k"))


if __name__ == "__main__":
    unittest.main()
