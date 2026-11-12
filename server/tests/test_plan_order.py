"""A cell's road plans are the same whatever was planned before it in the process, in what order, and on how many
threads: players sharing generated cells (BeamMP), the plan cache and the revisit cache all need one cell to have one
set of plans.

Each variant plans a drivability replay's cells (tests/drivability.py: the roads round a reported place) in a fresh
process of its own: the planner's memos and the modules' (roads' facts, railways' ground, the rail nodes) start empty,
as in a new session. On 2026-11-12 these sites' cells changed with the order: the decks' corrected ground read past the
cell being planned with that cell's decks alone and kept per point for the next cells (Perth's railway beds, 2 mm), and
the pin heights of the roads a road ends on were planned in one call with the first one's context and kept per road id
and end (a61's cells)."""
import hashlib
import multiprocessing
import os
import pickle
import sys
import unittest
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SITES = ("perth_mount_henry_520", "a61_928")


def plan_hashes(name, order="fwd", threads=1):
    """{cell: hash of its plans (every field, bit for bit)} of the replay's cells planned in this process: order "fwd"
    (key order), "rev", or an index (that cell alone); threads: cells planned at once."""
    sys.path.insert(0, os.path.dirname(HERE))
    sys.path.insert(0, HERE)
    from types import SimpleNamespace
    from unittest import mock

    import numpy as np

    import drivability as dv
    import grading
    import interchanges
    import mapcells
    import revisit
    import roadsurf
    with mock.patch.object(revisit, "ON", False), mock.patch.object(grading, "PLAN_THREADS", threads), \
            mock.patch.object(roadsurf, "ON", False):
        site = interchanges.load(name, dv.SITES[name][0], keep_m=dv.KEEP_M_SITE.get(name, dv.KEEP_M))
        md = SimpleNamespace(cell=lambda key, **kw: SimpleNamespace(layers={"roads": site.cells.get(str(key))}),
                             dem=SimpleNamespace(sample=site.sample))
        dv._hook_cells(site, md, name)
        store = grading.PlanStore(md, site.sample)
        store.frame_of = lambda region: site.frame
        keys = sorted(site.cells)
        keys = keys[::-1] if order == "rev" else [keys[order]] if isinstance(order, int) else keys
        keys = [mapcells.CellKey(*map(int, k.split("/"))) for k in keys]

        def one(k):
            return store.planner_of_cell(k)._cell_plans(k)
        if threads > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(threads) as ex:
                got = list(ex.map(one, keys))
        else:
            got = [one(k) for k in keys]
    out = {}
    for k, plans in zip(keys, got):
        rows = [(fid, [(s, np.asarray(v).tobytes() if isinstance(v, np.ndarray) else repr(v))
                       for s in p.__slots__ for v in [getattr(p, s, None)]]) for fid, p in plans]
        out[str(k)] = hashlib.sha1(pickle.dumps(rows, protocol=4)).hexdigest()
    return out


class PlanOrderTest(unittest.TestCase):
    def test_plans_independent_of_order_threads_and_process(self):
        with ProcessPoolExecutor(min(16, os.cpu_count() or 1), mp_context=multiprocessing.get_context("spawn"),
                                 max_tasks_per_child=1) as pool:
            runs = []
            for name in SITES:
                for order, threads in (("fwd", 1), ("rev", 1), ("rev", 4)):
                    runs.append((name, f"{order} x{threads}", pool.submit(plan_hashes, name, order, threads)))
                for i in range(len(_cells(name))):
                    runs.append((name, f"cell {i} alone", pool.submit(plan_hashes, name, i)))
            seen = {}
            for name, label, fut in runs:
                for cell, h in fut.result().items():
                    first = seen.setdefault((name, cell), (label, h))
                    with self.subTest(name, cell=cell, run=label):
                        self.assertEqual(h, first[1], f"{name} {cell}: planned {label} it differs from {first[0]}")


def _cells(name):
    import numpy as np
    with np.load(os.path.join(HERE, "fixtures", f"{_prefix(name)}_{name}.npz"), allow_pickle=False) as a:
        return [str(k) for i, k in enumerate(a["cells"]) if f"cell{i}.ids" in a.files]


def _prefix(name):
    import drivability as dv
    return dv.SITES[name][0]


if __name__ == "__main__":
    unittest.main()
