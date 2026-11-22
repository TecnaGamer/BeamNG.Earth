"""The time limit on every test (bnge_timeouts.py): a hung test fails with every thread's stack and the run goes on, with and
without pytest-xdist; one the interrupt cannot reach is killed with its process and its worker replaced. The hanging tests are
timeout_probe.py's, run in a subprocess with short limits. (Under unittest, tools/run_tests.py, there is no pytest: skipped.)"""
import os
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path

try:
    import pytest
    import bnge_timeouts as bt
except ImportError:             # (no pytest: the suite's unittest runner)
    pytest = bt = None

try:
    import xdist  # noqa: F401
except ImportError:
    xdist = None

SERVER = Path(__file__).resolve().parent.parent
SOFT = "test_ok_before or test_hangs_in_a_sleep or test_swallows or test_a_marker or test_ok_after"


def run_probe(select, *extra, timeout_s="2", grace_s="3"):
    """(returncode, output, seconds) of pytest over timeout_probe.py's tests matching select."""
    env = dict(os.environ, BNGE_TEST_TIMEOUT=timeout_s, BNGE_TEST_TIMEOUT_GRACE=grace_s)
    t = time.time()
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/timeout_probe.py", "-k", select, "-p", "no:cacheprovider",
                        "-q", *extra], cwd=str(SERVER), env=env, capture_output=True, text=True, errors="replace",
                       timeout=300)
    return r.returncode, r.stdout + r.stderr, time.time() - t


def stub(nodeid, mark=None):
    return types.SimpleNamespace(nodeid=nodeid, path=Path(nodeid.split("::")[0]),
                                 get_closest_marker=lambda name: types.SimpleNamespace(args=(mark,)) if mark else None)


@unittest.skipUnless(pytest, "pytest is not installed")
class LimitTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        os.environ.pop("BNGE_TEST_TIMEOUT", None)

    def test_the_default_applies_where_nothing_is_said(self):
        self.assertEqual(bt.limit_for(stub("tests/test_x.py::T::t")), bt.DEFAULT_S)

    def test_the_environment_sets_the_default_and_zero_turns_the_limits_off(self):
        os.environ["BNGE_TEST_TIMEOUT"] = "7"
        self.assertEqual(bt.limit_for(stub("tests/test_x.py::T::t")), 7.0)
        os.environ["BNGE_TEST_TIMEOUT"] = "0"
        self.assertEqual(bt.limit_for(stub("tests/test_x.py::T::t", mark=900)), 0.0)

    def test_a_long_test_has_its_own_limit_the_longest_match_wins_and_none_lowers_the_default(self):
        old = dict(bt.LONG)
        self.addCleanup(lambda: (bt.LONG.clear(), bt.LONG.update(old)))
        bt.LONG.clear()
        bt.LONG.update({"test_x.py": 90, "test_x.py::T::t_slow": 250, "test_x.py::T::t_tiny": 1})
        self.assertEqual(bt.limit_for(stub("tests/test_x.py::T::t_fast")), 90)
        self.assertEqual(bt.limit_for(stub("tests/test_x.py::T::t_slow[param]")), 250)
        self.assertEqual(bt.limit_for(stub("tests/test_x.py::T::t_tiny")), bt.DEFAULT_S)
        self.assertEqual(bt.limit_for(stub("tests/test_y.py::T::t")), bt.DEFAULT_S)

    def test_a_marker_raises_the_limit_and_never_lowers_it(self):
        self.assertEqual(bt.limit_for(stub("tests/test_x.py::t", mark=200)), 200)
        self.assertEqual(bt.limit_for(stub("tests/test_x.py::t", mark=1)), bt.DEFAULT_S)

    def test_the_default_is_a_minute_and_no_test_may_take_more_than_five(self):
        self.assertEqual((bt.DEFAULT_S, bt.MAX_S), (60, 300))
        self.assertEqual(bt.limit_for(stub("tests/test_x.py::t", mark=1200)), bt.MAX_S)
        old = dict(bt.LONG)
        self.addCleanup(lambda: (bt.LONG.clear(), bt.LONG.update(old)))
        bt.LONG["test_x.py"] = 10 ** 5
        self.assertEqual(bt.limit_for(stub("tests/test_x.py::t")), bt.MAX_S)
        os.environ["BNGE_TEST_TIMEOUT"] = "100000"
        self.assertEqual(bt.limit_for(stub("tests/test_y.py::t")), bt.MAX_S)
        for secs in old.values():
            self.assertLessEqual(secs, bt.MAX_S)

    def test_each_case_of_a_parametrised_test_has_the_whole_limit_to_itself(self):
        old = dict(bt.LONG)
        self.addCleanup(lambda: (bt.LONG.clear(), bt.LONG.update(old)))
        bt.LONG["test_truth.py::test_earth_digs"] = 120
        for site in ("zurich_duttweilerbruecke", "goeschenen_gotthard_portal"):
            self.assertEqual(bt.limit_for(stub("tests/test_truth.py::test_earth_digs[%s]" % site)), 120)
        self.assertEqual(bt.limit_for(stub("tests/test_truth.py::test_other[a]")), bt.DEFAULT_S)

    def test_every_listed_long_test_is_a_test_of_the_suite(self):
        names = {p.name for p in (SERVER / "tests").glob("test_*.py")}
        for key in bt.LONG:
            self.assertIn(key.split("::")[0], names, key)


@unittest.skipUnless(pytest, "pytest is not installed")
class MechanismTest(unittest.TestCase):
    def check_soft(self, out, took):
        self.assertIn("FAILED tests/timeout_probe.py::test_hangs_in_a_sleep", out)
        self.assertIn("FAILED tests/timeout_probe.py::test_swallows_exceptions", out)      # (its except Exception did not hold it)
        self.assertIn("FAILED tests/timeout_probe.py::test_a_marker_never_lowers_the_limit", out)
        self.assertIn("exceeded its limit of 2 s", out)
        self.assertIn("most recent call first", out, "every thread's stack is in the report")
        self.assertIn("in test_hangs_in_a_sleep", out)
        self.assertNotIn("FAILED tests/timeout_probe.py::test_a_marker_raises_the_limit", out)
        self.assertRegex(out, r"3 failed, 3 passed")      # (the ones before and after it, and the marked one that is long enough)
        self.assertLess(took, 90, "the run moved on, it did not wait for the hung tests")

    def test_a_hung_test_fails_with_every_threads_stack_and_the_run_goes_on(self):
        rc, out, took = run_probe(SOFT)
        self.assertEqual(rc, 1, out)
        self.check_soft(out, took)

    @unittest.skipUnless(xdist, "pytest-xdist is not installed")
    def test_the_same_under_xdist(self):
        rc, out, took = run_probe(SOFT, "-n", "2")
        self.assertEqual(rc, 1, out)
        self.check_soft(out, took)
        self.assertNotIn("node down", out, "a hung test the interrupt reaches costs no worker")

    @unittest.skipUnless(xdist, "pytest-xdist is not installed")
    def test_a_test_the_interrupt_cannot_reach_is_killed_and_its_worker_replaced_under_xdist(self):
        rc, out, took = run_probe("test_ok_before or test_ignores_the_interrupt or test_ok_after", "-n", "2")
        self.assertEqual(rc, 1, out)
        self.assertIn("crashed while running 'tests/timeout_probe.py::test_ignores_the_interrupt'", out)
        self.assertIn("exceeded its limit of 2 s", out, "its stacks are in the crash report")
        self.assertIn("test_ignores_the_interrupt", out)
        self.assertRegex(out, r"1 failed, 2 passed")      # (the run went on to the tests after it)
        self.assertLess(took, 90)

    def test_without_xdist_a_test_the_interrupt_cannot_reach_ends_the_run_with_its_stacks(self):
        rc, out, took = run_probe("test_ignores_the_interrupt")
        self.assertNotEqual(rc, 0)
        self.assertIn("did not stop 3 s after its 2 s limit", out)
        self.assertIn("most recent call first", out)
        self.assertLess(took, 90)


if __name__ == "__main__":
    unittest.main()
