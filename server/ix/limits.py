"""A wall-clock limit on each step of an interchange's pipeline (SPEC.md): a step that runs on stops, says so, and the
interchange is not stored as solved.

A limit is there to make slowness fail loudly, not to fit today's worst case: paths 30 s, extent (with the growth) 60 s, order
10 s, terrain 30 s, datum 10 s, heights 120 s, per interchange. The Shanghai interchanges (2026-11-19: extent with its growth
1206 s, heights 5043 s) fail extent and heights: that is the point, the fix is the restructure (the rail bed build, the extent's
repeated find), not a looser limit. BNE_IX_STEP_LIMITS overrides them, `heights=300,extent=120` (seconds; 0: no limit for that
step; the steps it does not name keep theirs).

Enforced as the cancel mechanism is (xproc.check_cancel: the growth's rounds, the region's loop, the heights' relaxation
rounds ask it): a deadline of the thread the step runs in, the step stopping where it next asks, never a thread killed. The
heights kernel's own budget is capped by what is left of the step's. A step that never asks is stopped when it ends, if it
ended late. The stopped step raises xproc.TimeLimit out of scope(); pipeline.system gives the step's line ('failed: time
limit N s') and a Limit artifact (the cache on: ixcheck reports it), and the owner (ix/owner.py) stores nothing of the
interchange and gives its claim back."""
import contextlib
import logging
import os

import xproc

log = logging.getLogger("earthd.grading")

# step -> seconds, per interchange
DEFAULTS = {"paths": 30.0, "extent": 60.0, "order": 10.0, "terrain": 30.0, "datum": 10.0, "heights": 120.0}
PREFIX = "time limit"           # (what an interchange's failure reason starts with when a step's limit stopped it)


def table():
    """{step: seconds} now: DEFAULTS with BNE_IX_STEP_LIMITS applied (read each time: a test or a run may set it)."""
    out = dict(DEFAULTS)
    for part in os.environ.get("BNE_IX_STEP_LIMITS", "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _eq, val = part.partition("=")
        try:
            secs = float(val)
            if name.strip() not in out or secs < 0 or secs != secs:
                raise ValueError(part)
        except ValueError:
            log.warning("BNE_IX_STEP_LIMITS: %r ignored (step=seconds, the steps %s)", part, ", ".join(DEFAULTS))
            continue
        out[name.strip()] = secs
    return out


def limit_of(step):
    return table()[step]


def is_limit(why):
    """Whether an interchange's failure reason (pipeline.interchange's) is a step's time limit."""
    return isinstance(why, str) and why.startswith(PREFIX)


def reason(step, secs):
    return "%s %g s (step %s)" % (PREFIX, secs, step)


@contextlib.contextmanager
def scope(step, used=0.0):
    """The block is `step`, which may take its limit less `used` seconds (already spent on it: the growth's, before the
    steps' own). xproc.TimeLimit out of it when the limit ran out inside, or had when the block ended."""
    secs = limit_of(step)
    if secs <= 0:
        yield
        return
    old = xproc.set_deadline(step, secs, used)
    try:
        xproc.check_deadline()          # (the growth's rounds took it all already)
        yield
        xproc.check_deadline()
    finally:
        xproc.restore_deadline(old)


def record(scope_name, step, secs, took, text, parents=()):
    """The Limit artifact of a stopped step, written to the cache when it is on (ixcheck reads it): its key is its step's."""
    from . import artifacts, cache
    finding = artifacts.Finding(_number(step), "fail", "time_limit", text)
    art = artifacts.Limit(artifacts.Meta(_number(step), 1, "", tuple(parents), "", scope_name), scope_name, step, float(secs),
                          float(took), [finding])
    key = cache.key_of(art.meta.step, art.meta.format, art.meta.laws, parents, [scope_name.encode(), step.encode()])
    import dataclasses
    art = dataclasses.replace(art, meta=dataclasses.replace(art.meta, key=key))
    if cache.ROOT:
        cache.put(art)
    return art


def _number(step):
    return ("paths", "extent", "order", "terrain", "datum", "heights").index(step) + 1


class StepLimit(Exception):
    """pipeline.system's: a step went past its limit (step, limit seconds, its seconds, the Limit artifact)."""

    def __init__(self, step, secs, took, art):
        super().__init__(reason(step, secs))
        self.step, self.secs, self.took, self.art = step, secs, took, art
        self.why = reason(step, secs)
