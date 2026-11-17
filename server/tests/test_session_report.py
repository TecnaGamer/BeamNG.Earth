"""tools/session_report.py: a server log read into a session's load times and problems; the history file."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import session_report as sr  # noqa: E402

LOG = """\
2026-11-13 19:54:59,484 INFO earthd: road-tile processes: 4 (20 threads, 51 GB)
2026-11-13 19:54:59,484 INFO earthd: far-tile processes: 10 (3 at once while driving)
2026-11-13 19:54:59,484 INFO earthd: cores: the server on CPUs 17,18,19, the game on 0,1,2 (cpupin.py; --pin off)
earthd listening on 127.0.0.1:47613 session 20261113-195500-064f -> C:\\x\\temp\\beamngEarth\\s (far tiles: cdae, cache cap: 20 GB)
2026-11-13 19:55:34,484 INFO earthd: client connected ('127.0.0.1', 55832)
2026-11-13 19:55:57,000 INFO earthd: anchor 1 (52.33000, 4.87000) ready in 7.00 s, terrain at (-128, 384), groundZ -0.6; dem {}
2026-11-13 19:56:05,000 WARNING earthd.pools: road tile worker process died: its pool made again (1 so far)
2026-11-13 19:56:13,000 INFO earthd: terrain (1, -128.0, 384.0) z16 in 16.00 s (pre 0.00 queue 6.00 work 9.00 back 0.10 heights 6.31)
2026-11-13 19:56:13,000 INFO earthd: anchor 1: graded first block in 23.00 s after the request
2026-11-13 19:56:20,000 INFO earthd: road geotile 67307_43097 in 16.0 s (queue 1.0; content 3.5 s 2.7 cpu, skin 0.5 s 0.0 cpu, exact 0.0 s): items 1.1
2026-11-13 19:56:21,000 INFO earthd: road geotile 67308_43097 in 18.0 s (queue 1.2; content 4.0 s 2.7 cpu, skin 1.0 s 0.0 cpu, exact 0.0 s): items 1.1
2026-11-13 19:56:22,000 INFO earthd: trees geotile 67310_43097 in 21.0 s (queue 7.0, 32 in its job)
2026-11-13 19:56:23,000 INFO earthd: terrain_ready (1, -128.0, 128.0) after 7500 ms
2026-11-13 19:56:24,000 INFO earthd: provisional block (1, -128.0, 128.0) in 2.50 s in a worker (heights 0.46)
2026-11-13 19:56:30,000 WARNING earthd: event loop stalled > 2.0 s (every game message waits) in x.py:f:1
2026-11-13 19:56:31,000 WARNING earthd: event loop: the stall above lasted 2.5 s
2026-11-13 19:56:40,000 WARNING earthd.plancache: cell 14/1/2's plans: waited 90 s for process 1 making them (they came)
2026-11-13 19:56:41,000 WARNING earthd.roads: road geotile (1, 2) of anchor 1: not ready after 60 s (4 in flight, 0 sent): a first visit
2026-11-13 19:56:42,000 WARNING earthd.pools: road tile worker processes restarted (road geotile 1_2 hung for 300 s; 2 so far)
2026-11-13 19:56:43,000 ERROR earthd: provisional block (3, 128.0, 0.0) failed (the graded one is waited for)
Traceback (most recent call last):
PermissionError: [WinError 5] Access is denied
2026-11-13 19:56:44,000 INFO earthd.staging: hm_3_128_0_m8.png was held (Windows: the game or a virus scanner): replaced at try 2
2026-11-13 19:57:09,000 INFO earthd: anchor 1 far field done: 435 tiles (775 sent, 435 swaps in 71.79 s; build median 263.4 ms)
2026-11-13 19:57:11,000 INFO earthd: anchor 1 far field done: 396 tiles (8 sent, 6 swaps in 0.15 s; build median 263.4 ms)
2026-11-13 19:57:30,000 INFO earthd: game: perf minute 100 fps, 1% low 70, worst 1342 ms, 5 hitches (worst: x) at 25.0,55.1, 37 mph
2026-11-13 19:58:30,000 INFO earthd: game: perf minute 120 fps, 1% low 60, worst 76 ms, 0 hitches at 25.0,55.1, 0 mph
2026-11-13 20:01:20,000 INFO earthd: anchor 2 (39.28000, -76.59000) ready in 4.00 s, terrain at (-256, 384), groundZ 2.6; dem {}
"""


class SessionReportTest(unittest.TestCase):
    def setUp(self):
        self.s = sr.parse(LOG.splitlines(), "server.log")

    def test_session_and_loads(self):
        s = self.s
        self.assertEqual(s["id"], "20261113-195500-064f")
        self.assertEqual((s["road_processes"], s["far_processes"], s["server_cpus"]), (4, 10, "17,18,19"))
        self.assertEqual(s["connected_after_s"], 35.0)
        first, second = s["loads"]
        self.assertEqual((first["lat"], first["lon"], first["ground_s"], first["graded_first_s"]),
                         (52.33, 4.87, 7.0, 23.0))
        self.assertEqual(first["requested"], "19:55:50")
        self.assertEqual(first["first_road_s"], 30.0)           # 19:56:20, the first road tile after the request
        self.assertEqual((first["far_field_s"], first["far_tiles"]), (79.0, 435))   # the first "done", not the top-up
        self.assertEqual((second["ground_s"], second["graded_first_s"], second["first_road_s"]), (4.0, None, None))

    def test_build_times_and_game(self):
        s = self.s
        self.assertEqual(s["road_tile_s"], {"n": 2, "median": 17.0, "p95": 18.0, "max": 18.0})
        self.assertEqual(s["road_work_s"]["median"], 4.5)       # content + skin
        self.assertEqual((s["terrain_s"]["max"], s["terrain_queue_s"]["max"]), (16.0, 6.0))
        self.assertEqual((s["terrain_ready_s"]["max"], s["provisional_s"]["max"], s["trees_tile_s"]["n"]), (7.5, 2.5, 1))
        self.assertEqual(s["fps"], {"minutes": 2, "mean": 110, "lowest_1pct_low": 60, "worst_frame_ms": 1342,
                                    "hitches": 5})

    def test_problems(self):
        p = self.s["problems"]
        self.assertEqual((p["errors"], p["worker_died"], p["workers_restarted"], p["worker_hung_worst_s"]), (1, 1, 1, 300))
        self.assertEqual((p["plans_waited"], p["plans_waited_worst_s"]), (1, 90))
        self.assertEqual((p["road_tile_not_ready"], p["road_tile_late_worst_s"]), (1, 60))
        self.assertEqual((p["stalls"], p["stall_worst_s"], p["files_held_retried"]), (1, 2.5, 1))
        self.assertEqual(self.s["errors"], [{"count": 1, "message": "provisional block (..) failed (the graded one is "
                                                                    "waited for)"}])

    def test_empty_and_text(self):
        self.assertIsNone(sr.parse([], "x")["start"])
        self.assertIsNone(sr.dist([]))
        text = sr.detail(self.s) + sr.compare([self.s, self.s])
        self.assertIn("52.3300", text)
        self.assertIn("road tile median s", text)

    def test_history_keeps_label_and_commit(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "h", "history.jsonl")
            first = dict(self.s, label="cores=6", git="main@abc")
            self.assertEqual(len(sr.record(path, [first])), 1)
            again = dict(self.s, label=None, git="other@def", minutes=99.0)
            hist = sr.record(path, [again, dict(self.s, id="older", start="2026-01-01 00:00:00")])
            self.assertEqual([h["id"] for h in hist], ["older", "20261113-195500-064f"])
            self.assertEqual((hist[1]["label"], hist[1]["git"], hist[1]["minutes"]), ("cores=6", "main@abc", 99.0))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(len([json.loads(x) for x in f]), 2)


if __name__ == "__main__":
    unittest.main()
