"""The server's downloads in flight, across its processes (the loading line's "downloading" part: the user, 2026-11-04,
"add a downloading progress to the loading bar to indicate that the world is waiting for that").

The world server's own process makes a small shared memory block (init) before its worker processes start; they find
it by name (the environment, BNGE_NETSTATE) and each takes a slot: its pid, the requests it has in flight and the bytes
it has received. netfetch marks every request (begin / end); the server's process sums the slots (snapshot) for the
mod's load_progress. Best effort everywhere: no block, a full table or any error only means nothing is shown.
"""
import os
import struct
import threading
import time

ENV = "BNGE_NETSTATE"
SLOTS = 96
# pid, requests in flight, bytes received, last change (time.time), and what the loading line names (the user,
# 2026-11-11: players with gigabit lines read "1 MB/s" as their connection; it is the photo hosts' request limits):
# the host most of this process's requests are on now (its index in hosts(), -1: none), how many, and how many of its
# requests wait for our own per-host limit (netfetch._acquire)
SLOT = struct.Struct("<qqqdiii")
STALE_S = 120.0                         # a slot not changed this long with nothing in flight may be taken over

_shm = None
_owner = False
_slot = None
_lock = threading.Lock()
_active = 0
_bytes = 0
_per_host = {}          # host -> [in flight, waiting for our limit]
_waiting = 0
_HOSTS = None


def hosts():
    """The hosts the loading line can name (netfetch.ROLES' keys, sorted: the same list in every process of a build)."""
    global _HOSTS
    if _HOSTS is None:
        try:
            import netfetch
            _HOSTS = sorted(netfetch.ROLES)
        except Exception:
            _HOSTS = []
    return _HOSTS


def _top():
    """(index, count) of the host with the most requests in flight or waiting in this process, (-1, 0) if none."""
    best, n = None, 0
    for h, (a, w) in _per_host.items():
        if a + w > n:
            best, n = h, a + w
    if best is None:
        return -1, 0
    hs = hosts()
    try:
        return hs.index(best), n
    except ValueError:
        return -1, n


def init():
    """The server's process: make the block and name it for the workers it starts after this. Returns its name."""
    global _shm, _owner
    try:
        from multiprocessing import shared_memory
        _shm = shared_memory.SharedMemory(create=True, size=SLOTS * SLOT.size)
        _shm.buf[:SLOTS * SLOT.size] = bytes(SLOTS * SLOT.size)
        _owner = True
        os.environ[ENV] = _shm.name
        return _shm.name
    except Exception:
        _shm = None
        return None


def close():
    global _shm
    if _shm is not None:
        try:
            _shm.close()
            if _owner:
                _shm.unlink()
        except Exception:
            pass
        _shm = None


def _attach():
    global _shm
    if _shm is not None:
        return _shm
    name = os.environ.get(ENV)
    if not name:
        return None
    try:
        from multiprocessing import shared_memory
        try:        # (the resource tracker must not unlink the server's block when this worker ends)
            _shm = shared_memory.SharedMemory(name=name, track=False)
        except TypeError:
            _shm = shared_memory.SharedMemory(name=name)
            try:
                from multiprocessing import resource_tracker
                resource_tracker.unregister(_shm._name, "shared_memory")
            except Exception:
                pass
    except Exception:
        os.environ.pop(ENV, None)       # (gone: not asked again)
        return None
    return _shm


def _claim(shm):
    """This process's slot: its own, else a free or stale one (pid written into it)."""
    global _slot
    pid = os.getpid()
    now = time.time()
    first = None
    for i in range(SLOTS):
        p, act, _b, at, _t, _n, _w = SLOT.unpack_from(shm.buf, i * SLOT.size)
        if p == pid:
            _slot = i
            return i
        if first is None and (p == 0 or (act <= 0 and now - at > STALE_S)):
            first = i
    if first is None:
        return None
    SLOT.pack_into(shm.buf, first * SLOT.size, pid, 0, 0, now, -1, 0, 0)
    _slot = first
    return first


def _write():
    shm = _attach()
    if shm is None:
        return
    i = _slot if _slot is not None else _claim(shm)
    if i is None:
        return
    t, n = _top()
    SLOT.pack_into(shm.buf, i * SLOT.size, os.getpid(), _active, _bytes, time.time(), t, n, _waiting)


def begin(host=None):
    """A request of this process went out (netfetch)."""
    global _active
    try:
        with _lock:
            _active += 1
            if host:
                _per_host.setdefault(host, [0, 0])[0] += 1
            _write()
    except Exception:
        pass


def end(nbytes=0, host=None):
    """It came back (nbytes received) or failed."""
    global _active, _bytes
    try:
        with _lock:
            _active = max(0, _active - 1)
            _bytes += int(nbytes or 0)
            if host and host in _per_host:
                e = _per_host[host]
                e[0] = max(0, e[0] - 1)
                if not any(e):
                    del _per_host[host]
            _write()
    except Exception:
        pass


def wait(host, on):
    """A request of this process starts (on) / stops waiting for our own limit on host (netfetch._acquire)."""
    global _waiting
    try:
        with _lock:
            e = _per_host.setdefault(host, [0, 0])
            e[1] = max(0, e[1] + (1 if on else -1))
            _waiting = max(0, _waiting + (1 if on else -1))
            if not any(e):
                del _per_host[host]
            _write()
    except Exception:
        pass


def snapshot(detail=False):
    """(requests in flight, bytes received) summed over the server's processes; (0, 0) without the block. detail: also
    the host most requests are on across them (None) and how many requests wait for our own limits."""
    shm = _attach()
    if shm is None:
        return (0, 0, None, 0) if detail else (0, 0)
    act = got = waiting = 0
    by = {}
    try:
        for i in range(SLOTS):
            p, a, b, _at, t, n, w = SLOT.unpack_from(shm.buf, i * SLOT.size)
            if p:
                act += max(0, a)
                got += max(0, b)
                waiting += max(0, w)
                if t >= 0 and n > 0:
                    by[t] = by.get(t, 0) + n
    except Exception:
        return (0, 0, None, 0) if detail else (0, 0)
    if not detail:
        return act, got
    hs = hosts()
    top = max(by, key=by.get) if by else None
    return act, got, (hs[top] if top is not None and top < len(hs) else None), waiting


class Meter:
    """The server's process: the downloads as the loading line shows them, from successive snapshots: requests in
    flight, MB received since this burst of downloads began (after IDLE_S without any) and the recent rate (MB/s,
    over RATE_S)."""
    RATE_S = 4.0
    IDLE_S = 5.0        # nothing in flight this long: the next downloads count from 0 again (a new wait)

    def __init__(self, clock=time.monotonic, snap=None):
        self.clock, self.snap = clock, snap
        self.top, self.waiting = None, 0      # (read: the host most requests are on, requests waiting for our limits)
        self.base = None
        self.idle_since = None
        self.samples = []

    def read(self):
        if self.snap is None:
            act, got, self.top, self.waiting = snapshot(detail=True)
        else:
            act, got = self.snap()
        now = self.clock()
        if act > 0:
            if self.base is None or (self.idle_since is not None and now - self.idle_since >= self.IDLE_S):
                self.base = got
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = now
        if self.base is None:
            self.base = got
        self.samples.append((now, got))
        while len(self.samples) > 2 and now - self.samples[1][0] >= self.RATE_S:
            self.samples.pop(0)
        t0, b0 = self.samples[0]
        rate = (got - b0) / 1e6 / (now - t0) if now - t0 >= 0.5 else 0.0
        return act, max(0.0, (got - self.base) / 1e6), max(0.0, rate)
