"""The roads round reported places (tests/drivability.py) are never less drivable than the baseline, and an improvement
is written to the baseline (python server/tests/drivability.py --update) so that it holds from then on."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import drivability as dv  # noqa: E402

STEPS_TOL = 2           # metres of wheel track over the limit (a sample beside the limit on another platform)
UPDATE = "python server/tests/drivability.py --update {}"


def compare(result, base):
    """([worse], [better]) of a measurement against its baseline entry, each a line for whoever reads the failure."""
    worse, better = [], []
    if result["steps"] > base["steps"] + STEPS_TOL:
        worse.append(f"steps {base['steps']} -> {result['steps']}")
    elif result["steps"] < base["steps"] - STEPS_TOL:
        better.append(f"steps {base['steps']} -> {result['steps']}")
    for key in sorted(set(result["parts"]) | set(base["parts"])):
        now, was = result["parts"].get(key), base["parts"].get(key)
        for m, (limit, tol, *_rest) in dv.METRICS.items():
            was_m, now_m = (was or {}).get(m, 0.0), (now or {}).get(m, 0.0)
            if now_m > max(was_m, limit) + tol:
                worse.append(f"{key} ({now['class']}, at {now.get(m + '_at')}): {m} {was_m if was else 'ok'} -> {now_m}")
            elif was_m > limit and now_m < was_m - tol:
                better.append(f"{key}: {m} {was_m} -> {now_m if now_m > limit else 'ok'}")
    return worse, better


class DrivabilityTest(unittest.TestCase):
    def test_no_site_gets_worse(self):
        baseline = dv.load_baseline()
        for name in dv.SITES:
            with self.subTest(name):
                self.assertIn(name, baseline, f"{name} has no baseline: {UPDATE.format(name)}")
                worse, better = compare(dv.measure(name), baseline[name])
                self.assertFalse(worse, f"{name} got worse ({dv.SITES[name][1]}):\n  " + "\n  ".join(worse))
                self.assertFalse(better, f"{name} improved: lock it in with {UPDATE.format(name)}\n  "
                                 + "\n  ".join(better))


class CompareTest(unittest.TestCase):
    base = {"steps": 10, "parts": {"ground:a": {"step_m": 1.0, "grade": 0.0}}}

    def result(self, steps=10, **parts):
        return {"steps": steps, "parts": {k: {"class": "primary", "step_m_at": [0, 0], **v} for k, v in parts.items()}}

    def test_same_passes(self):
        self.assertEqual(compare(self.result(**{"ground:a": {"step_m": 1.01}}), self.base), ([], []))

    def test_a_worse_part_and_a_new_bad_part_fail(self):
        worse, _ = compare(self.result(**{"ground:a": {"step_m": 1.2}, "bridge:b": {"step_m": 0.5}}), self.base)
        self.assertEqual(len(worse), 2)

    def test_a_fixed_part_must_be_locked_in(self):
        worse, better = compare(self.result(steps=0), self.base)
        self.assertEqual(worse, [])
        self.assertEqual(len(better), 2)


if __name__ == "__main__":
    unittest.main()
