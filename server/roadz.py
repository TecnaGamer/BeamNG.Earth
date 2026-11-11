"""Real road heights (docs/research/2026-10-28-3d-road-data.md; the user, 2026-10-28: "Find a way to pull actual 3d
road data", then "Ok work on the 3d road / lidar"): a height-source override for bridge decks, tunnel floors and
(on coarse elevation only) ground roads, today's inference staying the fallback everywhere else.

Sources, each per z14 map-data cell, kept on disk under <cache>/roadz/v<VERSION>/<source>/<z>/<x>/<y>.npz:
- "ign": France, IGN BD TOPO troncon_de_route (Etalab Licence Ouverte 2.0) from the Geoplateforme WFS: per-vertex Z
  (photogrammetry, 1.5-2.5 m; -1000 = none), position_par_rapport_au_sol (>0 on a bridge, <0 in a tunnel), cpx_numero
  (the road's number), nom_collaboratif_gauche (its name). The WFS request is the one Madderose's PR #456 (ign_fr.py)
  reads BD TOPO with (CRS:84 bbox, GeoJSON, the "ign_wfs" lane).
- "nvdb": Norway, the Norwegian Public Roads Administration's NVDB API v4 road network (NLOD 2.0): LINESTRING Z,
  10-15 cm; medium L (in the air: a bridge), U / B (underground, in a building: a tunnel); vegsystem (E18, R4 ...).
- "hk": Hong Kong, the Transport Department's Road Network centrelines (ELEVATION: a signed level tier, 0 ground, 1-4
  decks, -1 / -2 tunnels; ROUTE_NUM; STREET_ENAME) with heights from CEDD's 2020 airborne LiDAR (0.5 m DTM and DSM, a
  750 x 600 m sheet a zip): ground roads the DTM, decks the DSM's median across the carriageway, none in a tunnel
  or on a deck under a higher one (roadzhk.py). From the CSDI Portal (DATA.GOV.HK terms: free use with attribution).
  The sheets a cell reads are kept (hklidar.py, shared with the terrain) for its neighbours; a road build
  waits for a cell as for the lidar's (gap_answers), the read going on for the next run. Its heights
  are the HK government's: plans made with them stay on this computer (hk_cell: no world bundle, BeamMP share or
  baked pack).
- "lidar": the US, USGS 3DEP point clouds (Entwine Point Tiles on AWS, public domain): for every road bridge of the
  cell, its deck measured along its own map line (earthfast.deck_lidar: the points' surfaces per 4 m station, one
  drivable surface picked by a Viterbi pass; any non-noise class, as class 17 is not to be trusted: Baltimore 2014
  put it on the ground), the dataset's heights brought onto our DEM's (the median of its ground points' difference;
  feet found by the scale that fits). The EPT nodes of one octree depth (the first with LIDAR_DENSITY points a m2)
  along the bridges only: ~0.3 MB a node, a few per bridge. Then the drawn roads' ground pieces going on from a deck
  end that the lidar shows elevated (roads.lidar_gap_candidates: an untagged viaduct stretch), read where the
  deck's measured end stands over the DTM only (the nodes the decks' read had not): their elevated start is a deck
  in roads.level_ranges (gap_answers, one decision per cell and server run; BNE_LIDAR_GAPS=off: none).
Matching (earthfast.roadz_match): each station of a map road takes the nearest source segment of its level (bridge,
tunnel, ground) within reach whose direction agrees and whose name / number does not contradict the map's; the
heights then lose their spikes and impossible grades (earthfast.profile_clean). Vector heights are taken relative to
the source's own ground: the median of (source Z - our DEM) over its ground roads near the part (else the cell's) is
taken off, so a datum or survey offset of 0.5-2 m makes no step where the deck meets our graded approach.

Off: BNE_ROADZ=off (every source), BNE_LIDAR_DECKS=off (the US lidar decks only), BNE_ROADZ_HK=off (Hong Kong
only). Pure given the cached data: every process reads the same files; a fetch that fails is a revisit fault
(netfetch) and the inference stands in.
Malformed vector survey records are isolated: invalid XY splits a line into valid runs, invalid Z is missing, and
bridge/tunnel levels stay attached to each run. Cached invalid coordinates trigger a refetch; if unavailable, the
existing inference remains the fallback. This validation does not smooth or alter finite surveyed heights.
"""
import contextlib
import gzip
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict

import numpy as np

import fast
import hkdtm
import hklidar
import mapcells
import roadzhk
import xproc

log = logging.getLogger("earthd.roadz")

VERSION = 3                 # (2: the lidar cells carry the ground stretches found elevated: <y>.npz.gaps.json;
                            # 3: and the approaches' measured surfaces, level 0)
NATIVE = 102                # earthfast.VERSION of the kernels below
ON = os.environ.get("BNE_ROADZ", "on") != "off"
LIDAR_ON = os.environ.get("BNE_LIDAR_DECKS", "on") != "off"
HK_ON = os.environ.get("BNE_ROADZ_HK", "on") != "off"
LIDAR_INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usgs3dep_index.json.gz")
PROVIDER = None             # the server's RoadZ (MapData makes it): BridgeProfiles / Grader ask it

FR_BOUNDS = (41.3, -5.2, 51.2, 9.7)         # lat0, lon0, lat1, lon1: metropolitan France (PR #456's box)
NO_BOUNDS = (57.9, 4.0, 71.3, 31.3)         # mainland Norway
HK_BOUNDS = hklidar.BOUNDS
US_BOUNDS = ((24.0, -125.0, 49.5, -66.5), (51.0, -170.0, 71.5, -129.0), (18.5, -161.0, 22.5, -154.5))
WFS_URL = "https://data.geopf.fr/wfs/ows"
WFS_LANE = "ign_wfs"
WFS_PAGE = 1000
WFS_PAGES = 30
NVDB_URL = "https://nvdbapiles.atlas.vegvesen.no/vegnett/api/v4/veglenkesekvenser/segmentert"
NVDB_HEADERS = {"X-Client": "BeamNG.Earth", "Accept": "application/json"}     # (NVDB asks clients to name themselves)
NVDB_PAGE = 1000
NVDB_PAGES = 40
HK_TD_URL = ("https://portal.csdi.gov.hk/server/rest/services/common/td_rcd_1638949160594_2844/FeatureServer/10/"
             "query")
HK_IDS = 200                # OBJECTIDs a request (its URL under the service's limit: 2.4 kB answered 404, 1.8 kB not)
EPT_BASE = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/"
EPT_R = 6378137.0           # EPSG:3857's sphere

LIDAR_DENSITY = 0.5         # points a m2 the octree depth read gives at least (~20 a 4 m station of a 10 m deck)
LIDAR_MAX_DEPTH = 14
LIDAR_NODES_MAX = 160       # EPT nodes a cell reads at most (~50 MB): a denser city's bridges keep the inference
LIDAR_STEP_M = 4.0          # m between a deck's stations
LIDAR_PAD_M = 2.0           # m beyond the carriageway's half width a deck's points count
LIDAR_MAX_GRADE = 0.10
LIDAR_DATASETS = 2          # the newest datasets tried at a place
LIDAR_FETCH_THREADS = 4     # node reads at once (the host's policy: 4)
BOX_STEP_M = 40.0           # m of a bridge's line per box its nodes are picked by
MASK_M = 8.0                # m, the cells of the boxes' mask the points are kept by
DATUM_MIN_PTS = 50
DATUM_MAX_MAD_M = 2.0       # the dataset's ground off ours by more than this (spread): not used there
CLIP_PAD_M = 60.0           # m past a cell's box its bridges are measured (the next cell measures on)

# NVDB flags few bridges on its road links (medium "L"; most bridges are objects of their own, type 60), but its links'
# heights are the road's own, on a deck too: a map bridge may take any link of it that is not in a tunnel (the
# direction, reach and the spike / grade cleaning keep a road under it out). BD TOPO flags its bridges.
LOOSE_BRIDGES = ("nvdb",)
MATCH_REACH_M = 6.0         # m past the map road's half width a vector source's line may lie
LIDAR_REACH_M = 3.0         # (the lidar decks are measured on the map's own lines)
MATCH_COS = 0.85            # directions within ~32 deg
CLEAN_GRADE = 0.12
CLEAN_SPIKE_M = 1.5
CLEAN_WIN_M = 20.0
CLEAN_GAP_M = 30.0
CLEAN_SIGMA_M = 3.0
MIN_COVER = 0.6             # share of a part's stations with a height, or the part keeps the inference
DATUM_NEAR_M = 300.0        # m round a part's ends its source's ground roads give the datum offset
DATUM_NEAR_MIN = 5
DEM_ZOOM = 15
MEM_CELLS = 96
# The elevated ground stretches (roads.lidar_gap_candidates / _lidar_gaps): measured in the decks' own lidar read, a
# stretch is elevated from the deck's end while its surface stays LIDAR_GAP_RISE_M or more over our DTM (the first
# station within LIDAR_GAP_END_TOL_M of the deck's measured end), LIDAR_GAP_MISS stations without passed over; kept when
# LIDAR_GAP_MIN_M or longer. gap_answers waits LIDAR_GAPS_WAIT_S at most for a cell's lidar (the rule meanwhile, for the
# whole session: the decision is kept in a session folder every process of the server reads, BNGE_ROADZ_SESSION).
LIDAR_GAP_RISE_M = 3.0
LIDAR_GAP_END_TOL_M = 1.5
LIDAR_GAP_MISS = 1
LIDAR_GAP_MIN_M = 8.0
LIDAR_GAPS_WAIT_S = float(os.environ.get("BNE_LIDAR_GAPS_WAIT", "8"))
# the sources a road build waits that long at most for a cell of, the inference for the rest of the run meanwhile
# (gap_answers). Hong Kong's read goes on where the lidar's stops (_read): its heights apply only once each sheet of the
# cell is kept, 167 MB in 62 s for a dense one (a cold place planned in 647 s, 115 s with it off; 2026-11-10), and the
# server's processes read each sheet once (hklidar)
DEFERRED = ("lidar", "hk")
# once a cell's lidar has missed LIDAR_GAPS_WAIT_S in this process (a slow connection, a busy source), the next cells
# wait this long at most: each call of a road tile's cells waited 8 s anew, 43 s of one cold Denver road tile's 132 s
# (2026-11-05); those left to the rule are faults (built again with their lidar on a later visit)
LIDAR_GAPS_SLOW_WAIT_S = 1.0
# Fast pre-cache's world builds (blockworker.prebuild_geo, its own road processes and the game paused) wait this long
# for a cell's lidar instead of dropping it: the area's own lidar steps read only the cells round its box, and a
# geotile's roads reach cells further out; dropping those made every geotile of a pre-cache fail without a sound reason
# (2026-11-08: 9 of 16 at Lower Manhattan on a slow connection, each read cut off after 8 s). Live play, and the slow
# pre-cache that shares the live road pool, keep LIDAR_GAPS_WAIT_S.
PRECACHE_LIDAR_WAIT_S = float(os.environ.get("BNGE_PRECACHE_LIDAR_WAIT", "900"))
GAP_NATIVE = 107            # earthfast.VERSION of lidar_gap_stretch


@contextlib.contextmanager
def precache_gaps_wait(seconds=None):
    """Wait as long as a fast pre-cache's geotile build can for its cells' lidar (blockworker.prebuild_geo): its own
    road processes, the game paused, no live tile waits on them. Restores the live values after."""
    global LIDAR_GAPS_WAIT_S, LIDAR_GAPS_SLOW_WAIT_S
    old = LIDAR_GAPS_WAIT_S, LIDAR_GAPS_SLOW_WAIT_S
    LIDAR_GAPS_WAIT_S = LIDAR_GAPS_SLOW_WAIT_S = float(PRECACHE_LIDAR_WAIT_S if seconds is None else seconds)
    try:
        yield
    finally:
        LIDAR_GAPS_WAIT_S, LIDAR_GAPS_SLOW_WAIT_S = old


# The approaches (the user, 2026-10-31: "use the lidar data to improve how ramps connect"): every drawn road going on
# from a measured deck end (the gap candidates) is measured too, from that end while its surface starts within
# APPROACH_END_TOL_M of the deck's measured end, up to where it is back on our DTM (APPROACH_REJOIN_M, two stations
# running); kept (a ground line, level 0) when it stands APPROACH_MIN_DEV_M or more off the DTM somewhere. The grader
# takes it for those roads' heights (Grader._obs: on lidar terrain too): an approach's embankment, narrower than a
# road, our DTM smooths away (abutments 2-4 m under their decks' measured ends at Springfield, Dallas, Houston,
# approachlidar.py), and the inferred ramp (a cosine at APPROACH_GRADE) ran 100 m and more where the real one falls in 30.
APPROACH_END_TOL_M = 1.0
APPROACH_REJOIN_M = 0.3
APPROACH_MIN_DEV_M = 0.5
SESSION_ENV = xproc.RUN_ENV

class LidarDropped(Exception):
    """A cell's lidar read stopped: this run goes without it (RoadZ._read)."""


_TOKEN = re.compile(r"[a-z0-9]+")
_REF = re.compile(r"^([a-z]{0,3})(\d{1,4})([a-z]?)$")


def _in(b, lat, lon):
    return b[0] <= lat <= b[2] and b[1] <= lon <= b[3]


def sources_at(lat, lon, lidar=None):
    """The sources that may know heights at (lat, lon): 'ign', 'nvdb', 'hk' (HK_ON), 'lidar' (bridges only; lidar: on,
    default LIDAR_ON)."""
    lidar = LIDAR_ON if lidar is None else lidar
    out = []
    if _in(FR_BOUNDS, lat, lon):
        out.append("ign")
    if _in(NO_BOUNDS, lat, lon):
        out.append("nvdb")
    if HK_ON and _in(HK_BOUNDS, lat, lon):
        out.append("hk")
    if lidar and any(_in(b, lat, lon) for b in US_BOUNDS):
        out.append("lidar")
    return out


def hk_tags(key):
    """The Hong Kong government data map cell key's road plans are made with, when its box meets HK_BOUNDS with
    CLIP_PAD_M to spare: "hk1", its measured road heights (this source on), "hkdtm1", its terrain (hkdtm.py on)."""
    lon0, lat0, lon1, lat1 = mapcells.CellKey(int(key[0]), int(key[1]), int(key[2])).bounds()
    pad = CLIP_PAD_M / 111320.0
    if not (lat0 - pad <= HK_BOUNDS[2] and HK_BOUNDS[0] <= lat1 + pad
            and lon0 - pad <= HK_BOUNDS[3] and HK_BOUNDS[1] <= lon1 + pad):
        return ()
    return (("hk1",) if ON and HK_ON else ()) + (("hkdtm1",) if hkdtm.ON else ())


def hk_cell(key):
    """Are map cell key's road plans made with Hong Kong's measured heights or terrain (hk_tags)? They are then kept on
    this computer only: the HK government's terms allow redistribution, but with an indemnity and terms that may change
    without notice, so they never go into a world bundle (plancache: no .complete marker), a BeamMP share
    (mpshare.note_built) or a baked pack (made with both off)."""
    return bool(hk_tags(key))


_HK_LINES = {}          # path -> (mtime, has lines) of the Hong Kong cells hk_credited_near looked at


def hk_credited_near(cache_dir, lat, lon, near_m):
    """Is a Hong Kong cell with lines kept on this computer within near_m of (lat, lon) (credits.feature_ids)? Disk
    only."""
    if not (ON and HK_ON and cache_dir and _in(HK_BOUNDS, lat, lon)):
        return False
    d = near_m / 111320.0
    k = max(0.05, math.cos(math.radians(lat)))
    for key in mapcells.cells_for_bbox(lat - d, lon - d / k, lat + d, lon + d / k):
        p = os.path.join(cache_dir, "roadz", f"v{VERSION}", "hk", str(key) + ".npz")
        try:
            mt = os.stat(p).st_mtime
            got = _HK_LINES.get(p)
            if got is None or got[0] != mt:
                with np.load(p, allow_pickle=False) as z:
                    got = _HK_LINES[p] = (mt, len(z["off"]) > 1)
        except (OSError, ValueError, KeyError):
            continue
        if got[1]:
            return True
    return False


def _norm(text):
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return "" if t in ("none", "null", "nan") else t


def refs_of(text):
    """The road numbers in a name or ref ('A 75', 'E18', 'D41A' -> {'a75', 'e18', 'd41a'}): letters + digits."""
    toks = _TOKEN.findall(_norm(text))
    out, i = set(), 0
    while i < len(toks):
        t = toks[i]
        if t.isalpha() and len(t) <= 2 and i + 1 < len(toks) and toks[i + 1].isdigit():
            out.add(t + toks[i + 1])
            i += 2
            continue
        if _REF.match(t) and any(c.isdigit() for c in t):
            out.add(t)
        i += 1
    return out


def _digits(r):
    m = _REF.match(r)
    return m.group(2) if m else r


def name_ok(map_name, src_name, src_ref):
    """May a map road named map_name be the source's road (src_name, src_ref)? Only a contradiction says no: both have
    road numbers and none agrees (on its digits: 'E 18' and 'E18', 'R4' and '4'), or both have names that share no
    word of 4 letters or more. Pure."""
    m = _norm(map_name)
    if not m:
        return True
    mr, sr = refs_of(m), refs_of(src_ref) | refs_of(src_name)
    if mr and sr and not ({_digits(r) for r in mr} & {_digits(r) for r in sr}):
        return False
    s = _norm(src_name)
    if s:
        mw = {t for t in _TOKEN.findall(m) if len(t) >= 4}
        sw = {t for t in _TOKEN.findall(s) if len(t) >= 4}
        if mw and sw and not (mw & sw):
            return False
    return True


class Lines:
    """3D road lines of one cell and source: lonlat (n, 2) and z (n,) (m; NaN: none) of every vertex, off (L + 1,) the
    lines' vertex ranges, level (L,) int8 (>0 bridge, 0 ground, <0 tunnel), ref / name (L,) text, corrected: the
    heights already on our DEM's datum (the lidar decks)."""

    def __init__(self, lonlat, z, off, level, ref, name, corrected=False):
        self.lonlat = np.asarray(lonlat, np.float64).reshape(-1, 2)
        if not (np.isfinite(self.lonlat).all() and (np.abs(self.lonlat) <= [180.0, 90.0]).all()):
            # Reject old/corrupt cached geometry too: lines() will fetch it again through the validated parser.
            raise ValueError("road survey coordinates must be finite longitude/latitude")
        self.z = np.asarray(z, np.float64).ravel().copy()
        self.z[~np.isfinite(self.z)] = np.nan
        self.off = np.asarray(off, np.int64).ravel()
        self.level = np.asarray(level, np.int8).ravel()
        self.ref = np.asarray(ref, dtype=str).ravel() if len(ref) else np.zeros(0, "U1")
        self.name = np.asarray(name, dtype=str).ravel() if len(name) else np.zeros(0, "U1")
        self.corrected = bool(corrected)

    @classmethod
    def build(cls, rows, corrected=False):
        """From [(lonlat (k, 2), z (k,), level, ref, name)]."""
        rows = [r for r in rows if len(r[0]) >= 2]
        if not rows:
            return cls(np.zeros((0, 2)), np.zeros(0), [0], [], [], [], corrected)
        off = np.r_[0, np.cumsum([len(r[0]) for r in rows])]
        return cls(np.concatenate([np.asarray(r[0], np.float64)[:, :2] for r in rows]),
                   np.concatenate([np.asarray(r[1], np.float64) for r in rows]), off,
                   [int(r[2]) for r in rows], [str(r[3] or "") for r in rows], [str(r[4] or "") for r in rows],
                   corrected)

    def __len__(self):
        return len(self.off) - 1

    def save(self, path):
        import staging
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = staging.tmp_for(path)
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, lonlat=self.lonlat, z=self.z, off=self.off, level=self.level, ref=self.ref,
                                name=self.name, corrected=np.array(self.corrected))
        staging.replace_cached(tmp, path)

    @classmethod
    def load(cls, path):
        with np.load(path, allow_pickle=False) as d:
            return cls(d["lonlat"], d["z"], d["off"], d["level"], d["ref"], d["name"], bool(d["corrected"]))


# ---- the vector sources ----------------------------------------------------------------------------------------

def _mapping(value):
    return value if isinstance(value, dict) else {}


def _survey_runs(vertices, lat_first=False):
    """Finite geographic runs, split at bad XY rather than drawing a new segment across missing geometry.
    A missing/bad Z keeps its vertex with NaN: the profile cleaner handles height gaps separately."""
    run = []
    for vertex in vertices:
        try:
            if not isinstance(vertex, (list, tuple)) or len(vertex) < 2:
                raise ValueError("missing coordinate")
            lon, lat = float(vertex[1 if lat_first else 0]), float(vertex[0 if lat_first else 1])
            if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                raise ValueError("invalid coordinate")
        except (TypeError, ValueError, OverflowError):
            if len(run) >= 2:
                yield np.asarray(run, np.float64)
            run = []
            continue
        try:
            z = float(vertex[2]) if len(vertex) > 2 else math.nan
        except (TypeError, ValueError, OverflowError):
            z = math.nan
        run.append((lon, lat, z if math.isfinite(z) else math.nan))
    if len(run) >= 2:
        yield np.asarray(run, np.float64)


def parse_ign(features):
    """[(lonlat, z, level, ref, name)] of BD TOPO troncon_de_route GeoJSON features (CRS:84, xyz). Fictitious
    (fictif) links have no surveyed heights: left out; Z -1000 (none) is NaN."""
    rows = []
    for f in features or ():
        f = _mapping(f)
        p = _mapping(f.get("properties"))
        if p.get("fictif") in (True, "true", "1", 1):
            continue
        g = _mapping(f.get("geometry"))
        coords = g.get("coordinates")
        coords = coords if isinstance(coords, (list, tuple)) else []
        lines = [coords] if g.get("type") == "LineString" else (
            coords if g.get("type") == "MultiLineString" else [])
        try:
            pos = int(str(p.get("position_par_rapport_au_sol") or "0").strip())
        except ValueError:
            pos = 0
        lvl = 1 if pos > 0 else (-1 if pos < 0 else 0)
        for ln in lines:
            if not isinstance(ln, (list, tuple)):
                continue
            for a in _survey_runs(ln):
                a[a[:, 2] <= -999.0, 2] = np.nan
                rows.append((a[:, :2], a[:, 2], lvl, p.get("cpx_numero") or "", p.get("nom_collaboratif_gauche") or ""))
    return rows


_WKT = re.compile(r"\s*LINESTRING\s*Z?\s*\(([^()]*)\)\s*", re.I)


def parse_nvdb(objects):
    """[(lonlat, z, level, ref, name)] of NVDB v4 segmented road links (srid 4326: WKT in lat lon z order)."""
    rows = []
    for o in objects or ():
        o = _mapping(o)
        g = _mapping(o.get("geometri"))
        wkt = g.get("wkt")
        m = _WKT.fullmatch(wkt) if isinstance(wkt, str) else None
        if not m:
            continue
        med = str(g.get("medium") or o.get("medium") or "T").upper()
        lvl = 1 if med == "L" else (-1 if med in ("U", "B") else 0)
        vs = _mapping(_mapping(o.get("vegsystemreferanse")).get("vegsystem"))
        ref = f"{vs.get('vegkategori', '')}{vs.get('nummer', '')}" if vs.get("nummer") is not None else ""
        for a in _survey_runs((tri.split() for tri in m.group(1).split(",")), lat_first=True):
            rows.append((a[:, :2], a[:, 2], lvl, ref, ""))
    return rows


# ---- the US lidar ------------------------------------------------------------------------------------------------

def merc_xy(lat, lon):
    lat = np.clip(np.asarray(lat, np.float64), -85.0, 85.0)
    return (np.radians(np.asarray(lon, np.float64)) * EPT_R,
            np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * EPT_R)


def merc_ll(x, y):
    return (np.degrees(2 * np.arctan(np.exp(np.asarray(y, np.float64) / EPT_R)) - np.pi / 2),
            np.degrees(np.asarray(x, np.float64) / EPT_R))


def _in_ring(lon, lat, ring):
    c = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[i - 1]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            c = not c
    return c


_index = None
_index_lock = threading.Lock()


def lidar_datasets(lat, lon):
    """The 3DEP datasets covering (lat, lon), the newest first (then by name): [name]."""
    global _index
    with _index_lock:
        if _index is None:
            try:
                with gzip.open(LIDAR_INDEX_PATH, "rt", encoding="utf-8") as fh:
                    _index = json.load(fh)["datasets"]
            except Exception as e:
                log.warning("3DEP index %s: %s", LIDAR_INDEX_PATH, e)
                _index = []
    hits = [d for d in _index if d["b"][0] <= lon <= d["b"][2] and d["b"][1] <= lat <= d["b"][3]
            and any(_in_ring(lon, lat, r) for r in d["p"])]
    hits.sort(key=lambda d: (-int(d["y"]), d["n"]))
    return [d["n"] for d in hits]


def resample_line(xy, step):
    """(points (m, 2), arc (m,)) every ~step m along polyline xy, ending on both ends."""
    xy = np.asarray(xy, np.float64)[:, :2]
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if len(xy) < 2 or s[-1] <= 0:
        return xy[:1], np.zeros(min(1, len(xy)))
    m = max(1, int(math.ceil(s[-1] / step - 1e-9))) + 1
    t = np.linspace(0.0, s[-1], m)
    return np.stack([np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])], 1), t


def datum_fit(z_src, z_dem):
    """(scale, offset) bringing a source's heights onto our DEM's (z_dem ~ z_src * scale - offset): scale 1, or 0.3048
    for a dataset in feet; offset the median difference. None when the spread (median absolute deviation) is over
    DATUM_MAX_MAD_M or there are too few points. Pure."""
    z_src, z_dem = np.asarray(z_src, np.float64), np.asarray(z_dem, np.float64)
    ok = np.isfinite(z_src) & np.isfinite(z_dem)
    if ok.sum() < DATUM_MIN_PTS:
        return None
    best = None
    for scale in (1.0, 0.3048):
        d = z_src[ok] * scale - z_dem[ok]
        off = float(np.median(d))
        mad = float(np.median(np.abs(d - off)))
        if best is None or mad < best[2]:
            best = (scale, off, mad)
    return None if best[2] > DATUM_MAX_MAD_M else best[:2]


class Ept:
    """One 3DEP dataset's Entwine Point Tiles: its ept.json and hierarchy (kept on disk: they do not change), node
    reads (not kept: the decks made from them are)."""

    def __init__(self, name, fetch_json):
        self.name = name
        self._json = fetch_json
        self.meta = fetch_json(f"{name}/ept.json")
        b = self.meta["bounds"]
        self.x0, self.y0, self.z0 = b[0], b[1], b[2]
        self.w = b[3] - b[0]
        srs = self.meta.get("srs") or {}
        if str(srs.get("horizontal", "")) != "3857":
            raise ValueError(f"{name}: srs {srs.get('horizontal')}")
        self.hier = {}
        self._files = set()
        self._lock = threading.Lock()
        self._load("0-0-0-0")

    def _load(self, key):
        with self._lock:
            if key in self._files:
                return
        h = self._json(f"{self.name}/ept-hierarchy/{key}.json")
        with self._lock:
            self._files.add(key)
            self.hier.update(h)

    def box(self, key):
        d, x, y, _z = map(int, key.split("-"))
        w = self.w / 2 ** d
        return self.x0 + x * w, self.y0 + y * w, self.x0 + (x + 1) * w, self.y0 + (y + 1) * w

    def nodes(self, boxes, depth):
        """Keys of the nodes of `depth` (all their Z) whose XY overlaps any of boxes [(x0, y0, x1, y1)], sorted."""
        out, todo = set(), ["0-0-0-0"]
        while todo:
            k = todo.pop()
            c = self.hier.get(k)
            if c is None:
                continue
            if c == -1:
                self._load(k)
                c = self.hier.get(k, 0)
                if c == -1:
                    continue
            b = self.box(k)
            if not any(b[0] <= q[2] and q[0] <= b[2] and b[1] <= q[3] and q[1] <= b[3] for q in boxes):
                continue
            d, x, y, z = map(int, k.split("-"))
            if d == depth:
                if c > 0:
                    out.add(k)
                continue
            for dx in (0, 1):
                for dy in (0, 1):
                    for dz in (0, 1):
                        todo.append(f"{d + 1}-{2 * x + dx}-{2 * y + dy}-{2 * z + dz}")
        return sorted(out, key=lambda k: tuple(map(int, k.split("-"))))

    def depth_at(self, x, y):
        """The first depth whose nodes at (x, y) (all Z) hold LIDAR_DENSITY points a m2 (LIDAR_MAX_DEPTH at most)."""
        for d in range(6, LIDAR_MAX_DEPTH + 1):
            w = self.w / 2 ** d
            keys = self.nodes([(x, y, x, y)], d)
            n = 0
            for k in keys:
                c = self.hier.get(k, 0)
                n += c if c > 0 else 0
            if n / (w * w) >= LIDAR_DENSITY:
                return d
        return LIDAR_MAX_DEPTH


class RoadZ:
    """The heights the sources know, per cell (see the module docstring). md: the server's MapData (its fetcher, its
    road cells and its DEM)."""

    def __init__(self, cache_dir, fetcher, md=None):
        self.dir = os.path.join(cache_dir, "roadz", f"v{VERSION}") if cache_dir else None
        self.fetcher = fetcher
        self.md = md
        self._mem = OrderedDict()           # (source, cell) -> Lines
        self._enu = OrderedDict()           # (frame, source, cell) -> segment arrays
        self._off = {}                      # (source, cell) -> the cell's datum offset (None: none)
        self._dropped = set()               # cells this run goes without the lidar of: their reads stop (_read)
        self._names = {}                    # (source, cell, map name) -> name_ok per line
        self._ept = {}                      # dataset name -> Ept
        self._lock = threading.Lock()
        self._cell_locks = {}
        self.sheets = hklidar.at(cache_dir, fetcher)     # (Hong Kong's LiDAR sheets: the terrain reads them too)
        self.stats = {"cells": 0, "requests": 0, "bytes": 0, "seconds": 0.0}     # (built here, not read from disk)
        self.enabled = ON                   # (the probes measure with it on whatever BNE_ROADZ says)
        self.lidar = LIDAR_ON

    # ---- cells ----
    def lidar_sources(self, key):
        """The DEFERRED sources (a LiDAR: the US's, Hong Kong's) this cell's road heights read. No network."""
        if not self.enabled or self.fetcher is None:
            return []
        la, lo = mapcells.CellKey(int(key[0]), int(key[1]), int(key[2])).center()
        return [s for s in sources_at(la, lo, self.lidar) if s in DEFERRED]

    def lidar_wanted(self, key):
        """Do this cell's road heights read a LiDAR (gap_answers)? No network."""
        return bool(self.lidar_sources(key))

    def lidar_on_disk(self, key):
        """Is this cell's LiDAR kept on disk already? No network."""
        k = mapcells.CellKey(int(key[0]), int(key[1]), int(key[2]))
        return all(p and os.path.exists(p) for p in (self._path(s, k) for s in self.lidar_sources(k)))

    def _path(self, src, key):
        import roads
        if src == "lidar" and not roads.LIDAR_GAPS_ON:
            src = "lidar-nogaps"            # (built without the ground stretches: not the cells with them)
        return os.path.join(self.dir, src, str(key.z), str(key.x), f"{key.y}.npz") if self.dir else None

    def lines(self, src, key):
        """The cell's Lines of source src (fetched and kept on disk the first time), or None when it could not be
        had (offline, a failed fetch: asked again next time)."""
        key = mapcells.CellKey(int(key[0]), int(key[1]), int(key[2]))
        mk = (src, str(key))
        with self._lock:
            got = self._mem.get(mk)
            if got is not None:
                self._mem.move_to_end(mk)
                return got
            lock = self._cell_locks.setdefault(mk, threading.Lock())
        with lock:
            with self._lock:
                got = self._mem.get(mk)
            if got is not None:
                return got
            p = self._path(src, key)
            if p and os.path.exists(p):
                try:
                    got = Lines.load(p)
                    if src == "lidar":      # (its elevated ground stretches: written before the lines)
                        with open(p + ".gaps.json", encoding="utf-8") as fh:
                            got.gaps = [(str(f), tuple(a), tuple(b)) for f, a, b in json.load(fh)]
                except Exception as e:
                    log.debug("roadz read %s: %s", p, e)
                    got = None
            if got is None:
                t0 = time.monotonic()
                try:
                    got = {"ign": self._ign, "nvdb": self._nvdb, "hk": self._hk, "lidar": self._lidar}[src](key)
                except Exception as e:      # (a FetchError is a revisit fault already)
                    log.debug("roadz %s %s: %s: %s", src, key, type(e).__name__, e)
                    import revisit
                    revisit.fault()
                    return None
                if got is None:
                    return None
                with self._lock:
                    self.stats["cells"] += 1
                    self.stats["seconds"] += time.monotonic() - t0
                if p:
                    try:
                        if src == "lidar":
                            import staging
                            os.makedirs(os.path.dirname(p), exist_ok=True)
                            tmp = staging.tmp_for(p + ".gaps.json")
                            with open(tmp, "w", encoding="utf-8") as fh:
                                json.dump([[f, list(a), list(b)] for f, a, b in getattr(got, "gaps", [])], fh)
                            staging.replace_cached(tmp, p + ".gaps.json")
                        got.save(p)
                        if src == "lidar":      # (the cell whole on disk: its partial node reads have done their job)
                            import shutil
                            shutil.rmtree(os.path.join(self.dir, "eptparts", str(key).replace("/", "_")),
                                          ignore_errors=True)
                    except Exception as e:
                        log.debug("roadz write %s: %s", p, e)
            with self._lock:
                self._mem[mk] = got
                while len(self._mem) > MEM_CELLS:
                    self._mem.popitem(last=False)
            return got

    def _fetch(self, url, headers=None, lane=None):
        res = self.fetcher.get(url, kind="demand", headers=headers, lane=lane)
        with self._lock:
            self.stats["requests"] += 1
            self.stats["bytes"] += len(res.body)
        return res.body

    def warm(self, keys):
        """Fetch / build the sources' cells of keys (in that order) in a background thread, so the road processes find
        them on disk (earthd.Server.warm_place). Returns at once."""
        if not self.enabled or self.fetcher is None or not keys:
            return

        def run():
            for k in keys:
                la, lo = k.center()
                for src in sources_at(la, lo, self.lidar):
                    try:
                        self.lines(src, k)
                    except Exception as e:      # (the road processes ask again)
                        log.debug("roadz warm %s %s: %s", src, k, e)
        threading.Thread(target=run, name="roadz-warm", daemon=True).start()

    def _get_json(self, url, headers=None, lane=None):
        return json.loads(self._fetch(url, headers, lane).decode("utf-8"))

    def _ign(self, key):
        lon0, lat0, lon1, lat1 = key.bounds()
        rows = []
        props = "geometrie,position_par_rapport_au_sol,cpx_numero,nom_collaboratif_gauche,fictif"
        for page in range(WFS_PAGES):
            url = (f"{WFS_URL}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES=BDTOPO_V3:troncon_de_route"
                   f"&PROPERTYNAME={props}&BBOX={lon0:.6f},{lat0:.6f},{lon1:.6f},{lat1:.6f},CRS:84&SRSNAME=CRS:84"
                   f"&OUTPUTFORMAT=application/json&COUNT={WFS_PAGE}&STARTINDEX={page * WFS_PAGE}&SORTBY=cleabs")
            d = self._get_json(url, lane=WFS_LANE)
            feats = d.get("features") or []
            rows += parse_ign(feats)
            if len(feats) < WFS_PAGE:
                break
        return Lines.build(rows)

    def _nvdb(self, key):
        lon0, lat0, lon1, lat1 = key.bounds()
        rows, start = [], None
        for _ in range(NVDB_PAGES):
            url = (f"{NVDB_URL}?kartutsnitt={lon0:.6f},{lat0:.6f},{lon1:.6f},{lat1:.6f}&srid=4326"
                   f"&trafikantgruppe=K&antall={NVDB_PAGE}" + (f"&start={start}" if start else ""))
            d = self._get_json(url, headers=NVDB_HEADERS)
            objs = d.get("objekter") or []
            rows += parse_nvdb(objs)
            nxt = ((d.get("metadata") or {}).get("neste") or {}).get("start")
            if len(objs) < NVDB_PAGE or not nxt or nxt == start:
                break
            start = nxt
        return Lines.build(rows)

    def _hk(self, key):
        lon0, lat0, lon1, lat1 = key.bounds()
        k = 1.0 / max(0.05, math.cos(math.radians((lat0 + lat1) / 2)))
        plat = CLIP_PAD_M / 111320.0
        plon = plat * k
        # (the lines a little further out too: a higher deck there hides a lower one in the box)
        qlat = (CLIP_PAD_M + roadzhk.OCCLUDE_M) / 111320.0
        env = f"{lon0 - qlat * k:.6f},{lat0 - qlat:.6f},{lon1 + qlat * k:.6f},{lat1 + qlat:.6f}"
        fields = "OBJECTID,ELEVATION,ROUTE_NUM,STREET_ENAME"
        feats = hklidar.arcgis(self._get_json, HK_TD_URL, {
            "where": "1=1", "geometry": env, "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outSR": 4326, "outFields": fields, "orderByFields": "OBJECTID"})
        # the same features in HK1980 Grid by their ids: the envelope query with outSR 2326 answers another set of
        # lines (2026-11-10: 89 for 84 at Shek Mun, some 50 m outside the box), the vertices themselves agree to 0.4 mm
        ids = sorted({(f.get("attributes") or {}).get("OBJECTID") for f in feats} - {None})
        grid = []
        for i in range(0, len(ids), HK_IDS):
            grid += hklidar.arcgis(self._get_json, HK_TD_URL, {
                "objectIds": ",".join(str(int(v)) for v in ids[i:i + HK_IDS]), "outSR": 2326, "outFields": "OBJECTID",
                "orderByFields": "OBJECTID"})
        lines = roadzhk.parse_td(feats, grid)

        def inside(ll):
            return ((ll[:, 0] >= lon0 - plon) & (ll[:, 0] <= lon1 + plon)
                    & (ll[:, 1] >= lat0 - plat) & (ll[:, 1] <= lat1 + plat))
        runs = roadzhk.stations(lines, inside)
        return Lines.build(roadzhk.rows(lines, runs, self._hk_reader("dtm"), self._hk_reader("dsm")))

    def _hk_reader(self, layer):
        """(x, y) -> the LiDAR layer's ("dtm" / "dsm") heights at those HK1980 Grid points, NaN where it has none: the
        sheets under them (hklidar: kept, or downloaded)."""
        def read(x, y):
            out = np.full(len(x), np.nan)
            if not len(x):
                return out
            todo, taken = [], np.zeros(len(x), bool)
            for name, url, (x0, y0, x1, y1) in self.sheets.index(layer, (x.min() - 1, y.min() - 1, x.max() + 1,
                                                                          y.max() + 1)):
                m = (x >= x0) & (x < x1) & (y >= y0) & (y < y1) & ~taken
                if m.any():
                    taken |= m
                    todo.append((m, name, url))

            def one(t):
                m, name, url = t
                return m, self.sheets.get(layer, name, url).sample(x[m], y[m])
            from runtimeprofiler import ThreadPoolExecutor
            # (the host serves ~0.8 MB/s a connection)
            with ThreadPoolExecutor(hklidar.FETCH_THREADS, thread_name_prefix="roadz-hk") as pool:
                for m, got in pool.map(one, todo):
                    out[m] = got
            return out
        return read

    def _ept_of(self, name):
        with self._lock:
            e = self._ept.get(name)
        if e is None:
            e = Ept(name, self._ept_json)
            with self._lock:
                self._ept.setdefault(name, e)
                e = self._ept[name]
        return e

    def _ept_json(self, rel):
        p = os.path.join(self.dir, "ept", rel) if self.dir else None
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        body = self._fetch(EPT_BASE + rel)
        d = json.loads(body.decode("utf-8"))
        if p:
            import staging
            os.makedirs(os.path.dirname(p), exist_ok=True)
            # (a name of its own: a cell's neighbours read the same EPT file from threads of their own, and on Windows
            # a temporary file another thread has open cannot be opened: that cell then took the rule for the run)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(body)
            staging.replace_cached(tmp, p)
        return d

    def _bridges(self, key):
        """[(lonlat (k, 2), half width)] of the cell's road bridge parts within CLIP_PAD_M of its box."""
        import roads
        feats = self.md.cell(key, layers=("roads",), kind="demand").layers.get("roads") if self.md else None
        if feats is None or not len(feats):
            return []
        lon0, lat0, lon1, lat1 = key.bounds()
        pad_lat = CLIP_PAD_M / 111320.0
        pad_lon = pad_lat / max(0.05, math.cos(math.radians((lat0 + lat1) / 2)))
        out = []
        # (the rule's ranges: roads.level_ranges asks this provider for the lidar's stretches, gap_answers)
        for i, rr in sorted(roads.rule_level_ranges_near(feats).items()):
            if not any(k == "bridge" for _, _, k in rr):
                continue
            w = roads._structure_width(feats, i)
            if w is None:
                continue
            for ring in roads._parts_by_level_new(feats, i, "bridge", rr):
                ring = np.asarray(ring, np.float64)[:, :2]
                inside = ((ring[:, 0] >= lon0 - pad_lon) & (ring[:, 0] <= lon1 + pad_lon)
                          & (ring[:, 1] >= lat0 - pad_lat) & (ring[:, 1] <= lat1 + pad_lat))
                if len(ring) >= 2 and inside.any():
                    out.append((ring, max(2.5, w / 2)))
        return out

    def _gap_candidates(self, key):
        """roads.lidar_gap_candidates of the cell whose deck end (the line's first point) lies in its box: [(fid, lonlat
        (k, 2), half width)] ([] off)."""
        import roads
        if not roads.LIDAR_GAPS_ON or not self.md:
            return []
        feats = self.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        if feats is None or not len(feats):
            return []
        lon0, lat0, lon1, lat1 = key.bounds()
        return [c for c in roads.lidar_gap_candidates(feats)
                if lon0 <= c[1][0, 0] < lon1 and lat0 <= c[1][0, 1] < lat1]

    def _lidar(self, key):
        import geodesy
        parts = self._bridges(key)
        cands = self._gap_candidates(key)
        if not parts and not cands:
            return Lines.build([], corrected=True)
        lat_c, lon_c = key.center()
        names = lidar_datasets(lat_c, lon_c)
        if not names:
            first = parts[0][0] if parts else cands[0][1]
            mid = first[len(first) // 2]
            names = lidar_datasets(float(mid[1]), float(mid[0]))
        if not names:
            return Lines.build([], corrected=True)
        frame = geodesy.EnuFrame(lat_c, lon_c)
        nb = len(parts)
        parts = parts + [(ring, half) for _fid, ring, half in cands]     # (the candidates measured in the same read)
        local = []
        for ring, half in parts:
            e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
            local.append((np.stack([np.asarray(e), np.asarray(n)], 1), half))
        for name in names[:LIDAR_DATASETS]:
            got = self._lidar_from(name, key, frame, parts, local, nb, [c[0] for c in cands])
            if got is not None:
                return got
        return Lines.build([], corrected=True)

    def _boxes(self, frame, parts, local):
        """EPT boxes (Mercator) along parts: one per BOX_STEP_M of each line, padded by its half width."""
        boxes = []
        for (ring, half), (xy, _h) in zip(parts, local):
            # (a box per BOX_STEP_M of the line: a curving ramp's own box took in every node round its curve)
            pts_, _ = resample_line(xy, BOX_STEP_M)
            la_, lo_, _ = frame.enu_to_geodetic(pts_[:, 0], pts_[:, 1], 0.0)
            x, y = merc_xy(la_, lo_)
            k = 1.0 / math.cos(math.radians(float(ring[0, 1])))         # (Mercator metres are longer)
            pad = (half + LIDAR_PAD_M + 2.0) * k
            for j in range(max(1, len(x) - 1)):
                xs, ys = x[j:j + 2], y[j:j + 2]
                boxes.append((float(xs.min()) - pad, float(ys.min()) - pad, float(xs.max()) + pad,
                              float(ys.max()) + pad))
        return boxes

    def _read(self, name, nodes, boxes, key=None):
        """(xyz (n, 3) Mercator / the dataset's heights, class (n,)) of the EPT nodes' points in boxes, LIDAR_FETCH_THREADS
        reads at once, in node order. Stops (LidarDropped) once this run goes without cell key's lidar (gap_answers):
        the reads went on after the decision, ~1.2 GB in 78 s of a Seattle first visit beside everything the road
        tiles waited for (2026-11-04)."""
        bb = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
        # the boxes as a grid of MASK_M cells (the points kept: those in a box's cells)
        nx = int(math.ceil((bb[2] - bb[0]) / MASK_M)) + 1
        ny = int(math.ceil((bb[3] - bb[1]) / MASK_M)) + 1
        mask = np.zeros((ny, nx), bool)
        for b in boxes:
            mask[int((b[1] - bb[1]) // MASK_M):int((b[3] - bb[1]) // MASK_M) + 1,
                 int((b[0] - bb[0]) // MASK_M):int((b[2] - bb[0]) // MASK_M) + 1] = True

        # each node's points (the boxes' only) kept on disk as they come in: a read this run stops (LidarDropped) goes
        # on from there on the next visit instead of from nothing (a busy cell's read was dropped at every visit and
        # downloaded again: a Fells Point road tile 43 s warm, 2026-11-06); keyed by the boxes, so other bridges or a
        # changed map read their own
        part_dir = None
        if self.dir and key is not None:
            import hashlib
            h = hashlib.sha1(np.round(np.asarray(boxes, np.float64), 1).tobytes()).hexdigest()[:16]
            part_dir = os.path.join(self.dir, "eptparts", str(key).replace("/", "_"), name.replace("/", "_"), h)

        def read(k):
            if key is not None and str(key) in self._dropped:
                raise LidarDropped(str(key))
            pp = os.path.join(part_dir, f"{k}.npz") if part_dir else None
            if pp and os.path.exists(pp):
                try:
                    with np.load(pp) as z:
                        return z["p"], z["c"]
                except Exception as e:
                    log.debug("roadz part %s: %s", pp, e)
            p, c = fast._ef.las_decode(self._fetch(f"{EPT_BASE}{name}/ept-data/{k}.laz"), bb)
            if len(p):
                keep = mask[((p[:, 1] - bb[1]) // MASK_M).astype(int).clip(0, ny - 1),
                            ((p[:, 0] - bb[0]) // MASK_M).astype(int).clip(0, nx - 1)]
                p, c = p[keep], c[keep]
            if pp:
                try:
                    import staging
                    os.makedirs(part_dir, exist_ok=True)
                    tmp = f"{pp}.{os.getpid()}.{threading.get_ident()}.tmp.npz"
                    np.savez(tmp, p=p, c=c)
                    staging.replace_cached(tmp, pp)
                except Exception as e:
                    log.debug("roadz part write %s: %s", pp, e)
            return p, c
        from runtimeprofiler import ThreadPoolExecutor
        with ThreadPoolExecutor(LIDAR_FETCH_THREADS, thread_name_prefix="roadz-ept") as pool:
            got = list(pool.map(read, nodes))           # (in node order: the same points whichever came first)
        xyz = [p for p, _c in got if len(p)]
        cls = [c for p, c in got if len(p)]
        if not xyz:
            return np.zeros((0, 3)), np.zeros(0, np.uint8)
        return np.concatenate(xyz), np.concatenate(cls)

    def _lidar_from(self, name, key, frame, parts, local, nb=None, fids=()):
        """The cell's decks from dataset name, or None when it has no usable points there. parts[nb:] are ground
        stretches from a deck's end (roads.lidar_gap_candidates, of the features fids): read (the EPT nodes the decks'
        read had not) only where a deck's measured end stands LIDAR_GAP_RISE_M or more over the DTM; the elevated ones
        go in as decks (their measured lines) and in the result's gaps [(fid, lon / lat from, lon / lat to)]."""
        nb = len(parts) if nb is None else nb
        if nb == 0:
            got = Lines.build([], corrected=True)
            got.gaps = []
            return got
        ept = self._ept_of(name)
        boxes = self._boxes(frame, parts[:nb], local[:nb])
        b0 = boxes[len(boxes) // 2]            # (at a bridge: the cell's centre may lie outside the dataset)
        depth = ept.depth_at((b0[0] + b0[2]) / 2, (b0[1] + b0[3]) / 2)
        nodes = ept.nodes(boxes, depth)
        if not nodes:
            return None
        if len(nodes) > LIDAR_NODES_MAX:
            log.debug("roadz lidar %s %s: %d nodes, over %d", name, key, len(nodes), LIDAR_NODES_MAX)
            return None
        fast.require(NATIVE, "roadz lidar decks")
        # (the points of these nodes along every candidate kept too: a stretch's own nodes are partly the decks')
        xyz, cls = self._read(name, nodes, boxes + self._boxes(frame, parts[nb:], local[nb:]), key)
        if not len(xyz):
            return None
        la, lo = merc_ll(xyz[:, 0], xyz[:, 1])
        # the dataset's datum onto ours: its ground points (class 2) against our DEM
        gi = np.flatnonzero(cls == 2)
        if len(gi) > 4000:
            gi = gi[:: int(math.ceil(len(gi) / 4000))]
        fit = datum_fit(xyz[gi, 2], self._dem_raw(la[gi], lo[gi])) if len(gi) else None
        if fit is None:
            log.debug("roadz lidar %s %s: no datum fit (%d ground points)", name, key, len(gi))
            return None
        scale, off = fit

        def local_pts(xyz_, la_, lo_):
            e, n, _ = frame.geodetic_to_enu(la_, lo_, 0.0)
            return np.ascontiguousarray(np.stack([np.asarray(e), np.asarray(n), xyz_[:, 2] * scale - off], 1))
        pts = local_pts(xyz, la, lo)
        cls = np.ascontiguousarray(cls, np.uint8)
        rows, ends = self._decks(frame, parts[:nb], local[:nb], pts, cls)
        gaps, extra = [], 0
        # the stretches from an elevated deck end, their nodes the decks' read had not; the approaches from any
        # measured deck end (read with them)
        todo = [n_ for n_ in range(nb, len(parts)) if self._elevated_end(frame, ends, local[n_][0][0])]
        appr = [n_ for n_ in range(nb, len(parts)) if np.isfinite(self._end_z(ends, local[n_][0][0]))]
        if appr:
            fast.require(GAP_NATIVE, "roadz lidar gaps")
            cb = self._boxes(frame, [parts[n_] for n_ in appr], [local[n_] for n_ in appr])
            had = set(nodes)
            more = [k for k in ept.nodes(cb, depth) if k not in had]
            if len(more) + len(nodes) <= LIDAR_NODES_MAX:
                extra = len(more)
                if more:
                    x2, c2 = self._read(name, more, cb, key)
                    if len(x2):
                        la2, lo2 = merc_ll(x2[:, 0], x2[:, 1])
                        pts = np.ascontiguousarray(np.vstack([pts, local_pts(x2, la2, lo2)]))
                        cls = np.ascontiguousarray(np.r_[cls, np.asarray(c2, np.uint8)])
                r2, gaps = self._stretches(frame, [parts[n_] for n_ in todo], [local[n_] for n_ in todo],
                                           [fids[n_ - nb] for n_ in todo], pts, cls, ends)
                rows += r2
                rows += self._approaches(frame, [parts[n_] for n_ in appr], [local[n_] for n_ in appr], pts, cls, ends)
        log.debug("roadz lidar %s %s: depth %d, %d + %d nodes, %d points, %d decks, %d of %d stretches elevated, datum "
                  "%.3f x - %.2f", name, key, depth, len(nodes), extra, len(pts), nb, len(gaps), len(parts) - nb, scale,
                  off)
        got = Lines.build(rows, corrected=True)
        got.gaps = gaps
        return got

    @staticmethod
    def _profile(frame, xy, half, pts, cls):
        """deck_lidar along line xy: (s, z, the stations' x, y, lat, lon, arc of xy) or None without a surface."""
        s, z, _cnt = fast._ef.deck_lidar(np.ascontiguousarray(xy), pts, cls, float(half + LIDAR_PAD_M),
                                         LIDAR_STEP_M, LIDAR_MAX_GRADE, 3, 24.0, 4.0)
        s, z = np.asarray(s, np.float64), np.asarray(z, np.float64)
        if len(s) < 2 or not np.isfinite(z).any():
            return None
        arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        sx, sy = np.interp(s, arc, xy[:, 0]), np.interp(s, arc, xy[:, 1])
        sla, slo, _ = frame.enu_to_geodetic(sx, sy, 0.0)
        return s, z, sx, sy, np.asarray(sla, np.float64), np.asarray(slo, np.float64), arc

    def _decks(self, frame, parts, local, pts, cls):
        """(rows, ends): the decks' measured lines (Lines.build rows) and their measured ends [(x, y, z)]."""
        rows, ends = [], []
        for (ring, half), (xy, _h) in zip(parts, local):
            got = self._profile(frame, xy, half, pts, cls)
            if got is None:
                continue
            s, z, sx, sy, sla, slo, _arc = got
            rows.append((np.stack([slo, sla], 1), z, 1, "", ""))
            ok = np.flatnonzero(np.isfinite(z))
            for at, far in ((ok[0], s[ok[0]]), (ok[-1], s[-1] - s[ok[-1]])):
                if far <= LIDAR_STEP_M:             # (measured right at its end: where a stretch must go on from)
                    ends.append((float(sx[at]), float(sy[at]), float(z[at])))
        return rows, ends

    def _end_z(self, ends, p):
        """The measured deck end height within 2 LIDAR_STEP_M of p (x, y), NaN: none."""
        if not ends:
            return np.nan
        de = np.asarray(ends, np.float64)
        d = np.hypot(de[:, 0] - p[0], de[:, 1] - p[1])
        k = int(np.argmin(d))
        return float(de[k, 2]) if d[k] <= 2.0 * LIDAR_STEP_M else np.nan

    def _elevated_end(self, frame, ends, p):
        """Does a measured deck end at p (x, y) stand LIDAR_GAP_RISE_M or more over our DTM?"""
        z = self._end_z(ends, p)
        if not np.isfinite(z):
            return False
        la, lo, _ = frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
        g = float(self._dem_raw(np.asarray(la, np.float64), np.asarray(lo, np.float64))[0])
        return np.isfinite(g) and z - g >= LIDAR_GAP_RISE_M

    def _stretches(self, frame, parts, local, fids, pts, cls, ends):
        """(rows, gaps) of the ground stretches parts (from a deck's end: lidar_gap_stretch) of the features fids."""
        rows, gaps = [], []
        for fid, (ring, half), (xy, _h) in zip(fids, parts, local):
            z_end = self._end_z(ends, xy[0])
            got = self._profile(frame, xy, half, pts, cls) if np.isfinite(z_end) else None
            if got is None:
                continue
            s, z, sx, sy, sla, slo, arc = got
            dem = self._dem_raw(sla, slo)
            m = float(fast._ef.lidar_gap_stretch(np.ascontiguousarray(s), np.ascontiguousarray(z),
                                                 np.ascontiguousarray(dem, np.float64), z_end, LIDAR_GAP_RISE_M,
                                                 LIDAR_GAP_END_TOL_M, LIDAR_GAP_MISS))
            if m < LIDAR_GAP_MIN_M:
                continue
            keep = s <= m + 1e-6
            rows.append((np.stack([slo, sla], 1)[keep], z[keep], 1, "", ""))
            ex, ey = np.interp(m, arc, xy[:, 0]), np.interp(m, arc, xy[:, 1])
            ela, elo, _ = frame.enu_to_geodetic(np.array([ex]), np.array([ey]), 0.0)
            gaps.append((str(fid), (float(ring[0, 0]), float(ring[0, 1])), (float(elo[0]), float(ela[0]))))
        return rows, gaps

    def _approaches(self, frame, parts, local, pts, cls, ends):
        """Lines.build rows (level 0) of the approaches parts (drawn roads from a deck end, as the gap candidates): each
        measured from its deck's measured end to where it is back on our DTM (APPROACH_*); none where it never stands
        APPROACH_MIN_DEV_M off it (the DTM is right) or starts off the deck's end (another surface: a deck over it)."""
        rows = []
        for (ring, half), (xy, _h) in zip(parts, local):
            z_end = self._end_z(ends, xy[0])
            got = self._profile(frame, xy, half, pts, cls) if np.isfinite(z_end) else None
            if got is None:
                continue
            s, z, sx, sy, sla, slo, _arc = got
            ok = np.flatnonzero(np.isfinite(z))
            if not len(ok) or s[ok[0]] > 2.0 * LIDAR_STEP_M or abs(float(z[ok[0]]) - z_end) > APPROACH_END_TOL_M:
                continue
            dev = np.abs(z - np.asarray(self._dem_raw(sla, slo), np.float64))
            on = np.isfinite(dev) & (dev < APPROACH_REJOIN_M)
            stop = len(s)
            for k in range(int(ok[0]) + 1, len(s) - 1):
                if on[k] and on[k + 1]:
                    stop = k + 2
                    break
            if not (np.isfinite(dev[:stop]) & (dev[:stop] >= APPROACH_MIN_DEV_M)).any():
                continue
            keep = np.arange(len(s)) < stop
            rows.append((np.stack([slo, sla], 1)[keep], z[keep], 0, "", ""))
        return rows

    def _measure(self, frame, parts, local, nb, fids, pts, cls):
        """(rows, gaps): _decks of parts[:nb], then _stretches of the rest (of the features fids) that go on from an
        elevated measured deck end, all from the points pts (n, 3) in frame (heights on our DEM's datum), cls (n,)."""
        rows, ends = self._decks(frame, parts[:nb], local[:nb], pts, cls)
        todo = [n_ for n_ in range(nb, len(parts)) if self._elevated_end(frame, ends, local[n_][0][0])]
        r2, gaps = self._stretches(frame, [parts[n_] for n_ in todo], [local[n_] for n_ in todo],
                                   [fids[n_ - nb] for n_ in todo], pts, cls, ends)
        r3 = self._approaches(frame, parts[nb:], local[nb:], pts, cls, ends)
        return rows + r2 + r3, gaps

    # ---- the elevated ground stretches, one answer per cell and session ----
    def _session_dir(self):
        """The folder this server run's decisions are kept in (xproc.run_dir). None without a cache."""
        if not self.dir:
            return None
        got = getattr(self, "_sdir", None)
        if got is None:
            got = self._sdir = xproc.run_dir(os.path.join(self.dir, "gapsession"))
        return got

    def _decision(self, src, key):
        d = self._session_dir()
        return os.path.join(d, f"{src}_{key.z}_{key.x}_{key.y}.json") if d else None

    def _decide(self, key, gaps, src="lidar"):
        """This session's decision for cell key's source src: gaps (a list, or None: the rule) unless a process decided
        first, whose decision is taken (xproc.decide)."""
        p = self._decision(src, key)
        if p is None:
            return gaps
        got = xproc.decide(p, {"gaps": None if gaps is None else [[f, list(a), list(b)] for f, a, b in gaps]})
        try:
            got = got["gaps"]
            return None if got is None else [(str(f), tuple(a), tuple(b)) for f, a, b in got]
        except (KeyError, TypeError, ValueError):
            return gaps

    def gap_answers(self, keys, src="lidar"):
        """{key: [(fid, lon / lat from, lon / lat to)] the cell's elevated ground stretches, or None: the rule} for the
        cells keys (the cells holding the deck ends of a roads layer's candidates), the same in every process of this
        server run: the cell's lidar (built if need be, LIDAR_GAPS_WAIT_S at most, in parallel) or this run's decision.
        A cell left to the rule is a revisit fault (no cached ranges from it). [] outside the lidar's reach. src: the
        DEFERRED source asked ("hk": [] once the cell's lines are built, None: the inference)."""
        out, todo = {}, []
        for key in keys:
            key = mapcells.CellKey(int(key[0]), int(key[1]), int(key[2]))
            if src not in self.lidar_sources(key):
                out[key] = []
                continue
            mk = ("gapans", src, str(key))
            with self._lock:
                got = self._off.get(mk, ...)
            if got is not ...:
                if got is None:             # (each build that takes the rule from it is not kept either)
                    import revisit
                    revisit.fault(lidar=True)   # (the session's answer: roads._FACTS_MEM keeps the facts made on it)
                out[key] = got
                continue
            todo.append(key)
        if todo:
            done = {}

            def build(key):
                try:
                    ln = self.lines(src, key)
                    done[str(key)] = None if ln is None else list(getattr(ln, "gaps", []))
                except Exception as e:
                    log.debug("roadz %s gaps %s: %s: %s", src, key, type(e).__name__, e)
                    done[str(key)] = None
            threads = []
            for key in todo:
                p = self._decision(src, key)
                if p and os.path.exists(p):
                    continue                # (decided already: _decide reads it)
                t = threading.Thread(target=build, args=(key,), name="roadz-gaps", daemon=True)
                t.start()
                threads.append(t)
            end = time.monotonic() + (LIDAR_GAPS_SLOW_WAIT_S if getattr(self, "_gaps_slow", False) else LIDAR_GAPS_WAIT_S)
            for t in threads:
                t.join(max(0.0, end - time.monotonic()))
            if any(t.is_alive() for t in threads):
                self._gaps_slow = True          # (LIDAR_GAPS_SLOW_WAIT_S from now on)
            for key in todo:
                got = self._decide(key, done.get(str(key)), src)    # (not built in time: None, the rule)
                if got is None:
                    import revisit
                    revisit.fault(lidar=True)
                    self._dropped.add(str(key))     # (its read, still going, stops: _read)
                with self._lock:
                    self._off[("gapans", src, str(key))] = got
                out[key] = got
        return out

    def _lidar_skipped(self, keys, src="lidar"):
        """Does this server run go without src (a lidar) for one of these cells? Its decision for each (gap_answers, the cells
        at once: the lidar built
        within LIDAR_GAPS_WAIT_S, else the rule for the whole run, the same in every process), so a road tile never
        waits for a cell's whole read: up to LIDAR_NODES_MAX nodes, ~50 MB a cell, ~1.5 GB of a 20 km drive through
        Seattle, and its tiles waited 18 s on average, 86 s at most, on a slow connection (BNGE_NET_MBPS=1,
        2026-11-04). The read goes on and is kept for the next run; a tile built without it is not cached."""
        try:
            return any(v is None for v in self.gap_answers(keys, src).values())
        except Exception as e:
            log.debug("roadz lidar decision %s: %s: %s", keys, type(e).__name__, e)
            return False

    # ---- heights for a map road ----
    def _dem_raw(self, la, lo):
        d = getattr(self.md, "dem", None) if self.md is not None else None
        if d is None and self.md is not None:
            import gradesep
            d = gradesep._dem(self.md)
        if d is None:
            return np.full(len(np.atleast_1d(la)), np.nan)
        return np.asarray(d.sample(np.asarray(la, np.float64), np.asarray(lo, np.float64), DEM_ZOOM), np.float64)

    def _cell_offset(self, src, key, ln):
        mk = (src, str(key))
        with self._lock:
            if mk in self._off:
                return self._off[mk]
        off = None
        if ln is not None and len(ln):
            ground = np.repeat(ln.level == 0, np.diff(ln.off)) & np.isfinite(ln.z)
            gi = np.flatnonzero(ground)
            if len(gi) > 1500:
                gi = gi[:: int(math.ceil(len(gi) / 1500))]
            if len(gi) >= DATUM_NEAR_MIN:
                d = ln.z[gi] - self._dem_raw(ln.lonlat[gi, 1], ln.lonlat[gi, 0])
                d = d[np.isfinite(d)]
                off = float(np.median(d)) if len(d) >= DATUM_NEAR_MIN else None
        with self._lock:
            self._off[mk] = off
        return off

    def _segments(self, frame, src, key, ln):
        """(seg (m, 4), segz (m, 2), line of each segment (m,), vertex xy (n, 2)) of Lines ln in frame, made once."""
        fk = (round(frame.lat0, 9), round(frame.lon0, 9), src, str(key))
        with self._lock:
            got = self._enu.get(fk)
        if got is not None:
            return got
        e, n, _ = frame.geodetic_to_enu(ln.lonlat[:, 1], ln.lonlat[:, 0], 0.0)
        xy = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], 1)
        cnt = np.diff(ln.off)
        line_of_v = np.repeat(np.arange(len(cnt)), cnt)
        a = np.arange(len(xy) - 1)
        a = a[line_of_v[a] == line_of_v[a + 1]] if len(a) else a
        got = (np.ascontiguousarray(np.hstack([xy[a], xy[a + 1]])),
               np.ascontiguousarray(np.stack([ln.z[a], ln.z[a + 1]], 1)), line_of_v[a], xy)
        with self._lock:
            self._enu[fk] = got
            while len(self._enu) > MEM_CELLS * 2:
                self._enu.popitem(last=False)
        return got

    def observed(self, frame, xy, half, level, name="", info=None, lidar_ground=False):
        """Heights (m, geodetic like dem.sample: on our DEM's datum) at the points xy (k, 2) of a map road part in the
        anchor frame, its carriageway half width `half`, level > 0 a bridge, 0 ground, < 0 a tunnel, `name` its map
        name: NaN where no source knows; None when none covers MIN_COVER of it (or it is off). info: a dict the
        caller may pass for what was used (source, coverage, datum offset)."""
        if not self.enabled or self.fetcher is None:
            return None
        xy = np.asarray(xy, np.float64)[:, :2]
        if len(xy) < 2:
            return None
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        mid = len(xy) // 2
        # (the lidar's ground lines are the approaches alone: asked for those (lidar_ground), any share of the part;
        # lidar_ground alone asks no vector source either)
        srcs = [s for s in sources_at(float(la[mid]), float(lo[mid]), self.lidar)
                if (s == "lidar") == bool(lidar_ground) or (s == "lidar" and level > 0)]
        if lidar_ground:
            srcs = [s for s in srcs if s == "lidar"]
        if not srcs:
            return None
        pad = (half + MATCH_REACH_M) / 111320.0
        keys = sorted(set(mapcells.cells_for_points(la, lo, pad, pad / max(0.05, math.cos(math.radians(la[mid]))))),
                      key=str)
        fast.require(NATIVE, "roadz matching")
        d = np.gradient(xy, axis=0)
        qdir = np.ascontiguousarray(d / np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None])
        for src in srcs:
            segs, segz, oks, cells = [], [], [], []
            if src in DEFERRED and self._lidar_skipped(keys, src):
                return None                 # (this run goes without a cell's lidar: the inference, every process)
            for k in keys:
                ln = self.lines(src, k)
                if ln is None:
                    return None             # (incomplete: the inference rather than half a source)
                if not len(ln):
                    continue
                seg, sz, line_of, vxy = self._segments(frame, src, k, ln)
                lv = ln.level[line_of]
                if level > 0 and src in LOOSE_BRIDGES:
                    want = lv >= 0          # (its own heights on a bridge whatever its flag: not a tunnel's)
                else:
                    want = (lv > 0) if level > 0 else ((lv < 0) if level < 0 else (lv == 0))
                nm_ok = self._names_ok(src, k, ln, name)
                segs.append(seg)
                segz.append(sz)
                oks.append((want & nm_ok[line_of]).astype(np.uint8))
                cells.append((k, ln, vxy))
            if not segs:
                continue
            reach = half + (LIDAR_REACH_M if src == "lidar" else MATCH_REACH_M)
            z, dist = fast._ef.roadz_match(np.ascontiguousarray(xy), qdir, np.ascontiguousarray(np.vstack(segs)),
                                           np.ascontiguousarray(np.vstack(segz)),
                                           np.ascontiguousarray(np.concatenate(oks)), float(reach), MATCH_COS)
            if not np.isfinite(z).any():
                continue
            off = 0.0
            if src != "lidar":
                off = self._local_offset(src, cells, xy, frame)
                if off is None:
                    continue
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            zc = np.asarray(fast._ef.profile_clean(s, np.ascontiguousarray(z - off), CLEAN_GRADE, CLEAN_SPIKE_M,
                                                   CLEAN_WIN_M, CLEAN_GAP_M, CLEAN_SIGMA_M), np.float64)
            cover = float(np.isfinite(zc).mean())
            if info is not None:
                info.update(source=src, cover=cover, offset=off, matched=float(np.isfinite(z).mean()))
            if cover < (0.0 if (lidar_ground and level == 0) else MIN_COVER) or not np.isfinite(zc).any():
                return None
            return zc
        return None

    def _names_ok(self, src, key, ln, name):
        """Per line of ln: may it be the map road called name (name_ok)? Kept per (source, cell, name)."""
        if not name:
            return np.ones(len(ln), bool)
        mk = (src, str(key), name)
        with self._lock:
            got = self._names.get(mk)
        if got is None:
            got = np.array([name_ok(name, ln.name[j], ln.ref[j]) for j in range(len(ln))], bool)
            with self._lock:
                if len(self._names) > 4096:
                    self._names.clear()
                self._names[mk] = got
        return got

    def _local_offset(self, src, cells, xy, frame):
        """The datum offset of a vector source at a part: the median (Z - our DEM) of its ground roads' vertices within
        DATUM_NEAR_M of the part's ends, else the cell's; None when neither has enough."""
        ends = xy[[0, -1]]
        zs, lls = [], []
        for k, ln, vxy in cells:
            ground = np.repeat(ln.level == 0, np.diff(ln.off)) & np.isfinite(ln.z)
            near = ground & (np.minimum(np.hypot(*(vxy - ends[0]).T), np.hypot(*(vxy - ends[1]).T)) <= DATUM_NEAR_M)
            gi = np.flatnonzero(near)
            zs.append(ln.z[gi])
            lls.append(ln.lonlat[gi])
        z = np.concatenate(zs) if zs else np.zeros(0)
        if len(z) >= DATUM_NEAR_MIN:
            ll = np.concatenate(lls)
            if len(z) > 400:
                sel = np.arange(0, len(z), int(math.ceil(len(z) / 400)))
                z, ll = z[sel], ll[sel]
            d = z - self._dem_raw(ll[:, 1], ll[:, 0])
            d = d[np.isfinite(d)]
            if len(d) >= DATUM_NEAR_MIN:
                return float(np.median(d))
        offs = [self._cell_offset(src, k, ln) for k, ln, _ in cells]
        offs = [o for o in offs if o is not None]
        return float(np.median(offs)) if offs else None


def install(cache_dir, fetcher, md):
    """Make the server's provider (MapData calls it), unless one is there already (tests set their own)."""
    global PROVIDER
    if PROVIDER is None and ON:
        PROVIDER = RoadZ(cache_dir, fetcher, md)
    return PROVIDER
