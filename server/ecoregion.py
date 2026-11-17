"""The ecoregion at a point: RESOLVE's Ecoregions 2017 (Dinerstein et al. 2017, An Ecoregion-Based Approach to
Protecting Half the Terrestrial Realm, BioScience 67(6): 534-545; CC BY 4.0), rasterized at 0.05 degrees by
tools/build_ecoregions.py (ecoregions_2017_0p05.png beside the modules: 7200 x 3600 from 180 W, 90 N, 16-bit ECO_ID;
0 the sea, the large lakes, the ice) with ecoregions_2017.json ({ECO_ID: [name, biome, realm]}). The vegetation's
regional rules (vegetation.py) name ecoregions by ECO_ID where a box of coordinates stood before."""
import json
import os
import threading

import numpy as np

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecoregions_2017_0p05.png")
META_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecoregions_2017.json")
RES = 0.05
NEAR_PX = 3             # a point on a 0 pixel (a coast at 5.5 km): the most common ecoregion within this many
# RESOLVE's biomes (BIOME_NUM)
B_TROP_MOIST, B_TROP_DRY, B_TROP_CONIFER, B_TEMP_BROAD, B_TEMP_CONIFER, B_BOREAL, B_TROP_GRASS, B_TEMP_GRASS, \
    B_FLOODED_GRASS, B_MONTANE_GRASS, B_TUNDRA, B_MEDITERRANEAN, B_DESERT, B_MANGROVE = range(1, 15)

_grid = None
_biome = None
_meta = None
_lock = threading.Lock()


def _load():
    global _grid, _biome, _meta
    with _lock:
        if _grid is None:
            from PIL import Image
            with Image.open(PATH) as im:
                g = np.asarray(im, np.uint16)
            with open(META_PATH, encoding="utf-8") as f:
                meta = {int(k): v for k, v in json.load(f).items()}
            biome = np.zeros(int(g.max()) + 1, np.uint8)
            for k, v in meta.items():
                if k < len(biome):
                    biome[k] = v[1]
            _meta, _biome, _grid = meta, biome, g
        return _grid


def _index(lat, lon, g):
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    j = np.clip(((90.0 - lat) / RES).astype(np.int64), 0, g.shape[0] - 1)
    i = (((lon + 180.0) % 360.0) / RES).astype(np.int64) % g.shape[1]
    return j, i


def eco_many(lat, lon):
    """The ECO_ID of each point (arrays); a point on a 0 pixel (a coast, a lake shore) the most common ecoregion within
    NEAR_PX pixels, else 0."""
    g = _load()
    j, i = _index(lat, lon, g)
    e = g[j, i].astype(np.int64)
    for n in np.flatnonzero(e == 0):
        win = g[max(j[n] - NEAR_PX, 0):j[n] + NEAR_PX + 1, np.arange(i[n] - NEAR_PX, i[n] + NEAR_PX + 1) % g.shape[1]]
        vals = win[win > 0]
        if vals.size:
            e[n] = int(np.bincount(vals).argmax())
    return e


def biome_of(eco):
    """RESOLVE's biome (B_*; 0 none) of ECO_IDs."""
    _load()
    eco = np.asarray(eco, np.int64)
    return _biome[np.clip(eco, 0, len(_biome) - 1)]


def name(eco):
    """An ecoregion's name ("" for 0 or an unknown ID)."""
    _load()
    v = _meta.get(int(eco))
    return v[0] if v else ""


def ids(*names):
    """The ECO_IDs of ecoregions by exact name (a KeyError for a misspelt one): the rules name them for the reader."""
    _load()
    by = {v[0]: k for k, v in _meta.items()}
    return tuple(by[n] for n in names)
