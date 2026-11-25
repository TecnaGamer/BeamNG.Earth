"""The exit with background threads in the native kernels (fast.py's atexit hook, the vendored PyO3's exit_guard,
earthfast 622): CPython before 3.14 ended every thread taking the GIL during finalization with pthread_exit, a forced
unwind through the kernel's Rust frames that crashed (SIGSEGV) or hung the process at exit (2026-11-24: test_deshadow
under the packaged app's Python 3.12, the de-shadow worker in its horizon march). Each case is a child process whose
daemon threads are in a kernel when the main thread returns: it must exit 0, promptly. Without the hook (trunk
5ef8805f, Python 3.12.13) both cases' children crashed at exit 6 times in 6 (SIGSEGV); 3.14 never did."""
import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
import fast  # noqa: E402

CHILD = textwrap.dedent("""
    import sys, threading, time
    sys.path.insert(0, {server!r})
    import numpy as np
    import fast
    case = {case!r}
    hp = np.random.default_rng(1).random((420, 420), dtype=np.float32) * 100
    started = threading.Barrier({threads} + 1)

    def nothing():
        pass

    def slow_callback():
        time.sleep(0.002)           # Python, asleep, on top of the kernel's Rust frames (waking during finalization)

    def worker():
        started.wait()
        cb = slow_callback if case == "callback" else nothing
        while True:                 # the horizon march: GIL released for each batch, pace called between them
            fast._ef.shadow_horizon(hp, 1.0, 200, 20, 37.0, 190.0, 1.0, 1, cb)

    for _ in range({threads}):
        threading.Thread(target=worker, daemon=True).start()
    started.wait()
    time.sleep(0.25)                # all of them busy in the kernel
    junk = [[i] for i in range(100000)]     # work for the final garbage collection
    print("exit", flush=True)
""")


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 622, "needs earthfast 622 (begin_exit)")
class ExitWithKernelThreadsTest(unittest.TestCase):

    def run_child(self, case, threads=4):
        code = CHILD.format(server=str(SERVER), case=case, threads=threads)
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, "-X", "faulthandler", "-c", code], capture_output=True, text=True,
                           timeout=60, env=dict(os.environ, PYTHONFAULTHANDLER="1"))
        return p, time.monotonic() - t0

    def test_threads_in_a_detached_kernel(self):
        """Threads coming back from the kernel's detached batches while the interpreter finalizes park."""
        for _ in range(5):
            p, dt = self.run_child("detached")
            self.assertEqual((p.returncode, p.stdout.strip()), (0, "exit"), p.stderr[-2000:])
            self.assertLess(dt, 20.0)

    def test_threads_in_python_called_back_from_a_kernel(self):
        """A thread asleep in Python that a kernel called (pacer.pace, a DEM fetch) is let finish before the
        interpreter finalizes, then parks at its next return to Python."""
        for _ in range(5):
            p, dt = self.run_child("callback")
            self.assertEqual((p.returncode, p.stdout.strip()), (0, "exit"), p.stderr[-2000:])
            self.assertLess(dt, 20.0)

    def test_counts_balance(self):
        """No thread left counted on top of PyO3 frames once every call returned: the exit hook's wait ends at
        once (the attach / detach bookkeeping of the patch balances)."""
        import numpy as np
        import threading
        hp = np.zeros((64, 64), np.float32)

        def calls():
            for _ in range(200):
                fast._ef.shadow_horizon(hp, 1.0, 20, 10, 10.0, 15.0, 1.0, 1, lambda: None)
        ts = [threading.Thread(target=calls) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(fast._ef.exit_guard_counts(), (1, 0, 0))     # (this call itself: inside, not away)


if __name__ == "__main__":
    unittest.main()
