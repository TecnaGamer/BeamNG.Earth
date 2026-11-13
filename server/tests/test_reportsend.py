"""reportsend.py: the in-game reports' server part (folders by name only, the scrub of what is written, the send with
its retries and queue, the player's choices: screenshot, exact location, logs)."""
import asyncio
import base64
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast  # noqa: E402
import reportsend  # noqa: E402

NAME = "20261112_101500_report_1"
JPG = b"\xff\xd8\xff\xe0" + b"\x01" * 500


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= reportsend.NEED, "earthfast 267")
class ReportSendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = self.tmp.name
        self.dir = os.path.join(self.user, "beamngearth", "reports", NAME)
        os.makedirs(self.dir)
        home = os.path.expanduser("~")
        with open(os.path.join(self.dir, "report.json"), "w", encoding="utf-8") as fh:
            json.dump({"kind": "map", "title": "Floating road", "lat": 40.712776, "lon": -74.005974,
                       "folder": "beamngEarth/reports/" + NAME + "/", "note": f"log at {home}/x.log",
                       "view": {"camera": {"lat": 40.7128, "lon": -74.0059}}}, fh)
        with open(os.path.join(self.dir, "hardware.json"), "w", encoding="utf-8") as fh:
            json.dump({"cpu": {"name": "Test"}, "path": "C:\\Users\\Someone\\AppData"}, fh)
        with open(os.path.join(self.dir, "screenshot.jpg"), "wb") as fh:
            fh.write(JPG)
        self.posts = []
        self.answers = []
        self.notes = []

    def fake_post(self, url, body, timeout=0):
        self.posts.append(json.loads(body))
        return self.answers.pop(0) if self.answers else (200, {"ok": True, "id": "ABCD-EFGH"})

    def sender(self, **kw):
        return reportsend.Sender(self.user, self.tmp.name, "1.9.1-beta", "0" * 32, self.notes.append,
                                 url="http://test/report", post_fn=self.fake_post, backoff=(0, 0), **kw)

    def choose(self, **c):
        d = {"screenshot": True, "exactLocation": True, "logs": True}
        d.update(c)
        with open(os.path.join(self.dir, "send.json"), "w", encoding="utf-8") as fh:
            json.dump(d, fh)

    def state(self):
        with open(os.path.join(self.dir, "sent.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_folders_by_name_only(self):
        self.assertEqual(reportsend.folder(self.user, NAME), self.dir)
        for bad in ("../" + NAME, "/etc", NAME + "/..", "x", None, 5, "20261112_101500_report_1/../../x"):
            self.assertIsNone(reportsend.folder(self.user, bad), bad)
        os.makedirs(os.path.join(self.user, "beamngEarth", "reports", "20261112_101501_profile_2"))
        self.assertTrue(reportsend.folder(self.user, "20261112_101501_profile_2"))

    def test_send_with_choices(self):
        self.choose(screenshot=False, exactLocation=False)
        asyncio.run(self.sender().send(NAME))
        self.assertEqual(len(self.posts), 1)
        b = self.posts[0]
        self.assertEqual(b["report"]["lat"], 40.71, "the location rounded")
        self.assertEqual(b["report"]["view"]["camera"]["lon"], -74.01)
        self.assertNotIn("screenshot", b)
        self.assertEqual(b["sent"], {"exactLocation": False})
        self.assertIn("<home>", b["report"]["note"])
        self.assertEqual(b["hardware"]["path"], "<home>\\AppData")
        self.assertEqual(self.state()["state"], "sent")
        self.assertEqual(self.state()["id"], "ABCD-EFGH")
        self.assertEqual(self.notes[-1]["state"], "sent")
        # a second click: not sent again
        asyncio.run(self.sender().send(NAME))
        self.assertEqual(len(self.posts), 1)

    def test_exact_and_screenshot(self):
        self.choose()
        asyncio.run(self.sender().send(NAME))
        b = self.posts[0]
        self.assertEqual(b["report"]["lat"], 40.712776)
        self.assertEqual(base64.b64decode(b["screenshot"]), JPG)

    def test_retries_then_queue_and_resume(self):
        self.choose()
        self.answers = [(0, {"error": "no network"}), (503, {}), (502, {})]
        asyncio.run(self.sender().send(NAME))
        self.assertEqual(len(self.posts), 3)
        self.assertEqual(self.state()["state"], "queued", "kept for the next start")
        self.assertEqual([n["state"] for n in self.notes if n["state"] == "retry"], ["retry", "retry"])
        s = self.sender()
        self.assertEqual(s.pending(), [NAME])
        asyncio.run(s.resume(delay=0))
        self.assertEqual(self.state()["state"], "sent")
        self.assertEqual(s.pending(), [])

    def test_refused_is_failed_not_retried(self):
        self.choose()
        self.answers = [(415, {"error": "the screenshot is not a JPEG"})]
        asyncio.run(self.sender().send(NAME))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.state()["state"], "failed")
        self.assertIn("JPEG", self.state()["text"])

    def test_offline_queues(self):
        self.choose()
        asyncio.run(self.sender(offline=True).send(NAME))
        self.assertEqual(self.posts, [])
        self.assertEqual(self.state()["state"], "queued")

    def test_context_files_scrubbed(self):
        home = os.path.expanduser("~")
        reportsend.write_context(self.dir, {"env": {"BNE_X": f"{home}/cache"}, "spot": {"aim": {"lat": 40.712776}},
                                            "fps": float("nan"), "obj": object()},
                                 {"server.log": f"started in {home}/BeamNG.Earth\npeer 203.0.113.7 joined"},
                                 reportsend.user_names())
        with open(os.path.join(self.dir, "server.json"), encoding="utf-8") as fh:
            sj = fh.read()
        with open(os.path.join(self.dir, "logs.json"), encoding="utf-8") as fh:
            lj = fh.read()
        self.assertNotIn(home, sj + lj)
        self.assertIn("<ip>", lj)
        # sent without the exact location: the spot data left out, the logs' coordinates rounded
        self.choose(exactLocation=False)
        with open(os.path.join(self.dir, "logs.json"), "w", encoding="utf-8") as fh:
            json.dump({"server.log": "teleport to 40.712776, -74.005974"}, fh)
        asyncio.run(self.sender().send(NAME))
        b = self.posts[0]
        self.assertNotIn("spot", b["server"])
        logs = json.loads(gzip.decompress(base64.b64decode(b["logs"])))
        self.assertEqual(logs["server.log"], "teleport to 40.71, -74.01")

    def test_scrub_saved_and_env(self):
        reportsend.scrub_saved(self.dir, ["Someone"])
        with open(os.path.join(self.dir, "hardware.json"), encoding="utf-8") as fh:
            self.assertNotIn("Someone", fh.read())
        with mock.patch.dict(os.environ, {"BNGE_TELEMETRY_TOKEN": "secret", "BNE_ROADS": "on", "OTHER": "x"}):
            env = reportsend.env_overrides()
        self.assertEqual(env["BNGE_TELEMETRY_TOKEN"], "<set>")
        self.assertEqual(env["BNE_ROADS"], "on")
        self.assertNotIn("OTHER", env)

    def test_old_queued_reports_are_not_resumed(self):
        self.choose()
        with open(os.path.join(self.dir, "sent.json"), "w", encoding="utf-8") as fh:
            json.dump({"state": "queued"}, fh)
        old = time.time() - (reportsend.RESUME_DAYS + 1) * 86400
        os.utime(os.path.join(self.dir, "send.json"), (old, old))
        self.assertEqual(self.sender().pending(), [])


if __name__ == "__main__":
    unittest.main()
