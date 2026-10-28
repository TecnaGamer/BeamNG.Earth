"""The Koppen-Geiger climate class at a point (the forest ambience's set, vegetation._ambience): Beck et al. (2023),
High-resolution (1 km) Koppen-Geiger maps for 1901-2099, Scientific Data 10, 724 (CC BY 4.0), its 1991-2020 map at
0.1 degrees (koppen_1991_2020_0p1.png beside the modules: 3600 x 1800 from 180 W, 90 N; 0 the sea, 1-30 as their
legend: 1-3 A tropical, 4-7 B arid, 8-16 C temperate, 17-28 D cold, 29-30 E polar)."""
import os
import threading

import numpy as np

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "koppen_1991_2020_0p1.png")
RES = 0.1
NEAR_PX = 3             # a point on the sea's pixels (a coast at 11 km): the most common class within this many

_grid = None
_lock = threading.Lock()


def _load():
    global _grid
    with _lock:
        if _grid is None:
            from PIL import Image
            with Image.open(PATH) as im:
                _grid = np.asarray(im, np.uint8)
        return _grid


def koppen_at(lat, lon):
    """The class (1-30) at (lat, lon), the most common one within NEAR_PX pixels on the sea's (a coast), else 0."""
    g = _load()
    j = min(max(int((90.0 - lat) / RES), 0), g.shape[0] - 1)
    i = int(((lon + 180.0) % 360.0) / RES) % g.shape[1]
    k = int(g[j, i])
    if k:
        return k
    win = g[max(j - NEAR_PX, 0):j + NEAR_PX + 1, np.arange(i - NEAR_PX, i + NEAR_PX + 1) % g.shape[1]]
    vals = win[win > 0]
    return int(np.bincount(vals).argmax()) if vals.size else 0
