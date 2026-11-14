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

The manifest in use is pagesdata.Manifest's (shared with railtags): once one is in use this run keeps it, since every
cell decoded before another build is stale. Its "data" host is where the build's files are read (pagesdata.data_url),
else Pages.
"""
import gzip
import hashlib
import json
import logging
import os
import threading

import pagesdata

log = logging.getLogger("regionaldata")

LOCAL = None               # (tests: patched; else BNE_REGIONAL_DIR, read when asked: _local)
NAME = "regional"
RETRY_S = 60.0              # an unreachable Pages asked again after this long
MANIFEST_TTL_S = pagesdata.MANIFEST_TTL_S  # the manifest pinned on disk is used at once; one older is refreshed in the background
FETCH_WAIT_S = pagesdata.FETCH_WAIT_S      # s a caller waits for another thread's manifest fetch

_lock = threading.Lock()
_state = {"cache": None, "fetcher": None, "pages": None, "cells": {}}


def _local():
    return LOCAL if LOCAL is not None else (os.environ.get("BNE_REGIONAL_DIR") or None)


def _cells_changed():
    """Another build is in use (called under the Manifest's lock): the cells decoded from the old one are not it."""
    with _lock:
        _state["cells"].clear()


def _pages():
    """The manifest in use (pagesdata.Manifest), made on the first ask (or reset by configure)."""
    with _lock:
        got = _state.get("pages")
        if got is None:
            got = _state["pages"] = pagesdata.Manifest(
                _state["cache"], _state["fetcher"], NAME, retry_s=RETRY_S, local=_local, recheck=False,
                on_change=_cells_changed)
        return got


def configure(cache_dir, fetcher):
    with _lock:
        if _state["fetcher"] is None:
            _state["cache"], _state["fetcher"], _state["cells"] = cache_dir, fetcher, {}
            _state["pages"] = None      # (a Manifest with the new cache/fetcher; nothing read yet)


def _base():
    return f"{pagesdata.PAGES}/{NAME}"


def _write(path, body):
    """body to path whole (pagesdata.write_atomic: a temporary file of this process and thread's own, renamed over it)."""
    pagesdata.write_atomic(path, body)


def manifest():
    """The build's manifest (dict) or None (none reachable). The one pinned on disk at once, whatever its age (one older
    than MANIFEST_TTL_S refreshed in the background); without one, one thread fetches it and the others wait for it.
    Once one is in use it is kept (pagesdata.Manifest, recheck=False): every cell decoded before another build is
    stale. (It was fetched by every process at its start, before the disk copy, and the night of 1.9.0 GitHub answered
    504s for hours: a road process's first tile waited for it behind the other Pages manifests.)"""
    return _pages().current()


def signature():
    m = manifest()
    return m.get("built", "absent") if m else "absent"


def _build_dir():
    m = manifest()
    if _local() is not None:
        return _local()
    d = _pages().dir
    return os.path.join(d, str(m.get("built", "x"))) if d and m else None


def _fetch(rel):
    """The bytes of rel (from BNE_REGIONAL_DIR, the disk copy, or the manifest's data host / Pages: kept), None
    unreachable."""
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
        body = _pages().fetch(rel)      # (the "data" host the manifest names, else Pages)
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
