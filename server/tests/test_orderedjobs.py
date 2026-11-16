"""orderedjobs.OrderedJobs: the waiting job wanted soonest now goes next; the unwanted are dropped."""
import concurrent.futures
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import orderedjobs  # noqa: E402


class Inline:
    workers = 1

    def submit(self, fn, *args):
        f = concurrent.futures.Future()
        try:
            f.set_result(fn(*args))
        except Exception as e:
            f.set_exception(e)
        return f


def jobs(rank):
    j = orderedjobs.OrderedJobs(Inline(), rank)
    j.threads = []          # (no threads: the test hands over itself)
    return j


class OrderedJobsTest(unittest.TestCase):
    def test_smallest_rank_now_goes_first(self):
        rank = {"a": 3, "b": 1, "c": 2}
        j = jobs(rank.get)
        futs = {k: j.submit(k, str.upper, k) for k in "abc"}
        order = []
        rank["a"] = 0           # (the car turned: a is next now)
        while True:
            job = j.take()
            if job is None:
                break
            order.append(job[0])
            j.run(job)
        self.assertEqual(order, ["a", "b", "c"])
        self.assertEqual(futs["c"].result(), "C")
        self.assertGreaterEqual(futs["c"].queued_s, 0.0)

    def test_unwanted_job_is_dropped_with_none(self):
        rank = {"a": 1}
        j = jobs(rank.get)
        fa, fb = j.submit("a", str.upper, "a"), j.submit("b", str.upper, "b")
        job = j.take()
        self.assertEqual(job[0], "a")
        self.assertIsNone(fb.result(timeout=0))
        self.assertEqual(j.stats["dropped"], 1)
        j.run(job)
        self.assertEqual(fa.result(), "A")
        self.assertIsNone(j.take())

    def test_job_error_reaches_its_future(self):
        j = jobs(lambda k: 0)
        f = j.submit("a", int, "x")
        j.run(j.take())
        self.assertIsInstance(f.exception(), ValueError)

    def test_threads_run_the_jobs(self):
        j = orderedjobs.OrderedJobs(Inline(), lambda k: k, workers=2)
        futs = [j.submit(i, lambda v: v * 2, i) for i in range(20)]
        self.assertEqual([f.result(timeout=5) for f in futs], [i * 2 for i in range(20)])
        self.assertEqual(len(j.threads), 2)
        self.assertTrue(all(isinstance(t, threading.Thread) for t in j.threads))


if __name__ == "__main__":
    unittest.main()
