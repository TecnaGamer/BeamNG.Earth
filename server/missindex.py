"""Remembered 404s ("this source has no tile there") kept as one small index file per tile column (GitHub #1081: a
player waited a very long time deleting the cache; 28 % of the developer's 734k cache files were empty <y>.404
markers, 203k of them in 16k folders).

A tile source's 404 at <base>/<src>/<z>/<x>/<y> is a line "<y> <unix s>\\n" appended to <base>/<src>/<z>/<x>/_misses;
a tile whose data arrived later is a tombstone line "<y> 0" (the last line of a y wins). The callers still name a miss
by its old marker path ("<dir>/<y>.404": record / clear / when): only the storage changed, and netfetch's miss TTL
(Fetcher.miss_alive) reads the line's time where it read the marker's mtime.

Several processes (the server's main one and its workers) write the same columns: a line goes in with one O_APPEND
write (on Windows under a byte-range lock: its C runtime's append is a seek and a write), so another process's lines
are never lost; nothing is ever rewritten in place. A line cut short (a process killed mid-write) is ended by the next
writer (a newline first) and skipped by the readers, as are damaged lines. Readers keep each column's lines in memory,
keyed by the file's (inode, size, mtime): one stat per lookup, as the marker's exists() was; any change is read
again whole (the files are small: 8 lines median, 136 at most in the developer's cache).

Old caches: <y>.404 files still count until the column is migrated (migrate(): their lines appended with the files'
mtimes as times, then the files deleted). cache.Cache migrates every marker it finds in its start-up walk (a thread of
its own) and then leaves a <src>/_misses_migrated flag in every source folder: lookups under a flagged source skip the
old-marker stat (a source without the flag pays one stat more per lookup, the cost of the marker check before).

Listeners (covermap: the map's cache view counts misses as "resolved"): fn(marker_path, added) for every miss this
process records, clears or migrates.
"""
import logging
import os
import threading
import time

log = logging.getLogger("earthd.missindex")

NAME = "_misses"                    # the column's index file
SUFFIX = ".404"                     # the old per-tile marker (and the name the callers use for a miss)
MIGRATED = "_misses_migrated"       # in a source folder: no old markers left under it
MEMO_COLUMNS = 8192                 # columns kept in memory per process (the oldest read dropped first)
FLAG_RECHECK_S = 60.0               # a source without the flag looks for it again this often
LOCK_AT = 1 << 30                   # Windows: the byte locked around an append (beyond any real size)
MAX_T = 1e11                        # (a time past this is a damaged line)
IS_WIN = os.name == "nt"

listeners = []

_lock = threading.Lock()
_memo = {}                          # index path -> ((ino, size, mtime_ns), {y: t}), oldest first
_flags = {}                         # source folder -> (flagged, checked at monotonic s)
_colflag = {}                       # column folder -> True: its source is flagged
_EMPTY = {}


# --- names ----------------------------------------------------------------------------------------
def split(path):
    """(column folder, y) of a marker path "<dir>/<y>.404"; (None, None) for anything else (rangeread's "<dir>/404")."""
    d, _, name = path.rpartition(os.sep)
    if os.altsep and os.altsep in name:
        d, name = os.path.split(path)
    if not name.endswith(SUFFIX):
        return None, None
    stem = name[:-len(SUFFIX)]
    if not stem.isdigit():
        return None, None
    return d, int(stem)


def marker(d, y):
    return os.path.join(d, f"{y}{SUFFIX}")


def index_path(d):
    return os.path.join(d, NAME)


# --- reading --------------------------------------------------------------------------------------
def parse(data):
    """{y: t} of an index file's bytes: whole lines only, the last line of a y wins, a "y 0" clears it; damaged lines
    (a cut-short write, noise) skipped."""
    out = {}
    end = data.rfind(b"\n")
    if end < 0:
        return out
    for line in data[:end].split(b"\n"):
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            y = int(parts[0])
            t = float(parts[1])
        except ValueError:
            continue
        if y < 0 or not 0.0 <= t < MAX_T:       # (also NaN)
            continue
        if t:
            out[y] = t
        else:
            out.pop(y, None)
    return out


def column(d):
    """{y: t} of the misses remembered in column folder d (the index file only; a copy is not made: don't change it).
    The hot path takes no lock (a dict read); the memo drops its oldest columns past MEMO_COLUMNS."""
    p = d + os.sep + NAME
    try:
        st = os.stat(p)
    except OSError:
        if p in _memo:
            with _lock:
                _memo.pop(p, None)
        return _EMPTY
    key = (st.st_ino, st.st_size, st.st_mtime_ns)
    m = _memo.get(p)
    if m is not None and m[0] == key:
        return m[1]
    try:
        with open(p, "rb") as f:
            data = f.read()
    except OSError:                 # (Windows: held for a moment by a clear) the lines as last read, if any
        return m[1] if m is not None else _EMPTY
    ys = parse(data)
    with _lock:
        _memo.pop(p, None)
        _memo[p] = (key, ys)        # (newest last)
        while len(_memo) > MEMO_COLUMNS:
            del _memo[next(iter(_memo))]
    return ys


def _source_of(d):
    """The source folder of column folder d (<src>/<z>/<x> -> <src>)."""
    return os.path.dirname(os.path.dirname(d))


def migrated(d):
    """Is the source of column d flagged as having no old markers left (MIGRATED)? (A yes is kept per column.)"""
    if d in _colflag:
        return True
    src = _source_of(d)
    f = _flags.get(src)
    if f is not None and f[0]:
        _yes(d)
        return True
    now = time.monotonic()
    if f is not None and now - f[1] < FLAG_RECHECK_S:
        return False
    ok = os.path.exists(os.path.join(src, MIGRATED))
    _flags[src] = (ok, now)
    if ok:
        _yes(d)
    return ok


def _yes(d):
    if len(_colflag) > 4 * MEMO_COLUMNS:
        _colflag.clear()
    _colflag[d] = True


def when(path):
    """The time (unix s) of the 404 remembered for marker path, or None. A path that names no tile (rangeread's
    "<dir>/404") is a marker file of its own: its mtime."""
    d, y = split(path)
    if d is None:
        try:
            return os.path.getmtime(path)
        except OSError:
            return None
    t = column(d).get(y)
    if t is not None:
        return t
    if migrated(d):
        return None
    try:
        return os.path.getmtime(path)      # an old marker not migrated yet
    except OSError:
        return None


def known(path):
    """A 404 is remembered for marker path (whatever its age)."""
    return when(path) is not None


# --- writing --------------------------------------------------------------------------------------
def _append(p, data):
    """data appended to file p in one write (made with its folder if missing); a line cut short before it is ended
    first. Windows: under a byte-range lock (LOCK_AT) so two processes' appends never land on the same offset."""
    try:
        fd = os.open(p, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    except FileNotFoundError:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd = os.open(p, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    locked = False
    try:
        if IS_WIN:
            import msvcrt
            try:
                os.lseek(fd, LOCK_AT, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)      # (retries ~10 s, then OSError: written unlocked)
                locked = True
            except OSError:
                pass
        size = os.fstat(fd).st_size
        if size:
            os.lseek(fd, size - 1, os.SEEK_SET)
            if os.read(fd, 1) != b"\n":
                data = b"\n" + data
        os.write(fd, data)
    finally:
        if locked:
            try:
                import msvcrt
                os.lseek(fd, LOCK_AT, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def _notify(path, added):
    for fn in list(listeners):
        try:
            fn(path, added)
        except Exception:           # (a view's bookkeeping never fails a download)
            log.debug("missindex listener failed", exc_info=True)


def _group(paths):
    cols = {}
    for p in paths:
        d, y = split(p)
        if d is None:
            raise ValueError(f"not a tile marker path: {p}")
        cols.setdefault(d, []).append(y)
    return cols


def record(paths, disk=None, t=None):
    """Remember 404s at marker paths (one path or a list; one append per column), at time t (default: now: the miss
    TTL starts). disk: the cache's bookkeeping (cache.Cache / NoCache: wrote() of the index file)."""
    if isinstance(paths, str):
        paths = [paths]
    t = time.time() if t is None else float(t)
    for d, ys in _group(paths).items():
        p = os.path.join(d, NAME)
        _append(p, "".join(f"{y} {t:.0f}\n" for y in ys).encode())
        if disk is not None:
            disk.wrote(p)
        for y in ys:
            _notify(marker(d, y), True)


def clear(path, disk=None):
    """Data arrived for marker path's tile: its remembered 404 (index line, or an old marker file) is cleared. True when
    there was one."""
    d, y = split(path)
    if d is None:
        try:
            os.remove(path)
        except OSError:
            return False
        if disk is not None:
            disk.forget(path)
        return True
    had = False
    if y in column(d):
        p = os.path.join(d, NAME)
        _append(p, f"{y} 0\n".encode())
        if disk is not None:
            disk.wrote(p)
        had = True
    if not migrated(d):
        try:
            os.remove(path)
            had = True
            if disk is not None:
                disk.forget(path)
        except OSError:
            pass
    if had:
        _notify(path, False)
    return had


# --- old markers ----------------------------------------------------------------------------------
def legacy(d):
    """[(y, mtime)] of the old <y>.404 marker files in column folder d."""
    out = []
    try:
        it = os.scandir(d)
    except OSError:
        return out
    with it:
        for de in it:
            name = de.name
            if not name.endswith(SUFFIX) or not name[:-len(SUFFIX)].isdigit():
                continue
            try:
                out.append((int(name[:-len(SUFFIX)]), de.stat(follow_symlinks=False).st_mtime))
            except OSError:
                continue
    return out


def migrate(d, disk=None, found=None):
    """Column folder d's old marker files into its index (their mtimes as the times; a y the index already has keeps
    its line), then deleted. found: [(y, mtime)] already listed (else listed now). -> markers migrated, or None when
    they could not be (an append that failed: the files stay)."""
    found = legacy(d) if found is None else found
    if not found:
        return 0
    have = column(d)
    lines = "".join(f"{y} {t:.0f}\n" for y, t in sorted(found) if y not in have)
    p = os.path.join(d, NAME)
    if lines:
        try:
            _append(p, lines.encode())
        except OSError as e:
            log.debug("missindex: %s not migrated: %s", d, e)
            return None
        if disk is not None:
            disk.wrote(p)
    n = 0
    for y, _ in found:
        m = marker(d, y)
        try:
            os.remove(m)
        except FileNotFoundError:
            pass
        except OSError:
            continue                # (held on Windows: its line is in; removed next time)
        n += 1
        if disk is not None:
            disk.forget(m)
        _notify(m, True)            # (after the forget: the view keeps it as a miss)
    return n


def flag_sources(base, skip=()):
    """MIGRATED written in every source folder under base (<root>/imagery, <root>/dem: a folder with zoom folders) that
    lacks it, but the ones in skip (old markers left there). -> flags written."""
    n = 0
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    for name in names:
        src = os.path.join(base, name)
        flag = os.path.join(src, MIGRATED)
        try:
            if src in skip or os.path.exists(flag) or not os.path.isdir(src) or os.path.islink(src):
                continue
            if not any(z.isdigit() for z in os.listdir(src)):
                continue
            open(flag, "wb").close()        # (empty: a marker the cache never evicts)
            n += 1
        except OSError:
            continue
    return n


def reset_memo():
    """Forget the memo and the flags (tests)."""
    with _lock:
        _memo.clear()
        _flags.clear()
        _colflag.clear()
