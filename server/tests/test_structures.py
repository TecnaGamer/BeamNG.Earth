"""Road revamp track RS steps S1 / S2a (structures.py, earthfast.structure_groups / structure_touch): a bridge chain's
structure groups as a shadow JSON (BNE_DECK_STRUCT=shadow). The groups follow the design's rules (twins, a T on a
deck, joints, never across OSM levels); the JSON's digests are the same whichever tile asks first, in whichever cell
order, in another process. S2a: the JSON comes only from the published solve (never a losing or short one), is made
by the background writer after the publish (no build's cache eligibility changes), is named by its inputs; the chain
closure holds every pair that could be one structure (one owner); members are unique; levels fall back to the OSM
layer."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import mapcells  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402
import structures  # noqa: E402
from test_roads import FRAME, LAT, LON, M_LAT, M_LON, features  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def scene(twin2=False):
    """(the west cell's Features, the east cell's, the boxes near deck a and deck b): a bridge chain over a map cell's
    east edge (a, b end to end), a twin beside it (one deck), a ramp ending on deck a (a T), a street under it, and an
    upper deck of another OSM level crossing over b. twin2: and a deck 2 m off a's other edge (BESIDE, past the old
    closure's 1.5 m)."""
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
    if twin2:
        lines.append([(east - 280, -(w + 2.0)), (east - 120, -(w + 2.0))])
        classes.append("primary")
        levels.append((6, 1.0, 0.0, 1.0))
        ids.append("twin2")
    west = features(lines, classes, levels=levels, ids=ids)
    eastf = features(lines, classes, levels=levels, ids=ids)
    return home, west, eastf, (east - 200, -50, east - 100, 50), (east + 200, -50, east + 300, 50)


def self_flush():
    assert structures.flush(30), "the writer finished"


def read_docs(d):
    out = os.path.join(d, *structures.DEBUG_DIR)
    docs = {}
    for n in sorted(os.listdir(out)) if os.path.isdir(out) else ():
        with open(os.path.join(out, n), encoding="utf-8") as f:
            docs[n] = json.load(f)
    return docs


def run(first, order=1, shadow=True, twin2=False):
    """Solve the scene's chain asking box `first` first (cells listed in `order`): (decks {fid: z bytes}, the shadow
    JSONs {name: doc})."""
    home, west, eastf, box_a, box_b = scene(twin2)
    cell_fn = lambda key: west if key == home else eastf          # noqa: E731
    flat = lambda la, lo: np.full(np.shape(la), 10.0)             # noqa: E731
    with tempfile.TemporaryDirectory() as d, mock.patch.object(revisit, "CACHE_DIR", d), \
            mock.patch.object(structures, "SHADOW", shadow):
        bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
        cells = [west, eastf][::order]
        bp.parts(cells, first)
        decks = {fid: z.tobytes() for box in (box_a, box_b) for fid, spec, xy, z, lon in bp.parts(cells, box)}
        self_flush()
        docs = read_docs(d)
    return decks, docs


def digests(docs):
    return sorted((n, d["digest"], tuple(g["digest"] for g in d["groups"])) for n, d in docs.items())


def chain_doc(docs, fid="a"):
    return next(d for d in docs.values() if any(m["key"].startswith(fid + ":") and m["kind"] != "outside"
                                                for m in d.get("members", ())))


class GroupsTest(unittest.TestCase):
    def test_the_chain_groups(self):
        _decks, docs = run(scene()[3])
        self.assertTrue(docs, "a shadow JSON for the chain")
        doc = chain_doc(docs)
        group = {m["key"].split(":")[0]: m["group"] for m in doc["members"] if m["kind"] != "outside"}
        self.assertEqual(group["a"], group["b"], "end to end: JOIN")
        self.assertEqual(group["a"], group["twin"], "the twin beside them: BESIDE")
        self.assertEqual(group["a"], group["ramp"], "the ramp's end on deck a: END_ON (the T)")
        if "over" in group:
            self.assertNotEqual(group["over"], group["a"], "another OSM level: never one structure")
        kinds = {e["kind"] for e in doc["edges"]}
        self.assertTrue({"JOIN", "BESIDE"} <= kinds, kinds)
        self.assertEqual(len(doc["digest"]), 16)
        self.assertEqual({m["level_src"] for m in doc["members"]}, {"overture"})

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

    def test_off_writes_nothing(self):
        """off: no JSON; in this scene (every pair already in one chain) shadow's decks are off's."""
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
        with tempfile.TemporaryDirectory() as d:
            comp = [("v01", 0), ("v02", 0)]
            self.assertTrue(structures.published(P(), comp, {}, [], None, (3, 4), d))
            self_flush()
            first = read_docs(d)
            self.assertEqual(len(first), 1)
            self.assertTrue(structures.published(P(), comp, {}, [], None, (3, 4), d))
            self_flush()
            self.assertEqual(read_docs(d), first)
            doc = next(iter(first.values()))
            self.assertEqual((doc["capped"], doc["parts"]), ([3, 4], ["v01:0", "v02:0"]))
            self.assertNotIn("groups", doc)

    def test_only_the_published_solve(self):
        """Two solves of one chain staged together, a complete one and one that read short (a fault: the fallback),
        finishing in both orders: the JSON is the published solve's when that is the complete one (its heights are
        the ones in `done`), none when the short one published first (and none from the complete one that lost)."""
        for first in ("complete", "short"):
            with self.subTest(first=first):
                home, west, eastf, box_a, _b = scene()
                cell_fn = lambda key: west if key == home else eastf          # noqa: E731

                def ground(la, lo):
                    if threading.current_thread().name == "short":
                        revisit.fault()
                    return np.full(np.shape(la), 10.0)
                with tempfile.TemporaryDirectory() as d, mock.patch.object(revisit, "CACHE_DIR", d), \
                        mock.patch.object(structures, "SHADOW", True):
                    bp = roads.BridgeProfiles(FRAME, cell_fn, ground)
                    real = bp._structure
                    both = threading.Barrier(2)
                    go = {"complete": threading.Event(), "short": threading.Event()}
                    once = threading.local()

                    def staged(chain, infra_out=None):
                        if not getattr(once, "done", False):      # (the first chain each thread solves)
                            once.done = True
                            both.wait(20)
                            go[threading.current_thread().name].wait(20)
                        return real(chain) if infra_out is None else real(chain, infra_out)
                    bp._structure = staged
                    errs = []

                    def ask():
                        try:
                            bp.parts([west, eastf], box_a)
                        except Exception as ex:     # (reported below)
                            errs.append(ex)
                    ts = {n: threading.Thread(target=ask, name=n) for n in go}
                    skipped = structures.STATS["skipped"]
                    for t in ts.values():
                        t.start()
                    second = "short" if first == "complete" else "complete"
                    go[first].set()
                    ts[first].join(30)
                    go[second].set()
                    ts[second].join(30)
                    self.assertEqual(errs, [])
                    self_flush()
                    docs = {n: v for n, v in read_docs(d).items() if v.get("capped") is None}
                    self.assertGreater(structures.STATS["skipped"], skipped, "the losing / short solve skipped")
                    if first == "short":
                        self.assertEqual(docs, {}, "no JSON from a short publisher, none from the loser")
                        continue
                    self.assertEqual(len(docs), 1, sorted(docs))
                    doc = next(iter(docs.values()))
                    for m in doc["members"]:
                        if m["kind"] == "outside":
                            continue
                        fid, pk = m["key"].split(":")
                        z = bp.done[(fid, int(pk))][2]
                        self.assertEqual(m["z_mm"], [int(round(float(v) * 1000.0)) for v in z], m["key"])

    def test_cache_eligibility_with_the_writer(self):
        """The grouping and the file are made by the writer after the publish, in a scope of its own: a build that
        ends while the writer is still at its job is complete, and a fault inside the writer is no orphan (every
        build running would not be kept). A concurrent orphan fault during the build: not kept, shadow on as off."""
        home, west, eastf, box_a, _b = scene()
        cell_fn = lambda key: west if key == home else eastf          # noqa: E731
        flat = lambda la, lo: np.full(np.shape(la), 10.0)             # noqa: E731
        started, release = threading.Event(), threading.Event()
        real = structures.groups

        def slow(members, outlines=()):
            started.set()
            release.wait(20)
            revisit.fault()             # (the writer reading something short: its own)
            return real(members, outlines)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(revisit, "CACHE_DIR", d), \
                mock.patch.object(structures, "SHADOW", True), mock.patch.object(structures, "groups", slow):
            bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
            with revisit.scope() as b:
                bp.parts([west, eastf], box_a)
            self.assertTrue(started.wait(20), "the writer took the chain")
            self.assertTrue(b.complete, "the build ended before the grouping ran, nothing short")
            release.set()
            self_flush()
            self.assertTrue(b.complete, "the writer's fault is its own scope's")
            self.assertTrue(read_docs(d))
        for shadow in (False, True):
            with self.subTest(shadow=shadow), tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(revisit, "CACHE_DIR", d), mock.patch.object(structures, "SHADOW", shadow):
                bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
                with revisit.scope() as b:
                    t = threading.Thread(target=revisit.fault)
                    t.start()
                    t.join()
                    bp.parts([west, eastf], box_a)
                self_flush()
                self.assertFalse(b.complete)

    def test_one_owner(self):
        """A deck 2 m off deck a's edge is one structure with it (BESIDE): the switch's closure solves it in a's chain
        (no outside conflict); the old 1.5 m closure alone left it outside."""
        _decks, docs = run(scene()[3], twin2=True)
        doc = chain_doc(docs)
        group = {m["key"].split(":")[0]: m["group"] for m in doc["members"] if m["kind"] != "outside"}
        self.assertEqual(group.get("twin2"), group["a"])
        self.assertFalse([c for c in doc["conflicts"] if c["reason"] == "outside"])
        with mock.patch.object(structures, "touch", lambda entries, keys: []):
            _decks, old = run(scene()[3], twin2=True)
        doc = chain_doc(old)
        self.assertTrue([c for c in doc["conflicts"] if c["reason"] == "outside" and "twin2:" in c["a"] + c["b"]])

    def test_one_owner_across_a_cell_edge(self):
        """Two decks 2 m apart edge to edge on either side of a map cell's edge, each in its own cell's map alone:
        the closure reads the cells within HALO_M of the chain, so either side asked first solves both in one chain
        (Codex review: neither centre line reached the other's cell)."""
        home = mapcells.cell_of(LAT, LON)
        east = (home.bounds()[2] - LON) * M_LON
        ekey = mapcells.cell_of(LAT, home.bounds()[2] + 1e-4)
        probe = features([[(0, 0), (100, 0)]], ["primary"], levels=[(0, 1.0, 0.0, 1.0)], ids=["p"])
        w = roads.bridge_specs(probe)[0].width
        west = features([[(east - w / 2 - 1.0, -100), (east - w / 2 - 1.0, 100)]], ["primary"],
                        levels=[(0, 1.0, 0.0, 1.0)], ids=["p"])
        eastf = features([[(east + w / 2 + 1.0, -100), (east + w / 2 + 1.0, 100)]], ["primary"],
                         levels=[(0, 1.0, 0.0, 1.0)], ids=["q"])
        cell_fn = lambda key: west if key == home else eastf if key == ekey else None      # noqa: E731
        flat = lambda la, lo: np.full(np.shape(la), 10.0)             # noqa: E731
        got = []
        for first in ((east - 20, -20, east - 2, 20), (east + 2, -20, east + 20, 20)):
            with tempfile.TemporaryDirectory() as d, mock.patch.object(revisit, "CACHE_DIR", d), \
                    mock.patch.object(structures, "SHADOW", True):
                bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
                bp.parts([west, eastf], first)
                self_flush()
                docs = read_docs(d)
            doc = chain_doc(docs, "p")
            group = {m["key"].split(":")[0]: m["group"] for m in doc["members"] if m["kind"] != "outside"}
            self.assertEqual(group.get("q"), group["p"], first)
            got.append(digests(docs))
        self.assertEqual(got[0], got[1])

    def test_a_wide_deck_across_a_cell_edge_either_order(self):
        """S2a fix (Codex): a narrow deck and a wide one either side of a map cell's edge, each in its own cell's map
        alone, each seed's own cell features alone in the first ask. 60 m wide, 2 m apart edge to edge (BESIDE): S2a's
        halo allowed for a 40 m other deck, so from the narrow deck it never reached the wide deck's cell while the
        wide deck's reached the narrow one's, and the deck asked first decided the chain; now one structure either
        way. 100 m wide (past structures.HALF_MAX_M), 0.5 m apart edge to edge at their raw widths, the cell edge just
        past the narrow deck's halo: the old 1.5 m beside test at raw widths paired them from the wide side alone;
        capped, neither side pairs them.
        Both orders publish the same parts, trusses and shadow JSONs."""
        home = mapcells.cell_of(LAT, LON)
        east = (home.bounds()[2] - LON) * M_LON
        ekey = mapcells.cell_of(LAT, home.bounds()[2] + 1e-4)
        probe = features([[(0, 0), (100, 0)]], ["primary"], levels=[(0, 1.0, 0.0, 1.0)], ids=["p"])
        w = roads.bridge_specs(probe)[0].width
        flat = lambda la, lo: np.full(np.shape(la), 10.0)             # noqa: E731
        for wide, xp, xq, one in ((60.0, east + 0.5 - 32.0 - w / 2, east + 0.5, True),
                                  (100.0, east - 1.1 * (w / 2 + 36.0) - 1.0,
                                   east - 1.1 * (w / 2 + 36.0) - 1.0 + w / 2 + 50.5, False)):
            self.assertGreater(east - xp, 1.1 * (w / 2 + 20.0 + structures.TOUCH_M))     # (past S2a's halo)
            west = features([[(xp, -100), (xp, 100)]], ["primary"], levels=[(0, 1.0, 0.0, 1.0)], ids=["p"])
            eastf = features([[(xq, -100), (xq, 100)]], ["primary"], levels=[(0, 1.0, 0.0, 1.0)], ids=["q"])
            cell_fn = lambda key, a=west, b=eastf: a if key == home else b if key == ekey else None  # noqa: E731

            def specs(feats, wide=wide):
                return [s._replace(width=wide) if s is not None and str(feats.ids[i]) == "q" else s
                        for i, s in enumerate(roads.bridge_specs(feats))]
            boxes = {"p": (xp - 1, -20, xp + 1, 20), "q": (xq - 1, -20, xq + 1, 20)}
            got = []
            for first, seed in (("p", west), ("q", eastf)):
                with self.subTest(wide=wide, first=first), tempfile.TemporaryDirectory() as d, \
                        mock.patch.object(revisit, "CACHE_DIR", d), mock.patch.object(structures, "SHADOW", True):
                    bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
                    bp.specs_of = specs
                    self.assertEqual([fid for fid, *_r in bp.parts([seed], boxes[first])], [first])
                    for box in boxes.values():
                        bp.parts([west, eastf], box)
                    self_flush()
                    docs = read_docs(d)
                    done = sorted((str(k), np.asarray(v[2], np.float64).tobytes()) for k, v in bp.done.items())
                    trusses = sorted((str(k), repr(v)) for k, v in bp.trusses.items())
                    doc = chain_doc(docs, "p")
                    group = {m["key"].split(":")[0]: m["group"] for m in doc["members"] if m["kind"] != "outside"}
                    self.assertEqual(group.get("q") == group["p"], one)
                    got.append((done, trusses, digests(docs)))
            self.assertEqual(got[0], got[1], wide)

    def test_halo_cells_never_cap_a_chain(self):
        """S2a fix (Codex): a chain of 7 map cells 20 m from a cell row's edge touches 7 empty cells of the next row
        with its halo. Counted against CHAIN_CELLS they made 14 and the chain went to window solves (seams); with the
        halo's own budget it is one solve, the same asked from either end. With no halo budget at all the same chain
        is capped (the halo cells are there)."""
        home = mapcells.cell_of(LAT, LON)
        w_, s_, e_, _n = home.bounds()
        cw = (e_ - w_) * M_LON
        x0, y = (w_ - LON) * M_LON, (s_ - LAT) * M_LAT + 20.0
        xs = [x0 + cw * (0.3 + 6.4 * t / 7) for t in range(8)]
        lines = [[(xs[i] + (xs[i + 1] - xs[i]) * t / 8, y) for t in range(9)] for i in range(7)]
        ids = [f"c{i}" for i in range(7)]
        feats = features(lines, ["primary"] * 7, levels=[(i, 1.0, 0.0, 1.0) for i in range(7)], ids=ids)
        cell_fn = lambda key: feats if key.y == home.y and key.z == home.z else None      # noqa: E731
        flat = lambda la, lo: np.full(np.shape(la), 10.0)             # noqa: E731
        got = []
        for budget, box in ((roads.CHAIN_HALO_CELLS, (xs[0], y - 10, xs[0] + 50, y + 10)),
                            (roads.CHAIN_HALO_CELLS, (xs[7] - 50, y - 10, xs[7], y + 10)),
                            (0, (xs[0], y - 10, xs[0] + 50, y + 10))):
            with self.subTest(budget=budget, box=box), tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(revisit, "CACHE_DIR", d), mock.patch.object(structures, "SHADOW", True), \
                    mock.patch.object(roads, "CHAIN_HALO_CELLS", budget):
                bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
                bp.parts([feats], box)
                self_flush()
                docs = read_docs(d)
                capped = [v for v in docs.values() if v.get("capped") is not None]
                whole = [v for v in docs.values() if v.get("capped") is None]
                if budget:
                    self.assertFalse(capped)
                    self.assertEqual(len(whole), 1)
                    self.assertEqual(sorted({m["key"].split(":")[0] for m in whole[0]["members"]}), ids)
                    got.append(digests(docs))
                else:
                    self.assertTrue(capped)
                    self.assertFalse(whole)
        self.assertEqual(got[0], got[1])

    def test_an_owners_solve_checks_its_own_chain(self):
        """The owner store's solve asked by another store's adoption (adopt False) writes the JSON only when the
        chain it resolved is its own, by region (region_of: no store made or touched; a store evicted and made again
        for the same region is the same owner): Codex review rounds 1 and 2."""
        home, west, eastf, box_a, _b = scene()
        cell_fn = lambda key: west if key == home else eastf          # noqa: E731
        flat = lambda la, lo: np.full(np.shape(la), 10.0)             # noqa: E731
        for mine in (True, False):
            with self.subTest(mine=mine), tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(revisit, "CACHE_DIR", d), mock.patch.object(structures, "SHADOW", True):
                bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
                bp.region = (5, 7)
                bp.owner_of = lambda prof, comp, pool: object()        # (never asked: another instance anyway)
                bp.region_of = lambda prof, comp, pool, _m=mine: (5, 7) if _m else (5, 8)
                key = next(k for k, _v in bp._cell_entries(home)[0] if k[0] == "a")
                xy = dict(bp._cell_entries(home)[0])[key][1]
                self.assertIsNotNone(bp._chain_of(key, bp._ends_ll(xy), adopt=False))
                self_flush()
                docs = {n: v for n, v in read_docs(d).items() if v.get("capped") is None}
                self.assertEqual(bool(docs), mine)

    def test_a_full_queue_is_marked(self):
        """A job the writer's full queue refuses: published() says so, counted, and the writer marks the cache's
        JSONs incomplete (dropped-<pid>.json)."""
        import queue

        class P:
            region = None
        full = queue.Queue(1)
        full.put(None)
        with tempfile.TemporaryDirectory() as d:
            n = structures.STATS["dropped"]
            with mock.patch.object(structures, "_queue", lambda: full):
                self.assertFalse(structures.published(P(), [("v", 0)], {}, [], None, (1, 1), d))
            self.assertEqual(structures.STATS["dropped"], n + 1)
            structures._mark_drops()
            self.assertIn(f"dropped-{os.getpid()}.json", read_docs(d))

    def test_named_by_inputs(self):
        """Two snapshots of one chain (another height) are two files; the same inputs one."""
        class P:
            region = None
            frame = FRAME

        class Spec:
            cls, width = "primary", 8.0
        pool = {("a", 0): (Spec(), np.array([[0.0, 0.0], [50.0, 0.0]]), 0.0, 1.0),
                ("b", 0): (Spec(), np.array([[50.0, 0.0], [100.0, 0.0]]), 0.0, 1.0)}
        comp = sorted(pool)
        with tempfile.TemporaryDirectory() as d:
            for _ in range(2):
                structures.published(P(), comp, pool, [np.full(2, 10.0), np.full(2, 10.1)], [], None, d)
            self_flush()
            self.assertEqual(len(read_docs(d)), 1)
            structures.published(P(), comp, pool, [np.full(2, 12.0), np.full(2, 10.1)], [], None, d)
            self_flush()
            docs = read_docs(d)
            self.assertEqual(len(docs), 2)
            self.assertEqual({v["chain"] for v in docs.values()}, {"a:0"})
            self.assertEqual(sorted(n[:-5].rsplit("_", 1)[1] for n in docs), sorted(v["inputs"] for v in docs.values()))

    def test_part_levels(self):
        """The grouping level: the Overture level where a level rule covers the part, else the OSM layer (>= 1), else
        the store's level."""
        f = features([[(0, 0), (100, 0)], [(0, 50), (100, 50)]], ["primary", "primary"],
                     levels=[(0, 2.0, 0.3, 1.0)], ids=["x", "y"])
        f.attrs["osm_layer"] = np.array([3.0, np.nan])
        spans = [(0.0, 0.3, 1.0), (0.3, 1.0, 2.0)]
        self.assertEqual(structures.part_levels(f, 0, spans, 2), [(3.0, 1), (2.0, 0)] * 2)
        self.assertEqual(structures.part_levels(f, 1, [(0.0, 1.0, 1.0)], 1), [(1.0, 2)])
        # a span the rules cover only in part (the span builder merged a mapped level 1 with an unmapped stretch):
        # the OSM layer unless the rules cover at least half of it (Codex review)
        g = features([[(0, 0), (100, 0)]], ["primary"], levels=[(0, 1.0, 0.0, 0.3)], ids=["z"])
        g.attrs["osm_layer"] = np.array([3.0])
        self.assertEqual(structures.part_levels(g, 0, [(0.0, 1.0, 1.0)], 1), [(3.0, 1)])
        self.assertEqual(structures.part_levels(g, 0, [(0.0, 0.5, 1.0)], 1), [(1.0, 0)])
        # a row the span builder ignores (no number) covers nothing: the OSM layer (Codex round 2)
        h = features([[(0, 0), (100, 0)]], ["primary"], levels=[(0, float("nan"), 0.0, 1.0)], ids=["n"])
        h.attrs["osm_layer"] = np.array([3.0])
        self.assertEqual(structures.part_levels(h, 0, [(0.0, 1.0, 1.0)], 1), [(3.0, 1)])

    def test_halo_reaches_every_cell_near(self):
        """The closure's cell halo: a short deck 30 m from a map cell's corner (both directions) reads all four cells
        there; one 2 km inside reads its own cell alone."""
        home = mapcells.cell_of(LAT, LON)
        w, s, e, n = home.bounds()
        ce, cn = (e - LON) * M_LON, (n - LAT) * 111320.0
        near = np.array([[ce - 30.0, cn - 30.0], [ce - 60.0, cn - 60.0]])

        def cells(xy):
            pts = structures.halo(xy, 4.0)
            la = LAT + pts[:, 1] / 111320.0
            lo = LON + pts[:, 0] / M_LON
            return {str(c) for c in mapcells.cells_of(la, lo)}
        self.assertEqual(len(cells(near)), 4)
        mid = np.array([[(w + e) / 2 - LON, (s + n) / 2 - LAT]]) * [M_LON, 111320.0]
        self.assertEqual(cells(np.r_[mid, mid + 10.0]), {str(home)})

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
        # one way twice under two keys (San Diego's c54ad200): one member, the copy in its group, in no edge
        got = structures.groups([m("w:1", [(0, 0), (300, 0)], 10.0, fid="w"),
                                 m("w:2", [(0, 0.01), (300, 0.01)], 10.0, fid="w"),
                                 m("v:1", [(0, 9), (300, 9)], 10.2, fid="v")])
        self.assertEqual((got["dup"], got["root"]), ([0, 0, 2], [0, 0, 0]))
        self.assertTrue(all(1 not in e[:2] for e in got["edges"]))


def ask_all(cell_fn, boxes, feats_list, seed, capped_cells=None):
    """S2b fixpoint: a fresh store asks every box of `boxes` {name: box} in a random order (seed), each with its cells'
    Features in a random order and the asked piece's ring points shuffled (the cells its first reads come from): the
    published parts {key: z bytes}, trusses and shadow digests, and per asked key the chain and whether it was capped
    (_resolve's answer)."""
    import random
    rnd = random.Random(seed)
    names = sorted(boxes)
    rnd.shuffle(names)
    seen = {}
    orig_resolve, orig_chain_of = roads.BridgeProfiles._resolve, roads.BridgeProfiles._chain_of

    def resolve(self, start, pool, ground, loaded, grow=True):
        try:
            comp = orig_resolve(self, start, pool, ground, loaded, grow)
        except roads._Capped:
            seen.setdefault(str(start), set()).add("capped")
            raise
        seen.setdefault(str(start), set()).add((grow, tuple(sorted(str(k) for k in comp))))
        return comp

    def chain_of(self, key, ends, adopt=True):
        ends = list(ends)
        rnd.shuffle(ends)
        return orig_chain_of(self, key, ends, adopt)
    patches = [mock.patch.object(roads.BridgeProfiles, "_resolve", resolve),
               mock.patch.object(roads.BridgeProfiles, "_chain_of", chain_of)]
    if capped_cells is not None:
        patches.append(mock.patch.object(roads, "CHAIN_CELLS", capped_cells))
    flat = lambda la, lo: np.full(np.shape(la), 10.0)             # noqa: E731
    with tempfile.TemporaryDirectory() as d, mock.patch.object(revisit, "CACHE_DIR", d), \
            mock.patch.object(structures, "SHADOW", True), patches[0], patches[1], \
            (patches[2] if len(patches) > 2 else mock.patch.object(roads, "CHAIN_CELLS", roads.CHAIN_CELLS)):
        bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
        for n in names:
            fl = list(feats_list)
            rnd.shuffle(fl)
            bp.parts(fl, boxes[n])
        self_flush()
        docs = read_docs(d)
        done = sorted((str(k), np.asarray(v[2], np.float64).tobytes()) for k, v in bp.done.items())
        trusses = sorted((str(k), repr(v)) for k, v in bp.trusses.items())
    dig = sorted((n, d.get("digest"), tuple(g["digest"] for g in d.get("groups", ())), tuple(d.get("parts", ())))
                 for n, d in docs.items())
    return done, trusses, dig, docs, seen


def end_on_scene():
    """S2b (Codex's third order dependence of incremental discovery): deck A's free end stands on deck B (one level in
    the legacy store, OSM layers 1 and 2: never one structure, so only the legacy end-on test pairs them), and a ground
    road G goes on from A's end 2 m away: G makes A's end an abutment (no end-on pair). B lies in cell Y alone, 5.5 m
    from its north edge; A comes from Y's north neighbour YN straight down onto B's north half; G lies in YN alone.
    (cell_fn, boxes, the cells' Features)"""
    home = mapcells.cell_of(LAT, LON)
    ykey = mapcells.CellKey(home.z, home.x + 1, home.y)
    ynkey = mapcells.CellKey(home.z, home.x + 1, home.y - 1)
    yw, _ys, ye, yn = ykey.bounds()
    xw, xe = (yw - LON) * M_LON, (ye - LON) * M_LON
    y_n = (yn - LAT) * M_LAT
    xm = (xw + xe) / 2
    b = [(xm - 300, y_n - 5.5), (xm + 300, y_n - 5.5)]
    a = [(xm, y_n + 400), (xm, y_n + 200), (xm, y_n - 1.5)]
    g = [(xm + 1.0, y_n + 0.3), (xm + 300, y_n + 150)]
    fy = features([a, b], ["primary", "primary"], flags=[(0, "is_bridge"), (1, "is_bridge")], ids=["A", "B"])
    fy.attrs["osm_layer"] = np.array([1.0, 2.0])
    fyn = features([a, g], ["primary", "residential"], flags=[(0, "is_bridge")], ids=["A", "G"])
    fyn.attrs["osm_layer"] = np.array([1.0, np.nan])
    cell_fn = lambda key: fy if key == ykey else fyn if key == ynkey else None      # noqa: E731
    boxes = {"A": (xm - 5, y_n + 350, xm + 5, y_n + 360), "B": (xm + 250, y_n - 9, xm + 260, y_n - 3)}
    return cell_fn, boxes, [fy, fyn]


class FixpointTest(unittest.TestCase):
    """S2b: chain discovery on the switch path is a fixpoint of the complete local set (roads.BridgeProfiles._closure):
    the same chains, owners, window decisions and published parts whatever deck is asked first and whatever the cell
    order."""

    def test_a_provisional_end_on_never_caps(self):
        """Codex's case: from B, the end-on pair with A was judged before G's cell was read; A and B together need two
        cells, over a budget of one, so B went to window solves. Now B is settled (its halo read) before any of its
        pairs counts: G is there, A's end is an abutment, B's chain is itself (1 cell) from either order."""
        cell_fn, boxes, fl = end_on_scene()
        got = []
        for seed in range(6):
            with self.subTest(seed=seed):
                done, trusses, dig, docs, seen = ask_all(cell_fn, boxes, fl, seed, capped_cells=1)
                whole = [v for v in docs.values() if v.get("capped") is None]
                self.assertTrue(any(m["key"].startswith("B:") for v in whole for m in v["members"]
                                    if m["kind"] != "outside"), "B's chain solved whole")
                self.assertFalse(any(m["key"].startswith("A:") for v in whole for m in v["members"]
                                     if m["kind"] != "outside"), "A (two cells) capped")
                got.append((done, trusses, dig))
        self.assertTrue(all(g == got[0] for g in got))

    def test_random_seeds_and_cell_orders(self):
        """The S1 scene (a chain over a cell edge, its twin, a T ramp, an upper level; twin2 beside), the wide-deck
        case across a cell edge and the end-on case: every random first deck and cell order publishes the same parts,
        trusses and shadow JSONs, and each asked part's chain is one answer."""
        home, west, eastf, box_a, box_b = scene(twin2=True)
        boxes = {"a": box_a, "b": box_b, "ramp": (box_a[0] + 90, 40, box_a[0] + 110, 60),
                 "twin2": (box_a[0] + 60, -30, box_a[0] + 80, -10)}
        cases = [(lambda key: west if key == home else eastf, boxes, [west, eastf], None),
                 end_on_scene() + (1,)]
        for n, (cell_fn, bx, fl, cap) in enumerate(cases):
            ref = None
            for seed in range(8):
                with self.subTest(case=n, seed=seed):
                    done, trusses, dig, _docs, seen = ask_all(cell_fn, bx, fl, seed, capped_cells=cap)
                    self.assertTrue(done)
                    # (a window's structure is built too: never a None stored under a live key, Codex round 2)
                    self.assertNotIn("None", [t for _k, t in trusses])
                    chains = {k: sorted(v, key=repr) for k, v in seen.items()}
                    for k, v in chains.items():
                        grown = [c for c in v if c != "capped" and c[0]]
                        self.assertLessEqual(len(grown) + ("capped" in v), 1, (k, v))
                    if ref is None:
                        ref = (done, trusses, dig)
                    self.assertEqual((done, trusses, dig), ref)


def window_scene():
    """S2b part 1 fix (Codex high on 1ced286a): one chain b - a - c - d end to end along a row of map cells, over a
    budget of 2 cells (window solves). a (the lowest key) lies in both overlapping windows of the blocks of a and of
    c; b only in a's window, d only in c's. (cell_fn, boxes {name: box}, {box name: its cells' Features})"""
    home = mapcells.cell_of(LAT, LON)
    w0 = (home.bounds()[0] - LON) * M_LON
    cw = (home.bounds()[2] - home.bounds()[0]) * M_LON
    y = 900.0
    x = lambda k, f: w0 + (k - home.x % roads.CHAIN_BLOCK + f) * cw          # noqa: E731 (k: cells from a block's first)
    parts = {"b": [(x(-1, 0.3), y), (x(0, 0.9), y)], "a": [(x(0, 0.9), y), (x(1, 0.95), y)],
             "c": [(x(1, 0.95), y), (x(3, 0.05), y)], "d": [(x(3, 0.05), y), (x(3, 0.9), y)]}

    def cells_of(ln):
        ln = np.asarray(ln, np.float64)
        return {str(c) for c in mapcells.cells_of(LAT + ln[:, 1] / M_LAT, LON + ln[:, 0] / M_LON)}
    feats = {}
    for k in range(-3, 7):
        ck = mapcells.CellKey(home.z, home.x + k, home.y)
        ids = [i for i in sorted(parts) if str(ck) in cells_of(np.linspace(parts[i][0], parts[i][1], 50))]
        if ids:
            feats[str(ck)] = features([parts[i] for i in ids], ["primary"] * len(ids),
                                      flags=[(j, "is_bridge") for j in range(len(ids))], ids=ids)
    boxes, asked = {}, {}
    for i, ((x0, _), (x1, _)) in parts.items():
        m = (x0 + x1) / 2
        boxes[i] = (m - 5, y - 5, m + 5, y + 5)
        asked[i] = [feats[c] for c in sorted(cells_of([(m - 5, y), (m + 5, y)])) if c in feats]
    return (lambda key: feats.get(str(key))), boxes, asked


class WindowStructureTest(unittest.TestCase):
    """S2b part 1 fix: on the switch path a window solve's live structure (trusses, their piers) is the legacy path's
    exactly, its decks too; the switch only writes diagnostics there."""

    def run_windows(self, order, shadow):
        cell_fn, boxes, asked = window_scene()

        def structure(_self, chain, infra_out=None):         # (what the structure was made from, as its beams)
            made = repr(sorted((round(float(xy[0][0]), 1), round(float(xy[-1][0]), 1), np.asarray(z).tobytes())
                               for xy, z, _w in chain))
            return [made], [made]
        flat = lambda la, lo: np.full(np.shape(la), 10.0)             # noqa: E731
        with tempfile.TemporaryDirectory() as d, mock.patch.object(revisit, "CACHE_DIR", d), \
                mock.patch.object(structures, "SHADOW", shadow), mock.patch.object(roads, "CHAIN_CELLS", 2), \
                mock.patch.object(roads.BridgeProfiles, "_structure", structure):
            bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
            for n in order:
                bp.parts(asked[n], boxes[n])
            self_flush()
            done = sorted((str(k), np.asarray(v[2], np.float64).tobytes()) for k, v in bp.done.items())
            trusses = sorted((str(k), repr(v)) for k, v in bp.trusses.items())
            piers = sorted((str(k), repr(v)) for k, v in bp.piers.items())
        return done, trusses, piers

    def test_overlapping_windows_as_switch_off(self):
        """Two overlapping windows sharing their lowest piece a, asked in either order: the live decks, beams and
        piers on the switch path are the switch-off ones (1ced286a: the window's structure was keyed by min(wcap) and
        made from every capped part of the window, b a c or a c d, the first window to commit winning). The decks
        are the same in both orders. (The structure under a window's lowest piece follows the order in which its
        windows commit: the legacy path's own behaviour, switch off byte-identical, kept)"""
        decks = None
        for order in (("a", "c"), ("c", "a"), ("b", "d"), ("d", "b")):
            with self.subTest(order=order):
                off = self.run_windows(order, False)
                on = self.run_windows(order, True)
                self.assertEqual(on, off)
                self.assertEqual(len(on[0]), 4)
                self.assertEqual(len(on[1]), 1)
                decks = decks or on[0]
                self.assertEqual(on[0], decks)


class JointHeightsTest(unittest.TestCase):
    """S2b part 1: the joint height solve (structures.heights: earthfast.structure_heights), shadow only."""

    def twins(self, dz=0.8, link=False):
        xs = np.linspace(0.0, 600.0, 151)
        za = np.full(len(xs), 10.0)
        zb = 10.0 + dz * 4.0 * (xs / 600.0) * (1.0 - xs / 600.0)
        m = lambda k, y, z: {"key": k, "fid": k, "cls": "primary", "xy": np.c_[xs, np.full(len(xs), y)],  # noqa: E731
                             "z": z, "half": 4.0, "kind": structures.ROAD, "level": 1.0}
        members = [m("a:0", 0.0, za), m("b:0", 9.0, zb)]
        got = structures.groups(members)
        raw = {"z": [za, zb], "floors": [], "under": [8.35, 8.35], "rank": [2, 2], "link": [False, link]}
        return members, got, raw

    def test_twins_meet(self):
        """Twins 9 m apart, one 0.8 m higher mid-span: one structure; the joint heights meet within 2 cm in the
        middle (each half way: equal weights), the abutments (pinned: plan neutral) as they were."""
        members, got, raw = self.twins()
        self.assertEqual(got["root"], [0, 0])
        out = structures.heights(members, 2, got, raw)
        st = out["structures"][0]
        self.assertEqual(st["status"], "solved")
        self.assertGreater(st["today"]["twin_dz_mm"], 700)
        self.assertLessEqual(st["joint"]["twin_free_mm"], 20)     # (the abutments pinned: their eases as they were)
        za, zb = out["z_joint"]
        self.assertEqual((za[0], zb[-1]), (10000, 10000))
        self.assertLessEqual(abs(za[75] - zb[75]), 20)
        self.assertLess(abs(za[75] - 10400), 50)
        self.assertEqual(out["digest"], structures.heights(members, 2, got, raw)["digest"])

    def test_a_ramp_bends(self):
        """The same with b a link: the mainline (weight 10) moves 1/11 of the gap, the ramp the rest."""
        members, got, raw = self.twins(link=True)
        za, zb = structures.heights(members, 2, got, raw)["z_joint"]
        self.assertLess(abs(za[75] - 10000), 100)
        self.assertLessEqual(abs(za[75] - zb[75]), 20)

    def test_in_the_shadow_json(self):
        """The shadow JSON carries z_in_mm and z_joint_mm beside z_mm for every chain member and a joint section;
        the same whichever tile asks first."""
        _decks, docs = run(scene()[3])
        doc = chain_doc(docs)
        self.assertEqual(doc["format"], 3)
        for m in doc["members"]:
            if m["kind"] == "outside":
                self.assertIsNone(m.get("z_joint_mm"))
                continue
            self.assertEqual(len(m["z_joint_mm"]), len(m["z_mm"]))
            self.assertEqual(len(m["z_in_mm"]), len(m["z_mm"]))
        self.assertTrue(doc["joint"]["structures"])
        self.assertTrue({s["status"] for s in doc["joint"]["structures"]} <= set(structures.STATUS))
        other = chain_doc(run(scene()[4], order=-1)[1])
        self.assertEqual(other["joint"]["digest"], doc["joint"]["digest"])

    def test_over_budget_falls_back_and_the_queue_goes_on(self):
        """A dense synthetic structure (80 decks 0.2 m apart, 2 km, every pair an edge: millions of coupling
        candidates) is over the native solve's budgets (heights.rs Budget, counted before anything is made): today's
        heights with status "budget", at once, and the writer's next job (the twins) is solved."""
        import time
        n = 80
        xs = np.linspace(0.0, 2000.0, 501)
        members = [{"key": f"d{k}:0", "fid": f"d{k}", "cls": "primary", "xy": np.c_[xs, np.full(len(xs), 0.2 * k)],
                    "z": np.full(len(xs), 11.0 + 0.001 * k), "half": 4.0, "kind": structures.ROAD, "level": 1.0}
                   for k in range(n)]
        beside = structures.EDGE_KINDS.index("BESIDE")
        dense = {"root": [0] * n, "dup": list(range(n)), "conflicts": [],
                 "edges": [(a, b, beside, 0, 0.0) for a in range(n) for b in range(a + 1, n)]}
        raw = {"z": [np.full(len(xs), 10.0 + 0.01 * k) for k in range(n)], "floors": [], "under": [8.35] * n,
               "rank": [2] * n, "link": [False] * n}
        tw_members, tw_got, tw_raw = self.twins()
        out = []

        def dense_job():
            t0 = time.monotonic()
            got = structures.heights(members, n, dense, raw)
            out.append(("dense", got, time.monotonic() - t0))
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(structures._submit(dense_job, d))
            self.assertTrue(structures._submit(lambda: out.append(("twins", structures.heights(tw_members, 2, tw_got,
                                                                                               tw_raw), 0.0)), d))
            self_flush()
        self.assertEqual([o[0] for o in out], ["dense", "twins"])
        got, took = out[0][1], out[0][2]
        self.assertLess(took, 10.0)        # (the counts alone: ~0.1 s; a loose bound for a busy machine)
        self.assertEqual({s["status"] for s in got["structures"]}, {"budget"})
        self.assertEqual(got["z_joint"][7], [11007] * len(xs))       # (today's heights, in mm)
        self.assertEqual(out[1][1]["structures"][0]["status"], "solved")


if __name__ == "__main__":
    unittest.main()
