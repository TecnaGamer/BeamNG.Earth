"""Worker process pools that come back when a process dies (GitHub #76, #91: roads that never loaded).

A ProcessPoolExecutor whose process dies (a native crash, the system killing it short of memory) is broken for good:
every later submit raises BrokenProcessPool. The road pool broken that way left a session without a single new road
tile while the terrain and the far buildings went on loading. RespawningPool makes the pool again at the next submit
(at most every RESPAWN_S: a pool whose processes cannot even start does not spawn without end) and logs it.
"""
import collections
import concurrent.futures
import functools
import logging
import os
import sys
import runtimeprofiler
import threading
import time
import weakref
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

log = logging.getLogger("earthd.pools")

RESPAWN_S = 10.0
QUEUED_POLL_S = 1.0


def tasks_per_child_kw(n):
    """RespawningPool's tasks_per_child=n (its processes replaced by fresh ones after n jobs each, the queue kept; the
    lean memory tiers' road process, budget.tasks_per_child) as keyword arguments; {} for 0.
    Not ProcessPoolExecutor's max_tasks_per_child: before CPython's gh-115634 fix (not in 3.12, the packaged app's
    and CI's Python) the executor takes the exited worker's last "idle" token for a live process, starts no
    replacement and every later job waits for good, shutdown(wait=True) too (the release workflow's test_pools
    hung on both systems from 07d79eba on, 2026-11-12, while Python 3.14 here passed; tools/build_osm_tag_shards.py
    met the same hang)."""
    if not n or n <= 0:
        return {}
    return {"tasks_per_child": int(n)}


class Abandoned(Exception):
    """A queued job its caller no longer wants (result_running's abandon: a teleport left its place), cancelled."""


# Busy or hung (central London's first visit, 2026-11-13: road processes planning big cells for minutes, burning CPU,
# were taken for hung at 300 s and restarted, every tile in flight thrown away). A job past its time is waited for on
# while its pool's processes still use CPU: one of them at CPU_BUSY_FRAC of a core or more over the last CPU_WINDOW_S
# (a hung process, deadlocked or waiting for an answer that never comes, uses ~none). Not per job: the processes do
# not say which job each runs, and a tile waiting for a plan another process is making (plancache's lock) is as
# much busy as that process. Past HUNG_CEILING_X times its time a job is hung whatever the CPU (a process spinning
# without end). CPU read every CPU_SAMPLE_S at most, only while a job runs.
CPU_WINDOW_S = 60.0
CPU_BUSY_FRAC = 0.05
CPU_SAMPLE_S = 5.0
HUNG_CEILING_X = 3.0


def _clk_tck():
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100.0


_CLK_TCK = _clk_tck()
_K32 = []


def _win_cpu_s(pid):
    """A Windows process's CPU seconds (kernel + user: GetProcessTimes), or None."""
    import ctypes
    from ctypes import wintypes
    if not _K32:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE      # (64-bit handles)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        _K32.append(k32)
    k32 = _K32[0]
    h = k32.OpenProcess(0x1000, False, int(pid))       # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        t = [wintypes.FILETIME() for _ in range(4)]     # creation, exit, kernel, user
        if not k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
            return None
        return sum((x.dwHighDateTime << 32 | x.dwLowDateTime) for x in t[2:]) / 1e7     # (100 ns units)
    finally:
        k32.CloseHandle(h)


def process_cpu_s(pid):
    """Another process's CPU time in seconds (user + system), or None (gone, unreadable, or neither Linux nor
    Windows). Linux: /proc/<pid>/stat; Windows: GetProcessTimes (no psutil in the server's requirements)."""
    try:
        if sys.platform == "win32":
            return _win_cpu_s(pid)
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            f = fh.read().rsplit(b")", 1)[1].split()       # (after the name, which may hold spaces)
        if f[0] == b"Z":
            return None                 # (ended, not reaped yet)
        return (int(f[11]) + int(f[12])) / _CLK_TCK      # utime + stime, clock ticks
    except Exception:
        return None


class CpuWatch:
    """The CPU times of a pool's processes sampled over the last `window` s (at most every `every` s): whether one of
    them is still working (progress)."""

    def __init__(self, read=None, every=CPU_SAMPLE_S, window=CPU_WINDOW_S):
        self.read = read or process_cpu_s
        self.every, self.window = every, window
        self._lock = threading.Lock()
        self._last = -float("inf")
        self._hist = {}                 # pid -> deque of (time, CPU s), the oldest at or before the window's start

    def sample(self, now, pids):
        """The processes pids read at now (time.monotonic()), unless read less than `every` ago. True when read."""
        with self._lock:
            if now - self._last < self.every:
                return False
            self._last = now
            seen = set()
            for pid in pids:
                c = self.read(pid)
                if c is None:
                    continue
                seen.add(pid)
                h = self._hist.setdefault(pid, collections.deque())
                h.append((now, c))
                while len(h) > 2 and h[1][0] <= now - self.window:
                    h.popleft()
            for pid in [p for p in self._hist if p not in seen]:
                del self._hist[pid]         # (gone: a recycled pool's, an exited one's)
            return True

    def progress(self):
        """(pid, CPU s, over s, its CPU s in all) of the process that used most of a core over the samples kept;
        "short" when none was read twice yet; None when none could be read."""
        with self._lock:
            if not self._hist:
                return None
            best = None
            for pid, h in self._hist.items():
                if len(h) < 2:
                    continue
                (t0, c0), (t1, c1) = h[0], h[-1]
                if t1 <= t0:
                    continue
                if best is None or (c1 - c0) / (t1 - t0) > best[1] / best[2]:
                    best = (pid, max(0.0, c1 - c0), t1 - t0, c1)
            return best if best is not None else "short"


def result_running(fut, timeout, waited=None, abandon=None, pool=None, what=None):
    """fut's result, its timeout counted from when it left the pool's queue (running: in the executor's call queue,
    at most the pool's size + 1 jobs before the worker takes it), not from its submit: a road tile behind a long
    queue (a first visit's backlog, a slow machine) was taken for a hung worker after 300 s of waiting and the pool
    restarted, failing every tile in flight (GitHub #663). TimeoutError (concurrent.futures') when it runs longer.
    waited (a list): its time in the queue appended (to QUEUED_POLL_S) when it ran past it. abandon (a callable): while
    the job is still queued, abandon() true cancels it and raises Abandoned (a teleport left its place: the user,
    2026-11-11, "if the server is still building one city but initiates a teleport, stop generating the old city").
    pool (the RespawningPool fut came from): past its timeout the job is waited for on while the pool's processes
    still use CPU (RespawningPool.overdue: busy, not hung), up to HUNG_CEILING_X times it; the TimeoutError's message
    says why it was given up on. what: the job in the one warning that says it is waited for on ("road tile 3_4")."""
    t0 = time.perf_counter()
    while not fut.running() and not fut.done():
        if abandon is not None and abandon() and fut.cancel():
            raise Abandoned()
        try:
            return fut.result(timeout=QUEUED_POLL_S)
        except concurrent.futures.TimeoutError:
            continue
    if waited is not None:          # (the caller's log: its time in the queue)
        waited.append(time.perf_counter() - t0)
    if timeout is None or pool is None or not hasattr(pool, "overdue"):
        return fut.result(timeout=timeout)
    t_run = time.monotonic()
    while True:                     # (woken every CPU_SAMPLE_S at most: the pool's CPU sampled, rate-limited per pool)
        ran = time.monotonic() - t_run
        try:
            return fut.result(timeout=max(0.01, min(pool.cpu.every, timeout - ran) if ran < timeout
                                          else pool.cpu.every))
        except concurrent.futures.TimeoutError:
            pass
        now = time.monotonic()
        pool.cpu_sample(now)
        why = pool.overdue(now - t_run, timeout, now, fut, what)
        if why:
            raise concurrent.futures.TimeoutError(why)


def proc_name(name):
    """The worker's short process name ("bnge:roadtile"; Linux /proc/<pid>/comm, 15 bytes at most): top, htop and the
    whole-system samplers tell the pools apart (every spawned worker's command line is the same multiprocessing one).
    Nothing matches processes by this name (pgrep -f and the launcher read command lines)."""
    return ("bnge:" + name.replace(" ", ""))[:15]


def _named_init(name, init, *initargs):
    """A pool's initializer, after naming its process (proc_name)."""
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            ctypes.CDLL(None, use_errno=True).prctl(15, proc_name(name).encode(), 0, 0, 0)     # PR_SET_NAME
        except (OSError, AttributeError):
            pass
    if init is not None:
        init(*initargs)


# s a job may run in a worker before the worker writes every thread's stack to its stderr (the app's server.log), once,
# without stopping it: a hung road process told where it hangs before the pool's watchdog restarts it (GitHub #1058:
# four road processes stopped answering after a teleport, cause unknown). 0: off.
HUNG_DUMP_S = float(os.environ.get("BNGE_HUNG_DUMP_S", "") or 240.0)


def _arm_dump():
    """faulthandler's stack dump after HUNG_DUMP_S, or False (off, or no stderr: pythonw)."""
    if HUNG_DUMP_S <= 0:
        return False
    try:
        import faulthandler
        sys.stderr.fileno()
        faulthandler.dump_traceback_later(HUNG_DUMP_S, repeat=False, file=sys.stderr)
        return True
    except Exception:
        return False


def _call(fn, args, kwargs):
    """A job in the worker, then its memory ceiling (budget.maybe_shed: on a small machine a worker over it drops its
    caches between jobs); never during an idle shed (budget.JOB_LOCK), whose clock it starts again. A job still running
    after HUNG_DUMP_S writes the worker's stacks (_arm_dump)."""
    import budget
    armed = _arm_dump()
    with budget.job_lock():
        try:
            return fn(*args, **kwargs)
        finally:
            if armed:
                try:
                    import faulthandler
                    faulthandler.cancel_dump_traceback_later()
                except Exception:
                    pass
            try:
                budget.job_ended()
                budget.maybe_shed()
            except Exception:
                pass


def _shedding(submit):
    """pool.submit with every job wrapped in _call."""
    return lambda fn, *args, **kwargs: submit(_call, fn, args, kwargs)


class RespawningPool:
    """ProcessPoolExecutor(*args, **kwargs) made again when broken. submit / shutdown as the executor's."""

    def __init__(self, name, *args, hung_s=None, tasks_per_child=0, **kwargs):
        kwargs = dict(kwargs, initializer=functools.partial(_named_init, name, kwargs.get("initializer")),
                      initargs=tuple(kwargs.get("initargs", ())))         # (its processes named: proc_name)
        self.name, self._args, self._kwargs = name, args, kwargs
        self._lock = threading.Lock()
        self._pool = ProcessPoolExecutor(*args, **kwargs)
        self._last = -float("inf")
        self.respawns = 0
        self.inflight = 0           # jobs submitted and not done (Server.live_busy: the saved areas' downloads wait)
        # (its own lock: a future cancelled by _renew's shutdown runs _done in that thread, under self._lock)
        self._count_lock = threading.Lock()
        self.workers = getattr(self._pool, "_max_workers", None) or 1
        # submit_low's jobs: given to the processes only while fewer than `workers` jobs are in flight, or when none of
        # them is (one at a time beside the rest); a thread of its own hands them over (the done callbacks may run
        # under self._lock: _renew)
        self._low, self._low_active = collections.deque(), 0
        self._low_cv = threading.Condition()
        self._low_thread = None
        # tasks_per_child (tasks_per_child_kw; 0: off): the pool's own queue (_gen_loop). A "generation" is one
        # executor's processes: it is handed tasks_per_child jobs a process, at most `workers` at once (none waits
        # inside the executor), and once they are all done the pool is rotated: the old processes exit at once with
        # nothing left to run, the next jobs start fresh ones (never the old beside the new, as rotate() with a
        # backlog would be)
        self.tasks_per_child = max(0, int(tasks_per_child or 0))
        self._gen = collections.deque()         # (outer future, fn, args, kwargs) not handed over yet
        self._gen_cv = threading.Condition()
        self._gen_active = 0                    # handed over and not done
        self._gen_jobs, self._gen_pool = 0, None    # jobs handed to the executor _gen_pool so far
        self._gen_thread = None
        # hung_s: a job running (out of the queue) this long means its process hangs: the pool is recycled whoever waits
        # for it (GitHub #1058: four road processes stopped finishing jobs after a teleport; the AI road graph's and
        # the terrain patches' bare .result() never timed out, the road geotiles behind them never left the queue to
        # start their own 300 s, and nothing restarted the processes for 16 minutes)
        self.hung_s = hung_s
        self._live = {}             # in-flight future -> when the watchdog first saw it running (None: still queued)
        self._live_lock = threading.Lock()
        # busy or hung (overdue): its processes' CPU, read by the watchdog and result_running(pool=) while jobs run
        self.cpu = CpuWatch(every=min(CPU_SAMPLE_S, self.WATCH_S) if hung_s else CPU_SAMPLE_S)
        self._warned = weakref.WeakSet()    # jobs past their time whose waiting on was logged (once each)
        self._retired = collections.deque(maxlen=4)     # rotated executors, their processes finishing their jobs
        if hung_s:
            threading.Thread(target=self._watch, name=f"{name} watchdog", daemon=True).start()
        # the memory watch (budget.ROTATE_MB: small machines): a worker over it has its pool rotated (rotate)
        self.rotations = 0
        self._last_rotate = time.monotonic()
        threading.Thread(target=self._mem_watch, name=f"{name} memory", daemon=True).start()

    def _to_pool(self, fn, args, kwargs):
        """The job given to the executor (made again when broken): the executor's future."""
        with self._lock:
            pool = self._pool
        try:
            return runtimeprofiler.collector.submit(_shedding(pool.submit), self.name, fn, args, kwargs)
        except BrokenProcessPool:
            return runtimeprofiler.collector.submit(_shedding(self._renew(pool).submit), self.name, fn, args, kwargs)

    def submit(self, fn, *args, **kwargs):
        if self.tasks_per_child:
            fut = self._queue_gen(fn, args, kwargs)     # (running once handed to a process: result_running, _watch)
        else:
            fut = self._to_pool(fn, args, kwargs)
        with self._count_lock:
            self.inflight += 1
        if self.hung_s:
            with self._live_lock:
                self._live[fut] = None
        fut.add_done_callback(self._done)
        return fut

    def _done(self, _fut):
        with self._count_lock:
            self.inflight = max(0, self.inflight - 1)
        if self.hung_s:
            with self._live_lock:
                self._live.pop(_fut, None)
        if self._low:
            with self._low_cv:
                self._low_cv.notify()

    def _queue_gen(self, fn, args, kwargs):
        """tasks_per_child's submit: a future of our own, the job in _gen until _gen_loop hands it over."""
        outer = concurrent.futures.Future()
        with self._gen_cv:
            self._gen.append((outer, fn, args, kwargs))
            if self._gen_thread is None:
                self._gen_thread = threading.Thread(target=self._gen_loop, daemon=True, name=f"{self.name}-gen")
                self._gen_thread.start()
            self._gen_cv.notify_all()
        return outer

    def _gen_loop(self):
        while True:
            try:
                self._gen_step()
            except Exception as e:      # (never let the queue's thread die)
                log.warning("%s queue: %s: %s", self.name, type(e).__name__, e)
                time.sleep(1.0)

    def _gen_step(self):
        """One step of tasks_per_child's queue: a job handed to a free process, or the pool rotated when its
        generation has had its jobs and finished them (at once, not at the next submit: an idle server gets the
        memory back; the fresh executor starts its processes with its first job)."""
        limit = self.tasks_per_child * self.workers
        with self._gen_cv:
            while True:
                # (read without self._lock: _renew cancels a broken pool's futures under it, and their callbacks
                # (_gen_done) wait for _gen_cv, held here)
                pool = self._pool
                if pool is not self._gen_pool:      # (rotated, recycled or made again meanwhile: a new generation)
                    self._gen_pool, self._gen_jobs = pool, 0
                full = self._gen_jobs >= limit
                if full and self._gen_active == 0:
                    job = None
                    break
                if not full and self._gen and self._gen_active < self.workers:
                    job = self._gen.popleft()
                    if not job[0].set_running_or_notify_cancel():
                        continue                    # (cancelled while it waited: result_running's abandon)
                    self._gen_active += 1
                    self._gen_jobs += 1
                    break
                self._gen_cv.wait(timeout=1.0)
        if job is None:                 # (outside _gen_cv: rotate takes self._lock, under which done callbacks may run)
            if not self.rotate(f"{limit} jobs done"):
                time.sleep(0.2)         # (broken: the next job's submit makes it again, a new generation)
            return
        outer, fn, args, kwargs = job
        try:
            inner = self._to_pool(fn, args, kwargs)
        except BaseException as e:
            self._gen_done(None, outer, e)
            return
        inner.add_done_callback(lambda f, o=outer: self._gen_done(f, o))

    def _gen_done(self, inner, outer, error=None):
        with self._gen_cv:
            self._gen_active -= 1
            self._gen_cv.notify_all()
        if error is not None:
            outer.set_exception(error)
        elif inner.cancelled():         # (a recycle / a broken pool's shutdown cancelled it in the executor)
            outer.set_exception(BrokenProcessPool(f"{self.name} job cancelled with its pool"))
        elif inner.exception() is not None:
            outer.set_exception(inner.exception())
        else:
            outer.set_result(inner.result())

    def submit_low(self, fn, *args, **kwargs):
        """submit for work that may wait for the rest (the road processes' trees beside their road tiles: first come
        first served, ~120 trees geotiles of ~11 s took the four road processes from ~50 road geotiles, and on a first
        visit the roads were never ahead of a car at 100 mph, nyc_speed.py --cold 2026-11-02): the job goes to the
        processes when one is free of the other jobs, or when no low job runs (never starved). Its future is running
        once handed over (result_running)."""
        return self._queue_low(fn, args, kwargs, None)

    def submit_low_lazy(self, fn, make_args):
        """submit_low with fn's arguments made at its handover: make_args() -> (args, kwargs). Jobs asked for while it
        waits can join it (Server.build_veg_geo: the trees geotiles waiting together go in one job)."""
        return self._queue_low(fn, (), {}, make_args)

    def _queue_low(self, fn, args, kwargs, make_args):
        outer = concurrent.futures.Future()
        with self._low_cv:
            self._low.append((outer, fn, args, kwargs, make_args))
            if self._low_thread is None:
                self._low_thread = threading.Thread(target=self._low_loop, daemon=True, name=f"{self.name}-low")
                self._low_thread.start()
            self._low_cv.notify()
        return outer

    def _low_loop(self):
        while True:
            with self._low_cv:
                while not (self._low and (self.inflight < self.workers or self._low_active == 0)):
                    self._low_cv.wait(timeout=1.0)
                outer, fn, args, kwargs, make_args = self._low.popleft()
                if not outer.set_running_or_notify_cancel():
                    continue                # (cancelled while it waited)
                self._low_active += 1
            try:
                if make_args is not None:   # (outside _low_cv: it may take its caller's lock)
                    args, kwargs = make_args()
                inner = self.submit(fn, *args, **kwargs)
            except BaseException as e:
                with self._low_cv:
                    self._low_active -= 1
                outer.set_exception(e)
                continue
            inner.add_done_callback(lambda f, o=outer: self._low_done(f, o))

    def _low_done(self, inner, outer):
        with self._low_cv:
            self._low_active -= 1
            self._low_cv.notify()
        if inner.cancelled():
            outer.set_exception(concurrent.futures.CancelledError())
        elif inner.exception() is not None:
            outer.set_exception(inner.exception())
        else:
            outer.set_result(inner.result())

    def _renew(self, broken):
        with self._lock:
            if self._pool is broken:
                now = time.monotonic()
                if now - self._last < RESPAWN_S:
                    raise BrokenProcessPool(f"{self.name} pool broken again within {RESPAWN_S:.0f} s")
                self._last = now
                self.respawns += 1
                log.warning("%s worker process died: its pool made again (%d so far)", self.name, self.respawns)
                try:
                    broken.shutdown(wait=False, cancel_futures=True)
                except Exception:       # (it is broken already)
                    pass
                self._pool = ProcessPoolExecutor(*self._args, **self._kwargs)
            return self._pool

    WATCH_S = 5.0

    def _watch(self):
        """The hung-job watchdog (hung_s): every WATCH_S, a future running (handed to the processes) for hung_s or
        more recycles the pool; its waiters and those of the rest in flight get BrokenProcessPool (they retry)."""
        while True:
            time.sleep(self.WATCH_S)
            try:
                self._watch_step(time.monotonic())
            except Exception as e:      # (never let the watchdog die)
                log.debug("%s watchdog: %s: %s", self.name, type(e).__name__, e)

    def _watch_step(self, now):
        """One watchdog check at now (time.monotonic()): True when it recycled the pool (a job running for hung_s or
        more while the processes use no CPU, or for HUNG_CEILING_X times it: overdue)."""
        oldest, oldest_fut = None, None
        with self._live_lock:
            for fut, t in list(self._live.items()):
                if fut.done():
                    self._live.pop(fut, None)
                elif t is None:
                    if fut.running():
                        self._live[fut] = now
                elif oldest is None or t < oldest:
                    oldest, oldest_fut = t, fut
        if oldest is None:
            return False
        self.cpu_sample(now)            # (only while a job runs)
        why = self.overdue(now - oldest, self.hung_s, now, oldest_fut)
        if not why:
            return False
        if not self.recycle(f"a job {why}"):
            return False
        with self._live_lock:
            for fut in self._live:
                self._live[fut] = None      # (the rest start again, counted afresh)
        return True

    def _pids(self):
        """The process ids of the pool's processes: the current executor's and those of rotated ones still finishing
        their jobs."""
        with self._lock:
            execs = [self._pool] + list(self._retired)
        pids = []
        for ex in execs:
            live = [pid for pid, p in list((getattr(ex, "_processes", None) or {}).items())
                    if getattr(getattr(p, "_popen", None), "returncode", None) is None]     # (no wait: no reaping)
            if not live and ex is not execs[0]:
                try:
                    self._retired.remove(ex)    # (its processes all ended)
                except ValueError:
                    pass
            pids.extend(live)
        return pids

    def cpu_sample(self, now=None):
        """The processes' CPU times read for overdue (at most every cpu.every s). True when read."""
        now = time.monotonic() if now is None else now
        if now - self.cpu._last < self.cpu.every:
            return False                # (before the process list: most calls end here)
        return self.cpu.sample(now, self._pids())

    def overdue(self, ran_s, limit_s, now=None, fut=None, what=None):
        """Why a job running for ran_s s with limit_s s to run is to be given up on (and the processes restarted:
        recycle), or "" while it is waited for on: within its time, or past it while one of the pool's processes still
        uses CPU (CpuWatch; one WARNING per job, fut). Past HUNG_CEILING_X times limit_s given up on whatever the CPU;
        where no process's CPU can be read (neither Linux nor Windows), at limit_s as before."""
        if ran_s < limit_s:
            return ""
        if ran_s >= limit_s * HUNG_CEILING_X:
            return f"running for {ran_s:.0f} s, past {HUNG_CEILING_X:g}x its {limit_s:.0f} s"
        got = self.cpu.progress()
        if got is None:
            return f"running for {ran_s:.0f} s, no answer from its process"
        if got == "short":
            return ""                   # (read once so far: decided at the next look, cpu.every s on)
        pid, used, over, total = got
        if used < CPU_BUSY_FRAC * over:
            return f"running for {ran_s:.0f} s, its processes idle ({used:.1f} s CPU at most in the last {over:.0f} s)"
        if fut is None or fut not in self._warned:
            if fut is not None:
                self._warned.add(fut)
            log.warning("%s: %s running for %.0f s, past its %.0f s, but its processes are busy, not hung (pid %s: "
                        "%.1f s CPU in the last %.0f s, %.0f s in all): waited for, not restarted (given up at %.0f s "
                        "or once they idle)", self.name, what or "a job", ran_s, limit_s, pid, used, over, total,
                        limit_s * HUNG_CEILING_X)
        return ""

    def recycle(self, why):
        """The pool's processes killed and the pool made again: for a worker that is alive but never answers (a
        road tile in flight for minutes left the loading line at "roads & buildings 0/26" for good, Windows,
        1.7.0-beta, KyzoFox): a dead process makes the pool broken (_renew), a hung one never did. The futures in
        flight end with BrokenProcessPool (their callers retry). At most every RESPAWN_S. True when recycled."""
        with self._lock:
            now = time.monotonic()
            if now - self._last < RESPAWN_S:
                return False
            self._last = now
            self.respawns += 1
            old = self._pool
            self._pool = ProcessPoolExecutor(*self._args, **self._kwargs)
        log.warning("%s worker processes restarted (%s; %d so far)", self.name, why, self.respawns)
        for p in list((getattr(old, "_processes", None) or {}).values()):
            try:
                p.kill()
            except Exception:           # (gone already)
                pass
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        return True

    def _mem_watch(self):
        """budget.ROTATE_MB's watch: every ROTATE_CHECK_S, the pool rotated when one of its processes is over it."""
        import budget
        while True:
            time.sleep(budget.ROTATE_CHECK_S)
            try:
                self._mem_step(time.monotonic())
            except Exception as e:      # (never let the watch die)
                log.debug("%s memory watch: %s: %s", self.name, type(e).__name__, e)

    def worker_mb(self):
        """{pid: MB} of the current pool's processes (budget.process_mb; None where unreadable)."""
        import budget
        with self._lock:
            pool = self._pool
        return {pid: budget.process_mb(pid) for pid in list((getattr(pool, "_processes", None) or {}).keys())}

    def _mem_step(self, now):
        """One memory check at now (time.monotonic()): True when it rotated the pool."""
        import budget
        if budget.ROTATE_MB <= 0 or now - self._last_rotate < budget.ROTATE_MIN_S:
            return False
        mbs = [mb for mb in self.worker_mb().values() if mb is not None]
        if not mbs or max(mbs) < budget.ROTATE_MB:
            return False
        if self.inflight > self.workers:
            return False                    # (a backlog: it would run in the old processes beside the new ones)
        return self.rotate(f"a process at {max(mbs):.0f} MB, over {budget.ROTATE_MB} MB", now)

    def rotate(self, why, now=None):
        """A fresh pool for the jobs from now on; the old one finishes what it was given (queued and running jobs: none
        fails) and its processes exit after, giving their memory back (budget.ROTATE_MB). True when rotated."""
        with self._lock:
            old = self._pool
            if getattr(old, "_broken", False):
                return False                # (broken: _renew's to make again)
            self._pool = ProcessPoolExecutor(*self._args, **self._kwargs)
            self._last_rotate = time.monotonic() if now is None else now
            self.rotations += 1
            self._retired.append(old)       # (its processes' CPU still counted while they finish: overdue)
        log.info("%s worker processes replaced (%s; %d so far): the old ones finish their jobs and exit",
                 self.name, why, self.rotations)
        try:
            old.shutdown(wait=False, cancel_futures=False)
        except Exception:
            pass
        return True

    def shutdown(self, wait=True, *, cancel_futures=False):
        if self.tasks_per_child:            # (its own queue: cancelled, or handed over and finished first, as asked)
            with self._gen_cv:
                if cancel_futures:
                    while self._gen:
                        self._gen.popleft()[0].cancel()
                while wait and (self._gen or self._gen_active):
                    self._gen_cv.wait(timeout=1.0)
        with self._lock:
            pool = self._pool
        return pool.shutdown(wait=wait, cancel_futures=cancel_futures)


# An ElasticPool's extra processes stop once none of them has had a job for this long (s). BNGE_POOL_EXTRA_IDLE_S=0:
# kept for the session once started (how the far-tile pool's ten processes stayed from 1.9.0 on: GitHub #1114, #1026)
EXTRA_IDLE_S = float(os.environ.get("BNGE_POOL_EXTRA_IDLE_S", "") or 60.0)
REAP_CHECK_S = 5.0


class ElasticPool:
    """A RespawningPool of `base` processes kept for the session and, beside it, up to `extra` more started when every
    base process has a job and stopped again once they idle for EXTRA_IDLE_S: the far-tile pool's 10 processes (the 3D
    map, a teleport's loading boost; 3 at once while driving) all stayed for the session from the first boost on, each
    with its own caches, a desktop's server at 21 processes and ~14 GB after four minutes of a fresh city (GitHub #1114:
    "unplayable at 22 instances"; 2026-11-11). submit / shutdown / inflight / workers as a RespawningPool's."""

    def __init__(self, name, base, extra, *args, idle_s=None, **kwargs):
        self.name, self.base_n, self.extra_n = name, max(1, int(base)), max(0, int(extra))
        self._args, self._kwargs = args, kwargs
        self.base = RespawningPool(name, self.base_n, *args, **kwargs)
        self.extra = None
        self.workers = self.base_n + self.extra_n
        self.idle_s = EXTRA_IDLE_S if idle_s is None else idle_s
        self._lock = threading.Lock()
        self._idle_since = None
        self.extra_starts = self.extra_stops = 0
        # want(): the jobs its caller runs at once now (earthd: Server.far_limit, 3 while driving, 10 with the 3D map
        # open or the loading boost on): no extras while it is within the base. Without it a job finishing and the
        # next submitted at once could find the base still counting the old one (its future's callbacks run after its
        # waiter wakes) and start the extras while driving
        self.want = None
        if self.extra_n and self.idle_s > 0:
            threading.Thread(target=self._reap_loop, name=f"{name} reaper", daemon=True).start()

    @property
    def inflight(self):
        ex = self.extra
        return self.base.inflight + (ex.inflight if ex is not None else 0)

    @property
    def respawns(self):
        ex = self.extra
        return self.base.respawns + (ex.respawns if ex is not None else 0)

    def submit(self, fn, *args, **kwargs):
        """To a base process while one is free of jobs (counted in flight: queued or running) or the caller wants no
        more than the base at once (want), else to the extras (started now if they are not running)."""
        if not self.extra_n or self.base.inflight < self.base_n or self._within_base():
            return self.base.submit(fn, *args, **kwargs)
        with self._lock:            # (the reaper stops them under it: never a submit to a pool shut down)
            if self.extra is None:
                self.extra = RespawningPool(self.name, self.extra_n, *self._args, **self._kwargs)
                self.extra_starts += 1
                log.info("%s: %d more processes for a burst (they stop after %.0f s without a job)", self.name,
                         self.extra_n, self.idle_s)
            self._idle_since = None
            return self.extra.submit(fn, *args, **kwargs)

    def _within_base(self):
        import budget
        if budget.OVER:             # (the server over its memory limit, memwatch.py: no burst processes)
            return True
        want = self.want
        if want is None:
            return False
        try:
            return want() <= self.base_n
        except Exception:
            return False

    def _reap_loop(self):
        while True:
            time.sleep(REAP_CHECK_S)
            try:
                self.reap_step(time.monotonic())
            except Exception as e:      # (never let the reaper die)
                log.debug("%s reaper: %s: %s", self.name, type(e).__name__, e)

    def reap_step(self, now):
        """The extras stopped when they have had no job in flight for idle_s up to now. True when stopped."""
        with self._lock:
            ex = self.extra
            if ex is None or self.idle_s <= 0:
                return False
            if ex.inflight:
                self._idle_since = None
                return False
            if self._idle_since is None:
                self._idle_since = now
                return False
            if now - self._idle_since < self.idle_s:
                return False
            self.extra, self._idle_since = None, None
            self.extra_stops += 1
        try:
            ex.shutdown(wait=False)
        except Exception:
            pass
        log.info("%s: the %d burst processes stopped (idle %.0f s)", self.name, self.extra_n, self.idle_s)
        return True

    def shutdown(self, *args, **kwargs):
        with self._lock:
            ex, self.extra = self.extra, None
        if ex is not None:
            ex.shutdown(*args, **kwargs)
        return self.base.shutdown(*args, **kwargs)
