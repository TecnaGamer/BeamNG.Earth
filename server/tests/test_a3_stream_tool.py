"""Controlled A3 captures restore runtime limits and shadow diagnostics after setup failures."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("a3_stream_test", ROOT / "tools/roadstudy/a3_stream.py")
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


class ControlledStudyTest(unittest.TestCase):
    def run_study(self, tmp, fail=None):
        original = {"fpsLimit": 90, "fpsLimitEnabled": True,
                    "fpsLimitBackground": 60, "fpsLimitBackgroundEnabled": True}
        live = dict(original)
        shadow = ["1"]

        def lua(code):
            if code == study.FPS_CAPTURE_LUA:
                return json.dumps({"runtime": dict(live), "configured": dict(original),
                                   "reason": {"windowFocused": False, "limit": 60}})
            if code == study.FPS_APPLY_LUA:
                live["fpsLimitBackground"] = live["fpsLimit"]
                live["fpsLimitBackgroundEnabled"] = live["fpsLimitEnabled"]
                if fail == "fps_apply":
                    raise RuntimeError("fps_apply")  # setter succeeded, MCP reply lost
            elif "return 'FPS settings restored'" in code:
                live.update(original)
            elif code == study.FPS_REASON_LUA:
                return json.dumps({"windowFocused": False, "limit": live["fpsLimitBackground"]})
            elif "return t:getField('castShadows',0)" in code:
                return shadow[0]
            elif code.endswith("return 'set'"):
                shadow[0] = "0"
                if fail == "shadow_apply":
                    raise RuntimeError("shadow_apply")
            elif code.endswith("return 'restored'"):
                shadow[0] = "1"
            elif code.startswith("local v=getPlayerVehicle(0)") and fail == "camera":
                raise RuntimeError("camera")
            elif code.startswith("local c={enabled=0"):
                return '{"enabled":0,"disabled":100}'
            return "ok"

        def bench(name, value=None):
            if fail == name:
                raise RuntimeError(name)
            if name == "teleport":
                return {"ok": True}
            if name == "progress":
                return {"far": {"queued": 0, "old": 0}, "roads": {"walksQueued": 0, "queued": 0},
                        "player": {"lat": study.START[0], "lon": study.START[1]}, "serverBacklog": 0}
            return {}

        def render(name):
            if fail == "render":
                raise RuntimeError("render")
            return "{}"

        def drive(argv):
            self.assertEqual(live["fpsLimitBackground"], 90)
            self.assertEqual(shadow[0], "0")
            if fail == "drive":
                raise RuntimeError("drive")
            return 0

        profile = Path(tmp) / "profile.json"
        profile.write_text('{"points":[{"s":0,"speed":55.55,"heading":90}],"km":6}')
        out = Path(tmp) / "capture"
        with patch.object(sys, "argv", ["a3_stream.py", "--out", str(out), "--terrain-shadows", "off"]), \
                patch.object(study, "lua", side_effect=lua), patch.object(study, "call", side_effect=bench), \
                patch.object(study.mcp, "quiet_ui"), patch.object(study.mcp, "call", side_effect=render), \
                patch.object(study.drive, "main", side_effect=drive), patch.object(study, "PROFILE", profile), \
                patch.object(study.time, "sleep"), patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                patch("sys.stdout", new_callable=io.StringIO):
            if fail:
                with self.assertRaisesRegex(RuntimeError, fail):
                    study.main()
            else:
                self.assertEqual(study.main(), 0)
        self.assertEqual(live, original)
        self.assertEqual(shadow[0], "1")
        return json.loads((out / "environment.json").read_text()) if (out / "environment.json").exists() else None

    def test_matching_runtime_limits_and_start_end_reasons_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self.run_study(tmp)
        self.assertEqual(environment["fps_limits_saved"]["runtime"]["fpsLimitBackground"], 60)
        self.assertEqual(environment["fps_limits_test"]["fpsLimitBackground"], 90)
        self.assertEqual(environment["fps_limit_reason_start"], {"windowFocused": False, "limit": 90})
        self.assertEqual(environment["fps_limit_reason_end"], {"windowFocused": False, "limit": 90})

    def test_all_overrides_restore_after_setup_and_drive_failures(self):
        for stage in ("fps_apply", "teleport", "shadow_apply", "camera", "render", "drive"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                self.run_study(tmp, fail=stage)


if __name__ == "__main__":
    unittest.main()
