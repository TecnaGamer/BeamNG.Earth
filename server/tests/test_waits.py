"""waits: what a job's thread waited for, by name (the slow road geotile's log line)."""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import waits  # noqa: E402


class WaitsTest(unittest.TestCase):
    def test_named_waits_are_summed_for_the_collecting_job(self):
        with waits.collect() as w:
            with waits.waiting("map data"):
                time.sleep(0.03)
            with waits.waiting("map data"):
                time.sleep(0.03)
            with waits.waiting("town signs"):
                time.sleep(0.01)
        self.assertGreaterEqual(w["map data"], 0.06)
        self.assertGreaterEqual(w["town signs"], 0.01)
        self.assertLess(w["town signs"], w["map data"])

    def test_nothing_without_a_job_and_an_inner_wait_counts_once(self):
        with waits.waiting("map data"):      # (no job collects: nothing kept, no error)
            pass
        with waits.collect() as w:
            with waits.waiting("town signs"):
                with waits.waiting("map data"):      # (inside the town signs' wait: theirs)
                    time.sleep(0.02)
        self.assertEqual(list(w), ["town signs"])

    def test_another_threads_waits_are_its_own(self):
        with waits.collect() as w:
            def other():
                with waits.waiting("elevation"):
                    time.sleep(0.01)
            t = threading.Thread(target=other)
            t.start()
            t.join()
        self.assertEqual(w, {})

    def test_nested_jobs_add_up_and_an_error_still_counts(self):
        with waits.collect() as outer:
            with waits.collect() as inner:
                with self.assertRaises(ValueError):
                    with waits.waiting("road plans"):
                        time.sleep(0.01)
                        raise ValueError
            self.assertIn("road plans", inner)
        self.assertGreaterEqual(outer["road plans"], 0.01)

    def test_text(self):
        self.assertEqual(waits.text({"map data": 2.04, "town signs": 9.12, "elevation": 0.01}),
                         "town signs 9.1 map data 2.0")
        self.assertEqual(waits.text({}), "")
        self.assertEqual(waits.text(None), "")


if __name__ == "__main__":
    unittest.main()
