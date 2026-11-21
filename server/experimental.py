"""The settings page's Experimental switches (the user, 2026-11-15: "add the toggles for the features that can only be
enabled via environment variables"; the French / European scenery as one of them): each one a setting of the mod's
settings file (settings/beamngEarth.json in the game's user folder: settings.lua SCHEMA, earthSettingsStore.js's
Experimental page) and the BNE_* build switches it stands for.

The switches are read when a module is imported (railways.RAIL_COLLIDE, roadsigns.ON, grading.GUARD_ON ...), in the
server's main process and in every worker process it starts, so a switch must be in the environment before the server
starts: launcher.start_server (the app) and start.py (a source install, run_server.sh / .bat) put env_for(...) into
the server's environment, never the server itself part way (a worker on, the main process off). A change on the page
holds from the next start of BeamNG.Earth.

Defaults: every switch off, as before, and NOTHING added to the environment for a switch that is off: every BNE_*
variable set is in the cache keys (revisit.code_key / switches_key: the road plans, cell facts, road tiles, blocks;
decisions.vkey_doc: the shared decision archives, built with the default switches), so an "off" written out would make
other keys for the same world and lose the archives. A variable already in the environment (set by hand) is kept.
The switches proven to change no plan and no fact (revisit.PLAN_NEUTRAL, tests/test_plan_neutral.py) are left out of
the plans' and facts' keys and the archives' (revisit.plans_switches_key): with only those on, the archives still hold.
"""
import json
import os

SETTINGS_FILE = ("settings", "beamngEarth.json")    # (cpupin.MOD_SETTINGS; the game writes it in lower case on Linux)

# (setting id, {variable: value when on}, the setting it needs on as well or None). The order is the page's.
FEATURES = (
    ("roadSigns", {"BNE_ROADSIGNS": "on"}, None),                         # roadsigns.ON
    ("expNlRoadSigns", {"BNE_NL_ROADSIGNS": "on"}, "roadSigns"),           # roadsigns.NL_ON (only with ON)
    # The road revamp's switches as one (the user, 2026-11-17: "if revamped roads arent enabled by default in the dev
    # builds yet make it a single experemental toggle"); each later revamp switch joins this set, not the page
    # (BNE_LEVEL_SPACING left out: on the 33-place ride corpus it made perth_915 and a5_underpass worse, 2026-11-17)
    ("expRoadRevamp", {"BNE_SPEED_TAPERS": "on", "BNE_DECK_K_SPEED": "on",
                       "BNE_FOOT_CLEAR": "on", "BNE_RAIL_LIFECYCLE": "on", "BNE_RACEWAY_DIG": "on",
                       "BNE_TUNNEL_ROAD_OVER": "on",
                       # the ski-ramp fixes (the clearance study's causes 1-3; BNE_UNDER_SPEED left out: no gain)
                       "BNE_FAST_DECK_ENDS": "on", "BNE_FAST_GAP": "on", "BNE_FAST_DECK_GROUND": "on",
                       # racetracks without street poles (BNE_POWER_YARDS left out: one open Codex high, feeders
                       # classified differently by the span and yard tiles)
                       "BNE_RACEWAY_PROPS": "on",
                       # railways at roads (the user's reports r61-r64, r80, r92)
                       "BNE_RAIL_ROAD_FIT": "on", "BNE_RAIL_SEAM_NATURAL": "on"}, None),
    # (BNE_BATTER=vanilla, the vanilla-style cut and fill slopes of road revamp v2 P1a, left out: nothing joins this
    # set before the user has looked at it in game; a variable of its own until then, off by default)
    ("expEuScenery", {"BNE_EU_ROAD_DETAILS": "on", "BNE_FR_ROADS": "on", "BNE_FR_HOUSES": "on"}, None),
    ("expDropRails", {"BNE_DROP_RAILS": "on"}, None),                     # droprails.enabled
    ("expEntranceFlares", {"BNE_ENTRANCE_FLARES": "on"}, None),           # roads.flares_enabled
    ("expPriorityStops", {"BNE_PRIORITY_STOPS": "on"}, None),             # furniture.PRIORITY_STOPS
    ("expRoadBumps", {"BNE_BUMPS": "1"}, None),                           # bumps.SCALE
    ("expRoughDirt", {"BNE_DIRT_BUMP": "1", "BNE_DIRT_BANK": "0.015"}, None),   # grading.DIRT_ROUGH / DIRT_BANK_M
    ("expWearStrips", {"BNE_WEAR_STRIPS": "on"}, None),                   # roadwear.STRIPS_ON
    ("expGradeGuard", {"BNE_GRADE_GUARD": "on"}, None),                   # grading.GUARD_ON
    ("expPhotoSurface", {"BNE_PHOTO_SURFACE": "1"}, None),                # photosurface.ON
    ("expTerrainBumps", {"BNE_TERRAIN_BUMPS": "on"}, None),               # grading.TERRAIN_BUMPS_ON
    ("expCliffWalls", {"BNE_CLIFF_WALLS": "on"}, None),                   # roads.CLIFF_WALLS_ON
    ("expTunnelLids", {"BNE_TUNNEL_LIDS": "on"}, None),                   # tunnellids.ON
    ("expSubseaTunnels", {"BNE_SUBSEA_RELAX": "on"}, None),               # subsea.RELAX
    ("expAiTunnels", {"BNE_AI_TUNNELS": "on"}, None),                     # navgraph.AI_TUNNELS
    ("expRailCollide", {"BNE_RAIL_COLLIDE": "on"}, None),                 # railways.RAIL_COLLIDE
    ("expCatenary", {"BNE_CATENARY": "1"}, None),                         # catenary.ENABLED
    ("expRailCrossings", {"BNE_RAIL_XING": "1"}, None),                   # railxing.ENABLED
    ("expRailTunnels", {"BNE_RAIL_TUNNELS": "on"}, None),                 # railways.TUNNELS_ON
    ("expRailCurves", {"BNE_RAIL_CURVES": "on"}, None),                   # roadsmooth.RAIL_ON
    ("expWatertight", {"BNE_WATERTIGHT": "on"}, None),                    # shells.ON
    ("expCanadaLidar", {"BNE_LIDARBC": "1", "BNE_HRDEM_NEAR": "1"}, None),     # dem.LIDARBC / HRDEM_NEAR
    ("expGenGround", {"BNE_GEN_GROUND": "on"}, None),                     # genground.ON
    ("expStaleRevisit", {"BNE_STALE_REVISIT": "on"}, None),               # revisit.STALE_ON (not a key: NOT_SWITCHES)
)
IDS = tuple(f[0] for f in FEATURES)
# A setting that counts only in a file of this version or newer (settings.lua M.VERSION): the Road signs switch was on
# by default (and hidden) before version 3, its true then was the old default, not the player's choice
SINCE_VERSION = {"roadSigns": 3}


def read_settings(user_dir):
    """The mod's settings file in the game's user folder as a dict ({} when there is none or it is unreadable)."""
    if not user_dir:
        return {}
    for name in (SETTINGS_FILE[1], SETTINGS_FILE[1].lower()):
        try:
            with open(os.path.join(user_dir, SETTINGS_FILE[0], name), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        return d if isinstance(d, dict) else {}
    return {}


def chosen(settings):
    """The ids of FEATURES the settings dict turns on (a value of exactly true; its needed setting on too)."""
    try:
        version = int(settings.get("version") or 1)
    except (TypeError, ValueError, OverflowError):
        version = 1

    def on(sid):
        return settings.get(sid) is True and version >= SINCE_VERSION.get(sid, 0)

    return [sid for sid, _env, needs in FEATURES if on(sid) and (needs is None or on(needs))]


def env_for(settings):
    """{variable: value} of the switches the settings dict turns on (nothing for a switch that is off)."""
    by_id = {sid: env for sid, env, _needs in FEATURES}
    out = {}
    for sid in chosen(settings):
        out.update(by_id[sid])
    return out


def apply(environ, user_dir):
    """The player's Experimental switches (the settings file in user_dir) put into environ (a server's environment,
    before it starts), a variable already set there kept. Returns the ids turned on, for the log."""
    d = read_settings(user_dir)
    ids = chosen(d)
    for k, v in env_for(d).items():
        environ.setdefault(k, v)
    return ids
