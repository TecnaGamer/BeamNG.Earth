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

    def test_tiles_read_matches_kernel(self):
        """_tiles_read's tiles are exactly those whose pixels the kernel reads: NaN tiles in all others leave every
        sample finite, at random points of a 3 x 3 box, edges and corners included."""
        import fast
        rng = np.random.default_rng(11)
        for _ in range(200):
            nx, ny = int(rng.integers(1, 4)), int(rng.integers(1, 4))
            if nx * ny < 2:
                nx = 2
            m = int(rng.integers(1, 6))
            fx = rng.choice([rng.uniform(-3, nx * T + 3), float(rng.integers(0, nx) * T - 1) + 0.5,
                             float(nx * T - 1)], m) if rng.random() < 0.5 else rng.uniform(-3, nx * T + 3, m)
            fy = rng.uniform(-3, ny * T + 3, m)
            read = demmod._tiles_read(np.asarray(fx, np.float64), fy, nx, ny)
            tiles = [np.ones((T, T), np.float32) if r else np.full((T, T), np.nan, np.float32) for r in read]
            out = fast._ef.bilinear_tiles(tiles, nx, T, np.asarray(fx, np.float64), fy)
            self.assertTrue(np.isfinite(out).all(), (nx, ny, fx, fy, read))


if __name__ == "__main__":
    unittest.main()
