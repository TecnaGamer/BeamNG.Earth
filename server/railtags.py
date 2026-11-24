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
import pagesdata
import signshards
from pagesdata import FETCH_WAIT_S, MANIFEST_TTL_S, PAGES, RETRY_S, data_url, fetch_data   # (re-exported: nycmassing, nyctanks)

log = logging.getLogger("earthd.railtags")

ON = os.environ.get("BNE_RAILTAGS", "on") != "off"
SHARD_ZOOM = 7
SHARD_RETRY_S = 600             # a shard that could not be had: asked again after this long (not by every tile meanwhile)
COLUMNS = ("railway", "gauge", "electrified", "service", "usage", "highway")    # (highway: a rail trail, railways.drawn)
# Tags read only with BNE_RAIL_LIFECYCLE (railways.heritage: a heritage line keeps its level crossings; road revamp
# R1h): joined (attrs osm_<tag>) and part of the shared decisions' shard slices (decisions._shard_slice) only then, so
# with the switch off a shard carrying them changes neither the cached road layers nor the decisions' digests.
LIFECYCLE_ON = os.environ.get("BNE_RAIL_LIFECYCLE", "off") == "on"     # (railways.RAIL_LIFECYCLE's switch)
LIFECYCLE_TAGS = ("railway:preserved",)


def rail_columns():
    """The OSM tags RailTags.join adds as attrs (osm_<tag>): COLUMNS, and LIFECYCLE_TAGS with BNE_RAIL_LIFECYCLE."""
    return COLUMNS + LIFECYCLE_TAGS if LIFECYCLE_ON else COLUMNS


def slice_tags(t):
    """A way's tags t as the shared decisions' slices read them (decisions._shard_slice): without LIFECYCLE_TAGS
    unless BNE_RAIL_LIFECYCLE is on."""
    if LIFECYCLE_ON or not isinstance(t, dict) or not any(k in t for k in LIFECYCLE_TAGS):
        return t
    return {k: v for k, v in t.items() if k not in LIFECYCLE_TAGS}


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


PIN_RECHECK_S = 1.0         # s between looks for a manifest another process may have pinned meanwhile (Shards.known)


class Shards:
    """A dataset of BeamNG.Earth-data's GitHub Pages (name: its folder, e.g. "rail", "fra"): z7 shards
    <PAGES>/<name>/<x>/<y>.json.gz and its manifest, each shard fetched once and kept on disk per build. The manifest
    stays on Pages; the shards come from its "data" folder when it names one, else Pages while Pages serves the run's
    build (pagesdata.Manifest: a shard is only ever kept under the build it is of)."""

    def __init__(self, cache_dir, fetcher, name):
        self.name = name
        self.fetcher = fetcher
        self.lock = threading.Lock()        # self.shards
        self.manifest = pagesdata.Manifest(cache_dir, fetcher, name, base=f"{PAGES}/{name}", retry_s=RETRY_S,
                                           on_change=self._build_changed)
        self.dir = self.manifest.dir
        self.shards = {}            # (x, y) -> dict
        self.shard_down = {}        # (x, y) -> when it could not be had (SHARD_RETRY_S)

    @property
    def built(self):
        """The manifest's build in use (the shards' cache keys; the tests set it to stand in for a run that asked)."""
        return self.manifest.built

    @built.setter
    def built(self, value):
        self.manifest.built = value

    @property
    def data(self):
        """The build's "data" host (pagesdata.data_url), None: the files beside the manifest on Pages."""
        return self.manifest.data

    @data.setter
    def data(self, value):
        self.manifest.data = value

    @property
    def base(self):
        """The dataset's folder (the manifest's and the shards'; osmtagshards sets another: a staged build)."""
        return self.manifest.base

    @base.setter
    def base(self, value):
        self.manifest.base = value

    @property
    def missing(self):
        """The last manifest fetch was a 404 (not published), not a failure (osmyears.py)."""
        return self.manifest.missing

    @property
    def fetching(self):
        """The Event of a manifest fetch in progress, while the other callers wait for its answer."""
        return self.manifest.fetching

    def _build_changed(self):
        """Another build is in use (called under the Manifest's lock): the shards decoded from the old one are not
        it."""
        with self.lock:
            self.shards.clear()

    # (architecture v2's CIP cells, cip.py: the build's slice of this dataset per cell, its weekly build pinned)
    CIP_SECTION = None          # the cell section of this dataset's slices ("t:rail", "t:lanes"); None: not in CIP
    CIP_DATA = None             # its name in a CIP build's data builds (cip.WEEKLY: "rails", "lanes")

    def cip_pin(self):
        """(CIP reader, the dataset build the CIP build pinned) when CIP is on and its build slices this dataset, else
        (None, None). The pinned build is the one in use, for every cell (its cache keys: versions())."""
        if self.CIP_SECTION is None:
            return None, None
        import cip
        r = cip.any_reader()
        if r is None:
            return None, None
        pin = r.data_builds().get(self.CIP_DATA)
        return (r, pin) if pin else (None, None)

    def known(self):
        """The build in use as far as known without asking the network (versions(): every tile's cache key): the
        run's (pagesdata.Manifest's session), decided from the pin on disk when none is yet. (None until the manifest came: the tiles built in
        a run's first minutes had other keys than the same tiles built later, in the revisit cache: a warm drive
        rebuilt and stored again ~1.5 GB of far tiles, 2026-11-04.)"""
        _, pin = self.cip_pin()
        if pin:
            return pin
        return self.manifest.built if not ON else self.manifest.known(PIN_RECHECK_S)

    def version(self):
        """The build in use ("built" of its manifest), or None (no data this run). The run's, decided once for every
        process of it (pagesdata.Manifest.current: see pagesdata). CIP on: the build its cells' slices are of."""
        _, pin = self.cip_pin()
        if pin:
            return pin
        if not ON or self.fetcher is None:
            return None
        return self.manifest.build()

    def get(self, rel, kind="demand"):
        """The bytes of the dataset's file rel (path in its folder) for the build in use: from the manifest's "data"
        host, else (or that failing) Pages while Pages serves the same build (pagesdata.Manifest.fetch: else
        pagesdata.OtherBuild, not had)."""
        return self.manifest.fetch(rel, kind)

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
                tmp = _part(path)
                with open(tmp, "wb") as fh:
                    fh.write(body if body is not None else gzip.compress(b"{}"))
                os.replace(tmp, path)
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
        """The shard holding map cell key. CIP on: the cell's slice of it (what a join of the cell reads: its ways'
        tags, its rules, the nodes in the joins' pad, in the shard's order), or, the cell not in the CIP build, the
        shard when the manifest's build is the pinned one (else None: unreachable, joined again later)."""
        r, pin = self.cip_pin()
        if pin:
            got = self._cip_slice(r, key)
            if got is not None:
                return got
            if self.manifest.build() != pin:
                return None
        shift = key.z - SHARD_ZOOM
        return self.shard(key.x >> shift, key.y >> shift)

    def _cip_slice(self, r, key):
        import cip
        k = ("cip", str(key))
        with self.lock:
            got = self.shards.get(k)
        if got is not None:
            return got
        sl = cip.side_json(r, self.CIP_SECTION, key)
        if sl is None:
            return None
        got = shard_of_slice(sl)
        with self.lock:
            self.shards[k] = got
            self.shards[("cipcell", key.z, key.x, key.y)] = True
        return got

    def cip_read(self, key):
        """Was cell key's CIP slice read (mapdata._tags_read's question for the CIP cells)?"""
        with self.lock:
            return ("cipcell", key.z, key.x, key.y) in self.shards


PAD = 0.002                 # degrees: the joins' nodes round a cell


def cell_slice(sh, key, ids):
    """The slice of shard sh a join of cell key reads (cip.py's t: sections; shard_of_slice reads it back): the ways
    of ids (the cell's roads' way ids, decisions.ids_of) with their tags, the road rules of those, the nodes within the
    joins' pad of the cell, each in the shard's own order. JSON-ready."""
    ways = sh.get("ways") or {}
    found = _tags_of(ways, list(ids))
    out = {"kind": "Ways" if isinstance(ways, Ways) else "dict",
           "ways": [[i, t] for i, t in zip(ids, found) if t is not None]}
    if isinstance(ways, Ways):          # (in the shard's order: ids ascending)
        out["ways"].sort(key=lambda w: int(w[0]))
    rs = sh.get("rules")
    if isinstance(rs, dict):
        rs = Rules(rs)
    if isinstance(rs, Rules):
        want = np.array(sorted({int(i) for i in ids if str(i).isdigit()}), np.int64)
        rules = {}
        for k, (at, idx, values) in rs.keys.items():
            m = np.isin(at, want)
            by = {}
            for v_i, wid in zip(idx[m].tolist(), at[m].tolist()):
                by.setdefault(v_i, []).append(wid)
            rules[k] = {values[v_i]: np.diff(np.array(by[v_i], np.int64), prepend=0).tolist()
                        for v_i in sorted(by)}
        out["rules"] = rules
    xd = sh.get("xsec")
    if isinstance(xd, dict):            # (BNE_ROADSYS_XSEC: the cross-section rows of those ways, the table's form)
        xs = xsec_table(sh, None)
        want = sorted({int(i) for i in ids if str(i).isdigit()})
        got = xs.get_many(want) if xs is not None else []
        rows = [(w, t) for w, t in zip(want, got) if t is not None]
        keys = sorted({k for _w, t in rows for k in t})
        sets, idx, at = [], [], {}
        for _w, t in rows:
            row = tuple(t.get(k) for k in keys)
            if row not in at:
                at[row] = len(sets)
                sets.append(list(row))
            idx.append(at[row])
        out["xsec"] = {"keys": keys, "sets": sets, "ids": np.diff(np.array([w for w, _t in rows], np.int64), prepend=0).tolist(),
                       "set": idx}
    lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
    out["nodes"] = [list(nd) for nd in sh.get("nodes") or ()
                    if lon0 - PAD <= nd[1] <= lon1 + PAD and lat0 - PAD <= nd[2] <= lat1 + PAD]
    return out


def shard_of_slice(sl):
    """A shard dict from a cell_slice (its ways a Ways when the shard's were, its rules Rules)."""
    ways = {str(i): t for i, t in sl.get("ways") or ()}
    got = {"ways": Ways(ways) if sl.get("kind") == "Ways" else ways,
           "nodes": list(sl.get("nodes") or ())}
    if "rules" in sl:
        got["rules"] = Rules(sl["rules"])
    if isinstance(sl.get("xsec"), dict):
        got["xsec"] = sl["xsec"]
    return got


class RailTags(Shards):
    CIP_SECTION, CIP_DATA = "t:rail", "rails"

    def __init__(self, cache_dir, fetcher):
        super().__init__(cache_dir, fetcher, "rail")

    def join(self, feats, key):
        """Add the OSM tags (attrs osm_railway, osm_gauge, osm_electrified, osm_service, osm_usage) to cell key's rail
        segments, and its crossing / mast nodes (feats._rail_nodes: [(lon, lat, tags)]), in place. Nothing when the
        shard cannot be had: False then, and a revisit fault (the caller joins again later)."""
        sub = feats.attrs.get("subtype")
        if sub is None or not len(feats):
            return
        rail = np.flatnonzero(np.asarray(sub).astype(str) == "rail")
        if not len(rail):
            return
        sh = self.of_cell(key)
        if sh is None:
            if self.version() is None:      # (not published, or no build known yet: nothing to join, no fault)
                return None
            import revisit                  # (a known build's shard unreachable: what is built on the untagged
            revisit.fault()                 # layer is not kept, and the caller joins again next time)
            return False
        ways = sh.get("ways", {})
        rec = feats.attrs.get("src_record")
        columns = rail_columns()
        cols = {c: np.full(len(feats), None, object) for c in columns}
        found = _tags_of(ways, [way_id(str(rec[i])) or "" for i in rail]) if rec is not None else []
        for i, t in zip(rail, found):
            if t:
                for c in columns:
                    cols[c][i] = t.get(c)
        for c in columns:
            feats.attrs["osm_" + c] = cols[c]
        lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
        pad = 0.002
        feats.__dict__["_rail_nodes"] = [(lo, la, t) for _id, lo, la, t in sh.get("nodes", ())
                                         if lon0 - pad <= lo <= lon1 + pad and lat0 - pad <= la <= lat1 + pad]


LANES_ON = os.environ.get("BNE_LANETAGS", "on") != "off"
# (BNE_ROADSYS_XSEC=on: the road system's cross-sections, native/roadsys xsec.rs) the lane shards' "xsec" table
# (tools/build_lane_tags.py --xsec: the street tags of every drivable highway way with any of XSEC_KEYS, by way id)
# joined to the roads as attrs XSEC_ATTR, one {key: value} (or None) per road: the way covering most of it. Off: not
# read. BNE_LANES_XSEC_DIR: a local --xsec build's folder whose shards' "xsec" stand in where the shards in use have
# none (a sample build: tests and probes; its path is keyed as every BNE_ setting is).
XSEC_ON = os.environ.get("BNE_ROADSYS_XSEC", "off").strip().lower() == "on"
XSEC_DIR = os.environ.get("BNE_LANES_XSEC_DIR") or None
XSEC_ATTR = "osm_xsec"


class XsecTable:
    """A shard's "xsec" table: {"keys": [k...], "sets": [[value or None per key]...], "ids": way ids sorted and
    delta-coded, "set": the set of each}: get_many(way ids) -> [{key: value} or None]."""

    def __init__(self, doc):
        keys = [str(k) for k in doc.get("keys") or ()]
        self.sets = [{k: str(v) for k, v in zip(keys, row) if v is not None} for row in doc.get("sets") or ()]
        self.ids = np.cumsum(np.asarray(doc.get("ids") or (), np.int64))
        self.set = np.asarray(doc.get("set") or (), np.int64)
        if len(self.ids) != len(self.set) or (len(self.set) and (self.set.min() < 0 or self.set.max() >= len(self.sets))):
            self.ids, self.set = np.zeros(0, np.int64), np.zeros(0, np.int64)

    def get_many(self, ids):
        out = []
        for w in ids:
            if w is None or not len(self.ids):
                out.append(None)
                continue
            j = int(np.searchsorted(self.ids, int(w)))
            out.append(self.sets[int(self.set[j])] if j < len(self.ids) and int(self.ids[j]) == int(w) else None)
        return out


_XSEC_LOCAL = {}            # (x, y) -> XsecTable or None (BNE_LANES_XSEC_DIR's shards)


def xsec_table(sh, key):
    """The XsecTable of shard sh (the cell key's), from BNE_LANES_XSEC_DIR where sh has none, or None."""
    got = sh.get("_xsec_obj") if isinstance(sh, dict) else None
    if got is not None:
        return got
    doc = sh.get("xsec") if isinstance(sh, dict) else None
    if isinstance(doc, dict):
        got = XsecTable(doc)
        sh["_xsec_obj"] = got
        return got
    if XSEC_DIR is None or key is None:
        return None
    shift = key.z - SHARD_ZOOM
    xy = (key.x >> shift, key.y >> shift)
    if xy not in _XSEC_LOCAL:
        try:
            with gzip.open(os.path.join(XSEC_DIR, str(xy[0]), f"{xy[1]}.json.gz"), "rt", encoding="utf-8") as fh:
                d = json.load(fh).get("xsec")
            _XSEC_LOCAL[xy] = XsecTable(d) if isinstance(d, dict) else None
        except (OSError, ValueError):
            _XSEC_LOCAL[xy] = None
    return _XSEC_LOCAL[xy]
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


def _part(path):
    """A temporary name of path's own for this thread (two threads of a process, or two processes, fetching the same
    shard at once each wrote path + ".part": the second's rename found it gone, FileNotFoundError, and the cell's tags
    were not joined; seen as a shared decision item planned locally, 2026-11-13)."""
    return f"{path}.{os.getpid()}.{threading.get_ident()}.part"


def _save_idx(path, shard):
    w = shard["ways"]
    rest = {k: v for k, v in shard.items() if k != "ways"}
    try:
        tmp = _part(path)
        with open(tmp, "wb") as fh:
            np.savez(fh, ids=w.ids, idx=w.idx,
                     tags=np.frombuffer(json.dumps(w.tags, separators=(",", ":")).encode("utf-8"), np.uint8),
                     rest=np.frombuffer(json.dumps(rest, separators=(",", ":")).encode("utf-8"), np.uint8))
        os.replace(tmp, path)
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
    CIP_SECTION, CIP_DATA = "t:lanes", "lanes"

    def __init__(self, cache_dir, fetcher):
        super().__init__(cache_dir, fetcher, "lanes")

    def join(self, feats, key):
        """Add to cell key's road segments, in place, joined by way id (None where absent):
        - OSM's lane tags from the shard's "ways": attrs lane_attr(k) for k in LANE_COLUMNS, and LANE_PARTS;
        - Germany's road rules from its "rules" (tools/build_lane_tags.py): attrs osm_priority_road (designated /
          end / yes_unposted) and osm_zone_maxspeed (DE:30 / DE:20 / DE:10), lane_attr(k) for k in RULE_COLUMNS;
        - its German sign nodes in the cell (junction signs and the mapped Ortstafeln), for sign_nodes(feats).
        Nothing when the shard cannot be had (False then, and a revisit fault: the caller joins again later); a shard without "rules" or "nodes" (an older build) joins none of them.
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
            if self.version() is None:      # (not published, or no build known yet: nothing to join, no fault)
                return None
            import revisit                  # (a known build's shard unreachable: what is built on the untagged
            revisit.fault()                 # layer is not kept, and the caller joins again next time)
            return False
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
        if XSEC_ON:             # (the road system's cross-sections: the street tags of the way covering most of a road)
            col = np.full(n, None, object)
            xs = xsec_table(sh, key)
            if xs is not None and rec is not None:
                for i, t in zip(road, xs.get_many([way_id(str(rec[i])) for i in road])):
                    col[i] = t
            feats.attrs[XSEC_ATTR] = col
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
