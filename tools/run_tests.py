#!/usr/bin/env python3
"""The server tests, each module in its own process, several at a time, with live progress and an ETA.

  python3 tools/run_tests.py                     every server/tests/test_*.py
  python3 tools/run_tests.py test_roads test_dem  just these modules

  --par N        modules at a time (default 6)
  --timeout S    a module still running after S seconds is killed with its child processes (default 1500);
                 shortly before, faulthandler prints every thread's stack (where it hangs)
  --logs DIR     one live log per module (unittest -v): tail -f DIR/<module>.log (default build/test-logs)

Prints a line for every test that fails, errors, is skipped or takes over 5 s, a status line every 30 s (modules
done, the ETA, what is running and its current test) and a summary per module. The ETA comes from the earlier runs'
times (build/test_times.json); the longest modules start first. Exit status 1 when a module did not pass.
BNGE_SKIP_DRIVE=1 skips the drive test, as with unittest discover.
"""
import argparse
import concurrent.futures as cf
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
TESTS = os.path.join(REPO_ROOT, "server", "tests")
BUILD = os.path.join(REPO_ROOT, "build")
HISTORY = os.path.join(BUILD, "test_times.json")
DEFAULT_S = 30.0        # a module never timed before
SLOW_S = 5.0            # a passing test this slow gets its own line
STATUS_S = 30.0
# unittest -v: "test_x (module.Class.test_x) ... ok"; the verdict may come lines later (the test's own output)
START = re.compile(r"^(test\w*) \(([\w.]+)\)")
VERDICT = re.compile(r"\b(ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$")


class Runner:
    def __init__(self, mods, par, timeout, logs, hist):
        self.mods, self.par, self.timeout, self.logs, self.hist = mods, par, timeout, logs, hist
        self.lock = threading.Lock()
        self.state = {}         # module -> {start, current, t_test, n, bad, proc, killed, done}
        self.t0 = time.time()

    def say(self, msg):
        with self.lock:
            print(f"[{time.time() - self.t0:6.0f}s] {msg}", flush=True)

    def expected(self, m):
        return self.hist.get(m, {}).get("secs", DEFAULT_S)

    def kill(self, st):
        """The module's process and everything it started (servers, pool workers: on Windows they outlive a
        killed parent)."""
        p = st.get("proc")
        if p is None or p.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def timed_out(self, st):
        st["killed"] = True
        self.kill(st)

    def run(self, m):
        st = self.state[m] = {"start": time.time(), "current": None, "t_test": time.time(), "n": 0, "bad": [],
                              "proc": None, "killed": False}
        # the stacks only: the timer kills the tree (an exit here would leave the module's servers behind)
        # --durations was introduced in Python 3.12; the companion supports 3.10+.
        code = ("import faulthandler,sys,unittest;faulthandler.dump_traceback_later(%d);"
                "sys.exit(not unittest.main(module=None,argv=['run_tests','-v',%r]"
                "+(['--durations=5'] if sys.version_info >= (3,12) else []),exit=False)"
                ".result.wasSuccessful())" % (max(1, self.timeout - 10), m))
        with open(os.path.join(self.logs, f"{m}.log"), "w", encoding="utf-8") as log:
            p = st["proc"] = subprocess.Popen(
                [sys.executable, "-B", "-u", "-c", code], cwd=TESTS, env=dict(os.environ, PYTHONUNBUFFERED="1"),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                start_new_session=os.name != "nt")      # its own process group: killpg reaches its children
            timer = threading.Timer(self.timeout, self.timed_out, (st,))
            timer.start()
            try:
                for line in p.stdout:
                    log.write(line)
                    log.flush()
                    self.parse(m, st, line.rstrip())
                p.wait()
            finally:
                timer.cancel()
        dt = time.time() - st["start"]
        res = "HANG" if st["killed"] else "OK" if p.returncode == 0 else "FAIL"
        self.say(f"== {m:26} {res:6} {dt:6.1f}s  {st['n']} tests"
                 + (f"  bad: {', '.join(st['bad'])}" if st["bad"] else ""))
        return m, res, dt

    def parse(self, m, st, line):
        if line.startswith("Timeout ("):                        # faulthandler's dump before the timeout
            self.say(f"{m:29} slow    near its timeout: stacks in {os.path.join(self.logs, m + '.log')}")
        s = START.match(line)
        if s:
            st["current"] = s.group(2).split(".", 1)[-1]        # its clock runs since the previous verdict
        v = VERDICT.search(line)
        if v and st["current"]:
            verdict, dt = v.group(1), time.time() - st["t_test"]
            st["n"] += 1
            if verdict in ("FAIL", "ERROR"):
                st["bad"].append(st["current"])
            if verdict != "ok" or dt > SLOW_S:
                self.say(f"{m:29} {verdict[:7]:7} {dt:6.1f}s  {st['current']}")
            st["current"], st["t_test"] = None, time.time()

    def eta(self):
        """Seconds left: the running modules' expected rest and the queued ones' times, over --par slots."""
        now = time.time()
        with self.lock:
            rest = [max(5.0, self.expected(m) - (now - s["start"])) for m, s in self.state.items() if "done" not in s]
            queued = [self.expected(m) for m in self.mods if m not in self.state]
        slots = sorted(rest + [0.0] * max(0, self.par - len(rest)))
        for q in queued:                        # in start order, each into the slot that frees first
            slots[0] += q
            slots.sort()
        return max(slots) if slots else 0.0

    def progress(self, m, s):
        now = time.time()
        tests = self.hist.get(m, {}).get("tests")
        left = self.expected(m) - (now - s["start"])
        return (f"{m} {now - s['start']:.0f}s {s['n']}/{tests if tests else '?'}"
                + (f" ~{left:.0f}s left" if left > 0 else " (over its usual time)")
                + (f" in {s['current']} {now - s['t_test']:.0f}s" if s["current"] else ""))

    def ticker(self, stop):
        while not stop.wait(STATUS_S):
            with self.lock:
                running = [(m, s) for m, s in self.state.items() if "done" not in s]
                done = len(self.state) - len(running)
            if running:
                self.say(f"{done}/{len(self.mods)} modules done, ETA ~{self.eta() / 60:.1f} min | running: "
                         + "; ".join(self.progress(m, s) for m, s in running))

    def save(self, m, dt):
        self.hist[m] = {"secs": round(dt, 1), "tests": self.state[m]["n"]}
        os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
        with open(HISTORY, "w", encoding="utf-8") as f:
            json.dump(self.hist, f, indent=1, sort_keys=True)

    def all(self):
        self.say(f"{len(self.mods)} modules, {self.par} at a time, timeout {self.timeout} s, "
                 f"ETA ~{self.eta() / 60:.1f} min, logs in {self.logs}")
        stop = threading.Event()
        threading.Thread(target=self.ticker, args=(stop,), daemon=True).start()
        res = []
        ex = cf.ThreadPoolExecutor(self.par)
        try:
            for f in cf.as_completed([ex.submit(self.run, m) for m in self.mods]):
                m, r, dt = f.result()
                self.state[m]["done"] = True
                res.append((m, r, dt))
                if r != "HANG":                         # a killed run's time says nothing
                    self.save(m, dt)
        except KeyboardInterrupt:
            self.say("interrupted: stopping the running modules")
            ex.shutdown(wait=False, cancel_futures=True)
            for s in list(self.state.values()):
                self.kill(s)
            return 130
        finally:
            stop.set()
        ex.shutdown()
        bad = [(m, r) for m, r, _ in res if r != "OK"]
        self.say(f"done: {len(res) - len(bad)} OK, {len(bad)} not OK"
                 + (": " + ", ".join(f"{m} ({r})" for m, r in bad) if bad else ""))
        # Each failed module's log tail in the ordinary CI log too: a failure stays diagnosable when the
        # artifact download is unavailable (Windows cleanup errors especially). PR #1214.
        for m, r in bad:
            try:
                with open(os.path.join(self.logs, m + '.log'), encoding='utf-8', errors='replace') as fh:
                    tail = fh.readlines()[-100:]
            except OSError as e:
                self.say(f"--- {m} ({r}): no log ({e}) ---")
                continue
            self.say(f"--- {m} ({r}): last {len(tail)} log lines ---")
            for line in tail:
                self.say(line.rstrip())
        return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description="Run the server tests module by module, with live progress.")
    ap.add_argument("modules", nargs="*", help="test modules (default: every server/tests/test_*.py)")
    ap.add_argument("--par", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=1500)
    ap.add_argument("--logs", default=os.path.join(BUILD, "test-logs"))
    a = ap.parse_args()
    mods = [m[:-3] if m.endswith(".py") else m for m in a.modules] or \
        sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(TESTS, "test_*.py")))
    os.makedirs(a.logs, exist_ok=True)
    try:
        with open(HISTORY, encoding="utf-8") as f:
            hist = json.load(f)
    except (OSError, ValueError):
        hist = {}
    mods.sort(key=lambda m: -hist.get(m, {}).get("secs", DEFAULT_S))     # longest first: none ends the run alone
    return Runner(mods, max(1, a.par), a.timeout, os.path.abspath(a.logs), hist).all()


if __name__ == "__main__":
    sys.exit(main())
