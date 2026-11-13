"""Shared decision archives (architecture item 7, 2026-11-13): the generator's decisions (a map cell's road plans and
its cell facts, the format-keyed products that dominate planning) built once by the project's own machine for exactly
this server's version and downloaded instead of computed, after an update and on first visits to popular places.

Only the project's builder makes them (tools/build_decisions.py: the server's own planning code, run on the mini PC
after each release); nothing a player's machine made is ever served. Each block file is signed (ed25519; the public
key compiled into earthfast, decisions.rs TRUSTED) and checked whole before anything in it is used; any miss, another
version, a bad signature, a network error: the server plans the cell itself, as before.

What is shared, and its key. A version key (vkey: sha256 of vkey_doc) names everything a product's own cache key
holds besides the cell: PLANS_FORMAT / FACTS_FORMAT (plancache.plans_format: with the grade guard's tag), the plans'
frames, the build switches (revisit.switches_key: the BNE_* that change output), the map data's versions
(MapData.versions(): Overture release, map source, the optional layers' versions) WITHOUT the weekly datasets' builds
(WEEKLY: rails, lanes, stations; a weekly build made every archive unreachable). What a cell's items read of the weekly
rail and lane tags is keyed per item instead (FORMAT 2): each item's key ends in "@<dep>", the digest of the slices of
the cells round it (ring: RING_PLANS cells for plans, whose facts read their neighbours' facts; RING_FACTS for facts),
a cell's slice (slice_digest) being the rail and lane shards' tags of the OSM ways of that cell's roads layer, their
road rules, and the shards' nodes in it (what railtags' joins read). The way ids of each of those cells are in the
block (w/<z>_<x>_<y>: they follow the Overture release, which the vkey holds), so a player's server makes the same
digest from its own weekly shards without the map data: a weekly build misses only the items whose cells' tags
changed. The station shards are no part of it: no plan or fact reads them (gasstations' road tiles do).
Inside a block, items are keyed as the local caches key them, plus their digest:
    p/<signature>/<z>_<x>_<y>@<dep>                  a cell's plans (plancache's npz bytes; signature: the grading settings)
    f/<layer>/<kind>/<extra JSON>/<z>/<x>/<y>@<dep>  a cell's facts (roads._facts_store's JSON: "ranges", "specs")
    w/<z>_<x>_<y>                                    a cell's way ids (JSON), for every cell of the block's rings
A cell whose local key carries more (Hong Kong's measured heights, roadz.hk_tags; the photo's road-surface verdicts,
roadsurf.cell_tags / layer_tags) never matches an item: the builder makes none there, and the client does not ask.
A hit is copied into the local revisit cache under the client's own key (plancache._revisit_key, roads._facts_key):
read once from the archive, from the local cache after.

Layout under BASE (the project's CDN, R2 bnge-tiles behind tiles.tgforever.net; BNGE_DECISIONS_URL another base):
    d<FORMAT>/<vkey>/manifest.bngm          signed: {"format", "vkey", "doc" (vkey_doc), "blocks": [[x, y, h], ...], ...}
                                            (served no-cache: the only file that changes)
    d<FORMAT>/<vkey>/12/<x>/<y>.<h>.bngd    one z12 block (4 x 4 cells): its items, signed (decisions.rs); h: the
                                            first 16 hex of the file's sha256, so a rebuilt block is a new name (no
                                            cache purge; served immutable) and the old name stays for a grace period
Kept under <cache>/decisions/d<FORMAT>/<vkey>/ (manifest.bngm, 12/x/y.h.bngd: verified when downloaded). Region downloads
(a saved area's "dec" steps: region_block) fetch a box's blocks and pin them (pins.json: never pruned); other
versions' folders go at the server's start (prune).

Off unless the switch file says {"decisions": {"enabled": true}} (cdn.decisions_on, read at the server's start:
earthd sets BNGE_DECISIONS for its processes) or BNGE_DECISIONS=on (tests). BNGE_DECISIONS_PUBKEY (hex, comma
separated): the keys accepted instead of the compiled ones (tests and a local builder only).
"""
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from collections import OrderedDict

import fast
import netfetch

log = logging.getLogger("earthd.decisions")

NATIVE = 282
FORMAT = 2                      # the archives' layout and vkey_doc's (2: weekly datasets per item, hashed block
                                # names; the block and manifest files themselves are still decisions.rs FORMAT 1)
RING_PLANS = 2                  # cells round a cell whose weekly slices its plans' digest holds (its facts' ring + 1)
RING_FACTS = 1                  # ... its facts' (gradesep / lane sections read the neighbours' roads)
HASH_HEX = 16                   # a block file name's content hash
BLOCK_Z = 12
CELL_Z = 14
ENV = "BNGE_DECISIONS"
LANE = "dec"
SUBDIR = "decisions"
MANIFEST_TTL_S = 600.0          # a version's manifest read again after this (blocks built on demand show up)
RETRY_S = 600.0                 # a version without a manifest (404) / a block that failed: not asked again sooner
NET_RETRY_S = 60.0              # ... after a network error
FETCH_WAIT_S = 20.0             # s a process waits for another downloading the same block
MAX_BLOCK_BYTES = 96 << 20
MEM_BLOCKS = 6                  # blocks kept in memory per process
WEEKLY = ("rails", "lanes", "stations")     # versions() keys left out of the vkey (per item instead: slice_digest)
PINS = "pins.json"


def _base():
    import imagery
    return (os.environ.get("BNGE_DECISIONS_URL") or f"{imagery.CDN_BASE}/dec").strip().rstrip("/")


def enabled():
    return os.environ.get(ENV, "").strip().lower() == "on"


def pubkeys():
    """The builder keys to accept: BNGE_DECISIONS_PUBKEY's (tests, a local builder), else None (earthfast's own)."""
    v = os.environ.get("BNGE_DECISIONS_PUBKEY", "").strip()
    return [bytes.fromhex(k.strip()) for k in v.split(",") if k.strip()] if v else None


def canonical(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def vkey_doc(versions):
    """What every shared item's key holds besides the cell and its weekly slices, or None (no versions). The weekly
    datasets' builds are left out (each item's key holds the digest of what it read of them: slice_digest), so a
    server whose weekly manifests are not known yet, or are a week old, has the builder's vkey all the same."""
    if not isinstance(versions, dict):
        return None
    import plancache
    import revisit
    import roads
    return {"dec": FORMAT, "plans": plancache.plans_format(), "facts": roads.FACTS_FORMAT,
            "frames": plancache.FRAMES, "switches": revisit.switches_key(),
            "rings": [RING_PLANS, RING_FACTS],
            "versions": json.loads(json.dumps({k: v for k, v in versions.items() if k not in WEEKLY},
                                              sort_keys=True))}


_VK = {}


def vkey(versions):
    """(hex, 32 bytes) of the version key of versions (a dict, or its JSON as the cell ids hold it), or None."""
    if isinstance(versions, str):
        memo = _VK.get(versions)
        if memo is not None:
            return memo
        try:
            v = json.loads(versions)
        except ValueError:
            return None
        got = vkey(v)
        _VK[versions] = got
        return got
    doc = vkey_doc(versions)
    if doc is None:
        return None
    d = hashlib.sha256(canonical(doc)).digest()
    return d.hex()[:32], d


def plans_key(signature, cell, dep=None):
    k = f"p/{signature}/{int(cell[0])}_{int(cell[1])}_{int(cell[2])}"
    return k if dep is None else f"{k}@{dep}"


def facts_key(layer, kind, extra, cell, dep=None):
    """cell: "z/x/y" (a cell id's own form)."""
    k = f"f/{layer}/{kind}/{json.dumps(extra)}/{cell}"
    return k if dep is None else f"{k}@{dep}"


def ways_key(cell):
    return f"w/{int(cell[0])}_{int(cell[1])}_{int(cell[2])}"


def base_key(key):
    """An item key without its digest (one item per base key and block: a rebuild replaces it)."""
    return key.split("@", 1)[0]


def ring(cell, r):
    """The z14 cells within r cells of cell (z, x, y) (itself included; x wraps, rows past the poles left out)."""
    z, x, y = (int(v) for v in cell)
    n = 1 << z
    return [(z, (x + dx) % n, y + dy) for dy in range(-r, r + 1) for dx in range(-r, r + 1) if 0 <= y + dy < n]


# --- the weekly datasets' slices (FORMAT 2) ------------------------------------------------------------------------
_WAY = None


def ids_of(feats):
    """The sorted OSM way ids (strings) of a roads layer: every feature's source record and its source ways (the ids
    railtags' joins look up), or [] (no layer)."""
    global _WAY
    if feats is None or not len(feats):
        return []
    if _WAY is None:
        import railtags
        _WAY = railtags.way_id
    out = set()
    rec = feats.attrs.get("src_record")
    if rec is not None:
        out.update(_WAY(str(r)) for r in rec)
    rules = getattr(feats, "rules", None)
    table = rules.get("src_ways") if rules is not None and hasattr(rules, "get") else None
    if table is not None and len(table):
        out.update(_WAY(str(v)) for v in table.value)
    out.discard(None)
    return sorted(out, key=lambda v: (len(v), v))


def _shard_slice(sh, cell, ids, rules):
    """What a join of cell's ways (ids) reads of shard sh (railtags.RailTags.join / LaneTags.join): the tags of the
    ways in it, the road rules of those (lanes), the nodes within the join's pad of the cell."""
    import mapcells
    import railtags
    ways = sh.get("ways") or {}
    found = railtags._tags_of(ways, ids) if ids else []
    out = {"ways": [[i, t] for i, t in zip(ids, found) if t is not None]}
    if rules:
        rs = sh.get("rules")
        if isinstance(rs, dict):
            rs = railtags.Rules(rs)
        if isinstance(rs, railtags.Rules) and ids:
            out["rules"] = {c: [[i, v] for i, v in zip(ids, rs.get_many(c, ids)) if v is not None]
                            for c in railtags.RULE_COLUMNS}
    lon0, lat0, lon1, lat1 = (float(v) for v in mapcells.CellKey(*cell).bounds())
    pad = 0.002                     # (the joins' own)
    out["nodes"] = sorted([list(nd) for nd in _nodes_near(sh, cell)
                           if lon0 - pad <= nd[1] <= lon1 + pad and lat0 - pad <= nd[2] <= lat1 + pad],
                          key=lambda nd: json.dumps(nd, sort_keys=True))
    return out


_NODE_INDEX = OrderedDict()         # id(shard) -> (shard, {(z, x, y): [node]}): a shard's nodes by cell, made once
_NODE_LOCK = threading.Lock()


def _nodes_near(sh, cell):
    """The shard's nodes that may lie within the joins' pad of cell (a superset: the caller filters exactly)."""
    import math
    nodes = sh.get("nodes") or ()
    if len(nodes) < 64:
        return nodes
    with _NODE_LOCK:
        got = _NODE_INDEX.get(id(sh))
    if got is None or got[0] is not sh:
        z = int(cell[0])
        n = 1 << z
        pad = 0.003                 # (wider than the joins' 0.002: a candidate test)
        idx = {}
        for nd in nodes:
            try:
                lo, la = float(nd[1]), float(nd[2])
            except (TypeError, ValueError, IndexError):
                continue
            xs = {int((lo + d + 180.0) / 360.0 * n) % n for d in (-pad, pad)}
            ys = set()
            for d in (-pad, pad):
                r = math.radians(max(-85.05, min(85.05, la + d)))
                ys.add(int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n))   # det-ok: an index
            for x in range(min(xs), max(xs) + 1) if max(xs) - min(xs) <= 1 else xs:
                for y in range(min(ys), max(ys) + 1):
                    idx.setdefault((z, x, y), []).append(nd)
        got = (sh, idx)
        with _NODE_LOCK:
            _NODE_INDEX[id(sh)] = got
            while len(_NODE_INDEX) > 8:
                _NODE_INDEX.popitem(last=False)
    return got[1].get(tuple(int(v) for v in cell), [])


def slice_digest(sources, cell, ids, fetch=True):
    """The digest (32 hex) of what cell's roads (their way ids) read of the weekly rail and lane tags, from sources
    ({"rails": railtags.RailTags or None (off), "lanes": LaneTags or None}); None when a shard cannot be had (fetch
    False: when it is not in memory)."""
    import mapcells
    doc = {}
    for name in ("rails", "lanes"):
        t = sources.get(name)
        if t is None:
            doc[name] = "off"
            continue
        key = mapcells.CellKey(*(int(v) for v in cell))
        if fetch:
            sh = t.of_cell(key)
        else:
            if t.known() is None:
                return None
            shift = key.z - 7
            with t.lock:
                sh = t.shards.get((key.x >> shift, key.y >> shift))
        if sh is None:
            return None
        doc[name] = _shard_slice(sh, cell, ids, name == "lanes")
    return hashlib.sha256(canonical(doc)).hexdigest()[:32]


def dep_digest(slices):
    """An item's digest (HASH_HEX hex) of its ring's slices ([(cell, slice digest)])."""
    return hashlib.sha256(canonical([[f"{c[0]}/{c[1]}/{c[2]}", d] for c, d in sorted(slices)])).hexdigest()[:HASH_HEX]


_SOURCES = []


def register_weekly(md):
    """The process's map data, whose weekly shards the digests are made from (MapData.__init__)."""
    import weakref
    _SOURCES[:] = [weakref.ref(md)]


def weekly_sources():
    """{"rails", "lanes"} of the registered map data (None: that dataset off), or None (no map data)."""
    md = _SOURCES[0]() if _SOURCES else None
    if md is None:
        return None
    return {"rails": getattr(md, "rail_tags", None), "lanes": getattr(md, "lane_tags", None)}


def _weekly_builds(sources):
    return tuple(None if t is None else t.known() for t in (sources.get("rails"), sources.get("lanes")))


def block_of(cell):
    """(x, y) of the z12 block holding z14 cell (z, x, y)."""
    sh = int(cell[0]) - BLOCK_Z
    return int(cell[1]) >> sh, int(cell[2]) >> sh


def _cell_tuple(cell):
    if isinstance(cell, str):
        return tuple(int(v) for v in cell.split("/"))
    return tuple(int(v) for v in cell)


class Store:
    """One cache folder's archives in this process: manifests, blocks on disk and in memory, downloads, the weekly
    slices' digests."""

    def __init__(self, cache_dir, fetcher=None, base=None, sources=None):
        fast.require(NATIVE, "shared decision archives")
        self.cache_dir = cache_dir
        self.root = os.path.join(cache_dir, SUBDIR, f"d{FORMAT}")
        self.base = (base or _base()).rstrip("/")
        self._fetcher = fetcher
        self._sources = sources         # (tests: {"rails", "lanes"}; else the registered map data's: weekly_sources)
        self._lock = threading.Lock()
        self._man = {}              # vkey hex -> ({(bx, by): name hash} or None (no manifest), read again after)
        self._mem = OrderedDict()   # (vkey, bx, by) -> (name hash, bytes)
        self._keys = {}             # (vkey, bx, by, hash) -> frozenset of item keys
        self._slices = {}           # (cell, weekly builds, ids digest) -> slice digest
        self._failed = {}           # (vkey, bx, by) -> monotonic time a download failed
        self._inflight = {}         # (vkey, bx, by) -> Event
        self.stats = {"hits": 0, "misses": 0, "fetched": 0, "bytes": 0, "bad": 0, "fetch_s": 0.0, "stale": 0}

    @property
    def fetcher(self):
        if self._fetcher is None:
            self._fetcher = netfetch.default_fetcher()
        return self._fetcher

    def sources(self):
        return self._sources if self._sources is not None else weekly_sources()

    def _count(self, k, n=1):
        with self._lock:
            self.stats[k] = self.stats.get(k, 0) + n

    def _dir(self, vk):
        return os.path.join(self.root, vk)

    def block_path(self, vk, bx, by, h):
        return os.path.join(self._dir(vk), str(BLOCK_Z), str(bx), f"{by}.{h}.bngd")

    def url(self, vk, rest):
        return f"{self.base}/d{FORMAT}/{vk}/{rest}"

    def _local(self, vk, bx, by):
        """(hash, path) of the newest copy of a block on disk (any name), or None."""
        d = os.path.join(self._dir(vk), str(BLOCK_Z), str(bx))
        best = None
        try:
            names = os.listdir(d)
        except OSError:
            return None
        for n in names:
            bits = n.split(".")
            if len(bits) == 3 and bits[0] == str(by) and bits[2] == "bngd":
                p = os.path.join(d, n)
                try:
                    t = os.path.getmtime(p)
                except OSError:
                    continue
                if best is None or t > best[0]:
                    best = (t, bits[1], p)
        return (best[1], best[2]) if best else None

    # --- manifests ---------------------------------------------------------------------------------------------
    def manifest(self, vk):
        """{(bx, by): name hash} of the blocks built for version vk, or None (none built: no manifest)."""
        now = time.monotonic()
        with self._lock:
            got = self._man.get(vk)
            if got is not None and now < got[1]:
                return got[0]
        blocks, ttl = self._load_manifest(vk)
        with self._lock:
            self._man[vk] = (blocks, time.monotonic() + ttl)
        return blocks

    def known_manifest(self, vk):
        """The manifest as last read (memory, else the disk's copy, whatever its age), nothing fetched; or None."""
        with self._lock:
            got = self._man.get(vk)
        if got is not None:
            return got[0]
        try:
            with open(os.path.join(self._dir(vk), "manifest.bngm"), "rb") as f:
                blocks = self._parse_manifest(vk, f.read())
        except (OSError, ValueError):
            return None
        with self._lock:                # (kept, due at once: manifest() reads it again before a download)
            self._man.setdefault(vk, (blocks, time.monotonic()))
        return blocks

    def _parse_manifest(self, vk, body):
        doc = json.loads(fast._ef.dec_open_manifest(body, pubkeys()))
        if doc.get("format") != FORMAT or doc.get("vkey") != vk:
            raise ValueError(f"manifest of {doc.get('vkey')} (format {doc.get('format')}) for {vk}")
        out = {}
        for b in doc.get("blocks", ()):
            h = str(b[2])
            if len(h) != HASH_HEX or any(c not in "0123456789abcdef" for c in h):
                raise ValueError(f"block name {h!r}")
            out[(int(b[0]), int(b[1]))] = h
        return out

    def _load_manifest(self, vk):
        path = os.path.join(self._dir(vk), "manifest.bngm")
        absent = path + ".absent"
        try:                            # (another process of this server read it a moment ago)
            for p, is_absent in ((path, False), (absent, True)):
                age = time.time() - os.path.getmtime(p) if os.path.exists(p) else None
                if age is not None and age < (RETRY_S if is_absent else MANIFEST_TTL_S):
                    if is_absent:
                        return None, RETRY_S - age
                    with open(p, "rb") as f:
                        return self._parse_manifest(vk, f.read()), MANIFEST_TTL_S - age
        except (OSError, ValueError) as e:
            log.info("decisions: manifest of %s on disk unusable (%s: %s)", vk, type(e).__name__, e)
        try:
            body = self.fetcher.get(self.url(vk, "manifest.bngm"), kind="demand", lane=LANE).body
            blocks = self._parse_manifest(vk, body)
            _write(path, body)
            try:
                os.remove(absent)
            except OSError:
                pass
            log.info("decisions: version %s: %d blocks built", vk, len(blocks))
            return blocks, MANIFEST_TTL_S
        except netfetch.NotFound:
            _write(absent, b"")
            log.info("decisions: none built for version %s", vk)
            return None, RETRY_S
        except (netfetch.FetchError, ValueError, OSError) as e:
            log.info("decisions: manifest of %s unavailable (%s: %s)", vk, type(e).__name__, e)
            try:                        # (the copy on disk, however old: blocks only ever get added)
                with open(path, "rb") as f:
                    return self._parse_manifest(vk, f.read()), NET_RETRY_S
            except (OSError, ValueError):
                return None, NET_RETRY_S

    # --- blocks ------------------------------------------------------------------------------------------------
    def block(self, vk, vkb, bx, by, fetch=True):
        """(name hash, file bytes) of block (bx, by) for version vk (verified when it was downloaded), or None: the
        manifest's name from memory or disk, else (fetch, and the manifest lists it) one download, checked whole
        before it is kept. fetch False: nothing fetched, the manifest as last read (else the newest copy on disk)."""
        k = (vk, bx, by)
        if fetch:
            blocks = self.manifest(vk)
            want = blocks.get((bx, by)) if blocks else None
            if want is None:
                if blocks:              # (this version is served, this block not built: the builder may be asked)
                    ask_build(self.cache_dir, vk, bx, by)
                return None
        else:
            blocks = self.known_manifest(vk)
            want = blocks.get((bx, by)) if blocks else None
        with self._lock:
            got = self._mem.get(k)
            if got is not None and (want is None or got[0] == want):
                self._mem.move_to_end(k)
                return got
        if want is None:
            loc = self._local(vk, bx, by)
            if loc is None:
                return None
            want, path = loc
        else:
            path = self.block_path(vk, bx, by, want)
        b = _read(path)
        if b is None and fetch:
            b = self._download(vk, vkb, bx, by, want, path)
        if b is None:
            return None
        got = (want, b)
        with self._lock:
            self._mem[k] = got
            while len(self._mem) > MEM_BLOCKS:
                self._mem.popitem(last=False)
        return got

    def _download(self, vk, vkb, bx, by, h, path):
        k = (vk, bx, by)
        with self._lock:
            t = self._failed.get(k)
            if t is not None and time.monotonic() - t < RETRY_S:
                return None
            ev = self._inflight.get(k)
            mine = ev is None
            if mine:
                ev = self._inflight[k] = threading.Event()
        if not mine:
            ev.wait(FETCH_WAIT_S)
            return _read(path)
        try:
            import xproc
            os.makedirs(os.path.dirname(path), exist_ok=True)
            got, own = xproc.once(path + ".lock", lambda: _read(path), FETCH_WAIT_S)
            if got is not None:
                return got
            try:
                return self._fetch(vk, vkb, bx, by, h, path)
            finally:
                if own:
                    xproc.release(path + ".lock")
        finally:
            with self._lock:
                self._inflight.pop(k, None)
            ev.set()

    def _fetch(self, vk, vkb, bx, by, h, path, kind="demand"):
        t0 = time.perf_counter()
        try:
            body = self.fetcher.get(self.url(vk, f"{BLOCK_Z}/{bx}/{by}.{h}.bngd"), kind=kind, lane=LANE).body
            if len(body) > MAX_BLOCK_BYTES:
                raise ValueError(f"{len(body)} bytes")
            if hashlib.sha256(body).hexdigest()[:HASH_HEX] != h:
                raise ValueError("not the file its name holds")
            fast._ef.dec_verify_block(body, BLOCK_Z, bx, by, vkb, pubkeys())
        except (netfetch.FetchError, ValueError) as e:
            bad = isinstance(e, ValueError)
            if bad:
                self._count("bad")
            log.info("decisions: block %s/%s of %s %s (%s: %s): planned here", bx, by, vk,
                     "rejected" if bad else "unavailable", type(e).__name__, e)
            with self._lock:
                self._failed[(vk, bx, by)] = time.monotonic() - (0 if bad else RETRY_S - NET_RETRY_S)
            return None
        _write(path, body)
        old = os.path.dirname(path)     # (the block's older copies: a rebuild's new name replaces them)
        for n in os.listdir(old):
            if n.startswith(f"{by}.") and n.endswith(".bngd") and n != os.path.basename(path):
                try:
                    os.remove(os.path.join(old, n))
                except OSError:
                    pass
        dt = time.perf_counter() - t0
        with self._lock:
            self.stats["fetched"] += 1
            self.stats["bytes"] += len(body)
            self.stats["fetch_s"] += dt
        log.info("decisions: block %s/%s of %s: %.0f KB in %.2f s", bx, by, vk, len(body) / 1e3, dt)
        return body

    def keys(self, vk, bx, by, got):
        k = (vk, bx, by, got[0])
        with self._lock:
            ks = self._keys.get(k)
        if ks is None:
            ks = frozenset(fast._ef.dec_block_info(got[1])[4])
            with self._lock:
                self._keys[k] = ks
        return ks

    # --- the weekly slices -------------------------------------------------------------------------------------
    def dep(self, vk, bx, by, got, cell, r, fetch=True):
        """The digest of cell's ring r of weekly slices as this server's shards have them (its items' "@" part), or
        None (a ring cell's way ids not in the block, a shard not to be had: no item matches then)."""
        sources = self.sources()
        if sources is None:
            return None
        builds = _weekly_builds(sources)
        ks = self.keys(vk, bx, by, got)
        out = []
        for c in ring(cell, r):
            wk = ways_key(c)
            if wk not in ks:
                return None
            mk = (vk, got[0], c, builds)
            with self._lock:
                d = self._slices.get(mk)
            if d is None:
                try:
                    ids = json.loads(fast._ef.dec_block_item(got[1], wk))
                except (ValueError, TypeError):
                    return None
                d = slice_digest(sources, c, ids, fetch)
                if d is None:
                    return None
                with self._lock:
                    if len(self._slices) > 4096:
                        self._slices.clear()
                    self._slices[mk] = d
            out.append((c, d))
        return dep_digest(out)

    def item(self, versions, cell, key, r, fetch=True):
        """The raw bytes of item key (its base: no digest) of cell for these versions and this server's weekly
        slices, or None."""
        vv = vkey(versions)
        if vv is None:
            return None
        bx, by = block_of(cell)
        got = self.block(vv[0], vv[1], bx, by, fetch)
        if got is None:
            self._count("misses")
            return None
        d = self.dep(vv[0], bx, by, got, cell, r, fetch)
        ks = self.keys(vv[0], bx, by, got)
        if d is None or f"{key}@{d}" not in ks:
            if d is not None and any(base_key(k) == key for k in ks):
                self._count("stale")        # (built with other weekly tags in its ring: planned here)
            self._count("misses")
            return None
        try:
            data = fast._ef.dec_block_item(got[1], f"{key}@{d}")
        except ValueError as e:         # (a damaged file on disk: dropped, planned here)
            log.warning("decisions: block %s/%s of %s: %s", bx, by, vv[0], e)
            self._drop(vv[0], bx, by, got[0])
            return None
        self._count("hits" if data is not None else "misses")
        return data

    def has_item(self, versions, cell, key, r):
        """Is item key of cell in a block on this computer, for this server's weekly slices (shards in memory)?"""
        vv = vkey(versions)
        if vv is None:
            return False
        bx, by = block_of(cell)
        got = self.block(vv[0], vv[1], bx, by, fetch=False)
        if got is None:
            return False
        d = self.dep(vv[0], bx, by, got, cell, r, fetch=False)
        return d is not None and f"{key}@{d}" in self.keys(vv[0], bx, by, got)

    def _drop(self, vk, bx, by, h):
        with self._lock:
            self._mem.pop((vk, bx, by), None)
            self._keys.pop((vk, bx, by, h), None)
        try:
            os.remove(self.block_path(vk, bx, by, h))
        except OSError:
            pass

    def prefetch(self, versions, cells):
        """The blocks of cells (z14 keys) downloaded now when built (a teleport's ring: before planning asks)."""
        vv = vkey(versions)
        if vv is None:
            return 0
        n = 0
        for bx, by in sorted({block_of(c) for c in cells}):
            if self.block(vv[0], vv[1], bx, by) is not None:
                n += 1
        return n


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# --- on-demand builds ----------------------------------------------------------------------------------------------
# A block of a served version that is not built yet: the project's collector service is told (POST {"v": 1, "vkey",
# "block": [x, y]}: a ~60 km2 square, nothing about the player) so the builder adds it after the popular places, at
# most once per block per REQUEST_GAP_S per machine and MAX_REQUESTS_PER_HOUR per process; only with the player's
# anonymous statistics on (aerialpacks.consent: the welcome page's choice) and the switch's "request": true
# (BNGE_DECISIONS_REQUEST=on; earthd sets it). The collector's endpoint is not deployed yet.
REQUEST_URL = os.environ.get("BNGE_DECISIONS_REQUEST_URL", "https://earth.tgforever.net/decisions/request")
REQUEST_GAP_S = 6 * 3600.0
MAX_REQUESTS_PER_HOUR = 6
_asked = []
_ask_lock = threading.Lock()


def ask_build(cache_dir, vk, bx, by):
    """Ask for block (bx, by) of version vk to be built (in the background); True when a request was sent off."""
    if os.environ.get("BNGE_DECISIONS_REQUEST", "").lower() != "on" or not REQUEST_URL or REQUEST_URL == "off":
        return False
    import aerialpacks
    if not aerialpacks.consent():
        return False
    marker = os.path.join(cache_dir, SUBDIR, "asked", f"{vk}_{bx}_{by}")
    now = time.time()
    with _ask_lock:
        try:
            if now - os.path.getmtime(marker) < REQUEST_GAP_S:
                return False
        except OSError:
            pass
        _asked[:] = [t for t in _asked if now - t < 3600.0]
        if len(_asked) >= MAX_REQUESTS_PER_HOUR:
            return False
        _asked.append(now)
        try:
            _write(marker, b"")
        except OSError:
            return False

    def post():
        import urllib.request
        body = json.dumps({"v": 1, "vkey": vk, "block": [int(bx), int(by)]}, separators=(",", ":")).encode()
        req = urllib.request.Request(REQUEST_URL, body, {"Content-Type": "application/json",
                                                         "User-Agent": "BeamNG.Earth"})
        try:
            with urllib.request.urlopen(req, timeout=8.0) as r:
                r.read(4096)
            log.info("decisions: build of block %s/%s asked", bx, by)
        except Exception as e:
            log.info("decisions: build request for block %s/%s failed (%s: %s)", bx, by, type(e).__name__, e)
    threading.Thread(target=post, name="decisions-ask", daemon=True).start()
    return True


_STORES = {}
_STORES_LOCK = threading.Lock()


def store_for(cache_dir):
    with _STORES_LOCK:
        s = _STORES.get(cache_dir)
        if s is None:
            s = _STORES[cache_dir] = Store(cache_dir)
        return s


# --- the hooks (plancache.PlanCache._baked, roads._facts_baked) ----------------------------------------------------
def plans(cache_dir, signature, cell, versions, fetch=True):
    """A shared archive's plans (plancache's npz bytes) of cell for these versions and grading signature, or None."""
    if not enabled() or not cache_dir:
        return None
    try:
        return store_for(cache_dir).item(versions, cell, plans_key(signature, cell), RING_PLANS, fetch)
    except Exception as e:          # (never in the way: planned here)
        log.info("decisions: plans of %s: %s: %s", cell, type(e).__name__, e)
        return None


def has_plans(cache_dir, signature, cell, versions):
    """Does a block already on this computer hold cell's plans for these versions? Nothing fetched or unpacked."""
    if not enabled() or not cache_dir:
        return False
    try:
        return store_for(cache_dir).has_item(versions, cell, plans_key(signature, cell), RING_PLANS)
    except Exception:
        return False


def facts(cache_dir, cid, kind, extra, fetch=True):
    """A shared archive's facts (JSON bytes) of a layer's cell id (cell, layer, versions JSON), or None."""
    if not enabled() or not cache_dir or cid is None:
        return None
    try:
        cell = _cell_tuple(cid[0])
        if len(cell) != 3 or cell[0] != CELL_Z:
            return None
        return store_for(cache_dir).item(cid[2], cell, facts_key(cid[1], kind, extra, cid[0]), RING_FACTS, fetch)
    except Exception as e:
        log.info("decisions: facts of %s: %s: %s", cid[0] if cid else None, type(e).__name__, e)
        return None


def region_block(cache_dir, versions, bx, by, kind="predownload"):
    """One z12 block of a saved area (areas.py's "dec" steps) on disk and pinned; nothing to do when none is built
    for this version. RuntimeError when it is built but could not be had (the area's next pass tries again)."""
    s = store_for(cache_dir)
    vv = vkey(versions)
    if vv is None:
        raise RuntimeError("the map data's versions are not known yet")
    pin(cache_dir, vv[0], (bx, by, bx, by))
    blocks = s.manifest(vv[0])
    if not blocks or (bx, by) not in blocks:
        return False
    path = s.block_path(vv[0], bx, by, blocks[(bx, by)])
    if os.path.exists(path):
        return True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if s._fetch(vv[0], vv[1], bx, by, blocks[(bx, by)], path, kind=kind) is None:
        raise RuntimeError(f"decision block {bx}/{by} not had")
    return True


def region_block_present(cache_dir, versions, bx, by):
    """Is a saved area's block on disk, or known not to be built for this version (nothing to download)?"""
    s = store_for(cache_dir)
    vv = vkey(versions)
    if vv is None:
        return False
    blocks = s.manifest(vv[0])
    if blocks is None or (bx, by) not in blocks:
        return blocks is not None
    return os.path.exists(s.block_path(vv[0], bx, by, blocks[(bx, by)]))


def prefetch_near(cache_dir, versions, lat, lon, radius_m=2500.0):
    """The built blocks within radius_m of (lat, lon) downloaded (earthd: a teleport's first cells), count of blocks."""
    if not enabled() or not cache_dir:
        return 0
    import math
    import mapcells
    dlat = radius_m / 110540.0
    dlon = radius_m / (111320.0 * max(0.05, math.cos(math.radians(lat))))  # det-ok: which blocks to fetch early, no output
    try:
        cells = mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon, z=CELL_Z)
        return store_for(cache_dir).prefetch(versions, [(c.z, c.x, c.y) for c in cells])
    except Exception as e:
        log.info("decisions: prefetch near %.4f,%.4f: %s: %s", lat, lon, type(e).__name__, e)
        return 0


# --- pins and pruning ----------------------------------------------------------------------------------------------
def pin(cache_dir, vk, rect):
    path = os.path.join(cache_dir, SUBDIR, PINS)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    rects = doc.setdefault(vk, [])
    if list(rect) not in rects:
        rects.append([int(v) for v in rect])
        _write(path, json.dumps(doc).encode())


_PIN_MEMO = {}


def _pins(cache_root):
    """{vkey: [(x0, y0, x1, y1)]} of the downloaded regions (memoised on the file's stamp), or None."""
    path = os.path.join(cache_root, SUBDIR, PINS)
    try:
        st = os.stat(path)
    except OSError:
        return None
    memo = _PIN_MEMO.get(path)
    if memo is None or memo[0] != (st.st_mtime_ns, st.st_size):
        try:
            with open(path, encoding="utf-8") as f:
                doc = {k: [tuple(int(v) for v in r) for r in rs] for k, rs in json.load(f).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            doc = {}
        memo = _PIN_MEMO[path] = ((st.st_mtime_ns, st.st_size), doc)
    return memo[1] or None


def pinned_keys(cache_root, keys):
    """[bool] per cache key ("decisions/d2/<vkey>/12/<x>/<y>.<h>.bngd"): a block of a downloaded region, or a pinned
    version's manifest. None when nothing is pinned (cache.Cache._unpinned)."""
    pins = _pins(cache_root)
    if not pins:
        return None
    out = []
    for k in keys:
        bits = k.split("/")
        if k == f"{SUBDIR}/{PINS}":
            out.append(True)
        elif len(bits) < 3 or bits[0] != SUBDIR or bits[2] not in pins:
            out.append(False)
        elif len(bits) == 6 and bits[3] == str(BLOCK_Z):
            try:
                x, y = int(bits[4]), int(bits[5].split(".", 1)[0])
            except ValueError:
                out.append(False)
                continue
            out.append(any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in pins[bits[2]]))
        else:
            out.append(True)
    return out


def prune(cache_dir, current=None, keep_s=86400.0):
    """Other versions' folders untouched for keep_s removed (pinned versions kept: a downloaded region stays until the
    player removes it). current: this server's vkey, never removed."""
    root = os.path.join(cache_dir, SUBDIR, f"d{FORMAT}")
    for f in range(1, FORMAT):          # (an older layout's folders: never read again)
        shutil.rmtree(os.path.join(cache_dir, SUBDIR, f"d{f}"), ignore_errors=True)
    try:
        with open(os.path.join(cache_dir, SUBDIR, PINS), encoding="utf-8") as f:
            pinned = set(json.load(f))
    except (OSError, ValueError):
        pinned = set()
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    n = 0
    for vk in names:
        p = os.path.join(root, vk)
        if vk == current or vk in pinned:
            continue
        try:
            if time.time() - os.path.getmtime(p) < keep_s:
                continue
            shutil.rmtree(p)
            n += 1
        except OSError:
            pass
    return n
