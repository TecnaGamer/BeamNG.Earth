"""Opt-in regional pole experiment; geometry families are not country-exclusive.

The FR demo deliberately mixes two models by stable road id. Its 50/50 weight is
an A/B visual-test setting, not a measured French equipment distribution. FR all
uses concrete for every existing roadside pole classified as French. FR varied
prefers wood and enables the French furniture/sign kit. All mixes are trial settings.
Placement still comes from furniture's heuristics, not mapped power=pole nodes.
"""
from dataclasses import dataclass
import hashlib
import europeinfra


@dataclass(frozen=True)
class PoleStyle:
    group: str
    height: float
    # None preserves the stock shape's existing three-wire layout exactly.
    wire_anchors: tuple | None = None
    wire_radius: float | None = None


STOCK = PoleStyle("pole", 9.2)
RECESSED = PoleStyle("pole_concrete_recessed", 8.5, ((0.25, 0.0, 7.85),), 0.025)
WOOD = PoleStyle("fr_wood_pole", 8.5, RECESSED.wire_anchors, RECESSED.wire_radius)
EU_WOOD = PoleStyle("eu_wood_pole", 8.5, RECESSED.wire_anchors, RECESSED.wire_radius)
FR_KIT = "fr-varied-furniture-v3"
EU_KIT = europeinfra.VERSION
# Explicit country/territory coverage, including the requested RU/TR presets.
# Overseas territories keep their own codes. Geometry families can be shared.
EUROPE_COUNTRIES = europeinfra.COUNTRIES
FR_DEMO = "fr-recessed-demo-v1"
FR_ALL = "fr-recessed-all-v1"


def profile_for(country, mode="off"):
    """Unknown/unsupported countries fail closed to stock. No geographic override."""
    country = str(country or "").strip().upper()
    if mode == "europe" and country in EUROPE_COUNTRIES:
        return FR_KIT if country == "FR" else EU_KIT+':'+country
    if country != "FR":
        return None
    return {"fr-demo": FR_DEMO, "fr-all": FR_ALL, "fr-varied": FR_KIT}.get(mode)


def choose(profile, road_id):
    """One style for the whole road, independent of tile, anchor and load order."""
    if profile == EU_KIT:
        return EU_WOOD
    if isinstance(profile,str) and profile.startswith(EU_KIT+':'):
        country=profile[len(EU_KIT)+1:];kit=europeinfra.KITS.get(country)
        if kit is None:return STOCK
        h=hashlib.blake2b(('eu-pole-v3:'+country+':'+str(road_id)).encode(),digest_size=8).digest()
        family=kit.poles[int.from_bytes(h,'little')%len(kit.poles)]
        if family=='fr_wood':return WOOD
        if family=='fr_concrete':return RECESSED
        height,anchors=europeinfra.POLE_LAYOUTS[family]
        return PoleStyle('eu_pole_'+family,height,anchors,.012)
    if profile == FR_ALL:
        return RECESSED
    if profile == FR_KIT:
        # Trial preference for rural/village scenery, not a national statistic.
        h = hashlib.blake2b(("fr-wood-v1:" + str(road_id)).encode(), digest_size=8).digest()
        return WOOD if int.from_bytes(h, "little") < 3 * 2 ** 62 else RECESSED
    if profile == FR_DEMO:
        h = hashlib.blake2b(("pole-style-v1:" + str(road_id)).encode("utf-8"), digest_size=8).digest()
        if int.from_bytes(h, "little") < 2 ** 63:
            return RECESSED
    return STOCK


def geographic_profile(mode):
    """Serializable worker/cache token, including algorithm AND boundary data version."""
    if mode not in ("fr-demo", "fr-all", "fr-varied", "europe"):
        return None
    import countrybounds
    fingerprint = countrybounds.DATA_SHA256
    if mode in ("fr-varied", "europe"):
        import townsigns
        import directionsigns
        fingerprint += ":" + townsigns.DATA_SHA256 + ":" + directionsigns.DATA_SHA256 + ":" + FR_KIT + ":" + EU_KIT
        if mode=='europe':
            import regionalstops
            import delineatorprofiles
            fingerprint += ':'+townsigns.EU_DATA_SHA256+':'+regionalstops.DATA_SHA256+':'+delineatorprofiles.VERSION
    return (countrybounds.ALGORITHM, mode, fingerprint)


def selector(profile, frame):
    """Resolve a pole's country at its actual position in the tile's ENU frame.

    Legacy profile strings remain usable by small, geographic-independent tests.
    The server only supplies geographic_profile tokens for enabled CLI modes.
    """
    if not isinstance(profile, (tuple, list)):
        return lambda fid, x, y, z: choose(profile, fid)
    import countrybounds
    if len(profile) != 3 or tuple(profile) != geographic_profile(profile[1]):
        raise ValueError("Unknown regional pole/country data profile")
    if frame is None:
        raise ValueError("Regional country lookup needs the road tile's ENU frame")
    index = countrybounds.bundled()
    mode = profile[1]

    def at(fid, x, y, z):
        lat, lon, _ = frame.enu_to_geodetic(x, y, z)
        return choose(profile_for(index.at(float(lat), float(lon)), mode), fid)
    return at


def kit_enabled(profile):
    return profile == FR_KIT or (isinstance(profile, (tuple, list)) and len(profile) == 3 and profile[1] in ("fr-varied", "europe"))


def country_selector(profile,frame):
    """Country at the prop, also used by the subnational STOP appearance gate."""
    if profile==FR_KIT:return lambda x,y,z:'FR'
    if not kit_enabled(profile):return lambda x,y,z:None
    selector(profile,frame)
    import countrybounds
    index=countrybounds.bundled();mode=profile[1]
    def at(x,y,z):
        lat,lon,_=frame.enu_to_geodetic(x,y,z)
        country=index.at(float(lat),float(lon))
        return country if mode=='europe' or country=='FR' else None
    return at


def kit_selector(profile, frame):
    """French kit at this position, independent of its pole variant."""
    if not kit_enabled(profile):
        return lambda x, y, z: False
    if profile == FR_KIT:
        return lambda x, y, z: True
    selector(profile, frame)  # validates token and frame, with no airport fallback
    import countrybounds
    index = countrybounds.bundled()
    def at(x, y, z):
        lat, lon, _ = frame.enu_to_geodetic(x, y, z)
        return index.at(float(lat), float(lon)) == "FR"
    return at


def europe_selector(profile, frame):
    """Country gate for sidewalk materials; junction controls stay upstream."""
    if profile == FR_KIT:
        return lambda x, y, z: True
    if profile == EU_KIT:
        return lambda x, y, z: True
    if not kit_enabled(profile):
        return lambda x, y, z: False
    selector(profile, frame)
    import countrybounds
    index = countrybounds.bundled()
    countries = EUROPE_COUNTRIES if profile[1] == "europe" else {"FR"}
    def at(x, y, z):
        lat, lon, _ = frame.enu_to_geodetic(x, y, z)
        return index.at(float(lat), float(lon)) in countries
    return at
