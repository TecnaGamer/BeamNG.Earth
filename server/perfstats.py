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
import sys
import threading
import time

import fast

# the names a summary and a setup may carry (perfsummary.lua's; a settings table only its fixed lists' names)
SUMMARY_NAMES = dict.fromkeys(("v", "s", "fh", "hit", "hitLoad", "causes", "teleports", "blocks", "mapOpen", "bands", "scene",
                               "loaded", "lua", "held", "roadsQueuedP90", "drops", "age",
                               # version 2 (2026-11-11): the window's own percentiles, the fine histogram (sparse), a
                               # slow frame's context, the 1 s+ causes, time since teleport, idle, the machine's memory
                               "ft", "p50", "p99", "p999", "over2x", "over4x", "ctx", "cause1s", "tp", "idle", "mach"))
GAME_SETTINGS = ["GraphicOverallQuality", "GraphicMeshQuality", "GraphicTextureQuality", "GraphicTerrainQuality",
                 "GraphicLightingQuality", "GraphicShadowsQuality", "GraphicAnisotropic", "GraphicAntialiasType",
                 "GraphicAntialias", "vsync", "PostFXDOFGeneralEnabled", "PostFXSSAOGeneralEnabled",
                 "PostFXMotionBlurEnabled", "GraphicDynReflectionEnabled", "GraphicGrassDensity", "GraphicDisplayModes"]
MOD_SETTINGS = ["quality", "trees", "treeDistance", "groundCover", "groundCoverDensity", "grassDistance", "rocks",
                "nightLights", "distantTraffic", "distantTrafficQuality", "viewDistanceMode", "viewDistanceKm",
                "terrainDetail", "buildingDistance", "groundPhoto", "objectDetail", "roads", "clouds", "skyMode",
                "mapStyle", "framePacing", "teleportPriority"]
SETUP_NAMES = {**dict.fromkeys(("v", "cpu", "cores", "threads", "gpu", "vramGB", "ramGB", "api", "os",
                                "windowSystem", "res", "hz", "game", "otherMods")),
               "gameSettings": GAME_SETTINGS, "modSettings": MOD_SETTINGS}
KEEP_SUMMARIES = 4              # the mod's windows kept between reports (one a report at 5 minutes each)
MEMORY_STEPS = (0, 512, 1024, 2048, 3072, 4096, 6144, 8192, 12288, 16384, 24576, 32768)
CORE_STEPS = (0, 0.25, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 12, 16)
WORKER_STEPS = (0, 1, 2, 4, 6, 8, 12, 16, 24, 32)
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


def _workers():
    """This server's worker processes' pids (multiprocessing's children: no scan of every process, cpupin.pin_tree)."""
    import multiprocessing
    try:
        return [p.pid for p in multiprocessing.active_children() if p.pid]
    except Exception:
        return []


def _pss_mb(pid):
    """A Linux process's proportional memory (Pss + SwapPss, MB: shared pages split between the processes sharing them,
    so the tree's sum is what it really holds, not RSS's double count), or None."""
    try:
        total = 0
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:") or line.startswith("SwapPss:"):
                    total += int(line.split()[1])
        return total / 1024.0
    except (OSError, ValueError, IndexError):
        return None


def _win_proc(pid):
    """(CPU seconds, private MB) of a Windows process, or None."""
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                           "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                                           "PrivateUsage")]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.K32GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
    h = k32.OpenProcess(0x1000 | 0x0010, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ
    if not h:
        return None
    try:
        t = [wintypes.FILETIME() for _ in range(4)]
        cpu = None
        if k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
            cpu = sum((x.dwHighDateTime << 32 | x.dwLowDateTime) for x in t[2:]) / 1e7     # kernel + user, 100 ns
        c = PMC()
        c.cb = ctypes.sizeof(c)
        mem = c.PrivateUsage / 2 ** 20 if k32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb) else None
        return cpu, mem
    finally:
        k32.CloseHandle(h)


def tree_stats():
    """(CPU seconds, memory MB, workers) of this server and its worker processes, or None: on Linux the CPU of the whole
    process tree (native proc_tree) and the main process's and workers' PSS; on Windows their CPU times and private
    bytes (the reports had no server numbers from Windows, 99 % of the players, before 2026-11-11)."""
    kids = _workers()
    pids = [os.getpid()] + kids
    if sys.platform == "win32":
        cpu = mem = 0.0
        got = False
        for pid in pids:
            try:
                r = _win_proc(pid)
            except Exception:
                r = None
            if r:
                got = True
                cpu += r[0] or 0.0
                mem += r[1] or 0.0
        return (cpu, mem, len(kids)) if got else None
    t = fast.proc_tree(os.getpid())
    pss = [_pss_mb(p) for p in pids]
    pss = [x for x in pss if x is not None]
    if not t and not pss:
        return None
    cpu, rss = t if t else (None, None)
    return cpu, (sum(pss) if pss else rss), len(kids)


class Collector:
    def __init__(self, clock=time.monotonic, tree=None):
        self.clock = clock
        self.tree = tree or tree_stats
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
            cpu, mem = t[0], t[1]
            if cpu is not None:
                if self._cpu_last and now > self._cpu_last[1]:
                    srv["cores"] = fast.perf_step(max(0.0, (cpu - self._cpu_last[0]) / (now - self._cpu_last[1])),
                                                  CORE_STEPS)
                self._cpu_last = (cpu, now)
            if mem is not None:
                srv["memMB"] = fast.perf_step(mem, MEMORY_STEPS)
            if len(t) > 2 and t[2] is not None:     # (the worker processes running: the server's size on this machine)
                srv["workers"] = fast.perf_step(t[2], WORKER_STEPS)
        try:        # the server's RAM limit (the settings page's Max RAM, rounded), whether the player set it, its tier
            import budget
            if budget.LIMIT_MB:
                srv["memLimitMB"] = fast.perf_step(budget.LIMIT_MB, MEMORY_STEPS)
                srv["memSet"] = bool(budget.setting_gb())
                srv["memTier"] = {"desktop": 0, "small": 1, "lean": 2, "emergency": 3}.get(budget.TIER, 0)
        except Exception:
            pass
        errs, codes = self.errors.take()
        if errs:
            srv["errors"] = errs
            srv["errorCodes"] = codes       # {fingerprint: {"OSError:24": n}} (the collector keeps them per day)
        out["server"] = srv
        return out
