"""The roads round reported places (tests/drivability.py) are never less drivable than the baseline, and an improvement
is written to the baseline (python server/tests/drivability.py --update) so that it holds from then on."""
import sys
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import drivability as dv  # noqa: E402

STEPS_TOL = 2           # metres of wheel track over the limit (a sample beside the limit on another platform)
REL_TOL = 0.05          # of a value already over its limit, when more than the absolute tolerance: a crest of 22 g
                        # becoming 22.1 g is a road already broken shifting a little, not a new defect
UPDATE = "python server/tests/drivability.py --update {}"


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


@unittest.skipUnless(sys.platform.startswith("linux"), "the baseline is Linux's: Windows builds other roads at 2 of the "
                     "sites (Südtiroler Platz's crest 0.78 g instead of 0.26 g, a step at Kennedybrücke 1.41 m instead "
                     "of 0.96 m), its maths library, not the test")
class DrivabilityTest(unittest.TestCase):
    def test_no_site_gets_worse(self):
        baseline = dv.load_baseline()
        for name, result in zip(dv.SITES, dv.measure_all(dv.SITES)):
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
                    worse, better = compare(run.result(), dv.baseline_entry(pinned[name].result()))
                    self.assertFalse(worse + better, f"{name} with {variant}:\n  " + "\n  ".join(worse + better))


class ScoreTest(unittest.TestCase):
    def test_a_point_per_doubling_over_the_limit(self):
        entry = {"parts": {"ground:a": {"step_m": 0.4, "grade": 0.15}, "bridge:b": {"lift_g": 0.4, "step_m": 0.05}}}
        self.assertAlmostEqual(dv.score(entry), 3.0)

    def test_drivable_is_zero(self):
        self.assertEqual(dv.score({"parts": {}}), 0)


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
