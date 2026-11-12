"""BeamNG.Earth-data's GitHub Pages: a dataset's manifest (the build in use) pinned on disk, and the primitives that
read, pin and fetch it.

A dataset (rail, lanes, signs, regional...) publishes <PAGES>/<name>/manifest.json beside its shards; its "built"
names the build the shards on disk belong to, and its "data" (data_url) the https folder the build's files are served
from instead (the tile host, R2 behind Cloudflare's cache), or none: beside the manifest on Pages. The manifest pinned
on disk is used at once, whatever its age (the data on disk is always usable): one older than MANIFEST_TTL_S is
refreshed in the background, one fetch at a time, and kept when that fetch fails. Without one (a first run) one thread
fetches it and the others wait for its answer, at most FETCH_WAIT_S, instead of asking each for itself. (2026-11-09,
the night of 1.9.0: GitHub answered 504s for hours; every road worker process asked each dataset's manifest for itself,
2 tries x 20 s, two at a time, and dropped the data it had on disk once a day's manifest was due: world gen hung at
15 % for players; a test with 10 s 504s cost a first tile 17 s.)

rail tags, lane tags, sign tags (railtags.Shards) and the regional scenery (regionaldata) read their manifests and
fetch their files through the one state machine here: the pinning, the data host and the fetch-once policy are not
copied per dataset.
"""
import json
import logging
import os
import threading
import time

import netfetch

log = logging.getLogger("earthd.pagesdata")

PAGES = os.environ.get("BNE_DATA_PAGES", "https://tecnagamer.github.io/BeamNG.Earth-data")

MANIFEST_TTL_S = 24 * 3600      # the build in use is checked again after this long
RETRY_S = 3600                  # unreachable: asked again after this long
FETCH_WAIT_S = 90.0             # s a caller waits for another thread's manifest fetch (then goes on without the data)


def read_pin(d):
    """(the manifest pinned in folder d, its age s), or (None, None)."""
    try:
        path = os.path.join(d, "manifest.json")
        age = time.time() - os.path.getmtime(path)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), age
    except (OSError, TypeError, ValueError):
        return None, None


def data_url(m):
    """A dataset manifest's "data": the https folder its build's files are served from (the data repo's workflow puts
    them on the project's tile host, R2 behind Cloudflare's cache, data/<dataset>/<build>/, the same paths as on
    Pages), or None: the files beside the manifest on Pages. (2026-11-11: ~1.15 TB a month of players' reads against
    Pages' 100 GB soft limit.)"""
    d = m.get("data") if isinstance(m, dict) else None
    return d.rstrip("/") if isinstance(d, str) and d.startswith("https://") and len(d) > 8 else None


def fetch_data(fetcher, data, pages, rel, kind="demand"):
    """rel's bytes from a dataset's data host (data: data_url's, or None) else its Pages folder (pages). The data
    host failing (not a 404: that answers for the build) is asked of Pages as well, while Pages still has the files
    (old clients read them there); that failing too raises the data host's error, never a NotFound (no "none
    there" from a copy that may be gone)."""
    if not data:
        return fetcher.get(f"{pages}/{rel}", kind=kind).body
    try:
        return fetcher.get(f"{data}/{rel}", kind=kind).body
    except (netfetch.NotFound, netfetch.Deferred):
        raise
    except Exception as e:
        first = e
    try:
        return fetcher.get(f"{pages}/{rel}", kind=kind).body
    except Exception:
        raise first from None


def write_atomic(path, body):
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


def fetch_pin(fetcher, base, d):
    """The manifest of base (a dict), fetched and pinned in folder d (None: not kept); raises when it cannot be had."""
    body = fetcher.get(f"{base}/manifest.json", kind="demand").body
    got = json.loads(body.decode("utf-8"))
    if not isinstance(got, dict):
        raise ValueError("the manifest is not an object")
    if d:
        os.makedirs(d, exist_ok=True)
        write_atomic(os.path.join(d, "manifest.json"), body)
    return got


def build_of(manifest):
    """The build ("built") a manifest names, or None."""
    return manifest.get("built") if isinstance(manifest, dict) else None


class Manifest:
    """The manifest in use of one BeamNG.Earth-data Pages dataset (name: its folder under PAGES), pinned in
    <cache_dir>/pagesdata/<name> and shared by every process that reads the dataset.

    current() is the state machine a reader asks before using the data; known()/build() is the build in use as far as
    known without asking the network (the tiles' cache keys), read from the pinned copy before this run has asked.
    fetch(rel) reads a file of the build from the manifest's "data" host (data/None in self.data, data_url) else
    Pages. key (build_of) is what "another build" means for a dataset; on_change runs (under self.lock) when it
    changes, so decoded data made from the old one is dropped. local (() -> folder) reads that folder's manifest.json
    instead of Pages, once; recheck=False uses the first manifest in use for the rest of the run (regionaldata: the
    scenery changes with the build, and every cell built before it is stale)."""

    def __init__(self, cache_dir, fetcher, name, *, base=None, ttl=MANIFEST_TTL_S, retry_s=RETRY_S,
                 wait_s=FETCH_WAIT_S, key=build_of, on_change=None, local=None, recheck=True):
        self.name = name
        self.base = base or f"{PAGES}/{name}"
        self.dir = os.path.join(cache_dir, "pagesdata", name) if cache_dir else None
        self.fetcher = fetcher
        self.ttl, self.retry_s, self.wait_s = ttl, retry_s, wait_s
        self.key = key
        self.on_change = on_change
        self.local = local          # () -> local folder whose manifest.json is read instead of Pages (None: Pages)
        self.recheck = recheck      # False: once a manifest is in use, never ask for another
        self.lock = threading.Lock()
        self.manifest = None        # the manifest in use
        self.built = None           # its key (the build), what cache keys are made from
        self.data = None            # its "data" host (data_url), None: the files beside the manifest on Pages
        self.checked = 0.0
        self.down_at = 0.0
        self.fetching = None        # threading.Event while a manifest fetch runs (the others wait for its answer)

    def known(self, recheck_s=None):
        """The build in use as far as known without asking the network; before this run has asked for it, the last
        build pinned on disk (None until the manifest came). (The tiles built in a run's first minutes had other keys
        than the same tiles built later, in the revisit cache: a warm drive rebuilt and stored again ~1.5 GB of far
        tiles, 2026-11-04.) recheck_s: with none pinned yet, the folder is looked at again at most this often (None:
        once a run)."""
        if self.built is not None:
            return self.built
        got = getattr(self, "_pinned", False)
        # (none pinned yet: looked for again, at most every recheck_s: another process pins it a few seconds into a
        # first run, and a process that had looked once kept None in its keys for the whole run while the others had
        # the build, the next run's keys: a first visit's plans, cell facts and tiles were never found again, 2026-11-13)
        if got is False or (got is None and recheck_s is not None
                            and time.monotonic() - getattr(self, "_pin_at", 0.0) > recheck_s):
            self._pin_at = time.monotonic()
            got = self._pinned = build_of(read_pin(self.dir)[0])
        return got

    def current(self):
        """The manifest in use (a dict), or None (none reachable yet).

        The manifest pinned on disk is used whatever its age: a stale one (ttl) is refreshed in the background, one
        fetch at a time, and kept if that fails. Without one (a first run) one thread fetches it and the others wait
        for its answer (2026-11-09: the night of 1.9.0, GitHub answered 504s for hours; see the module docstring).
        A local folder's manifest is read once, before Pages, and kept; with recheck=False the first manifest in use
        is kept too."""
        local = self.local() if self.local is not None else None
        if local is not None:
            with self.lock:
                if self.manifest is not None:
                    return self.manifest
            try:
                with open(os.path.join(local, "manifest.json"), encoding="utf-8") as fh:
                    got = json.load(fh)
            except (OSError, ValueError) as e:
                log.warning("%s: %s: %s", self.name, local, e)
                got = None
            return self._take(got)
        if self.fetcher is None:
            return None
        now = time.time()
        with self.lock:
            if self.recheck:
                if self.built is not None and now - self.checked < self.ttl:
                    return self.manifest
            elif self.manifest is not None:
                return self.manifest
        pinned, age = read_pin(self.dir)
        if pinned is not None:
            refresh = False
            with self.lock:
                key, data = self.key(pinned), data_url(pinned)
                if not self.recheck or (key != self.built and (self.built is None or age < self.ttl)):
                    changed = key != self.built
                    self.manifest, self.built = pinned, key
                    if changed:
                        self._changed()
                if self.built == key:
                    self.data = data        # (the same build may have moved to another host)
                if age < self.ttl:
                    self.checked = now - age
                elif self.fetching is None and now - self.down_at >= self.retry_s:
                    self.fetching = threading.Event()
                    self.checked = now          # (asked once; the next check after ttl or a failure)
                    refresh = True
                got = self.manifest
            if refresh:
                threading.Thread(target=self._refresh, name=f"{self.name}-manifest", daemon=True).start()
            return got
        with self.lock:
            if self.built is None and now - self.down_at < self.retry_s:
                return None
            ev = self.fetching
            mine = ev is None
            if mine:
                ev = self.fetching = threading.Event()
        if not mine:
            ev.wait(self.wait_s)
            with self.lock:
                return self.manifest
        return self._refresh()

    def _take(self, got):
        """Take a manifest read without the network (None: not available): the one in use."""
        with self.lock:
            if got is None:
                self.down_at = time.time()
            else:
                key = self.key(got)
                changed = key != self.built
                self.manifest, self.built, self.data = got, key, data_url(got)
                if changed:
                    self._changed()
            return self.manifest

    def fetch(self, rel, kind="demand"):
        """The bytes of the build's file rel (path in the dataset's folder): from the manifest's "data" host, else
        (or that failing) Pages (fetch_data). Ask current()/build() first: self.data is what the manifest in use
        names, None before one is read."""
        return fetch_data(self.fetcher, self.data, self.base, rel, kind)

    def build(self):
        """The build in use ("built" of its manifest), or None (none reachable yet)."""
        self.current()
        return self.built

    def _changed(self):
        """The manifest in use changed (called under self.lock): drop what was decoded from the old one."""
        if self.on_change is not None:
            self.on_change()

    def _refresh(self):
        """Fetch the manifest (this thread holds self.fetching), pin it, and take its build; the manifest in use when
        it cannot be had."""
        now = time.time()
        try:
            try:
                got = fetch_pin(self.fetcher, self.base, self.dir)
            except Exception as e:      # (offline, not published yet: without it, or with the build pinned before)
                log.debug("%s manifest: %s: %s", self.name, type(e).__name__, e)
                with self.lock:
                    self.down_at = now
                    return self.manifest   # (a build learnt before stays in use)
            key = self.key(got)
            with self.lock:
                changed = key != self.built
                self.manifest, self.built, self.data, self.checked = got, key, data_url(got), now
                if changed:
                    self._changed()
            return got
        finally:
            with self.lock:
                ev, self.fetching = self.fetching, None
            if ev is not None:
                ev.set()
