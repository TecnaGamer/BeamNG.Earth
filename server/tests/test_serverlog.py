"""The server's real logging pipeline with a blocked sink, including process-exit paths."""
import asyncio
import io
import logging
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import sys
import threading
import time
import unittest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
import serverlog  # noqa: E402


class ServerLogTest(unittest.TestCase):
    def install(self, sink, capacity=8):
        root = logging.getLogger()
        original, level = root.handlers, root.level
        self.addCleanup(setattr, root, "handlers", original)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(serverlog.shutdown)
        root.handlers = [sink]
        root.setLevel(logging.DEBUG)
        serverlog.configure(logging.DEBUG, capacity)
        return logging.getLogger("earthd.test")

    def test_slow_writes_and_shutdown(self):
        stream = io.StringIO()
        started = threading.Event()
        flushed = []

        class Slow(logging.StreamHandler):
            def emit(self, record):
                started.set()
                time.sleep(3)
                super().emit(record)

            def flush(self):
                flushed.append(threading.get_ident())
                super().flush()

        log = self.install(Slow(stream))
        logging.getLogger().setLevel(logging.INFO)
        listener = logging.getLogger().handlers[0].listener._thread

        async def drive():
            loop = asyncio.get_running_loop()
            last = loop.time()
            log.info("first")
            peak = 0
            while not started.is_set():
                await asyncio.sleep(0.005)
                self.assertLess(loop.time() - last, 1.0, "listener failed to start")
            log.warning("second")
            deadline = last + 3.1
            while last < deadline:
                await asyncio.sleep(0.005)
                now = loop.time()
                peak = max(peak, now - last)
                last = now
            return peak

        peak = asyncio.run(drive())
        # The guarded failure is a 3 s write blocking the loop; 0.5 s still catches it. 50 ms measured the machine:
        # 63 ms on the mini PC lane with 600 tests beside it (gate 304).
        self.assertLess(peak, 0.5)
        serverlog.shutdown()
        self.assertEqual(stream.getvalue().splitlines(), ["first", "second"])
        self.assertFalse(listener.is_alive())
        self.assertIn(threading.get_ident(), flushed)  # explicit flush after joining
        print(f"\n  3 s per write: max event-loop interval {peak * 1000:.2f} ms")

    def test_full_queue_keeps_warnings_and_tracebacks(self):
        started, release = threading.Event(), threading.Event()
        lines, fingerprints = [], []

        class Blocked(logging.Handler):
            def emit(self, record):
                if record.msg == "first":
                    started.set()
                    release.wait(10)
                lines.append(self.format(record))

        class Errors(logging.Handler):
            def emit(self, record):
                fingerprints.append((record.exc_info[0], threading.get_ident()))

        log = self.install(Blocked(), capacity=3)
        self.addCleanup(release.set)
        serverlog.add_sink(Errors(logging.ERROR))
        log.info("first")
        self.assertTrue(started.wait(5))
        log.info("old info")
        log.debug("debug 1")
        log.debug("debug 2")
        log.debug("debug 3")
        log.info("new info")
        log.warning("warning")
        try:
            raise ValueError("bad input")
        except ValueError:
            log.exception("exception")
        self.assertEqual(logging.getLogger().handlers[0].queue.qsize(), 3)
        release.set()
        serverlog.shutdown()
        self.assertEqual(lines[:1] + lines[2:4], ["first", "new info", "warning"])
        self.assertIn("dropped 3 DEBUG and 1 INFO", lines[1])
        self.assertIn("ValueError: bad input", lines[4])
        self.assertEqual(len(fingerprints), 1)
        self.assertIs(fingerprints[0][0], ValueError)
        self.assertNotEqual(fingerprints[0][1], threading.get_ident())

    def test_shutdown_with_a_warning_waiting_for_space(self):
        started, release = threading.Event(), threading.Event()
        lines = []

        class Blocked(logging.Handler):
            def emit(self, record):
                if record.msg == "one":
                    started.set()
                    release.wait(5)
                lines.append(record.getMessage())

        log = self.install(Blocked(), capacity=1)
        self.addCleanup(release.set)
        log.warning("one")
        self.assertTrue(started.wait(2))
        log.warning("two")
        writer = threading.Thread(target=log.warning, args=("three",))
        writer.start()
        records = logging.getLogger().handlers[0].queue
        deadline = time.monotonic() + 2
        while not records.waiters and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertEqual(records.waiters, 1)
        t0 = time.monotonic()
        log.info("discardable")
        self.assertLess(time.monotonic() - t0, .05)
        threading.Timer(.05, release.set).start()
        serverlog.shutdown()
        writer.join(2)
        self.assertFalse(writer.is_alive())
        self.assertEqual([line for line in lines if "dropped" not in line], ["one", "two", "three"])
        self.assertIn("log queue full: dropped 0 DEBUG and 1 INFO records", lines)

    def test_fatal_main_flushes_before_unwind(self):
        script = """
import earthd, logging, sys, time
class Slow(logging.StreamHandler):
    def emit(self, record):
        time.sleep(.2)
        super().emit(record)
logging.getLogger().addHandler(Slow())
def crash(*args):
    logging.getLogger("earthd").warning("warning before failure")
    raise RuntimeError("fatal in earthd")
earthd.run = crash
try:
    earthd.main()
finally:
    print("main unwound", file=sys.stderr, flush=True)
"""
        proc = subprocess.run([sys.executable, "-c", script, "--offline"], cwd=SERVER,
                              capture_output=True, text=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("warning before failure\nmain unwound\nTraceback", proc.stderr)
        self.assertIn("RuntimeError: fatal in earthd", proc.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX launcher stop")
    def test_sigterm_drains_pending_warning(self):
        script = """
import logging, os, signal, time
import serverlog
class Slow(logging.StreamHandler):
    def emit(self, record):
        time.sleep(.2)
        super().emit(record)
logging.getLogger().addHandler(Slow())
serverlog.configure(logging.WARNING)
logging.warning("queued at SIGTERM")
os.kill(os.getpid(), signal.SIGTERM)
time.sleep(10)
"""
        proc = subprocess.run([sys.executable, "-c", script], cwd=SERVER, capture_output=True,
                              text=True, timeout=5)
        self.assertEqual(proc.returncode, -signal.SIGTERM)
        self.assertIn("queued at SIGTERM", proc.stderr)

    def test_worker_exit_drains_warning(self):
        script = """
import blockworker, logging, sys, threading, time
class Slow(logging.StreamHandler):
    def emit(self, record):
        time.sleep(.2)
        super().emit(record)
        if record.msg == "last worker warning" and threading.current_thread() is threading.main_thread():
            print("synchronous worker write", file=sys.stderr)
logging.getLogger().addHandler(Slow())
blockworker.init(sys.argv[1], True, False, False, False, False, None)
logging.getLogger("earthd.worker").warning("last worker warning")
if sys.argv[2] == "orphan":
    blockworker._watch_parent(-1)
"""
        with tempfile.TemporaryDirectory() as cache:
            for mode in ("normal", "orphan"):
                with self.subTest(mode=mode):
                    proc = subprocess.run([sys.executable, "-c", script, cache, mode], cwd=SERVER,
                                          capture_output=True, text=True, timeout=30)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("last worker warning", proc.stderr)
                    self.assertNotIn("synchronous worker write", proc.stderr)

    def test_restart_and_hard_restart_guard_drain_warnings(self):
        import srvrestart
        script = """
import logging, sys, time
import serverlog, srvrestart
class Slow(logging.StreamHandler):
    def emit(self, record):
        time.sleep(.2)
        super().emit(record)
logging.getLogger().addHandler(Slow())
serverlog.configure(logging.WARNING)
logging.warning("queued before restart")
if sys.argv[1] == "guard":
    srvrestart.finish_soon(hard_s=.01)
    time.sleep(10)
else:
    srvrestart.finish(logging.getLogger())
"""
        for mode in ("clean", "guard"):
            with self.subTest(mode=mode):
                proc = subprocess.run([sys.executable, "-c", script, mode], cwd=SERVER,
                                      capture_output=True, text=True, timeout=5)
                self.assertEqual(proc.returncode, srvrestart.EXIT_CODE, proc.stderr)
                self.assertIn("queued before restart", proc.stderr)
