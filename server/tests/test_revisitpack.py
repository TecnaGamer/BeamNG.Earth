"""revisitpack: the revisit cache's items in per-process pack files (2026-11-11). Round trips (zstd and stored),
other processes' items found a moment after they were kept (several writers at once), damaged records a miss and
never an error, a pack removed under a reader, rotation, the start-up prune (whole packs, oldest first; saved areas'
items copied out; small packs merged without duplicates), hits from the packs the next prune would take copied
forward, zips of before moved into a pack."""
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import revisit  # noqa: E402
import revisitpack as rp  # noqa: E402


def _writer(cache, tag, n, out):
    """A worker process: n items kept under keys "<tag>-<i>"."""
    import revisitpack
    st = revisitpack.store_for(cache)
    for i in range(n):
        st.put("roads", f"{tag}-{i}", (f"{tag}:{i}:" * (50 + i % 7)).encode())
    out.put(tag)


class PackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "cache")
        rp.reset()
        self.addCleanup(rp.reset)
        self.on = revisit.ON
        revisit.ON = True
        self.addCleanup(setattr, revisit, "ON", self.on)

    def packdir(self):
        return os.path.join(self.cache, "revisit", rp.SUBDIR)

    def test_round_trip_and_codecs(self):
        st = rp.store_for(self.cache)
        text = json.dumps([{"a": i, "b": "road" * 5} for i in range(2000)]).encode()
        noise = os.urandom(50000)
        st.put("cellfacts", "a" * 40, text)
        st.put("bldfar", "b" * 40, noise)
        st.put("bldfar", "c" * 40, b"")
        self.assertEqual(st.get("cellfacts", "a" * 40), text)
        self.assertEqual(st.get("bldfar", "b" * 40), noise)
        self.assertEqual(st.get("bldfar", "c" * 40), b"")
        self.assertIsNone(st.get("roads", "a" * 40))        # (another kind's key: not this item)
        self.assertIsNone(st.get("cellfacts", "d" * 40))
        ents = rp.entries_of(self.cache, os.listdir(self.packdir())[0].rsplit(".", 1)[0])
        codecs = {e[0]: e[2] for e in ents}
        self.assertEqual(codecs[bytes.fromhex("a" * 40)], rp.ZSTD)
        self.assertEqual(codecs[bytes.fromhex("b" * 40)], rp.RAW)        # (does not shrink: kept as it is)
        rp.reset()                      # (a new process: everything from the files)
        st = rp.store_for(self.cache)
        self.assertEqual(st.get("cellfacts", "a" * 40), text)
        self.assertTrue(st.has("bldfar", "b" * 40))
        self.assertFalse(st.has("bldfar", "e" * 40))

    def test_revisit_items_and_where(self):
        k = revisit.key("roads", 1, 2)
        self.assertTrue(revisit.store(self.cache, "roads", k, {"a.json": b'{"x":1}', "b.cdae": b"\0" * 999},
                                      {"n": 3}))
        blobs, extra = revisit.load(self.cache, "roads", k)
        self.assertEqual(blobs, {"a.json": b'{"x":1}', "b.cdae": b"\0" * 999})
        self.assertEqual(extra, {"n": 3})
        self.assertTrue(revisit.has(self.cache, "roads", k))
        self.assertFalse(os.path.exists(revisit._path(self.cache, "roads", k)))
        self.assertEqual([n for n in os.listdir(os.path.join(self.cache, "revisit")) if n != rp.SUBDIR], [])

    def test_other_processes_items_are_found(self):
        st = rp.store_for(self.cache)
        self.assertIsNone(st.get("roads", "p0-1"))          # (loaded empty first)
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_writer, args=(self.cache, f"p{i}", 150, q)) for i in range(3)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        time.sleep(rp.REFRESH_S * 2)
        for t in range(3):
            for i in range(150):
                self.assertEqual(st.get("roads", f"p{t}-{i}"), (f"p{t}:{i}:" * (50 + i % 7)).encode())
        names = sorted(os.listdir(self.packdir()))
        self.assertEqual(len(names), 6)         # (a pack and its index each)

    def test_forked_child_writes_its_own_pack(self):
        if not hasattr(os, "fork"):
            self.skipTest("no fork")
        st = rp.store_for(self.cache)
        st.put("roads", "parent", b"P" * 100)
        pid = os.fork()
        if pid == 0:
            try:
                rp.store_for(self.cache).put("roads", "child", b"C" * 100)
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        st.put("roads", "parent2", b"Q" * 100)
        time.sleep(rp.REFRESH_S * 2)
        self.assertEqual(st.get("roads", "child"), b"C" * 100)
        self.assertEqual(len([n for n in os.listdir(self.packdir()) if n.endswith(rp.PACK_SUFFIX)]), 2)
        rp.reset()
        st = rp.store_for(self.cache)
        for k, v in (("parent", b"P"), ("child", b"C"), ("parent2", b"Q")):
            self.assertEqual(st.get("roads", k), v * 100)

    def test_damage_is_a_miss(self):
        st = rp.store_for(self.cache)
        st.put("roads", "k1", b"x" * 5000)
        st.put("roads", "k2", b"y" * 5000)
        name = os.listdir(self.packdir())[0].rsplit(".", 1)[0]
        path = os.path.join(self.packdir(), name)
        with open(path + rp.INDEX_SUFFIX, "ab") as f:
            f.write(b"\1" * 20)                     # (an entry half written: ignored)
        ents = rp.entries_of(self.cache, name)
        with open(path + rp.PACK_SUFFIX, "r+b") as f:
            f.seek(ents[0][3] + rp.REC.size + 3)
            f.write(b"\xff\xff")
        rp.reset()
        st = rp.store_for(self.cache)
        self.assertIsNone(st.get("roads", "k1"))
        self.assertEqual(st.get("roads", "k2"), b"y" * 5000)
        self.assertEqual(st.stats["bad"], 1)
        st.put("roads", "k1", b"z" * 10)          # (built again: the new record wins)
        self.assertEqual(st.get("roads", "k1"), b"z" * 10)

    def test_rotation_and_a_pack_removed_under_a_reader(self):
        with mock.patch.object(rp, "PACK_BYTES", 3000):
            st = rp.store_for(self.cache)
            for i in range(6):
                st.put("roads", f"r{i}", os.urandom(1500))
        packs, _ = rp.list_packs(self.cache)
        self.assertGreaterEqual(len(packs), 3)
        rp.reset()
        st = rp.store_for(self.cache)
        self.assertTrue(st.has("roads", "r0"))
        first = min(packs, key=lambda p: int(p[0].rsplit("_", 1)[1]))[0]
        self.assertTrue(rp.remove_pack(self.cache, first))
        time.sleep(rp.REFRESH_S * 2)
        st.get("roads", "r0")                     # (read through a handle still open, or a miss: never an error)
        st.refresh(force=True)
        self.assertFalse(st.has("roads", "r0"))
        self.assertIsNotNone(st.get("roads", "r5"))

    def test_pack_file_evicted_without_its_index(self):
        """The cache's LRU removed a .rpk but not its .rix: its items are a miss, and not found again."""
        rp.store_for(self.cache).put("roads", "gone", b"g" * 100)
        rp.reset()
        with mock.patch.object(rp, "_START", 2):
            rp.store_for(self.cache).put("roads", "kept", b"k" * 100)
        rp.reset()
        first = sorted(n for n in os.listdir(self.packdir()) if n.endswith(rp.PACK_SUFFIX))
        os.remove(os.path.join(self.packdir(), [n for n in first if "_2_" not in n][0]))
        st = rp.store_for(self.cache)
        self.assertTrue(st.has("roads", "gone"))           # (its entry still listed ...)
        self.assertIsNone(st.get("roads", "gone"))         # (... but nothing to read: forgotten)
        st.refresh(force=True)
        self.assertFalse(st.has("roads", "gone"))
        self.assertEqual(st.get("roads", "kept"), b"k" * 100)

    def test_writer_starts_a_new_pack_when_its_file_is_removed(self):
        if os.name == "nt":
            self.skipTest("a held pack cannot be removed on Windows")
        st = rp.store_for(self.cache)
        st.put("roads", "a", b"1" * 100)
        for n in os.listdir(self.packdir()):
            os.remove(os.path.join(self.packdir(), n))
        st.put("roads", "b", b"2" * 100)
        rp.reset()
        st = rp.store_for(self.cache)
        self.assertEqual(st.get("roads", "b"), b"2" * 100)
        self.assertIsNone(st.get("roads", "a"))

    def test_legacy_zip_is_moved_into_a_pack(self):
        k = "ab" + "0" * 38
        zp = revisit._path(self.cache, "roads", k)
        os.makedirs(os.path.dirname(zp))
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("t.json", b"{}")
            z.writestr("meta.json", json.dumps({"names": ["t.json"], "extra": {"n": 1}}))
        self.assertTrue(revisit.has(self.cache, "roads", k))
        self.assertEqual(revisit.load(self.cache, "roads", k), ({"t.json": b"{}"}, {"n": 1}))
        self.assertFalse(os.path.exists(zp))
        rp.reset()
        self.assertEqual(revisit.load(self.cache, "roads", k), ({"t.json": b"{}"}, {"n": 1}))

    def test_legacy_zip_with_a_side_file_is_moved_too(self):
        k = "cd" + "1" * 38
        zp = revisit._path(self.cache, "far", k)
        os.makedirs(os.path.dirname(zp))
        with open(revisit._side(zp, "tex.dds"), "wb") as f:
            f.write(b"DDS")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("built.npz", b"npz")
            z.writestr("meta.json", json.dumps({"names": ["built.npz"], "side": ["tex.dds"], "extra": {"a": 1}}))
        blobs, extra = revisit.load(self.cache, "far", k)
        self.assertEqual((blobs["built.npz"], blobs["tex.dds"].read(), extra), (b"npz", b"DDS", {"a": 1}))
        self.assertFalse(os.path.exists(zp))
        rp.reset()
        blobs, extra = revisit.load(self.cache, "far", k)
        self.assertEqual((blobs["built.npz"], blobs["tex.dds"].read(), extra), (b"npz", b"DDS", {"a": 1}))

    def _age(self, name, t):
        for suf in (rp.PACK_SUFFIX, rp.INDEX_SUFFIX):
            os.utime(os.path.join(self.packdir(), name + suf), (t, t))

    def _packs_by_seq(self):
        packs, _ = rp.list_packs(self.cache)
        return sorted((p[0] for p in packs), key=lambda n: int(n.rsplit("_", 1)[1]))

    def test_prune_removes_oldest_packs_and_keeps_pinned_items(self):
        with mock.patch.object(rp, "PACK_BYTES", 40000):
            st = rp.store_for(self.cache)
            for i in range(12):
                st.put("roads", f"{i:040x}", os.urandom(20000))
        names = self._packs_by_seq()
        self.assertEqual(len(names), 6)
        now = time.time()
        for j, n in enumerate(names):
            self._age(n, now - 10000 + j * 100)
        # a zip of before, older than every pack, and a newer one
        old_zip = revisit._path(self.cache, "roads", "e" * 40)
        os.makedirs(os.path.dirname(old_zip))
        for p, t in ((old_zip, now - 20000), (revisit._path(self.cache, "roads", "e" * 39 + "f"), now - 50)):
            with open(p, "wb") as f:
                f.write(b"z" * 1000)
            os.utime(p, (t, t))
        rp.reset()
        pinned = {f"{0:040x}"}                        # (in the oldest pack: a saved area's)
        n, merged, left = rp.prune(self.cache, 150000, pinned, before=now, files_match=lambda s: s.endswith(".zip"),
                                   now=now)
        self.assertGreaterEqual(n, 3)               # (the old zip and the oldest packs; the new zip stays)
        self.assertTrue(os.path.exists(revisit._path(self.cache, "roads", "e" * 39 + "f")))
        self.assertFalse(os.path.exists(old_zip))
        self.assertLessEqual(left, 150000 * rp.LOW_WATER)
        rest = self._packs_by_seq()
        self.assertNotIn(names[0], rest)
        self.assertIn(names[-1], rest)
        self.assertEqual(merged, 0)
        st = rp.store_for(self.cache)
        self.assertIsNotNone(st.get("roads", f"{0:040x}"))          # (copied out before its pack went)
        self.assertIsNone(st.get("roads", f"{2:040x}"))
        self.assertIsNotNone(st.get("roads", f"{11:040x}"))

    def test_prune_leaves_live_packs(self):
        st = rp.store_for(self.cache)
        st.put("roads", "x", os.urandom(30000))
        now = time.time()
        self.assertEqual(rp.prune(self.cache, 1000, before=now - 100, now=now)[0], 0)
        self.assertEqual(st.get("roads", "x") is not None, True)

    def test_small_packs_merged_once(self):
        now = time.time()
        for run in range(4):
            rp.reset()
            with mock.patch.object(rp, "_START", 1000 + run):
                st = rp.store_for(self.cache)
                st.put("roads", "shared", f"v{run}".encode())        # (kept again by every run)
                st.put("roads", f"own{run}", f"o{run}".encode())
        for j, n in enumerate(self._packs_by_seq_all()):
            self._age(n, now - 5000 + j)
        rp.reset()
        n, merged, _left = rp.prune(self.cache, 1e9, before=now, now=now)
        self.assertEqual((n, merged), (0, 4))
        names = self._packs_by_seq_all()
        self.assertEqual(len(names), 1)
        ents = rp.entries_of(self.cache, names[0])
        self.assertEqual(len(ents), 5)               # (shared once, four own items)
        st = rp.store_for(self.cache)
        self.assertEqual(st.get("roads", "shared"), b"v3")       # (the latest run's)
        rp.reset()
        st = rp.store_for(self.cache)
        self.assertEqual([st.get("roads", f"own{i}") for i in range(4)], [b"o0", b"o1", b"o2", b"o3"])

    def _packs_by_seq_all(self):
        packs, _ = rp.list_packs(self.cache)
        return sorted(p[0] for p in packs)

    def test_hits_from_packs_about_to_go_are_copied_forward(self):
        with mock.patch.object(rp, "_START", 1):
            st = rp.store_for(self.cache)
            for i in range(10):
                st.put("roads", f"h{i}", os.urandom(10000))
        rp.reset()
        with mock.patch.object(rp, "CAP_BYTES", 110000):       # (100 KB in packs: over PROMOTE_MIN of the cap)
            st = rp.store_for(self.cache)
            self.assertIsNotNone(st.get("roads", "h3"))
            self.assertEqual(st.stats["promoted"], 1)
            self.assertIsNotNone(st.get("roads", "h3"))     # (now from its own pack: once)
            self.assertEqual(st.stats["promoted"], 1)
        rp.reset()
        with mock.patch.object(rp, "CAP_BYTES", 1e9):
            st = rp.store_for(self.cache)
            st.get("roads", "h4")
            self.assertEqual(st.stats["promoted"], 0)


if __name__ == "__main__":
    unittest.main()
