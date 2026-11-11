"""The revisit cache's pack files (2026-11-11): its items appended to a few large files instead of a zip each.

Why: ~30 k zips in revisit/ (each a file to create, open, deflate and inflate; Windows scans each new file on its
first open) and Python's zipfile + deflate cost 0.3-7 ms of CPU an item to write (a road plan 6.7 ms). Now each
server process appends its items to its own pack (no locks between processes: one writer per file), one zstd frame
a record (earthfast zstd_pack, level 1: 1-4x quicker both ways and smaller than deflate; a record that does not
shrink, the far buildings' shapes whose bodies are zstd already, is kept as it is: nothing to unpack).

The files, in <cache>/revisit/packs/:
  <pid>_<start>_<n>.rpk   MAGIC, then records: REC (key, kind, codec, lengths, crc32 of the stored bytes) + the
                          stored bytes. Written by one process, appended to while it lives, a new file past PACK_BYTES.
  <pid>_<start>_<n>.rix   IMAGIC, then one ENT per record (where it is), appended after the record itself: a reader
                          that sees an entry finds the record whole (its crc is checked on every read anyway: a crash
                          between the two leaves bytes no entry points to, or an entry whose record fails its crc).

Every process keeps the entries of all packs in memory (~150 bytes an item) and reads the .rix files' new tails when a
key is not there (at most every REFRESH_S): another process's item is found a moment after it was kept. Reads are
positional reads from a handle held per pack (Windows: opened with FILE_SHARE_DELETE, so a held handle never blocks
the cache's eviction or "clear cache"; a removed pack stays readable through it until the next refresh drops it).

Eviction (prune, the server's start): the oldest units (a pack by its last append; a zip of before; a far texture
kept beside its item) are removed until the cache is back under its size, as before with the zips (cache.prune_dir).
A pack goes whole: so that one does not take an item still in use with it, an item read from a pack that the next
prune would remove (PROMOTE_FROM of the size from the oldest) is copied into the reader's own pack (its stored bytes,
nothing packed again): LRU by pack, like a log-structured cache. Saved areas' items (pinned) are copied out of a pack
before it goes. Small packs of earlier runs are merged into one (fewer files and handles).
"""
import errno
import hashlib
import os
import struct
import threading
import time
import zlib

SUBDIR = "packs"
PACK_SUFFIX = ".rpk"
INDEX_SUFFIX = ".rix"
MAGIC = b"BNGERPK1"
IMAGIC = b"BNGERIX1"
# a record's header in the pack: magic, codec, 0, 0, kind tag, key, stored length, raw length, crc32 of the stored
REC = struct.Struct("<4sBBHI20sIII4x")
REC_MAGIC = b"RREC"
# an index entry: key, kind tag, codec, the record's offset (its header), stored length, raw length, crc32
ENT = struct.Struct("<20sIB3xQIII")
RAW, ZSTD = 0, 1                # codecs
LEVEL = 1                       # zstd level of a record
PACK_BYTES = 32 << 20           # a pack this big is closed and the next one started
SMALL_BYTES = 8 << 20           # earlier runs' packs smaller than this are merged at the server's start ...
MERGE_MIN = 3                   # ... when there are at least this many of them
REFRESH_S = 0.02                # a missing key looks at the other processes' new entries at most this often
PROMOTE_FROM = 0.6              # an item read from the oldest packs is copied forward when the next prune would ...
PROMOTE_MIN = 0.5               # ... reach that pack (cut to LOW_WATER) or once the packs are over this of the cap
LOW_WATER = 0.9                 # cut back to this share of the cap (cache.LOW_WATER)
CAP_BYTES = 2000e6              # the revisit cache's size (revisit.MAX_MB sets it)
_START = int(time.time())

assert REC.size == 48 and ENT.size == 48


def kind_tag(kind):
    return zlib.crc32(kind.encode("utf-8"))


def key20(k):
    """The 20 bytes a revisit key is kept under: a sha1 hex key's own bytes, any other string's sha1."""
    if len(k) == 40:
        try:
            return bytes.fromhex(k)
        except ValueError:
            pass
    return hashlib.sha1(k.encode("utf-8")).digest()


def _ef():
    import fast
    fast.require(171, "the revisit cache's packs")
    return fast._ef


def encode(raw):
    """(codec, stored bytes) of a record's raw bytes: zstd, or raw when that does not shrink it by 3 %."""
    z = _ef().zstd_pack(raw, LEVEL)
    if len(z) < len(raw) * 0.97:
        return ZSTD, z
    return RAW, raw


def decode(codec, stored, size):
    if codec == RAW:
        if len(stored) != size:
            raise ValueError("revisit pack: raw record of the wrong length")
        return bytes(stored)
    if codec == ZSTD:
        return _ef().zstd_unpack(stored, size)
    raise ValueError(f"revisit pack: unknown codec {codec}")


# --- files -----------------------------------------------------------------------------------------------------------
if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.HANDLE)
    _GENERIC_READ, _SHARE_ALL, _OPEN_EXISTING, _NORMAL = 0x80000000, 0x7, 3, 0x80

    def _open_read(path):
        """A read handle that never blocks the file's removal (FILE_SHARE_DELETE; Python's open() leaves it out)."""
        h = _k32.CreateFileW(path, _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING, _NORMAL, None)
        if h is None or h == wintypes.HANDLE(-1).value:
            err = ctypes.get_last_error()
            raise FileNotFoundError(errno.ENOENT, ctypes.FormatError(err), path) if err in (2, 3) else \
                OSError(errno.EACCES, ctypes.FormatError(err), path)
        try:
            return msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY)
        except OSError:
            _k32.CloseHandle(h)
            raise
    _GENERIC_WRITE, _CREATE_NEW, _ERROR_FILE_EXISTS = 0x40000000, 1, 80

    def _open_new_append(path):
        """A new file's append handle that never blocks its removal either (FILE_SHARE_DELETE): os.open's held the
        process's open pack and index against every delete, "clear cache" and the tests' temporary folders alike
        ([WinError 32], the Windows CI, 2026-11-11)."""
        h = _k32.CreateFileW(path, _GENERIC_WRITE, _SHARE_ALL, None, _CREATE_NEW, _NORMAL, None)
        if h is None or h == wintypes.HANDLE(-1).value:
            err = ctypes.get_last_error()
            if err == _ERROR_FILE_EXISTS:
                raise FileExistsError(errno.EEXIST, ctypes.FormatError(err), path)
            raise FileNotFoundError(errno.ENOENT, ctypes.FormatError(err), path) if err in (2, 3) else \
                OSError(errno.EACCES, ctypes.FormatError(err), path)
        try:
            return msvcrt.open_osfhandle(h, os.O_WRONLY | os.O_APPEND | os.O_BINARY)
        except OSError:
            _k32.CloseHandle(h)
            raise
else:
    def _open_read(path):
        return os.open(path, os.O_RDONLY)

    def _open_new_append(path):
        return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL, 0o644)


def _pread(fd, lock, n, off):
    if hasattr(os, "pread"):
        out = os.pread(fd, n, off)
        while len(out) < n:             # (a short read: one more try, then whatever is there)
            more = os.pread(fd, n - len(out), off + len(out))
            if not more:
                break
            out += more
        return out
    with lock:                          # (Windows: no pread; seek + read under the pack's lock)
        os.lseek(fd, off, os.SEEK_SET)
        parts, got = [], 0
        while got < n:
            b = os.read(fd, n - got)
            if not b:
                break
            parts.append(b)
            got += len(b)
        return b"".join(parts)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError(errno.EIO, "revisit pack: nothing written")
        view = view[n:]


class _Pack:
    """One pack as a reader sees it: its index entries (the .rix bytes read so far) and read handles (held: no open
    per read or per refresh; the .rix's size from its handle, as a directory listing's sizes on Windows lag behind a
    file another process is appending to)."""
    __slots__ = ("no", "name", "path", "ents", "seen", "fd", "ixfd", "lock", "mtime", "size", "old", "mine")

    def __init__(self, no, name, path):
        self.no, self.name, self.path = no, name, path
        self.ents = bytearray()     # the ENT records read so far
        self.seen = len(IMAGIC)     # .rix bytes consumed
        self.fd = None
        self.ixfd = None
        self.lock = threading.Lock()
        self.mtime = 0.0
        self.size = 0
        self.old = False            # in the part the next prune would remove: hits are copied forward
        self.mine = False           # written by this process

    def entry(self, i):
        return ENT.unpack_from(self.ents, i * ENT.size)

    def read(self, off, n):
        fd = self.fd
        if fd is None:
            with self.lock:
                if self.fd is None:
                    self.fd = _open_read(self.path + PACK_SUFFIX)
                fd = self.fd
        return _pread(fd, self.lock, n, off)

    def new_entries(self):
        """The .rix's whole entries past what was read (bytes; b"" when none or unreadable). Under the store's lock."""
        try:
            if self.ixfd is None:
                self.ixfd = _open_read(self.path + INDEX_SUFFIX)
                if _pread(self.ixfd, self.lock, len(IMAGIC), 0) != IMAGIC:
                    return b""
            st = os.fstat(self.ixfd)
            self.mtime = st.st_mtime
            want = len(IMAGIC) + (st.st_size - len(IMAGIC)) // ENT.size * ENT.size
            if want <= self.seen:
                return b""
            data = _pread(self.ixfd, self.lock, want - self.seen, self.seen)
        except OSError:
            return b""
        data = data[:len(data) // ENT.size * ENT.size]
        self.seen += len(data)
        return data

    def close(self):
        for attr in ("fd", "ixfd"):
            fd = getattr(self, attr)
            setattr(self, attr, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


class _Writer:
    __slots__ = ("pid", "pack", "fd", "ixfd", "off")


class Store:
    """The packs of one cache folder, as this process sees them."""

    def __init__(self, cache_dir):
        self.dir = os.path.join(cache_dir, "revisit", SUBDIR)
        self.lock = threading.RLock()
        self.packs = {}         # name -> _Pack
        self.by_no = []         # pack number -> _Pack (None once gone)
        self.index = {}         # key20 -> pack number << 32 | entry number
        self.dead = set()       # packs whose .rpk went alone (_forget): not listed again
        self.loaded = False
        self.at = -1e18         # last refresh (monotonic)
        self.w = None           # this process's _Writer
        self.seq = 0
        self.stats = {"hits": 0, "misses": 0, "stored": 0, "promoted": 0, "bad": 0, "refreshes": 0}

    # --- the index ------------------------------------------------------------------------------------------------
    def _add_entries(self, p, data):
        """New ENT bytes of pack p into its buffer and the index (under the lock)."""
        n0 = len(p.ents) // ENT.size
        p.ents += data
        base = (p.no << 32) + n0
        self.index.update(zip([bytes(data[i:i + 20]) for i in range(0, len(data), ENT.size)],
                              range(base, base + len(data) // ENT.size)))

    def refresh(self, force=False):
        """The packs' new entries (other processes' items), the ones gone dropped. Never raises."""
        now = time.monotonic()
        with self.lock:
            if not force and now - self.at < REFRESH_S:
                return
            self.at = now
            self.stats["refreshes"] += 1
            try:
                with os.scandir(self.dir) as it:
                    listing = {de.name[:-len(INDEX_SUFFIX)] for de in it if de.name.endswith(INDEX_SUFFIX)} - self.dead
            except OSError:
                listing = set()
            gone = [n for n in self.packs if n not in listing and not self.packs[n].mine]
            changed = bool(gone)
            for n in gone:
                p = self.packs.pop(n)
                self.by_no[p.no] = None
                p.close()
            for name in sorted(listing):
                p = self.packs.get(name)
                if p is None:
                    p = _Pack(len(self.by_no), name, os.path.join(self.dir, name))
                    self.by_no.append(p)
                    self.packs[name] = p
                    changed = True
                if p.mine:
                    continue        # (its entries go in as they are written)
                data = p.new_entries()
                if data:
                    self._add_entries(p, data)
                    changed = True
            if gone:
                self._rebuild()
            if changed:
                self._mark_old()
            self.loaded = True

    def _rebuild(self):
        """The index again from the packs still there (a pack went: an item it held may be in another)."""
        self.index = {}
        for p in sorted(self.packs.values(), key=lambda q: q.mtime):
            base, e = p.no << 32, p.ents
            self.index.update(zip([bytes(e[i:i + 20]) for i in range(0, len(e), ENT.size)],
                                  range(base, base + len(e) // ENT.size)))

    def _mark_old(self):
        """Mark the packs the next prune would remove (oldest first, down to PROMOTE_FROM of the cap): an item read
        from one is copied forward. Nothing while the packs are under PROMOTE_MIN of the cap."""
        total = 0
        for p in self.packs.values():
            p.old = False
            if p.ents and not p.mine:
                _k, _t, _c, off, n, _raw, _crc = p.entry(len(p.ents) // ENT.size - 1)
                p.size = off + REC.size + n
            total += p.size
        if total <= CAP_BYTES * PROMOTE_MIN:
            return
        cut, run = total - CAP_BYTES * PROMOTE_FROM, 0
        for p in sorted(self.packs.values(), key=lambda q: q.mtime):
            if run >= cut or p.mine:
                break
            p.old = True
            run += p.size

    def _find(self, kind, k):
        """(pack, entry) of key k, or None."""
        v = self.index.get(k)
        if v is None:
            return None
        p = self.by_no[v >> 32]
        if p is None:
            return None
        e = p.entry(v & 0xffffffff)
        if e[1] != kind_tag(kind):
            return None
        return p, e

    def _lookup(self, kind, k):
        if not self.loaded:
            self.refresh(force=True)
        with self.lock:
            got = self._find(kind, k)
        if got is None:
            self.refresh()
            with self.lock:
                got = self._find(kind, k)
        return got

    # --- reading --------------------------------------------------------------------------------------------------
    def has(self, kind, key):
        return self._lookup(kind, key20(key)) is not None

    def get(self, kind, key):
        """The raw bytes of item key, or None (none kept, or its record damaged)."""
        k = key20(key)
        for attempt in (0, 1):
            got = self._lookup(kind, k)
            if got is None:
                self.stats["misses"] += 1
                return None
            p, (_k, _tag, codec, off, n, raw_n, crc) = got
            try:
                stored = p.read(off + REC.size, n)
            except FileNotFoundError:       # (its .rpk removed alone: the cache's eviction; none of it is there)
                self._forget(p)
                stored = None
            except OSError:
                stored = None
            if stored is None or len(stored) != n:
                if attempt == 0:            # (a pack removed under us, compacted elsewhere: look again)
                    self.refresh(force=True)
                    continue
                return None
            if zlib.crc32(stored) != crc:
                self._drop(k, p)
                return None
            try:
                raw = decode(codec, stored, raw_n)
            except (ValueError, RuntimeError):
                self._drop(k, p)
                return None
            self.stats["hits"] += 1
            if p.old and not p.mine:        # (the next prune would take it: kept in this process's pack)
                try:
                    self._append(kind_tag(kind), k, codec, stored, raw_n, crc)
                    self.stats["promoted"] += 1
                except OSError:
                    pass
            return raw
        return None

    def _forget(self, p):
        """Pack p is gone: out of the index (another pack may hold some of its items)."""
        with self.lock:
            if self.packs.get(p.name) is p and not p.mine:
                self.dead.add(p.name)
                del self.packs[p.name]
                self.by_no[p.no] = None
                p.close()
                self._rebuild()

    def _drop(self, k, p):
        self.stats["bad"] += 1
        with self.lock:
            v = self.index.get(k)
            if v is not None and v >> 32 == p.no:
                del self.index[k]

    # --- writing --------------------------------------------------------------------------------------------------
    def put(self, kind, key, raw):
        """Keep raw bytes under key (packed). Raises OSError (not kept)."""
        codec, stored = encode(raw)
        self._append(kind_tag(kind), key20(key), codec, stored, len(raw), zlib.crc32(stored))
        self.stats["stored"] += 1


    def _open_writer(self):
        os.makedirs(self.dir, exist_ok=True)
        while True:
            self.seq += 1
            name = f"{os.getpid()}_{_START}_{self.seq}"
            path = os.path.join(self.dir, name)
            try:
                fd = _open_new_append(path + PACK_SUFFIX)
            except FileExistsError:
                continue
            break
        try:
            _write_all(fd, MAGIC)
            ixfd = _open_new_append(path + INDEX_SUFFIX)
        except OSError:
            os.close(fd)
            raise
        try:
            _write_all(ixfd, IMAGIC)
        except OSError:
            os.close(fd)
            os.close(ixfd)
            raise
        w = _Writer()
        w.pid, w.fd, w.ixfd, w.off = os.getpid(), fd, ixfd, len(MAGIC)
        p = _Pack(len(self.by_no), name, path)
        p.mine = True
        p.mtime = time.time()
        self.by_no.append(p)
        self.packs[name] = p
        w.pack = p
        return w

    def _close_writer(self):
        w, self.w = self.w, None
        if w is not None and w.pid == os.getpid():
            for fd in (w.fd, w.ixfd):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _append(self, tag, k, codec, stored, raw_n, crc):
        """One record (kind tag, key20, codec, stored bytes) appended to this process's pack, then its entry."""
        with self.lock:
            w = self.w
            if w is not None and (w.pid != os.getpid() or w.off >= PACK_BYTES or _unlinked(w.fd)):
                self._close_writer()
                w = None
            if w is None:
                w = self.w = self._open_writer()
            off = w.off
            head = REC.pack(REC_MAGIC, codec, 0, 0, tag, k, len(stored), raw_n, crc)
            try:
                if len(stored) < (1 << 20):
                    _write_all(w.fd, head + stored)     # (one write: a small record is not copied twice)
                else:
                    _write_all(w.fd, head)
                    _write_all(w.fd, stored)
                w.off = off + REC.size + len(stored)
                ent = ENT.pack(k, tag, codec, off, len(stored), raw_n, crc)
                _write_all(w.ixfd, ent)
            except OSError:
                self._close_writer()        # (its offset is not known any more: the next item starts a new pack)
                raise
            p = w.pack
            p.seen += ENT.size
            p.mtime = time.time()
            p.size = w.off
            self._add_entries(p, ent)


def _unlinked(fd):
    """Was the pack removed under its writer (a cache folder replaced, "clear cache" on Unix)?"""
    try:
        return os.fstat(fd).st_nlink == 0
    except OSError:
        return True


_stores = {}
_stores_lock = threading.Lock()


def store_for(cache_dir):
    d = os.path.abspath(cache_dir)
    with _stores_lock:
        s = _stores.get(d)
        if s is None:
            s = _stores[d] = Store(d)
        return s


def _after_fork():
    """A forked child writes and reads its own handles (never the parent's pack: one writer per file)."""
    global _stores_lock
    _stores_lock = threading.Lock()
    for s in list(_stores.values()):
        s.lock = threading.RLock()
        w, s.w = s.w, None
        for fd in ((w.fd, w.ixfd) if w is not None else ()):
            try:
                os.close(fd)        # (the child's copies: the parent's pack is the parent's alone)
            except OSError:
                pass
        for p in s.packs.values():
            p.lock = threading.Lock()
            p.close()
    _stores.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def reset():
    """Forget every store (tests; a cache folder replaced)."""
    with _stores_lock:
        for s in _stores.values():
            with s.lock:
                s._close_writer()
                for p in s.packs.values():
                    p.close()
        _stores.clear()


# --- the server's start: eviction and merging --------------------------------------------------------------------------
def list_packs(cache_dir):
    """[(name, bytes of .rpk + .rix, mtime)] of the packs whose both files are there; orphans' paths separately."""
    d = os.path.join(cache_dir, "revisit", SUBDIR)
    files = {}
    try:
        with os.scandir(d) as it:
            for de in it:
                for suf in (PACK_SUFFIX, INDEX_SUFFIX):
                    if de.name.endswith(suf):
                        try:
                            st = de.stat()
                        except OSError:
                            continue
                        files.setdefault(de.name[:-len(suf)], {})[suf] = (st.st_size, st.st_mtime)
    except OSError:
        return [], []
    packs, orphans = [], []
    for name, got in files.items():
        if len(got) == 2:
            packs.append((name, got[PACK_SUFFIX][0] + got[INDEX_SUFFIX][0],
                          max(got[PACK_SUFFIX][1], got[INDEX_SUFFIX][1])))
        else:
            suf, (_size, mtime) = next(iter(got.items()))
            orphans.append((os.path.join(d, name + suf), mtime))
    return packs, orphans


def entries_of(cache_dir, name):
    """The ENT tuples of pack name (read from its .rix)."""
    try:
        with open(os.path.join(cache_dir, "revisit", SUBDIR, name + INDEX_SUFFIX), "rb") as f:
            data = f.read()
    except OSError:
        return []
    if not data.startswith(IMAGIC):
        return []
    body = data[len(IMAGIC):]
    return list(ENT.iter_unpack(body[:len(body) // ENT.size * ENT.size]))


def remove_pack(cache_dir, name):
    """A pack's two files removed (its index first: readers stop finding it). True when both are gone."""
    base = os.path.join(cache_dir, "revisit", SUBDIR, name)
    ok = True
    for suf in (INDEX_SUFFIX, PACK_SUFFIX):
        try:
            os.remove(base + suf)
        except FileNotFoundError:
            pass
        except OSError:
            ok = False
    return ok




def copy_out(cache_dir, name, ents, keep):
    """The records of pack name (ents: its ENT tuples) whose key20 is in keep appended to this process's pack, their
    stored bytes as they are (crc checked). Returns (records, bytes) copied."""
    st = store_for(cache_dir)
    try:
        fd = _open_read(os.path.join(cache_dir, "revisit", SUBDIR, name + PACK_SUFFIX))
    except OSError:
        return 0, 0
    n = b = 0
    lock = threading.Lock()
    try:
        for k, tag, codec, off, size, raw_n, crc in ents:
            if k not in keep:
                continue
            try:
                stored = _pread(fd, lock, size, off + REC.size)
            except OSError:
                continue
            if len(stored) != size or zlib.crc32(stored) != crc:
                continue
            st._append(tag, k, codec, stored, raw_n, crc)
            n, b = n + 1, b + REC.size + size + ENT.size
    finally:
        os.close(fd)
    return n, b


def prune(cache_dir, max_bytes, pinned=(), before=None, keep_s=600.0, files_match=None, now=None):
    """The revisit cache cut back to LOW_WATER of max_bytes once it is over (the server's start): its units, oldest
    first, a pack by its last append (whole: its pinned items, a saved area's, copied out first) and every file
    files_match(name) takes (the zips of before and the far textures kept beside the items: by their mtime, a hit
    touches them) - none changed within keep_s or since before (this run's processes write them). Then earlier runs'
    small packs merged into this process's pack. pinned: revisit keys kept. -> (units removed, packs merged, bytes
    left)."""
    now = time.time() if now is None else now
    before = now if before is None else before
    root = os.path.join(cache_dir, "revisit")
    packs, orphans = list_packs(cache_dir)

    def idle(mtime):
        return mtime < before and now - mtime >= keep_s

    for path, mtime in orphans:             # (a pack's file without its twin: a crash, a partial removal)
        if idle(mtime):
            try:
                os.remove(path)
            except OSError:
                pass
    units, total = [], 0
    if files_match is not None:
        for dirpath, dirs, names in os.walk(root):
            if dirpath == root:
                dirs[:] = [d for d in dirs if d not in (SUBDIR, "where")]
            for n in names:
                if not files_match(n):
                    continue
                p = os.path.join(dirpath, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                units.append((st.st_mtime, st.st_size, p, None))
                total += st.st_size
    for name, size, mtime in packs:
        units.append((mtime, size, None, name))
        total += size
    pinned = set(pinned or ())
    pinned20 = {key20(k) for k in pinned}
    removed, gone = 0, set()
    if total > max_bytes:
        for mtime, size, path, name in sorted(units, key=lambda u: u[0]):
            if total <= max_bytes * LOW_WATER:
                break
            if not idle(mtime):
                continue
            if name is None:
                if os.path.basename(path).split(".", 1)[0] in pinned:
                    continue
                try:
                    os.remove(path)
                except OSError:
                    continue
            else:
                ents = entries_of(cache_dir, name)
                keep = {e[0] for e in ents} & pinned20
                if keep:
                    try:
                        total += copy_out(cache_dir, name, ents, keep)[1]
                    except OSError:
                        continue
                if not remove_pack(cache_dir, name):
                    continue
                gone.add(name)
            total -= size
            removed += 1
    merged = _merge_small(cache_dir, [(n, s, m) for n, s, m in packs if n not in gone and idle(m)], gone)
    st = _stores.get(os.path.abspath(cache_dir))
    if st is not None and (gone or merged):
        st.refresh(force=True)
    return removed, merged, total


def _merge_small(cache_dir, idle_packs, gone):
    """Earlier runs' packs under SMALL_BYTES merged into this process's pack (MERGE_MIN of them or more): their items
    no newer pack holds, once each. Returns the packs merged."""
    small = sorted((m, n) for n, s, m in idle_packs if s < SMALL_BYTES)
    if len(small) < MERGE_MIN:
        return 0
    small_names = {n for _m, n in small}
    packs, _orphans = list_packs(cache_dir)
    newer = {}                  # key20 -> the newest mtime of a pack (not merged) holding it
    for n, _s, m in packs:
        if n in small_names or n in gone:
            continue
        for e in entries_of(cache_dir, n):
            if newer.get(e[0], -1.0) < m:
                newer[e[0]] = m
    done = set()
    merged = 0
    for m, n in reversed(small):        # (newest first: an item kept twice is taken from its latest pack)
        ents = entries_of(cache_dir, n)
        keep = {e[0] for e in ents if e[0] not in done and newer.get(e[0], -1.0) <= m}
        try:
            copy_out(cache_dir, n, ents, keep)
        except OSError:
            break                       # (the disk: the rest stay as they are)
        done |= keep
        if remove_pack(cache_dir, n):
            gone.add(n)
            merged += 1
    return merged
