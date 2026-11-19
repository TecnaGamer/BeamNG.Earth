"""Lane flow's counts from each road's home cell and the ramp lane rule's overrides (earthfast 400, roadnet::flowhome:
the road revamp's R2a) against roads._lane_flow_home / _lane_overrides' Python, value for value: random layers whose
roads are at home here, in a neighbour that was read, in one that was not, or nowhere known; repeated ids in the home
layer (the last wins); neighbours that fail to read; layers without a cell or neighbours; counts of every type the
solve and its memo hold (int, numpy int, float, None); the same neighbour cells asked with the same set, the same
home layers solved in the same order; malformed rows raise the Python's exception classes.

The layers' own solves, home cells and neighbour reads are stand-ins (they are other kernels' and mapdata's: tested
there). The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads  # noqa: E402

log = roads.log


# ---- the Python versions (before earthfast 400), verbatim ----

def ref_lane_flow_home(feats, which=0):
    _lane_flow_local, _lane_flow_counted, _home_cells, _near_layers = (
        roads._lane_flow_local, roads._lane_flow_counted, roads._home_cells, roads._near_layers)
    out = list(_lane_flow_local(feats)[which])
    me = feats.__dict__.get("_bnge_cell")
    hook = feats.__dict__.get("_bnge_neighbours")
    counted = _lane_flow_counted(feats).tolist()
    homes = _home_cells(feats) if me is not None and hook is not None else ()
    away = {home for i, home in enumerate(homes) if counted[i] and home != me[0]}
    if away:
        try:
            near = {f.__dict__["_bnge_cell"][0]: f for f in _near_layers(hook, away)
                    if f is not None and f.__dict__.get("_bnge_cell")}
        except Exception as e:          # (this cell's own)
            log.debug("lane flow: neighbours: %s: %s", type(e).__name__, e)
            near = {}
        rows = {}
        for i, home in enumerate(homes):
            if counted[i] and home != me[0] and home in near:
                rows.setdefault(home, []).append(i)
        for home, idx in rows.items():
            other = near[home]
            res = _lane_flow_local(other)[which]
            pos = {fid: j for j, fid in enumerate(other.ids.tolist())}
            for i in idx:
                j = pos.get(feats.ids[i])
                if j is not None:
                    out[i] = res[j]
    return out


def ref_lane_overrides(feats):
    d = feats.__dict__.get("_lane_overrides")
    if d is None:
        d = {}
        if roads.RAMP_LANE_RULE and (roads._lane_flow_rows(feats)[2][:, 4] > 0.5).any():
            got = roads._cell_memo(feats, "_ramp_rule_home", lambda: roads._lane_flow_home(feats, 1))
            d = {i: int(v) for i, v in enumerate(got) if v is not None}
        feats.__dict__["_lane_overrides"] = d
    return d


class Layer:
    def __init__(self, ids, cell=None, hook=None):
        self.ids = np.array(ids)
        if cell is not None:
            self._bnge_cell = (cell, "x")
        if hook is not None:
            self._bnge_neighbours = hook

    def __len__(self):
        return len(self.ids)


CELLS = ["14/1/1", "14/1/2", "14/2/1", "14/2/2", "14/3/3"]


def value(rng):
    return rng.choice([None, None, 1, 2, 3, np.int64(4), 5.0, 6])


def world(rng):
    """(layer, {layer id: (local lists)}, counted, homes, the neighbour layers, raises)"""
    n = rng.randint(0, 12)
    me = rng.choice(CELLS)
    ids = [f"w{rng.randint(0, 15)}" for _ in range(n)]
    hook = (lambda: []) if rng.random() < 0.9 else None
    f = Layer(ids, me if rng.random() < 0.9 else None, hook)
    others = []
    for c in CELLS:
        if c != me and rng.random() < 0.7:
            oid = [f"w{rng.randint(0, 15)}" for _ in range(rng.randint(0, 10))]
            others.append(Layer(oid, c if rng.random() < 0.95 else None))
    if rng.random() < 0.1:
        others.append(None)
    local = {id(L): ([value(rng) for _ in range(len(L))], [value(rng) for _ in range(len(L))])
             for L in [f] + [o for o in others if o is not None]}
    counted = np.array([rng.random() < 0.6 for _ in range(n)], bool)
    homes = [rng.choice(CELLS) if rng.random() < 0.7 else me for _ in range(n)]
    return f, local, counted, homes, others, rng.random() < 0.1


class FlowHomeParityTest(unittest.TestCase):
    def run_both(self, w, which):
        f, local, counted, homes, others, raises = w
        logs = {"ref": [], "new": []}

        def stubs(tag):
            def near(hook, away):
                logs[tag].append(("near", sorted(away), type(away).__name__))
                if raises:
                    raise OSError("neighbour read")
                return list(others)

            def lfl(L):
                logs[tag].append(("local", id(L)))
                return local[id(L)]
            return [mock.patch.object(roads, "_lane_flow_local", lfl),
                    mock.patch.object(roads, "_lane_flow_counted", lambda L: counted),
                    mock.patch.object(roads, "_home_cells", lambda L: list(homes)),
                    mock.patch.object(roads, "_near_layers", near)]
        out = {}
        for tag, fn in (("ref", ref_lane_flow_home), ("new", roads._lane_flow_home)):
            ps = stubs(tag)
            for p in ps:
                p.start()
            try:
                out[tag] = fn(f, which)
            finally:
                for p in ps:
                    p.stop()
        return out, logs

    def test_random_layers(self):
        rng = random.Random(400)
        moved = 0
        for k in range(4000):
            w = world(rng)
            which = rng.choice([0, 1])
            out, logs = self.run_both(w, which)
            want, got = out["ref"], out["new"]
            self.assertEqual(type(got), list)
            self.assertEqual(len(got), len(want), k)
            for a, b in zip(got, want):
                self.assertIs(a, b, k)
            self.assertEqual(logs["new"], logs["ref"], k)
            base = w[1][id(w[0])][which]
            moved += sum(a is not b for a, b in zip(want, base))
        self.assertGreater(moved, 500)

    def test_short_counted_raises_alike(self):
        f = Layer(["a", "b", "c"], "14/1/1", lambda: [])
        for counted in (np.array([True], bool), np.array([False, False], bool)):
            res = {}
            for tag, fn in (("ref", ref_lane_flow_home), ("new", roads._lane_flow_home)):
                with mock.patch.object(roads, "_lane_flow_local", lambda L: ([1, 2, 3], [None] * 3)), \
                        mock.patch.object(roads, "_lane_flow_counted", lambda L: counted), \
                        mock.patch.object(roads, "_home_cells", lambda L: ["14/2/2"] * 3), \
                        mock.patch.object(roads, "_near_layers", lambda hook, away: []):
                    try:
                        res[tag] = ("ok", fn(f, 0))
                    except Exception as e:      # noqa: BLE001
                        res[tag] = ("raised", type(e))
            self.assertEqual(res["new"], res["ref"])


class OverridesParityTest(unittest.TestCase):
    def test_random_layers(self):
        rng = random.Random(4001)
        made = 0
        for k in range(3000):
            n = rng.randint(0, 9)
            attrs = np.array([[rng.choice([0.0, 1.0, -1.0]), rng.choice([np.nan, 2.0]), 0.0, np.nan,
                               rng.choice([0.0, 0.0, 0.0, 1.0, np.nan, 0.5, 0.51])] for _ in range(n)],
                             np.float64).reshape(n, 5)
            home = [rng.choice([None, 0, 1, 2, np.int64(3), 4.0, 2.9, True]) for _ in range(n)]
            calls = {"ref": 0, "new": 0}
            res = {}
            for tag, fn in (("ref", ref_lane_overrides), ("new", roads._lane_overrides)):
                f = Layer([f"r{i}" for i in range(n)])

                def lfh(feats, which, tag=tag):
                    calls[tag] += 1
                    return list(home)
                with mock.patch.object(roads, "_lane_flow_rows", lambda L: (None, None, attrs)), \
                        mock.patch.object(roads, "_cell_memo", lambda feats, name, make: make()), \
                        mock.patch.object(roads, "_lane_flow_home", lfh):
                    res[tag] = fn(f)
                    self.assertIs(fn(f), res[tag])         # (remembered on the layer)
            self.assertEqual(calls["new"], calls["ref"])
            self.assertEqual(list(res["new"].items()), list(res["ref"].items()), k)
            for v in res["new"].values():
                self.assertIs(type(v), int)
            made += len(res["ref"])
        self.assertGreater(made, 1000)

    def test_rule_off_and_short_rows(self):
        f = Layer(["a"])
        with mock.patch.object(roads, "RAMP_LANE_RULE", False), \
                mock.patch.object(roads, "_lane_flow_rows", lambda L: 1 / 0):
            self.assertEqual(roads._lane_overrides(f), {})
        for cols in (3, 4):
            res = {}
            for tag, fn in (("ref", ref_lane_overrides), ("new", roads._lane_overrides)):
                g = Layer(["a", "b"])
                with mock.patch.object(roads, "_lane_flow_rows", lambda L: (None, None, np.zeros((2, cols)))):
                    try:
                        res[tag] = ("ok", fn(g))
                    except Exception as e:      # noqa: BLE001
                        res[tag] = ("raised", type(e))
            self.assertEqual(res["new"], res["ref"])
            self.assertEqual(res["ref"], ("raised", IndexError))


if __name__ == "__main__":
    unittest.main()
