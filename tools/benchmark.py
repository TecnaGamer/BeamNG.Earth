#!/usr/bin/env python3
"""Automated visual benchmark (docs/DEVTOOLS.md "Visual benchmark"; docs/ROADMAP.md "Principle:
improvements must generalise"): the same views of a fixed, diverse set of places (docs/benchmarks.json),
with the same sun and sky, screenshotted and measured, so a change can be judged everywhere before
and after.

    python3 tools/benchmark.py plan  [--set tuning|heldout|all] [--places id,id]   the shots, no game
    python3 tools/benchmark.py check                                              preconditions (reads only)
    python3 tools/benchmark.py run --label before-x [--set ...] [--places ...]     needs the game
    python3 tools/benchmark.py compare <runA> <runB> [--out DIR]                  side by side, no game
    python3 tools/benchmark.py restore <run>                                      undo a killed run

run: for each place, teleport there (beamngEarth_core.teleport), wait until the far field is done,
nothing is queued or waiting to swap and the tile under the car is the exact level (core status,
polled once a second; with --wait-backlog also until the server's backlog is 0, which is only fresh
with earthd --status-interval 1; with --server-log also until no colour-match / de-shadow region
was computed for --region-quiet s), stop the sky clock at the place's fixed time and give it a clear sky,
then for each view: the free camera there, a settle, a perfmon window (fps, p50 / p95 / p99, max),
a screenshot. Numbers: load time (to the anchor commit, far_done, exact ground, ready), tiles
(live, imports, failed, textured), exactMiss while loading and after. Everything goes to
docs/research/benchmarks/<date>-<label>/ (git-ignored): the images, run.json (written after every
place) and state.json (what restore needs, written before anything changes).

Safe for the user: refuses while the player vehicle moves faster than 1 m/s (--force overrides) and
stops before the next teleport when it does mid-run (the user took the car: it is then left alone),
zeroes the inputs and sets the parking brake (again after each teleport), and always (finally, also
on Ctrl-C / SIGTERM, each step even after a second interrupt) puts back the weather and sky modes and
values, teleports the player back to where it started and gives the camera back (the game camera, or
the user's free camera at the same place relative to the car; the free camera's own zoom as it was).
A teleport that fails or never commits takes no shots (they would show the previous place); a
timed-out MCP request is sent once more.
GE side: tools/benchmark.lua (sent through MCP run_lua, like tools/m4_probes.py's chunk).

compare: pairs the shots of two runs by place and view, writes <out>/index.html (A | B | difference
heat map per shot, metrics, number deltas) and <out>/compare.json: per image the mean absolute
difference (RGB, 0-255), a windowed SSIM on luminance (7 x 7 box windows, Wang et al. 2004
constants) and the share of pixels whose luminance changed by more than 16; per place the load and
tile numbers, per view the frame times, as A, B, delta and %, flagged "worse" beyond TOLERANCE.
Images are compared at --metric-width px wide (default 1280), the same for both.
"""
import argparse
import datetime
import hashlib
import html
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
LUA = HERE / "benchmark.lua"
SET_PATH = ROOT / "docs" / "benchmarks.json"
ROADS_QUIET_S = 6.0         # s without a new or deleted road before the views are ready
RUNS_DIR = ROOT / "docs" / "research" / "benchmarks"

MAX_SPEED = 1.0             # m/s: refuse to start while the player moves faster (--force overrides)
POLL_S = 1.0                # core status polls at most once a second (as tools/drive.py)
TIMEOUT_S = 300.0           # per place, teleport to ready
VIEW_TIMEOUT_S = 60.0       # per view, after moving the camera
HOME_TIMEOUT_S = 120.0      # the teleport back: until the anchor is committed
SETTLE_S = 3.0              # after the time / weather and after each camera move (motion blur, TAA, fog)
FPS_S = 5.0                 # perfmon window per view
STABLE_POLLS = 2            # ready on this many polls in a row
MCP_RETRIES = 1             # a timed-out MCP call is sent again this often (every BNGE_BENCH call is idempotent)
REGION_QUIET_S = 20.0       # --server-log: no region line for this long
REGION_RE = re.compile(r"(colormatch|deshadow) region")
CLEAR = {"coverage": 0.0, "cirrus": 0.0, "visM": 60000}
METRIC_WIDTH = 1280
THUMB_WIDTH = 960
CHANGED_DY = 16             # luminance step that counts a pixel as changed
SSIM_K = 7
# compare: a number is flagged "worse" when it moved this far the wrong way (relative, and absolute)
TOLERANCE = {
    "load.ready_s": (0.20, 2.0, "lower"),
    "tiles.live": (0.10, 20, "none"),
    "tiles.imports": (0.10, 20, "none"),
    "tiles.failed": (0.0, 0, "lower"),
    "exact.missAfter": (0.0, 0, "lower"),
    "exact.missLoad": (0.25, 10, "lower"),
    "perf.fps": (0.05, 1.0, "higher"),
    "perf.p95": (0.10, 1.0, "lower"),
    "perf.maxMs": (0.25, 10.0, "lower"),
}
PLACE_NUMBERS = ("load.ready_s", "load.anchor_s", "load.farDone_s", "tiles.live", "tiles.imports",
                 "tiles.failed", "tiles.textured", "exact.missLoad", "exact.missAfter")
PLACE_FLAGS = (("ok", False), ("load.timedOut", True))     # (key, the bad value)
VIEW_NUMBERS = ("perf.fps", "perf.p50", "perf.p95", "perf.p99", "perf.maxMs", "perf.over50")
SSIM_FLAG = 0.98            # compare: an image below this SSIM is "changed"
CHANGED_FLAG = 0.01          # ... or with more than this share of its pixels changed (|dY| > CHANGED_DY)

ID_RE = re.compile(r"^[a-z0-9_]+$")


class BenchError(RuntimeError):
    """A place or a step failed (the run goes on with the next place where it can)."""


class SetError(ValueError):
    """docs/benchmarks.json is invalid."""


class McpError(RuntimeError):
    """The MCP request itself failed (not the Lua in it). timeout: the game did not answer in time
    (a stall: worth one more try); otherwise it is unreachable (refused, reset)."""

    def __init__(self, msg, timeout=False):
        super().__init__(msg)
        self.timeout = timeout


# --- the benchmark set (pure) ------------------------------------------------------------------
def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate_view(pid, v, problems):
    name = v.get("name")
    where = f"{pid}/{name}"
    if not isinstance(name, str) or not ID_RE.match(name):
        problems.append(f"{pid}: view name {name!r} (lower case, digits, _)")
    if ("alt" in v) == ("agl" in v):
        problems.append(f"{where}: exactly one of alt / agl")
    for k in ("alt", "agl", "e", "n", "back", "heading", "pitch", "fov", "lat", "lon"):
        if k in v and not _num(v[k]):
            problems.append(f"{where}: {k} must be a number")
    if ("lat" in v) != ("lon" in v):
        problems.append(f"{where}: lat and lon go together")
    if "lat" in v and any(k in v for k in ("e", "n", "back")):
        problems.append(f"{where}: lat/lon or e/n/back, not both")
    if _num(v.get("pitch", 0)) and not -89.5 <= v.get("pitch", 0) <= 89.5:
        problems.append(f"{where}: pitch within +-89.5")
    if _num(v.get("fov", 70)) and not 10 <= v.get("fov", 70) <= 120:
        problems.append(f"{where}: fov 10..120")
    if _num(v.get("agl", 1)) and v.get("agl", 1) <= 0:
        problems.append(f"{where}: agl > 0")


def _parse_date(s):
    try:
        return datetime.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def validate_set(s):
    """Raise SetError with every problem of a benchmark set."""
    problems = []
    if not isinstance(s, dict) or not isinstance(s.get("places"), list) or not s["places"]:
        raise SetError("a benchmark set is {defaults, places: [...]}")
    d = s.get("defaults") or {}
    if _parse_date(d.get("date")) is None:
        problems.append(f"defaults: date {d.get('date')!r} (YYYY-MM-DD)")
    for v in d.get("views") or []:
        validate_view("defaults", v, problems)
    if not d.get("views"):
        problems.append("defaults: views")
    seen = set()
    for p in s["places"]:
        pid = p.get("id")
        if not isinstance(pid, str) or not ID_RE.match(pid):
            problems.append(f"place id {pid!r} (lower case, digits, _)")
            continue
        if pid in seen:
            problems.append(f"{pid}: duplicate id")
        seen.add(pid)
        if not (_num(p.get("lat")) and -90 <= p["lat"] <= 90 and _num(p.get("lon")) and -180 <= p["lon"] <= 180):
            problems.append(f"{pid}: lat/lon")
        if not isinstance(p.get("heldout", False), bool):
            problems.append(f"{pid}: heldout is true or false")
        if p.get("weather", d.get("weather", "clear")) not in ("clear", "keep"):
            problems.append(f"{pid}: weather clear or keep")
        if "date" in p and _parse_date(p["date"]) is None:
            problems.append(f"{pid}: date {p['date']!r}")
        hour = p.get("hour", d.get("hour", 10.0))
        if not (_num(hour) and 0 <= hour < 24):
            problems.append(f"{pid}: hour 0..24")
        for k in ("heading", "utcOffset"):
            if k in p and not _num(p[k]):
                problems.append(f"{pid}: {k} must be a number")
        views = p.get("views", d.get("views") or [])
        names = [v.get("name") for v in views]
        if len(set(names)) != len(names):
            problems.append(f"{pid}: view names repeat")
        if "views" in p:
            for v in p["views"]:
                validate_view(pid, v, problems)
    if problems:
        raise SetError("; ".join(problems))
    return s


def load_set(path=SET_PATH):
    with open(path) as f:
        s = json.load(f)
    return validate_set(s)


def set_digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def select_places(s, which="tuning", ids=None):
    """The places of a run, in the set's order: `ids` (any group, held out or not), else by `which`:
    tuning (not held out), heldout, all."""
    places = s["places"]
    if ids:
        known = {p["id"] for p in places}
        bad = [i for i in ids if i not in known]
        if bad:
            raise SetError("unknown place(s): " + ", ".join(bad))
        return [p for p in places if p["id"] in ids]
    if which == "tuning":
        return [p for p in places if not p.get("heldout")]
    if which == "heldout":
        return [p for p in places if p.get("heldout")]
    if which == "all":
        return list(places)
    raise SetError(f"--set {which!r}: tuning, heldout or all")


def camera_dir(heading, pitch):
    """Unit view vector in the anchor's ENU frame (x east, y north, z up); heading 0 = north."""
    h, p = math.radians(heading), math.radians(pitch)
    return {"x": math.sin(h) * math.cos(p), "y": math.cos(h) * math.cos(p), "z": math.sin(p)}


def resolve_view(place, view):
    """A view of the set -> the Lua view() config (benchmark.lua): e/n (or lat/lon), alt or agl, dir, fov."""
    heading = float(view.get("heading", place.get("heading", 0.0)))
    pitch = float(view.get("pitch", 0.0))
    cfg = {"name": view["name"], "heading": heading, "pitch": pitch, "fov": float(view.get("fov", 70.0))}
    if "lat" in view:
        cfg["lat"], cfg["lon"] = float(view["lat"]), float(view["lon"])
    else:
        back = float(view.get("back", 0.0))
        h = math.radians(heading)
        cfg["e"] = round(float(view.get("e", 0.0)) - back * math.sin(h), 3)
        cfg["n"] = round(float(view.get("n", 0.0)) - back * math.cos(h), 3)
    if "alt" in view:
        cfg["alt"] = float(view["alt"])
    else:
        cfg["agl"] = float(view["agl"])
    if view.get("under"):                       # in a tunnel: on its floor, the lowest flat surface (benchmark.lua)
        cfg["under"] = True
    cfg["dir"] = camera_dir(heading, pitch)
    return cfg


def tod_fields(place, defaults):
    """TimeOfDay fields for the place's fixed time (sky.lua's convention: `time` = local clock as a day
    fraction with noon = 0; utcOffset in hours). Without an explicit utcOffset the offset is lon / 15,
    i.e. local mean solar time: hour 10 puts the sun at the same hour angle everywhere."""
    date = _parse_date(place.get("date", defaults.get("date")))
    hour = float(place.get("hour", defaults.get("hour", 10.0)))
    off = place.get("utcOffset")
    off = round(place["lon"] / 15.0, 4) if off is None else float(off)
    return {"time": round((hour / 24.0 - 0.5) % 1.0, 6), "year": date.year, "month": date.month, "day": date.day,
            "utcOffset": off, "hour": hour, "date": date.isoformat()}


def plan(s, which="tuning", ids=None):
    """Every place of the run with its time, weather and resolved views."""
    d = s.get("defaults") or {}
    out = []
    for p in select_places(s, which, ids):
        out.append({
            "id": p["id"], "name": p.get("name", p["id"]), "lat": p["lat"], "lon": p["lon"],
            "group": p.get("group", ""), "heldout": bool(p.get("heldout")), "tags": p.get("tags", []),
            "time": tod_fields(p, d), "weather": p.get("weather", d.get("weather", "clear")),
            "views": [resolve_view(p, v) for v in p.get("views", d.get("views") or [])],
        })
    return out


# --- readiness and safety (pure) ----------------------------------------------------------------
def committed(p, anchor_before):
    """Has the teleport's anchor been committed (a new anchor, no request pending)?"""
    return p.get("anchorId") is not None and p.get("anchorId") != anchor_before and not p.get("pending")


def teleport_failed(p, anchor_before):
    """The teleport's request is gone without a new anchor: the server answered anchor_error (core.lua
    drops the pending request and keeps the anchor; a teleport is not retried). beamngEarth_core.teleport
    sets the pending request synchronously, so this cannot be a poll that came too early."""
    return bool(p.get("mod")) and not p.get("pending") and not committed(p, anchor_before)


def exact_ok(far):
    """The tile under the car is the exact (physics) level of the current anchor. Only mesh mode has
    near tiles; in terrain mode the TerrainBlock is the near ground, always exact."""
    if far.get("nearVisual") != "mesh":
        return True
    return far.get("underLevel") is not None and far.get("underLevel") == far.get("exactLevel") and not far.get("underOld")


def readiness(p, anchor_before=None, need_anchor=True, exact=True, backlog=False):
    """Why the place is not loaded yet (empty: ready). p = BNGE_BENCH.progress()."""
    if not p.get("mod"):
        return ["the BeamNG.Earth mod is not running"]
    reasons = []
    if p.get("pending"):
        reasons.append(f"anchor request pending ({p['pending']})")
    elif need_anchor and not committed(p, anchor_before):
        reasons.append("teleport not committed yet")
    if (p.get("messages") or {}).get("queued"):
        reasons.append(f"incoming messages: {p['messages']['queued']} waiting")
    far = p.get("far") or {}
    if not far.get("done"):
        reasons.append("far field: waiting for far_done")
    if far.get("queued"):
        reasons.append(f"far field: {far['queued']} tiles queued")
    if far.get("groups"):
        reasons.append(f"far field: {far['groups']} swap groups waiting")
    if far.get("transitions"):
        reasons.append(f"far field: {far['transitions']} transitions running")
    if exact and not exact_ok(far):
        reasons.append(f"near ground: L{far.get('underLevel')} under the car, exact is L{far.get('exactLevel')}"
                       + (" (previous anchor)" if far.get("underOld") else ""))
    roads = p.get("roads") or {}
    if roads.get("enabled") and roads.get("tiles") == 0:
        reasons.append("roads: no road tiles yet")             # a teleport's tiles are still being built
    elif roads.get("enabled") and roads.get("walksQueued"):
        reasons.append(f"roads: {roads['walksQueued']} road / sidewalk / building meshes to make")
    elif roads.get("enabled") and (roads.get("queued") or roads.get("deleting")):
        reasons.append(f"roads: {roads.get('queued', 0)} to make, {roads.get('deleting', 0)} to delete")
    elif roads.get("enabled") and roads.get("quietS") is not None and roads["quietS"] < ROADS_QUIET_S:
        reasons.append("roads: still arriving / settling")    # the engine fits new DecalRoads over frames
    if backlog and p.get("serverBacklog"):     # only fresh with earthd --status-interval 1
        reasons.append(f"server backlog {p['serverBacklog']}")
    return reasons


def check_info(info, force=False):
    """Problems that forbid a run (empty: go). info = BNGE_BENCH.info()."""
    bad = []
    if not info.get("mod"):
        bad.append("the BeamNG.Earth mod is not running (beamngEarth_core)")
    else:
        if info.get("net") != "connected":
            bad.append(f"the companion server is not connected ({info.get('net')})")
        if not info.get("active"):
            bad.append("the BeamNG.Earth world is not active (load the level or teleport once)")
        if info.get("pending"):
            bad.append(f"an anchor request is pending ({info['pending']}): wait for it")
    speed = info.get("speed")
    if speed is None:
        bad.append("no player vehicle")
    elif speed > MAX_SPEED and not force:
        bad.append(f"the player vehicle moves ({speed:.1f} m/s): park it, or pass --force")
    pl = info.get("player") or {}
    if not (_num(pl.get("lat")) and _num(pl.get("lon"))):
        bad.append("the player's lat/lon is unknown (needed to bring the car back)")
    if not info.get("saved"):
        bad.append("no saved state from the game")
    return bad


def counter_delta(a, b, key):
    va, vb = (a or {}).get(key), (b or {}).get(key)
    return vb - va if _num(va) and _num(vb) else None


# --- the game ------------------------------------------------------------------------------------
def lua_long(text):
    """A Lua long-bracket string literal holding `text` verbatim."""
    n = 0
    while ("]" + "=" * n + "]") in text:
        n += 1
    return "[" + "=" * n + "[" + text + "]" + "=" * n + "]"


class Game:
    """The running game through its MCP server (tools/mcp.py): run_lua and the screenshot tool."""

    def __init__(self):
        sys.path.insert(0, str(HERE))
        import mcp      # tools/mcp.py
        self.mcp = mcp

    @staticmethod
    def _transport(fn, what):
        """fn() with mcp.py's failures as RuntimeError (an RPC / tool error) or McpError (the request
        itself: timed out, refused, reset)."""
        try:
            return fn()
        except SystemExit as e:         # mcp.py reports RPC errors that way
            raise RuntimeError(str(e)) from None
        except urllib.error.URLError as e:          # before OSError: it is one
            timeout = isinstance(e.reason, (TimeoutError, socket.timeout))
            raise McpError(f"MCP {what}: {e.reason}", timeout=timeout) from None
        except (TimeoutError, socket.timeout):
            raise McpError(f"MCP {what}: timed out", timeout=True) from None
        except OSError as e:            # refused, reset, broken pipe, http.client's RemoteDisconnected
            raise McpError(f"MCP {what}: {e!r}") from None

    def lua(self, code, timeout=60):
        res = self._transport(lambda: self.mcp.rpc("tools/call", {"name": "run_lua", "arguments": {"code": code}},
                                                    timeout=timeout), "run_lua")
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise RuntimeError("lua: " + text[:400])
        return text

    def screenshot(self, jpg=True):
        getattr(self.mcp, "quiet_ui", lambda: None)()     # our welcome page out of the picture (fresh games)
        return self._transport(lambda: self.mcp.call("screenshot", {"jpg": bool(jpg)}),
                               "screenshot").splitlines()[-1].strip()


class LogWatcher:
    """New colour-match / de-shadow region lines of the server log (earthd --log-file), from the end
    of the file at start."""

    def __init__(self, path):
        self.f = open(path, errors="replace")
        self.f.seek(0, 2)

    def region_lines(self):
        return [ln.rstrip() for ln in self.f.readlines() if REGION_RE.search(ln)]

    def close(self):
        self.f.close()


def options(**kw):
    """The run options with their defaults (argparse fills the same names)."""
    o = dict(force=False, timeout=TIMEOUT_S, view_timeout=VIEW_TIMEOUT_S, home_timeout=HOME_TIMEOUT_S,
             settle=SETTLE_S, fps_seconds=FPS_S, poll=POLL_S, exact=True, backlog=False, png=False, server_log=None,
             region_quiet=REGION_QUIET_S, weather=None)
    o.update(kw)
    return argparse.Namespace(**o)


class Bench:
    """One run against the game. game: .lua(code) -> text and .screenshot(jpg) -> path; clock:
    .monotonic() and .sleep(s) (tests pass fakes)."""

    def __init__(self, game, opts, clock=time, log=print):
        self.game, self.opts, self.clock, self.log = game, opts, clock, log
        self.watcher = LogWatcher(opts.server_log) if opts.server_log else None
        self.last_region = None

    def load_chunk(self):
        if self.game.lua(LUA.read_text()) != "ok":
            raise BenchError("could not load tools/benchmark.lua")

    def mcp(self, what, fn):
        """fn() again after a timed-out MCP request, up to MCP_RETRIES times (a game stall, e.g. shader
        compiles at a new place; every BNGE_BENCH call and a screenshot are safe to repeat). An
        unreachable game (refused, reset) is not retried."""
        for attempt in range(MCP_RETRIES + 1):
            try:
                return fn()
            except McpError as e:
                if not e.timeout or attempt == MCP_RETRIES:
                    raise
                self.log(f"  {what}: {e}; sending it again")
                self.clock.sleep(self.opts.poll)

    def call(self, fn, *args, _retry=True):
        code = f"return BNGE_BENCH.{fn}({', '.join(lua_long(json.dumps(a)) for a in args)})"
        try:
            text = self.mcp(fn, lambda: self.game.lua(code))
        except McpError:
            raise
        except RuntimeError as e:
            if _retry and "BNGE_BENCH" in str(e):     # GE Lua was reloaded: send the chunk again, once
                self.load_chunk()
                return self.call(fn, *args, _retry=False)
            raise
        try:
            out = json.loads(text)
        except ValueError:
            raise BenchError(f"{fn}: unexpected answer {text[:200]!r}") from None
        if not isinstance(out, dict):
            raise BenchError(f"{fn}: unexpected answer {text[:200]!r}")
        return out

    # -- loading
    def drain_regions(self, t):
        if self.watcher and self.watcher.region_lines():
            self.last_region = t

    def wait_ready(self, t0, timeout, anchor_before=None, need_anchor=True):
        """Poll progress until ready on STABLE_POLLS polls in a row (ready_s = the first of them) or
        `timeout` s after t0. Returns the timings, the last progress and the reasons left."""
        res = {"timedOut": False, "polls": 0}
        streak, first = 0, None
        start_t = self.clock.monotonic()
        while True:
            p = self.call("progress")
            now = self.clock.monotonic() - t0
            res["polls"] += 1
            self.drain_regions(self.clock.monotonic())
            far = p.get("far") or {}
            if need_anchor and teleport_failed(p, anchor_before):
                res["failed"], res["progress"], res["reasons"] = True, p, [f"teleport failed: {p.get('status')}"]
                break
            if need_anchor and committed(p, anchor_before):
                if "anchor_s" not in res:
                    res["anchor_s"], res["atCommit"] = now, far
                if far.get("done") and "farDone_s" not in res:
                    res["farDone_s"] = now
                if exact_ok(far) and "exact_s" not in res:
                    res["exact_s"] = now
            reasons = readiness(p, anchor_before, need_anchor, self.opts.exact, self.opts.backlog)
            res["progress"], res["reasons"] = p, reasons
            if not reasons:
                streak += 1
                first = now if streak == 1 else first
                if streak >= STABLE_POLLS:
                    res["ready_s"] = first
                    break
            else:
                streak = 0
            if self.clock.monotonic() - start_t >= timeout:
                res["timedOut"] = True
                break
            self.clock.sleep(self.opts.poll)
        return res

    def wait_regions(self, deadline):
        """--server-log: until no region line for region_quiet s (or the deadline). Returns s waited."""
        if not self.watcher:
            return None
        t0 = self.clock.monotonic()
        while True:
            now = self.clock.monotonic()
            self.drain_regions(now)
            if self.last_region is None or now - self.last_region >= self.opts.region_quiet or now >= deadline:
                return round(now - t0, 2)
            self.clock.sleep(self.opts.poll)

    def screenshot(self, dest_stem):
        src = self.mcp("screenshot", lambda: self.game.screenshot(jpg=not self.opts.png))
        last = -1
        for _ in range(100):                    # written asynchronously within a few frames
            size = os.path.getsize(src) if os.path.exists(src) else 0
            if size > 0 and size == last:
                break
            last = size
            self.clock.sleep(0.2)
        else:
            raise BenchError(f"screenshot {src} was not written")
        dest = f"{dest_stem}{os.path.splitext(src)[1] or '.png'}"
        shutil.copyfile(src, dest)
        return dest

    # -- one place
    def place(self, pl, out_dir):
        o = self.opts
        rec = {k: pl[k] for k in ("id", "name", "lat", "lon", "group", "heldout", "tags")}
        rec.update({"time": pl["time"], "weather": pl["weather"], "views": [], "ok": False})
        before = self.call("progress")
        anchor_before, far0 = before.get("anchorId"), before.get("far") or {}
        self.drain_regions(None)                # lines of the previous place are not this one's
        self.last_region = None
        t0 = self.clock.monotonic()
        tp = self.call("teleport", {"lat": pl["lat"], "lon": pl["lon"]})
        if not tp.get("ok"):
            rec["error"] = f"teleport refused: {tp.get('error') or tp.get('status')}"
            return rec
        self.last_region = t0
        w = self.wait_ready(t0, o.timeout, anchor_before)
        far_ready = (w["progress"].get("far") or {})
        commit = w.get("atCommit") or far0
        rec["load"] = {k: (round(w[k], 2) if k in w else None) for k in ("anchor_s", "farDone_s", "exact_s", "ready_s")}
        rec["load"].update({"timedOut": w["timedOut"], "reasons": w["reasons"], "polls": w["polls"]})
        rec["status"] = w["progress"].get("status")
        # without the new anchor the camera would show the previous place under this one's id
        if w.get("failed"):
            rec["error"] = w["reasons"][0]
            return rec
        if "anchor_s" not in w:
            rec["error"] = f"teleport not committed after {o.timeout:.0f} s"
            return rec
        self.call("zeroInputs")                 # the teleport reset the vehicle (spawn.safeTeleport): park it again
        rec["load"]["regionsWait_s"] = self.wait_regions(self.clock.monotonic() + o.timeout)
        rec["tiles"] = {"live": far_ready.get("live"), "covered": far_ready.get("covered"),
                        "materials": far_ready.get("materials"),
                        "imports": counter_delta(far0, far_ready, "imports"),
                        "failed": counter_delta(far0, far_ready, "failed"),
                        "textured": counter_delta(far0, far_ready, "textured"),
                        "pops": counter_delta(far0, far_ready, "pops")}
        rec["exact"] = {"missLoad": counter_delta(commit, far_ready, "exactMiss"),
                        "framesLoad": counter_delta(commit, far_ready, "exactFrames"),
                        "underLevel": far_ready.get("underLevel"), "exactLevel": far_ready.get("exactLevel"),
                        "nearVisual": far_ready.get("nearVisual")}
        player = (w["progress"].get("player") or {})
        rec["ground_z"] = player.get("z")
        if w["timedOut"]:
            self.log(f"  {pl['id']}: not ready after {o.timeout:.0f} s ({'; '.join(w['reasons'])}): shots taken anyway")
        rec["sky"] = self.call("setTime", pl["time"]).get("sky")
        weather = o.weather or pl["weather"]
        if weather == "clear":
            rec["weatherSet"] = self.call("setWeather", CLEAR)
        for v in pl["views"]:
            try:
                rec["views"].append(self.view(pl, v, out_dir))
            except BenchError as e:             # this view only (e.g. a screenshot never written)
                rec["views"].append({"name": v["name"], "error": str(e), "camera": v})
        end = self.call("progress")
        far_end = end.get("far") or {}
        rec["exact"]["missAfter"] = counter_delta(far_ready, far_end, "exactMiss")
        rec["exact"]["framesAfter"] = counter_delta(far_ready, far_end, "exactFrames")
        rec["ok"] = not w["timedOut"] and all(v.get("file") for v in rec["views"])
        return rec

    def view(self, pl, v, out_dir):
        o = self.opts
        r = self.call("view", v)
        if not r.get("ok"):
            return {"name": v["name"], "error": r.get("error"), "camera": v}
        self.clock.sleep(o.settle)
        t0 = self.clock.monotonic()
        w = self.wait_ready(t0, o.view_timeout, need_anchor=False)
        self.clock.sleep(o.settle if w["polls"] > STABLE_POLLS else 0)   # something loaded: settle again
        self.call("perfStart")
        self.clock.sleep(o.fps_seconds)
        perf = self.call("perfRead")
        dest = self.screenshot(os.path.join(out_dir, f"{pl['id']}__{v['name']}"))
        return {"name": v["name"], "file": os.path.basename(dest), "camera": v,
                "placed": {k: r.get(k) for k in ("x", "y", "z", "ground", "groundFrom")},
                "wait_s": round(self.clock.monotonic() - t0, 2), "timedOut": w["timedOut"], "reasons": w["reasons"],
                "perf": {k: perf.get(k) for k in ("fps", "avgMs", "p50", "p95", "p99", "maxMs", "over33", "over50",
                                                   "over100", "frames", "simRatio", "focused", "fpsLimit")}}

    # -- restore
    def go_home(self, home):
        before = self.call("progress")
        t0 = self.clock.monotonic()
        tp = self.call("teleport", {"lat": home["lat"], "lon": home["lon"]})
        if not tp.get("ok"):
            raise BenchError(f"teleport home refused: {tp.get('error') or tp.get('status')}")
        while True:
            p = self.call("progress")
            if committed(p, before.get("anchorId")):
                return round(self.clock.monotonic() - t0, 2)
            if teleport_failed(p, before.get("anchorId")):
                raise BenchError(f"teleport home failed: {p.get('status')}")
            if self.clock.monotonic() - t0 >= self.opts.home_timeout:
                raise BenchError(f"teleport home not committed after {self.opts.home_timeout:.0f} s")
            self.clock.sleep(self.opts.poll)

    def driving(self):
        """The player vehicle's speed when it moves faster than MAX_SPEED on two polls in a row (the
        user is driving), else None."""
        speed = None
        for i in range(2):
            if i:
                self.clock.sleep(self.opts.poll)
            speed = (self.call("progress").get("player") or {}).get("speed")
            if not (_num(speed) and speed > MAX_SPEED):
                return None
        return speed

    def restore(self, saved, home, leave_car=None):
        """Weather, sky and perfmon, then the player back home and its inputs zeroed, then the camera,
        whatever fails on the way: each step in its own try, which also catches Ctrl-C / SIGTERM (a
        second interrupt while restoring skips that step only). leave_car: why the car is not
        touched (the user is driving it): no teleport home, no inputs."""
        res = {"errors": []}

        def step(name, fn):
            try:
                return fn()
            except KeyboardInterrupt:
                res["errors"].append(f"{name}: interrupted")
            except Exception as e:      # noqa: BLE001 (every step is tried; the errors are reported)
                res["errors"].append(f"{name}: {e}")
            return None

        r = step("weather/sky", lambda: self.call("restore", saved, {"weather": True, "sky": True, "perfmon": True}))
        if r is not None:
            res["errors"] += r.get("errors") or []
            res["weather"], res["sky"] = r.get("weather"), r.get("sky")
        if leave_car:
            res["home"] = f"skipped: {leave_car}"
        else:
            if home:
                res["home_s"] = step("home", lambda: self.go_home(home))
            step("inputs", lambda: self.call("zeroInputs"))
        r = step("camera", lambda: self.call("restore", saved, {"cam": True}))
        if r is not None:
            res["errors"] += r.get("errors") or []
            res["camera"] = r.get("cam")
        res["ok"] = not res["errors"]
        return res


def git_info():
    def git(*a):
        r = subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    try:
        return {"commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}
    except (OSError, subprocess.SubprocessError):
        return None


def run_dir(base, label, today=None):
    """<base>/<date>-<label>, with -2, -3 ... if it exists (never overwritten)."""
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "run"
    stem = f"{(today or datetime.date.today()).isoformat()}-{label}"
    p, n = Path(base) / stem, 2
    while p.exists():
        p, n = Path(base) / f"{stem}-{n}", n + 1
    return p


def _write_json(path, obj):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def run(game, places, out_dir, opts, clock=time, log=print, meta=None):
    """The whole run. Returns the run record (also written to <out_dir>/run.json)."""
    b = Bench(game, opts, clock, log)
    b.load_chunk()
    info = b.call("info")
    bad = check_info(info, opts.force)
    if bad:
        raise BenchError("not now: " + "; ".join(bad))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    saved, home = info["saved"], {"lat": info["player"]["lat"], "lon": info["player"]["lon"]}
    _write_json(out_dir / "state.json", {"saved": saved, "home": home})
    rec = {"version": 1, "started": datetime.datetime.now().isoformat(timespec="seconds"), **(meta or {}),
           "options": {k: v for k, v in vars(opts).items()}, "home": home,
           "game": {k: info.get(k) for k in ("level", "nearVisual", "session", "status")}, "places": []}
    _write_json(out_dir / "run.json", rec)
    leave_car = None
    try:
        b.call("zeroInputs")
        for i, pl in enumerate(places):
            # the user may take the car back during a run: no teleport from under them (--force: the
            # car may move)
            speed = b.driving() if i and not opts.force else None
            if speed is not None:
                leave_car = f"the user is driving ({speed:.1f} m/s)"
                rec["aborted"] = f"stopped before {pl['id']}: {leave_car}"
                log(rec["aborted"])
                break
            log(f"{pl['id']} ({pl['lat']}, {pl['lon']}) ...")
            t0 = clock.monotonic()
            try:
                pr = b.place(pl, str(out_dir))
            except BenchError as e:            # this place only (an MCP failure is not a BenchError)
                pr = {"id": pl["id"], "name": pl["name"], "ok": False, "error": str(e)}
            pr["wall_s"] = round(clock.monotonic() - t0, 1)
            rec["places"].append(pr)
            _write_json(out_dir / "run.json", rec)
            log("  " + summary_line(pr))
    finally:
        if leave_car is None and not opts.force:    # driving off during the last place: no teleport home either
            try:
                speed = b.driving()
                leave_car = f"the user is driving ({speed:.1f} m/s)" if speed is not None else None
            except (Exception, KeyboardInterrupt):  # noqa: BLE001 (the game may be gone: the restore reports it)
                pass
        rec["restore"] = b.restore(saved, home, leave_car)
        rec["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        _write_json(out_dir / "run.json", rec)
        if b.watcher:
            b.watcher.close()
        r = rec["restore"]
        log("restored" if r["ok"] else "RESTORE PROBLEMS: " + "; ".join(r["errors"]))
    return rec


def summary_line(pr):
    if pr.get("error"):
        return f"FAILED: {pr['error']}"
    ld, t = pr.get("load") or {}, pr.get("tiles") or {}
    parts = [f"ready {ld.get('ready_s')} s" if not ld.get("timedOut") else "NOT READY",
             f"{t.get('live')} tiles", f"exactMiss {(pr.get('exact') or {}).get('missLoad')} loading"]
    for v in pr.get("views", []):
        p = v.get("perf") or {}
        if v.get("error"):
            parts.append(f"{v['name']} FAILED {v['error']}")
            continue
        note = ""
        if p.get("focused") is False:
            cap = (p.get("fpsLimit") or {}).get("fpsCap")
            note = f" (window not focused: active cap {cap:g} fps)" if _num(cap) else " (window not focused)"
        parts.append(f"{v['name']} {p.get('fps') or 0:.0f} fps p95 {p.get('p95') or 0:.1f} ms" + note)
    return ", ".join(parts)


# --- compare (no game) ------------------------------------------------------------------------------
def load_rgb(path, width=None):
    """float64 H x W x 3, resized (Lanczos) to `width` px wide when it is wider."""
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        if width and im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        return np.asarray(im, dtype=np.float64)


def luminance(rgb):
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def box_mean(a, k):
    """Mean over every k x k window (valid part only), by summed-area table."""
    import numpy as np
    c = np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def ssim(a, b, k=SSIM_K, peak=255.0):
    """Mean SSIM (Wang et al. 2004: C1 = (0.01 L)^2, C2 = (0.03 L)^2) over k x k box windows of two
    equally sized grey images. 1 = identical."""
    if a.shape[0] < k or a.shape[1] < k:
        k = max(1, min(a.shape))
    c1, c2 = (0.01 * peak) ** 2, (0.03 * peak) ** 2
    ma, mb = box_mean(a, k), box_mean(b, k)
    va, vb = box_mean(a * a, k) - ma * ma, box_mean(b * b, k) - mb * mb
    cov = box_mean(a * b, k) - ma * mb
    m = ((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2))
    return float(m.mean())


def heat(dy):
    """|delta luminance| (0..255) -> an RGB uint8 heat map (black, red, yellow, white; x4 gain)."""
    import numpy as np
    t = np.clip(dy * 4.0 / 255.0, 0.0, 1.0) * 3.0
    rgb = np.stack([np.clip(t, 0, 1), np.clip(t - 1, 0, 1), np.clip(t - 2, 0, 1)], axis=-1)
    return (rgb * 255).round().astype(np.uint8)


def image_metrics(path_a, path_b, width=METRIC_WIDTH, diff_path=None):
    """mad (mean |A - B| over RGB, 0-255), ssim (luminance), changed (share of pixels with |dY| >
    CHANGED_DY), at `width` px wide; B is resized to A's size when they differ. Optionally writes the
    heat map of |dY|."""
    import numpy as np
    from PIL import Image
    a = load_rgb(path_a, width)
    b = load_rgb(path_b, width)
    resized = a.shape != b.shape
    if resized:
        with Image.open(path_b) as im:
            b = np.asarray(im.convert("RGB").resize((a.shape[1], a.shape[0]), Image.LANCZOS), dtype=np.float64)
    ya, yb = luminance(a), luminance(b)
    dy = np.abs(ya - yb)
    out = {"mad": round(float(np.abs(a - b).mean()), 3), "ssim": round(ssim(ya, yb), 4),
           "changed": round(float((dy > CHANGED_DY).mean()), 4), "size": [a.shape[1], a.shape[0]], "resized": resized}
    if diff_path:
        Image.fromarray(heat(dy)).save(diff_path)
    return out


def get_path(d, dotted):
    for k in dotted.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def number_delta(key, a, b):
    """{a, b, delta, pct, verdict}: verdict 'worse' / 'better' beyond TOLERANCE[key], else 'same'
    (or None when the number has no direction or a side is missing)."""
    out = {"a": a, "b": b, "delta": None, "pct": None, "verdict": None}
    if not (_num(a) and _num(b)):
        return out
    out["delta"] = round(b - a, 4)
    out["pct"] = round(100.0 * (b - a) / abs(a), 2) if a else None
    rel, absol, better = TOLERANCE.get(key, (None, None, "none"))
    if better == "none":
        return out
    bad = (b - a) if better == "lower" else (a - b)
    beyond = abs(b - a) > absol and (not a or abs(b - a) / abs(a) > rel)
    out["verdict"] = "same" if not beyond else ("worse" if bad > 0 else "better")
    return out


def flag_delta(a, b, bad):
    """{a, b, delta, pct, verdict} of a yes/no outcome (PLACE_FLAGS): 'worse' when only B has the bad
    value, 'better' when only A has it (a place that never got ready has no ready_s to compare)."""
    out = {"a": a, "b": b, "delta": None, "pct": None, "verdict": None}
    if isinstance(a, bool) and isinstance(b, bool):
        out["verdict"] = "same" if a == b else ("worse" if b == bad else "better")
    return out


def place_numbers(A, B):
    nums = {k: flag_delta(get_path(A, k), get_path(B, k), bad) for k, bad in PLACE_FLAGS}
    nums.update({k: number_delta(k, get_path(A, k), get_path(B, k)) for k in PLACE_NUMBERS})
    return nums


def camera_differs(x, y):
    """Both shots carry their camera (run.json) and they differ: the set moved it between the runs."""
    return x.get("camera") is not None and y.get("camera") is not None and x["camera"] != y["camera"]


def shot_changed(m, ssim_flag=SSIM_FLAG, changed_flag=CHANGED_FLAG):
    """A shot pair differs beyond the noise: a low mean SSIM (a change all over), or many changed
    pixels (a local change the mean hides)."""
    return m["ssim"] < ssim_flag or m["changed"] > changed_flag


def _thumb(src, dest, width=THUMB_WIDTH):
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        im.save(dest, quality=85)


def load_run(path):
    path = Path(path)
    with open(path / "run.json") as f:
        return json.load(f)


def compare(run_a, run_b, out_dir, width=METRIC_WIDTH, log=print, ssim_flag=SSIM_FLAG, changed_flag=CHANGED_FLAG):
    """Side by side of two runs (see the module doc). Returns the report (also compare.json)."""
    run_a, run_b, out_dir = Path(run_a), Path(run_b), Path(out_dir)
    ra, rb = load_run(run_a), load_run(run_b)
    (out_dir / "img").mkdir(parents=True, exist_ok=True)
    pa = {p["id"]: p for p in ra.get("places", [])}
    pb = {p["id"]: p for p in rb.get("places", [])}
    order = [i for i in pa] + [i for i in pb if i not in pa]
    rep = {"a": str(run_a), "b": str(run_b), "labels": [ra.get("label"), rb.get("label")],
           "metricWidth": width, "ssimFlag": ssim_flag, "changedFlag": changed_flag, "places": []}
    for pid in order:
        A, B = pa.get(pid), pb.get(pid)
        row = {"id": pid, "name": (A or B).get("name", pid), "inA": A is not None, "inB": B is not None,
               "numbers": {}, "views": []}
        if A and B:
            row["numbers"] = place_numbers(A, B)
        va = {v["name"]: v for v in (A or {}).get("views", [])}
        vb = {v["name"]: v for v in (B or {}).get("views", [])}
        for name in [n for n in va] + [n for n in vb if n not in va]:
            x, y = va.get(name), vb.get(name)
            vr = {"name": name, "fileA": None, "fileB": None, "metrics": None, "numbers": {}}
            stem = f"{pid}__{name}"
            for side, v, base in (("A", x, run_a), ("B", y, run_b)):
                if v and v.get("file") and (base / v["file"]).exists():
                    t = f"img/{side}_{stem}.jpg"
                    _thumb(base / v["file"], out_dir / t)
                    vr["file" + side] = t
            if vr["fileA"] and vr["fileB"]:
                vr["diff"] = f"img/diff_{stem}.png"
                vr["metrics"] = image_metrics(run_a / x["file"], run_b / y["file"], width, out_dir / vr["diff"])
                _thumb(out_dir / vr["diff"], out_dir / vr["diff"])
                vr["changed"] = shot_changed(vr["metrics"], ssim_flag, changed_flag)
            if x and y:
                vr["numbers"] = {k: number_delta(k, get_path(x, k), get_path(y, k)) for k in VIEW_NUMBERS}
                notes = []
                if camera_differs(x, y):
                    notes.append("the camera differs between the runs (the set moved it): not comparable")
                if get_path(x, "perf.focused") is False or get_path(y, "perf.focused") is False:
                    notes.append("the game window was not focused in a run: frame times not comparable")
                    for n in vr["numbers"].values():
                        n["verdict"] = None
                if notes:
                    vr["note"] = "; ".join(notes)
            row["views"].append(vr)
            m = vr["metrics"]
            log(f"{pid:22s} {name:12s} " + (f"mad {m['mad']:6.2f}  ssim {m['ssim']:.4f}  changed {m['changed'] * 100:5.1f} %"
                                             if m else "missing in " + ("A" if not vr["fileA"] else "B")))
        rep["places"].append(row)
    rep["summary"] = summarize_compare(rep)
    if ra.get("setDigest") and rb.get("setDigest") and ra["setDigest"] != rb["setDigest"]:
        rep["summary"]["note"] = (f"the benchmark set differs between the runs (setDigest {ra['setDigest']} vs "
                                  f"{rb['setDigest']}): check the shots marked 'camera differs'")
        log(rep["summary"]["note"])
    _write_json(out_dir / "compare.json", rep)
    (out_dir / "index.html").write_text(compare_html(rep))
    s = rep["summary"]
    log(f"{s['shots']} shot pairs: mean mad {s['meanMad']}, mean ssim {s['meanSsim']}, {s['changedShots']} changed "
        f"(ssim < {ssim_flag} or > {changed_flag * 100:g} % of the pixels); {s['worse']} number(s) worse, {s['better']} better; {s['missing']} shot(s) missing")
    return rep


def summarize_compare(rep):
    ms = [v["metrics"] for p in rep["places"] for v in p["views"] if v.get("metrics")]
    verdicts = [n["verdict"] for p in rep["places"] for n in list(p["numbers"].values())
                + [x for v in p["views"] for x in v["numbers"].values()]]
    return {"shots": len(ms),
            "meanMad": round(sum(m["mad"] for m in ms) / len(ms), 3) if ms else None,
            "meanSsim": round(sum(m["ssim"] for m in ms) / len(ms), 4) if ms else None,
            "changedShots": sum(1 for p in rep["places"] for v in p["views"] if v.get("changed")),
            "missing": sum(1 for p in rep["places"] for v in p["views"] if not v.get("metrics")),
            "worse": verdicts.count("worse"), "better": verdicts.count("better")}


def _fmt(x):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.2f}" if abs(x) < 1000 else f"{x:.0f}"
    return str(x)


def _numbers_table(nums):
    rows = []
    for k, n in nums.items():
        cls = n.get("verdict") or ""
        pct = f" ({n['pct']:+.1f} %)" if n.get("pct") is not None else ""
        d = f"{n['delta']:+g}{pct}" if n.get("delta") is not None else "-"
        rows.append(f"<tr class='{cls}'><td>{html.escape(k)}</td><td>{_fmt(n.get('a'))}</td>"
                    f"<td>{_fmt(n.get('b'))}</td><td>{d}</td><td>{cls}</td></tr>")
    if not rows:
        return ""
    return ("<table class='num'><tr><th>number</th><th>A</th><th>B</th><th>delta</th><th></th></tr>"
            + "".join(rows) + "</table>")


def compare_html(rep):
    s = rep["summary"]
    e = html.escape
    mean_ssim = "-" if s["meanSsim"] is None else f"{s['meanSsim']:.4f}"
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Benchmark compare</title><style>
:root {{ --bg: #fafaf7; --fg: #1d1d1b; --muted: #6b6b66; --line: #ddd9cf; --worse: #b3261e; --better: #1b6e3a; --card: #fff; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #161615; --fg: #ecebe6; --muted: #9b9a93; --line: #34332f;
  --worse: #ff8a80; --better: #7bd88f; --card: #1f1f1d; }} }}
body {{ background: var(--bg); color: var(--fg); font: 14px/1.45 system-ui, sans-serif; margin: 0 auto;
  padding: 16px; max-width: 1600px; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }} h2 {{ font-size: 16px; margin: 28px 0 6px; }}
.muted {{ color: var(--muted); }} code {{ font-size: 12px; }}
.grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }}
@media (max-width: 800px) {{ .grid {{ grid-template-columns: 1fr; }} }}
figure {{ margin: 0; background: var(--card); border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }}
figure img {{ width: 100%; display: block; }} figcaption {{ padding: 4px 8px; font-size: 12px; color: var(--muted); }}
.shot {{ margin: 10px 0 18px; }} .metrics {{ margin: 4px 0; font-variant-numeric: tabular-nums; }}
.changed {{ color: var(--worse); font-weight: 600; }}
table.num {{ border-collapse: collapse; margin: 6px 0; font-variant-numeric: tabular-nums; font-size: 13px; }}
table.num td, table.num th {{ border-bottom: 1px solid var(--line); padding: 2px 10px 2px 0; text-align: left; }}
tr.worse td {{ color: var(--worse); }} tr.better td {{ color: var(--better); }}
.empty {{ padding: 40px 8px; text-align: center; color: var(--muted); }}
</style></head><body>
<h1>Benchmark compare</h1>
<p class="muted">A: <code>{e(rep['a'])}</code><br>B: <code>{e(rep['b'])}</code><br>
{s['shots']} shot pairs, mean MAD {_fmt(s['meanMad'])}, mean SSIM {mean_ssim},
{s['changedShots']} changed (SSIM &lt; {rep['ssimFlag']} or more than {rep['changedFlag'] * 100:g} % of the pixels
changed by more than {CHANGED_DY} in luminance); numbers: {s['worse']} worse, {s['better']} better; {s['missing']} shot(s) missing.
Metrics at {rep['metricWidth']} px wide; heat map = |&Delta; luminance| x4.</p>"""]
    if s.get("note"):
        parts.append(f"<p class='changed'>{e(s['note'])}</p>")
    for p in rep["places"]:
        miss = "" if p["inA"] and p["inB"] else f" <span class='changed'>(only in {'A' if p['inA'] else 'B'})</span>"
        parts.append(f"<h2>{e(p['name'])} <span class='muted'>{e(p['id'])}</span>{miss}</h2>")
        parts.append(_numbers_table(p["numbers"]))
        for v in p["views"]:
            m = v.get("metrics")
            mt = (f"MAD {m['mad']:.2f} &middot; SSIM {m['ssim']:.4f} &middot; changed {m['changed'] * 100:.1f} %"
                  + (" &middot; <span class='changed'>changed</span>" if v.get("changed") else "")
                  + (" &middot; B resized" if m.get("resized") else "")) if m else "no pair"
            cells = []
            for side, key in (("A", "fileA"), ("B", "fileB"), ("difference", "diff")):
                src = v.get(key)
                body = (f'<a href="{e(src)}"><img loading="lazy" src="{e(src)}" alt="{side}"></a>' if src
                        else '<div class="empty">missing</div>')
                cells.append(f"<figure>{body}<figcaption>{side}</figcaption></figure>")
            parts.append(f"<div class='shot'><div><b>{e(v['name'])}</b> <span class='metrics'>{mt}</span></div>"
                         f"<div class='grid'>{''.join(cells)}</div>{_numbers_table(v['numbers'])}"
                         + (f"<p class='changed'>{e(v['note'])}</p>" if v.get("note") else "") + "</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


# --- CLI ------------------------------------------------------------------------------------------
def print_plan(pl):
    for p in pl:
        t = p["time"]
        print(f"{p['id']:22s} {p['group']:7s} {'HELD-OUT' if p['heldout'] else '        '} "
              f"{p['lat']:10.5f} {p['lon']:11.5f}  {t['date']} {t['hour']:05.2f} (UTC{t['utcOffset']:+g})  "
              f"{p['weather']:5s}  " + ", ".join(v["name"] for v in p["views"]))
    print(f"{len(pl)} places, {sum(len(p['views']) for p in pl)} shots")


def _sigterm(*_):
    raise KeyboardInterrupt("SIGTERM")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--benchmarks", default=str(SET_PATH), help="the benchmark set (docs/benchmarks.json)")
        sp.add_argument("--set", dest="which", default="tuning", choices=("tuning", "heldout", "all"))
        sp.add_argument("--places", default=None, help="comma-separated place ids (any group; overrides --set)")
    rp = sub.choices["run"]
    rp.add_argument("--label", required=True, help="run name: <date>-<label> under --runs")
    rp.add_argument("--runs", default=str(RUNS_DIR))
    rp.add_argument("--out", default=None, help="the run folder (default <runs>/<date>-<label>)")
    rp.add_argument("--force", action="store_true", help="run even while the player vehicle moves")
    rp.add_argument("--timeout", type=float, default=TIMEOUT_S, help="s per place to get ready")
    rp.add_argument("--view-timeout", type=float, default=VIEW_TIMEOUT_S)
    rp.add_argument("--home-timeout", type=float, default=HOME_TIMEOUT_S)
    rp.add_argument("--settle", type=float, default=SETTLE_S)
    rp.add_argument("--fps-seconds", type=float, default=FPS_S)
    rp.add_argument("--poll", type=float, default=POLL_S)
    rp.add_argument("--no-exact", dest="exact", action="store_false", help="do not wait for the exact tile under the car")
    rp.add_argument("--wait-backlog", dest="backlog", action="store_true",
                    help="also wait for the server's backlog 0 (run earthd with --status-interval 1)")
    rp.add_argument("--png", action="store_true", help="PNG screenshots (default JPG: a 4K PNG is ~15 MB)")
    rp.add_argument("--server-log", default=None, help="earthd's log file: also wait for colour-match / de-shadow regions")
    rp.add_argument("--region-quiet", type=float, default=REGION_QUIET_S)
    rp.add_argument("--weather", default=None, choices=("clear", "keep"), help="override every place's weather")
    sub.add_parser("check")
    cp = sub.add_parser("compare")
    cp.add_argument("run_a")
    cp.add_argument("run_b")
    cp.add_argument("--out", default=None, help="default <runs>/compare-<A>-vs-<B>")
    cp.add_argument("--metric-width", type=int, default=METRIC_WIDTH)
    cp.add_argument("--ssim-flag", type=float, default=SSIM_FLAG,
                    help="a shot below this SSIM is 'changed' (set it from two runs of the same build)")
    cp.add_argument("--changed-flag", type=float, default=CHANGED_FLAG,
                    help="... or with more than this share of its pixels changed (0-1)")
    sp = sub.add_parser("restore")
    sp.add_argument("run", help="a run folder (its state.json)")
    a = ap.parse_args(argv)

    if a.cmd == "compare":
        A, B = Path(a.run_a), Path(a.run_b)
        out = a.out or str(RUNS_DIR / f"compare-{A.name}-vs-{B.name}")
        compare(A, B, out, a.metric_width, ssim_flag=a.ssim_flag, changed_flag=a.changed_flag)
        print(f"wrote {out}/index.html")
        return 0
    if a.cmd == "check":
        b = Bench(Game(), options())
        b.load_chunk()
        info = b.call("info")
        bad = check_info(info)
        print(json.dumps({"go": not bad, "problems": bad, "info": info}, indent=1))
        return 0 if not bad else 1
    if a.cmd == "restore":
        with open(Path(a.run) / "state.json") as f:
            state = json.load(f)
        b = Bench(Game(), options())
        b.load_chunk()
        r = b.restore(state["saved"], state["home"])
        print(json.dumps(r, indent=1))
        return 0 if r["ok"] else 1
    s = load_set(a.benchmarks)
    ids = [x.strip() for x in a.places.split(",") if x.strip()] if a.places else None
    pl = plan(s, a.which, ids)
    if a.cmd == "plan":
        print_plan(pl)
        return 0
    opts = options(force=a.force, timeout=a.timeout, view_timeout=a.view_timeout, home_timeout=a.home_timeout,
                   settle=a.settle, fps_seconds=a.fps_seconds, poll=a.poll, exact=a.exact, backlog=a.backlog, png=a.png,
                   server_log=a.server_log, region_quiet=a.region_quiet, weather=a.weather)
    out = Path(a.out) if a.out else run_dir(a.runs, a.label)
    meta = {"label": a.label, "set": os.path.relpath(a.benchmarks, ROOT), "setDigest": set_digest(a.benchmarks),
            "which": a.which if not ids else "places", "git": git_info()}
    signal.signal(signal.SIGTERM, _sigterm)
    print(f"run -> {out} ({len(pl)} places)")
    rec = run(Game(), pl, out, opts, meta=meta)
    bad = [p["id"] for p in rec["places"] if not p.get("ok")]
    print(f"done: {len(rec['places']) - len(bad)}/{len(rec['places'])} places ok"
          + (f" (not ok: {', '.join(bad)})" if bad else "") + (f"; {rec['aborted']}" if rec.get("aborted") else ""))
    return 0 if not bad and not rec.get("aborted") and rec["restore"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
