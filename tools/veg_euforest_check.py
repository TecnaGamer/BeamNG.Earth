"""Compare the generator's species rules with EU-Forest (Mauri et al. 2017, CC0): tree species occurrences on the
National Forest Inventories' plots of 21 countries, 1 km INSPIRE grid (EPSG:3035).

Per 1-degree cell with enough plots: the observed share of each category = its occurrences / all categorised
occurrences (frequency of occurrence, not abundance: a plot with one birch counts as one birch); the model's share =
the generator's groups for synthetic forest trees (heights 6-25 m) at those same plots (the leaf type map, the Koppen
class, the DEM's height, the ecoregion: everything but the canopy height model's tree positions).

usage (from server/; the CSV from https://doi.org/10.6084/m9.figshare.c.3288407, EU-Forest-species.csv, CC0):
    PYTHONPATH=. python ../tools/veg_euforest_check.py CSV CACHE OUT.json
then tools/veg_euforest_report.py DIR (DIR/euf_ours.json [and DIR/euf_dev.json]) for the scores."""
import csv
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

import climate
import dem as demmod
import netfetch
import vegetation as veg

CATS = ("pine", "fir_spruce", "birch", "oak", "beech", "mediterranean", "eucalyptus")

GENUS = {
    "Pinus": "pine",
    "Picea": "fir_spruce", "Abies": "fir_spruce", "Larix": "fir_spruce", "Pseudotsuga": "fir_spruce",
    "Betula": "birch",
    "Quercus": "oak", "Fraxinus": "oak", "Ulmus": "oak", "Castanea": "oak", "Robinia": "oak",
    "Fagus": "beech", "Carpinus": "beech", "Acer": "beech", "Tilia": "beech", "Ostrya": "beech",
    "Olea": "mediterranean", "Arbutus": "mediterranean", "Ceratonia": "mediterranean", "Phillyrea": "mediterranean",
    "Eucalyptus": "eucalyptus",
}
EVERGREEN_OAKS = {"Quercus ilex", "Quercus suber", "Quercus coccifera", "Quercus rotundifolia"}
SPECIES = {"Populus tremula": "birch"}

GROUP_CAT = {"pine_eu": "pine", "pine_maritime": "pine", "pine_west": "pine", "spruce": "fir_spruce",
             "birch": "birch", "oak": "oak", "beech": "beech", "mediterranean": "mediterranean",
             "maquis": "mediterranean", "eucalyptus": "eucalyptus",
             # the dev branch's generic groups: only their leaf type is comparable
             "conifer": "conifer?", "deciduous": "broadleaf?"}
CONIFER_CATS = ("pine", "fir_spruce", "conifer?")


def category(name):
    if name in SPECIES:
        return SPECIES[name]
    if name in EVERGREEN_OAKS:
        return "mediterranean"
    return GENUS.get(name.split(" ")[0])


def laea_inverse(x, y):
    """EPSG:3035 (ETRS89-LAEA, lat0 52, lon0 10) to lat, lon in degrees: the sphere of the authalic radius (km-exact,
    enough for 1-degree cells)."""
    R, lat0, lon0 = 6371007.2, math.radians(52.0), math.radians(10.0)
    X, Y = np.asarray(x, float) - 4321000.0, np.asarray(y, float) - 3210000.0
    rho = np.hypot(X, Y)
    c = 2 * np.arcsin(np.clip(rho / (2 * R), -1, 1))
    with np.errstate(invalid="ignore", divide="ignore"):
        lat = np.arcsin(np.cos(c) * math.sin(lat0) + np.where(rho > 0, Y * np.sin(c) * math.cos(lat0) / rho, 0))
        lon = lon0 + np.arctan2(X * np.sin(c), rho * math.cos(lat0) * np.cos(c) - Y * math.sin(lat0) * np.sin(c))
    return np.degrees(lat), np.degrees(lon)


def load(path):
    plots = defaultdict(set)
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            c = category(r["SPECIES NAME"].strip())
            if c:
                plots[(int(float(r["X"])), int(float(r["Y"])))].add(c)
    return plots


def model_groups(lat, lon, elev, rng, per=40):
    n = len(lat) * per
    la, lo, z = (np.repeat(a, per) for a in (lat, lon, elev))
    h = rng.uniform(6.0, 25.0, n)
    u = [rng.random(n) for _ in range(9)]
    share = veg._conifer_share(la, lo)
    if hasattr(veg, "conifer_odds"):
        p = veg.conifer_odds(share, h)
    else:
        p = share * np.clip(h / 12.0, 0.5, 1.2)
    group = np.where(u[0] < p, veg.G_CONIFER, veg.G_DECIDUOUS).astype(np.uint8)
    lcls = np.full(n, veg.WC["tree"], np.uint8)
    group = veg.regional(group, h, la, lo, lcls, u[1])
    if hasattr(veg, "species_groups"):
        k = climate.koppen_many(la, lo)
        group = veg.species_groups(group, h, la, lo, lcls, k, z, u[2], u[3], u[4], u[5], u[6], u_stand=u[7])
    return group.reshape(len(lat), per)


def main(csv_path, cache, out):
    plots = load(csv_path)
    xy = np.array(list(plots.keys()), float)
    lat, lon = laea_inverse(xy[:, 0], xy[:, 1])
    cells = defaultdict(list)
    for i, (a, b) in enumerate(zip(lat, lon)):
        cells[(math.floor(a), math.floor(b))].append(i)
    rng = np.random.default_rng(1)
    dem = demmod.Dem(os.path.join(cache, "dem"), fetcher=netfetch.Fetcher(offline=False))
    res = []
    keys = sorted(k for k, v in cells.items() if len(v) >= 30)
    print(f"{len(plots)} plots, {len(keys)} cells with >= 30", flush=True)
    for j, key in enumerate(keys):
        idx = cells[key]
        obs = defaultdict(int)
        for i in idx:
            for c in plots[tuple(xy[i].astype(int))]:
                obs[c] += 1
        pick = rng.choice(idx, min(len(idx), 25), replace=False)
        try:
            z = np.asarray(dem.sample(lat[pick], lon[pick], 9), float).reshape(-1)
        except Exception as e:
            print("dem", key, e, flush=True)
            z = np.zeros(len(pick))
        g = model_groups(lat[pick], lon[pick], z, rng).reshape(-1)
        mod = defaultdict(int)
        for gi, cnt in zip(*np.unique(g, return_counts=True)):
            c = GROUP_CAT.get(veg.GROUPS[int(gi)].replace("_open", ""))
            if c:
                mod[c] += int(cnt)
        res.append({"cell": key, "plots": len(idx), "obs": dict(obs), "model": dict(mod),
                    "elev": float(np.median(z))})
        if j % 50 == 0:
            print(j, key, flush=True)
    json.dump(res, open(out, "w"))


if __name__ == "__main__":
    main(*sys.argv[1:4])
