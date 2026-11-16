"""What a job waited for, by name: the calling thread's waits summed while a job collects them.

A road geotile at 300 km/h took 2-14 s of wall time with 0.6-2.5 s of CPU, and its stage timers said only "build_tile":
the tile waits inside it for a map cell another thread reads, the town signs' village points, another process making
the same road plan, an elevation tile. The places that wait name themselves here (waiting(name): around the blocking
call), and the job reads the sums (collect()): the slow tile's log line says what it waited for.

Only the collecting thread's own waits count (a helper thread's are its own), and a wait inside a wait counts once,
under the outer name. Nothing is kept when no job collects: one attribute read a call."""
import contextlib
import threading
import time

_t = threading.local()


class collect:
    """with waits.collect() as w: ... -> w: {name: s} of the calling thread's waits meanwhile. Nests: an inner
    collect's waits are added to the outer one's when it ends."""

    def __enter__(self):
        self.outer = getattr(_t, "acc", None)
        _t.acc = self.acc = {}
        return self.acc

    def __exit__(self, *exc):
        _t.acc = self.outer
        if self.outer is not None:
            for k, v in self.acc.items():
                self.outer[k] = self.outer.get(k, 0.0) + v
        return False


@contextlib.contextmanager
def waiting(name):
    """The calling thread waits for `name` inside: its seconds added to the job collecting (none: nothing)."""
    acc = getattr(_t, "acc", None)
    if acc is None or getattr(_t, "inside", False):
        yield
        return
    _t.inside = True
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _t.inside = False
        acc[name] = acc.get(name, 0.0) + time.perf_counter() - t0


def text(acc, least=0.05, top=5):
    """"towns 9.1 map data 2.0" of {name: s}: the longest first, those under `least` s left out; "" for none."""
    rows = sorted(((v, k) for k, v in (acc or {}).items() if v >= least), reverse=True)[:top]
    return " ".join(f"{k} {v:.1f}" for v, k in rows)
