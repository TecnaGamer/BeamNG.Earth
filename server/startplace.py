"""Where the game's level start will teleport the player (the mod's core.lua startTeleport / defaultStart, read from the
game's own files in its user folder): the server loads that place while the game itself loads (earthd Server.preload
"start"; an outside review, 2026-11-13: the level load is 10 to 30 s of otherwise idle time).

The mod's order: the place picked in the level select (settings/cloud/game-state.json, levels.beamng_earth's
defaultSpawnPointName: "bne_place_<id>", one of the saved places in settings/beamngEarth_places.json; "bne_random": a
random road, unknown here), else where the car was last (settings/beamngEarth_lastpos.json, places.notePosition),
else the newest recent place. The game writes its file names in lower case on Linux (cpupin.player_settings).
"""
import json
import math
import os

GAME_STATE = ("settings", "cloud", "game-state.json")
PLACES = ("settings", "beamngEarth_places.json")
LASTPOS = ("settings", "beamngEarth_lastpos.json")
MAX_BYTES = 1 << 20     # a file larger than this is not read (none of the three is near it; never a huge parse)
LEVEL = "beamng_earth"
RANDOM = "bne_random"
PREFIX = "bne_place_"


def _read(user_dir, parts):
    """The JSON of user_dir/parts (its last part in lower case too), or None (none, unreadable, over MAX_BYTES)."""
    head, name = parts[:-1], parts[-1]
    for n in dict.fromkeys((name, name.lower())):
        try:
            with open(os.path.join(user_dir, *head, n), "rb") as f:
                data = f.read(MAX_BYTES + 1)
        except OSError:
            continue
        if len(data) > MAX_BYTES:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
    return None


def _latlon(d):
    if not isinstance(d, dict):
        return None
    try:
        lat, lon = float(d["lat"]), float(d["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def start_point(user_dir):
    """(lat, lon, why) of the level start's teleport, or None (no user folder, a random start, nothing saved yet)."""
    if not user_dir:
        return None
    gs = _read(user_dir, GAME_STATE)
    lv = gs.get("levels", {}).get(LEVEL) if isinstance(gs, dict) and isinstance(gs.get("levels"), dict) else None
    name = lv.get("defaultSpawnPointName") if isinstance(lv, dict) else None
    places = _read(user_dir, PLACES)
    places = places if isinstance(places, dict) else {}
    if name == RANDOM:
        return None
    if isinstance(name, str) and name.startswith(PREFIX):
        try:
            pid = int(name[len(PREFIX):])
        except ValueError:
            pid = None
        for p in places.get("places") or []:
            if isinstance(p, dict) and p.get("id") == pid:
                got = _latlon(p)
                if got:
                    return got[0], got[1], "the level select's place"
    got = _latlon(_read(user_dir, LASTPOS))
    if got:
        return got[0], got[1], "where the car was last"
    recent = places.get("recent") or []
    got = _latlon(recent[0]) if recent else None
    if got:
        return got[0], got[1], "the newest recent place"
    return None
