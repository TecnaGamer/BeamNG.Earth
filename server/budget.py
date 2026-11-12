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
# The lean tiers (tier()): an 8 GB PC (LEAN_GB) and one with 4-6 GB (EMERGENCY_GB). BeamNG.drive itself asks for 16 GB
# (its minimum since 0.3x; it holds ~6.5-7.5 GB private on Windows), so an 8 GB machine leaves the server ~1-1.5 GB before
# everything swaps: the small profile's eight processes held ~6 GB + 2 GB of swap after 2 minutes of a cold Baltimore
# in a 6 GB scope (drive_mem, 2026-11-12), and the players' reports showed 8 GB machines at 1,493 hitches over 100 ms an
# hour (16 GB 540). Fewest processes (earthd LEAN_PROCS: the server and one road-tile process that also builds the far
# buildings and the trees, replaced every LEAN_TASKS_PER_CHILD jobs; the blocks and the far tiles in the server's own
# threads), smaller caches, a coarser far field; the emergency tier's extras off too (earthd EMERGENCY_OFF).
# BNGE_LEAN=off: the small profile's tiny sizes below LEAN_GB as before; BNGE_BUDGET=lean / emergency forces one.
LEAN_ON = os.environ.get("BNGE_LEAN", "on").lower() not in ("off", "0", "false", "no")
LEAN_GB = 10.0              # below this much RAM: the lean tier (an 8 GB PC shows ~7.7-8.4 GB)
EMERGENCY_GB = 6.0          # ... below this the emergency tier (4 GB: below what the game needs to run at all)
LEAN_DEM_TILES = 64
LEAN_MAPDATA_MB = 128
LEAN_WALL_CELLS = 8
LEAN_FACTS_CELLS = 96
LEAN_FIN_MB = 24
LEAN_FIN_DECODED = 64
LEAN_IMAGERY_TILES = 128    # imagery.MEM_TILES (512: ~100 MB of decoded source tiles a process)
LEAN_PMTILES_MB = 32        # pmtiles.MEM_TILES_MB (96)
LEAN_RANGEREAD_MB = 16      # rangeread.MEM_BYTES (64 MB)
LEAN_SEA_CELLS = 6          # seacarve.MEM_CELLS (24 rasters of ~10 MB)
LEAN_PHOTOSURFACE_TILES = 32    # photosurface.MEM_TILES (96)
LEAN_FAR_K = 0.6            # (instead of SMALL_FAR_K)
LEAN_FAR_TEX = 256
EMERGENCY_DEM_TILES = 48
EMERGENCY_MAPDATA_MB = 96
EMERGENCY_FAR_K = 0.45
EMERGENCY_FAR_TEX = 128
LEAN_ROAD_PROCESSES = 1
LEAN_FAR_WORKERS = 1
LEAN_SHED_MB = 700          # a worker's ceiling (SHED_MB) ...
LEAN_ROTATE_MB = 1000       # ... and its pool's rotation (ROTATE_MB)
EMERGENCY_SHED_MB = 500
EMERGENCY_ROTATE_MB = 800
TIERS = ("desktop", "small", "lean", "emergency")
# The lean tiers' road-tile process is replaced by a fresh one after this many jobs (pools.tasks_per_child_kw; its
# queue kept): its caches' sheds gave back ~0.3 of 1-1.6 GB, a rotation never ran with its queue always full (the old
# process would finish the whole queue beside the new one), and it grew to 1.58 GB in 8 minutes of Baltimore + I-95
# (drive_mem, 2026-11-12). BNGE_TASKS_PER_CHILD=<n>, 0 off.
LEAN_TASKS_PER_CHILD = 40


def tasks_per_child():
    """The lean tiers' jobs per road process (LEAN_TASKS_PER_CHILD; BNGE_TASKS_PER_CHILD overrides, 0 off)."""
    v = os.environ.get("BNGE_TASKS_PER_CHILD", "")
    try:
        return max(0, int(v)) if v != "" else LEAN_TASKS_PER_CHILD
    except ValueError:
        return LEAN_TASKS_PER_CHILD


# The player's Max RAM for BeamNG.Earth (the settings page's maxRamGb; cpupin.apply_player_settings or earthd
# --max-ram-gb put it in SETTING_ENV, which the worker processes inherit): the whole server's limit (native
# membudget.rs: Auto = a third of the RAM below 22 GB, at least 1.5 GB; 60 % of it from 22 GB up, at most 24 GB;
# an explicit one at most 90 % of it), its tier (by the limit:
# Auto gives every machine the tier its RAM alone gave before the setting), the desktop tier's cache sizes and worker
# ceilings by it (desktop_sizes), and the watchdog that holds the server to it (memwatch.py). LIMIT_MB: this process's
# (apply()); 0 while the RAM is unknown and the setting Auto (no limit, the desktop sizes, as before).
SETTING_ENV = "BNGE_MAX_RAM_GB"
LIMIT_MB = 0.0
OVER = False                # (memwatch.py: the server over 90 % of its limit; no far-tile burst processes start)


def setting_gb():
    """The player's Max RAM in GB (2^30 bytes) from SETTING_ENV; 0.0 for Auto (unset, "auto", 0 or unreadable)."""
    v = os.environ.get(SETTING_ENV, "").strip().lower()
    if v in ("", "auto"):
        return 0.0
    try:
        x = float(v)
    except ValueError:
        return 0.0
    return x if 0 < x < 1e6 else 0.0


def total_mb(gb=None):
    """The RAM in MB (2^20 bytes) for gb GB (10^9 bytes; None: total_gb()); 0.0 when unknown."""
    gb = total_gb() if gb is None else gb
    return gb * 1e9 / 2 ** 20 if gb else 0.0


def limit_mb(gb=None, setting=None):
    """The whole server's RAM limit in MB for RAM gb (None: this machine's) and the setting in GB (None: SETTING_ENV;
    0: Auto); 0.0 when the RAM is unknown and the setting Auto."""
    setting = setting_gb() if setting is None else setting
    mb = total_mb(gb)
    if not mb and not setting:
        return 0.0
    import fast
    return float(fast.ram_limit_mb(setting, mb))


def tier(gb=None, setting=None):
    """The memory tier: 'desktop', 'small', 'lean' or 'emergency', by the RAM limit (limit_mb: Auto keeps the tiers
    the RAM alone gave, SMALL_GB / LEAN_GB / EMERGENCY_GB). BNGE_BUDGET=<tier> forces one (lean / emergency need
    BNGE_LEAN on; BNGE_LEAN=off: 'small' instead of them); the lean tiers are small machines too."""
    force = os.environ.get("BNGE_BUDGET", "").lower()
    if force in TIERS and (LEAN_ON or force in ("desktop", "small")):
        return force
    mb = limit_mb(gb, setting)
    if not mb:
        return "desktop"
    import fast
    t = fast.ram_tier(mb)
    if not LEAN_ON and t in ("lean", "emergency"):
        return "small"
    return t


def desktop_sizes(limit=None):
    """The desktop tier's (DEM tiles, map data MB, shed MB, rotate MB, road processes at most, far processes at most)
    for a limit in MB (None: LIMIT_MB); None without a limit (the desktop sizes of the modules, no ceilings)."""
    limit = LIMIT_MB if limit is None else limit
    if not limit:
        return None
    import fast
    return tuple(int(x) for x in fast.ram_desktop_sizes(limit))


def lean(gb=None):
    """Is this machine on one of the lean tiers (lean, emergency)?"""
    return tier(gb) in ("lean", "emergency")


GAME_MIN_GB = 16            # BeamNG.drive's own minimum (its Steam page and docs.beamng.com, 2026)


def memory_notice(gb=None, tier_=None):
    """(title, text) for the player on a lean tier (the launcher at start; the emergency one in the game too: hello
    memoryNote), or None. Plain words: the RAM, what the game needs, what BeamNG.Earth does about it, what helps."""
    gb = total_gb() if gb is None else gb
    tier_ = tier(gb) if tier_ is None else tier_
    if gb is None or tier_ not in ("lean", "emergency"):
        return None
    have = f"{gb * 1e9 / 2 ** 30:.0f} GB" if gb > 0 else "very little"
    if tier_ == "emergency":
        return ("Not enough memory", (
            f"This computer has {have} of memory. BeamNG.drive needs {GAME_MIN_GB} GB, and the game alone does not fit "
            f"in {have}: expect long freezes, and the game or BeamNG.Earth may close by itself. BeamNG.Earth runs its "
            "smallest world server, but it cannot make up for the missing memory. Use the Low quality preset, close "
            "every other program before playing, or play on a computer with more memory."))
    return ("Low memory", (
        f"This computer has {have} of memory; BeamNG.drive asks for {GAME_MIN_GB} GB. BeamNG.Earth runs its "
        "low-memory world server, but expect some stutter while the world loads. What helps most: the Low quality "
        "preset (pause menu > BeamNG.Earth > Graphics; a first start picks it), and closing other programs (web "
        "browsers especially)."))


def total_gb():
    """The machine's RAM in GB (None if unknown). BNGE_TOTAL_GB: taken as this much (tests of the smaller tiers on a
    bigger machine)."""
    if os.environ.get("BNGE_TOTAL_GB"):
        try:
            return float(os.environ["BNGE_TOTAL_GB"])
        except ValueError:
            pass
    try:
        phys = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
        lim = cgroup_limit_gb() if CGROUP_MEM else None
        return min(phys, lim) if lim else phys      # (a container's or a scope's memory limit: CGROUP_MEM)
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


# Linux: a memory limit on the server's control group (a container, systemd's MemoryMax, the low-memory tests' capped
# scope) counts as the machine's free memory when it is the smaller: /proc/meminfo tells the whole machine's, and a
# server capped at 1.5 GB on a 64 GB PC never shed by the free memory while it swapped at its limit (2026-11-12).
# BNGE_CGROUP_MEM=off: MemAvailable alone, as before.
CGROUP_MEM = os.environ.get("BNGE_CGROUP_MEM", "on").lower() not in ("off", "0", "false", "no")
_cg_dir = None


def _cgroup_dir():
    """This process's cgroup v2 folder (cached), or "" when there is none."""
    global _cg_dir
    if _cg_dir is None:
        _cg_dir = ""
        try:
            with open("/proc/self/cgroup") as f:
                for line in f:
                    if line.startswith("0::"):
                        _cg_dir = CG_ROOT + line[3:].strip()
        except OSError:
            pass
    return _cg_dir


CG_ROOT = "/sys/fs/cgroup"


def cgroup_limit_gb(d=None, root=CG_ROOT):
    """The tightest memory.max of this process's cgroup (folder d under root) and its parents in GB; None when none is
    limited."""
    d = _cgroup_dir() if d is None else d
    best = None
    while d and d.startswith(root) and len(d) > len(root):
        try:
            with open(os.path.join(d, "memory.max")) as f:
                mx = f.read().strip()
            if mx != "max":
                best = int(mx) / 1e9 if best is None else min(best, int(mx) / 1e9)
        except (OSError, ValueError):
            pass
        d = os.path.dirname(d)
    return best


def cgroup_free_gb(d=None, root=CG_ROOT):
    """The memory left under the tightest limit of this process's cgroup (folder d under root) and its parents
    (memory.max less what is charged, the inactive file cache counted free), in GB; None when none is limited (or not
    Linux)."""
    d = _cgroup_dir() if d is None else d
    best = None
    while d and d.startswith(root) and len(d) > len(root):
        try:
            with open(os.path.join(d, "memory.max")) as f:
                mx = f.read().strip()
            if mx != "max":
                with open(os.path.join(d, "memory.current")) as f:
                    cur = int(f.read())
                inactive = 0
                try:
                    with open(os.path.join(d, "memory.stat")) as f:
                        for line in f:
                            if line.startswith("inactive_file "):
                                inactive = int(line.split()[1])
                                break
                except (OSError, ValueError):
                    pass
                left = max(0, int(mx) - cur + inactive) / 1e9
                best = left if best is None else min(best, left)
        except (OSError, ValueError):
            pass
        d = os.path.dirname(d)
    return best


def free_gb():
    """The RAM free for new work now, in GB (Linux MemAvailable, or the room under a cgroup memory limit when smaller:
    CGROUP_MEM; Windows ullAvailPhys; None if unknown)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    free = int(line.split()[1]) * 1024 / 1e9
                    if CGROUP_MEM:
                        cg = cgroup_free_gb()
                        if cg is not None:
                            free = min(free, cg)
                    return free
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


def small(gb=None, setting=None):
    """Is this a small machine (the small profile, the lean tiers too: tier(), by the RAM limit)? BNGE_BUDGET=small /
    desktop / lean / emergency overrides."""
    force = os.environ.get("BNGE_BUDGET", "").lower()
    if force in TIERS:
        return force != "desktop"
    return tier(gb, setting) != "desktop"


def small_machine(gb=None):
    """A machine with less than SMALL_GB of RAM, whatever the RAM limit (the CPU's share for the server, cpupin.split:
    the setting is about memory, not cores). BNGE_BUDGET overrides as small() does."""
    force = os.environ.get("BNGE_BUDGET", "").lower()
    if force in TIERS:
        return force != "desktop"
    gb = total_gb() if gb is None else gb
    return gb is not None and gb < SMALL_GB


SMALL_FACTS_CELLS = 192         # roads._FACTS_MEM entries on a small machine (~24 cells; a Steam Deck: ~9 server processes)


def shed_mb(is_small, gb, tier_=None, limit=None):
    """A worker's memory ceiling (SHED_MB) for the profile and the machine's RAM (GB, None unknown); 0: none.
    tier_: the lean tiers' own (tier()); a desktop's by the RAM limit (limit MB, None: limit_mb(gb); desktop_sizes)."""
    if tier_ == "emergency":
        return EMERGENCY_SHED_MB
    if tier_ == "lean":
        return LEAN_SHED_MB
    if is_small:
        return TINY_SHED_MB if gb is not None and gb < TINY_GB else SMALL_SHED_MB
    sizes = desktop_sizes(limit_mb(gb) if limit is None and gb is not None else (limit or 0))
    return sizes[2] if sizes else 0


def low_free_gb(gb):
    """The free memory (GB) under which a worker drops its caches between jobs (LOW_FREE_*), for RAM gb."""
    if gb is None:
        return LOW_FREE_MIN_GB
    return min(LOW_FREE_MAX_GB, max(LOW_FREE_MIN_GB, LOW_FREE_FRAC * gb))


def apply(is_small=None):
    """Set the cache sizes of this process's modules for the profile; returns whether it is the small one."""
    is_small = small() if is_small is None else is_small
    global SHED_MB, LOW_FREE_GB, TREE_MB, ROTATE_MB, TIER, LIMIT_MB
    gb = total_gb()
    LIMIT_MB = limit_mb(gb)
    TIER = tier(gb) if is_small else "desktop"
    if TIER == "desktop" and is_small:
        TIER = "small"
    SHED_MB = shed_mb(is_small, gb, TIER, limit=LIMIT_MB)
    if os.environ.get("BNGE_SHED_MB"):              # (A/B: the ceiling in MB, 0 off)
        SHED_MB = int(os.environ["BNGE_SHED_MB"])
    LOW_FREE_GB = low_free_gb(gb)
    if os.environ.get("BNGE_LOW_FREE_GB"):          # (A/B: the free memory in GB, 0 off)
        LOW_FREE_GB = float(os.environ["BNGE_LOW_FREE_GB"])
    TREE_MB = tree_mb(gb) if TREE_ON else 0
    if os.environ.get("BNGE_TREE_MB"):              # (A/B: the whole server's budget in MB, 0 off)
        TREE_MB = int(os.environ["BNGE_TREE_MB"])
    ROTATE_MB = rotate_mb(is_small, gb, TIER, limit=LIMIT_MB)
    if os.environ.get("BNGE_ROTATE_MB"):            # (A/B: the pools' rotation ceiling in MB, 0 off)
        ROTATE_MB = int(os.environ["BNGE_ROTATE_MB"])
    if is_small:
        import dem
        import farfield
        import mapdata
        import roadwidth
        tiny = gb is not None and gb < TINY_GB
        emergency, leanest = TIER == "emergency", TIER in ("lean", "emergency")
        dem.MEM_TILES = EMERGENCY_DEM_TILES if emergency else LEAN_DEM_TILES if leanest else \
            TINY_DEM_TILES if tiny else SMALL_DEM_TILES
        mapdata.MEM_MB = EMERGENCY_MAPDATA_MB if emergency else LEAN_MAPDATA_MB if leanest else \
            TINY_MAPDATA_MB if tiny else SMALL_MAPDATA_MB
        roadwidth.WALL_CELLS = LEAN_WALL_CELLS if leanest else SMALL_WALL_CELLS
        import roads
        roads.FACTS_MEM_KEPT = LEAN_FACTS_CELLS if leanest else SMALL_FACTS_CELLS   # (~0.5 MB a city cell's facts)
        import imagery
        gb = total_gb() or 16.0
        imagery.MEM_FINISHED_MB = int(min(imagery.MEM_FINISHED_MB, max(SMALL_FIN_MB, round(FIN_MB_PER_GB * gb))))
        imagery.MEM_DECODED = min(imagery.MEM_DECODED, SMALL_FIN_DECODED)
        if leanest:
            imagery.MEM_FINISHED_MB = min(imagery.MEM_FINISHED_MB, LEAN_FIN_MB)
            imagery.MEM_DECODED = min(imagery.MEM_DECODED, LEAN_FIN_DECODED)
            # (the per-process caches the small profile keeps at a desktop's size: a lean block process held 291 MB
            # of imagery arrays after two minutes of Baltimore, 2026-11-12)
            import pmtiles
            import photosurface
            import rangeread
            import seacarve
            imagery.MEM_TILES = min(imagery.MEM_TILES, LEAN_IMAGERY_TILES)
            pmtiles.MEM_TILES_MB = min(pmtiles.MEM_TILES_MB, LEAN_PMTILES_MB)
            rangeread.MEM_BYTES = min(rangeread.MEM_BYTES, LEAN_RANGEREAD_MB * 2 ** 20)
            seacarve.MEM_CELLS = min(seacarve.MEM_CELLS, LEAN_SEA_CELLS)
            photosurface.MEM_TILES = min(photosurface.MEM_TILES, LEAN_PHOTOSURFACE_TILES)
        far_k = EMERGENCY_FAR_K if emergency else LEAN_FAR_K if leanest else SMALL_FAR_K
        far_tex = EMERGENCY_FAR_TEX if emergency else LEAN_FAR_TEX if leanest else SMALL_FAR_TEX
        for cells, table in farfield.K_TABLE.items():       # (read when the server makes its Layout)
            farfield.K_TABLE[cells] = {lvl: k * far_k for lvl, k in table.items()}
        for cells, tex in farfield.TEX_DEFAULT.items():
            farfield.TEX_DEFAULT[cells] = tuple(min(t, far_tex) for t in tex)
    else:
        sizes = desktop_sizes()
        if sizes:           # (the desktop tier by the RAM limit: a 32 GB PC: ~315 DEM tiles, 160 MB of map data)
            import dem
            import mapdata
            dem.MEM_TILES = min(dem.MEM_TILES, sizes[0])
            mapdata.MEM_MB = min(mapdata.MEM_MB, sizes[1])
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
TIER = "desktop"         # (apply(): tier() of this process's machine)
SMALL_SHED_MB = 1400     # (1200 on a Deck in Manhattan: two road workers dropped every ~15 s, 1206 -> 822 MB, their working set)
TINY_SHED_MB = 1000      # (an 8-12 GB machine: TINY_GB)
# A desktop's ceiling follows the RAM limit (desktop_sizes: 3000 MB from a 16 GB limit up, a 32 GB PC's Auto).
# Before 2026-11-11 a desktop had none: a 32 GB PC's server grew to 20 GB after 15 miles of Los Angeles, the system
# killed it after ~90 miles, other players' games swapped (Discord, GitHub #893 #1026); a road worker's working set in
# a city is ~0.9-1.3 GB (drive_mem, 2026-11-11)
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
            # (argtypes: without them ctypes passed the pseudo handle (2**64 - 1 as HANDLE) as a C int, OverflowError,
            # and this returned None on every Windows machine: the worker memory ceiling never ran there, 2026-11-11 -
            # 16 GB test PC: eight workers at 0.9-3.6 GB private each, 33 of 36.8 GB committed after 30 minutes)
            k32.K32GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
            k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
            if k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return c.PrivateUsage / 2 ** 20
        except Exception:
            pass
    return None


def process_mb(pid):
    """Another process's memory in MB as memory_mb counts its own (Linux: resident + swapped; Windows: private bytes),
    or None."""
    try:
        rss = swap = 0
        with open(f"/proc/{int(pid)}/status") as f:
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
            import perfstats
            got = perfstats._win_proc(int(pid))
            return got[1] if got else None
        except Exception:
            pass
    return None


# A pool whose worker is still over ROTATE_MB (pools.RespawningPool's memory watch, every ROTATE_CHECK_S) is replaced
# by a fresh one, at most every ROTATE_MIN_S: the old one's queued and running jobs finish in its own processes, which
# then exit and give everything back. The caches maybe_shed drops are not all a worker keeps (native memory of the map
# readers, module-level caches): on the 16 GB Windows test PC (2026-11-11), with the ceiling working, four workers were
# at 1.8-2.3 GB private after 25 minutes and the server at 14.6 GB. 0: off; BNGE_ROTATE_MB. A desktop by its RAM
# (below GB, MB), none from the last up: a 32 GB profile's road workers were at 2.0-2.4 GB after 25 minutes of LA,
# London and New York with their caches shed (drive_mem, 2026-11-11), the server at 25 GB, and growing with each
# new city: the same growth as the 16 GB PC's, later.
ROTATE_MB = 0
SMALL_ROTATE_MB = 1800
# (a desktop's: desktop_sizes, by the RAM limit)
ROTATE_CHECK_S = 30.0
ROTATE_MIN_S = 300.0


def rotate_mb(is_small, gb, tier_=None, limit=None):
    """The pools' rotation ceiling (ROTATE_MB) for the profile and the machine's RAM (GB, None unknown); 0: none.
    tier_: the lean tiers' own (tier()); a desktop's by the RAM limit (limit MB, None: limit_mb(gb); desktop_sizes)."""
    if tier_ == "emergency":
        return EMERGENCY_ROTATE_MB
    if tier_ == "lean":
        return LEAN_ROTATE_MB
    if is_small:
        return SMALL_ROTATE_MB
    sizes = desktop_sizes(limit_mb(gb) if limit is None and gb is not None else (limit or 0))
    return sizes[3] if sizes else 0


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
    mb = memory_mb()
    if mb is None:
        return False
    asked = shed_asked(mb)
    if asked and (_last_shed is None or time.monotonic() - _last_shed >= ASKED_MIN_S):
        _last_shed = time.monotonic()
        _shed(mb, asked, collect)
        return True
    if _last_shed is not None and time.monotonic() - _last_shed < SHED_MIN_S:
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


def asked_idle_shed():
    """An idle worker answers the watchdog's request too (maybe_shed answers it after a job). True when it shed."""
    global _last_shed
    import time
    if not _shedders or not os.environ.get(TREE_ENV):
        return False
    lock = job_lock()
    if not lock.acquire(blocking=False):
        return False            # (a job runs: maybe_shed after it)
    try:
        mb = memory_mb()
        why = shed_asked(mb) if mb is not None else None
        if not why or (_last_shed is not None and time.monotonic() - _last_shed < ASKED_MIN_S):
            return False
        _last_shed = time.monotonic()
        _shed(mb, why)
        return True
    finally:
        lock.release()


def _idle_loop():
    import time
    while True:
        time.sleep(IDLE_CHECK_S)
        try:
            idle_shed_now()
            asked_idle_shed()
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


# The memory watchdog's requests (memwatch.py, in the server's main process) in a header after the slots: (epoch,
# target MB, time). A worker that sees a new epoch drops its caches between jobs (maybe_shed, its idle check) when it
# holds at least the target (the server's limit shared out by process) and SHED_FLOOR_MB, at most every ASKED_MIN_S.
ASKED_MIN_S = 20.0
_asked_seen = 0


def shed_ask(target_mb):
    """The server's main process: every worker holding target_mb or more is asked to drop its caches (a new epoch in
    the header). False without the block."""
    shm = _tree_attach()
    if shm is None:
        return False
    import time
    st = _tree_struct()
    try:
        epoch = st.unpack_from(shm.buf, TREE_SLOTS * st.size)[0]
        st.pack_into(shm.buf, TREE_SLOTS * st.size, epoch + 1, float(target_mb), time.time())
        return True
    except Exception:
        return False


def shed_asked(mb):
    """A worker holding mb: the reason when the watchdog asked for a shed it has not answered and mb is over its
    target, else None (the epoch is taken as seen either way)."""
    global _asked_seen
    shm = _tree_attach() if os.environ.get(TREE_ENV) else None
    if shm is None:
        return None
    st = _tree_struct()
    try:
        epoch, target, _t = st.unpack_from(shm.buf, TREE_SLOTS * st.size)
    except Exception:
        return None
    if epoch <= _asked_seen:
        return None
    _asked_seen = epoch
    if mb < max(SHED_FLOOR_MB, target):
        return None
    return f"the server over its memory limit, this process over {target:.0f} MB"


def tree_init():
    """The server's process, before its workers start: the shared block made and named for them (the slots and the
    watchdog's header). Returns its name."""
    global _tree_shm, _tree_owner
    try:
        from multiprocessing import shared_memory
        size = (TREE_SLOTS + 1) * _tree_struct().size
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
    """Give the freed memory back now (glibc: malloc_trim; Windows: HeapCompact of the process's heaps, which
    decommits their large free blocks: the C runtime's malloc is the process heap there; BNGE_WIN_TRIM=0 off)."""
    libc = _libc() if sys.platform.startswith("linux") else None
    if libc is not None and hasattr(libc, "malloc_trim"):
        libc.malloc_trim(0)
    elif sys.platform == "win32" and os.environ.get("BNGE_WIN_TRIM", "1") != "0":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32")
            k32.GetProcessHeaps.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
            k32.GetProcessHeaps.restype = wintypes.DWORD
            k32.HeapCompact.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            k32.HeapCompact.restype = ctypes.c_size_t
            n = k32.GetProcessHeaps(0, None)
            heaps = (wintypes.HANDLE * max(1, n))()
            n = min(k32.GetProcessHeaps(len(heaps), heaps), len(heaps))
            for i in range(n):
                k32.HeapCompact(heaps[i], 0)
        except Exception:
            pass


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
