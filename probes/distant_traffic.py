"""Temporary distant-traffic renderer probe (#565). Run with the checkout's .venv Python.

  distant_traffic.py start --mode calibration  an opacity/color row in front of the existing camera
  distant_traffic.py start --mode roads        bounded moving lights on the current near graph (not far coverage)
  distant_traffic.py start --mode far          production renderer on the cached far-road snapshot
  distant_traffic.py status                    counts and measured Lua update time
  distant_traffic.py stop                      remove only the probe's own objects
  distant_traffic.py shot                      call twice: async capture, then save an image under build/traffic-probe

Assets/Lua must first be copied into the user folder's temp/beamngEarthTrafficProbe, not the game installation.
No current mod files, input, camera, traffic vehicles, physics or environment settings are changed.
"""
import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import mcp


def call(name, args=None):
    result = mcp.rpc("tools/call", {"name": name, "arguments": args or {}}, timeout=15)
    if result.get("isError"):
        raise SystemExit(result)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["start", "stop", "status", "inspect", "shot", "metrics"])
    ap.add_argument("--mode", choices=["calibration", "occlusion", "roads", "far"], default="calibration")
    ap.add_argument("--count", type=int, default=32)
    ap.add_argument("--output", type=Path, default=ROOT / "build" / "traffic-probe" / "shot.jpg")
    a = ap.parse_args()
    if a.action == "shot":
        result = call("screenshot_image", {"scale": 0.8})
    elif a.action == "metrics":
        result = call("get_performance_metrics")
    else:
        module = "stream" if a.mode == "far" else "probe"
        if a.action == "start":
            code = "if extensions.bneTraffic_probe then extensions.unload('bneTraffic_probe') end; " \
                   "if extensions.bneTraffic_stream then extensions.unload('bneTraffic_stream') end; " \
                   f"local n,m = extensions.loadAtRoot('/temp/beamngEarthTrafficProbe/{module}', 'bneTraffic'); " \
                   "return jsonEncode({ started = m.start(%s), status = m.status() })" % \
                   ("{mode=" + json.dumps(a.mode) + ",count=" + str(a.count) + "}")
        else:
            prefix = f"local m = extensions.bneTraffic_{module}; if not m then return 'not loaded' end; "
            if a.action == "stop":
                code = prefix + f"m.stop(); local s=m.status(); extensions.unload('bneTraffic_{module}'); return jsonEncode(s)"
            else:
                code = prefix + ("return jsonEncode(m.inspect())" if a.action == "inspect" else "return jsonEncode(m.status())")
        result = call("run_lua", {"code": code})
    for block in result.get("content", []):
        if block.get("type") == "image":
            a.output.parent.mkdir(parents=True, exist_ok=True)
            a.output.write_bytes(base64.b64decode(block["data"]))
            print(a.output)
        elif block.get("type") == "text":
            print(block["text"])


if __name__ == "__main__":
    main()
