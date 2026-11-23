"""A time limit on every test (pytest), so a hung test fails and the run moves on.

A test that hangs (a lock nobody releases, a server that never answers, a loop that never ends) used to hold its
pytest-xdist worker for hours: one test of a full run that passes alone in seconds, and every shell loop waiting on that
run's log waited with it. Now each test (setup, call and teardown together) has a limit in seconds:

  the default            DEFAULT_S (60 s); BNGE_TEST_TIMEOUT sets another (0: no limits, for a debugger)
  a longer one           an entry of LONG below, or @pytest.mark.max_seconds(N) on the test (a test file must not import
                         pytest: tools/run_tests.py runs them with unittest too, hence the table); per test, so each case
                         of a parametrised test has its own
  no test runs longer than MAX_S (300 s), whatever the table, a marker or the environment says: a test that needs more is
  too slow, to be split or sped up, and fails (the list is in the PR that put the limits in)

When a test passes its limit:
  1. every thread's stack is written (faulthandler), and the main thread is interrupted (SIGINT, which wakes a sleep,
     a lock or a socket wait too): the test fails with "exceeded N s" and those stacks in its report, its teardown
     runs, and the run goes on with the next test;
  2. when the main thread does not come out within GRACE_S (blocked where an interrupt does not reach, or a teardown
     that hangs as well) the stacks go to the console and the process and its children are killed. Under xdist the
     test is reported as failed ("worker crashed while running") and the worker is replaced; without xdist the whole
     run ends, and the stacks say where.

pytest-timeout is not needed (it only exits the process on Windows, where there is no SIGALRM: the run would not
move on). Its markers and options are not used here, so installing it changes nothing."""
import faulthandler
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

import pytest

DEFAULT_S = 60           # a limit is there to make slowness fail loudly, not to fit today's worst case
MAX_S = 300              # the most any test may take
GRACE_S = 60             # after the interrupt: the test's teardown, or a hang the interrupt cannot reach

# (test file name, then "::" and what follows in the node id, or the whole file) -> seconds, at most MAX_S; the longest match
# wins; it is per test, so each case of a parametrised test (test_truth, test_drivability) has the whole limit to itself.
# From the measured durations (a run of the slowest files, 2026-11-22, -n 6, 300 s cap), the tests that passed their 60 s
# only by being given more:
LONG = {
    "test_integration_drive.py::FollowDrive512Test": 120,    # 42, 28 and 21 s measured
    "test_roadgeo.py::RealDataTest": MAX_S,                  # 226 and 198 s measured
    "test_fargeo.py::RealDataTest": MAX_S,                   # 197 and 179 s measured
    "test_otto_bridge.py": MAX_S,                             # 212 s in the class's setup (the first test's)
    # TOO SLOW, TO BE SPLIT OR SPED UP: they ran into the 300 s cap and fail on time, as they should
    "test_integration_drive.py::MeshDriveTest": MAX_S,
    "test_integration_drive.py::MeshDrive64Test": MAX_S,
    "test_integration_drive.py::MeshDrive64Block2048Test": MAX_S,
    "test_integration_drive.py::TerrainDriveTest": MAX_S,
}


class TestTimeout(BaseException):
    """BaseException: a test's `except Exception` must not swallow it (unittest records it as the test's error)."""
    __test__ = False


def _seconds(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def limit_for(item):
    """Seconds this test may take; 0 for no limit."""
    base = _seconds("BNGE_TEST_TIMEOUT", DEFAULT_S)
    if base <= 0:
        return 0.0
    base = min(base, MAX_S)
    mark = item.get_closest_marker("max_seconds")
    if mark is not None:
        return min(MAX_S, max(base, float(mark.args[0])))
    key = item.path.name + item.nodeid[item.nodeid.find("::"):] if "::" in item.nodeid else item.path.name
    best, best_len = None, -1
    for prefix, secs in LONG.items():
        if key.startswith(prefix) and len(prefix) > best_len:
            best, best_len = secs, len(prefix)
    return min(MAX_S, max(base, float(best))) if best is not None else base


def _interrupt_main():
    """SIGINT to the main thread so that what it waits in wakes: on Windows raise() runs the C handler that also sets the
    event a sleep or a lock wait watches (_thread.interrupt_main does not), elsewhere the signal must be sent to that
    thread for its system call to return EINTR."""
    if os.name == "nt":
        signal.raise_signal(signal.SIGINT)
    else:
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)


class _Watch:
    """One test's clock. The timer thread does the work; the main thread's SIGINT handler raises."""

    def __init__(self, item, secs):
        self.item, self.secs = item, secs
        self.lock = threading.Lock()
        self.armed = True
        self.fired = False
        self.stacks = ""
        self.t0 = time.monotonic()
        self.timer = threading.Timer(secs, self.fire)
        self.timer.daemon = True
        self.grace = None

    def start(self):
        self.timer.start()

    def stop(self):
        with self.lock:
            self.armed = False
        self.timer.cancel()
        if self.grace is not None:
            self.grace.cancel()

    def fire(self):
        with self.lock:
            if not self.armed:
                return
            self.fired = True
            with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as f:
                faulthandler.dump_traceback(file=f, all_threads=True)
                f.seek(0)
                self.stacks = f.read()
            self.grace = threading.Timer(_seconds("BNGE_TEST_TIMEOUT_GRACE", GRACE_S), self.give_up)
            self.grace.daemon = True
            self.grace.start()
            _PENDING.append(self)          # (under the lock: stop() cannot disarm between the two)
            _interrupt_main()

    def message(self):
        return (f"{self.item.nodeid} exceeded its limit of {self.secs:g} s (BNGE_TEST_TIMEOUT, the LONG table or "
                f"@pytest.mark.max_seconds in server/tests/bnge_timeouts.py).\nEvery thread's stack when it ran out:\n"
                f"{self.stacks}")

    def give_up(self):
        """The main thread did not come out: the stacks to the console, then the process and its children go."""
        with self.lock:
            if not self.armed:
                return
        capman = self.item.config.pluginmanager.getplugin("capturemanager")
        try:
            if capman is not None:
                capman.suspend_global_capture(in_=False)
        except Exception:
            pass
        path = _stacks_file(self.item.nodeid)
        try:
            with open(path, "w", encoding="utf-8", errors="replace") as f:
                f.write(self.message())
        except OSError:
            pass
        err = sys.__stderr__
        try:
            err.write(f"\n[bnge timeout] {self.item.nodeid} did not stop {_seconds('BNGE_TEST_TIMEOUT_GRACE', GRACE_S):g}"
                      f" s after its {self.secs:g} s limit: killing this process and its children\n{self.stacks}\n")
            err.flush()
        except Exception:
            pass
        kill_own_tree()


def _stacks_file(nodeid):
    """Where a killed worker leaves its stacks for the controller (xdist shows nothing a worker writes at its end)."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid)[-150:]
    return os.path.join(tempfile.gettempdir(), f"bnge-test-timeout-{name}.log")


def pytest_runtest_logreport(report):
    """The controller (xdist): the crash report of a killed worker gets the stacks it left."""
    if not report.failed or "crashed while running" not in str(report.longrepr):
        return
    path = _stacks_file(report.nodeid)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        os.remove(path)
    except OSError:
        return
    report.sections.append(("bnge timeout", text))


def kill_own_tree():
    """This process and what it started (servers, pool workers: on Windows they outlive a killed parent)."""
    pid = os.getpid()
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30)
        else:
            subprocess.run(["pkill", "-KILL", "-P", str(pid)], capture_output=True, timeout=30)
    except Exception:
        pass
    os._exit(70)


_CURRENT = []        # the running test's _Watch (one at a time: the main thread runs the tests)
_PENDING = []        # interrupts sent and not yet met by the handler


def _on_sigint(signum, frame):
    """Our interrupt: raised in the test if it is still running, dropped when it ended meanwhile. Anything else is
    the user's Ctrl-C."""
    __tracebackhide__ = True
    if not _PENDING:
        return signal.default_int_handler(signum, frame)
    watch = _PENDING.pop()
    if watch.armed and _CURRENT and _CURRENT[-1] is watch:
        raise TestTimeout(watch.message())


def pytest_configure(config):
    config.addinivalue_line("markers", "max_seconds(n): this test may run n seconds, not the default limit "
                                       "(server/tests/bnge_timeouts.py)")
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _on_sigint)      # kept for the session: with nothing pending it is Ctrl-C


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    secs = limit_for(item)
    if secs <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return
    watch = _Watch(item, secs)
    _CURRENT.append(watch)
    watch.start()
    try:
        yield
    finally:
        watch.stop()
        _CURRENT.pop()
