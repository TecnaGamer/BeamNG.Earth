"""The revisit cache: what was built for an anchor kept on disk by the anchor's place, so a place visited again (a game
started where it was left, a teleport back) loads it instead of building it again. Teleport anchors sit on a grid
(the mod's geodesy.snapAnchor), so the same place gets the same frame, and what was built in it fits again.

One file per item: a zip (deflated at level 1: ~5 times smaller, ~8 ms a city road tile to read back) of named blobs
and meta.json; the callers take the session's names out and put them back. Cut back at the server's start like the
other caches (cache.prune_dir: the least recently used first).

Only a complete build is kept: one during which nothing it may have read failed or was not there yet (a download
that failed, a map cell that could not be read, land cover or land use not cached yet: those builds leave things out
and would be kept so). fault() marks that, process wide; a build is kept when faults() did not change during it.

The key holds the server's code (code_key: a hash of its source files and native module), so any change to how things
are built starts afresh.

Where each item stands (the map's cache view, covermap.py): store(..., where=where(...)) also appends one line to this
process's revisit/where/<pid>_<start>.tsv (one os.write under O_APPEND: whole lines, and only this process writes
the file): the code_key, the settings it was built with (sig(): one short hash per setting, compared with
earthd.Server.world_signature()), kind, key, the anchor and the item's box in degrees. compact() (the server's start)
folds the files into one and drops the lines whose zip is gone."""
import hashlib
import json
import os
import re
import threading
import time
import zipfile

ON = os.environ.get("BNE_REVISIT", "on") != "off"
SKIP = {k for k in os.environ.get("BNE_REVISIT_SKIP", "").split(",") if k}     # kinds left out (tests: "roads,far")
MAX_MB = float(os.environ.get("BNE_REVISIT_MB", "2000"))      # the cache's size, cut back at start (earthd)
SUBDIR = "revisit"
CACHE_DIR = None            # the server's cache folder (blockworker._State, earthd.Server): stores without one at hand

WHERE_DIR = "where"
WHERE_KINDS = ("roads", "roadsg", "plans", "bldfar", "bldsky", "brfar", "fargeo", "far")    # (cellfacts: per cell, no place)
NEAR_KINDS = ("roads", "roadsg", "plans")    # the drivable world (the road geotiles too); the others the distant view
COMPACTED = threading.Event()           # set once compact() ran in this process (covermap reads the files after it)
_where = {}                             # cache dir -> this process's open sidecar fd
_START = int(time.time())

_faults = [0]
_code = []
_lock = threading.Lock()


def fault():
    """Something a build may read failed or was not there yet: builds running now are not kept."""
    _faults[0] += 1


def faults():
    return _faults[0]


def code_key():
    """A hash of the server's source files and native module (once per process)."""
    with _lock:
        if not _code:
            here = os.path.dirname(os.path.abspath(__file__))
            h = hashlib.sha1()
            for name in sorted(os.listdir(here)):
                # (a packaged app's modules are in its archive, not beside __file__: the build's CODE_ID, their
                # fingerprint, is; without it a Python-only update reused the older build's items. From PR #368)
                if name == "CODE_ID" or name.endswith(".py") or name.startswith("earthfast"):
                    try:
                        with open(os.path.join(here, name), "rb") as f:
                            h.update(name.encode() + b"\0" + f.read())
                    except OSError:
                        pass
            for k in sorted(os.environ):                # (the build switches: BNE_*)
                if k.startswith("BNE_") and k not in ("BNE_REVISIT", "BNE_REVISIT_MB", "BNE_REVISIT_SKIP"):
                    h.update(f"{k}={os.environ[k]}".encode())
            _code.append(h.hexdigest()[:16])
        return _code[0]


def key(kind, *parts):
    """The cache key of an item: its kind, what it was built from (the anchor's lat/lon, the tile, the settings) and
    the code."""
    return hashlib.sha1(repr((kind, code_key(), parts)).encode()).hexdigest()


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


def _note(cache_dir, kind, k, wh):
    """One sidecar line for item k (never raises)."""
    try:
        lat, lon, s, w, n, e, settings = wh
        line = (f"{code_key()}\t{settings}\t{kind}\t{k}\t{lat:.7f}\t{lon:.7f}\t{s:.6f}\t{w:.6f}\t{n:.6f}\t{e:.6f}\n")
        fd = _where.get(cache_dir)
        if fd is None:
            with _lock:
                fd = _where.get(cache_dir)
                if fd is None:
                    d = os.path.join(cache_dir, SUBDIR, WHERE_DIR)
                    os.makedirs(d, exist_ok=True)
                    fd = _where[cache_dir] = os.open(os.path.join(d, f"{os.getpid()}_{_START}.tsv"),
                                                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0),
                                                     0o644)
        os.write(fd, line.encode("utf-8"))
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
    only those whose zip is still there (the start-up prune removes the oldest). Run once at the server's start,
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
        lines = [line for (kind, k), line in rows.items() if os.path.exists(_path(cache_dir, kind, k))]
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


def store(cache_dir, kind, k, blobs, extra=None, where=None):
    """Keep an item under key k: blobs {name: bytes}, extra JSON data; where (where()): the place it stands, for the
    map's cache view (a sidecar line). Never raises; False when not kept."""
    if not ON or kind in SKIP:
        return False
    path = _path(cache_dir, kind, k)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for name, data in blobs.items():
                z.writestr(name, data)
            z.writestr("meta.json", json.dumps({"names": list(blobs), "extra": extra}))
        os.replace(tmp, path)
        if where is not None:
            _note(cache_dir, kind, k, where)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def load(cache_dir, kind, k):
    """({name: bytes}, extra) of the item under key k, or None when there is none (or it cannot be read)."""
    if not ON or kind in SKIP:
        return None
    path = _path(cache_dir, kind, k)
    if not os.path.exists(path):
        return None
    try:
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read("meta.json"))
            blobs = {name: z.read(name) for name in meta["names"]}
        try:
            os.utime(path, None)            # (used: kept longest when cut back)
        except OSError:
            pass
        return blobs, meta.get("extra")
    except Exception:
        return None


def swap(text, tokens):
    """text with each (a, b) of tokens replaced in turn."""
    for a, b in tokens:
        text = text.replace(a, b)
    return text


SHAPE_RE = re.compile(r'"shape":"([^"]+)"')


def shapes_in(text, vfs_dir):
    """The file names of the shapes a tile's JSON points to in vfs_dir."""
    pre = vfs_dir.rstrip("/") + "/"
    return sorted({s[len(pre):] for s in SHAPE_RE.findall(text) if s.startswith(pre) and "/" not in s[len(pre):]})
