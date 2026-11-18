"""Road revamp track RS step S1 (structures.py, earthfast.structure_groups): a bridge chain's structure groups as a
shadow JSON (BNE_DECK_STRUCT=shadow). The groups follow the design's rules (twins, a T on a deck, joints, never
across OSM levels); the JSON's digests are the same whichever tile asks first, in whichever cell order, in another
process; the switch changes no deck."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import mapcells  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402
import structures  # noqa: E402
from test_roads import FRAME, LAT, LON, M_LON, features  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def scene():
    """(the west cell's Features, the east cell's, the boxes near deck a and deck b): a bridge chain over a map cell's
    east edge (a, b end to end), a twin beside it (one deck), a ramp ending on deck a (a T), a street under it, and an
    upper deck of another OSM level crossing over b."""
    home = mapcells.cell_of(LAT, LON)
    east = (home.bounds()[2] - LON) * M_LON             # m: the cell's east edge
    probe = features([[(0, 0), (100, 0)]], ["primary"], levels=[(0, 1.0, 0.0, 1.0)], ids=["p"])
    w = roads.bridge_specs(probe)[0].width
    lines = [[(east - 300, 0), (east + 20, 0)], [(east + 20, 0), (east + 340, 0)],
             [(east - 300, w + 1.0), (east + 340, w + 1.0)],          # the twin: 1 m from a / b edge to edge
             [(east - 100, 90), (east - 100, 0.5)],                   # the ramp: its end on deck a
             [(east - 60, -200), (east - 60, 200)],                   # the street under the deck (west cell)
             [(east + 150, -200), (east + 150, 200)]]                 # the upper deck crossing over b
    classes = ["primary", "primary", "primary", "secondary", "residential", "primary"]
    levels = [(0, 1.0, 0.0, 1.0), (1, 1.0, 0.0, 1.0), (2, 1.0, 0.0, 1.0), (3, 1.0, 0.3, 1.0), (5, 2.0, 0.0, 1.0)]
    ids = ["a", "b", "twin", "ramp", "street", "over"]
    west = features(lines, classes, levels=levels, ids=ids)
    eastf = features(lines, classes, levels=levels, ids=ids)
    return home, west, eastf, (east - 200, -50, east - 100, 50), (east + 200, -50, east + 300, 50)


def run(first, order=1, shadow=True):
    """Solve the scene's chain asking box `first` first (cells listed in `order`): (decks {fid: z bytes}, the shadow
    JSONs {name: doc})."""
    home, west, eastf, box_a, box_b = scene()
    cell_fn = lambda key: west if key == home else eastf          # noqa: E731
    flat = lambda la, lo: np.full(np.shape(la), 10.0)             # noqa: E731
    with tempfile.TemporaryDirectory() as d, mock.patch.object(revisit, "CACHE_DIR", d), \
            mock.patch.object(structures, "SHADOW", shadow):
        bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
        cells = [west, eastf][::order]
        bp.parts(cells, first)
        decks = {fid: z.tobytes() for box in (box_a, box_b) for fid, spec, xy, z, lon in bp.parts(cells, box)}
        out = os.path.join(d, *structures.DEBUG_DIR)
        docs = {}
        for n in sorted(os.listdir(out)) if os.path.isdir(out) else ():
            with open(os.path.join(out, n), encoding="utf-8") as f:
                docs[n] = json.load(f)
    return decks, docs


def digests(docs):
    return sorted((n, d["digest"], tuple(g["digest"] for g in d["groups"])) for n, d in docs.items())


class GroupsTest(unittest.TestCase):
    def test_the_chain_groups(self):
        _decks, docs = run(scene()[3])
        self.assertTrue(docs, "a shadow JSON for the chain")
        doc = next(d for d in docs.values() if any(m["key"].startswith("a:") for m in d["members"]))
        group = {m["key"].split(":")[0]: m["group"] for m in doc["members"] if m["kind"] != "outside"}
        self.assertEqual(group["a"], group["b"], "end to end: JOIN")
        self.assertEqual(group["a"], group["twin"], "the twin beside them: BESIDE")
        self.assertEqual(group["a"], group["ramp"], "the ramp's end on deck a: END_ON (the T)")
        if "over" in group:
            self.assertNotEqual(group["over"], group["a"], "another OSM level: never one structure")
        kinds = {e["kind"] for e in doc["edges"]}
        self.assertTrue({"JOIN", "BESIDE"} <= kinds, kinds)
        self.assertEqual(len(doc["digest"]), 16)

    def test_the_same_whichever_tile_and_cell_order(self):
        _h, _w, _e, box_a, box_b = scene()
        base = digests(run(box_a)[1])
        self.assertTrue(base)
        for first, order in ((box_b, 1), (box_a, -1), (box_b, -1)):
            with self.subTest(first=first, order=order):
                self.assertEqual(digests(run(first, order)[1]), base)

    def test_the_same_in_another_process(self):
        code = ("import sys, json; sys.path[:0] = [%r, %r]; import test_structures as t; "
                "print(json.dumps(t.digests(t.run(t.scene()[4])[1])))" % (HERE, os.path.dirname(HERE)))
        env = dict(os.environ, PYTHONHASHSEED="12345")
        got = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
        here = json.loads(json.dumps(digests(run(scene()[3])[1])))     # (another start tile too)
        self.assertEqual(json.loads(got.stdout.strip().splitlines()[-1]), here)

    def test_the_switch_changes_no_deck(self):
        _h, _w, _e, box_a, _b = scene()
        off, none = run(box_a, shadow=False)
        on, docs = run(box_a, shadow=True)
        self.assertEqual(none, {})
        self.assertTrue(docs)
        self.assertEqual(off, on)

    def test_a_window_solve_writes_a_marker(self):
        """A chain over CHAIN_CELLS cells is solved per window: no groups (the legacy path in v1), a marker that is the
        same whichever window's solve writes it (Codex round 2: it was tied to the truss cache's first solve)."""
        class P:
            region = (1, 2)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(revisit, "CACHE_DIR", d):
            comp = [("v01", 0), ("v02", 0)]
            a = structures.capped(P(), comp, (3, 4))
            with open(a, encoding="utf-8") as f:
                first = f.read()
            self.assertEqual(structures.capped(P(), comp, (3, 4)), a)
            with open(a, encoding="utf-8") as f:
                self.assertEqual(f.read(), first)
            doc = json.loads(first)
            self.assertEqual((doc["capped"], doc["parts"]), ([3, 4], ["v01:0", "v02:0"]))
            self.assertNotIn("groups", doc)

    def test_rules_on_small_cases(self):
        def m(key, xy, z, half=4.0, level=1.0, kind=structures.ROAD, fid=None):
            xy = np.asarray(xy, np.float64)
            return {"key": key, "fid": fid or key, "cls": "primary", "xy": xy, "z": np.full(len(xy), z),
                    "half": half, "kind": kind, "level": level}
        # E6 Vist: twins on a 17 m deck (80 % of the shorter deck); Queensboro: two levels 0.8 m apart, never one
        got = structures.groups([m("e1", [(0, 0), (17, 0)], 10.0), m("e2", [(0, 9), (17, 9)], 10.4),
                                 m("q1", [(0, 100), (200, 100)], 30.0),
                                 m("q2", [(0, 101), (200, 101)], 30.8, level=2.0)])
        self.assertEqual(got["root"], [0, 0, 2, 3])
        self.assertIn(("level", "OVERLAP_ALONG"), {(structures.REASONS[c[2]], structures.EDGE_KINDS[c[3]])
                                                    for c in got["conflicts"]})


if __name__ == "__main__":
    unittest.main()
