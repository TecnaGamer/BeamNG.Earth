"""The interchange pipeline skeleton (server/ix, SPEC.md): the steps wrap today's _Planner._shape_region_calc with no
change in what it returns (a private copy of the old body, from git HEAD, is the reference), every step's output is an
artifact that round-trips save -> load, two runs give the same keys and bytes, and without BNE_IX_CACHE nothing is
written."""
import glob
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import dnp  # noqa: E402
import grading  # noqa: E402
import gradeshapes as _gsh  # noqa: E402
import mapcells  # noqa: E402
from ix import artifacts, cache, owner, pipeline  # noqa: E402
from test_approaches import features, flat_elev, overpass  # noqa: E402
from test_grading import FakeMD, LAT, LON  # noqa: E402

from grading import SHAPES_PAD_M, log  # noqa: E402

# (the old body's own: the farthest it followed a system from the cell, its solve tables' lock, its solve cache's key)
SHAPES_CAP_M = 4000.0
_SHAPE_SOLVE_LOCK = threading.Lock()


def _shape_sys_key(net, sysm, smp):
    import hashlib
    h = hashlib.sha1(_gsh.system_key(net, sysm).encode())
    for x in smp:
        h.update(np.round(np.asarray(x["xy"], np.float64), 3).tobytes())
        for i in np.unique(x["mem"]).tolist():
            h.update(repr(net.m[i].key).encode() + repr((net.m[i].kind, getattr(net.m[i], "level", None))).encode())
    return h.hexdigest()


def underpass():
    """A trunk road east-west in a short tunnel under a secondary road north-south on the ground."""
    a, b = (400 - 100) / 800, (400 + 100) / 800
    return features([[(-400, 0), (400, 0)], [(0, -400), (0, 400)]], ["trunk", "secondary"],
                    flags=[(0, "is_tunnel", a, b)], ids=["trk", "sec"])


def _shape_region_calc_ref(planner, home, hk):
    """The pre-change _Planner._shape_region_calc, copied verbatim from commit 9e22fc3bf (only self -> planner); the
    pipeline's reference."""
    import xproc
    t0 = time.perf_counter()
    lon0, lat0, lon1, lat1 = (float(v) for v in home.bounds())
    ce, cn, _ = planner.frame.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]),
                                           0.0)
    h_lo = np.array([np.min(ce), np.min(cn)]) - SHAPES_PAD_M
    h_hi = np.array([np.max(ce), np.max(cn)]) + SHAPES_PAD_M
    box_lo, box_hi = h_lo - 200.0, h_hi + 200.0
    cap_lo, cap_hi = h_lo - SHAPES_CAP_M, h_hi + SHAPES_CAP_M

    def make_fixed(net):
        rail_tops = {}

        def fixed_z(i, s_):
            if np.ndim(s_):                 # (many: the ground read once for all, _ground_lines)
                sv_ = np.asarray(s_, np.float64)
                if net.m[i].kind in ("rail", "rail_deck"):
                    return np.array([np.nan if z is None else z
                                     for z in planner._rail_tops(net, i, sv_.tolist(), rail_tops)])
                sv = np.clip(sv_[:, None] + np.linspace(-24.0, 24.0, 13)[None], 0.0, net.L[i])
                xy = np.stack([dnp.interp(sv, net.s[i], net.lines[i][:, 0]),
                               dnp.interp(sv, net.s[i], net.lines[i][:, 1])], axis=2)
                return planner._ground_lines(xy)[:, 6]
            m = net.m[i]
            if m.kind in ("rail", "rail_deck"):     # (a track, on its deck or on the ground: its rails' top over the
                # bed the railways lay and draw it on, never a height of the shapes' own: its deck's mapped ends' ground,
                # down the slopes of the street's cutting under it, stood Compiegne's 7 m rail bridge 2.8 m under its
                # track, the street was dug that much too deep and the track's fill beside it stood 10.5 m tall)
                return planner._rail_tops(net, i, [float(s_)], rail_tops)[0]   # (None: the railways lay none of it, no
                #                                                               track there to clear)
            q = _gsh.at_s(net.lines[i], net.s[i], s_)
            sv = np.clip(np.linspace(s_ - 24.0, s_ + 24.0, 13), 0.0, net.L[i])
            xy = np.stack([dnp.interp(sv, net.s[i], net.lines[i][:, 0]),
                           dnp.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1)
            return float(planner._ground_line(xy)[6]) if len(xy) else None

        return fixed_z

    byk, loaded = {}, set()
    while True:
        xproc.check_cancel()        # (a place a teleport left: its systems not found and solved for it)
        cla, clo, _ = planner.frame.enu_to_geodetic(np.array([box_lo[0], box_hi[0], box_lo[0], box_hi[0]]),
                                                 np.array([box_lo[1], box_lo[1], box_hi[1], box_hi[1]]), 0.0)
        keys = sorted(set(mapcells.cells_for_bbox(float(np.min(cla)), float(np.min(clo)), float(np.max(cla)),
                                                  float(np.max(clo)))) - loaded, key=str)
        for k in keys:
            loaded.add(k)
            for m in planner._shape_members(k):
                old = byk.get(m.key)
                if old is None or len(m.line) > len(old.line):
                    byk[m.key] = m
        members = _gsh.one_road([byk[q] for q in sorted(byk, key=str)
                                 if ((byk[q].line >= box_lo) & (byk[q].line <= box_hi)).all(1).any()])
        net, systems = _gsh.find_systems(members, lambda net_: (planner._ground_line, make_fixed(net_)))
        mine = []
        n_lo, n_hi = box_lo.copy(), box_hi.copy()
        for sysm in systems:
            pts = np.concatenate([net.lines[i] for i in sysm["seeds"]])
            b_lo, b_hi = pts.min(0), pts.max(0)
            if (b_hi < h_lo).any() or (b_lo > h_hi).any():
                continue
            mine.append(sysm)
            if (b_lo < box_lo + 100.0).any() or (b_hi > box_hi - 100.0).any():     # (open: follow it)
                n_lo = np.minimum(n_lo, b_lo - 300.0)
                n_hi = np.maximum(n_hi, b_hi + 300.0)
        n_lo, n_hi = np.maximum(n_lo, cap_lo), np.minimum(n_hi, cap_hi)
        if (n_lo >= box_lo).all() and (n_hi <= box_hi).all():
            break
        box_lo, box_hi = n_lo, n_hi
    t1 = time.perf_counter()

    fixed_z = make_fixed(net)

    shapes, failed, solved = [], 0, []
    for sysm in mine:
        xproc.check_cancel()        # (... nor its next system: a left city's 40 s solves held the road tiles)
        pcs = _gsh.pieces(net, sysm)
        smp = _gsh.sample(net, pcs)
        ck = _shape_sys_key(net, sysm, smp)
        cache = planner.__dict__.setdefault("_shape_solved", {})   # (this grading's: its ground, its frame)
        solving = planner.__dict__.setdefault("_shape_solving", {})
        diag = {}
        # (one thread solves a system, the others asking for it meanwhile wait for its answer: the regions round a
        # system are planned on as many threads, each found the cache empty and solved the same system at the same
        # time, Brunau's 4 regions 4 times, 6 of the 8 s of its solves)
        while True:
            with _SHAPE_SOLVE_LOCK:
                hit = cache.get(ck)
                ev = None if hit is not None else solving.get(ck)
                if hit is None and ev is None:
                    ev = solving[ck] = threading.Event()
                    break
            if hit is not None:
                break
            ev.wait()
        if hit is not None:
            ground, zs, diag = hit
        else:
            try:
                ground = [planner._ground_line(x["xy"]) for x in smp]
                zs = _gsh.solve_shape(net, sysm, pcs, smp, ground, fixed_z, diag)
                with _SHAPE_SOLVE_LOCK:
                    if len(cache) >= 4096:
                        cache.clear()
                    cache[ck] = (ground, zs, diag)
            finally:
                with _SHAPE_SOLVE_LOCK:
                    solving.pop(ck, None)
                ev.set()
        if zs is None:          # (no answer: the system as it is, and why in the log; no stand-in)
            failed += 1
            log.error("grading: SHAPES infeasible system in region %s: %d pieces, %d crossings, %d ends; %s; "
                      "members %s", home, len(pcs), len(sysm["crossings"]), len(sysm["ends"]),
                      diag.get("qp_error") or ", ".join("%s %s" % (k, diag[k]) for k in sorted(diag)
                                                        if k not in ("converged", "feasible")),
                      ", ".join(sorted({str(net.m[i].key[0])[:8] + ":" + net.m[i].kind for i in sysm["seeds"]}))[:600])
            continue
        solved.append(sysm)
        shapes.append((pcs, smp, zs))
        if os.environ.get("BNE_SHAPES_DEBUG"):
            planner._shape_debug(net, sysm, pcs, smp, zs, ground, fixed_z, diag)

    def ground_along(i, sv):
        xy = np.stack([dnp.interp(sv, net.s[i], net.lines[i][:, 0]), dnp.interp(sv, net.s[i], net.lines[i][:, 1])],
                      axis=1)
        return planner._ground_line(xy)
    emb = _gsh.embankments(net, solved, shapes, ground_along)
    prof = {}
    for (pcs, smp, zs) in shapes:
        for pc, x, z in zip(pcs, smp, zs):
            if pc["kind"] == "deck":
                prof.setdefault(str(net.m[pc["main"]].key[0]), []).append((x["xy"], np.asarray(z, np.float64),
                                                                            "deck"))
    for j, lst in emb.items():
        m = net.m[j]
        if m.kind not in ("ground", "tunnel"):
            continue
        for sv, z, mode in lst:
            xy = np.stack([dnp.interp(sv, net.s[j], net.lines[j][:, 0]),
                           dnp.interp(sv, net.s[j], net.lines[j][:, 1])], axis=1)
            prof.setdefault(str(m.key[0]), []).append((xy, np.asarray(z, np.float64), mode))
    log.warning("grading: SHAPES pid %d region %s: %d cells, %d members, %d systems (%d infeasible, left as they are), %d roads "
                "graded, find %.2f s, solve %.2f s", os.getpid(), hk, len(loaded), len(members), len(mine), failed,
                len(prof), t1 - t0, time.perf_counter() - t1)
    return prof


def prof_of(feats, calc, hk="test"):
    """A fresh planner's region of the overpass fixture, through calc (planner, home, hk) -> prof."""
    home = mapcells.cell_of(LAT, LON)
    planner = grading.PlanStore(FakeMD(feats), flat_elev).planner_of_cell(home)
    with unittest.mock.patch.object(grading, "SHAPES_ON", True):
        return calc(planner, home, hk)


def prof_bytes(prof):
    """{fid: [(xy bytes, z bytes, mode)]}: comparable byte for byte."""
    return {fid: [(np.asarray(xy, np.float64).tobytes(), np.asarray(z, np.float64).tobytes(), mode)
                  for xy, z, mode in lst] for fid, lst in prof.items()}


def check_same(test, a, b, where=""):
    """a and b (two records, or parts of one) are equal: records field by field, arrays by bytes, the rest by ==."""
    if artifacts.is_record(a):
        test.assertTrue(artifacts.is_record(b), where)
        test.assertEqual(type(a).__name__, type(b).__name__, where)
        for f in artifacts.fields(a):
            check_same(test, getattr(a, f.name), getattr(b, f.name), where + "." + f.name)
        return
    if isinstance(a, np.ndarray):
        test.assertTrue(isinstance(b, np.ndarray), where)
        test.assertEqual(a.dtype, b.dtype, where)
        test.assertEqual(a.tobytes(), b.tobytes(), where)
        return
    if isinstance(a, (list, tuple)):
        test.assertEqual(type(a), type(b), where)
        test.assertEqual(len(a), len(b), where)
        for n, (x, y) in enumerate(zip(a, b)):
            check_same(test, x, y, "%s[%d]" % (where, n))
        return
    if isinstance(a, dict):
        test.assertEqual(sorted(a), sorted(b), where)
        for k in a:
            check_same(test, a[k], b[k], where + "[%r]" % k)
        return
    test.assertEqual(a, b, where)


class SameOutputTest(unittest.TestCase):
    """ix.pipeline.region and the pre-change _shape_region_calc give identical prof: same keys, same xy / z bytes, same
    modes."""

    def test_the_overpass_region_is_the_old_body_s(self):
        old = prof_of(overpass(), _shape_region_calc_ref, "ref")
        new = prof_of(overpass(), lambda p, h, k: pipeline.region(p, h, k), "new")
        self.assertTrue(new, "the overpass is graded")
        self.assertEqual(list(old), list(new))
        self.assertEqual(prof_bytes(old), prof_bytes(new))

    def test_the_underpass_region_is_the_old_body_s(self):
        old = prof_of(underpass(), _shape_region_calc_ref, "ref")
        new = prof_of(underpass(), lambda p, h, k: p._shape_region_calc(h, k), "new")
        self.assertTrue(new, "the underpass is graded")
        self.assertEqual(list(old), list(new))
        self.assertEqual(prof_bytes(old), prof_bytes(new))


class RoundTripTest(unittest.TestCase):
    """Every artifact of a run round-trips save -> load with equal arrays (tobytes) and equal fields."""

    def test_every_artifact_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(cache, "ROOT", tmp):
                prof = prof_of(overpass(), lambda p, h, k: p._shape_region_calc(h, k), "round")
            self.assertTrue(prof)
            files = sorted(glob.glob(os.path.join(tmp, "s*", "*", "*.npz")))
            steps = {int(os.path.basename(os.path.dirname(os.path.dirname(f)))[1:]) for f in files}
            self.assertTrue(set((1, 2, 3, 4, 5, 6)) <= steps, "every step wrote its artifact: %s" % sorted(steps))
            for f in files:
                art = artifacts.load(f)
                self.assertTrue(artifacts.is_record(art), f)
                data = artifacts.to_bytes(art)
                with open(f, "rb") as fh:
                    self.assertEqual(data, fh.read(), "%s saves back byte for byte" % f)
                again = f + ".again"
                try:
                    artifacts.save(art, again)
                    check_same(self, artifacts.load(again), art, os.path.relpath(f, tmp))
                finally:
                    if os.path.exists(again):
                        os.unlink(again)


class DeterminismTest(unittest.TestCase):
    """Two runs produce the same artifact keys and bytes."""

    def run_once(self, tmp):
        for table in (owner.PROCESS, owner.INDEX):      # (the first run's interchange is not the second's to read)
            table.clear()
        with unittest.mock.patch.object(cache, "ROOT", tmp):
            prof = prof_of(overpass(), lambda p, h, k: p._shape_region_calc(h, k), "det")
        out = {}
        for f in sorted(glob.glob(os.path.join(tmp, "s*", "*", "*.npz"))):
            with open(f, "rb") as fh:
                out[os.path.relpath(f, tmp)] = fh.read()
        return prof, out

    def test_two_runs_the_same_keys_and_bytes(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            prof_a, files_a = self.run_once(a)
            prof_b, files_b = self.run_once(b)
        self.assertTrue(files_a)
        self.assertEqual(sorted(files_a), sorted(files_b))
        for k in files_a:
            self.assertEqual(files_a[k], files_b[k], k)
        self.assertEqual(prof_bytes(prof_a), prof_bytes(prof_b))


class OffByDefaultTest(unittest.TestCase):
    """With BNE_IX_CACHE unset nothing is written: the game's default, no change in disk use."""

    def test_nothing_written_without_the_cache(self):
        with unittest.mock.patch.dict(os.environ):
            os.environ.pop("BNE_IX_CACHE", None)
            self.assertIsNone(os.environ.get("BNE_IX_CACHE") or None)
            with unittest.mock.patch.object(cache, "ROOT", None), \
                    unittest.mock.patch.object(cache, "put", side_effect=AssertionError("the cache is off")):
                prof = prof_of(overpass(), lambda p, h, k: p._shape_region_calc(h, k), "off")
        self.assertTrue(prof, "the profiles still come back")


class TimingLinesTest(unittest.TestCase):
    """One INFO line per step (per system for steps 3-6) and one per region; a failing step is named in its line."""

    def region_lines(self, feats, calc=None):
        import logging
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        h = Grab(level=logging.INFO)
        old = log.level
        log.addHandler(h)
        log.setLevel(logging.INFO)
        try:
            prof_of(feats, calc or (lambda p, hm, k: pipeline.region(p, hm, k)), "tm")
        finally:
            log.removeHandler(h)
            log.setLevel(old)
        return [r for r in records if r.startswith("ix ")]

    def test_a_line_per_step_and_a_region_line(self):
        import re
        lines = self.region_lines(overpass())
        step = [ln for ln in lines if ln.startswith("ix step: ")]
        pat = re.compile(r"^ix step: (\w+) key ([0-9a-f]{12}) pieces (\d+) crossings (\d+) (\d+\.\d\d) s (ok|failed: .+) @ \S+")
        self.assertTrue(all(pat.match(ln) for ln in step), step)
        names = [pat.match(ln).group(1) for ln in step]
        # (the region's own, then the interchange's: its extent (its paths only for the cache), then its one system's steps)
        self.assertEqual(names, ["paths", "extent", "extent", "order", "terrain", "datum", "heights"])
        self.assertTrue(all(" ok " in ln for ln in step), step)
        region = [ln for ln in lines if ln.startswith("ix region: ")]
        self.assertEqual(len(region), 1)
        for n in pipeline.STEPS:
            self.assertIn("%s " % n, region[0])

    def test_a_system_with_no_answer_is_named_in_its_heights_line(self):
        def no_answer(net, sysm, pcs, smp, ground, fixed_z, diag=None):
            diag.update({"converged": False, "feasible": False, "status": "stuck", "rounds": 7})
            return None
        with unittest.mock.patch.object(_gsh, "solve_shape", no_answer):
            lines = self.region_lines(overpass())
        heights = [ln for ln in lines if ln.startswith("ix step: heights ")]
        self.assertEqual(len(heights), 1)
        self.assertIn("failed: infeasible (rounds 7, status stuck)", heights[0])

    def test_a_level_cycle_is_named_in_its_order_line(self):
        from ix import s3_order, artifacts
        cycle = artifacts.Finding(3, "fail", "order_cycle", "the crossings of a loop")
        real = s3_order.run

        def with_cycle(*a):
            out = real(*a)
            return type(out)(out.meta, out.system, out.crossings, out.sign, out.level, [cycle])
        with unittest.mock.patch.object(s3_order, "run", with_cycle):
            lines = self.region_lines(overpass())
        order = [ln for ln in lines if ln.startswith("ix step: order ")]
        self.assertTrue(order and all("failed: order_cycle: the crossings of a loop" in ln for ln in order), order)


class CancelTest(unittest.TestCase):
    """A place a teleport left stops between systems and between the region's growth rounds (xproc.check_cancel)."""

    def test_a_cancelled_job_raises_before_any_system(self):
        import pools
        import xproc
        solved = []
        with unittest.mock.patch.object(xproc, "_cancelled", lambda: True),                 unittest.mock.patch.object(_gsh, "solve_shape", lambda *a, **k: solved.append(1)):
            with self.assertRaises(pools.Abandoned):
                prof_of(overpass(), lambda p, h, k: pipeline.region(p, h, k), "cancel")
        self.assertEqual(solved, [])

    def test_a_cancel_between_systems_stops_before_the_next_solve(self):
        import pools
        import xproc
        asked = []

        def cancel_after_the_growth():
            asked.append(1)
            return len(asked) > 1       # (the growth loop's first round passes; the first system is stopped)
        with unittest.mock.patch.object(xproc, "_cancelled", cancel_after_the_growth):
            with self.assertRaises(pools.Abandoned):
                prof_of(overpass(), lambda p, h, k: pipeline.region(p, h, k), "cancel2")


class PerInterchangeTest(unittest.TestCase):
    """Steps 1 and 2 are one interchange's: its members are what it names and reaches, never a ground road's pair with
    another, numbered by key, and every later artifact of the interchange uses that numbering."""

    def run_overpass(self, hk="pi"):
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(cache, "ROOT", tmp):
                self.assertTrue(prof_of(overpass(), lambda p, h, k: pipeline.region(p, h, k), hk))
            from ixcheck import load
            return load.scan(tmp)

    def test_the_paths_and_extent_are_the_interchange_s_own(self):
        scanned = self.run_overpass()
        paths, extents = scanned["steps"][1], scanned["steps"][2]
        self.assertEqual(len(paths), len(extents))
        self.assertTrue(paths)
        for p, e in zip(paths, extents):
            self.assertTrue(p.meta.scope.startswith("system:"))
            self.assertEqual(p.meta.scope, e.meta.scope)
            self.assertEqual(len(e.systems), 1)
            keys = [m.key for m in p.members]
            self.assertEqual(keys, sorted(keys), "numbered by key")
            kinds = [m.kind for m in p.members]
            for r in list(p.joins) + list(p.crossings):
                self.assertTrue(kinds[r.a] in artifacts.STRUCTURES or kinds[r.b] in artifacts.STRUCTURES,
                                "a ground road's pair with another is no relation of an interchange")
            for i in e.systems[0].seeds:
                self.assertIn(kinds[i], artifacts.STRUCTURES)

    def test_the_heights_use_the_paths_numbering(self):
        scanned = self.run_overpass()
        for h in scanned["steps"][6]:
            if getattr(h, "pieces", None) is None:
                continue
            paths = [a for a in scanned["steps"][1] if a.meta.scope == h.meta.scope][0]
            for pc in h.pieces:
                self.assertTrue(0 <= pc.main < len(paths.members))
                self.assertTrue(((np.asarray(pc.mem) >= 0) & (np.asarray(pc.mem) < len(paths.members))).all())

    def test_two_regions_find_the_same_interchange_with_the_same_artifacts(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for tmp, hk in ((a, "one"), (b, "two")):
                with unittest.mock.patch.object(cache, "ROOT", tmp):
                    prof_of(overpass(), lambda p, h, k: pipeline.region(p, h, k), hk)
            names = {}
            for tmp in (a, b):
                names[tmp] = {os.path.relpath(f, tmp) for f in glob.glob(os.path.join(tmp, "s[1-5]", "*", "*.npz"))}
            self.assertEqual(names[a], names[b], "steps 1-5 of an interchange do not depend on the region")
            self.assertTrue(names[a])


class FindInterchangesTest(unittest.TestCase):
    """find_systems is find_interchanges' systems; the seconds are the interchange's own."""

    def test_same_systems_and_seconds(self):
        feats = overpass()
        home = mapcells.cell_of(LAT, LON)
        planner = grading.PlanStore(FakeMD(feats), flat_elev).planner_of_cell(home)
        members = []
        with unittest.mock.patch.object(grading, "SHAPES_ON", True):
            for k in mapcells.cells_for_bbox(LAT - 0.01, LON - 0.01, LAT + 0.01, LON + 0.01):
                members += planner._shape_members(k)
        members = _gsh.one_road(members)
        net, systems = _gsh.find_systems(members)
        net2, found = _gsh.find_interchanges(members)
        self.assertTrue(found)
        self.assertEqual(len(systems), len(found))
        for s, x in zip(systems, found):
            self.assertEqual(s["seeds"], x.sysm["seeds"])
            self.assertTrue(x.paths_s >= 0.0 and x.extent_s > 0.0)
        net3, none = _gsh.find_interchanges(members, want=lambda n_, seeds: False)
        self.assertEqual(none, [], "an interchange not asked for is not closed")
        net4, some = _gsh.find_interchanges(members, want=lambda n_, seeds: True)
        for a, b in zip(found, some):
            self.assertEqual(a.sysm["seeds"], b.sysm["seeds"])


class ChecksPassTest(unittest.TestCase):
    """The independent checks read a real run's cache and find no fail: the artifacts the pipeline writes are what
    ixcheck reads (the levels' direction, the parents' chain, the keys)."""

    def test_the_fixtures_pass_every_check(self):
        from ixcheck import load, run
        for name, feats in (("overpass", overpass), ("underpass", underpass)):
            with tempfile.TemporaryDirectory() as tmp:
                with unittest.mock.patch.object(cache, "ROOT", tmp):
                    self.assertTrue(prof_of(feats(), lambda p, h, k: p._shape_region_calc(h, k), "chk"))
                scanned = load.scan(tmp)
                self.assertFalse(scanned["errors"], name)
                self.assertTrue(set((1, 2, 3, 4, 5, 6)) <= set(scanned["steps"]), name)
                fails = ["step %d %s: %s" % (f.step, f.code, f.text)
                         for _a, fs in run.check_all(scanned) for f in fs if f.level == "fail"]
                self.assertEqual(fails, [], name)


if __name__ == "__main__":
    unittest.main()
