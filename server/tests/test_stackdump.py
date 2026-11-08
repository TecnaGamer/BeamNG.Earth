"""stackdump: every thread's stack in ONE log record (several workers dumping at once interleaved unreadably)."""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import stackdump  # noqa: E402


class StackDumpTest(unittest.TestCase):
    def test_one_record_with_every_thread(self):
        ev = threading.Event()

        def parked_somewhere():
            ev.wait(5)
        t = threading.Thread(target=parked_somewhere, name="parked")
        t.start()
        try:
            with self.assertLogs("earthd.stackdump", "WARNING") as cm:
                stackdump.dump("cell (14, 1, 2)'s plans still being made")
        finally:
            ev.set()
            t.join(5)
        self.assertEqual(len(cm.records), 1)
        text = cm.output[0]
        self.assertIn("cell (14, 1, 2)", text)
        self.assertIn("parked_somewhere", text)
        self.assertIn("test_one_record_with_every_thread", text)

    def test_watch_cancelled_in_time_says_nothing(self):
        w = stackdump.watch(0.3, "never")
        w.cancel()
        time.sleep(0.4)
        with self.assertRaises(AssertionError):
            with self.assertLogs("earthd.stackdump", "WARNING"):
                pass


if __name__ == "__main__":
    unittest.main()
