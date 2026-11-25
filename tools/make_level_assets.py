"""Generates the static files of the mod level (JSON + small PNG/JPG assets).

  python3 tools/make_level_assets.py mod/beamng_earth/levels/beamng_earth
"""
import json, os, sys
from PIL import Image, ImageDraw

L = sys.argv[1]  # .../mod/beamng_earth/levels/beamng_earth
T = os.path.join(L, "art/terrains")
os.makedirs(T, exist_ok=True)

def lines(path, objs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for o in objs:
            f.write(json.dumps(o, separators=(",", ":")) + "\n")

def pretty(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")

# The level select's card: the whole Earth's details as the stock levels have theirs (the same as the mod's
# levelinfo.lua BASE, which writes this file over again in the user folder with a spawn point per saved place). size:
# the equator's 40,075 km by 12,728 km, whose product is the Earth's surface (510,072,000 km2).
pretty(os.path.join(L, "info.json"), {
    "title": "BeamNG.Earth",
    "description": "The whole Earth, streamed around you by the BeamNG.Earth companion server. Start the server, then "
                   "pick a place below or teleport from the BeamNG.Earth window. Preview: Sentinel-2 cloudless 2016 by "
                   "EOX IT Services GmbH (CC BY 4.0).",
    "previews": ["beamng_earth_preview.jpg"],
    "size": [40075017, 12727986],
    "biome": "Every biome on Earth: oceans, deserts, rainforests, forests, grasslands, tundra, mountains and ice",
    "roads": "About 64 million km of roads in every country: motorways, highways, city streets, rural and dirt roads",
    "suitablefor": "Freeroam, road trips, exploring, off-roading",
    "features": "The real planet from open data: terrain, roads, bridges, tunnels, buildings, forests, lakes, seas, "
                "airports and live weather",
    "country": "Every country",
    "authors": "BeamNG.Earth",
    "defaultSpawnPointName": "spawn_default",
    "supportsTraffic": True,      # the stock traffic / parked-car choice at the level start (2026-10-14)
    "supportsTimeOfDay": True,
    "spawnPoints": [
        {"objectname": "spawn_default", "translationId": "Where you left off", "preview": "beamng_earth_preview.jpg",
         "description": "The last place you teleported to."},
        {"objectname": "bne_random", "translationId": "Random location", "preview": "beamng_earth_preview.jpg",
         "description": "A road somewhere in the world, picked at random each time."},
    ],
})

lines(os.path.join(L, "main/items.level.json"), [
    {"name": "MissionGroup", "class": "SimGroup", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c01", "enabled": "1"},
])
lines(os.path.join(L, "main/MissionGroup/items.level.json"), [
    {"name": "PlayerDropPoints", "class": "SimGroup", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c02", "__parent": "MissionGroup", "enabled": "1"},
    {"name": "Sky", "class": "SimGroup", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c03", "__parent": "MissionGroup"},
    {"name": "Weather", "class": "SimGroup", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c20", "__parent": "MissionGroup"},
])
lines(os.path.join(L, "main/MissionGroup/PlayerDropPoints/items.level.json"), [
    {"name": "spawn_default", "class": "SpawnSphere", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c04", "__parent": "PlayerDropPoints",
     "position": [0, 0, 0.5], "autoplaceOnSpawn": "0", "dataBlock": "SpawnSphereMarker", "enabled": "1", "homingCount": "0",
     "indoorWeight": "1", "isAIControlled": "0", "lockCount": "0", "outdoorWeight": "1", "radius": 5, "sphereWeight": "1"},
])
# Sky group: LevelInfo/ScatterSky/TimeOfDay as in smallgrid's Sky/items.level.json, with the
# view distance raised for the far field and no GroundPlane (the CloudLayer is in the Weather group).
lines(os.path.join(L, "main/MissionGroup/Sky/items.level.json"), [
    {"name": "theLevelInfo", "class": "LevelInfo", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c05", "__parent": "Sky",
     "canvasClearColor": [1, 1, 1, 255], "enabled": "1", "fogAtmosphereHeight": 1000, "fogColor": [0.741176009, 0.815685987, 0.925490022, 1],
     "fogDensity": 4e-05, "globalEnviromentMap": "BNG_Sky_01_cubemap", "gravity": -9.81000042, "nearClip": 0.1, "visibleDistance": 100000,
     # (time of day, C at sea level) pairs: ISA 15 C until real weather sets its own (weather.lua)
     "temperatureCurveC": [0, 15, 1, 15]},
    {"name": "sunsky", "class": "ScatterSky", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c06", "__parent": "Sky",
     "position": [0, 0, 100], "ambientScale": [1, 0.894117653, 0.78039217, 1], "ambientScaleGradientFile": "art/sky_gradients/default/gradient_ambient.png",
     "azimuth": 57.2957764, "colorize": [0.219607845, 0.349019617, 0.607843161, 1], "colorizeGradientFile": "art/sky_gradients/default/gradient_colorize.png",
     "elevation": 144, "enableFogFallBack": False, "flareScale": 5, "flareType": "BNG_Sunflare_3", "fogScale": [0.396078438, 0.666666687, 1, 1],
     "fogScaleGradientFile": "art/sky_gradients/default/gradient_fog.png", "mieScattering": 0.000429999985, "moonLightColor": [1, 1, 1, 1],
     "moonAngularSize": 0.67, "moonMat": "Moon_Mat", "moonScale": 0.300000012, "nightColor": [1, 0.894117653, 0.78039217, 1],
     "nightCubemap": "nightCubemap", "nightFogColor": [0.396078438, 0.666666687, 1, 1], "nightFogGradientFile": "art/sky_gradients/default/gradient_fog.png",
     "nightGradientFile": "art/sky_gradients/default/gradient_ambient.png", "occlusionScale": 0.0250000004, "shadowDarkenColor": [0, 0, 0, 0],
     "skyBrightness": 40, "sunScale": [0.996078432, 0.811764717, 0.70588237, 1], "sunScaleGradientFile": "art/sky_gradients/default/gradient_sunscale.png",
     "useNightCubemap": True},
    # TimeOfDay.time: 0 = noon, 0.5 = midnight (ARCHITECTURE.md Phase 0). latitude/longitude are
    # read by core/celestial.lua (readWhen) and follow the player at runtime. dayLength: a real day (GitHub #791: without
    # it the engine's 30-minute day ran whenever the sky was set to Manual; the game's own day-length menu offers the
    # level's authored value as its "level default").
    {"name": "tod", "class": "TimeOfDay", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c07", "__parent": "Sky",
     "position": [0, 0, 100], "axisTilt": 10, "play": False, "dayLength": 86400, "startTime": 0.1, "time": 0.1,
     "latitude": 36.5785, "longitude": -118.2923},
])

# Weather group: the objects every official level has, so core_environment, the pause menu's
# Time & Weather panel and the weather presets (core_weather) find them
# (docs/research/2026-10-10-game-feature-quick-wins.md §5.1). None is positional: the clouds
# follow the ground under the player (sky.lua), the precipitation follows the camera and the reverb
# is 2D, so the floating origin does not matter. Precipitation: BWM_Weather's verified 0.39 setup
# (rain_drop; the pool `numDrops` is set when it rains, then setPercentage); no `numOfDrops` field,
# so BWM_Weather still uses its own rain object here. rotateWithCamVel off: on, the drops were drawn as streaks
# across the whole screen at odd angles even with the car standing still (the user's "raindrops jumping /
# streaking", Fells Point; in-game 2026-10-17: each of the three drop options off in turn, only this one fixed it).
# dropSize 0.2 as west_coast_usa's rain (the same rain_drop in a 50 m box): at 0.36 a drop beside the camera was
# still a line across the screen.
# The weather plan 2026-11-24, phase A (its bugs B3, B4, B6, P10): every drop fades out inside its box (fadeDistEnd
# under the half width: the 50 m box faded only at 65 m, so drops popped in and out at its faces 25-35 m away, still
# 50-70 % opaque, and followCam wrapped them onto the front face while driving), a smaller box for a denser rain where it
# is seen; no splashes (the star sprites hung in the air at drops' cut-offs on invisible colliders, GitHub #615, also on
# DX11); no turbulence on the rain (drops stretched into lines near the camera); snow collides (it fell through every
# roof, bridge and tunnel) and draws real flakes (bngEarthSnowflake: art/weather/precipitation.datablocks.json,
# tools/make_precip_textures.py; Snow_menu's sheet of specks drew centimetre dots: invisible). weather.lua sets the
# flake size and speeds by temperature (M.FLAKES) and fades both out under roofs.
os.makedirs(os.path.join(L, "art/weather"), exist_ok=True)
with open(os.path.join(L, "art/weather/precipitation.datablocks.json"), "w") as f:
    json.dump({"bngEarthSnowflake": {"name": "bngEarthSnowflake", "class": "PrecipitationData",
                                     "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c26",
                                     "dropTexture": "/levels/beamng_earth/art/weather/bngEarth_snowflake.color.dds",
                                     "dropsPerSide": 2, "splashesPerSide": 1},
               # (the rain's own streak, brighter than rain_drop's: the stock one read as dry at noon)
               "bngEarthRainDrop": {"name": "bngEarthRainDrop", "class": "PrecipitationData",
                                    "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c27",
                                    "dropTexture": "/levels/beamng_earth/art/weather/bngEarth_raindrop.color.dds",
                                    "splashTexture": "art/shapes/particles/Particle_rain_splash.dds",
                                    "dropsPerSide": 1, "splashesPerSide": 1}}, f, indent=2)
    f.write("\n")
lines(os.path.join(L, "main/MissionGroup/Weather/items.level.json"), [
    {"name": "bngEarthClouds", "class": "CloudLayer", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c21", "__parent": "Weather",
     "texture": "art/skies/clouds/clouds_normal_displacement.png", "coverage": 0.5, "cirrusCoverage": 0.3, "exposure": 1.45,
     "windSpeed": 0.1, "baseColor": [0.99, 0.99, 0.99, 0.9], "altitudeKm": 1.5},
    {"name": "bngEarthRain", "class": "Precipitation", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c22", "__parent": "Weather",
     "dataBlock": "bngEarthRainDrop", "numDrops": 0, "boxWidth": 36, "boxHeight": 24, "dropSize": 0.2, "splashSize": 0,
     "fadeDist": 2, "fadeDistEnd": 17, "followCam": True, "useWind": True, "useTurbulence": False, "doCollision": True,
     "hitVehicles": False, "rotateWithCamVel": False},
    {"name": "bngEarthSnow", "class": "Precipitation", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c23", "__parent": "Weather",
     "dataBlock": "bngEarthSnowflake", "numDrops": 0, "boxWidth": 30, "boxHeight": 20, "dropSize": 0.08, "splashSize": 0,
     "minSpeed": 0.7, "maxSpeed": 1.1, "fadeDist": 2, "fadeDistEnd": 14, "followCam": True, "useWind": True,
     "useTurbulence": True, "maxTurbulence": 0.45, "turbulenceSpeed": 0.2, "doCollision": True, "hitVehicles": False,
     "rotateWithCamVel": False},
    {"name": "bngEarthWind", "class": "ForestWindEmitter", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c24", "__parent": "Weather",
     "position": [0, 0, 0], "strength": 0.5},
    {"name": "bngEarthReverb", "class": "SFXEmitter", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c25", "__parent": "Weather",
     "position": [0, 0, 0], "fadeInTime": 0, "fadeOutTime": 0, "is3D": False, "isLooping": False, "maxDistance": 0.01,
     "referenceDistance": 0, "track": "snapshot:>Reverb_WC_Open", "useTrackDescriptionOnly": True},
])

# Terrain material: the template level's "Dirt" (v1.5 PBR) with detail/macro textures taken
# straight from the always-mounted /assets/materials/terrain zip and solid base textures shipped
# here (means of template's t_terrain_base_dirt_*), so nothing depends on another level's zip.
A = "/assets/materials/terrain/soil/"
B = "/levels/beamng_earth/art/terrains/"
BASE_TEX = 2048     # base texture slot (px): the shipped flat base maps and the server's photo (earthd.BASE_PX)
# Every texture slot of every material is filled: an empty one makes the engine build a default texture at
# every TerrainBlock:updateMaterial (~0.8 s per call with the surface materials, in-game 2026-10-12)
FLAT_H = B + "bngEarth_flat_h_1024.png"     # neutral height for the detail / macro height slots
# The server's surface materials (server/surface.py MATERIALS, the tyres' ground models), each with its
# own stock detail / macro textures for the detail near-visual mode (terrain.lua LOOKS has the same
# choices; in-game 2026-10-12: TerrainMaterials made at runtime cost ~1 s per updateMaterial, so they are
# level materials now). Texture sizes are in heightmap samples (TEX_SQ: 1 m on the shipped block since 2026-11-01;
# below in 2 m units, doubled: detail 2-4 m, macro 32-64 m, dividing the 128 m block step); the colour comes from the
# photo base in detail mode.
TEX_SQ = 2          # samples per 2 m (the square is heightmap.SQUARE = 1 m)
T_ = "/assets/materials/terrain/"
TL_ = "/temp/beamngEarth/terraintex/"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import terraintex   # noqa: E402  (rc_name: the zero-mean colour copies the server writes)
# The near/far border (the user, 2026-11-16: "the texture between the ground with collision vs no collision is very
# different. and there's a visible border"): past the drawn block's edge the far tiles show the photo alone, so every
# layer's macro (colour, normal, roughness: the engine fades them all by macroDistAtten) is gone before the nearest
# block edge can be seen (the player stays >= 229 m inside it, mod follow.lua): the strength goes linearly from its near
# value at 0 m to its far one at MACRO_DISTANCES[2], then to nothing at [3]; the shader samples no macro past [3]. Its
# colour grain is zero-mean (terraintex RECENTRED_STEMS): the photo's mean colour at every distance.
MACRO_DISTANCES = [0, 0, 100, 200]
MACRO_DIST_ATTEN = [0, 1]


def colour_tex(stem):
    """A layer's colour map of a stock texture stem: the server's zero-mean copy (terraintex.rc_name)."""
    rel = stem + "_b"
    return TL_ + terraintex.rc_name(rel) if rel in terraintex.RECENTRED_STEMS else T_ + rel + ".png"


def surface(name, gm, pid, detail, dsize, macro, msize, strength=0.25, macro_strength=(0.12, 0.08), base_size=128):
    """A ground layer: the stock detail texture (repeat dsize x 2 m) and macro (msize x 2 m), the colour as the server's
    zero-mean copies, the detail colour at `strength` near (0.08 by 80 m), the macro colour at macro_strength (near, at
    100 m; gone by 200 m: MACRO_DISTANCES). terrain.lua look() makes the same fields (its LOOKS: metres)."""
    m = {"name": name, "internalName": name, "class": "TerrainMaterial", "persistentId": pid, "groundmodelName": gm,
         "detailDistances": [0, 0, 50, 80], "macroDistances": list(MACRO_DISTANCES),
         "macroDistAtten": list(MACRO_DIST_ATTEN),
         "baseColorDetailStrength": [strength, 0.08], "normalDetailStrength": [1, 0.3], "roughnessDetailStrength": [1, 0.4],
         "aoDetailStrength": [0.8, 0.2], "baseColorMacroStrength": list(macro_strength),
         "normalMacroStrength": [0.5, 0.3], "roughnessMacroStrength": [0, 0.8]}
    for key, suf, base in (("baseColor", "_b", "bngEarth_base_b.png"), ("normal", "_nm", "bngEarth_base_nm.png"),
                           ("roughness", "_r", "bngEarth_base_r.png"), ("ao", "_ao", "bngEarth_base_ao.png")):
        m[key + "BaseTex"], m[key + "BaseTexSize"] = B + base, base_size * TEX_SQ
        m[key + "DetailTex"], m[key + "DetailTexSize"] = T_ + detail + suf + ".png", int(dsize * TEX_SQ)
        m[key + "MacroTex"], m[key + "MacroTexSize"] = T_ + macro + suf + ".png", int(msize * TEX_SQ)
    m["baseColorDetailTex"], m["baseColorMacroTex"] = colour_tex(detail), colour_tex(macro)
    m["heightBaseTex"], m["heightBaseTexSize"] = B + "bngEarth_base_h.png", base_size * TEX_SQ
    # flat (no height-blended borders: they swirled across whole blocks, in-game 2026-10-12), never empty
    m["heightDetailTex"], m["heightDetailTexSize"] = FLAT_H, int(dsize * TEX_SQ)
    m["heightMacroTex"], m["heightMacroTexSize"] = FLAT_H, int(msize * TEX_SQ)
    return {name: m}
# The layers' looks (2026-11-16, the stock maps' way, .claude/work/stock-terrain-materials-2026-11-16.md: each surface its
# own detail AND macro texture and strengths, where we had one 64 m macro at 12 % for all). Our base is a photo that
# already has the 10-1000 m variation; what was missing is the 4-16 m band (clumps, bare patches, tussocks, gravel
# flows) between the 1-4 m detail grain and the photo's texels: the macros repeat every 8-32 m (dividing the 128 m block
# step) with the stock "breakup" macros (dry grass 02, flows, holes, rocky, clumpy). (detail, its repeat in 2 m units,
# macro, its repeat in 2 m units, detail colour strength near, macro colour strength (near, at 100 m)); the macro is
# gone by 200 m (the block's edge, MACRO_DISTANCES) in every one.
LOOKS = {
    "bngEarthGroundAsphalt": ("asphalt/asphalt/t_asphalt", 2, "asphalt/macro_asphalt/t_macro_asphalt", 16, 0.3, (0.12, 0.08)),
    "bngEarthGroundGrass": ("grass/t_grass_01/t_grass_01", 1, "grass/macro_dry_grass_02/t_macro_dry_grass_02", 8, 0.25,
                            (0.12, 0.08)),
    "bngEarthGroundDusty": ("soil/dirt_loose_dusty/t_dirt_loose_dusty", 1, "soil/t_macro_flows/t_macro_flows", 8, 0.25,
                            (0.15, 0.1)),
    "bngEarthGroundLoose": ("soil/t_gravel/t_gravel", 1, "soil/t_macro_flows/t_macro_flows", 4, 0.3, (0.2, 0.12)),
    "bngEarthGroundRock": ("rock/t_dirt_rocky/t_dirt_rocky", 2, "rock/macro_rocky/t_macro_rocky", 8, 0.25, (0.15, 0.1)),
    "bngEarthGroundSnow": ("sand/t_sand/t_sand", 2, "sand/t_macro_sand/t_macro_sand", 16, 0.12, (0.06, 0.04)),
    "bngEarthGroundMud": ("mud/mud/t_mud", 1, "soil/macro_holes/t_macro_holes", 8, 0.25, (0.12, 0.08)),
    # mown town yards (server/surface.py LAWN): the grass detail at a 1 m repeat (finer grain), an even macro
    "bngEarthGroundLawn": ("grass/t_grass_01/t_grass_01", 0.5, "soil/t_macro_flows/t_macro_flows", 8, 0.2, (0.06, 0.04)),
    # the level's default (built-up land, farmland, no data)
    "beamngEarthDirt": ("soil/dirt_crumbly/t_dirt_crumbly", 1, "soil/macro_clumply/t_macro_clumpy", 16, 0.25, (0.12, 0.08)),
    # (server/surface.py FOREST ... SAND, 2026-11-16) forest floor: East Coast USA's / Italy's leaf litter, a weak
    # macro (the photo there is the crowns' dark: the macro's linear-light offsets weigh most on dark ground)
    "bngEarthGroundForest": ("forest/t_forest_ground_02/t_forest_ground_02", 1, "forest/t_macro_forest/t_macro_forest", 8,
                             0.2, (0.08, 0.05)),
    "bngEarthGroundDryGrass": ("grass/t_dirt_dry_grass/t_dirt_dry_grass", 1,
                               "grass/macro_dry_grass_02/t_macro_dry_grass_02", 8, 0.25, (0.12, 0.08)),
    "bngEarthGroundCrop": ("soil/dirt_crumbly/t_dirt_crumbly", 1, "soil/macro_holes/t_macro_holes", 8, 0.25, (0.12, 0.08)),
    "bngEarthGroundSand": ("sand/t_sand/t_sand", 1, "sand/t_macro_sand/t_macro_sand", 16, 0.25, (0.12, 0.08)),
}
NEW_GROUND = (("bngEarthGroundForest", "GRASS", 70), ("bngEarthGroundDryGrass", "GRASS", 71),
              ("bngEarthGroundCrop", "DIRT", 72), ("bngEarthGroundSand", "SAND", 73))
SURFACES = {}
for i, name in enumerate(("bngEarthGroundAsphalt", "bngEarthGroundGrass", "bngEarthGroundDusty", "bngEarthGroundLoose",
                          "bngEarthGroundRock", "bngEarthGroundSnow", "bngEarthGroundMud", "bngEarthGroundLawn")):
    gm = {"bngEarthGroundAsphalt": "ASPHALT_OLD", "bngEarthGroundGrass": "GRASS", "bngEarthGroundDusty": "DIRT_DUSTY",
          "bngEarthGroundLoose": "DIRT_DUSTY_LOOSE", "bngEarthGroundRock": "ROCK", "bngEarthGroundSnow": "SNOW",
          "bngEarthGroundMud": "MUD", "bngEarthGroundLawn": "GRASS"}[name]
    detail, dsize, macro, msize, strength, ms = LOOKS[name]
    SURFACES.update(surface(name, gm, f"3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c{30 + i}", detail, dsize, macro, msize,
                            strength, ms))
for name, gm, pid in NEW_GROUND:
    detail, dsize, macro, msize, strength, ms = LOOKS[name]
    SURFACES.update(surface(name, gm, f"3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c{pid}", detail, dsize, macro, msize,
                            strength, ms))

# Golf (server/surface.py). The detail texture is painted over the photo; these are not GroundCover layers. An even,
# weak macro (mown).
for i, (name, gm, detail, dsize, macro, msize, strength, far) in enumerate((
        ("bngEarthGroundGolfRough", "GRASS", "grass/t_grass_01/t_grass_01", 2, "grass/macro_dry_grass_02/t_macro_dry_grass_02", 8, 0.72, 0.28),
        ("bngEarthGroundGolfFairway", "GRASS", "grass/t_grass_01/t_grass_01", 1, "soil/t_macro_flows/t_macro_flows", 8, 0.45, 0.18),
        ("bngEarthGroundGolfGreen", "GRASS", "grass/t_grass_01/t_grass_01", 0.5, "soil/t_macro_flows/t_macro_flows", 8, 0.5, 0.2),
        ("bngEarthGroundGolfBunker", "SAND", "sand/t_sand/t_sand", 1, "sand/t_macro_sand/t_macro_sand", 16, 0.95, 0.55))):
    made = surface(name, gm, f"3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c{50 + i}", detail, dsize, macro, msize, strength,
                   (0.06, 0.04))
    mat = made[name]
    mat["baseColorDetailStrength"] = [strength, far]
    SURFACES.update(made)

# Squares (server/surface.py PAVERS / BRICK, the user 2026-11-11: "paint the plaza and apply a texture to it like the
# roads"): no stock terrain texture has pavers or brick, so the building tileables at the texture set's 1024 detail
# size (the large grey slabs of West Coast USA's brutalist plazas; the red herringbone brick). A 2 m repeat: 1 m slabs,
# 0.25 m bricks. The detail colour shows over the photo (pavers' joints, brick's red). Their own roughness and ambient
# occlusion (2026-11-16; they had the asphalt's roughness, ao off): the 512 maps resized to 1024 and stored as the
# layer's offsets from the flat base (server/terraintex.py BASE_R), the setts' from their specular map and colour.
# The terrain packing reads no DDS (GBitmap) and these ship as DDS only: the server writes PNG copies from the
# player's game files under /temp/beamngEarth/terraintex/ (server/terraintex.py).
PAVING_DETAIL_DISTANCES = (0, 0, 150, 250)     # m: the detail texture whole to 150 m, gone at 250 (terrain.lua tileLook)
for i, (name, gm, tile, dsize, strength, far) in enumerate((
        ("bngEarthGroundPavers", "ASPHALT_OLD", "pavers", 1, 0.5, 0.15),
        ("bngEarthGroundBrick", "COBBLESTONE", "brick", 1, 0.85, 0.3),
        # cobbled roads and squares (the layer had t_asphalt_02's grain): Italy's setts, persistentId kept
        ("bngEarthGroundCobble", "COBBLESTONE", "cobble", 1, 0.6, 0.2))):
    pid = "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c37" if name == "bngEarthGroundCobble" else \
        f"3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c{60 + i}"
    made = surface(name, gm, pid, "asphalt/asphalt/t_asphalt", dsize, "asphalt/macro_asphalt/t_macro_asphalt", 16,
                   strength)
    mat = made[name]
    for key, suf in (("baseColor", "_b"), ("normal", "_nm"), ("roughness", "_r"), ("ao", "_ao")):
        mat[key + "DetailTex"] = f"{TL_}bne_{tile}{suf}.png"
    mat["baseColorDetailStrength"], mat["aoDetailStrength"] = [strength, far], [1, 0.3]
    mat["roughnessDetailStrength"] = [1, 0.3]
    # (seen further than the land's 50-80 m: the user, 2026-11-11, "the texture fades away pretty quickly")
    mat["detailDistances"] = list(PAVING_DETAIL_DISTANCES)
    SURFACES.update(made)

# The level's own default layer (built-up land, farmland, no data; the block's material 0): the same rules as the rest
# (it had its own: a 128 m macro, no detail repeat set, the stock height maps; 2026-11-16 made it agree with terrain.lua's
# LOOKS), base slot 256 x 2 m, persistentId kept
_d = LOOKS["beamngEarthDirt"]
DIRT = surface("beamngEarthDirt", "DIRT", "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c10", _d[0], _d[1], _d[2], _d[3], _d[4],
               _d[5], base_size=256)

pretty(os.path.join(T, "main.materials.json"), {
    **DIRT,
    **SURFACES,
    "beamngEarthTerrainMaterialTextureSet": {
        "name": "beamngEarthTerrainMaterialTextureSet",
        "class": "TerrainMaterialTextureSet",
        "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c11",
        # 2048: the detail near-visual mode swaps a 2048² aerial photo of the block's footprint in as every
        # layer's base colour (terrain.lua setBase; the engine needs a base texture's pixel size to match)
        "baseTexSize": [BASE_TEX, BASE_TEX], "detailTexSize": [1024, 1024], "macroTexSize": [1024, 1024],
    },
})

# The flat base maps at every ground photo size the settings offer (the mod's groundPhoto: terrain.lua setPhotoSize
# points the texture set's base slot and every layer's base maps at one size before the level's first block; the
# server's earthd.BASE_SIZES / base_ao_vfs): BASE_TEX's under the plain names, the others with a _<px> suffix. A map
# whose size differs from the slot's costs a resize at every material rebuild (~240 ms a 4096 map in a 2048 slot).
BASE_SIZES = (1024, 2048, 4096)
for n in BASE_SIZES:
    suf = "" if n == BASE_TEX else f"_{n}"
    Image.new("RGB", (n, n), (100, 88, 69)).save(os.path.join(T, f"bngEarth_base_b{suf}.png"), optimize=True)
    Image.new("RGB", (n, n), (127, 127, 255)).save(os.path.join(T, f"bngEarth_base_nm{suf}.png"), optimize=True)
    Image.new("L", (n, n), 240).save(os.path.join(T, f"bngEarth_base_r{suf}.png"), optimize=True)
    Image.new("L", (n, n), 251).save(os.path.join(T, f"bngEarth_base_ao{suf}.png"), optimize=True)
    Image.new("L", (n, n), 175).save(os.path.join(T, f"bngEarth_base_h{suf}.png"), optimize=True)
Image.new("L", (1024, 1024), 128).save(os.path.join(T, "bngEarth_flat_h_1024.png"), optimize=True)   # detail size
# importMaps inputs, sized like the terrain block (N = 1024, docs/V1_1_SPEC.md 1a; and 512 for the
# M4 block, docs/V1_4_SPEC.md 3.5; 2048 for the 2 km block at 1 m, --block 2048: the layer map must match the
# heightmap size): one all-white layer map (material 0 everywhere) and a flat 16-bit heightmap per size.
for n in (2048, 1024, 512):
    Image.new("RGB", (n, n), (255, 255, 255)).save(os.path.join(T, f"beamngEarth_layer_{n}.png"), optimize=True)
    Image.new("I;16", (n, n), 0).save(os.path.join(T, f"beamngEarth_flat_{n}.png"), optimize=True)

# (the preview, the whole Earth as the world map shows it: tools/make_level_preview.py)
print("ok")
