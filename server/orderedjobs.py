"""Jobs handed to a pool one by one, each time the one wanted soonest now.

A pool's queue is first come, first served: a tile asked for while the car was 20 s from it stays ahead of one asked for
later that the car reaches in 3 s, and one the car has passed is still built. Here the jobs wait in this process and
each of the pool's workers takes the waiting job with the smallest rank at that moment (rank(key): a number, asked anew
at every handover, so the order follows the car as it moves), and a job whose rank is None by then (nobody wants it any
more) is dropped: its future's result is None. One job per worker at a time: a job's result is back as soon as it is
done, not with a batch's.
"""
import concurrent.futures
import threading
import time


class OrderedJobs:
    def __init__(self, pool, rank, name="ordered", workers=None):
        """pool: an executor (submit); rank(key) -> number (smallest first) or None (not wanted any more); workers: jobs
        in the pool at once (default: the pool's workers)."""
        self.pool, self.rank, self.name = pool, rank, name
        self.workers = max(1, int(workers or getattr(pool, "workers", 1)))
        self.waiting = []           # [(key, future, fn, args, t submitted)]
        self.cv = threading.Condition()
        self.threads = None
        self.stats = {"done": 0, "dropped": 0, "queue_s": 0.0, "work_s": 0.0}

    def submit(self, key, fn, *args):
        """A future of fn(*args)'s result, or of None when the job was dropped unwanted. Once handed to the pool it is
        running, its time in the queue in its .queued_s."""
        fut = concurrent.futures.Future()
        with self.cv:
            self.waiting.append((key, fut, fn, args, time.perf_counter()))
            if self.threads is None:
                self.threads = [threading.Thread(target=self._loop, daemon=True, name=f"{self.name}-{i}")
                                for i in range(self.workers)]
                for t in self.threads:
                    t.start()
            self.cv.notify()
        return fut

    def take(self):
        """The waiting job wanted soonest now (removed), the unwanted ones dropped; None when none is left. Under cv."""
        best = None
        keep = []
        for job in self.waiting:
            try:
                r = self.rank(job[0])
            except Exception:       # (a rank that cannot be told: built, after the ranked ones)
                r = float("inf")
            if r is None:
                if job[1].set_running_or_notify_cancel():
                    job[1].queued_s = time.perf_counter() - job[4]
                    job[1].set_result(None)
                self.stats["dropped"] += 1
                continue
            keep.append(job)
            if best is None or r < best[0]:
                best = (r, job)
        self.waiting = keep
        if best is None:
            return None
        self.waiting.remove(best[1])
        return best[1]

    def _loop(self):
        while True:
            with self.cv:
                job = self.take()
                while job is None:
                    self.cv.wait()
                    job = self.take()
            self.run(job)

    def run(self, job):
        _key, fut, fn, args, t0 = job
        if not fut.set_running_or_notify_cancel():
            return                  # (cancelled while it waited)
        t1 = time.perf_counter()
        fut.queued_s = t1 - t0
        try:
            res = self.pool.submit(fn, *args).result()
        except BaseException as e:
            fut.set_exception(e)
            return
        finally:
            self.stats["done"] += 1
            self.stats["queue_s"] += t1 - t0
            self.stats["work_s"] += time.perf_counter() - t1
        fut.set_result(res)
