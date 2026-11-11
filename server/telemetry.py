"""Anonymous usage reports (the user, 2026-11-06: "create a telemetry system. Have it track current players and a way to
monitor the stress we're putting on the data sources"; on by default with an opt-out, country only).

Every REPORT_S the server sends https://earth.tgforever.net/telemetry (the project's server-list service, mini PC):

    {"id": a random install id (TELEMETRY_ID file in the cache; no name, account or hardware in it),
     "v": the app version, "os": windows / linux / deck / macos, "game": a game connected now, "mp": in BeamMP,
     "interval": REPORT_S,
     "sources": {host: {"req", "mb", "ok", "e4xx", "e404", "e429", "e5xx", "err", "retry"}}}   (since the last report)

Nothing else: no place, no position, no IP kept (the collector derives the country from the address and drops it).
Off with the settings page's Anonymous usage statistics switch (read before every report), BNE_TELEMETRY=off (or
earthd --telemetry off), and always in --offline mode. Failures are silent: a report that
cannot be sent is dropped (its counts go into the next one).

The per-host counts are every process's (netfetch.process_totals: the worker processes write theirs to
BNE_NETSTATS_PROC, this sums what changed since the last report).
"""
import json
import logging
import os
import platform
import secrets
import threading
import time
import urllib.request

import netfetch

log = logging.getLogger("earthd.telemetry")

URL = os.environ.get("BNE_TELEMETRY_URL", "https://earth.tgforever.net/telemetry")
ON = os.environ.get("BNE_TELEMETRY", "on").lower() not in ("off", "0", "false", "no")
# the welcome page's choice this version asks for (settings telemetryChoice): nothing is sent before it. 2: the
# performance data is sent (from development builds first), everyone who chose under 1 is asked again
CONSENT_VERSION = 2
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
PROC_KEEP_S = 3600.0        # a dead process's totals file is removed this long after its last write
KEYS = {"req": ("requests",), "mb": ("bytes",), "ok": ("s2xx",), "e4xx": ("s4xx",), "e404": ("s404",),
        "e429": ("s429",), "e5xx": ("s5xx",), "err": ("errors",), "retry": ("retries",)}


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
        self.seen = {}              # pid -> {host: {counter: n}} last counted
        self.carry = {}             # host -> counts not yet delivered (a failed report)
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
        """{pid: {host: {counter: n}}}: this process's and the other processes' files."""
        out = {os.getpid(): netfetch.process_totals()}
        if not self.proc_dir or not os.path.isdir(self.proc_dir):
            return out
        now = time.time()
        for name in os.listdir(self.proc_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.proc_dir, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    d = json.load(fh)
                pid = int(d.get("pid"))
                if pid != os.getpid():
                    out[pid] = d.get("hosts") or {}
                if now - float(d.get("at", now)) > PROC_KEEP_S:
                    os.remove(path)
                    self.seen.pop(pid, None)
            except (OSError, ValueError, TypeError):
                continue
        return out

    def deltas(self):
        """{host: {key: n}} counted since the last call (every process), plus what a failed report left."""
        out = {h: dict(v) for h, v in self.carry.items()}
        for pid, hosts in self._process_totals().items():
            last = self.seen.get(pid, {})
            for host, c in hosts.items():
                prev = last.get(host, {})
                d = out.setdefault(host, dict.fromkeys(KEYS, 0))
                for key, (counter,) in KEYS.items():
                    n = max(0, int(c.get(counter, 0)) - int(prev.get(counter, 0)))
                    d[key] = d.get(key, 0) + n
            self.seen[pid] = {h: dict(c) for h, c in hosts.items()}
        return {h: d for h, d in out.items() if any(d.values())}

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
        a = self.allowed()
        a = a if isinstance(a, dict) else {"main": bool(a), "net": bool(a), "perf": False}
        if not a.get("main"):           # (off, or not chosen yet on the welcome page: nothing sent, these counts dropped)
            self.carry = {}
            return
        if not a.get("net"):            # (network load off: no per-source counts)
            sources = {}
        body = self.body(sources)
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
        if self.send(body):
            self.carry = {}
        else:
            self.carry = sources        # (sent with the next report)

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
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return 200 <= r.status < 300
        except Exception as e:
            log.debug("telemetry report: %s: %s", type(e).__name__, e)
            return False
