"""The forests' leaf type at a point: the conifers' and the evergreen broadleaves' shares of the forest around it, from
the Copernicus Global Land Service's Land Cover 100 m, collection 3, epoch 2019, Forest Type layer (Buchhorn et al.
2020, CC BY 4.0), counted per 0.05 degree pixel by tools/build_leaftype.py (leaftype_2019.png beside the modules: an
LA PNG from 180 W, 90 N, band 0 the conifers' share 0-250, band 1 the evergreen broadleaves', 255 too little forest to
say). vegetation._conifer_share takes the first over its coarse rule wherever the map has a say; species_groups the
second for the subtropics' evergreen (laurel) forests."""
import os
import threading

import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(_DIR, "leaftype_2019.png")
# (39) and the ESA CCI Land Cover / C3S map of 2020 (300 m; tools/build_leaftype_cci.py, the same layout; "(c) ESA
# Climate Change Initiative - Land Cover led by UCLouvain (2017)"): the two averaged where both have a say, either
# alone where one has. Against the national forest inventories' plots (EU-Forest, 675 one-degree cells in Europe) the
# conifer share's error 0.185 for the mean, 0.192 for CCI, 0.201 for LC100 alone (LC100 reads Sardinia's maquis,
# Lapland's birch woods and Spain's holm oaks as conifers; CCI misses some of Germany's spruces).
PATHS = (PATH, os.path.join(_DIR, "leaftype_cci_2020.png"))
NONE = 255

_grids = {}         # path -> (h, w, 2) uint8
_lock = threading.Lock()


def _load(path=PATH):
    with _lock:
        if path not in _grids:
            from PIL import Image
            with Image.open(path) as im:
                _grids[path] = np.ascontiguousarray(np.asarray(im.convert("LA"), np.uint8))
        return _grids[path]


def _bilinear(lat, lon, band):
    """Per point: band's share (0-1), the mean of the maps (PATHS) that have a say there; NaN where none has."""
    num, n = 0.0, 0
    for path in PATHS:
        if not os.path.exists(path):
            continue
        v = _bilinear1(lat, lon, band, path)
        ok = ~np.isnan(v)
        num = num + np.where(ok, v, 0.0)
        n = n + ok
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(np.asarray(n) > 0, num / np.maximum(n, 1), np.nan)


def _bilinear1(lat, lon, band, path):
    """Per point: one map's band share (0-1), bilinear between the pixels' centres over those with a say; NaN where
    none of the four has one."""
    g = _load(path)
    h, w = g.shape[:2]
    res = 180.0 / h
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    fy = (90.0 - lat) / res - 0.5
    fx = ((lon + 180.0) % 360.0) / res - 0.5
    y0, x0 = np.floor(fy).astype(np.int64), np.floor(fx).astype(np.int64)
    ty, tx = fy - y0, fx - x0
    num = np.zeros(lat.shape)
    den = np.zeros(lat.shape)
    for dy, wy in ((0, 1.0 - ty), (1, ty)):
        j = np.clip(y0 + dy, 0, h - 1)
        for dx, wx in ((0, 1.0 - tx), (1, tx)):
            v = g[j, (x0 + dx) % w, band]
            ok = v != NONE
            wt = np.where(ok, wy * wx, 0.0)
            num += wt * np.where(ok, v, 0) / 250.0
            den += wt
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 1e-9, num / np.maximum(den, 1e-9), np.nan)


def conifer_share(lat, lon):
    """Per point: the conifers' share of the forest (0-1); NaN where there is no forest about (the caller's rule)."""
    return _bilinear(lat, lon, 0)


def evergreen_broad_share(lat, lon):
    """Per point: the evergreen broadleaves' share of the forest (0-1); NaN where there is no forest about."""
    return _bilinear(lat, lon, 1)
