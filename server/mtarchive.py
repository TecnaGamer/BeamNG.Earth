"""Mapterhorn's elevation tiles of zoom 13 and deeper from its regional PMTiles archives, by HTTP range reads, instead
of its tile endpoint (the user, 2026-11-15: the maintainer asked apps to use the archives; players' servers asked
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

    <dem>/mtarchive/v2/<md5>/head.bin        the archive's first 16 KB (header + root directory)
                         /meta.json       its name and size, each mirror's ETag (its copy checked), mirrors whose
                                          copy differs
                         /leaf/<off>_<len> leaf directories as stored (gzip; ~10 KB, ~4,100 tiles each)

Releases never mix: everything an archive caches sits under the MD5 the file list gives for it (a new release is a new
folder; folders of releases the list no longer names go after a day), and a mirror's copy is read only once it is
proven to be that release: its ETag tied to the list's MD5, by the project's binding (bindings_of: the whole copy
hashed by tools/build_mt_bindings.py, its ETag recorded) or, for a copy stored in one part, by the ETag being the MD5
itself. Every read of it, the first one too, carries If-Match with that ETag, so no byte of another version is ever
read (a 412: not used for this release again); its size must be the list's and its first 16 KB the cached head's. Size,
head and ETag alone prove nothing (Codex review 2026-11-15: two copies with the same head and directories and other
tile bytes of the same lengths). No proven copy: the archive is not used, the endpoint as before. A mirror that fails
is skipped for DEMOTE_S.

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
NAMESPACE = "v2"                # <dem>/mtarchive/<NAMESPACE>/<md5>: v2, copies proven the listed release (bound); the
                                # folders before it (<dem>/mtarchive/<md5>) dropped with their tiles (migrate_unproven)
MERGE_GAP = 0                   # bytes between two tiles still read in one range (0: only back-to-back tiles)
MAX_RUN = 4 * 1024 * 1024       # bytes one range read takes at most (a single larger tile: a read of its own)
MAX_DEPTH = 4                   # directory levels (PMTiles: root + leaves)
DEMOTE_S = 300.0                # s a failing mirror is skipped
LEAF_MEM = 256                  # leaf directories kept in memory per process (~10 KB each)
LOCK_WAIT_S = 30.0              # another process reading the same head / leaf: waited for this long
PRUNE_AGE_S = 86400.0           # an older release's folder untouched this long goes (main process)
READ_THREADS = 4
# The project's binding of the mirrors' copies to Mapterhorn's releases (tools/build_mt_bindings.py hashes each copy
# whole: its MD5 the file list's, its ETag recorded): a copy is used only through it, If-Match its bound ETag
# (BNGE_MT_BINDINGS: a URL or a file; default <CDN>/dem/mapterhorn/archives.json). Read again after BIND_MAX_AGE_S; a
# failed read asked again after BIND_RETRY_S.
BINDINGS_URL = os.environ.get("BNGE_MT_BINDINGS") or None
BIND_MAX_AGE_S = 86400.0
BIND_RETRY_S = 600.0
_NAME = re.compile(r"^6-(\d+)-(\d+)\.pmtiles$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")


def bindings_of(body):
    """{archive name: {"md5", "size", "mirrors": {host: ETag}}} of a binding file (tools/build_mt_bindings.py's JSON:
    {"format": 1, "archives": {name: {"md5", "size", "mirrors": {host: ETag}}}}); malformed entries left out, only the
    hosts of MIRRORS. Raises ValueError for a file that is not one. Pure."""
    doc = json.loads(body)
    if not isinstance(doc, dict) or doc.get("format") != 1 or not isinstance(doc.get("archives"), dict):
        raise ValueError("not a binding file (format 1)")
    out = {}
    for name, e in doc["archives"].items():
        try:
            md5, size, mirrors = str(e["md5"]).lower(), int(e["size"]), e["mirrors"]
        except (KeyError, TypeError, ValueError):
            continue
        if not _NAME.match(str(name)) or not _MD5.match(md5) or size <= 0 or not isinstance(mirrors, dict):
            continue
        m = {h: str(t) for h, t in mirrors.items() if h in HOSTS and isinstance(t, str) and 0 < len(t) <= 128}
        if m:
            out[str(name)] = {"md5": md5, "size": size, "mirrors": m}
    return out


def migrate_unproven(dem_dir):
    """Once per cache: what the archives gave before a copy had to be proven the listed release (Codex review of
    5776353e: the branch's builds before it, never released, could keep the wrong copy's tiles). In each region an old
    namespace folder (<dem>/mtarchive/<md5>, its meta.json naming the archive) stands for, the raw tiles of zoom 13 and
    deeper, their columns' remembered 404s and the decoded tiles of every zoom over it (a z12 or coarser tile's lidar
    test read those directories) are removed, all read again from proven copies or the endpoint. The old folder, the
    only record of which region to clean, goes only once all of its region's files are gone, and the flag
    (<dem>/mtarchive/<NAMESPACE>/.migrated) only once no old folder is left: a migration killed midway, or a file that
    cannot be removed now, is finished at a later start (Codex review of bce5d9f4). One process at a time (xproc). A
    folder without meta.json had no mirror checked, so no tile read through it: removed alone."""
    base = os.path.join(dem_dir, "mtarchive")
    flag = os.path.join(base, NAMESPACE, ".migrated")
    if os.path.exists(flag):
        return
    if any(_MD5.match(n) for n in _listdir(base)):
        lock = flag + ".lock"
        got, mine = xproc.once(lock, lambda: True if os.path.exists(flag) else None, 120.0, patient=True)
        if got:
            return
        try:
            _migrate(dem_dir, base)
        finally:
            if mine:
                xproc.release(lock)
    if any(_MD5.match(n) for n in _listdir(base)):
        return                                  # (not done: the next start goes on)
    try:
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        with open(flag, "w", encoding="utf-8") as f:
            f.write("1")
    except OSError:
        pass


def _migrate(dem_dir, base):
    import shutil
    dropped = {}
    n_regions = 0
    for n in sorted(x for x in _listdir(base) if _MD5.match(x)):
        p = os.path.join(base, n)
        region = None
        try:
            with open(os.path.join(p, "meta.json"), encoding="utf-8") as f:
                mm = _NAME.match(str(json.load(f).get("name", "")))
            if mm is None:
                continue                        # (a name it cannot read: kept, not taken as nothing to clean)
            region = (int(mm.group(1)), int(mm.group(2)))
        except FileNotFoundError:
            region = None                       # (no mirror ever checked through it: nothing read)
        except (OSError, ValueError, AttributeError):
            continue                            # (unreadable now: kept for a later start)
        if region is not None:
            if region not in dropped:
                dropped[region] = _drop_region(dem_dir, *region)
                n_regions += 1
            if not dropped[region]:
                continue                        # (a file left: this folder stays the record of it)
        shutil.rmtree(p, ignore_errors=True)
    if n_regions:
        log.info("mtarchive: tiles of %d regions read before the archives' copies were proven: dropped, read again%s",
                 n_regions, "" if all(dropped.values()) else " (some files not yet: again at the next start)")


def _drop_region(dem_dir, rx, ry):
    """The raw z13+ tiles (and their columns' miss indexes) and every decoded tile over zoom-6 region (rx, ry)
    removed: True when none is left."""
    ok = True
    raw = os.path.join(dem_dir, "mapterhorn")
    for z in range(MIN_ZOOM, 19):
        d = 1 << (z - COVERAGE_ZOOM)
        zdir = os.path.join(raw, str(z))
        if not os.path.isdir(zdir):
            continue
        for x in range(rx * d, (rx + 1) * d):
            col = os.path.join(zdir, str(x))
            for n in _listdir(col):
                stem = n.split(".", 1)[0]
                if n == "_misses" or (stem.isdigit() and ry * d <= int(stem) < (ry + 1) * d
                                      and n.endswith((".webp", ".404"))):
                    ok = _remove(os.path.join(col, n)) and ok
    dec = os.path.join(dem_dir, "decoded")
    for ver in _listdir(dec):
        for g in _listdir(os.path.join(dec, ver)):
            gdir = os.path.join(dec, ver, g)
            for z in range(0, 19):
                if z >= COVERAGE_ZOOM:
                    d = 1 << (z - COVERAGE_ZOOM)
                    xs, y0, y1 = range(rx * d, (rx + 1) * d), ry * d, (ry + 1) * d
                else:
                    sh = COVERAGE_ZOOM - z
                    xs, y0, y1 = [rx >> sh], ry >> sh, (ry >> sh) + 1
                zdir = os.path.join(gdir, str(z))
                if not os.path.isdir(zdir):
                    continue
                for x in xs:
                    col = os.path.join(zdir, str(x))
                    for n in _listdir(col):
                        stem = n.split(".", 1)[0]
                        if stem.isdigit() and y0 <= int(stem) < y1:
                            ok = _remove(os.path.join(col, n)) and ok
    return ok


def _listdir(p):
    try:
        return os.listdir(p)
    except OSError:
        return []


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

    def __init__(self, arcs, name, md5, size, bound):
        self.a, self.name, self.md5, self.size = arcs, name, md5, size
        self.bound = bound              # {mirror index: the ETag the project's binding ties to this release}
        self.dir = os.path.join(arcs.root, md5)
        self._bad = set(self._meta()["bad"])    # "<mirror>:<ETag>" copies found not to be it
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

    def dead(self):
        """Has every bound copy been found not to be this release (or failed its If-Match)?"""
        return all(f"{i}:{t}" in self._bad for i, t in self.bound.items())

    def _meta_update(self, fn):
        with self._lock:
            m = self._meta()
            fn(m)
            self._bad.update(m["bad"])
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
        if etag and tag != etag:
            raise Mismatch(f"{url}: ETag {tag or 'none'}, bound as {etag}")
        return res.body, tag

    def _checked(self, i, kind):
        """Mirror i's ETag when its copy is the release the file list names: the project's binding (bindings_of,
        tools/build_mt_bindings.py: the whole file hashed to the list's MD5) ties that ETag to it, and the copy answers
        with it (its first read already If-Match: no byte of another version is read), its first HEAD_BYTES the cached
        head's (the head written from it when there is none). None: not bound, or it differs (marked)."""
        tag = self.bound.get(i)
        if not tag:
            return None
        mark = f"{i}:{tag}"
        m = self._meta()
        if mark in m["bad"]:
            return None
        if m["etag"].get(str(i)) == tag:
            return tag
        try:
            head, _ = self._range(i, 0, HEAD_BYTES, kind, tag)
            have = _read(os.path.join(self.dir, "head.bin"))
            if have is not None and have != head:
                raise Mismatch(f"{MIRRORS[i].format(name=self.name)}: another head than the cached one")
        except Mismatch as e:
            log.info("mtarchive: %s: not used for %s (%s)", HOSTS[i], self.name, e)
            self._meta_update(lambda mm: mm["bad"].append(mark) if mark not in mm["bad"] else None)
            return None
        if have is None:
            _write(os.path.join(self.dir, "head.bin"), head)
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
                return self._range(i, start, end, kind, tag)[0]
            except netfetch.Deferred:
                raise
            except Mismatch as e:
                log.info("mtarchive: %s: not used for %s any more (%s)", HOSTS[i], self.name, e)
                mark = f"{i}:{self.bound.get(i)}"
                self._meta_update(lambda mm: (mm["bad"].append(mark) if mark not in mm["bad"] else None,
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
    """path removed (or already gone): True; False when it is still there."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        return not os.path.exists(path)
    return True


class Archives:
    """The regional archives of the file list items() gives ({(x6, y6): archives_of's entry}, or None: not known)."""

    def __init__(self, dem_dir, fetcher, items):
        fast.require(NATIVE, "Mapterhorn's regional archives (pmt_find_many, pmt_ranges)")
        self.root = os.path.join(dem_dir, "mtarchive", NAMESPACE)
        migrate_unproven(dem_dir)
        self.fetcher, self._items = fetcher, items
        self._regions = {}
        self._lock = threading.Lock()
        self._demoted = {}
        self._pruned = False
        self._bind = {"at": -1e18, "failed_at": -1e18, "doc": None}
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
        # (only copies tied to the listed release: the project's binding's ETags; without one, an ETag that is the
        # listed MD5 itself (a file stored in one part: S3's and R2's ETag is then its MD5), asked for with If-Match,
        # so another copy answers 412 and is never read. No copy left: the archive is not this module's)
        b = (self.bindings() or {}).get(it["name"])
        if b is not None and b["md5"] == it["md5"] and b["size"] == it["size"]:
            bound = {i: b["mirrors"][h] for i, h in enumerate(HOSTS) if b["mirrors"].get(h)}
        else:
            bound = {i: f'"{it["md5"]}"' for i in range(len(HOSTS))}
        if not bound:
            return None
        with self._lock:
            r = self._regions.get(it["md5"])
            if r is None:
                r = self._regions[it["md5"]] = Region(self, it["name"], it["md5"], it["size"], bound)
            r.bound = bound
            prune = not self._pruned
            self._pruned = True
        if prune:
            self._prune({v["md5"] for v in items.values()})
        if r.dead():
            return None
        return r

    def bindings(self):
        """{archive name: {"md5", "size", "mirrors": {host: ETag}}}: the project's binding of the mirrors' copies to
        Mapterhorn's releases (BINDINGS_URL, bindings_of), from disk (read again after BIND_MAX_AGE_S) or the net; the
        older copy on disk while the net cannot give one (its entries stand only where their MD5 is the list's); {}
        when there is none (the archives are not used: the endpoint as before)."""
        st = self._bind
        now = time.monotonic()
        if st["doc"] is not None and now - st["at"] < BIND_MAX_AGE_S:
            return st["doc"]
        with self._lock:
            if st["doc"] is not None and now - st["at"] < BIND_MAX_AGE_S:
                return st["doc"]
            path = os.path.join(self.root, "bindings.json")
            disk = _read(path)
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                age = None
            old = None
            if disk is not None:
                try:
                    old = bindings_of(disk)
                except ValueError:
                    old = None
            if old is not None and age is not None and age < BIND_MAX_AGE_S:
                st["doc"], st["at"] = old, now - age
                return old
            if now - st["failed_at"] >= BIND_RETRY_S:
                try:
                    body = self._bindings_body()
                    doc = bindings_of(body)
                    _write(path, body)
                    st["doc"], st["at"] = doc, now
                    return doc
                except (netfetch.FetchError, OSError, ValueError) as e:
                    log.info("mtarchive: no binding of the archives (%s: %s): %s", type(e).__name__, e,
                             "the copy on disk" if old else "the tile endpoint meanwhile")
                    st["failed_at"] = now
            st["doc"], st["at"] = (old or {}), now - BIND_MAX_AGE_S + BIND_RETRY_S
            return st["doc"]

    def _bindings_body(self):
        url = BINDINGS_URL
        if url is None:
            import imagery as imagerymod
            url = f"{imagerymod.CDN_BASE}/dem/mapterhorn/archives.json"
        if not url.startswith(("https://", "http://")):
            with open(url, "rb") as f:
                return f.read()
        return self.fetcher.get(url, kind="demand").body

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
