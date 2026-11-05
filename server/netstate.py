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
SLOT = struct.Struct("<qqqd")          # pid, requests in flight, bytes received, last change (time.time)
STALE_S = 120.0                         # a slot not changed this long with nothing in flight may be taken over

_shm = None
_owner = False
_slot = None
_lock = threading.Lock()
_active = 0
_bytes = 0


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
        p, act, _b, at = SLOT.unpack_from(shm.buf, i * SLOT.size)
        if p == pid:
            _slot = i
            return i
        if first is None and (p == 0 or (act <= 0 and now - at > STALE_S)):
            first = i
    if first is None:
        return None
    SLOT.pack_into(shm.buf, first * SLOT.size, pid, 0, 0, now)
    _slot = first
    return first


def _write():
    shm = _attach()
    if shm is None:
        return
    i = _slot if _slot is not None else _claim(shm)
    if i is None:
        return
    SLOT.pack_into(shm.buf, i * SLOT.size, os.getpid(), _active, _bytes, time.time())


def begin():
    """A request of this process went out (netfetch)."""
    global _active
    try:
        with _lock:
            _active += 1
            _write()
    except Exception:
        pass


def end(nbytes=0):
    """It came back (nbytes received) or failed."""
    global _active, _bytes
    try:
        with _lock:
            _active = max(0, _active - 1)
            _bytes += int(nbytes or 0)
            _write()
    except Exception:
        pass


def snapshot():
    """(requests in flight, bytes received) summed over the server's processes; (0, 0) without the block."""
    shm = _attach()
    if shm is None:
        return 0, 0
    act = got = 0
    try:
        for i in range(SLOTS):
            p, a, b, _at = SLOT.unpack_from(shm.buf, i * SLOT.size)
            if p:
                act += max(0, a)
                got += max(0, b)
    except Exception:
        return 0, 0
    return act, got


class Meter:
    """The server's process: the downloads as the loading line shows them, from successive snapshots: requests in
    flight, MB received since this burst of downloads began (after IDLE_S without any) and the recent rate (MB/s,
    over RATE_S)."""
    RATE_S = 4.0
    IDLE_S = 5.0        # nothing in flight this long: the next downloads count from 0 again (a new wait)

    def __init__(self, clock=time.monotonic, snap=snapshot):
        self.clock, self.snap = clock, snap
        self.base = None
        self.idle_since = None
        self.samples = []

    def read(self):
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
