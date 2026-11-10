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
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

log = logging.getLogger("earthd.pools")

RESPAWN_S = 10.0
QUEUED_POLL_S = 1.0


def result_running(fut, timeout, waited=None):
    """fut's result, its timeout counted from when it left the pool's queue (running: in the executor's call queue,
    at most the pool's size + 1 jobs before the worker takes it), not from its submit: a road tile behind a long
    queue (a first visit's backlog, a slow machine) was taken for a hung worker after 300 s of waiting and the pool
    restarted, failing every tile in flight (GitHub #663). TimeoutError (concurrent.futures') when it runs longer.
    waited (a list): its time in the queue appended (to QUEUED_POLL_S) when it ran past it."""
    t0 = time.perf_counter()
    while not fut.running() and not fut.done():
        try:
            return fut.result(timeout=QUEUED_POLL_S)
        except concurrent.futures.TimeoutError:
            continue
    if waited is not None:          # (the caller's log: its time in the queue)
        waited.append(time.perf_counter() - t0)
    return fut.result(timeout=timeout)


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

    def __init__(self, name, *args, hung_s=None, **kwargs):
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
        # hung_s: a job running (out of the queue) this long means its process hangs: the pool is recycled whoever waits
        # for it (GitHub #1058: four road processes stopped finishing jobs after a teleport; the AI road graph's and
        # the terrain patches' bare .result() never timed out, the road geotiles behind them never left the queue to
        # start their own 300 s, and nothing restarted the processes for 16 minutes)
        self.hung_s = hung_s
        self._live = {}             # in-flight future -> when the watchdog first saw it running (None: still queued)
        self._live_lock = threading.Lock()
        if hung_s:
            threading.Thread(target=self._watch, name=f"{name} watchdog", daemon=True).start()

    def submit(self, fn, *args, **kwargs):
        with self._lock:
            pool = self._pool
        try:
            fut = runtimeprofiler.collector.submit(_shedding(pool.submit), self.name, fn, args, kwargs)
        except BrokenProcessPool:
            fut = runtimeprofiler.collector.submit(_shedding(self._renew(pool).submit), self.name, fn, args, kwargs)
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
        """One watchdog check at now (time.monotonic()): True when it recycled the pool."""
        oldest = None
        with self._live_lock:
            for fut, t in list(self._live.items()):
                if fut.done():
                    self._live.pop(fut, None)
                elif t is None:
                    if fut.running():
                        self._live[fut] = now
                elif oldest is None or t < oldest:
                    oldest = t
        if oldest is None or now - oldest < self.hung_s:
            return False
        if not self.recycle(f"a job running for {now - oldest:.0f} s, no answer from its process"):
            return False
        with self._live_lock:
            for fut in self._live:
                self._live[fut] = None      # (the rest start again, counted afresh)
        return True

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

    def shutdown(self, *args, **kwargs):
        with self._lock:
            pool = self._pool
        return pool.shutdown(*args, **kwargs)
