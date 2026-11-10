"""The server's memory and CPU budget for the machine it runs on.

Every worker process keeps its own caches (decoded DEM tiles, map data cells, imagery tiles), sized for a desktop.
On a Steam Deck (16 GB shared with the GPU, 4 cores) seven processes filled the memory: 8.2 GB for the server next to
the game's 2.4 GB and the GPU's 1.9 GB, swap at 6.9 GB, and the system's out-of-memory guard killed the game in a
Baltimore drive (the user, 2026-10-20: "the game just closed"; "I want to keep world generation fast on the deck as
well"). SMALL machines (less than SMALL_GB of RAM) get smaller caches and fewer processes; the others keep the
desktop sizes.

    budget.apply()          # in each process before its caches are made (earthd main, blockworker.init)
"""
import os
import sys

SMALL_GB = 20.0             # below this much RAM: the small profile (a Steam Deck shows ~14.5 GB)
# per process, small profile (desktop size in the module: dem 512 tiles). The sea floors (seacarve.MEM_CELLS) and the
# imagery tiles (imagery.MEM_TILES, the main process only) keep theirs, the map data cells nearly: smaller,
# the workers and the server's ground-ahead and imagery threads spent minutes reading them back from disk and
# decoding them again (sampled on a Steam Deck, 2026-10-20)
SMALL_DEM_TILES = 192       # decoded 512² float32 DEM tiles (1 MB each)
SMALL_WALL_CELLS = 12       # cells of building walls for the street widths (roadwidth.WALL_CELLS, 48)
SMALL_MAPDATA_MB = 160      # decoded map data layers (mapdata.MEM_MB, 192): a road tile's cells still fit
SMALL_ROAD_PROCESSES = 2    # road-tile processes (earthd ROAD_PROCESSES; with one the road tiles never caught up)
# A desktop's road-tile processes by its threads and memory (each ~1-1.5 GB): driving off right after a teleport into
# Manhattan at 50 mph, 2 had the road under the car for the first ~15 s and solid ~27 s after, 4 at ~6 s and ~15 s,
# at the same frame rate (a 16-thread PC, 2026-10-25)
ROAD_PROCESSES_BY_CPU = ((12, 24.0, 4), (8, 16.0, 3))     # (threads at least, GB at least, processes)
SMALL_FAR_K = 0.8           # the far field's refinement factors (farfield.K_TABLE) times this: 204 tiles for 332
SMALL_FAR_TEX = 256         # px of the far tiles' textures at every level (512 from L2 on a desktop)
ARENA_MAX = 2               # glibc malloc arenas a process (every profile): one per thread kept freed memory ...
TRIM_S = 20.0               # ... and every TRIM_S the freed memory is given back (malloc_trim): a worker held ~1 GB
                            # for ~0.3 GB of Python and numpy objects (traced, a Steam Deck 2026-10-20)
SMALL_FAR_WORKERS = 2       # far-tile worker processes (earthd --workers)
SMALL_SERVER_SHARE = 0.5    # of the physical cores for the server (cpupin.SERVER_SHARE): one of four was too few
# The imagery's background analyses (de-shadowing, colour seams) off: on a Steam Deck they kept one of the server's
# threads busy (colour matching ~85 s of CPU in a Baltimore drive) and stream large arrays through the memory the GPU
# shares (on a desktop such a thread made the game's shadow pass 5-6x slower, pacer.py)
SMALL_ANALYSES_OFF = True
# The finished imagery pyramid's memory (imagery.MEM_FINISHED_MB of its JPEGs, MEM_DECODED tiles decoded at 192 KB) in
# every process that renders textures (the server; on a small machine its far-tile workers too, the analyses off):
# 160 MB + 384 on a desktop, ~230 MB a process; on a small machine FIN_MB_PER_GB per GB of RAM (at most the desktop's,
# at least SMALL_FIN_MB) and SMALL_FIN_DECODED (a rebase's textures share their neighbours' tiles: a far texture
# touches 9-16 of them)
FIN_MB_PER_GB = 4.0
SMALL_FIN_MB = 32
SMALL_FIN_DECODED = 96


def total_gb():
    """The machine's RAM in GB (None if unknown)."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        pass
    try:                                    # Windows
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return m.ullTotalPhys / 1e9
    except Exception:
        pass
    return None


def free_gb():
    """The RAM free for new work now, in GB (Linux MemAvailable, Windows ullAvailPhys; None if unknown)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024 / 1e9
    except (OSError, ValueError, IndexError):
        pass
    try:                                    # Windows
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return m.ullAvailPhys / 1e9
    except Exception:
        pass
    return None


def small(gb=None):
    """Is this a small machine (the small profile)? BNGE_BUDGET=small / desktop overrides."""
    force = os.environ.get("BNGE_BUDGET", "").lower()
    if force in ("small", "desktop"):
        return force == "small"
    gb = total_gb() if gb is None else gb
    return gb is not None and gb < SMALL_GB


SMALL_FACTS_CELLS = 192         # roads._FACTS_MEM entries on a small machine (~24 cells; a Steam Deck: ~9 server processes)


def apply(is_small=None):
    """Set the cache sizes of this process's modules for the profile; returns whether it is the small one."""
    is_small = small() if is_small is None else is_small
    global SHED_MB
    SHED_MB = SMALL_SHED_MB if is_small else 0
    if os.environ.get("BNGE_SHED_MB"):              # (A/B: the ceiling in MB, 0 off)
        SHED_MB = int(os.environ["BNGE_SHED_MB"])
    if is_small:
        import dem
        import farfield
        import mapdata
        import roadwidth
        dem.MEM_TILES = SMALL_DEM_TILES
        mapdata.MEM_MB = SMALL_MAPDATA_MB
        roadwidth.WALL_CELLS = SMALL_WALL_CELLS
        import roads
        roads.FACTS_MEM_KEPT = SMALL_FACTS_CELLS   # (~0.5 MB a city cell's facts, in each process)
        import imagery
        gb = total_gb() or 16.0
        imagery.MEM_FINISHED_MB = int(min(imagery.MEM_FINISHED_MB, max(SMALL_FIN_MB, round(FIN_MB_PER_GB * gb))))
        imagery.MEM_DECODED = min(imagery.MEM_DECODED, SMALL_FIN_DECODED)
        for cells, table in farfield.K_TABLE.items():       # (read when the server makes its Layout)
            farfield.K_TABLE[cells] = {lvl: k * SMALL_FAR_K for lvl, k in table.items()}
        for cells, tex in farfield.TEX_DEFAULT.items():
            farfield.TEX_DEFAULT[cells] = tuple(min(t, SMALL_FAR_TEX) for t in tex)
    if os.environ.get("BNGE_DEM_MEM_TILES"):        # (A/B: decoded DEM tiles kept per process)
        import dem
        dem.MEM_TILES = int(os.environ["BNGE_DEM_MEM_TILES"])
    # every machine: a desktop's server held 13-17 GB in its eleven processes (1-1.5 GB a worker, mostly freed memory
    # its 40-65 threads' arenas kept), next to the game; a 32 GB PC with 4 cores swapped (GitHub #496, 2026-10-30)
    _malloc_small()
    return is_small


def _libc():
    import ctypes
    import ctypes.util
    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
    except OSError:
        return None


_trimming = False

# A worker process over SHED_MB (resident + swapped) after a job drops its caches (each module registers how:
# register_shed) and gives the memory back: on a Steam Deck two Manhattan drives took a road worker to 1.6 GB and the
# server to ~5 GB PSS with 6.7 GB of swap until the system killed the game's UI (2026-11-04); the caches' own caps
# count entries, and the results kept on cached map features are counted nowhere. 0 = off (a desktop: apply()).
SHED_MB = 0
SMALL_SHED_MB = 1400     # (1200 on a Deck in Manhattan: two road workers dropped every ~15 s, 1206 -> 822 MB, their working set)
SHED_MIN_S = 90.0        # a worker drops its caches at most this often (a dense city's working set comes straight back)
_shedders = []
shed_count = 0
_last_shed = None


def register_shed(fn):
    """fn(): drop a cache of this process (called between jobs, in the worker's own thread)."""
    _shedders.append(fn)


def memory_mb():
    """This process's resident + swapped memory in MB (Linux /proc; Windows: private bytes), or None."""
    try:
        rss = swap = 0
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                elif line.startswith("VmSwap:"):
                    swap = int(line.split()[1])
        return (rss + swap) / 1024.0
    except (OSError, ValueError):
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                   "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                   "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                                                   "PrivateUsage")]
            c = PMC()
            c.cb = ctypes.sizeof(c)
            k32 = ctypes.WinDLL("kernel32")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi")
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return c.PrivateUsage / 2 ** 20
        except Exception:
            pass
    return None


def maybe_shed():
    """Between jobs: the registered caches dropped when this process is over SHED_MB. True when it shed."""
    global shed_count
    if not SHED_MB or not _shedders:
        return False
    import time
    global _last_shed
    if _last_shed is not None and time.monotonic() - _last_shed < SHED_MIN_S:
        return False
    mb = memory_mb()
    if mb is None or mb < SHED_MB:
        return False
    _last_shed = time.monotonic()
    import gc
    import logging
    for fn in list(_shedders):
        try:
            fn()
        except Exception:
            pass
    gc.collect()
    trim()
    shed_count += 1
    after = memory_mb()
    logging.getLogger("earthd.worker").info("memory: pid %d at %.0f MB (over %d): caches dropped, now %.0f MB",
                                            os.getpid(), mb, SHED_MB, after or 0)
    return True


def _malloc_small():
    """Linux (glibc): at most ARENA_MAX malloc arenas, and the freed memory given back every TRIM_S (a thread)."""
    global _trimming
    if not sys.platform.startswith("linux") or _trimming:
        return
    libc = _libc()
    if libc is None or not hasattr(libc, "malloc_trim"):
        return
    try:
        libc.mallopt(-8, ARENA_MAX)         # M_ARENA_MAX
    except Exception:
        pass
    import threading
    import time

    def loop():
        while True:
            time.sleep(TRIM_S)
            try:
                libc.malloc_trim(0)
            except Exception:
                return
    _trimming = True
    threading.Thread(target=loop, daemon=True, name="malloc-trim").start()


def trim():
    """Give the freed memory back now (glibc; nothing elsewhere)."""
    libc = _libc() if sys.platform.startswith("linux") else None
    if libc is not None and hasattr(libc, "malloc_trim"):
        libc.malloc_trim(0)


# Windows has no niceness: priority classes. The server's own (earthd --nice, default 10) is "below normal"; its workers
# keep it. Without it all nine server processes ran at the game's own priority (start.py passed --nice 0 on Windows,
# the lowering never ran; 2026-10-21). Not "idle" for the far field's and far buildings' workers (1.6.0 to 1.6.1):
# Windows runs a lower class only when no thread of a higher one is ready, and the server has its own few cores
# (cpupin.split: 2 of a 6-core CPU), which the road and block workers can fill for minutes on a first visit; then the
# far workers got nothing at all (a Windows player, i5-9400F, 1.6.1: "distant land 5/514", only the land near the car
# loading). os.nice elsewhere only weights the share.
WIN_BELOW_NORMAL = 0x4000


def lower_priority(nice, worker=False):
    """This process below the game: os.nice(nice) where it exists; on Windows the "below normal" class for the
    server (worker False, nice > 0); a worker keeps the class it inherited. Returns the change made, for the log
    ("nice +n", "below normal", None)."""
    if not nice or nice <= 0:
        return None
    if hasattr(os, "nice"):
        os.nice(int(nice))
        return f"nice +{int(nice)}"
    if sys.platform != "win32" or worker:
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 64-bit handles: without these the pseudo handle went in as a 32-bit int and the call failed (as earthd's did)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k32.SetPriorityClass.restype = wintypes.BOOL
    if not k32.SetPriorityClass(k32.GetCurrentProcess(), WIN_BELOW_NORMAL):
        raise OSError("SetPriorityClass(below normal) failed")
    return "below normal"


# The server's event loop answers every message the game sends (a pose, a block request). In the "below normal" class
# its thread waited for the interpreter lock like the ~20 busy others of the process: at 200 mph on a first visit (A13,
# 2 server cores, Windows; 2026-11-04) it lagged 0.5-1 s in the median and up to 15 s (GitHub #725). One step above
# them it is still inside the process's class, below the game's "normal".
WIN_THREAD_ABOVE_NORMAL = 1     # THREAD_PRIORITY_ABOVE_NORMAL


def raise_thread():
    """The calling thread one step above the others of its process (Windows; the event loop's, earthd.LOOP_RAISE).
    Returns the change made, for the log ("thread above normal", None: nothing to do elsewhere)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # (64-bit handles, as lower_priority's)
    k32.GetCurrentThread.restype = wintypes.HANDLE
    k32.SetThreadPriority.argtypes = (wintypes.HANDLE, ctypes.c_int)
    k32.SetThreadPriority.restype = wintypes.BOOL
    if not k32.SetThreadPriority(k32.GetCurrentThread(), WIN_THREAD_ABOVE_NORMAL):
        raise OSError("SetThreadPriority(above normal) failed")
    return "thread above normal"


def road_processes(cpus=None, gb=None):
    """Road-tile processes for a desktop (ROAD_PROCESSES_BY_CPU), 2 below them."""
    cpus = (os.cpu_count() or 1) if cpus is None else cpus
    gb = (total_gb() or 0.0) if gb is None else gb
    for c, g, n in ROAD_PROCESSES_BY_CPU:
        if cpus >= c and gb >= g:
            return n
    return SMALL_ROAD_PROCESSES
