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
SMALL_FAR_K = 0.8           # the far field's refinement factors (farfield.K_TABLE) times this: 204 tiles for 332
SMALL_FAR_TEX = 256         # px of the far tiles' textures at every level (512 from L2 on a desktop)
ARENA_MAX = 2               # glibc malloc arenas a process (small profile): one per thread kept freed memory ...
TRIM_S = 20.0               # ... and every TRIM_S the freed memory is given back (malloc_trim): a worker held ~1 GB
                            # for ~0.3 GB of Python and numpy objects (traced, a Steam Deck 2026-10-20)
SMALL_FAR_WORKERS = 2       # far-tile worker processes (earthd --workers)
SMALL_SERVER_SHARE = 0.5    # of the physical cores for the server (cpupin.SERVER_SHARE): one of four was too few
# The imagery's background analyses (de-shadowing, colour seams) off: on a Steam Deck they kept one of the server's
# threads busy (colour matching ~85 s of CPU in a Baltimore drive) and stream large arrays through the memory the GPU
# shares (on a desktop such a thread made the game's shadow pass 5-6x slower, pacer.py)
SMALL_ANALYSES_OFF = True


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


def small(gb=None):
    """Is this a small machine (the small profile)? BNGE_BUDGET=small / desktop overrides."""
    force = os.environ.get("BNGE_BUDGET", "").lower()
    if force in ("small", "desktop"):
        return force == "small"
    gb = total_gb() if gb is None else gb
    return gb is not None and gb < SMALL_GB


def apply(is_small=None):
    """Set the cache sizes of this process's modules for the profile; returns whether it is the small one."""
    is_small = small() if is_small is None else is_small
    if is_small:
        import dem
        import farfield
        import mapdata
        import roadwidth
        dem.MEM_TILES = SMALL_DEM_TILES
        mapdata.MEM_MB = SMALL_MAPDATA_MB
        roadwidth.WALL_CELLS = SMALL_WALL_CELLS
        for cells, table in farfield.K_TABLE.items():       # (read when the server makes its Layout)
            farfield.K_TABLE[cells] = {lvl: k * SMALL_FAR_K for lvl, k in table.items()}
        for cells, tex in farfield.TEX_DEFAULT.items():
            farfield.TEX_DEFAULT[cells] = tuple(min(t, SMALL_FAR_TEX) for t in tex)
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
