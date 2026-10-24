"""Data credits for what is around the player (docs/DEVTOOLS.md "Data credits overlay").

The mod draws a small line of text in a screen corner naming the sources that built the world the
player sees right now (CC BY and similar licences ask for visible attribution). The server knows
them per tile:

  - imagery: the sources whose texels a tile's texture took (Imagery.render's per-source counts;
    "none" = the fallback colour and "deshadow" = a relit share are not sources; "colormatch" =
    texels whose colours the seam feather corrected names its S2 references, as hello.attribution
    does with --colormatch s2). Capture dates
    are not in the tiles (USGS ImageryOnly carries none; EOX's year is the layer's own, part of
    its credit);
  - elevation: the provider of every DEM tile under the tile's footprint at its DEM zoom
    (dem.Dem.served: Mapterhorn or the AWS Terrain Tiles fallback). Mapterhorn's underlying
    dataset (USGS 3DEP, swissALTI3D, ...) is not in the tiles it serves, so the credit names
    Mapterhorn with its attribution page;
  - weather: Open-Meteo once a weather message went to the mod (weather.CREDIT).

Credits.message() splits the shown tiles (the connection's `shown`, previous anchors' tiles
included) into those within NEAR_M of the player and the rest (a source already near stays near
while one of its tiles is within NEAR_M + NEAR_HYST_M: a player rocking across the radius does not
flip it every check), and builds the 'credits' message, once some tile's sources are known (the
weather alone would replace the mod's full attribution line with one name):
`near` (the line the overlay shows first), `far` (the sources only the far field uses, short),
`ids` / `farIds` (source ids, for tests and tools). Tracker sends it only when it changed, after
at most one check per INTERVAL_S (pokes from poses and swaps), and again on a hello.

Off unless the server has a Credits object (earthd --credits on, the command line's default;
Server() has none, so the golden message streams of test_cli_default / test_v131_default are
unchanged).
"""
import logging
import math
import time

import numpy as np

import colormatch as colormatchmod
import dem as demmod
import polardem
import weather as weathermod

log = logging.getLogger("earthd.credits")

VERSION = 1
NEAR_M = 2000.0                 # m from the player: tiles this close are "near"
NEAR_HYST_M = 200.0             # ... and a near source stays near until its tiles are this much further
INTERVAL_S = 1.0                # at most one check (and so one message) per second
MAX_DEM_KEYS = 64               # DEM tiles looked up per far tile (a footprint needs 1-4)

# Overlay wording per source id, word for word the attribution CREDITS.md lists (test_credits checks
# it): EOX's is their prescribed attribution (docs/research/2026-10-10-rate-limits.md, imagery.SOURCES
# without the licence name); the others as in imagery.SOURCES / earthd.ELEVATION_CREDIT. The full
# credits stay in hello.attribution (the settings page) and CREDITS.md.
IMAGERY = {
    "md6in": "State of Maryland, MD iMAP / DoIT",
    "naipusda": "USDA NAIP",
    "naip": "USDA NAIP via USGS The National Map",
    "usgs": "USGS The National Map",
    "eox2023": "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
               "(Contains modified Copernicus Sentinel data 2023)",
    "eox2016": "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
               "(Contains modified Copernicus Sentinel data 2016 & 2017)",
}
IMAGERY.update({n: v[1] for n, v in __import__("imagery_regional").REGIONAL.items()})   # their own wording
ELEVATION = {
    demmod.MAPTERHORN["name"]: "Mapterhorn (see mapterhorn.com/attribution)",
    demmod.AWS["name"]: "AWS Terrain Tiles / Mapzen",
    polardem.SOURCE: "Produced using Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space GmbH "
                     "2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved",
}
WEATHER_ID = "wx:open-meteo"

# The overlay's short names (the user, 2026-10-18: "trim down the amount of text we use to credit a single source").
# The full wordings above stay the attribution of record: hello.attribution (the settings page) and CREDITS.md.
SHORT = {
    "img:md6in": "MD iMAP", "img:naipusda": "USDA NAIP", "img:naip": "USDA NAIP", "img:usgs": "USGS",
    "img:eox2023": "EOX Sentinel-2 cloudless 2023 (Copernicus)",
    "img:eox2016": "EOX Sentinel-2 cloudless 2016 (Copernicus)",
    "img:ct2023": "CT ECO", "img:ma2025": "MassGIS", "img:dc2025": "DC OCTO", "img:indiana": "Indiana IGIO",
    "img:ohio": "Ohio OGRIP", "img:ncarolina": "NC OneMap", "img:penn": "PEMA / PASDA", "img:virginia": "VGIN",
    "img:newyork": "NYS ITS", "img:newjersey": "NJ OGIS", "img:delaware": "Delaware FirstMap", "img:vermont": "VCGI",
    "img:swiss": "swisstopo", "img:france": "IGN France", "img:spain": "PNOA IGN Spain", "img:netherlands": "PDOK",
    "img:luxembourg": "geoportail.lu", "img:austria": "basemap.at", "img:it_bolzano": "Prov. Bozen",
    "img:it_pcn": "Geoportale Nazionale", "img:czechia": "CUZK", "img:estonia": "Maa-amet",
    "img:flanders": "Digitaal Vlaanderen", "img:de_bayern": "LDBV Bayern", "img:de_nrw": "Geobasis NRW",
    "img:de_bw": "LGL BW", "img:de_nds": "LGLN", "img:de_berlin": "Geoportal Berlin", "img:de_hessen": "HVBG Hessen",
    "img:japan": "GSI Japan", "img:india": "Esri / Maxar", "img:au_nsw": "Spatial Services NSW", "img:au_tas": "theLIST Tasmania",
    "img:ontario": "Ontario GEO",
    f"dem:{demmod.MAPTERHORN['name']}": "Mapterhorn", f"dem:{demmod.AWS['name']}": "AWS Terrain Tiles / Mapzen",
    f"dem:{polardem.SOURCE}": "Copernicus DEM (DLR, Airbus)",
    WEATHER_ID: "Open-Meteo",
}
# Every other source that built what is near the player (the user, 2026-10-18: "show all sources", not only those
# that ask for credit): the map data's datasets (the cells' provenance), land cover, canopy, sea depth, bridge types,
# airports, gravity. id -> short name.
FEATURES = {
    "map:Overture": "Overture Maps", "map:OpenStreetMap": "OpenStreetMap", "map:Microsoft ML Buildings": "Microsoft",
    "map:TomTom": "TomTom", "map:Esri Community Maps": "Esri", "map:Google Open Buildings": "Google",
    "map:USGS Lidar": "USGS Lidar", "map:doi:10.5281/zenodo.8174931": "Shi et al. (East Asia buildings)",
    "land:esa-worldcover": "ESA WorldCover", "canopy:meta-wri-chm": "Meta & WRI",
    "sea:noaa": "NOAA NCEI", "bridge:nbi": "FHWA NBI", "bridge:wikidata": "Wikidata",
    "air:ourairports": "OurAirports", "grav:egm2008": "NGA EGM2008",
}
SHORT.update(FEATURES)
GROUPS = [("img", "Imagery", "Far imagery"), ("dem", "Elevation", "Far elevation"), ("map", "Map", "Far map"),
          ("land", "Land cover", "Far land cover"), ("canopy", "Trees", "Far trees"), ("sea", "Sea depth", "Far sea"),
          ("bridge", "Bridges", "Far bridges"), ("air", "Airports", "Far airports"), ("wx", "Weather", "Far weather"),
          ("grav", "Gravity", "Far gravity")]
# Display order of the ids (unknown ones follow, by name)
ORDER = ([f"img:{n}" for n in IMAGERY] + [f"dem:{n}" for n in ELEVATION] + list(FEATURES) + [WEATHER_ID])


# Imagery.render's counts that are no source: the fallback colour, the relit and colour-matched shares, the leaf-off
# imagery's share (a count of other sources' texels; it showed as "leafon" in the overlay, 2026-10-18)
NOT_SOURCES = ("none", "polar", "deshadow", "colormatch", "leafon")


def _rank(i):
    return (ORDER.index(i), "") if i in ORDER else (len(ORDER), i)


def ordered(ids):
    return sorted(ids, key=_rank)


def full_label(i):
    """The source's full attribution wording (CREDITS.md)."""
    kind, _, name = i.partition(":")
    if kind == "img":
        return IMAGERY.get(name, name)
    if kind == "dem":
        return ELEVATION.get(name, name)
    if i == WEATHER_ID:
        return weathermod.CREDIT
    return FEATURES.get(i, name)


def label(i):
    """The overlay's short name of a source id."""
    return SHORT.get(i) or full_label(i)


def line(ids, far=False):
    """'Imagery: a, b | Elevation: c | Map: OpenStreetMap, Overture Maps | ... | Weather: Open-Meteo' (far: 'Far
    imagery: ...'), short names (label), in ORDER, each name once; '' for no ids."""
    groups = {}
    for i in ordered(ids):
        names = groups.setdefault(i.partition(":")[0], [])
        n = label(i)
        if n not in names:
            names.append(n)
    out = []
    for kind, title, far_title in GROUPS:
        if groups.get(kind):
            out.append(f"{far_title if far else title}: " + ", ".join(groups[kind]))
    for kind, names in groups.items():          # (a kind without a group: its names alone)
        if kind not in {g[0] for g in GROUPS}:
            out.append(", ".join(names))
    return " | ".join(out)


FEATURE_NEAR_M = 1500.0         # m: the map data, bridges and airports this close to the player are credited


def feature_ids(conn):
    """The ids of the non-tile sources that built what is near the player (FEATURES): the map data's datasets in
    the cells around it (their provenance: OpenStreetMap, Microsoft's buildings, TomTom, ...) once road tiles are
    shown, the land cover with the surface on, the canopy with trees shown, NOAA with sea tiles, the bridge
    inventories with a record near, OurAirports with a runway near, EGM2008 with real gravity. Cheap: cached
    provenance only (mapdata.layer_provenance), never a download."""
    import mapcells
    s = conn.s
    ids = set()
    anchor = getattr(s, "anchors", {}).get(conn.active)
    if anchor is None:
        return ids
    fx, fy = conn.focus
    lat, lon, _ = anchor.frame.enu_to_geodetic(fx, fy, 0.0)
    lat, lon = float(lat), float(lon)
    md = getattr(s, "mapdata", None)
    roads = getattr(conn, "roads", None)
    roads_on = md is not None and roads is not None and getattr(roads, "aid", None) == conn.active and roads.sent
    veg = getattr(conn, "veg", None)
    veg_on = getattr(s, "veg", None) is not None and veg is not None and getattr(veg, "aid", None) == conn.active \
        and veg.sent
    if md is not None and hasattr(md, "layer_provenance"):
        dlat = FEATURE_NEAR_M / 110540.0
        dlon = FEATURE_NEAR_M / (111320.0 * max(0.1, math.cos(math.radians(lat))))
        for key in mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon):
            layers = (("roads", "buildings", "water") if roads_on else ()) \
                + (("landcover",) if getattr(s, "surface", None) is not None else ()) \
                + (("canopy",) if veg_on else ())
            for layer in layers:
                p = md.layer_provenance(key, layer)
                if not isinstance(p, dict):
                    continue
                pid = p.get("id", "")
                if pid.startswith("overture:"):
                    ds = [d.get("dataset") for d in p.get("datasets") or [] if d.get("features", 1) > 0]
                    if ds:
                        ids.add("map:Overture")
                        ids |= {f"map:{d}" for d in ds if d}
                elif pid == "esa-worldcover":
                    ids.add("land:esa-worldcover")
                elif pid == "meta-wri-chm":
                    ids.add("canopy:meta-wri-chm")
    sea = getattr(conn, "sea_sent", None)
    if sea and sea[0] == conn.active and sea[1] and sea[1][0]:
        ids.add("sea:noaa")
    if roads_on:
        try:
            import bridgetypes
            if len(bridgetypes.nbi_near(lat, lon, FEATURE_NEAR_M)[0]):
                ids.add("bridge:nbi")
            if bridgetypes.wd_near(lat, lon, FEATURE_NEAR_M):
                ids.add("bridge:wikidata")
        except Exception:           # (the inventories are optional files)
            pass
    ap = getattr(s, "airports", None)
    db = getattr(ap, "db", None)
    if db is not None:
        try:
            if db.near(lat, lon, FEATURE_NEAR_M):
                ids.add("air:ourairports")
        except Exception:
            pass
    grav = getattr(getattr(conn, "gravity", None), "msg", None)
    if isinstance(grav, dict) and grav.get("source") == "egm2008":
        ids.add("grav:egm2008")
    return ids


def dem_keys(frame, bbox, zoom):
    """The (z, x, y) DEM tiles (dem.TILE px Web-Mercator grid) under an ENU footprint of `frame`,
    from a 3 x 3 grid of points on it (the footprint's edges bow on large tiles); at most
    MAX_DEM_KEYS, none beyond the Mercator limit."""
    x0, y0, x1, y1 = bbox
    E, N = np.meshgrid(np.linspace(x0, x1, 3), np.linspace(y0, y1, 3))
    lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
    lat, lon = np.ravel(lat), np.ravel(lon)
    if np.abs(lat).max() > demmod.MAX_LAT:
        return []
    n = 2 ** zoom
    fx = (lon / 360.0 + 0.5) * n
    fx = fx - n * np.round((fx - fx[0]) / n)                # unwrap across the antimeridian
    fy = (0.5 - np.arcsinh(np.tan(np.radians(lat))) / (2 * np.pi)) * n
    tx0, tx1 = int(math.floor(fx.min())), int(math.floor(fx.max()))
    ty0, ty1 = max(0, int(math.floor(fy.min()))), min(n - 1, int(math.floor(fy.max())))
    if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > MAX_DEM_KEYS:
        return []
    return [(zoom, tx % n, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]


def tile_sources(frame, bbox, dem, zoom, used):
    """Source ids of one built tile: 'img:<source>' for every imagery source with texels in
    `used` (Imagery.render's counts; None = no imagery), 'dem:<provider>' for every DEM tile
    under the footprint at `zoom` whose provider the Dem recorded (dem.served; a DEM without it,
    e.g. a test's synthetic one, gives none)."""
    used = used or {}
    ids = {f"img:{name}" for name, n in used.items() if name not in NOT_SOURCES and n > 0}
    if used.get("colormatch", 0) > 0:       # --colormatch: the S2 layers the correction came from
        ids |= {f"img:{name}" for name in colormatchmod.REFS}
    served = getattr(dem, "served", None)
    if served is not None and zoom is not None:
        for k in dem_keys(frame, bbox, zoom):
            p = served.get(k)
            if p:
                ids.add(f"dem:{p}")
    if callable(getattr(dem, "polar", None)) and polar_covered(frame, bbox, dem.polar()):
        ids.add(f"dem:{polardem.SOURCE}")
    return frozenset(ids)


def polar_covered(frame, bbox, polar):
    """Whether an ENU footprint of `frame` reaches past the Web-Mercator limit onto a polar DEM file (Antarctica;
    the Arctic Ocean has none), from a 5 x 5 grid of points on it."""
    x0, y0, x1, y1 = bbox
    E, N = np.meshgrid(np.linspace(x0, x1, 5), np.linspace(y0, y1, 5))
    lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
    cap = np.abs(lat) > demmod.MAX_LAT
    if not cap.any():
        return False
    try:
        return polar.covers(lat[cap], lon[cap])
    except (OSError, ValueError):
        return False


def bbox_distance(bb, x, y):
    """Distance from (x, y) to the nearest point of the box (0 inside)."""
    dx = max(bb[0] - x, 0.0, x - bb[2])
    dy = max(bb[1] - y, 0.0, y - bb[3])
    return math.hypot(dx, dy)


class Credits:
    """The server's credits setting (earthd --credits on): the near radius and its hysteresis, the
    throttle and the clock (injectable for tests)."""

    def __init__(self, near_m=NEAR_M, interval=INTERVAL_S, clock=time.monotonic, hyst_m=NEAR_HYST_M):
        self.near_m, self.interval, self.clock, self.hyst_m = near_m, interval, clock, hyst_m

    def message(self, conn, near_before=()):
        """The 'credits' message for a connection's shown tiles around its player, or None before
        anything is known (no anchor, no tile built; built tiles without sources, the polar ice, still send
        one). near_before: the ids near in the
        last message (they stay near within the hysteresis band)."""
        if conn.active is None:
            return None
        fx, fy = conn.focus
        tiles = conn.s.tiles
        near, far, kept = set(), set(), set()
        keep = frozenset(near_before)
        known = False                   # a shown tile was built (its sources known, perhaps none: the polar ice)
        for ck, bb in list(conn.shown.items()):
            info = tiles.get(ck)
            ids = getattr(info, "credits", None)
            known = known or ids is not None
            if not ids:
                continue
            d = bbox_distance(bb, fx, fy)
            if d <= self.near_m:
                near |= ids
            else:
                far |= ids
                if keep and d <= self.near_m + self.hyst_m:
                    kept |= ids & keep
        near |= kept
        if not near and not far and not known:
            return None
        if conn.weather.msg is not None:
            near.add(WEATHER_ID)
        try:
            near |= feature_ids(conn)
        except Exception:               # (a bug here must not lose the imagery credits)
            log.exception("credits: feature sources failed")
        far -= near
        return {"type": "credits", "v": VERSION, "near": line(near), "far": line(far, far=True),
                "ids": ordered(near), "farIds": ordered(far)}


class Tracker:
    """Per connection: the last credits message sent (re-sent on hello) and the throttle (one
    check per Credits.interval; a poke during the wait is folded into the scheduled check)."""

    def __init__(self):
        self.msg = None
        self.last_check = -math.inf
        self.timer = None
        self.checks = 0

    def poke(self, conn):
        """Something the credits depend on may have changed (a pose, a swap): check once the
        interval since the last check has passed."""
        cr = conn.s.credits
        if cr is None or self.timer is not None:
            return
        delay = max(0.0, self.last_check + cr.interval - cr.clock())
        self.timer = conn.loop.call_later(delay, self.check, conn)

    def check(self, conn):
        self.timer = None
        cr = conn.s.credits
        if cr is None:
            return
        self.last_check = cr.clock()
        self.checks += 1
        try:
            msg = cr.message(conn, self.msg["ids"] if self.msg else ())
        except Exception:               # a bug must not stop the connection
            log.exception("credits failed")
            return
        if msg is not None and msg != self.msg:
            self.msg = msg
            conn.send(msg)

    def close(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
