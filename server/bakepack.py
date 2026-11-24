"""Baked world packs (2026-11-02, the user: "drive through NYC at 500 mph on all devices"; first visits too).

A first visit's heavy, map-only work is baked offline (tools/bake_area.py; a GitHub Actions job of the data repo) and
shipped as one file per region: each map cell's road plans (grading: 10-30 CPU-s a dense cell) and road facts
(roads._cell_ranges / road_specs: ~2 CPU-s). At 500 mph a run down Manhattan needs a cell every ~8 s, more than a
server's four threads could plan.

A pack is valid only for the inputs it was made from (its "meta"): the map data's versions (Overture release, lanes,
rails ...), the plans' signature and frames, and the formats (plancache.PLANS_FORMAT, roads.FACTS_FORMAT: bumped when
their output changes, tests/test_bake_formats.py checks it) instead of the revisit cache's code hash. A pack whose meta
differs is not read.

The file: MAGIC, the index's length (8 bytes, little endian), the index (JSON: {"meta": {...}, "items": {key:
[offset, length]}}, offsets from the end of the index), then the items. Keys: "plans/<sig>/<z>_<x>_<y>" (plancache's
npz bytes) and "facts/<kind>/<extra>/<cell>" (roads' facts JSON). Packs live in <cache>/bake/*.bnge.
"""
import json
import logging
import os
import struct
import threading
import dmath

log = logging.getLogger("earthd.bakepack")

MAGIC = b"BNGEBAKE1\n"
SUBDIR = "bake"
SUFFIX = ".bnge"
BAKE_FORMAT = 1

_lock = threading.Lock()
_packs = {}             # cache dir -> [Pack]
_seen = {}              # cache dir -> the pack files' (name, size, mtime) when last listed


class Writer:
    """Items into a pack file (bake_area.py): add(key, bytes), then close(meta)."""

    def __init__(self, path):
        self.path = path
        self.tmp = path + ".tmp"
        self.body = open(self.tmp + ".body", "wb")
        self.items = {}
        self.off = 0

    def add(self, key, data):
        if key in self.items:
            return
        self.body.write(data)
        self.items[key] = [self.off, len(data)]
        self.off += len(data)

    def close(self, meta):
        self.body.close()
        index = json.dumps({"meta": meta, "items": self.items}, sort_keys=True).encode("utf-8")
        with open(self.tmp, "wb") as out, open(self.tmp + ".body", "rb") as body:
            out.write(MAGIC + struct.pack("<Q", len(index)) + index)
            while True:
                chunk = body.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        os.remove(self.tmp + ".body")
        os.replace(self.tmp, self.path)
        return len(self.items)


class Pack:
    """One pack file, read in place (its index in memory)."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            if f.read(len(MAGIC)) != MAGIC:
                raise ValueError(f"{path}: not a bake pack")
            n, = struct.unpack("<Q", f.read(8))
            index = json.loads(f.read(n).decode("utf-8"))
        self.base = len(MAGIC) + 8 + n
        self.meta = index.get("meta") or {}
        self.items = index.get("items") or {}

    def get(self, key):
        at = self.items.get(key)
        if at is None:
            return None
        with open(self.path, "rb") as f:
            f.seek(self.base + at[0])
            data = f.read(at[1])
        return data if len(data) == at[1] else None


def packs(cache_dir):
    """The valid-looking packs of a cache (listed again when the folder changes)."""
    d = os.path.join(cache_dir, SUBDIR)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(SUFFIX))
        stamp = tuple((n, os.path.getsize(os.path.join(d, n)), os.path.getmtime(os.path.join(d, n))) for n in names)
    except OSError:
        return []
    with _lock:
        if _seen.get(cache_dir) == stamp:
            return _packs.get(cache_dir, [])
        out = []
        for n in names:
            try:
                out.append(Pack(os.path.join(d, n)))
            except (OSError, ValueError) as e:
                log.warning("bake pack %s: %s", n, e)
        _packs[cache_dir], _seen[cache_dir] = out, stamp
        if out:
            log.info("bake packs: %s", ", ".join(f"{os.path.basename(p.path)} ({len(p.items)} items)" for p in out))
        return out


def _matches(pack, expect):
    m = pack.meta
    return all(m.get(k) == v for k, v in expect.items())


def find(cache_dir, key, expect):
    """The bytes of item key from a pack whose meta has every (key, value) of expect, or None."""
    if not cache_dir:
        return None
    for p in packs(cache_dir):
        if key in p.items and _matches(p, expect):
            got = p.get(key)
            if got is not None:
                return got
    return None


def plans_key(signature, cell):
    return f"plans/{signature}/{int(cell[0])}_{int(cell[1])}_{int(cell[2])}"


def facts_key(kind, extra, cell):
    return f"facts/{kind}/{json.dumps(extra)}/{cell}"


# --- the data repo's packs (its "bake" release: index.json and one .bnge per region) ------------------------------
RELEASE_URL = "https://github.com/TecnaGamer/BeamNG.Earth-data/releases/download/bake/"
INDEX_TTL_S = 24 * 3600     # index.json read again after this long
CHECK_S = 600.0             # a place's packs looked for at most this often ...
CHECK_RETRY_S = 30.0        # ... sooner while the map data's versions are not all known yet
NEAR_M = 5000.0             # a pack within this of the player is fetched
_sync = {"at": -1e9, "busy": False, "backoff": None}


# Switches whose output is confined to cells that are never baked or shared (roadz.hk_cell: Hong Kong's measured heights
# and terrain stay on the computer that read them): the packs and the BeamMP helper are made with both off
# (sharehelper.py, bake_area), the players' servers with both on; the cells they change are left out by cell instead;
# and Switzerland's (roadz.tlm_tags, from BNE_ROADZ_TLM_DIR too: no pack or archive is made with its cells)
CELL_SCOPED = frozenset({"BNE_ROADZ_HK", "BNE_HKDTM", "BNE_ROADZ_TLM", "BNE_ROADZ_TLM_DIR", "BNGE_ROADZ_TLM_URL"})
_switches = []


def switches():
    """The build switches a pack's (or a BeamMP share's, mpshare.expect_code) items were made with: revisit.switches_key's
    BNE_* that change output, the settings page's Experimental ones among them, without CELL_SCOPED and the switches no
    plan or fact depends on (revisit.PLAN_NEUTRAL: the same key as revisit.plans_switches_key's); in its meta and in
    every expect: a pack made with other switches (the defaults, for a player with an experimental switch on) holds
    other plans and facts. The bridge profile as the roads read it (earthd writes its CLI default into the
    environment, the helper and bake_area do not: the same rule as the decision archives). Once a process."""
    if not _switches:
        import hashlib
        import revisit
        import roads
        sw = {k: v for k, v in os.environ.items()       # (revisit.PLAN_NEUTRAL: no plan or fact differs with them)
              if revisit.keyed_env(k) and k not in CELL_SCOPED and k not in revisit.PLAN_NEUTRAL}
        sw["BNE_BRIDGE_PROFILE"] = roads.bridge_profile_mode()
        h = hashlib.sha1()
        for k in sorted(sw):
            h.update(revisit.keyed_value(k, sw[k]).encode())
        _switches.append(h.hexdigest()[:16])
    return _switches[0]


def _expect_for(md, signature):
    import plancache
    import roads
    return {"bake_format": BAKE_FORMAT, "plans_format": plancache.PLANS_FORMAT, "facts_format": roads.FACTS_FORMAT,
            "frames": plancache.FRAMES, "signature": signature, "switches": switches(),
            "versions": json.loads(json.dumps(md.versions(), sort_keys=True))}


def _get(net, url, path, cap):
    import urllib.request
    import netfetch
    tmp = path + ".part"
    with net.session(url, kind="demand") as s:
        req = urllib.request.Request(url, headers=s.headers)
        n = 0
        # (Windows: the system's certificate check, as netfetch's own requests, GitHub #567)
        with urllib.request.urlopen(req, timeout=60, context=netfetch.windows_ssl_context()) as resp, \
                open(tmp, "wb") as out:
            s.status = resp.status
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                n += len(chunk)
                if n > cap:
                    raise IOError(f"{url}: over {cap} bytes")
                out.write(chunk)
            s.nbytes = n
    os.replace(tmp, path)


def sync(cache_dir, net, md, lat, lon, signature, now=None):
    """The data repo's packs near (lat, lon) that fit this server (_expect_for) into <cache>/bake, once (a background
    thread's job: earthd calls it as the player moves). Returns the names fetched now."""
    import math
    import time as _t
    now = _t.monotonic() if now is None else now
    if _sync["busy"] or now - _sync["at"] < CHECK_S or net is None or md is None:
        return []
    expect = _expect_for(md, signature)
    if any(v is None for v in (expect["versions"] or {}).values() if not isinstance(v, (list, dict))):
        _sync["at"] = now - CHECK_S + CHECK_RETRY_S     # (the weekly tags' versions not known yet: soon again)
        return []
    _sync["at"], _sync["busy"] = now, True
    got = []
    import netfetch
    backoff = _sync["backoff"]
    if backoff is None:
        backoff = _sync["backoff"] = netfetch.Backoff(CHECK_S, INDEX_TTL_S / 4)
    try:
        d = os.path.join(cache_dir, SUBDIR)
        os.makedirs(d, exist_ok=True)
        ip = os.path.join(d, "index.json")
        # (nothing waits on these downloads, in a background thread no build's scope covers: their failures fault no
        # build, netfetch.stale_ok; the server plans for itself meanwhile. A stale index stays in use when its refresh
        # fails, asked again after the backoff)
        with netfetch.stale_ok():
            old = _read_index(ip)
            if old is None or _t.time() - os.path.getmtime(ip) > INDEX_TTL_S:
                if old is None or backoff.due(now):
                    try:        # (checked before it replaces the copy: a portal's page is no index)
                        _get(net, RELEASE_URL + "index.json", ip + ".new", 4 << 20)
                        if _read_index(ip + ".new") is None:
                            raise ValueError("index.json: not an index")
                        os.replace(ip + ".new", ip)
                        backoff.ok()
                    except Exception as ex:
                        if old is None:
                            raise
                        netfetch.refresh_failed(log, backoff, now, "bake packs: index.json", ex)
        index = _read_index(ip)
        if index is None:
            raise ValueError("index.json unreadable")
        for e in index.get("packs", []):
            s, w, n, east = e["bbox"]
            k = 111320.0 * dmath.cos(math.radians(lat))
            dy = max(s - lat, 0.0, lat - n) * 110540.0
            dx = max(w - lon, 0.0, lon - east) * k
            if math.hypot(dx, dy) > NEAR_M or not _matches(_Meta(e.get("meta") or {}), expect):
                continue
            path = os.path.join(d, e["name"])
            if os.path.exists(path) and os.path.getsize(path) == e.get("size"):
                continue
            with netfetch.stale_ok():
                _get(net, RELEASE_URL + e["name"], path, int(e.get("size", 0)) + (1 << 20))
            log.info("bake pack %s fetched (%.1f MB)", e["name"], os.path.getsize(path) / 1e6)
            got.append(e["name"])
    except Exception as ex:          # (none there, offline, a rate limit: the server plans for itself, as before)
        log.info("bake packs: %s: %s", type(ex).__name__, ex)
    finally:
        _sync["busy"] = False
    return got


def _read_index(ip):
    """The release's index.json on disk (a dict), or None (none, unreadable)."""
    try:
        with open(ip, encoding="utf-8") as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


class _Meta:
    def __init__(self, meta):
        self.meta = meta


def index_of(folder):
    """{"packs": [{name, size, bbox, meta}]} of the packs in folder (tools/bake_area.py --index: the release's
    index.json)."""
    out = []
    for n in sorted(os.listdir(folder)):
        if n.endswith(SUFFIX):
            p = Pack(os.path.join(folder, n))
            out.append({"name": n, "size": os.path.getsize(os.path.join(folder, n)), "bbox": p.meta.get("bbox"),
                        "meta": {k: v for k, v in p.meta.items() if k not in ("bbox", "made")}, "items": len(p.items)})
    return {"packs": out}
