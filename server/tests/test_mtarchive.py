"""mtarchive.py / dem.py: Mapterhorn's zoom 13+ tiles from its regional PMTiles archives by range reads (Source
Cooperative's mirror, then download.mapterhorn.com, the tile endpoint last). No network: a recorded fixture of the
real archive 6-18-24 (its head, two leaf directories and three tiles, tests/fixtures/mt_6-18-24.npz: the tiles are
the endpoint's bytes, checked equal to the archive's when recorded) and small archives written here."""
import gzip
import io
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dem as demmod  # noqa: E402
import fast  # noqa: E402
import mtarchive  # noqa: E402
import netfetch  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mt_6-18-24.npz"
SRC, MTD = mtarchive.HOSTS
ENDPOINT = "tiles.mapterhorn.com"


def load_fixture():
    z = np.load(FIXTURE)
    meta = json.loads(bytes(z["meta"]).decode())
    segs = {int(k[1:]): bytes(z[k]) for k in z.files if k.startswith("s")}
    return meta, segs


class Sparse:
    """An archive's bytes as recorded segments (a read outside them fails the test) or whole."""

    def __init__(self, size, segs=None, whole=None):
        self.size, self.segs, self.whole = size, segs or {}, whole

    def read(self, a, b):
        b = min(b, self.size)
        if self.whole is not None:
            return self.whole[a:b]
        for s, d in self.segs.items():
            if s <= a and b <= s + len(d):
                return d[a - s:b - s]
        raise AssertionError(f"read {a}-{b} outside the recorded segments")


class R:
    def __init__(self, status, body, headers):
        self.status, self.body, self.headers = status, body, headers


class ArchiveFetcher:
    """The file list, the two archive mirrors (each can fail, give another size / ETag / head) and the endpoint."""

    def __init__(self, archives, items, endpoint_tiles=None):
        self.archives = archives            # name -> Sparse
        self.items = items                  # download_urls.json items
        self.endpoint_tiles = endpoint_tiles or {}
        self.mirror = {SRC: {}, MTD: {}}    # host -> {"fail", "size", "etag", "head"}
        # the project's binding (tools/build_mt_bindings.py): every listed archive's copies on both mirrors, by the
        # ETags these mirrors answer; None: no binding published (404)
        self.binding = {"format": 1, "archives": {
            i["name"]: {"md5": i["md5sum"], "size": i["size"], "mirrors": {SRC: f'"{SRC}-etag"', MTD: f'"{MTD}-etag"'}}
            for i in items if i["name"] != "planet.pmtiles"}}
        self.calls = []
        self.served = []                    # hosts that answered with data (not a 412 / 404 / failure)
        self.lock = threading.Lock()

    def get(self, url, *, kind="demand", headers=None):
        host = netfetch.host_of(url)
        res = self._get(url, kind, headers, host)
        if res.status in (200, 206) and host in (SRC, MTD, ENDPOINT) and url != demmod.COVERAGE_URL:
            with self.lock:
                self.served.append(host)
        return res

    def served_hosts(self):
        with self.lock:
            return set(self.served)

    def _get(self, url, kind, headers, host):
        with self.lock:
            self.calls.append((host, url, (headers or {}).get("Range"), kind, (headers or {}).get("If-Match")))
        if url == demmod.COVERAGE_URL:
            return R(200, json.dumps({"version": "test", "items": self.items}).encode(), {})
        if url.endswith("/dem/mapterhorn/archives.json"):
            if self.binding is None:
                raise netfetch.NotFound("404", url, host, status=404)
            return R(200, json.dumps(self.binding).encode(), {})
        if host == ENDPOINT:
            z, x, y = (int(v) for v in url.rsplit(".", 1)[0].rsplit("/", 3)[1:])
            b = self.endpoint_tiles.get((z, x, y))
            if b is None:
                raise netfetch.NotFound("404", url, host, status=404)
            return R(200, b, {})
        cfg = self.mirror[host]
        if cfg.get("fail"):
            raise netfetch.HostUnavailable("down", url, host)
        if kind == "prefetch" and cfg.get("defer"):
            raise netfetch.Deferred("busy", url, host)
        arc = self.archives[url.rsplit("/", 1)[1]]
        etag = cfg.get("etag", f'"{host}-etag"')
        want = (headers or {}).get("If-Match")
        if want and want != etag:
            raise netfetch.FetchFailed("412", url, host, status=412)
        a, b = (int(v) for v in headers["Range"].split("=")[1].split("-"))
        body = arc.read(a, b + 1)
        if a == 0 and cfg.get("head"):
            body = cfg["head"][:len(body)]
        size = cfg.get("size", arc.size)
        return R(206, body, {"content-range": f"bytes {a}-{a + len(body) - 1}/{size}", "etag": etag})

    def miss_alive(self, path, url):
        return netfetch.miss_alive(path, None)

    def register_chain(self, name, urls):
        pass

    def hosts(self):
        with self.lock:
            return [c[0] for c in self.calls if c[1] != demmod.COVERAGE_URL
                    and not c[1].endswith("/dem/mapterhorn/archives.json")]

    def reads(self, host=None):
        with self.lock:
            return [c for c in self.calls if c[2] and (host is None or c[0] == host)]


# --- small archives written here ------------------------------------------------------------------------------------
def _varint(v):
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        if v:
            out.append(c | 0x80)
        else:
            out.append(c)
            return bytes(out)


def write_pmtiles(tiles, minz=13, maxz=16):
    """A PMTiles v3 archive (clustered, gzip root directory, no leaves, WebP type, tiles uncompressed) of
    {(z, x, y): bytes}."""
    ents = sorted((fast._ef.pmt_tile_id(*k), b) for k, b in tiles.items())
    data, dirents, off = bytearray(), [], 0
    for t, b in ents:
        dirents.append((t, 1, len(b), off))
        data += b
        off += len(b)
    d = bytearray(_varint(len(dirents)))
    last = 0
    for t, *_ in dirents:
        d += _varint(t - last)
        last = t
    for _, r, _, _ in dirents:
        d += _varint(r)
    for _, _, ln, _ in dirents:
        d += _varint(ln)
    prev_end = None
    for _, _, ln, o in dirents:
        d += _varint(0 if prev_end == o else o + 1)
        prev_end = o + ln
    root = gzip.compress(bytes(d), mtime=0)
    root_off, data_off = 127, 127 + len(root)
    hdr = bytearray(b"PMTiles\x03")
    hdr += struct.pack("<11Q", root_off, len(root), data_off, 0, data_off, 0, data_off, len(data),
                       len(ents), len(ents), len(ents))
    hdr += bytes([1, 2, 1, 4, minz, maxz])
    hdr += b"\x00" * (127 - len(hdr))
    return bytes(hdr) + root + bytes(data)


def terrarium(v):
    from PIL import Image
    h = 32768 + 100 + int(v)
    a = np.zeros((512, 512, 3), np.uint8)
    col = np.arange(512)[None, :] + np.arange(512)[:, None]
    hh = h + col % 50
    a[..., 0], a[..., 1], a[..., 2] = hh // 256, hh % 256, (col * 7) % 256
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, "PNG")
    return buf.getvalue()


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= mtarchive.NATIVE, "earthfast 304")
class _Base(unittest.TestCase):
    def setUp(self):
        saved_cov = dict(demmod._coverage)
        demmod._coverage.clear()
        demmod._coverage.update(index=None, at=-1e18, failed=False, lock=saved_cov["lock"])
        saved_mb, demmod.MIRROR_MB = demmod.MIRROR_MB, 0

        def restore():
            demmod._coverage.clear()
            demmod._coverage.update(saved_cov)
            demmod.MIRROR_MB = saved_mb
        self.addCleanup(restore)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def dem(self, f, sub="c", archives=True):
        d = demmod.Dem(os.path.join(self.tmp.name, sub), fetcher=f, fetch_workers=2)
        if not archives:
            d.archives = None
        self.addCleanup(d.pool.shutdown)
        return d

    def raw(self, sub, k):
        p = os.path.join(self.tmp.name, sub, "mapterhorn", str(k[0]), str(k[1]), f"{k[2]}.webp")
        with open(p, "rb") as fh:
            return fh.read()


class RealArchiveTest(_Base):
    """The recorded archive 6-18-24 (Baltimore): parsing, the same bytes and heights as the endpoint's."""

    def setUp(self):
        super().setUp()
        self.meta, segs = load_fixture()
        self.tiles = {tuple(json.loads(k.replace("(", "[").replace(")", "]"))): tuple(v)
                      for k, v in self.meta["tiles"].items()}
        self.ep = {k: segs[off] for k, (off, _ln) in self.tiles.items()}
        self.items = [{"name": "planet.pmtiles", "md5sum": "0" * 32, "size": 1, "min_zoom": 0, "max_zoom": 12},
                      {"name": self.meta["name"], "md5sum": self.meta["md5sum"], "size": self.meta["size"],
                       "min_zoom": 13, "max_zoom": self.meta["max_zoom"]}]
        self.arc = {self.meta["name"]: Sparse(self.meta["size"], segs)}

    def fetcher(self):
        return ArchiveFetcher(self.arc, self.items, self.ep)

    def test_directories_locate_the_recorded_tiles(self):
        f = self.fetcher()
        d = self.dem(f)
        r = d.archives.region(16, 18530, 24623)
        self.assertEqual(r.name, "6-18-24.pmtiles")
        self.assertIsNone(d.archives.region(12, 1158, 1538), "zoom 12: the planet's (the tile host's packs)")
        self.assertIsNone(d.archives.region(16, 0, 0), "no archive there")
        got = r.locate(list(self.tiles), "demand")
        self.assertEqual({k: tuple(v) for k, v in got.items()}, self.tiles)
        self.assertEqual(len(f.reads()), 1 + 2, "the head (a mirror's check) and the two leaves, nothing else")
        n = len(f.reads())
        self.assertEqual(d.archives.exists([(13, 2308, 3188), (16, 18531, 24623)]),
                         {(13, 2308, 3188): True, (16, 18531, 24623): True})
        self.assertEqual(len(f.reads()), n, "answered from the cached leaves")
        # (a tile an archive lacks: WrittenArchiveTest)

    def test_byte_identical_to_the_endpoint(self):
        fa, fe = self.fetcher(), self.fetcher()
        da, de = self.dem(fa, "a"), self.dem(fe, "e", archives=False)
        for k in self.tiles:
            ta, te = np.array(da._load_tile(k)), np.array(de._load_tile(k))
            self.assertEqual(ta.tobytes(), te.tobytes(), k)
            self.assertEqual(self.raw("a", k), self.raw("e", k), f"{k}: the raw cache's bytes")
        self.assertNotIn(ENDPOINT, fa.hosts(), "the archive path never asks the endpoint")
        self.assertEqual(set(fa.hosts()), {SRC}, "Source Cooperative first")
        self.assertEqual(sum(1 for h in fe.hosts() if h == ENDPOINT), 3)

    def test_the_lidar_test_from_the_index_alone(self):
        f = self.fetcher()
        d = self.dem(f)
        self.assertTrue(d._lidar(1154, 1594))               # (z13 2308/3188 is in the archive)
        data_reads = [c for c in f.reads() if int(c[2].split("=")[1].split("-")[0]) >= 900_000_000]
        self.assertEqual(data_reads, [], "no tile read: the directories answered")

    def test_fallback_order(self):
        k = (16, 18530, 24623)
        f = self.fetcher()
        f.mirror[SRC]["fail"] = True
        d = self.dem(f, "m2")
        self.assertEqual(d._download(demmod.MAPTERHORN, *k), self.ep[k])
        self.assertIn(MTD, f.hosts())
        self.assertNotIn(ENDPOINT, f.hosts(), "the second mirror before the endpoint")
        f2 = self.fetcher()
        f2.mirror[SRC]["fail"] = f2.mirror[MTD]["fail"] = True
        d2 = self.dem(f2, "ep")
        with self.assertRaises(netfetch.Deferred):
            d2._download(demmod.MAPTERHORN, *k, kind="prefetch")
        self.assertNotIn(ENDPOINT, f2.hosts(), "a prefetch never falls back to the endpoint")
        self.assertEqual(d2._download(demmod.MAPTERHORN, *k), self.ep[k], "a demand load: the endpoint, last")
        self.assertIn(ENDPOINT, f2.hosts())

    def test_no_binding_no_archive(self):
        f = self.fetcher()
        f.binding = None                    # (none published: the endpoint, as before)
        d = self.dem(f, "nb")
        k = (16, 18530, 24623)
        self.assertEqual(d._download(demmod.MAPTERHORN, *k), self.ep[k])
        self.assertEqual(f.served_hosts(), {ENDPOINT}, "multipart copies (ETag not the MD5): 412, never read")
        self.assertTrue(all(c[1] == demmod.COVERAGE_URL or c[0] == ENDPOINT or c[1].endswith("archives.json")
                            or c[2] == "bytes=0-16383" for c in f.calls), "only the If-Match head tries")
        self.assertEqual(d._download(demmod.MAPTERHORN, 16, 18531, 24623, kind="prefetch"),
                         self.ep[(16, 18531, 24623)], "no copy left: a prefetch asks the endpoint as before")

    def test_a_new_release_never_mixes(self):
        k = (16, 18530, 24623)
        f = self.fetcher()
        d = self.dem(f, "rel")
        self.assertEqual(d._download(demmod.MAPTERHORN, *k), self.ep[k])
        old = os.path.join(self.tmp.name, "rel", "mtarchive", mtarchive.NAMESPACE, self.meta["md5sum"])
        self.assertTrue(os.path.exists(os.path.join(old, "head.bin")))
        # the file list names a new release (new MD5 and size) while the mirrors still have the old file
        items = [dict(i) for i in self.items]
        items[1].update(md5sum="b" * 32, size=self.meta["size"] + 1)
        demmod._coverage.update(index=None, at=-1e18, failed=False, archives=None)
        os.remove(os.path.join(self.tmp.name, "rel", "mapterhorn", "download_urls.json"))    # (the weekly re-read)
        f2 = ArchiveFetcher(self.arc, items, self.ep)
        d2 = self.dem(f2, "rel")
        k2 = (16, 18531, 24623)
        self.assertEqual(d2._download(demmod.MAPTERHORN, *k2), self.ep[k2], "from the endpoint")
        self.assertIn(ENDPOINT, f2.hosts(), "the old copies are not used for the new release")
        new = os.path.join(self.tmp.name, "rel", "mtarchive", mtarchive.NAMESPACE, "b" * 32)
        self.assertFalse(os.path.exists(os.path.join(new, "head.bin")), "no copy's byte read for the new release")
        self.assertEqual(f2.served_hosts(), {ENDPOINT}, "the copies answered 412 to If-Match the new MD5")

    def test_a_changed_copy_is_dropped(self):
        f = self.fetcher()
        d = self.dem(f, "chg")
        d._download(demmod.MAPTERHORN, 16, 18530, 24623)
        f.mirror[SRC]["etag"] = '"changed"'             # (Source Cooperative's copy replaced: If-Match fails)
        d2 = self.dem(f, "chg")
        self.assertEqual(d2._download(demmod.MAPTERHORN, 16, 18531, 24623), self.ep[(16, 18531, 24623)])
        self.assertIn(MTD, f.hosts(), "the other mirror, checked against the cached head")
        with open(os.path.join(self.tmp.name, "chg", "mtarchive", mtarchive.NAMESPACE, self.meta["md5sum"],
                               "meta.json")) as fh:
            m = json.load(fh)
        self.assertEqual(m["bad"], [f'0:"{SRC}-etag"'])
        # a mirror whose first 16 KB differ from the cached head: never used
        f3 = self.fetcher()
        f3.mirror[SRC]["fail"] = True
        head = bytearray(load_fixture()[1][0])
        head[200] ^= 1
        f3.mirror[MTD]["head"] = bytes(head)
        d3 = self.dem(f3, "chg3")
        d3.archives.region(16, 1, 1)
        hd = os.path.join(self.tmp.name, "chg3", "mtarchive", mtarchive.NAMESPACE, self.meta["md5sum"])
        os.makedirs(hd, exist_ok=True)
        with open(os.path.join(hd, "head.bin"), "wb") as fh:
            fh.write(load_fixture()[1][0])
        self.assertEqual(d3._download(demmod.MAPTERHORN, 16, 18530, 24623), self.ep[(16, 18530, 24623)])
        self.assertIn(ENDPOINT, f3.hosts())


class WrittenArchiveTest(_Base):
    """Archives written here: neighbouring tiles in one read, a missing z16 tile's parent in the same batch, 404s."""

    def setUp(self):
        super().setUp()
        # a 4 x 4 block of z16 tiles (one missing) and the z15 parent of the missing one, in region 6-18-24
        self.have = {(16, 18528 + i, 24620 + j): terrarium(i * 4 + j) for i in range(4) for j in range(4)}
        self.gone = (16, 18531, 24623)
        del self.have[self.gone]
        self.have[(15, 18531 >> 1, 24623 >> 1)] = terrarium(99)
        blob = write_pmtiles(self.have)
        self.items = [{"name": "6-18-24.pmtiles", "md5sum": "c" * 32, "size": len(blob), "min_zoom": 13,
                       "max_zoom": 16}]
        self.arc = {"6-18-24.pmtiles": Sparse(len(blob), whole=blob)}

    def test_one_read_for_neighbours_and_the_parent_in_the_batch(self):
        f = ArchiveFetcher(self.arc, self.items, dict(self.have))
        d = self.dem(f)
        keys = [(16, 18528 + i, 24620 + j) for i in range(4) for j in range(4)]
        got = d.tiles(keys)
        tile_reads = [c for c in f.reads() if not c[2].startswith("bytes=0-")]
        self.assertLessEqual(len(tile_reads), 3, tile_reads)            # (the block and the parent: back to back)
        self.assertNotIn(ENDPOINT, f.hosts())
        for k in keys:
            if k != self.gone:
                self.assertEqual(self.raw("c", k), self.have[k])
        self.assertTrue(demmod.missindex.known(os.path.join(self.tmp.name, "c", "mapterhorn", "16", "18531",
                                                            "24623.404")), "the missing tile: a remembered 404")
        self.assertEqual(self.raw("c", (15, 9265, 12311)), self.have[(15, 9265, 12311)], "its parent fetched along")
        # the same heights as the endpoint path
        fe = ArchiveFetcher(self.arc, self.items, dict(self.have))
        de = self.dem(fe, "e", archives=False)
        ge = de.tiles(keys)
        for k in keys:
            self.assertEqual(np.asarray(got[k]).tobytes(), np.asarray(ge[k]).tobytes(), k)
        self.assertGreater(sum(1 for h in fe.hosts() if h == ENDPOINT), len(keys) - 1)

    def test_exists_from_the_directories(self):
        f = ArchiveFetcher(self.arc, self.items)
        d = self.dem(f)
        self.assertEqual(d.archives.exists([self.gone, (16, 18528, 24620), (12, 1158, 1538)]),
                         {self.gone: False, (16, 18528, 24620): True}, "zoom 12 is not an archive's")
        self.assertEqual([c for c in f.reads() if not c[2].startswith("bytes=0-")], [], "no tile read")

    def test_prefetch_batches(self):
        f = ArchiveFetcher(self.arc, self.items)
        d = self.dem(f)
        keys = [(16, 18528 + i, 24620 + j) for i in range(4) for j in range(4)]
        n, deferred = d.prefetch(keys)
        self.assertEqual((n, deferred), (0, False), "all on disk after the batch (counted by the batch, not here)")
        self.assertLessEqual(len([c for c in f.reads() if not c[2].startswith("bytes=0-")]), 3)
        self.assertTrue(all(c[3] == "prefetch" for c in f.reads()))
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "c", "mapterhorn", "16", "18528", "24620.webp")))


class ReleaseIdentityTest(_Base):
    """A mirror's copy is used only once the project's binding (tools/build_mt_bindings.py: the whole file hashed)
    ties its ETag to the MD5 the file list names (Codex review of 38d0dbda [high]: size + first 16 KB + ETag did not
    prove it: two copies with the same head and directories and other tile bytes of the same lengths mixed)."""

    def setUp(self):
        super().setUp()
        self.k = (16, 18528, 24620)
        good = {self.k: terrarium(1), (16, 18529, 24620): terrarium(2)}
        other = {self.k: terrarium(1)[:-8] + b"\x00" * 8, (16, 18529, 24620): terrarium(2)}   # (same lengths)
        self.good, self.other = write_pmtiles(good), write_pmtiles(other)
        self.items = [{"name": "6-18-24.pmtiles", "md5sum": "d" * 32, "size": len(self.good), "min_zoom": 13,
                       "max_zoom": 16}]
        self.ep = good

    def fetcher(self):
        f = ArchiveFetcher({"6-18-24.pmtiles": Sparse(len(self.good), whole=self.good)}, self.items, self.ep)
        other = Sparse(len(self.other), whole=self.other)
        get0 = f.get

        def get(url, *, kind="demand", headers=None):     # (the second mirror holds the other copy)
            if netfetch.host_of(url) == MTD:
                f.archives = {"6-18-24.pmtiles": other}
                try:
                    return get0(url, kind=kind, headers=headers)
                finally:
                    f.archives = {"6-18-24.pmtiles": Sparse(len(self.good), whole=self.good)}
            return get0(url, kind=kind, headers=headers)
        f.get = get
        return f

    def test_unbound_copies_never_serve_a_tile(self):
        f = self.fetcher()
        d = self.dem(f)
        d.archives.bindings = lambda: {}                 # (no binding published)
        f.mirror[SRC]["fail"] = True                     # (the first mirror down: the other copy would answer)
        self.assertEqual(d._download(demmod.MAPTERHORN, *self.k), self.ep[self.k], "the endpoint's bytes")
        self.assertEqual(f.served_hosts(), {ENDPOINT}, "the other copy answered 412, never read")

    def test_a_copy_whose_etag_is_the_listed_md5_binds_itself(self):
        # (a file stored in one part: its ETag is its MD5, a proof of the release without the project's binding)
        f = self.fetcher()
        f.mirror[SRC]["etag"] = '"' + "d" * 32 + '"'
        d = self.dem(f)
        d.archives.bindings = lambda: {}
        self.assertEqual(d._download(demmod.MAPTERHORN, *self.k), self.ep[self.k])
        self.assertEqual(f.served_hosts(), {SRC})
        self.assertTrue(all(c[4] == '"' + "d" * 32 + '"' for c in f.calls if c[0] == SRC), "every read pinned to it")

    def test_only_the_bound_mirror(self):
        f = self.fetcher()
        d = self.dem(f)
        d.archives.bindings = lambda: {"6-18-24.pmtiles": {"md5": "d" * 32, "size": len(self.good),
                                                           "mirrors": {SRC: f'"{SRC}-etag"'}}}
        self.assertEqual(d._download(demmod.MAPTERHORN, *self.k), self.ep[self.k])
        self.assertEqual(set(f.hosts()), {SRC})
        f.mirror[SRC]["fail"] = True
        k2 = (16, 18529, 24620)
        self.assertEqual(d._download(demmod.MAPTERHORN, *k2), self.ep[k2])
        self.assertNotIn(MTD, f.hosts(), "the unbound copy is never asked")
        self.assertIn(ENDPOINT, f.hosts())

    def test_a_binding_of_another_release_is_ignored(self):
        f = self.fetcher()
        d = self.dem(f)
        d.archives.bindings = lambda: {"6-18-24.pmtiles": {"md5": "e" * 32, "size": len(self.good),
                                                           "mirrors": {SRC: f'"{SRC}-etag"', MTD: f'"{MTD}-etag"'}}}
        self.assertEqual(d._download(demmod.MAPTERHORN, *self.k), self.ep[self.k])
        self.assertEqual(f.served_hosts(), {ENDPOINT})

    def test_a_bound_mirror_answering_another_etag_is_dropped(self):
        f = self.fetcher()
        f.mirror[SRC]["etag"] = '"replaced"'
        d = self.dem(f)
        d.archives.bindings = lambda: {"6-18-24.pmtiles": {"md5": "d" * 32, "size": len(self.good),
                                                           "mirrors": {SRC: f'"{SRC}-etag"'}}}
        self.assertEqual(d._download(demmod.MAPTERHORN, *self.k), self.ep[self.k])
        self.assertIn(ENDPOINT, f.hosts())


class ArchivesOfTest(unittest.TestCase):
    def test_archives_of(self):
        doc = {"items": [{"name": "planet.pmtiles", "md5sum": "0" * 32, "size": 5, "min_zoom": 0, "max_zoom": 12},
                         {"name": "6-18-24.pmtiles", "md5sum": "A" * 32, "size": 7, "min_zoom": 13, "max_zoom": 16},
                         {"name": "6-1-2.pmtiles", "md5sum": "nothex", "size": 7, "min_zoom": 13, "max_zoom": 16},
                         {"name": "../x.pmtiles", "md5sum": "a" * 32, "size": 7, "min_zoom": 13, "max_zoom": 16}]}
        self.assertEqual(mtarchive.archives_of(doc),
                         {(18, 24): {"name": "6-18-24.pmtiles", "md5": "a" * 32, "size": 7, "max_zoom": 16}})

    def test_bindings_of(self):
        doc = {"format": 1, "archives": {
            "6-18-24.pmtiles": {"md5": "A" * 32, "size": 7, "mirrors": {SRC: '"e1"', "evil.example.org": '"e2"'}},
            "6-1-2.pmtiles": {"md5": "a" * 32, "size": 7, "mirrors": {"evil.example.org": '"e2"'}},
            "../x.pmtiles": {"md5": "a" * 32, "size": 7, "mirrors": {SRC: '"e"'}},
            "6-3-4.pmtiles": {"md5": "nothex", "size": 7, "mirrors": {SRC: '"e"'}}}}
        self.assertEqual(mtarchive.bindings_of(json.dumps(doc)),
                         {"6-18-24.pmtiles": {"md5": "a" * 32, "size": 7, "mirrors": {SRC: '"e1"'}}})
        with self.assertRaises(ValueError):
            mtarchive.bindings_of(json.dumps({"format": 2, "archives": {}}))

    @unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= mtarchive.NATIVE, "earthfast 304")
    def test_ranges(self):
        self.assertEqual(fast._ef.pmt_ranges([100, 150, 900, 100], [50, 50, 10, 50], 0, 1 << 20),
                         [(100, 200, [0, 3, 1]), (900, 910, [2])])


if __name__ == "__main__":
    unittest.main()
