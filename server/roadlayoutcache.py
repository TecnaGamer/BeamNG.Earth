"""Bounded reuse of source layouts across tiles; no elevation or tile results cached."""
from collections import OrderedDict
import sys
import threading
import weakref

import numpy as np


def _size(value, seen=None):
    """Count shared objects once, including numpy views' backing allocations."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    total = sys.getsizeof(value)
    if isinstance(value, np.ndarray):
        return total + (_size(value.base, seen) if value.base is not None else 0)
    if isinstance(value, dict):
        return total + sum(_size(k, seen)+_size(v, seen) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return total + sum(_size(v, seen) for v in value)
    if hasattr(value, '__dict__'):
        return total + _size(vars(value), seen)
    return total


class LayoutCache:
    """Inputs are immutable decoded cells, as in roads' existing source caches.

    Weak identity checks reject replaced/collected cells even if Python reuses an
    id. Settings and the map geometry epoch are part of the caller's variant.
    Compute outside the lock: recursive source layout calls and map fetches must
    never wait behind this cache. Racing misses may compute twice, safely.
    Returned layouts are read-only to callers; copy before changing heights.
    """
    def __init__(self, max_entries=16, max_bytes=64*1024*1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.entries = OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()

    def get(self, frame, cells, variant, build, size_of=_size):
        sources = tuple(f for f in cells if f is not None)
        key = (frame.lat0, frame.lon0, frame.h0, tuple(map(id, sources)), variant)
        with self.lock:
            got = self.entries.get(key)
            if got is not None and all(ref() is f for ref, f in zip(got[0], sources)):
                self.entries.move_to_end(key)
                return got[1]
        value = build()  # exceptions are not cached; a failed fetch can retry
        size = size_of(value)
        with self.lock:
            old = self.entries.pop(key, None)
            if old is not None:
                self.bytes -= old[2]
            if size <= self.max_bytes and self.max_entries > 0:
                self.entries[key] = (tuple(weakref.ref(f) for f in sources), value, size)
                self.bytes += size
            while self.entries and (len(self.entries) > self.max_entries or self.bytes > self.max_bytes):
                _, old = self.entries.popitem(last=False)
                self.bytes -= old[2]
        return value


layouts = LayoutCache()
