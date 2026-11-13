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
# ElasticPool: its extra processes end after this long without a job of theirs. A desktop's far-tile pool grows from 3
# processes to 10 for a teleport's loading boost and the 3D map (earthd FAR_MAX_DESKTOP), and all ten then stayed for
# the rest of the session: the executor hands its jobs round every process it has, so the three jobs at once of a
# drive kept ten processes' caches alive (Windows, 32 threads, 2026-11-12: seven of them at ~540 MB each two minutes
# after the last of four teleports round Manhattan). Long enough that a second teleport or the map opened again
# finds them still there. Measured there, the same drive with them kept and ended: the server's 18 processes 11
# and its private bytes 10.4 -> 7.6 GB when they ended (12.35 -> 9.45 GB on a first visit); the next teleport's far
# field, which starts them again, done in 10.9 s for 8.7 s (304 tiles, one run each; the teleport before it, the
# processes there in both runs, 7.4 s and 9.5 s). BNGE_POOL_IDLE_S overrides it (A/B; 0: they stay for good, as before)
EXTRA_IDLE_S = 120.0


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


def _call(fn, args, kwargs):
    """A job in the worker, then its memory ceiling (budget.maybe_shed: on a small machine a worker over it drops its
    caches between jobs)."""
    try:
        return fn(*args, **kwargs)
    finally:
        try:
            import budget
            budget.maybe_shed()
        except Exception:
            pass


def _shedding(submit):
    """pool.submit with every job wrapped in _call."""
    return lambda fn, *args, **kwargs: submit(_call, fn, args, kwargs)


class RespawningPool:
    """ProcessPoolExecutor(*args, **kwargs) made again when broken. submit / shutdown as the executor's."""

    def __init__(self, name, *args, **kwargs):
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

    def submit(self, fn, *args, **kwargs):
        with self._lock:
            pool = self._pool
        try:
            fut = runtimeprofiler.collector.submit(_shedding(pool.submit), self.name, fn, args, kwargs)
        except BrokenProcessPool:
            fut = runtimeprofiler.collector.submit(_shedding(self._renew(pool).submit), self.name, fn, args, kwargs)
        with self._count_lock:
            self.inflight += 1
        fut.add_done_callback(self._done)
        return fut

    def _done(self, _fut):
        with self._count_lock:
            self.inflight = max(0, self.inflight - 1)
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


def extra_idle_s():
    """EXTRA_IDLE_S, or BNGE_POOL_IDLE_S's seconds (0: the extra processes never end)."""
    try:
        return float(os.environ["BNGE_POOL_IDLE_S"])
    except (KeyError, ValueError):
        return EXTRA_IDLE_S


class ElasticPool:
    """A pool of `keep` processes that stay and up to `extra` more beside them while the work asks for them
    (EXTRA_IDLE_S). Two RespawningPools: a job goes to the kept processes while one of them is free, else to the
    extra ones, whose pool is made at its first job (the executor starts a process per job at once, up to `extra`) and
    shut down, its processes with their memory gone, when no job of its own ran for idle_s. With every process busy a
    job waits where the queue is shorter for the processes behind it. submit / shutdown / inflight / workers /
    respawns as RespawningPool's; extra 0 (or idle_s 0: the extra pool is never shut down) is the plain pool's
    behaviour."""

    def __init__(self, name, keep, extra=0, idle_s=None, **kwargs):
        self.name, self.keep, self.extra = name, max(1, int(keep)), max(0, int(extra))
        self.idle_s = extra_idle_s() if idle_s is None else float(idle_s)
        self._kwargs = kwargs
        self.base = RespawningPool(name, self.keep, **kwargs)
        self.workers = self.keep + self.extra
        self._lock = threading.Lock()
        self._extra = None              # the extra processes' RespawningPool while they are wanted
        self._extra_busy = 0            # its jobs submitted and not done
        self._extra_used = 0.0          # monotonic time of its last job's submit or end
        self._closed = False
        self.started = self.retired = 0     # extra pools made / shut down for want of work
        self._respawns = 0              # ... of the extra pools gone

    @property
    def inflight(self):
        with self._lock:
            return self.base.inflight + self._extra_busy

    @property
    def respawns(self):
        with self._lock:
            return self.base.respawns + self._respawns + (self._extra.respawns if self._extra is not None else 0)

    def extra_running(self):
        """Are the extra processes' pool there (made and not shut down yet)?"""
        with self._lock:
            return self._extra is not None

    def submit(self, fn, *args, **kwargs):
        with self._lock:
            pool = self._pick()
            if pool is not self.base:
                self._extra_busy += 1
                self._extra_used = time.monotonic()
        if pool is self.base:
            return pool.submit(fn, *args, **kwargs)
        try:
            fut = pool.submit(fn, *args, **kwargs)
        except BaseException:
            self._extra_done(None)
            raise
        fut.add_done_callback(self._extra_done)
        return fut

    def _pick(self):
        """Under self._lock: the pool of the next job."""
        if not self.extra or self._closed or self.base.inflight < self.keep:
            return self.base
        waiting, waiting_extra = self.base.inflight - self.keep, self._extra_busy - self.extra
        if waiting_extra >= 0 and waiting * self.extra <= waiting_extra * self.keep:
            return self.base            # (every process busy: the shorter queue for its processes)
        if self._extra is None:
            self._extra = RespawningPool(self.name, self.extra, **self._kwargs)
            self.started += 1
            log.info("%s: more processes for the work waiting (up to %d beside the %d kept)", self.name, self.extra,
                     self.keep)
            if self.idle_s > 0:
                threading.Thread(target=self._retire_loop, args=(self._extra,), daemon=True,
                                 name=f"{self.name}-idle").start()
        return self._extra

    def _extra_done(self, _fut):
        with self._lock:
            self._extra_busy = max(0, self._extra_busy - 1)
            self._extra_used = time.monotonic()

    def _retire_loop(self, pool):
        """The extra pool's own thread: shut it down once no job of its own ran for idle_s (then a later job makes a
        new one, with a thread of its own)."""
        while True:
            time.sleep(min(5.0, max(0.02, self.idle_s / 4.0)))
            with self._lock:
                if self._extra is not pool:
                    return              # (shutdown)
                if self._extra_busy or time.monotonic() - self._extra_used < self.idle_s:
                    continue
                self._extra = None
                self._respawns += pool.respawns
                self.retired += 1
            n = len(getattr(getattr(pool, "_pool", None), "_processes", None) or ())
            t0 = time.monotonic()
            try:
                pool.shutdown(wait=True)        # (no job of its own in flight: its processes end at once)
            except Exception as e:
                log.warning("%s: its extra processes' shutdown failed: %s: %s", self.name, type(e).__name__, e)
                return
            log.info("%s: %d extra processes ended after %.0f s without work (%.1f s; the %d kept go on)", self.name,
                     n, self.idle_s, time.monotonic() - t0, self.keep)
            return

    def shutdown(self, *args, **kwargs):
        with self._lock:
            self._closed = True
            extra, self._extra = self._extra, None
        if extra is not None:
            extra.shutdown(*args, **kwargs)
        return self.base.shutdown(*args, **kwargs)
