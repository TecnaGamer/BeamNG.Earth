"""server/evals/earth_trace.py: the parts that need no Earth tree (the server log's block parameters, the profile along a stage line).

    python -m pytest tests
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import earth_trace  # noqa: E402


def test_block_params_from_log(tmp_path):
    log = tmp_path / "server.log"
    log.write_text(
        "2026-11-14 15:36:07,982 INFO earthd: terrain (4, 640.0, 896.0) z16 in 61.70 s (heights 22.27): "
        "zBase 192 maxHeight 2048 (Q 1/32)\n"
        "2026-11-14 15:36:08,000 INFO earthd: terrain (4, 768.0, 896.0) z16 in 1.0 s (x): zBase 256 maxHeight 4096\n",
        encoding="utf-8")
    assert earth_trace.block_params_from_log(str(log), 640.0, 896.0) == (192.0, 2048.0)
    assert earth_trace.block_params_from_log(str(log), 768.0, 896.0) == (256.0, 4096.0)
    assert earth_trace.block_params_from_log(str(log), 512.0, 896.0) is None


def test_profile_finds_the_put_back_strip():
    xs = np.arange(0.0, 101.0)
    ys = np.arange(0.0, 101.0)
    X, Y = np.meshgrid(xs, ys)
    graded = np.zeros_like(X)
    hill = 0.2 * (X - 50.0)                     # the slope across a road running north along x = 50
    strip = (Y >= 40) & (Y <= 58)               # a tunnel's strip across it
    final = np.where(strip, hill, graded)
    reverted = strip & (np.abs(hill) >= 0.5)
    U = {"ground": hill, "graded": graded, "arch": final}
    line = np.array([[50.0, 0.0], [50.0, 100.0]])
    p = earth_trace.line_profile(line, xs, ys, U, reverted, core=4.0, side=12.0)
    hit = p["s"][p["rv"].any(1)]
    assert hit.min() >= 39 and hit.max() <= 59 and len(hit) >= 17
    lines = earth_trace.profile_lines(p, 4.0, 12.0)
    assert len(lines) == 2 and lines[1].startswith("  s 40 to 58 m")
    k = int(np.flatnonzero(p["s"] == 50)[0])
    assert np.isclose(np.nanmax(np.abs(p["dev"][k])), 0.2 * 12.0)


def test_profile_outside_the_box():
    xs = ys = np.arange(0.0, 51.0)
    U = {k: np.zeros((51, 51)) for k in ("ground", "graded", "arch")}
    line = np.array([[200.0, 0.0], [200.0, 100.0]])
    assert earth_trace.line_profile(line, xs, ys, U, np.zeros((51, 51), bool), 4.0, 12.0) is None
    assert earth_trace.profile_lines(None, 4.0, 12.0) == ["stage line: not in the box"]
