"""build_lamp_lens.py: the street lamps' lens (mod/.../art/shapes/beamngEarth/bne_lamp_lens.dae): a flat ellipse,
LENS_X x LENS_Y m, both faces, material bne_lamp_lens (pale glass by day; nightlights.lua lights it after dusk). The
stock lamp heads' glass is a material every building's windows share (bld_glass_transparent), so it cannot glow alone:
the server lays this lens just under each lamp head (furniture.py G_LENS).

    python3 tools/build_lamp_lens.py
"""
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import dae  # noqa: E402

LENS_X, LENS_Y = 0.60, 0.34     # m (a cobra head's lens: along the arm, across it)
N = 20


def main():
    ang = np.linspace(0.0, 2.0 * math.pi, N, endpoint=False)
    rim = np.c_[0.5 * LENS_X * np.cos(ang), 0.5 * LENS_Y * np.sin(ang), np.zeros(N)]
    P = np.vstack([[0.0, 0.0, 0.0], rim])
    UV = np.c_[0.5 + P[:, 0] / LENS_X, 0.5 + P[:, 1] / LENS_Y]
    down = [(0, 1 + (k + 1) % N, 1 + k) for k in range(N)]          # facing down (the street)
    up = [(0, 1 + k, 1 + (k + 1) % N) for k in range(N)]            # and up (seen from above)
    out = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                       "bne_lamp_lens.dae")
    dae.write_grid_dae(out, P.ravel(), UV.ravel(), np.array(down + up, np.int64).ravel(), material="bne_lamp_lens")
    print(out, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main()
