"""The revisit cache: what was built for an anchor kept on disk by the anchor's place, so a place visited again (a game
started where it was left, a teleport back) loads it instead of building it again. Teleport anchors sit on a grid
(the mod's geodesy.snapAnchor), so the same place gets the same frame, and what was built in it fits again.

An item is one record in a pack file (revisitpack.py, since 2026-11-11: each process appends to its own; one zstd
frame a record): its named blobs and its JSON data; the callers take the session's names out and put them back. The
zips of before (one a file, deflated) are still read, and a zip read is moved into a pack (and removed). Cut back at
the server's start (prune: the oldest packs, zips and kept files first) like the other caches.

Only a complete build is kept: one during which nothing it read failed or was not there yet (a download that failed,
a map cell that could not be read, land cover or land use not cached yet, the lidar rule for want of the lidar: those
builds leave things out and would be kept so). fault() marks that for the builds that read it: each build runs in its
own scope (scope() / begin(): the thread it runs in, the pool tasks it submits, the results of other tasks it takes:
task()), and is kept when its scope has no fault (Build.complete). A fault in a thread no scope covers (a background
thread of its own) is the process's: every build running then is not kept. Until 2026-11-13 every fault was the
process's: on a first visit the lidar rule's (roads.level_ranges, 100k-500k a visit) and the worker processes' other
threads' kept nearly nothing, and a place was warm only on its third visit.

The key holds the server's code (code_key: a hash of its source files and native module), so any change to how things
are built starts afresh.

Where each item stands (the map's cache view, covermap.py): store(..., where=where(...)) also appends one line to this
process's revisit/where/<pid>_<start>.tsv (one os.write under O_APPEND: whole lines, and only this process writes
the file; the handle closes after each append so Windows can remove an idle cache): the code_key, the settings it was
built with (sig(): one short hash per setting, compared with earthd.Server.world_signature()), kind, key, the anchor
and the item's box in degrees. compact() (the server's start)
folds the files into one and drops the lines whose item is gone."""
import hashlib
import json
import os
import re
import struct
import threading
import time
import zipfile

import revisitpack

ON = os.environ.get("BNE_REVISIT", "on") != "off"
SKIP = {k for k in os.environ.get("BNE_REVISIT_SKIP", "").split(",") if k}     # kinds left out (tests: "roads,far")
MAX_MB = float(os.environ.get("BNE_REVISIT_MB", "2000"))      # the cache's size, cut back at start (earthd)
revisitpack.CAP_BYTES = MAX_MB * 1e6
SUBDIR = "revisit"
CACHE_DIR = None            # the server's cache folder (blockworker._State, earthd.Server): stores without one at hand

WHERE_DIR = "where"
WHERE_KINDS = ("roads", "roadsg", "plans", "bldfar", "bldsky", "brfar", "fargeo", "far")    # (cellfacts: per cell, no place)
NEAR_KINDS = ("roads", "roadsg", "plans")    # the drivable world (the road geotiles too); the others the distant view
COMPACTED = threading.Event()           # set once compact() ran in this process (covermap reads the files after it)
_START = int(time.time())

_faults = [0]
_code = []
_lock = threading.Lock()


_tl = threading.local()
_orphans = [0]              # faults in threads no build scope covers (the process's: every build running is not kept)
# BNGE_REVISIT_SITES=1 (debugging): where each build's faults came from (Build.sites, why_not) and the process's
SITES = bool(os.environ.get("BNGE_REVISIT_SITES"))
_orphan_sites = {}


class Build:
    """One build's faults (scope / begin): fault() in the thread it runs in and in the pool tasks it submitted or whose
    results it took (task). complete: none, and no fault of the process's (an uncovered thread's) meanwhile."""
    __slots__ = ("n", "lidar", "orphans0", "sites")

    def __init__(self):
        self.n = self.lidar = 0
        self.orphans0 = _orphans[0]
        self.sites = {} if SITES else None

    @property
    def complete(self):
        return self.n == 0 and self.lidar == 0 and _orphans[0] == self.orphans0

    def why_not(self):
        """Why it is not complete (BNGE_REVISIT_SITES: the sites), or "" when it is."""
        if self.complete:
            return ""
        parts = [f"{k} x{v}" for k, v in sorted((self.sites or {}).items(), key=lambda kv: -kv[1])[:6]]
        if _orphans[0] != self.orphans0:
            parts.append(f"process x{_orphans[0] - self.orphans0}")
        return ", ".join(parts) or f"{self.n} faults, {self.lidar} lidar"


def _stack():
    st = getattr(_tl, "stack", None)
    if st is None:
        st = _tl.stack = []
    return st


def begin():
    """A new build's scope in this thread (end it with end(b), in the same thread): its Build."""
    b = Build()
    _stack().append(b)
    return b


def end(b):
    """b's scope ended (begin): b, its faults counted."""
    st = _stack()
    for k in range(len(st) - 1, -1, -1):
        if st[k] is b:
            del st[k]
            break
    return b


def depth():
    """This thread's open scopes (unwind)."""
    return len(_stack())


def unwind(n):
    """This thread's scopes past the first n closed (a job that raised before it ended its own: the next job of the
    thread must not run in them)."""
    st = _stack()
    del st[n:]


def scoped_call(fn, *a, **k):
    """(fn(*a, **k), its Build): fn run in a scope of its own (work handed to another thread: its faults read back)."""
    with scope() as b:
        return fn(*a, **k), b


def all_complete(builds):
    """Every Build of builds (None: not checked) complete."""
    return all(b is None or b.complete for b in builds)


class scope:
    """with scope() as b: a build's scope (begin / end); b.complete after it."""
    __slots__ = ("b",)

    def __enter__(self):
        self.b = begin()
        return self.b

    def __exit__(self, *exc):
        end(self.b)
        return False


def _site():
    import sys
    f = sys._getframe(2)
    out = []
    while f is not None and len(out) < 2:
        name = os.path.splitext(os.path.basename(f.f_code.co_filename))[0]
        if name not in ("revisit", "netfetch"):
            out.append(f"{name}.{f.f_code.co_name}")
        f = f.f_back
    return "<".join(out) or "?"


def fault(lidar=False, here=False):
    """Something a build read failed or was not there yet (lidar: built on the lidar rule for want of the lidar): the
    builds whose scope this thread is in are not kept (and the thread's own count: thread_faults, thread_lidar_faults).
    In a thread no scope covers: the process's (every build running now), unless lidar or here: said where the short
    thing is read, in the reader's thread (the lidar rule in level_ranges, plans made short of something), so nothing
    else read it."""
    _faults[0] += 1
    if not lidar:
        _tl.n = getattr(_tl, "n", 0) + 1
    else:
        _tl.lidar = getattr(_tl, "lidar", 0) + 1
    st = getattr(_tl, "stack", None)
    if st:
        site = _site() if SITES else None
        for b in st:
            if lidar:
                b.lidar += 1
            else:
                b.n += 1
            if site is not None and b.sites is not None:
                b.sites[site] = b.sites.get(site, 0) + 1
    elif not (lidar or here):
        _orphans[0] += 1
        if SITES:
            k = (threading.current_thread().name.split("_")[0], _site())
            n = _orphan_sites[k] = _orphan_sites.get(k, 0) + 1
            if n == 1 or n % 100 == 0:
                import logging
                logging.getLogger("earthd.revisit").info("revisit: the process's fault x%d in thread %s at %s", n, *k)


class _Task:
    """A pool task's own faults (task): handed to the threads that take its result."""
    __slots__ = ("n", "lidar", "sites")

    def __init__(self):
        self.n = self.lidar = 0
        self.sites = {} if SITES else None


def task(fn):
    """(fn wrapped, its faults) for a pool: fn runs in the submitting thread's scopes (a build's helper task) and its
    own (_Task), whose faults go to every thread that takes its result (bind: a shared download's other waiters)."""
    carried = tuple(getattr(_tl, "stack", None) or ())
    own = _Task()

    def run(*a, **k):
        st = _stack()
        saved = st[:]
        st[:] = [*carried, own]
        try:
            return fn(*a, **k)
        finally:
            st[:] = saved
    import functools
    try:
        functools.update_wrapper(run, fn)      # (its name for the runtime profiler's job list)
    except (AttributeError, TypeError):
        pass
    return run, own


def take(own):
    """This thread took a result of a task with faults own (task): its scopes have them too."""
    if not (own.n or own.lidar):
        return
    st = getattr(_tl, "stack", None)
    if not st:
        return
    for b in st:
        if b is own:
            continue
        b.n += own.n
        b.lidar += own.lidar
        if b.sites is not None and own.sites:
            for k, v in own.sites.items():
                b.sites[k] = b.sites.get(k, 0) + v


def bind(fut, own):
    """fut (a concurrent.futures.Future of task's fn) hands own's faults to whoever takes its result or exception."""
    result, exception = fut.result, fut.exception

    def res(timeout=None):
        try:
            return result(timeout)
        finally:
            take(own)

    def exc(timeout=None):
        try:
            return exception(timeout)
        finally:
            take(own)
    fut.result, fut.exception = res, exc
    return fut


def carry(n, lidar=0):
    """This thread takes the faults (thread_faults, thread_lidar_faults) a helper thread counted for work it did for it
    (grading's cells' bridge tables made side by side): its scopes too."""
    if n:
        _tl.n = getattr(_tl, "n", 0) + n
    if lidar:
        _tl.lidar = getattr(_tl, "lidar", 0) + lidar
    for b in getattr(_tl, "stack", None) or ():
        b.n += n
        b.lidar += lidar


def faults():
    """Every fault of this process so far (tests, statistics; a build's own: scope / begin)."""
    return _faults[0]


def thread_faults():
    """This thread's faults (fault): what a computation done in this thread alone read short (roads._cell_ranges)."""
    return getattr(_tl, "n", 0)


def thread_lidar_faults():
    """This thread's lidar rule faults (fault(lidar=True)): built on the rule for want of the lidar."""
    return getattr(_tl, "lidar", 0)


_PRODUCES = re.compile(r"\brevisit\.(?:key|load|store)\(|\b_revisit_tile\(")
_IMPORT = re.compile(r"^[ \t]*(?:from[ \t]+([A-Za-z_]\w*)[\w.]*[ \t]+import\b|import[ \t]+([^#\n]+))", re.M)


# Modules that change how items are kept, not what they hold: left out of the key (a change of the pack format keeps
# the items; revisitpack reads its own files' versions)
STORAGE_ONLY = ("revisitpack",)


def source_closure(here):
    """The server modules (file names) whose code the cached items depend on: the ones that make or read them (a
    revisit.key / load / store or _revisit_tile call), and every server module they import, anywhere in their code (a
    function's own imports too), and so on. The rest (earthd, its pools and schedulers, the UI's helpers: ~25 of 118
    modules) changes nothing the cache keeps: an update of those alone kept every place's cache, where any change
    of any module made every place a first visit again (a dev build each push; 2026-11-02)."""
    srcs = {}
    for name in os.listdir(here):
        if name.endswith(".py"):
            try:
                with open(os.path.join(here, name), encoding="utf-8") as f:
                    srcs[name[:-3]] = f.read()
            except (OSError, UnicodeDecodeError):
                pass
    todo = [m for m, src in srcs.items() if m != "revisit" and _PRODUCES.search(src)]
    seen = set()
    while todo:
        m = todo.pop()
        if m in seen or m not in srcs:
            continue
        seen.add(m)
        for a, b in _IMPORT.findall(srcs[m]):
            for n in ([a] if a else [p.strip().split(" ")[0].split(".")[0] for p in b.split(",")]):
                if n in srcs and n not in seen and n not in STORAGE_ONLY:
                    todo.append(n)
    seen.add("revisit")
    return sorted(f"{m}.py" for m in seen)


def source_id(here):
    """The fingerprint of source_closure's files and the native module (a packaged app's REVISIT_ID, tools/build_app.py)."""
    h = hashlib.sha1()
    names = source_closure(here) + sorted(n for n in os.listdir(here) if n.startswith("earthfast"))
    for name in names:
        try:
            with open(os.path.join(here, name), "rb") as f:
                h.update(name.encode() + b"\0" + f.read())
        except OSError:
            pass
    return h.hexdigest()


# BNE_* settings that change no item's content (the cache's own, how far ahead tiles are wanted): not build switches
NOT_SWITCHES = ("BNE_REVISIT", "BNE_REVISIT_MB", "BNE_REVISIT_SKIP", "BNE_ROADS_SEND_M", "BNE_ROADS_DROP_M",
                "BNE_ROADS_LEAD_S", "BNE_ROADS_LEAD_M")


def code_key():
    """A hash of the code the cached items depend on (source_id; a packaged app's REVISIT_ID, written by its build) and
    the build switches (BNE_*), once per process."""
    with _lock:
        if not _code:
            here = os.path.dirname(os.path.abspath(__file__))
            h = hashlib.sha1()
            got = None
            # (a packaged app's modules are in its archive, not beside __file__: the build's fingerprint is; without one
            # a Python-only update reused the older build's items. From PR #368. An older build's CODE_ID: all of it)
            for name in ("REVISIT_ID", "CODE_ID"):
                try:
                    with open(os.path.join(here, name), "rb") as f:
                        got = name.encode() + b"\0" + f.read()
                    break
                except OSError:
                    pass
            if got is None or any(n.endswith(".py") for n in os.listdir(here)):
                got = source_id(here).encode()      # (from the sources beside it: a dev tree, a source install)
            h.update(got)
            for k in sorted(os.environ):                # (the build switches: BNE_*)
                if k.startswith("BNE_") and k not in NOT_SWITCHES:
                    h.update(f"{k}={os.environ[k]}".encode())
            import heightmap        # (the lighter ground's 2 m squares: road tiles drape on its lattice; 1 m keys as before)
            if heightmap.SQUARE != 1.0:
                h.update(f"square={heightmap.SQUARE:g}".encode())
            _code.append(h.hexdigest()[:16])
        return _code[0]


def key(kind, *parts):
    """The cache key of an item: its kind, what it was built from (the anchor's lat/lon, the tile, the settings) and
    the code."""
    return hashlib.sha1(repr((kind, code_key(), parts)).encode()).hexdigest()


# Items kept across code changes (the user, 2026-11-02: road plans and cell facts survive updates): keyed by their
# output's format number instead of the code (plancache.PLANS_FORMAT, roads.FACTS_FORMAT: bumped whenever a change
# makes other output; tests/test_bake_formats.py fails on its fixture until they are), with the build switches still.
_formats = {}               # kind -> its current format marker (format_marker), for the map's cache view


def switches_key():
    """A hash of the build switches (BNE_*) alone, once per process: code_key's without the code."""
    with _lock:
        if not _switches:
            h = hashlib.sha1()
            for k in sorted(os.environ):
                if k.startswith("BNE_") and k not in NOT_SWITCHES:
                    h.update(f"{k}={os.environ[k]}".encode())
            _switches.append(h.hexdigest()[:16])
        return _switches[0]


_switches = []


def format_marker(kind, fmt):
    """What a format-keyed item's sidecar line holds in place of the code key ("fmt:<kind>:<n>:<switches>")."""
    m = f"fmt:{kind}:{fmt}:{switches_key()}"
    _formats[kind] = m
    return m


def format_key(kind, fmt, *parts):
    """key() for an item kept across code changes: its kind, its output format fmt and what it was built from."""
    return hashlib.sha1(repr((kind, format_marker(kind, fmt), parts)).encode()).hexdigest()


def current_codes():
    """The sidecar code values of items this server reads: its code_key and the format markers (the plans' always:
    they are stored by the road processes, the map's cache view asks in the main one)."""
    import plancache
    format_marker("plans", plancache.plans_format())
    return {code_key(), *_formats.values()}


def _path(cache_dir, kind, k):
    return os.path.join(cache_dir, SUBDIR, kind, k[:2], k + ".zip")


def sig(**settings):
    """The settings an item was built with, as the sidecar keeps them: "name=hash,..." (sorted; 8 hex of each value's
    repr). earthd.Server.world_signature() gives the server's current values under the same names."""
    return ",".join(f"{n}={_h(v)}" for n, v in sorted(settings.items()))


def _h(v):
    return hashlib.sha1(repr(v).encode()).hexdigest()[:8]


def sig_current(row_sig, current):
    """Was a sidecar row built with the current settings (current: {name: value}, world_signature())? Every setting the
    row names must have the current value's hash; a row naming none is current."""
    for part in row_sig.split(","):
        if not part:
            continue
        name, _, h = part.partition("=")
        if name not in current or _h(current[name]) != h:
            return False
    return True


def where(lat, lon, s, w, n, e, settings):
    """store()'s where: the anchor (lat, lon), the item's box (degrees) and its sig()."""
    return (float(lat), float(lon), float(s), float(w), float(n), float(e), settings)


def _note(cache_dir, kind, k, wh, code=None):
    """One sidecar line for item k (never raises)."""
    try:
        lat, lon, s, w, n, e, settings = wh
        line = (f"{code or code_key()}\t{settings}\t{kind}\t{k}\t{lat:.7f}\t{lon:.7f}\t{s:.6f}\t{w:.6f}\t{n:.6f}\t{e:.6f}\n")
        d = os.path.join(cache_dir, SUBDIR, WHERE_DIR)
        path = os.path.join(d, f"{os.getpid()}_{_START}.tsv")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        # Keep the handle only while writing. Cached descriptors blocked Windows cache cleanup and, on Unix,
        # silently wrote into an unlinked file after a cache directory was replaced. Serialise our threads too:
        # Windows' append descriptors do not promise POSIX's atomic seek-to-end/write between different handles.
        with _lock:
            try:
                fd = os.open(path, flags, 0o644)
            except FileNotFoundError:
                os.makedirs(d, exist_ok=True)
                fd = os.open(path, flags, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
    except Exception:
        pass


def parse_where(line):
    """A sidecar line -> (code, sig, kind, key, lat, lon, s, w, n, e), or None (a damaged one)."""
    p = line.rstrip("\n").split("\t")
    if len(p) != 10 or p[2] not in WHERE_KINDS:
        return None
    try:
        return (p[0], p[1], p[2], p[3]) + tuple(float(v) for v in p[4:])
    except ValueError:
        return None


def compact(cache_dir, before=None):
    """The sidecar files last written before `before` (s; default now) folded into one: a line per item (the last),
    only those whose item is still there (the start-up prune removes the oldest). Run once at the server's start,
    after the prune. Returns the lines kept. Sets COMPACTED."""
    d = os.path.join(cache_dir, SUBDIR, WHERE_DIR)
    before = time.time() if before is None else before
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".tsv"))
    except OSError:
        COMPACTED.set()
        return 0
    try:
        old, rows = [], {}
        mine = f"{os.getpid()}_{_START}.tsv"
        for n in names:
            p = os.path.join(d, n)
            try:
                if n == mine or os.path.getmtime(p) >= before:
                    continue                # (a process writing now)
                with open(p, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        r = parse_where(line)
                        if r is not None:
                            rows[(r[2], r[3])] = line if line.endswith("\n") else line + "\n"
                old.append(p)
            except OSError:
                continue
        if not old:
            return 0
        lines = [line for (kind, k), line in rows.items() if has(cache_dir, kind, k)]
        out = os.path.join(d, f"c{os.getpid()}_{int(before)}.tsv")
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)
        os.replace(tmp, out)
        for p in old:
            if p != out:
                try:
                    os.remove(p)
                except OSError:
                    pass
        return len(lines)
    finally:
        COMPACTED.set()


# A far tile's texture (and its road mask) kept as a file beside its zip, not in it: a hit hard-links it into the
# session folder (place) and keeping one hard-links the session's file in (Side on store), so neither writes its
# bytes again: every warm visit unzipped and wrote each far texture, ~0.5 GB of a warm 8 min drive at 150 mph
# (2026-11-04, Discord: "hard on the drive"). Zips of before keep them inside and still load.
# (and a block's base photo, earthd Server.base_revisit_key: its photo linked the same way, the game's packed copy of it
# written as a file of its own both ways, for that one lives in the game's cache folder, which the game writes; and a
# finished far tile, farfin: its texture and road mask linked beside it)
SIDE_KINDS = frozenset({"far", "fartex", "basephoto", "farfin"})
SIDE_NAMES = ("tex.dds", "mask.dds", "photo", "base.dds")
SIDE_SUFFIXES = tuple("." + n for n in SIDE_NAMES)


class Side:
    """A blob that is a file: its path (load: the kept file; store: a file to keep, linked in)."""
    __slots__ = ("path",)

    def __init__(self, path):
        self.path = path

    def read(self):
        with open(self.path, "rb") as f:
            return f.read()


def _side(path, name):
    return f"{path[:-4]}.{name}"


def _link_or_copy(src, dst):
    """dst becomes src's content atomically: a hard link (nothing written), a copy where links fail (another volume)."""
    import shutil
    tmp = f"{dst}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.link(src, tmp)
    except OSError:
        shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def place(blob, dst):
    """A loaded blob written to dst (atomically, staging): a Side hard-linked, bytes written."""
    import staging
    tmp = staging.tmp_for(dst)
    if isinstance(blob, Side):
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        # (a link only through a unique staging name: the fallback name, dst + ".tmp", is the one every writer opens
        # with "wb", and a link left there by a crash would have been truncated with the cache's file behind it)
        linked = False
        if tmp != dst + ".tmp":
            try:
                os.link(blob.path, tmp)
                linked = True
            except OSError:
                pass
        if not linked:
            import shutil
            shutil.copyfile(blob.path, tmp)
    else:
        with open(tmp, "wb") as f:
            f.write(blob)
    staging.replace(tmp, dst)


_HEAD = struct.Struct("<I")


def _encode(inside, side, extra):
    """A record's bytes: the JSON (names, sizes, side files, extra) and the blobs one after the other."""
    meta = json.dumps({"names": list(inside), "sizes": [len(b) for b in inside.values()], "side": side,
                       "extra": extra}).encode("utf-8")
    return b"".join([_HEAD.pack(len(meta)), meta, *inside.values()])


def _decode(raw):
    """({name: bytes}, meta) of a record's bytes (_encode)."""
    n, = _HEAD.unpack_from(raw)
    meta = json.loads(raw[_HEAD.size:_HEAD.size + n])
    at, blobs = _HEAD.size + n, {}
    for name, size in zip(meta["names"], meta["sizes"]):
        blobs[name] = raw[at:at + size]
        at += size
    if at != len(raw):
        raise ValueError("revisit: a record's blobs do not add up")
    return blobs, meta


def _write_sides(path, kind, blobs):
    """The SIDE_NAMES blobs of a SIDE_KINDS item as files beside its key (linked in when a Side): their names."""
    side = [n for n in SIDE_NAMES if n in blobs] if kind in SIDE_KINDS else []
    for n in side:          # (before the record: a record there means its files are)
        b = blobs[n]
        if isinstance(b, Side):
            if os.path.abspath(b.path) != os.path.abspath(_side(path, n)):
                _link_or_copy(b.path, _side(path, n))
        else:
            st = f"{_side(path, n)}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(st, "wb") as f:
                f.write(b)
            os.replace(st, _side(path, n))
    return side


def store(cache_dir, kind, k, blobs, extra=None, where=None, code=None):
    """Keep an item under key k: blobs {name: bytes, or Side(path): a file linked in}, extra JSON data; where (where()):
    the place it stands, for the map's cache view (a sidecar line; code: its code value there, format_marker's for a
    format-keyed item, else code_key). Never raises; False when not kept."""
    if not ON or kind in SKIP or os.environ.get("BNGE_WORLD_BUNDLE"):
        return False
    path = _path(cache_dir, kind, k)
    try:
        if kind in SIDE_KINDS and any(n in blobs for n in SIDE_NAMES):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        side = _write_sides(path, kind, blobs)
        inside = {n: (b.read() if isinstance(b, Side) else b) for n, b in blobs.items() if n not in side}
        revisitpack.store_for(cache_dir).put(kind, k, _encode(inside, side, extra))
        if where is not None:
            _note(cache_dir, kind, k, where, code)
        return True
    except Exception:
        return False


def has(cache_dir, kind, k):
    """Is item k kept (in a pack, or a zip of before)? Nothing read."""
    try:
        if revisitpack.store_for(cache_dir).has(kind, k):
            return True
    except Exception:
        pass
    return os.path.exists(_path(cache_dir, kind, k))


def load(cache_dir, kind, k):
    """({name: bytes}, extra) of the item under key k, or None when there is none (or it cannot be read)."""
    if not ON or kind in SKIP or os.environ.get("BNGE_WORLD_BUNDLE"):
        return None
    path = _path(cache_dir, kind, k)
    try:
        raw = revisitpack.store_for(cache_dir).get(kind, k)
    except Exception:
        raw = None
    if raw is not None:
        try:
            blobs, meta = _decode(raw)
        except (ValueError, KeyError, TypeError, struct.error):
            return None
        return _with_sides(path, blobs, meta)
    if not os.path.exists(path):
        return None
    try:            # (a zip of before)
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read("meta.json"))
            blobs = {name: z.read(name) for name in meta["names"]}
    except Exception:
        return None
    record = _encode(blobs, list(meta.get("side") or ()), meta.get("extra"))
    got = _with_sides(path, blobs, meta)
    if got is not None:         # (moved into this process's pack: one file fewer, read quicker next time)
        try:
            revisitpack.store_for(cache_dir).put(kind, k, record)
            os.remove(path)
        except Exception:
            pass
    return got


def locate(cache_dir, kind, k):
    """(where item k's record lies (revisitpack.Store.locate), {side name: kept file}) of a pack item, or None (none
    kept, kept in a zip of before, or one of its side files gone): for a native reader of many items at once
    (farrestore). The side files are touched, as a load does."""
    if not ON or kind in SKIP or os.environ.get("BNGE_WORLD_BUNDLE"):
        return None
    try:
        loc = revisitpack.store_for(cache_dir).locate(kind, k)
    except Exception:
        return None
    if loc is None:
        return None
    sides = {}
    if kind in SIDE_KINDS:
        path = _path(cache_dir, kind, k)
        for n in SIDE_NAMES:
            sp = _side(path, n)
            try:
                os.utime(sp, None)
            except FileNotFoundError:
                continue
            except OSError:
                pass
            sides[n] = sp
    return loc, sides


def _with_sides(path, blobs, meta):
    """(blobs with the item's side files as Side, extra), or None when one of them is gone (cut back alone: built
    again). The side files are touched (used: kept longest when cut back)."""
    side = meta.get("side") or ()
    for n in side:
        sp = _side(path, n)
        if not os.path.exists(sp):
            return None
        blobs[n] = Side(sp)
        try:
            os.utime(sp, None)
        except OSError:
            pass
    return blobs, meta.get("extra")


def prune(cache_dir, pinned=(), before=None):
    """The cache cut back to MAX_MB (the server's start; revisitpack.prune): the oldest packs, zips of before and
    files kept beside the items first; pinned (the saved areas' keys) kept. -> (units removed, packs merged, bytes
    left)."""
    return revisitpack.prune(cache_dir, MAX_MB * 1e6, pinned, before,
                             files_match=lambda n: n.endswith(".zip") or n.endswith(SIDE_SUFFIXES))


def swap(text, tokens):
    """text with each (a, b) of tokens replaced in turn."""
    for a, b in tokens:
        text = text.replace(a, b)
    return text


SHAPE_RE = re.compile(r'"(?:shape|texture)":"([^"]+)"')   # (texture: a town sign's face, townsigns.write_face)


def shapes_in(text, vfs_dir):
    """The file names of the shapes a tile's JSON points to in vfs_dir."""
    pre = vfs_dir.rstrip("/") + "/"
    return sorted({s[len(pre):] for s in SHAPE_RE.findall(text) if s.startswith(pre) and "/" not in s[len(pre):]})
