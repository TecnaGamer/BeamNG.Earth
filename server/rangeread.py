"""Byte ranges of large remote files (GeoParquet, Cloud-Optimised GeoTIFF) through netfetch, cached on
disk in fixed blocks (docs/ARCHITECTURE.md "Map data").

The map-data pipeline (mapdata.py) never downloads a whole Overture parquet file (0.1-0.5 GB) or a
WorldCover / canopy-height COG (0.1-0.3 GB): it reads the parts a cell needs with HTTP Range
requests. Every request goes through the server's netfetch.Fetcher (the host's rate limit, slots,
retries, circuit breaker, request kind), so DuckDB never opens a socket of its own: DuckDB reads the
parquet through the fsspec filesystem below, whose reads land here.

  * RemoteFile(urls, size, cache_dir, fetcher): the file at the first URL (later URLs are mirrors of
    the same bytes, asked in turn when a host cannot deliver: Overture's AWS bucket, then Azure).
    read(start, end, kind) returns bytes [start, end). The file is split into BLOCK-byte blocks; a
    read fetches the blocks it lacks, consecutive ones in one Range request (at most MAX_RUN
    blocks), and keeps each block as its own file <cache_dir>/<block index>.blk (written atomically,
    so a crash or the cache cap's LRU eviction only costs a re-fetch). Two threads that need the
    same block fetch it once. One in-memory LRU for all RemoteFiles of the process (MEM_BYTES)
    serves re-reads of hot blocks (a parquet footer is read once per query); everything else
    comes from the disk (and the OS page cache): a long drive passes dozens of source files.
  * NetfetchFS: an fsspec filesystem over RemoteFiles for DuckDB (duckdb.register_filesystem). The
    request kind is the protocol (nfdemand://, nfprefetch://, nfbackground://, nfpredownload://),
    because DuckDB reads from its own threads; paths are resolved to RemoteFiles by a callable.

Content-addressing: a block's bytes are a pure function of (file, block index) for the immutable
release files this is used with (an Overture release, WorldCover v200, CHMv2), so the cache never
needs revalidating; the caller puts the release id into cache_dir.
"""
import datetime
import io
import logging
import os
import re
import threading
from collections import OrderedDict

import netfetch
import staging
import xproc

log = logging.getLogger("earthd.mapdata")

BLOCK = 256 * 1024          # bytes per cached block
MAX_RUN = 64                # blocks per Range request (16 MiB)
BLOCK_WAIT_S = 60.0         # a block another process is fetching is waited for this long (xproc.once)
MEM_BYTES = 64 * 1024 * 1024    # in-memory block LRU shared by every RemoteFile of the process
CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")
# Errors after which the next mirror is asked (the same bytes from another host); NotFound is not
# one of them: a release file that is missing on one mirror is missing.
_FAILOVER = (netfetch.HostUnavailable, netfetch.RateLimited, netfetch.FetchFailed)


class RangeError(IOError):
    """A remote range could not be read (no mirror could deliver, or a bad answer)."""


class Changed(RangeError):
    """A file pinned to an ETag (RemoteFile if_match) changed on the server: the cached blocks are of the old version
    (osmtags.py: a file rewritten in place)."""


class Missing(RangeError):
    """The file does not exist (404 / 410; remembered by a marker <cache_dir>/404 for the host's
    miss_ttl, as dem.py and imagery.py remember missing tiles)."""


# --- the process-wide in-memory block LRU -------------------------------------------------------
_mem = OrderedDict()            # (cache_dir, block index) -> bytes
_mem_lock = threading.Lock()
_mem_used = 0


def _mem_get(k):
    with _mem_lock:
        b = _mem.get(k)
        if b is not None:
            _mem.move_to_end(k)
        return b


def _mem_put(k, b):
    global _mem_used
    with _mem_lock:
        old = _mem.pop(k, None)
        if old is not None:
            _mem_used -= len(old)
        _mem[k] = b
        _mem_used += len(b)
        while _mem_used > MEM_BYTES and _mem:
            _mem_used -= len(_mem.popitem(last=False)[1])


def mem_bytes():
    """Bytes of blocks held in memory (at most MEM_BYTES)."""
    with _mem_lock:
        return _mem_used


def clear_memory():
    global _mem_used
    with _mem_lock:
        _mem.clear()
        _mem_used = 0


class RemoteFile:
    """See the module docstring. size: the file's length in bytes (from an index, e.g. the STAC
    item's file:size), or None to learn it from the first answer's Content-Range (then kept in
    <cache_dir>/size, so a later session can check the length of every cached block too). cache_dir
    is the file's own directory (it also keys its blocks in the shared memory LRU). if_match: the ETag of the
    version the blocks must come from (a file that is rewritten in place: put the ETag into cache_dir too); a request
    the server answers for another version raises Changed."""

    def __init__(self, urls, size, cache_dir, fetcher, if_match=None, block=None):
        self.if_match = if_match
        self.block = block or BLOCK         # bytes per cached block (smaller for many small reads of many files)
        self.urls = (urls,) if isinstance(urls, str) else tuple(urls)
        self.cache_dir = cache_dir
        self.fetcher = fetcher
        self._key = os.path.abspath(cache_dir)
        self.size = self._saved_size() if size is None else int(size)
        self._inflight = {}                 # block index -> threading.Event
        self._lock = threading.Lock()
        self.stats = {"requests": 0, "net_bytes": 0, "disk_blocks": 0, "mem_blocks": 0}

    # --- blocks ----------------------------------------------------------------------------------
    def _path(self, i):
        return os.path.join(self.cache_dir, f"{i}.blk")

    def _lock_path(self, i):
        return os.path.join(self.cache_dir, f"{i}.blk.lock")

    def _saved_size(self):
        try:
            with open(os.path.join(self.cache_dir, "size"), "rb") as f:
                n = int(f.read().strip())
            return n if n > 0 else None
        except (OSError, ValueError):
            return None

    def _learn_size(self, total):
        self.size = total
        self._write(os.path.join(self.cache_dir, "size"), str(total).encode())

    def _write(self, path, b):
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(b)
        staging.replace_cached(tmp, path)

    def _block_from_cache(self, i):
        b = _mem_get((self._key, i))
        if b is not None:
            with self._lock:
                self.stats["mem_blocks"] += 1
            return b
        try:
            with open(self._path(i), "rb") as f:
                b = f.read()
        except OSError:
            return None
        # a damaged block (a crash after os.replace but before the data reached the disk leaves it
        # short or empty): refetched. Without the file's size only a full block can be checked.
        if len(b) != (self._block_len(i) if self.size is not None else self.block):
            return None
        with self._lock:
            self.stats["disk_blocks"] += 1
        self._remember(i, b)
        return b

    def _remember(self, i, b):
        _mem_put((self._key, i), b)

    def _block_len(self, i):
        return max(0, min(self.block, self.size - i * self.block))

    def _store(self, i, b):
        self._write(self._path(i), b)
        self._remember(i, b)

    def _fetch_run(self, i0, i1, kind):
        """Blocks i0..i1-1 in one Range request (mirrors in turn); stores and returns them."""
        start = i0 * self.block
        end = i1 * self.block if self.size is None else min(self.size, i1 * self.block)
        marker = os.path.join(self.cache_dir, "404")
        if self.fetcher.miss_alive(marker, self.urls[0]):
            raise Missing(f"{self.urls[0]}: not found (remembered)")
        err = None
        # the mirrors take turns by block (Overture's AWS and Azure copies: twice the connections the hosts allow one
        # client), each one's failure asking the next
        k = (i0 // MAX_RUN + i0) % len(self.urls)
        for url in self.urls[k:] + self.urls[:k]:
            hd = {"Range": f"bytes={start}-{end - 1}"}
            if self.if_match:
                hd["If-Match"] = self.if_match
            try:
                r = self.fetcher.get(url, kind=kind, headers=hd)
            except netfetch.FetchFailed as e:
                if self.if_match and getattr(e, "status", None) == 412:
                    raise Changed(f"{url}: no longer {self.if_match}") from e
                err = e
                continue
            except netfetch.NotFound as e:
                os.makedirs(self.cache_dir, exist_ok=True)
                open(marker, "wb").close()
                raise Missing(f"{url}: not found") from e
            except _FAILOVER as e:
                err = e
                continue
            body = r.body
            if self.if_match and r.headers.get("etag", self.if_match).strip() != self.if_match:
                raise Changed(f"{url}: {r.headers.get('etag')} is not {self.if_match}")
            if r.status == 206:
                m = CONTENT_RANGE.match(r.headers.get("content-range", ""))
                if not m or int(m.group(1)) != start:
                    raise RangeError(f"{url}: bad Content-Range {r.headers.get('content-range')!r}")
                if m.group(3) != "*":
                    total = int(m.group(3))
                    if self.size is None:
                        self._learn_size(total)
                        end = min(end, total)
                    elif total != self.size:
                        raise RangeError(f"{url}: size {total} differs from the index's {self.size}")
            elif not (r.status == 200 and start == 0 and self.size is not None and len(body) == self.size):
                raise RangeError(f"{url}: HTTP {r.status} without the range")
            else:
                body = body[:end]
            if len(body) != end - start:
                raise RangeError(f"{url}: {len(body)} bytes for range {start}-{end - 1}")
            with self._lock:
                self.stats["requests"] += 1
                self.stats["net_bytes"] += len(body)
            out = {}
            for i in range(i0, i1):
                b = body[(i - i0) * self.block:(i - i0 + 1) * self.block]
                if not b:
                    break
                self._store(i, b)
                out[i] = b
            return out
        if isinstance(err, netfetch.FetchError):
            raise err
        raise RangeError(f"no mirror of {self.urls[0]} could deliver")

    def blocks(self, i0, i1, kind="demand"):
        """{i: bytes} for blocks i0..i1-1 (beyond the end of the file: absent)."""
        if self.size is not None:
            i1 = min(i1, -(-self.size // self.block))
        out, missing = {}, []
        for i in range(i0, i1):
            b = self._block_from_cache(i)
            if b is not None:
                out[i] = b
            else:
                missing.append(i)
        if not missing:
            return out
        mine, theirs = [], []
        with self._lock:
            for i in missing:
                ev = self._inflight.get(i)
                if ev is None:
                    self._inflight[i] = threading.Event()
                    mine.append(i)
                else:
                    theirs.append((i, ev))
        try:
            # a thread that fetched one of them between our cache look and the lock has stored it and
            # left _inflight (it stores before it lets go): look again, never fetch a block twice
            need = []
            for i in mine:
                b = self._block_from_cache(i)
                if b is not None:
                    out[i] = b
                else:
                    need.append(i)
            # the blocks another of the server's processes is fetching are waited for (xproc): every process starts
            # on a new place at once and reads the same footers and row groups
            held = [i for i in need if xproc.take(self._lock_path(i), kind)]
            later = sorted(set(need) - set(held))
            try:
                run = []
                for i in held + [None]:
                    if run and (i is None or i != run[-1] + 1 or len(run) >= MAX_RUN):
                        out.update(self._fetch_run(run[0], run[-1] + 1, kind))
                        run = []
                    if i is not None:
                        run.append(i)
            finally:
                for i in held:
                    xproc.release(self._lock_path(i))
            for i in later:
                b, lk = xproc.once(self._lock_path(i), lambda i=i: self._block_from_cache(i), BLOCK_WAIT_S, kind)
                if b is None:
                    try:
                        b = self._fetch_run(i, i + 1, kind).get(i)
                    finally:
                        if lk:
                            xproc.release(self._lock_path(i))
                if b is not None:
                    out[i] = b
        finally:
            with self._lock:
                for i in mine:
                    self._inflight.pop(i).set()
        for i, ev in theirs:
            ev.wait()
            b = self._block_from_cache(i)
            if b is None:                   # the other thread failed: ask ourselves
                b = self._fetch_run(i, i + 1, kind).get(i)
            if b is not None:
                out[i] = b
        return out

    def read(self, start, end, kind="demand"):
        """Bytes [start, end) of the file (cut at its end)."""
        if self.size is not None:
            end = min(end, self.size)
        if end <= start:
            return b""
        i0, i1 = start // self.block, (end - 1) // self.block + 1
        got = self.blocks(i0, i1, kind)
        buf = b"".join(got[i] for i in range(i0, i1) if i in got)
        out = buf[start - i0 * self.block:end - i0 * self.block]
        # (a block left out mid-range would shift every byte after it: a corrupt page for DuckDB, not an error)
        if len(out) != end - start and (self.size is not None or i1 - 1 in got):
            log.warning("rangeread: short read of %s [%d, %d): %d bytes, blocks %s of %d..%d, size %s",
                        self.cache_dir, start, end, len(out), sorted(got), i0, i1 - 1, self.size)
        return out


# --- fsspec filesystem for DuckDB ----------------------------------------------------------------
PROTOCOLS = {"demand": "nfdemand", "prefetch": "nfprefetch", "background": "nfbackground",
             "predownload": "nfpredownload"}

try:
    import fsspec
    from fsspec.spec import AbstractBufferedFile
except ImportError:                         # mapdata reports it when Overture is asked for
    fsspec = None


if fsspec is not None:
    class _RangeBufferedFile(AbstractBufferedFile):
        def __init__(self, fs, path, remote, kind):
            self.remote, self.kind = remote, kind
            super().__init__(fs, path, mode="rb", block_size=BLOCK, cache_type="none", size=remote.size)

        def _fetch_range(self, start, end):
            return self.remote.read(start, end, self.kind)

    class NetfetchFS(fsspec.AbstractFileSystem):
        """Read-only fsspec filesystem: `<protocol>://<name>` -> resolve(name) -> RemoteFile, read
        with the protocol's request kind. One instance per kind (see PROTOCOLS)."""
        cachable = False

        def __init__(self, resolve, kind="demand", **kw):
            self.protocol = PROTOCOLS[kind]
            super().__init__(skip_instance_cache=True, **kw)
            self.resolve, self.kind = resolve, kind

        def _strip(self, path):
            path = str(path)
            p = self.protocol + "://"
            return path[len(p):] if path.startswith(p) else path

        def _remote(self, path):
            r = self.resolve(self._strip(path))
            if r is None:
                raise FileNotFoundError(path)
            if r.size is None:              # learn the size from the first block's Content-Range
                r.blocks(0, 1, self.kind)
            return r

        def info(self, path, **kw):
            r = self._remote(path)
            return {"name": self._strip(path), "size": r.size, "type": "file"}

        def modified(self, path):
            """DuckDB asks for it (its cache key); release files never change, so a fixed time."""
            return datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)

        def ls(self, path, detail=True, **kw):
            i = self.info(path)
            return [i] if detail else [i["name"]]

        def glob(self, path, **kw):
            return [self._strip(path)]

        def exists(self, path, **kw):
            try:
                self._remote(path)
                return True
            except FileNotFoundError:
                return False

        def _open(self, path, mode="rb", **kw):
            if mode != "rb":
                raise PermissionError("read-only")
            return _RangeBufferedFile(self, self._strip(path), self._remote(path), self.kind)
else:
    NetfetchFS = None


class MemoryRange:
    """A RemoteFile stand-in over bytes in memory (tests and local fixture files)."""

    def __init__(self, data):
        self.data = bytes(data)
        self.size = len(self.data)
        self.reads = []

    def read(self, start, end, kind="demand"):
        self.reads.append((start, end, kind))
        return self.data[start:min(end, self.size)]

    def blocks(self, i0, i1, kind="demand"):
        return {}


def read_all(remote, kind="demand"):
    """A whole (small) remote file as a BytesIO."""
    return io.BytesIO(remote.read(0, remote.size, kind))
