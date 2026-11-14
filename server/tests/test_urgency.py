"""urgency.py: background jobs wait to start while urgent work runs (MAX_HOLD_S at most each), not otherwise."""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import urgency  # noqa: E402


class UrgencyTest(unittest.TestCase):
    def test_no_wait_when_idle(self):
        self.assertEqual(urgency.wait_turn(), 0.0)

    def test_waits_until_urgent_done(self):
        done = threading.Event()

        def work():
            with urgency.urgent():
                time.sleep(0.3)
            done.set()
        t = threading.Thread(target=work)
        t.start()
        time.sleep(0.05)
        w = urgency.wait_turn(2.0)
        self.assertTrue(done.is_set(), "started only after the urgent work")
        self.assertGreater(w, 0.15)
        t.join()

    def test_wait_capped(self):
        with urgency.urgent():
            t0 = time.perf_counter()
            urgency.wait_turn(0.2)
            self.assertLess(time.perf_counter() - t0, 0.5, "a background job never waits past its cap")


if __name__ == "__main__":
    unittest.main()
