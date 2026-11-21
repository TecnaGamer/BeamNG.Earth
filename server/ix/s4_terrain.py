"""Step 4 (SPEC.md): the ground the solve reads at each of the system's samples, and its trust, as the Terrain
artifact (the skeleton's adapter: 1 where the ground is finite, 0 where it is not)."""
import numpy as np

from . import artifacts, cache

FORMAT = 2
LAWS = (("gradeshapes", ["STEP_M"]),)
LAWS_DIGEST = cache.laws_digest(*LAWS)
STEP = 4


def ground_of(planner, smp, ck):
    """The ground the solve reads: _ground_line of each piece's samples; of a system already solved (the solve's cache,
    by ck), the ground it read."""
    import grading      # (imported here: grading imports this package first; the lock is every planner's one)
    with grading._SHAPE_SOLVE_LOCK:
        hit = planner.__dict__.get("_shape_solved", {}).get(ck)
    if hit is not None:
        return hit[0]
    return [planner._ground_line(x["xy"]) for x in smp]


def run(planner, smp, ck, system, scope):
    """(Terrain, ground): the ground the solve will read at the samples, and its trust."""
    ground = ground_of(planner, smp, ck)
    g = [np.asarray(z, np.float64) for z in ground]
    return artifacts.Terrain(artifacts.Meta(STEP, FORMAT, "", (), LAWS_DIGEST, scope), system, g,
                             [np.where(np.isfinite(z), 1.0, 0.0) for z in g]), ground
