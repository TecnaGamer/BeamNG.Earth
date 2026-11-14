"""The 1 m aerial packs on the project's own tile host (imagery source "bngaer"; the user, 2026-11-13: prebaked for
metro areas and main road corridors, built on demand by the mini PC elsewhere, only from openly licensed photos: NAIP
and the open European agencies). AVIF 4:4:4 tiles in BNGP packs (tilepack.py) of zooms 17 .. 14, built by
tools/build_aerial_packs.py (its docstring: layout, coverage format); the coverage files' logic is in Rust
(earthfast aerialcov: aerial_cov_table / aerial_pack_state), this is the glue: fetch, cache, ask for builds.

    {BASE}/cov/{rx}/{ry}.json        per zoom-6 region: the zoom-11 cells' packs and build generations
    {BASE}/g{gen}/{z}/{px}/{py}.bngp the packs (immutable)

Coverage.state(z, px, py) answers (state, generation): 1 built (fetch g{gen}), 0 not built yet (the live sources
draw it; ask_build asks the mini PC to build its cell), 2 no open photos there; None while the region's file is not on
hand (its fetch started; wait > 0 waits for it). A region file is read again after COV_TTL_S (an on-demand build shows
up), sooner while a build this process asked for is pending (POLL_S).

On-demand builds (ask_build): POST {REQUEST_URL} {"v": 1, "cell": [x, y]} to the project's collector service
(native/serverlist on the mini PC), at most once per cell per REQUEST_GAP_S on this machine (a marker file per cell
under the cache, shared by the server's processes) and MAX_REQUESTS_PER_HOUR per process; never with anything that
identifies the player (no install id, no position beyond the ~15 km cell). Failures are silent: the live sources go
on drawing the place.

BNGE_AERIAL_BASE: another base (tests: a local server; tools/aerial_mock.py). BNGE_AERIAL_REQUEST_URL: another
request endpoint ("off": never ask).
"""
import json
import logging
import os
import threading
import time
import urllib.request

import fast
import netfetch

log = logging.getLogger("earthd.aerial")

NATIVE = 271                    # earthfast aerialcov
COV_TTL_S = 600.0               # s a region's coverage is used before it is read again
COV_FAIL_S = 60.0               # s a region whose fetch failed is not asked again (the live sources meanwhile)
COV_WAIT_S = 3.0                # s a pack download waits for its region's first coverage (the first block)
POLL_S = (90.0, 180.0, 300.0, 600.0)    # a region with a build asked for: read again after these (then COV_TTL_S)
REQUEST_GAP_S = 6 * 3600.0      # a cell asked for at most this often from one machine
MAX_REQUESTS_PER_HOUR = 6       # ... and at most this many cells an hour per process
REQUEST_TIMEOUT_S = 8.0
REQUEST_URL = os.environ.get("BNGE_AERIAL_REQUEST_URL", "https://earth.tgforever.net/aerial/request")
FORMAT = 1


def avif_ok():
    """Can this build's Pillow decode AVIF (its libavif with a decoder: the PyPI wheels' dav1d, Pillow >= 11.3)?"""
    try:
        from PIL import features
        return bool(features.check("avif"))
    except Exception:
        return False


class Coverage:
    def __init__(self, fetcher, base, cov_dir, request_url=REQUEST_URL):
        self.fetcher, self.base, self.cov_dir = fetcher, base.rstrip("/"), cov_dir
        self.request_url = "" if request_url in ("", "off") else request_url
        self._lock = threading.Lock()
        self._regions = {}          # (rx, ry) -> (table bytes or None (no file: nothing built), loaded at, next read)
        self._inflight = {}         # (rx, ry) -> threading.Event
        self._failed = {}           # (rx, ry) -> time of the failed fetch
        self._asked = []            # times of this process's build requests (MAX_REQUESTS_PER_HOUR)
        self.stats = {}

    def _count(self, k):
        self.stats[k] = self.stats.get(k, 0) + 1

    # --- coverage ------------------------------------------------------------------------------------------------
    def state(self, z, px, py, wait=0.0):
        """(state, generation) of pack (z, px, py) (see the module's docstring), or None while unknown."""
        where = fast._ef.aerial_pack_where(int(z), int(px), int(py))
        if where is None:
            return (3, 0)
        r = where[0]
        got = self._region(r, wait)
        if got is None:
            return None
        if got[0] is None:                      # no file: nothing built in the region yet
            return (0, 0)
        return tuple(fast._ef.aerial_pack_state(r[0], r[1], got[0], int(z), int(px), int(py)))

    def tile_state(self, z, x, y, wait=0.0):
        """state() of the pack holding tile (z, x, y) (zooms 14 .. 17)."""
        return self.state(z, x >> 3, y >> 3, wait)

    def cell_of(self, z, px, py):
        where = fast._ef.aerial_pack_where(int(z), int(px), int(py))
        return None if where is None else where[1]

    def _region(self, r, wait):
        now = time.time()
        ev = None
        with self._lock:
            got = self._regions.get(r)
            if got is not None and now < got[2]:
                return got
            if now - self._failed.get(r, -1e9) < COV_FAIL_S:
                return got                      # (the stale one, if any)
            ev = self._inflight.get(r)
            if ev is None:
                ev = self._inflight[r] = threading.Event()
                threading.Thread(target=self._load, args=(r, ev), name="aerial-cov", daemon=True).start()
        if wait > 0 and ev is not None:
            ev.wait(wait)
        with self._lock:
            return self._regions.get(r) if (got is None or r in self._regions) else got

    def _disk(self, r):
        return os.path.join(self.cov_dir, str(r[0]), f"{r[1]}.json")

    def _load(self, r, ev):
        try:
            path = self._disk(r)
            body, fresh_until = None, 0.0
            try:                                # another process of this server read it a moment ago
                age = time.time() - os.path.getmtime(path)
                if age < COV_TTL_S and not self._pending(r):
                    with open(path, "rb") as fh:
                        body = fh.read()
                    fresh_until = time.time() + COV_TTL_S - age
            except OSError:
                pass
            if body is None:
                try:
                    body = self.fetcher.get(f"{self.base}/cov/{r[0]}/{r[1]}.json", kind="demand").body
                    self._count("cov:net")
                except netfetch.NotFound:
                    body = b""
                    self._count("cov:404")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
                with open(tmp, "wb") as fh:
                    fh.write(body)
                os.replace(tmp, path)
                fresh_until = time.time() + self._ttl(r)
            else:
                self._count("cov:disk")
            table = None
            if body:
                rx, ry, table = fast._ef.aerial_cov_table(body)
                if (rx, ry) != tuple(r):
                    raise ValueError(f"coverage file of region {rx},{ry} at {r}")
            with self._lock:
                self._regions[r] = (table, time.time(), fresh_until)
                self._failed.pop(r, None)
        except Exception as e:                  # (offline, the CDN down, a bad file: the copy on disk, else the live sources)
            log.info("aerial: coverage %s unavailable (%s: %s)", r, type(e).__name__, e)
            self._count("cov:error")
            stale = None
            try:
                with open(self._disk(r), "rb") as fh:
                    body = fh.read()
                stale = fast._ef.aerial_cov_table(body)[2] if body else None
                ok = True
            except (OSError, ValueError):
                ok = False
            with self._lock:
                self._failed[r] = time.time()
                if ok and r not in self._regions:
                    self._regions[r] = (stale, time.time(), time.time() + COV_FAIL_S)
        finally:
            with self._lock:
                self._inflight.pop(r, None)
            ev.set()

    # --- on-demand builds --------------------------------------------------------------------------------------
    def _marker(self, cell):
        return os.path.join(self.cov_dir, "asked", f"{cell[0]}_{cell[1]}")

    def _pending(self, r):
        """A build this machine asked for in region r within the last POLL_S[-1] s (read its coverage sooner)."""
        d = os.path.join(self.cov_dir, "asked")
        try:
            now = time.time()
            for n in os.listdir(d):
                cx, cy = (int(v) for v in n.split("_"))
                if (cx >> 5, cy >> 5) == tuple(r) and now - os.path.getmtime(os.path.join(d, n)) < POLL_S[-1]:
                    return True
        except (OSError, ValueError):
            pass
        return False

    def _ttl(self, r):
        """s until region r is read again: sooner while a build asked from here is pending."""
        d = os.path.join(self.cov_dir, "asked")
        try:
            now = time.time()
            ages = [now - os.path.getmtime(os.path.join(d, n)) for n in os.listdir(d)
                    if tuple(int(v) >> 5 for v in n.split("_")) == tuple(r)]
        except (OSError, ValueError):
            ages = []
        for a in sorted(ages):
            for p in POLL_S:
                if a < p:
                    return max(15.0, p - a)
        return COV_TTL_S

    def ask_build(self, z, px, py):
        """Ask for the on-demand build of the cell holding pack (z, px, py) (not built, photos may exist): in the
        background, at most once per cell per REQUEST_GAP_S per machine. True when a request was sent off. Only with
        the player's anonymous statistics on (consent()): the request names the ~15 km cell the player is in."""
        if not self.request_url or not consent():
            return False
        cell = self.cell_of(z, px, py)
        if cell is None:
            return False
        m = self._marker(cell)
        now = time.time()
        with self._lock:
            try:
                if now - os.path.getmtime(m) < REQUEST_GAP_S:
                    return False
            except OSError:
                pass
            self._asked = [t for t in self._asked if now - t < 3600.0]
            if len(self._asked) >= MAX_REQUESTS_PER_HOUR:
                self._count("ask:limited")
                return False
            self._asked.append(now)
            try:
                os.makedirs(os.path.dirname(m), exist_ok=True)
                with open(m, "w"):
                    pass
            except OSError:
                return False
            r = (cell[0] >> 5, cell[1] >> 5)
            got = self._regions.get(r)
            if got is not None:                 # read the region again at the first poll
                self._regions[r] = (got[0], got[1], min(got[2], now + POLL_S[0]))
        threading.Thread(target=self._post, args=(cell,), name="aerial-ask", daemon=True).start()
        return True

    def _post(self, cell):
        body = json.dumps({"v": FORMAT, "cell": [int(cell[0]), int(cell[1])]}, separators=(",", ":")).encode()
        req = urllib.request.Request(self.request_url, body, {"Content-Type": "application/json",
                                                              "User-Agent": "BeamNG.Earth"})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as r:
                ans = json.loads(r.read(4096) or b"{}")
            self._count("ask:" + str(ans.get("state", "sent"))[:16])
            log.info("aerial: build of cell %s asked: %s", cell, ans.get("state"))
        except Exception as e:
            self._count("ask:error")
            log.info("aerial: build request for cell %s failed (%s: %s)", cell, type(e).__name__, e)


def consent():
    """May an on-demand build request be sent? Only when the player chose to share anonymous statistics (the welcome
    page's choice, the settings page's switch: telemetry.choices) and telemetry is not off (BNE_TELEMETRY): the request
    tells the project's server which ~15 km cell is being visited (stored without the address). The game's user
    folder comes from BNGE_USER_DIR (earthd sets it at start; block worker processes inherit it); none: no request."""
    try:
        import cpupin
        import telemetry
        user_dir = os.environ.get("BNGE_USER_DIR") or ""
        return bool(user_dir) and telemetry.ON and telemetry.choices(cpupin.player_settings(user_dir))["main"]
    except Exception as e:              # (no request rather than one without the player's choice)
        log.debug("aerial: consent unknown (%s: %s): no build request", type(e).__name__, e)
        return False


_COV = {}
_COV_LOCK = threading.Lock()


def coverage(fetcher, base, cov_dir):
    """The process's Coverage for this base and folder (one per process: block workers make their own)."""
    fast.require(NATIVE, "aerial pack coverage")
    with _COV_LOCK:
        c = _COV.get((base, cov_dir))
        if c is None:
            c = _COV[(base, cov_dir)] = Coverage(fetcher, base, cov_dir,
                                                 os.environ.get("BNGE_AERIAL_REQUEST_URL") or REQUEST_URL)
        return c
