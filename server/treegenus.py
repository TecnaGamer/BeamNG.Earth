"""Europe's tree genera at a point: the shares of the trees around it that are larches, spruces (and firs), pines,
beeches, oaks, other conifers and other broadleaves, from ForestPaths' European Tree Genus Map 2020 (early access,
De Keersmaecker et al. 2025, Zenodo 10.5281/zenodo.13341104, CC BY 4.0), counted per 0.05 degree cell by
tools/build_treegenus.py (treegenus_2020.npz beside the modules: uint16 counts (7, rows, columns) from its north-west
corner). vegetation reads it for the conifers' share and the species of Europe's trees where it has a say."""
import os
import threading

import numpy as np

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "treegenus_2020.npz")
LARIX, PICEA, PINUS, FAGUS, QUERCUS, OTHER_NEEDLE, OTHER_BROAD = range(7)
NEEDLE = (LARIX, PICEA, PINUS, OTHER_NEEDLE)
MIN_TREES = 60          # sampled tree pixels (40 m: ~10 ha) the four cells about a point need for a say

_data = None
_lock = threading.Lock()


def _load():
    global _data
    with _lock:
        if _data is None:
            if not os.path.exists(PATH):
                _data = False
            else:
                with np.load(PATH) as z:
                    _data = (np.asarray(z["counts"], np.float64), float(z["north"]), float(z["west"]), float(z["res"]))
        return _data


def shares(lat, lon):
    """Per point: (n, 7) the genera's shares of the trees about it (bilinear in the cells' counts); NaN rows where the
    map has no say (outside Europe, too few trees)."""
    lat, lon = np.atleast_1d(np.asarray(lat, np.float64)), np.atleast_1d(np.asarray(lon, np.float64))
    out = np.full((len(lat), 7), np.nan)
    d = _load()
    if not d:
        return out
    c, north, west, res = d
    _, rows, cols = c.shape
    fy = (north - lat) / res - 0.5
    fx = (lon - west) / res - 0.5
    y0, x0 = np.floor(fy).astype(np.int64), np.floor(fx).astype(np.int64)
    inside = (y0 >= -1) & (y0 < rows) & (x0 >= -1) & (x0 < cols)
    if not inside.any():
        return out
    ty, tx = fy - y0, fx - x0
    acc = np.zeros((len(lat), 7))
    for dy, wy in ((0, 1.0 - ty), (1, ty)):
        j = np.clip(y0 + dy, 0, rows - 1)
        for dx, wx in ((0, 1.0 - tx), (1, tx)):
            i = np.clip(x0 + dx, 0, cols - 1)
            acc += (wy * wx)[:, None] * c[:, j, i].T
    tot = acc.sum(axis=1)
    ok = inside & (tot >= MIN_TREES / 4.0)
    out[ok] = acc[ok] / tot[ok, None]
    return out


def conifer_share(lat, lon):
    """Per point: the conifers' share of the trees (NaN: no say)."""
    s = shares(lat, lon)
    return s[:, list(NEEDLE)].sum(axis=1)
