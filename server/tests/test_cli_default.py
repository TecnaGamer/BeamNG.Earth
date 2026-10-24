"""The command line's defaults are exactly v1.4 M1's (docs/V1_4_SPEC.md §0: every later milestone ships
behind a flag that is off until its in-game probes pass; M4 `--block 512`, M1h `--horizon`): the server
`earthd.py` builds with no flags but `--block 1024` (mesh mode, 64 cells, no horizon; the CLI's block is
512 since M4's probes passed, test_new_flags_default_off) answers hello and
status with the same messages, selects the same tiles (with hysteresis and HOLD_S, on a fake clock),
groups and orders them the same way, predicts the same terrain requests (the prefetch), snaps and
commits the same blocks, and writes the same heightmap as the M1 tree did, compared with
fixtures/m1_cli_golden.json.

The fixture was written by the M1 code (git HEAD 5db1faf, before M4 and M1h) through compute() below;
regenerate it only from such a tree with

    python3 server/tests/test_cli_default.py --write <server dir> server/tests/fixtures/m1_cli_golden.json

(test_v131_default.py pins the Server() library default and terrain mode the same way.) compute()
patches the fake clock into the earthd module it imported only inside a `with` block.
"""
import asyncio
import hashlib
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "m1_cli_golden.json"
LAT0, LON0 = 36.5785, -118.2923
# (heading deg, speed m/s, seconds): a 134 m/s run at 37°, a slow turn, a 200 m/s run, a reversal
LEGS = [(37.0, 134.0, 20.0), (120.0, 20.0, 15.0), (0.0, 200.0, 12.0), (200.0, 60.0, 15.0)]
POSE_DT = 0.25


class RoughDem:
    def sample(self, lat, lon, zoom, progress=None):
        lat, lon = np.asarray(lat), np.asarray(lon)
        return (1500 + 300 * np.sin(lat * 90) * np.cos(lon * 70) + zoom * 7.0
                + 4.0 * np.sin(lat * 61000) * np.cos(lon * 47000))


def _h(data):
    return hashlib.sha256(data).hexdigest()


def _hs(items):
    return _h(",".join(sorted(map(str, items))).encode())


class _Clock:
    """earthd's `time` module with a settable monotonic clock (everything else is the real module)."""

    def __init__(self, real):
        self.real, self.now = real, 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(self.real, name)


class _Writer:
    def __init__(self):
        self.msgs = []

    def write(self, data):
        self.msgs.append(json.loads(data[4:].decode()))     # one protocol frame (4-byte length) per send

    def is_closing(self):
        return False

    def get_extra_info(self, name):
        return ("127.0.0.1", 0)


def compute(server_dir):
    """Outputs of the command-line default server of the tree at server_dir (v1.4 M1 or later)."""
    sys.path.insert(0, str(server_dir))
    try:
        import earthd
        import farfield as ff
        import heightmap
    finally:
        sys.path.pop(0)
    out = {}
    cells = earthd.resolve_cells(None, "mesh")
    clock = _Clock(earthd.time)

    async def run(tmp):
        s = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=None, cells=cells)
        s.dem = RoughDem()
        snap = getattr(s, "snap", heightmap.snap)       # M4 (Server.snap: the layout's grid); M1: 256 m
        try:
            out["server"] = {"cells": s.layout.cells, "format": s.layout.format, "min_level": s.min_level,
                             "near_visual": s.near_visual, "radius": s.radius,
                             "near_radii": sorted(s.near_radii.items()),
                             "tex": [s.tex_size(lvl) for lvl in range(-2, 10)],
                             "switch": [ff.switch_distance(lvl, s.kfun, s.near_radii, s.min_level)
                                        for lvl in range(-2, 10)]}
            w = _Writer()
            c = earthd.Connection(s, None, w)
            prefetches = []
            c.maybe_prefetch = lambda: prefetches.append(
                None if c.predicted_block is None else [c.predicted_block.cx, c.predicted_block.cy])
            c.dispatch({"type": "hello", "protocol": earthd.PROTOCOL, "format": 4, "modVersion": "0.4.0"})
            hello = dict(w.msgs[-1])
            hello.pop("sessionId")
            hello.pop("appVersion", None)     # the release (VERSION): changes every release
            out["hello"] = hello
            c.status("Ready")
            out["status"] = w.msgs[-1]
            a = s.new_anchor(LAT0, LON0)
            s.anchors[a.id] = a
            out["zooms"] = [a.zooms[lvl] for lvl in range(0, 10)]
            info = await s.terrain(a, snap(300.0), snap(-140.0))
            out["terrain"] = {k: (round(v, 6) if isinstance(v, float) else v)
                              for k, v in sorted(info.items()) if k not in ("seconds", "stages", "ter")}
            from PIL import Image
            with Image.open(os.path.join(s.out_dir, s.hm_name((a.id, snap(300.0), snap(-140.0))))) as im:
                out["terrain_png"] = [im.size[0], im.mode, _h(np.asarray(im).tobytes())]
            a.first_terrain = (0.0, 0.0)
            c.active = a.id
            c._set_block(a.id, 0.0, 0.0)
            c.update_far()
            trace = []
            x = y = 0.0
            for heading, speed, secs in LEGS:
                vx = speed * math.sin(math.radians(heading))
                vy = speed * math.cos(math.radians(heading))
                for _ in range(int(secs / POSE_DT)):
                    clock.now += POSE_DT
                    x, y = x + vx * POSE_DT, y + vy * POSE_DT
                    n0 = len(w.msgs)
                    # no camera: since 2026-10-16 it orders the build queue (what is on screen first; test_sse_lod),
                    # which M1's fixture predates
                    c.on_pose({"type": "pose", "anchorId": a.id, "x": x, "y": y, "z": 1500.0, "speed": speed,
                               "vx": vx, "vy": vy, "lat": LAT0, "lon": LON0})
                    # the mod's 1024² rule: prepare the predicted block, commit it 450 m past the centre
                    b = c.block
                    if c.pending_block is None and c.predicted_block is not None:
                        c.pending_block = c.predicted_block
                        c.update_far()
                    if c.pending_block is not None and max(abs(x - b.cx), abs(y - b.cy)) > 450:
                        c.commit_terrain({"type": "commit_terrain", "anchorId": a.id,
                                          "cx": c.pending_block.cx, "cy": c.pending_block.cy})
                    trace.append([round(x, 3), round(y, 3), [c.block.cx, c.block.cy],
                                  None if c.pending_block is None else [c.pending_block.cx, c.pending_block.cy],
                                  prefetches[-1] if prefetches else None,
                                  len(c.desired), _hs(c.desired), _hs(c.refined), len(c.groups),
                                  _h(json.dumps(sorted([sorted(ad), sorted(rm)] for ad, rm in c.groups)).encode()),
                                  _h(",".join(t.key for t in c.queue[:64]).encode()),
                                  # (load_progress: the on-screen loading line, added after M1; UI only)
                                  _h(json.dumps([m for m in w.msgs[n0:] if m.get("type") != "load_progress"],
                                                sort_keys=True).encode())])
            out["drive"] = trace
            out["predict"] = [list(earthd.predict_request(px, py, vx, vy, cx, cy))
                              for px in (-310.0, 0.0, 299.0, 301.0, 455.0) for py in (-120.0, 310.0)
                              for vx, vy in ((134.0, 0.0), (-80.0, 101.0), (0.3, -0.2), (190.0, 145.0))
                              for cx, cy in ((0.0, 0.0), (256.0, -512.0))]
            out["snap"] = [snap(v) for v in (-385.0, -129.0, -127.9, 0.0, 127.0, 129.0, 383.0, 385.0)]
        finally:
            s.hm_pool.shutdown(wait=True)
            s.tile_pool.shutdown(wait=True)
            if getattr(s, "horizon_pool", None) is not None:
                s.horizon_pool.shutdown(wait=True)

    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(earthd, "time", clock):
        asyncio.run(run(tmp))
    return out


class CliDefaultTest(unittest.TestCase):
    maxDiff = None

    def test_command_line_default_is_m1(self):
        want = json.loads(FIXTURE.read_text())
        got = json.loads(json.dumps(compute(Path(__file__).resolve().parents[1])))
        self.assertEqual(sorted(got), sorted(want))
        for k in want:
            self.assertEqual(got[k], want[k], k)

    def test_new_flags_default_off(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        try:
            import earthd
        finally:
            sys.path.pop(0)
        import inspect
        self.assertEqual(earthd.BLOCK_DEFAULT, 1024)                        # Server()'s block
        # M4 is on from the command line in mesh mode (P2, P7, P11 passed in-game); terrain mode keeps 1024
        self.assertEqual((earthd.resolve_block(None, "mesh"), earthd.resolve_block(None, "terrain"),
                          earthd.resolve_block(1024, "mesh")), (512, 1024, 1024))
        # M1h is on from the command line in mesh mode (2026-10-11); --no-horizon and terrain mode: off
        self.assertEqual((earthd.resolve_horizon(None, "mesh"), earthd.resolve_horizon(None, "terrain"),
                          earthd.resolve_horizon(False, "mesh")), (True, False, False))
        p = inspect.signature(earthd.Server).parameters
        self.assertEqual((p["block"].default, p["horizon"].default), (1024, False))   # M1h off until P12


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--write":
        res = compute(Path(sys.argv[2]).resolve())
        Path(sys.argv[3]).write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
        print("wrote", sys.argv[3])
    else:
        unittest.main()
