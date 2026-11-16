"""Anonymous usage reports (the user, 2026-11-06: "create a telemetry system. Have it track current players and a way to
monitor the stress we're putting on the data sources"; on by default with an opt-out, country only).

Every REPORT_S the server sends https://earth.tgforever.net/telemetry (the project's server-list service, mini PC):

    {"id": a random install id (TELEMETRY_ID file in the cache; no name, account or hardware in it),
     "v": the app version, "os": windows / linux / deck / macos, "game": a game connected now, "mp": in BeamMP,
     "interval": REPORT_S, "rid": a random id of this server run, "seq": the report's number in the run,
     "sources": {host: {"req", "mb", "ok", "e4xx", "e404", "e429", "e5xx", "err", "retry", "deferred", "waited",
                        "waitMs", "limitFail", "budget"}},   (since the last report)
     "dem": {"<source>:<caller>:<zoom>:<outcome>": n}}   (the elevation tiles' raw reads, under the network switch;
                                                         dem.py _note: which module, no position)

A report whose answer did not come is sent again unchanged (its rid and seq) before any new counts, for as long as it
takes, so a collector that got it the first time counts it once (never under another seq); one the collector refuses
outright (a 4xx but 408 / 429) is dropped.

Nothing else: no place, no position, no IP kept (the collector derives the country from the address and drops it).
Off with the settings page's Anonymous usage statistics switch (read before every report), BNE_TELEMETRY=off (or
earthd --telemetry off), and always in --offline mode. Failures are silent: a report that
cannot be sent is dropped (its counts go into the next one).

The per-host counts are every process's (netfetch.process_totals: the worker processes write theirs to
BNE_NETSTATS_PROC, this run's own folder: run_proc_dir; this sums what changed since the last report).
"""
import json
import logging
import os
import platform
import secrets
import threading
import time
import urllib.error
import urllib.request

import netfetch

log = logging.getLogger("earthd.telemetry")

URL = os.environ.get("BNE_TELEMETRY_URL", "https://earth.tgforever.net/telemetry")
ON = os.environ.get("BNE_TELEMETRY", "on").lower() not in ("off", "0", "false", "no")
# the welcome page's choice this version asks for (settings telemetryChoice): nothing is sent before it. 2: the
# performance data is sent (from development builds first), everyone who chose under 1 is asked again. 3 (2026-11-11):
# new kinds of performance data (each slow frame's context, time since a teleport, idle time, the game's and the system's
# memory and VRAM in use, the server's memory and worker count: TELEMETRY.md), everyone asked again
CONSENT_VERSION = 3
# The performance section (perfstats.py; the collector reads it: native/serverlist perf.rs) is sent by development
# builds (the user, 2026-11-08: "start sending performance data on the dev builds. No release tonight") and, from the
# release after that, releases too (the user, 2026-11-08: "releases should be the same as dev"): True = every build,
# "dev" = only a version with ".dev." / "-dev." in it (release.yml's development builds); off: measured and kept beside
# the last report (LAST_FILE "perf_not_sent_yet"). Always only under the player's Performance data choice.
PERF_UPLOAD = True


def perf_upload(version):
    """Whether this build sends the performance section."""
    if PERF_UPLOAD is True:
        return True
    v = str(version or "")
    return PERF_UPLOAD == "dev" and (".dev." in v or "-dev." in v)
LAST_FILE = "telemetry_last.json"   # the last report as sent (the settings page's "What is sent")
REPORT_S = 300.0            # s between reports (the collector counts an install online for 2 of them)
FIRST_S = 60.0              # s after the start before the first
TIMEOUT_S = 10.0
ID_FILE = "telemetry_id"
PROC_KEEP_S = 3600.0        # a dead process's totals file is removed this long after its last write (and an old
                            # run's folder this long after its last change)
DEM_KEYS = 64               # DEM detail keys a report holds at most (the rest summed under "other"; ~3 KB: the
                            # collector takes 16 KB a report)
REJECTED = "rejected"       # send()'s answer for a report the collector refused outright (True: taken; False: no
                            # answer, perhaps taken: sent again unchanged)
KEYS = {"req": ("requests",), "mb": ("bytes",), "ok": ("s2xx",), "e4xx": ("s4xx",), "e404": ("s404",),
        "e429": ("s429",), "e5xx": ("s5xx",), "err": ("errors",), "retry": ("retries",),
        # our own limits: background requests dropped (deferred), demand ones that waited (and ms) or were refused,
        # daily-budget refusals (netfetch COUNTERS)
        "deferred": ("deferred",), "waited": ("waited",), "waitMs": ("wait_ms",), "limitFail": ("limit_fail",),
        "budget": ("budget",)}


def install_id(cache_dir):
    """The install's random id (created once in the cache; a cleared cache is a new install)."""
    path = os.path.join(cache_dir, ID_FILE)
    try:
        with open(path, encoding="ascii") as fh:
            v = fh.read().strip()
        if len(v) == 32 and all(c in "0123456789abcdef" for c in v):
            return v
    except OSError:
        pass
    v = secrets.token_hex(16)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path + ".tmp", "w", encoding="ascii") as fh:
            fh.write(v)
        os.replace(path + ".tmp", path)
    except OSError as e:
        log.debug("telemetry id: %s", e)
    return v


def run_proc_dir(root):
    """root/<a new run id>, made, for this server run's process totals files (netfetch.write_process_totals): what
    this run's telemetry reads, and nothing an older run left. A new Reporter counts every file it finds from zero, so
    the files the last run's processes left (counted and sent by that run) were counted again at each restart, and
    two servers reporting from one folder counted each other's (2026-11-15 review). Older runs' folders untouched for
    PROC_KEEP_S, and the totals files of versions before the run folders (root/<pid>.json), are removed uncounted."""
    run = f"r{int(time.time())}-{os.getpid()}-{secrets.token_hex(3)}"
    got = os.path.join(root, run)
    os.makedirs(got, exist_ok=True)
    now = time.time()
    try:
        names = os.listdir(root)
    except OSError:
        return got
    import shutil
    for name in names:
        p = os.path.join(root, name)
        try:
            if name == run:
                continue
            if os.path.isdir(p):
                newest = max([os.path.getmtime(p)] + [os.path.getmtime(os.path.join(p, n)) for n in os.listdir(p)])
                if now - newest > PROC_KEEP_S:
                    shutil.rmtree(p, ignore_errors=True)
            elif name.endswith((".json", ".tmp")):
                os.remove(p)
        except OSError:
            continue
    return got


def choices(settings):
    """{"main", "net", "perf"} the player chose (cpupin.player_settings): all False before the welcome page's choice
    (telemetryChoice < CONSENT_VERSION), sub-switches only under the main one."""
    if int(settings.get("telemetryChoice") or 0) < CONSENT_VERSION or settings.get("telemetry", True) is False:
        return {"main": False, "net": False, "perf": False}
    return {"main": True, "net": settings.get("telemetryNet", True) is not False,
            "perf": settings.get("telemetryPerf", True) is not False}


def os_name():
    s = platform.system().lower()
    if s == "linux":
        try:
            with open("/etc/os-release", encoding="utf-8") as fh:
                if "steamos" in fh.read().lower():
                    return "deck"
        except OSError:
            pass
        return "linux"
    return {"darwin": "macos"}.get(s, s or "unknown")


class Reporter:
    """The report loop (a daemon thread). state() -> (game connected, in BeamMP)."""

    def __init__(self, cache_dir, version, state, proc_dir=None, url=URL, send=None, clock=time.monotonic,
                 allowed=None, keep_id=True, perf=None):
        self.cache_dir, self.version, self.state = cache_dir, str(version or ""), state
        self.proc_dir = proc_dir
        self.url = url
        self.send = send or self._post
        self.clock = clock
        # (the switches, read before every report: True / False, or {"main", "net", "perf"}: choices())
        self.allowed = allowed or (lambda: True)
        # perf(): the "perf" section (perfstats.Collector.take), sent only under the "Performance data" switch
        self.perf = perf
        # keep_id False (privacyregion.NO_ID: Russia): a random id of this run only, never stored
        self.id = install_id(cache_dir) if keep_id else secrets.token_hex(16)
        self.seen = {}              # (pid, instance) -> ({host: {counter: n}}, {detail key: n}) last counted
        self.carry = {}             # host -> counts not yet in a report
        self.carry_dem = {}         # detail key -> counts not yet in a report
        # (the reports' idempotency: rid a random id of this run, seq each report's number; a report whose answer was
        # lost is sent again as it was, the same seq, so a collector that saw it can tell and count it once)
        self.rid = secrets.token_hex(8)
        self.seq = 0
        self.pending = None         # (seq, sources, dem) of a report not acknowledged: sent again before new counts
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, name="telemetry", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        if self._stop.wait(FIRST_S):
            return
        while True:
            try:
                self.report()
            except Exception as e:          # (never a reason for the server to fail)
                log.debug("telemetry: %s: %s", type(e).__name__, e)
            if self._stop.wait(REPORT_S):
                return

    def _process_totals(self):
        """{(pid, instance): ({host: {counter: n}}, {detail key: n})}: this process's and the other processes' files
        (proc_dir: this server run's own folder, run_proc_dir). A file whose process has not written for PROC_KEEP_S
        is counted (what changed since it was last seen) and then removed: its process is gone."""
        me = os.getpid()
        out = {(me, netfetch.instance()): (netfetch.process_totals(), netfetch.process_details())}
        if not self.proc_dir or not os.path.isdir(self.proc_dir):
            return out
        now = time.time()
        gone = []
        for name in os.listdir(self.proc_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.proc_dir, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    d = json.load(fh)
                pid = int(d.get("pid"))
                k = (pid, str(d.get("inst") or ""))
                if pid != me:
                    out[k] = (d.get("hosts") or {}, d.get("detail") or {})
                if now - float(d.get("at", now)) > PROC_KEEP_S:
                    gone.append((path, k))
            except (OSError, ValueError, TypeError):
                continue
        self._gone = gone
        return out

    def deltas(self):
        """{host: {key: n}} counted since the last call (every process), plus what has not been in a report yet;
        the DEM detail counts likewise in self.carry_dem."""
        out = {h: dict(v) for h, v in self.carry.items()}
        dem = dict(self.carry_dem)
        self._gone = []
        for k, (hosts, detail) in self._process_totals().items():
            last, last_d = self.seen.get(k, ({}, {}))
            for host, c in hosts.items():
                prev = last.get(host, {})
                d = out.setdefault(host, dict.fromkeys(KEYS, 0))
                for key, (counter,) in KEYS.items():
                    n = max(0, int(c.get(counter, 0)) - int(prev.get(counter, 0)))
                    d[key] = d.get(key, 0) + n
            for key, v in detail.items():
                try:
                    n = max(0, int(v) - int(last_d.get(key, 0)))
                except (TypeError, ValueError):
                    continue
                if n:
                    dem[str(key)] = dem.get(str(key), 0) + n
            self.seen[k] = ({h: dict(c) for h, c in hosts.items()}, dict(detail))
        for path, k in self._gone:          # (counted above: removed now, and forgotten)
            try:
                os.remove(path)
            except OSError:
                pass
            self.seen.pop(k, None)
        self.carry_dem = {key: n for key, n in dem.items() if n}
        return {h: d for h, d in out.items() if any(d.values())}

    def take_dem(self):
        """The DEM detail counts deltas() gathered (cleared): {key: n}, at most DEM_KEYS of them, the rest summed
        under "other"."""
        dem, self.carry_dem = self.carry_dem, {}
        if len(dem) <= DEM_KEYS:
            return dem
        top = sorted(dem.items(), key=lambda kv: (-kv[1], kv[0]))
        out = dict(top[:DEM_KEYS - 1])
        out["other"] = out.get("other", 0) + sum(n for _, n in top[DEM_KEYS - 1:])
        return out

    def body(self, sources):
        game, mp = self.state()
        src = {}
        for host, d in sorted(sources.items())[:64]:
            e = {k: int(v) for k, v in d.items() if k != "mb"}
            e["mb"] = round(d.get("mb", 0) / 1e6, 3)
            src[str(host)[:64]] = e
        return {"id": self.id, "v": self.version[:24], "os": os_name(), "game": bool(game), "mp": bool(mp),
                "interval": REPORT_S, "sources": src}

    def report(self):
        sources = self.deltas()
        dem = self.take_dem()
        a = self.allowed()
        a = a if isinstance(a, dict) else {"main": bool(a), "net": bool(a), "perf": False}
        if not a.get("main"):           # (off, or not chosen yet on the welcome page: nothing sent, these counts dropped)
            self.carry, self.pending = {}, None
            return
        if not a.get("net"):            # (network load off: no per-source counts)
            sources, dem, self.pending = {}, {}, None
        if self.pending is not None:
            # (the last report's answer did not come: it is sent again as it was, its seq, before any new count; the
            # new ones wait for the next report)
            seq, send_sources, send_dem = self.pending
            self.carry, self.carry_dem = sources, dem
        else:
            self.seq += 1
            seq, send_sources, send_dem = self.seq, sources, dem
            self.carry, self.carry_dem = {}, {}
        body = self.body(send_sources)
        body["rid"], body["seq"] = self.rid, seq
        if send_dem:
            body["dem"] = {str(k)[:64]: int(v) for k, v in sorted(send_dem.items())}
        perf = None
        if self.perf:
            try:
                perf = self.perf()          # (taken either way: nothing piles up while the switch is off)
            except Exception as e:
                log.debug("telemetry perf: %s: %s", type(e).__name__, e)
            if not a.get("perf"):
                perf = None
        upload = perf_upload(self.version)
        if perf and upload:
            body["perf"] = perf
        self._keep_last(body, None if upload else perf)
        got = self.send(body)
        if got is REJECTED:
            # (refused outright, a 400 / 413: the collector took none of it, and the same body would be refused
            # again: its counts dropped, never put under a new seq)
            log.debug("telemetry: report %d refused by the collector: its counts dropped", seq)
            self.pending = None
        elif got:
            self.pending = None
        elif self.pending is None:
            # (no answer: it may have arrived. Sent again as it is, its rid and seq, until an answer comes: its counts
            # are never put under another seq, which a collector would count a second time; one body at most, the
            # new counts wait in carry)
            self.pending = (seq, send_sources, send_dem)

    def _keep_last(self, body, held=None):
        """The report as sent, for the settings page's "What is sent" (LAST_FILE in the cache); held: the performance
        section measured but not sent yet (PERF_UPLOAD off: before the collector takes it)."""
        try:
            p = os.path.join(self.cache_dir, LAST_FILE)
            d = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "report": body}
            if held:
                d["perf_not_sent_yet"] = held
            with open(p + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1)
            os.replace(p + ".tmp", p)
        except OSError as e:
            log.debug("telemetry last: %s", e)

    def _post(self, body):
        req = urllib.request.Request(self.url, json.dumps(body, separators=(",", ":")).encode("utf-8"),
                                     {"Content-Type": "application/json", "User-Agent": "BeamNG.Earth"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=netfetch.windows_ssl_context()) as r:
                return 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            log.debug("telemetry report: HTTP %s", e.code)
            # (the collector's own answer: a 4xx other than "too soon" (429) / a timeout (408) means it took nothing
            # and never will; 429, 5xx: not counted now, the same report again later)
            return REJECTED if 400 <= e.code < 500 and e.code not in (408, 429) else False
        except Exception as e:
            log.debug("telemetry report: %s: %s", type(e).__name__, e)
            return False
