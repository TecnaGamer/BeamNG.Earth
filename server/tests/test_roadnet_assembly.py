"""The Spec tuples from the spec kernels' columns and back (earthfast 381, roadnet::assembly: the road revamp's R2a)
against the Python they replaced: roads._specs_of, dual_carriageways' columns of the ground specs and its result on
them, _bridge_specs' ground median / direction columns. Equal values of the same Python types (a float32 width as
numpy.float32, the gap as numpy.float64, None for NaN / -1), over random kernel columns (every kind, class, surface
token and side code, -1s, NaN and float32-flagged widths) and random spec lists (None, numpy widths, medians 0.0).

The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import roads  # noqa: E402
from roads import STYLE, Spec  # noqa: E402

_KINDS = ("paved", "cobble", "gravel", "dirt")
_KIND_CODE = {k: c for c, k in enumerate(_KINDS)}
_SIDES_OF = ((1, -1), (1,), (-1,), ())


# ---- the Python versions (before earthfast 381), verbatim ----

def ref_specs_of(d, inp, states):
    names, tokens = inp["names"], inp["tokens"]
    w32 = d["w32"]
    widths = [np.float32(w) if f else w for w, f in zip(d["width"].tolist(), w32.tolist())] if w32.any() else \
        d["width"].tolist()
    out = []
    for (pres, w, k, u, c, ow, dr, kmh, gap, med, ln, sf, lk, sd, pk, mt, stt) in zip(
            d["present"].tolist(), widths, d["kind"].tolist(), d["urban"].tolist(), d["cls"].tolist(),
            d["oneway"].tolist(), d["dir"].tolist(), d["kmh"].tolist(), list(d["gap"]), d["median"].tolist(),
            d["lanes"].tolist(), d["surf"].tolist(), d["link"].tolist(), d["sides"].tolist(), d["parking"].tolist(),
            d["mountain"].tolist(), states.tolist()):
        if not pres:
            out.append(None)
            continue
        out.append(Spec(w, _KINDS[k], u, names[c], ow, 2.5, dr, None if kmh != kmh else kmh, None if gap != gap else gap,
                        None if med != med else med, None if ln < 0 else ln, None if sf < 0 else tokens[sf], lk,
                        None if sd < 0 else _SIDES_OF[sd], pk, mt, False, stt))
    return out


def ref_dual_cols(specs, pos):
    nan = float("nan")
    n = len(specs)
    return {"present": np.fromiter((sp is not None for sp in specs), bool, n),
            "width": np.fromiter((nan if sp is None else float(sp.width) for sp in specs), np.float64, n),
            "kind": np.fromiter((-1 if sp is None else _KIND_CODE.get(sp.kind, -1) for sp in specs), np.int8, n),
            "urban": np.fromiter((sp is not None and bool(sp.urban) for sp in specs), bool, n),
            "cls": np.fromiter((-1 if sp is None else pos.get(sp.cls, -1) for sp in specs), np.int32, n),
            "oneway": np.fromiter((sp is not None and bool(sp.oneway) for sp in specs), bool, n),
            "lanes": np.fromiter((-1 if sp is None or sp.lanes is None else int(sp.lanes) for sp in specs), np.int64,
                                 n),
            "link": np.fromiter((sp is not None and bool(sp.link) for sp in specs), bool, n),
            "parking": np.fromiter((sp is not None and bool(sp.parking) for sp in specs), bool, n),
            "median": np.fromiter((nan if sp is None or sp.median is None else float(sp.median) for sp in specs),
                                  np.float64, n),
            "dir": np.fromiter((0 if sp is None else int(sp.dir) for sp in specs), np.int64, n)}


def ref_dual_apply(specs, d):
    out = list(specs)
    code, width, kept, dirs, med = d["changed"], d["width"], d["w32"], d["dir"], d["median"]
    for i in np.flatnonzero(code).tolist():
        sp, c = specs[i], int(code[i])
        w = sp.width if kept[i] else float(width[i])
        if c == 1:
            out[i] = sp._replace(width=w)
        elif c == 2:
            out[i] = sp._replace(median=float(med[i]), width=w)
        else:
            out[i] = sp._replace(oneway=True, dir=int(dirs[i]), width=w, median=float(med[i]))
    return out


def ref_ground(ground):
    nan = float("nan")
    n = len(ground)
    gmed = np.fromiter((g.median if g is not None and g.median else nan for g in ground), np.float64, n)
    gdir = np.fromiter((g.dir if g is not None else 0 for g in ground), np.int64, n)
    return gmed, gdir


def same_obj(a, b):
    """Equal values of the same types, NaN equal to NaN, tuples / Specs element by element."""
    if type(a) is not type(b):
        return False
    if isinstance(a, tuple):
        return len(a) == len(b) and all(same_obj(x, y) for x, y in zip(a, b))
    if isinstance(a, (float, np.floating)):
        return np.asarray(a).tobytes() == np.asarray(b).tobytes()
    return a == b


def random_cols(rng, n, n_names, n_tokens):
    f = lambda vals: rng.choice(vals)  # noqa: E731
    return {"present": np.array([rng.random() < 0.8 for _ in range(n)], bool),
            "width": np.array([f([3.5, 6.0, 7.300000190734863, 12.25, np.nan, 0.1]) for _ in range(n)]),
            "w32": np.array([rng.random() < 0.3 for _ in range(n)], bool),
            "kind": np.array([rng.randrange(-1, 4) for _ in range(n)], np.int8),
            "urban": np.array([rng.random() < 0.5 for _ in range(n)], bool),
            "cls": np.array([rng.randrange(-1, n_names) for _ in range(n)], np.int32),
            "oneway": np.array([rng.random() < 0.5 for _ in range(n)], bool),
            "dir": np.array([f([-1, 0, 1]) for _ in range(n)], np.int64),
            "kmh": np.array([f([np.nan, 50.0, 88.5]) for _ in range(n)]),
            "gap": np.array([f([np.nan, 12.0, 30.5]) for _ in range(n)]),
            "median": np.array([f([np.nan, 0.0, 1.5]) for _ in range(n)]),
            "lanes": np.array([f([-1, 1, 2, 4]) for _ in range(n)], np.int64),
            "surf": np.array([rng.randrange(-1, n_tokens) for _ in range(n)], np.int32),
            "link": np.array([rng.random() < 0.3 for _ in range(n)], bool),
            "sides": np.array([rng.randrange(-1, 4) for _ in range(n)], np.int8),
            "parking": np.array([rng.random() < 0.3 for _ in range(n)], bool),
            "mountain": np.array([rng.random() < 0.1 for _ in range(n)], bool)}


def random_specs(rng, n):
    out = []
    for _ in range(n):
        if rng.random() < 0.2:
            out.append(None)
            continue
        out.append(Spec(rng.choice([6.0, np.float32(7.3), 11.5, np.float64(3.25)]), rng.choice(_KINDS + ("odd",)),
                        rng.random() < 0.5, rng.choice(list(STYLE) + ["nope"]), rng.random() < 0.5, 2.5,
                        rng.choice([-1, 0, 1]), None, None, rng.choice([None, 0.0, 1.5, np.float32(2.5)]),
                        rng.choice([None, 1, 2, 3]), None, rng.random() < 0.3, None, rng.random() < 0.3))
    return out


class AssemblyParityTest(unittest.TestCase):
    def test_specs_of(self):
        rng = random.Random(379)
        names = list(STYLE)
        for k in range(1500):
            n = rng.randint(0, 12)
            toks = [rng.choice(["asphalt", "sett", "gravel", "concrete"]) for _ in range(rng.randint(1, 4))]
            d = random_cols(rng, n, len(names), len(toks))
            states = np.array([rng.choice(["", "MD", "CA"]) for _ in range(n)], dtype="U2")
            inp = {"names": names, "tokens": toks}
            want = ref_specs_of(d, inp, states)
            got = roads._specs_of(d, inp, states)
            self.assertEqual(len(got), len(want), k)
            for g, w in zip(got, want):
                if w is None:
                    self.assertIsNone(g, k)
                    continue
                self.assertIs(type(g), Spec)
                self.assertTrue(same_obj(tuple(g), tuple(w)), (k, g, w))

    def test_dual_cols_apply_and_ground(self):
        rng = random.Random(3791)
        pos = {c: k for k, c in enumerate(STYLE)}
        for k in range(1500):
            n = rng.randint(0, 12)
            specs = random_specs(rng, n)
            want = ref_dual_cols(specs, pos)
            got = fast._ef.dual_cols(specs, pos)
            self.assertEqual(sorted(got), sorted(want))
            for key in want:
                self.assertEqual(got[key].dtype, want[key].dtype, key)
                self.assertEqual(got[key].tobytes(), want[key].tobytes(), (k, key))
            d = {"changed": np.array([rng.choice([0, 0, 1, 2, 3]) if specs[i] is not None else 0 for i in range(n)],
                                     np.uint8),
                 "width": np.array([rng.choice([3.5, 7.25]) for _ in range(n)]),
                 "w32": np.array([rng.random() < 0.5 for _ in range(n)], bool),
                 "dir": np.array([rng.choice([-1, 1]) for _ in range(n)], np.int64),
                 "median": np.array([rng.choice([0.6, 9.0]) for _ in range(n)])}
            want = ref_dual_apply(specs, d)
            got = fast._ef.dual_apply(specs, d)
            for g, w in zip(got, want):
                self.assertTrue(same_obj(g, w) if w is None else same_obj(tuple(g), tuple(w)), (k, g, w))
            wm, wd = ref_ground(specs)
            gm, gd = fast._ef.ground_med_dir(specs)
            self.assertEqual(gm.tobytes(), wm.tobytes(), k)
            self.assertEqual(gd.tobytes(), wd.tobytes(), k)


if __name__ == "__main__":
    unittest.main()
