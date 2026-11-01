"""Physical scale and CPU sampling of the cliff material's shared heightmap."""
from functools import lru_cache
from pathlib import Path

import numpy as np

WIDTH_M = 91.44
HEIGHT_M = 91.44
RELIEF_M = 3.0
# Runtime reference grid: about 0.36 m across / 1.43 m vertically. A 0.5 m
# terrain grid spans ~1.5 m vertically on a representative 72-degree cliff.
HEIGHT_SIZE = (256, 64)
REFERENCE_SLOPE = 3.0
UV_OFFSET = (0.0, 0.0)
HEIGHT_PATH = Path(__file__).with_name('cliff_height.png')
ROAD_CUT_HEIGHT_PATH = Path(__file__).with_name('cliff_cut_height.png')
ROAD_CUT_RELIEF_M = 0.65
ROAD_CUT_MATERIAL = 'bne_cliff_roadcut'


@lru_cache(maxsize=2)
def _heightmap(roadcut=False):
    from PIL import Image
    with Image.open(ROAD_CUT_HEIGHT_PATH if roadcut else HEIGHT_PATH) as im:
        return np.flipud(np.asarray(im, np.float32)/65535).copy()


def sample(u, v, roadcut=False):
    """Periodic bilinear height (metres) at the material's unreduced UVs.

    Pixel-centre UVs are shared with the mesh writer. The map is a geometry input,
    not a shader-only/parallax effect, so it changes silhouettes and casts shadows.
    """
    grid = _heightmap(roadcut)
    ny, nx = grid.shape
    x, y = np.mod(u, 1)*nx-.5, np.mod(v, 1)*ny-.5
    ix, iy = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    fx, fy = x-ix, y-iy
    amplitude = ROAD_CUT_RELIEF_M if roadcut else RELIEF_M
    return amplitude*((1-fy)*((1-fx)*grid[iy%ny, ix%nx] + fx*grid[iy%ny, (ix+1)%nx])
                    + fy*((1-fx)*grid[(iy+1)%ny, ix%nx] + fx*grid[(iy+1)%ny, (ix+1)%nx]))
