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
wait longer than wait_s does the work itself. A holder touches its locks every HEARTBEAT_S while it holds them, so a
long piece of work is never taken for a dead one's. A lock holds its taker's token (a random number): it is taken,
taken over (stale, or its holder gone) and released only under its guard (_guard, an OS file lock: one process
at a time), and removed only while its token is the one expected, so two processes breaking the same dead
holder's lock never remove the new holder's after it (as stageshare.rs's claims, its folder lock and owner tokens).
The guard is polled, never waited on blindly (GUARD_WAIT_S): when it is not had nothing changes and the caller hears
"not now" (take: not taken; break_dead: not broken; release: put off, retried by the heartbeat thread), so callers'
deadlines and cancel checks still hold with a guard's holder stopped or stuck.
"""
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid

log = logging.getLogger("earthd.xproc")

STALE_S = 120.0
HEARTBEAT_S = 20.0          # s between touches of the locks this process holds (_hold): a holder at work is never stale
ALIVE_RECHECK_S = 1.0       # s between looks at whether a lock's holder still runs (once, _await_part)
POLL_S = 0.05
PRIORITY = 0                # this process's niceness above the server's (blockworker.init's nice)
KIND_RANK = {"urgent": 0, "demand": 1, "prefetch": 2, "background": 3, "predownload": 4}
RUN_ENV = "BNGE_ROADZ_SESSION"      # (earthd's; not BNE_: those are part of the revisit cache's code key)
RUN_KEEP_S = 86400.0


_FAVOURED = [0]             # jobs of this process running as favoured() now
_HELD = {}                  # lock -> its token: the locks this process holds now (take, release), touched by _heartbeat
_HELD_LOCK = threading.Lock()
_HEART = [0]                # the pid whose _heartbeat thread runs (a forked child starts its own)
_PENDING = {}               # lock -> token: this process's releases still to do (their guard was busy: _retry_pending)
_MINE = threading.local()   # .held: the locks this thread took and holds (once's live_s: a waiter holding none)


def _hold(lock, token):
    """lock now this process's: touched every HEARTBEAT_S until released. A lock's age was all STALE_S went by: a
    central London cell planned for longer than 120 s on a busy machine had its claim taken over by the processes
    waiting for it while its planner still worked (and its release then removed the new holder's lock), the cell
    planned by several processes at once, a road tile past 300 s and its road processes restarted, 2026-11-13."""
    with _HELD_LOCK:
        if _HEART[0] != os.getpid():
            _HELD.clear()
            _PENDING.clear()
            _HEART[0] = os.getpid()
            threading.Thread(target=_heartbeat, name="xproc-heartbeat", daemon=True).start()
        _HELD[lock] = token
    _MINE.__dict__.setdefault("held", set()).add(lock)


def holds_none():
    """Does this thread hold no lock now (taken here, not released: a lock released by another thread counts as held
    here, the cautious side)?"""
    return not getattr(_MINE, "held", None)


def _heartbeat():
    me = os.getpid()
    while _HEART[0] == me:
        _WAKE.wait(PENDING_RETRY_S if _PENDING else HEARTBEAT_S)
        _WAKE.clear()
        if _PENDING:
            time.sleep(0.05)        # (a busy guard: a moment's grace before the first retry)
        _retry_pending()
        if time.monotonic() >= _NEXT_TOUCH[0]:
            _NEXT_TOUCH[0] = time.monotonic() + HEARTBEAT_S
            _touch_held()


_NEXT_TOUCH = [0.0]
_WAKE = threading.Event()   # set when a release is put off (_PENDING): retried soon, not at the next heartbeat
PENDING_RETRY_S = 1.0       # s between tries of the releases whose guard was busy (_PENDING)


def _retry_pending():
    """The releases put off (their guard busy) tried again: each removed under its guard while its token is still
    this process's. Not touched meanwhile (not in _HELD): one this process never gets back goes stale (STALE_S),
    and once this process is gone it is a dead holder's (break_dead)."""
    with _HELD_LOCK:
        todo = list(_PENDING.items())
    for lock, token in todo:
        with _guard(lock) as g:
            if not g.ok:
                continue
            done = _token(lock) != token or _remove(lock)
        if done:
            with _HELD_LOCK:
                if _PENDING.get(lock) == token:
                    del _PENDING[lock]


def _touch_held():
    """Every lock this process holds touched now (its age: STALE_S counts from here)."""
    with _HELD_LOCK:
        held = list(_HELD.items())
    for lock, token in held:
        if _token(lock) != token:   # (taken over meanwhile, or released: not this process's to keep fresh)
            continue
        try:
            os.utime(lock)
        except OSError:             # (released meanwhile; a file system without times: STALE_S as before)
            pass


GUARD_SHARDS = 256          # guard files (_guard): a lock's is one of these by its path, in the temporary folder (not
                            # beside the locks: cache folders are listed, and a file there that is not theirs counts)
GUARD_WAIT_S = 5.0          # s a guard is waited for (polled) before the step is given up: nothing changed, "not now"


def _guard_path(lock):
    import hashlib
    import tempfile
    user = os.getuid() if hasattr(os, "getuid") else 0
    h = hashlib.sha1(os.path.normcase(os.path.abspath(lock)).encode("utf-8", "surrogateescape")).digest()
    shard = int.from_bytes(h[:4], "little") % GUARD_SHARDS
    return os.path.join(tempfile.gettempdir(), f"bnge-xproc-{user}", f"{shard}.guard")


def _try_lock(fd):
    """One non-blocking try at fd's OS lock: True when held now."""
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


class _guard:
    """lock's guard held for this process alone (an OS lock on its guard file: fcntl.flock on Linux, msvcrt.locking on
    Windows) while lock is taken, taken over or released: a look at its holder and the change it decides are one step
    for the other processes. Polled without blocking for up to GUARD_WAIT_S (a holder stopped or stuck with the
    guard never holds its callers past their own deadlines and cancel checks); ok: held. Nothing is created, taken
    over or removed without it: when it is not had, the caller changes nothing and says "not now". missing: no guard
    file can be made at all (no temporary folder): take works unlocked then, making no lock file."""

    def __init__(self, lock):
        self.path = _guard_path(lock)
        self.fd = None
        self.ok = self.missing = False

    def __enter__(self):
        try:
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
            except FileNotFoundError:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        except OSError:
            self.missing = True
            return self
        end = time.monotonic() + GUARD_WAIT_S
        pause = 0.001
        while not _try_lock(fd):
            if time.monotonic() >= end:
                os.close(fd)
                return self
            time.sleep(pause)
            pause = min(pause * 2, 0.02)
        self.fd, self.ok = fd, True
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                os.close(self.fd)
                self.fd = None
        return False


def _token(lock):
    """The token written in lock (its taker's), or None (released, being written, or a lock without one)."""
    try:
        with open(lock, "rb") as fh:
            held = fh.read().split()
        return held[3].decode() if len(held) > 3 else None
    except (OSError, IndexError, UnicodeDecodeError):
        return None


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
    when another process holds it (a stale lock is taken over). Under its guard (_guard; a lock there that is not
    stale is answered without it: once polls take)."""
    try:
        if time.time() - os.path.getmtime(lock) <= STALE_S:
            return False
    except OSError:
        pass                                # (none there, or a folder still to make)
    try:
        os.makedirs(os.path.dirname(lock), exist_ok=True)
    except OSError:
        return True
    with _guard(lock) as g:
        if g.missing:
            return True                     # (no guard to be had: unlocked, as when no lock file can be made)
        if not g.ok:
            return False                    # (its guard busy: not taken now, nothing changed; asked again)
        for _ in range(2):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(lock) > STALE_S:
                        _remove(lock)       # its process died doing it (or left it: a failed release on Windows)
                        continue
                except OSError:
                    continue                # (released meanwhile)
                return False
            except OSError:
                return True
            token = uuid.uuid4().hex
            try:
                os.write(fd, ("%d %d %d %s" % (*_rank(kind), os.getpid(), token)).encode())
            finally:
                os.close(fd)
            _hold(lock, token)
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


def once(lock, ready, wait_s, kind="demand", on_wait=None, live_s=None, live_note=None):
    """(result, False): ready() gave it (at once or after waiting for another process); (None, True): this process
    does the work (release(lock) after the result is written); (None, False): do it without the lock (a wait past
    wait_s, or a less urgent holder). on_wait(): called once when this process starts waiting. live_s: past wait_s,
    waited on up to live_s while the holder still works (alive, its lock touched within LIVE_FRESH_S: _heartbeat) and
    this thread holds no lock itself (no wait of its can close a circle of waits: the holder's own waits for other
    processes stay bounded by their wait_s); such a wait past LIVE_WARN_S is logged once (live_note: what it waits
    for)."""
    got = ready()
    if got is not None:
        return got, False
    t0 = time.monotonic()
    waited = False
    holder = seen = None
    pid, recheck = None, 0.0
    warned = False
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
            seen = _token(lock)
            pid, recheck = (h[2] if h is not None else None), time.monotonic() + ALIVE_RECHECK_S
            if h is not None and not alive(h[2]):
                # its process is gone (the game closed mid-download: the server's processes end with it); the
                # lock was waited out for STALE_S before, a minute a block after a quick restart
                break_dead(lock, seen)
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
                break_dead(lock, seen)
                holder = None
                continue
        if not waited:
            waited = True
            if on_wait is not None:
                on_wait()
        if time.monotonic() - t0 > wait_s:
            if not (live_s is not None and time.monotonic() - t0 <= live_s and _works(lock, pid) and holds_none()):
                return None, False
            if not warned and time.monotonic() - t0 > LIVE_WARN_S:
                # (alive and touching its lock, not necessarily getting on: a holder stuck alive is waited out to
                # live_s by every waiter, each of its cells)
                warned = True
                log.warning("%s: waited %.0f s for process %s, still at it (alive, its lock kept fresh)",
                            live_note or os.path.basename(lock), time.monotonic() - t0, pid)
        if waited and _cancelled():
            import pools
            raise pools.Abandoned()
        time.sleep(POLL_S)
        got = ready()
        if got is not None:
            return got, False


RELEASE_S = 2.0         # s a release keeps trying to remove its lock file
LIVE_FRESH_S = 3 * HEARTBEAT_S      # s: a lock touched within this long has a holder at work (once's live_s)
LIVE_WARN_S = 120.0                 # s: a live wait this long is logged (with the holder's pid: a holder stuck alive)


def _works(lock, pid):
    """Is lock's holder (pid) still at work: alive, the lock touched by its heartbeat within LIVE_FRESH_S?"""
    try:
        fresh = time.time() - os.path.getmtime(lock) < LIVE_FRESH_S
    except OSError:
        return False
    return fresh and pid is not None and alive(pid)


def release(lock):
    """Give this process's lock back: removed under its guard while its token is still the one take wrote.
    One taken over meanwhile (STALE_S) stays its new holder's: removing it let a third process in beside that holder.
    A lock this process does not hold is left as it is (a dead holder's: break_dead)."""
    with _HELD_LOCK:
        token = _HELD.pop(lock, None)
    getattr(_MINE, "held", set()).discard(lock)
    if token is None:
        return
    with _guard(lock) as g:
        if g.ok and (_token(lock) != token or _remove(lock)):
            return
    with _HELD_LOCK:                # (its guard busy, or the remove failed: tried again by the heartbeat thread)
        _PENDING[lock] = token
    _WAKE.set()


def break_dead(lock, seen=None):
    """Remove lock when its holder is gone, under its guard: its holder read again there (alive: kept), and
    with seen (the token the caller saw with the dead holder) only that same lock, never a new holder's that another
    waiter let in meanwhile. True when removed."""
    with _guard(lock) as g:
        if not g.ok:
            return False                    # (its guard busy: not now, nothing changed)
        h = _holder(lock)
        if h is None or h[2] is None or alive(h[2]):
            return False
        if seen is not None and _token(lock) != seen:
            return False
        return _remove(lock)


def _remove(lock):
    """Remove the lock file (the caller holds its guard). On Windows a file another process has open (a waiter reading
    its urgency) cannot be removed: tried again for up to RELEASE_S. A failed remove left the lock behind, so every
    process wanting that piece waited out its wait_s and the next ones STALE_S (road tiles "building for 90 s", the
    road workers "stuck", on Windows since the locks came in 1.6.1: GitHub #435, PR #481's diagnosis)."""
    t0 = time.monotonic()
    while True:
        try:
            os.remove(lock)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if time.monotonic() - t0 > RELEASE_S:
                return False
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
