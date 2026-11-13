"""polandlod1 (GUGiK LOD1 footprints, BNE_PL_LOD1) and pl1992 (EPSG:2180 without pyproj).

The dataset is 250 MB, so the format is exercised with a small file written here by a minimal encoder; the decoder's
reading of "ortho" rings (angle unit, `swap`) was checked on the real tiles (Wroclaw old town) when it was written.
The index and the files come from a fake netfetch.Fetcher that serves them: no test touches the network.
"""
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
        self.saved = (polandlod1._index, dict(polandlod1._state), polandlod1._in_poland_only)
        polandlod1._index = None
        polandlod1._covers.clear()
        polandlod1._in_poland_only = lambda lats, lons: True

    def tearDown(self):
        polandlod1._index, state, polandlod1._in_poland_only = self.saved
        polandlod1._state.update(state)
        polandlod1._covers.clear()
        if self.env is None:
            os.environ.pop(polandlod1.ENV, None)
        else:
            os.environ[polandlod1.ENV] = self.env
        self.tmp.cleanup()

    def fresh(self):
        """A new process' view: the Index again, the files on disk as they are."""
        polandlod1._index = None
        polandlod1._covers.clear()


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
        self.url = polandlod1.Manifest.url(self.NAME)
        self.fetcher = FakeFetcher({self.url: self.body, polandlod1.INDEX_URL: self.index})
        self.cache = os.path.join(self.tmp.name, "cache")
        polandlod1.configure(self.cache, self.fetcher)
        os.environ[polandlod1.ENV] = "1"
        self.folder = os.path.join(self.cache, "polandlod1")
        self.file = os.path.join(self.folder, self.NAME)
        self.index_file = os.path.join(self.folder, "index.npz")

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
        self.assertIsNone(polandlod1.query(far))
        polandlod1._in_poland_only = lambda lats, lons: False           # a border cell, or unknown borders
        polandlod1._covers.clear()
        self.assertFalse(polandlod1.covers(self.key))
        self.assertIsNone(polandlod1.query(self.key))

    def test_signature_is_the_index_not_the_folder(self):
        sig = polandlod1.signature()
        self.assertEqual(sig, {"version": polandlod1.VERSION, "index": sig["index"], "files": 1, "buildings": 3})
        self.assertRegex(sig["index"], r"^[0-9a-f]{16}$")


class Download(Served):
    """Nothing in the cache folder: the index and the file come from the (fake) repository, once, and are checked."""

    def test_urls_follow_master(self):
        base = "https://raw.githubusercontent.com/maalos/LOD1_2024_PL/refs/heads/master"
        self.assertEqual(polandlod1.INDEX_URL, f"{base}/index.npz")
        self.assertEqual(polandlod1.Manifest.url("0201_M-33.bld"), f"{base}/bld/0201_M-33.bld")

    def test_covered_needs_only_the_index(self):
        self.assertTrue(polandlod1.covers(self.key))
        self.assertTrue(polandlod1.enabled())
        self.assertEqual(self.fetcher.urls(), [polandlod1.INDEX_URL])
        self.assertFalse(os.path.exists(self.file))

    def test_first_query_fetches_index_and_file_once(self):
        f, sources = polandlod1.query(self.key, "prefetch")
        self.assertEqual(len(f), 3)
        self.assertEqual(self.fetcher.calls, [(polandlod1.INDEX_URL, "demand", polandlod1.LANE),
                                              (self.url, "prefetch", polandlod1.LANE)])
        self.assertEqual(Path(self.file).read_bytes(), self.body)
        self.assertEqual(Path(self.index_file).read_bytes(), self.index)
        self.assertEqual(sorted(os.listdir(self.folder)), [self.NAME, "index.npz"])     # no .part, no .check
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
        self.assertEqual(os.listdir(self.folder), ["index.npz"])
        self.fetcher.bodies[self.url] = self.body
        self.assertEqual(len(polandlod1.query(self.key)[0]), 3)         # asked again, it works

    def test_a_failed_file_download_defers_the_cell_instead_of_using_overture(self):
        self.place(file=False)
        self.fetcher.errors[self.url] = netfetch.HostUnavailable("raw.githubusercontent.com unavailable", self.url,
                                                                 "raw.githubusercontent.com")
        with self.assertRaises(netfetch.HostUnavailable):
            polandlod1.query(self.key)
        self.assertTrue(polandlod1.covers(self.key))                    # still LOD1's cell: never Overture's
        self.assertEqual(os.listdir(self.folder), ["index.npz"])
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
        polandlod1.query(self.key)
        self.fresh()
        self.assertEqual(polandlod1.signature(), before)

    def test_without_an_index_overture_serves_and_it_is_asked_again_later(self):
        self.fetcher.errors[polandlod1.INDEX_URL] = netfetch.HostUnavailable("down", polandlod1.INDEX_URL,
                                                                             "raw.githubusercontent.com")
        self.assertFalse(polandlod1.enabled())
        self.assertFalse(polandlod1.covers(self.key))
        self.assertIsNone(polandlod1.query(self.key))                   # (the caller uses Overture)
        self.assertEqual(len(self.fetcher.calls), 1)                    # not asked again at once
        self.fetcher.errors.clear()
        polandlod1._state["retry_at"] = 0.0                             # (a minute later)
        self.assertEqual(len(polandlod1.query(self.key)[0]), 3)

    def test_a_bad_index_is_not_kept(self):
        self.fetcher.bodies[polandlod1.INDEX_URL] = b"<html>not an index</html>"
        self.assertFalse(polandlod1.enabled())
        self.assertFalse(os.path.exists(self.index_file))
        self.assertEqual(os.listdir(self.folder) if os.path.isdir(self.folder) else [], [])

    def test_an_old_index_is_refreshed_and_a_failed_refresh_keeps_it(self):
        self.place()
        old = time.time() - 8 * 86400
        os.utime(self.index_file, (old, old))
        self.fetcher.errors[polandlod1.INDEX_URL] = netfetch.Offline("offline", polandlod1.INDEX_URL, "h")
        self.assertTrue(polandlod1.enabled())                           # the old one stays
        self.assertEqual(Path(self.index_file).read_bytes(), self.index)
        self.assertLess(os.path.getmtime(self.index_file), time.time() - 7 * 86400)
        self.fetcher.errors.clear()
        self.fresh()
        self.assertTrue(polandlod1.enabled())                           # and now it is fetched again
        self.assertGreater(os.path.getmtime(self.index_file), time.time() - 60)
        self.assertEqual(self.fetcher.urls(), [polandlod1.INDEX_URL] * 2)

    def test_a_fresh_index_is_not_fetched_again(self):
        self.place()
        self.assertTrue(polandlod1.enabled())
        self.assertEqual(self.fetcher.calls, [])

    def test_offline_serves_what_is_on_disk_and_defers_the_rest(self):
        polandlod1.configure(self.cache, netfetch.Fetcher(offline=True))
        self.assertIsNone(polandlod1.query(self.key))                   # no index yet: off, Overture serves
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
        tr = self.Transport(bodies)
        now = [1_000_000.0]

        def sleep(s):
            now[0] += max(0.0, s)
        fetcher = netfetch.Fetcher(clock=lambda: now[0], sleep=sleep, wall=lambda: now[0], transport=tr)
        polandlod1.configure(os.path.join(self.tmp.name, "cache"), fetcher)
        os.environ[polandlod1.ENV] = "1"
        idx = polandlod1.index()
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


class LanePolicy(unittest.TestCase):
    def test_lod1_lane_has_a_policy_of_its_own(self):
        f = netfetch.Fetcher()
        lane, host = f.policy("raw.githubusercontent.com#lod1"), f.policy("raw.githubusercontent.com")
        self.assertGreaterEqual(lane.daily_budget, 433)                 # a whole country's files in a day
        self.assertTrue(lane.allow_prefetch and lane.optional)
        self.assertEqual(host.daily_budget, 24)                         # the update check's own, as before
        self.assertEqual(f.policy("raw.githubusercontent.com#other"), host)
        self.assertEqual(f.policy("github.com#x"), f.policy("github.com"))
        self.assertIn("Poland", netfetch.role_of("raw.githubusercontent.com#lod1"))
        self.assertEqual(netfetch.role_of("raw.githubusercontent.com"), "the update check")


if __name__ == "__main__":
    unittest.main()
