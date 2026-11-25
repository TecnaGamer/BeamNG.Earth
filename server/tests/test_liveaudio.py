"""server/liveaudio.py: the live weather sounds' process plumbing (the mod's 'weather_audio' to a `weathersynth
live-serve` child, its state lines back as 'audio_state'), against a stand-in child (a Python script that speaks the
same lines), and the 'audio_state' messages against server/protocol_table.json."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import liveaudio  # noqa: E402

TABLE = json.loads((Path(__file__).resolve().parents[1] / "protocol_table.json").read_text(encoding="utf-8"))

# the stand-in: echoes each input line to a file, says live once per line, a line {"die": ...} exits with stderr
FAKE = r'''
import json, sys
log = open(sys.argv[1], "a")
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


def check_state(test, m):
    spec = TABLE["server_to_mod"]["audio_state"]["fields"]
    test.assertEqual(m["type"], "audio_state")
    test.assertLessEqual(set(m) - {"type"}, set(spec))
    test.assertIsInstance(m["state"], str)
    for k in ("reason",):
        if k in m:
            test.assertIsInstance(m[k], str)
    if "latencyMs" in m:
        test.assertIsInstance(m["latencyMs"], float)


class LiveAudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = os.path.join(self.tmp.name, "fake_synth.py")
        self.log = os.path.join(self.tmp.name, "lines.log")
        Path(self.fake).write_text(FAKE, encoding="utf-8")
        self.started = []

        def popen(args, **kw):
            self.started.append(args)
            self.assertEqual(args[1:], ["live-serve"])
            kw.pop("creationflags", None)
            return subprocess.Popen([sys.executable, "-I", self.fake, self.log], **kw)

        self.la = liveaudio.LiveAudio(call=lambda fn, *a: fn(*a), binary="weathersynth", popen=popen)

    def tearDown(self):
        self.la.stop()
        time.sleep(0.2)
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

    def test_owner_close_stops(self):
        c = Conn()
        self.la.on_message(c, {"mode": "live"})
        self.assertTrue(c.wait(1))
        self.la.on_close(c)
        self.assertEqual(self.lines(2)[-1], "EOF")
        self.assertIsNone(self.la.proc)

    def test_child_dies_failed_no_respawn(self):
        c = Conn()
        self.la.on_message(c, {"mode": "die"})          # (not live: nothing starts)
        self.assertEqual(self.started, [])
        self.la.on_message(c, {"mode": "live"})
        self.assertTrue(c.wait(1))
        self.la.write({"mode": "die"})
        self.assertTrue(c.wait(2))
        check_state(self, c.sent[-1])
        self.assertEqual(c.sent[-1]["state"], "failed")
        self.assertIn("no audio device", c.sent[-1]["reason"])
        n = len(self.started)
        self.la.on_message(c, {"mode": "live"})
        self.assertEqual(len(self.started), n, "no respawn until pre-recorded and back")
        self.la.on_message(c, {"mode": "prerecorded"})
        self.la.on_message(c, {"mode": "live"})
        self.assertEqual(len(self.started), n + 1, "pre-recorded and back: tried again")

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
        self.assertIsNone(liveaudio.find_binary({"BNE_WEATHERSYNTH": "/nonexistent/weathersynth"}))

    def test_child_env(self):
        env = {"LD_LIBRARY_PATH": "/tmp/_MEI/x", "LD_LIBRARY_PATH_ORIG": "/opt/lib", "HOME": "/h"}
        self.assertEqual(liveaudio.child_env(env, frozen=False), env, "a source run: as it is")
        if os.name != "nt":
            self.assertEqual(liveaudio.child_env(env, frozen=True), {"LD_LIBRARY_PATH": "/opt/lib", "HOME": "/h"})
            self.assertEqual(liveaudio.child_env({"LD_LIBRARY_PATH": "/tmp/_MEI/x"}, frozen=True), {},
                             "frozen without an original: none")


if __name__ == "__main__":
    unittest.main()
