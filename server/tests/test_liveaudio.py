"""server/liveaudio.py: the live weather sounds' process plumbing (the mod's 'weather_audio' to a `weathersynth
live-serve` child, its state lines back as 'audio_state'), against a stand-in child (a Python script that speaks the
same lines), and the 'audio_state' messages against server/protocol_table.json. Includes a child that does not read
its stdin for a while (the event loop must never block on it), the retries after a failed start, and the binary
lookup."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import liveaudio  # noqa: E402

TABLE = json.loads((Path(__file__).resolve().parents[1] / "protocol_table.json").read_text(encoding="utf-8"))

# the stand-in: echoes each input line to a file, says live once per line; {"mode": "die"} exits with a stderr line;
# argv[2] seconds of not reading stdin at the start (weathersynth opening a slow audio device)
FAKE = r'''
import json, sys, time
log = open(sys.argv[1], "a")
time.sleep(float(sys.argv[2]))
for line in sys.stdin:
    log.write(line); log.flush()
    d = json.loads(line)
    if d.get("mode") == "die":
        sys.stderr.write("no audio device: ALSA said no\n"); sys.stderr.flush()
        sys.exit(3)
    sys.stdout.write(json.dumps({"state": "live", "latencyMs": 21.5, "underruns": 0}) + "\n"); sys.stdout.flush()
log.write("EOF\n")
'''


class Conn:
    def __init__(self):
        self.sent = []
        self.closing = False
        self.cv = threading.Condition()

    def send(self, m):
        with self.cv:
            self.sent.append(m)
            self.cv.notify_all()

    def wait(self, n, timeout=5.0):
        end = time.monotonic() + timeout
        with self.cv:
            while len(self.sent) < n and time.monotonic() < end:
                self.cv.wait(0.05)
        return len(self.sent) >= n

    def states(self, s):
        return [m for m in self.sent if m.get("state") == s]


def check_state(test, m):
    spec = TABLE["server_to_mod"]["audio_state"]["fields"]
    test.assertEqual(m["type"], "audio_state")
    test.assertLessEqual(set(m) - {"type"}, set(spec))
    test.assertIsInstance(m["state"], str)
    if "reason" in m:
        test.assertIsInstance(m["reason"], str)
    if "latencyMs" in m:
        test.assertIsInstance(m["latencyMs"], float)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class LiveAudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = os.path.join(self.tmp.name, "fake_synth.py")
        self.log = os.path.join(self.tmp.name, "lines.log")
        Path(self.fake).write_text(FAKE, encoding="utf-8")
        self.started = []
        self.delay = 0.0
        self.clock = Clock()

        def popen(args, **kw):
            self.started.append(args)
            self.assertEqual(args[1:], ["live-serve"])
            kw.pop("creationflags", None)
            return subprocess.Popen([sys.executable, "-I", self.fake, self.log, str(self.delay)], **kw)

        self.la = liveaudio.LiveAudio(call=lambda fn, *a: fn(*a), binary="weathersynth", popen=popen, clock=self.clock)

    def tearDown(self):
        self.la.stop()
        time.sleep(0.3)
        self.tmp.cleanup()

    def lines(self, n, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if os.path.exists(self.log):
                got = Path(self.log).read_text(encoding="utf-8").splitlines()
                if len(got) >= n:
                    return got
            time.sleep(0.05)
        return Path(self.log).read_text(encoding="utf-8").splitlines() if os.path.exists(self.log) else []

    def test_live_round_trip(self):
        c = Conn()
        msg = {"type": "weather_audio", "mode": "live", "rainMmH": 4.0, "windMs": 3, "shelter": 1, "tunnel": False,
               "cabin": True, "volume": 0.5, "thunderVolume": 0.4, "muted": False, "duck": 1, "t": 1.7e9,
               "junk": "dropped", "thunders": [{"id": "7@1.000", "at": 6.0, "d": 1715, "kind": "close", "gain": 0.6,
                                                "seed": 7, "final": True, "x": 1}]}
        self.la.on_message(c, msg)
        self.assertTrue(c.wait(1), "a state line comes back")
        check_state(self, c.sent[0])
        self.assertEqual(c.sent[0], {"type": "audio_state", "state": "live", "latencyMs": 21.5})
        got = json.loads(self.lines(1)[0])
        self.assertNotIn("junk", got)
        self.assertNotIn("type", got)
        self.assertEqual(got["thunders"], [{"id": "7@1.000", "at": 6.0, "d": 1715, "kind": "close", "gain": 0.6,
                                            "seed": 7, "final": True}])
        self.la.on_message(c, dict(msg, rainMmH=5.0, thunders=None))
        self.assertEqual(json.loads(self.lines(2)[1])["rainMmH"], 5.0)
        self.assertEqual(len(self.started), 1, "one child for the session")
        # pre-recorded: the child gets EOF (it fades out and ends), the mod hears 'off'
        self.la.on_message(c, {"type": "weather_audio", "mode": "prerecorded"})
        self.assertEqual(self.lines(3)[-1], "EOF")
        self.assertEqual(c.sent[-1], {"type": "audio_state", "state": "off"})
        self.assertIsNone(self.la.proc)

    def test_a_child_not_reading_never_blocks(self):
        # (the weather agent's la_block.py: a child that did not read for a while blocked on_message for 6 s)
        self.delay = 3.0
        c = Conn()
        big = {"mode": "live", "rainMmH": 1.0, "pad": "x"}
        thunder = {"id": "keep", "at": 9.0, "d": 900, "kind": "close", "gain": 0.5, "seed": 1, "final": True}
        t0 = time.monotonic()
        for i in range(400):        # (~400 lines of ~100 bytes and more: well over a Windows pipe's 4 KB)
            m = dict(big, t=float(i), volume=0.5, thunders=[thunder] if i == 3 else None)
            self.la.on_message(c, m)
        self.assertLess(time.monotonic() - t0, 0.5, "the event loop never waits on the child")
        self.assertLessEqual(len(self.la.queue.items), liveaudio.QUEUE_MAX)
        got = [json.loads(x) for x in self.lines(liveaudio.QUEUE_MAX, timeout=8.0)]
        ths = [t for g in got for t in (g.get("thunders") or [])]
        self.assertIn("keep", [t["id"] for t in ths], "the dropped line's thunder rode on")
        self.assertEqual(got[-1]["t"], 399.0, "the newest line arrives")

    def test_owner_close_stops(self):
        c = Conn()
        self.la.on_message(c, {"mode": "live"})
        self.assertTrue(c.wait(1))
        self.la.on_close(c)
        self.assertEqual(self.lines(2)[-1], "EOF")
        self.assertIsNone(self.la.proc)

    def test_child_dies_retries_then_files(self):
        c = Conn()
        self.la.on_message(c, {"mode": "die"})          # (not live: nothing starts)
        self.assertEqual(self.started, [])

        def die_once():
            n = len(c.states("failed"))
            self.la.on_message(c, {"mode": "live"})
            self.la.queue.put({"mode": "die"})
            end = time.monotonic() + 5
            while len(c.states("failed")) <= n and time.monotonic() < end:
                time.sleep(0.02)
            return c.states("failed")[-1]

        first = die_once()
        check_state(self, first)
        self.assertEqual(first["state"], "failed")
        self.assertIn("no audio device", first["reason"])
        self.assertIn("5 s", first["reason"])
        n = len(self.started)
        self.la.on_message(c, {"mode": "live"})
        self.assertEqual(len(self.started), n, "not before the retry time")
        self.clock.t += 5.1
        self.assertIn("30 s", die_once()["reason"], "the first retry, then 30 s")
        self.clock.t += 30.1
        last = die_once()
        self.assertNotIn("trying again", last["reason"], "retries used up")
        n = len(self.started)
        self.clock.t += 1000
        self.la.on_message(c, {"mode": "live"})
        self.assertEqual(len(self.started), n, "then the files until the setting changes")
        self.la.on_message(c, {"mode": "prerecorded"})
        self.la.on_message(c, {"mode": "live"})
        self.assertEqual(len(self.started), n + 1, "pre-recorded and back: tried again")
        # live for a minute, then a failure: its retries again
        self.assertTrue(c.wait(len(c.sent) + 1))
        time.sleep(0.2)
        self.clock.t += liveaudio.LIVE_RESET_S + 1
        self.assertIn("5 s", die_once()["reason"])

    def test_oserror_retries(self):
        calls = []

        def popen(args, **kw):
            calls.append(args)
            raise PermissionError(13, "Access is denied")   # (Defender holding a freshly unpacked exe)

        la = liveaudio.LiveAudio(call=lambda fn, *a: fn(*a), binary="weathersynth", popen=popen, clock=self.clock)
        c = Conn()
        la.on_message(c, {"mode": "live"})
        self.assertEqual(len(calls), 1)
        self.assertIn("trying again in 5 s", c.sent[-1]["reason"])
        la.on_message(c, {"mode": "live"})
        self.assertEqual(len(calls), 1)
        self.clock.t += 5.1
        la.on_message(c, {"mode": "live"})
        self.assertEqual(len(calls), 2)

    def test_no_binary_off_once(self):
        la = liveaudio.LiveAudio(call=lambda fn, *a: fn(*a), binary=lambda: None)
        c = Conn()
        la.on_message(c, {"mode": "live"})
        la.on_message(c, {"mode": "live"})
        self.assertEqual(len(c.sent), 1)
        check_state(self, c.sent[0])
        self.assertEqual(c.sent[0]["state"], "off")
        self.assertIn("weathersynth", c.sent[0]["reason"])

    def test_pure(self):
        self.assertIsNone(liveaudio.state_message("not json"))
        self.assertIsNone(liveaudio.state_message('{"x": 1}'))
        self.assertEqual(liveaudio.state_message('{"state": "failed", "reason": "r", "latencyMs": true}'),
                         {"type": "audio_state", "state": "failed", "reason": "r"})
        many = {"mode": "live", "thunders": [{"id": str(i)} for i in range(100)] + ["junk"]}
        self.assertEqual(len(liveaudio.clean(many)["thunders"]), liveaudio.MAX_THUNDERS)
        self.assertEqual(set(liveaudio.FIELDS), set(TABLE["mod_to_server"]["weather_audio"]["fields"]))

    def test_line_queue(self):
        q = liveaudio.LineQueue(maxlen=2)
        q.put({"t": 1, "thunders": [{"id": "a"}]})
        q.put({"t": 2})
        q.put({"t": 3})
        self.assertEqual(q.dropped, 1)
        self.assertEqual(q.get(), {"t": 2})
        self.assertEqual(q.get(), {"t": 3, "thunders": [{"id": "a"}]})
        q.close()
        self.assertIsNone(q.get())

    def test_find_binary_and_exec_bit(self):
        self.assertIsNone(liveaudio.find_binary({"BNE_WEATHERSYNTH": "/nonexistent/weathersynth"}))
        with mock.patch.object(liveaudio, "candidates", return_value=[]):
            self.assertIsNone(liveaudio.find_binary({}), "never PATH")
        if os.name == "nt":
            return
        p = os.path.join(self.tmp.name, liveaudio.EXE)
        Path(p).write_bytes(b"#!/bin/sh\n")
        os.chmod(p, 0o644)
        liveaudio.ensure_exec(p)
        self.assertFalse(os.stat(p).st_mode & stat.S_IXUSR, "not ours: left as it is")
        with mock.patch.object(liveaudio, "candidates", return_value=[p]):
            self.assertEqual(liveaudio.find_binary({}), p)
            liveaudio.ensure_exec(p)
        self.assertTrue(os.stat(p).st_mode & stat.S_IXUSR, "ours (a zip lost the bit): executable again")

    def test_rate_log(self):
        clock = Clock()
        rl = liveaudio.RateLog(n=3, clock=clock)
        with self.assertLogs("liveaudio", "WARNING") as cm:
            for i in range(10):
                rl.line(f"x{i}")
            clock.t += 61
            rl.line("y")
        self.assertEqual([r.getMessage() for r in cm.records],
                         ["weathersynth: x0", "weathersynth: x1", "weathersynth: x2",
                          "weathersynth: 7 more lines left out of the log", "weathersynth: y"])

    def test_child_env(self):
        env = {"LD_LIBRARY_PATH": "/tmp/_MEI/x", "LD_LIBRARY_PATH_ORIG": "/opt/lib", "HOME": "/h"}
        self.assertEqual(liveaudio.child_env(env, frozen=False), env, "a source run: as it is")
        if os.name != "nt":
            self.assertEqual(liveaudio.child_env(env, frozen=True), {"LD_LIBRARY_PATH": "/opt/lib", "HOME": "/h"})
            self.assertEqual(liveaudio.child_env({"LD_LIBRARY_PATH": "/tmp/_MEI/x"}, frozen=True), {},
                             "frozen without an original: none")


if __name__ == "__main__":
    unittest.main()
