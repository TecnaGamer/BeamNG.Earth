"""French Open Data (IGN Géoplateforme WFS, Etalab Licence Ouverte 2.0) for agricultural parcels and hedgerows:
  - RPG.LATEST:parcelles_graphiques (Registre Parcellaire Graphique):
      * Vineyards (Vignes): code_cultu in ('VRC', 'VRT') or code_group == '21'
      * Lavender & Lavandin (Lavande / Lavandin): code_cultu in ('LAV', 'PPAM') or code_group == '24'
      * Olive Groves (Oliveraies): code_cultu == 'OLI' or code_group == '23'
      * Truffle Oaks (Truffières): code_cultu == 'TRU'
  - BDTOPO_V3:haie:
      * Linear hedgerows (Mistral cypress windbreaks in Provence / Mediterranean France)
  - BDTOPO_V3:zone_de_vegetation:
      * Vineyard polygons (nature == 'Vigne') and mapped hedgerow strips (nature == 'Haie')

Cached per z14 map-data cell under <cache>/ign_fr/v<VERSION>/<z>/<x>/<y>.json.gz.
"""
from collections import OrderedDict
from dataclasses import dataclass
import gzip
import json
import logging
import math
import os
import threading

import numpy as np

import mapcells
import mapgeom
import staging

log = logging.getLogger("earthd.ign_fr")

VERSION = 1
ON = os.environ.get("BNE_IGN_FR", "on") != "off"
WFS_URL = "https://data.geopf.fr/wfs/ows"
LANE = "ign_wfs"
MEM_CELLS = 64

# Metropolitan France bounding box (lat0, lon0, lat1, lon1)
FRANCE_BOUNDS = (41.3, -5.2, 51.2, 9.7)
# Provence & wider Mediterranean France where hedgerows (BDTOPO_V3:haie) are predominantly cypress windbreaks
PROVENCE_LAT_MAX = 45.2
PROVENCE_LON_MIN = 2.0


def in_france(lat, lon):
    """True when (lat, lon) lies within Metropolitan France."""
    s, w, n, e = FRANCE_BOUNDS
    return s <= float(lat) <= n and w <= float(lon) <= e


def cell_in_france(bounds):
    """True when cell bounds (lon0, lat0, lon1, lat1) overlap Metropolitan France."""
    if not bounds:
        return False
    lon0, lat0, lon1, lat1 = bounds
    s, w, n, e = FRANCE_BOUNDS
    return lon1 >= w and lon0 <= e and lat1 >= s and lat0 <= n


def in_provence(lat, lon):
    """True in Southern / Mediterranean France (Provence, Languedoc, Rhone valley) where cypress windbreaks dominate."""
    la, lo = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    return (la >= 41.3) & (la <= PROVENCE_LAT_MAX) & (lo >= PROVENCE_LON_MIN) & (lo <= 9.7)


def in_mediterranean(lat, lon):
    """True across the Mediterranean basin (Southern France, Italy, Spain, Greece, etc.)."""
    la, lo = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    return (la >= 35.0) & (la <= 45.2) & (lo >= -9.5) & (lo <= 28.0)


@dataclass
class FrCell:
    polys: mapgeom.Features     # POLYGON features with attrs["crop"] in {"vineyard", "lavender", "olive", "truffle"}
    hedges: list                # list of (np.ndarray (N, 2) lonlat, float height_m, float width_m)


def _classify_rpg(props):
    """Classify an RPG parcel's properties dict into 'vineyard', 'lavender', 'olive', 'truffle', or None."""
    if not props:
        return None
    cultu = str(props.get("code_cultu") or "").strip().upper()
    group = str(props.get("code_group") or "").strip()
    if cultu in ("VRC", "VRT") or group == "21":
        return "vineyard"
    if cultu in ("LAV", "PPAM") or group == "24":
        return "lavender"
    if cultu == "OLI" or group == "23":
        return "olive"
    if cultu == "TRU":
        return "truffle"
    return None


def _parse_wall_mat(code):
    """Map DGFiP MAJIC / BD TOPO materiaux_des_murs (dmatgm) 1-2 digit code to facade_material."""
    s = str(code or "").strip()
    if not s or s in ("0", "00", "9", "90", "99", "None", "null"):
        return ""
    for ch in s:
        if ch in ("1", "2"):
            return "stone"
        if ch in ("3", "4", "5"):
            return "stucco"
        if ch == "6":
            return "wood"
        if ch == "8":
            return "metal"
    return ""


def _parse_roof_mat(code):
    """Map DGFiP MAJIC / BD TOPO materiaux_de_la_toiture (dmatto) 1-2 digit code to roof_material."""
    s = str(code or "").strip()
    if not s or s in ("0", "00", "9", "90", "99", "None", "null"):
        return ""
    for ch in s:
        if ch == "1":
            return "tile"
        if ch == "2":
            return "slate"
        if ch == "3":
            return "metal"
        if ch == "4":
            return "concrete"
    return ""


def _parse_bld_class(props):
    """Map BD TOPO nature / usage_1 / construction_legere to Overture building class."""
    nat = str(props.get("nature") or "").strip()
    usg = str(props.get("usage_1") or "").strip()
    leg = bool(props.get("construction_legere"))
    if nat in ("Chapelle", "Eglise") or "Religieux" in usg:
        return "church"
    if nat == "Serre":
        return "greenhouse"
    if nat in ("Silo", "Bâtiment agricole", "Grange") or "Agricole" in usg:
        return "farm_auxiliary"
    if nat == "Bâtiment industriel" or "Industriel" in usg:
        return "industrial"
    if nat == "Bâtiment commercial" or "Commercial" in usg:
        return "commercial"
    if leg:
        return "shed"
    if "Résidentiel" in usg or usg in ("Indifférencié", ""):
        return "residential"
    return ""


def _extract_rings(geom):
    """Extract a list of polygon parts (each part is a list of closed rings, each ring a list of [lon, lat] pairs)
    from a GeoJSON Polygon or MultiPolygon geometry."""
    if not geom:
        return []
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    parts = []
    if gtype == "Polygon":
        raw_parts = [coords]
    elif gtype == "MultiPolygon":
        raw_parts = coords
    else:
        return []
    for part in raw_parts:
        rings = []
        for ring in part:
            if len(ring) >= 4:
                pts = [[round(float(p[0]), 7), round(float(p[1]), 7)] for p in ring]
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                rings.append(pts)
        if rings:
            parts.append(rings)
    return parts


def _extract_lines(geom):
    """Extract a list of polylines (each a list of [lon, lat] pairs) from LineString or MultiLineString."""
    if not geom:
        return []
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "LineString":
        raw = [coords]
    elif gtype == "MultiLineString":
        raw = coords
    else:
        return []
    out = []
    for line in raw:
        if len(line) >= 2:
            out.append([[round(float(p[0]), 7), round(float(p[1]), 7)] for p in line])
    return out


def _build_features(poly_records):
    """Convert [(crop, parts_list), ...] into a mapgeom.Features table for earthfast.mask_polys."""
    if not poly_records:
        z_i = np.zeros(1, np.int64)
        return mapgeom.Features(
            layer="ign_fr",
            ids=np.zeros(0, dtype="U1"),
            kind=np.zeros(0, dtype=np.uint8),
            part_off=z_i,
            ring_off=z_i.copy(),
            coord_off=z_i.copy(),
            lonlat=np.zeros((0, 2), dtype=np.float64),
            attrs={"crop": np.zeros(0, dtype="U1")},
        )
    ids = []
    crops = []
    part_off = [0]
    ring_off = [0]
    coord_off = [0]
    coords = []
    for idx, (crop, parts) in enumerate(poly_records):
        ids.append(f"fr_{idx}")
        crops.append(crop)
        for rings in parts:
            for ring in rings:
                arr = np.asarray(ring, dtype=np.float64)
                coords.append(arr)
                coord_off.append(coord_off[-1] + len(arr))
            ring_off.append(len(coord_off) - 1)
        part_off.append(len(ring_off) - 1)
    lonlat = np.vstack(coords) if coords else np.zeros((0, 2), dtype=np.float64)
    return mapgeom.Features(
        layer="ign_fr",
        ids=np.asarray(ids),
        kind=np.full(len(ids), mapgeom.POLYGON, dtype=np.uint8),
        part_off=np.asarray(part_off, dtype=np.int64),
        ring_off=np.asarray(ring_off, dtype=np.int64),
        coord_off=np.asarray(coord_off, dtype=np.int64),
        lonlat=np.ascontiguousarray(lonlat, dtype=np.float64),
        attrs={"crop": np.asarray(crops)},
    )


def _bdtopo_features(records):
    """Build a mapgeom.Features('buildings', ...) table from unmatched BDTOPO_V3:batiment records."""
    ids = []
    part_off = [0]
    ring_off = [0]
    coord_off = [0]
    coords = []
    heights, floors, roof_heights = [], [], []
    classes, roof_shapes, roof_mats, facade_mats = [], [], [], []
    for b in records:
        ids.append(f"~ign:{b['id']}")
        heights.append(b.get("h"))
        floors.append(b.get("fl"))
        roof_heights.append(b.get("rh"))
        classes.append(b.get("cl") or "residential")
        rs_v = "gabled" if b.get("rm") in ("tile", "slate") else (b.get("rs") or ("gabled" if (b.get("rh") or 0) >= 0.6 else ""))
        roof_shapes.append(rs_v)
        roof_mats.append(b.get("rm") or "")
        wm_v = b.get("wm") or ""
        if wm_v in ("brick", "concrete"):
            wm_v = "stucco"
        facade_mats.append(wm_v)
        for rings in b["p"]:
            for ring in rings:
                arr = np.asarray(ring, dtype=np.float64)
                coords.append(arr)
                coord_off.append(coord_off[-1] + len(arr))
            ring_off.append(len(coord_off) - 1)
        part_off.append(len(ring_off) - 1)
    n = len(ids)
    lonlat = np.vstack(coords) if coords else np.zeros((0, 2), dtype=np.float64)
    return mapgeom.Features(
        layer="buildings",
        ids=mapgeom.text_column(ids),
        kind=np.full(n, mapgeom.POLYGON, dtype=np.uint8),
        part_off=np.asarray(part_off, dtype=np.int64),
        ring_off=np.asarray(ring_off, dtype=np.int64),
        coord_off=np.asarray(coord_off, dtype=np.int64),
        lonlat=np.ascontiguousarray(lonlat, dtype=np.float64),
        own=np.ones(n, dtype=bool),
        clipped=np.zeros(n, dtype=bool),
        attrs={
            "subtype": mapgeom.text_column(["building"] * n),
            "class": mapgeom.text_column(classes),
            "name": mapgeom.text_column([""] * n),
            "src_dataset": mapgeom.text_column(["IGN_BDTOPO"] * n),
            "src_record": mapgeom.text_column(ids),
            "roof_shape": mapgeom.text_column(roof_shapes),
            "roof_orientation": mapgeom.text_column([""] * n),
            "roof_material": mapgeom.text_column(roof_mats),
            "roof_color": mapgeom.text_column([""] * n),
            "facade_material": mapgeom.text_column(facade_mats),
            "facade_color": mapgeom.text_column([""] * n),
            "height_src": mapgeom.text_column(["ign_bdtopo"] * n),
            "height": mapgeom.float_column(heights),
            "min_height": mapgeom.float_column([None] * n),
            "num_floors": mapgeom.float_column(floors),
            "num_floors_underground": mapgeom.float_column([None] * n),
            "min_floor": mapgeom.float_column([None] * n),
            "level": mapgeom.float_column([None] * n),
            "roof_height": mapgeom.float_column(roof_heights),
            "roof_direction": mapgeom.float_column([None] * n),
            "is_underground": np.zeros(n, dtype=bool),
            "has_parts": np.zeros(n, dtype=bool),
        },
    )


def _cell_from_dict(d):
    """Build FrCell from decoded JSON dict {"polys": [[crop, parts], ...], "hedges": [[pts, h, w], ...]}."""
    polys = _build_features(d.get("polys") or [])
    hedges = []
    for pts, h, w in d.get("hedges") or []:
        arr = np.asarray(pts, dtype=np.float64)
        if len(arr) >= 2:
            hedges.append((arr, float(h), float(w)))
    return FrCell(polys=polys, hedges=hedges)


class IgnFr:
    """Fetches and caches IGN RPG & BD TOPO vegetation/hedgerows per z14 cell."""

    def __init__(self, cache_dir=None, fetcher=None):
        if cache_dir:
            norm = os.path.normpath(str(cache_dir))
            if os.path.basename(norm) == "imagery":
                norm = os.path.dirname(norm)
            self.dir = os.path.join(norm, "ign_fr", f"v{VERSION}")
            self.bld_dir = os.path.join(norm, "ign_fr", f"bld_v{VERSION}")
        else:
            self.dir = None
            self.bld_dir = None
        self.fetcher = fetcher
        self._mem = OrderedDict()
        self._bld_mem = OrderedDict()
        self._lock = threading.Lock()
        self._cell_locks = {}
        self._bld_locks = {}

    def _path(self, z, x, y):
        if not self.dir:
            return None
        return os.path.join(self.dir, str(z), str(x), f"{y}.json.gz")

    def _bld_path(self, z, x, y):
        if not self.bld_dir:
            return None
        return os.path.join(self.bld_dir, str(z), str(x), f"{y}.json.gz")

    def cached_cell(self, key):
        """Return the FrCell for key if already in memory or on disk (never touches the network)."""
        k = (int(key[0]), int(key[1]), int(key[2]))
        with self._lock:
            if k in self._mem:
                self._mem.move_to_end(k)
                return self._mem[k]
        p = self._path(*k)
        if p and os.path.exists(p):
            try:
                with gzip.open(p, "rt", encoding="utf-8") as fh:
                    cell = _cell_from_dict(json.load(fh))
                with self._lock:
                    self._mem[k] = cell
                    while len(self._mem) > MEM_CELLS:
                        self._mem.popitem(last=False)
                return cell
            except Exception as e:
                log.debug("ign_fr read %s: %s", p, e)
        return None

    def cell(self, key, bounds=None, kind="demand"):
        """Return FrCell for z14 cell key, fetching from IGN WFS if needed; None outside France or offline."""
        if not ON:
            return None
        ck = mapcells.CellKey(int(key[0]), int(key[1]), int(key[2]))
        if bounds is None:
            bounds = ck.bounds()
        if not cell_in_france(bounds):
            return None
        k = (ck.z, ck.x, ck.y)
        with self._lock:
            if k in self._mem:
                self._mem.move_to_end(k)
                return self._mem[k]
            lock = self._cell_locks.setdefault(k, threading.Lock())
        with lock:
            got = self.cached_cell(k)
            if got is not None:
                return got
            if self.fetcher is None:
                return None
            raw, complete = self._fetch_cell(bounds, kind=kind)
            if raw is None:
                return None
            cell = _cell_from_dict(raw)
            p = self._path(*k)
            if p and complete:
                try:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    tmp = staging.tmp_for(p)
                    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                        json.dump(raw, fh, separators=(",", ":"))
                    staging.replace_cached(tmp, p)
                except Exception as e:
                    log.debug("ign_fr write %s: %s", p, e)
            with self._lock:
                self._mem[k] = cell
                while len(self._mem) > MEM_CELLS:
                    self._mem.popitem(last=False)
            return cell

    def _wfs_get(self, typename, bbox_str, count=2000, kind="demand"):
        url = (
            f"{WFS_URL}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
            f"&TYPENAMES={typename}&BBOX={bbox_str},CRS:84&SRSNAME=CRS:84"
            f"&OUTPUTFORMAT=application/json&COUNT={count}"
        )
        res = self.fetcher.get(url, kind=kind, lane=LANE)
        return json.loads(res.body.decode("utf-8"))

    def _fetch_cell(self, bounds, kind="demand"):
        lon0, lat0, lon1, lat1 = (float(v) for v in bounds)
        bbox_str = f"{lon0:.6f},{lat0:.6f},{lon1:.6f},{lat1:.6f}"
        polys = []
        hedges = []
        ok_count = 0

        # 1. RPG parcels (vineyards, lavender fields, olive groves, truffle oaks)
        try:
            rpg = self._wfs_get("RPG.LATEST:parcelles_graphiques", bbox_str, count=2000, kind=kind)
            ok_count += 1
            for feat in rpg.get("features") or []:
                crop = _classify_rpg(feat.get("properties"))
                if crop:
                    parts = _extract_rings(feat.get("geometry"))
                    if parts:
                        polys.append([crop, parts])
        except Exception as e:
            log.debug("ign_fr RPG %s: %s: %s", bbox_str, type(e).__name__, e)

        # 2. BD TOPO linear hedgerows (Mistral cypress windbreaks)
        try:
            haie = self._wfs_get("BDTOPO_V3:haie", bbox_str, count=2000, kind=kind)
            ok_count += 1
            for feat in haie.get("features") or []:
                props = feat.get("properties") or {}
                h_val = props.get("hauteur")
                w_val = props.get("largeur")
                h_m = float(h_val) if h_val is not None else 0.0
                w_m = float(w_val) if w_val is not None else 2.0
                for line in _extract_lines(feat.get("geometry")):
                    hedges.append([line, h_m, w_m])
        except Exception as e:
            log.debug("ign_fr haie %s: %s: %s", bbox_str, type(e).__name__, e)

        # 3. BD TOPO zone_de_vegetation (non-CAP vineyards and mapped hedgerow polygons)
        try:
            zv = self._wfs_get("BDTOPO_V3:zone_de_vegetation", bbox_str, count=2000, kind=kind)
            ok_count += 1
            for feat in zv.get("features") or []:
                props = feat.get("properties") or {}
                nat = str(props.get("nature") or "").strip()
                if nat == "Vigne":
                    parts = _extract_rings(feat.get("geometry"))
                    if parts:
                        polys.append(["vineyard", parts])
        except Exception as e:
            log.debug("ign_fr zone_de_vegetation %s: %s: %s", bbox_str, type(e).__name__, e)

        if ok_count == 0:
            return None, False
        return {"polys": polys, "hedges": hedges}, (ok_count == 3)

    def buildings_cell(self, key, bounds=None, kind="demand"):
        """Return list of BD TOPO building dicts for z14 cell key, cached under <cache>/ign_fr/bld_v1/<z>/<x>/<y>.json.gz."""
        if not ON:
            return None
        ck = mapcells.CellKey(int(key[0]), int(key[1]), int(key[2]))
        if bounds is None:
            bounds = ck.bounds()
        if not cell_in_france(bounds):
            return None
        k = (ck.z, ck.x, ck.y)
        with self._lock:
            if k in self._bld_mem:
                self._bld_mem.move_to_end(k)
                return self._bld_mem[k]
            lock = self._bld_locks.setdefault(k, threading.Lock())
        with lock:
            with self._lock:
                if k in self._bld_mem:
                    self._bld_mem.move_to_end(k)
                    return self._bld_mem[k]
            p = self._bld_path(*k)
            if p and os.path.exists(p):
                try:
                    with gzip.open(p, "rt", encoding="utf-8") as fh:
                        blds = json.load(fh)
                    with self._lock:
                        self._bld_mem[k] = blds
                        while len(self._bld_mem) > MEM_CELLS:
                            self._bld_mem.popitem(last=False)
                    return blds
                except Exception as e:
                    log.debug("ign_fr bld read %s: %s", p, e)
            if self.fetcher is None:
                return None
            blds = self._fetch_buildings(bounds, kind=kind)
            if blds is None:
                return None
            if p:
                try:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    tmp = staging.tmp_for(p)
                    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                        json.dump(blds, fh, separators=(",", ":"))
                    staging.replace_cached(tmp, p)
                except Exception as e:
                    log.debug("ign_fr bld write %s: %s", p, e)
            with self._lock:
                self._bld_mem[k] = blds
                while len(self._bld_mem) > MEM_CELLS:
                    self._bld_mem.popitem(last=False)
            return blds

    def _fetch_buildings(self, bounds, kind="demand"):
        lon0, lat0, lon1, lat1 = (float(v) for v in bounds)
        bbox_str = f"{lon0:.6f},{lat0:.6f},{lon1:.6f},{lat1:.6f}"
        cols = (
            "geometrie,cleabs,nature,usage_1,construction_legere,nombre_d_etages,"
            "materiaux_des_murs,materiaux_de_la_toiture,hauteur,altitude_minimale_toit,altitude_maximale_toit"
        )
        url = (
            f"{WFS_URL}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
            f"&TYPENAMES=BDTOPO_V3:batiment&PROPERTYNAME={cols}"
            f"&BBOX={bbox_str},CRS:84&SRSNAME=CRS:84"
            f"&OUTPUTFORMAT=application/json&COUNT=5000"
        )
        try:
            res = self.fetcher.get(url, kind=kind, lane=LANE)
            data = json.loads(res.body.decode("utf-8"))
        except Exception as e:
            log.debug("ign_fr batiment %s: %s: %s", bbox_str, type(e).__name__, e)
            return None
        out = []
        for idx, feat in enumerate(data.get("features") or []):
            parts = _extract_rings(feat.get("geometry"))
            if not parts or not parts[0] or len(parts[0][0]) < 4:
                continue
            ring = parts[0][0][:-1]
            n_pts = len(ring)
            clon = sum(pt[0] for pt in ring) / n_pts
            clat = sum(pt[1] for pt in ring) / n_pts
            props = feat.get("properties") or {}
            cleabs = str(props.get("cleabs") or f"b{idx}")
            h_raw = props.get("hauteur")
            h_m = round(float(h_raw), 2) if h_raw is not None and float(h_raw) > 0.5 else None
            fl_raw = props.get("nombre_d_etages")
            fl = int(fl_raw) if fl_raw is not None and int(fl_raw) > 0 else None
            z_min_t = props.get("altitude_minimale_toit")
            z_max_t = props.get("altitude_maximale_toit")
            rh, rs = None, ""
            if z_min_t is not None and z_max_t is not None:
                dh = float(z_max_t) - float(z_min_t)
                if 0.0 <= dh < 0.6:
                    rh, rs = 0.0, "flat"
                elif 0.6 <= dh <= 14.0:
                    rh = round(dh, 2)
            wm = _parse_wall_mat(props.get("materiaux_des_murs"))
            rm = _parse_roof_mat(props.get("materiaux_de_la_toiture"))
            if not rs and (rm in ("tile", "slate") or (rh is not None and rh >= 0.6)):
                rs = "gabled"
            cl = _parse_bld_class(props)
            out.append({
                "id": cleabs,
                "c": [round(clon, 7), round(clat, 7)],
                "h": h_m,
                "fl": fl,
                "rh": rh,
                "rs": rs,
                "wm": wm,
                "rm": rm,
                "cl": cl,
                "p": parts,
            })
        return out

    def join_buildings(self, feats, key, frame=None, kind="demand"):
        """Enrich Overture building footprints in France with IGN BDTOPO_V3:batiment 3D heights, storeys,
        roof/wall materials, and append any unmatched rural French buildings."""
        if not ON or feats is None or feats.__dict__.get("_ign_bld_joined"):
            return feats
        ck = mapcells.CellKey(int(key[0]), int(key[1]), int(key[2]))
        bounds = ck.bounds()
        if not cell_in_france(bounds):
            feats.__dict__["_ign_bld_joined"] = True
            return feats
        blds = self.buildings_cell(ck, bounds=bounds, kind=kind)
        if not blds:
            feats.__dict__["_ign_bld_joined"] = True
            return feats

        lon0, lat0, lon1, lat1 = (float(v) for v in bounds)
        lat_c = 0.5 * (lat0 + lat1)
        lon_c = 0.5 * (lon0 + lon1)
        mx = 111320.0 * math.cos(math.radians(lat_c))
        my = 111320.0
        cell_m = 16.0

        # Spatial hash of BD TOPO building centroids
        grid = {}
        bd_xy = []
        for j, b in enumerate(blds):
            bx = (float(b["c"][0]) - lon_c) * mx
            by = (float(b["c"][1]) - lat_c) * my
            bd_xy.append((bx, by))
            gx, gy = int(math.floor(bx / cell_m)), int(math.floor(by / cell_m))
            grid.setdefault((gx, gy), []).append(j)

        n = len(feats)
        matched_bd = [False] * len(blds)
        a = feats.attrs

        if n > 0:
            height = np.array(a["height"], dtype=np.float32, copy=True) if "height" in a else np.full(n, np.nan, np.float32)
            num_floors = np.array(a["num_floors"], dtype=np.float32, copy=True) if "num_floors" in a else np.full(n, np.nan, np.float32)
            roof_height = np.array(a["roof_height"], dtype=np.float32, copy=True) if "roof_height" in a else np.full(n, np.nan, np.float32)
            roof_shape = [str(x) for x in a["roof_shape"]] if "roof_shape" in a else [""] * n
            facade_mat = [str(x) for x in a["facade_material"]] if "facade_material" in a else [""] * n
            roof_mat = [str(x) for x in a["roof_material"]] if "roof_material" in a else [""] * n
            b_class = [str(x) for x in a["class"]] if "class" in a else [""] * n
            height_src = [str(x) for x in a["height_src"]] if "height_src" in a else [""] * n
            src_ds = [str(x) for x in a["src_dataset"]] if "src_dataset" in a else ["OpenStreetMap"] * n
            has_parts = a.get("has_parts")
            clipped_col = getattr(feats, "clipped", None)

            ll = feats.lonlat
            p_off = feats.part_off
            r_off = feats.ring_off
            c_off = feats.coord_off

            pairs = []
            for i in range(n):
                p0, p1 = int(p_off[i]), int(p_off[i + 1])
                if p1 > p0:
                    r0 = int(r_off[p0])
                    if int(r_off[p0 + 1]) > r0:
                        c0, c1 = int(c_off[r0]), int(c_off[r0 + 1])
                        if c1 - c0 >= 4:
                            pts = ll[c0:c1 - 1]
                            olon = float(pts[:, 0].mean())
                            olat = float(pts[:, 1].mean())
                            ox = (olon - lon_c) * mx
                            oy = (olat - lat_c) * my
                            gx, gy = int(math.floor(ox / cell_m)), int(math.floor(oy / cell_m))
                            for dx in (-1, 0, 1):
                                for dy in (-1, 0, 1):
                                    for j in grid.get((gx + dx, gy + dy), ()):
                                        bx, by = bd_xy[j]
                                        d2 = (ox - bx) ** 2 + (oy - by) ** 2
                                        if d2 <= 100.0:  # 10.0 m match radius squared
                                            pairs.append((d2, i, j))
            pairs.sort()
            best_j_for_i = [-1] * n
            for _d2, i_cand, j_cand in pairs:
                if best_j_for_i[i_cand] < 0 and not matched_bd[j_cand]:
                    best_j_for_i[i_cand] = j_cand
                    matched_bd[j_cand] = True

            new_p_off = [0]
            new_r_off = [0]
            new_c_off = [0]
            new_coords = []
            geom_replaced = False

            for i in range(n):
                p0, p1 = int(p_off[i]), int(p_off[i + 1])
                best_j = best_j_for_i[i]
                hp = bool(has_parts[i]) if has_parts is not None else False
                cl_flag = bool(clipped_col[i]) if clipped_col is not None else False
                use_ign_geom = False
                if best_j >= 0:
                    b = blds[best_j]
                    if b.get("h") is not None and (not np.isfinite(height[i]) or height_src[i] != "OpenStreetMap"):
                        height[i] = float(b["h"])
                        height_src[i] = "ign_bdtopo"
                    if b.get("fl") is not None and not np.isfinite(num_floors[i]):
                        num_floors[i] = float(b["fl"])
                    if b.get("rh") is not None and not np.isfinite(roof_height[i]):
                        roof_height[i] = float(b["rh"])
                    wm_val = b.get("wm") or ""
                    if wm_val in ("brick", "concrete"):
                        wm_val = "stucco"
                    if wm_val and not facade_mat[i]:
                        facade_mat[i] = str(wm_val)
                    if b.get("rm") and not roof_mat[i]:
                        roof_mat[i] = str(b["rm"])
                    rs_val = "gabled" if b.get("rm") in ("tile", "slate") else (b.get("rs") or ("gabled" if (b.get("rh") or 0) >= 0.6 else ""))
                    if rs_val and (not roof_shape[i] or (roof_shape[i] == "flat" and b.get("rm") in ("tile", "slate"))):
                        roof_shape[i] = str(rs_val)
                    cl_val = b.get("cl") or "residential"
                    if cl_val and not b_class[i]:
                        b_class[i] = str(cl_val)
                    if not hp and not cl_flag and b.get("p"):
                        use_ign_geom = True
                        geom_replaced = True
                        src_ds[i] = "IGN_BDTOPO"
                elif len(blds) >= 5 and not hp and not cl_flag:
                    # Unmatched/duplicate Overture or shifted OSM footprint in an IGN BDTOPO-covered cell:
                    # suppress it (< FLAT_FOOTPRINT_M) because all unmatched BD TOPO buildings are appended below.
                    height[i] = 0.01

                if use_ign_geom:
                    for rings in blds[best_j]["p"]:
                        for ring in rings:
                            arr = np.asarray(ring, dtype=np.float64)
                            new_coords.append(arr)
                            new_c_off.append(new_c_off[-1] + len(arr))
                        new_r_off.append(len(new_c_off) - 1)
                    new_p_off.append(len(new_r_off) - 1)
                else:
                    for p_idx in range(p0, p1):
                        for r_idx in range(int(r_off[p_idx]), int(r_off[p_idx + 1])):
                            arr = ll[int(c_off[r_idx]):int(c_off[r_idx + 1])]
                            new_coords.append(arr)
                            new_c_off.append(new_c_off[-1] + len(arr))
                        new_r_off.append(len(new_c_off) - 1)
                    new_p_off.append(len(new_r_off) - 1)

            a["height"] = height
            a["num_floors"] = num_floors
            a["roof_height"] = roof_height
            a["roof_shape"] = mapgeom.text_column(roof_shape)
            a["facade_material"] = mapgeom.text_column(facade_mat)
            a["roof_material"] = mapgeom.text_column(roof_mat)
            a["class"] = mapgeom.text_column(b_class)
            a["height_src"] = mapgeom.text_column(height_src)
            if "src_dataset" in a:
                a["src_dataset"] = mapgeom.text_column(src_ds)
            if geom_replaced:
                feats.part_off = np.asarray(new_p_off, dtype=np.int64)
                feats.ring_off = np.asarray(new_r_off, dtype=np.int64)
                feats.coord_off = np.asarray(new_c_off, dtype=np.int64)
                feats.lonlat = np.ascontiguousarray(np.vstack(new_coords) if new_coords else np.zeros((0, 2), np.float64), dtype=np.float64)
                feats.__dict__.pop("_xy_in", None)
                feats.__dict__.pop("_centres", None)
                feats.__dict__.pop("_boxes", None)
                if frame is not None:
                    feats.localize(frame)

        # Append unmatched rural French buildings whose centroids lie inside this cell
        unmatched = [
            b for j, b in enumerate(blds)
            if not matched_bd[j] and lon0 <= float(b["c"][0]) < lon1 and lat0 <= float(b["c"][1]) < lat1
        ]
        if unmatched:
            unmatched.sort(key=lambda b: f"~ign:{b['id']}")
            extra = _bdtopo_features(unmatched)
            feats = mapgeom.concat(feats, extra)
            if frame is not None:
                feats.localize(frame)

        feats.__dict__["_ign_bld_joined"] = True
        return feats

    def tint_lavender_rgb(self, rgb, frame, x0, y0, size):
        """Apply a subtle Provençal summer lavender bloom & furrow stripe modulation to terrain texels inside
        IGN RPG 'lavender' parcels (visible at all LOD rings and even when aerial imagery was flown post-harvest)."""
        if not ON or rgb is None or rgb.ndim != 3:
            return rgb
        # Check whether the tile is in France
        clat, clon, _ = frame.enu_to_geodetic(x0 + size * 0.5, y0 + size * 0.5, 0.0)
        if not in_france(float(clat), float(clon)):
            return rgb
        la, lo, _ = frame.enu_to_geodetic(
            np.array([x0, x0 + size, x0, x0 + size]),
            np.array([y0, y0, y0 + size, y0 + size]),
            np.zeros(4),
        )
        try:
            keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()), max_cells=16)
        except ValueError:
            return rgb

        lav_rings = []
        for k in keys:
            cell = self.cached_cell(k)
            if cell is None or not len(cell.polys):
                continue
            crops = cell.polys.attrs.get("crop")
            if crops is None:
                continue
            for idx in np.flatnonzero(crops == "lavender"):
                for part in cell.polys.parts(int(idx)):
                    if part and len(part[0]) >= 4:
                        lav_rings.append(part[0])
        if not lav_rings:
            return rgb

        px = rgb.shape[0]
        step = size / px
        # Mercator grid matching chm mask_polys convention (row 0 = north = y0 + size)
        import fast
        from matplotlib.path import Path
        xs = x0 + (np.arange(px) + 0.5) * step
        ys = (y0 + size) - (np.arange(px) + 0.5) * step
        out = None
        for ring_lonlat in lav_rings:
            e, n, _ = frame.geodetic_to_enu(ring_lonlat[:, 1], ring_lonlat[:, 0], 0.0)
            ring_en = np.column_stack([e, n])
            minx, miny = ring_en.min(axis=0)
            maxx, maxy = ring_en.max(axis=0)
            if maxx < x0 or minx > x0 + size or maxy < y0 or miny > y0 + size:
                continue
            c0 = max(0, int(math.floor((minx - x0) / step)))
            c1 = min(px, int(math.ceil((maxx - x0) / step)) + 1)
            r0 = max(0, int(math.floor((y0 + size - maxy) / step)))
            r1 = min(px, int(math.ceil((y0 + size - miny) / step)) + 1)
            if c1 <= c0 or r1 <= r0:
                continue
            XX, YY = np.meshgrid(xs[c0:c1], ys[r0:r1])
            pts = np.column_stack([XX.ravel(), YY.ravel()])
            inside = Path(ring_en).contains_points(pts).reshape(XX.shape)
            if not inside.any():
                continue
            if out is None:
                out = rgb.copy()
            # Compute furrow orientation using oriented_box_core if available
            vx, vy = 0.0, 1.0
            try:
                best = fast._ef.oriented_box_core(np.ascontiguousarray(ring_en, dtype=np.float64))
                if best is not None:
                    ux, uy, bvx, bvy, amin, amax, bmin, bmax = best
                    if (bmax - bmin) > (amax - amin):
                        vx, vy = ux, uy
                    else:
                        vx, vy = bvx, bvy
            except Exception:
                pass
            # Furrow wave across v (1.5m spacing when texels are fine, smooth bloom at coarse texels)
            cross = XX * vx + YY * vy
            furrow = 0.5 + 0.5 * np.cos(2.0 * math.pi * cross / 1.55) if step <= 0.8 else np.full_like(XX, 0.65)
            patch = out[r0:r1, c0:c1].astype(np.float32)
            lum = 0.299 * patch[..., 0] + 0.587 * patch[..., 1] + 0.114 * patch[..., 2]
            # Target Provençal lavender bloom colour modulated by existing luminance & furrow crest
            scale_l = np.clip(lum / 135.0, 0.55, 1.25)
            target_r = (108.0 + 30.0 * furrow) * scale_l
            target_g = (84.0 + 14.0 * furrow) * scale_l
            target_b = (152.0 + 48.0 * furrow) * scale_l
            alpha = (0.38 + 0.18 * furrow)[..., None]
            blended = patch * (1.0 - alpha) + np.stack([target_r, target_g, target_b], axis=-1) * alpha
            out[r0:r1, c0:c1] = np.where(inside[..., None], np.clip(np.rint(blended), 0, 255).astype(np.uint8), out[r0:r1, c0:c1])
        return out if out is not None else rgb

