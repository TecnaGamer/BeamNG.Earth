"""The ecoregion at a point: RESOLVE's Ecoregions 2017 (Dinerstein et al. 2017, An Ecoregion-Based Approach to
Protecting Half the Terrestrial Realm, BioScience 67(6): 534-545; CC BY 4.0), rasterized at 0.05 degrees by
tools/build_ecoregions.py (ecoregions_2017_0p05.png beside the modules: 7200 x 3600 from 180 W, 90 N, 16-bit ECO_ID;
0 the open sea, the ice; the coasts grown 3 pixels out) with ecoregions_2017.json ({ECO_ID: [name, biome, realm]}).
The vegetation's regional rules (vegetation.py) name ecoregions by ECO_ID where a box of coordinates stood before."""
import json
import os
import threading

import numpy as np

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecoregions_2017_0p05.png")
META_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecoregions_2017.json")
RES = 0.05
# RESOLVE's biomes (BIOME_NUM)
B_TROP_MOIST, B_TROP_DRY, B_TROP_CONIFER, B_TEMP_BROAD, B_TEMP_CONIFER, B_BOREAL, B_TROP_GRASS, B_TEMP_GRASS, \
    B_FLOODED_GRASS, B_MONTANE_GRASS, B_TUNDRA, B_MEDITERRANEAN, B_DESERT, B_MANGROVE = range(1, 15)
# RESOLVE's realms (REALM), numbered 1.. in this order (0 none)
REALMS = ("Afrotropic", "Antarctica", "Australasia", "Indomalayan", "Nearctic", "Neotropic", "Oceania", "Palearctic")
R_AFROTROPIC, R_ANTARCTICA, R_AUSTRALASIA, R_INDOMALAYAN, R_NEARCTIC, R_NEOTROPIC, R_OCEANIA, R_PALEARCTIC = \
    range(1, len(REALMS) + 1)

_grid = None
_biome = None
_realm = None
_meta = None
_lock = threading.Lock()


def _load():
    global _grid, _biome, _realm, _meta
    with _lock:
        if _grid is None:
            import staticmaps

            def decode():
                from PIL import Image
                with Image.open(PATH) as im:
                    return np.asarray(im, np.uint16)
            g = staticmaps.mapped(PATH, decode)         # (shared with the worker processes)
            with open(META_PATH, encoding="utf-8") as f:
                meta = {int(k): v for k, v in json.load(f).items()}
            n = max(int(g.max()), max(meta)) + 1
            biome = np.zeros(n, np.uint8)
            realm = np.zeros(n, np.uint8)
            for k, v in meta.items():
                biome[k] = v[1]
                realm[k] = REALMS.index(v[2]) + 1 if v[2] in REALMS else 0
            _meta, _biome, _realm, _grid = meta, biome, realm, g
        return _grid


def _index(lat, lon, g):
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    j = np.clip(((90.0 - lat) / RES).astype(np.int64), 0, g.shape[0] - 1)
    i = (((lon + 180.0) % 360.0) / RES).astype(np.int64) % g.shape[1]
    return j, i


def eco_many(lat, lon):
    """The ECO_ID of each point (arrays; 0 at sea). The raster's coasts reach 3 pixels (~16 km) into the sea
    (tools/build_ecoregions.py grow_coasts): a quay's or a small island's tree finds its land by a plain lookup."""
    g = _load()
    j, i = _index(lat, lon, g)
    return g[j, i].astype(np.int64)


def biome_of(eco):
    """RESOLVE's biome (B_*; 0 none) of ECO_IDs."""
    _load()
    return _biome[np.clip(np.asarray(eco, np.int64), 0, len(_biome) - 1)]


def realm_of(eco):
    """RESOLVE's realm (R_*; 0 none) of ECO_IDs."""
    _load()
    return _realm[np.clip(np.asarray(eco, np.int64), 0, len(_realm) - 1)]


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
