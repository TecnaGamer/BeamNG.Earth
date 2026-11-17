"""The forests' leaf type at a point: the conifers' share of the forest around it, from the Copernicus Global Land
Service's Land Cover 100 m, collection 3, epoch 2019, Forest Type layer (Buchhorn et al. 2020, CC BY 4.0), counted
per 0.05 degree pixel by tools/build_leaftype.py (leaftype_2019.png beside the modules: an LA PNG from 180 W, 90 N,
band 0 the conifers' share 0-250, band 1 the evergreen broadleaves' (unused yet), 255 too little forest to say).
vegetation._conifer_share takes it over its coarse rule wherever the map has a say."""
import os
import threading

import numpy as np

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leaftype_2019.png")
NONE = 255

_grid = None
_lock = threading.Lock()


def _load():
    global _grid
    with _lock:
        if _grid is None:
            from PIL import Image
            with Image.open(PATH) as im:
                _grid = np.ascontiguousarray(np.asarray(im.getchannel(0), np.uint8))
        return _grid


def conifer_share(lat, lon):
    """Per point: the conifers' share of the forest (0-1), bilinear between the pixels' centres over those with a say;
    NaN where none of the four has one (no forest about: the caller's rule)."""
    g = _load()
    h, w = g.shape
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
            v = g[j, (x0 + dx) % w]
            ok = v != NONE
            wt = np.where(ok, wy * wx, 0.0)
            num += wt * np.where(ok, v, 0) / 250.0
            den += wt
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 1e-9, num / np.maximum(den, 1e-9), np.nan)
