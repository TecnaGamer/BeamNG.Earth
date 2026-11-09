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

_lock = threading.Lock()
_state = {"cache": None, "fetcher": None, "manifest": None, "down_at": 0.0, "cells": {}}


def _local():
    return LOCAL if LOCAL is not None else (os.environ.get("BNE_REGIONAL_DIR") or None)


def configure(cache_dir, fetcher):
    with _lock:
        if _state["fetcher"] is None:
            _state["cache"], _state["fetcher"] = cache_dir, fetcher


def _base():
    import railtags
    return f"{railtags.PAGES}/{NAME}"


def _dir():
    return os.path.join(_state["cache"], "pagesdata", NAME) if _state["cache"] else None


def manifest():
    """The build's manifest (dict) or None (none reachable)."""
    with _lock:
        if _state["manifest"] is not None:
            return _state["manifest"]
        if _local() is None and time.time() - _state["down_at"] < RETRY_S:
            return None
    got = None
    if _local() is not None:
        try:
            with open(os.path.join(_local(), "manifest.json"), encoding="utf-8") as fh:
                got = json.load(fh)
        except (OSError, ValueError) as e:
            log.warning("BNE_REGIONAL_DIR %s: %s", _local(), e)
    elif _state["fetcher"] is not None:
        try:
            body = _state["fetcher"].get(f"{_base()}/manifest.json", kind="demand").body
            got = json.loads(body.decode("utf-8"))
            d = _dir()
            if d:
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "manifest.json.part"), "wb") as fh:
                    fh.write(body)
                os.replace(os.path.join(d, "manifest.json.part"), os.path.join(d, "manifest.json"))
        except Exception as e:      # (offline, not published yet: the last one pinned on disk, else none)
            log.info("regional data manifest: %s: %s", type(e).__name__, e)
            d = _dir()
            try:
                with open(os.path.join(d, "manifest.json"), encoding="utf-8") as fh:
                    got = json.load(fh)
            except (OSError, TypeError, ValueError):
                got = None
    with _lock:
        if got is None:
            _state["down_at"] = time.time()
        else:
            _state["manifest"] = got
        return _state["manifest"]


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
    with open(path + ".part", "wb") as fh:
        fh.write(body)
    os.replace(path + ".part", path)
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
