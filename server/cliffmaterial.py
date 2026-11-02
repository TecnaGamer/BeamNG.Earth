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
ARID_CUT_HEIGHT_PATH = Path(__file__).with_name('cliff_arid_cut_height.png')
ARID_CUT_MATERIAL = 'bne_cliff_roadcut_arid'
NATURAL, LIMESTONE_CUT, ARID_CUT = range(3)
MATERIALS = ('bne_cliff_strata', ROAD_CUT_MATERIAL, ARID_CUT_MATERIAL)


def cut_family(biomes):
    """Arid climate rock versus the established default limestone cut.

    Biome 1 comes from the bundled Koppen B climates, not coordinates. This is
    an environmental appearance prior, not a claim of mapped local lithology.
    """
    return np.where(np.asarray(biomes) == 1, ARID_CUT, LIMESTONE_CUT)


@lru_cache(maxsize=3)
def _heightmap(family=NATURAL):
    from PIL import Image
    with Image.open((HEIGHT_PATH, ROAD_CUT_HEIGHT_PATH, ARID_CUT_HEIGHT_PATH)[family]) as im:
        return np.flipud(np.asarray(im, np.float32)/65535).copy()


def sample(u, v, roadcut=False, *, family=None):
    """Periodic bilinear height (metres) at the material's unreduced UVs.

    Pixel-centre UVs are shared with the mesh writer. The map is a geometry input,
    not a shader-only/parallax effect, so it changes silhouettes and casts shadows.
    """
    family = (LIMESTONE_CUT if roadcut else NATURAL) if family is None else family
    grid = _heightmap(family)
    ny, nx = grid.shape
    x, y = np.mod(u, 1)*nx-.5, np.mod(v, 1)*ny-.5
    ix, iy = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    fx, fy = x-ix, y-iy
    amplitude = RELIEF_M if family == NATURAL else ROAD_CUT_RELIEF_M
    return amplitude*((1-fy)*((1-fx)*grid[iy%ny, ix%nx] + fx*grid[iy%ny, (ix+1)%nx])
                    + fy*((1-fx)*grid[(iy+1)%ny, ix%nx] + fx*grid[(iy+1)%ny, (ix+1)%nx]))
