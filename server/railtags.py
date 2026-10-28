"""OpenStreetMap's railway tags for Overture's rail segments (the user, 2026-10-27: "Build our own weekly rail file"):
the railway value (proposed and razed lines left out, disused and abandoned ones worn), gauge, electrification and
service by OSM way id, and the level crossings' and catenary masts' nodes, from the shards tools/build_rail_tags.py
writes weekly (GitHub Action of TecnaGamer/BeamNG.Earth-data, served by its GitHub Pages: PAGES).

A shard covers a z7 tile (~300 km): a place needs one to four, each fetched once and kept on disk for the manifest's
build (a new weekly build: fetched again as needed). Unreachable or not built yet: nothing is joined and the lines keep
Overture's classes and flags, as before. Data (c) OpenStreetMap contributors, ODbL 1.0.
"""
import gzip
import json
import logging
import os
import re
import threading
import time

import numpy as np

import netfetch

log = logging.getLogger("earthd.railtags")

PAGES = os.environ.get("BNE_DATA_PAGES", "https://tecnagamer.github.io/BeamNG.Earth-data")
ON = os.environ.get("BNE_RAILTAGS", "on") != "off"
SHARD_ZOOM = 7
MANIFEST_TTL_S = 24 * 3600      # the build in use is checked again after this long
RETRY_S = 3600                  # unreachable: asked again after this long
COLUMNS = ("railway", "gauge", "electrified", "service", "usage")


def way_id(record):
    m = re.match(r"^w(\d+)", record or "")
    return m.group(1) if m else None


class Shards:
    """A dataset of BeamNG.Earth-data's GitHub Pages (name: its folder, e.g. "rail", "fra"): z7 shards
    <PAGES>/<name>/<x>/<y>.json.gz and its manifest, each shard fetched once and kept on disk per build."""

    def __init__(self, cache_dir, fetcher, name):
        self.name = name
        self.base = f"{PAGES}/{name}"
        self.dir = os.path.join(cache_dir, "pagesdata", name) if cache_dir else None
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.built = None           # the manifest's build in use
        self.checked = 0.0
        self.shards = {}            # (x, y) -> dict
        self.down_at = 0.0

    def known(self):
        """The build in use as far as known without asking the network (versions(): every tile's cache key)."""
        return self.built

    def version(self):
        """The build in use ("built" of its manifest), or None (none reachable yet)."""
        if not ON or self.fetcher is None:
            return None
        now = time.time()
        with self.lock:
            if self.built is not None and now - self.checked < MANIFEST_TTL_S:
                return self.built
            if now - self.down_at < RETRY_S and self.built is None:
                return None
        pin = os.path.join(self.dir, "manifest.json") if self.dir else None
        built = None
        if pin and os.path.exists(pin) and now - os.path.getmtime(pin) < MANIFEST_TTL_S:
            try:
                with open(pin, encoding="utf-8") as fh:
                    built = json.load(fh).get("built")
            except (OSError, ValueError):
                built = None
        if built is None:
            try:
                body = self.fetcher.get(f"{self.base}/manifest.json", kind="demand").body
                built = json.loads(body.decode("utf-8")).get("built")
                if pin:
                    os.makedirs(self.dir, exist_ok=True)
                    with open(pin + ".part", "wb") as fh:
                        fh.write(body)
                    os.replace(pin + ".part", pin)
            except Exception as e:      # (offline, not published yet: without it)
                log.debug("%s manifest: %s: %s", self.name, type(e).__name__, e)
                with self.lock:
                    self.down_at = now
                    return self.built   # (a build learnt before stays in use)
        with self.lock:
            if built != self.built:
                self.shards.clear()
            self.built, self.checked = built, now
        return built

    def shard(self, x, y):
        """The shard of z7 tile (x, y) (a dict), {} none there, None unreachable."""
        built = self.version()
        if built is None:
            return None
        with self.lock:
            got = self.shards.get((x, y))
        if got is not None:
            return got
        d = os.path.join(self.dir, re.sub(r"[^0-9A-Za-z]+", "_", built)) if self.dir else None
        path = os.path.join(d, f"{x}_{y}.json.gz") if d else None
        got = None
        if path and os.path.exists(path):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    got = json.load(fh)
            except (OSError, ValueError):
                got = None
        if got is None:
            try:
                body = self.fetcher.get(f"{self.base}/{x}/{y}.json.gz", kind="demand").body
                got = json.loads(gzip.decompress(body).decode("utf-8"))
            except netfetch.NotFound:
                body, got = None, {}
            except Exception as e:
                log.debug("%s %s/%s: %s: %s", self.name, x, y, type(e).__name__, e)
                return None
            if path:
                os.makedirs(d, exist_ok=True)
                with open(path + ".part", "wb") as fh:
                    fh.write(body if body is not None else gzip.compress(b"{}"))
                os.replace(path + ".part", path)
        with self.lock:
            self.shards[(x, y)] = got
        return got

    def of_cell(self, key):
        """The shard holding map cell key."""
        shift = key.z - SHARD_ZOOM
        return self.shard(key.x >> shift, key.y >> shift)


class RailTags(Shards):
    def __init__(self, cache_dir, fetcher):
        super().__init__(cache_dir, fetcher, "rail")

    def join(self, feats, key):
        """Add the OSM tags (attrs osm_railway, osm_gauge, osm_electrified, osm_service, osm_usage) to cell key's rail
        segments, and its crossing / mast nodes (feats._rail_nodes: [(lon, lat, tags)]), in place. Nothing when the
        shard cannot be had."""
        sub = feats.attrs.get("subtype")
        if sub is None or not len(feats):
            return
        rail = np.flatnonzero(np.asarray(sub).astype(str) == "rail")
        if not len(rail):
            return
        sh = self.of_cell(key)
        if sh is None:
            return
        ways = sh.get("ways", {})
        rec = feats.attrs.get("src_record")
        cols = {c: np.full(len(feats), None, object) for c in COLUMNS}
        for i in rail:
            t = ways.get(way_id(str(rec[i])) or "") if rec is not None else None
            if t:
                for c in COLUMNS:
                    cols[c][i] = t.get(c)
        for c in COLUMNS:
            feats.attrs["osm_" + c] = cols[c]
        lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
        pad = 0.002
        feats.__dict__["_rail_nodes"] = [(lo, la, t) for _id, lo, la, t in sh.get("nodes", ())
                                         if lon0 - pad <= lo <= lon1 + pad and lat0 - pad <= la <= lat1 + pad]
