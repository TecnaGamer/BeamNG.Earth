"""Industrial sounds: West Coast USA's refinery tower sounds (the user, 2026-10-26, on a clip of them: "various fans and
monitors"; its power plant sound, "an alarm", is not used) on a GRID_M grid inside OpenStreetMap's refineries and
petrochemical plants (industrial=refinery / petrochemical areas, built once by tools/build_refineries.py into
refineries.npz beside the modules: ODbL, (c) OpenStreetMap contributors; Overture keeps only an area's main tag,
landuse=industrial, so its land use alone cannot tell them; its industrial / works areas named as one count too:
NAME_WORDS; Baytown's ExxonMobil plant is no refinery area in OSM), the three in turn by grid square, UP_M over the
ground. Rows for roads.sound_boxes' list: [kind, x, y, z, 0, 0, 0, 0] (the mod's ambience.lua plays a point kind's
event there)."""
import os
import re
import threading

import numpy as np

from polygon import Path

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refineries.npz")
GRID_M = 150.0
UP_M = 5.0
NAME_WORDS = re.compile(r"refin|raffin|rafin|нефтеперераб", re.I)
PLANT_CLASSES = ("industrial", "works")
# Power plants that look like ones (the user, 2026-10-26: West Coast USA's power plant sound "just quieter", "at
# locations that look like power plants"): areas mapped as plants (Overture infrastructure class plant, or land use
# tagged power=plant) burning fuel or nuclear, by plant:source / generator:source (solar, wind and hydro farms and
# plants of no stated source are left out); their sound on the same grid (the mod plays it quieter: ambience.lua)
THERMAL = re.compile(r"coal|gas|oil|diesel|nuclear|biomass|biogas|biofuel|waste|geothermal|lignite|peat", re.I)

_data = None
_lock = threading.Lock()


def _load():
    """(lonlat (n, 2) degrees, ring offsets, each ring's area, ring boxes (k, 4) lon0 lat0 lon1 lat1)."""
    global _data
    with _lock:
        if _data is None:
            if not os.path.exists(PATH):
                _data = (np.zeros((0, 2)), np.zeros(1, np.int64), np.zeros(0, np.int32), np.zeros((0, 4)))
            else:
                with np.load(PATH, allow_pickle=False) as f:
                    ll = f["lonlat"].astype(np.float64) / 1e6
                    off, area = f["ring_off"], f["ring_area"]
                boxes = np.array([[ll[a:b, 0].min(), ll[a:b, 1].min(), ll[a:b, 0].max(), ll[a:b, 1].max()]
                                  for a, b in zip(off[:-1], off[1:])]).reshape(-1, 4)
                _data = (ll, off, area, boxes)
        return _data


def _named_plants(frame, tx, ty, md):
    """The parts (rings in the frame) of the Overture industrial / works areas named as refineries in the tile's cells."""
    import roads
    if md is None:
        return []
    out, seen = [], set()
    for key in roads.cells_of_tile(frame, tx, ty, margin=0.0):
        f = md.cell(key, layers=("land_use",), kind="demand").layers.get("land_use")
        if f is None or not len(f.kind) or f.attrs.get("class") is None or f.attrs.get("name") is None:
            continue
        names = f.attrs["name"]
        xy = None
        for i in np.flatnonzero(np.isin(f.attrs["class"].astype(str), PLANT_CLASSES)):
            if f.ids[i] in seen or not NAME_WORDS.search(str(names[i])):
                continue
            seen.add(f.ids[i])
            xy = f.xy_in(frame) if xy is None else xy
            out.append(f.parts(i, xy))
    return out


def refinery_sound_rows(frame, tx, ty, md, elev):
    """The refineries' sound points in road tile (tx, ty) (squares of a GRID_M grid of the anchor frame, so a refinery
    across tiles gets one grid); elev(lat, lon): the ground; md: the map data (Overture's named plants)."""
    import roads
    ll, off, area, boxes = _load()
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1], np.float64), np.array([y0, y0, y1, y1], np.float64),
                                      np.zeros(4))
    near = np.flatnonzero((boxes[:, 0] <= lo.max()) & (boxes[:, 2] >= lo.min()) &
                          (boxes[:, 1] <= la.max()) & (boxes[:, 3] >= la.min())) if len(boxes) else []
    named = _named_plants(frame, tx, ty, md)
    if not len(near) and not named:
        return []
    kx = np.arange(np.floor(x0 / GRID_M), np.ceil(x1 / GRID_M))
    ky = np.arange(np.floor(y0 / GRID_M), np.ceil(y1 / GRID_M))
    gx, gy = np.meshgrid((kx + 0.5) * GRID_M, (ky + 0.5) * GRID_M)
    pts = np.stack([gx.ravel(), gy.ravel()], -1)
    pts = pts[(pts[:, 0] >= x0) & (pts[:, 0] < x1) & (pts[:, 1] >= y0) & (pts[:, 1] < y1)]
    if not len(pts):
        return []
    # every ring of the areas near (all of an area's rings: even-odd within it)
    inside = np.zeros(len(pts), bool)
    for a in np.unique(area[near]):
        here = np.zeros(len(pts), bool)
        for r in np.flatnonzero(area == a):
            ring = ll[off[r]:off[r + 1]]
            e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], np.zeros(len(ring)))
            here ^= Path(np.stack([np.asarray(e), np.asarray(n)], -1)).contains_points(pts)
        inside |= here
    for parts in named:
        for part in parts:
            here = np.zeros(len(pts), bool)
            for ring in part:
                here ^= Path(ring).contains_points(pts)
            inside |= here
    if not inside.any():
        return []
    p = pts[inside]
    pla, plo, _ = frame.enu_to_geodetic(p[:, 0], p[:, 1], np.zeros(len(p)))
    _, _, z = frame.geodetic_to_enu(pla, plo, np.asarray(elev(pla, plo), np.float64))
    out = []
    for (x, y), zz in zip(p, np.asarray(z)):
        n = int(np.floor(x / GRID_M) + np.floor(y / GRID_M)) % len(roads.SOUND_REFINERY)
        out += [float(roads.SOUND_REFINERY[n]), round(float(x), 2), round(float(y), 2), round(float(zz) + UP_M, 2),
                0.0, 0.0, 0.0, 0.0]
    return out


def power_plant_sound_rows(frame, tx, ty, md, elev):
    """The fuel-burning / nuclear power plants' sound points in road tile (tx, ty) (THERMAL; kind
    roads.SOUND_POWER_PLANT), on the GRID_M grid inside them."""
    import json
    import roads
    if md is None:
        return []
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    kx = np.arange(np.floor(x0 / GRID_M), np.ceil(x1 / GRID_M))
    ky = np.arange(np.floor(y0 / GRID_M), np.ceil(y1 / GRID_M))
    gx, gy = np.meshgrid((kx + 0.5) * GRID_M, (ky + 0.5) * GRID_M)
    pts = np.stack([gx.ravel(), gy.ravel()], -1)
    pts = pts[(pts[:, 0] >= x0) & (pts[:, 0] < x1) & (pts[:, 1] >= y0) & (pts[:, 1] < y1)]
    if not len(pts):
        return []
    inside = np.zeros(len(pts), bool)
    seen = set()
    for key in roads.cells_of_tile(frame, tx, ty, margin=0.0):
        cell = md.cell(key, layers=("infrastructure", "land_use"), kind="demand")
        for layer in ("infrastructure", "land_use"):
            f = cell.layers.get(layer)
            if f is None or not len(f.kind) or f.attrs.get("tags") is None:
                continue
            cls, tags = f.attrs.get("class"), f.attrs["tags"]
            xy = None
            for i in np.flatnonzero(f.kind == 3):
                t = str(tags[i])
                if f.ids[i] in seen or not ((layer == "infrastructure" and cls is not None and str(cls[i]) == "plant")
                                            or '"power":"plant"' in t):
                    continue
                try:
                    d = json.loads(t) if t.startswith("{") else {}
                except ValueError:
                    d = {}
                if not THERMAL.search(str(d.get("plant:source") or d.get("generator:source") or "")):
                    continue
                seen.add(f.ids[i])
                xy = f.xy_in(frame) if xy is None else xy
                for part in f.parts(i, xy):
                    here = np.zeros(len(pts), bool)
                    for ring in part:
                        here ^= Path(ring).contains_points(pts)
                    inside |= here
    if not inside.any():
        return []
    p = pts[inside]
    pla, plo, _ = frame.enu_to_geodetic(p[:, 0], p[:, 1], np.zeros(len(p)))
    _, _, z = frame.geodetic_to_enu(pla, plo, np.asarray(elev(pla, plo), np.float64))
    out = []
    for (x, y), zz in zip(p, np.asarray(z)):
        out += [float(roads.SOUND_POWER_PLANT), round(float(x), 2), round(float(y), 2), round(float(zz) + UP_M, 2),
                0.0, 0.0, 0.0, 0.0]
    return out
