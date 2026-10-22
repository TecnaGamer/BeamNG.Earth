"""Separate cores for the world server and the game (Linux; a recorded session 2026-10-15, Las Vegas: whenever the
server's workers were busy the game fell to 12-24 fps with its physics at 0.5-0.9 of real time, even at idle
scheduling priority: the workers ran on the other half of the game's physical cores, hyperthreads sharing the cores'
execution units and caches. With the server on two cores and the game on the other six: 51-56 fps and the physics at
real time while the server worked; the server alone pinned did not help, the game's threads still landed there).

Windows too (KyzoFox 1.5.56): the same split through kernel32 (process affinity masks, which the worker processes
inherit; the cores from GetLogicalProcessorInformation). Before, a Windows server only lowered its priority class,
and on a 6-core laptop (i5-11400H) the CPU sat at 100 % with the game at 17-20 fps; the user fell through a bridge
whose tiles came 19-47 s late. BNGE_SERVER_CORES=<n> sets how many physical cores the server gets.

    server_cpus, game_cpus = split()              # the CPU sets, or (None, None) when it cannot be done
    pin_self(server_cpus)                          # the server (its threads and worker processes inherit it)
    pin_game(game_cpus)                            # every thread of the running game
"""
import os
import sys

EXE = "BeamNG.drive.x64"
WIN = sys.platform == "win32"
WIN_EXE = EXE.lower() + ".exe"
SERVER_SHARE = 0.25         # of the physical cores, at least one, never more than half


def _siblings(usable=None):
    """[(core key, [cpu ids])] of the usable CPUs (default: this process's) grouped by physical core, in core order."""
    if WIN:
        return _win_cores(usable)
    if usable is None:
        usable = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else []
    groups = {}
    for cpu in usable:
        base = f"/sys/devices/system/cpu/cpu{cpu}/topology/"
        try:
            core = int(open(base + "core_id").read())
            pkg = int(open(base + "physical_package_id").read())
        except (OSError, ValueError):
            core, pkg = cpu, 0
        groups.setdefault((pkg, core), []).append(cpu)
    return sorted(groups.items())


def split(share=SERVER_SHARE):
    """(server CPU set, game CPU set): the last cores (with their hyperthread siblings) for the server."""
    cores = _siblings()
    if len(cores) < 4:
        return None, None              # too few cores to split usefully
    k = max(1, min(len(cores) // 2, int(round(len(cores) * share))))
    try:                                # (the player's choice: BNGE_SERVER_CORES=<n>, still at most half)
        want = int(os.environ.get("BNGE_SERVER_CORES", "") or 0)
        if want > 0:
            k = max(1, min(len(cores) // 2, want))
    except ValueError:
        pass
    server = {c for _, cpus in cores[-k:] for c in cpus}
    game = {c for _, cpus in cores[:-k] for c in cpus}
    return server, game


def pin_self(cpus):
    if cpus and WIN:
        _win_set(None, cpus)
    elif cpus and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, cpus)


def game_pids():
    """The running game's main processes (not its CEF helpers) of this user."""
    if WIN:                 # (its helpers too: on the game's CPUs, where they belong)
        return [pid for pid, _pp, exe in _win_processes() if exe.lower() == WIN_EXE]
    out = []
    uid = os.getuid() if hasattr(os, "getuid") else None
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if uid is not None and os.stat(f"/proc/{d}").st_uid != uid:
                continue
            exe = os.path.basename(os.readlink(f"/proc/{d}/exe"))
            if exe != EXE:
                continue
            with open(f"/proc/{d}/cmdline", "rb") as f:
                if b"--type=" in f.read():
                    continue
            out.append(int(d))
        except OSError:
            continue
    return out


def pin_game(cpus):
    """Every thread of the game on cpus; the number of threads set (0: no game, or not allowed). Windows: the number
    of processes (a process's mask holds all its threads)."""
    if cpus and WIN:
        return sum(_win_set(pid, cpus) for pid in game_pids())
    if not cpus or not hasattr(os, "sched_setaffinity"):
        return 0
    n = 0
    for pid in game_pids():
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                os.sched_setaffinity(int(tid), cpus)
                n += 1
            except OSError:
                pass
    return n


# --- the loading boost ------------------------------------------------------------------------------------------------
# Right after a teleport the player stands and waits for the world (2026-10-16: ~30 s in Fells Point, the server's
# four CPUs busy throughout while the game's twelve mostly idled). Until the far field is done or the car moves, the
# server's processes may also run on half of the game's cores (the last ones); they keep their idle scheduling, so a
# game thread that wants a CPU still gets it. Then they go back to their own cores.
LEND_SHARE = 0.5            # of the game's physical cores


def boosted(server_cpus, game_cpus, share=LEND_SHARE):
    """The server's CPU set while loading: its own plus the last `share` of the game's physical cores (with their
    hyperthread siblings); None when there is nothing to lend."""
    if not server_cpus or not game_cpus:
        return None
    # (every CPU of the split: the server process itself is pinned to its own by now)
    cores = [cpus for _, cpus in _siblings(sorted(set(server_cpus) | set(game_cpus))) if set(cpus) <= set(game_cpus)]
    k = int(len(cores) * share)
    if k <= 0:
        return None
    return set(server_cpus) | {c for cpus in cores[-k:] for c in cpus}


def _children():
    kids = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(d))
    return kids


def pin_tree(cpus, root=None):
    """Every thread of this process (or root) and of its descendants on cpus; the number of threads set (Windows: of
    processes)."""
    if cpus and WIN:
        procs = _win_processes()
        kids = {}
        for pid, pp, _exe in procs:
            kids.setdefault(pp, []).append(pid)
        todo, n, seen = [root or os.getpid()], 0, set()
        while todo:
            pid = todo.pop()
            if pid in seen:
                continue
            seen.add(pid)
            todo += kids.get(pid, [])
            n += _win_set(pid, cpus)
        return n
    if not cpus or not hasattr(os, "sched_setaffinity"):
        return 0
    kids = _children()
    todo, n = [root or os.getpid()], 0
    while todo:
        pid = todo.pop()
        todo += kids.get(pid, [])
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                os.sched_setaffinity(int(tid), cpus)
                n += 1
            except OSError:
                pass
    return n


# --- Windows (KyzoFox 1.5.56) -----------------------------------------------------------------------------------------

def _k32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    k.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
                                         ctypes.POINTER(ctypes.c_size_t)]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k.GetLogicalProcessorInformation.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k.SetProcessAffinityMask.restype = wintypes.BOOL
    return k


def _mask(cpus):
    m = 0
    for c in cpus:
        if 0 <= c < 64:             # (processor group 0)
            m |= 1 << c
    return m


def _bits(m):
    return [c for c in range(64) if m >> c & 1]


def cores_from_masks(masks, usable):
    """[((0, i), [cpu ids])] per physical core from the cores' processor masks, those CPUs in usable only. Pure."""
    use = set(usable) if usable is not None else None
    out = []
    for m in masks:
        cpus = [c for c in _bits(m) if use is None or c in use]
        if cpus:
            out.append(cpus)
    out.sort(key=lambda c: c[0])
    return [((0, i), c) for i, c in enumerate(out)]


def _win_core_masks():
    """The physical cores' processor masks (GetLogicalProcessorInformation, RelationProcessorCore)."""
    import ctypes

    class SLPI(ctypes.Structure):
        _fields_ = [("ProcessorMask", ctypes.c_size_t), ("Relationship", ctypes.c_int),
                    ("Reserved", ctypes.c_ulonglong * 2)]
    k = _k32()
    n = ctypes.c_ulong(0)
    k.GetLogicalProcessorInformation(None, ctypes.byref(n))
    if not n.value:
        return []
    buf = (SLPI * (n.value // ctypes.sizeof(SLPI) + 1))()
    if not k.GetLogicalProcessorInformation(buf, ctypes.byref(n)):
        return []
    return [buf[i].ProcessorMask for i in range(n.value // ctypes.sizeof(SLPI)) if buf[i].Relationship == 0]


def _win_cores(usable=None):
    try:
        if usable is None:
            import ctypes
            k = _k32()
            pm, sm = ctypes.c_size_t(0), ctypes.c_size_t(0)
            if k.GetProcessAffinityMask(k.GetCurrentProcess(), ctypes.byref(pm), ctypes.byref(sm)):
                usable = _bits(pm.value)
        return cores_from_masks(_win_core_masks(), usable)
    except Exception:
        return []


def _win_set(pid, cpus):
    """Process pid's (None: this one's) CPUs; 1 if set, else 0."""
    try:
        k = _k32()
        if pid is None or pid == os.getpid():
            return 1 if k.SetProcessAffinityMask(k.GetCurrentProcess(), _mask(cpus)) else 0
        h = k.OpenProcess(0x0200 | 0x1000, False, int(pid))     # PROCESS_SET_INFORMATION | QUERY_LIMITED_INFORMATION
        if not h:
            return 0
        try:
            return 1 if k.SetProcessAffinityMask(h, _mask(cpus)) else 0
        finally:
            k.CloseHandle(h)
    except Exception:
        return 0


def _win_processes():
    """[(pid, parent pid, exe name)] of the running processes (a Toolhelp snapshot)."""
    import ctypes
    from ctypes import wintypes

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    out = []
    try:
        k = _k32()
        snap = k.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return out
        try:
            e = PE()
            e.dwSize = ctypes.sizeof(PE)
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
                ok = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
    except Exception:
        pass
    return out
