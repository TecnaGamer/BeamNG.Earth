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
# What a car hits and what must clear a carriageway (furniture.COLLISION_BOXES; furniture.RoadClearance), for every
# group a style can name. Read 2026-11-11 from each shape's Colmesh (bne_eu_pole_*, bne_fr_wood_neutral,
# bne_pole_concrete_recessed: all modelled 8.5 m tall, so a row's height over 8.5 is its scale), bounds rounded outwards
# to 1 cm: the trunk or the A-frame's legs, not the cross-arm. A group without a box is "clear" everywhere, and the
# Czech A-frame poles and the others stood in the lanes of Police nad Metuji's roundabout (the user, 2026-11-11).
_TRUNK = (-0.12, -0.14, -0.01, 0.15, 0.14, 8.51)
_POLE_BOXES = {
    "pole_concrete_recessed": (-0.21, -0.14, -0.01, 0.21, 0.14, 8.51),
    "fr_wood_pole": (-0.15, -0.13, -0.01, 0.15, 0.13, 8.51),
    "eu_wood_pole": (-0.15, -0.13, -0.01, 0.15, 0.13, 8.51),
    "eu_pole_wood_a": (-0.76, -0.11, -0.02, 0.76, 0.11, 8.52),
    "eu_pole_wood_h": (-1.01, -0.11, -0.01, 1.01, 0.11, 8.51),
}
COLLISION_BOXES = {g: (8.5, (b,)) for g, b in _POLE_BOXES.items()}
FR_KIT = "fr-varied-furniture-v3"
EU_KIT = europeinfra.VERSION
# Explicit country/territory coverage, including the requested RU/TR presets.
# Overseas territories keep their own codes. Geometry families can be shared.
EUROPE_COUNTRIES = europeinfra.COUNTRIES
for _family in europeinfra.POLE_LAYOUTS:
    COLLISION_BOXES.setdefault("eu_pole_" + _family, (8.5, (_TRUNK,)))
POLE_GROUPS = ("pole",) + tuple(g for g in COLLISION_BOXES)
FR_DEMO = "fr-recessed-demo-v1"
FR_ALL = "fr-recessed-all-v1"


def profile_for(country, mode="off"):
    """Unknown/unsupported countries fail closed to stock. No geographic override."""
    country = str(country or "").strip().upper()
    if mode == "poles" and country in EUROPE_COUNTRIES:
        return EU_KIT+':'+country
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
    if mode not in ("fr-demo", "fr-all", "fr-varied", "europe", "poles"):
        return None
    import countrybounds
    if mode == "poles":
        # the country's own poles only (the user, 2026-11-07: "the poles we have are not the local ones"): no town or
        # direction signs, stops, guard rails or French kit, so no data of theirs in the cache token either
        import citylamps
        return (countrybounds.ALGORITHM, mode, countrybounds.signature() + ":" + EU_KIT + ":" + citylamps.VERSION)
    import townlayout
    fingerprint = countrybounds.signature()+":"+townlayout.VERSION
    if mode in ("fr-varied", "europe"):
        import townsigns
        import directionsigns
        import signmesh
        fingerprint += ":" + townsigns.DATA_SHA256 + ":" + directionsigns.DATA_SHA256 + ":" + FR_KIT + ":" + EU_KIT
        fingerprint += ':'+townsigns.VERSION+':'+signmesh.font_signature()
        import euguardrails
        if euguardrails.enabled():
            import frlocaldirections
            import frmotorwaymarkers
            import roadphones
            fingerprint += ':'.join(('', euguardrails.VERSION, roadphones.signature(), directionsigns.ROAD_DATA_SHA256,
                                     frlocaldirections.SIGNATURE, frmotorwaymarkers.SIGNATURE))
        if mode=='europe':
            import regionalstops
            import delineatorprofiles
            fingerprint += ':'+regionalstops.VERSION+':'+delineatorprofiles.VERSION    # (the places / Quebec data: countrybounds.signature)
            import citylamps
            fingerprint += ':'+citylamps.VERSION
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


def lamp_country_selector(profile, frame):
    """x, y, z -> the ISO country code where the country's own street lamp posts stand (the "poles" and "europe" modes:
    Europe and its neighbours), else None (the stock post): citylamps.localize's gate."""
    if not (isinstance(profile, (tuple, list)) and len(profile) == 3 and profile[1] in ("poles", "europe")):
        return lambda x, y, z: None
    selector(profile, frame)
    import countrybounds
    index = countrybounds.bundled()

    def at(x, y, z):
        lat, lon, _ = frame.enu_to_geodetic(x, y, z)
        country = index.at(float(lat), float(lon))
        return country if country in EUROPE_COUNTRIES else None
    return at


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
