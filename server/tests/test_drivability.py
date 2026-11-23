"""The roads round reported places (tests/drivability.py) are never less drivable than the baseline, and an improvement
is written to the baseline (python server/tests/drivability.py --update) so that it holds from then on."""
import sys
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import drivability as dv  # noqa: E402

STEPS_TOL = 2           # metres of wheel track over the limit (a sample beside the limit on another platform)
REL_TOL = 0.05          # of a value already over its limit, when more than the absolute tolerance: a crest of 22 g
                        # becoming 22.1 g is a road already broken shifting a little, not a new defect
UPDATE = "python server/tests/drivability.py --update {}"

# The baseline was 20 merged PRs behind the code at 2026-11-18 (402b4d85d, PR 19, was its last). Re-measured on the head and bisected
# per site, the sites that were worse were not all new defects (a site's score, 402b4d85d's baseline -> the head's):
#   a20_921 10.7 -> 18.3: the measurement. The native gradebins' under_deck rule (53edbea74) grades the ground past an abutment's
#       end under the deck at the abutment's slope; the harness walks that ground as a road under its own deck, 13 m of it.
#   perth_915 44.9 -> 63.0: PR 19 (find-ground), but not the track's ramp, which is at railways.GRADE with no step
#       (test_findground RiseRampTest). Measured chain (2026-11-18): two rail bridges and two motorway decks cross one street
#       (Morley Drive) side by side, the rails 4.8 m from the southbound deck's axis. The street under the decks is dug 6 m by
#       the road decks' own dip (grading._lower_underpasses, still the older law for a road deck: the road goes down) while the
#       rail bridges, by find-ground, leave the street where the elevation has it and rise 5.25 m over THAT ground (railways
#       are the elevation's alone: their beds are pure in it, railprofile.py), so the rail stands 11.7 m over the dug street
#       and its 5 % approaches are 5 m embankments whose toes lie under both ends of the motorway deck: the ground there
#       27.5-28.1 over the deck's 26.8-27.2, and the deck, which stays DECK_CLEAR over the ground under it with its
#       abutments pinned, rises 1.2 m in its first 3.4 m (a 35 % grade) and drops 0.8 m at its end. Without the rise
#       (plain_underpasses -> []) 45.6, steps 136 -> 40; RISE_PIECE_M 25 -> 60 changes nothing (62.9). BNE_SHAPES=1, where no
#       road is dug and the decks rise instead (the one rule), 44.8 with the rise and 49.2 without it. Making the legacy dips
#       follow find-ground for road decks too (findground.leaves in _lower_underpasses, every span) measured 63.0 -> 30.6 here
#       and 1126.7 -> 1076.0 over the 29 sites (castlegar 21.8 -> 5.7, prague 34.1 -> 29.9, m1_ireland 71.0 -> 68.6;
#       mooslacken 11.2 -> 16.3 the one worse): that is the I-20 question (a motorway over a local road: dug or raised), not
#       the rail's. A defect, not fixed (the baseline holds it).
#   casagranda_portal 35.0 -> 55.3, casagranda_bridge 37.4 -> 51.1: PR 34 (tunnel mouths: a tunnel's portal where a bridge
#       goes on read the ground line outward from the mouth, the gorge's floor 14 m under the deck: the tunnels and the
#       bridge between them lowered 15 m, 31 % grade into the portal). Fixed: where a bridge goes on the portal is the
#       structure's own height (roads.mouth_over_deck), 30.9 and 26.7, under both the old numbers (the bridge between the
#       tunnels no longer climbs 5 m in its 31 m).
#   riva_pontera_tunnel 20.6 -> 24.2 (the centre line of d4d83607 rises 1.8 m), boston_storrow_massave 26.1 -> 31.3,
#       boston_bowker 81.6 -> 84.3, paris_maillot_ax 120.0 -> 124.5, tamar_bridge 20.7 -> 21.4: PR 33 (retaining walls: the wall
#       line between two roads at other levels, which the harness reads as a step across it). The Riva one is a probable
#       defect; not fixed (the baseline holds them).
#   vienna_mooslacken 8.0 -> 11.2 (steps 75 -> 34): PR 39 (rail grade 5 %, the rail beds' ramps), fewer longer steps.
# Fix the defect, then lock the improvement in with UPDATE.


def compare(result, base):
    """([worse], [better]) of a measurement against its baseline entry, each a line for whoever reads the failure."""
    worse, better = [], []
    steps_tol = _tolerance(base["steps"], 0, STEPS_TOL)
    if result["steps"] > base["steps"] + steps_tol:
        worse.append(f"steps {base['steps']} -> {result['steps']}")
    elif result["steps"] < base["steps"] - steps_tol:
        better.append(f"steps {base['steps']} -> {result['steps']}")
    for key in sorted(set(result["parts"]) | set(base["parts"])):
        now, was = result["parts"].get(key), base["parts"].get(key)
        for m, (limit, abs_tol, *_rest) in dv.METRICS.items():
            was_m, now_m = (was or {}).get(m, 0.0), (now or {}).get(m, 0.0)
            tol = _tolerance(was_m, limit, abs_tol)
            if now_m > max(was_m, limit) + tol:
                worse.append(f"{key} ({now['class']}, at {now.get(m + '_at')}): {m} {was_m if was else 'ok'} -> {now_m}")
            elif was_m > limit and now_m < was_m - tol:
                better.append(f"{key}: {m} {was_m} -> {now_m if now_m > limit else 'ok'}")
    return worse, better


def _tolerance(was, limit, abs_tol):
    """How far a value may move before it counts: abs_tol, or REL_TOL of a value already over its limit."""
    return max(abs_tol, REL_TOL * was) if was > limit else abs_tol


class DrivabilityTest(unittest.TestCase):
    """On every platform: Windows built other roads at 2 of the sites until the maths was made deterministic (GitHub
    #1169: dm.rs / dnp.py / dmath.py, tests/test_determinism.py); the 27 sites now measure the same on Linux (glibc 2.35
    and 2.44) and Windows."""

    def test_no_site_gets_worse(self):
        baseline = dv.load_baseline()
        for name, result in zip(dv.SITES, dv.measure_all(dv.SITES), strict=True):
            with self.subTest(name):
                self.assertIn(name, baseline, f"{name} has no baseline: {UPDATE.format(name)}")
                worse, better = compare(result, baseline[name])
                scores = f"score {dv.score(baseline[name]):.1f} -> {dv.score(result):.1f}"
                self.assertFalse(worse, f"{name} got worse ({dv.SITES[name][1]}), {scores}:\n  " + "\n  ".join(worse))
                self.assertFalse(better, f"{name} improved, {scores}: lock it in with {UPDATE.format(name)}\n  "
                                 + "\n  ".join(better))


class IndependenceTest(unittest.TestCase):
    """The roads must not depend on how many cells are planned at once or on where a session started: in the game the
    planner runs PLAN_THREADS cells at once, and the player arrives from anywhere.

    Cells planned at the same time once shared state: on main (1.9.1-beta) Silistra's bridge got a 1.9 m step in 25 %
    of the runs with 4 threads and 42 % with all 11 of its cells at once, never with one thread and in any order one
    at a time. A race shows only in some runs, so Silistra plans all its cells at once RACE_RUNS times: a race that
    common passes all of them in about 1 % of the test runs."""
    SITES = ("silistra_936", "vienna_s1_breitenleer_1083", "perth_915", "compiegne_roundabout")
    VARIANTS = ({"threads": 16}, {"approach": (0.0, 600.0)}, {"approach": (600.0, 0.0)})
    RACE_SITE, RACE_RUNS = "silistra_936", 8

    def test_threads_and_approach_change_nothing(self):
        cases = [(name, v) for name in self.SITES for v in self.VARIANTS]
        cases += [(self.RACE_SITE, {"threads": 16})] * (self.RACE_RUNS - 1)
        with ProcessPoolExecutor() as pool:
            pinned = {name: pool.submit(dv.measure, name) for name in self.SITES}
            runs = [(name, v, pool.submit(dv.measure, name, **v)) for name, v in cases]
            for name, variant, run in runs:
                with self.subTest(name, **{k: str(x) for k, x in variant.items()}):
                    self.assertEqual(run.result(), pinned[name].result())


class ScoreTest(unittest.TestCase):
    def test_a_point_per_doubling_over_the_limit(self):
        entry = {"parts": {"ground:a": {"step_m": 0.4, "grade": 0.15}, "bridge:b": {"lift_g": 0.4, "step_m": 0.05}}}
        self.assertAlmostEqual(dv.score(entry), 3.0)

    def test_drivable_is_zero(self):
        self.assertEqual(dv.score({"parts": {}}), 0)


class TrackTest(unittest.TestCase):
    def test_empty_invalid_and_subthreshold_tracks(self):
        xy = np.column_stack((np.arange(4), np.zeros(4)))
        inside = np.ones(4, dtype=bool)
        where = {"surface": "road", "part": "r", "class": "residential"}
        def sample(z):
            report = dv.Report()
            report.add(where, xy, np.asarray(z), inside, centre=True)
            return report.result()
        with self.assertRaises(ValueError):
            dv.Report().result()
        with self.assertRaises(ValueError):
            sample([0., 0., np.nan, 0.])
        a, b = sample([0., 0., 0., 0.]), sample([0., 1e-12, 0., 0.])
        self.assertEqual(a["parts"], b["parts"])
        self.assertNotEqual(a["tracks"], b["tracks"])


class CompareTest(unittest.TestCase):
    base = {"steps": 10, "parts": {"ground:a": {"step_m": 1.0, "grade": 0.0}}}

    def result(self, steps=10, **parts):
        return {"steps": steps, "parts": {k: {"class": "primary", "step_m_at": [0, 0], **v} for k, v in parts.items()}}

    def test_same_passes(self):
        self.assertEqual(compare(self.result(**{"ground:a": {"step_m": 1.01}}), self.base), ([], []))

    def test_a_worse_part_and_a_new_bad_part_fail(self):
        worse, _ = compare(self.result(**{"ground:a": {"step_m": 1.2}, "bridge:b": {"step_m": 0.5}}), self.base)
        self.assertEqual(len(worse), 2)

    def test_a_bad_part_may_shift_within_the_relative_tolerance(self):
        self.assertEqual(compare(self.result(steps=10, **{"ground:a": {"step_m": 1.04}}), self.base), ([], []))
        self.assertEqual(len(compare(self.result(**{"ground:a": {"step_m": 1.06}}), self.base)[0]), 1)
        self.assertEqual(len(compare(self.result(**{"ground:a": {"step_m": 0.94}}), self.base)[1]), 1)

    def test_a_new_bad_part_gets_only_the_absolute_tolerance(self):
        worse, _ = compare(self.result(**{"ground:a": {"step_m": 1.0}, "bridge:b": {"step_m": 0.13}}), self.base)
        self.assertEqual(len(worse), 1)

    def test_a_fixed_part_must_be_locked_in(self):
        worse, better = compare(self.result(steps=0), self.base)
        self.assertEqual(worse, [])
        self.assertEqual(len(better), 2)


if __name__ == "__main__":
    unittest.main()
