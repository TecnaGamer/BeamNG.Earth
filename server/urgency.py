"""The server's urgent work, ahead of its background jobs in this process (the user, 2026-10-31: blocks ready 10-20 s
after they were asked for at 130-175 mph, the edge guard holding the car; their stand-in photos, 0.2 s of road paint
alone, took 5 s and more: the far tiles' textures and the vegetation's geometry held this interpreter's lock, the
urgent photo threads ~9 % of a core, tools/perfmon.py + per-thread CPU).

While a block the game waits for has its photo still coming (earthd._finish_heightmap), background jobs (far tiles,
vegetation / road geotiles, ground ahead) wait at their start, MAX_HOLD_S at most each: sleeping threads leave the lock
to the urgent ones. Jobs already running finish as they are. BNE_URGENCY=off: none wait."""
import os
import threading
import time

ON = os.environ.get("BNE_URGENCY", "on") != "off"
MAX_HOLD_S = 3.0        # s a background job waits at most (a run of urgent blocks never stops the rest for long)

_cv = threading.Condition()
_n = 0
held_s = 0.0            # s background jobs have waited in all (the status log)


class urgent:
    """Context manager: urgent work running (nested and from several threads at once)."""

    def __enter__(self):
        global _n
        with _cv:
            _n += 1
        return self

    def __exit__(self, *exc):
        global _n
        with _cv:
            _n = max(0, _n - 1)
            if not _n:
                _cv.notify_all()
        return False


def busy():
    return _n > 0


def wait_turn(max_s=MAX_HOLD_S):
    """A background job's start: while urgent work runs, wait (max_s at most). Returns the seconds waited."""
    global held_s
    if not ON or not _n:
        return 0.0
    t0 = time.perf_counter()
    end = t0 + max_s
    with _cv:
        while _n:
            left = end - time.perf_counter()
            if left <= 0:
                break
            _cv.wait(left)
    w = time.perf_counter() - t0
    held_s += w
    return w
