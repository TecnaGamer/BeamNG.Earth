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
are built starts afresh."""
import hashlib
import json
import os
import re
import threading
import zipfile

ON = os.environ.get("BNE_REVISIT", "on") != "off"
SKIP = {k for k in os.environ.get("BNE_REVISIT_SKIP", "").split(",") if k}     # kinds left out (tests: "roads,far")
MAX_MB = float(os.environ.get("BNE_REVISIT_MB", "2000"))      # the cache's size, cut back at start (earthd)
SUBDIR = "revisit"
CACHE_DIR = None            # the server's cache folder (blockworker._State, earthd.Server): stores without one at hand

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


def store(cache_dir, kind, k, blobs, extra=None):
    """Keep an item under key k: blobs {name: bytes}, extra JSON data. Never raises; False when not kept."""
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
