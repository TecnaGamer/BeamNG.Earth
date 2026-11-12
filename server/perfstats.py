"""The anonymous performance data of a report (TELEMETRY.md sections 2-5; the user, 2026-11-08: "start using the
telemetry to collect performance and world gen speed data ... Collect system specs too. but keep everything as anomomus
as possible"): glue between the mod's summaries (perfsummary.lua: perf_setup, perf_summary), a few world-generation
counters of this server and its error fingerprints, and telemetry.Reporter, which adds them to a report only while the
player chose "Performance data".

The checks, the rounding, the fingerprints and the process tree's CPU and memory are the Rust kernels' (native
perfclean.rs, through fast.py): what the mod sends is rebuilt from the allowed shapes only, so neither a mod bug nor a
changed mod can put a position, a path or free text into a report.
"""
import logging
import re
import os
import threading
import time

import fast

# the names a summary and a setup may carry (perfsummary.lua's; a settings table only its fixed lists' names)
SUMMARY_NAMES = dict.fromkeys(("v", "s", "fh", "hit", "hitLoad", "causes", "teleports", "blocks", "mapOpen", "bands", "scene",
                               "loaded", "lua", "held", "roadsQueuedP90", "drops"))
GAME_SETTINGS = ["GraphicOverallQuality", "GraphicMeshQuality", "GraphicTextureQuality", "GraphicTerrainQuality",
                 "GraphicLightingQuality", "GraphicShadowsQuality", "GraphicAnisotropic", "GraphicAntialiasType",
                 "GraphicAntialias", "vsync", "PostFXDOFGeneralEnabled", "PostFXSSAOGeneralEnabled",
                 "PostFXMotionBlurEnabled", "GraphicDynReflectionEnabled", "GraphicGrassDensity", "GraphicDisplayModes"]
MOD_SETTINGS = ["quality", "trees", "treeDistance", "groundCover", "groundCoverDensity", "grassDistance", "rocks",
                "nightLights", "distantTraffic", "distantTrafficQuality", "viewDistanceMode", "viewDistanceKm",
                "terrainDetail", "buildingDistance", "groundPhoto", "objectDetail", "roads", "clouds", "skyMode",
                "mapStyle"]
SETUP_NAMES = {**dict.fromkeys(("v", "cpu", "cores", "threads", "gpu", "vramGB", "ramGB", "api", "os",
                                "windowSystem", "res", "hz", "game", "otherMods")),
               "gameSettings": GAME_SETTINGS, "modSettings": MOD_SETTINGS}
KEEP_SUMMARIES = 4              # the mod's windows kept between reports (one a report at 5 minutes each)
MEMORY_STEPS = (0, 512, 1024, 2048, 3072, 4096, 6144, 8192, 12288, 16384, 24576, 32768)
CORE_STEPS = (0, 0.25, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 12, 16)
FINGERPRINTS = 24               # distinct error fingerprints a report carries


_SRC_FRAME = re.compile(r'File "[^"]*[/\\]([A-Za-z_][A-Za-z0-9_]*)\.py", line (\d+)')


def raised_at(exc):
    """'<module>-<line>' of the innermost frame of the server's own code an exception was raised in, or "": only a
    module name and a line number, never a path or a value. An exception from a worker process carries its traceback as
    text (concurrent.futures' _RemoteTraceback, its __cause__); the far tiles' 1.1 M BadZipFile in a week were all
    logged at one line of earthd, whichever cache file was damaged (2026-11-09)."""
    if exc is None:
        return ""
    try:
        cause = getattr(exc, "__cause__", None)
        text = getattr(cause, "tb", None)
        if isinstance(text, str):
            frames = _SRC_FRAME.findall(text)
        else:
            frames = []
            tb = getattr(exc, "__traceback__", None)
            while tb is not None:
                frames.append((os.path.basename(tb.tb_frame.f_code.co_filename)[:-3], str(tb.tb_lineno)))
                tb = tb.tb_next
        own = [(m, n) for m, n in frames if m in _OWN_MODULES()]
        if not own:
            return ""
        m, n = own[-1]
        return f"{m[:14]}-{n}"
    except Exception:
        return ""


def _OWN_MODULES():
    """The server's own module names (the .py files beside this one), once."""
    global _own
    if _own is None:
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            _own = frozenset(f[:-3] for f in os.listdir(here) if f.endswith(".py"))
        except OSError:
            _own = frozenset()
    return _own


_own = None


class ErrorPrints(logging.Handler):
    """Counts the server's errors by fingerprint (fast.perf_fingerprint of the exception's type and the logging call's
    module and line: never the message, a path or a value) and, per fingerprint, the error's code: an OSError's
    number (winerror on Windows, else errno: 24 too many open files, 28 disk full, 32 a file in use ...) beside its
    type name. (2026-11-08: the first night's reports had nine OSError fingerprints, one 3,287 times, and nothing to
    tell a full disk from Windows' open-files limit.)"""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.counts = {}
        self.codes = {}
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            exc = record.exc_info[1] if record.exc_info else None
            kind = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "log"
            key = fast.perf_fingerprint(kind, record.module, record.lineno)
            num = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            code = f"{kind[:24]}:{num}" if isinstance(num, int) else kind[:24]
            where = raised_at(exc)
            if where:       # (the module and line it was raised at: a far tile's error is logged in one place for all)
                code = (code + "-" + where)[:40]
            with self._lock:
                if key in self.counts or len(self.counts) < FINGERPRINTS:
                    self.counts[key] = self.counts.get(key, 0) + 1
                    c = self.codes.setdefault(key, {})
                    if code in c or len(c) < 4:
                        c[code] = c.get(code, 0) + 1
        except Exception:
            pass

    def take(self):
        with self._lock:
            out, codes, self.counts, self.codes = self.counts, self.codes, {}, {}
        return out, codes


class Collector:
    def __init__(self, clock=time.monotonic, tree=None):
        self.clock = clock
        self.tree = tree or (lambda: fast.proc_tree(os.getpid()))
        self._lock = threading.Lock()
        self.setup = self.setup_sent = None
        self.summaries = []
        self.catchup_s, self.catchups, self._catchup_at = 0.0, 0, None
        self.errors = ErrorPrints()
        self._cpu_last = None
        self._mem_peak = 0.0

    # the mod's messages
    def on_setup(self, setup):
        s = fast.perf_clean(setup, strings=True, allowed=SETUP_NAMES)
        if isinstance(s, dict) and s:
            with self._lock:
                self.setup = s

    def on_summary(self, summary):
        s = fast.perf_clean(summary, allowed=SUMMARY_NAMES)
        if isinstance(s, dict) and s:
            with self._lock:
                self.summaries.append(s)
                del self.summaries[:-KEEP_SUMMARIES]

    # this server's world generation: the catch-up boost (earthd Conn.catch_up)
    def catchup(self, on):
        now = self.clock()
        with self._lock:
            if on and self._catchup_at is None:
                self._catchup_at, self.catchups = now, self.catchups + 1
            elif not on and self._catchup_at is not None:
                self.catchup_s += now - self._catchup_at
                self._catchup_at = None

    def take(self):
        """The report's "perf" section: the setup (when new or changed), the mod's windows since the last report, this
        server's counters (rounded)."""
        now = self.clock()
        try:
            t = self.tree()
        except Exception:
            t = None
        with self._lock:
            out = {}
            if self.setup and self.setup != self.setup_sent:
                out["setup"] = self.setup_sent = self.setup
            if self.summaries:
                out["game"], self.summaries = self.summaries, []
            cs = self.catchup_s
            if self._catchup_at is not None:
                cs += now - self._catchup_at
                self._catchup_at = now
            srv = {"catchupS": round(cs / 5) * 5, "catchups": self.catchups}
            self.catchup_s, self.catchups = 0.0, 0
        if t:
            cpu, mem = t
            if self._cpu_last and now > self._cpu_last[1]:
                srv["cores"] = fast.perf_step(max(0.0, (cpu - self._cpu_last[0]) / (now - self._cpu_last[1])),
                                              CORE_STEPS)
            self._cpu_last = (cpu, now)
            srv["memMB"] = fast.perf_step(mem, MEMORY_STEPS)
        errs, codes = self.errors.take()
        if errs:
            srv["errors"] = errs
            srv["errorCodes"] = codes       # {fingerprint: {"OSError:24": n}} (the collector keeps them per day)
        out["server"] = srv
        return out
