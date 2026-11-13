"""One process does a piece of work while the server's other processes wait for its result (2026-10-23).

The server's processes (the main one, the two terrain-block processes, the far-tile, road-tile and far-building
workers) all start on a new place at once and need the same things: a fresh place's network log on a PC showed 51 %
of the Overture requests and 72 % of the OpenStreetMap ones repeated by another process within ~0.1 s (146 of ~400 MB),
and every process planned the same road cells (plancache.py). The in-process locks (threads) never saw each other.

once(lock, ready, ...) before such a piece of work, whose result lands in a file: ready() returns the result when the
file is there. A lock file (O_EXCL) says who does it; the others poll ready() while it exists. The lock holds how
urgent its holder is (the request kind's rank, then the process's PRIORITY, the niceness the server gave it): a
process waits only for a holder at least as urgent as itself, so the terrain block under the car never waits behind
a background fetch or a road tile's process below it. A lock older than STALE_S (its process died) is taken over; a
wait longer than wait_s does the work itself.
"""
import json
import os
import re
import shutil
import sys
import threading
import time

STALE_S = 120.0
ALIVE_RECHECK_S = 1.0       # s between looks at whether a lock's holder still runs (once, _await_part)
POLL_S = 0.05
PRIORITY = 0                # this process's niceness above the server's (blockworker.init's nice)
KIND_RANK = {"urgent": 0, "demand": 1, "prefetch": 2, "background": 3, "predownload": 4}
RUN_ENV = "BNGE_ROADZ_SESSION"      # (earthd's; not BNE_: those are part of the revisit cache's code key)
RUN_KEEP_S = 86400.0


_FAVOURED = [0]             # jobs of this process running as favoured() now


def _rank(kind):
    return KIND_RANK.get(kind, 1), (0 if _FAVOURED[0] > 0 else int(PRIORITY))


class favoured:
    """While a job the player's first block waits for runs in this process (blockworker.plan_first: one of its cells
    planned by a road tile's process): its claims are held, and other processes' waited for, at the terrain blocks'
    own priority, so the block's process waits for its plans instead of making them again (once: "a process waits
    only for a holder at least as urgent as itself"). The whole process's (its planning and layer threads take the
    claims; a worker process runs one job at a time)."""

    def __enter__(self):
        _FAVOURED[0] += 1

    def __exit__(self, *exc):
        _FAVOURED[0] -= 1
        return False


def take(lock, kind="demand"):
    """True when this process now holds lock (or no lock file can be made: then it does the work unlocked); False
    when another process holds it (a stale lock is taken over)."""
    for _ in range(2):
        try:
            os.makedirs(os.path.dirname(lock), exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > STALE_S:
                    release(lock)           # its process died doing it
                    continue
            except OSError:
                continue                    # (released meanwhile)
            return False
        except OSError:
            return True
        try:
            os.write(fd, ("%d %d %d" % (*_rank(kind), os.getpid())).encode())
        finally:
            os.close(fd)
        return True
    return False


def _holder(lock):
    """(rank, priority, pid or None) of lock's holder, or None (released, or being written)."""
    try:
        with open(lock, "rb") as fh:
            held = fh.read().split()
        return int(held[0]), int(held[1]), (int(held[2]) if len(held) > 2 else None)
    except (OSError, ValueError, IndexError):
        return None


def _less_urgent(lock, kind):
    """Whether lock's holder is less urgent than this process asking for kind."""
    h = _holder(lock)
    return h is not None and h[:2] > _rank(kind)


def alive(pid):
    """Whether process pid still runs (True when it cannot be told)."""
    if pid is None or pid <= 0 or pid == os.getpid():
        return True
    if sys.platform == "win32":
        try:
            return _alive_windows(int(pid))
        except Exception:               # (never a reason to fail a download: waited out as before)
            return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    try:                                # (ended but not reaped yet by its parent, the server's main process: gone)
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            return fh.read().rsplit(b")", 1)[1].split()[0] != b"Z"
    except (OSError, IndexError):
        return True


def _alive_windows(pid):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE          # (64-bit handles: budget.lower_priority)
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5             # (access denied: it exists)
    try:
        code = wintypes.DWORD()
        return not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value == 259     # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


_cancel = threading.local()
_cancel_proc = [None]           # a worker process's job-wide check (its planning threads have no thread-local one)


def set_cancel(check):
    """This thread's cancel check while it waits in once() (None: none): check() true raises pools.Abandoned (a
    block or road tile of a place a teleport left, waiting for another process's road plans: blockworker). In a
    worker process (one job at a time) the check is the process's too: the job's planning threads see it."""
    _cancel.check = check
    import multiprocessing
    if multiprocessing.current_process().name != "MainProcess":
        _cancel_proc[0] = check


def _cancelled():
    check = getattr(_cancel, "check", None) or _cancel_proc[0]
    return check is not None and check()


def check_cancel():
    """Raise pools.Abandoned when this thread's (or worker process's) job is no longer wanted (set_cancel)."""
    if _cancelled():
        import pools
        raise pools.Abandoned()


def once(lock, ready, wait_s, kind="demand", on_wait=None):
    """(result, False): ready() gave it (at once or after waiting for another process); (None, True): this process
    does the work (release(lock) after the result is written); (None, False): do it without the lock (a wait past
    wait_s, or a less urgent holder). on_wait(): called once when this process starts waiting."""
    got = ready()
    if got is not None:
        return got, False
    t0 = time.monotonic()
    waited = False
    holder = None
    pid, recheck = None, 0.0
    while True:
        if take(lock, kind):
            got = ready()                   # (written by the process whose lock we just followed)
            if got is not None:
                release(lock)
                return got, False
            return None, True
        # the holder's urgency read once per holder (a lock file of another time or size: a new holder), not at every
        # poll: on Windows a lock open for reading cannot be removed, and its holder's release failed (below)
        try:
            st = os.stat(lock)
            now_holder = (st.st_mtime_ns, st.st_size)
        except OSError:
            now_holder = None
        if now_holder is not None and now_holder[1] > 0 and now_holder != holder:
            holder = now_holder
            h = _holder(lock)
            pid, recheck = (h[2] if h is not None else None), time.monotonic() + ALIVE_RECHECK_S
            if h is not None and not alive(h[2]):
                # its process is gone (the game closed mid-download: the server's processes end with it); the
                # lock was waited out for STALE_S before, a minute a block after a quick restart
                release(lock)
                holder = None
                continue
            if h is not None and h[:2] > _rank(kind):
                return None, False          # do it here, not wait
        elif holder is not None and holder == now_holder and pid is not None and time.monotonic() >= recheck:
            # (the same holder, looked at again now and then: one that died after we first looked, a road tile's
            # worker killed or a process ended mid-build, kept its lock to the end of wait_s, BUILD_WAIT_S 180 s of
            # a road tile's cells, 2026-11-11)
            recheck = time.monotonic() + ALIVE_RECHECK_S
            if not alive(pid):
                release(lock)
                holder = None
                continue
        if not waited:
            waited = True
            if on_wait is not None:
                on_wait()
        if time.monotonic() - t0 > wait_s:
            return None, False
        if waited and _cancelled():
            import pools
            raise pools.Abandoned()
        time.sleep(POLL_S)
        got = ready()
        if got is not None:
            return got, False


RELEASE_S = 2.0         # s a release keeps trying to remove its lock file


def release(lock):
    """Remove the lock file. On Windows a file another process has open (a waiter reading its urgency) cannot be
    removed: tried again for up to RELEASE_S. A failed remove left the lock behind, so every process wanting that piece
    waited out its wait_s and the next ones STALE_S (road tiles "building for 90 s", the road workers "stuck", on
    Windows since the locks came in 1.6.1: GitHub #435, PR #481's diagnosis)."""
    t0 = time.monotonic()
    while True:
        try:
            os.remove(lock)
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() - t0 > RELEASE_S:
                return
            time.sleep(0.01)


def run_dir(root):
    """root/<this server run>, made (its processes share RUN_ENV; a process alone: its own); the folders of runs older
    than RUN_KEEP_S under root removed."""
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", os.environ.get(RUN_ENV) or f"p{os.getpid()}.{int(time.time())}")
    got = os.path.join(root, sid)
    os.makedirs(got, exist_ok=True)
    try:
        now = time.time()
        for name in os.listdir(root):
            q = os.path.join(root, name)
            if name != sid and now - os.path.getmtime(q) > RUN_KEEP_S:
                shutil.rmtree(q, ignore_errors=True)
    except OSError:
        pass
    return got


def decide(path, value):
    """The decision kept at path (JSON): value when this process makes it first, else the one another process made (a
    file made once: os.link fails when it is there); value itself when it cannot be kept or read."""
    if not os.path.exists(path):
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(value, fh)
            os.link(tmp, path)
        except OSError:
            pass                            # (decided by another process meanwhile)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return value
