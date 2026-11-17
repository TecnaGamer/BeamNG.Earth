"""Physical scale and CPU sampling of the cliff material's shared heightmap."""
from functools import lru_cache
from pathlib import Path

import numpy as np
import clifftypes
from clifftypes import NATURAL, LIMESTONE_CUT, ARID_CUT, CANYON

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
CANYON_RELIEF_M = 0.65
# The canyon albedo uses broad beds, but nearby geometry needs the original
# scanned rock's broken ledges too. Keep its physical 91.44 m chart.
CANYON_SCAN_RELIEF_WEIGHT = 0.60
FAR_CANYON_NORMAL_WIDTH_SCALE = 3.0  # far-only broad erosion; near normals align with diffuse
CANYON_HEIGHT_M = 182.88  # broad sedimentary beds at canyon viewing distances
CANYON_HEIGHT_PATH = Path(__file__).with_name("cliff_canyon_height.png")
# Extra shader headroom is cancelled by vertex tint; it permits light satellite
# colours as well as dark ones without clipping individual colour channels.
# Linear-light gains; the baker encodes material factors as sRGB. Vertex
# colors cancel this headroom while allowing satellite tint to brighten rock.
SATELLITE_HEADROOM = 4.0
SATELLITE_ALBEDO_DETAIL_STRENGTH = .45  # other far rock: quiet variation around satellite colour
FAR_CANYON_ALBEDO_DETAIL_STRENGTH = .84  # restrained layers, satellite mean remains unchanged
NEAR_CANYON_EXPOSURE = .53  # reduce sunlit near-face brightness without flattening bed colors
NEAR_CANYON_NORMAL_STRENGTH = .42
FAR_CANYON_NORMAL_STRENGTH = .70  # soften grazing-light noise, retain macro erosion
CANYON_MEDIAN_SRGB = np.array((136, 105, 95), float)/255
CANYON_COLOR_FACTOR = np.array((.84, .90, .93))
FAR_STRATA_MEDIAN_SRGB = np.array((155, 152, 147), float)/255
FAR_STRATA_COLOR_FACTOR = np.array((1.3, 1.4, 1.5))
MATERIALS = ('bne_cliff_strata', ROAD_CUT_MATERIAL, ARID_CUT_MATERIAL, 'bne_cliff_canyon_sat')


def cut_family(biomes):
    """Arid climate rock versus the established default limestone cut.

    Biome 1 comes from the bundled Koppen B climates, not coordinates. This is
    an environmental appearance prior, not a claim of mapped local lithology.
    """
    return clifftypes.select(0, 0, context="road_cut", biomes=biomes)


@lru_cache(maxsize=4)
def _heightmap(family=NATURAL):
    from PIL import Image
    with Image.open((HEIGHT_PATH, ROAD_CUT_HEIGHT_PATH, ARID_CUT_HEIGHT_PATH, CANYON_HEIGHT_PATH)[family]) as im:
        return np.flipud(np.asarray(im, np.float32)/65535).copy()


def _sample(u, v, family, *, scan=False):
    import fast
    u, v = np.broadcast_arrays(np.asarray(u, float), np.asarray(v, float))
    amplitude = RELIEF_M if family == NATURAL else CANYON_RELIEF_M if family == CANYON else ROAD_CUT_RELIEF_M
    detail = (np.ascontiguousarray(_heightmap(NATURAL), float), RELIEF_M,
              CANYON_HEIGHT_M/HEIGHT_M, CANYON_SCAN_RELIEF_WEIGHT) if scan else None
    values = fast._ef.cliff_height_sample(np.ascontiguousarray(u.ravel()),
        np.ascontiguousarray(v.ravel()), np.ascontiguousarray(_heightmap(family), float), amplitude, detail)
    return values.reshape(u.shape)


def sample(u, v, roadcut=False, *, family=None):
    """Periodic pixel-centre height in metres, shared with native terrain relief."""
    family = (LIMESTONE_CUT if roadcut else NATURAL) if family is None else family
    return _sample(u, v, family)


def geometry_sample(u, v, *, family=NATURAL):
    """Blend canyon beds with scanned ledges at their original physical scale."""
    return _sample(u, v, family, scan=family == CANYON)


def natural_family(lat, lon):
    return clifftypes.select(lat, lon)


def height_span(family):
    return CANYON_HEIGHT_M if family == CANYON else HEIGHT_M
