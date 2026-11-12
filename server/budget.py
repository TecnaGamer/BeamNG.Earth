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
TINY_GB = 13.0              # ... and below this (an 8 or 12 GB PC) its smaller sizes (TINY_*)
# per process, small profile (desktop size in the module: dem 512 tiles). The sea floors (seacarve.MEM_CELLS) and the
# imagery tiles (imagery.MEM_TILES, the main process only) keep theirs, the map data cells nearly: smaller,
# the workers and the server's ground-ahead and imagery threads spent minutes reading them back from disk and
# decoding them again (sampled on a Steam Deck, 2026-10-20)
SMALL_DEM_TILES = 192       # decoded 512² float32 DEM tiles (1 MB each)
SMALL_WALL_CELLS = 12       # cells of building walls for the street widths (roadwidth.WALL_CELLS, 48)
SMALL_MAPDATA_MB = 160      # decoded map data layers (mapdata.MEM_MB, 256): a road tile's cells still fit
TINY_DEM_TILES = 128
TINY_MAPDATA_MB = 128       # (a Baltimore road tile's cells: ~124 MB)
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
    """The machine's RAM in GB (None if unknown). BNGE_TOTAL_GB: taken as this much (tests of the smaller tiers on a
    bigger machine)."""
    if os.environ.get("BNGE_TOTAL_GB"):
        try:
            return float(os.environ["BNGE_TOTAL_GB"])
        except ValueError:
            pass
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


def shed_mb(is_small, gb):
    """A worker's memory ceiling (SHED_MB) for the profile and the machine's RAM (GB, None unknown); 0: none."""
    if is_small:
        return TINY_SHED_MB if gb is not None and gb < TINY_GB else SMALL_SHED_MB
    if gb is None:
        return 0
    for below, mb in DESKTOP_SHED_MB:
        if gb < below:
            return mb
    return 0


def low_free_gb(gb):
    """The free memory (GB) under which a worker drops its caches between jobs (LOW_FREE_*), for RAM gb."""
    if gb is None:
        return LOW_FREE_MIN_GB
    return min(LOW_FREE_MAX_GB, max(LOW_FREE_MIN_GB, LOW_FREE_FRAC * gb))


def apply(is_small=None):
    """Set the cache sizes of this process's modules for the profile; returns whether it is the small one."""
    is_small = small() if is_small is None else is_small
    global SHED_MB, LOW_FREE_GB, TREE_MB
    gb = total_gb()
    SHED_MB = shed_mb(is_small, gb)
    if os.environ.get("BNGE_SHED_MB"):              # (A/B: the ceiling in MB, 0 off)
        SHED_MB = int(os.environ["BNGE_SHED_MB"])
    LOW_FREE_GB = low_free_gb(gb)
    if os.environ.get("BNGE_LOW_FREE_GB"):          # (A/B: the free memory in GB, 0 off)
        LOW_FREE_GB = float(os.environ["BNGE_LOW_FREE_GB"])
    TREE_MB = tree_mb(gb) if TREE_ON else 0
    if os.environ.get("BNGE_TREE_MB"):              # (A/B: the whole server's budget in MB, 0 off)
        TREE_MB = int(os.environ["BNGE_TREE_MB"])
    if is_small:
        import dem
        import farfield
        import mapdata
        import roadwidth
        tiny = gb is not None and gb < TINY_GB
        dem.MEM_TILES = TINY_DEM_TILES if tiny else SMALL_DEM_TILES
        mapdata.MEM_MB = TINY_MAPDATA_MB if tiny else SMALL_MAPDATA_MB
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
TINY_SHED_MB = 1000      # (an 8-12 GB machine: TINY_GB)
# A desktop's ceiling by its RAM, (below GB, MB); none from the last up. Before 2026-11-11 a desktop had none: a 32 GB
# PC's server grew to 20 GB after 15 miles of Los Angeles, the system killed it after ~90 miles, other players' games
# swapped (Discord, GitHub #893 #1026); a road worker's working set in a city is ~0.9-1.3 GB (drive_mem, 2026-11-11)
DESKTOP_SHED_MB = ((40.0, 2000), (56.0, 3000))
SHED_MIN_S = 90.0        # a worker drops its caches at most this often (a dense city's working set comes straight back)
# The system's own memory, every profile: a worker holding at least SHED_FLOOR_MB drops its caches between jobs (at most
# every SHED_MIN_S) while less than LOW_FREE_GB is free (Linux MemAvailable, Windows available physical memory): a
# ceiling by process cannot know the game's and the other programs' share, and a machine that starts swapping makes the
# game hitch long before the server's own ceilings are reached (RAM is the strongest predictor of hitches in the
# performance reports, 8 GB 1,435 / 16 GB 540 / 32 GB 308 over 100 ms an hour, 2026-11-11)
LOW_FREE_FRAC = 0.08     # of the RAM ...
LOW_FREE_MIN_GB = 1.0    # ... at least ...
LOW_FREE_MAX_GB = 3.0    # ... at most
LOW_FREE_GB = 0.0        # (apply(); 0 off)
SHED_FLOOR_MB = 600
_shedders = []
shed_count = 0
low_free_sheds = 0
tree_sheds = 0
_last_shed = None


def register_shed(fn):
    """fn(): drop a cache of this process (called between jobs, in the worker's own thread)."""
    _shedders.append(fn)


def memory_mb(private=False):
    """This process's resident + swapped memory in MB (Linux /proc; Windows: private bytes), or None. private: its
    own resident pages only (Linux RssAnon: not the shared libraries' pages every process maps; the whole server's
    table, tree_publish)."""
    try:
        rss = swap = 0
        key = "RssAnon:" if private else "VmRSS:"
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith(key):
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


def maybe_shed(ceiling=True, collect=True):
    """Between jobs: the registered caches dropped when this process is over SHED_MB, or holds SHED_FLOOR_MB while the
    whole server is over TREE_MB (and this process over its share: tree_should_shed) or the system has less than
    LOW_FREE_GB free. True when it shed. ceiling False: not SHED_MB (the server's own process: tree_publisher);
    collect False: no full garbage collection after it (_shed)."""
    if not (SHED_MB or LOW_FREE_GB or TREE_MB) or not _shedders:
        return False
    import time
    global _last_shed, low_free_sheds, tree_sheds
    priv = memory_mb(private=True) if TREE_MB else None
    if priv is not None:
        tree_publish(priv)
    if _last_shed is not None and time.monotonic() - _last_shed < SHED_MIN_S:
        return False
    mb = memory_mb()
    if mb is None:
        return False
    why = None
    if ceiling and SHED_MB and mb >= SHED_MB:
        why = f"over {SHED_MB}"
    elif mb >= SHED_FLOOR_MB:
        if priv is not None:
            total, n = tree_total()
            if tree_should_shed(priv, total, n, TREE_MB):
                why = f"server at {total:.0f} MB of {TREE_MB}"
                tree_sheds += 1
        if why is None and LOW_FREE_GB:
            free = free_gb()
            if free is not None and free < LOW_FREE_GB:
                why = f"{free:.1f} GB free"
                low_free_sheds += 1
    if why is None:
        return False
    _last_shed = time.monotonic()
    _shed(mb, why, collect)
    return True


def _shed(mb, why, collect=True):
    """The registered caches dropped and the memory given back (maybe_shed, the idle shed). collect: a full garbage
    collection first (not in the server's own process: its event loop would wait for it)."""
    global shed_count
    import gc
    import logging
    for fn in list(_shedders):
        try:
            fn()
        except Exception:
            pass
    if collect:
        gc.collect()
    trim()
    shed_count += 1
    after = memory_mb()
    logging.getLogger("earthd.worker").info("memory: pid %d at %.0f MB (%s): caches dropped, now %.0f MB",
                                            os.getpid(), mb or 0, why, after or 0)


# A worker that has run no job for IDLE_SHED_S drops its caches once (every profile): the far-tile workers the 3D map
# set going, every worker on a stock level, kept what they had for as long as the game ran (a desktop: 5.7 -> 9.3 GB in
# 17 workers after a minute of the 3D map, still there on West Coast USA; GitHub #1026). The next job reads its cells
# from disk again. A job waits for a shed under way (JOB_LOCK, pools._call).
IDLE_SHED_S = float(os.environ.get("BNGE_IDLE_SHED_S", "300"))     # (0: off)
IDLE_CHECK_S = 30.0
JOB_LOCK = None
_last_job_end = None
_idle_shed = False
_idle_thread = False


def job_lock():
    """The lock a worker's job holds (pools._call), made once."""
    global JOB_LOCK
    if JOB_LOCK is None:
        import threading
        JOB_LOCK = threading.Lock()
    return JOB_LOCK


def job_ended():
    """A job finished in this worker (pools._call): the idle clock starts again, its watcher started once."""
    global _last_job_end, _idle_shed, _idle_thread
    import time
    _last_job_end, _idle_shed = time.monotonic(), False
    if not _idle_thread and IDLE_SHED_S > 0 and _shedders:
        import threading
        _idle_thread = True
        threading.Thread(target=_idle_loop, daemon=True, name="idle-shed").start()


def idle_shed_now(now=None):
    """The idle shed's test: True when it shed (idle IDLE_SHED_S since the last job, not shed since, no job running)."""
    global _idle_shed
    import time
    now = time.monotonic() if now is None else now
    if IDLE_SHED_S <= 0 or _idle_shed or _last_job_end is None or now - _last_job_end < IDLE_SHED_S or not _shedders:
        return False
    lock = job_lock()
    if not lock.acquire(blocking=False):
        return False
    try:
        if _idle_shed or now - _last_job_end < IDLE_SHED_S:
            return False
        _idle_shed = True
        _shed(memory_mb(), f"idle {now - _last_job_end:.0f} s")
        return True
    finally:
        lock.release()


def _idle_loop():
    import time
    while True:
        time.sleep(IDLE_CHECK_S)
        try:
            idle_shed_now()
            if TREE_MB:
                tree_publish(memory_mb(private=True))   # (an idle worker still holds its memory: TREE_FRESH_S)
        except Exception:
            pass


# The whole server's memory, every profile: its processes (the server's own and every worker) each keep their memory in
# a slot of a small shared block (the server makes it before its workers start; they find it by name, TREE_ENV); a
# worker over its share of TREE_MB (TREE_FRAC of the RAM) drops its caches between jobs while the sum is over it. Each
# process's caches are capped on their own, but a desktop runs ~20 processes: a 32 GB PC's server held 19.5 GB after
# eight minutes of a fresh Los Angeles (drive_mem, 2026-11-11; Discord: "20 GB after 15 miles of LA", the system's
# out-of-memory killer after ~90 miles), each worker under its own caps.
TREE_FRAC = 0.33            # of the RAM for the whole server (16 GB: 5.3 GB, 32 GB: 10.6 GB, 64 GB: 21 GB)
# OFF by default (BNGE_TREE=on, or BNGE_TREE_MB=<MB>): measured 2026-11-11 (drive_mem, LA -> London -> NYC, cold) the
# budget was out of reach: a shed gives back ~0.3-0.6 GB of a worker's 1-2.4 GB, the rest is not in the registered
# caches and comes straight back, so a 16 GB machine's server stayed at ~13 GB private (budget 5.3) while its workers
# shed every 90 s: far tiles -35 % (2 far-tile processes re-reading their DEM tiles), London's road geotiles 23 -> 59 s
# median; on the 32 GB profile -13 % peak for +5 % CPU. The free-memory shed (LOW_FREE_GB) covers a machine that is
# really short; the table stays for when the floors come down.
TREE_ON = os.environ.get("BNGE_TREE", "off").lower() in ("on", "1", "true")
TREE_MIN_MB = 2500
TREE_MB = 0                 # (apply(); 0 off)
TREE_ENV = "BNGE_MEMTABLE"
TREE_SLOTS = 64
TREE_FRESH_S = 120.0        # a slot not written this long is a process gone (the workers write after every job, and
TREE_PUBLISH_S = 30.0       # ... every this often when idle: _idle_loop; the server's own process: tree_publisher)
_TREE_SLOT = None
_tree_shm = None
_tree_owner = False
_tree_slot = None


def tree_mb(gb):
    """The whole server's memory budget (MB) for RAM gb (None: unknown, no budget)."""
    if gb is None:
        return 0
    return int(max(TREE_MIN_MB, TREE_FRAC * gb * 1000))


def tree_should_shed(mine, total, n, budget_mb):
    """Should a process holding `mine` MB drop its caches, the server at `total` MB in n processes against budget_mb?
    Over the budget, the processes over an equal share of it: the big ones give their memory back first."""
    if not budget_mb or total <= budget_mb or mine < SHED_FLOOR_MB:
        return False
    return mine >= budget_mb / max(1, n)


def _tree_struct():
    global _TREE_SLOT
    if _TREE_SLOT is None:
        import struct
        _TREE_SLOT = struct.Struct("<qdd")      # pid, MB, time.time() of the write
    return _TREE_SLOT


def tree_init():
    """The server's process, before its workers start: the shared block made and named for them. Returns its name."""
    global _tree_shm, _tree_owner
    try:
        from multiprocessing import shared_memory
        size = TREE_SLOTS * _tree_struct().size
        _tree_shm = shared_memory.SharedMemory(create=True, size=size)
        _tree_shm.buf[:size] = bytes(size)
        _tree_owner = True
        os.environ[TREE_ENV] = _tree_shm.name
        return _tree_shm.name
    except Exception:
        _tree_shm = None
        return None


def tree_close():
    global _tree_shm
    if _tree_shm is not None:
        try:
            _tree_shm.close()
            if _tree_owner:
                _tree_shm.unlink()
        except Exception:
            pass
        _tree_shm = None


def _tree_attach():
    global _tree_shm
    if _tree_shm is None and os.environ.get(TREE_ENV):
        try:
            from multiprocessing import shared_memory
            try:        # (3.13+: not handed to the resource tracker, which would unlink the server's block at our exit)
                _tree_shm = shared_memory.SharedMemory(name=os.environ[TREE_ENV], track=False)
            except TypeError:
                _tree_shm = shared_memory.SharedMemory(name=os.environ[TREE_ENV])
                try:
                    from multiprocessing import resource_tracker
                    resource_tracker.unregister(_tree_shm._name, "shared_memory")
                except Exception:
                    pass
        except Exception:
            os.environ.pop(TREE_ENV, None)      # (not again)
    return _tree_shm


def tree_publish(mb, now=None):
    """This process's memory (MB) into its slot (taken at the first write: its own pid's, else a free or stale one).
    Best effort: False when there is no block or no slot."""
    global _tree_slot
    shm = _tree_attach()
    if shm is None or mb is None:
        return False
    import time
    now = time.time() if now is None else now
    st, pid = _tree_struct(), os.getpid()
    try:
        if _tree_slot is None or st.unpack_from(shm.buf, _tree_slot * st.size)[0] != pid:
            _tree_slot = None
            start = pid % TREE_SLOTS
            for i in range(TREE_SLOTS):
                k = (start + i) % TREE_SLOTS
                p, _, t = st.unpack_from(shm.buf, k * st.size)
                if p in (0, pid) or now - t > TREE_FRESH_S:
                    st.pack_into(shm.buf, k * st.size, pid, float(mb), now)
                    if st.unpack_from(shm.buf, k * st.size)[0] == pid:
                        _tree_slot = k
                        break
            if _tree_slot is None:
                return False
        st.pack_into(shm.buf, _tree_slot * st.size, pid, float(mb), now)
        return True
    except Exception:
        return False


def tree_total(now=None):
    """(MB, processes) of the slots written within TREE_FRESH_S; (0.0, 0) without the block."""
    shm = _tree_attach()
    if shm is None:
        return 0.0, 0
    import time
    now = time.time() if now is None else now
    st = _tree_struct()
    total, n = 0.0, 0
    try:
        for k in range(TREE_SLOTS):
            p, mb, t = st.unpack_from(shm.buf, k * st.size)
            if p and now - t <= TREE_FRESH_S:
                total += mb
                n += 1
    except Exception:
        return 0.0, 0
    return total, n


def tree_publisher():
    """The server's own process: its memory into the block every TREE_PUBLISH_S / 3 (a thread), and its own registered
    caches (earthd: its DEM tiles and map data cells) dropped as a worker's are, by the whole server's budget or the
    free memory (not by a worker's ceiling: SHED_MB; no full collection)."""
    import threading
    import time

    def loop():
        while True:
            try:
                if _shedders:
                    maybe_shed(ceiling=False, collect=False)
                else:
                    tree_publish(memory_mb(private=True))
            except Exception:
                pass
            time.sleep(TREE_PUBLISH_S / 3)
    threading.Thread(target=loop, daemon=True, name="mem-table").start()


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
    """Give the freed memory back now: glibc's malloc_trim; on Windows the C runtime's heap compacted (msvcrt.heapmin:
    its free blocks back to the system, after a shed; before 2026-11-11 nothing there, GitHub #893)."""
    if sys.platform == "win32":
        try:
            import msvcrt
            msvcrt.heapmin()
        except Exception:       # (OSError when there is nothing to give back)
            pass
        return
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
