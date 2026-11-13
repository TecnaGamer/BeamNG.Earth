"""blockcache.py / earthfast blockstore: the graded terrain blocks kept on disk. A block from the cache is the block
built again, byte for byte (PNG, masks, .ter, info); a moved block reads its old chunks and computes only the new
strip; every input in the key changes it; a short build keeps nothing; a damaged file is a miss; the folder is cut
back least recently used first."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import blockcache  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import grading  # noqa: E402
import heightmap  # noqa: E402
import revisit  # noqa: E402
import surface as surfacemod  # noqa: E402

SIZE = 1024 if heightmap.SQUARE == 1.0 else 512


def elev(lat, lon):
    """A synthetic DEM, elementwise, with some sub-metre detail (the float64's low bits all used)."""
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    return (5.0 + 30.0 * np.sin(lat * 900.0) * np.cos(lon * 700.0) + 1000.0 * (lat - 39.28)
            + 0.37 * np.sin(lat * 91000.0) * np.sin(lon * 77000.0))


class _Pads:
    """A Surface stand-in: pad_digest of its own pads (box -> those reaching it)."""

    def __init__(self):
        self.pads = []      # (sid, la0, la1, lo0, lo1)

    def pad_digest(self, la0, la1, lo0, lo1, bus_only=False):
        h = hashlib.sha256(repr(bus_only).encode())
        for sid, a0, a1, b0, b1 in self.pads:
            if (bus_only and not sid.startswith("bus:")) or a1 < la0 or a0 > la1 or b1 < lo0 or b0 > lo1:
                continue
            h.update(repr((sid, a0, a1, b0, b1)).encode())
        return h.digest()


def material_index(u):
    """A stand-in for the land cover and paint: block-dependent like theirs (the slope at the block's edge)."""
    gy, gx = np.gradient(u)
    idx = np.where(gx * gx + gy * gy > 0.02, surfacemod.ROCK, surfacemod.GRASS).astype(np.uint8)
    idx[(np.round(u * 7) % 5) == 0] = surfacemod.ASPHALT
    return idx


class _Built:
    """One block built the way blockworker._build does it (heights through the chunk cache and the disk, material
    index, masks, tunnel holes, .ter), and the cache's Build of it."""

    def __init__(self, test, cache_dir, chunks, cx, cy, out, holes=False, surface=None, frame=None, fault=False):
        frame = frame or test.frame
        name, ter = f"hm_{int(cx)}_{int(cy)}.png", f"hm_{int(cx)}_{int(cy)}.ter"
        self.bc = blockcache.begin(cache_dir, frame, 16, cx, cy, SIZE, test.q, ("test",), None, surface, chunks)
        self.calls = 0
        self.hit = self.bc.hit(out, name, ter, (0.0, 0.0), True) if self.bc is not None else None
        if self.hit is not None:
            self.info = self.hit
            return

        def counted(la, lo):
            self.calls += np.size(la)
            if fault:
                revisit.fault()
            return elev(la, lo)
        keep = {}
        info = heightmap.generate(frame, counted, os.path.join(out, name), cx, cy, q=test.q, size=SIZE, keep=keep,
                                  chunks=chunks, ground_at=(0.0, 0.0), disk=self.bc.disk if self.bc else None)
        idx = material_index(keep["u"])
        info["layers"] = surfacemod.write_masks(idx[::-1], out, name[:-4])
        info["surfaceComplete"] = info["roadsComplete"] = True
        pre = idx.copy()
        if holes:
            h = np.zeros(idx.shape, bool)
            h[100:120, 200:260] = True
            idx[h] = heightmap.TER_HOLE
            info["tunnelHoles"] = int(h.sum())
        info["ter"], mats = surfacemod.write_ter(out, ter, keep["qgrid"], idx)
        if mats is not None:
            info["mats"] = mats
        self.kept = self.bc.finish(info, keep, pre, idx, None) if self.bc is not None else False
        self.info = info


def _files(out, info, cx, cy):
    names = [f"hm_{int(cx)}_{int(cy)}.png", f"hm_{int(cx)}_{int(cy)}.ter"] + list(info.get("layers", []))
    got = {}
    for n in names:
        with open(os.path.join(out, n), "rb") as f:
            got[n] = f.read()
    return got


def _flip(path, at):
    """One byte of a file changed (at: a share of its length, or an offset from its end)."""
    with open(path, "rb") as f:
        data = bytearray(f.read())
    i = int(len(data) * at) if isinstance(at, float) else at
    data[i] ^= 0x55
    with open(path, "wb") as f:
        f.write(bytes(data))


@unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= blockcache.NATIVE, "earthfast 269")
class BlockCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bnge_blockcache_")
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(self.cache)
        self.frame = geodesy.EnuFrame(39.28, -76.59)
        self.q = 1 / 512
        self.env = mock.patch.dict(os.environ, {"BNGE_BLOCKCACHE": "on"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def out(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    def test_cached_block_equals_rebuilt(self):
        """A miss keeps the block; a later build (new process: empty memory) is a hit, byte for byte the rebuild's;
        with tunnel holes too."""
        for holes in (False, True):
            cache = os.path.join(self.tmp, f"c{holes}")
            os.makedirs(cache)
            first = _Built(self, cache, heightmap.ChunkCache(), 0.0, 0.0, self.out(f"a{holes}"), holes=holes)
            self.assertIsNone(first.hit)
            self.assertTrue(first.kept)
            again = _Built(self, cache, heightmap.ChunkCache(), 0.0, 0.0, self.out(f"b{holes}"), holes=holes)
            self.assertIsNotNone(again.hit, "a hit")
            self.assertEqual(again.calls, 0)
            with mock.patch.dict(os.environ, {"BNGE_BLOCKCACHE": "off"}):
                rebuilt = _Built(self, cache, heightmap.ChunkCache(), 0.0, 0.0, self.out(f"r{holes}"), holes=holes)
            self.assertIsNone(rebuilt.bc)
            self.assertEqual(again.info, rebuilt.info)
            self.assertEqual(first.info, rebuilt.info)
            got, want = _files(self.out(f"b{holes}"), again.info, 0, 0), _files(self.out(f"r{holes}"), rebuilt.info, 0, 0)
            self.assertEqual(sorted(got), sorted(want))
            for n in want:
                self.assertEqual(got[n], want[n], n)

    def test_moved_block_reads_old_chunks(self):
        """A block 128 m on, in a new process: 56 of its 64 chunks from the disk, 8 computed; its heights the ones a
        build from scratch gives, bit for bit."""
        _Built(self, self.cache, heightmap.ChunkCache(), 0.0, 0.0, self.out("a"))
        step = heightmap.grid(SIZE)
        moved = _Built(self, self.cache, heightmap.ChunkCache(), step, 0.0, self.out("b"))
        self.assertIsNone(moved.hit)
        nc = SIZE // heightmap.CHUNK
        self.assertEqual(moved.bc.disk.read, nc * (nc - 1))
        self.assertEqual(moved.calls, heightmap.CHUNK * SIZE)
        u = heightmap.chunk_grid(step, 0.0, SIZE, None, moved.bc.disk)
        self.assertTrue(np.array_equal(u.view(np.uint64), heightmap.grid_u(self.frame, elev, step, 0.0, SIZE)
                                       .view(np.uint64)))
        with mock.patch.dict(os.environ, {"BNGE_BLOCKCACHE": "off"}):
            rebuilt = _Built(self, self.cache, None, step, 0.0, self.out("r"))
        got, want = _files(self.out("b"), moved.info, step, 0), _files(self.out("r"), rebuilt.info, step, 0)
        for n in want:
            self.assertEqual(got[n], want[n], n)

    def keys(self, **kw):
        a = dict(cache_dir=self.cache, frame=self.frame, hm_zoom=16, cx=0.0, cy=0.0, size=SIZE, q=self.q,
                 setting=("s",), md=None, surface=None, chunks=heightmap.ChunkCache())
        a.update(kw)
        b = blockcache.begin(**a)
        return b.block_key, dict(b.chunk_keys)

    def test_keys(self):
        """The same inputs: the same keys; each input changed: other keys. A chunk's key is the same in every block
        over it (shared), a block's only its own."""
        base_block, base_chunks = self.keys()
        self.assertEqual(self.keys(), (base_block, base_chunks))

        class Md:
            def __init__(self, v):
                self.v = v

            def versions(self):
                return {"overture": self.v}
        changed = {
            "frame": dict(frame=geodesy.EnuFrame(39.29, -76.59)),
            "dem zoom": dict(hm_zoom=15),
            "settings": dict(setting=("t",)),
            "map data": dict(md=Md("2026-10-21.0")),
        }
        for what, kw in changed.items():
            b, c = self.keys(**kw)
            self.assertNotEqual(b, base_block, what)
            self.assertFalse(set(c.values()) & set(base_chunks.values()), what)
        self.assertNotEqual(self.keys(md=Md("a"))[0], self.keys(md=Md("b"))[0])
        # the block's own: its quantum
        b, c = self.keys(q=self.q * 2)
        self.assertNotEqual(b, base_block)
        self.assertEqual(c, base_chunks)
        # a setting in the environment changes them; a run's own ids do not
        with mock.patch.dict(os.environ, {"BNGE_GROUND_SQUARE_TEST": "2"}):
            self.assertNotEqual(self.keys()[0], base_block)
        with mock.patch.dict(os.environ, {"BNGE_PLAN_RUN": "123_456", "BNGE_ROADSURF_T0": "1.0"}):
            self.assertEqual(self.keys()[0], base_block)
        # the code, the plans' format, the cache's format
        with mock.patch.object(blockcache, "_code_id", lambda: "other"):
            self.assertNotEqual(self.keys()[0], base_block)
        with mock.patch.object(blockcache, "_plans_format", lambda: "0.0"):
            self.assertNotEqual(self.keys()[0], base_block)
        with mock.patch.object(blockcache, "FORMAT", blockcache.FORMAT + 1):
            self.assertNotEqual(self.keys()[0], base_block)
        # the road surface verdicts round them
        import roadsurf
        with mock.patch.object(roadsurf, "area_tags", lambda *a: ("rsabc",)):
            b, c = self.keys()
            self.assertNotEqual(b, base_block)
            self.assertFalse(set(c.values()) & set(base_chunks.values()))
        # a chunk shared by two blocks has one key
        step = heightmap.grid(SIZE)
        _b2, c2 = self.keys(cx=step)
        shared = set(base_chunks) & set(c2)
        self.assertEqual(len(shared), (SIZE // heightmap.CHUNK) * (SIZE // heightmap.CHUNK - 1))
        for k in shared:
            self.assertEqual(base_chunks[k], c2[k])

    def test_pads_in_keys(self):
        """A bus yard near a chunk changes that chunk's key (it levels the ground) and the block's; one far off changes
        nothing; a fuel pad near the block changes the block's key alone."""
        pads = _Pads()
        b0, c0 = self.keys(surface=pads)
        pads.pads.append(("bus:1", 39.2795, 39.2796, -76.5905, -76.5904))
        b1, c1 = self.keys(surface=pads)
        self.assertNotEqual(b0, b1)
        diff = [k for k in c0 if c0[k] != c1[k]]
        self.assertTrue(0 < len(diff) < len(c0), len(diff))
        pads.pads[-1] = ("bus:1", 40.0, 40.0001, -76.0, -75.9999)
        self.assertEqual(self.keys(surface=pads), (b0, c0))
        pads.pads[-1] = ("fuel:1", 39.2795, 39.2796, -76.5905, -76.5904)
        b3, c3 = self.keys(surface=pads)
        self.assertNotEqual(b3, b0)
        self.assertEqual(c3, c0)

    def test_short_build_keeps_nothing(self):
        """A fault during the build: nothing on disk, its chunks marked dirty; a clean neighbour built on them keeps
        its own new chunks but no block entry."""
        chunks = heightmap.ChunkCache()
        bad = _Built(self, self.cache, chunks, 0.0, 0.0, self.out("a"), fault=True)
        self.assertFalse(bad.kept)
        self.assertFalse(os.path.isdir(blockcache.folder(self.cache)) and any(
            files for _d, _s, files in os.walk(blockcache.folder(self.cache))))
        self.assertTrue(chunks.any_dirty([k for _, _, k in heightmap.block_chunks(0.0, 0.0, SIZE)]))
        step = heightmap.grid(SIZE)
        nxt = _Built(self, self.cache, chunks, step, 0.0, self.out("b"))
        self.assertFalse(nxt.kept)
        self.assertIsNone(_Built(self, self.cache, heightmap.ChunkCache(), step, 0.0, self.out("c")).hit)

    def test_another_builds_fault_does_not_stop_it(self):
        """A fault of another build (its own thread and scope, revisit.scope) while this block is built: kept; a fault
        in a thread no build's scope covers: not kept (revisit's per-build faults, 2026-11-13)."""
        import threading

        def other():
            with revisit.scope():
                revisit.fault()

        orig = heightmap.generate

        def generate_with(fn):
            def gen(*a, **k):
                t = threading.Thread(target=fn)
                t.start()
                t.join()
                return orig(*a, **k)
            return gen
        heightmap.generate = generate_with(other)
        try:
            ok = _Built(self, self.cache, heightmap.ChunkCache(), 0.0, 0.0, self.out("a"))
        finally:
            heightmap.generate = orig
        self.assertTrue(ok.kept)
        heightmap.generate = generate_with(revisit.fault)
        try:
            step = heightmap.grid(SIZE)
            bad = _Built(self, self.cache, heightmap.ChunkCache(), step, 0.0, self.out("b"))
        finally:
            heightmap.generate = orig
        self.assertFalse(bad.kept)

    def test_plans_not_known_complete(self):
        """Plans of a cell made short, or of unknown completeness: no block kept; once known complete, kept."""
        key = (14, 4600, 6200)
        grading._note_plans(key, False)
        self.assertFalse(grading.plans_complete([key]))
        self.assertFalse(grading.plans_complete([(14, 1, 1)]))
        grading._note_plans(key, True)
        self.assertTrue(grading.plans_complete([key]))

        class G:
            @staticmethod
            def _keys(la, lo):
                return [(14, 7, 7)]
        b = blockcache.begin(self.cache, self.frame, 16, 0.0, 0.0, SIZE, self.q, ("s",), None, None,
                             heightmap.ChunkCache())
        keep = {"lat": np.array([39.28, 39.29]), "lon": np.array([-76.6, -76.59])}
        self.assertIn("plans", b._short({}, keep, G(), []))
        grading._note_plans((14, 7, 7), True)
        self.assertIsNone(b._short({}, keep, G(), []))

    def test_damaged_files_are_misses(self):
        """A truncated or garbled chunk or entry: a miss (and removed); the block is built again, the same."""
        first = _Built(self, self.cache, heightmap.ChunkCache(), 0.0, 0.0, self.out("a"))
        d = blockcache.folder(self.cache)
        hs = sorted(os.path.join(r, f) for r, _s, fs in os.walk(d) for f in fs if f.endswith(".h"))
        bs = [os.path.join(r, f) for r, _s, fs in os.walk(d) for f in fs if f.endswith(".b")]
        self.assertEqual(len(bs), 1)
        with open(hs[0], "r+b") as f:
            f.truncate(os.path.getsize(hs[0]) - 7)
        _flip(hs[1], 0.5)
        again = _Built(self, self.cache, heightmap.ChunkCache(), 0.0, 0.0, self.out("b"))
        self.assertIsNone(again.hit)
        self.assertEqual(again.bc.disk.read, len(hs) - 2)
        self.assertEqual(again.calls, 2 * heightmap.CHUNK * heightmap.CHUNK)
        self.assertEqual(again.info, first.info)
        for n, b in _files(self.out("a"), first.info, 0, 0).items():
            self.assertEqual(_files(self.out("b"), again.info, 0, 0)[n], b, n)
        # the entry garbled: a miss, rebuilt the same
        _flip(bs[0], -5)
        third = _Built(self, self.cache, heightmap.ChunkCache(), 0.0, 0.0, self.out("c"))
        self.assertIsNone(third.hit)
        self.assertEqual(third.calls, 0)        # (its chunks all on disk again)
        self.assertEqual(third.info, first.info)

    def test_prune_least_recently_used(self):
        """Over its limit the folder loses its least recently used files first, down to the low water mark; files
        used within keep_s stay; a hit marks a file used."""
        d = blockcache.folder(self.cache)
        keys = [bytes([i]) * 32 for i in range(10)]
        u = np.random.default_rng(1).random((10, heightmap.CHUNK, heightmap.CHUNK))
        fast._ef.blockstore_put_heights(d, keys, u, 1)
        paths = {k: os.path.join(d, k.hex()[:2], k.hex() + ".h") for k in keys}
        now = time.time()
        for i, k in enumerate(keys):
            os.utime(paths[k], (now - 10000 + i, now - 10000 + i))
        size = os.path.getsize(paths[keys[0]])
        removed, left = fast._ef.blockstore_prune(d, int(size * 6.5), 0.9, 600.0)
        self.assertEqual(removed, 5)
        self.assertEqual([os.path.exists(paths[k]) for k in keys], [False] * 5 + [True] * 5)
        self.assertLessEqual(left, size * 6.5 * 0.9)
        # recently used: kept whatever the limit
        for k in keys[5:]:
            os.utime(paths[k], None)
        self.assertEqual(fast._ef.blockstore_prune(d, 1, 0.9, 600.0)[0], 0)
        # a read marks a file used
        os.utime(paths[keys[6]], (now - 5000, now - 5000))
        fast._ef.blockstore_get_heights(d, [keys[6]], heightmap.CHUNK)
        self.assertGreater(os.path.getmtime(paths[keys[6]]), now - 60)

    def test_surface_pad_digest(self):
        """surface.Surface.pad_digest: the bus yards alone for a chunk (bus_only, and only with bus_pads on); every
        kind for a block; pads off the box change nothing."""
        s = surfacemod.Surface.__new__(surfacemod.Surface)
        import threading
        s._lock = threading.Lock()
        s._fuel_pads, s._frontage_pads, s.gas_pads, s.bus_pads = {}, {}, True, True
        box = (39.27, 39.29, -76.60, -76.58)
        empty_bus, empty_all = s.pad_digest(*box, bus_only=True), s.pad_digest(*box)
        ring = [(np.array([39.28, 39.2801, 39.2801]), np.array([-76.59, -76.59, -76.5899]))]
        s._fuel_pads["fuel:1"] = (ring, (39.28, -76.59, 39.2801, -76.5899))
        self.assertEqual(s.pad_digest(*box, bus_only=True), empty_bus)
        self.assertNotEqual(s.pad_digest(*box), empty_all)
        s._fuel_pads["bus:2"] = (ring, (39.28, -76.59, 39.2801, -76.5899))
        self.assertNotEqual(s.pad_digest(*box, bus_only=True), empty_bus)
        far = (40.0, 40.1, -76.0, -75.9)
        s2 = surfacemod.Surface.__new__(surfacemod.Surface)
        s2._lock, s2._fuel_pads, s2._frontage_pads, s2.gas_pads, s2.bus_pads = threading.Lock(), {}, {}, True, True
        self.assertEqual(s.pad_digest(*far), s2.pad_digest(*far))
        s.bus_pads = False
        self.assertNotEqual(s.pad_digest(*box, bus_only=True), empty_bus)   # (the switch itself is in it)
        s2.bus_pads = False
        self.assertEqual(s.pad_digest(*box, bus_only=True), s2.pad_digest(*box, bus_only=True))

    def test_removed_cache_folder_not_made_again(self):
        """A build whose cache folder was removed meanwhile (a test's, a cleared cache) writes nothing there."""
        gone = os.path.join(self.tmp, "gone")
        u = np.zeros((1, heightmap.CHUNK, heightmap.CHUNK))
        self.assertFalse(blockcache._writer.write(gone, [b"\1" * 32], u, None))
        self.assertFalse(os.path.exists(gone))

    def test_budget(self):
        self.assertEqual(blockcache.budget_mb(None), blockcache.DEFAULT_MB)
        self.assertEqual(blockcache.budget_mb(20e9), 2000.0)
        self.assertEqual(blockcache.budget_mb(1e9), blockcache.MIN_MB)
        self.assertEqual(blockcache.budget_mb(1e12), blockcache.MAX_MB_CAP)



def tearDownModule():
    import revisit      # (the Builds made here hold a scope each, closed by finish or blockworker.build's unwind)
    revisit.unwind(0)

if __name__ == "__main__":
    unittest.main()
