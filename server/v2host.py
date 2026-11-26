"""Architecture v2, phase V1 (~/bnge-tests/archv2/DESIGN.md 3.2, 3.3, 6.2): BNE_V2_HOST=on runs the server's jobs on
the Rust deadline scheduler (earthfast.Sched, native/earthfast/src/host/sched.rs): classes P0 (collision) to P5 (late),
earliest deadline first, cancel by place (a teleport drops the old place's queued work at once, flags its running work
and preempts what still runs in a worker process after its grace: pools.SlotPool), no slot held while a job waits for
another (block / wait), and the CPU target following the game's frame time (the pose's frameMs / frameMaxMs).

Off (the default) nothing here is used: today's executors and worker pools, byte for byte. Thin glue: the policy is
in the Rust.
"""
import contextlib
import json
import os
import threading
import time

ON = os.environ.get("BNE_V2_HOST", "").strip().lower() in ("on", "1", "true")

P0, P1, P2, P3, P4, P5 = range(6)       # safety, drivable, visible, far, speculative, late (sched.rs DUE_S)
SCHED_VERSION = 605                     # earthfast.Sched / Lane / sched_cancelled / sched_classify

_sched = None


def thread_range():
    """(min, max) of the adaptive target: 2 to half the logical CPUs (BNE_V2_THREADS: a hard cap, the settings')."""
    n = os.cpu_count() or 4
    hi = max(2, n // 2)
    cap = os.environ.get("BNE_V2_THREADS", "").strip()
    if cap.isdigit() and int(cap) > 0:
        hi = max(1, min(hi, int(cap)))
    return min(2, hi), hi


def sched():
    """The server's one scheduler (made at the first call; None with the switch off)."""
    global _sched
    if not ON:
        return None
    if _sched is None:
        import fast
        import pools
        import xproc
        fast.require(SCHED_VERSION, "the v2 scheduler (BNE_V2_HOST)")
        lo, hi = thread_range()
        _sched = fast._ef.Sched("bnge-job", pools.Abandoned, lo, hi)
        xproc.extra_cancel(fast._ef.sched_cancelled)     # (a job of a place left gives up at xproc's cancel checks)
    return _sched


def active():
    """The scheduler, if this process made one (worker processes never do)."""
    return _sched


@contextlib.contextmanager
def blocked():
    """While this thread's job waits for another job (or a worker process doing another job's work): its slot given
    back to the scheduler. Nothing off a job thread or with the switch off."""
    s = _sched
    held = s is not None and s.block()
    try:
        yield
    finally:
        if held:
            s.unblock()


def cancelled():
    """Is this thread's job cancelled (its place left)? False off a job thread or with the switch off."""
    s = _sched
    return s is not None and s.cancelled()


def summary():
    """The scheduler's line since the last one (earthd's net_log, once a minute), or None (off, or nothing ran):
    "v2 sched: target 6 (frame base 16.7 ms, cpu pressure 3 %); P0 41 done, late p50 -1.62 / p95 -0.20 / max 0.85 s,
    wait p95 0.01 s, 0 dropped; ..."."""
    s = _sched
    if s is None:
        return None
    st = s.stats(True)
    parts = []
    for name, c in st["classes"].items():
        if not (c["done"] or c["failed"] or c["dropped"] or c["refused"]):
            continue
        late = "" if c["lateP50"] is None else (f", late p50 {c['lateP50']:.2f} / p95 {c['lateP95']:.2f} / max "
                                                f"{c['lateMax']:.2f} s ({c['missed']} past)")
        wait = "" if c["waitP95"] is None else f", wait p95 {c['waitP95']:.2f} s"
        parts.append(f"{name} {c['done']} done{late}{wait}, {c['failed']} failed, {c['dropped']} dropped, "
                     f"{c['refused']} refused")
    if not parts:
        return None
    base = st["baseMs"]
    return (f"v2 sched: target {st['target']} (frame base {'-' if base is None else f'{base:.1f} ms'}, cpu pressure "
            f"{st['psi']:.0f} %); " + "; ".join(parts))


_warm_io = None


def _warm_executor():
    """One slot at P1 (never the urgent one): the boards' writes in order."""
    global _warm_io
    if _warm_io is None:
        s = _sched or sched()
        _warm_io = s.executor(s.lane("warm-io", 1, False, False), P1)
    return _warm_io


class WarmBoard:
    """BNE_V2_HOST: the road cells a block lane's processes plan ahead (earthd.Server.warm_roads: put_nowait, as the
    multiprocessing queue it replaces) as one file holding the latest list (only the latest matters), written by a
    scheduler job and renamed over; the processes look at it every blockworker.WARM_POLL_S. No lock is shared with
    them: a block process preempted (killed) while waiting in the queue's get() held its reader lock for good, and the
    lane's other and next processes never read a list again."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._latest, self._pending, self._seq = None, False, 0

    def put_nowait(self, item):
        with self._lock:
            self._latest = item
            if self._pending:
                return
            self._pending = True
        _warm_executor().submit(self._write)

    def _write(self):
        with self._lock:
            item, self._latest, self._pending = self._latest, None, False
            self._seq += 1
            seq = self._seq
        if item is None:
            return
        aid, lat, lon, keys, *rest = item
        data = json.dumps({"seq": seq, "aid": None if aid is None else int(aid), "lat": float(lat), "lon": float(lon),
                           "keys": [[int(v) for v in k] for k in keys], "readOnly": bool(rest and rest[0])})
        tmp = f"{self.path}.{seq}.tmp"
        for i in range(5):          # (Windows: a reader's open file holds the rename off a moment)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, self.path)
                return
            except OSError:
                time.sleep(0.005 * (i + 1))
        try:
            os.remove(tmp)
        except OSError:
            pass


def read_warm(path, seen):
    """(a block process) WarmBoard's list when its file changed since seen (its last (mtime, size)): ((aid, lat, lon,
    keys, [read_only]), seen), else (None, seen). A file being replaced is read next time."""
    try:
        st = os.stat(path)
    except OSError:
        return None, seen
    mark = (st.st_mtime_ns, st.st_size)
    if mark == seen:
        return None, seen
    try:
        with open(path, "rb") as f:
            d = json.loads(f.read().decode("utf-8"))
        import mapcells
        keys = [mapcells.CellKey(int(k[0]), int(k[1]), int(k[2])) for k in d["keys"]]
        got = (d["aid"], float(d["lat"]), float(d["lon"]), keys, [bool(d.get("readOnly"))])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None, seen
    return got, mark
