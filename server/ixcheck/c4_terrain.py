"""Check 4, Terrain: the ground the solve reads, one value per sample of each piece, and its trust."""
import numpy as np

from ix import artifacts


def check(art, parents):
    if not isinstance(art, artifacts.Terrain):
        return []
    out = []
    h = parents.get(6)
    pcs = h.pieces if h is not None and getattr(h, "system", None) == art.system else None
    if pcs is not None and len(art.ground) != len(pcs):
        out.append(_f(art, "fail", "bad_shape", "%d ground arrays for %d pieces" % (len(art.ground), len(pcs))))
    for p, g in enumerate(art.ground):
        g = np.asarray(g, np.float64)
        t = np.asarray(art.trust[p], np.float64) if p < len(art.trust) else np.zeros(0)
        n = len(np.asarray(pcs[p].xy, np.float64)) if pcs is not None and p < len(pcs) else None
        if len(t) != len(g) or (n is not None and len(g) != n):
            out.append(_f(art, "fail", "bad_shape", "ground[%d] has %d values and trust %d, the piece has %s samples"
                          % (p, len(g), len(t), n if n is not None else "?")))
            continue                                   # the shapes disagree: the values cannot be read against them
        if (t < 0.0).any() or (t > 1.0).any() or not np.isfinite(t).all():
            out.append(_f(art, "fail", "bad_trust", "trust[%d] leaves [0, 1]" % p))
        n_bad = int((~np.isfinite(g) & (t > 0.0)).sum())
        if n_bad:
            out.append(_f(art, "fail", "ground_nan", "ground[%d] non-finite at %d samples trusted above 0" % (p, n_bad)))
    return out


def _f(art, level, code, text, where=()):
    return artifacts.Finding(art.meta.step, level, code, text, where)
