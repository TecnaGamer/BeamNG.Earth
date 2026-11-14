"""The regional scenery's data (PR #505: country borders, Quebec, European localities, French direction signs),
fetched on demand from BeamNG.Earth-data's Pages (<PAGES>/regional/, tools/build_regional_data.py's layout) and kept
on disk per build, instead of ~310 MB shipped inside every download for an opt-in feature.

    file(name)        the local path of a whole file (the border band files): fetched once, sha256-checked
    cell(ds, x, y)    the rows of one cell of a dataset ([] where it has none), None while unreachable
    dataset(ds)       {"cell", "cells", "meta"} of a dataset, None while unreachable
    signature()       the build in use (cache keys: the scenery changes with it), "absent" without one

BNE_REGIONAL_DIR: a local folder of that layout read instead (tests, probes, before a build is published).
configure(cache_dir, fetcher): MapData does it in every process that has one; until then (or without a fetcher)
only BNE_REGIONAL_DIR is read.
"""
import gzip
import hashlib
import json
import logging
import os
import threading
import time

log = logging.getLogger("regionaldata")

LOCAL = None               # (tests: patched; else BNE_REGIONAL_DIR, read when asked: _local)
NAME = "regional"
RETRY_S = 60.0              # an unreachable Pages asked again after this long
MANIFEST_TTL_S = 24 * 3600  # the manifest pinned on disk is used at once; one older than this is refreshed in the background
FETCH_WAIT_S = 90.0         # s a caller waits for another thread's manifest fetch

_lock = threading.Lock()
_state = {"cache": None, "fetcher": None, "manifest": None, "down_at": 0.0, "cells": {}, "fetching": None}


def _local():
    return LOCAL if LOCAL is not None else (os.environ.get("BNE_REGIONAL_DIR") or None)


def configure(cache_dir, fetcher):
    with _lock:
        if _state["fetcher"] is None:
            _state["cache"], _state["fetcher"] = cache_dir, fetcher


def _base():
    import railtags
    return f"{railtags.PAGES}/{NAME}"


def _write(path, body):
    """body to path whole: a temporary file of this process and thread's own, renamed over it. Several fetching the
    same file at once (the road processes' first tiles in Germany on a cold cache) each write a whole one; a shared
    temporary name had one's rename fail and another read a part of it. A rename refused with path there (Windows:
    another process holds it open) keeps that copy, another fetch's of the same bytes; a write that fails (a full disk)
    leaves no temporary file behind."""
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(body)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if not os.path.exists(path):
            raise


def _dir():
    return os.path.join(_state["cache"], "pagesdata", NAME) if _state["cache"] else None


def _pinned():
    """(the manifest pinned on disk, its age s), or (None, None)."""
    d = _dir()
    try:
        path = os.path.join(d, "manifest.json")
        age = time.time() - os.path.getmtime(path)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), age
    except (OSError, TypeError, ValueError):
        return None, None


def _refresh():
    """Fetch the manifest (this thread holds _state["fetching"]) and pin it; the one in use when it cannot be had."""
    try:
        try:
            body = _state["fetcher"].get(f"{_base()}/manifest.json", kind="demand").body
            got = json.loads(body.decode("utf-8"))
            d = _dir()
            if d:
                os.makedirs(d, exist_ok=True)
                _write(os.path.join(d, "manifest.json"), body)
        except Exception as e:      # (offline, not published yet: the last one pinned on disk, else none)
            log.info("regional data manifest: %s: %s", type(e).__name__, e)
            got = None
        with _lock:
            if got is None:
                _state["down_at"] = time.time()
            else:
                _state["manifest"] = got
            return _state["manifest"]
    finally:
        with _lock:
            ev, _state["fetching"] = _state.get("fetching"), None
        if ev is not None:
            ev.set()


def manifest():
    """The build's manifest (dict) or None (none reachable). The one pinned on disk at once, whatever its age (one older
    than MANIFEST_TTL_S refreshed in the background); without one, one thread fetches it and the others wait for it.
    (It was fetched by every process at its start, before the disk copy, and the night of 1.9.0 GitHub answered 504s
    for hours: a road process's first tile waited for it behind the other Pages manifests.)"""
    with _lock:
        if _state["manifest"] is not None:
            return _state["manifest"]
        if _local() is None and time.time() - _state["down_at"] < RETRY_S:
            return None
    if _local() is not None:
        try:
            with open(os.path.join(_local(), "manifest.json"), encoding="utf-8") as fh:
                got = json.load(fh)
        except (OSError, ValueError) as e:
            log.warning("BNE_REGIONAL_DIR %s: %s", _local(), e)
            got = None
        with _lock:
            if got is None:
                _state["down_at"] = time.time()
            else:
                _state["manifest"] = got
            return _state["manifest"]
    if _state["fetcher"] is None:
        return None
    pinned, age = _pinned()
    with _lock:
        if _state["manifest"] is not None:
            return _state["manifest"]
        if pinned is not None:
            _state["manifest"] = pinned
            refresh = age >= MANIFEST_TTL_S and _state.get("fetching") is None
            if refresh:
                _state["fetching"] = threading.Event()
        else:
            ev = _state.get("fetching")
            mine = ev is None
            if mine:
                ev = _state["fetching"] = threading.Event()
    if pinned is not None:
        if refresh:
            threading.Thread(target=_refresh, name="regional-manifest", daemon=True).start()
        return pinned
    if not mine:
        ev.wait(FETCH_WAIT_S)
        with _lock:
            return _state["manifest"]
    return _refresh()


def signature():
    m = manifest()
    return m.get("built", "absent") if m else "absent"


def _build_dir():
    m = manifest()
    if _local() is not None:
        return _local()
    d = _dir()
    return os.path.join(d, str(m.get("built", "x"))) if d and m else None


def _fetch(rel):
    """The bytes of rel (from BNE_REGIONAL_DIR, the disk copy, or Pages: kept), None unreachable."""
    d = _build_dir()
    if d is None:
        return None
    path = os.path.join(d, rel)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        if _local() is not None:
            return None
    try:
        body = _state["fetcher"].get(f"{_base()}/{rel}", kind="demand").body
    except Exception as e:
        log.info("regional data %s: %s: %s", rel, type(e).__name__, e)
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write(path, body)
    return body


def file(name):
    """The local path of a whole file of the build (sha256-checked), or None."""
    m = manifest()
    want = (m or {}).get("files", {}).get(name)
    if not want:
        return None
    body = _fetch(name)
    if body is None:
        return None
    if hashlib.sha256(body).hexdigest() != want:
        log.warning("regional data %s: checksum mismatch", name)
        if _local() is None:
            try:
                os.remove(os.path.join(_build_dir(), name))
            except OSError:
                pass
        return None
    return os.path.join(_build_dir(), name)


def dataset(ds):
    m = manifest()
    got = (m or {}).get("datasets", {}).get(ds)
    if got is not None and "_set" not in got:
        got["_set"] = {tuple(c) for c in got["cells"]}
    return got


def cell(ds, x, y):
    """The rows of cell (x, y) of dataset ds: [] where it has none, None while unreachable."""
    info = dataset(ds)
    if info is None:
        return None
    if (x, y) not in info["_set"]:
        return []
    key = (ds, x, y)
    with _lock:
        got = _state["cells"].get(key)
    if got is not None:
        return got
    body = _fetch(f"{ds}/{x}_{y}.json.gz")
    if body is None:
        return None
    got = tuple(json.loads(gzip.decompress(body).decode("utf-8")))
    with _lock:
        if len(_state["cells"]) >= 64:      # (a drive's few cells; a long one forgets the oldest)
            _state["cells"].pop(next(iter(_state["cells"])))
        _state["cells"][key] = got
    return got
