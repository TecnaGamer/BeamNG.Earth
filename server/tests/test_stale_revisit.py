"""An older version shown while the current one is built (revisit.stale, blockcache.Build.stale_hit, the trackers'
redo; earthd's stale mode): after an update or a format bump (here revisit.SIM_BUMP) a stale scope finds the items
the generation before kept, whole and of that generation alone; items of another format, of other build switches or
too old are not found; nothing is read so with the switch off; the swap sends again only what changed."""
import json
import os
import pickle
import shutil
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, wait
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blockcache  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import heightmap  # noqa: E402
import revisit  # noqa: E402
import revisitpack  # noqa: E402
import roads  # noqa: E402
import vegetation  # noqa: E402


class _Bump:
    """with _Bump(tag): the process keys as after an update tagged tag (another code key, other format markers, another
    block code id); the keys of before come back after."""

    def __init__(self, tag):
        self.tag = tag

    def _reset(self):
        revisit._code.clear()
        revisit._switches.clear()
        blockcache._code.clear()

    def __enter__(self):
        self.old = revisit.SIM_BUMP
        revisit.SIM_BUMP = self.tag
        self._reset()
        return self

    def __exit__(self, *exc):
        revisit.SIM_BUMP = self.old
        self._reset()
        return False


def _job(d, parts):
    """A pool job's stand-in: an item's lookup by its key."""
    k = revisit.key("roadsg", *parts)
    got = revisit.load(d, "roadsg", k)
    return None if got is None else {"data": got[0]["a"]}


class GenerationsTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bnge_stale_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.addCleanup(revisitpack.reset)
        self.addCleanup(lambda: setattr(revisit._tl, "stale", None))

    def test_a_bump_is_another_generation_and_the_last_one_is_older(self):
        g1 = revisit.register(self.d, now=1000.0)
        self.assertEqual(revisit.older(self.d, now=1001.0), [])
        with _Bump("b"):
            g2 = revisit.generation()
            self.assertNotEqual(g1["id"], g2["id"])
            self.assertNotEqual(g1["code"], g2["code"])
            self.assertNotEqual(g1["block"], g2["block"])
            old = revisit.older(self.d, now=2000.0)
            self.assertEqual([g["id"] for g in old], [g1["id"]])
            revisit.register(self.d, now=2000.0)
            self.assertEqual([g["id"] for g in revisit.older(self.d, now=2001.0)], [g1["id"]])
        # back on the first: the bumped one is the older one now, the first is not its own stand-in
        self.assertEqual([g["id"] for g in revisit.older(self.d, now=2002.0)], [g2["id"]])

    def test_bounded_by_age_number_and_switches(self):
        revisit.register(self.d, now=0.0)
        ids = []
        for i, tag in enumerate("abcd"):
            with _Bump(tag):
                ids.append(revisit.register(self.d, now=100.0 * (i + 1))["id"])
        with _Bump("e"):
            got = revisit.older(self.d, now=500.0)
            self.assertEqual([g["id"] for g in got], ids[:1:-1], "the newest STALE_GENS, newest first")
            self.assertEqual(len(got), revisit.STALE_GENS)
            # too old (BNE_STALE_DAYS): none
            day = 86400.0
            self.assertEqual(revisit.older(self.d, now=400.0 + revisit.stale_days() * day + 1.0), [])
            with mock.patch.dict(os.environ, {"BNE_STALE_DAYS": "0.001"}):
                self.assertEqual(revisit.older(self.d, now=600.0), [])
            # the file keeps GENS_KEPT at most
            for i in range(10):
                with _Bump(f"x{i}"):
                    revisit.register(self.d, now=1000.0 + i)
            self.assertLessEqual(len(revisit._read_gens(self.d)), revisit.GENS_KEPT)
        # other build switches: other content, never a stand-in
        with mock.patch.dict(os.environ, {"BNE_SOME_SWITCH": "1"}), _Bump("f"):
            self.assertEqual(revisit.older(self.d, now=1010.0), [])
            # (tests only: a real change made with a switch, swapped in as an update's would be)
            with mock.patch.dict(os.environ, {"BNGE_STALE_ANY_SWITCHES": "1"}):
                self.assertTrue(revisit.older(self.d, now=1010.0))

    def test_off_switch_and_not_a_build_switch(self):
        revisit.register(self.d, now=0.0)
        with _Bump("b"):
            self.assertTrue(revisit.older(self.d, now=1.0))
            with mock.patch.object(revisit, "STALE_ON", False):
                self.assertEqual(revisit.older(self.d, now=1.0), [])
            with mock.patch.dict(os.environ, {"BNGE_WORLD_BUNDLE": "/x"}):
                self.assertEqual(revisit.older(self.d, now=1.0), [])
        self.assertIn("BNE_STALE_REVISIT", revisit.NOT_SWITCHES)
        self.assertIn("BNE_STALE_DAYS", revisit.NOT_SWITCHES)
        k = revisit.switches_key()
        with mock.patch.dict(os.environ, {"BNE_STALE_REVISIT": "off", "BNE_STALE_DAYS": "3"}):
            revisit._switches.clear()
            self.assertEqual(revisit.switches_key(), k)
        revisit._switches.clear()

    def test_a_damaged_file_is_no_generations(self):
        os.makedirs(os.path.join(self.d, revisit.SUBDIR))
        with open(revisit._gens_path(self.d), "w") as f:
            f.write("{not json")
        self.assertEqual(revisit.older(self.d), [])
        with open(revisit._gens_path(self.d), "w") as f:
            json.dump([{"id": 1}, "x", {"id": "a", "code": "c"}], f)
        self.assertEqual(revisit.older(self.d), [])
        self.assertTrue(revisit.register(self.d)["id"])


class StaleLookupTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bnge_stale_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.addCleanup(revisitpack.reset)
        self.addCleanup(lambda: setattr(revisit._tl, "stale", None))
        self.g1 = revisit.register(self.d, now=0.0)
        self.parts = ("g16_1_2", 15, True)
        self.k1 = revisit.key("roadsg", *self.parts)
        self.assertTrue(revisit.store(self.d, "roadsg", self.k1, {"a": b"old content"}, {"n": 1}))
        self.s1 = revisit.key("rskin", self.k1, 39.28, -76.59)
        self.assertTrue(revisit.store(self.d, "rskin", self.s1, {"a": b"old skin"}))

    def test_the_older_item_is_found_only_in_a_stale_scope(self):
        with _Bump("b"):
            old = revisit.older(self.d, now=1.0)
            k2 = revisit.key("roadsg", *self.parts)
            self.assertNotEqual(k2, self.k1)
            self.assertIsNone(revisit.load(self.d, "roadsg", k2), "outside a scope: a miss, as before")
            self.assertFalse(revisit.has(self.d, "roadsg", k2))
            with revisit.stale(old):
                k2 = revisit.key("roadsg", *self.parts)
                self.assertTrue(revisit.has(self.d, "roadsg", k2))
                got = revisit.load(self.d, "roadsg", k2)
                self.assertEqual(got[0]["a"], b"old content")
                self.assertEqual(revisit.take_stale_hit(), self.g1["id"])
                self.assertIsNone(revisit.take_stale_hit())
                # an item keyed by another's key (a skin by its content's): that generation's content key in it
                s2 = revisit.key("rskin", k2, 39.28, -76.59)
                self.assertEqual(revisit.load(self.d, "rskin", s2)[0]["a"], b"old skin")
                # another place's: in neither
                k3 = revisit.key("roadsg", "g16_9_9", 15, True)
                self.assertIsNone(revisit.load(self.d, "roadsg", k3))
            self.assertIsNone(revisit.load(self.d, "roadsg", k2), "the scope ended")
            # nothing was kept under the current key by the lookups
            self.assertFalse(revisit._has(self.d, "roadsg", k2))

    def test_only_reads_one_generation_and_never_builds(self):
        with _Bump("b"):
            old = revisit.older(self.d, now=1.0)
            k2 = revisit.key("roadsg", *self.parts)
            self.assertTrue(revisit.store(self.d, "roadsg", k2, {"a": b"new content"}))
            with revisit.stale(old):            # (not only: the current one first)
                k2 = revisit.key("roadsg", *self.parts)
                self.assertEqual(revisit.load(self.d, "roadsg", k2)[0]["a"], b"new content")
            with revisit.stale(old, only=True):
                # the group shown is one generation: the older content, although the current one is there too
                k2 = revisit.key("roadsg", *self.parts)
                self.assertEqual(revisit.load(self.d, "roadsg", k2)[0]["a"], b"old content")
                self.assertTrue(revisit.stale_only())
                k3 = revisit.key("roadsg", "g16_9_9", 15, True)
                self.assertTrue(revisit.store(self.d, "roadsg", k3, {"a": b"new elsewhere"}))
                with self.assertRaises(revisit.StaleMiss):
                    revisit.load(self.d, "roadsg", k3)      # (this generation's alone: not the group's)
                # kinds never read from an older generation are read as ever (the plans, the cell facts)
                kp = revisit.format_key("plans", "1.1", "cell")
                self.assertIsNone(revisit.load(self.d, "plans", kp))
            self.assertFalse(revisit.stale_only())

    def test_plans_and_facts_are_never_aliased(self):
        kp1 = revisit.format_key("plans", "65.26", "cell")
        self.assertTrue(revisit.store(self.d, "plans", kp1, {"plans.npz": b"old plans"}))
        with _Bump("b"):
            old = revisit.older(self.d, now=1.0)
            with revisit.stale(old):
                kp2 = revisit.format_key("plans", "65.26", "cell")
                self.assertNotEqual(kp1, kp2, "a bump: other plans")
                self.assertIsNone(revisit.load(self.d, "plans", kp2), "nothing is built from older plans")
                self.assertFalse(revisit.has(self.d, "plans", kp2))
        self.assertNotIn("plans", revisit.STALE_KINDS)
        self.assertNotIn("cellfacts", revisit.STALE_KINDS)

    def test_an_unreadable_format_is_skipped(self):
        with _Bump("b"):
            old = revisit.older(self.d, now=1.0)
            with mock.patch.dict(revisit.READ_FORMAT, {"roadsg": revisit.READ_FORMAT["roadsg"] + 1}):
                self.assertFalse(revisit.readable(old[0], "roadsg"))
                self.assertTrue(revisit.readable(old[0], "rskin"))
                with revisit.stale(old):
                    k2 = revisit.key("roadsg", *self.parts)
                    self.assertIsNone(revisit.load(self.d, "roadsg", k2))
                with revisit.stale(old, only=True):
                    k2 = revisit.key("roadsg", *self.parts)
                    with self.assertRaises(revisit.StaleMiss):
                        revisit.load(self.d, "roadsg", k2)
            # the mod reads another tile format: none of the older tiles
            with mock.patch.object(revisit, "mod_version", lambda: -1):
                self.assertFalse(revisit.readable(old[0], "rskin"))
                self.assertFalse(revisit.readable(old[0], "block"))

    def test_a_job_in_a_scope_across_a_pickle(self):
        with _Bump("b"):
            old = revisit.older(self.d, now=1.0)
            job = pickle.loads(pickle.dumps(revisit.StaleJob(_job, old, only=True)))
            got = job(self.d, self.parts)
            self.assertEqual(got, {"data": b"old content", "stale": self.g1["id"]})
            self.assertIsNone(job(self.d, ("g16_9_9", 15, True)), "a miss is 'not at hand yet'")
            with self.assertRaises(revisit.StaleMiss):
                revisit.StaleJob(_job, old, only=True, raises=True)(self.d, ("g16_9_9", 15, True))
            self.assertIsNone(_job(self.d, self.parts), "the job's scope ended with it")
            # another thread is not in the scope
            with revisit.stale(old, only=True):
                with ThreadPoolExecutor(1) as ex:
                    self.assertIsNone(ex.submit(_job, self.d, self.parts).result())


def _elev(lat, lon):
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    return 5.0 + 30.0 * np.sin(lat * 900.0) * np.cos(lon * 700.0) + 1000.0 * (lat - 39.28)


@unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= blockcache.NATIVE, "earthfast 269")
class StaleBlockTest(unittest.TestCase):
    """A terrain block of the generation before: read whole (its entry and every chunk of that generation), written as
    that generation wrote it, the anchor's chunk memory untouched."""

    def setUp(self):
        from test_blockcache import _Built, _files
        self._Built, self._files = _Built, _files
        self.tmp = tempfile.mkdtemp(prefix="bnge_staleblock_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(lambda: setattr(revisit._tl, "stale", None))
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(self.cache)
        self.frame = geodesy.EnuFrame(39.28, -76.59)
        self.q = 1 / 512
        env = mock.patch.dict(os.environ, {"BNGE_BLOCKCACHE": "on"})
        env.start()
        self.addCleanup(env.stop)

    def out(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    def test_the_older_block_comes_back_whole_and_alone(self):
        revisit.register(self.cache, now=0.0)
        first = self._Built(self, self.cache, heightmap.ChunkCache(), 0.0, 0.0, self.out("a"), holes=True)
        self.assertTrue(first.kept)
        want = self._files(self.out("a"), first.info, 0, 0)
        with _Bump("b"):
            old = revisit.older(self.cache, now=1.0)
            self.assertEqual(len(old), 1)
            # outside a scope: a miss (and the test's build keeps this generation's block)
            chunks = heightmap.ChunkCache()
            bc = blockcache.begin(self.cache, self.frame, 16, 0.0, 0.0, first.bc.size, self.q, ("test",), None, None,
                                  chunks)
            self.assertNotEqual(bc.block_key, first.bc.block_key)
            self.assertEqual(bc.latest()["block"], first.bc.block_key.hex(), "the place's note: the key it had then")
            self.assertFalse(bc.latest_is_current())
            self.assertIsNone(bc.hit(self.out("m"), "hm_0_0.png", "hm_0_0.ter", (0.0, 0.0), True))
            revisit.end(bc.scope)
            # in a scope: the older block, byte for byte
            with revisit.stale(old, only=True):
                bc = blockcache.begin(self.cache, self.frame, 16, 0.0, 0.0, first.bc.size, self.q, ("test",), None,
                                      None, chunks)
                info = bc.hit(self.out("s"), "hm_0_0.png", "hm_0_0.ter", (0.0, 0.0), True)
                revisit.end(bc.scope)
            self.assertEqual(info.pop("stale"), old[0]["id"])
            self.assertEqual(info, first.info)
            got = self._files(self.out("s"), info, 0, 0)
            self.assertEqual(sorted(got), sorted(want))
            for n in want:
                self.assertEqual(got[n], want[n], n)
            # the anchor's chunk memory has none of the older heights: the current build computes its own
            ks = [k for _, _, k in heightmap.block_chunks(0.0, 0.0, first.bc.size)]
            self.assertTrue(all(chunks.get(k) is None for k in ks))
            # a neighbour the older generation never built: not in the group (nothing built in the scope)
            with revisit.stale(old, only=True):
                bc = blockcache.begin(self.cache, self.frame, 16, 128.0 * heightmap.SQUARE * 0 + 1024.0, 0.0,
                                      first.bc.size, self.q, ("test",), None, None, chunks)
                with self.assertRaises(revisit.StaleMiss):
                    bc.hit(self.out("n"), "hm_n.png", "hm_n.ter", (0.0, 0.0), True)
            # this generation's own block, once kept, is not what a stale-only scope reads: the place has none of
            # the older generation any more (its note names the last block kept), so the scope has a miss
            cur = self._Built(self, self.cache, heightmap.ChunkCache(), 0.0, 0.0, self.out("c"))
            self.assertTrue(cur.kept)
            with revisit.stale(old, only=True):
                bc = blockcache.begin(self.cache, self.frame, 16, 0.0, 0.0, first.bc.size, self.q, ("test",), None,
                                      None, heightmap.ChunkCache())
                with self.assertRaises(revisit.StaleMiss):
                    bc.hit(self.out("s2"), "hm_0_0.png", "hm_0_0.ter", (0.0, 0.0), True)
            bc = blockcache.begin(self.cache, self.frame, 16, 0.0, 0.0, first.bc.size, self.q, ("test",), None, None,
                                  heightmap.ChunkCache())
            self.assertTrue(bc.latest_is_current())
            self.assertIsNone(bc.stale_hit(old[0]))
            revisit.end(bc.scope)

    def test_the_older_block_is_found_whatever_pads_this_run_knows(self):
        """A block is kept under the pads its session ended with; the next run's first build of it knows none yet: the
        place's note finds it all the same."""
        from test_blockcache import _Pads
        revisit.register(self.cache, now=0.0)
        surf = _Pads()
        surf.pads.append(("fuel:1", 39.2799, 39.2801, -76.5901, -76.5899))
        first = self._Built(self, self.cache, heightmap.ChunkCache(), 0.0, 0.0, self.out("a"), surface=surf)
        self.assertTrue(first.kept)
        with _Bump("b"):
            old = revisit.older(self.cache, now=1.0)
            with revisit.stale(old, only=True):
                bc = blockcache.begin(self.cache, self.frame, 16, 0.0, 0.0, first.bc.size, self.q, ("test",), None,
                                      _Pads(), heightmap.ChunkCache())
                info = bc.hit(self.out("s"), "hm_0_0.png", "hm_0_0.ter", (0.0, 0.0), True)
                revisit.end(bc.scope)
            self.assertEqual(info.get("stale"), old[0]["id"])
            self.assertEqual(self._files(self.out("s"), info, 0, 0), self._files(self.out("a"), first.info, 0, 0))
        # the notes go when unused for long
        self.assertEqual(blockcache.prune_latest(self.cache, now=__import__("time").time() + 10 * 86400.0), 0)
        self.assertEqual(blockcache.prune_latest(self.cache, now=__import__("time").time() + 100 * 86400.0), 1)

    def test_a_block_with_a_chunk_gone_or_of_another_format_is_no_hit(self):
        revisit.register(self.cache, now=0.0)
        first = self._Built(self, self.cache, heightmap.ChunkCache(), 0.0, 0.0, self.out("a"))
        self.assertTrue(first.kept)
        with _Bump("b"):
            old = revisit.older(self.cache, now=1.0)
            bc = blockcache.begin(self.cache, self.frame, 16, 0.0, 0.0, first.bc.size, self.q, ("test",), None, None,
                                  heightmap.ChunkCache())
            self.assertIsNotNone(bc.stale_hit(old[0]))
            with mock.patch.dict(revisit.READ_FORMAT, {"block": 99}):
                self.assertIsNone(bc.stale_hit(old[0]), "a format this code cannot read")
            with mock.patch.object(blockcache, "FORMAT", blockcache.FORMAT + 1):
                bc2 = blockcache.begin(self.cache, self.frame, 16, 0.0, 0.0, first.bc.size, self.q, ("test",), None,
                                       None, heightmap.ChunkCache())
                self.assertIsNone(bc2.stale_hit(old[0]), "the files' own format changed")
                revisit.end(bc2.scope)
            # another generation than the one asked for: not that one's block
            self.assertIsNone(bc.stale_hit(dict(old[0], block="0" * 64)))
            self.assertIsNone(bc.stale_hit(dict(old[0], plans="1.1")))
            # one chunk file removed: the block is not whole, so it is not shown
            h = bc.latest()["chunks"][5]
            os.remove(os.path.join(blockcache.folder(self.cache), h[:2], h + ".h"))
            self.assertIsNone(bc.stale_hit(old[0]))
            revisit.end(bc.scope)


class ServerRulesTest(unittest.TestCase):
    """earthd's stale mode is never on in a BeamMP session (the players would stand on different ground), with the
    switch off, or without an older generation."""

    def test_not_in_a_shared_session(self):
        import types
        import earthd
        s = types.SimpleNamespace(grading=True, mapdata=object(), road_pool=object(), _share_hub=None,
                                  _stale_older=({"id": "x"},), cache_dir="/nowhere")
        self.assertTrue(earthd.Server.stale_allowed(s))
        s._share_hub = object()             # (share_hello: a BeamMP session)
        self.assertFalse(earthd.Server.stale_allowed(s))
        s._share_hub = None
        with mock.patch.object(revisit, "STALE_ON", False):
            self.assertFalse(earthd.Server.stale_allowed(s))
        s._stale_older = ()
        self.assertFalse(earthd.Server.stale_allowed(s))
        # an anchor without the probe's verdict is the current version's
        a = types.SimpleNamespace()
        self.assertEqual(earthd.Server.stale_of(s, a), ())
        a.stale = ({"id": "x"},)
        self.assertEqual(earthd.Server.stale_of(s, a), ({"id": "x"},))

    def test_the_shared_items_are_never_an_older_generations(self):
        # (mpshare's items: plans and cell facts, by their format keys; no stale scope reads those kinds)
        self.assertFalse({"plans", "cellfacts"} & set(revisit.STALE_KINDS))


class StaleRunTest(unittest.TestCase):
    """earthd Connection.stale_run with stand-ins: while the anchor is shown from the older version the current one's
    road geotiles are built behind (after the older ones are all shown, behind the live work), and nothing is sent
    again; then ONE swap is asked for (stale_swap); one that fails leaves the older version for the anchor (held),
    never released later."""

    def run_case(self, missing=(), near_missing=False, settle=True, hub_after=None, leave_after=None,
                 fail=(), ticks=60, swap_ok=True, active=7):
        import asyncio
        import functools
        import types
        import earthd
        import geotiles
        frame = geodesy.EnuFrame(39.2834, -76.5934)
        near = [(a, b) for a, b, _d in geotiles.tiles_near(frame, 0.0, 0.0, 500.0)]
        far = [(a, b) for a, b, d in geotiles.tiles_near(frame, 0.0, 0.0, 900.0) if d > 500.0]
        events, calls = [], []
        gen = {"id": "old", "plans": "1.1"}
        anchor = types.SimpleNamespace(id=7, frame=frame, stale=(gen,), stale_missing=set(), stale_why=None,
                                       stale_held=False)
        sent = set(near)
        if missing:
            miss = set(far[:2]) if not near_missing else {near[0]}
            sent -= miss
            anchor.stale_missing |= miss
        roads_tr = types.SimpleNamespace(geo=True, sent=sent, car=set(near), aid=7 if settle else 3)
        tick = [0]

        def prebuild(a, gx, gy, low):
            calls.append(((gx, gy), low, bool(a.stale)))
            return (gx, gy) not in fail

        from concurrent.futures import ThreadPoolExecutor as TPE
        ex = TPE(2)
        self.addCleanup(ex.shutdown)
        server = types.SimpleNamespace(is_abandoned=lambda aid: leave_after is not None and tick[0] >= leave_after,
                                       stale_exec=lambda: ex, stale_prebuild=prebuild, root=tempfile.gettempdir(),
                                       _share_hub=None)
        conn = types.SimpleNamespace(s=server, active=active, preparing_aid=7 if active != 7 else None, closing=False,
                                     roads=roads_tr, focus=(0.0, 0.0))
        conn.stale_keys = functools.partial(earthd.Connection.stale_keys, conn)

        async def swap(a, g, t0):
            events.append(("swap", bool(a.stale), len(calls)))
            if swap_ok:
                a.stale = ()
            return swap_ok
        conn.stale_swap = swap
        real_sleep = asyncio.sleep

        async def fast_sleep(_s):
            tick[0] += 1
            if hub_after is not None and tick[0] >= hub_after:
                server._share_hub = object()
            if tick[0] > ticks:
                conn.closing = True
            await real_sleep(0.01)

        async def go():
            with mock.patch.object(earthd.asyncio, "sleep", fast_sleep):
                await earthd.Connection.stale_run(conn, anchor)
        asyncio.run(go())
        return anchor, events, calls, near, far, tick[0]

    def test_all_built_behind_then_one_swap(self):
        anchor, events, calls, near, _far, _ = self.run_case()
        self.assertEqual(anchor.stale, ())
        self.assertEqual(sorted(c[0] for c in calls), sorted(near), "each geotile shown built behind once")
        self.assertTrue(all(low and stale for _k, low, stale in calls), "behind the live work, while the older is shown")
        self.assertEqual([c[0] for c in calls][0], near[0], "nearest first")
        self.assertEqual(events, [("swap", True, len(near))], "one swap, after all of it was built, still stale then")

    def test_nothing_behind_before_the_older_version_is_all_shown(self):
        anchor, events, calls, _near, _far, _ = self.run_case(settle=False, ticks=12)
        self.assertEqual(calls, [])
        self.assertTrue(anchor.stale, "still shown from the older version")
        self.assertEqual(events, [])

    def test_no_swap_before_the_anchor_is_the_players(self):
        anchor, events, _calls, _near, _far, _ = self.run_case(active=3, ticks=12)
        self.assertEqual(events, [])
        self.assertTrue(anchor.stale)

    def test_a_far_missing_tile_is_built_behind_before_the_swap(self):
        anchor, events, calls, near, far, _ = self.run_case(missing=True)
        self.assertEqual(sorted(c[0] for c in calls), sorted(set(near) | set(far[:2])))
        self.assertEqual([e[:2] for e in events], [("swap", True)])
        self.assertEqual(events[0][2], len(calls), "the swap after every one of them was built behind")

    def test_a_missing_tile_near_the_car_swaps_early_through_the_same_barrier(self):
        anchor, events, calls, near, _far, _ = self.run_case(missing=True, near_missing=True)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][:2], ("swap", True))
        self.assertLess(events[0][2], len(near), "before everything was built behind")

    def test_a_shared_session_swaps(self):
        anchor, events, _calls, _near, _far, _ = self.run_case(settle=False, hub_after=3)
        self.assertEqual([e[0] for e in events], ["swap"])

    def test_left_behind_nothing_is_swapped(self):
        anchor, events, _calls, _near, _far, _ = self.run_case(settle=False, leave_after=3)
        self.assertEqual(anchor.stale, ())
        self.assertEqual(events, [])

    def test_a_tile_that_cannot_be_kept_does_not_hold_the_swap(self):
        import earthd
        import geotiles
        frame = geodesy.EnuFrame(39.2834, -76.5934)
        bad = [(a, b) for a, b, _d in geotiles.tiles_near(frame, 0.0, 0.0, 500.0)][1]
        anchor, events, calls, _near, _far, _ = self.run_case(fail={bad}, ticks=200)
        self.assertEqual(sum(1 for c in calls if c[0] == bad), earthd.Server.STALE_TRIES)
        self.assertEqual([e[0] for e in events], ["swap"])

    def test_a_failed_swap_keeps_the_older_version_and_is_not_tried_again(self):
        anchor, events, _calls, _near, _far, ticks = self.run_case(swap_ok=False, ticks=80)
        self.assertEqual([e[0] for e in events], ["swap"], "one try")
        self.assertTrue(anchor.stale, "the older version stays, whole")
        self.assertTrue(anchor.stale_held)
        self.assertLess(ticks, 80, "and the run ends: nothing is released later")


class StaleSwapTest(unittest.TestCase):
    """earthd Connection.stale_swap: the commit barrier's protocol. Nothing of the current version is sent unstaged
    before the mod's commit: the blocks and skins carry the swap's token between swap_begin and swap_end, the content
    of tiles the mod has is held back until 'swap_done'; a block that comes out the same is not sent; any failure is
    swap_abort and the older version whole."""

    def make(self, block_same=False, mod_ok=True, mod_answers=True, build_fails=False, skins=2, contents=1):
        import asyncio
        import collections
        import types
        import earthd
        d = tempfile.mkdtemp(prefix="bnge_swap_")
        self.addCleanup(shutil.rmtree, d, True)
        sent, events = [], []
        gen = {"id": "old", "plans": "1.1"}
        key = (7, 0.0, 0.0)
        with open(os.path.join(d, "hm.png"), "wb") as f:
            f.write(b"old heights")
        with open(os.path.join(d, "old.ter"), "wb") as f:
            f.write(b"old ter")
        anchor = types.SimpleNamespace(id=7, stale=(gen,), stale_fresh=set(), stale_held=False, swap_gate=None)
        server = types.SimpleNamespace(out_dir=d, hm=collections.OrderedDict({key: {"ter": "old.ter", "stale": "old"}}),
                                       hm_committed=collections.deque([key]), hm_sent=collections.deque([key]),
                                       ui_pool=None, is_abandoned=lambda aid: False, hm_name=lambda k: "hm.png",
                                       _remove=lambda p: events.append(("removed", os.path.basename(p))))

        async def terrain(a, cx, cy, lane=None):
            events.append(("build", (a.id, cx, cy) in a.stale_fresh, bool(a.stale)))
            if build_fails:
                raise RuntimeError("no block")
            with open(os.path.join(d, "hm.png"), "wb") as f:
                f.write(b"old heights" if block_same else b"new heights")
            with open(os.path.join(d, "new.ter"), "wb") as f:
                f.write(b"old ter" if block_same else b"new ter")
            info = {"ter": "new.ter"}
            server.hm[(a.id, cx, cy)] = info        # (as _hm_done)
            return info
        server.terrain = terrain

        class Tracker:
            geo = True

            def __init__(self):
                self.redo, self.building, self.swap, self.swap_staged, self.held = set(), set(), None, 0, []

            def swap_begin(self, token):
                self.swap = token
                events.append(("tracker begin", token))

            def redo_all(self):         # (the tracker's builds: the skins staged, the content held)
                assert self.swap is not None
                for i in range(skins):
                    conn.send({"type": "roads_skin", "key": f"g{i}", "swap": self.swap})
                    self.swap_staged += 1
                self.held = [{"type": "roads_tile", "key": f"g{i}"} for i in range(contents)]

            def swap_end(self, ok):
                events.append(("tracker end", ok))
                self.swap = None
                if ok:
                    for m in self.held:
                        conn.send(m)
                    return len(self.held)
                return 0
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)

        def send(m):
            sent.append(m)
            if m["type"] == "swap_end" and mod_answers:      # (the mod: staged, committed, says so)
                loop.call_soon(lambda: conn.swap_done.set_result(mod_ok))
        conn = types.SimpleNamespace(s=server, roads=Tracker(), active=7, closing=False, send=send, loop=loop,
                                     veg=types.SimpleNamespace(redo_all=lambda: events.append(("veg redo",))),
                                     signs=types.SimpleNamespace(refresh=lambda: events.append(("signs",))),
                                     terrain_msg=lambda k, info: {"cx": k[1], "cy": k[2], "ter": info["ter"]},
                                     spawn=lambda c: c.close(), SWAP_BUILD_S=5.0, SWAP_COMMIT_S=0.3)
        conn._block_digest = lambda k, info: earthd.Connection._block_digest(conn, k, info)
        conn._remove_later = lambda p: earthd.Connection._remove_later(conn, p)

        class Inline:
            def submit(self, fn, *a):
                from concurrent.futures import Future
                f = Future()
                f.set_result(fn(*a))
                return f
        server.ui_pool = Inline()
        ok = loop.run_until_complete(earthd.Connection.stale_swap(conn, anchor, gen, 0.0))
        return ok, anchor, server, conn, sent, events, key

    def types_of(self, sent):
        return [(m["type"], m.get("swap")) for m in sent]

    def test_staged_then_committed_then_the_content(self):
        ok, anchor, server, conn, sent, events, key = self.make()
        self.assertTrue(ok)
        token = sent[0]["gen"]
        self.assertEqual(self.types_of(sent), [
            ("swap_begin", None), ("terrain_refresh", token), ("roads_skin", token), ("roads_skin", token),
            ("swap_end", None), ("roads_tile", None)])
        # the block was built as this version's while the anchor was still the older one's, and not shown meanwhile
        self.assertEqual(events[0], ("build", True, True))
        self.assertEqual(anchor.stale, ())
        self.assertEqual(server.hm[key], {"ter": "new.ter"})
        self.assertIsNone(anchor.swap_gate, "the blocks' gate is open again")
        # the content, the trees and the signs only after the mod's commit
        self.assertLess(events.index(("tracker end", True)), events.index(("veg redo",)))
        self.assertEqual(sent.index(next(m for m in sent if m["type"] == "roads_tile")), len(sent) - 1)

    def test_a_block_that_did_not_change_is_not_sent(self):
        ok, anchor, server, conn, sent, events, key = self.make(block_same=True)
        self.assertTrue(ok)
        self.assertNotIn("terrain_refresh", [m["type"] for m in sent])
        self.assertEqual(server.hm[key], {"ter": "old.ter"}, "the mod's block: its mark gone, its .ter kept")
        self.assertIn(("removed", "new.ter"), events)

    def test_the_mod_cannot_commit_the_older_version_stays_whole(self):
        for kw in ({"mod_ok": False}, {"mod_answers": False}):
            ok, anchor, server, conn, sent, events, key = self.make(**kw)
            self.assertFalse(ok, kw)
            self.assertEqual(sent[-1]["type"], "swap_abort", kw)
            self.assertNotIn("roads_tile", [m["type"] for m in sent], "no content released")
            self.assertTrue(anchor.stale, "the older version's again")
            self.assertEqual(server.hm[key].get("stale"), "old")
            self.assertIn(("tracker end", False), events)
            self.assertNotIn(("veg redo",), events)
            self.assertIn(("removed", "new.ter"), events)
            self.assertIsNone(anchor.swap_gate)

    def test_a_block_that_cannot_be_built_nothing_is_sent_but_the_abort(self):
        ok, anchor, server, conn, sent, events, key = self.make(build_fails=True)
        self.assertFalse(ok)
        self.assertEqual([m["type"] for m in sent], ["swap_abort"])
        self.assertTrue(anchor.stale)
        self.assertEqual(server.hm[key].get("stale"), "old")

    def test_block_requests_wait_behind_the_swap(self):
        import inspect
        import earthd
        src = inspect.getsource(earthd.Connection.prepare_terrain)
        self.assertIn("swap_gate", src)
        self.assertLess(src.index("await gate.wait()"), src.index("self.s.terrain(anchor, cx, cy)"))
        # a block the older version lacks is built only once the swap is committed
        self.assertIn("stale_wait_swap", inspect.getsource(earthd.Server.make_heightmap))
        # and no ungraded stand-in under the older version's skins
        self.assertIn('getattr(anchor, "stale", ())', inspect.getsource(earthd.Connection.provisional_follow))


class _Loop:
    """run_in_executor in the caller's thread (a finished Future: its callbacks run when they are added), so a pose's
    builds and their messages are done when on_pose's caller goes on."""

    def __init__(self):
        self.futs = []

    def run_in_executor(self, pool, fn, *args):
        from concurrent.futures import Future
        f = Future()
        try:
            f.set_result(fn(*args))
        except Exception as e:
            f.set_exception(e)
        return f


class _Conn:
    def __init__(self, out_dir=None):
        self.msgs = []
        self.lock = threading.Lock()
        self.s = type("S", (), {"out_dir": out_dir})() if out_dir else None

    def send(self, m):
        with self.lock:
            self.msgs.append(m)


class SwapTest(unittest.TestCase):
    """The swap (earthd Connection.stale_run's end): the trackers ask again for everything sent; what comes back the
    same is not sent again, what changed is, and nothing is dropped or missing meanwhile."""

    def test_road_geotiles_sent_again_only_where_they_changed(self):
        LAT, LON = 39.2834, -76.5934
        f = geodesy.EnuFrame(LAT, LON)
        version = {"n": 1}
        changed = set()
        jobs = []

        def build(aid, gx, gy, content, skin, exact):
            jobs.append((gx, gy, content, skin))
            v = version["n"] if (gx, gy) in changed else 1
            return {"content": (f"/s/rg_{gx}_{gy}.json", 1, {"h": f"c{gx}_{gy}_{v}", "parts": {}}, []) if content else None,
                    "skin": (f"/s/rs_{aid}_{gx}_{gy}.json", {"lift": 0.0, "h": f"s{gx}_{gy}_{v}"}) if skin else None,
                    "exact": {}, "time": {}, **({"stale": "g"} if version["n"] == 1 else {})}
        tr = roads.GeoTracker()
        loop, conn = _Loop(), _Conn()

        def settle():
            for _ in range(80):
                tr.on_pose(conn, 1, 0.0, 0.0, loop, build, frame=f)
                wait(loop.futs)
                if not tr.building and not tr.redo and all(k in tr.sent and k in tr.skins for k in tr.car):
                    return
            self.fail("the tracker did not settle")
        settle()
        sent = set(tr.sent)
        self.assertTrue(len(sent) > 4)
        n0, j0 = len(conn.msgs), len(jobs)
        # the swap: two tiles changed in the current version (one of them the car's)
        version["n"] = 2
        ks = sorted(sent)
        changed.update(ks[:2])
        tr.redo_all()
        settle()
        tr.close()
        after = conn.msgs[n0:]
        self.assertEqual(sorted(j[:2] for j in jobs[j0:]), sorted(sent), "every tile sent asked for again, once")
        self.assertTrue(all(j[2] and j[3] for j in jobs[j0:]), "content and skin together")
        tiles = [m for m in after if m["type"] == "roads_tile"]
        skins = [m for m in after if m["type"] == "roads_skin"]
        self.assertEqual(len(tiles), 2)
        self.assertEqual(len(skins), 2)
        self.assertFalse([m for m in after if m["type"] == "roads_drop"], "nothing dropped at a swap")
        self.assertEqual(tr.sent, sent)
        self.assertEqual(tr.stats["redo"], len(sent))
        self.assertEqual(tr.stats["redo_same"], len(sent) - 2)

    def test_trees_sent_again_only_where_their_file_changed(self):
        d = tempfile.mkdtemp(prefix="bnge_staleveg_")
        self.addCleanup(shutil.rmtree, d, True)
        f = geodesy.EnuFrame(39.2834, -76.5934)
        version = {"n": 1}
        changed = set()

        def build(gx, gy):
            v = version["n"] if (gx, gy) in changed else 1
            name = f"veg_{gx}_{gy}.json"
            with open(os.path.join(d, name), "w") as fh:
                fh.write(json.dumps({"items": [gx, gy, v]}))
            return f"/s/{name}", 1, (39.28, -76.59), 0.0
        tr = vegetation.Tracker(geo=True)
        tr.digests = True
        loop, conn = _Loop(), _Conn(d)

        def settle():
            for _ in range(200):
                tr.on_pose(conn, 1, 0.0, 0.0, loop, build, frame=f)
                wait(loop.futs)
                if not tr.building and not tr.redo and all(k in tr.sent for k in tr.car):
                    return
            self.fail("the tracker did not settle")
        settle()
        sent = set(tr.sent)
        n0 = len(conn.msgs)
        version["n"] = 2
        changed.update(sorted(sent)[:3])
        tr.redo_all()
        settle()
        tr.close()
        after = conn.msgs[n0:]
        self.assertEqual(len([m for m in after if m["type"] == "veg_tile"]), 3)
        self.assertFalse([m for m in after if m["type"] == "veg_drop"])
        self.assertEqual(tr.sent, sent)


if __name__ == "__main__":
    unittest.main()
