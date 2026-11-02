"""Opt-in worker telemetry. No samples or task wrapping while disabled.

Jobs keep their ordinary Future/result/exception semantics. A bounded manager queue
transports metadata from spawned processes; a full queue drops telemetry instead of waiting for space.
Times are wall milliseconds and this thread's CPU milliseconds, not process CPU.
"""
import atexit
import asyncio
import collections
import contextvars
import functools
import logging
import multiprocessing
import os
import queue
import threading
import time
import uuid
import ioprofiler
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor

_context = contextvars.ContextVar("earth_profile_job", default=None)
_live, _live_lock, _wake, _heartbeat_thread = {}, threading.Lock(), threading.Event(), None
MAX_ACTIVE = 512
MAX_RECENT = 120
MAX_STEPS = 64
PUBLISH_S = 0.5


def _emit(ctx, kind, data):
    try:
        ctx["queue"].put_nowait((ctx["session"], ctx["id"], kind, data))
    except Exception:
        ctx["dropped"] += 1


def _heartbeat():
    # One diagnostic thread per interpreter, asleep without profiled jobs. Sampling
    # the stack avoids an IPC round trip for every building, vertex or DEM call.
    while True:
        _wake.wait()
        with _live_lock:
            contexts = list(_live.values()) if ioprofiler.monitor.mode == 'full' else []
            if not contexts:
                _wake.clear()
        for ctx in contexts:
            stack = ctx["stack"][:]
            _emit(ctx, "state", {"stage": stack[-1] if stack else "job body"})
        if contexts:
            time.sleep(0.1)


def _register(ctx):
    global _heartbeat_thread
    with _live_lock:
        _live[ctx["id"]] = ctx
        if _heartbeat_thread is None:
            thread = threading.Thread(target=_heartbeat, daemon=True, name="profiler-telemetry")
            try:
                thread.start()
            except RuntimeError:
                return  # telemetry must not turn an otherwise valid job into a failure
            _heartbeat_thread = thread
        _wake.set()


def step(name):
    """Inclusive stage timer, nested within a job; aggregates only, bounded by name."""
    def decorate(fn):
        @functools.wraps(fn)
        def measured(*args, **kwargs):
            ctx = _context.get()
            if ctx is None or ctx.get('mode', 'full') != 'full' or ioprofiler.monitor.mode != 'full':
                return fn(*args, **kwargs)
            start, cpu = time.perf_counter(), time.thread_time()
            ctx["stack"].append(name)
            try:
                return fn(*args, **kwargs)
            finally:
                wall_ms = (time.perf_counter() - start) * 1000
                cpu_ms = (time.thread_time() - cpu) * 1000
                ctx["stack"].pop()
                steps = ctx["steps"]
                if name in steps or len(steps) < MAX_STEPS:
                    row = steps.setdefault(name, {"name": name, "count": 0, "totalMs": 0, "cpuMs": 0, "maxMs": 0})
                    row["count"] += 1
                    row["totalMs"] += wall_ms
                    row["cpuMs"] += cpu_ms
                    row["maxMs"] = max(row["maxMs"], wall_ms)
        return measured
    return decorate


def _io_configure(channel, session, config):
    config = config or {'mode': 'full', 'ownerPid': os.getpid()}
    control = config.get('control')
    if control is not None:
        if os.getpid() == config['ownerPid']:
            return config if (ioprofiler.monitor.session, ioprofiler.monitor.mode) == (session, config['mode']) else None
        try:
            if control.value != (session, config['mode']):
                return None  # A queued job from an expired capture must not restart collection.
        except Exception:
            return None
    ioprofiler.monitor.configure(config['mode'], session,
        channel if os.getpid() != config['ownerPid'] else None, control)
    return config


def _essential_run(channel, session, config, fn, args, kwargs):
    _io_configure(channel, session, config)
    try:
        return fn(*args, **kwargs)
    finally:
        ioprofiler.monitor.flush()


def _run(channel, session, job_id, fn, args, kwargs, root_id=None, io_config=None):
    """Module-level entry point so ProcessPoolExecutor(spawn) can pickle it."""
    io_config = _io_configure(channel, session, io_config)
    if io_config is None:
        return fn(*args, **kwargs)
    start, cpu = time.perf_counter(), time.thread_time()
    ctx = {"queue": channel, "session": session, "id": job_id, "rootId": root_id or job_id, "steps": {},
           "stack": [], "dropped": 0, 'mode': 'full', 'ioConfig': io_config}
    token = _context.set(ctx)
    _emit(ctx, "start", {"start": start, "stage": "job body",
                         "worker": f"{os.getpid()}/{threading.current_thread().name}"})
    _register(ctx)
    outcome = "ok"
    try:
        return fn(*args, **kwargs)
    except BaseException:
        outcome = "error"
        raise
    finally:
        with _live_lock:
            _live.pop(job_id, None)
        _emit(ctx, "end", {"start": start, "end": time.perf_counter(),
                           "cpuMs": (time.thread_time() - cpu) * 1000, "outcome": outcome,
                           "steps": list(ctx["steps"].values()), "dropped": ctx["dropped"]})
        _context.reset(token)
        ioprofiler.monitor.flush()


class Collector:
    def __init__(self):
        self.enabled = False
        self.mode = 'full'
        self.control = None
        self.manager = self.channel = None
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.session = uuid.uuid4().hex
            self.started = time.perf_counter()
            self.active = {}
            self.recent = collections.deque(maxlen=MAX_RECENT)
            self.failed_roots = collections.OrderedDict()
            self.skipped = 0
            self.io_sources = {}
            self.io_view = ioprofiler.View()

    def set_enabled(self, enabled):
        self.configure(enabled, 'full')

    def configure(self, enabled, mode='full'):
        if mode not in ('essential', 'full'):
            raise ValueError('invalid profiler mode')
        with self.lock:
            if enabled and self.manager is None:
                # Explicit spawn also on Linux: never fork a multithreaded server.
                manager = multiprocessing.get_context("spawn").Manager()
                try:
                    channel = manager.Queue(2048)
                    control = manager.Value('O', None)
                except BaseException:
                    manager.shutdown()
                    raise
                self.manager, self.channel = manager, channel
                self.control = control
            changed = self.enabled != bool(enabled) or self.mode != mode
            if changed:
                self.reset()
            self.mode = mode
            self.enabled = bool(enabled)
            if changed and self.control is not None: self.control.value = (self.session, mode) if enabled else None
            ioprofiler.monitor.configure(mode if enabled else 'off', self.session)

    def close(self):
        with self.lock:
            self.enabled = False
            ioprofiler.monitor.configure('off')
            if self.control is not None:
                try: self.control.value = None
                except Exception: pass
            if self.manager is not None:
                self.manager.shutdown()
                self.manager = self.channel = None
                self.control = None

    def submit(self, submit, pool, fn, args, kwargs):
        if not self.enabled:
            return submit(fn, *args, **kwargs)
        if self.mode == 'essential':
            return submit(_essential_run, self.channel, self.session,
                          dict(mode='essential', ownerPid=os.getpid(), control=self.control), fn, args, kwargs)
        with self.lock:
            if not self.enabled or len(self.active) >= MAX_ACTIVE:
                self.skipped += 1
                return submit(fn, *args, **kwargs)
            job_id = uuid.uuid4().hex
            session = self.session
            parent = _context.get()
            root_id = parent["rootId"] if parent and parent["session"] == session else job_id
            self.active[job_id] = {"id": job_id, "pool": pool,
                                   "rootId": root_id,
                                   "name": getattr(fn, "__qualname__", type(fn).__name__),
                                   "submitted": time.perf_counter(), "stage": "queued"}
            channel = self.channel
            config = dict(mode='full', ownerPid=os.getpid(), control=self.control)
        try:
            fut = submit(_run, channel, session, job_id, fn, args, kwargs, root_id, config)
        except BaseException:
            with self.lock:
                self.active.pop(job_id, None)
            raise

        def done(f):
            with self.lock:
                row = self.active.get(job_id)
                if session == self.session and row is not None:
                    row["doneAt"] = time.perf_counter()
                    row["outcome"] = "cancelled" if f.cancelled() else ("error" if f.exception() else "ok")
        fut.add_done_callback(done)
        return fut

    def snapshot(self):
        with self.lock:
            now = time.perf_counter()
            if not self.enabled:
                return {"enabled": False}
            for _ in range(4096):
                try:
                    session, job_id, kind, data = self.channel.get_nowait()
                except queue.Empty:
                    break
                if session != self.session:
                    continue
                if kind == 'io':
                    if len(self.io_sources) < 128 or data['pid'] in self.io_sources:
                        self.io_sources[data['pid']] = data
                    continue
                if kind == "submitted" and job_id not in self.active:
                    if len(self.active) >= MAX_ACTIVE:
                        self.skipped += 1
                        continue
                    self.active[job_id] = data
                if job_id not in self.active:
                    continue
                row = self.active[job_id]
                row.update(data)
                row["observedAt"] = now
                if kind == "end":
                    row["runMs"] = max(0, (row["end"] - row["start"]) * 1000)
                    row["queueMs"] = max(0, (row["start"] - row["submitted"]) * 1000)
                    row["ageMs"] = max(0, (row["end"] - row["submitted"]) * 1000)
                    self.recent.append(row)
                    del self.active[job_id]
                    if row["outcome"] == "error" and row.get("rootId") == job_id:
                        self.failed_roots[job_id] = now
            for job_id, row in self.active.items():
                if row.get("outcome") == "error" and row.get("rootId") == job_id and "doneAt" in row:
                    self.failed_roots[job_id] = row["doneAt"]
            while len(self.failed_roots) > MAX_ACTIVE:
                self.failed_roots.popitem(last=False)
            # A cancelled future, dead process or dropped end event must not leave a worker busy forever.
            for job_id, row in list(self.active.items()):
                if "doneAt" not in row and row.get("rootId") in self.failed_roots:
                    row.update(doneAt=self.failed_roots[row["rootId"]], outcome="error")
                if (row.get("external") and "doneAt" not in row and row.get("rootId") not in self.active
                        and now - row.get("observedAt", now) > 3):
                    row.update(doneAt=now - 2, outcome="unknown")
                if "doneAt" in row and now - row["doneAt"] > 1:
                    row["incomplete"] = True
                    row["ageMs"] = max(0, (row["doneAt"] - row["submitted"]) * 1000)
                    self.recent.append(row)
                    del self.active[job_id]
            live = []
            for row in self.active.values():
                live.append({**row, "ageMs": max(0, (now - row["submitted"]) * 1000),
                             "runMs": max(0, (now - row["start"]) * 1000) if "start" in row else None,
                             "queueMs": max(0, ((row.get("start") or now) - row["submitted"]) * 1000)})
            def offsets(row):
                out = dict(row)
                for key in ("submitted", "start", "end", "doneAt"):
                    if key in out:
                        out[key + "OffsetMs"] = (out[key] - self.started) * 1000
                return out
            return {"enabled": True, 'mode': self.mode, "elapsedMs": (now - self.started) * 1000,
                    "active": [offsets(row) for row in live],
                    "recent": [offsets(row) for row in self.recent], "skipped": self.skipped}

    def io_snapshot(self, server=None):
        with self.lock:
            own = ioprofiler.monitor.snapshot()
            sources = [own] + [s for pid,s in self.io_sources.items() if pid != os.getpid()]
            return self.io_view.snapshot(sources, self.mode, server)


collector = Collector()
atexit.register(collector.close)


class ThreadPoolExecutor(_ThreadPoolExecutor):
    def submit(self, fn, /, *args, **kwargs):
        parent = _context.get()
        if parent is not None and not collector.enabled:
            # A thread spawned inside a profiled process job shares its telemetry
            # channel. No new manager and no cross-process collector singleton.
            job_id = uuid.uuid4().hex
            ctx = {**parent, "id": job_id, "dropped": 0}
            row = {"id": job_id, "pool": self._thread_name_prefix, "rootId": parent["rootId"],
                   "external": True,
                   "name": getattr(fn, "__qualname__", type(fn).__name__),
                   "submitted": time.perf_counter(), "stage": "queued"}
            _emit(ctx, "submitted", row)
            try:
                fut = super().submit(_run, parent["queue"], parent["session"], job_id, fn, args, kwargs, parent["rootId"], parent.get('ioConfig'))
            except BaseException:
                _emit(ctx, "notice", {"doneAt": time.perf_counter(), "outcome": "error"})
                raise
            def done(f):
                _emit(ctx, "notice", {"doneAt": time.perf_counter(),
                      "outcome": "cancelled" if f.cancelled() else ("error" if f.exception() else "ok")})
            fut.add_done_callback(done)
            return fut
        return collector.submit(super().submit, self._thread_name_prefix, fn, args, kwargs)


async def stream(server):
    """Only live subscribers enable collection. Queue RPCs stay off the event loop."""
    interval = PUBLISH_S
    try:
        while True:
            await asyncio.sleep(interval)
            clients = [c for c in server.connections if getattr(c, "profile_until", 0) > time.monotonic()]
            try:
                mode = 'full' if any(getattr(c, 'profile_mode', 'full') == 'full' for c in clients) else 'essential'
                interval = PUBLISH_S if mode == 'full' else max(1.0, PUBLISH_S)
                await asyncio.to_thread(collector.configure, bool(clients), mode)
                if clients:
                    state = await asyncio.to_thread(collector.snapshot)
                    state['io'] = await asyncio.to_thread(collector.io_snapshot, server)
                    for c in clients:
                        # A slow consumer must not grow the world connection's write buffer without limit.
                        if c.writer.transport.get_write_buffer_size() < 256 * 1024:
                            client_mode = getattr(c, 'profile_mode', 'full')
                            profile = dict(state, collectionMode=mode, mode=client_mode)
                            if client_mode == 'essential':
                                profile.update(active=[], recent=[])
                                io = dict(state['io'], mode='essential', processes=[])
                                io['services'] = [{k:v for k,v in row.items() if k not in ('responseHistory','responseP95Ms','jitterMs')}
                                                  for row in io.get('services',[])]
                                profile['io'] = io
                            c.send({"type": "runtime_profiler", "profile": profile})
            except Exception:
                logging.getLogger("earthd").exception("runtime profiler telemetry failed")
    finally:
        await asyncio.to_thread(collector.configure, False)
