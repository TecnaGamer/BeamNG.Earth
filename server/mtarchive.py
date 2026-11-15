"""Mapterhorn's elevation tiles of zoom 13 and deeper from its regional PMTiles archives, by HTTP range reads, instead of
its tile endpoint (the user, 2026-11-15: the maintainer asked apps to use the archives; players' servers asked
tiles.mapterhorn.com ~60 M times a day).

Mapterhorn's file list (download_urls.json, dem.Dem._coverage) names one archive per zoom-6 tile with lidar under it
("6-<x>-<y>.pmtiles", zooms 13 to its max_zoom, its size and MD5). The same files are on Source Cooperative's mirror
(data.source.coop, asked first) and on download.mapterhorn.com; the tile endpoint is only the last fallback (dem.py).
A tile in an archive is the same bytes the endpoint serves (6-18-24, 2026-11-15: two z16 and a z13 tile from the
endpoint's cache equal, byte for byte, to the archive's), and a tile the archive's directories do not hold is the
endpoint's 404.

Thin glue: the network (netfetch, the hosts' shared rate limits), the disk cache and the mirrors are here; the PMTiles
header, directories, lookups and the plan of the range reads are native (earthfast pmtiles.rs: pmt_header,
pmt_find_many, pmt_ranges).

    <dem>/mtarchive/<md5>/head.bin        the archive's first 16 KB (header + root directory)
                         /meta.json       its name and size, each mirror's ETag (its copy checked), mirrors whose
                                          copy differs
                         /leaf/<off>_<len> leaf directories as stored (gzip; ~10 KB, ~4,100 tiles each)

Releases never mix: everything an archive caches sits under the MD5 the file list gives for it (a new release is a new
folder; folders of releases the list no longer names go after a day). A mirror is used for an archive only once its
copy is checked against it: its size (Content-Range) the list's, and its first 16 KB the same bytes as the cached head;
every later read from it carries If-Match with the ETag it answered then (a 412: its copy changed: not used for this
release again). A mirror that fails is skipped for DEMOTE_S.

Tiles go into the same raw cache as the endpoint's (dem/mapterhorn/<z>/<x>/<y>.webp, a missing one a remembered 404):
everything downstream is unchanged. Tiles next to each other in an archive (its tiles are stored in Hilbert order:
neighbours on the ground are mostly neighbours in the file) are one range read (MAX_RUN at most).
"""
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import fast
import netfetch
import xproc

log = logging.getLogger("earthd.dem")

NATIVE = 304
MIRRORS = ("https://data.source.coop/mapterhorn/mapterhorn/{name}", "https://download.mapterhorn.com/{name}")
HOSTS = tuple(netfetch.host_of(m) for m in MIRRORS)
ON = os.environ.get("BNGE_DEM_ARCHIVES", "1") != "0"
MIN_ZOOM = 13                   # the regional archives' first zoom (the planet's z0-12: the tile host's packs)
COVERAGE_ZOOM = 6
HEAD_BYTES = 16384
MERGE_GAP = 0                   # bytes between two tiles still read in one range (0: only back-to-back tiles)
MAX_RUN = 4 * 1024 * 1024       # bytes one range read takes at most (a single larger tile: a read of its own)
MAX_DEPTH = 4                   # directory levels (PMTiles: root + leaves)
DEMOTE_S = 300.0                # s a failing mirror is skipped
LEAF_MEM = 256                  # leaf directories kept in memory per process (~10 KB each)
LOCK_WAIT_S = 30.0              # another process reading the same head / leaf: waited for this long
PRUNE_AGE_S = 86400.0           # an older release's folder untouched this long goes (main process)
READ_THREADS = 4
_NAME = re.compile(r"^6-(\d+)-(\d+)\.pmtiles$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")


class Unavailable(IOError):
    """No mirror can serve the archive now (the caller asks the tile endpoint)."""


class Mismatch(IOError):
    """A mirror's copy of an archive is not the release the file list names."""


def archives_of(doc):
    """{(x, y) of a zoom-6 tile: {"name", "md5", "size", "max_zoom"}} of the regional archives in Mapterhorn's
    download_urls.json (malformed entries left out). Pure."""
    out = {}
    for it in doc.get("items", ()) if isinstance(doc, dict) else ():
        try:
            name = str(it["name"])
            m = _NAME.match(name)
            md5 = str(it["md5sum"]).lower()
            size, minz, maxz = int(it["size"]), int(it.get("min_zoom", MIN_ZOOM)), int(it["max_zoom"])
        except (KeyError, TypeError, ValueError):
            continue
        if m is None or not _MD5.match(md5) or size <= 0 or minz != MIN_ZOOM or maxz < MIN_ZOOM:
            continue
        out[(int(m.group(1)), int(m.group(2)))] = {"name": name, "md5": md5, "size": size, "max_zoom": maxz}
    return out


def _write(path, data):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError as e:
        log.debug("mtarchive: %s: %s", path, e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


class Region:
    """One regional archive (one release: its MD5)."""

    def __init__(self, arcs, name, md5, size):
        self.a, self.name, self.md5, self.size = arcs, name, md5, size
        self.dir = os.path.join(arcs.root, md5)
        self._lock = threading.Lock()
        self._head = None               # (header dict, root directory bytes)
        self._leaves = OrderedDict()

    # --- the mirrors' copies -------------------------------------------------------------------------------------
    def _meta(self):
        try:
            with open(os.path.join(self.dir, "meta.json"), encoding="utf-8") as f:
                m = json.load(f)
            if isinstance(m, dict) and m.get("size") == self.size:
                return m
        except (OSError, ValueError):
            pass
        return {"name": self.name, "size": self.size, "etag": {}, "bad": []}

    def _meta_update(self, fn):
        with self._lock:
            m = self._meta()
            fn(m)
            _write(os.path.join(self.dir, "meta.json"), json.dumps(m, sort_keys=True).encode())
            return m

    def _range(self, i, start, end, kind, etag=None):
        """Bytes [start, end) of mirror i's copy (the file's end may come sooner); (body, its ETag). Raises
        Mismatch (another size, another ETag, a 412), netfetch.FetchError."""
        url = MIRRORS[i].format(name=self.name)
        hd = {"Range": f"bytes={start}-{end - 1}"}
        if etag:
            hd["If-Match"] = etag
        try:
            res = self.a.fetcher.get(url, kind=kind, headers=hd)
        except netfetch.FetchFailed as e:
            if getattr(e, "status", None) == 412:
                raise Mismatch(f"{url}: changed (If-Match {etag})") from None
            raise
        total = None
        cr = res.headers.get("content-range") or ""
        if res.status == 206 and "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[1])
            except ValueError:
                total = None
        if total is not None and total != self.size:
            raise Mismatch(f"{url}: {total} bytes, the file list's release has {self.size}")
        want = min(end, self.size) - start
        if res.status != 206 or total is None or len(res.body) != want:
            raise netfetch.BadResponse(f"{url}: HTTP {res.status}, {len(res.body)} bytes for {start}-{end - 1}", url,
                                       netfetch.host_of(url), status=res.status)
        tag = (res.headers.get("etag") or "").strip()
        if etag and tag and tag != etag:
            raise Mismatch(f"{url}: ETag {tag}, checked as {etag}")
        return res.body, tag

    def _checked(self, i, kind):
        """Mirror i's ETag once its copy is checked against this release (its size, its first HEAD_BYTES the cached
        head's: the head written from it when there is none); None when it differs (marked: not asked again)."""
        m = self._meta()
        if str(i) in m["bad"]:
            return None
        tag = m["etag"].get(str(i))
        if tag:
            return tag
        try:
            head, tag = self._range(i, 0, HEAD_BYTES, kind)
            have = _read(os.path.join(self.dir, "head.bin"))
            if have is not None and have != head:
                raise Mismatch(f"{MIRRORS[i].format(name=self.name)}: another head than the cached one")
        except Mismatch as e:
            log.info("mtarchive: %s: not used for %s (%s)", HOSTS[i], self.name, e)
            self._meta_update(lambda mm: mm["bad"].append(str(i)) if str(i) not in mm["bad"] else None)
            return None
        if have is None:
            _write(os.path.join(self.dir, "head.bin"), head)
        tag = tag or "-"
        self._meta_update(lambda mm: mm["etag"].__setitem__(str(i), tag))
        return tag

    def get(self, start, end, kind):
        """Bytes [start, end) of the archive from the first mirror that can give them (checked, If-Match). Raises
        Unavailable (none can), netfetch.Deferred (a prefetch refused)."""
        err = None
        for i in self.a.order():
            try:
                tag = self._checked(i, kind)
                if tag is None:
                    continue
                return self._range(i, start, end, kind, None if tag == "-" else tag)[0]
            except netfetch.Deferred:
                raise
            except Mismatch as e:
                log.info("mtarchive: %s: not used for %s any more (%s)", HOSTS[i], self.name, e)
                self._meta_update(lambda mm: (mm["bad"].append(str(i)) if str(i) not in mm["bad"] else None,
                                              mm["etag"].pop(str(i), None)))
                err = e
            except netfetch.FetchError as e:
                self.a.demote(i)
                err = e
        raise Unavailable(f"{self.name}: no mirror ({type(err).__name__ if err else 'none left'}: {err})")

    # --- directories ---------------------------------------------------------------------------------------------
    def head(self, kind):
        """(header, root directory bytes), from disk or read (one process reads it: xproc)."""
        with self._lock:
            if self._head is not None:
                return self._head
        path = os.path.join(self.dir, "head.bin")
        b = _read(path)
        if b is None:
            b, mine = xproc.once(path + ".lock", lambda: _read(path), LOCK_WAIT_S, kind=_kind(kind), patient=True)
            if b is None:
                try:
                    for i in self.a.order():    # (the first mirror's check reads and writes it)
                        try:
                            if self._checked(i, kind) is not None:
                                break
                        except netfetch.Deferred:
                            raise
                        except netfetch.FetchError:
                            self.a.demote(i)
                    b = _read(path)
                    if b is None:
                        b = self.get(0, min(HEAD_BYTES, self.size), kind)
                        _write(path, b)
                finally:
                    if mine:
                        xproc.release(path + ".lock")
        try:
            h = fast._ef.pmt_header(b)
        except ValueError as e:
            _remove(path)
            raise Unavailable(f"{self.name}: {e}") from None
        if h["internal_compression"] not in (1, 2) or h["tile_compression"] != 1 or h["tile_type"] != 4:
            raise Unavailable(f"{self.name}: unsupported archive (compression {h['internal_compression']}/"
                              f"{h['tile_compression']}, type {h['tile_type']})")
        ro, rl = h["root_offset"], h["root_length"]
        root = b[ro:ro + rl] if ro + rl <= len(b) else self.get(ro, ro + rl, kind)
        with self._lock:
            self._head = (h, root)
        return self._head

    def leaf(self, off, ln, kind):
        k = (off, ln)
        with self._lock:
            b = self._leaves.get(k)
            if b is not None:
                self._leaves.move_to_end(k)
                return b
        path = os.path.join(self.dir, "leaf", f"{off}_{ln}")

        def ready():
            got = _read(path)
            return got if got is not None and len(got) == ln else None
        b, mine = xproc.once(path + ".lock", ready, LOCK_WAIT_S, kind=_kind(kind), patient=True)
        if b is None:
            try:
                b = ready()
                if b is None:
                    h = self.head(kind)[0]
                    b = self.get(h["leaf_offset"] + off, h["leaf_offset"] + off + ln, kind)
                    _write(path, b)
            finally:
                if mine:
                    xproc.release(path + ".lock")
        with self._lock:
            self._leaves[k] = b
            while len(self._leaves) > LEAF_MEM:
                self._leaves.popitem(last=False)
        return b

    def locate(self, keys, kind):
        """{(z, x, y): (absolute offset, length) of its bytes, or None: the archive has no such tile}."""
        h, root = self.head(kind)
        ids = {k: fast._ef.pmt_tile_id(*k) for k in keys}
        out = {}
        todo = [(None, sorted(set(ids.values())))]
        where = {}
        for _ in range(MAX_DEPTH):
            nxt = {}
            for dk, tids in todo:
                d = root if dk is None else self.leaf(dk[0], dk[1], kind)
                for t, e in zip(tids, fast._ef.pmt_find_many(d, tids)):
                    if e is None or e[1] == 0:
                        where[t] = None
                    elif e[2]:
                        nxt.setdefault((e[0], e[1]), []).append(t)
                    else:
                        where[t] = (h["data_offset"] + e[0], e[1])
            todo = sorted(nxt.items())
            if not todo:
                break
        else:
            raise Unavailable(f"{self.name}: directories deeper than {MAX_DEPTH} levels")
        for k, t in ids.items():
            out[k] = where.get(t)
        return out


def _kind(kind):
    return kind if kind in xproc.KIND_RANK else "demand"


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class Archives:
    """The regional archives of the file list items() gives ({(x6, y6): archives_of's entry}, or None: not known)."""

    def __init__(self, dem_dir, fetcher, items):
        fast.require(NATIVE, "Mapterhorn's regional archives (pmt_find_many, pmt_ranges)")
        self.root = os.path.join(dem_dir, "mtarchive")
        self.fetcher, self._items = fetcher, items
        self._regions = {}
        self._lock = threading.Lock()
        self._demoted = {}
        self._pruned = False
        self.pool = ThreadPoolExecutor(READ_THREADS, thread_name_prefix="dem-archive")
        self.stats = {"reads": 0, "tiles": 0, "missing": 0}

    def order(self):
        now = time.monotonic()
        return [i for i in range(len(MIRRORS)) if self._demoted.get(i, 0.0) <= now]

    def demote(self, i):
        self._demoted[i] = time.monotonic() + DEMOTE_S

    def region(self, z, x, y):
        """The Region holding Mapterhorn tile (z, x, y), or None (no archive lists it: not this module's)."""
        if z < MIN_ZOOM:
            return None
        items = self._items()
        if not items:
            return None
        d = z - COVERAGE_ZOOM
        it = items.get((x >> d, y >> d))
        if it is None or z > it["max_zoom"]:
            return None
        with self._lock:
            r = self._regions.get(it["md5"])
            if r is None:
                r = self._regions[it["md5"]] = Region(self, it["name"], it["md5"], it["size"])
            prune = not self._pruned
            self._pruned = True
        if prune:
            self._prune({v["md5"] for v in items.values()})
        return r

    def _group(self, keys):
        by = {}
        for k in dict.fromkeys(keys):
            r = self.region(*k)
            if r is not None:
                by.setdefault(r, []).append(k)
        return by

    def exists(self, keys, kind="demand"):
        """{key: True / False} for the keys an archive can answer from its directories alone (no tile read); the
        others left out (no archive, or none to be read now: Unavailable / errors are not raised, but
        netfetch.Deferred is)."""
        out = {}
        for r, ks in self._group(keys).items():
            try:
                out.update({k: w is not None for k, w in r.locate(ks, kind).items()})
            except netfetch.Deferred:
                raise
            except (Unavailable, netfetch.FetchError, OSError, ValueError) as e:
                log.debug("mtarchive: %s: %s: %s", r.name, type(e).__name__, e)
        return out

    def fetch(self, keys, kind="demand"):
        """{key: tile bytes, or None (not in its archive: the endpoint's 404)} for the keys an archive could give;
        the others left out (the caller asks the endpoint). Tiles next to each other in an archive in one range read.
        Raises netfetch.Deferred (a prefetch refused) only."""
        out = {}
        for r, ks in self._group(keys).items():
            try:
                where = r.locate(ks, kind)
            except netfetch.Deferred:
                raise
            except (Unavailable, netfetch.FetchError, OSError, ValueError) as e:
                log.debug("mtarchive: %s: %s: %s", r.name, type(e).__name__, e)
                continue
            have = [(k, w) for k, w in where.items() if w is not None]
            out.update({k: None for k, w in where.items() if w is None})
            self.stats["missing"] += len(where) - len(have)
            if not have:
                continue
            runs = fast._ef.pmt_ranges([w[0] for _, w in have], [w[1] for _, w in have], MERGE_GAP, MAX_RUN)

            def one(run, r=r):
                a, b, idx = run
                body = r.get(a, b, kind)
                return {have[i][0]: bytes(body[have[i][1][0] - a:have[i][1][0] - a + have[i][1][1]]) for i in idx}
            futs = [self.pool.submit(one, run) for run in runs[1:]]
            results = []
            for run in runs[:1]:
                results.append(self._safe(one, run))
            for f in futs:
                results.append(self._safe(f.result))
            deferred = None
            for got in results:
                if isinstance(got, netfetch.Deferred):
                    deferred = got
                elif isinstance(got, dict):
                    out.update(got)
                    self.stats["tiles"] += len(got)
            self.stats["reads"] += len(runs)
            if deferred is not None and not any(k in out for k, _ in have):
                raise deferred
        return out

    @staticmethod
    def _safe(fn, *a):
        try:
            return fn(*a)
        except netfetch.Deferred as e:
            return e
        except (Unavailable, netfetch.FetchError, OSError, ValueError) as e:
            log.debug("mtarchive: read: %s: %s", type(e).__name__, e)
            return None

    def _prune(self, keep):
        """Folders of releases the file list no longer names, untouched for PRUNE_AGE_S: removed (main process)."""
        import multiprocessing
        if multiprocessing.current_process().name != "MainProcess":
            return
        import shutil
        try:
            names = os.listdir(self.root)
        except OSError:
            return
        now = time.time()
        for n in names:
            p = os.path.join(self.root, n)
            try:
                if n not in keep and _MD5.match(n) and now - os.path.getmtime(p) > PRUNE_AGE_S:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                continue
