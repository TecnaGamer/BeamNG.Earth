"""Cold first-anchor failures must reach the connected client instead of leaving it waiting forever."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import earthd
import launcher
import pacer
from test_server import ServerHarness


class InitialAnchorErrorsTest(ServerHarness):
    def request_error(self, stage):
        self.server.warm_place = Mock()
        self.hello()
        self.c.send({"type": "prepare_anchor", "requestId": 71, "lat": 36.5785, "lon": -118.2923,
                     "purpose": "teleport"})
        m, seen = self.c.until(lambda m: m["type"] in ("anchor_error", "anchor_ready"), timeout=2)
        self.assertEqual(m["type"], "anchor_error", stage)
        self.assertEqual(m["requestId"], 71)
        self.assertIn("fixture", m["error"])
        self.assertEqual(m["stage"], stage)
        self.assertFalse(self.conn().teleport_pending)

    def test_unwritable_initial_output_reports_an_anchor_error(self):
        self.server.new_anchor = Mock(side_effect=PermissionError("fixture: output is not writable"))
        self.request_error("preparing")

    def test_land_cover_preparation_reports_an_anchor_error(self):
        self.server.surface = SimpleNamespace(ready=Mock(side_effect=OSError("fixture: land cover failed")))
        self.request_error("land_cover")

    def test_ready_message_preparation_reports_an_anchor_error(self):
        self.server.terrain = AsyncMock(return_value={"groundZ": 10.})
        self.server.dem.stats_snapshot = Mock(side_effect=OSError("fixture: finalization failed"))
        self.request_error("finalizing")

    def test_a_slow_first_ground_reports_its_phase_and_stops_reporting_after_ready(self):
        self.server.warm_place = Mock()
        self.server.surface = SimpleNamespace(ready=lambda *_: time.sleep(.08))
        async def terrain(*_args):
            await asyncio.sleep(.08)
            return {"groundZ": 10.}
        self.server.terrain = terrain
        self.hello()
        self.conn().terrain_msg = lambda *_args: {"size": 1024}
        with patch.object(earthd, "ANCHOR_PROGRESS_S", .01):
            self.c.send({"type": "prepare_anchor", "requestId": 72, "lat": 36., "lon": 6., "purpose": "teleport"})
            m, seen = self.c.until(lambda m: m["type"] == "anchor_ready", timeout=3)
            progress = [x for x in seen if x["type"] == "anchor_progress"]
            self.assertTrue({"preparing", "land_cover", "terrain"}.issubset({x["stage"] for x in progress}))
            self.assertTrue(all(x["requestId"] == 72 for x in progress))
            elapsed = [x["elapsed"] for x in progress]
            self.assertEqual(elapsed, sorted(elapsed))
            self.assertFalse([x for x in self.c.drain(.06) if x["type"] == "anchor_progress"])

    def test_cancelling_a_preparation_cleans_its_heartbeat_and_foreground_hold(self):
        self.server.warm_place = Mock()
        async def slow(*_args):
            await asyncio.sleep(100)
        self.server.terrain = slow
        self.hello()
        with patch.object(earthd, "ANCHOR_PROGRESS_S", .01), \
                patch.object(pacer, "foreground_begin", wraps=pacer.foreground_begin) as begin, \
                patch.object(pacer, "foreground_end", wraps=pacer.foreground_end) as end:
            self.c.send({"type": "prepare_anchor", "requestId": 73, "lat": 36., "lon": 6., "purpose": "teleport"})
            self.c.until(lambda m: m["type"] == "anchor_progress" and m["stage"] == "terrain", timeout=3)
            async def cancel():
                tasks = [t for t in self.conn().tasks if t.get_coro().__name__ == "prepare_anchor"]
                self.assertEqual(len(tasks), 1)
                tasks[0].cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            asyncio.run_coroutine_threadsafe(cancel(), self.loop).result(3)
            begin.assert_called_once()
            end.assert_called_once()
            self.hello()  # FIFO barrier: progress already queued before cancellation is consumed before this ack.
            with self.assertRaises(TimeoutError):
                self.c.until(lambda x: x["type"] == "anchor_progress", timeout=.1)


class StartupDiagnosticsTest(unittest.TestCase):
    def test_server_exit_reports_its_stage_and_real_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_text('2026-11-06 INFO earthd: startup: opening the download cache\n'
                            'Traceback (most recent call last):\nPermissionError: fixture: cache is not writable\n', encoding='utf-8')
            text = launcher.server_start_diagnostic(Mock(poll=lambda: 1), path)
        self.assertIn("exit code 1", text)
        self.assertIn("opening the download cache", text)
        self.assertIn("PermissionError", text)

    def test_deadline_is_distinguished_from_a_crash_and_missing_log_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = launcher.server_start_diagnostic(Mock(poll=lambda: None), Path(tmp) / "missing")
        self.assertIn("still running", text)
        self.assertIn("deadline", text)
        self.assertNotIn("exit code", text)

    def test_stderr_warning_does_not_hide_the_last_startup_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_text('x' * 100000 + '\n2026-11-06 INFO earthd: startup: preparing worker processes\n'
                            'resource_tracker: UserWarning: leaked semaphore objects\n')
            self.assertLessEqual(len(launcher._startup_tail(path)), 65536)
            text = launcher.server_start_diagnostic(Mock(poll=lambda: -9), path)
        self.assertIn("exit code -9", text)
        self.assertIn("preparing worker processes", text)
        self.assertLess(len(text), 700)


if __name__ == "__main__":
    unittest.main()
