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

pretty(os.path.join(L, "info.json"), {
    "title": "BeamNG.Earth",
    "description": "The whole Earth, streamed around you by the BeamNG.Earth companion server. Start the server, then teleport from the BeamNG.Earth window.",
    "previews": ["beamng_earth_preview.jpg"],
    "size": [-1, -1],
    "biome": "Earth",
    "roads": "None (v1)",
    "authors": "BeamNG.Earth",
    "defaultSpawnPointName": "spawn_default",
    "supportsTraffic": True,      # the stock traffic / parked-car choice at the level start (2026-10-14)
    "supportsTimeOfDay": True,
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
    # read by core/celestial.lua (readWhen) and follow the player at runtime.
    {"name": "tod", "class": "TimeOfDay", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c07", "__parent": "Sky",
     "position": [0, 0, 100], "axisTilt": 10, "play": False, "startTime": 0.1, "time": 0.1,
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
lines(os.path.join(L, "main/MissionGroup/Weather/items.level.json"), [
    {"name": "bngEarthClouds", "class": "CloudLayer", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c21", "__parent": "Weather",
     "texture": "art/skies/clouds/clouds_normal_displacement.png", "coverage": 0.5, "cirrusCoverage": 0.3, "exposure": 1.45,
     "windSpeed": 0.1, "baseColor": [0.99, 0.99, 0.99, 0.9], "altitudeKm": 1.5},
    {"name": "bngEarthRain", "class": "Precipitation", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c22", "__parent": "Weather",
     "dataBlock": "rain_drop", "numDrops": 0, "boxWidth": 50, "boxHeight": 40, "dropSize": 0.2, "splashSize": 0.2,
     "fadeDist": 6, "fadeDistEnd": 65, "followCam": True, "useWind": True, "useTurbulence": True, "doCollision": True,
     "hitVehicles": False, "rotateWithCamVel": False},
    {"name": "bngEarthSnow", "class": "Precipitation", "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c23", "__parent": "Weather",
     "dataBlock": "Snow_menu", "numDrops": 0, "boxWidth": 40, "boxHeight": 30, "dropSize": 0.25, "splashSize": 0,
     "minSpeed": 0.15, "maxSpeed": 0.3, "fadeDist": 6, "fadeDistEnd": 45, "followCam": True, "useWind": True,
     "useTurbulence": True, "maxTurbulence": 0.5, "turbulenceSpeed": 0.2, "doCollision": False,
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
BASE_TEX = 1024     # base texture slot (px): the shipped flat base maps and the server's photo (earthd.BASE_PX)
# Every texture slot of every material is filled: an empty one makes the engine build a default texture at
# every TerrainBlock:updateMaterial (~0.8 s per call with the surface materials, in-game 2026-10-12)
FLAT_H = B + "bngEarth_flat_h_1024.png"     # neutral height for the detail / macro height slots
# The server's surface materials (server/surface.py MATERIALS, the tyres' ground models), each with its
# own stock detail / macro textures for the detail near-visual mode (terrain.lua LOOKS has the same
# choices; in-game 2026-10-12: TerrainMaterials made at runtime cost ~1 s per updateMaterial, so they are
# level materials now). Texture sizes are in heightmap samples (2 m on the shipped block: detail 4 m,
# macro 32-64 m, dividing the 128 m block step); the colour comes from the photo base in detail mode.
T_ = "/assets/materials/terrain/"
def surface(name, gm, pid, detail, dsize, macro, msize, strength=0.25):
    m = {"name": name, "internalName": name, "class": "TerrainMaterial", "persistentId": pid, "groundmodelName": gm,
         "detailDistances": [0, 0, 50, 80], "macroDistances": [0, 0, 150, 1500],
         "baseColorDetailStrength": [strength, 0.08], "normalDetailStrength": [1, 0.3], "roughnessDetailStrength": [1, 0.4],
         "aoDetailStrength": [0.8, 0.2], "baseColorMacroStrength": [0.12, 0.08], "normalMacroStrength": [0.5, 0.3],
         "roughnessMacroStrength": [0, 0.8]}
    for key, suf, base in (("baseColor", "_b", "bngEarth_base_b.png"), ("normal", "_nm", "bngEarth_base_nm.png"),
                           ("roughness", "_r", "bngEarth_base_r.png"), ("ao", "_ao", "bngEarth_base_ao.png")):
        m[key + "BaseTex"], m[key + "BaseTexSize"] = B + base, 128
        m[key + "DetailTex"], m[key + "DetailTexSize"] = T_ + detail + suf + ".png", dsize
        m[key + "MacroTex"], m[key + "MacroTexSize"] = T_ + macro + suf + ".png", msize
    m["heightBaseTex"], m["heightBaseTexSize"] = B + "bngEarth_base_h.png", 128
    # flat (no height-blended borders: they swirled across whole blocks, in-game 2026-10-12), never empty
    m["heightDetailTex"], m["heightDetailTexSize"] = FLAT_H, dsize
    m["heightMacroTex"], m["heightMacroTexSize"] = FLAT_H, msize
    return {name: m}
SURFACES = {}
for i, args in enumerate((
        ("bngEarthGroundAsphalt", "ASPHALT_OLD", "asphalt/asphalt/t_asphalt", 2, "asphalt/macro_asphalt/t_macro_asphalt", 16, 0.3),
        ("bngEarthGroundGrass", "GRASS", "grass/t_grass_01/t_grass_01", 1, "grass/t_macro_grass/t_macro_grass", 32),
        ("bngEarthGroundDusty", "DIRT_DUSTY", "soil/dirt_loose_dusty/t_dirt_loose_dusty", 1, "soil/macro_clumply/t_macro_clumpy", 32),
        ("bngEarthGroundLoose", "DIRT_DUSTY_LOOSE", "soil/t_gravel/t_gravel", 1, "soil/macro_clumply/t_macro_clumpy", 32),
        ("bngEarthGroundRock", "ROCK", "rock/t_dirt_rocky/t_dirt_rocky", 2, "rock/macro_rocky/t_macro_rocky", 32),
        ("bngEarthGroundSnow", "SNOW", "sand/t_sand/t_sand", 2, "sand/t_macro_sand/t_macro_sand", 32, 0.12),
        ("bngEarthGroundMud", "MUD", "mud/mud/t_mud", 1, "soil/macro_clumply/t_macro_clumpy", 32),
        ("bngEarthGroundCobble", "COBBLESTONE", "asphalt/t_asphalt_02/t_asphalt_02", 1, "asphalt/macro_asphalt/t_macro_asphalt", 16, 0.3),
        # mown town yards (server/surface.py LAWN): the grass detail, finer grain
        ("bngEarthGroundLawn", "GRASS", "grass/t_grass_01/t_grass_01", 1, "grass/t_macro_grass/t_macro_grass", 32, 0.2))):
    name, gm, detail, dsize, macro, msize, *st = args
    SURFACES.update(surface(name, gm, f"3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c{30 + i}", detail, dsize, macro, msize, *st))

pretty(os.path.join(T, "main.materials.json"), {
    "beamngEarthDirt": {
        "name": "beamngEarthDirt",
        "internalName": "beamngEarthDirt",
        "class": "TerrainMaterial",
        "persistentId": "3b0f6f0e-8f5e-4a51-9a59-0b8e2f6a1c10",
        "groundmodelName": "DIRT",
        # Texture repeat sizes (metres) must divide the 256 m terrain-follow step, or the texture
        # pattern shifts under the car at every terrain move (in-game 2026-10-10: macro 80 m / base 512 m
        # made 24% of the view change at each commit; 64 m / 256 m makes it pixel-identical).
        "aoBaseTex": B + "bngEarth_base_ao.png", "aoBaseTexSize": 256,
        "aoDetailStrength": [1, 0], "aoDetailTex": A + "dirt_crumbly/t_dirt_crumbly_ao.png",
        "aoMacroTex": A + "macro_clumply/t_macro_clumpy_ao.png", "aoMacroTexSize": 64,
        "baseColorBaseTex": B + "bngEarth_base_b.png", "baseColorBaseTexSize": 256,
        "baseColorDetailStrength": [0.25, 0], "baseColorDetailTex": A + "dirt_crumbly/t_dirt_crumbly_b.png",
        "baseColorMacroStrength": [0.12, 0.08], "baseColorMacroTex": A + "macro_clumply/t_macro_clumpy_b.png", "baseColorMacroTexSize": 64,
        "detailDistances": [0, 0, 30, 60], "macroDistances": [0, 10, 100, 3000],
        "heightBaseTex": B + "bngEarth_base_h.png", "heightBaseTexSize": 256,
        "heightDetailTex": A + "dirt_crumbly/t_dirt_crumbly_h.png",
        "heightMacroTex": A + "macro_clumply/t_macro_clumpy_h.png", "heightMacroTexSize": 64,
        "normalBaseTex": B + "bngEarth_base_nm.png", "normalBaseTexSize": 256,
        "normalDetailStrength": [0.8, 0], "normalDetailTex": A + "dirt_crumbly/t_dirt_crumbly_nm.png",
        "normalMacroStrength": [0.5, 0.2], "normalMacroTex": A + "macro_clumply/t_macro_clumpy_nm.png", "normalMacroTexSize": 64,
        "roughnessBaseTex": B + "bngEarth_base_r.png", "roughnessBaseTexSize": 256,
        "roughnessDetailStrength": [1, 0], "roughnessDetailTex": A + "dirt_crumbly/t_dirt_crumbly_r.png",
        "roughnessMacroStrength": [0, 0.8], "roughnessMacroTex": A + "macro_clumply/t_macro_clumpy_r.png", "roughnessMacroTexSize": 64,
    },
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

Image.new("RGB", (BASE_TEX, BASE_TEX), (100, 88, 69)).save(os.path.join(T, "bngEarth_base_b.png"), optimize=True)
Image.new("RGB", (BASE_TEX, BASE_TEX), (127, 127, 255)).save(os.path.join(T, "bngEarth_base_nm.png"), optimize=True)
Image.new("L", (BASE_TEX, BASE_TEX), 240).save(os.path.join(T, "bngEarth_base_r.png"), optimize=True)
Image.new("L", (BASE_TEX, BASE_TEX), 251).save(os.path.join(T, "bngEarth_base_ao.png"), optimize=True)
Image.new("L", (BASE_TEX, BASE_TEX), 175).save(os.path.join(T, "bngEarth_base_h.png"), optimize=True)
Image.new("L", (1024, 1024), 128).save(os.path.join(T, "bngEarth_flat_h_1024.png"), optimize=True)   # detail size
# importMaps inputs, sized like the terrain block (N = 1024, docs/V1_1_SPEC.md 1a; and 512 for the
# M4 block, docs/V1_4_SPEC.md 3.5: the layer map must match the heightmap size): one all-white
# layer map (material 0 everywhere) and a flat 16-bit heightmap per size. The v1 2048-sample files
# are removed.
for n in (1024, 512):
    Image.new("RGB", (n, n), (255, 255, 255)).save(os.path.join(T, f"beamngEarth_layer_{n}.png"), optimize=True)
    Image.new("I;16", (n, n), 0).save(os.path.join(T, f"beamngEarth_flat_{n}.png"), optimize=True)
for old in ("beamngEarth_layer_2048.png", "beamngEarth_flat_2048.png"):
    if os.path.exists(os.path.join(T, old)):
        os.remove(os.path.join(T, old))

prev = Image.new("RGB", (512, 288))
d = ImageDraw.Draw(prev)
for y in range(288):
    t = y / 287
    d.line([(0, y), (511, y)], fill=(int(70 + 90 * t), int(120 + 60 * t), int(200 - 60 * t)))
d.ellipse([156, 44, 356, 244], fill=(40, 90, 160), outline=(230, 230, 230), width=3)
d.text((200, 250), "BeamNG.Earth", fill=(255, 255, 255))
prev.save(os.path.join(L, "beamng_earth_preview.jpg"), quality=85)
print("ok")
