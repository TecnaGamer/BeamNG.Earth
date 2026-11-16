"""Soviet and post-Soviet housing (plan 048): thin glue around the kernel's `earthfast.cis_classify` (native/earthfast/src/
cisbld, kernel 290). The rules (which kind a building is, where its entrances are) are all Rust; here: which countries,
what the map says of each building (floors, height, year, material, class), the roads as segments, and the kind's
facade for buildings.plan until the facade cells of stage C replace it.

BNE_CIS_BUILDINGS=off switches it off (on by default).
"""
from functools import lru_cache
import logging
import os

import numpy as np

import fast

log = logging.getLogger("cisbuildings")

ON = os.environ.get("BNE_CIS_BUILDINGS", "on") != "off"
KERNEL = 290

# country code of the kernel's attrs (0 other)
COUNTRY = {"UA": 1, "BY": 2, "RU": 3, "MD": 4, "KZ": 5, "UZ": 6, "KG": 6, "TJ": 6, "TM": 6, "AZ": 6, "AM": 6, "GE": 6,
           "LV": 7, "LT": 7, "EE": 7}
# coarse boxes (country, lat0, lat1, lon0, lon1) used only where the border data is not there (offline): the cores of
# the countries, kept clear of their neighbours (Poland, Romania, Finland, Mongolia, China); the order matters. The real
# answer is countrybounds' (regionaldata fetches it on demand).
_BOXES = (("EE", 58.0, 59.6, 23.0, 27.9), ("LV", 56.2, 57.7, 21.5, 27.5), ("LT", 54.5, 56.2, 21.3, 26.3),
          ("BY", 51.6, 56.0, 24.0, 32.0), ("MD", 46.3, 48.0, 28.0, 29.7),
          ("UA", 48.3, 51.3, 24.1, 40.0), ("UA", 45.6, 48.3, 29.8, 40.0), ("UA", 44.3, 47.1, 32.0, 40.0),
          ("KZ", 46.5, 54.0, 50.0, 80.0), ("KZ", 43.0, 45.5, 70.0, 80.0),
          ("RU", 50.0, 60.0, 28.4, 87.0), ("RU", 60.0, 70.0, 32.0, 180.0), ("RU", 52.5, 72.0, 87.0, 120.0),
          ("RU", 54.0, 72.0, 120.0, 180.0))
# kind names, by the kernel's codes (classify.rs)
KITS = ("none", "house_cis", "stalinka", "khrush_brick", "khrush_panel", "panel9", "panel16", "tower", "brick_mid",
        "late_soviet", "new_mono", "new_low", "new_panel")
COLUMNS = ("kit", "soviet", "conf", "floors", "l", "w", "ux", "uy", "road_side", "entrances", "tech", "serial",
           "section_m", "cx", "cy", "arm")
# kind -> 1 apartment blocks of the old series, used by the skin below
_PANEL_KITS = {"khrush_panel", "panel9", "panel16", "late_soviet", "new_panel"}
_BRICK_KITS = {"khrush_brick", "brick_mid", "tower"}

_MATERIAL = {"concrete": 1, "precast": 1, "panel": 1, "reinforced_concrete": 1, "cement": 1,
             "brick": 2, "stone": 2,
             "glass": 3, "metal": 3, "steel": 3, "aluminium": 3,
             "plaster": 4, "stucco": 4, "render": 4, "wood": 4, "block": 4, "cement_block": 4}
_NOT_HOUSING = {"commercial", "retail", "office", "industrial", "warehouse", "supermarket", "school", "kindergarten",
                "hospital", "church", "chapel", "cathedral", "civic", "public", "government", "hotel", "garage",
                "garages", "parking", "service", "shed", "barn", "farm_auxiliary", "transportation", "train_station",
                "hangar", "stadium", "university", "college", "fire_station", "kiosk", "restaurant", "mosque",
                "synagogue", "temple", "monastery", "greenhouse", "silo", "storage_tank", "roof", "carport", "hut"}
_HOUSE = {"house", "detached", "semidetached_house", "bungalow", "cabin", "residential_house", "farm", "static_caravan"}


@lru_cache(maxsize=32768)
def _country_cell(ilat, ilon):
    lat, lon = ilat / 100.0, ilon / 100.0
    code = None
    try:
        import countrybounds
        code = countrybounds.bundled().at(lat, lon)
    except Exception:       # (no border data, offline)
        code = None
    if code is None:
        for c, la0, la1, lo0, lo1 in _BOXES:
            if la0 <= lat <= la1 and lo0 <= lon <= lo1:
                code = c
                break
    return code


def country(lat, lon):
    """The ISO country code at a point where it is one of the post-Soviet countries, else None."""
    if lat is None or lon is None or not (np.isfinite(lat) and np.isfinite(lon)):
        return None
    code = _country_cell(int(round(lat * 100.0)), int(round(lon * 100.0)))
    return code if code in COUNTRY else None


def active(lat, lon):
    return ON and country(lat, lon) is not None


def _road_segments(roads_xy):
    segs = []
    for ln in roads_xy or ():
        a = np.asarray(ln, np.float64)
        if a.ndim == 2 and len(a) >= 2:
            segs.append(np.concatenate([a[:-1, :2], a[1:, :2]], axis=1))
    return np.concatenate(segs) if segs else np.zeros((0, 4))


def attr_row(at, shared, code):
    """One footprint's attrs for the kernel (floors, height, year, material, obat, class, touching, country)."""
    def num(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return float("nan")
        return v if np.isfinite(v) else float("nan")
    cls = str(at.get("class") or "").lower()
    klass = 2 if cls in _NOT_HOUSING else 1 if cls in _HOUSE else 0
    year = num(at.get("_year"))
    if not np.isfinite(year):
        year = num(str(at.get("start_date") or "")[:4] or None)
    return (num(at.get("num_floors")), num(at.get("building_height") if at.get("building_height") is not None
                                           else at.get("height")), year,
            _MATERIAL.get(str(at.get("facade_material") or "").lower(), 0), int(at.get("_obat") or 0), klass,
            int(shared), code)


def classify_tile(rings, attrs, shared, roads_xy, clat, clon):
    """One kernel call for the tile's footprints: the verdict rows (n, len(COLUMNS)) or None where the tile is not in a
    post-Soviet country or the kernel is older. Only footprints of the part that stands on the ground (not the parts of a
    building drawn in bands) should be passed: the caller marks the others with kit 0 afterwards."""
    if not ON or not len(rings):
        return None
    code = COUNTRY.get(country(clat, clon) or "", 0)
    if code == 0 or getattr(fast, "_ef", None) is None or not hasattr(fast._ef, "cis_classify"):
        return None
    ends = np.cumsum([len(r) for r in rings]).astype(np.int64)
    starts = np.concatenate(([0], ends[:-1])).astype(np.int64)
    xy = np.ascontiguousarray(np.concatenate([np.asarray(r, np.float64)[:, :2] for r in rings]))
    rows = np.array([attr_row(at, sh, code) for at, sh in zip(attrs, shared)], np.float64).reshape(-1, 8)
    return np.asarray(fast._ef.cis_classify(xy, starts, ends, rows, np.ascontiguousarray(_road_segments(roads_xy))),
                      np.float64)


def info(row):
    """A kernel row as the dict plan() keeps in the building's attrs (None where the kernel left it alone)."""
    kit = int(row[0])
    if kit <= 0:
        return None
    d = dict(zip(COLUMNS, (float(v) for v in row)))
    d["kit"] = KITS[kit] if kit < len(KITS) else "none"
    d["soviet"] = bool(row[1])
    d["floors"] = int(row[3])
    d["entrances"] = int(row[9])
    d["tech"] = bool(row[10])
    return d


# ----------------------------------------------------------------------------------------- the facade and its modules
# (plan 048 stages C and D: the kernel cuts the walls into the atlas's cells and stamps the art's modules, cisbld/)
MATERIALS = ("bne_cis_khrush_panel", "bne_cis_khrush_brick", "bne_cis_panel9", "bne_cis_panel16", "bne_cis_new_mono",
             "bne_cis_common", "bne_cis_roof", "bne_cis_concrete", "bne_cis_brick", "bne_cis_silicate", "bne_cis_wood",
             "bne_cis_pvc", "bne_cis_glass", "bne_cis_frosted", "bne_cis_graphite", "bne_cis_sheet_brown", "bne_cis_lamp")
# kind -> the atlas its facade is cut from (None: the older facade code draws it); new_low and new_panel use the modern
# atlas until theirs (A8, A9) are there
KIT_ATLAS = {"khrush_panel": "khrush_panel", "khrush_brick": "khrush_brick", "panel9": "panel9", "panel16": "panel16",
             "tower": "panel16", "brick_mid": "khrush_brick", "late_soviet": "panel9", "new_mono": "new_mono",
             "new_panel": "new_mono", "new_low": "new_mono"}
# kind -> the kind its modules are picked for (the module table's `fits`)
MODULE_KIT = {"tower": "panel16", "late_soviet": "panel9", "new_panel": "new_mono", "new_low": "new_mono"}
KERNEL_FACADE = 290

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


@lru_cache(maxsize=1)
def _atlas_texts():
    """{kit: the cells.json text} of server/data/cis_atlases.json (tools/build_cis_data.py), {} when it is not there."""
    import json
    try:
        with open(os.path.join(_DATA, "cis_atlases.json"), encoding="utf-8") as f:
            return {k: json.dumps(v, separators=(",", ":"), sort_keys=True) for k, v in json.load(f).items()}
    except (OSError, ValueError):
        log.warning("cis_atlases.json missing: the post-Soviet blocks keep the older facades")
        return {}


@lru_cache(maxsize=1)
def _modules_text():
    try:
        with open(os.path.join(_DATA, "cis_modules.json"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def drawn_kit(kit):
    """Whether blocks of this kind are drawn by draw_walls (the kernel and the art's data are there)."""
    ef = getattr(fast, "_ef", None)
    return bool(ON and kit in KIT_ATLAS and ef is not None and hasattr(ef, "cis_facade")
                and KIT_ATLAS[kit] in _atlas_texts() and "common" in _atlas_texts())


def draw_walls(m, spec, ring, z0, z_eave, mat_idx, lite=False):
    """The walls of a block of the post-Soviet kinds as the atlas's cells, with its balconies, entrance canopies and roof
    boxes. True when it did; False leaves the walls to the older code (a courtyard block, a deck, a kind with no atlas)."""
    cis = spec.cis
    if not cis or not drawn_kit(cis["kit"]) or spec.holes or spec.deck or (spec.min_h or 0.0) > 0.5:
        return False
    texts = _atlas_texts()
    atlas = KIT_ATLAS[cis["kit"]]
    floors = max(1, int(cis["floors"] or spec.floors))     # (the kernel's: the map's count or the height's; plan()'s guess may be 1)
    z_base = z_eave - float(spec.height)
    P, UV, mid, slots = fast._ef.cis_facade(
        cis["kit"], texts[atlas], texts["common"], np.ascontiguousarray(np.asarray(ring, np.float64)[:, :2]),
        float(z0), float(z_base), float(z_eave), floors, bool(cis["tech"]), max(1, int(cis["entrances"])),
        [float(cis["ux"]), float(cis["uy"])], [float(cis["cx"]), float(cis["cy"])], float(cis["l"]), float(cis["arm"]),
        float(cis["road_side"]), float(spec.seed) if isinstance(spec.seed, float) else 0.0, bool(lite))
    if not len(mid):
        return False
    m.quads(P, UV, mid, [mat_idx[f"bne_cis_{atlas}"], mat_idx["bne_cis_common"]])
    if not lite and len(slots) and _modules_text():
        mp, muv, mmat, names = fast._ef.cis_modules(_modules_text(), MODULE_KIT.get(cis["kit"], cis["kit"]), slots,
                                                    float(spec.seed) if isinstance(spec.seed, float) else 0.0, floors)
        for k, name in enumerate(names):
            sel = np.repeat(mmat == k, 3)
            if sel.any():
                m.tris(mp[sel], muv[sel], mat_idx[name])
    return True
