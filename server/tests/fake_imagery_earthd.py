"""earthd.py with a deterministic, offline imagery source, for the mesh-mode integration drive
(test_integration_drive.py): every source tile is seeded noise, so the drive needs no imagery
network access and every far tile gets a texture and its own material (the full protocol-4 path).

Usage: python3 fake_imagery_earthd.py <earthd.py arguments>
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd  # noqa: E402
import imagery  # noqa: E402

# Source tags in the red channel tell the sources apart in a texture (as test_server.FakeImagery).
RED = {"usgs": 60, "bngs2": 140, "bngs2fine": 140, "eox2016": 200}


def fake_load(self, key):
    """Imagery._load without the network: a 256² tile of seeded noise (never uniform, so the
    coverage probes count it as data) for every source, zoom and position."""
    src, z, x, y = key
    rng = np.random.default_rng([z, x, y])
    a = rng.integers(20, 220, (imagery.TILE, imagery.TILE, 3), dtype=np.uint8)
    a[..., 0] = RED.get(src.name, 100)
    self._count(src.name + ":fake")
    return imagery.as_tile(a)


imagery.Imagery._load = fake_load

if __name__ == "__main__":
    earthd.main()
