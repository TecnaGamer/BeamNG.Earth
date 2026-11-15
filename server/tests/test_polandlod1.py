"""polandlod1 (GUGiK LOD1 footprints, BNE_PL_LOD1) and pl1992 (EPSG:2180 without pyproj).

The dataset is 250 MB, so the format is exercised with a small file written here by a minimal encoder; the decoder's
reading of "ortho" rings (angle unit, `swap`) was checked on the real tiles (Wroclaw old town) when it was written.
The index and the files come from a fake netfetch.Fetcher that serves them: no test touches the network.
"""
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapcells  # noqa: E402
import netfetch  # noqa: E402
import pl1992  # noqa: E402
import polandlod1  # noqa: E402


def uv(n):
    out = bytearray()
    while True:
        b = n & 127
        n >>= 7
        out.append(b | (128 if n else 0))
        if not n:
            return bytes(out)


def sv(n):
    return uv((n << 1) ^ (n >> 63))


def stream(b):
    return uv(len(b)) + b


def ring(n, hole=False, ortho=False):
    return uv((n << 2) | (int(hole) << 1) | int(ortho))


def make_file(deflate, origin=(362000, 362000)):
    """One tile, one block, three buildings (grid step 10 cm: 10 steps a metre):
    A  plain 20 x 10 m outer ring with a 4 x 4 m hole, height 12 m
    B  ortho 10 x 5 m, axis-aligned (k 0, swap 0), height 6 m
    C  ortho 10 x 5 m turned 45 degrees (k 8192), swap 1, height 6 m."""
    ax, ay = [sv(0), sv(500), sv(500)], [sv(0), sv(0), sv(0)]      # anchors: (0,0) (500,0) (1000,0) grid steps
    heights = uv(2) + uv(120) + uv(60) + uv(0) + uv(1) + uv(1)
    rings = (uv(2) + ring(4) + ring(4, hole=True)) + (uv(1) + ring(4, ortho=True)) + (uv(1) + ring(4, ortho=True))
    v = b""
    for dx, dy in ((200, 0), (0, 100), (-200, 0)):                      # A outer: (0,0) (200,0) (200,100) (0,100)
        v += sv(dx) + sv(dy)
    v += sv(50) + sv(30)                                                # hole start vs ring start: (50, 30)
    for dx, dy in ((40, 0), (0, 40), (-40, 0)):
        v += sv(dx) + sv(dy)
    v += uv((0 << 1) | 0) + sv(100) + sv(50)                            # B: k 0, swap 0, steps 10 m, 5 m
    v += uv((8192 << 1) | 1) + sv(100) + sv(50)                         # C: k 8192, swap 1
    raw = b"".join(stream(s) for s in (b"".join(ax), b"".join(ay), sv(0) * 3, heights, rings, v))
    block = zlib.compressobj(wbits=-15) if deflate else None
    stored = (lambda c: c.compress(raw) + c.flush())(block) if deflate else raw
    head = b"GBLD" + bytes([2, 1 if deflate else 0, 10, 10]) + uv(1)
    head += sv(origin[0]) + sv(origin[1]) + uv(3000) + uv(3000) + uv(3)
    head += uv(1) + uv(1) + uv((len(stored) << 1) | int(deflate))
    return head + stored


def index_bytes(folder, files):
    """The repository's index.npz for these (name, bytes) files, as tools/build_polandlod1_index.py writes it."""
    src = Path(folder, "src")
    src.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, data in files:
        (src / name).write_bytes(data)
        paths.append(str(src / name))
    out = Path(folder, "index_built.npz")
    np.savez_compressed(out, **polandlod1.make_index(paths))
    return out.read_bytes()


class FakeFetcher:
    """Serves bodies by URL and records the calls (url, kind, lane); `errors` (url -> exception) are raised instead."""

    def __init__(self, bodies=None, errors=None):
        self.bodies, self.errors, self.calls = dict(bodies or {}), dict(errors or {}), []
        self.lock = threading.Lock()

    def get(self, url, *, kind="demand", headers=None, lane=None):
        with self.lock:
            self.calls.append((url, kind, lane))
        time.sleep(0.05)                    # (so that threads asking for the same file overlap)
        if url in self.errors:
            raise self.errors[url]
        if url not in self.bodies:
            raise netfetch.NotFound(f"{url}: HTTP 404", url, "raw.githubusercontent.com", status=404)
        return types.SimpleNamespace(status=200, body=self.bodies[url], headers={})

    def urls(self):
        return [c[0] for c in self.calls]


class Isolated(unittest.TestCase):
    """polandlod1's module state and environment, put back afterwards; the border check says "all in Poland"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.environ.pop(polandlod1.ENV, None)
        self.saved = (polandlod1._index, dict(polandlod1._state), polandlod1._in_poland_only, polandlod1.INDEX_SHA256)
        polandlod1._index = None
        polandlod1._covers.clear()
        polandlod1._wanted.clear()
        polandlod1._in_poland_only = lambda lats, lons: True

    def tearDown(self):
        polandlod1._index, state, polandlod1._in_poland_only, polandlod1.INDEX_SHA256 = self.saved
        polandlod1._state.update(state)
        polandlod1._covers.clear()
        polandlod1._wanted.clear()
        if self.env is None:
            os.environ.pop(polandlod1.ENV, None)
        else:
            os.environ[polandlod1.ENV] = self.env
        self.tmp.cleanup()

    def fresh(self):
        """A new process' view: the Index again, the files on disk as they are."""
        polandlod1._index = None
        polandlod1._covers.clear()
        polandlod1._wanted.clear()


class Served(Isolated):
    """BNE_PL_LOD1=1 with a cache folder, and a fake GitHub that has one county file (a cell's worth of buildings)
    and the index that lists it."""
    NAME = "0001_M-33.bld"

    def setUp(self):
        super().setUp()
        self.key = mapcells.cell_of(51.1, 17.0)
        cx, cy = pl1992.from_lonlat(self.key.center()[1], self.key.center()[0])   # the fixture sits mid-cell
        self.origin = (int(round(float(cx))) - 60, int(round(float(cy))) - 10)
        self.body = make_file(True, self.origin)
        self.index = index_bytes(self.tmp.name, [(self.NAME, self.body)])
        polandlod1.INDEX_SHA256 = hashlib.sha256(self.index).hexdigest()         # (the pin: this test's index)
        self.url = polandlod1.Manifest.url(self.NAME)
        self.fetcher = FakeFetcher({self.url: self.body, polandlod1.INDEX_URL: self.index})
        self.cache = os.path.join(self.tmp.name, "cache")
        polandlod1.configure(self.cache, self.fetcher)
        os.environ[polandlod1.ENV] = "1"
        self.folder = os.path.join(self.cache, "polandlod1")
        self.file = os.path.join(self.folder, f"{self.NAME}.{hashlib.sha256(self.body).hexdigest()[:16]}")
        self.index_name = f"index.{polandlod1.INDEX_SHA256[:16]}.npz"
        self.index_file = os.path.join(self.folder, self.index_name)

    def place(self, index=True, file=True):
        os.makedirs(self.folder, exist_ok=True)
        if index:
            Path(self.index_file).write_bytes(self.index)
        if file:
            Path(self.file).write_bytes(self.body)


class Projection(unittest.TestCase):
    def test_central_meridian(self):
        x, y = pl1992.from_lonlat(19.0, 0.0)
        self.assertAlmostEqual(float(x), 500000.0, 6)
        self.assertAlmostEqual(float(y), -5300000.0, 6)

    def test_scale_and_roundtrip(self):
        _, y1 = pl1992.from_lonlat(19.0, 51.0)
        _, y2 = pl1992.from_lonlat(19.0, 52.0)
        self.assertLess(abs(float(y2 - y1) - 111250 * 0.9993), 150)       # a degree of latitude, times k0
        lon = np.array([14.2, 17.03, 19.0, 21.0, 24.1])
        lat = np.array([50.0, 51.11, 52.0, 52.23, 54.3])
        x, y = pl1992.from_lonlat(lon, lat)
        lo, la = pl1992.to_lonlat(x, y)
        self.assertLess(float(np.abs(lo - lon).max()), 1e-9)
        self.assertLess(float(np.abs(la - lat).max()), 1e-9)

    def test_x_is_east(self):
        lon, lat = pl1992.to_lonlat(362294.0, 362199.0)                 # Wroclaw's market square
        self.assertAlmostEqual(float(lon), 17.03, delta=0.02)
        self.assertAlmostEqual(float(lat), 51.11, delta=0.02)


class Decode(unittest.TestCase):
    def blocks(self, deflate):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.bld")
            Path(p).write_bytes(make_file(deflate))
            h = polandlod1.read_header(p)
            raw = Path(p).read_bytes()[h.block_offsets[0]:h.block_offsets[0] + h.blocks[0][1]]
            return polandlod1.decode_block(zlib.decompress(raw, -15) if deflate else raw, h, 0, 1)

    def check(self, b):
        self.assertEqual(len(b.bld_ring_off) - 1, 3)
        r = lambda i: b.coords[b.ring_off[i]:b.ring_off[i + 1]]
        np.testing.assert_allclose(r(0), [[362000, 362000], [362020, 362000], [362020, 362010], [362000, 362010]])
        self.assertEqual(list(b.ring_hole), [False, True, False, False])
        np.testing.assert_allclose(r(1)[0], [362005, 362003])           # a hole starts relative to its outer ring
        np.testing.assert_allclose(r(2), [[362050, 362000], [362060, 362000], [362060, 362005], [362050, 362005]],
                                   atol=1e-9)
        c = r(3)                                                        # 10 x 5 m turned 45 deg, first edge along y'
        self.assertEqual(len(c), 4)
        np.testing.assert_allclose(c[1] - c[0], 10 * np.array([-math.sin(math.pi / 4), math.cos(math.pi / 4)]),
                                   atol=1e-9)
        np.testing.assert_allclose(c[2] - c[1], 5 * np.array([math.cos(math.pi / 4), math.sin(math.pi / 4)]),
                                   atol=1e-9)
        np.testing.assert_allclose(c[3] - c[2], -(c[1] - c[0]), atol=1e-9)      # a rectangle: it closes
        np.testing.assert_allclose(b.height, [12.0, 6.0, 6.0])
        self.assertEqual(b.anchor.tolist(), [[3620000, 3620000], [3620500, 3620000], [3621000, 3620000]])

    def test_raw_block(self):
        self.check(self.blocks(False))

    def test_deflated_block(self):
        self.check(self.blocks(True))


class CellQuery(Served):
    """The index and the file are already in the cache folder: nothing is fetched."""

    def setUp(self):
        super().setUp()
        self.place()

    def test_features(self):
        f, sources = polandlod1.query(self.key)
        self.assertEqual(len(f), 3)
        self.assertEqual(list(f.ids), sorted(f.ids))
        self.assertEqual(sources, {(polandlod1.DATASET, "open data (GUGiK)"): 3})
        self.assertEqual(sorted(f.attrs["height"].tolist()), [6.0, 6.0, 12.0])
        self.assertEqual(set(f.attrs["src_dataset"]), {polandlod1.DATASET})
        a = f.ids.tolist().index(f"pl1:{self.origin[0] * 10:08d}{self.origin[1] * 10:08d}")
        parts = f.parts(a)
        self.assertEqual(len(parts), 1)
        self.assertEqual(len(parts[0]), 2)                              # outer ring and its hole
        self.assertTrue(np.allclose(parts[0][0][0], parts[0][0][-1]))   # closed
        lon, lat = pl1992.to_lonlat(float(self.origin[0]), float(self.origin[1]))
        np.testing.assert_allclose(parts[0][0][0], [lon, lat], atol=1e-9)
        self.assertEqual(self.fetcher.calls, [])

    def test_columns_are_overtures(self):
        import overture
        f, _ = polandlod1.query(self.key)
        spec = overture.LAYERS["buildings"]
        want = {"cx", "cy"} | {c for k in ("text", "float", "bool") for c in spec.get(k, ())}
        self.assertEqual(set(f.attrs), want)
        for c in spec.get("float", ()):
            self.assertEqual(f.attrs[c].dtype, overture.empty("buildings").attrs[c].dtype)

    def test_cache_format_roundtrip(self):
        import mapgeom
        f, _ = polandlod1.query(self.key)
        g = mapgeom.Features.from_arrays("buildings", f.to_arrays(""), "")
        self.assertEqual(list(g.ids), list(f.ids))
        np.testing.assert_array_equal(g.lonlat, f.lonlat)

    def test_cell_without_tiles_or_outside_poland_is_not_covered(self):
        far = mapcells.cell_of(52.5, 20.5)                              # in Poland, no tile in this index
        self.assertFalse(polandlod1.covers(far))
        self.assertFalse(polandlod1.resolve(far))
        self.assertIsNone(polandlod1.query(far))
        self.assertFalse(polandlod1.routes_to_lod1(far))                # (the index is loaded: Overture's path)
        polandlod1._in_poland_only = lambda lats, lons: False           # a border cell
        polandlod1._wanted.clear()
        polandlod1._covers.clear()
        self.assertFalse(polandlod1.covers(self.key))
        self.assertFalse(polandlod1.resolve(self.key))
        self.assertIsNone(polandlod1.query(self.key))
        self.assertFalse(polandlod1.routes_to_lod1(self.key))

    def test_signature_is_the_setting_and_the_pin(self):
        sig = polandlod1.signature()
        self.assertEqual(sig, {"version": polandlod1.VERSION, "commit": polandlod1.COMMIT[:12],
                               "index": polandlod1.INDEX_SHA256[:16]})
        self.assertRegex(sig["index"], r"^[0-9a-f]{16}$")

    def test_the_answer_asked_for_is_the_answer_acted_on(self):
        self.assertTrue(polandlod1.resolve(self.key))
        polandlod1._covers.clear()
        polandlod1._in_poland_only = lambda lats, lons: self.fail("asked again")    # (query takes `covered` as given)
        self.assertEqual(len(polandlod1.query(self.key, covered=True)[0]), 3)


class Download(Served):
    """Nothing in the cache folder: the index and the file come from the (fake) repository, once, and are checked."""

    def test_urls_are_pinned_to_a_commit(self):
        self.assertRegex(polandlod1.COMMIT, r"^[0-9a-f]{40}$")
        base = f"https://raw.githubusercontent.com/maalos/LOD1_2024_PL/{polandlod1.COMMIT}"
        self.assertEqual(polandlod1.INDEX_URL, f"{base}/index.npz")
        self.assertEqual(polandlod1.Manifest.url("0201_M-33.bld"), f"{base}/bld/0201_M-33.bld")
        self.assertNotIn("master", polandlod1.BASE_URL)
        self.assertNotIn("refs/heads", polandlod1.BASE_URL)

    def test_a_name_is_quoted_in_the_url(self):
        self.assertTrue(polandlod1.Manifest.url("a b/../x?y#z.bld").endswith("/bld/a%20b%2F..%2Fx%3Fy%23z.bld"))

    def test_covered_needs_only_the_index(self):
        self.assertFalse(polandlod1.covers(self.key))                   # not loaded: covers() never fetches
        self.assertEqual(self.fetcher.calls, [])
        self.assertTrue(polandlod1.resolve(self.key))                   # this fetches it
        self.assertTrue(polandlod1.covers(self.key))
        self.assertTrue(polandlod1.enabled())
        self.assertEqual(self.fetcher.urls(), [polandlod1.INDEX_URL])
        self.assertFalse(os.path.exists(self.file))

    def test_first_query_fetches_index_and_file_once(self):
        f, sources = polandlod1.query(self.key, "prefetch")
        self.assertEqual(len(f), 3)
        self.assertEqual(self.fetcher.calls, [(polandlod1.INDEX_URL, "prefetch", polandlod1.LANE),   # (the build's own kind)
                                              (self.url, "prefetch", polandlod1.LANE)])
        self.assertEqual(Path(self.file).read_bytes(), self.body)
        self.assertEqual(Path(self.index_file).read_bytes(), self.index)
        self.assertEqual(sorted(os.listdir(self.folder)), sorted([os.path.basename(self.file), self.index_name]))
        polandlod1.query(self.key)
        self.fresh()
        polandlod1.query(self.key)                                      # a restart: all on disk
        self.assertEqual(len(self.fetcher.calls), 2)

    def test_a_file_of_another_size_is_fetched_again(self):
        self.place(file=False)
        Path(self.file).write_bytes(b"half a file")
        self.assertEqual(len(polandlod1.query(self.key)[0]), 3)
        self.assertEqual(self.fetcher.urls(), [self.url])
        self.assertEqual(Path(self.file).read_bytes(), self.body)

    def test_a_download_that_is_not_the_listed_file_is_refused(self):
        self.place(file=False)
        bad = bytearray(self.body)
        bad[-1] ^= 1                                                    # same size, other content
        self.fetcher.bodies[self.url] = bytes(bad)
        with self.assertRaises(netfetch.BadResponse):
            polandlod1.query(self.key)
        self.assertEqual(os.listdir(self.folder), [self.index_name])
        self.fetcher.bodies[self.url] = self.body
        self.assertEqual(len(polandlod1.query(self.key)[0]), 3)         # asked again, it works

    def test_a_failed_file_download_defers_the_cell_instead_of_using_overture(self):
        self.place(file=False)
        self.fetcher.errors[self.url] = netfetch.HostUnavailable("raw.githubusercontent.com unavailable", self.url,
                                                                 "raw.githubusercontent.com")
        with self.assertRaises(netfetch.HostUnavailable):
            polandlod1.query(self.key)
        self.assertTrue(polandlod1.covers(self.key))                    # still LOD1's cell: never Overture's
        self.assertEqual(os.listdir(self.folder), [self.index_name])
        self.fetcher.errors.clear()
        self.assertEqual(len(polandlod1.query(self.key)[0]), 3)

    def test_threads_share_one_download(self):
        self.place(file=False)
        results = []
        ts = [threading.Thread(target=lambda: results.append(len(polandlod1.query(self.key)[0]))) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results, [3] * 6)
        self.assertEqual(self.fetcher.urls(), [self.url])

    def test_the_cache_identity_does_not_depend_on_what_is_on_disk(self):
        before = polandlod1.signature()
        self.assertEqual(self.fetcher.calls, [])                        # (and asking took no request)
        polandlod1.query(self.key)
        self.fresh()
        self.assertEqual(polandlod1.signature(), before)

    def test_without_an_index_the_cell_waits_it_is_not_built_from_overture(self):
        self.fetcher.errors[polandlod1.INDEX_URL] = netfetch.HostUnavailable("down", polandlod1.INDEX_URL,
                                                                             "raw.githubusercontent.com")
        before = polandlod1.signature()
        with self.assertRaises(netfetch.HostUnavailable):
            polandlod1.resolve(self.key)
        self.assertTrue(polandlod1.enabled())                           # keyed on the setting, not on the index
        self.assertEqual(polandlod1.signature(), before)
        self.assertFalse(polandlod1.covers(self.key))
        self.assertTrue(polandlod1.routes_to_lod1(self.key))            # its cache path is LOD1's, not Overture's
        with self.assertRaises(netfetch.HostUnavailable):
            polandlod1.query(self.key)                                  # (an error, not None: Overture does not serve)
        self.assertEqual(len(self.fetcher.calls), 1)                    # the failure is remembered: not asked again at once
        self.fetcher.errors.clear()
        polandlod1._state["retry_at"] = 0.0                             # (a minute later)
        self.assertEqual(len(polandlod1.query(self.key)[0]), 3)

    def test_a_bad_index_is_not_kept(self):
        self.fetcher.bodies[polandlod1.INDEX_URL] = b"<html>not an index</html>"
        with self.assertRaises(netfetch.BadResponse):
            polandlod1.resolve(self.key)
        self.assertFalse(os.path.exists(self.index_file))
        self.assertEqual(os.listdir(self.folder) if os.path.isdir(self.folder) else [], [])

    def test_an_index_that_is_not_the_pinned_one_is_refused(self):
        other = index_bytes(os.path.join(self.tmp.name, "other"), [("0002_M-33.bld", self.body)])
        self.fetcher.bodies[polandlod1.INDEX_URL] = other               # a valid index, not the pinned bytes
        with self.assertRaises(netfetch.BadResponse):
            polandlod1.resolve(self.key)
        self.assertFalse(polandlod1.enabled() and polandlod1.covers(self.key))
        self.assertEqual(os.listdir(self.folder) if os.path.isdir(self.folder) else [], [])

    def test_a_cached_index_that_is_not_the_pin_is_dropped_and_fetched_again(self):
        self.place(index=False, file=False)
        Path(self.index_file).write_bytes(self.index[:-1] + bytes([self.index[-1] ^ 1]))
        self.assertIsNone(polandlod1.index())
        self.assertFalse(os.path.exists(self.index_file))
        self.assertTrue(polandlod1.resolve(self.key))
        self.assertEqual(Path(self.index_file).read_bytes(), self.index)

    def test_the_index_is_never_fetched_again_it_is_pinned(self):
        self.place()
        old = time.time() - 400 * 86400
        os.utime(self.index_file, (old, old))
        self.assertTrue(polandlod1.resolve(self.key))
        self.assertEqual(self.fetcher.calls, [])

    def test_a_pin_change_leaves_no_old_files_behind(self):
        self.place()
        stale = [os.path.join(self.folder, n) for n in (self.NAME, f"{self.NAME}.0123456789abcdef", "index.npz")]
        for p in stale:
            Path(p).write_bytes(b"old")
        os.remove(self.file)
        polandlod1.query(self.key)                                      # fetches the file: its older versions go
        self.assertEqual(sorted(os.listdir(self.folder)), sorted([os.path.basename(self.file), self.index_name, "index.npz"]))

    def test_the_versions_path_never_waits_for_a_fetch(self):
        """index(), covers(), signature(), routes_to_lod1(): what MapData.versions() / path() call for every tile key,
        anywhere in the world, while some thread is in the middle of a (slow) index fetch."""
        done = []

        def ask():
            polandlod1.index(), polandlod1.signature(), polandlod1.enabled()
            polandlod1.covers(self.key), polandlod1.routes_to_lod1(self.key), polandlod1.wanted(self.key)
            done.append(True)
        with polandlod1._fetch_lock:                                    # (a fetch in progress)
            t = threading.Thread(target=ask)
            t.start()
            t.join(5)
            self.assertEqual(done, [True])
            self.assertFalse(t.is_alive())
        self.assertEqual(self.fetcher.calls, [])

    def test_no_request_runs_under_the_state_lock(self):
        """A slow index request does not hold the lock that versions() and path() need."""
        started, release = threading.Event(), threading.Event()
        slow = self.fetcher.get

        def get(url, **kw):
            started.set()
            release.wait(5)
            return slow(url, **kw)
        self.fetcher.get = get
        t = threading.Thread(target=lambda: polandlod1.resolve(self.key))
        t.start()
        self.assertTrue(started.wait(5))
        try:
            self.assertTrue(polandlod1._index_lock.acquire(timeout=1))   # (free while the request is out)
            polandlod1._index_lock.release()
            self.assertEqual(polandlod1.signature()["version"], polandlod1.VERSION)
        finally:
            release.set()
            t.join(10)
        self.assertTrue(polandlod1.covers(self.key))

    def test_a_fresh_index_is_not_fetched_again(self):
        self.place()
        self.assertTrue(polandlod1.enabled())
        self.assertEqual(self.fetcher.calls, [])

    def test_offline_serves_what_is_on_disk_and_defers_the_rest(self):
        polandlod1.configure(self.cache, netfetch.Fetcher(offline=True))
        with self.assertRaises(netfetch.Offline):                       # no index yet: the cell waits
            polandlod1.query(self.key)
        polandlod1._state["retry_at"] = 0.0
        self.place(file=False)
        with self.assertRaises(netfetch.Offline):                       # an index, but not the file: deferred
            polandlod1.query(self.key)
        self.place()
        self.assertEqual(len(polandlod1.query(self.key)[0]), 3)


class OffByDefault(Isolated):
    def test_off_without_the_variable(self):
        polandlod1.configure(self.tmp.name, FakeFetcher())
        self.assertFalse(polandlod1.enabled())
        self.assertIsNone(polandlod1.signature())
        self.assertFalse(polandlod1.covers(mapcells.cell_of(51.11, 17.03)))
        self.assertEqual(polandlod1._state["fetcher"].calls, [])        # and nothing was fetched

    def test_the_variable_turns_it_on(self):
        for value in ("1", "yes", "TRUE", "on"):
            os.environ[polandlod1.ENV] = value
            self.assertTrue(polandlod1.on(), value)
        for value in ("", "0", "no", "off"):
            os.environ[polandlod1.ENV] = value
            self.assertFalse(polandlod1.on(), value)

    def test_the_folder_is_in_the_cache(self):
        polandlod1.configure(self.tmp.name, FakeFetcher())
        self.assertEqual(polandlod1.folder(), os.path.join(self.tmp.name, "polandlod1"))

    def test_no_local_folder_variable(self):
        self.assertFalse(hasattr(polandlod1, "folder_env"))
        os.environ["BNE_PL_LOD1_DIR"] = self.tmp.name
        try:
            self.assertFalse(polandlod1.on())
        finally:
            os.environ.pop("BNE_PL_LOD1_DIR")

    def test_buildings_trust_the_official_footprints(self):
        import buildings
        self.assertIn(polandlod1.DATASET, buildings.TRUSTED_FOOTPRINTS)
        self.assertIn("OpenStreetMap", buildings.TRUSTED_FOOTPRINTS)


class ThroughTheFetcher(Isolated):
    """The real netfetch.Fetcher (a scripted transport, a fake clock): the lane's own policy and headers."""

    class Transport:
        def __init__(self, bodies):
            self.bodies, self.calls = bodies, []

        def request(self, url, headers, timeout):
            self.calls.append((url, dict(headers), timeout))
            return 200, {"content-type": "application/octet-stream"}, self.bodies[url]

    def test_more_files_than_the_update_checks_daily_budget(self):
        n = 30                                                          # (the host's own budget is 24 a day)
        body = make_file(True)
        names = [f"{i:04d}_M-33.bld" for i in range(n)]
        bodies = {polandlod1.Manifest.url(nm): body for nm in names}
        bodies[polandlod1.INDEX_URL] = index_bytes(self.tmp.name, [(nm, body) for nm in names])
        polandlod1.INDEX_SHA256 = hashlib.sha256(bodies[polandlod1.INDEX_URL]).hexdigest()
        tr = self.Transport(bodies)
        now = [1_000_000.0]

        def sleep(s):
            now[0] += max(0.0, s)
        fetcher = netfetch.Fetcher(clock=lambda: now[0], sleep=sleep, wall=lambda: now[0], transport=tr)
        polandlod1.configure(os.path.join(self.tmp.name, "cache"), fetcher)
        os.environ[polandlod1.ENV] = "1"
        idx = polandlod1.fetch_index()
        for fi in range(n):
            self.assertTrue(os.path.getsize(idx.ensure(fi)) > 0)
        self.assertEqual(len(tr.calls), n + 1)                          # the index and every file
        self.assertTrue(all(h["User-Agent"].startswith("BeamNG.Earth/") for _, h, _ in tr.calls))
        self.assertGreaterEqual(tr.calls[1][2], 60.0)                   # a few MB: the lane's longer timeout


class IndexFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.body = make_file(True)

    def test_make_index_describes_the_files_and_tiles(self):
        import hashlib
        path = os.path.join(self.tmp.name, "index.npz")
        Path(path).write_bytes(index_bytes(self.tmp.name, [("0001_M-33.bld", self.body),
                                                           ("0002_M-33.bld", make_file(False))]))
        m = polandlod1.Manifest(path)
        self.assertEqual(m.names, ["0001_M-33.bld", "0002_M-33.bld"])
        self.assertEqual(m.sizes[0], len(self.body))
        self.assertEqual(m.sha[0], hashlib.sha256(self.body).hexdigest())
        self.assertEqual(m.buildings, [3, 3])
        self.assertEqual(m.tile_file.tolist(), [0, 1])
        self.assertEqual(m.tile_blk.tolist(), [0, 0])
        self.assertEqual((m.x0.tolist(), m.y0.tolist()), ([362000.0] * 2, [362000.0] * 2))
        self.assertEqual((m.x1.tolist(), m.y1.tolist()), ([365000.0] * 2, [365000.0] * 2))
        self.assertRegex(m.digest, r"^[0-9a-f]{64}$")

    def test_the_builder_tool_writes_the_index_into_the_clone(self):
        bld = Path(self.tmp.name, "clone", "bld")
        bld.mkdir(parents=True)
        (bld / "0001_M-33.bld").write_bytes(self.body)
        tool = Path(__file__).resolve().parents[2] / "tools" / "build_polandlod1_index.py"
        r = subprocess.run([sys.executable, "-I", str(tool), str(bld.parent)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        m = polandlod1.Manifest(str(bld.parent / "index.npz"))
        self.assertEqual((m.names, m.buildings), (["0001_M-33.bld"], [3]))


def crafted_index(folder, files, **columns):
    """An index.npz whose file list is `files` ([[name, size, sha, buildings], ...]) as given, whatever it says, with
    the tile columns of a real one (or `columns` over them): what a hostile or broken index would be."""
    base = polandlod1.make_index([_one_file(folder)])
    base["meta"] = np.frombuffer(json.dumps({"files": files}).encode(), np.uint8)
    base.update(columns)
    out = Path(folder, "crafted.npz")
    np.savez_compressed(out, **base)
    return out


def _one_file(folder):
    p = Path(folder, "one.bld")
    p.write_bytes(make_file(True))
    return str(p)


class IndexNames(unittest.TestCase):
    """Every file name of the index goes into a local path and a URL: the whole index is refused for one bad name."""
    SHA = "0" * 64

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def manifest(self, *names):
        return polandlod1.Manifest(crafted_index(self.tmp.name, [[n, 10, self.SHA, 1] for n in names]))

    def test_the_real_names_are_accepted(self):
        m = self.manifest("0001_M-33.bld", "1465_N-34.bld", "3021_N-33.bld")
        self.assertEqual(len(m.names), 3)

    def test_paths_urls_and_other_shapes_are_refused(self):
        for bad in ("../../x", "../0001_M-33.bld", "/etc/passwd", "/0001_M-33.bld", "C:/Users/x/Startup/a.bld",
                    "C:\\x.bld", "0001_M-33.bld/../../x", "bld/0001_M-33.bld", "0001_M-33.bld\n", "0001_M-33.BLD",
                    "0001_m-33.bld", "000_M-33.bld", "00001_M-33.bld", "\u0660\u0660\u0660\u0661_M-33.bld",   # (Arabic-Indic digits)
                    "0001_M-33.bld ", "", ".", "..", "0001_M-33.bld\x00.exe", "https://x/0001_M-33.bld"):
            with self.subTest(name=bad):
                with self.assertRaises(polandlod1.BadData):
                    self.manifest("0002_M-33.bld", bad)                  # one bad name refuses the whole index

    def test_other_malformed_entries_are_refused(self):
        for files in ([["0001_M-33.bld", 10, self.SHA, 1]] * 2,                         # listed twice
                      [["0001_M-33.bld", 0, self.SHA, 1]], [["0001_M-33.bld", True, self.SHA, 1]],
                      [["0001_M-33.bld", polandlod1.MAX_FILE_BYTES + 1, self.SHA, 1]],
                      [["0001_M-33.bld", 10, "xyz", 1]], [["0001_M-33.bld", 10, "A" * 64, 1]],
                      [["0001_M-33.bld", 10, self.SHA]], [], [7], "files"):
            with self.subTest(files=str(files)[:60]):
                with self.assertRaises((polandlod1.BadData, TypeError)):
                    polandlod1.Manifest(crafted_index(self.tmp.name, files))

    def test_tiles_must_name_files_and_blocks_that_exist(self):
        with self.assertRaises(polandlod1.BadData):
            polandlod1.Manifest(crafted_index(self.tmp.name, [["0001_M-33.bld", 10, self.SHA, 1]],
                                              tile_file=np.array([5], np.uint16)))

    def test_a_refused_index_on_disk_is_removed_and_the_feature_waits(self):
        body = crafted_index(self.tmp.name, [["../../x", 10, self.SHA, 1]]).read_bytes()
        saved = polandlod1.INDEX_SHA256
        polandlod1.INDEX_SHA256 = hashlib.sha256(body).hexdigest()      # (even a pinned index is checked)
        self.addCleanup(setattr, polandlod1, "INDEX_SHA256", saved)
        d = os.path.join(self.tmp.name, "folder")
        os.makedirs(d)
        Path(polandlod1._index_path(d)).write_bytes(body)
        with self.assertLogs("polandlod1", "WARNING"):
            self.assertIsNone(polandlod1._read_index(d))
        self.assertEqual(os.listdir(d), [])


class Bounds(unittest.TestCase):
    """What the decoder will do is bounded, and malformed data is one error type."""

    def test_an_inflate_bomb_is_stopped_at_the_limit(self):
        c = zlib.compressobj(wbits=-15)
        bomb = c.compress(b"\0" * (polandlod1.MAX_BLOCK_BYTES * 4)) + c.flush()
        self.assertLess(len(bomb), 100_000)
        with self.assertRaises(polandlod1.BadData):
            polandlod1._inflate(bomb)
        c = zlib.compressobj(wbits=-15)
        ok = c.compress(b"\1" * 1000) + c.flush()
        self.assertEqual(polandlod1._inflate(ok), b"\1" * 1000)

    def test_a_cut_short_stream_is_refused(self):
        c = zlib.compressobj(wbits=-15)
        data = c.compress(os.urandom(5000)) + c.flush()
        with self.assertRaises(polandlod1.BadData):
            polandlod1._inflate(data[:len(data) // 2])

    def test_varints_are_bounded(self):
        for bad in (bytes([255] * 11 + [1]), bytes([200]), bytes([0x80] * 70)):          # too long, or cut off
            with self.assertRaises(polandlod1.BadData):
                polandlod1._varints(bad)
        for bad in (bytes([255] * 11 + [1]), bytes([0x80] * 70)):
            with self.assertRaises(polandlod1.BadData):
                polandlod1._uvarint(bad, 0)

    def header(self, tiles=1, blocks=((1, 10, 0),), extra=b"", n_tiles=None):
        out = b"GBLD" + bytes([2, 0, 10, 10]) + uv(tiles if n_tiles is None else n_tiles)
        out += b"".join(sv(0) + sv(0) + uv(10) + uv(10) + uv(1) for _ in range(tiles))
        out += uv(len(blocks)) + b"".join(uv(t) + uv((ln << 1) | d) for t, ln, d in blocks)
        return out + extra

    def test_header_counts_are_capped(self):
        polandlod1.Header(self.header(1))
        with self.assertRaises(polandlod1.BadData):
            polandlod1.Header(b"GBLD" + bytes([2, 0, 10, 10]) + uv(polandlod1.MAX_TILES + 1))
        with self.assertRaises(polandlod1.BadData):
            polandlod1.Header(self.header(1, blocks=((1, polandlod1.MAX_BLOCK_BYTES + 1, 0),)))
        with self.assertRaises(polandlod1.BadData):                      # blocks that do not add up to the tiles
            polandlod1.Header(self.header(2, blocks=((1, 10, 0),)))
        with self.assertRaises(polandlod1.BadData):
            polandlod1.Header(b"GBLD" + bytes([2, 0, 0, 10]) + uv(0) + uv(0))            # zero grid step
        big = b"GBLD" + bytes([2, 0, 10, 10]) + uv(1) + sv(0) + sv(0) + uv(1) + uv(1) + uv(polandlod1.MAX_TILE_BUILDINGS + 1)
        with self.assertRaises(polandlod1.BadData):
            polandlod1.Header(big)

    def test_a_file_shorter_than_its_blocks_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.bld")
            Path(p).write_bytes(self.header(1, blocks=((1, 5000, 0),), extra=b"short"))
            with self.assertRaises(polandlod1.BadData):
                polandlod1.read_header(p)

    def block_with(self, total, rings=b"", verts=b""):
        """A block's six streams (`total` buildings in the one tile) with the given ring and vertex streams."""
        raw = b"".join(stream(x) for x in (sv(0) * total, sv(0) * total, b"", uv(1) + uv(10) + uv(0) * total, rings, verts))
        h = polandlod1.Header(b"GBLD" + bytes([2, 0, 10, 10]) + uv(1) + sv(0) + sv(0) + uv(10) + uv(10) + uv(total)
                              + uv(1) + uv(1) + uv(len(raw) << 1))
        return raw, h

    def test_the_building_count_is_capped_before_anything_is_allocated(self):
        raw, h = self.block_with(1)
        h.tiles[0] = (0, 0, 10, 10, polandlod1.MAX_BLOCK_BUILDINGS + 1)             # a tile table that lies
        with self.assertRaises(polandlod1.BadData):
            polandlod1.decode_block(raw, h, 0, 1)

    def test_a_ring_that_claims_more_than_the_stream_holds_is_refused(self):
        raw, h = self.block_with(1, rings=uv(1) + ring(1_000_000), verts=sv(1) * 6)
        with self.assertRaises(polandlod1.BadData):
            polandlod1.decode_block(raw, h, 0, 1)
        raw, h = self.block_with(1, rings=uv(1) + ring(50_000, ortho=True), verts=sv(1) * 6)
        with self.assertRaises(polandlod1.BadData):
            polandlod1.decode_block(raw, h, 0, 1)

    def test_a_stream_longer_than_the_block_is_refused(self):
        h = self.block_with(1)[1]
        with self.assertRaises(polandlod1.BadData):
            polandlod1.decode_block(uv(1 << 20) + b"xx", h, 0, 1)
        with self.assertRaises(polandlod1.BadData):
            polandlod1.decode_block(b"\0" * (polandlod1.MAX_BLOCK_BYTES + 1), h, 0, 1)


class OneErrorPerFile(Served):
    """A file the index lists (and whose hash matches) but that does not parse: netfetch.BadResponse, logged once."""

    def setUp(self):
        super().setUp()
        good = make_file(True, self.origin)
        # (a valid header over a block of garbage: the tile table and block table say one deflated block)
        h = polandlod1.read_header(self._write(good))
        garbage = b"\xff" * h.blocks[0][1]
        self.body = good[:h.block_offsets[0]] + garbage
        self.index = index_bytes(self.tmp.name, [(self.NAME, self.body)])
        polandlod1.INDEX_SHA256 = hashlib.sha256(self.index).hexdigest()
        self.fetcher.bodies.update({self.url: self.body, polandlod1.INDEX_URL: self.index})
        self.file = os.path.join(self.folder, f"{self.NAME}.{hashlib.sha256(self.body).hexdigest()[:16]}")
        self.index_file = os.path.join(self.folder, f"index.{polandlod1.INDEX_SHA256[:16]}.npz")

    def _write(self, data):
        p = os.path.join(self.tmp.name, "probe.bld")
        Path(p).write_bytes(data)
        return p

    def test_the_cells_of_a_malformed_file_fail_the_same_way_and_the_log_says_it_once(self):
        with self.assertLogs("polandlod1", "WARNING") as logs:
            for _ in range(3):
                with self.assertRaises(netfetch.BadResponse) as cm:
                    polandlod1.query(self.key)
                self.assertIn(self.NAME, str(cm.exception))
                self.assertIn("malformed", str(cm.exception))
        self.assertEqual(len([m for m in logs.output if "malformed" in m]), 1)
        self.assertEqual(self.fetcher.urls().count(self.url), 1)        # (nor fetched again for every cell)


class BorderTest(Isolated):
    """wanted(): the cell's edges are sampled as finely as the border data is detailed, and unknown borders are not
    remembered as 'not Poland'."""

    def setUp(self):
        super().setUp()
        os.environ[polandlod1.ENV] = "1"
        polandlod1.configure(self.tmp.name, FakeFetcher())
        self.key = mapcells.cell_of(51.1, 17.0)

    def test_edges_are_sampled_every_few_metres(self):
        seen = {}

        def probe(lats, lons):
            seen["p"] = (np.array(lats), np.array(lons))
            return True
        polandlod1._in_poland_only = probe
        self.assertTrue(polandlod1.wanted(self.key))
        lats, lons = seen["p"]
        x, y = pl1992.from_lonlat(lons, lats)
        n = (len(x) - polandlod1._SAMPLES ** 2) // 4
        for k in range(4):                                               # bottom, top, left, right edge
            ex, ey = x[k * n:(k + 1) * n], y[k * n:(k + 1) * n]
            gap = np.hypot(np.diff(ex), np.diff(ey)).max()
            self.assertLessEqual(gap, polandlod1.BORDER_STEP_M * 1.05, k)
        self.assertGreater(n, 50)                                        # (a 9-point grid was ~150 m apart)

    def test_unknown_borders_are_not_poland_and_are_asked_again(self):
        calls = []
        polandlod1._in_poland_only = lambda lats, lons: calls.append(1)             # None: unknown
        self.assertFalse(polandlod1.wanted(self.key))
        self.assertFalse(polandlod1.wanted(self.key))
        self.assertEqual(len(calls), 2)                                  # not remembered
        polandlod1._in_poland_only = lambda lats, lons: calls.append(2) or True
        self.assertTrue(polandlod1.wanted(self.key))
        self.assertTrue(polandlod1.wanted(self.key))
        self.assertEqual(calls.count(2), 1)                              # a known answer is remembered

    def test_without_the_variable_nothing_is_asked(self):
        os.environ.pop(polandlod1.ENV)
        polandlod1._in_poland_only = lambda lats, lons: self.fail("asked")
        self.assertFalse(polandlod1.wanted(self.key))
        self.assertFalse(polandlod1.routes_to_lod1(self.key))


class MapDataDecidesOnce(Served):
    """MapData takes the cell's source once and uses it for the cache path and the build (the index arriving between
    the two used to store Overture's buildings under LOD1's path, or the other way round)."""

    def setUp(self):
        super().setUp()
        import mapdata
        self.mapdata = mapdata
        self.md = mapdata.MapData(self.cache, self.fetcher)
        self.md.overture = types.SimpleNamespace(query=lambda *a, **k: self.fail("Overture was asked"))

    def pllod1_files(self):
        out = []
        for root, _, files in os.walk(os.path.join(self.cache, "mapdata")):
            out += [os.path.join(root, f) for f in files if f.endswith(".npz")]
        return out

    def test_path_follows_the_decision_it_is_given(self):
        p = lambda lod1: self.md.path(self.key, "buildings", lod1=lod1)
        self.assertIn("+pllod1.", p(True))
        self.assertNotIn("+pllod1.", p(False))
        self.assertIn("+pllod1.", self.md.path(self.key, "building_parts", lod1=True))
        self.assertNotIn("+pllod1.", self.md.path(self.key, "roads", lod1=True))     # (only the two layers)

    def test_a_reader_before_the_index_looks_at_lod1_path_not_overtures(self):
        self.assertIn("+pllod1.", self.md.path(self.key, "buildings"))               # (index not loaded: it will be LOD1's)
        polandlod1.resolve(self.key)
        self.assertIn("+pllod1.", self.md.path(self.key, "buildings"))
        far = mapcells.cell_of(52.5, 20.5)                                           # in Poland, no tile in the index
        self.assertNotIn("+pllod1.", self.md.path(far, "buildings"))

    def test_a_cell_is_built_from_the_source_its_path_says(self):
        arrays, hit = self.md.layer_arrays(self.key, "buildings")
        self.assertFalse(hit)
        self.assertEqual(len(arrays["ids"]), 3)
        files = self.pllod1_files()
        self.assertEqual(len(files), 1)
        self.assertIn("+pllod1.", files[0])
        self.assertEqual(files[0], self.md.path(self.key, "buildings"))
        parts, _ = self.md.layer_arrays(self.key, "building_parts")                  # emptied beside LOD1 buildings
        self.assertEqual(len(parts["ids"]), 0)
        self.assertTrue(all("+pllod1." in f for f in self.pllod1_files()))

    def test_no_index_no_cell_is_stored_and_overture_is_not_asked(self):
        self.fetcher.errors[polandlod1.INDEX_URL] = netfetch.HostUnavailable("down", polandlod1.INDEX_URL, "h")
        for layer in ("buildings", "building_parts"):
            with self.assertRaises(netfetch.HostUnavailable):
                self.md.layer_arrays(self.key, layer)
        self.assertEqual(self.pllod1_files(), [])
        self.fetcher.errors.clear()
        polandlod1._state["retry_at"] = 0.0
        self.assertEqual(len(self.md.layer_arrays(self.key, "buildings")[0]["ids"]), 3)

    def test_versions_carry_the_setting_whether_or_not_the_index_has_arrived(self):
        before = self.md.versions()["pl_lod1"]
        self.assertEqual(self.fetcher.calls, [])
        polandlod1.resolve(self.key)
        self.assertEqual(self.md.versions()["pl_lod1"], before)
        os.environ.pop(polandlod1.ENV)
        self.assertNotIn("pl_lod1", self.md.versions())


class LanePolicy(unittest.TestCase):
    def test_lod1_lane_has_a_policy_of_its_own(self):
        f = netfetch.Fetcher()
        lane, host = f.policy("raw.githubusercontent.com#lod1"), f.policy("raw.githubusercontent.com")
        self.assertGreaterEqual(lane.daily_budget, 433)                 # a whole country's files in a day
        self.assertTrue(lane.allow_prefetch)
        self.assertTrue(lane.shared)                                    # one bucket and budget for all the server's processes
        self.assertFalse(lane.optional)                                 # a Polish cell (and its road tiles) wait for these files
        self.assertEqual(host.daily_budget, 24)                         # the update check's own, as before
        self.assertEqual(f.policy("raw.githubusercontent.com#other"), host)
        self.assertEqual(f.policy("github.com#x"), f.policy("github.com"))
        self.assertIn("Poland", netfetch.role_of("raw.githubusercontent.com#lod1"))
        self.assertEqual(netfetch.role_of("raw.githubusercontent.com"), "the update check")


if __name__ == "__main__":
    unittest.main()
