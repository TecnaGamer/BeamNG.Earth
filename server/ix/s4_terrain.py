"""Step 4 (SPEC.md): the ground the solve reads at each of the system's samples, and its trust, as the Terrain
artifact (the skeleton's adapter: 1 where the ground is finite, 0 where it is not)."""
import numpy as np

from . import artifacts, cache

FORMAT = 1
LAWS = (("gradeshapes", ["STEP_M"]),)
LAWS_DIGEST = cache.laws_digest(*LAWS)
STEP = 4


def ground_of(planner, smp):
    """The solve's own ground: _ground_line of each piece's samples."""
    return [planner._ground_line(x["xy"]) for x in smp]


def run(planner, smp, ground, system, scope):
    """(Terrain, ground): the ground the solve read (the cache hit's, or what ground_of computed for it)."""
    g = [np.asarray(z, np.float64) for z in ground]
    return artifacts.Terrain(artifacts.Meta(STEP, FORMAT, "", (), LAWS_DIGEST, scope), system, g,
                             [np.where(np.isfinite(z), 1.0, 0.0) for z in g]), ground
