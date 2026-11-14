"""New York's shard datasets on the data repo's Pages (nycmassing.py: the 2014 3-D model's setbacks; nyctanks.py: the
rooftop tanks on the 2024 photo): their manifest frozen per process and named in the keys of the tiles that use them.

Before (Codex review of the building years, 2026-11-14): a tile was keyed without the dataset, so a tile built while
the manifest was a 404 ("not published": nothing asked, nothing faulted), before a cell was added, or from shards an
in-place rebuild has replaced, kept its look under the same key for good; and the shards' fixed paths (14/x/y.npz,
cached on disk with no build in their names) could mix two builds.

Now:
- The build in use is the manifest pinned on disk when the process first asks for its identity (Shards.frozen, at a
  tile's key: blockworker._nyc_key, only for tiles near BOX), the same for the whole process. Its identity, "<built>-
  <the manifest's sha256, 12 hex>", is in the keys of those tiles; "none" without one, "off" when off. Nothing pinned
  yet: "none" for now and the manifest fetched in the background (pin), frozen at a later ask once it is on disk. A
  pinned one older than MANIFEST_TTL_S is fetched again in the background for the next process; a 404 unpins it.
- The data of a frozen build only: cells it lists (others: none there), fetched from the host its manifest names
  ("data": the mirror's folder of that build, immutable) else Pages, each checked against the manifest's entry for it
  (its sha256 when the builder wrote one, else its bytes); one that does not match, or a listed cell gone (404), is not
  had (the tile faulted, asked again after SHARD_RETRY_S). Kept on disk under the build's identity (<dir>/<id>/), old
  builds' folders pruned after PRUNE_S.
- A local folder (BNE_NYC_*_DIR): its manifest's identity ("local-<sha>"; "local" without one: every cell read).
"""
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time

log = logging.getLogger("earthd.nycshards")

BOX = (40.47, -74.28, 40.94, -73.68)    # south, west, north, east: the five boroughs (both datasets' coverage)
NEAR_DEG = 0.01                         # a tile this close to BOX is keyed on the datasets (records reach past a tile)
MANIFEST_TTL_S = 86400                  # a pinned manifest older than this is fetched again (for the next process)
MANIFEST_RETRY_S = 3600                 # a manifest fetch that failed (or a 404): asked again after this long
SHARD_RETRY_S = 600                     # a shard that could not be had (or did not match): asked again after this long
PRUNE_S = 7 * 86400                     # an older build's folder of shards is deleted once untouched this long
_UNSET = object()


class Shards:
    """A dataset's manifest and shards (subclasses: NAME, VERSION, ON, local_dir, base_url)."""

    NAME = None
    VERSION = "v1"

    def __init__(self, cache_dir, fetcher=None, local=None, base=None):
        self.root = os.path.join(cache_dir, "pagesdata", self.NAME, self.VERSION) if cache_dir else None
        self.local = local
        self.base = base
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.mem = {}
        self.failed = {}
        self._frozen = _UNSET
        self._pin_at = -1e18
        self._pinning = False

    def on(self):
        return True

    # --- the build in use -------------------------------------------------------------------------------------
    def _parse(self, body, local=False):
        try:
            j = json.loads(body.decode("utf-8"))
            cells = j.get("cells", {})
            if not isinstance(cells, dict):
                return None
        except (ValueError, AttributeError, UnicodeDecodeError):
            return None
        import railtags
        sha = hashlib.sha256(body).hexdigest()[:12]
        ident = f"local-{sha}" if local else f"{re.sub(r'[^0-9A-Za-z_.-]+', '_', str(j.get('built') or 'x'))[:40]}-{sha}"
        return {"id": ident, "cells": cells, "data": None if local else railtags.data_url(j)}

    def frozen(self):
        """{"id", "cells" ({"z/x/y": entry} or None: every cell, a local folder without one), "data"} of the build this
        process uses, or None (none pinned yet: asked for in the background, frozen at a later ask)."""
        with self.lock:
            if self._frozen is not _UNSET:
                return self._frozen
        got, pinned_at = None, None
        if self.local:
            try:
                with open(os.path.join(self.local, "manifest.json"), "rb") as fh:
                    got = self._parse(fh.read(), local=True)
            except OSError:
                got = {"id": "local", "cells": None, "data": None}
        elif self.root:
            path = os.path.join(self.root, "manifest.json")
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
                pinned_at = os.path.getmtime(path)
                got = self._parse(body)
            except OSError:
                got = None
        if not self.local and (got is None or time.time() - (pinned_at or 0.0) > MANIFEST_TTL_S):
            self._pin_later()
        if got is None:
            return None                 # (not frozen: a later ask may find it pinned)
        with self.lock:
            if self._frozen is _UNSET:
                self._frozen = got
                threading.Thread(target=self._prune, args=(got["id"],), daemon=True).start()
            return self._frozen

    def current(self):
        """The build frozen so far (frozen's), or None, without freezing one: only a tile's key freezes it (identity),
        so a tile's data is never a build its key does not name."""
        if self.local:                  # (a local folder: static, no key needed first)
            return self.frozen()
        with self.lock:
            return None if self._frozen is _UNSET else self._frozen

    def identity(self):
        """The tiles' key for this dataset: "off", "none" (no build frozen) or the frozen build's id."""
        if not self.on() or not (self.root or self.local):
            return "off"
        got = self.frozen()
        return got["id"] if got is not None else "none"

    def _pin_later(self):
        with self.lock:
            if self._pinning or time.time() - self._pin_at < MANIFEST_RETRY_S:
                return
            self._pinning, self._pin_at = True, time.time()
        threading.Thread(target=self.pin, name=f"{self.NAME}-manifest", daemon=True).start()

    def pin(self):
        """Fetch the manifest and pin it on disk (for a later ask's freeze, or the next process); a 404 unpins it.
        True when one is pinned."""
        import netfetch
        with self.lock:
            self._pin_at = time.time()      # (the next ask after MANIFEST_RETRY_S at the earliest)
        try:
            if self.fetcher is None:
                self.fetcher = netfetch.Fetcher()
            path = os.path.join(self.root, "manifest.json") if self.root else None
            try:
                body = self.fetcher.get(f"{self.base}/manifest.json", kind="demand").body
            except netfetch.NotFound:   # (not published, or withdrawn)
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                return False
            except Exception as e:      # (offline, the site failing: the pinned one stays)
                log.debug("%s manifest: %s: %s", self.NAME, type(e).__name__, e)
                return False
            if self._parse(body) is None or not path:
                return False
            _keep(path, body)
            return True
        finally:
            with self.lock:
                self._pinning = False

    def _prune(self, ident):
        if not self.root:
            return
        try:
            names = os.listdir(self.root)
        except OSError:
            return
        now = time.time()
        for n in names:
            p = os.path.join(self.root, n)
            try:
                if n.endswith(".npz"):          # (the first layout: shards by cell alone, no build)
                    os.remove(p)
                elif n != ident and os.path.isdir(p) and now - os.path.getmtime(p) > PRUNE_S:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass

    # --- the shards -------------------------------------------------------------------------------------------
    def cells(self):
        """The frozen build's cells ("z/x/y" set; None: every cell, a local folder without a manifest), or False (no
        build frozen yet by a key: no data, nothing faulted)."""
        got = self.current()
        if got is None:
            return False
        return None if got["cells"] is None else set(got["cells"])

    def path(self, key):
        """Where the frozen build's shard of cell key is kept on disk (None: no build, or no cache folder)."""
        got = self.current()
        if got is None or not self.root or self.local:
            return None
        return os.path.join(self.root, got["id"], f"{key.z}_{key.x}_{key.y}.npz")

    def body(self, key):
        """The frozen build's shard bytes of cell key (b"": none there, or no build frozen), or None (not had: offline,
        the host failing, gone, or not the manifest's: asked again after SHARD_RETRY_S)."""
        got = self.current()
        if got is None:
            return b""
        rel = f"{key.z}/{key.x}/{key.y}.npz"
        cells = got["cells"]
        entry = cells.get(rel[:-4]) if cells is not None else None
        if cells is not None and entry is None:
            return b""                  # (a cell the manifest does not list: none there)
        if self.local:
            try:
                with open(os.path.join(self.local, rel), "rb") as fh:
                    return fh.read()
            except OSError:
                return b""
        path = self.path(key)
        if path and os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    b = fh.read()
                if matches(entry, b):
                    return b
            except OSError:
                pass
        import netfetch
        import railtags
        with self.lock:
            if time.time() - self.failed.get(key, -1e18) < SHARD_RETRY_S:
                return None
            if self.fetcher is None:
                self.fetcher = netfetch.Fetcher()
            fetcher = self.fetcher
        try:
            b = railtags.fetch_data(fetcher, got["data"], self.base, rel)
        except Exception as e:          # (a listed cell gone, offline, limits: not had)
            log.debug("%s shard %s: %s: %s", self.NAME, key, type(e).__name__, e)
            b = None
        if b is not None and not matches(entry, b):
            log.warning("%s shard %s: not the one build %s lists (%d bytes)", self.NAME, key, got["id"], len(b))
            b = None
        if b is None:
            with self.lock:
                self.failed[key] = time.time()
            return None
        if path:
            _keep(path, b)
        return b


def matches(entry, body):
    """Is body the shard the manifest's entry names: its sha256 when it has one, else its byte count (else any)."""
    if not isinstance(entry, dict):
        return True
    sha = entry.get("sha256")
    if isinstance(sha, str):
        return hashlib.sha256(body).hexdigest() == sha
    n = entry.get("bytes")
    if isinstance(n, int):
        return len(body) == n
    return True


def _keep(path, body):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError as e:
        log.debug("nyc shards: keep %s: %s", path, e)


def near(lat0, lon0, lat1, lon1):
    """Does the lat / lon box come within NEAR_DEG of BOX."""
    return lat0 <= BOX[2] + NEAR_DEG and lat1 >= BOX[0] - NEAR_DEG and lon0 <= BOX[3] + NEAR_DEG \
        and lon1 >= BOX[1] - NEAR_DEG


def identity_near(cache_dir, lat0, lon0, lat1, lon1):
    """The New York datasets' identities for a tile's key over that lat / lon box: None far from BOX (the key as
    before), else (nycmassing's, nyctanks')."""
    if not near(lat0, lon0, lat1, lon1):
        return None
    import nycmassing
    import nyctanks
    return (nycmassing.identity(cache_dir), nyctanks.identity(cache_dir))
