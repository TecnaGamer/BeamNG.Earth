"""A DEM tile made while a download failed (a stand-in) makes every build that reads it short, whichever thread
loaded it and whenever the build reads it; and a sample asks only the tiles its points read, so a failed download of
a tile no point lies in makes no build short (Manhattan 14_4823_6159's plans were complete or not by the planning
threads' timing: a deck batch's box took in two z16 tiles that could not be had, 2026-11-18)."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dem as demmod  # noqa: E402
import revisit  # noqa: E402

T = demmod.TILE


class StandInDem(demmod.Dem):
    """No network: tiles in `failing` are made as stand-ins for a download that failed (a fault, as netfetch's), the
    others are a level plane at their x. hold: an Event a stand-in's load waits for (the other thread's timing)."""

    def __init__(self, cache_dir, failing=(), hold=None):
        super().__init__(cache_dir, fetch_workers=4)
        self.failing, self.hold = set(failing), hold
        self.loaded = []
        self.loaded_lock = threading.Lock()

    def _load_tile(self, key):
        with self.loaded_lock:
            self.loaded.append(key)
        if key in self.failing:
            if self.hold is not None:
                self.hold.wait(10.0)
            revisit.fault()             # (netfetch.Fetcher.get: the download failed)
            return np.zeros((T, T), np.float32)
        return np.full((T, T), float(key[1]), np.float32)


def in_thread(fn):
    """fn() in a thread of its own, in a build scope of its own: (its result, its Build)."""
    out = {}

    def run():
        out["r"] = revisit.scoped_call(fn)
    t = threading.Thread(target=run)
    t.start()
    t.join(30.0)
    return out["r"]


class KeptStandInTest(unittest.TestCase):
    # (zoom 12 keys: under the regional archives, so no archive or file list is asked, no module state set)
    def test_later_reader_of_a_kept_stand_in_is_short(self):
        key = (12, 5, 7)
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d, failing={key})
            _r, first = in_thread(lambda: dem.tiles([key]))
            self.assertFalse(first.complete)
            # (another cell's planning thread, later: the tile comes from memory, not loaded again)
            _r, later = in_thread(lambda: dem.tiles([key]))
            self.assertEqual(dem.loaded, [key])
            self.assertFalse(later.complete, "a build that read the kept stand-in counted as complete")
            _r, clean = in_thread(lambda: dem.tiles([(12, 6, 7)]))
            self.assertTrue(clean.complete)

    def test_waiter_on_the_same_load_is_short(self):
        key = (12, 5, 7)
        with tempfile.TemporaryDirectory() as d:
            hold = threading.Event()
            dem = StandInDem(d, failing={key}, hold=hold)
            res = {}

            def first():
                res["a"] = revisit.scoped_call(lambda: dem.tiles([key]))
            t = threading.Thread(target=first)
            t.start()
            for _ in range(200):            # (the first thread's load is under way: the second waits for it)
                if dem.loaded:
                    break
                threading.Event().wait(0.01)
            waiter = {}

            def second():
                waiter["b"] = revisit.scoped_call(lambda: dem.tiles([key]))
            t2 = threading.Thread(target=second)
            t2.start()
            threading.Event().wait(0.05)
            hold.set()
            t.join(10.0)
            t2.join(10.0)
            self.assertEqual(dem.loaded, [key])
            self.assertFalse(res["a"][1].complete)
            self.assertFalse(waiter["b"][1].complete)

    def test_ancestor_stand_in_kept(self):
        """A deeper tile upsampled from a stand-in ancestor (_from_ancestor) is short too: an inline stand-in (not
        kept: made again by the next reader, short again) and one kept in the LRU by its owner's load."""
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d, failing={(12, 2, 2)})
            _r, first = in_thread(lambda: dem._from_ancestor(13, 4, 4, 12))
            self.assertFalse(first.complete)
            _r, later = in_thread(lambda: dem._from_ancestor(13, 5, 5, 12))
            self.assertFalse(later.complete)
            self.assertEqual(dem.loaded, [(12, 2, 2)] * 2)
            in_thread(lambda: dem.tiles([(12, 2, 2)]))      # (the owner's load: kept, marked)
            _r, kept = in_thread(lambda: dem._from_ancestor(13, 4, 5, 12))
            self.assertFalse(kept.complete)
            self.assertEqual(len(dem.loaded), 3)

    def test_stand_in_made_again_after_retry(self):
        """Kept past SHORT_RETRY_S, a stand-in is made again: once its download works, its readers are complete."""
        from unittest import mock
        key = (12, 5, 7)
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d, failing={key})
            in_thread(lambda: dem.tiles([key]))
            with mock.patch.object(demmod, "SHORT_RETRY_S", -1.0):
                dem.failing.clear()                 # (the host is back)
                _r, again = in_thread(lambda: dem.tiles([key]))
            self.assertEqual(dem.loaded, [key, key])
            self.assertTrue(again.complete)
            self.assertNotIn(key, dem._lru_short)
            _r, later = in_thread(lambda: dem.tiles([key]))
            self.assertTrue(later.complete)
            self.assertEqual(len(dem.loaded), 2)

    def test_child_of_a_kept_short_ancestor_is_a_stand_in(self):
        """A tile upsampled from a short ancestor read from memory is a stand-in itself: never mirrored (where a
        restart would read it back without its marks)."""
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d, failing={(12, 2, 2)})
            in_thread(lambda: dem.tiles([(12, 2, 2)]))      # (kept short by its owner's load)
            seen = {}

            def child():
                dem._local.partial = False
                dem._from_ancestor(13, 5, 5, 12)
                seen["partial"] = dem._local.partial
            _r, b = in_thread(child)
            self.assertTrue(seen["partial"])
            self.assertFalse(b.complete)

    def test_partial_is_a_fault(self):
        """Every stand-in (_partial: a lidar sheet that raised OSError too, no download fault) makes its readers
        short, and its tile is kept marked."""
        class LidarFailDem(StandInDem):
            def _load_tile(self, key):
                self._partial()         # (as _canada_fine on a sheet's OSError)
                return np.zeros((T, T), np.float32)
        with tempfile.TemporaryDirectory() as d:
            dem = LidarFailDem(d)
            _r, first = in_thread(lambda: dem.tiles([(12, 1, 1)]))
            _r, later = in_thread(lambda: dem.tiles([(12, 1, 1)]))
            self.assertFalse(first.complete)
            self.assertFalse(later.complete)
            self.assertIn((12, 1, 1), dem._lru_short)

    def test_stand_in_never_replaces_a_complete_tile(self):
        """A stand-in (a retry that failed again, _from_ancestor's inline load) put after another thread published
        the same tile complete: the complete one stays, unmarked; a complete one replaces a stand-in."""
        key = (12, 5, 7)
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d)
            good = np.full((T, T), 9.0, np.float32)
            with dem._lock:
                dem._lru_put(key, good, None)
                dem._lru_put(key, np.zeros((T, T), np.float32), (1, 0, 0.0))
            self.assertIs(dem._lru[key], good)
            self.assertNotIn(key, dem._lru_short)
            other = (12, 6, 7)
            with dem._lock:
                dem._lru_put(other, np.zeros((T, T), np.float32), (1, 0, 0.0))
                dem._lru_put(other, good, None)
            self.assertIs(dem._lru[other], good)
            self.assertNotIn(other, dem._lru_short)

    def test_inline_ancestor_stand_in_stays_local(self):
        """_from_ancestor's inline load of an ancestor owns no future: while the owner's load of it is pending, an
        inline stand-in is not put in the LRU (tiles() would hand it out ahead of the owner's healthy load); its reader
        is short and its descendant partial; the owner's complete tile is what later readers get."""
        from concurrent.futures import Future
        anc = (12, 2, 2)
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d, failing={anc})
            owner = Future()                        # (the owner's load, still pending)
            dem._inflight[anc] = owner
            seen = {}

            def child():
                dem._local.partial = False
                dem._from_ancestor(13, 4, 4, 12)
                seen["partial"] = dem._local.partial
            _r, b = in_thread(child)
            self.assertFalse(b.complete)
            self.assertTrue(seen["partial"])
            self.assertNotIn(anc, dem._lru)
            self.assertNotIn(anc, dem._lru_short)
            owner.set_result((np.full((T, T), 6.0, np.float32), None))
            dem._finish(anc, owner)
            got, later = in_thread(lambda: dem.tiles([anc]))
            self.assertTrue(later.complete)
            self.assertEqual(float(got[anc][0, 0]), 6.0)
            # (a complete inline load is published, as before)
            dem.failing.clear()
            in_thread(lambda: dem._from_ancestor(13, 8, 8, 12))
            self.assertIn((12, 4, 4), dem._lru)
            self.assertNotIn((12, 4, 4), dem._lru_short)

    def test_superseded_load_does_not_publish(self):
        """A load whose key another load took over (the urgent lane's reload) publishes nothing when it finishes."""
        from concurrent.futures import Future
        key = (12, 5, 7)
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d)
            old, new = Future(), Future()
            old.set_result((np.zeros((T, T), np.float32), (1, 0, 0.0)))
            new.set_result((np.full((T, T), 3.0, np.float32), None))
            dem._inflight[key] = new
            dem._finish(key, old)                   # (the superseded one, finishing late)
            self.assertNotIn(key, dem._lru)
            self.assertIs(dem._inflight[key], new)
            dem._finish(key, new)
            self.assertEqual(float(dem._lru[key][0, 0]), 3.0)
            self.assertNotIn(key, dem._inflight)
            # (the reverse order: the urgent reload's stand-in first, then the superseded load's complete tile: the
            # complete one is kept, not the stand-in for its retry window)
            k2 = (12, 6, 7)
            queued, urgent = Future(), Future()
            urgent.set_result((np.zeros((T, T), np.float32), (1, 0, 0.0)))
            queued.set_result((np.full((T, T), 4.0, np.float32), None))
            dem._inflight[k2] = urgent
            dem._finish(k2, urgent)
            self.assertIn(k2, dem._lru_short)
            dem._finish(k2, queued)
            self.assertEqual(float(dem._lru[k2][0, 0]), 4.0)
            self.assertNotIn(k2, dem._lru_short)

    def test_old_mirror_generation_not_read(self):
        """A tile in the mirror's older folder (decoded/v<DECODED_VERSION>/, where a child of a stand-in ancestor may
        have been written as complete before 2026-11-18) is not read: the tile is made again."""
        from unittest import mock
        key = (12, 5, 7)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(demmod, "MIRROR_MB", 100.0):
            dem = StandInDem(d)
            new = dem._mirror_path(key)
            self.assertIn(f"v{demmod.DECODED_VERSION}m{demmod.MIRROR_GEN}", new)
            old = new.replace(f"v{demmod.DECODED_VERSION}m{demmod.MIRROR_GEN}", f"v{demmod.DECODED_VERSION}")
            with mock.patch.object(dem, "_mirror_path", lambda k: old):     # (written where the older code wrote)
                self.assertIsNotNone(dem._mirror_put(key, np.full((T, T), 1.0, np.float32)))
                self.assertIsNotNone(dem._mirror_get(key))                  # (the file itself reads)
            self.assertIsNone(dem._mirror_get(key))

    def test_evicted_marks_go(self):
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d, failing={(12, 0, 0)})
            dem.mem_tiles = 2
            in_thread(lambda: dem.tiles([(12, 0, 0)]))
            in_thread(lambda: dem.tiles([(12, 1, 0), (12, 2, 0)]))
            self.assertNotIn((12, 0, 0), dem._lru)
            self.assertEqual(dem._lru_short, {})


class SampleReadsOnlyItsTilesTest(unittest.TestCase):
    def _lat_lon(self, z, px, py):
        """lat, lon of global pixel (px, py) at zoom z (the inverse of Dem.sample's projection)."""
        w = T * 2 ** z
        lon = ((px + 0.5) / w - 0.5) * 360.0
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (py + 0.5) / w))))  # det-ok: a test's points
        return lat, lon

    def test_unread_box_tile_not_asked(self):
        z = 12                          # (under the regional archives' zooms: no archive read)
        x0, y0 = 2000, 1500
        # two points, in tiles (x0, y0) and (x0 + 1, y0 + 1): their box has (x0 + 1, y0) and (x0, y0 + 1) too
        pa = self._lat_lon(z, x0 * T + 100.3, y0 * T + 200.6)
        pb = self._lat_lon(z, (x0 + 1) * T + 300.2, (y0 + 1) * T + 50.9)
        lat, lon = np.array([pa[0], pb[0]]), np.array([pa[1], pb[1]])
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d, failing={(z, x0 + 1, y0), (z, x0, y0 + 1)})
            got, b = in_thread(lambda: dem.sample(lat, lon, z))
            self.assertTrue(b.complete, "a failed tile no point reads made the sample short")
            self.assertEqual(sorted(dem.loaded), [(z, x0, y0), (z, x0 + 1, y0 + 1)])
            self.assertEqual(got.tolist(), [float(x0), float(x0 + 1)])
            # (the same values as the whole box's mosaic: the kernel never read the tiles left out)
            full = StandInDem(d)
            np.testing.assert_array_equal(full.sample(lat, lon, z), got)

    def test_edge_point_reads_its_neighbour(self):
        """A point on a tile's last pixel column reads the next tile's first (bilinear): that tile is asked."""
        z = 12                          # (under the regional archives' zooms: no archive read)
        x0, y0 = 2000, 1500
        pa = self._lat_lon(z, x0 * T + T - 1 + 0.4, y0 * T + 10.2)
        pb = self._lat_lon(z, x0 * T + 3.1, (y0 + 1) * T + 7.7)
        lat, lon = np.array([pa[0], pb[0]]), np.array([pa[1], pb[1]])
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d, failing={(z, x0 + 1, y0)})
            _got, b = in_thread(lambda: dem.sample(lat, lon, z))
            self.assertIn((z, x0 + 1, y0), dem.loaded)
            self.assertFalse(b.complete)
            self.assertNotIn((z, x0 + 1, y0 + 1), dem.loaded)

    def test_asks_exactly_the_tiles_read(self):
        """earthfast.dem_sample asks for exactly the tiles holding each point's pixel and the next one right and down
        (the bilinear's four), at random points of a 3 x 3 box, tile edges and corners included; its bilinear never
        reads a tile it did not ask (it would raise), and the heights are the planes' own."""
        z = 12
        x0, y0 = 2000, 1500
        rng = np.random.default_rng(11)
        offs = [0.4, 0.6, T - 1.4, T - 0.6, T - 0.4, 200.3]
        for _ in range(100):
            m = int(rng.integers(1, 6))
            px = np.array([(x0 + int(rng.integers(0, 3))) * T + float(rng.choice(offs)) for _ in range(m)])
            py = np.array([(y0 + int(rng.integers(0, 3))) * T + float(rng.choice(offs)) for _ in range(m)])
            lat, lon = self._lat_lon(z, px, py)
            with tempfile.TemporaryDirectory() as d:
                dem = StandInDem(d)
                got = dem.sample(lat, lon, z)
            want = {(z, int(np.floor(x) + dx) // T, int(np.floor(y) + dy) // T)
                    for x, y in zip(px, py) for dx in (0, 1) for dy in (0, 1)}
            self.assertEqual(set(dem.loaded), want, (px, py))
            self.assertEqual(len(dem.loaded), len(want))
            self.assertTrue(np.isfinite(got).all())

    def test_bad_points(self):
        """A longitude that is not finite raises ValueError (math.floor's, as before the port); a latitude that is not
        finite goes to the polar DEM with the caps (sea level when it can't be had), the other points still sampled."""
        z = 12
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d)
            for bad in (np.nan, np.inf, -np.inf):
                with self.assertRaises(ValueError):
                    dem.sample(np.array([40.0, 40.0]), np.array([-73.9, bad]), z)
            def no_polar():
                raise OSError("no polar DEM here")
            dem.polar = no_polar
            with self.assertLogs("earthd.dem", "WARNING"):
                got = dem.sample(np.array([np.nan, 40.0, 89.0]), np.array([-73.9, -73.9, 0.0]), z)
            x = int(np.floor((-73.9 / 360.0 + 0.5) * 2 ** z))
            self.assertEqual(got.tolist(), [0.0, float(x), 0.0])

    def test_fetch_generator(self):
        """dem_sample takes the fetch callback's whole result before it borrows any tile: a generator works, and one
        that changes a tile it already yielded (resized: its memory moved) raises a Python exception instead of aborting the process
        (a borrow held while the generator ran found its array changed under it)."""
        z = 12
        x0, y0 = 2000, 1500
        pa = self._lat_lon(z, x0 * T + 100.3, y0 * T + 200.6)
        pb = self._lat_lon(z, (x0 + 1) * T + 300.2, (y0 + 1) * T + 50.9)
        lat, lon = np.array([pa[0], pb[0]]), np.array([pa[1], pb[1]])

        class GenDem(StandInDem):
            reshape = False

            def _sample_tiles(self, keys, progress):
                got = self.tiles(keys, progress)
                first = None
                for k in keys:
                    a = np.array(got[k], np.float32)
                    if first is None:
                        first = a
                    elif self.reshape:
                        first.resize((T, T // 2), refcheck=False)   # (its memory reallocated)
                    yield a

        with tempfile.TemporaryDirectory() as d:
            dem = GenDem(d)
            self.assertEqual(dem.sample(lat, lon, z).tolist(), [float(x0), float(x0 + 1)])
            dem.reshape = True
            with self.assertRaises((TypeError, ValueError)):
                dem.sample(lat, lon, z)

    def test_unaligned_subclass_tile(self):
        """A tile that is an unaligned view of an ndarray subclass whose copy() hands back itself is copied by
        numpy.array, not its own copy(): the heights are the tile's."""
        import fast

        class Selfish(np.ndarray):
            def copy(self, *a, **k):
                return self

        z = 12
        lat, lon = self._lat_lon(z, 2000 * T + 100.3, 1500 * T + 200.6)
        buf = np.zeros(T * T * 4 + 1, np.uint8)
        tile = np.frombuffer(buf.data, np.float32, T * T, 1).reshape(T, T).view(Selfish)
        self.assertFalse(tile.flags.aligned)
        got = fast._ef.dem_sample(np.array([lat]), np.array([lon]), z, T, 84.9, 1024, 16, 4, 2,
                                  lambda keys: [tile for _ in keys], lambda la, lo: None)
        self.assertEqual(got.tolist(), [0.0])

    def test_borrow_api_set_up_before_checks(self):
        """rust-numpy's borrow checking is set up (its first use imports numpy's multiarray module and looks a capsule
        up: Python hooks can run) before a caller's array is checked: a hook there that changes the input array makes
        the check refuse it (TypeError), never a borrow of an array changed after its check (a fresh process: the set
        up happens once)."""
        import subprocess
        code = r'''
import sys
sys.path.insert(0, sys.argv[1])
import numpy as np
import fast
try:
    import numpy._core.multiarray as ma
except ImportError:
    import numpy.core.multiarray as ma
lat, lon = np.array([40.0, 40.001]), np.array([-73.9, -73.9])
called = []
def hook(name):
    if name == "_RUST_NUMPY_BORROW_CHECKING_API" and not called:
        called.append(name)
        lat.resize((1, 2), refcheck=False)
    raise AttributeError(name)
ma.__getattr__ = hook
try:
    fast._ef.dem_sample(lat, lon, 12, 512, 84.9, 1024, 16, 4, 2,
                        lambda keys: [np.zeros((512, 512), np.float32) for _ in keys], lambda a, b: None)
    print("RESULT ok", len(called))
except (TypeError, ValueError) as e:
    print("RESULT", type(e).__name__, len(called))
'''
        r = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parents[1])],
                           capture_output=True, text=True, timeout=120)
        self.assertIn("RESULT TypeError 1", r.stdout, r.stdout + r.stderr[-2000:])

    def test_snapshot_budget(self):
        """The tiles are copied out for a bilinear without the GIL only within a byte budget: a huge tile that is one
        broadcast scalar (4 TiB as a copy) and a box of 81 tiles (81 MiB) are read in place, with the same heights."""
        import fast
        lat = np.linspace(10.0, 11.0, 5000)
        lon = np.linspace(20.0, 21.0, 5000)
        big = np.broadcast_to(np.float32(5.0), (1 << 20, 1 << 20))
        got = fast._ef.dem_sample(lat, lon, 0, 1 << 20, 84.9, 1024, 16, 4, 2,
                                  lambda keys: [big for _ in keys], lambda a, b: None)
        self.assertEqual(set(got.tolist()), {5.0})
        z, x0, y0 = 12, 2000, 1500
        px, py = np.meshgrid(np.linspace(x0 * T + 0.7, (x0 + 9) * T - 1.3, 300),
                             np.linspace(y0 * T + 0.7, (y0 + 9) * T - 1.3, 300))
        lat, lon = self._lat_lon(z, px.ravel(), py.ravel())
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d)
            whole = dem.sample(lat, lon, z)                      # (90,000 points on 81 tiles: in place)
            self.assertEqual(len(set(dem.loaded)), 81)
            parts = np.concatenate([dem.sample(lat[i:i + 900], lon[i:i + 900], z) for i in range(0, len(lat), 900)])
        np.testing.assert_array_equal(whole, parts)

    def test_shapes(self):
        """Broadcast lat / lon in, their shape out (a scalar: a 0-d array), float64."""
        z = 12
        with tempfile.TemporaryDirectory() as d:
            dem = StandInDem(d)
            self.assertEqual(dem.sample(40.0, -73.9, z).shape, ())
            g = dem.sample(np.array([[40.0], [40.01]]), np.array([-73.9, -73.8, -73.7]), z)
            self.assertEqual((g.shape, g.dtype), ((2, 3), np.float64))
            self.assertEqual(dem.sample(np.zeros(0), np.zeros(0), z).shape, (0,))
            # (coordinates in a packed buffer, not aligned: copied before the kernel reads them)
            lat = np.frombuffer(b"\0" + np.array([40.0, 40.01]).tobytes(), np.float64, 2, 1)
            lon = np.frombuffer(b"\0" + np.array([-73.9, -73.8]).tobytes(), np.float64, 2, 1)
            self.assertFalse(lat.flags.aligned)
            np.testing.assert_array_equal(dem.sample(lat, lon, z), dem.sample(lat.copy(), lon.copy(), z))

if __name__ == "__main__":
    unittest.main()
