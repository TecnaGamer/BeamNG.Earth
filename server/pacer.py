"""Duty-cycle pacing for the server's background analyses (2026-10-13).

The game's rendering is very sensitive to memory bandwidth: one numpy thread streaming large arrays made
its shadow pass 5-6x slower (3.4 -> 19 ms), a pure CPU loop changed nothing (in-game, the server frozen
for the comparison). The colour-seam and de-shadow analyses (colormatch.py, deshadow.py) are exactly that
kind of work: ~24 s per region, region after region while the player explores, and they doubled the
game's frame cost in ~15 s bursts (in-game 2026-10-13, the cycle stopped with the server frozen). Their
results are cached per region forever, so they can take longer: a thread that calls enable(duty) sleeps
at its pace() calls whenever it has been busy for SLICE_S, so that it runs at most `duty` of the time.

pace() is a no-op on threads that did not enable pacing (the tile and block builds share some of these
functions and keep full speed).
"""
import threading
import time

DUTY = 1.0          # background analyses run at most this share of the time (1: unpaced, the library default)
CLI_DUTY = 0.25     # earthd's --background-duty default
SLICE_S = 0.05      # busy this long between sleeps (the sleeps are SLICE_S * (1 / duty - 1) or longer)

_local = threading.local()
_exit = threading.Event()       # the process exits (exiting()): pace() sleeps no more
_sleeping = [0]                 # threads in pace()'s sleep
_sleep_cv = threading.Condition()


def enable(duty=DUTY):
    """Pace the calling thread from now on (duty >= 1: no pacing)."""
    _local.duty = duty if duty < 1 else None
    _local.t0 = time.perf_counter()


def pace():
    """Sleep if this (paced) thread has been busy for SLICE_S since its last sleep."""
    duty = getattr(_local, "duty", None)
    if duty is None:
        return
    busy = time.perf_counter() - _local.t0
    if busy >= SLICE_S:
        if not _exit.is_set():
            with _sleep_cv:
                _sleeping[0] += 1
            try:
                _exit.wait(busy * (1.0 / duty - 1.0))
            finally:
                with _sleep_cv:
                    _sleeping[0] -= 1
                    _sleep_cv.notify_all()
        _local.t0 = time.perf_counter()


def exiting(wait_s):
    """The process exits (fast.py's atexit hook): wake the threads sleeping in pace() and wait (at most wait_s) until
    they are out of it. pace() is called back from the kernels (sun_scores, the horizon march, colormatch's blurs): a
    thread asleep in it when the interpreter finalized was ended by CPython < 3.14 with pthread_exit through the
    kernel's Rust frames (fast.py, _exit_hook). True when none is left."""
    _exit.set()
    until = time.monotonic() + wait_s
    with _sleep_cv:
        while _sleeping[0]:
            left = until - time.monotonic()
            if left <= 0:
                return False
            _sleep_cv.wait(left)
    return True


def idle():
    """The thread is about to wait for work: its busy time starts again at the next job."""
    if getattr(_local, "duty", None) is not None:
        _local.t0 = time.perf_counter()


# --- the foreground gate -----------------------------------------------------------------------------------------
# While the player waits for a teleport's first terrain block (built in the server process), the server's background
# loops (the place's trees, water, imagery and road plans made ahead of need) hold before their next item: they share
# the interpreter with the block's grading, which took 10.6 s of a 16.9 s Baltimore block on a Steam Deck with them
# running (1.3 s on a desktop, 2026-10-20). They never hold longer than HOLD_MAX_S.
HOLD_MAX_S = 30.0

_fg = [0]
_fg_cv = threading.Condition()


def foreground_begin():
    with _fg_cv:
        _fg[0] += 1


def foreground_end():
    with _fg_cv:
        _fg[0] = max(0, _fg[0] - 1)
        if _fg[0] == 0:
            _fg_cv.notify_all()


def hold(max_s=HOLD_MAX_S):
    """Wait (at most max_s) while foreground work runs. Background loops call this between items, holding no lock."""
    with _fg_cv:
        if _fg[0]:
            _fg_cv.wait_for(lambda: _fg[0] == 0, timeout=max_s)
