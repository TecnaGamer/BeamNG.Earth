"""BeamMP shared world generation, v2 (.claude/work/mp_share_v2.md; the user, 2026-11-02: "start from scratch", "have the
server recive, store, and send cached, baked world gen data", "let the server itself help generate terrain").

The BeamMP server is its players' cache of world building: the plugin (beammp/Server/BeamNGEarth/main.lua) keeps what
they build of the expensive, anchor-free items (a map cell's road plans: bake packs' items, bakepack.py) and sends
them to whoever asks, from its store or from .bnge packs its host put there; a helper next to it (--share-helper)
plans the cells near the players into the same store. This module is a player's app's side, the Hub in the main
process:

  - hello: the plans' expect (formats, frames, signature, map data versions: PlanCache._bake_expect) and its code;
  - upload: every cell's plans the road processes keep (PlanCache.store persist) while sharing is on: they note it
    in <cache>/share/out (note_built), the hub sends the plans to the plugin (target S);
  - prefetch: the cells around and ahead of the player that are not here yet, asked for (want), answered with the
    item or a miss (asked again later: the helper or another player may make it meanwhile);
  - an item that comes in is checked (the key one we asked for, our signature, an npz without pickles, a size cap) and
    kept in the revisit cache under the key the road processes read: they find it as if planned here. They never
    wait for it: a cell needed before it comes is planned here, as alone.

The transport is BeamMP itself: app -> mod -> TriggerServerEvent('bngeShare', "S|<json>") -> the plugin, and back as
"S|<json>"; items as base64 chunks of CHUNK bytes, paced by a token bucket (OUT_BPS).
"""
import base64
import collections
import hashlib
import io
import json
import logging
import math
import dmath
import dnp
import os
import re
import threading
import time

log = logging.getLogger("earthd.mpshare")

CHUNK = 24 * 1024               # bytes of an item per message (32 KB as base64; the plugin's SHARE_CHUNK)
OUT_BPS = float(os.environ.get("BNGE_SHARE_BPS", 256 * 1024))     # bytes/s we send at most (the token bucket)
BURST = 4 * CHUNK               # the bucket's depth
ASSEMBLE_S = 60.0               # an item's chunks given up on after this long
ITEM_MAX = 8 << 20              # an item's size at most (the plugin's SHARE_ITEM_MAX)
SUBDIR = "share"                # <cache>/share: active (a session shares), out/ (built items to send)
PREFETCH_M = float(os.environ.get("BNGE_SHARE_PREFETCH_M", 1500.0))   # the cells this near the player are asked for
AHEAD_S = 20.0                  # ... and those this many seconds ahead of it
ASK_AGAIN_S = 20.0              # a missed cell asked for again after this long
WANT_BATCH = 48                 # items per want message (the plugin takes 64)
_KEY = re.compile(r"plans/(w[01]s[01])/(\d{1,2})_(\d{1,7})_(\d{1,7})")


# BeamMP's launcher (2.8.1, Network/GlobalHandler.cpp ServerSend) aborts on any message from the game over 500 bytes
# with "Zp" in it (found 2026-11-02: the first upload took the launcher down, "Connection Terminated"). Our base64
# has "-" for "Z": never "Zp". The plugin's G.b64 is the same.
def b64e(data):
    return base64.b64encode(data).replace(b"Z", b"-").decode("ascii")


def b64d(text):
    if not isinstance(text, str) or "Z" in text:
        raise ValueError("not our base64")
    return base64.b64decode(text.replace("-", "Z"), validate=True)


def _inflate(data):
    """zlib-inflated data, ITEM_MAX at most (a small item that inflates to gigabytes is refused)."""
    import zlib
    d = zlib.decompressobj()
    out = d.decompress(data, ITEM_MAX + 1)
    if len(out) > ITEM_MAX or d.unconsumed_tail or not d.eof:
        raise ValueError("over the cap or truncated")
    return out


def expect_code(expect):
    """The code of an expect (16 hex): what a pack's meta and the plugin's store are matched by."""
    return hashlib.sha1(json.dumps(expect, sort_keys=True, default=str).encode()).hexdigest()[:16]


def item_id(code, key):
    """An item's id: its file name in the plugin's store."""
    return hashlib.sha1(f"{code}|{key}".encode()).hexdigest()


def share_code(parts):
    """A hash of an app's sharing parts (formats, map data versions, settings): its share code (the log, the relay)."""
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def owner(members, key):
    """The member id (of members [{id, w}]) owning item key: weighted rendezvous hashing (each item's owner stays when
    others join or leave; a member with twice the weight owns about twice the items). None for no members. (Splitting
    the planning ahead among players near each other: step 5 of the plan.)"""
    best, best_s = None, -math.inf
    for m in members or ():
        w = float(m.get("w") or 1.0)
        h = hashlib.sha1(f"{key}|{m['id']}".encode()).digest()
        u = (int.from_bytes(h[:8], "big") + 0.5) / 2.0 ** 64
        s = -w / dmath.log(u)
        if s > best_s or (s == best_s and str(m["id"]) < str(best)):
            best, best_s = m["id"], s
    return best


# --- the road processes' side -------------------------------------------------------------------------------------
def active(cache_dir):
    """Is a BeamMP session sharing (the hub's <cache>/share/active)?"""
    return bool(cache_dir) and os.path.exists(os.path.join(cache_dir, SUBDIR, "active"))


def note_built(cache_dir, signature, cell):
    """A road process kept cell (z, x, y)'s complete plans (PlanCache.store persist): the hub sends them, while a session
    shares. Never raises."""
    if not active(cache_dir):
        return
    try:
        d = os.path.join(cache_dir, SUBDIR, "out")
        os.makedirs(d, exist_ok=True)
        z, x, y = (int(v) for v in cell)
        open(os.path.join(d, f"plans_{signature}_{z}_{x}_{y}"), "w").close()
    except (OSError, ValueError, TypeError):
        pass


COMING_WAIT_S = float(os.environ.get("BNGE_SHARE_WAIT_S", 3.0))   # a road process waits this long for a cell coming
COMING_MAX_AGE_S = 30.0         # a coming mark this old is stale (its answer lost)


def _coming_path(cache_dir, signature, cell):
    z, x, y = (int(v) for v in cell)
    return os.path.join(cache_dir, SUBDIR, "coming", f"{signature}_{z}_{x}_{y}")


def coming(cache_dir, signature, cell):
    """Has the hub asked the server for cell's plans, no answer yet (a road process then waits COMING_WAIT_S for them
    before planning the cell itself)?"""
    if not active(cache_dir):
        return False
    try:
        return time.time() - os.path.getmtime(_coming_path(cache_dir, signature, cell)) < COMING_MAX_AGE_S
    except (OSError, ValueError, TypeError):
        return False


def _mark(cache_dir, signature, cell, on):
    try:
        p = _coming_path(cache_dir, signature, cell)
        if on:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
        else:
            os.remove(p)
    except (OSError, ValueError, TypeError):
        pass


def valid_plans(data):
    """Are data a plans npz we can read (no pickles; its meta's plans list)?"""
    if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= ITEM_MAX:
        return False
    import numpy as np
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as f:
            if "meta" not in f.files or len(f.files) > 64:
                return False
            for name in f.files:            # (every member opened without pickles: an object array raises)
                if f[name].dtype.hasobject:
                    return False
            meta = json.loads(bytes(f["meta"]).decode("utf-8"))
            return isinstance(meta, dict) and isinstance(meta.get("plans"), list)
    except Exception:
        return False


# --- the main process's side --------------------------------------------------------------------------------------
class Hub:
    """A player's app's side of sharing, in the main process. send(msg): to the mod (Connection.send). store: a
    plancache.PlanCache whose persist is set (signature, frames, map data versions: has / _kept / _revisit_key / its
    expect), or a callable returning one (the map data's versions may come late). Thread-safe; pump() and tick() run
    on the event loop."""

    def __init__(self, send, cache_dir, store):
        self.send_mod = send
        self.cache_dir = cache_dir
        self._store = store
        self.lock = threading.Lock()
        self.out = collections.deque()      # (to, msg str)
        self.tokens, self.t_tokens = float(BURST), time.monotonic()
        self.parts = {}                     # (from, id) -> {"n", "at", "chunks", "key", "iid"}
        self.seq = 0
        self.stats = collections.Counter()
        self.code = None
        self.sent_hello = None              # the expect's code our last hello carried
        self.wanted = {}                    # iid -> (key, cell, time asked)
        self.missed = {}                    # cell -> time the server last said miss
        self.uploaded = set()               # cells sent
        self.probe_sent = False
        try:
            os.makedirs(os.path.join(cache_dir, SUBDIR, "out"), exist_ok=True)
            open(os.path.join(cache_dir, SUBDIR, "active"), "w").close()
        except OSError as e:
            log.warning("share: %s", e)

    def store(self):
        return self._store() if callable(self._store) else self._store

    def close(self):
        """The session is over: the road processes stop noting what they build."""
        try:
            os.remove(os.path.join(self.cache_dir, SUBDIR, "active"))
        except OSError:
            pass

    # --- sending -----------------------------------------------------------------------------------------------
    def _queue(self, obj, front=False):
        s = json.dumps(obj, separators=(",", ":"))
        with self.lock:
            (self.out.appendleft if front else self.out.append)(("S", s))

    def pump(self):
        """Send what the bucket allows now. -> seconds until the next message can go (None: nothing waiting)."""
        now = time.monotonic()
        with self.lock:
            self.tokens = min(BURST, self.tokens + (now - self.t_tokens) * OUT_BPS)
            self.t_tokens = now
            sent = []
            while self.out and (self.tokens >= len(self.out[0][1]) * 0.75 or not sent and self.tokens >= BURST):
                to, s = self.out.popleft()
                self.tokens -= len(s) * 0.75        # (base64: 4/3 of the bytes)
                sent.append((to, s))
            wait = None if not self.out else max(0.005, (len(self.out[0][1]) * 0.75 - self.tokens) / OUT_BPS)
        for to, s in sent:
            self.send_mod({"type": "share_out", "to": to, "d": s})
        return wait

    def hello(self):
        """Our expect and its code to the plugin (again when the expect changed: the map data's versions came)."""
        pc = self.store()
        if pc is None or pc.persist is None:
            return False
        expect = pc._bake_expect()
        if any(v is None for v in (expect.get("versions") or {}).values() if not isinstance(v, (list, dict))):
            return False                    # (the weekly tags' versions not known yet: hello once they are)
        code = expect_code(expect)
        if code == self.sent_hello:
            return True
        self.code = code
        self._queue({"t": "hello", "codes": {"plans": code}, "expects": {code: expect}}, front=True)
        self.sent_hello = code
        self.wanted.clear()
        self.missed.clear()
        log.info("share: hello to the BeamMP server (plans code %s, signature %s)", code, pc.signature)
        return True

    def upload_built(self):
        """The plans the road processes noted (note_built) sent to the plugin. -> how many."""
        pc = self.store()
        if pc is None or self.code is None:
            return 0
        d = os.path.join(self.cache_dir, SUBDIR, "out")
        try:
            names = os.listdir(d)
        except OSError:
            return 0
        n = 0
        for name in names:
            m = re.fullmatch(r"plans_(w[01]s[01])_(\d+)_(\d+)_(\d+)", name)
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                continue
            if not m or m.group(1) != pc.signature:
                continue
            cell = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
            if cell in self.uploaded:
                continue
            got = pc._kept(cell)
            if got is None:
                continue
            self.send_item(f"plans/{pc.signature}/{cell[0]}_{cell[1]}_{cell[2]}", got.read())
            self.uploaded.add(cell)
            n += 1
        return n

    def send_item(self, key, data):
        """data (bytes) as the plugin's item key (an upload), zlib-compressed (plans: ~1.6x), in chunks."""
        import zlib
        if self.code is None or len(data) > ITEM_MAX:
            return
        data = zlib.compress(data, 6)
        iid = item_id(self.code, key)
        with self.lock:
            self.seq += 1
            sid = f"u{self.seq}"
        n = max(1, math.ceil(len(data) / CHUNK))
        for i in range(n):
            self._queue({"t": "c", "id": sid, "i": i, "n": n, "iid": iid, "key": key, "c": self.code, "k": "plans",
                         "z": 1, "d": b64e(data[i * CHUNK:(i + 1) * CHUNK])})
        self.stats["items_out"] += 1
        self.stats["bytes_out"] += len(data)

    def prefetch(self, lat, lon, vx=0.0, vy=0.0, now=None):
        """Ask the plugin for the cells near (lat, lon) and ahead (vx, vy m/s east / north) we do not have. -> how
        many asked."""
        pc = self.store()
        if pc is None or self.code is None:
            return 0
        import mapcells
        now = time.monotonic() if now is None else now
        cells = []
        for ax, ay, r in ((0.0, 0.0, PREFETCH_M), (vx * AHEAD_S, vy * AHEAD_S, PREFETCH_M * 0.6)):
            if r <= 0 or (ax == 0 and ay == 0 and cells):
                continue
            cl = lat + ay / 111320.0
            cg = lon + ax / (111320.0 * max(dmath.cos(math.radians(lat)), 0.01))
            dl, dg = r / 111320.0, r / (111320.0 * max(dmath.cos(math.radians(cl)), 0.01))
            for k in mapcells.cells_for_bbox(cl - dl, cg - dg, cl + dl, cg + dg, max_cells=400):
                cells.append((dnp.sq(k.center()[0] - lat) + dnp.sq((k.center()[1] - lon) * dmath.cos(math.radians(lat))),
                              (k.z, k.x, k.y)))
        items = []
        asked = {v[1] for v in self.wanted.values()}
        for _d, cell in sorted(set(cells)):
            if cell in asked or now - self.missed.get(cell, -1e9) < ASK_AGAIN_S or pc.has(cell):
                continue
            key = f"plans/{pc.signature}/{cell[0]}_{cell[1]}_{cell[2]}"
            iid = item_id(self.code, key)
            self.wanted[iid] = (key, cell, now)
            _mark(self.cache_dir, pc.signature, cell, True)
            items.append({"iid": iid, "key": key, "c": self.code})
        for i in range(0, len(items), WANT_BATCH):
            self._queue({"t": "w", "items": items[i:i + WANT_BATCH]})
        for iid in [i for i, v in self.wanted.items() if now - v[2] > ASSEMBLE_S]:
            w = self.wanted.pop(iid, None)  # (no answer for a minute: asked again by the next round)
            if w is not None:
                _mark(self.cache_dir, pc.signature, w[1], False)
        self.stats["asked"] += len(items)
        return len(items)

    STATS_S = 30.0

    def tick(self, lat, lon, vx=0.0, vy=0.0):
        """Every few seconds (Connection.share_pump): hello, uploads, prefetch; the counts in the log now and then."""
        if not self.hello():
            return
        self.upload_built()
        if lat is not None and lon is not None:
            self.prefetch(lat, lon, vx, vy)
        now = time.monotonic()
        if now - getattr(self, "_stats_at", 0.0) >= self.STATS_S:
            self._stats_at = now
            st = self.stats
            log.info("share: sent %d plans (%.0f KB), asked %d, got %d (%.0f KB), missed %d, refused %d, queue %d",
                     st["items_out"], st["bytes_out"] / 1024, st["asked"], st["kept"], st["bytes_in"] / 1024,
                     st["missed"], st["refused"], len(self.out))

    # --- receiving ---------------------------------------------------------------------------------------------
    def on_data(self, data):
        """A message from the plugin: "<from>|<json>" (from: S, or a player for the relay)."""
        if not isinstance(data, str) or "|" not in data or len(data) > 4 * CHUNK:
            self.stats["dropped"] += 1
            return
        frm, _, body = data.partition("|")
        try:
            msg = json.loads(body)
        except ValueError:
            self.stats["dropped"] += 1
            return
        if not isinstance(msg, dict) or frm != "S":
            return
        if msg.get("t") == "c":
            self._chunk(msg)
        elif msg.get("t") == "m":
            now = time.monotonic()
            for iid in msg.get("items") or ():
                w = self.wanted.pop(iid, None) if isinstance(iid, str) else None
                if w is not None:
                    self.missed[w[1]] = now
                    self.stats["missed"] += 1
                    pc = self.store()
                    if pc is not None:
                        _mark(self.cache_dir, pc.signature, w[1], False)

    def _chunk(self, msg):
        iid = msg.get("iid")
        if not isinstance(iid, str) or iid not in self.wanted:
            self.stats["unasked"] += 1      # (only what we asked for)
            return
        try:
            i, n = int(msg.get("i")), int(msg.get("n"))
            part = b64d(msg.get("d") or "")
        except (TypeError, ValueError):
            self.stats["dropped"] += 1
            return
        if not 0 <= i < n or n * CHUNK > ITEM_MAX + CHUNK or len(part) > CHUNK:
            self.stats["dropped"] += 1
            return
        now = time.monotonic()
        k = str(msg.get("id"))
        with self.lock:
            for old in [x for x, v in self.parts.items() if now - v["at"] > ASSEMBLE_S]:
                self.parts.pop(old, None)
            st = self.parts.setdefault(k, {"n": n, "at": now, "chunks": {}, "iid": iid, "z": msg.get("z") == 1})
            if st["n"] != n or st["iid"] != iid:
                return
            st["chunks"][i] = part
            if len(st["chunks"]) < n:
                return
            self.parts.pop(k, None)
        data = b"".join(st["chunks"][j] for j in range(n))
        if st["z"]:
            try:
                data = _inflate(data)
            except Exception:
                self.wanted.pop(iid, None)
                self.stats["refused"] += 1
                return
        self.on_item(iid, data)

    def on_item(self, iid, data):
        """A wanted item: checked, then kept in the revisit cache where the road processes read it."""
        key, cell, _t = self.wanted.pop(iid, (None, None, None))
        pc = self.store()
        if key is None or pc is None:
            return
        try:
            self._keep(pc, key, cell, data)
        finally:
            _mark(self.cache_dir, pc.signature, cell, False)

    def _keep(self, pc, key, cell, data):
        m = _KEY.fullmatch(key)
        if not m or m.group(1) != pc.signature or not valid_plans(data):
            self.stats["refused"] += 1
            log.info("share: plans of %s refused (not readable plans)", key)
            return
        import revisit
        import plancache
        ok = revisit.store(pc.persist[0], "plans", pc._revisit_key(cell), {"plans.npz": data}, where=pc._where(cell),
                           code=revisit.format_marker("plans", plancache.plans_format()))
        self.stats["kept" if ok else "not_kept"] += 1
        self.stats["bytes_in"] += len(data)
