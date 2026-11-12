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
import zipfile
import logging
import os
import re
import threading
import time

import numpy as np

import fast
import netfetch
import signshards

log = logging.getLogger("earthd.railtags")

PAGES = os.environ.get("BNE_DATA_PAGES", "https://tecnagamer.github.io/BeamNG.Earth-data")
ON = os.environ.get("BNE_RAILTAGS", "on") != "off"
SHARD_ZOOM = 7
MANIFEST_TTL_S = 24 * 3600      # the build in use is checked again after this long
RETRY_S = 3600                  # unreachable: asked again after this long
SHARD_RETRY_S = 600             # a shard that could not be had: asked again after this long (not by every tile meanwhile)
FETCH_WAIT_S = 90.0             # s a caller waits for another thread's manifest fetch (then goes on without the data)
COLUMNS = ("railway", "gauge", "electrified", "service", "usage", "highway")    # (highway: a rail trail, railways.drawn)


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


_WAY_ID = re.compile(r"^w(\d+)")


def way_id(record):
    m = _WAY_ID.match(record or "")
    return m.group(1) if m else None


class Ways:
    """A shard's "ways" ({way id: tags}) kept compact: the ids sorted (int64) and each way's tags an index into the
    shard's distinct tag sets. A dense shard decoded to ~57 MB of Python objects for 156k ways in every server process
    that read it (rail and lane tags: 1-2 GB over a Steam Deck's ~9 processes, 2026-11-02); the same lookups. The tags
    handed out are shared: read them, do not change them."""

    def __init__(self, ways):
        tags, at, ids, idx = [], {}, [], []
        for k in sorted(ways, key=int):
            t = ways[k]
            sig = tuple(sorted(t.items())) if isinstance(t, dict) else ("", repr(t))
            n = at.get(sig)
            if n is None:
                n = at[sig] = len(tags)
                tags.append(t)
            ids.append(int(k))
            idx.append(n)
        self.ids = np.array(ids, np.int64)
        self.idx = np.array(idx, np.int32)
        self.tags = tags

    @staticmethod
    def of(ways):
        """A Ways of a shard's ways dict, or the dict itself when an id is not a plain number."""
        if not isinstance(ways, dict) or not all(isinstance(k, str) and k.isdigit() for k in ways):
            return ways
        return Ways(ways)

    def row(self, key):
        """The index into tags of way id key's tag set, or -1."""
        try:
            k = int(key)
        except (TypeError, ValueError):
            return -1
        i = int(np.searchsorted(self.ids, k))
        if i < len(self.ids) and self.ids[i] == k:
            return int(self.idx[i])
        return -1

    def get(self, key, default=None):
        r = self.row(key)
        return self.tags[r] if r >= 0 else default

    def rows_of(self, codes, records):
        """Per Overture source record ("w<id>@<version>": records, codes their fast.ucs4) the index into tags of its
        way's tag set (way_id's), or -1 (no way id, not in the shard). One native call (fast.way_tag_rows: a regex and
        a numpy search per record were most of LaneTags.join, 2026-11-08); what it leaves to way_id is decided here."""
        rows = fast.way_tag_rows(codes, self.ids, self.idx)
        for a in np.flatnonzero(rows == -2).tolist():
            w = way_id(str(records[a])) or ""
            k = np.array([int(w) if w.isdigit() else -1], np.int64)[0]     # (get_many's: past int64 raises)
            rows[a] = self.row(int(k)) if k >= 0 else -1
        return rows

    def get_many(self, keys):
        """[tags or None] of each key (one search for all)."""
        ks = np.array([int(k) if isinstance(k, str) and k.isdigit() else -1 for k in keys], np.int64)
        if not len(ks) or not len(self.ids):
            return [None] * len(ks)
        i = np.minimum(np.searchsorted(self.ids, ks), len(self.ids) - 1)
        hit = self.ids[i] == ks
        return [self.tags[self.idx[j]] if h else None for j, h in zip(i.tolist(), hit.tolist())]

    def __len__(self):
        return len(self.ids)

    def __bool__(self):
        return len(self.ids) > 0


class Rules:
    """A lane shard's "rules" ({key: {value: [way ids]}}, tools/build_lane_tags.py: Germany's road rules) kept
    compact, as Ways: per key the way ids sorted (int64) and each one's value an index into the key's values; never a
    dict per way. A value's id list is delta-coded (the first id, then each minus the one before: [100, 5, 125] is
    100, 105, 230) and decoded with a cumulative sum. A malformed entry (not a dict, not a list, a list with an
    entry that is not an int: dropped whole, a delta list cannot be partly trusted) is left out."""

    def __init__(self, rules):
        self.keys = {}
        for k, by in rules.items():
            if not isinstance(by, dict):
                continue
            values, ids, idx = [], [], []
            for v, got in by.items():
                ok = got if isinstance(got, list) and all(isinstance(i, int) and not isinstance(i, bool)
                                                           for i in got) else []
                if ok:
                    ok = np.cumsum(np.array(ok, np.int64)).tolist()
                    ids.extend(ok)
                    idx.extend([len(values)] * len(ok))
                    values.append(v)
            ids = np.array(ids, np.int64)
            o = np.argsort(ids, kind="stable")
            self.keys[k] = (ids[o], np.array(idx, np.int32)[o], values)

    def get_many(self, key, ids):
        """[key's value or None] of each way id (strings as railtags.way_id gives them, or None)."""
        got = self.keys.get(key)
        ks = np.array([int(k) if isinstance(k, str) and k.isdigit() else -1 for k in ids], np.int64)
        if got is None or not len(got[0]) or not len(ks):
            return [None] * len(ks)
        at, idx, values = got
        i = np.minimum(np.searchsorted(at, ks), len(at) - 1)
        hit = at[i] == ks
        return [values[idx[j]] if h else None for j, h in zip(i.tolist(), hit.tolist())]


class Shards:
    """A dataset of BeamNG.Earth-data's GitHub Pages (name: its folder, e.g. "rail", "fra"): z7 shards
    <PAGES>/<name>/<x>/<y>.json.gz and its manifest, each shard fetched once and kept on disk per build. The manifest
    stays on Pages; the shards come from its "data" folder when it names one (data_url, fetch_data)."""

    def __init__(self, cache_dir, fetcher, name):
        self.name = name
        self.base = f"{PAGES}/{name}"
        self.dir = os.path.join(cache_dir, "pagesdata", name) if cache_dir else None
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.built = None           # the manifest's build in use
        self.data = None            # its "data" folder (data_url), None: the files on Pages
        self.checked = 0.0
        self.shards = {}            # (x, y) -> dict
        self.down_at = 0.0
        self.fetching = None        # threading.Event while a manifest fetch runs (the others wait for its answer)
        self.shard_down = {}        # (x, y) -> when it could not be had (SHARD_RETRY_S)

    def known(self):
        """The build in use as far as known without asking the network (versions(): every tile's cache key); before
        this run has asked for it, the last build pinned on disk. (None until the manifest came: the tiles built in
        a run's first minutes had other keys than the same tiles built later, in the revisit cache: a warm drive
        rebuilt and stored again ~1.5 GB of far tiles, 2026-11-04.)"""
        if self.built is not None or not ON:
            return self.built
        got = getattr(self, "_pinned", False)
        if got is False:
            got = None
            pin = os.path.join(self.dir, "manifest.json") if self.dir else None
            if pin and os.path.exists(pin):
                try:
                    with open(pin, encoding="utf-8") as fh:
                        got = json.load(fh).get("built")
                except (OSError, ValueError):
                    got = None
            self._pinned = got
        return self.built if self.built is not None else got

    def version(self):
        """The build in use ("built" of its manifest), or None (none reachable yet).

        The manifest pinned on disk is used whatever its age: a stale one (MANIFEST_TTL_S) is refreshed in the
        background, one fetch at a time, and kept if that fails. Without one (a first run) one thread fetches it and
        the others wait for its answer. (2026-11-09, the night of 1.9.0: GitHub answered 504s for hours; every road
        worker process asked each dataset's manifest for itself, 2 tries x 20 s, two at a time, and dropped the data it
        had on disk once a day's manifest was due: world gen hung at 15 % for players; a test with 10 s 504s cost a
        first tile 17 s.)"""
        if not ON or self.fetcher is None:
            return None
        now = time.time()
        with self.lock:
            if self.built is not None and now - self.checked < MANIFEST_TTL_S:
                return self.built
        pin = os.path.join(self.dir, "manifest.json") if self.dir else None
        pinned, age, pdata = None, None, None
        if pin and os.path.exists(pin):
            try:
                age = now - os.path.getmtime(pin)
                with open(pin, encoding="utf-8") as fh:
                    j = json.load(fh)
                pinned, pdata = j.get("built"), data_url(j)
            except (OSError, ValueError, AttributeError):
                pinned = None
        if pinned is not None:
            refresh = False
            with self.lock:
                if pinned != self.built and (self.built is None or age < MANIFEST_TTL_S):
                    self.shards.clear()
                    self.built = pinned
                if pinned == self.built:
                    self.data = pdata
                if age < MANIFEST_TTL_S:
                    self.checked = now - age
                elif self.fetching is None and now - self.down_at >= RETRY_S:
                    self.fetching = threading.Event()
                    self.checked = now          # (asked once; the next check after MANIFEST_TTL_S or a failure)
                    refresh = True
                built = self.built
            if refresh:
                threading.Thread(target=self._refresh, name=f"{self.name}-manifest", daemon=True).start()
            return built
        with self.lock:
            if self.built is None and now - self.down_at < RETRY_S:
                return None
            ev = self.fetching
            mine = ev is None
            if mine:
                ev = self.fetching = threading.Event()
        if not mine:
            ev.wait(FETCH_WAIT_S)
            with self.lock:
                return self.built
        return self._refresh(now)

    def _refresh(self, now=None):
        """Fetch the manifest (this thread holds self.fetching), pin it, and take its build; the build in use when it
        cannot be had."""
        now = time.time() if now is None else now
        pin = os.path.join(self.dir, "manifest.json") if self.dir else None
        try:
            try:
                body = self.fetcher.get(f"{self.base}/manifest.json", kind="demand").body
                j = json.loads(body.decode("utf-8"))
                built, data = j.get("built"), data_url(j)
                if pin:
                    os.makedirs(self.dir, exist_ok=True)
                    with open(pin + ".part", "wb") as fh:
                        fh.write(body)
                    os.replace(pin + ".part", pin)
            except Exception as e:      # (offline, not published yet: without it, or with the build pinned before)
                log.debug("%s manifest: %s: %s", self.name, type(e).__name__, e)
                with self.lock:
                    self.down_at = now
                    return self.built   # (a build learnt before stays in use)
            with self.lock:
                if built != self.built:
                    self.shards.clear()
                self.built, self.data, self.checked = built, data, now
            return built
        finally:
            with self.lock:
                ev, self.fetching = self.fetching, None
            if ev is not None:
                ev.set()

    def get(self, rel, kind="demand"):
        """The bytes of the dataset's file rel (path in its folder) for the build in use: from the manifest's "data"
        host, else (or that failing) Pages (fetch_data)."""
        return fetch_data(self.fetcher, self.data, self.base, rel, kind)

    def clear_memory(self):
        """Drop the decoded shards kept in memory (budget.maybe_shed through MapData.clear_memory; ~20 MB a zoom-7
        shard, never let go otherwise); they load again from disk when asked."""
        with self.lock:
            self.shards.clear()

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
        side = path[:-len(".json.gz")] + IDX_SUFFIX if path else None
        if side and os.path.exists(side):
            got = _load_idx(side)
            if got is not None:
                if isinstance(got.get("rules"), dict):
                    got["rules"] = Rules(got["rules"])
                with self.lock:
                    self.shards[(x, y)] = got
                return got
        if path and os.path.exists(path):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    got = json.load(fh)
            except (OSError, ValueError):
                got = None
        if got is None:
            if time.time() - self.shard_down.get((x, y), 0.0) < SHARD_RETRY_S:
                return None             # (failed a moment ago: not every tile waits for it again)
            try:
                body = self.get(f"{x}/{y}.json.gz")
                got = json.loads(gzip.decompress(body).decode("utf-8"))
            except netfetch.NotFound:
                body, got = None, {}
            except Exception as e:
                log.debug("%s %s/%s: %s: %s", self.name, x, y, type(e).__name__, e)
                self.shard_down[(x, y)] = time.time()
                return None
            if path:
                os.makedirs(d, exist_ok=True)
                with open(path + ".part", "wb") as fh:
                    fh.write(body if body is not None else gzip.compress(b"{}"))
                os.replace(path + ".part", path)
        if isinstance(got, dict) and isinstance(got.get("ways"), dict):
            got["ways"] = Ways.of(got["ways"])
            if side and isinstance(got["ways"], Ways):
                _save_idx(side, got)        # (its "rules" still the shard's JSON: made Rules below, and on reading it)
        if isinstance(got, dict) and isinstance(got.get("rules"), dict):
            got["rules"] = Rules(got["rules"])        # (the lane shards' road rules)
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
        found = _tags_of(ways, [way_id(str(rec[i])) or "" for i in rail]) if rec is not None else []
        for i, t in zip(rail, found):
            if t:
                for c in COLUMNS:
                    cols[c][i] = t.get(c)
        for c in COLUMNS:
            feats.attrs["osm_" + c] = cols[c]
        lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
        pad = 0.002
        feats.__dict__["_rail_nodes"] = [(lo, la, t) for _id, lo, la, t in sh.get("nodes", ())
                                         if lon0 - pad <= lo <= lon1 + pad and lat0 - pad <= la <= lat1 + pad]


LANES_ON = os.environ.get("BNE_LANETAGS", "on") != "off"
# OpenStreetMap's lane-level road tags (tools/build_lane_tags.py), attrs osm_<key with ':' as '_'>: the road markings'
# lane-by-lane data (markings.py). bus:lanes:forward, bus:lanes:backward and psv:lanes join only if the shard has them.
LANE_COLUMNS = ("lanes", "turn:lanes", "turn:lanes:forward", "turn:lanes:backward", "turn:lanes:both_ways", "change:lanes",
                "change:lanes:forward", "change:lanes:backward", "placement", "placement:forward", "placement:backward",
                "lanes:forward", "lanes:backward", "lanes:both_ways", "overtaking", "overtaking:forward",
                "overtaking:backward", "divider", "lane_markings", "bus:lanes", "bus:lanes:forward",
                "bus:lanes:backward", "psv:lanes", "busway", "hov:lanes",
                "junction")     # (roundabout rings: roads._rings, attrs osm_junction)


# Germany's road rules (tools/build_lane_tags.py: the lane shards' "rules", never in their "ways"), attrs lane_attr(k):
# kept apart from LANE_COLUMNS, which the markings read as lane data (roads.lane_tags, LANE_PARTS)
RULE_COLUMNS = ("priority_road", "zone:maxspeed")


# per road feature of several ways: [(start, end, way id, its tags)], else None. The tags are the shard's own (Ways'
# shared tag sets, {} for a way without): read them, do not change them (roads.lane_tag_parts' readers: roads.way_lines,
# roads.extras / markings / franceroads.paint (copies before changing), motesfri; no copy per part, 2026-11-08)
LANE_PARTS = "osm_lane_parts"


# A shard's decoded form beside it (<x>_<y>.idx.npz): the Ways' arrays, its distinct tag sets and the rest of the shard
# as compact JSON. Every server process read the big shards again (gzip, JSON, the way index: ~0.5 s each, rail and
# lanes 1-2 s per process and place, 2026-11-07); this ~0.05 s. Same build folder, so as fresh as the shard.
IDX_SUFFIX = ".idx.npz"


def _save_idx(path, shard):
    w = shard["ways"]
    rest = {k: v for k, v in shard.items() if k != "ways"}
    try:
        with open(path + ".part", "wb") as fh:
            np.savez(fh, ids=w.ids, idx=w.idx,
                     tags=np.frombuffer(json.dumps(w.tags, separators=(",", ":")).encode("utf-8"), np.uint8),
                     rest=np.frombuffer(json.dumps(rest, separators=(",", ":")).encode("utf-8"), np.uint8))
        os.replace(path + ".part", path)
    except OSError as e:
        log.debug("%s: %s", path, e)


def _load_idx(path):
    """The shard of a _save_idx file, or None (unreadable: the shard itself is read)."""
    try:
        with np.load(path, allow_pickle=False) as z:
            w = Ways.__new__(Ways)
            w.ids, w.idx = z["ids"].astype(np.int64, copy=False), z["idx"].astype(np.int32, copy=False)
            w.tags = json.loads(z["tags"].tobytes().decode("utf-8"))
            got = json.loads(z["rest"].tobytes().decode("utf-8"))
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile) as e:    # (EOFError: empty, BadZipFile: cut short)
        log.debug("%s: %s", path, e)
        return None
    if not isinstance(got, dict) or not isinstance(w.tags, list) or len(w.ids) != len(w.idx):
        return None
    got["ways"] = w
    return got


def _tags_of(ways, keys):
    """[tags or None] of each way id key (a Ways or a plain dict)."""
    if isinstance(ways, Ways):
        return ways.get_many(keys)
    return [ways.get(k) if k else None for k in keys]


def lane_attr(key):
    if key == "lanes":
        return "osm_lane_total"     # preserve the independently downloaded Layercake count
    return "osm_" + key.replace(":", "_")


class LaneTags(Shards):
    def __init__(self, cache_dir, fetcher):
        super().__init__(cache_dir, fetcher, "lanes")

    def join(self, feats, key):
        """Add to cell key's road segments, in place, joined by way id (None where absent):
        - OSM's lane tags from the shard's "ways": attrs lane_attr(k) for k in LANE_COLUMNS, and LANE_PARTS;
        - Germany's road rules from its "rules" (tools/build_lane_tags.py): attrs osm_priority_road (designated /
          end / yes_unposted) and osm_zone_maxspeed (DE:30 / DE:20 / DE:10), lane_attr(k) for k in RULE_COLUMNS;
        - its German sign nodes in the cell (junction signs and the mapped Ortstafeln), for sign_nodes(feats).
        Nothing when the shard cannot be had; a shard without "rules" or "nodes" (an older build) joins none of them.
        The way ids are parsed and found in the shard natively (fast.way_tag_rows, fast.lane_parts), each distinct tag
        set's columns made once and the parts share the shard's tag sets (LANE_PARTS): a regex, a numpy search and a
        tag copy per road and per way part and a rule-table slice per road were ~14 % of a dense Manhattan road tile
        (2026-11-08). A shard whose ids are not plain numbers (Ways.of left it a dict) is read by way_id and the dict."""
        sub = feats.attrs.get("subtype")
        if sub is None or not len(feats):
            return
        sub = np.asarray(sub)
        road = np.flatnonzero((sub if sub.dtype.kind == "U" else sub.astype(str)) == "road")
        if not len(road):
            return
        sh = self.of_cell(key)
        if sh is None:
            return
        ways = sh.get("ways", {})
        rec = feats.attrs.get("src_record")
        n = len(feats)
        cols = {c: np.empty(n, object) for c in LANE_COLUMNS}     # (all None)
        if rec is not None and ways:
            if isinstance(ways, Ways):          # (each distinct tag set once: the rows of its roads)
                recs = rec[road] if isinstance(rec, np.ndarray) else [str(rec[i]) for i in road]
                k = ways.rows_of(fast.ucs4(recs), recs)
                hit = np.flatnonzero(k >= 0)
                u, inv = np.unique(k[hit], return_inverse=True)
                ts = [ways.tags[j] for j in u.tolist()]
                rows = road[hit]
            else:
                ts, rows, inv, seen = [], [], [], {}
                for i, t in zip(road.tolist(), _tags_of(ways, [way_id(str(rec[i])) or "" for i in road])):
                    if t is not None:
                        j = seen.get(id(t))
                        if j is None:
                            j = seen[id(t)] = len(ts)
                            ts.append(t)
                        rows.append(i)
                        inv.append(j)
                rows, inv = np.asarray(rows, np.int64), np.asarray(inv, np.int64)
            keep = np.array([bool(t) for t in ts], bool)
            if keep.any():
                m = keep[inv]
                rows, inv = rows[m], inv[m]
                for c in LANE_COLUMNS:
                    vals = [t[c] if c in t else None for t in ts]
                    if all(v is None for v in vals):
                        continue
                    tab = np.empty(len(ts), object)
                    for j, v in enumerate(vals):
                        tab[j] = v
                    cols[c][rows] = tab[inv]
        for c in LANE_COLUMNS:
            feats.attrs[lane_attr(c)] = cols[c]
        # (the road rules of the way covering most of a feature, as the columns above: kept apart from them and from
        # LANE_PARTS, which the markings read as lane data)
        rules = sh.get("rules")
        if isinstance(rules, dict):
            rules = Rules(rules)            # (a shard not read through Shards.shard)
        ids = [way_id(str(rec[i])) for i in road] if rec is not None and isinstance(rules, Rules) else []
        for c in RULE_COLUMNS:
            col = np.full(len(feats), None, object)
            for i, v in zip(road, rules.get_many(c, ids) if ids else ()):
                col[i] = v
            feats.attrs[lane_attr(c)] = col
        # (the columns above hold the way covering most of a feature; a feature of several ways keeps each one's tags)
        table = feats.rules.get("src_ways")
        parts = np.empty(n, object)
        if table is not None and len(table):
            f = np.asarray(table.feature, np.int64)
            is_road = np.zeros(n, bool)
            is_road[road] = True
            sel = np.flatnonzero((f >= 0) & (f < n))
            sel = sel[is_road[f[sel]]]
            if len(sel):
                sel = sel[np.argsort(f[sel], kind="stable")]       # (by feature, each one's rows ascending)
                vals = table.value[sel]
                if isinstance(ways, Ways):

                    def ask(k):             # (the records the kernel leaves to way_id)
                        w = way_id(vals[k:k + 1].tolist()[0])
                        r = ways.row(w) if w is not None else -1
                        return w, ways.tags[r] if r >= 0 else {}
                    at, lists = fast.lane_parts(fast.ucs4(vals), f[sel], table.start[sel].astype(np.float64),
                                                table.end[sel].astype(np.float64), ways.ids, ways.idx, ways.tags, ask)
                    for i, got in zip(at.tolist(), lists):
                        parts[i] = got
                else:
                    cur, at = None, -1
                    for i, r, a, b in zip(f[sel].tolist(), vals.tolist(), table.start[sel].tolist(),
                                          table.end[sel].tolist()):
                        if i != at:
                            at = i
                            cur = parts[i] = []
                        w = way_id(r)
                        cur.append((a, b, w, ways.get(w) or {}))
        feats.attrs[LANE_PARTS] = parts
        # (the shard's German sign nodes in the cell: [(lon, lat, tags)], as RailTags' _rail_nodes; none without a
        # cell to bound them)
        nodes, found = sh.get("nodes") or (), []
        if nodes and key is not None:
            lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
            pad = 0.002
            found = [(lo, la, t) for _id, lo, la, t in nodes
                     if lon0 - pad <= lo <= lon1 + pad and lat0 - pad <= la <= lat1 + pad]
        feats.__dict__["_sign_nodes"] = found
        import roadlanes
        roadlanes.invalidate(feats)


def sign_nodes(feats):
    """The German sign nodes LaneTags.join found in feats' cell: [(lon, lat, tags)], [] when none."""
    return feats.__dict__.get("_sign_nodes", [])


SIGNS_ON = os.environ.get("BNE_SIGNTAGS", "on") != "off"
# a local shard set (tools/build_sign_tags.py's output folder) read instead of the Pages one: tests and probes
SIGNS_DIR = os.environ.get("BNE_SIGNS_DIR") or None
SIGN_KINDS = signshards.KINDS
class SignTags(Shards):
    """Highway sign sources from OpenStreetMap (tools/build_sign_tags.py, dataset "signs"): exits (a motorway
    junction's ramp with its posted destinations and the approach before it), overhead destinations (directed
    mainline destination tags) and Interstate mile references (milestones). Each shard: {"objects": [{"id", "k":
    exit | through | mile, "lon", "lat", "d": {...}}]}, a record in the shard of its point. query() gives them in the
    form the sign modules take (exitsigns / throughsigns / milemarkers: {"id", "lon", "lat", "tags", "ways",
    "<kind>": d}). Not published yet, offline or unreachable: nothing (Shards: the manifest asked again after
    RETRY_S, netfetch's miss cache)."""

    def __init__(self, cache_dir, fetcher, local=None):
        super().__init__(cache_dir, fetcher, "signs")
        self.local = local if local is not None else SIGNS_DIR
        self.index = {}             # (x, y) -> (build, records, lon array, lat array)

    def version(self):
        if self.local is None:
            return super().version()
        if self.built is None:
            try:
                with open(os.path.join(self.local, "manifest.json"), encoding="utf-8") as fh:
                    self.built = "local:" + str(json.load(fh).get("built"))
            except (OSError, ValueError):
                return None
        return self.built

    def shard(self, x, y):
        if self.local is None:
            return super().shard(x, y)
        if self.version() is None:
            return None
        with self.lock:
            got = self.shards.get((x, y))
        if got is None:
            try:
                with gzip.open(os.path.join(self.local, str(x), f"{y}.json.gz"), "rt", encoding="utf-8") as fh:
                    got = json.load(fh)
            except FileNotFoundError:
                got = {}
            except (OSError, ValueError) as e:
                log.debug("signs %s/%s: %s", x, y, e)
                return None
            with self.lock:
                self.shards[(x, y)] = got
        return got

    def _indexed(self, x, y):
        built = self.version()
        if built is None:
            return None
        with self.lock:
            got = self.index.get((x, y))
            if got is not None and got[0] == built:
                return got[1:]
        sh = self.shard(x, y)
        if sh is None:
            return None
        recs = [r for r in sh.get("objects", ()) if r.get("k") in SIGN_KINDS]
        lon = np.array([float(r["lon"]) for r in recs], np.float64)
        lat = np.array([float(r["lat"]) for r in recs], np.float64)
        with self.lock:
            self.index[(x, y)] = (built, recs, lon, lat)
        return recs, lon, lat

    def query(self, lon0, lat0, lon1, lat1, kinds=None):
        """The records whose point is in the box, decoded, sorted by id ([] when there are none or no data)."""
        if not ON or not SIGNS_ON or (self.fetcher is None and self.local is None):
            return []
        xa, ya = signshards.tile(lat1, lon0)
        xb, yb = signshards.tile(lat0, lon1)
        out = {}
        for x in range(xa, xb + 1):
            for y in range(ya, yb + 1):
                got = self._indexed(x, y)
                if got is None:
                    continue
                recs, lon, lat = got
                for i in np.flatnonzero((lon >= lon0) & (lon <= lon1) & (lat >= lat0) & (lat <= lat1)):
                    r = recs[int(i)]
                    key = SIGN_KINDS[r["k"]]
                    if kinds is not None and key not in kinds:
                        continue
                    d = dict(r.get("d") or {})
                    for p in signshards.PATHS:
                        if p in d:
                            d[p] = signshards.decode_path(d[p], r["lon"], r["lat"])
                    out[int(r["id"])] = {"id": int(r["id"]), "lon": float(r["lon"]), "lat": float(r["lat"]),
                                         "tags": {}, "ways": [], key: d}
        return [out[k] for k in sorted(out)]


# The weekly station shards (tools/build_station_tags.py, dataset "stations"): every OpenStreetMap fuel and charging
# station with its capacity, and every charge point (Bauer33333 on #716, 2026-11-04: "Almost 3/4 of
# `amenity=charging_station` on OSM got a `capacity=*` tag, same for `man_made=charge_point`"). The station finder lists
# them where no map data is downloaded (stationindex.py); the EV chargers take their count and spots (evchargers.py).
STATIONS_ON = os.environ.get("BNE_STATIONTAGS", "on") != "off"
STATIONS_DIR = os.environ.get("BNE_STATIONS_DIR") or None       # a local build (tests, before the data is published)


class StationTags(Shards):
    """Shards {"s": [[id, kind, lon, lat, name, brand, cap], ...], "p": [[lon, lat], ...]} (FORMAT 1), each record in
    the shard of its point. stations_in / points_in give a lat/lon box's; None when off, not published yet, offline or
    unreachable (the callers go on without them, as before the dataset existed)."""

    def __init__(self, cache_dir, fetcher, local=None):
        super().__init__(cache_dir, fetcher, "stations")
        self.local = local if local is not None else STATIONS_DIR
        self.by_cell = {}           # id(shard) -> {z14 cell: (records, points)}: a box reads only its own cells

    def version(self):
        if self.local is None:
            return super().version()
        if self.built is None:
            try:
                with open(os.path.join(self.local, "manifest.json"), encoding="utf-8") as fh:
                    self.built = "local:" + str(json.load(fh).get("built"))
            except (OSError, ValueError):
                return None
        return self.built

    def shard(self, x, y):
        if self.local is None:
            return super().shard(x, y)
        if self.version() is None:
            return None
        with self.lock:
            got = self.shards.get((x, y))
        if got is None:
            try:
                with gzip.open(os.path.join(self.local, str(x), f"{y}.json.gz"), "rt", encoding="utf-8") as fh:
                    got = json.load(fh)
            except FileNotFoundError:
                got = {}
            except (OSError, ValueError) as e:
                log.debug("stations %s/%s: %s", x, y, e)
                return None
            with self.lock:
                self.shards[(x, y)] = got
        return got

    def clear_memory(self):
        """The shards and their cell indexes (each index holds its shard: Codex on #1111, 2026-11-10)."""
        with self.lock:
            self.shards.clear()
            self.by_cell.clear()

    def _shards(self, lat0, lon0, lat1, lon1):
        """The box's shards, or None when one of them cannot be had."""
        import mapcells
        if not STATIONS_ON:
            return None
        out = []
        for key in mapcells.cells_for_bbox(lat0, lon0, lat1, lon1, z=SHARD_ZOOM):
            sh = self.shard(key.x, key.y)
            if sh is None:
                return None
            out.append(sh)
        return out

    def _cells(self, sh):
        """The shard's records and points by map cell (z14), made once per shard (a city's 25 km map view asks ~800
        cells of a 15,000-station shard: 1.2 s filtering the whole shard for each, instant by cell)."""
        import mapcells
        got = self.by_cell.get(id(sh))
        if got is None or got[0] is not sh:
            cells = {}
            for r in sh.get("s") or ():
                cells.setdefault(mapcells.cell_of(r[3], r[2]), ([], []))[0].append(r)
            for p in sh.get("p") or ():
                cells.setdefault(mapcells.cell_of(p[1], p[0]), ([], []))[1].append(p)
            got = (sh, cells)
            with self.lock:
                self.by_cell[id(sh)] = got
        return got[1]

    def _in_box(self, lat0, lon0, lat1, lon1):
        """[(records, points)] of the box's map cells, or None."""
        import mapcells
        shards = self._shards(lat0, lon0, lat1, lon1)
        if shards is None:
            return None
        keys = mapcells.cells_for_bbox(lat0, lon0, lat1, lon1)
        return [c[k] for c in (self._cells(sh) for sh in shards) for k in keys if k in c]

    def stations_in(self, lat0, lon0, lat1, lon1):
        """[{"id", "kind" ("fuel" | "ev"), "lat", "lon", "name", "brand", "cap" (int or None)}] in the box, or None."""
        got = self._in_box(lat0, lon0, lat1, lon1)
        if got is None:
            return None
        return [{"id": r[0], "kind": r[1], "lat": r[3], "lon": r[2], "name": r[4], "brand": r[5], "cap": r[6]}
                for recs, _ in got for r in recs if lat0 <= r[3] <= lat1 and lon0 <= r[2] <= lon1]

    def points_in(self, lat0, lon0, lat1, lon1):
        """[(lat, lon)] of the charge points in the box, or None."""
        got = self._in_box(lat0, lon0, lat1, lon1)
        if got is None:
            return None
        return [(p[1], p[0]) for _, pts in got for p in pts if lat0 <= p[1] <= lat1 and lon0 <= p[0] <= lon1]
