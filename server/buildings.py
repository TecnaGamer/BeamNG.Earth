"""Buildings (v1.9; the user, 2026-10-13: "make the world look alive"): Overture footprints extruded into
walls with a window band per floor and a flat, gabled or hipped roof, one mesh per road tile (roads.TILE),
written next to the tile's road mesh and shown as a colliding TSStatic by the mod (roads.lua, "bld").

Overture rarely knows a building's height (Fells Point, 2026-10-13: 2 % of 12458; towns 50-80 %) and
almost never its roof shape or facade, so most of it is predicted from the footprint (area, narrowness,
class), deterministically per building (ROADMAP "deterministic generation": a hash of the footprint's
position, so it is the same building whichever way the player came).

Facades use the stock window sheets (asset survey 2026-10-13): m_highrise_plaster's four 256 px bands are
four floor styles (tall windows on plaster, square windows on panels, sash windows on plaster, sash windows
on red brick), m_highrise_metal's are office curtain wall and a shopfront; each floor of a wall is one band,
its windows a whole number of bays along every wall (no window cut at a corner). Pure functions of the
footprints and a ground-height function; no game state.
"""
from runtimeprofiler import step as profile_step
import os
import math

import numpy as np

import fast
import dmath
import dnp
import landmarks
import mapgeom
import nycmassing
import ships
import staging
import francehouses

FLOOR_M = 3.0               # m per storey (residential)
GROUND_FLOOR_SHOP_M = 4.2   # m, a shop's ground floor
FOUNDATION_M = 0.6          # m the walls reach under the lowest ground of the footprint
# The walls reach this far under the lowest ground of the footprint (a skirt): beyond the physics block (+-512 m) the
# ground drawn is the far field's mesh, coarser with distance (tens of metres between vertices at 1-2 km), which on any
# slope runs metres under the heights the buildings stand on: they floated (the user, 2026-10-18: "houses and
# buildings in many parts of the world are floating"). Under a correct ground the skirt is hidden; the floors stay
# where they were (anchored at the eaves, the rest a plain plinth). The lite mesh (far tiles, a near tile's distant
# detail) stands on the coarsest ground: the deepest skirt.
SKIRT_M = 3.0
SKIRT_LITE_M = 10.0
MIN_AREA_M2 = 12.0          # footprints smaller than this are skipped (kiosks, sheds' slivers)
MAX_VERTS = 64              # footprints with more vertices are simplified until under this
SIMPLIFY_M = 0.35           # m: vertices closer than this to the line of their neighbours are dropped
TILE_PX_M = 12.0            # m of wall per 1024 px of the window sheets (5 sash bays of 2.4 m)
ROOF_UV_M = 6.0             # m per repeat of the roof textures
SIDING_UV_M = 3.0           # m per repeat of the plain wall textures (sheds, garages)
PITCH = {"gabled": 32.0, "hipped": 26.0, "half-hipped": 32.0, "pyramidal": 26.0}     # degrees
# roof:shape forms with pitches of their own, whatever the region's (roof_rise, _roof)
ROUND_RISE = 0.45               # a barrel roof's rise over its half width (a mapped roof height its own)
ROUND_SEGS = 10                 # its arc's segments across the width
# A machine-learned height (Overture merges Microsoft's and Google's onto OSM footprints) is the footprint's average,
# not its top (research 2026-11-10: where OSM has floors and Overture an ML height, Manhattan came out at a median
# 1.57 m a floor): against mapped floors, one giving less than ML_FLOOR_M[0] or more than ML_FLOOR_M[1] a floor is left
# out and the floors decide.
ML_HEIGHT_SOURCES = {"Microsoft ML Buildings", "Google Open Buildings"}
ML_FLOOR_M = (2.2, 6.0)
RISE_FLAT_M = 0.8              # a measured roof rise (France's BD TOPO: bdtopo.py) below this: a flat roof
SKILLION_PITCH = 12.0           # a lean-to: one shallow slope from wall to wall
MANSARD_PITCHES = (68.0, 18.0)  # the steep lower slope, the shallow top
MANSARD_INSET_M = 1.2           # how far the lower slope comes in, at most
GAMBREL_PITCHES = (55.0, 25.0)  # a barn's: the same, on two sides only
GAMBREL_BREAK = 0.55            # the slope breaks this far out from the ridge (of the half width)
HALF_HIP = 0.35                 # the share of a half-hipped roof's height the small hips at its ends take
EAVE_M = 0.35               # m a pitched roof overhangs its walls
CORNICE_OUT_M = 0.28        # a flat roof's cornice: how far it stands out, its depth below and above the eave
CORNICE_DOWN_M = 0.45
CORNICE_UP_M = 0.2

# Materials (mod/.../art/shapes/beamngEarth/buildings.materials.json): the window sheets in a few tints, the
# shop / office sheet, roofs, plain walls.
FACADE_TINTS = ("bne_bld_fac_white", "bne_bld_fac_cream", "bne_bld_fac_sand", "bne_bld_fac_grey")
# the plastered blocks' colours by region (FACADE_TINTS elsewhere): the Mediterranean's warm ochres, oranges and pinks
# (Rome's Trastevere against Mapillary, 2026-11-10: ours were white / cream / sand / grey), Latin America's brighter
REGION_FACADE_TINTS = {
    "mediterranean": ("bne_bld_fac_cream", "bne_bld_fac_sand", "bne_bld_fac_ochre", "bne_bld_fac_terracotta",
                      "bne_bld_fac_salmon", "bne_bld_fac_yellow", "bne_bld_fac_white", "bne_bld_fac_ochre"),
    "latin_america": ("bne_bld_fac_white", "bne_bld_fac_cream", "bne_bld_fac_ochre", "bne_bld_fac_terracotta",
                      "bne_bld_fac_salmon", "bne_bld_fac_yellow", "bne_bld_fac_blue", "bne_bld_fac_green",
                      "bne_bld_fac_rose"),
}
PAINT_TINTS = ("bne_bld_fac_blue", "bne_bld_fac_green", "bne_bld_fac_rose", "bne_bld_fac_yellow")   # painted houses
BRICK_TINTS = ("bne_bld_fac_white", "bne_bld_fac_brickdark", "bne_bld_fac_brickwarm")   # the brick band's
SHOP = "bne_bld_shop"
OFFICE = "bne_bld_office"
ROOF_FLAT = "bne_bld_roof_flat"
ROOF_SLATE = "bne_bld_roof_slate"
# clay roof tiles (Italy's barrel tiles, the stock assets/materials/tileable/roofing/t_italy_bld_roof_tiles: the asset
# survey 2026-11-10): the clay roofs of the regional styles and of roof:material=roof_tiles, which were the slate texture
# tinted orange. Its repeat is ROOF_TILES_UV_M (about four tiles across), not the other roofs' ROOF_UV_M
ROOF_TILES = "bne_bld_roof_tiles"
ROOF_TILES_UV_M = 1.2
# an office tower's street floor: a lobby of glass doors (the stock corporate facade sheet,
# assets/materials/trim/metal/m_facade_corporate: its lower half, the asset survey 2026-11-10), not a shopfront
LOBBY = "bne_bld_lobby"
# more stock roofs and walls (the asset survey 2026-11-10; the user: "use as many assets as needed to give this world
# detail and variety"): fish-scale slate (central Europe), dark slate courses (Britain, northern France, mapped slate),
# wooden shingles (Alpine chalets, roof:material=wood), rough rubble stone (Mediterranean villages), cinder block
# (building:material=cement_block)
ROOF_SLATE_ROUND = "bne_bld_roof_slate_round"
ROOF_SLATE_DARK = "bne_bld_roof_slate_dark"
ROOF_WOOD = "bne_bld_roof_wood"
STONE_RUBBLE = "bne_bld_stone_rubble"
CINDER = "bne_bld_cinder"
# the textures' own repeats (m): the roofs' UVs are made at ROOF_UV_M and the walls' at SIDING_UV_M, Mesh.arrays
# scales these materials' vertices
OWN_UV_M = {ROOF_TILES: (ROOF_TILES_UV_M, ROOF_UV_M), ROOF_SLATE_ROUND: (1.2, ROOF_UV_M),
            ROOF_SLATE_DARK: (1.6, ROOF_UV_M), ROOF_WOOD: (0.9, ROOF_UV_M), STONE_RUBBLE: (2.4, SIDING_UV_M),
            CINDER: (2.8, SIDING_UV_M)}
LOBBY_BAND = (0.004, 0.496, 6.8)      # its rows (v, Collada: the sheet's lower half) and m per repeat (three bays)
LOBBY_NOT = {"retail", "supermarket", "kiosk", "restaurant", "commercial"}
ROOF_SHINGLE = "bne_bld_roof_shingle"
SIDING = "bne_bld_siding"
FACADE = "bne_bld_facade"    # the plaster window sheet, tinted per building by vertex colour
STUCCO = "bne_bld_stucco"   # plain stucco walls (windows laid over them from the sheet)
ROOF_METAL = "bne_bld_roof_metal"   # standing-seam metal (barns, sheds, farm buildings)
PANEL = "bne_bld_panel"     # precast concrete panels (big-box stores, strip malls)
METALWALL = "bne_bld_metalwall"     # corrugated metal walls (warehouses, factories)
GLASSHOUSE = "bne_bld_glasshouse"   # greenhouse glazing in aluminium bars (tools/make_glasshouse.py)
STONE = "bne_bld_stone"     # dressed stone (old villages and towns of southern and central Europe: Gordes came out as
                            # grey plaster, 2026-10-15)
STONE_FLOORS = 4            # stone walls on buildings this low at most
BIG_BOX_M2 = 1200.0         # m2: a low commercial building this big is a big box (blank walls, one storefront)
CHURCH_CLASSES = ("church", "chapel", "cathedral")
BARN_CLASSES = {"barn", "farm_auxiliary", "stable", "cowshed"}
# The mesh's materials (vertColor: each building's colours are its vertices'); the names above are the
# looks plan() picks, each one of these with a colour (LOOKS).
MARBLE = "bne_bld_marble"   # cut white marble (the stock m_marble_cut): Baltimore's row house steps
BRICK_WALL = "bne_bld_brick"    # plain red brick (the stock brick_plain): a garage's skin
CONCRETE = "bne_bridge_concrete"  # plain concrete (tunnel.materials.json): a precast garage's skin
SKY_WALL = "bne_bld_sky"    # the skyline's walls: plain (vertex colours), their windows lit at night (_sky_mesh)
LANDMARK_IRON = "bne_lm_iron"   # a landmark's painted iron (landmarks.py; vertex colours), lit at night (nightlights.lua)
LANDMARK_STONE = "bne_lm_stone"     # ... its stone (vertex colours), dark at night (lit by its lamps: the Colosseum)
LANDMARK_FLOODLIT = "bne_lm_floodlit"   # ... its floodlit stone, glowing at night (the Arc de Triomphe, Big Ben)
LANDMARK_LAMP = "bne_lm_lamp"   # ... its lamps (the Eiffel Tower's, the Colosseum's arches): points of light at night
LANDMARK_DIAL = "bne_lm_dial"   # ... a clock's dials (Big Ben's), lit from behind at night
MATERIALS = (FACADE, SHOP, OFFICE, ROOF_FLAT, ROOF_SLATE, ROOF_SHINGLE, SIDING, STUCCO, ROOF_METAL, PANEL, METALWALL,
             STONE, MARBLE, BRICK_WALL, CONCRETE, SKY_WALL, LANDMARK_IRON, LANDMARK_STONE, LANDMARK_LAMP,
             LANDMARK_FLOODLIT, LANDMARK_DIAL) + francehouses.MATERIALS + (GLASSHOUSE, ROOF_TILES, LOBBY, ROOF_SLATE_ROUND,
                                                                  ROOF_SLATE_DARK, ROOF_WOOD, STONE_RUBBLE, CINDER)
LOOKS = {
    "bne_bridge_steel": (METALWALL, (0.50, 0.52, 0.54)),
    "bne_marble": (MARBLE, (1.0, 1.0, 1.0)),
    "bne_brick_red": (BRICK_WALL, (1.0, 1.0, 1.0)), "bne_brick_dark": (BRICK_WALL, (0.78, 0.72, 0.70)),
    "bne_brick_tan": (BRICK_WALL, (1.0, 0.92, 0.80)),
    "bne_precast": (CONCRETE, (1.0, 1.0, 1.0)), "bne_precast_warm": (CONCRETE, (1.0, 0.95, 0.86)),
    "bne_bld_fac_white": (FACADE, (1.0, 1.0, 1.0)), "bne_bld_fac_cream": (FACADE, (1.0, 0.93, 0.80)),
    "bne_bld_fac_sand": (FACADE, (0.92, 0.82, 0.68)), "bne_bld_fac_grey": (FACADE, (0.80, 0.82, 0.84)),
    "bne_bld_fac_brickdark": (FACADE, (0.78, 0.72, 0.70)), "bne_bld_fac_brickwarm": (FACADE, (1.0, 0.88, 0.80)),
    "bne_bld_fac_blue": (FACADE, (0.80, 0.87, 0.96)), "bne_bld_fac_green": (FACADE, (0.84, 0.92, 0.82)),
    "bne_bld_fac_rose": (FACADE, (0.99, 0.86, 0.82)), "bne_bld_fac_yellow": (FACADE, (1.0, 0.95, 0.72)),
    # Rome's palazzi (Mapillary in Trastevere, 2026-11-10): ochre, burnt orange, salmon over the plaster sheet
    "bne_bld_fac_ochre": (FACADE, (1.0, 0.80, 0.52)), "bne_bld_fac_terracotta": (FACADE, (0.96, 0.66, 0.50)),
    "bne_bld_fac_salmon": (FACADE, (1.0, 0.80, 0.70)),
    SHOP: (SHOP, (1.0, 1.0, 1.0)), OFFICE: (OFFICE, (1.0, 1.0, 1.0)), SIDING: (SIDING, (0.93, 0.92, 0.88)),
    LOBBY: (LOBBY, (1.0, 1.0, 1.0)),
}
ROOF_TINTS = {               # per roof material: the colours a building's roof takes (hashed)
    ROOF_FLAT: ((0.85, 0.85, 0.85), (0.62, 0.62, 0.64), (0.95, 0.93, 0.88), (0.45, 0.45, 0.46), (0.75, 0.70, 0.64)),
    # 4..: clay tiles (terracotta, red-brown, faded orange) and dark grey / anthracite / brown-black (regional)
    ROOF_SLATE: ((1.0, 1.0, 1.0), (0.80, 0.66, 0.56), (0.76, 0.80, 0.90), (0.62, 0.62, 0.62),
                 (0.95, 0.52, 0.36), (0.78, 0.40, 0.30), (1.0, 0.66, 0.46), (0.42, 0.43, 0.46), (0.30, 0.31, 0.33),
                 (0.48, 0.38, 0.32)),
    ROOF_SHINGLE: ((0.75, 0.72, 0.70), (0.55, 0.50, 0.48), (0.70, 0.56, 0.48), (0.62, 0.66, 0.72),
                   (0.95, 0.52, 0.36), (0.78, 0.40, 0.30), (0.42, 0.43, 0.46), (0.36, 0.42, 0.52)),
    ROOF_METAL: ((0.85, 0.86, 0.88), (0.55, 0.57, 0.60), (0.45, 0.58, 0.48), (0.62, 0.30, 0.26), (0.40, 0.42, 0.48)),
    # the texture is terracotta already: as it is, weathered paler / darker, a redder brown
    ROOF_TILES: ((1.0, 1.0, 1.0), (1.0, 0.95, 0.9), (0.86, 0.84, 0.82), (0.92, 0.82, 0.76), (0.75, 0.72, 0.70)),
    ROOF_SLATE_ROUND: ((1.0, 1.0, 1.0), (0.80, 0.80, 0.82), (0.62, 0.62, 0.64)),           # blue-grey, grey, dark
    ROOF_SLATE_DARK: ((1.0, 1.0, 1.0), (1.25, 1.22, 1.20), (0.85, 0.85, 0.88)),
    ROOF_WOOD: ((0.55, 0.42, 0.30), (0.45, 0.36, 0.28), (0.58, 0.55, 0.50), (0.40, 0.33, 0.26)),  # larch, weathered grey
}
WALL_TINTS = {               # houses with plain walls and laid-over windows: their colours (hashed)
    SIDING: ((0.96, 0.95, 0.92), (0.90, 0.86, 0.76), (0.78, 0.84, 0.90), (0.95, 0.92, 0.74), (0.78, 0.84, 0.74),
             (0.82, 0.78, 0.72), (0.70, 0.72, 0.75), (0.62, 0.20, 0.17), (0.66, 0.24, 0.20)),   # the last: barn red
    STUCCO: ((1.0, 0.97, 0.90), (0.96, 0.88, 0.74), (0.93, 0.80, 0.68), (0.98, 0.95, 0.88), (0.88, 0.84, 0.78),
             # 5..: bright paints (Latin America, the Mediterranean, the tropics): ochre, terracotta, sky blue,
             # mint, pink, yellow
             (1.0, 0.78, 0.45), (0.92, 0.58, 0.44), (0.66, 0.82, 0.95), (0.70, 0.90, 0.78), (0.98, 0.72, 0.76),
             (1.0, 0.92, 0.52)),
    PANEL: ((0.96, 0.94, 0.89), (0.86, 0.86, 0.86), (0.92, 0.85, 0.74), (0.78, 0.80, 0.82), (0.88, 0.78, 0.68),
            (0.62, 0.62, 0.60), (0.56, 0.54, 0.50)),    # the last two: weathered concrete (parking decks)
    METALWALL: ((0.88, 0.89, 0.90), (0.72, 0.74, 0.77), (0.84, 0.80, 0.72), (0.66, 0.72, 0.78)),
    # limestone, honey, ochre and grey stone (the texture is a light ashlar: tinted down)
    STONE: ((1.0, 0.93, 0.80), (0.98, 0.86, 0.66), (0.95, 0.80, 0.60), (0.86, 0.85, 0.82), (0.92, 0.88, 0.80)),
    STONE_RUBBLE: ((1.0, 1.0, 1.0), (1.05, 0.98, 0.90), (0.92, 0.90, 0.88)),
    CINDER: ((1.0, 1.0, 1.0), (0.92, 0.90, 0.86), (1.05, 1.04, 1.02)),
    # curtain walls (the user, 2026-10-14: "the skyscrapers all use the same texture and look the same colour"):
    # silver, blue, green, bronze, charcoal and steel-blue glass, the common tints of real towers
    OFFICE: ((1.0, 1.0, 1.0), (0.74, 0.84, 0.98), (0.76, 0.92, 0.86), (0.98, 0.84, 0.66), (0.56, 0.58, 0.62),
             (0.66, 0.74, 0.86), (0.88, 0.90, 0.92)),
}
for _m, _cs in list(ROOF_TINTS.items()) + list(WALL_TINTS.items()):
    for _k, _c in enumerate(_cs):
        LOOKS[f"{_m}|{_k}"] = (_m, _c)
# stained wood boards (Alpine chalets; kept apart from the siding tints' indices): larch brown, dark brown, grey-brown
WOOD_TINTS = ((0.62, 0.44, 0.30), (0.46, 0.33, 0.24), (0.58, 0.52, 0.46))
for _k, _c in enumerate(WOOD_TINTS):
    LOOKS[f"{SIDING}|w{_k}"] = (SIDING, _c)
# Shop signs (Bishop's and Moab's main streets: a painted sign band over every shopfront, each shop its own
# colour): boxes over the ground floor of a shop's street wall, a colour per shop width (hashed).
SIGN_TINTS = ((0.62, 0.16, 0.14), (0.16, 0.22, 0.42), (0.18, 0.36, 0.24), (0.95, 0.90, 0.75), (0.42, 0.30, 0.22),
              (0.85, 0.65, 0.20), (0.13, 0.13, 0.14), (0.96, 0.96, 0.96), (0.15, 0.45, 0.48))
for _k, _c in enumerate(SIGN_TINTS):
    LOOKS[f"sign|{_k}"] = (FACADE, _c)
SIGN_H = 0.9                # m, the band's height (its top 0.15 m under the ground floor's top)
SIGN_OUT = 0.18             # m out from the wall
SIGN_SHOP_M = (6.0, 11.0)   # one shop's width (hashed in this range)
BARN_REDS = 2               # the last siding tints are barn red (barns only)
US_WALL_TINTS = {STUCCO: 5}  # the first tints of these kinds are the US's (later ones: regional paints)
JITTER = 0.06               # a building's colour varies this much (one factor, hashed)

COLOR_NAMES = {
    "white": (0.95, 0.95, 0.95), "black": (0.12, 0.12, 0.12),
    "grey": (0.50, 0.50, 0.50), "gray": (0.50, 0.50, 0.50),
    "lightgrey": (0.75, 0.75, 0.75), "lightgray": (0.75, 0.75, 0.75),
    "darkgrey": (0.30, 0.30, 0.30), "darkgray": (0.30, 0.30, 0.30),
    "silver": (0.75, 0.75, 0.78), "charcoal": (0.20, 0.20, 0.22), "anthracite": (0.22, 0.23, 0.25),
    "red": (0.75, 0.15, 0.15), "darkred": (0.50, 0.10, 0.10), "lightred": (0.85, 0.35, 0.35),
    "maroon": (0.45, 0.10, 0.15), "burgundy": (0.45, 0.12, 0.20), "crimson": (0.70, 0.10, 0.20),
    "pink": (0.90, 0.60, 0.65), "rose": (0.90, 0.55, 0.60), "coral": (0.90, 0.45, 0.35),
    "salmon": (0.90, 0.50, 0.45), "brick": (0.65, 0.25, 0.18),
    "green": (0.18, 0.55, 0.22), "darkgreen": (0.10, 0.35, 0.12), "lightgreen": (0.45, 0.75, 0.40),
    "olive": (0.45, 0.50, 0.20), "lime": (0.55, 0.75, 0.20), "forestgreen": (0.13, 0.40, 0.15),
    "teal": (0.12, 0.45, 0.45), "mint": (0.60, 0.85, 0.70), "sage": (0.55, 0.65, 0.55), "moss": (0.35, 0.45, 0.25),
    "blue": (0.18, 0.38, 0.72), "darkblue": (0.10, 0.20, 0.45), "lightblue": (0.50, 0.70, 0.88),
    "navy": (0.08, 0.15, 0.35), "skyblue": (0.50, 0.75, 0.90), "cyan": (0.20, 0.70, 0.75),
    "azure": (0.30, 0.60, 0.85), "cobalt": (0.15, 0.30, 0.65), "turquoise": (0.25, 0.70, 0.65),
    "steel": (0.45, 0.50, 0.55), "steel_blue": (0.28, 0.45, 0.60),
    "yellow": (0.92, 0.82, 0.20), "lightyellow": (0.95, 0.92, 0.60), "gold": (0.85, 0.70, 0.18),
    "orange": (0.90, 0.48, 0.12), "darkorange": (0.80, 0.35, 0.08), "amber": (0.88, 0.60, 0.12),
    "brown": (0.45, 0.30, 0.20), "darkbrown": (0.30, 0.20, 0.12), "lightbrown": (0.60, 0.45, 0.32),
    "rust": (0.60, 0.25, 0.15), "beige": (0.90, 0.85, 0.75), "cream": (0.95, 0.92, 0.82),
    "tan": (0.82, 0.70, 0.55), "terracotta": (0.78, 0.40, 0.26), "ochre": (0.80, 0.55, 0.20),
    "sand": (0.88, 0.80, 0.65), "khaki": (0.75, 0.70, 0.50), "copper": (0.55, 0.75, 0.65),
    "bronze": (0.55, 0.42, 0.25), "purple": (0.50, 0.20, 0.55), "violet": (0.55, 0.25, 0.60),
    "magenta": (0.75, 0.15, 0.55), "indigo": (0.25, 0.10, 0.45), "lavender": (0.75, 0.70, 0.85),
    "plum": (0.45, 0.20, 0.40),
}


# Overture writes OSM's colour names as strong X11 / CSS hexes (grey -> #778899, a blue slate; brown -> #8B4513; red ->
# #FF0000: 6,820 pure red roofs in one cache, research 2026-11-10): an exact one is read as the name it came from, in
# COLOR_NAMES' muted shades. A mapper's own hex rarely is one of these exactly.
OVERTURE_HEX = {
    "#ff0000": "red", "#778899": "grey", "#808080": "grey", "#708090": "grey", "#8b4513": "brown", "#a52a2a": "brown",
    "#ffff00": "yellow", "#ffffe0": "lightyellow", "#ffa500": "orange", "#0000ff": "blue", "#008000": "green",
    "#00ff00": "lime", "#b03060": "maroon", "#800000": "maroon", "#ffffff": "white", "#000000": "black",
    "#a9a9a9": "darkgrey", "#696969": "darkgrey", "#d3d3d3": "lightgrey", "#c0c0c0": "silver", "#f5f5dc": "beige",
    "#d2b48c": "tan", "#fffdd0": "cream", "#e2725b": "terracotta", "#ffc0cb": "pink", "#800080": "purple",
    "#00ffff": "cyan", "#008080": "teal", "#000080": "navy", "#ffd700": "gold", "#f0e68c": "khaki",
    "#fa8072": "salmon", "#ff7f50": "coral", "#add8e6": "lightblue", "#90ee90": "lightgreen", "#006400": "darkgreen",
    "#8b0000": "darkred", "#b22222": "brick", "#228b22": "forestgreen",
}


DOME_PALE_RGB = (0.95, 0.95, 0.93)     # a dome's pale painted metal (ROOF_METAL) where its walls lend no light look
DOME_WALLS = ("bne_bld_stone", "bne_bld_stone_rubble", "bne_bld_stucco", "bne_bld_panel")


def dome_look(facade, band):
    """A dome's look from its walls' (plan: no roof material mapped): a light plain wall's material and colour (the
    plaster window sheet's as plain stucco), else DOME_PALE_RGB metal."""
    mat, rgb = LOOKS.get(facade, (None, None))
    if mat == FACADE and band is not BAND_BRICK:
        mat = STUCCO
    if mat in DOME_WALLS and rgb is not None and len(rgb) >= 3 \
            and 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2] >= 0.6:
        return custom_look(mat, tuple(float(v) for v in rgb[:3]))
    return custom_look(ROOF_METAL, DOME_PALE_RGB)


def masonry_colour(rgb):
    """Is rgb (0..1) a stone's colour (MASONRY_WARM / MASONRY_PALE) rather than a curtain wall's?"""
    if rgb is None:
        return False
    r, g, b = (float(v) for v in rgb[:3])
    v = max(r, g, b)
    sat = (v - min(r, g, b)) / v if v > 0 else 0.0
    return (v >= MASONRY_WARM[0] and sat <= MASONRY_WARM[1] and r >= b + MASONRY_WARM[2]) \
        or (v >= MASONRY_PALE[0] and sat <= MASONRY_PALE[1])


def parse_color(val):
    """Parse an OSM/Overture color (hex or CSS name) into an (r, g, b) float tuple in [0, 1], or None."""
    if not val or not isinstance(val, str):
        return None
    s = val.strip().lower().replace(" ", "").replace("-", "_")
    s = OVERTURE_HEX.get(s, s)
    if s in COLOR_NAMES:
        return COLOR_NAMES[s]
    if s.startswith("#"):
        hex_str = s[1:]
        if len(hex_str) in (3, 4):
            try:
                return tuple(int(hex_str[i] * 2, 16) / 255.0 for i in range(3))
            except ValueError:
                return None
        elif len(hex_str) in (6, 8):
            try:
                return tuple(int(hex_str[2 * i:2 * i + 2], 16) / 255.0 for i in range(3))
            except ValueError:
                return None
    return None


# Roof colours from the aerial photo (the user, 2026-11-10: "more accurate textures on the buildings"): where a
# high-resolution regional orthophoto covers the tile and the ground photo has already fetched it (streetfront's
# cached_photo sources, read from disk only), a roof with no mapped colour takes the median colour of the photo's
# pixels on it (native roof_photo_colours: inside the footprint, ROOF_PHOTO_INSET_M in from its outline, vegetation and
# deep shadow left out). The roof textures are near neutral grey and the vertex colour multiplies them, so the tint
# is the photo's colour over the texture's own mean (ROOF_TEX_MEAN, measured from the stock textures), at most 1.
# The tile's photo is fetched (netfetch's limits; the same tiles the ground photo takes near the car) on a thread
# started as the tile begins (RoofPhotoFuture), while its footprints are put together, the lazy sources' tiles too
# (imagery.fetch_lazy: a render reads those from disk only); a failed or slow fetch faults the tile (revisit: built
# again, coloured, on a later visit) and keeps the procedural roofs. A tile the sources hold too little of once all
# was decided (a state's edge, a remembered nodata answer: roof_photo_final) keeps them without a fault, kept only
# until the first of those answers expires (revisit.expires).
ROOF_PHOTO_ON = os.environ.get("BNE_ROOF_PHOTO", "on") != "off"
ROOF_PHOTO_INSET_M = 1.0
ROOF_PHOTO_MIN_PX = 6
ROOF_PHOTO_SHADOW = 3 * 40          # an 8-bit r + g + b under this is shadow
ROOF_PHOTO_PX = 512                 # the tile's photo (0.6 m per pixel over a 256 m tile and its margin)
ROOF_PHOTO_STEP = 0.02              # tints rounded to this (fewer distinct looks)
ROOF_TEX_MEAN = {"bne_bld_roof_flat": (0.432, 0.431, 0.430), "bne_bld_roof_slate": (0.359, 0.361, 0.388),
                 "bne_bld_roof_shingle": (0.585, 0.577, 0.571), "bne_bld_roof_metal": (0.64, 0.64, 0.64),
                 "bne_bld_panel": (0.815, 0.815, 0.815), "bne_bld_roof_tiles": (0.625, 0.465, 0.361),
                 "bne_bld_roof_slate_round": (0.473, 0.484, 0.522), "bne_bld_roof_slate_dark": (0.157, 0.155, 0.136),
                 "bne_bld_roof_wood": (0.897, 0.889, 0.886)}
# a flat roof brighter in the photo than the asphalt roof's texture can show (a white membrane: 0.9 in the photo, the
# asphalt texture's 0.43 at most) takes the precast concrete's plain light texture instead
ROOF_BRIGHT_MAT = "bne_bld_panel"


class RoofPhoto:
    """The tile's cached orthophoto (rgb rows north to south over [x0, x0 + size] x [y0, y0 + size], anchor frame)."""

    def __init__(self, rgb, x0, y0, size):
        self.rgb, self.x0, self.y0, self.size = np.ascontiguousarray(rgb, np.uint8), float(x0), float(y0), float(size)

    def colours(self, rings):
        """(n, 3) sRGB roof colours of rings [(k, 2) anchor frame]; NaN rows where none."""
        if not rings:
            return np.zeros((0, 3))
        import fast
        fast.require(182, "roof_photo_colours")
        rr = [np.asarray(r, np.float64)[:, :2] for r in rings]
        off = np.r_[0, np.cumsum([len(r) for r in rr])].astype(np.int64)
        return np.asarray(fast._ef.roof_photo_colours(self.rgb, self.x0, self.y0, self.size,
                                                      np.ascontiguousarray(np.concatenate(rr)), off,
                                                      float(ROOF_PHOTO_INSET_M), int(ROOF_PHOTO_MIN_PX),
                                                      int(ROOF_PHOTO_SHADOW)), np.float64)


ROOF_PHOTO_WAIT_S = 8.0     # s the buildings wait for the tile's photo at most (then the procedural roofs, a fault)
NEG_RECHECK_S = 86400.0     # s a tile built where the sources hold no photo is kept at most (roof_photo_final)
_PHOTO_READERS = {}
_PHOTO_LOCK = __import__("threading").Lock()
_PHOTO_POOL = None


def _photo_reader_live(cache_dir, sources):
    """An imagery.Imagery of the regional sources that fetches what is missing (one per process and source set)."""
    key = (os.fspath(cache_dir), tuple(sources))
    with _PHOTO_LOCK:
        got = _PHOTO_READERS.get(key)
        if got is None:
            import imagery
            got = _PHOTO_READERS[key] = imagery.Imagery(os.path.join(key[0], "imagery"), key[1], fetch_workers=4,
                                                        mem_tiles=24, leaf_on=False)
        return got


_URGENT = __import__("threading").local()


@__import__("contextlib").contextmanager
def urgent_photos(on=True):
    """Within (the thread building a road tile): its roof photo fetched as netfetch "urgent", ahead of the far field's
    photos from the same host (blockworker.roads_geo: the tiles round the car). A fresh French place's first road tiles
    waited out ROOF_PHOTO_WAIT_S behind the far field's ~2000 IGN tile requests (its 25/s): ~8 s each, then the
    procedural roofs, a revisit fault and the tile built again later (2026-11-11)."""
    prev = getattr(_URGENT, "on", False)
    _URGENT.on = bool(on)
    try:
        yield
    finally:
        _URGENT.on = prev


class RoofPhotoFuture:
    """roof_photo on a thread (started when the tile starts): colours() waits for it ROOF_PHOTO_WAIT_S at most."""

    def __init__(self, frame, x0, y0, x1, y1, cache_dir):
        global _PHOTO_POOL
        self.fut = None
        if not ROOF_PHOTO_ON or cache_dir is None or not _photo_sources(frame, x0, y0, x1, y1):
            return
        with _PHOTO_LOCK:
            if _PHOTO_POOL is None:
                from concurrent.futures import ThreadPoolExecutor
                _PHOTO_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="roofphoto")
        self.fut = _PHOTO_POOL.submit(roof_photo_final, frame, x0, y0, x1, y1, cache_dir, True, None,
                                      getattr(_URGENT, "on", False))

    def colours(self, rings):
        if self.fut is None:
            return None
        try:
            ph, final, until = self.fut.result(timeout=ROOF_PHOTO_WAIT_S)
        except Exception:
            ph, final, until = None, False, None
        if ph is None:
            import revisit
            if not final:           # (could not be had, or not yet: a later visit builds it again, with the photo)
                revisit.fault()
            elif until is not None:     # (none there on answers that expire: kept until the first one does)
                revisit.expires(until)
            return None             # (none there: the procedural roofs are the tile's answer)
        if until is not None:       # (a later source's photo where an earlier one's "nothing there" expires)
            import revisit
            revisit.expires(until)
        return ph.colours(rings)


# The high-resolution photos a roof or square is read from, in the terrain imagery's order (imagery.DEFAULT_CHAIN):
# Maryland's own six-inch photo (md6in) first, then the regional ones. md6in was left out: Baltimore lies inside
# Virginia's box (south of 39.47 N), so its tiles read Virginia's service, blank outside that state, and no roof or
# square there was ever coloured (Fells Point's red-brick Broadway Square drawn grey pavers, the user's report 14,
# 2026-11-12)
PHOTO_EXTRA_SOURCES = ("md6in",)


def _photo_sources(frame, x0, y0, x1, y1):
    import imagery
    import imagery_regional
    lat, lon, _ = frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.0)
    lat, lon = float(np.asarray(lat).ravel()[0]), float(np.asarray(lon).ravel()[0])
    out = []
    for name in imagery.DEFAULT_CHAIN:
        if name in imagery_regional.REGIONAL:
            s_, w_, n_, e_ = imagery_regional.REGIONAL[name][2]
        elif name in PHOTO_EXTRA_SOURCES and name in imagery.SOURCES and imagery.SOURCES[name].bounds:
            s_, w_, n_, e_ = imagery.SOURCES[name].bounds
        else:
            continue
        if s_ <= lat <= n_ and w_ <= lon <= e_:
            out.append(name)
    return tuple(out)


def roof_photo(frame, x0, y0, x1, y1, cache_dir, online=False, px=None, urgent=False):
    """RoofPhoto of the tile's box from the regional orthophoto (online: fetching what is missing; else what is
    on disk), or None (none covers it, it could not be had, or ROOF_PHOTO_ON is off). px: its size (default
    ROOF_PHOTO_PX; coarser is cheaper: the squares' colour votes). urgent: fetched as netfetch "urgent"
    (urgent_photos)."""
    return roof_photo_final(frame, x0, y0, x1, y1, cache_dir, online, px, urgent)[0]


def roof_photo_final(frame, x0, y0, x1, y1, cache_dir, online=False, px=None, urgent=False):
    """(roof_photo's RoofPhoto or None, final, until). A photo's until: when an earlier lazy source's "nothing there"
    that handed some of its tiles to a later source expires (None: none did). A None is final when it is the answer:
    no regional photo covers the box or ROOF_PHOTO_ON is off (until None: for good), or every lazy source tile it
    could use is decided
    (imagery.lazy_state: on disk, or known to hold nothing there) and the sources still hold too little of it (a
    state's edge, a remembered nodata answer); those answers expire (a miss's host miss_ttl; the non-lazy sources'
    misses, not looked at one by one, within NEG_RECHECK_S), so until (unix s) is when the first may: what was built
    on it is kept only until then (revisit.expires). Not final when the photo could not be had (a fetch failed, a
    lazy tile not decided: online=False, an error): RoofPhotoFuture's revisit fault only then. Before 2026-11-13 any
    None was a fault: where Maryland's md6in was remembered missing (its service's white, kept as a miss for 90 days)
    no build of the road tiles there was ever kept (797 not kept in a Fells Point session, the tiles round the car at
    every visit); and online the lazy sources (md6in, New York's, the German states') were read from disk only, so a
    tile built before the ground photo's warm had fetched its photo faulted too."""
    if not ROOF_PHOTO_ON or cache_dir is None:
        return None, True, None
    try:
        import imagery
        import streetfront
        sources = _photo_sources(frame, x0, y0, x1, y1)
        if not sources:
            return None, True, None
        pad = 8.0
        size = max(x1 - x0, y1 - y0) + 2 * pad
        # (a whole number of the render's cells: any other size raised in imagery's tile_warp and read no photo, the
        # US squares' colour vote's sizes 7 times in 8: Broadway Square drawn grey pavers, the user's report 14)
        cells = imagery.MESH_CELLS
        px = int(-(-int(px or ROOF_PHOTO_PX) // cells) * cells)
        reader = _photo_reader_live(cache_dir, sources) if online else streetfront._photo_reader(os.fspath(cache_dir),
                                                                                                 sources)
        with (reader.urgent() if urgent and online else __import__("contextlib").nullcontext()):
            if online:          # (the lazy sources' tiles too: render reads those from disk only)
                reader.fetch_lazy(frame, x0 - pad, y0 - pad, size, px)
            rgb, used = reader.render(frame, x0 - pad, y0 - pad, size, px)
        if sum(used.get(name, 0) for name in sources) < px * px * 0.95:
            # (too little of it: the answer when every lazy tile it could use is decided; render raised where a
            # non-lazy source's fetch failed)
            resolved, expiry, _todo = reader.lazy_state(frame, x0 - pad, y0 - pad, size, px)
            if not resolved:
                return None, False, None
            cap = __import__("time").time() + NEG_RECHECK_S
            return None, True, cap if expiry is None else min(expiry, cap)
        # (the photo; where an earlier lazy source's "nothing there" gave a tile to a later one, kept until that expires.
        # Offline, roof_photo's callers: the photo alone)
        expiry = reader.lazy_state(frame, x0 - pad, y0 - pad, size, px)[1] if online else None
        return RoofPhoto(rgb, x0 - pad, y0 - pad, size), True, expiry
    except imagery.ImageryError:
        return None, False, None
    except Exception as ex:             # (the procedural roofs rather than none)
        import logging
        logging.getLogger("earthd.buildings").debug("roof photo: %s: %s", type(ex).__name__, ex)
        return None, False, None


def photo_tint(mat, rgb):
    """The vertex tint of roof material mat that shows the photo's colour rgb (sRGB 0..1) over its texture."""
    mean = ROOF_TEX_MEAN.get(mat)
    if mean is None:
        return None
    t = [min(1.0, max(0.05, float(c) / max(m, 1e-3))) for c, m in zip(rgb, mean)]
    return tuple(round(v / ROOF_PHOTO_STEP) * ROOF_PHOTO_STEP for v in t)


def circularity(ring):
    """4 pi A / P^2 of ring (k, 2): 1 for a circle, 0.79 for a square."""
    r = np.asarray(ring, np.float64)[:, :2]
    if len(r) < 3:
        return 0.0
    per = float(dnp.hypot(*np.diff(np.vstack([r, r[:1]]), axis=0).T).sum())
    return float(4.0 * math.pi * (abs(_area2(r)) / 2.0) / max(per * per, 1e-9))


# A building's era where its construction year is known (3DBAG's oorspronkelijkbouwjaar; OSM's start_date where read):
# old masonry before 1930 (more brick, tall sash windows, never a glass office), the post-war estates' precast
# concrete panels (1950-1990, four floors and up, ERA_PANEL_SHARE of them), and new offices more often glass (from
# 2000). The research's "era decides facade, windows and roof" (docs/research/2026-11-10-buildings/osm-3d-tags.md).
ERA_BRICK = {"old": 1.6, "postwar": 0.8, "new": 0.6}
ERA_GLASS = {"old": 0.0, "new": 1.8}
ERA_PANEL_SHARE = 0.6


def era(year):
    """"old" (before 1930), "postwar" (1950-1989), "new" (2000 on), or None (not known, or between)."""
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    if y < 1000 or y > 2100:
        return None
    return "old" if y < 1930 else "postwar" if 1950 <= y < 1990 else "new" if y >= 2000 else None


# A mapped facade colour is a name or a pure hex ("yellow": 0.92 / 0.82 / 0.20), a painted facade a muted one: a
# yellow house in Findlay, OH (41.0441 -83.6501, the street photos: pale yellow clapboard) was drawn lime. Saturated
# mapped colours are blended toward their own luminance's grey by up to FACADE_MUTE, by how saturated they are (toward
# a light neutral, a red facade turned pink).
FACADE_MUTE = 0.35


def facade_tone(rgb):
    """rgb (0..1) as a painted facade shows it: saturated colours muted toward their grey; muted ones as they are."""
    r = [float(v) for v in rgb]
    sat = max(r) - min(r)
    k = FACADE_MUTE * min(1.0, max(0.0, (sat - 0.25) / 0.35))
    lum = 0.2126 * r[0] + 0.7152 * r[1] + 0.0722 * r[2]
    return tuple(v + (lum - v) * k for v in r)


def custom_look(mat, rgb):
    """A look key in LOOKS for material mat with RGB tuple in [0, 1]."""
    key = f"{mat}|c_{rgb[0]:.3f}_{rgb[1]:.3f}_{rgb[2]:.3f}"
    if key not in LOOKS:
        LOOKS[key] = (mat, rgb)
    return key


class _Looks:
    """name -> (material index, RGBA bytes) for one building (its hashed brightness jitter)."""

    def __init__(self, seed):
        self.f = 1.0 + JITTER * (2.0 * _hash01(seed, "jit") - 1.0)

    def __getitem__(self, name):
        if name in LOOKS:
            mat, c = LOOKS[name]
        elif name.startswith("bne_fr_house_") and "|c_" in name:
            mat, rgb = name.split("|c_", 1)
            c = tuple(float(v) for v in rgb.split("_"))
        elif name.startswith("custom|") or ("|c_" in name):
            mat = name.split("|")[0]
            c = (1.0, 1.0, 1.0)
        else:
            mat, c = LOOKS.get(name, (FACADE, (1.0, 1.0, 1.0)))
        return MATERIALS.index(mat), tuple(int(max(0, min(255, round(255 * v * self.f)))) for v in c) + (255,)

# The window sheets' bands, top of the image first (v in Collada convention: v = 1 at the image top).
# (sheet, band index, bays per 1024 px)
BAND_TALL = (0, 3)          # m_highrise_plaster: tall windows on plaster (3 bays of uneven width)
BAND_PANEL = (1, 4)         # square windows on panels
BAND_SASH = (2, 5, (0, 2, 3))   # sash windows on plaster (bays 1 and 4 are black "open" windows: skipped)
ERA_BAND = {"old": BAND_SASH, "postwar": BAND_PANEL, "new": BAND_TALL}   # (era(): a plastered building's windows)
BAND_BRICK = (3, 5)         # sash windows on red brick
BAND_OFFICE = (0, 5)        # m_highrise_metal: curtain wall
BAND_OFFICE2 = (1, 4)
BAND_SHOP = (2, 2)          # shopfront with awnings
BANDS = 4

# Overture "bridge_structure" (OSM man_made=bridge parts with a height: the Queensboro Bridge's towers, 110 m) was a
# glass tower the decks ran through (the user's report, 2026-10-17)
BRIDGE_CLASSES = {"bridge_structure"}
BRIDGE_PIER_M = 12.0        # a bridge structure's height when not mapped
STEEL_MATERIALS = ("", "steel", "metal", "iron")     # a part over a bridge support of these (or none mapped): steel
MASONRY_MATERIALS = ("concrete", "stone", "brick", "masonry", "granite")
STONE_FACADES = ("stone", "sandstone", "limestone", "granite", "marble", "masonry", "tuff", "slate")
STEEL = "bne_bridge_steel"  # (a LOOKS name: the corrugated-metal material, painted steel grey)
# a bridge structure in its mapped paint (bridgetypes.PAINTS: the Golden Gate Bridge's towers, building:colour #bf4e3b,
# were steel grey beside its orange cables, 2026-10-18)
for _k, _c in __import__("bridgetypes").PAINTS.items():
    LOOKS[f"{STEEL}|{_k}"] = (METALWALL, tuple(v / 255.0 for v in _c))
STEEL_LEG_MAX = 3.0         # m: a frame's legs at most this thick (a quarter of its short side)
STEEL_BAY_M = 14.0          # m between a frame's bracing levels
STEEL_TOP_SHARE = 0.85      # cross beams over the decks only from this share of the height up
SHED_CLASSES = {"shed", "garage", "garages", "carport", "roof", "hut", "cabin", "barn", "farm_auxiliary",
                "greenhouse", "service", "storage_tank", "toilets", "kiosk", "parking", "shelter"}
# A grain elevator (the plains' small towns: Strasburg, CO by I-70 had two 8-storey brick apartment blocks with windows
# in the farmland): a tall slender footprint of no class (or a silo's) in a US small town, plain concrete
# A pyramid (Giza's, GitHub #47: drawn as 66-140 m boxes with windows): a pyramidal roof mapped as (nearly) the whole
# height over a monument-sized footprint is a stone pyramid on a low plinth (pitched roofs are otherwise flat past 28 m)
PYRAMID_ROOF_SHARE = 0.8
PYRAMID_MIN_M2 = 500.0
PYRAMID_PLINTH_M = 1.0
ELEVATOR_CLASSES = {"", "industrial", "silo", "grain_silo", "storage_tank", "farm_auxiliary", "agricultural"}
ELEVATOR_MIN_H = 15.0       # m mapped (a height, no floors)
TANK_CLASSES = {"storage_tank", "tank", "silo", "grain_silo", "gasometer", "digester", "slurry_tank", "oil_tank",
                "water_tank"}
TANK_ROUND = 0.85           # 4 pi A / P^2 at least: a circle (a square is 0.79)
TANK_MAX_M2 = 20000.0       # (the largest oil tanks: ~110 m across)
TANK_H, TANK_SILO_H = 0.6, 1.6      # height over the diameter when not mapped
TANK_RISE, TANK_SILO_RISE = 0.12, 0.2   # the dome's rise over the diameter
TANK_PAINTS = ((0.92, 0.92, 0.90), (0.84, 0.85, 0.86), (0.90, 0.88, 0.80), (0.72, 0.76, 0.72))
# A greenhouse (Westland, NL: 52.010 4.260, the aerial photo 2026-11-10): hectares of pale glass in aluminium bars, a
# Venlo block 5-7 m to the gutter under a near-flat ridged roof; a garden one a small glass gable. Drawn in glazing
# (GLASSHOUSE), clear, whitewashed (summer shading) or with the shade screens drawn (dark), or as the aerial shows it.
TOWER_METAL_FLOORS = 8      # storeys (or that height): a building's mapped metal is its curtain wall, not sheeting
SUPERTALL_M = 150.0         # m: a building this tall with no material mapped has a curtain wall, ...
# ... but not where its mapped colour is a stone's (masonry_colour): a pale or a warm light one, the limestone,
# terracotta and granite of the pre-war towers and their heirs; the dark, the cool mid greys and the coloured ones stay
# glass. A building drawn as its parts: its bare parts too where the colours its parts carry are (by area) mostly
# stone's (tile_buildings' _masonry_colour, parts_masonry_colour). The Empire State Building's parts carry no material
# and a pale grey on 3 of 11 (its tower turned dark glass once its base part's concrete no longer spread to them all),
# 1 Wall Street's 42 parts a light limestone colour on every one (a glass curtain wall), the 2026-11-14 in-game check.
# Of 307 buildings of 150 m or more in nine Manhattan map cells, 250 came out glass; 57 of them turn masonry: the
# Woolworth, the Trump Building, 30 Park Place, 15 Central Park West, the Waldorf and ~45 more of stone, brick or
# terracotta, and a handful of stone and glass ones (the Solow Building, 125 Broad Street); a tower with no colour
# mapped stays glass (no year to go by).
# A warm stone colour: its brightest channel, saturation and red over blue (MASONRY_WARM); a pale one (white, light
# grey): its brightest channel and saturation (MASONRY_PALE).
MASONRY_WARM = (0.60, 0.30, 0.02)   # brightest channel at least, saturation at most, red over blue at least
MASONRY_PALE = (0.80, 0.08)         # brightest channel at least, saturation at most
MASONRY_NOT = ("glass", "metal", "steel", "aluminium", "stainless_steel", "plastic")
MAST_MAX_M2 = 100.0         # m2: a mast's footprint at most ...
MAST_MIN_H = 20.0           # ... this tall ...
MAST_MATERIALS = ("metal", "steel")     # ... of steel, or slender past MAST_SLENDER (height / sqrt(area))
MAST_SLENDER = 8.0
MAST_TOP_SHARE = 0.9        # a building part is a mast only reaching this share of its building's top (or mapped metal)
CROWN_MIN_M = 60.0          # m: a part of a building this tall with a sloped or curved top and no roof material: metal
MAST_RGB = (0.62, 0.64, 0.66)
GREENHOUSE_CLASSES = {"greenhouse", "glasshouse"}
GREENHOUSE_H = 6.0          # m to the gutter of a commercial block (no height mapped)
GREENHOUSE_SMALL_M2 = 150.0     # m2: a garden greenhouse this small at most ...
GREENHOUSE_SMALL_H = 2.2        # ... its walls (m), under a glass gable of GREENHOUSE_PITCH
GREENHOUSE_PITCH = 25.0
GREENHOUSE_TINTS = (((0.98, 0.99, 0.99), 0.55), ((1.0, 1.0, 0.97), 0.25), ((0.62, 0.65, 0.66), 0.20))   # (tint, share)
GREENHOUSE_TEX_MEAN = (0.815, 0.837, 0.841)     # the glazing texture's mean (make_glasshouse.py prints it)
ELEVATOR_MAX_M2 = 1500.0
ELEVATOR_SLENDER = 0.7      # height / sqrt(area) at least (11 x 11 m, 25 m: 2.3; 28 x 31 m, 23 m: 0.78; a 15 m motel: 0.4)
GLASS_FLOORS = (20, 40)     # towers from 20 floors: glass, but for OLD_TOWER_GLASS of the old ones below 40 ...
OLD_TOWER_GLASS = 0.65      # ... (Midtown's pre-war skyscrapers are stone)
MIDRISE_GLASS_OFFICE = 0.45 # offices / hotels of 7+ floors: this share glass (modern), the rest masonry
TOWER_SHOP_FLOORS = 6       # a building this tall in a dense core: a shop / lobby ground floor whatever its class
SHOP_CLASSES = {"retail", "commercial", "supermarket", "kiosk", "office", "hotel", "civic", "restaurant"}
HOUSE_CLASSES = {"house", "detached", "semidetached_house", "bungalow", "residential", "farm", "static_caravan",
                 "cabin", "dormitory"}
ROW_CLASSES = {"terrace"}
GARAGE_MIN_M2 = 400.0       # a "parking" building this big is a parking structure
INDUSTRIAL_CLASSES = {"industrial", "warehouse", "manufacture", "factory", "hangar"}
INDUSTRIAL_LAND = {"industrial", "port", "depot"}     # land-use classes whose plain buildings are industrial ...
INDUSTRIAL_LAND_CLASSES = {"", "yes", "industrial", "commercial", "service", "warehouse", "office"}   # ... these


def _hash01(*vals):
    """A number in [0, 1) from the values (stable across runs and machines): BLAKE2b-64 of repr(vals) / 2^64
    (earthfast.hash01: ~400k calls a first visit's far building tiles)."""
    fast.require(242, "hash01")
    return fast._ef.hash01(*vals)


# ------------------------------------------------------------------------------------------- polygons

def _area2(p):
    """Twice the signed area of the ring p (n, 2), counter-clockwise positive."""
    x, y = p[:, 0], p[:, 1]
    # np.roll(v, -1) by hand: the same arrays without roll's axis handling (~28k calls a 9-tile set)
    return float(dnp.dot(x, np.concatenate((y[1:], y[:1]))) - dnp.dot(np.concatenate((x[1:], x[:1])), y))


def clean_ring(p):
    """The ring (n, 2) without its closing point, duplicate and nearly collinear vertices, counter-clockwise;
    None if fewer than 3 are left. The simplification in Rust (earthfast.clean_ring_core: numpy's arithmetic)."""
    fast.require(76, "clean_ring_core")
    p = fast._ef.clean_ring_core(np.ascontiguousarray(np.asarray(p, np.float64)[:, :2]), float(SIMPLIFY_M), int(MAX_VERTS))
    if p is None:
        return None
    p = np.asarray(p, np.float64)
    if _area2(p) < 0:
        p = p[::-1]
    return p


def triangulate(p):
    """Ear clipping of the counter-clockwise simple ring p (n, 2): (n-2, 3) indices into p (fewer if the
    ring is not simple: the rest is left out). The Rust loop (earthfast.triangulate)."""
    fast.require(76, "triangulate")
    return np.asarray(fast._ef.triangulate(np.ascontiguousarray(np.asarray(p, np.float64)[:, :2])), np.int64)


def oriented_box(p):
    """The minimum-area rectangle around ring p along one of its edges: (centre (2,), axis u (2,) along the
    long side, half length, half width). The edge search in Rust (earthfast.oriented_box_core: the same products and
    sums, math.hypot's own rounding), the rest here as before."""
    fast.require(76, "oriented_box_core")
    best = fast._ef.oriented_box_core(np.ascontiguousarray(np.asarray(p, np.float64)[:, :2]))
    if best is None:
        raise ValueError("oriented_box: a ring without an edge")
    ux, uy, vx, vy, amin, amax, bmin, bmax = best
    sa, sb = amax + amin, bmax + bmin
    c = np.array([ux * sa / 2 + vx * sb / 2, uy * sa / 2 + vy * sb / 2])
    hl, hw = np.float64((amax - amin) / 2), np.float64((bmax - bmin) / 2)     # (numpy scalars, as before)
    u = np.array([ux, uy])
    if hw > hl:
        u, hl, hw = np.array([vx, vy]), hw, hl
    return c, u, hl, hw


# ------------------------------------------------------------------------------------------- the model

class Spec:
    """What is built for one footprint (decided by plan())."""
    __slots__ = ("ring", "floors", "floor_h", "height", "roof", "roof_mat", "facade", "band", "shop", "plain", "min_h", "wall", "door", "windows", "front", "steeple", "stoop",
                 "sign", "porch", "cls", "seed", "deck", "pitch", "portal", "gring", "ship", "fr_house", "across", "hi",
                 "roof_h", "hf", "holes", "skel", "skel_cap", "tank", "street", "slope_dir", "ml")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Style:
    """A region's building habits (2026-10-15, the user: "the other parts of the world need more love": Swiss
    villages came out as clapboard houses with porches). Shares are of buildings whose data does not say:
    brick: of facades; walls: (kind, share) for detached houses' plain walls (SIDING, STUCCO; the rest the
    plaster window sheet), wall_tints: the tint indices the plain walls take (None: all but barn red);
    paint: plaster houses painted (PAINT_TINTS); porch; flat: detached houses with flat roofs; hipped: of the
    pitched; roofs: (roof material, (tint indices)) to pick from; pitch: the roof pitch in degrees (None: PITCH);
    stone: the share of low older buildings (houses and attached ones, up to STONE_FLOORS, no shops) with dressed stone
    walls; wood: siding walls are stained wood (WOOD_TINTS, barns too); barn_red: the share of barns painted barn red
    (the US's and Scandinavia's; elsewhere their siding colours: a Lauterbrunnen garage row came out a 150 m red
    wall, 2026-10-15); attached: low attached buildings (terraces, old town
    blocks) with a pitched roof (the US: flat; Amsterdam's canal houses, Europe's old towns: gables).
    Coarse boxes by region until a facade-material map; the US ones keep their earlier behaviour."""

    def __init__(self, name, brick, walls, porch, flat, hipped, roofs, pitch=None, paint=0.3, wall_tints=None,
                 wood=False, attached=0.0, barn_red=0.0, stone=0.0):
        self.name, self.brick, self.walls, self.porch, self.flat = name, brick, walls, porch, flat
        self.barn_red, self.stone = barn_red, stone
        self.hipped, self.roofs, self.pitch, self.paint, self.wall_tints = hipped, roofs, pitch, paint, wall_tints
        self.wood, self.attached = wood, attached


_US_ROOFS = ((ROOF_SLATE, (0, 1, 2, 3)), (ROOF_SHINGLE, (0, 1, 2, 3)))
_CLAY = (ROOF_TILES, (0, 1, 2, 3, 4))
_FISH = (ROOF_SLATE_ROUND, (0, 1, 2))
_COURSES = (ROOF_SLATE_DARK, (0, 1, 2))
_SHAKES = (ROOF_WOOD, (0, 1, 2, 3))
_DARK = (ROOF_SLATE, (7, 8, 9))
STYLES = {
    "us_east": Style("us_east", 0.75, ((SIDING, 0.75),), 0.55, 0.0, 0.4, _US_ROOFS, barn_red=0.5),
    "us_south": Style("us_south", 0.35, ((SIDING, 0.75),), 0.55, 0.0, 0.4, _US_ROOFS, barn_red=0.5),
    "us_plains": Style("us_plains", 0.15, ((SIDING, 0.75),), 0.55, 0.0, 0.4, _US_ROOFS, barn_red=0.5),
    "us_west": Style("us_west", 0.15, ((STUCCO, 0.65), (SIDING, 0.25)), 0.25, 0.0, 0.4, _US_ROOFS, barn_red=0.5),
    # Alpine villages: white plaster ground floors, dark wooden chalets, low wide gables of dark or brown roofs
    "alpine": Style("alpine", 0.05, ((SIDING, 0.45),), 0.0, 0.03, 0.15, (_DARK, (ROOF_SLATE, (9, 5)), _SHAKES, _FISH), pitch=24.0,
                    paint=0.05, wood=True, attached=0.85, stone=0.15),
    # north-west / central Europe: brick (Low Countries, northern Germany, Britain) or plaster, steep tiled or
    # slate roofs
    "europe_north": Style("europe_north", 0.5, (), 0.02, 0.05, 0.3, (_CLAY, _DARK, (ROOF_SLATE, (5,)), _FISH, _COURSES, _CLAY),
                          pitch=42.0,
                          paint=0.15, attached=0.75, stone=0.12),
    # Scandinavia and Finland: painted wooden houses (siding), little brick, dark or tiled roofs, falu-red barns
    "nordic": Style("nordic", 0.12, ((SIDING, 0.65),), 0.0, 0.08, 0.2, (_DARK, _CLAY, _COURSES), pitch=35.0, paint=0.2,
                    attached=0.3, barn_red=0.7),
    "mediterranean": Style("mediterranean", 0.08, ((STUCCO, 0.85),), 0.0, 0.35, 0.5, (_CLAY,), pitch=22.0,
                           paint=0.1, wall_tints=(0, 1, 2, 3, 4, 5), attached=0.8, stone=0.4),
    "latin_america": Style("latin_america", 0.25, ((STUCCO, 0.9),), 0.02, 0.7, 0.3, (_CLAY, (ROOF_METAL, (0, 1))),
                           pitch=22.0, paint=0.0, wall_tints=(0, 1, 2, 3, 5, 6, 7, 8, 9, 10)),
    "east_asia": Style("east_asia", 0.08, ((SIDING, 0.5), (STUCCO, 0.3)), 0.0, 0.35, 0.5,
                       (_DARK, (ROOF_SHINGLE, (6, 7)), (ROOF_METAL, (1, 4))), pitch=25.0, paint=0.0,
                       wall_tints=(0, 1, 6, 5), attached=0.3),
    "oceania": Style("oceania", 0.55, ((STUCCO, 0.5), (SIDING, 0.3)), 0.25, 0.05, 0.7, (_CLAY, (ROOF_METAL, (0, 1, 4))),
                     pitch=24.0, paint=0.1),
    # South Asia (India, Pakistan, Bangladesh, Sri Lanka, Nepal): a concrete frame and brick under plaster, flat terrace
    # roofs, pastel and whitewashed walls, metal roofs on sheds (OmdevShastri, PR #146)
    "south_asia": Style("south_asia", 0.08, ((STUCCO, 0.92),), 0.0, 0.94, 0.2, (_CLAY, (ROOF_METAL, (0, 1, 3))),
                        pitch=18.0, paint=0.15, wall_tints=(0, 1, 2, 3, 5, 6, 8, 10), attached=0.90),
    # elsewhere (Africa, the Middle East, ...): concrete and plaster, mostly flat roofs
    "warm": Style("warm", 0.15, ((STUCCO, 0.85),), 0.0, 0.75, 0.4, (_CLAY, (ROOF_METAL, (0, 1, 3))), pitch=20.0,
                  paint=0.0, wall_tints=(0, 1, 2, 3, 4, 5, 6, 8)),
    "cool": Style("cool", 0.3, ((SIDING, 0.4),), 0.05, 0.1, 0.3, (_DARK, (ROOF_METAL, (0, 1, 2, 3))), pitch=30.0,
                  paint=0.3, barn_red=0.5),
}


def region_style(lat, lon):
    """The Style of the region around (lat, lon) (None: the US East's)."""
    if lat is None or lon is None:
        return STYLES["us_east"]
    if 24.0 <= lat <= 72.0 and -170.0 <= lon <= -52.0:          # North America (and Canada)
        if lon < -100.0:
            return STYLES["us_west"]
        if lon <= -95.0:
            return STYLES["us_plains"]
        return STYLES["us_east"] if lat > 36.0 else STYLES["us_south"]
    if -56.0 <= lat < 24.0 and -120.0 <= lon <= -30.0:          # Mexico, the Caribbean, Central and South America
        return STYLES["latin_america"]
    if 45.4 <= lat <= 48.0 and 5.8 <= lon <= 16.5:              # the Alps
        return STYLES["alpine"]
    if 56.0 <= lat <= 72.0 and 4.5 <= lon <= 32.0:              # Norway, Sweden, Finland (brick Copenhagen, Malmo: not)
        return STYLES["nordic"]
    if 46.0 <= lat <= 72.0 and -11.0 <= lon <= 32.0:
        return STYLES["europe_north"]
    if 30.0 <= lat < 46.0 and -10.0 <= lon <= 40.0:             # southern Europe, North Africa's coast, Levant
        return STYLES["mediterranean"]
    if 20.0 <= lat <= 46.0 and 100.0 <= lon <= 146.0:           # China, Korea, Japan, Taiwan
        return STYLES["east_asia"]
    if -50.0 <= lat <= -10.0 and 110.0 <= lon <= 180.0:         # Australia, New Zealand
        return STYLES["oceania"]
    if 6.0 <= lat <= 37.5 and 68.0 <= lon <= 92.5:              # South Asia (not Myanmar, east of 92.5)
        return STYLES["south_asia"]
    if abs(lat) >= 50.0:
        return STYLES["cool"]
    return STYLES["warm"]


def brick_share(lat, lon):
    """The share of brick facades in a region (old East Coast / Midwest towns are mostly brick; the South and
    the West plaster, siding and stucco; region_style elsewhere). A coarse stand-in until a facade-material map."""
    if lat is None:
        return 0.4
    return region_style(lat, lon).brick


RUBBLE_STYLES = {"mediterranean": 0.6, "warm": 0.6, "latin_america": 0.3}


def stone_kind(style, seed):
    """A stone wall's material: rough rubble in the Mediterranean's and warm countries' villages (RUBBLE_STYLES of
    them), dressed stone elsewhere."""
    return STONE_RUBBLE if _hash01(seed, "rubble") < RUBBLE_STYLES.get(style.name, 0.0) else STONE


def roof_pitch(spec):
    """The roof pitch (degrees) of a Spec: its region's, else PITCH."""
    return spec.pitch or PITCH.get(spec.roof, PITCH["gabled"])


def roof_rise(roof, hw, pitch):
    """How far a roof rises (m) over a box of half width hw, pitch: its region's or PITCH's (a form with its own
    pitches keeps those). Pure."""
    tan = lambda deg: dmath.tan(math.radians(deg))
    if roof == "skillion":
        return 2.0 * hw * tan(SKILLION_PITCH)
    if roof == "round":
        return hw * ROUND_RISE
    if roof == "mansard":
        inset = min(0.3 * hw, MANSARD_INSET_M)
        return inset * tan(MANSARD_PITCHES[0]) + (hw - inset) * tan(MANSARD_PITCHES[1])
    if roof == "gambrel":
        out = hw * (1.0 - GAMBREL_BREAK)
        return out * tan(GAMBREL_PITCHES[0]) + (hw - out) * tan(GAMBREL_PITCHES[1])
    return hw * tan(pitch)


ALPINE_HUT_M2 = 120.0        # an isolated building this small above the tree line (a summit shelter, a mountain hut) ...
ALPINE_COVER = 0.02          # ... in a tile this empty gets dressed stone walls (the user, 2026-10-14: Mount Whitney's
                             # stone summit hut was a plain grey box)


def tree_line_m(lat):
    """A rough tree line (m) by latitude: ~3,600 m to 32 deg, falling ~110 m per degree (the Alps at 46 deg ~2,050 m,
    Scandinavia at 60 deg ~520 m), sea level towards 70 deg. Pure."""
    return max(0.0, 3600.0 - 110.0 * max(0.0, abs(float(lat)) - 32.0))


def landmark_spec(ring, name):
    """The Spec of landmark `name` (landmarks.WAYS) on its footprint ring: drawn by landmarks.build, no walls."""
    h = landmarks.height(name)
    return Spec(ring=np.asarray(ring, np.float64), floors=1, floor_h=h, height=h, roof="flat", roof_mat=ROOF_FLAT,
                facade=name, band=0, shop=False, plain=True, min_h=0.0, wall=None, door=None, windows=None, front=None,
                steeple=False, stoop=False, sign=None, porch=False, cls=landmarks.LANDMARK, seed=0.5, deck=False,
                pitch=0.0, portal=False)


LANDMARK_MATERIALS = {"iron": LANDMARK_IRON, "stone": LANDMARK_STONE, "floodlit": LANDMARK_FLOODLIT}


def _landmark_tokens():
    """landmarks.build's tokens: its lamps and dials, in their own materials (lit at night)."""
    return {"lamp": (MATERIALS.index(LANDMARK_LAMP), landmarks.LAMP_COLOUR),
            "dial": (MATERIALS.index(LANDMARK_DIAL), landmarks.BB_DIAL),
            "floodlit": (MATERIALS.index(LANDMARK_FLOODLIT), (255, 255, 255, 255))}


def _landmark_token(spec):
    return (MATERIALS.index(LANDMARK_MATERIALS[landmarks.material(spec.facade)]), landmarks.colour(spec.facade))


@profile_step('Building plan')
def plan(ring, attrs, neighbours=0, lat=None, lon=None, look=None, town=False, dense=False, west_town=False,
         alpine=False):
    """The Spec of a footprint (local ring (n, 2) in metres, counter-clockwise) from its Overture attributes
    (dict: class, height, num_floors, min_height, roof_shape, roof_height; garage: a mapped multi-storey car park
    covers it, garage_rings) and the number of buildings
    sharing a wall with it. Height and roof are predicted when not given. west_town: a small town of the
    arid West (district_town): single-storey main streets and ranch houses."""
    if attrs.get("landmark"):           # (landmarks.py: its own model, no prediction)
        return landmark_spec(ring, attrs["landmark"])
    area = _area2(ring) / 2
    c = ring.mean(0)
    # (its random choices: keyed on the map feature's id, the same from every anchor and tile; a ring without one
    # by its local centroid, as before. Keyed on the centroid in the tile's frame alone, a house changed its look
    # whenever a rebase moved the anchor: geotiles, 2026-10-29)
    sid = attrs.get("seed_id")
    seed = look if look is not None else _hash01(sid, "plan") if sid is not None else \
        _hash01(round(float(c[0]) * 2), round(float(c[1]) * 2), round(area))
    if attrs.get("ship"):           # a moored ship (ships.is_ship): a hull, not a building
        return ships.spec(ring, attrs, seed)
    if attrs.get("pier"):           # a quay structure alongside one (ships.pier_alongside): a low deck
        return ships.pier_spec(ring, seed)
    style = region_style(lat, lon)
    _, _, hl, hw = oriented_box(ring)
    box_fill = area / max(4 * hl * hw, 1e-6)
    cls = (attrs.get("class") or "").lower()
    name = str(attrs.get("name") or "").lower()
    terminal = any(w in name for w in ("terminal", "concourse"))
    # a building part (tile_buildings passes the building's top with each): judged as a piece of its building, not by
    # its own footprint (streets.gl comparison 2026-11-14: the Capitol's marble lantern rings under 40 m² came out sheds
    # in vinyl siding, the Chrysler Building's 185 m wings on under 100 m² windowless steel masts)
    part = attrs.get("building_height") is not None
    church = cls in CHURCH_CLASSES or any(w in name for w in ("church", "chapel", "cathedral"))
    if "hangar" in name:
        cls = "hangar"
    # a parking structure (a small "parking" building is a carport or a booth: a shed); "parking" is also a
    # shed class, which made big garages barns in barn-red siding (Columbia, 2026-10-14)
    garage = (cls in ("parking", "parking_garage") and area >= GARAGE_MIN_M2) or "parking garage" in name or (
        "garage" in name and area > 800) or (bool(attrs.get("garage")) and cls in GARAGE_TAG_CLASSES)
    narrow = hw < 4.2 and hl > 2.2 * hw                 # a row house: ~4-8 m wide, deep
    # in a dense core small buildings are row houses even where the footprints leave gaps (Fells Point)
    row = cls in ROW_CLASSES or (not west_town and not part and ((narrow and (neighbours >= 1 or dense)) or (
        dense and area < 260 and cls not in SHOP_CLASSES)))
    shed = (cls in SHED_CLASSES and not garage) or (area < 40 and not row and not part)
    shop = cls in SHOP_CLASSES
    industrial = cls in INDUSTRIAL_CLASSES or (area > 2500 and cls in ("", "commercial"))
    # (on industrial land, industrial_rings: a refinery's or a port's control rooms and stores, not houses with porches
    # and chimneys; Pernis, Rotterdam, against its aerial photo, 2026-11-10)
    industrial = industrial or (attrs.get("_land") == "industrial" and cls in INDUSTRIAL_LAND_CLASSES)
    industrial = industrial and not (garage or terminal)

    h = attrs.get("height")
    nf = attrs.get("num_floors")
    if terminal and (h is None or not np.isfinite(h)) and (nf is None or not np.isfinite(nf)):
        h, nf = 13.0, 2                                 # an airport terminal: two tall glass storeys
    if garage and (h is None or not np.isfinite(h)) and (nf is None or not np.isfinite(nf)):
        nf = 4 + int(seed * 2)
    if cls == "hangar" and (h is None or not np.isfinite(h)):
        h, nf = 12.0, 1
    if church and (h is None or not np.isfinite(h)) and (nf is None or not np.isfinite(nf)) and area < 2500:
        nf = 1                                          # a nave: one tall storey under a steep gable
    h = None if h is None or not np.isfinite(h) or h <= 0 else float(h)
    nf = None if nf is None or not np.isfinite(nf) or nf <= 0 else int(round(nf))
    if h is not None and nf is not None and str(attrs.get("height_src") or "") in ML_HEIGHT_SOURCES \
            and not (ML_FLOOR_M[0] <= h / nf <= ML_FLOOR_M[1]):
        h = None            # (an ML height, the footprint's average, against the map's own floors: the floors)
    rh = attrs.get("roof_height")
    rh = float(rh) if rh is not None and np.isfinite(rh) else None
    if ((attrs.get("roof_shape") or "").lower() == "pyramidal" and h is not None and rh is not None
            and rh >= PYRAMID_ROOF_SHARE * h and area >= PYRAMID_MIN_M2 and hw > 1.0):
        look = f"{STONE}|0"
        return Spec(ring=ring, floors=1, floor_h=PYRAMID_PLINTH_M, height=PYRAMID_PLINTH_M, roof="hipped", roof_mat=look,
                    facade=look, band=None, shop=False, plain=True, seed=seed, min_h=0.0, wall=None, windows=False,
                    steeple=False, porch=False, cls="pyramid", deck=False,
                    pitch=math.degrees(dmath.atan2(max(h - PYRAMID_PLINTH_M, 0.1), hw)))
    if (town and style.name.startswith("us_") and cls in ELEVATOR_CLASSES and h is not None and h >= ELEVATOR_MIN_H
            and nf is None and area <= ELEVATOR_MAX_M2 and h / math.sqrt(max(area, 1.0)) >= ELEVATOR_SLENDER
            and not (church or terminal or garage or part)):
        return Spec(ring=ring, floors=1, floor_h=h, height=h, roof="flat", roof_mat="bne_precast", facade="bne_precast",
                    band=None, shop=False, plain=True, seed=seed, min_h=0.0, wall=None, windows=False, steeple=False,
                    porch=False, cls="elevator", deck=False, pitch=None)
    # a round tank or silo (OSM's storage tanks, silos, gasometers, digesters are mapped as circles): painted steel or
    # plain concrete with a shallow dome, not a shed in siding with a gable over a circle (the user, 2026-11-10:
    # "smarter procedural generation to make them more realistic")
    if cls in TANK_CLASSES and len(ring) >= 8 and not (attrs.get("roof_shape") or "") and area <= TANK_MAX_M2 \
            and circularity(ring) >= TANK_ROUND:
        diam = 2.0 * math.sqrt(area / math.pi)
        silo = cls in ("silo", "grain_silo")
        if h is None or not np.isfinite(h) or h <= 0:
            h = float(np.clip((TANK_SILO_H if silo else TANK_H) * diam, 4.0, 45.0 if silo else 25.0))
        rise = min((TANK_SILO_RISE if silo else TANK_RISE) * diam, 0.4 * h)
        look = f"{PANEL}|0" if silo else custom_look(METALWALL, TANK_PAINTS[int(_hash01(seed, "tank") * len(TANK_PAINTS))])
        return Spec(ring=ring, floors=1, floor_h=max(h - rise, 1.0), height=max(h - rise, 1.0), roof="dome",
                    roof_mat=look, facade=look, band=None, shop=False, plain=True, seed=seed, min_h=0.0, wall=None,
                    windows=False, steeple=False, porch=False, cls="tank", deck=False,
                    pitch=math.degrees(dmath.atan2(rise, max(diam / 2, 0.1))))
    if cls in GREENHOUSE_CLASSES and not attrs.get("building_part"):
        small = area <= GREENHOUSE_SMALL_M2
        if h is None or not np.isfinite(h) or h <= 0:
            h = GREENHOUSE_SMALL_H if small else GREENHOUSE_H
        h = float(np.clip(h, 1.8, 12.0))
        ph = attrs.get("_roof_photo")
        if ph is not None:      # (the aerial: whitewashed, clear or screened, as it is)
            tint = tuple(round(min(1.0, max(0.2, c / m)) / ROOF_PHOTO_STEP) * ROOF_PHOTO_STEP
                         for c, m in zip(ph, GREENHOUSE_TEX_MEAN))
        else:
            x, tint = _hash01(seed, "glass"), GREENHOUSE_TINTS[-1][0]
            for t, share in GREENHOUSE_TINTS:
                if x < share:
                    tint = t
                    break
                x -= share
        look = custom_look(GLASSHOUSE, tint)
        return Spec(ring=ring, floors=1, floor_h=h, height=h, roof="gabled" if small and hw <= 6.0 else "flat",
                    roof_mat=look, facade=look, band=None, shop=False, plain=True, seed=seed, min_h=0.0, wall=None,
                    windows=False, steeple=False, porch=False, cls="greenhouse", deck=False,
                    pitch=GREENHOUSE_PITCH if small else None)
    # a mast or a spire's shaft: a very slender part or building of steel (One World Trade Center's 5 x 5 m, 121 m mast
    # was siding with 14 storeys of windows): plain steel, no windows, its mapped roof kept (mapped_pitch: a spire)
    # (not a slender part the map gives a sloped roof: a tower's crown fins, Manhattan's 133 mapped skillions drawn as
    # flat steel masts, streets.gl study 2026-11-11)
    if area <= MAST_MAX_M2 and h is not None and np.isfinite(h) and h >= MAST_MIN_H and not attrs.get("bridge_part") \
            and ROOF_ALIASES.get((attrs.get("roof_shape") or "").lower(), (attrs.get("roof_shape") or "").lower()) \
            not in ("skillion", "gabled", "hipped", "half-hipped") \
            and (str(attrs.get("facade_material") or "").lower() in MAST_MATERIALS
                 or (h / math.sqrt(max(area, 1.0)) >= MAST_SLENDER
                     and (not part or float(attrs.get("_part_min") or 0.0) + h
                          >= MAST_TOP_SHARE * float(attrs["building_height"])))):
        look = custom_look(METALWALL, MAST_RGB)
        mroof = ROOF_ALIASES.get((attrs.get("roof_shape") or "").lower(), (attrs.get("roof_shape") or "").lower())
        rh_m = mapped_roof_h(mroof, attrs.get("roof_height")) if mroof == "pyramidal" else None
        walls = max(h - rh_m, 1.0) if rh_m else h
        return Spec(ring=ring, floors=1, floor_h=walls, height=walls, roof=mroof if rh_m else "flat", roof_mat=look,
                    facade=look, band=None, shop=False, plain=True, seed=seed, min_h=0.0, wall=None, windows=False,
                    steeple=False, porch=False, cls="mast", deck=False,
                    pitch=mapped_pitch(mroof, oriented_box(ring)[3], attrs.get("roof_height")) if rh_m else None)
    if attrs.get("bridge_part"):    # a mapped part of a bridge's tower (support_rings): a plain box of painted steel
        return Spec(ring=ring, floors=1, floor_h=h or BRIDGE_PIER_M, height=h or BRIDGE_PIER_M, roof="flat",
                    roof_mat=f"{STEEL}|{attrs['bridge_part']}", facade=f"{STEEL}|{attrs['bridge_part']}", band=None,
                    shop=False, plain=True,
                    seed=seed, min_h=0.0, wall=None, windows=False, steeple=False, porch=False, cls="bridge_part",
                    deck=False, pitch=None)
    mat_ = str(attrs.get("facade_material") or "").lower()
    if attrs.get("bridge_masonry") or (cls in BRIDGE_CLASSES and mat_ in MASONRY_MATERIALS):
        # a mapped masonry pier (Tower Bridge's 9 m concrete piers under its stone towers): a solid block, not a frame
        look = f"{STONE}|0" if mat_ in ("stone", "brick") else "bne_precast"
        return Spec(ring=ring, floors=1, floor_h=h or BRIDGE_PIER_M, height=h or BRIDGE_PIER_M, roof="flat",
                    roof_mat=look, facade=look, band=None, shop=False, plain=True, seed=seed, min_h=0.0, wall=None,
                    windows=False, steeple=False, porch=False, cls="bridge_pier", deck=False, pitch=None)
    if cls in BRIDGE_CLASSES:       # a bridge's tower or pier: an open steel frame (_steel_frame), the decks through it
        import bridgetypes
        pt = bridgetypes.paint(attrs.get("facade_color"))
        return Spec(ring=ring, floors=1, floor_h=h or BRIDGE_PIER_M, height=h or BRIDGE_PIER_M, roof="flat",
                    roof_mat=ROOF_FLAT, facade=f"{STEEL}|{pt}" if pt else STEEL, band=None, shop=False, plain=True,
                    seed=seed, min_h=0.0,
                    wall=None, windows=False, steeple=False, porch=False, cls=cls, deck=False, pitch=None)
    floor_h = FLOOR_M
    if industrial:
        floor_h = 4.5
    if church:
        floor_h = 7.5
    # (a big shed is a barn; a row of garages, a carport or a canopy is not)
    barn = cls in BARN_CLASSES or (shed and area >= 150 and cls not in ("garage", "garages", "carport", "roof", "shelter"))
    if barn:
        floor_h = 5.5 if area >= 150 else 3.5          # one tall storey; the gable above it
    if h is None and nf is None:
        if shed:
            nf = 1
        elif row:
            nf = 2 if seed < 0.25 else (3 if seed < 0.85 else 4)
        elif west_town and not industrial and cls != "apartments":
            # Bishop's and Moab's photos (2026-10-14): one-storey shops with false fronts, ranch houses
            nf = 1 if seed < WEST_TOWN_ONE_STOREY else 2
        elif cls in HOUSE_CLASSES or (area < 220 and not shop):
            nf = 1 if seed < 0.3 else 2
        elif industrial:
            nf = 2 if area > 6000 else 1
        elif cls == "apartments":
            nf = 3 + int(seed * 3)
        elif town:
            nf = 1 if seed < 0.6 else 2                 # small towns: mostly single-storey shops and offices
        elif area < 900:
            nf = 2 + int(seed * 2)
        else:
            nf = 2 + int(seed * 3)
    # (Overture writes half_hipped: OSM's half-hipped never matched, its 133 k roofs came out predicted; its saltbox is
    # mostly OSM's double / quadruple saltbox, a gambrel's or a mansard's look: research 2026-11-10)
    roof_given = ROOF_ALIASES.get((attrs.get("roof_shape") or "").lower(), (attrs.get("roof_shape") or "").lower())
    measured_rise = attrs.get("roof_rise")      # (France's BD TOPO: a measured roof rise; None elsewhere)
    measured_rise = float(measured_rise) if measured_rise is not None and np.isfinite(measured_rise) else None
    h_src = str(attrs.get("height_src") or "")
    holed = bool(attrs.get("_holes"))           # (a block round a yard: tile_buildings' inner rings)
    if roof_given in ("gabled", "hipped", "flat", "pyramidal", "half-hipped", "mansard", "gambrel", "skillion"):
        roof = roof_given
    elif roof_given in DOME_ROOFS:
        roof = roof_given
    elif roof_given == "saltbox":
        roof = "gambrel" if hl > 1.3 * hw else "mansard"
    elif roof_given == "round":
        roof = "round"                              # (a barrel vault: hangars, halls; was drawn gabled)
    else:
        detached_house = (cls in HOUSE_CLASSES or (area < 260 and not shop)) and not industrial and not row
        if measured_rise is not None and not church:
            # France's measured roofs (bdtopo.py: IGN BD TOPO's roof rise, no shape): flat under RISE_FLAT_M, else a
            # pitched roof of the region's forms (Aix-en-Provence's old town, rises of 1.2-4.5 m: attached houses
            # under tiled pitched roofs, predicted flat as a block before)
            roof = "flat" if measured_rise < RISE_FLAT_M else \
                ("gabled" if seed < 1.0 - style.hipped else "hipped")
        elif church and neighbours == 0:
            roof = "gabled"
        elif neighbours >= 1:
            # attached: flat in the US (a pitched roof would show its gable to the neighbour); old European towns
            # are gabled (the ridge on the long axis: a narrow deep house shows its gable to the street)
            low = (nf or (h or 0) / FLOOR_M or 3) <= 6
            roof = "gabled" if (style.attached and low and not industrial and not shop
                                and _hash01(seed, "attached roof") < style.attached) else "flat"
        elif barn:
            roof = "gabled"
        elif shed and area < 80:
            roof = "gabled" if seed > 0.4 else "flat"
        elif detached_house:
            if style.flat and _hash01(seed, "flat roof") < style.flat:
                roof = "flat"
            else:
                roof = "gabled" if seed < 1.0 - style.hipped else "hipped"
        else:
            roof = "flat"
    across, hi = ridge_frame(ring, roof, attrs.get("roof_orientation"), attrs.get("roof_direction"))
    shw = hl if across else hw                      # (the half span the roof's slopes cover)
    # pitched roofs only on near-rectangular footprints (a church's nave keeps its gable beside a tower or an apse)
    # (a mapped dome follows its own outline: round parts and the largest domes keep it; a sliver or a dome over
    # DOME_MAX_HW is more likely a mapping slip: flat)
    # A roof shape the map gives on its own outline is never downgraded (streets.gl study, 2026-11-11: One World Trade
    # Center's four corner triangles are skillions 362 m tall, two of them drawn as flat 417 m prisms; of Manhattan's
    # 1,431 pitched parts 447 came out flat, 210 skillions by the half-span rule alone): a mapped skillion, or a mapped
    # pitched roof with its mapped height, is drawn over its own outline (heightfield_roof) whatever its box
    mapped_hf = (roof == roof_given and roof in HF_FORMS and len(ring) <= HF_MAX_VERTS and hw >= 0.5
                 and (roof == "skillion" or mapped_roof_h(roof, attrs.get("roof_height")) is not None))
    if roof in DOME_ROOFS:
        if hw < DOME_MIN_HW or hw > DOME_MAX_HW:
            roof = "flat"
    elif roof != "flat" and (shw < 2.0 or shw > (18.0 if barn else 20.0 if church else 14.0)) \
            and not (holed and roof in SKEL_FORMS) and not mapped_hf:
        roof = "flat"           # (a block round a yard: its wings' width counts, the skeleton's, not its box's)
    elif roof != "flat" and box_fill < (0.6 if church else 0.8):
        # (an L / T / U house: its roof on its own outline, heightfield_roof, where the box would add too much; a
        # church keeps its nave's box)
        roof = "flat" if (church or roof not in HF_FORMS or (box_fill < HF_MIN_FILL and not mapped_hf)
                          or len(ring) > HF_MAX_VERTS) else roof
    hf = roof in HF_FORMS and not church and (box_fill < HF_BOX_FILL or (holed and roof in SKEL_FORMS)) \
        and len(ring) <= HF_MAX_VERTS
    if mapped_hf and roof in HF_FORMS and (shw < 2.0 or shw > 14.0 or box_fill < HF_BOX_FILL):
        hf = True               # (its box's span says nothing of it: the slopes over the outline itself)
    # (a hipped-style roof over the outline and its yards by the straight skeleton: its rise from the skeleton itself,
    # so the walls stop where the roof starts; a mapped roof height its slope; else the region's pitch, capped)
    skel, skel_cap, skel_pitch = False, None, None
    if hf and roof in SKEL_FORMS:
        s1 = skeleton_rise(ring, attrs.get("_holes") if holed else ())
        if s1 > 0.3:
            rh_ = mapped_roof_h(roof, attrs.get("roof_height"))
            k_ = rh_ / s1 if rh_ else dmath.tan(math.radians(style.pitch or PITCH.get(roof, PITCH["gabled"])))
            k_ = min(max(k_, dmath.tan(math.radians(MAPPED_PITCH[0]))), dmath.tan(math.radians(MAPPED_PITCH[1])))
            skel, skel_pitch = True, math.degrees(dmath.atan(k_))
            skel_cap = None if rh_ else SKEL_CAP_M
            shw = min(s1, (SKEL_CAP_M / k_) if skel_cap else s1)     # (the roof_rise below: the skeleton's)
    # a city tower's ground floor is shops or a lobby (Midtown, 2026-10-14: most towers have no class, and their
    # curtain wall came down to the pavement)
    tall = (nf or (h or 0) / FLOOR_M) >= TOWER_SHOP_FLOORS
    if dense and tall and cls not in HOUSE_CLASSES and not (shed or barn or garage or terminal or church):
        shop = True
    if church:
        shop = False                                # (no shopfronts on a church, Cluj 2026-10-15)
    if h is None:
        h = nf * floor_h + (GROUND_FLOOR_SHOP_M - floor_h if shop and nf > 1 else 0.0) + (0.6 if roof == "flat" else 0.2)
        if west_town and roof == "flat" and nf == 1 and area >= 80 and not (shed or barn):
            h += FALSE_FRONT_M                      # a main-street shop's tall parapet
    else:
        if roof in DOME_ROOFS:
            h = max(2.5, h - dome_rise(roof, hw, attrs.get("roof_height")))
        elif attrs.get("roof_height") and np.isfinite(attrs["roof_height"]) and roof != "flat":
            h = max(2.5, h - float(attrs["roof_height"]))
        elif roof != "flat" and h_src != "ign_bdtopo_eave":     # (BD TOPO's height without a rise: the eaves')
            h = max(2.5, h - roof_rise(roof, shw, style.pitch or PITCH.get(roof, PITCH["gabled"])))
        elif roof == "flat" and h_src == "ign_bdtopo" and measured_rise is not None \
                and measured_rise >= RISE_FLAT_M:
            # a measured rise the outline cannot carry as a pitched roof (flattened above), or a block's lift housing:
            # the walls to the eaves (BD TOPO's hauteur), not to the top
            h = max(2.5, h - measured_rise)
        if nf is None:
            # a church: one tall storey (Cluj's St. Michael's, 25 m, came out as three 8 m floors)
            nf = 1 if church else max(1, int(round(h / floor_h)))
    nf = max(1, min(nf, 120))
    floor_h = h / nf
    if shed or barn:
        facade, band, plain = SIDING, None, True
    else:
        # glass curtain walls on towers; the mid-rises of old city cores (and a share of the older towers) are
        # masonry with punched windows (Midtown's street photos, 2026-10-14: the theatre district's 5-15 storey
        # brick and stone buildings had all come out as glass towers)
        u_glass = _hash01(seed, "glass")
        # a building drawn as its parts: the style of the whole building on every part (NYC 2026-10-14: a brick
        # part on a glass tower, each part decided by its own height)
        bh = attrs.get("building_height")
        snf = max(nf, int(bh / FLOOR_M)) if bh is not None and np.isfinite(bh) else nf
        # (the building's era where its construction year is known, era(): 3DBAG's, OSM's start_date)
        ep = era(attrs.get("_year"))
        modern = cls in ("office", "commercial", "hotel") and u_glass < MIDRISE_GLASS_OFFICE * ERA_GLASS.get(ep, 1.0)
        tall = snf >= GLASS_FLOORS[1] or (snf >= GLASS_FLOORS[0] and (modern or u_glass < OLD_TOWER_GLASS)) \
            or (snf >= 7 and modern)
        if ep == "postwar" and snf >= 4 and not tall and not industrial and not shop \
                and _hash01(seed, "era panel") < ERA_PANEL_SHARE:
            # (the post-war estates' precast concrete slabs, their windows in ribbons)
            facade, band = f"{PANEL}|{int(_hash01(seed, 'panel tint') * len(WALL_TINTS[PANEL]))}", BAND_PANEL
        elif snf >= 7 and not tall and not industrial:
            if _hash01(seed, "brick") < brick_share(lat, lon) * 1.2 * ERA_BRICK.get(ep, 1.0):
                facade, band = BRICK_TINTS[int(seed * 7919) % len(BRICK_TINTS)], BAND_BRICK
            else:
                tints = REGION_FACADE_TINTS.get(style.name, FACADE_TINTS)
                facade = tints[int(seed * 7919) % len(tints)]
                band = BAND_TALL if seed < 0.6 else BAND_SASH
        elif tall or (industrial and not shed):
            facade = f"{OFFICE}|{int(_hash01(seed, 'glass tint') * len(WALL_TINTS[OFFICE]))}" if tall \
                else FACADE_TINTS[3]
            band = (BAND_OFFICE if seed < 0.5 else BAND_OFFICE2) if tall else BAND_PANEL
        else:
            u_brick = _hash01(seed, "brick")
            detached = (cls in HOUSE_CLASSES or (area < 260 and not shop)) and not row and neighbours == 0
            brick = u_brick < brick_share(lat, lon) * (1.15 if row else 0.35 if detached else 1.0) * ERA_BRICK.get(ep, 1.0)
            if brick:
                band = BAND_BRICK
                facade = BRICK_TINTS[int(seed * 7919) % len(BRICK_TINTS)]
            else:
                tints = REGION_FACADE_TINTS.get(style.name, FACADE_TINTS)
                facade = tints[int(seed * 7919) % len(tints)]
                if (row or cls in HOUSE_CLASSES) and _hash01(seed, "paint") < style.paint:
                    facade = PAINT_TINTS[int(_hash01(seed, "pc") * len(PAINT_TINTS))]
                if ep in ERA_BAND:
                    band = ERA_BAND[ep]
                elif row or cls in HOUSE_CLASSES or roof != "flat":
                    band = BAND_SASH if seed < 0.7 else BAND_PANEL
                else:
                    band = (BAND_SASH, BAND_TALL, BAND_PANEL)[int(seed * 104729) % 3]
        plain = False
    wall = None
    house = (cls in HOUSE_CLASSES or (area < 260 and not shop and not industrial)) and not row and not shed \
        and not part \
        and neighbours == 0 and area < 300         # a long "residential" block is not a house (Fells Point)
    if house and band is not BAND_BRICK and nf <= 3:
        # detached houses: siding in the East and South, stucco in the West; windows laid over the wall
        u = _hash01(seed, "wall")
        kind, acc = None, 0.0
        for k, share in style.walls:            # (the US East: siding 75 %; the West: stucco 65 %, siding 25 %)
            acc += share
            if u < acc:
                kind = k
                break
        if kind is not None:
            tints = style.wall_tints
            if style.wood and kind == SIDING:   # chalets: stained wood
                wall = f"{SIDING}|w{int(_hash01(seed, 'wc') * len(WOOD_TINTS))}"
            elif tints is not None:
                tints = [t for t in tints if t < len(WALL_TINTS[kind]) - (BARN_REDS if kind == SIDING else 0)] or [0]
                wall = f"{kind}|{tints[int(_hash01(seed, 'wc') * len(tints))]}"
            else:
                n = len(WALL_TINTS[kind]) - (BARN_REDS if kind == SIDING else 0)     # houses: not barn red
                n = min(n, US_WALL_TINTS.get(kind, n))
                wall = f"{kind}|{int(_hash01(seed, 'wc') * n)}"
            facade = wall
    # a hut above the tree line, alone: stone walls (alpine: the tile is nearly empty and high, tile_buildings)
    stone_hut = wall is None and alpine and area < ALPINE_HUT_M2 and neighbours == 0
    if stone_hut:                       # (sheds too: a summit shelter is shed-sized)
        wall = f"{STONE}|{int(_hash01(seed, 'stc') * len(WALL_TINTS[STONE]))}"
        facade = wall
    # dressed stone: a regional share of low older buildings, attached ones too (their plaster sheet otherwise)
    if wall is None and style.stone and nf <= STONE_FLOORS and band is not BAND_BRICK \
            and not (shop or industrial or terminal or garage or shed or barn or church) \
            and cls not in ("apartments", "office", "commercial", "hotel", "retail") \
            and _hash01(seed, "stone") < style.stone:
        smat = stone_kind(style, seed)
        wall = f"{smat}|{int(_hash01(seed, 'stc') * len(WALL_TINTS[smat]))}"
        facade = wall
    # the mapped facade material (OSM building:material, Overture facade_material) over the guesses (Tower Bridge's
    # stone towers came out as glass towers, 2026-10-18)
    fm = str(attrs.get("facade_material") or "").lower()
    # a tower's metal is its curtain wall (high-rise metal panels and glass), not a shed's corrugated sheet; a
    # supertall with no material mapped is one too, not plaster or brick (New York by Gehry, 8 Spruce St: 271 m of
    # steel, its parts "metal" or nothing, drawn as a corrugated shed and a brown plaster sheet; the Brooklyn Bridge
    # view against a 360 photo and Google Earth, 2026-11-10)
    top_ = max(float(h or 0.0), float(attrs.get("building_height") or 0.0))
    tall_ = (nf or 0) >= TOWER_METAL_FLOORS or top_ >= TOWER_METAL_FLOORS * FLOOR_M
    if not fm and top_ >= SUPERTALL_M and not (church or barn or shed or industrial):
        own_c = parse_color(attrs.get("facade_color"))
        stone_c = own_c if masonry_colour(own_c) else attrs.get("_masonry_colour") if own_c is None else None
        if stone_c is not None:
            # (a stone colour: the plaster window sheet in it, its tall windows, MASONRY_*; the mapped colour is laid
            # on below)
            facade, band, plain, wall = custom_look(FACADE, facade_tone(stone_c)), BAND_TALL, False, None
        else:
            fm = "glass"
    if fm in ("metal", "steel", "aluminium") and tall_ and not (barn or shed or industrial):
        fm = "glass"
    if fm in STONE_FACADES and not (barn or shed):
        smat = stone_kind(style, seed) if fm in ("stone", "masonry", "tuff") else STONE
        wall = f"{smat}|{int(_hash01(seed, 'stc') * len(WALL_TINTS[smat]))}"
        facade, plain = wall, False
    elif fm == "brick" and not (barn or shed):
        facade, band, plain, wall = BRICK_TINTS[int(seed * 7919) % len(BRICK_TINTS)], BAND_BRICK, False, None
    elif fm == "glass":
        facade = f"{OFFICE}|{int(_hash01(seed, 'glass tint') * len(WALL_TINTS[OFFICE]))}"
        band, plain, wall = BAND_OFFICE, False, None
    elif fm in ("wood", "timber", "log", "wood_boards", "timber_framing") and not (barn or shed):
        wall = f"{SIDING}|w{int(_hash01(seed, 'wc') * len(WOOD_TINTS))}"
        facade, plain = wall, False
    elif fm in ("cement_block", "cinder_block", "concrete_block", "block") and not (barn or shed):
        wall = f"{CINDER}|{int(_hash01(seed, 'wc') * len(WALL_TINTS[CINDER]))}"
        facade, plain = wall, False
    elif fm in ("concrete", "cement", "precast") and not (barn or shed):
        wall = f"{CONCRETE}|0"
        facade, plain = wall, False
    elif fm in ("plaster", "stucco", "render", "clay") and not (barn or shed):
        wall = f"{STUCCO}|{int(_hash01(seed, 'wc') * len(WALL_TINTS[STUCCO]))}"
        facade = wall
    elif fm in ("metal", "steel", "corrugated_iron", "aluminium") and not (barn or shed):
        wall = f"{METALWALL}|{int(_hash01(seed, 'wc') * len(WALL_TINTS[METALWALL]))}"
        facade, plain = wall, False
    elif fm in ("plastic", "vinyl") and not (barn or shed):     # (vinyl siding, the US's most mapped "plastic")
        facade, band, plain = SIDING, None, True
    fc = parse_color(attrs.get("facade_color"))
    if fc is not None and not attrs.get("bridge_part"):
        fc = facade_tone(fc)
        if wall is not None:
            base = wall.split("|")[0]
            wall = custom_look(base, fc)
            facade = wall
        elif fm == "brick":
            facade = custom_look(BRICK_WALL, fc)
            wall = facade
        elif fm == "glass":
            facade = custom_look(OFFICE, fc)
        else:
            facade = custom_look(FACADE, fc)
    if terminal:
        facade, band, plain = OFFICE, BAND_OFFICE, False
    elif garage:
        facade, band, plain = FACADE_TINTS[3], BAND_PANEL, False
    windows = True
    if not (shed or barn or row or house or terminal or garage or church) and nf <= 2 and roof == "flat" \
            and (area >= BIG_BOX_M2 or industrial) \
            and cls not in HOUSE_CLASSES and cls != "apartments":
        kind = METALWALL if industrial else PANEL
        wall = f"{kind}|{int(_hash01(seed, 'wc') * len(WALL_TINTS[kind]))}"
        facade, windows = wall, False
    rmat = str(attrs.get("roof_material") or "").lower()
    # a dome with no roof material, or "slate" over stone walls (Overture's roof_material has no stone or marble: the
    # US Capitol's marble dome, OSM roof:material=marble, comes as slate; a flat "slate" roof there too, its drum's and
    # lantern's rings), takes its walls' light look, or a pale metal
    # (it came out in the region's dark slate, or a tall building's dark metal: the Capitol's white dome dark, the
    # 2026-11-14 in-game check; real domes are painted metal, stone, copper or lead, mapped where they are)
    slate_on_stone = rmat in ("slate", "stone", "marble") and str(attrs.get("facade_material") or "").lower() \
        in STONE_FACADES
    dome_walls = (roof in DOME_ROOFS and (not rmat or slate_on_stone)) or (roof == "flat" and slate_on_stone)
    if dome_walls:
        roof_mat = dome_look(facade, band)
    elif rmat in ("metal", "tin", "steel", "zinc", "corrugated_iron", "sheet"):
        roof_mat = f"{ROOF_METAL}|{int(_hash01(seed, 'roof') * len(ROOF_TINTS[ROOF_METAL]))}"
    elif rmat == "copper":
        roof_mat = custom_look(ROOF_METAL, COLOR_NAMES["copper"])
    elif rmat in ("tile", "tiles", "roof_tiles", "clay", "terracotta"):
        roof_mat = f"{ROOF_TILES}|{int(_hash01(seed, 'roof') * len(ROOF_TINTS[ROOF_TILES]))}"
    elif rmat in ("slate", "stone"):            # (dark slate courses or fish-scale slate)
        mat = ROOF_SLATE_DARK if _hash01(seed, "slate kind") < 0.6 else ROOF_SLATE_ROUND
        roof_mat = f"{mat}|{int(_hash01(seed, 'roof') * len(ROOF_TINTS[mat]))}"
    elif rmat in ("wood", "wooden", "shakes", "wood_shingle"):
        roof_mat = f"{ROOF_WOOD}|{int(_hash01(seed, 'roof') * len(ROOF_TINTS[ROOF_WOOD]))}"
    elif rmat in ("shingle", "shingles"):
        roof_mat = f"{ROOF_SHINGLE}|{int(_hash01(seed, 'roof') * len(ROOF_TINTS[ROOF_SHINGLE]))}"
    elif rmat in ("concrete", "cement", "tar_paper", "bitumen", "asphalt", "felt", "gravel"):
        roof_mat = f"{ROOF_FLAT}|{int(_hash01(seed, 'roof') * len(ROOF_TINTS[ROOF_FLAT]))}"
    elif rmat == "glass":
        roof_mat = f"{OFFICE}|0"
    elif rmat in ("eternit", "asbestos", "fibre_cement"):        # (corrugated fibre cement: pale grey)
        roof_mat = custom_look(ROOF_METAL, (0.62, 0.62, 0.60))
    elif rmat == "plastic":
        roof_mat = f"{ROOF_METAL}|{int(_hash01(seed, 'roof') * len(ROOF_TINTS[ROOF_METAL]))}"
    elif rmat in ("grass", "green"):                              # (a green roof)
        roof_mat = custom_look(ROOF_FLAT, (0.30, 0.42, 0.20))
    elif rmat == "thatch":
        roof_mat = custom_look(ROOF_SHINGLE, (0.52, 0.43, 0.28))
    elif rmat in ("solar_panels", "solar"):
        roof_mat = custom_look(ROOF_METAL, (0.10, 0.14, 0.28))
    elif roof == "flat" or barn or shed:
        roof_mat = ROOF_FLAT if roof == "flat" else ROOF_METAL
        roof_mat = f"{roof_mat}|{int(_hash01(seed, 'roof') * len(ROOF_TINTS[roof_mat]))}"
    elif part and float(attrs.get("building_height") or 0.0) >= CROWN_MIN_M:
        # a tall building's sloped or curved top with no roof material mapped: metal (the Chrysler Building's
        # stainless-steel arches came out in the region's asphalt shingles; Overture drops OSM's stainless_steel)
        roof_mat = f"{ROOF_METAL}|{int(_hash01(seed, 'roof') * len(ROOF_TINTS[ROOF_METAL]))}"
    else:       # the region's pitched roofs (the US: slate or shingle by the seed, their first four tints)
        mat, tints = style.roofs[min(int(seed * len(style.roofs)), len(style.roofs) - 1)]
        roof_mat = f"{mat}|{tints[int(_hash01(seed, 'roof') * len(tints))]}"
    rc = parse_color(attrs.get("roof_color"))
    if rc is not None:
        base_rmat = roof_mat.split("|")[0]
        if base_rmat == ROOF_TILES:         # (terracotta already: the mapped colour over the texture's mean)
            roof_mat = custom_look(base_rmat, photo_tint(ROOF_TILES, rc))
        else:
            roof_mat = custom_look(base_rmat, rc)
    elif attrs.get("_roof_photo") is not None and not rmat:
        # (the roof's colour in the aerial photo, RoofPhoto: a mapped colour or material wins)
        base_rmat = roof_mat.split("|")[0]
        if roof == "flat" and base_rmat == ROOF_FLAT and \
                max(attrs["_roof_photo"]) > 0.95 * max(ROOF_TEX_MEAN[ROOF_FLAT]):
            base_rmat = ROOF_BRIGHT_MAT
        tint = photo_tint(base_rmat, attrs["_roof_photo"])
        if tint is not None:
            roof_mat = custom_look(base_rmat, tint)
    if (barn or shed) and not stone_hut:    # plain walls tinted: barns the region's barn red or wood, sheds the siding colours
        tints = WALL_TINTS[SIDING]
        if barn and _hash01(seed, "barn") < style.barn_red:
            facade = f"{SIDING}|{len(tints) - 1 - int(_hash01(seed, 'red') * 2)}"
        elif style.wood:
            facade = f"{SIDING}|w{int(_hash01(seed, 'wc') * len(WOOD_TINTS))}"
        else:
            facade = f"{SIDING}|{int(_hash01(seed, 'sc') * (len(tints) - 2))}"
    # a front porch (the photos of Lancaster, Ocean City, Frederick, Moab: most houses have one): pitched-roof
    # houses, a regional share; doors() puts it on the front door's wall
    porch = (house and roof != "flat" and not barn and not shed and area < 300
             and _hash01(seed, "porch") < style.porch)
    if porch:   # pitched roofs draw the walls on the oriented box: an L-shaped house's door wall can be inside it
        _, _, hl, hw = oriented_box(ring)
        porch = area >= 0.9 * 4 * hl * hw
    if cls in CANOPY_CLASSES and not garage:
        # (an open roof on posts, driven and walked under: _canopy; its mapped height its top)
        roof, hf, porch = "flat", False, False
        try:
            hm = float(attrs.get("height"))
        except (TypeError, ValueError):
            hm = float("nan")
        h = hm if math.isfinite(hm) and 2.0 <= hm <= 30.0 else CANOPY_H.get(cls, 3.0)
        nf, floor_h = 1, h
    spec = Spec(ring=ring, floors=nf, floor_h=floor_h, height=h, roof=roof, roof_mat=roof_mat, facade=facade,
                band=band, shop=shop and not shed and nf >= 1, plain=plain, seed=seed, min_h=0.0, wall=wall,
                windows=windows, steeple=(church and area < 2500 and roof not in DOME_ROOFS and not part
                                          and not attrs.get("_parts_drawn")
                                          and (STEEPLE_KIND.get(style.name, "spire") if
                                               _hash01(seed, "tower") < STEEPLE_SHARE.get(style.name, 1.0) else False))
                or (steeple_top(STEEPLE_TOP_LOW if STEEPLE_KIND.get(style.name) == "campanile" else STEEPLE_TOP,
                                attrs["_steeple_part"])
                    if attrs.get("_steeple_part") and roof == "flat" else False),
                porch=porch, cls=cls,
                deck=garage and roof == "flat",
                pitch=math.degrees(dmath.atan2(dome_rise(roof, hw, rh), max(float(hw), 0.1))) if roof in DOME_ROOFS
                else (mapped_pitch(roof, shw, attrs.get("roof_height")) or style.pitch),
                across=across, hi=hi, roof_h=mapped_roof_h(roof, attrs.get("roof_height")), hf=hf, skel=skel,
                skel_cap=skel_cap, slope_dir=_slope_dir(roof, attrs.get("roof_direction")))
    if skel:
        spec.pitch = skel_pitch
    return francehouses.adapt(spec, attrs, lat, lon)


class Mesh:
    """Positions, uvs, colours, triangles and their material index, appended to. A material is a token
    (index, RGBA) as _Looks gives. Kept as flat lists of numbers (x y z x y z ..., one material per triangle):
    a road tile adds ~25k quads, and lists of tuples flattened at the end took ~2.5 s of a Steam Deck's 87 s. The
    walls kernel's quads come as numpy blocks (quads): the lists so far closed into a block before each (_flush),
    arrays() joins the blocks in order."""

    def __init__(self):
        self._P, self._UV, self._C, self._T, self._M = [], [], [], [], []
        self.n = 0
        self._blocks = []       # [(P (k, 3), UV (k, 2), C (k, 4) uint8, T (t, 3), M (t,))]: before the lists, in order

    # (the whole content as the flat lists it was: x y z x y z ..., for tests and callers that read it)
    P = property(lambda self: self._flat(0))
    UV = property(lambda self: self._flat(1))
    C = property(lambda self: self._flat(2))
    T = property(lambda self: self._flat(3))
    M = property(lambda self: self._flat(4))

    def _flat(self, k):
        self._flush()
        return np.concatenate([b[k].reshape(-1) for b in self._blocks]).tolist() if self._blocks else []

    def _lists(self):
        return (np.array(self._P, np.float64).reshape(-1, 3), np.array(self._UV, np.float64).reshape(-1, 2),
                np.array(self._C, np.uint8).reshape(-1, 4), np.array(self._T, np.int64).reshape(-1, 3),
                np.array(self._M, np.int64))

    def _flush(self):
        if self._P or self._UV or self._C or self._T or self._M:
            self._blocks.append(self._lists())
            self._P, self._UV, self._C, self._T, self._M = [], [], [], [], []

    def quad(self, a, b, c, d, uva, uvb, uvc, uvd, mat):
        """Corners counter-clockwise seen from outside."""
        i = self.n
        self._P += (*a, *b, *c, *d)
        self._UV += (*uva, *uvb, *uvc, *uvd)
        self._C += mat[1] * 4
        self._T += (i, i + 1, i + 2, i, i + 2, i + 3)
        self._M += (mat[0], mat[0])
        self.n += 4

    def quads(self, P, UV, mid, mats):
        """Quads from a kernel (earthfast.building_walls_np): P 12 numbers per quad, UV 8, mid the index into mats
        (material tokens) per quad; a block of the same numbers quad() would add."""
        q = len(mid)
        if not q:
            return
        self._flush()
        i = self.n
        mid = np.asarray(mid, np.intp)
        if any(mats[k] is None for k in np.unique(mid).tolist()):
            raise TypeError("a wall quad of no material")
        cols = np.array([tuple(mt[1]) * 4 if mt is not None else (0,) * 16 for mt in mats], np.uint8).reshape(-1, 16)
        idx = np.array([mt[0] if mt is not None else -1 for mt in mats], np.int64)
        T = (np.arange(q, dtype=np.int64)[:, None] * 4 + np.array([0, 1, 2, 0, 2, 3], np.int64) + i).reshape(-1, 3)
        self._blocks.append((np.asarray(P, np.float64).reshape(-1, 3), np.asarray(UV, np.float64).reshape(-1, 2),
                             cols[mid].reshape(-1, 4), T, np.repeat(idx[mid], 2)))
        self.n += 4 * q

    def tris(self, P, UV, mat):
        """Triangles P (3k, 3) with texture coordinates UV (3k, 2), all of material mat: a block of the same numbers
        tri() would add for each."""
        P = np.asarray(P, np.float64).reshape(-1, 3)
        k = len(P) // 3
        if not k:
            return
        self._flush()
        i = self.n
        self._blocks.append((P[:3 * k].copy(), np.asarray(UV, np.float64).reshape(-1, 2)[:3 * k].copy(),
                             np.tile(np.asarray(tuple(mat[1]), np.uint8), (3 * k, 1)),
                             np.arange(i, i + 3 * k, dtype=np.int64).reshape(-1, 3), np.full(k, mat[0], np.int64)))
        self.n += 3 * k

    def tri(self, a, b, c, uva, uvb, uvc, mat):
        i = self.n
        self._P += (*a, *b, *c)
        self._UV += (*uva, *uvb, *uvc)
        self._C += mat[1] * 3
        self._T += (i, i + 1, i + 2)
        self._M.append(mat[0])
        self.n += 3

    def poly(self, pts3, uv, mat):
        """A flat polygon facing up when counter-clockwise seen from above, down when clockwise (its underside:
        the ear clipping wants counter-clockwise rings and gave a clockwise one no triangles at all, so the
        parking decks' and floating parts' undersides were missing, the user's report 2026-10-14)."""
        xy = np.asarray(pts3)[:, :2]
        if _area2(xy) < 0:
            k = len(xy)
            tris = np.array([(k - 1 - c, k - 1 - b, k - 1 - a) for a, b, c in triangulate(xy[::-1])],
                            np.int64).reshape(-1, 3)
        else:
            tris = triangulate(xy)
        i = self.n
        self._P += np.asarray(pts3, np.float64)[:, :3].ravel().tolist()
        self._UV += np.asarray(uv, np.float64)[:, :2].ravel().tolist()
        self._C += tuple(mat[1]) * len(pts3)
        self._T += (np.asarray(tris, np.int64).reshape(-1, 3) + i).ravel().tolist()
        self._M += [mat[0]] * len(tris)
        self.n += len(pts3)

    def arrays(self):
        self._flush()
        blocks = self._blocks or [self._lists()]
        P, UV, C, T, M = (b[0].copy() if len(blocks) == 1 else np.concatenate(b) for b in zip(*blocks))
        for mi, f in _OWN_UV_SCALE:     # (textures with their own repeat: OWN_UV_M)
            sel = M == mi
            if sel.any():
                UV[np.unique(T[sel])] *= f
        return P, UV, T, M, C


# A sash window cut from the plaster sheet (band 2, bay 0, its sill included): u, v ranges (Collada v up)
WIN_UV = (53 / 1024, 154 / 1024, 1.0 - (512 + 238) / 1024, 1.0 - (512 + 45) / 1024)
WIN_W, WIN_H, WIN_SILL = 1.05, 1.75, 0.85   # m
HOUSE_BAY_M = 3.4           # m between a house's windows
DOOR_GAP_M = 0.75           # m each side of a front door kept free of the ground floor's windows
WIN_OUT_M = 0.03            # m the window stands proud of its wall


_WALL_CONSTS = None


def _walls(m, spec, ring, z0, z_eave, mat_idx, lite=False, hole=False):
    """Each wall of the ring, floor by floor: one band of the window sheet, a whole number of bays; a plain wall with
    sash windows laid over it for a plain tileable material (spec.wall; a big box's storefront band); a plain plinth
    under the floors on a slope; a shopfront ground floor; the good bays only where a band has some; the front door's
    bay as the band's plain strip, at the sheet's scale. The Rust loop (earthfast.building_walls: the quads the Python
    made, in its order and arithmetic)."""
    global _WALL_CONSTS
    if _WALL_CONSTS is None:
        _WALL_CONSTS = [float(v) for v in (SIDING_UV_M, TILE_PX_M, FOUNDATION_M, BANDS, BAND_SHOP[0], BAND_SHOP[1],
                                           DOOR_GAP_M, PLAIN_STRIP, GROUND_FLOOR_SHOP_M, WIN_OUT_M, HOUSE_BAY_M,
                                           *WIN_UV, WIN_W, WIN_H, WIN_SILL)]
    fast.require(197, "building_walls")
    band = spec.band or (0, 1)
    good = list(band[2]) if len(band) > 2 else []
    door = spec.door
    lobby = bool(spec.shop) and not spec.wall and str(spec.facade or "").startswith(OFFICE) \
        and spec.cls not in LOBBY_NOT
    mats = [mat_idx[spec.facade], mat_idx[LOBBY if lobby else SHOP] if (spec.shop or spec.wall) else None,
            mat_idx["bne_bld_fac_white"] if spec.wall else None]
    # each window its own brightness at night (see WIN_CELLS): the bands whose floors start at a hashed cell, near tiles
    # in runs of two band lengths (the shopfronts' two bays: a bay each)
    vary = []
    if mats[0][0] in _SHEET_MI and not (mats[0][0] == _FACADE_MI and band[0] == 0) and not (good and not lite):
        vary.append((0, int(band[0]), int(band[1]), 2 * int(band[1])))
    if mats[1] is not None and not lobby:
        vary.append((1, BAND_SHOP[0], BAND_SHOP[1], 1))
    fast.require(240, "building_walls_np")
    P, UV, mid = fast._ef.building_walls_np(
        np.ascontiguousarray(np.asarray(ring, np.float64)[:, :2]), float(z0), float(z_eave), bool(spec.wall),
        bool(spec.plain), int(band[0]), int(band[1]), [int(g) for g in good], int(spec.floors), float(spec.floor_h),
        bool(spec.shop), int(door[0]) if door else -1, float(door[1]) if door else 0.0, spec.roof == "flat",
        float(spec.seed) if isinstance(spec.seed, float) else 0.0, spec.windows is False,
        int(spec.front) if spec.front is not None else -1, bool(lite), _WALL_CONSTS, vary,
        WIN_CELLS if spec.wall else [],
        [-1] if hole else [int(e) for e in (spec.street or ())],    # (a shop's ground floor on its street walls: doors())
        LOBBY_BAND if lobby else None)
    if len(mid):
        m.quads(P, UV, mid, mats)


# Night windows, each its own brightness (the user, 2026-11-08: "the brightness of windows should be randomized at
# night", "Do towers too"; S-moreo's PR #916, reworked 2026-11-10). The window sheets' night maps light each cell of a
# band at its own level (tools/build_night_windows.py), and each floor's quad of a band (vary: material slot, band, bays
# per sheet, run) starts at a cell of it hashed from the building's seed, wall, floor and bay (whole bays: the same
# windows by day; every floor began at the band's first, so a lit window was lit from street to roof); near tiles (not
# lite) cut a floor wider than run bays into runs, each from its own cell (a band's few cells repeating along a floor
# showed), far tiles keep it one quad; the laid-over windows (slot 2) each one of WIN_CELLS (they all took WIN_UV: lit
# alike). In UVs only, in the kernel (earthfast.building_walls, wall_cells): #916 gave each sheet three materials (a
# bay each, hashed) and cut every floor at its bays, near and far, and cost 9 % of the frame rate at night in Times
# Square (138.7 / 133.9 fps -> 128.0 / 120.3, p99 11.5 -> 14 ms, new 74 ms render and 131 ms shadow hitches: the
# materials multiply a shape's draw batches and material instancing). A synthetic downtown (576 buildings on 20 m
# lots): #916 253k -> 835k triangles near, 102k -> 689k far, 4 -> 10 materials; this 258k near, far and materials the
# same; on 40 m lots 163k -> 185k near. (A numpy pass for it doubled the walls' build time.)
WIN_CELLS = [(53 / 1024, 154 / 1024), (459 / 1024, 562 / 1024), (663 / 1024, 764 / 1024)]  # the sash band's good bays
_FACADE_MI = MATERIALS.index(FACADE)
_TILES_MI = MATERIALS.index(ROOF_TILES)
_OWN_UV_SCALE = tuple((MATERIALS.index(m), base / own) for m, (own, base) in OWN_UV_M.items())
_SHEET_MI = (_FACADE_MI, MATERIALS.index(OFFICE))  # (the plaster sheet's band 0, BAND_TALL, has bays of uneven width:
                                                   # a floor shifted by a bay would cut its windows; left as it was)


PLAIN_STRIP = 0.3           # of a bay: the plain wall between two windows of a sheet's band (centred on a bay edge)


def _cornice(m, spec, ring, z_eave, mat_idx):
    """A box moulding round a flat roof's edge (row houses' cornices, parapets), the plain white plaster."""
    mat = mat_idx["bne_bld_fac_white"]
    o, z0, z1 = CORNICE_OUT_M, z_eave - CORNICE_DOWN_M, z_eave + CORNICE_UP_M
    uv = ((0.002, 0.995), (0.012, 0.995), (0.012, 0.999), (0.002, 0.999))
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        e = b - a
        L = float(math.hypot(*e))
        if L < 0.3:
            continue
        d = e / L
        nn = np.array([d[1], -d[0]])                  # outward (counter-clockwise ring)
        a2, b2 = a + nn * o - d * o, b + nn * o + d * o
        m.quad((a2[0], a2[1], z0), (b2[0], b2[1], z0), (b2[0], b2[1], z1), (a2[0], a2[1], z1), *uv, mat)
        m.quad((a[0], a[1], z1), (a2[0], a2[1], z1), (b2[0], b2[1], z1), (b[0], b[1], z1), *uv, mat)      # top
        m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b2[0], b2[1], z0), (a2[0], a2[1], z0), *uv, mat)      # under
        # the lip's inner face, above the roof (seen from on the roof it was missing: the user's report, 2026-10-14)
        m.quad((b[0], b[1], z_eave), (a[0], a[1], z_eave), (a[0], a[1], z1), (b[0], b[1], z1), *uv, mat)


def _inside(ring, p):
    """Point p inside the ring (even-odd)."""
    x, y = p
    return fast._ef.point_inside(np.asarray(ring, np.float64), float(x), float(y))     # (Rust; even-odd)


def _box(m, c, u, hx, hy, z0, z1, mat):
    """An axis-u box (half sizes hx along u, hy across) from z0 to z1: four sides and a top."""
    v = np.array([-u[1], u[0]])
    k = [c + u * sx * hx + v * sy * hy for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    uv = ((0.002, 0.995), (0.012, 0.995), (0.012, 0.999), (0.002, 0.999))
    for i in range(4):
        a, b = k[i], k[(i + 1) % 4]
        m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1), *uv, mat)
    m.quad(*[(p[0], p[1], z1) for p in k], *uv, mat)


def _signs(m, spec, ring, z_top, mat_idx):
    """The sign band over a shop's street wall (spec.sign: its edge), shop by shop, a few without one."""
    edge = spec.sign
    a, b = ring[edge], ring[(edge + 1) % len(ring)]
    L = float(math.hypot(*(b - a)))
    if L < 3.0:
        return
    d = (b - a) / L
    out = np.array([d[1], -d[0]])
    t, k = 0.3, 0
    while t < L - 1.5:
        w = SIGN_SHOP_M[0] + (SIGN_SHOP_M[1] - SIGN_SHOP_M[0]) * _hash01(spec.seed, "signw", k)
        t1 = min(L - 0.3, t + w)
        if L - 0.3 - t1 < 2.5:                         # no sliver of a shop at the end
            t1 = L - 0.3
        if _hash01(spec.seed, "signon", k) < 0.85:
            c = a + d * ((t + t1) / 2) + out * (SIGN_OUT / 2)
            look = f"sign|{int(_hash01(spec.seed, 'signc', k) * len(SIGN_TINTS))}"
            _box(m, c, d, (t1 - t) / 2 - 0.15, SIGN_OUT / 2, z_top - 0.15 - SIGN_H, z_top - 0.15, mat_idx[look])
        t, k = t1, k + 1


PORCH_DEPTH_M = 2.2
PORCH_H_M = 2.7             # the porch roof's underside above the floor
PORCH_DECK_M = 0.35         # the deck's top above the ground


PORTAL_NEAR_M = 25.0        # m round a building where the road at a tunnel's mouth is looked for


def _portal_lift(frame, ring, x0, y0, tunnels_xy, elev):
    """How far (m) over its lowest ground a building over a tunnel stands: to OVER_TUNNEL_CLEAR_M over the lowest ground
    along the tunnel lines within PORTAL_NEAR_M of it (the road at the mouth), 0 when its ground is that high."""
    lo = ring.min(0) - PORTAL_NEAR_M
    hi = ring.max(0) + PORTAL_NEAR_M
    pts = []
    for line in tunnels_xy:
        ln = np.asarray(line, np.float64)
        if len(ln) < 2:
            continue
        seg = dnp.hypot(*np.diff(ln, axis=0).T)
        s = np.concatenate(([0.0], np.cumsum(seg)))
        t = np.arange(0.0, s[-1] + 1e-9, 2.0)
        p = np.stack([np.interp(t, s, ln[:, 0]), np.interp(t, s, ln[:, 1])], axis=1)
        p = p[(p[:, 0] >= lo[0]) & (p[:, 0] <= hi[0]) & (p[:, 1] >= lo[1]) & (p[:, 1] <= hi[1])]
        if len(p):
            pts.append(p)
    if not pts:
        return 0.0
    q = np.concatenate(pts + [ring])
    la, lo_, _ = frame.enu_to_geodetic(q[:, 0] + x0, q[:, 1] + y0, np.zeros(len(q)))
    z = np.asarray(elev(la, lo_), np.float64)
    nr = len(ring)
    road, ground = z[:-nr], z[-nr:]
    if not len(road) or not np.isfinite(road).any() or not np.isfinite(ground).any():
        return 0.0
    return max(0.0, float(np.nanmin(road)) + OVER_TUNNEL_CLEAR_M - float(np.nanmin(ground)))


TWIN_SHARE = 0.85           # a ground footprint this much inside another one, and it inside this one: the same building
TWIN_STEP_M = 1.0           # m between footprint_twins' samples


def _twin_rank(at, mh):
    """footprint_twins' rank of a footprint: a mapped height or floor count before one machine-learned from photos
    (ML_HEIGHT_SOURCES: Berners Street's twin had Microsoft's 25.8 m, an office tower's glass, over a 5-storey stone
    store mapped with its floors), then the taller."""
    h, nf = at.get("height"), at.get("num_floors")
    h = float(h) if h is not None and np.isfinite(h) and h > 0 else None
    nf = float(nf) if nf is not None and np.isfinite(nf) and nf > 0 else None
    ml = h is not None and str(at.get("height_src") or "") in ML_HEIGHT_SOURCES
    mapped = (h is not None and not ml) or nf is not None
    top = h if h is not None and not (ml and nf is not None) else (nf * FLOOR_M + mh if nf is not None else (h or 0.0))
    return (1000.0 if mapped else 0.0) + top


def _drop_twins(rings, attrs, looks, mins):
    """tile_buildings' footprints without the lower ranked of two twins (earthfast.footprint_twins): the same ground
    building mapped twice (two OSM multipolygons on the same outer ways, 75-76 Berners Street in London: two facades
    in one plane, z-fighting, the user's report 2026-11-12). Only whole buildings on the ground (no parts, ships,
    piers, landmarks or bridge structures)."""
    idx = [k for k, (r, at, lk, mh) in enumerate(zip(rings, attrs, looks, mins))
           if lk is None and mh <= 0.5 and len(r) >= 3 and not at.get("landmark")
           and not any(at.get(k_) for k_ in ROAD_PASSAGE_SKIP_ATTRS)]
    if len(idx) < 2:
        return rings, attrs, looks, mins
    fast.require(266, "footprint_twins")
    rr = [np.asarray(rings[k], np.float64)[:, :2] for k in idx]
    off = np.r_[0, np.cumsum([len(r) for r in rr])].astype(np.int64)
    rank = np.array([_twin_rank(attrs[k], mins[k]) for k in idx], np.float64)
    drop = fast._ef.footprint_twins(np.ascontiguousarray(np.concatenate(rr)), off, rank, TWIN_SHARE, TWIN_STEP_M)
    gone = {k for k, d in zip(idx, drop) if d}
    if not gone:
        return rings, attrs, looks, mins
    keep = [k for k in range(len(rings)) if k not in gone]
    return [rings[k] for k in keep], [attrs[k] for k in keep], [looks[k] for k in keep], [mins[k] for k in keep]


def _open_tunnels(frame, lines, x0, y0, elev):
    """Per tunnel line (tunnel_lines, tile-relative): is it open all along, the roads' own rule (roads.tunnel_sections,
    no part of it under TUNNEL_OPEN_COVER of ground over the line between its ends: the road tiles draw it on the
    ground, an underpass in the open)? A building passage (OSM tunnel=building_passage: a mews arch, a road under a
    block) is such a 'tunnel' on the street's level; the building over it stands on the ground beside its way."""
    import roads as roadsmod
    out = np.zeros(len(lines), bool)
    for k, ln in enumerate(lines):
        ln = np.asarray(ln, np.float64)
        seg = dnp.hypot(*np.diff(ln, axis=0).T)
        arc = np.r_[0.0, np.cumsum(seg)]
        if arc[-1] < 0.5:
            continue
        s = np.unique(np.r_[np.arange(0.0, arc[-1], 2.0), arc[-1]])
        p = np.stack([np.interp(s, arc, ln[:, 0]), np.interp(s, arc, ln[:, 1])], 1)
        la, lo, _ = frame.enu_to_geodetic(p[:, 0] + x0, p[:, 1] + y0, np.zeros(len(p)))
        g = np.asarray(elev(np.asarray(la, np.float64), np.asarray(lo, np.float64)), np.float64)
        if not np.isfinite(g).all():
            continue
        _floor, kinds = roadsmod.tunnel_sections(s, g)
        out[k] = not np.asarray(kinds).any()
    return out


def base_z(spec, gmin, gmax):
    """The ground a building's floors stand on: the footprint's highest, but the lowest for one over a tunnel's portal
    (the road at its mouth: from the hill over it, Wabash's portal house floated ~10 m over the portal and
    Eisenhower's portal building over its frames, 2026-10-17)."""
    return gmin if spec.portal else gmax


# Open structures (research 2026-11-10: OSM's building=roof, 2.6 M worldwide, carports and shelters were walled sheds
# in front of fuel pumps, platforms and parking): a roof slab CANOPY_SLAB_M thick at the top on posts, the posts at
# the corners and every CANOPY_POST_GAP_M along the edges, CANOPY_POST_IN_M inside; collision the same (driven under).
CANOPY_CLASSES = {"roof", "carport", "shelter"}
CANOPY_H = {"roof": 4.5, "carport": 2.6, "shelter": 2.8}    # m: its top without a mapped height
CANOPY_SLAB_M = 0.35
CANOPY_POST_HALF_M = 0.14
CANOPY_POST_GAP_M = 8.0
CANOPY_POST_IN_M = 0.45
CANOPY_POSTS_MAX = 48


def canopy_posts(ring):
    """The posts' centres (k, 2) of a canopy over ring: each corner and every CANOPY_POST_GAP_M along each edge,
    CANOPY_POST_IN_M inside it (along the edge's inner normal; a corner along the two normals' mean). Pure."""
    r = np.asarray(ring, np.float64)[:, :2]
    if len(r) > 3 and np.allclose(r[0], r[-1]):
        r = r[:-1]
    if _area2(r) < 0:
        r = r[::-1]
    n = len(r)
    out = []
    for i in range(n):
        a, b = r[i], r[(i + 1) % n]
        d = b - a
        L = float(dnp.hypot(*d))
        if L < 1e-6:
            continue
        nin = np.array([-d[1], d[0]]) / L                   # (counter-clockwise: the inside is on the left)
        p0 = r[i - 1]
        d0 = a - p0
        L0 = max(float(dnp.hypot(*d0)), 1e-6)
        n0 = np.array([-d0[1], d0[0]]) / L0
        nc = n0 + nin
        nc = nc / max(float(dnp.hypot(*nc)), 1e-6)
        out.append(a + nc * CANOPY_POST_IN_M)
        k = int(L // CANOPY_POST_GAP_M)
        for j in range(1, k + 1):
            t = j / (k + 1)
            out.append(a + d * t + nin * CANOPY_POST_IN_M)
    if len(out) > CANOPY_POSTS_MAX:
        out = out[::int(math.ceil(len(out) / CANOPY_POSTS_MAX))]
    return np.array(out, np.float64).reshape(-1, 2)


def _canopy(m, spec, gmin, gmax, roof_tok, post_tok, lite=False):
    """An open structure (CANOPY_CLASSES): its roof slab and posts."""
    r = np.asarray(spec.ring, np.float64)[:, :2]
    if len(r) > 3 and np.allclose(r[0], r[-1]):
        r = r[:-1]
    if _area2(r) < 0:
        r = r[::-1]
    z_top = base_z(spec, gmin, gmax) + (spec.min_h or 0.0) + spec.height
    z_bot = z_top - CANOPY_SLAB_M
    uv = [(p[0] / ROOF_UV_M, p[1] / ROOF_UV_M) for p in r]
    m.poly([(p[0], p[1], z_top) for p in r], uv, roof_tok)
    m.poly([(p[0], p[1], z_bot) for p in r[::-1]], uv[::-1], post_tok)
    for i in range(len(r)):
        a, b = r[i], r[(i + 1) % len(r)]
        m.quad((a[0], a[1], z_bot), (b[0], b[1], z_bot), (b[0], b[1], z_top), (a[0], a[1], z_top),
               (0.002, 0.995), (0.012, 0.995), (0.012, 0.999), (0.002, 0.999), post_tok)
    posts = canopy_posts(r)
    if lite and len(posts) > 4:
        posts = posts[::max(1, len(posts) // 4)]
    for p in posts:
        _box(m, p, np.array([1.0, 0.0]), CANOPY_POST_HALF_M, CANOPY_POST_HALF_M, gmin - FOUNDATION_M, z_bot, post_tok)


def drawn_ring(spec):
    """The footprint its walls are drawn on: a pitched roof's house is its oriented box (build); its door, steps and
    porch go on these walls (on the map's outline they stood off a box wall at an angle, or inside it, only the
    door's frame showing: Stelvio pass, the user's reports 2026-10-17)."""
    ring = spec.ring
    if spec.roof != "flat" and spec.roof not in DOME_ROOFS and not getattr(spec, "hf", False):
        # (a dome stands on its own round drum; a heightfield roof on the outline itself)
        if spec.fr_house:
            import francehouse_mesh
            c, u, hl, hw = francehouse_mesh.model_box(ring)
        else:
            c, u, hl, hw = oriented_box(ring)
        v = np.array([-u[1], u[0]])
        ring = np.array([c - u * hl - v * hw, c + u * hl - v * hw, c + u * hl + v * hw, c - u * hl + v * hw])
    return ring


def _porch(m, spec, base, mat_idx):
    """A front porch on the front door's wall (spec.door: (edge, distance along it) of spec.ring): a deck, three
    posts and a shallow roof, the house's trim white and its roof's colour."""
    edge, sd = spec.door
    ring = drawn_ring(spec)
    a, b = ring[edge], ring[(edge + 1) % len(ring)]
    L = float(math.hypot(*(b - a)))
    if L < 4.0:
        return
    d = (b - a) / L
    out = np.array([d[1], -d[0]])
    w = min(L * 0.75, 7.5)
    c0 = float(np.clip(sd, w / 2, L - w / 2))
    c = a + d * c0
    white = mat_idx["bne_bld_fac_white"]
    D = PORCH_DEPTH_M
    _box(m, c + out * (D / 2), d, w / 2, D / 2, base - 0.3, base + PORCH_DECK_M, white)              # deck
    top = base + PORCH_DECK_M + PORCH_H_M
    for t in (-w / 2 + 0.15, 0.0, w / 2 - 0.15):                                                   # posts
        _box(m, c + d * t + out * (D - 0.15), d, 0.08, 0.08, base + PORCH_DECK_M, top, white)
    roof = mat_idx[spec.roof_mat] if spec.roof_mat else white
    _box(m, c + out * (D / 2 + 0.1), d, w / 2 + 0.15, D / 2 + 0.1, top, top + 0.18, roof)          # roof


def door_ground(spec, edge, along, gmax):
    """The ground at a point `along` m on edge `edge` of the footprint (its corners' ground, spec.gring), or gmax
    when not known: a row house's door and steps (from the footprint's highest ground, its back yard's on a slope,
    the steps on Bradford Street stood 1.5 m over the sidewalk, five or six of them; Mapillary: one or two)."""
    g = getattr(spec, "gring", None)
    ring = spec.ring
    if g is None or len(g) != len(ring):
        return gmax
    a, b = ring[edge], ring[(edge + 1) % len(ring)]
    L = float(math.hypot(*(b - a)))
    t = 0.0 if L < 1e-6 else min(max(along / L, 0.0), 1.0)
    return float(g[edge] + t * (g[(edge + 1) % len(ring)] - g[edge]))


def _stoop(m, spec, ring, gmin, base, mat_idx):
    """Marble steps up to the front door (spec.stoop: (edge, distance along it); base: the ground there, the door
    STOOP_H over it): STOOP_STEPS treads to a landing, each block down under the footprint's lowest ground (they floated where the street falls away, Fells Point, 2026-10-17)."""
    edge, sd = spec.stoop
    a, b = ring[edge], ring[(edge + 1) % len(ring)]
    L = float(math.hypot(*(b - a)))
    if L < 1e-6:
        return
    d = (b - a) / L
    out = np.array([d[1], -d[0]])
    c = a + d * sd
    mat = mat_idx[MARBLE_LOOK]
    base = door_ground(spec, edge, sd, base)     # (the ground at the door)
    top = base + STOOP_H
    z0 = min(gmin, base) - 0.3
    # as many steps as it takes down to the street (the footprint's lowest ground): from the highest one only the
    # bottom step stood a metre over the sidewalk on a white block (Bradford Street, Mapillary 2026-10-17)
    n = int(np.clip(round((top - min(gmin, base)) / STOOP_RISE), STOOP_STEPS - 1, STOOP_MAX_STEPS))
    rise = min((top - min(gmin, base)) / n, STOOP_RISE_MAX)    # (n risers: the landing's and n - 1 steps' fronts;
                                                                # higher, the bottom step on a plinth)
    for k in range(n):
        depth = STOOP_LANDING + k * STOOP_TREAD
        zt = top - k * rise
        _box_tiled(m, c + out * depth / 2, d, STOOP_W / 2, depth / 2, z0, zt, mat)


MARBLE_LOOK = "bne_marble"


def _box_tiled(m, c, u, hx, hy, z0, z1, mat, tile=1.0):
    """_box with the material tiled at `tile` m per repeat (a stone texture, not the facade sheets' colour patches)."""
    v = np.array([-u[1], u[0]])
    k = [c + u * sx * hx + v * sy * hy for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    for i in range(4):
        p, q = k[i], k[(i + 1) % 4]
        w = float(math.hypot(*(q - p))) / tile
        hz0, hz1 = z0 / tile, z1 / tile
        m.quad((p[0], p[1], z0), (q[0], q[1], z0), (q[0], q[1], z1), (p[0], p[1], z1),
               (0.0, hz0), (w, hz0), (w, hz1), (0.0, hz1), mat)
    m.quad(*[(p[0], p[1], z1) for p in k], *[(p[0] / tile, p[1] / tile) for p in k], mat)


# A church's tower by region (Rome's Trastevere, 2026-11-10: dozens of slate spires where the churches have Romanesque
# campanili, Mapillary at Santa Maria in Trastevere: a square brick tower beside the front, three times the nave's
# eaves, a low pyramid of clay tiles; many churches there have none): "spire" (northern Europe, the US: a tower with a
# tall slate spire), "campanile" (the Mediterranean, Latin America, warm countries: a tall square tower, a low tiled
# pyramid), on STEEPLE_SHARE of the churches.
STEEPLE_KIND = {"mediterranean": "campanile", "latin_america": "campanile", "warm": "campanile"}
STEEPLE_SHARE = {"mediterranean": 0.6, "warm": 0.6}
# A church mapped in parts: no tower of our own on every part (Trinity Church, New York: ten slate spires on its nave,
# aisles and tower parts, streets.gl comparison 2026-11-14), but a roof over its tower part, the top of its tallest
# compact column (steeple_parts; on the ground or not: New York's measured massing stacks a tower's top as a band of its
# own): a slate spire STEEPLE_TOP_RISE times the tower's short side (or up to the building's own height where that
# stands over the tower), or around the Mediterranean (STEEPLE_KIND) the campanile's low tiled pyramid.
STEEPLE_TOP, STEEPLE_TOP_LOW = "top", "top_low"
STEEPLE_TOP_RISE = 3.0          # x the short side (Trinity's 45 m spire on its ~10 m tower: 4.5; gothic spires 2-5)
STEEPLE_TOP_LOW_PITCH = 25.0    # degrees: a campanile's tiled pyramid
STEEPLE_TOP_MAX_HW = 3.5        # m: a spire's half width at most (St. Nicholas of Myra's 16 x 12 m tower band, New
                                # York: a squat pyramid over all of it)


def steeple_top(kind, rise):
    """Spec.steeple of a tower part: kind (STEEPLE_TOP / _LOW), with the spire's rise in metres where it is known
    (steeple_parts: up to the building's own height), "top:12.50"; rise True: the default."""
    return kind if rise is True or kind == STEEPLE_TOP_LOW else f"{kind}:{float(rise):.2f}"


def _is_steeple_top(steeple):
    return isinstance(steeple, str) and (steeple == STEEPLE_TOP_LOW or steeple.split(":")[0] == STEEPLE_TOP)


def _steeple_top(m, spec, z_eave, mat_idx):
    """A spire (or a campanile's low pyramid) over the whole of a church's tower part: its oriented box at the eave."""
    c, u, hl, hw = oriented_box(spec.ring)
    v = np.array([-u[1], u[0]])
    if spec.steeple == STEEPLE_TOP_LOW:
        k = [c + u * sx * hl + v * sy * hw for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    else:                           # (a spire on at most STEEPLE_TOP_MAX_HW each way: a wide tower top keeps a parapet)
        bl, bw = min(hl, STEEPLE_TOP_MAX_HW), min(hw, STEEPLE_TOP_MAX_HW)
        k = [c + u * sx * bl + v * sy * bw for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    if spec.steeple == STEEPLE_TOP_LOW:
        rise = hw * dmath.tan(math.radians(STEEPLE_TOP_LOW_PITCH))
        rm = mat_idx[f"{ROOF_TILES}|{int(_hash01(spec.seed, 'tower roof') * len(ROOF_TINTS[ROOF_TILES]))}"]
    else:
        rise = STEEPLE_TOP_RISE * 2.0 * hw
        if ":" in spec.steeple:     # (the building's own height over the tower: its spire's tip, at most the default)
            rise = min(rise, float(spec.steeple.split(":")[1]))
        rm = mat_idx[f"{ROOF_SLATE}|3"]
    apex = (float(c[0]), float(c[1]), float(z_eave + rise))
    for i in range(4):
        a, b = k[i], k[(i + 1) % 4]
        w = float(math.hypot(*(b - a))) / ROOF_UV_M
        m.tri((a[0], a[1], z_eave), (b[0], b[1], z_eave), apex, (0, 0), (w, 0), (w / 2, rise / ROOF_UV_M), rm)


def _steeple(m, spec, z_eave, mat_idx):
    """A church's square tower at one end of its nave (hashed): a slate spire on it, or a campanile (STEEPLE_KIND)."""
    if _is_steeple_top(spec.steeple):
        _steeple_top(m, spec, z_eave, mat_idx)
        return
    if spec.steeple == "campanile":
        _campanile(m, spec, z_eave, mat_idx)
        return
    c, u, hl, hw = oriented_box(spec.ring)
    if _hash01(spec.seed, "steeple") < 0.5:
        u = -u
    side = float(np.clip(1.1 * hw, 3.0, 6.0)) / 2
    p = c + u * (hl - side)
    for k in range(1, 12):              # on the footprint (an irregular one's box end can be outside it)
        if _inside(spec.ring, p):
            break
        p = c + u * (hl - side) * (1 - k / 12)
    rise = (min(hw, 12.0) + EAVE_M) * dmath.tan(math.radians(spec.pitch or PITCH["gabled"]))
    top = z_eave + rise + 6.0
    _box(m, p, u, side, side, z_eave - 1.0, top, mat_idx[spec.facade])
    v = np.array([-u[1], u[0]])
    k = [p + u * sx * side + v * sy * side for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    apex = (float(p[0]), float(p[1]), top + 2.6 * side * 2)
    rm = mat_idx[f"{ROOF_SLATE}|3"]
    for i in range(4):
        a, b = k[i], k[(i + 1) % 4]
        m.tri((a[0], a[1], top), (b[0], b[1], top), apex, (0, 0), (1, 0), (0.5, 2.0), rm)


def _campanile(m, spec, z_eave, mat_idx):
    """A Mediterranean bell tower: a tall square tower at one end of the nave, its own low pyramid of clay tiles."""
    c, u, hl, hw = oriented_box(spec.ring)
    if _hash01(spec.seed, "steeple") < 0.5:
        u = -u
    side = float(np.clip(1.0 * hw, 3.5, 7.0)) / 2
    p = c + u * (hl - side)
    for k in range(1, 12):
        if _inside(spec.ring, p):
            break
        p = c + u * (hl - side) * (1 - k / 12)
    rise = (min(hw, 12.0) + EAVE_M) * dmath.tan(math.radians(spec.pitch or PITCH["gabled"]))
    top = z_eave + rise + max(8.0, 1.6 * float(spec.height or 8.0))
    _box(m, p, u, side, side, z_eave - 1.0, top, mat_idx[spec.facade])
    v = np.array([-u[1], u[0]])
    o = side + 0.25                                 # (the roof's eaves past the walls)
    k = [p + u * sx * o + v * sy * o for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    apex = (float(p[0]), float(p[1]), top + 0.55 * o)
    rm = mat_idx[f"{ROOF_TILES}|{int(_hash01(spec.seed, 'tower roof') * len(ROOF_TINTS[ROOF_TILES]))}"]
    for i in range(4):
        a, b = k[i], k[(i + 1) % 4]
        m.tri((a[0], a[1], top), (b[0], b[1], top), apex, (0, 0), (2 * o / ROOF_UV_M, 0), (o / ROOF_UV_M, o / ROOF_UV_M),
              rm)


# What stands on a flat roof (the user, 2026-11-10: "apply high level of detail to all buildings and cities across the
# world"): earthfast's rooftop.rs plans it from aerial photos of flat roofs (Chicago's Loop, Berlin-Kreuzberg, Towson MD,
# Midtown): a tower's mechanical penthouse (louvred metal, a third of the roof or so), a mid-rise block's stair / lift
# bulkhead (the facade's colour), rooftop air-conditioning units by the roof's area (shops and warehouses: fields of
# them). Each box inside the outline (clear of its parapet, of each other and of a water tank's spot).
ROOF_KIT_INDUSTRIAL = ("industrial", "warehouse", "manufacture", "factory", "depot", "storage", "hangar", "service")
ROOF_KIT_COMMERCIAL = ("commercial", "retail", "office", "offices", "supermarket", "shop", "mall", "hotel", "hospital",
                       "school", "university", "college", "public", "civic", "government", "train_station",
                       "transportation", "parking", "sports_hall", "stadium", "kindergarten")
ROOF_KIT_LOUVRE_TILE_M = 3.0


def _roof_kit_kind(spec):
    cls = str(spec.cls or "")
    if cls in ROOF_KIT_INDUSTRIAL or str(spec.facade or "").startswith(METALWALL):
        return 2
    if cls in ROOF_KIT_COMMERCIAL or spec.shop or str(spec.facade or "").startswith(OFFICE):
        return 1
    return 0


def _clutter(m, spec, ring, z_eave, mat_idx):
    if spec.roof != "flat" or spec.plain or spec.cls in CHURCH_CLASSES or len(ring) < 3 \
            or spec.cls in ("mast", "tank", "greenhouse"):
        return
    import fast
    fast.require(187, "roof_kit")
    ring = np.asarray(ring, np.float64)[:, :2]
    c, u, hl, hw = oriented_box(ring)
    v = np.array([-u[1], u[0]])
    excl = (0.0, 0.0, 0.0)
    if spec.tank:                       # (the water tank's spot, tank_rows': kept clear)
        got = _tank_spot(spec)
        if got is not None:
            d = got[0] - c
            excl = (float(dnp.matmul(d, u)), float(dnp.matmul(d, v)), got[1] * 1.5 + 0.5)
    top = float(spec.min_h or 0.0) + float(spec.height or 0.0)
    boxes = fast._ef.roof_kit(np.ascontiguousarray(ring), float(c[0]), float(c[1]), float(u[0]), float(u[1]),
                              float(hl), float(hw), int(spec.seed * (1 << 53)) if spec.seed is not None else 0,
                              top, _roof_kit_kind(spec), excl)
    if not len(boxes):
        return
    unit = mat_idx["bne_bld_fac_grey"]
    louvre = mat_idx[f"{METALWALL}|{int(_hash01(spec.seed, 'louvre') * len(WALL_TINTS[METALWALL]))}"]
    own = mat_idx[spec.facade]
    for s, t, hs, ht, h, mat in boxes:
        p = c + u * s + v * t
        if mat == 1:
            _box_tiled(m, p, u, hs, ht, z_eave - 0.3, z_eave + h, louvre, ROOF_KIT_LOUVRE_TILE_M)
        else:
            _box(m, p, u, hs, ht, z_eave - 0.1, z_eave + h, own if mat == 0 else unit)


# Rooftop water tanks: New York's buildings of six storeys or more keep their water in a cedar tank on steel legs on
# the roof (the city's water pressure reaches about six storeys; ~10,000 tanks). The Brooklyn Bridge view against a
# Mapillary 360 photo (40.7088 -74.0002, 2026-11-10: a tank on the roofs in front of the skyline) and a player's
# screenshot that had them; none drawn before. Not mapped (Overture's infrastructure has none on Manhattan's roofs):
# by the seed, on WATER_TANK_SHARE of the flat roofs of WATER_TANK_FLOORS storeys in WATER_TANK_BOXES (the five
# boroughs, Hudson County), not on glass curtain walls, new buildings, sheds, churches or industry. The city's 2014 3-D
# Building Model (nycmassing.py) is no source for them either (measured 2026-11-10): it has no round solids for them (its
# 3-60 m2 rooftop pieces are boxes; the few round ones are turrets, cupolas, an apse), a tank there at all is a box of
# the size and height of a stair bulkhead (SoHo: 38.6 m2, 3.8 m on a 29.1 m roof, beside a 15.1 m2, 3.7 m bulkhead), and
# most tanks seen on the aerials have no piece; its tank-sized pieces follow the buildings' heights, not the tanks
# (pieces of 6-40 m2 rising 2.5-12 m per building: Midtown 0.83, Upper West Side 0.52, Lower East Side 0.33, Park
# Slope's rowhouses 0.10), and of 12 picked as the most tank-like (square or many-cornered) one (maybe two) was a
# tank on the photo. The NYS 2024 orthophoto is (2026-11-10, 15 hand-marked 250 m squares, 104 tanks): Chelsea 28 and
# Murray Hill 23 in a square, Jackson Heights' and Crown Heights' 5-6 storey blocks none at all, Park Slope none; where
# tools/build_nyc_tanks.py has found them (nyctanks.py, precision ~0.95, recall ~0.73) measured_tank_rows places those.
WATER_TANK_BOXES = ((40.49, -74.27, 40.92, -73.68),)
WATER_TANK_FLOORS = (6, 30)
WATER_TANK_MIN_M2 = 150.0
WATER_TANK_TOP_M = 6.0          # m: a part of a building drawn from parts carries its tank when its roof is this near the
                                # building's top (the main roof beside its stair / lift bulkhead)
WATER_TANK_SHARE = 0.55
# The tank: Small Island's rooftop water tower (si_water_tower: wooden staves under a metal top on steel legs and a
# base, 3.3 x 3.4 x 6.1 m; the user: "before creating new models see if they already exist in BeamNG assets"), a
# Forest row (tank_rows, trees.lua's rooftank), scaled WATER_TANK_SCALE by the seed
WATER_TANK_MODEL_H = 6.1
WATER_TANK_HALF_M = 1.75
WATER_TANK_SCALE = (0.9, 1.35)


def water_tank(spec, lat, lon, area, era_=None, floors=None):
    """Does this building get a rooftop water tank (WATER_TANK_*)? floors: the whole building's storeys (a part's own
    spec counts only its own). Pure (the seed)."""
    if lat is None or lon is None or spec.roof != "flat" or spec.plain or spec.holes or area < WATER_TANK_MIN_M2:
        return False
    if not any(b[0] <= lat <= b[2] and b[1] <= lon <= b[3] for b in WATER_TANK_BOXES):
        return False
    nf = floors if floors is not None else (spec.floors or 0)
    if not WATER_TANK_FLOORS[0] <= nf <= WATER_TANK_FLOORS[1] or era_ == "new":
        return False
    if str(spec.facade or "").startswith(OFFICE) or spec.cls in SHED_CLASSES or spec.cls in CHURCH_CLASSES \
            or spec.cls in ("industrial", "warehouse", "mast", "tank", "greenhouse", "hangar", "parking"):
        return False
    return _hash01(spec.seed, "water tank") < WATER_TANK_SHARE


def _tank_spot(spec):
    """(centre, half size, scale, axis u) of the building's water tank on its roof (tank_rows; _clutter keeps clear of
    it), None where none fits."""
    ring = np.asarray(spec.ring, np.float64)[:, :2]
    if len(ring) < 3:
        return None
    c, u, hl, hw = oriented_box(ring)
    v = np.array([-u[1], u[0]])
    sc = WATER_TANK_SCALE[0] + (WATER_TANK_SCALE[1] - WATER_TANK_SCALE[0]) * _hash01(spec.seed, "tank s")
    half = WATER_TANK_HALF_M * sc
    if hw < half + 1.0:
        return None
    for k in range(6):              # (a spot inside the roof, clear of its edges)
        sx = (_hash01(spec.seed, "tank x", k) * 2 - 1) * max(hl - half - 1.0, 0.0)
        sy = (_hash01(spec.seed, "tank y", k) * 2 - 1) * max(hw - half - 1.0, 0.0)
        p = c + u * sx + v * sy
        if all(_inside(ring, p + d) for d in (u * (half + .5), -u * (half + .5), v * (half + .5), -v * (half + .5))):
            return p, half, sc, u
    return None


def _tank_row(spec, gmin, gmax, p, u, sc, x0, y0):
    z = base_z(spec, gmin, gmax) + float(spec.min_h or 0.0) + float(spec.height or 0.0)
    yaw = math.degrees(dmath.atan2(u[1], u[0])) + 90.0 * int(_hash01(spec.seed, "tank yaw") * 4)
    return ["rooftank", round(float(p[0] + x0), 2), round(float(p[1] + y0), 2), round(z, 2),
            round(yaw % 360.0, 1), 1.0, round(WATER_TANK_MODEL_H * sc, 2), 0.0]


# Measured tanks (nyctanks.py: found on the NYS 2024 orthophoto): each on the topmost roof under its point, the model
# scaled to the photo's diameter (the model's top is WATER_TANK_MODEL_D across), within WATER_TANK_D_SCALE
WATER_TANK_MODEL_D = 3.4
WATER_TANK_D_SCALE = (0.8, 1.7)


def measured_tank_rows(items, x0, y0, tanks, covered):
    """Forest rows of measured tanks (tanks: nyctanks.Tanks in the frame; covered: per item, is it in a cell the photo
    answers for) plus the procedural ones (spec.tank) of the items outside the covered cells. A point on no item's
    roof: none (a building the map lacks)."""
    rows = []
    rings = []
    for k, (spec, gmin, gmax) in enumerate(items):
        ring = np.asarray(spec.ring, np.float64)[:, :2]
        rings.append(ring if len(ring) >= 3 else None)
        if not covered[k] and spec.tank:
            rows += tank_rows([(spec, gmin, gmax)], x0, y0)
    if tanks is None or not len(tanks):
        return rows
    lo = np.array([r.min(0) if r is not None else (np.inf, np.inf) for r in rings]).reshape(-1, 2)
    hi = np.array([r.max(0) if r is not None else (-np.inf, -np.inf) for r in rings]).reshape(-1, 2)
    for (px, py), d in zip(tanks.pts - np.array([x0, y0]), tanks.d):
        cand = np.flatnonzero((lo[:, 0] <= px) & (px <= hi[:, 0]) & (lo[:, 1] <= py) & (py <= hi[:, 1]))
        best, bz = None, -np.inf
        for k in cand.tolist():
            if not covered[k] or not _inside(rings[k], (px, py)):
                continue
            spec, gmin, gmax = items[k]
            z = base_z(spec, gmin, gmax) + float(spec.min_h or 0.0) + float(spec.height or 0.0)
            if z > bz:
                best, bz = k, z
        if best is None:
            continue
        spec, gmin, gmax = items[best]
        _c, u, _hl, _hw = oriented_box(rings[best])
        sc = float(np.clip(d / WATER_TANK_MODEL_D, *WATER_TANK_D_SCALE))
        rows.append(_tank_row(spec, gmin, gmax, np.array([px, py]), u, sc, x0, y0))
    return rows


def tank_rows(items, x0, y0):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] (trees.lua's rooftank: Small Island's rooftop water tank,
    the stock model) of the items' water tanks (spec.tank: water_tank), on their roofs, in the frame (x0, y0: the
    tile's origin, items' rings are relative to it)."""
    rows = []
    for spec, gmin, gmax in items:
        if not spec.tank:
            continue
        ring = np.asarray(spec.ring, np.float64)[:, :2]
        if len(ring) < 3:
            continue
        got = _tank_spot(spec)
        if got is None:
            continue
        p, half, sc, u = got
        rows.append(_tank_row(spec, gmin, gmax, p, u, sc, x0, y0))
    return rows


# Domes and onion domes from the map's roof:shape (GitHub #593's thread: "a dedicated landmark procedural generator"
# instead of hand-made models): a cathedral's, a mosque's or a capitol's dome is mapped as a building part with
# roof:shape=dome / onion and often its roof:height; drawn as a gable before. The footprint's own outline shrinks to
# a point up the profile (a round part: a dome; a square one: a cloister vault), (radius share, rise share) per ring.
DOME_ROOFS = ("dome", "onion")
DOME_RISE = {"dome": 1.0, "onion": 1.6}     # the rise over the footprint's half width when roof:height is not mapped
DOME_MAX_HW = 80.0          # m: the largest half width drawn as a dome (St. Peter's is ~21 m, the Pantheon's 22 m)
DOME_MIN_HW = 0.3           # m: the smallest (a lantern's cupola; under 1 m they went flat: 16 of Manhattan's domes)
DOME_PROFILE = {
    "dome": tuple((dmath.cos(a), dmath.sin(a)) for a in np.linspace(0.0, math.pi / 2, 9)),
    "onion": ((1.0, 0.0), (1.18, 0.15), (1.22, 0.3), (1.1, 0.45), (0.8, 0.6), (0.45, 0.75), (0.2, 0.88),
              (0.06, 0.96), (0.0, 1.0)),
}


def dome_rise(roof, hw, roof_height=None):
    """m a dome / onion roof rises over its eaves: the mapped roof:height, else DOME_RISE x the half width."""
    if roof_height is not None and np.isfinite(roof_height) and roof_height > 0:
        return float(roof_height)
    return DOME_RISE.get(roof, 1.0) * float(hw)


def _dome(m, ring, z_eave, rise, mat, shape="dome", lite=False):
    """A dome (or onion dome) of `rise` on the footprint ring at z_eave: every corner joined up DOME_PROFILE to the
    dome's centre (every other level when lite), the centre the outline's centroid, or its pole of inaccessibility where
    the centroid lies outside it (earthfast.dome_roof: Streets GL's CurvedRoofBuilder, MIT; a crescent's or an L's dome
    was scaled toward its corners' mean, outside it). mat: a material index (or the skyline's (index, colour))."""
    ring = np.asarray(ring, np.float64)[:, :2]
    if len(ring) < 3 or rise <= 0:
        return
    prof = DOME_PROFILE.get(shape, DOME_PROFILE["dome"])
    if lite:
        prof = prof[::2] if len(prof) % 2 else prof[::2] + prof[-1:]
    fast.require(297, "dome_roof")
    q, qu, t, tu = fast._ef.dome_roof(np.ascontiguousarray(ring), float(z_eave), float(rise),
                                      np.ascontiguousarray(np.asarray(prof, np.float64)), float(ROOF_UV_M))
    m.quads(q, qu, np.zeros(len(q), np.intp), [mat])
    m.tris(np.asarray(t, np.float64).reshape(-1, 3), np.asarray(tu, np.float64).reshape(-1, 2), mat)


# Pitched roofs on the footprint's own outline (heightfield_roof, research 2026-11-10): a footprint filling less than
# HF_BOX_FILL of its oriented box (an L / T / U house, a bent row) has its roof over its outline and its walls on it
# (the box's walls stood out past an L's inner corner; under 0.8 the roof went flat: 11-13 % of houses); under
# HF_MIN_FILL, or with more than HF_MAX_VERTS corners, flat as before.
HF_FORMS = {"gabled": 0, "hipped": 1, "pyramidal": 1, "half-hipped": 2, "skillion": 3}
HF_BOX_FILL = 0.9
HF_MIN_FILL = 0.45
HF_MAX_VERTS = 48


COURTYARD_MIN_M2 = 6.0      # m2: a smaller inner ring (a light well, a mapping slip) is left covered


def _holed_lid(m, ring, holes, z, mat, uv_on=True):
    """A flat lid over ring with its holes (courtyards) left open (fast.triangulate_holes)."""
    fast.require(177, "triangulate_holes")
    pts, tris = fast._ef.triangulate_holes(np.ascontiguousarray(np.asarray(ring, np.float64)[:, :2]),
                                           [np.ascontiguousarray(np.asarray(h, np.float64)[:, :2]) for h in holes])
    for a, b, c in tris.tolist():
        P = [(float(pts[i, 0]), float(pts[i, 1]), z) for i in (a, b, c)]
        uv = [(p[0] / ROOF_UV_M, p[1] / ROOF_UV_M) for p in P] if uv_on else [(0.0, 0.0)] * 3
        m.tri(P[0], P[1], P[2], uv[0], uv[1], uv[2], mat)


# The straight skeleton's roofs (earthfast skeleton_roof, research 2026-11-10): gabled, hipped, half-hipped and pyramidal
# roofs on an outline that is no rectangle (Spec.hf) and over a block's yards are its hipped roof (each wall's slope
# up to where the wavefront met; an L or a T house's wings each hipped), SKEL_CAP_M high at most without a mapped roof
# height (a flat top over it: a wide city block's roof); a footprint it fails on (2 in 14,345 Munich / Berlin) keeps
# heightfield_roof.
SKEL_FORMS = {"gabled", "hipped", "half-hipped", "pyramidal"}
SKEL_CAP_M = 5.0


def skeleton_rise(ring, holes=()):
    """The skeleton's highest point over ring (and its holes) at a slope of 1: the widest half span, m; 0 if it failed."""
    fast.require(180, "skeleton_roof")
    try:
        tri, _g, _gab = fast._ef.skeleton_roof(np.ascontiguousarray(np.asarray(ring, np.float64)[:, :2]),
                                               [np.ascontiguousarray(np.asarray(h, np.float64)[:, :2])
                                                for h in holes or ()], 1.0, 0.0, 0.0, False)
    except Exception:
        return 0.0
    return float(tri[:, 2].max()) if len(tri) else 0.0


def _skel_roof(m, spec, ring, z_eave, rm, fac):
    """A skeleton roof (Spec.skel) over ring and Spec.holes; False when the skeleton failed (the caller's other roof). A
    gabled one's hip ends are gables (the skeleton's gable: each end's top moved onto its wall, the wall in the
    facade's plain colour, as a box house's gable)."""
    fast.require(180, "skeleton_roof")
    k = dmath.tan(math.radians(roof_pitch(spec)))
    tri, grad, gab = fast._ef.skeleton_roof(
        np.ascontiguousarray(np.asarray(ring, np.float64)[:, :2]),
        [np.ascontiguousarray(np.asarray(h, np.float64)[:, :2]) for h in spec.holes or ()], float(k), float(z_eave),
        float(spec.skel_cap or 0.0), spec.roof == "gabled")
    if not len(grad):
        return False
    # (the gables, then the slopes, as blocks: the per-triangle loop with its texture coordinates in Python was ~3 % of
    # a first visit's far building tiles; earthfast.skeleton_roof_uv, the same numbers)
    fast.require(242, "skeleton_roof_uv")
    ng = len(gab) // 3
    if ng:
        m.tris(np.asarray(gab, np.float64)[:3 * ng, :3], np.tile(np.array([(0, 0.995), (0.02, 0.995), (0.01, 0.999)],
                                                                           np.float64), (ng, 1)), fac)
    tri = np.ascontiguousarray(np.asarray(tri, np.float64)[:3 * len(grad), :3])
    m.tris(tri, fast._ef.skeleton_roof_uv(tri, np.ascontiguousarray(grad, np.float64), float(ROOF_UV_M)), rm)
    return True


def _slope_dir(roof, direction):
    """A mapped skillion's roof:direction (compass degrees its slope faces, down toward), else None."""
    if roof != "skillion":
        return None
    try:
        d = float(direction)
    except (TypeError, ValueError):
        return None
    return d % 360.0 if math.isfinite(d) else None


def skillion_rise(spec, ring):
    """The rise (m, per point of ring (k, 2)) of a skillion roof over its eaves: along a mapped roof:direction
    (Spec.slope_dir) over the outline's extent that way, else across its box toward Spec.hi, as _hf_roof draws it
    (the skyline's sloped tops: sky_items keeps skillions, streets.gl study 2026-11-11)."""
    r = np.asarray(ring, np.float64)[:, :2]
    rh = getattr(spec, "roof_h", None)
    sd = getattr(spec, "slope_dir", None)
    if sd is not None:
        up = np.array([-dmath.sin(math.radians(sd)), -dmath.cos(math.radians(sd))])
        t = dnp.matmul(r, up)
        k = rh / max(float(t.max() - t.min()), 0.5) if rh else dmath.tan(math.radians(SKILLION_PITCH))
    else:
        c, u, hl, hw = oriented_box(r)
        if getattr(spec, "across", False):
            u, hl, hw = np.array([-u[1], u[0]]), hw, hl
        v = np.array([-u[1], u[0]])
        hi = getattr(spec, "hi", None) or (1.0 if spec.seed < 0.5 else -1.0)
        t = hi * (dnp.matmul(r - c, v))
        k = rh / (2.0 * max(hw, 0.5)) if rh else dmath.tan(math.radians(SKILLION_PITCH))
    return k * (t - t.min())


def _hf_roof(m, spec, ring, z_eave, rm, fac):
    """A heightfield roof (Spec.hf) over ring and the walls' tops up to it (fast.heightfield_roof): no overhang."""
    fast.require(176, "heightfield_roof")
    r = np.asarray(ring, np.float64)[:, :2]
    if len(r) > 3 and np.allclose(r[0], r[-1]):
        r = r[:-1]
    if _area2(r) < 0:
        r = r[::-1]
    r = np.ascontiguousarray(r)
    c, u, hl, hw = oriented_box(r)
    if getattr(spec, "across", False):
        u, hl, hw = np.array([-u[1], u[0]]), hw, hl
    rh = getattr(spec, "roof_h", None)
    hi = getattr(spec, "hi", None) or (1.0 if spec.seed < 0.5 else -1.0)
    sd = getattr(spec, "slope_dir", None)
    if spec.roof == "skillion" and sd is not None:
        # (a mapped roof:direction: the plane rises straight against it, over the outline's own extent that way, not
        # its box's axis: One World Trade Center's corner triangles, streets.gl study 2026-11-11)
        up = np.array([-dmath.sin(math.radians(sd)), -dmath.cos(math.radians(sd))])
        u, hi = np.array([up[1], -up[0]]), 1.0
        t = dnp.matmul(r, up)
        span = float(t.max() - t.min())
        k = rh / max(span, 0.5) if rh else dmath.tan(math.radians(SKILLION_PITCH))
    elif spec.roof == "skillion":
        k = rh / (2.0 * max(hw, 0.5)) if rh else dmath.tan(math.radians(SKILLION_PITCH))
    else:
        k = dmath.tan(math.radians(roof_pitch(spec)))
    tri, grad, walls = fast._ef.heightfield_roof(r, HF_FORMS[spec.roof], float(c[0]), float(c[1]), float(u[0]),
                                                 float(u[1]), float(k), float(z_eave), HALF_HIP, float(hi))
    for j in range(len(grad)):
        gx, gy = float(grad[j, 0]), float(grad[j, 1])
        gm = math.hypot(gx, gy)
        if gm < 1e-9:
            d, a, f = (0.0, 1.0), (1.0, 0.0), 1.0
        else:
            d = (gx / gm, gy / gm)                  # (up the slope)
            a, f = (-d[1], d[0]), math.sqrt(1.0 + gm * gm)
        pts = [tuple(float(x) for x in tri[3 * j + i]) for i in range(3)]
        uvs = [((p[0] * a[0] + p[1] * a[1]) / ROOF_UV_M, (p[0] * d[0] + p[1] * d[1]) * f / ROOF_UV_M) for p in pts]
        m.tri(pts[0], pts[1], pts[2], uvs[0], uvs[1], uvs[2], rm)
    tall = _slope_floors(m, spec, walls, z_eave, fac)
    for ax, ay, za, bx, by, zb in walls.tolist():
        if (za - z_eave > 0.02 or zb - z_eave > 0.02) and not (tall and max(za, zb) - z_eave >= SLOPE_WALL_RISE_M):
            m.quad((ax, ay, z_eave), (bx, by, z_eave), (bx, by, zb), (ax, ay, za),
                   (0, 0.995), (0.02, 0.995), (0.02, 0.999), (0, 0.999), fac)


# The walls above a tall building's sloped roof in floors of its window sheet (streets.gl study 2026-11-11, proposal 3:
# One World Trade Center's corner walls rise 360 m over their eave, a plain sliver of the facade's colour): a wall piece
# rising SLOPE_WALL_RISE_M over the eave, on a building of SLOPE_WALL_FLOORS floors or more with a window sheet (a
# house's gable stays plain, as real ones mostly are)
SLOPE_WALL_RISE_M = 6.0
SLOPE_WALL_FLOORS = 4


def _slope_floors(m, spec, walls, z_eave, fac):
    """Draw the tall walls above the eave as floors of the window sheet (earthfast.slope_wall_floors). -> drawn?"""
    if spec.plain or spec.wall or (spec.floors or 0) < SLOPE_WALL_FLOORS or fac[0] not in _SHEET_MI or not len(walls):
        return False
    fast.require(208, "windows above a sloped roof's eave")
    band = spec.band or (0, 1)
    P, UV = fast._ef.slope_wall_floors(np.ascontiguousarray(walls, np.float64), float(z_eave),
                                       float(spec.floor_h), float(TILE_PX_M), int(band[0]), float(BANDS),
                                       float(SLOPE_WALL_RISE_M))
    for k in range(0, len(P), 3):
        m.tri(tuple(P[k].tolist()), tuple(P[k + 1].tolist()), tuple(P[k + 2].tolist()),
              tuple(UV[k].tolist()), tuple(UV[k + 1].tolist()), tuple(UV[k + 2].tolist()), fac)
    return True


# the roof shapes as Overture spells some (OSM's spellings kept)
ROOF_ALIASES = {"half_hipped": "half-hipped", "halfhipped": "half-hipped", "double_saltbox": "saltbox",
                "quadruple_saltbox": "saltbox", "lean_to": "skillion", "monopitch": "skillion", "pitched": "gabled",
                "side_hipped": "hipped"}
MAPPED_PITCH = (8.0, 62.0)      # degrees: a mapped roof height's pitch kept in this range (slips: 30 m roofs)
# ... but a pyramidal roof on a small footprint rising several times its half span is a spire (One World Trade
# Center's mast: a 5 x 5 m part, roof:height 80 m, drawn 5 m tall at 62 degrees; mapped church steeples): its own
# rise, up to SPIRE_PITCH (the Brooklyn Bridge view against a 360 photo and a player's screenshot, 2026-11-10)
SPIRE_MAX_M = 12.0              # m: a spire's half span at most
SPIRE_PITCH = 89.5
PITCH_FORMS = ("gabled", "hipped", "half-hipped", "pyramidal")


def mapped_roof_h(roof, roof_height):
    """A mapped / measured roof height (m) a pitched roof is drawn with, or None."""
    try:
        rh = float(roof_height)
    except (TypeError, ValueError):
        return None
    if roof in ("flat",) or roof in DOME_ROOFS or not math.isfinite(rh) or rh <= 0.2:
        return None
    return rh


def mapped_pitch(roof, shw, roof_height):
    """The pitch (degrees) that makes a roof of half span shw rise roof_height (the map's or a measured LoD2 roof:
    its top where the map says, not at the region's pitch with the walls cut down by the mapped height: research
    2026-11-10), MAPPED_PITCH bounded; None: no mapped height, or a form with its own pitches."""
    rh = mapped_roof_h(roof, roof_height)
    if rh is None or roof not in PITCH_FORMS or shw <= 0.5:
        return None
    top = SPIRE_PITCH if roof == "pyramidal" and shw <= SPIRE_MAX_M else MAPPED_PITCH[1]
    return min(max(math.degrees(dmath.atan2(rh, float(shw))), MAPPED_PITCH[0]), top)


def ridge_frame(ring, roof, orientation, direction):
    """(across, hi) for a pitched roof on ring's oriented box: across: the ridge runs across the long axis (the map's
    roof:orientation=across, 72 % of the 40 k mapped; or a roof:direction, the way its slopes face, along the long
    axis); hi: a lean-to's high side (+1 / -1 along the box's width once turned, opposite where its slope faces), None:
    unknown (the seed's). Overture's roof_direction is OSM's roof:direction (compass degrees the slope faces) whatever
    its schema text says (checked on mapped skillions, 2026-11-10)."""
    if roof == "flat" or roof in DOME_ROOFS:
        return False, None
    across = str(orientation or "").lower() == "across"
    hi = None
    try:
        d = float(direction)
    except (TypeError, ValueError):
        d = float("nan")
    if math.isfinite(d):
        try:
            _c, u, _hl, _hw = oriented_box(ring)
        except ValueError:
            return across, None
        dv = np.array([dmath.sin(math.radians(d)), dmath.cos(math.radians(d))])     # (east, north)
        v = np.array([-u[1], u[0]])
        across = abs(float(dnp.matmul(dv, u))) > abs(float(dnp.matmul(dv, v)))
        if across:
            v = -u                                          # (the frame turned: v' = -u, as _roof turns it)
        hi = -1.0 if float(dnp.matmul(dv, v)) > 0 else 1.0             # (the slope faces down toward d: the high side away)
    return across, hi


# A chimney on a pitched-roof house (the user, 2026-11-10: "more features" and "smarter procedural generation"): most
# houses with a pitched roof in the temperate world have one (OSM2World draws them too), a short brick-coloured stack
# beside the ridge; CHIMNEY_SHARE of them by the seed, a house's own facade colour, not on sheds, barns or blocks.
CHIMNEY_SHARE = 0.55
CHIMNEY_HALF_M = (0.3, 0.45)        # m half size (by the seed)
CHIMNEY_OVER_M = 0.9                # m over the ridge
CHIMNEY_MAX_M2 = 400.0              # footprints larger than this (blocks, halls): none


def _chimney(m, spec, c, u, ridge_half, hw, zr, mat):
    """A chimney stack on the ridge line of a gabled / hipped box roof (centre c, ridge along u, ridge_half m each side
    of c along it, the house half hw m wide, the ridge at zr), for a house that has one by its seed."""
    if spec.cls in SHED_CLASSES or spec.cls in BARN_CLASSES or (spec.floors or 1) > 3 or ridge_half < 1.2 \
            or hw < 2.0 or 4.0 * (ridge_half + hw) * hw > CHIMNEY_MAX_M2 \
            or _hash01(spec.seed, "chimney") >= CHIMNEY_SHARE:
        return
    half = CHIMNEY_HALF_M[0] + (CHIMNEY_HALF_M[1] - CHIMNEY_HALF_M[0]) * _hash01(spec.seed, "chimney size")
    along = (2.0 * _hash01(spec.seed, "chimney at") - 1.0) * 0.7 * (ridge_half - half)
    side = (0.6 if _hash01(spec.seed, "chimney side") < 0.5 else -0.6) * min(1.0, 0.25 * hw)
    v = np.array([-u[1], u[0]])
    centre = np.asarray(c, np.float64) + u * along + v * side
    _box(m, centre, u, half, half, zr - 1.2, zr + CHIMNEY_OVER_M, mat)


def _roof(m, spec, ring, z_eave, mat_idx, lite=False):
    rm = mat_idx[spec.roof_mat]
    if spec.roof in DOME_ROOFS:
        _c, _u, _hl, hw = oriented_box(ring)
        _dome(m, ring, z_eave, hw * dmath.tan(math.radians(roof_pitch(spec))), rm, spec.roof, lite)
        return
    if spec.roof == "flat":
        if getattr(spec, "holes", None):
            _holed_lid(m, ring, spec.holes, z_eave, rm)
            return
        pts = [(p[0], p[1], z_eave) for p in ring]
        uv = [(p[0] / ROOF_UV_M, p[1] / ROOF_UV_M) for p in ring]
        m.poly(pts, uv, rm)
        return
    if getattr(spec, "skel", False) and _skel_roof(m, spec, ring, z_eave, rm, mat_idx[spec.facade]):
        return
    if getattr(spec, "holes", None) and spec.roof != "flat":      # (the skeleton failed over a yard: a flat lid)
        _holed_lid(m, ring, spec.holes, z_eave, rm)
        return
    if getattr(spec, "hf", False) and spec.roof in HF_FORMS:
        _hf_roof(m, spec, ring, z_eave, rm, mat_idx[spec.facade])
        return
    c, u, hl, hw = oriented_box(ring)
    if getattr(spec, "across", False):              # (the ridge across the long axis: the box's frame turned)
        u, hl, hw = np.array([-u[1], u[0]]), hw, hl
    v = np.array([-u[1], u[0]])
    hl2, hw2 = hl + EAVE_M, hw + EAVE_M
    rise = hw2 * dmath.tan(math.radians(roof_pitch(spec)))
    ze = z_eave - EAVE_M * dmath.tan(math.radians(roof_pitch(spec)))
    zr = ze + rise

    def at(s, t, z):
        p = c + u * s + v * t
        return (float(p[0]), float(p[1]), float(z))
    slope = math.hypot(hw2, rise)

    def uv(s, t):            # along the ridge, up the slope (m / ROOF_UV_M)
        return (s / ROOF_UV_M, (hw2 - abs(t)) / hw2 * slope / ROOF_UV_M)

    def end_wall(sgn, outline):
        """The wall up to the roof at an end of the house: outline (t, z) left to right seen from outside, fanned; the
        facade's plain colour, as a gable's."""
        s = sgn * hl
        pts = [at(s, sgn * t, z) for t, z in outline]
        for b, c in zip(pts[1:], pts[2:]):
            m.tri(pts[0], b, c, (0, 0.995), (0.02, 0.995), (0.01, 0.999), mat_idx[spec.facade])

    if spec.roof == "skillion":
        rh = getattr(spec, "roof_h", None)
        tan = rh / (2.0 * max(hw, 0.5)) if rh else dmath.tan(math.radians(SKILLION_PITCH))
        hi = getattr(spec, "hi", None) or (1.0 if spec.seed < 0.5 else -1.0)   # (the map's slope direction, or a coin)

        def z(t):
            return z_eave + (hi * t + hw) * tan
        m.quad(at(-hl2, -hw2, z(-hw2)), at(hl2, -hw2, z(-hw2)), at(hl2, hw2, z(hw2)), at(-hl2, hw2, z(hw2)),
               uv(-hl2, -hw2), uv(hl2, -hw2), (hl2 / ROOF_UV_M, 2 * hw2 / ROOF_UV_M), (-hl2 / ROOF_UV_M, 2 * hw2 / ROOF_UV_M),
               rm)
        top = z(hi * hw)
        fac = mat_idx[spec.facade]
        a, b = (hl, -hl) if hi > 0 else (-hl, hl)       # the high wall above the eave line
        m.quad(at(a, hi * hw, z_eave), at(b, hi * hw, z_eave), at(b, hi * hw, top), at(a, hi * hw, top),
               (0, 0.995), (0.02, 0.995), (0.02, 0.999), (0, 0.999), fac)
        for sgn in (-1, 1):
            end_wall(sgn, [(-hw, z_eave), (hw, z_eave), (sgn * hi * hw, top)])
        return
    if spec.roof == "round":
        rise = getattr(spec, "roof_h", None) or hw * ROUND_RISE
        ts = [-hw + 2.0 * hw * k / ROUND_SEGS for k in range(ROUND_SEGS + 1)]
        zs = [z_eave + rise * math.sqrt(max(0.0, 1.0 - dnp.sq(t / hw))) for t in ts]
        arc = 0.0
        for k in range(ROUND_SEGS):                      # (strips along the length, counter-clockwise from above)
            t0, t1, z0_, z1_ = ts[k], ts[k + 1], zs[k], zs[k + 1]
            seg = math.hypot(t1 - t0, z1_ - z0_)
            m.quad(at(hl, t0, z0_), at(hl, t1, z1_), at(-hl, t1, z1_), at(-hl, t0, z0_),
                   (hl / ROOF_UV_M, arc / ROOF_UV_M), (hl / ROOF_UV_M, (arc + seg) / ROOF_UV_M),
                   (-hl / ROOF_UV_M, (arc + seg) / ROOF_UV_M), (-hl / ROOF_UV_M, arc / ROOF_UV_M), rm)
            arc += seg
        for sgn in (-1, 1):
            end_wall(sgn, [(-hw, z_eave), (hw, z_eave)] + [(t, z) for t, z in zip(ts[::-1], zs[::-1])][1:-1])
        return
    if spec.roof == "gambrel":
        lo, up = (dmath.tan(math.radians(p)) for p in GAMBREL_PITCHES)
        tb = hw * GAMBREL_BREAK                          # the break line, out from the ridge
        ze = z_eave - EAVE_M * lo
        zb = z_eave + (hw - tb) * lo
        zr = zb + tb * up
        for sgn in (-1, 1):                              # (the slopes of one side, counter-clockwise from above)
            for t0, z0, t1, z1 in ((hw2, ze, tb, zb), (tb, zb, 0.0, zr)):
                a, b = (-hl2, hl2) if sgn < 0 else (hl2, -hl2)
                m.quad(at(a, sgn * t0, z0), at(b, sgn * t0, z0), at(b, sgn * t1, z1), at(a, sgn * t1, z1),
                       uv(a, t0), uv(b, t0), uv(b, t1), uv(a, t1), rm)
        for sgn in (-1, 1):
            end_wall(sgn, [(-hw, z_eave), (hw, z_eave), (tb, zb), (0.0, zr - EAVE_M * 0.2), (-tb, zb)])
        return
    if spec.roof == "mansard":
        lo, up = (dmath.tan(math.radians(p)) for p in MANSARD_PITCHES)
        inset = min(0.3 * hw, MANSARD_INSET_M)           # (from the walls: the overhang carries the slope on down)
        ze = z_eave - EAVE_M * lo
        zb = z_eave + inset * lo
        il, iw = hl - inset, hw - inset
        zr = zb + iw * up
        r = max(il - iw, 0.0)
        for (a, ta), (b, tb) in (((-1, -1), (1, -1)), ((1, -1), (1, 1)), ((1, 1), (-1, 1)), ((-1, 1), (-1, -1))):
            m.quad(at(a * hl2, ta * hw2, ze), at(b * hl2, tb * hw2, ze), at(b * il, tb * iw, zb), at(a * il, ta * iw, zb),
                   (0, 0), (2 * hl2 / ROOF_UV_M, 0), (2 * hl2 / ROOF_UV_M, (inset + EAVE_M) / ROOF_UV_M), (0, (inset + EAVE_M) / ROOF_UV_M), rm)
        m.quad(at(-il, -iw, zb), at(il, -iw, zb), at(r, 0, zr), at(-r, 0, zr), uv(-il, -iw), uv(il, -iw), uv(r, 0), uv(-r, 0), rm)
        m.quad(at(il, iw, zb), at(-il, iw, zb), at(-r, 0, zr), at(r, 0, zr), uv(il, iw), uv(-il, iw), uv(-r, 0), uv(r, 0), rm)
        m.tri(at(il, -iw, zb), at(il, iw, zb), at(r, 0, zr), (-iw / ROOF_UV_M, 0), (iw / ROOF_UV_M, 0), (0, slope / ROOF_UV_M), rm)
        m.tri(at(-il, iw, zb), at(-il, -iw, zb), at(-r, 0, zr), (-iw / ROOF_UV_M, 0), (iw / ROOF_UV_M, 0), (0, slope / ROOF_UV_M), rm)
        return
    if spec.roof == "pyramidal":
        apex = at(0, 0, zr)
        for (a, ta), (b, tb) in (((-1, -1), (1, -1)), ((1, -1), (1, 1)), ((1, 1), (-1, 1)), ((-1, 1), (-1, -1))):
            m.tri(at(a * hl2, ta * hw2, ze), at(b * hl2, tb * hw2, ze), apex,
                  (-hl2 / ROOF_UV_M, 0), (hl2 / ROOF_UV_M, 0), (0, slope / ROOF_UV_M), rm)
        return
    if spec.roof == "half-hipped":
        tw = hw2 * HALF_HIP                              # where the small hip's foot meets the slopes
        zc = zr - rise * HALF_HIP
        r = max(hl2 - tw, 0.0)
        m.quad(at(-hl2, -hw2, ze), at(hl2, -hw2, ze), at(hl2, -tw, zc), at(-hl2, -tw, zc),
               uv(-hl2, -hw2), uv(hl2, -hw2), uv(hl2, -tw), uv(-hl2, -tw), rm)
        m.quad(at(-hl2, -tw, zc), at(hl2, -tw, zc), at(r, 0, zr), at(-r, 0, zr),
               uv(-hl2, -tw), uv(hl2, -tw), uv(r, 0), uv(-r, 0), rm)
        m.quad(at(hl2, hw2, ze), at(-hl2, hw2, ze), at(-hl2, tw, zc), at(hl2, tw, zc),
               uv(hl2, hw2), uv(-hl2, hw2), uv(-hl2, tw), uv(hl2, tw), rm)
        m.quad(at(hl2, tw, zc), at(-hl2, tw, zc), at(-r, 0, zr), at(r, 0, zr),
               uv(hl2, tw), uv(-hl2, tw), uv(-r, 0), uv(r, 0), rm)
        m.tri(at(hl2, -tw, zc), at(hl2, tw, zc), at(r, 0, zr), (-tw / ROOF_UV_M, 0), (tw / ROOF_UV_M, 0), (0, slope / ROOF_UV_M), rm)
        m.tri(at(-hl2, tw, zc), at(-hl2, -tw, zc), at(-r, 0, zr), (-tw / ROOF_UV_M, 0), (tw / ROOF_UV_M, 0), (0, slope / ROOF_UV_M), rm)
        top = zc + EAVE_M * rise / hw2                   # (the wall reaches the hip above it, under the overhang)
        for sgn in (-1, 1):
            end_wall(sgn, [(-hw, z_eave), (hw, z_eave), (tw, top), (-tw, top)])
        return
    if spec.roof in ("gabled", "hipped") and not lite:
        _chimney(m, spec, c, u, hl2 if spec.roof == "gabled" else max(hl2 - hw2, 0.0), hw, zr, mat_idx[spec.facade])
    if spec.roof == "gabled":
        r = hl2
        m.quad(at(-hl2, -hw2, ze), at(hl2, -hw2, ze), at(r, 0, zr), at(-r, 0, zr),
               uv(-hl2, -hw2), uv(hl2, -hw2), uv(r, 0), uv(-r, 0), rm)
        m.quad(at(hl2, hw2, ze), at(-hl2, hw2, ze), at(-r, 0, zr), at(r, 0, zr),
               uv(hl2, hw2), uv(-hl2, hw2), uv(-r, 0), uv(r, 0), rm)
        # the gable ends: wall triangles from the eave line up to the ridge, the facade's plain colour
        fac = mat_idx[spec.facade]
        for sgn in (-1, 1):
            s = sgn * hl
            a, b, top = at(s, -sgn * hw, z_eave), at(s, sgn * hw, z_eave), at(s, 0, zr - EAVE_M * 0.2)
            if sgn > 0:
                m.tri(a, b, top, (0, 0.995), (0.02, 0.995), (0.01, 0.999), fac)
            else:
                m.tri(a, b, top, (0, 0.995), (0.02, 0.995), (0.01, 0.999), fac)
    else:                    # hipped
        r = max(hl2 - hw2, 0.0)
        m.quad(at(-hl2, -hw2, ze), at(hl2, -hw2, ze), at(r, 0, zr), at(-r, 0, zr),
               uv(-hl2, -hw2), uv(hl2, -hw2), uv(r, 0), uv(-r, 0), rm)
        m.quad(at(hl2, hw2, ze), at(-hl2, hw2, ze), at(-r, 0, zr), at(r, 0, zr),
               uv(hl2, hw2), uv(-hl2, hw2), uv(-r, 0), uv(r, 0), rm)
        m.tri(at(hl2, -hw2, ze), at(hl2, hw2, ze), at(r, 0, zr),
              (-hw2 / ROOF_UV_M, 0), (hw2 / ROOF_UV_M, 0), (0, slope / ROOF_UV_M), rm)
        m.tri(at(-hl2, hw2, ze), at(-hl2, -hw2, ze), at(-r, 0, zr),
              (-hw2 / ROOF_UV_M, 0), (hw2 / ROOF_UV_M, 0), (0, slope / ROOF_UV_M), rm)


# The engine draws a shape's mesh with 16-bit indices: past 65,535 vertices they wrap and triangles join far-apart
# vertices (the user's report 2026-10-14: slabs across a Fells Point street; dense tiles had up to 81k vertices).
# The far tiles and packs too (players' "spiderweb" reports 2026-11-14: a Manhattan pack was one mesh of 705k
# vertices, a far tile 80k): every building shape is build_parts' meshes, several in one shape (write: an object each).
MAX_MESH_VERTS = 60000
VERTS_MAX = 65535           # cdae.VERTS_MAX: a single building past it is left out (build_parts; none near: ~11k most)
over_limit = {"buildings": 0}   # buildings left out so by this process (build_parts)


def _concat(meshes):
    """build() meshes (the same MATERIALS) as one."""
    if len(meshes) == 1:
        return meshes[0]
    off = np.cumsum([0] + [len(m[0]) for m in meshes[:-1]])
    return (np.concatenate([m[0] for m in meshes]), np.concatenate([m[1] for m in meshes]),
            np.concatenate([m[2] + o for m, o in zip(meshes, off)]), np.concatenate([m[3] for m in meshes]),
            meshes[0][4], np.concatenate([m[5] for m in meshes]))


@profile_step('Building mesh assembly')
def build_parts(specs_and_ground, lite=False, max_verts=MAX_MESH_VERTS, with_lod=False):
    """build() split into meshes of at most max_verts vertices each (whole buildings per mesh): a list, [] if empty.
    Each building is built once (build draws them one by one) and the meshes joined. with_lod: each mesh carries the
    lite mesh of its buildings as a 7th element (write: the shape's coarse detail). A building alone past VERTS_MAX
    (the engine's 16-bit indices: its triangles would join far-apart vertices) is left out, counted in over_limit."""
    parts, cur, cur_lite, n = [], [], [], 0
    if not isinstance(specs_and_ground, (list, tuple)):
        specs_and_ground = list(specs_and_ground)
    prepared = None
    if any(item[0].fr_house for item in specs_and_ground) and max_verts == MAX_MESH_VERTS:
        import francehouse_mesh
        prepared = francehouse_mesh.prepared_parts(specs_and_ground, lite, with_lod, max_verts)

    def flush():
        if cur:
            mesh = _concat(cur)
            parts.append(mesh + (_concat(cur_lite) if cur_lite else None,) if with_lod and not lite else mesh)

    for i, item in enumerate(specs_and_ground):
        one = prepared[i][0] if prepared is not None else build([item], lite)
        if one is None:
            continue
        if len(one[0]) > VERTS_MAX:
            over_limit["buildings"] += 1
            continue
        if cur and n + len(one[0]) > max_verts:
            flush()
            cur, cur_lite, n = [], [], 0
        cur.append(one)
        n += len(one[0])
        if with_lod and not lite:
            o = prepared[i][1] if prepared is not None else build([item], lite=True)
            if o is not None:
                cur_lite.append(o)
    flush()
    return parts


# A parking structure you can drive into (the user, 2026-10-14: "garage buildings ... are solid"): open storeys of
# concrete decks on perimeter columns, a parapet round each upper deck, and a two-lane switchback ramp along the
# longest wall (one lane up per storey, a U-turn landing at each end). Only where the ramp fits inside the footprint
# and the ground under it is near level; otherwise the closed box as before. The tile's mesh collides as drawn.
DECK_LANE_M = 3.6           # a ramp lane
DECK_EDGE_M = 0.8           # the ramp lanes start this far in from the wall (the columns stand there)
DECK_LANDING_M = 7.0        # flat deck at each end of the ramps (the U-turn)
DECK_SLOPE = 0.17           # the steepest ramp
DECK_SLAB_M = 0.3
DECK_PARAPET_M = 1.0
DECK_COLUMN_M = 9.0         # perimeter column spacing
DECK_LEVEL_M = 3.5          # ground this uneven (max - min) under the footprint: no deck (up to it: the ground deck
                            # level at the highest ground on a plinth, driven into from the uphill side)
DECK_HEADROOM_M = 2.6       # the lowest storey height that still drives
DECK_STOREY_M = 3.0         # a deck's storey where the mapped floors are too many for the height


DECK_RAMP_EDGES = 4         # the longest edges tried for the ramp, longest first


def _deck_layout(ring, storey):
    """The ramp's frame on the longest edge of the counter-clockwise ring it fits along: (edge index i, A, along
    unit u, inward unit n, s0, s1), the ramps between arc lengths s0 and s1; None when it fits along none of the
    DECK_RAMP_EDGES longest (too short for DECK_SLOPE, or the well outside the ring: the longest edge of an
    L-shaped garage can be the short leg's, 3 of downtown Baltimore's 15 garages were boxes, 2026-10-17)."""
    lens = [float(dnp.hypot(*(ring[(k + 1) % len(ring)] - ring[k]))) for k in range(len(ring))]
    depth = DECK_EDGE_M + 2 * DECK_LANE_M
    for i in sorted(range(len(ring)), key=lambda k: -lens[k])[:DECK_RAMP_EDGES]:
        a, b = ring[i], ring[(i + 1) % len(ring)]
        L = lens[i]
        s0, s1 = 0.5 + DECK_LANDING_M, L - 0.5 - DECK_LANDING_M
        if s1 - s0 < storey / DECK_SLOPE:
            return None                         # the rest are shorter
        u = (b - a) / L
        n = np.array([-u[1], u[0]])             # the inside is on the left
        if all(_inside(ring, a + u * s + n * d) for s in np.linspace(0.5, L - 0.5, 9)
               for d in (0.3, depth / 2, depth + 0.5, depth + DECK_LANDING_M / 2)):
            return i, a, u, n, s0, s1
    return None


def _deck_wall(m, p, q, z0, z1, mat, t=0.2):
    """A thin wall (both faces and a top) from p to q, z0 to z1."""
    d = q - p
    L = float(dnp.hypot(*d))
    if L > 0.05:
        _box(m, (p + q) / 2, d / L, L / 2, t / 2, z0, z1, mat)


def deck_plan(spec, gmin, gmax):
    """(counter-clockwise ring, floors, storey height, _deck_layout) of a parking structure drawn as open decks, or
    None (not a garage, ground too uneven, storeys too low, the ramp does not fit: a closed box)."""
    if not spec.deck or gmax - gmin > DECK_LEVEL_M or not spec.height or (spec.min_h or 0.0) > 0.5:
        return None
    floors = max(1, int(spec.floors or 1))
    storey = spec.height / floors
    if storey < DECK_HEADROOM_M:                # split levels counted as floors (10 in 20.8 m): whole decks
        floors = max(1, int(spec.height // DECK_STOREY_M))
        storey = spec.height / floors
    if storey < DECK_HEADROOM_M:
        return None
    ring = np.asarray(spec.ring, np.float64)
    if _area2(ring) < 0:
        ring = ring[::-1]
    lay = _deck_layout(ring, storey)
    return None if lay is None else (ring, floors, storey, lay)


def _parking_deck(m, spec, gmin, gmax, mat_idx):
    """The open parking structure (see DECK_*); False when it does not apply (the caller draws the box)."""
    plan_ = deck_plan(spec, gmin, gmax)
    if plan_ is None:
        return False
    ring, floors, storey, (i, A, u, n, s0, s1) = plan_
    # weathered concrete (Baltimore's street photos: the white decks read as a skeleton; city garages are grey)
    mat = mat_idx[f"{PANEL}|{len(WALL_TINTS[PANEL]) - 1 - int(_hash01(spec.seed, 'deck') * 2)}"]
    W, e = DECK_LANE_M, DECK_EDGE_M
    depth = e + 2 * W
    zs = [gmax + 0.03 + k * storey for k in range(floors + 1)]

    def at(s, d):
        return A + u * s + n * d

    def uvp(p):
        return (float(p[0]) / 4.0, float(p[1]) / 4.0)

    k_ring = len(ring)
    m.poly([(p[0], p[1], zs[0]) for p in ring], [uvp(p) for p in ring], mat)          # the ground deck, whole
    if gmax - gmin > 0.05:                      # its plinth down to the lowest ground (a sloping street)
        for j in range(k_ring):
            p, q = ring[j], ring[(j + 1) % k_ring]
            m.quad((p[0], p[1], gmin - FOUNDATION_M), (q[0], q[1], gmin - FOUNDATION_M), (q[0], q[1], zs[0]),
                   (p[0], p[1], zs[0]), (0, 0), (1, 0), (1, 0.1), (0, 0.1), mat)
    # the upper decks: the ring with the ramp well (both lanes, s0..s1) cut in from the longest edge
    well = [at(s0, 0.0), at(s0, depth), at(s1, depth), at(s1, 0.0)]
    pts = [A] + well + [ring[(i + 1 + j) % k_ring] for j in range(k_ring - 1)]
    for k in range(1, floors + 1):
        z = zs[k]
        m.poly([(p[0], p[1], z) for p in pts], [uvp(p) for p in pts], mat)
        m.poly([(p[0], p[1], z - DECK_SLAB_M) for p in pts[::-1]], [uvp(p) for p in pts[::-1]], mat)
        for j in range(len(pts)):
            p, q = pts[j], pts[(j + 1) % len(pts)]
            m.quad((p[0], p[1], z - DECK_SLAB_M), (q[0], q[1], z - DECK_SLAB_M), (q[0], q[1], z), (p[0], p[1], z),
                   (0, 0), (1, 0), (1, 0.1), (0, 0.1), mat)
            if not (1 <= j <= 3):               # the parapet, not round the well (below)
                _deck_wall(m, p, q, z, z + DECK_PARAPET_M, mat)
        # round the well: its inner side and the end away from the ramps' landing (ramp k - 1 comes up at s1 when
        # k - 1 is even, and ramp k leaves from that same end); the top deck has no ramp leaving: its lane closed
        land = s1 if (k - 1) % 2 == 0 else s0
        far = s0 if land == s1 else s1
        _deck_wall(m, at(s0, depth), at(s1, depth), z, z + DECK_PARAPET_M, mat)
        _deck_wall(m, at(far, 0.0), at(far, depth), z, z + DECK_PARAPET_M, mat)
        if k == floors:
            d_free = e + (k % 2) * W
            _deck_wall(m, at(land, d_free), at(land, d_free + W), z, z + DECK_PARAPET_M, mat)
    # the ramps: ramp k from deck k to deck k + 1 on lane k % 2, even ones climbing from s0 to s1, walled both sides
    for k in range(floors):
        d0 = e + (k % 2) * W
        sa, sb = (s0, s1) if k % 2 == 0 else (s1, s0)
        za, zb = zs[k], zs[k + 1]
        top = [(at(sa, d0), za), (at(sb, d0), zb), (at(sb, d0 + W), zb), (at(sa, d0 + W), za)]
        if sb < sa:                             # counter-clockwise seen from above
            top = top[::-1]
        quad = [(p[0], p[1], z) for p, z in top]
        m.quad(*quad, (0, 0), (1, 0), (1, 1), (0, 1), mat)
        m.quad(*[(x, y, z - DECK_SLAB_M) for x, y, z in quad[::-1]], (0, 0), (1, 0), (1, 1), (0, 1), mat)
        for d in (d0, d0 + W):
            steps = 6
            for j in range(steps):
                f0, f1 = j / steps, (j + 1) / steps
                ca, cb = at(sa + (sb - sa) * f0, d), at(sa + (sb - sa) * f1, d)
                _deck_wall(m, ca, cb, za + (zb - za) * f0, za + (zb - za) * f1 + DECK_PARAPET_M, mat, 0.15)
    _garage_skin(m, spec, ring, zs, i, mat_idx)
    # the columns: round the outline every DECK_COLUMN_M, from the foundation to the top deck
    for j in range(k_ring):
        p, q = ring[j], ring[(j + 1) % k_ring]
        L = float(dnp.hypot(*(q - p)))
        if L < 0.5:
            continue
        d = (q - p) / L
        inward = np.array([-d[1], d[0]])
        for t in np.arange(0.0, L, DECK_COLUMN_M):
            _box(m, p + d * t + inward * 0.35, d, 0.25, 0.25, zs[0] - FOUNDATION_M, zs[-1], mat)
    return True


# A garage's skin (the user's reports #51/#52, 2026-10-17: "Garages look ugly and basic", "all garages look the
# same"; Canton's on Mapillary: brick or precast concrete spandrels with long openings between, solid corners, a
# stair tower over the roof): per garage (hashed) one of GARAGE_SKINS
GARAGE_SKINS = ("bne_brick_red", "bne_brick_dark", "bne_brick_tan", "bne_precast", "bne_precast_warm")
SPANDREL_H = 1.1            # m of spandrel over each deck (the slab's edge behind it)
SPANDREL_T = 0.3            # m thick, its face just outside the outline
PIER_W = 0.9                # m: the solid corners and every PIER_EVERY_M along the outline
PIER_EVERY_M = 18.0
STAIR_TOWER_M = 4.5         # m square, at the outline's corner farthest from the ramps, ...
STAIR_TOWER_UP = 3.2        # ... this far over the top deck


def _garage_skin(m, spec, ring, zs, ramp_edge, mat_idx):
    """The facade of an open parking structure: spandrels round its outline at every deck above the ground,
    piers at its corners and along it, a stair tower; brick or precast concrete by its seed (GARAGE_SKINS)."""
    mat = mat_idx[GARAGE_SKINS[int(_hash01(spec.seed, "skin") * len(GARAGE_SKINS)) % len(GARAGE_SKINS)]]
    k_ring = len(ring)
    for j in range(k_ring):
        p, q = ring[j], ring[(j + 1) % k_ring]
        L = float(dnp.hypot(*(q - p)))
        if L < 0.5:
            continue
        d = (q - p) / L
        out = np.array([d[1], -d[0]])           # (counter-clockwise ring: the outside on the right)
        c = (p + q) / 2 + out * (SPANDREL_T / 2 - 0.05)
        for z in zs[1:]:
            _box_tiled(m, c, d, L / 2, SPANDREL_T / 2, z - DECK_SLAB_M - 0.1, z + SPANDREL_H, mat, 2.0)
        for t in [0.0] + list(np.arange(PIER_EVERY_M, L - PIER_EVERY_M / 2, PIER_EVERY_M)):
            _box_tiled(m, p + d * t + out * 0.05, d, PIER_W / 2, PIER_W / 2, zs[0] - FOUNDATION_M,
                       zs[-1] + SPANDREL_H + 0.2, mat, 2.0)
    # the stair tower: at the ring's vertex farthest from the ramps' edge
    a, b = ring[ramp_edge], ring[(ramp_edge + 1) % k_ring]
    ab = b - a
    L2 = max(float(dnp.matmul(ab, ab)), 1e-9)

    def dist(v):
        t = float(np.clip((dnp.matmul(v - a, ab)) / L2, 0.0, 1.0))
        return float(dnp.hypot(*(v - (a + t * ab))))
    jv = max(range(k_ring), key=lambda j: dist(ring[j]))
    v = ring[jv]
    inward = ring.mean(0) - v
    inward = inward / max(float(dnp.hypot(*inward)), 1e-9)
    c = v + inward * (STAIR_TOWER_M * 0.75)
    if _inside(ring, c):
        e = ring[(jv + 1) % k_ring] - v
        e = e / max(float(dnp.hypot(*e)), 1e-9)
        _box_tiled(m, c, e, STAIR_TOWER_M / 2, STAIR_TOWER_M / 2, zs[0] - FOUNDATION_M, zs[-1] + STAIR_TOWER_UP,
                   mat, 2.0)


def garage_drives(items, lines, x0, y0):
    """Driveway chunks (road tile chunk dicts, draped by roadmesh) into each open parking structure: from the
    middle of its outline's edge nearest a road out to that road's edge (report #51, 2026-10-17: "No ramp leading
    into the garage")."""
    out = []
    if not lines:
        return out
    segs = []
    for ln in lines:
        pts, half = np.asarray(ln[0], np.float64)[:, :2], ln[1]
        for k in range(len(pts) - 1):
            segs.append((pts[k], pts[k + 1], half))
    if not segs:
        return out
    A = np.array([s[0] for s in segs])
    B = np.array([s[1] for s in segs])
    H = np.array([s[2] for s in segs])
    D = B - A
    L2 = np.maximum((D * D).sum(1), 1e-12)
    for spec, gmin, gmax in items:
        plan_ = deck_plan(spec, gmin, gmax)
        if plan_ is None:
            continue
        ring = plan_[0] + np.array([x0, y0])
        best = None
        for j in range(len(ring)):
            p, q = ring[j], ring[(j + 1) % len(ring)]
            if dnp.hypot(*(q - p)) < 2 * DRIVEWAY_W:
                continue
            mid = (p + q) / 2
            t = np.clip(((mid - A) * D).sum(1) / L2, 0.0, 1.0)
            near = A + t[:, None] * D
            dd = dnp.hypot(*(mid - near).T) - H
            k = int(np.argmin(dd))
            if best is None or dd[k] < best[0]:
                best = (float(dd[k]), mid, near[k], H[k], p, q)
        if best is None or not (0.5 < best[0] < GARAGE_DRIVE_MAX_M):
            continue
        _, mid, qn, half, p, q = best
        d = (q - p) / max(float(dnp.hypot(*(q - p))), 1e-9)
        inside = np.array([-d[1], d[0]])
        v = mid - qn
        dl = float(dnp.hypot(*v))
        p0 = mid + inside * 3.0             # a few metres in, onto the ground deck
        p1 = qn + v / max(dl, 1e-9) * (half + 0.1)
        n = max(2, int(dnp.hypot(*(p1 - p0)) // 4.0) + 1)
        pts = [p0 + (p1 - p0) * (j / (n - 1)) for j in range(n)]
        out.append({"m": DRIVEWAY["m"], "w": GARAGE_DRIVE_W, "tl": DRIVEWAY["tl"], "p": DRIVEWAY["p"],
                    "n": [[round(float(pp[0]), 3), round(float(pp[1]), 3), 0.0] for pp in pts]})
    return out


GARAGE_DRIVE_W = 6.5        # m: a garage entrance's two lanes
GARAGE_DRIVE_MAX_M = 25.0   # m from the outline to a road's edge at most


def build(specs_and_ground, lite=False):
    """The tile's mesh from [(Spec, ground z min, ground z max)] with rings in the tile's local frame:
    (positions, uvs, tris, tri material indices, MATERIALS) or None if empty. lite (far tiles): no
    cornices, whole walls instead of one quad per bay."""
    m = Mesh()
    for spec, gmin, gmax in specs_and_ground:
        if spec.cls == "toll_booth":        # a stock prop (toll_booth_rows)
            continue
        if spec.cls == landmarks.LANDMARK:
            landmarks.build(m, spec, gmin, _landmark_token(spec), lite, _landmark_tokens())
            continue
        mat_idx = _Looks(spec.seed)
        if spec.fr_house:
            import francehouse_mesh
            francehouse_mesh.draw(m, spec, gmin, gmax, mat_idx, lite)
            continue
        if spec.cls in BRIDGE_CLASSES:
            _steel_frame(m, spec, gmin, mat_idx[spec.facade if spec.facade in LOOKS else STEEL], lite)
            continue
        if spec.cls == "ship":
            ships.draw(m, spec, gmin, gmax, mat_idx, lite)
            continue
        if spec.cls in CANOPY_CLASSES and not spec.deck and (spec.min_h or 0.0) <= 0.5:
            _canopy(m, spec, gmin, gmax, mat_idx[spec.roof_mat], mat_idx[spec.facade], lite)
            continue
        ring = drawn_ring(spec)
        mh = spec.min_h or 0.0
        if spec.deck and not lite and _parking_deck(m, spec, gmin, gmax, mat_idx):
            continue
        z0 = gmin - (SKIRT_LITE_M if lite else SKIRT_M) if mh <= 0.5 else base_z(spec, gmin, gmax) + mh
        z_eave = base_z(spec, gmin, gmax) + mh + spec.height
        _walls(m, spec, ring, z0, z_eave, mat_idx, lite)
        for hole in (spec.holes or ()):               # (a courtyard's walls, facing the yard: the hole clockwise)
            _walls(m, spec, hole, z0, z_eave, mat_idx, lite, hole=True)
        if mh > 0.5:                                  # a part standing on another / over a street: its underside
            m.poly([(p[0], p[1], z0) for p in ring[::-1]], [(0.002, 0.995)] * len(ring), mat_idx[spec.facade])
        _roof(m, spec, ring, z_eave, mat_idx, lite)
        if spec.roof == "flat" and not spec.plain and spec.floors <= 6 and not lite:
            _cornice(m, spec, ring, z_eave, mat_idx)
        if not lite and not spec.holes:               # (roof clutter scattered over a yard would float)
            _clutter(m, spec, ring, z_eave, mat_idx)
        if spec.steeple:
            _steeple(m, spec, z_eave, mat_idx)
        if spec.stoop and not lite:
            _stoop(m, spec, ring, gmin, gmax, mat_idx)
        if spec.porch and spec.door and not lite:
            _porch(m, spec, gmax, mat_idx)
        if spec.sign is not None and spec.roof == "flat" and not lite:
            if spec.windows is False:           # a big box: over its storefront band (_plain_wall_with_windows)
                z_top = min(gmax + GROUND_FLOOR_SHOP_M + 1.15, z_eave)
            else:
                z_top = z_eave - (spec.floors - 1) * spec.floor_h if spec.floors > 1 else z_eave
            _signs(m, spec, ring, z_top, mat_idx)
    if m.n == 0:
        return None
    P, UV, T, M, C = m.arrays()
    import shells       # each building a closed solid, its bottom sunk (shells.close; the user, 2026-11-07), here so
    if shells.ON:
        P, T, M, (UV, C) = shells.close(P, T, M, (UV, C), skirt=False)  # (build_parts' vertex budget counts the closed
                        # mesh; the walls already reach SKIRT_M under the ground: the bottom capped there)
    return P, UV, T, M, MATERIALS, C


def _steel_frame(m, spec, gmin, mat, lite=False, legs_only=False):
    """A bridge structure (BRIDGE_CLASSES) as an open steel frame on its oriented box: a leg at each corner, bracing
    along the short sides every STEEL_BAY_M (beside the decks running along them), cross beams over the long sides
    only near the top (STEEL_TOP_SHARE, over any deck); a narrow footprint is one pier. lite: legs and top only."""
    c, u, hl, hw = oriented_box(np.asarray(spec.ring, np.float64))
    v = np.array([-u[1], u[0]])
    z0, z1 = gmin - FOUNDATION_M, gmin + spec.height
    if hl < 6.0:                                            # a pier
        _box(m, c, u, hl, hw, z0, z1, mat)
        return
    if hw < 3.0:    # a thin wall across the decks (the Queensboro Bridge's towers, 33 x 4.6 m): a leg at each end
        t = float(min(hw, STEEL_LEG_MAX))
        for sx in (-1, 1):
            _box(m, c + u * sx * (hl - t), u, t, hw, z0, z1, mat)
        if not legs_only:
            b = t * 0.6
            top = gmin + STEEL_TOP_SHARE * spec.height
            for z in [z1 - b] + ([] if lite else list(np.arange(z1 - b - STEEL_BAY_M / 2, top, -STEEL_BAY_M / 2))):
                _box(m, c, u, hl - 2 * t, b, z - b, z + b, mat)
        return
    t = float(np.clip(hw * 0.5, 0.5, STEEL_LEG_MAX))        # leg half size
    corners = [c + u * sx * (hl - t) + v * sy * (hw - t) for sx in (-1, 1) for sy in (-1, 1)]
    for k in corners:
        _box(m, k, u, t, t, z0, z1, mat)
    if legs_only:
        return
    b = t * 0.6                                             # beam half size
    levels = [z1 - b] if lite else list(np.arange(gmin + STEEL_BAY_M, z1 - b, STEEL_BAY_M)) + [z1 - b]
    for z in levels:
        for sx in (-1, 1):          # along the short sides (along v): beside a deck running through the frame
            _box(m, c + u * sx * (hl - t), v, hw - 2 * t, b, z - b, z + b, mat)
        if z >= gmin + STEEL_TOP_SHARE * spec.height:       # across the top only
            for sy in (-1, 1):
                _box(m, c + v * sy * (hw - t), u, hl - 2 * t, b, z - b, z + b, mat)


def collision_mesh(specs_and_ground):
    """The tile's buildings for physics only: one quad per wall and a flat lid at the eaves (pitched roofs on their
    oriented box), parking decks as drawn; (positions, uvs, tris, tri materials, MATERIALS, colours) or None.
    The drawn mesh (windows a quad per bay and floor, cornices, signs) cost the game's collision rebuild ~30 ms of
    a 40 ms rebuild in Fells Point (2026-10-15), every few seconds while driving; this one is ~1/20 of its triangles."""
    m = Mesh()
    tok = (MATERIALS.index(PANEL), (128, 128, 128, 255))
    zero = (0.0, 0.0)
    for spec, gmin, gmax in specs_and_ground:
        if spec.fr_house:
            import francehouse_mesh
            francehouse_mesh.draw(m, spec, gmin, gmax, _Looks(spec.seed), collision=True)
            continue
        if spec.cls in BRIDGE_CLASSES:          # its legs (the decks run between them)
            _steel_frame(m, spec, gmin, tok, True, legs_only=True)
            continue
        if spec.cls == landmarks.LANDMARK:      # (its legs and decks: walked and driven under)
            landmarks.collision(m, spec, gmin, tok)
            continue
        if spec.cls in CANOPY_CLASSES and not spec.deck and (spec.min_h or 0.0) <= 0.5:   # (driven under)
            _canopy(m, spec, gmin, gmax, tok, tok)
            continue
        if deck_plan(spec, gmin, gmax) is not None:
            _parking_deck(m, spec, gmin, gmax, _Looks(spec.seed))
            continue
        ring = np.asarray(spec.ring, np.float64)
        if spec.roof != "flat" and spec.roof not in DOME_ROOFS and not getattr(spec, "hf", False):
            # (a dome's drum: its own round outline, as drawn; a heightfield roof's walls: the outline)
            c, u, hl, hw = oriented_box(ring)
            v = np.array([-u[1], u[0]])
            ring = np.array([c - u * hl - v * hw, c + u * hl - v * hw, c + u * hl + v * hw, c - u * hl + v * hw])
        if _area2(ring) < 0:
            ring = ring[::-1]
        mh = spec.min_h or 0.0
        z0 = gmin - FOUNDATION_M if mh <= 0.5 else base_z(spec, gmin, gmax) + mh
        z1 = base_z(spec, gmin, gmax) + mh + (spec.floor_h if spec.cls == "ship" else spec.height)  # (a ship: its deck)
        for loop in [ring] + list(spec.holes or ()):  # (a courtyard's walls too: its hole clockwise, facing the yard)
            for i in range(len(loop)):
                a, b = loop[i], loop[(i + 1) % len(loop)]
                m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1), zero, zero, zero, zero,
                       tok)
        if spec.holes:
            _holed_lid(m, ring, spec.holes, z1, tok, uv_on=False)
        else:
            m.poly([(p[0], p[1], z1) for p in ring], [zero] * len(ring), tok)
        if mh > 0.5:                # over a street: its underside
            m.poly([(p[0], p[1], z0) for p in ring[::-1]], [zero] * len(ring), tok)
    if m.n == 0:
        return None
    P, UV, T, M, C = m.arrays()
    import shells       # each building a closed solid, its bottom sunk (shells.close; the user, 2026-11-07), here so
    if shells.ON:
        P, T, M, (UV, C) = shells.close(P, T, M, (UV, C), skirt=False)  # (build_parts' vertex budget counts the closed
                        # mesh; the walls already reach SKIRT_M under the ground: the bottom capped there)
    return P, UV, T, M, MATERIALS, C


# ------------------------------------------------------------------------------------------- a tile

def _shared_walls(rings, tol=0.6):
    """Per ring, the number of other rings with a vertex within tol of one of its edges (row houses)."""
    n = len(rings)
    cnt = np.zeros(n, np.int64)
    if n < 2:
        return cnt
    cents = np.array([r.mean(0) for r in rings])
    rads = np.array([dnp.hypot(*(r - r.mean(0)).T).max() for r in rings])
    order = np.argsort(cents[:, 0])
    fast.require(1, "shared_walls")
    offs = np.zeros(n + 1, np.int64)
    offs[1:] = np.cumsum([len(r) for r in rings])
    coords = np.ascontiguousarray(np.concatenate(rings)[:, :2], np.float64)
    return np.asarray(fast._ef.shared_walls(coords, offs, np.ascontiguousarray(cents, np.float64),
                                            np.ascontiguousarray(rads, np.float64), order.astype(np.int64),
                                            float(tol)), np.int64)

def _part_heights(a, i):
    """A building part's (own height, its bottom) from its attributes, or None without a height."""
    top = a["height"][i] if "height" in a else np.nan
    mh = a["min_height"][i] if "min_height" in a else np.nan
    mh = float(mh) if np.isfinite(mh) and mh > 0 else 0.0
    if not np.isfinite(top) or top <= mh + PART_MIN_THICK_M:
        nf, f0 = (a[k][i] if k in a else np.nan for k in ("num_floors", "min_floor"))
        if np.isfinite(nf) and nf > 0:
            f0 = float(f0) if np.isfinite(f0) and f0 > 0 else 0.0
            mh = mh or f0 * FLOOR_M
            return max(FLOOR_M, (float(nf) - f0) * FLOOR_M), mh
        return None
    return float(top) - mh, mh


ROAD_THROUGH_M = 15.0       # a footprint that public roads run through this far is a structure over them
OVER_ROAD_CLEAR_M = 5.5     # ... lifted this high (an expressway's or a station's slab over the streets: Baltimore's
                            # 17,500 m2 one by I-83, the AI drove into its wall at 35 mph, 2026-10-14)
LEVEL_THROUGH_M = 5.0       # ... this far for one mapped over the ground (OSM layer >= 1, Overture's level): Noranda Station's
                            # concourse over Tonkin Highway in Perth (layer 1, no min_height), 80 x 118 m over the platforms
                            # in the median and 6 m of one carriageway, stood as a wall across its lanes (GitHub #914)
SITE_M2 = 100_000.0         # m2: a footprint this large that public roads run through (ROAD_THROUGH_M) is a mapped site
                            # (a planned development, a campus outline tagged building), not a structure over them: left
                            # out, not lifted (the largest decks over roads are a few hectares; OSM's 1.35 km2 "Blueberry
                            # Commons" by I-94 near Huntley MT, its streets inside, stood 5.5 m up as a 15 m wall over
                            # empty prairie, Tann2019 2026-11-05)
ACROSS_W = 8.0              # m: a footprint at most this wide that a public road crosses (its long middle line) is a
                            # walkway or canopy over it (GitHub #48: the 69th Street station's 3 m shelters stood across
                            # the Dan Ryan as brick walls; a road crossing one runs only its width inside it)
THROUGH_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified",
                   "living_street"}


def tunnel_lines(frame, road_layers, x0, y0, margin=50.0, size=256.0):
    """Polylines (k, 2) relative to (x0, y0) of the road tunnels near the tile (roads.tunnel_parts; for
    road_through: a building a tunnel runs into is its portal building, its floor over the tube), but those of
    PORTAL_SKIP_CLASSES. A RoadLines: their half widths in .half (an open one's passage, _open_tunnels). A road in two
    cells is taken once."""
    import roads as roadsmod
    out, seen = RoadLines(), set()
    for f in road_layers:
        if f is None or len(f.kind) == 0:
            continue
        ts = roadsmod.tunnel_specs(f)
        for i in range(len(f.kind)):
            if ts[i] is None or ts[i].cls in PORTAL_SKIP_CLASSES or str(f.ids[i]) in seen:
                continue
            seen.add(str(f.ids[i]))
            w = float(ts[i].width) if ts[i].width is not None and np.isfinite(ts[i].width) else ROAD_PASSAGE_W
            for ring in roadsmod.tunnel_parts(f, i):
                e, n = mapgeom.enu_xy(f, frame, ring)
                xy = np.stack([e - x0, n - y0], 1)
                if xy[:, 0].max() < -margin or xy[:, 0].min() > size + margin or xy[:, 1].max() < -margin \
                        or xy[:, 1].min() > size + margin:
                    continue
                out.append(xy)
                out.half.append(w / 2.0)
    return out


GARAGE_TAGS = ("multi-storey", "multistorey", "multi_storey", "multi-level")
GARAGE_TAG_CLASSES = {"", "yes", "commercial", "garage", "garages", "parking", "parking_garage", "carport",
                      "service", "transportation", "industrial", "retail"}     # (not the apartments on a car park podium)


def garage_rings(frame, infra_layers, x0, y0, margin=50.0, size=256.0):
    """Outer rings (k, 2) relative to (x0, y0) of the mapped multi-storey car parks near the tile (Overture
    infrastructure, class parking, OSM parking=multi-storey). OSM often keeps the car park's tags on its own area
    and the building beside it as building=yes or =garage (a shed), so the class alone missed 25 of downtown
    Baltimore's 40 garages (2026-10-17, the user's 'parking garages are solid boxes')."""
    import json
    out, seen = [], set()
    for f in infra_layers:
        if f is None or len(f.kind) == 0:
            continue
        a = f.attrs
        cls, tags = a.get("class"), a.get("tags")
        if cls is None or tags is None:
            continue
        for i in np.flatnonzero((cls.astype(str) == "parking") & (f.kind == 3)):
            t = str(tags[i])
            if '"parking"' not in t:
                continue
            try:
                if str(json.loads(t).get("parking", "")).lower() not in GARAGE_TAGS:
                    continue
            except (ValueError, AttributeError):
                continue
            fid = f.ids[i]
            if fid in seen:
                continue
            seen.add(fid)
            for part in f.parts(i):
                if not part or len(part[0]) < 3:
                    continue
                e, n = mapgeom.enu_xy(f, frame, part[0])
                xy = np.stack([e - x0, n - y0], 1)
                if xy[:, 0].max() < -margin or xy[:, 0].min() > size + margin or xy[:, 1].max() < -margin \
                        or xy[:, 1].min() > size + margin:
                    continue
                out.append(xy)
    return out


def support_rings(frame, infra_layers, x0, y0, margin=50.0, size=256.0):
    """[(ring (k, 2) relative to (x0, y0), paint)] of the mapped bridge supports near the tile (Overture
    infrastructure, class bridge_support: OSM bridge:support towers and piers, their building:colour as a
    bridgetypes paint, grey without one). The Golden Gate Bridge's towers are mapped as building parts over them and
    were drawn as a 225 m glass tower (2026-10-18)."""
    import json
    import bridgetypes
    out, seen = [], set()
    for f in infra_layers:
        if f is None or len(f.kind) == 0:
            continue
        a = f.attrs
        cls, tags = a.get("class"), a.get("tags")
        if cls is None:
            continue
        cs = cls.astype(str)
        # a bridge's supports, and its own outlines mapped as buildings (Tower Bridge's high walkways: building:colour
        # lightblue, 45 m); not a bare bridge outline (Ponte Vecchio's shops stand on one)
        three_d = f.__dict__.get("_three_d")         # (once per cell's features: every tile of it asked)
        if three_d is None:
            three_d = f._three_d = (np.array(['"building' in str(t) for t in tags], bool) if tags is not None
                                    else np.zeros(len(cs), bool))
        for i in np.flatnonzero(((cs == "bridge_support") | ((cs == "bridge") & three_d)) & (f.kind == 3)):
            fid = f.ids[i]
            if fid in seen:
                continue
            seen.add(fid)
            pt = None
            if tags is not None and "colour" in str(tags[i]):
                try:
                    td = json.loads(str(tags[i]))
                    pt = bridgetypes.paint(td.get("building:colour") or td.get("colour"))
                except ValueError:
                    pt = None
            for part in f.parts(i):
                if not part or len(part[0]) < 3:
                    continue
                e, n = mapgeom.enu_xy(f, frame, part[0])
                xy = np.stack([e - x0, n - y0], 1)
                if xy[:, 0].max() < -margin or xy[:, 0].min() > size + margin or xy[:, 1].max() < -margin \
                        or xy[:, 1].min() > size + margin:
                    continue
                out.append((xy, pt or "grey"))
    return out


# Storage tanks and silos mapped as infrastructure (Overture utility / storage_tank: OSM man_made=storage_tank / silo
# areas; 896,945 storage tanks and 371,676 silos in OSM, most not tagged building=*): none were drawn, a refinery's tank
# farm was bare ground (Pernis, Rotterdam: 77 in one map cell). Each one, as a tank (plan's TANK_CLASSES rule: round
# ones painted steel under a shallow dome), or the building mapped over it given its class (tile_buildings).
TANK_INFRA = {"storage_tank": "storage_tank", "silo": "silo", "gasometer": "gasometer", "digester": "digester",
              "slurry_tank": "slurry_tank"}


def industrial_rings(frame, land_layers, x0, y0, margin=50.0, size=256.0):
    """[ring (k, 2) relative to (x0, y0)] of the industrial, port and depot land (INDUSTRIAL_LAND) reaching the tile."""
    out, seen = [], set()
    for f in land_layers:
        if f is None or len(f.kind) == 0 or f.attrs.get("class") is None:
            continue
        cs = f.attrs["class"].astype(str)
        for i in np.flatnonzero(np.isin(cs, list(INDUSTRIAL_LAND)) & (f.kind == 3)):
            fid = f.ids[i]
            if fid in seen:
                continue
            seen.add(fid)
            for part in f.parts(i):
                if not part or len(part[0]) < 3:
                    continue
                e, n = mapgeom.enu_xy(f, frame, part[0])
                xy = np.stack([e - x0, n - y0], 1)
                if xy[:, 0].max() < -margin or xy[:, 0].min() > size + margin or xy[:, 1].max() < -margin \
                        or xy[:, 1].min() > size + margin:
                    continue
                out.append(xy)
    return out


def tank_rings(frame, infra_layers, x0, y0, margin=0.0, size=256.0):
    """[(ring (k, 2) relative to (x0, y0), attrs {class, height?})] of the mapped tanks and silos (TANK_INFRA) whose
    outline reaches the tile."""
    import json
    out, seen = [], set()
    for f in infra_layers:
        if f is None or len(f.kind) == 0:
            continue
        a = f.attrs
        cls, tags = a.get("class"), a.get("tags")
        if cls is None:
            continue
        cs = cls.astype(str)
        for i in np.flatnonzero(np.isin(cs, list(TANK_INFRA)) & (f.kind == 3)):
            fid = f.ids[i]
            if fid in seen:
                continue
            seen.add(fid)
            at = {"class": TANK_INFRA[cs[i]]}
            if tags is not None and tags[i]:
                try:
                    td = json.loads(str(tags[i]))
                except ValueError:
                    td = {}
                if isinstance(td, dict):
                    if td.get("man_made") == "silo" or td.get("silo"):
                        at["class"] = "silo"
                    for key in ("height", "est_height"):
                        try:
                            hv = float(str(td.get(key, "")).replace("m", "").strip())
                        except ValueError:
                            continue
                        if 1.0 <= hv <= 120.0:
                            at["height"] = hv
                            break
            for part in f.parts(i):
                if not part or len(part[0]) < 8:
                    continue
                e, n = mapgeom.enu_xy(f, frame, part[0])
                xy = np.stack([e - x0, n - y0], 1)
                if xy[:, 0].max() < -margin or xy[:, 0].min() > size + margin or xy[:, 1].max() < -margin \
                        or xy[:, 1].min() > size + margin:
                    continue
                out.append((xy, at))
    return out


# A wind turbine's base mapped as a building (Herzogenrath's R90004, a 10 m round building=yes round the tower: a
# building stood in the turbine, GitHub #1085): a footprint of at most TURBINE_BASE_M2 round a turbine the world draws
# (windturbines.keep's rules, by its mapped size) is left out. An unsized turbine's only when the footprint is round
# (TURBINE_BASE_ROUND: a rooftop unit's house stays).
TURBINE_BASE_ON = os.environ.get("BNE_TURBINE_BASE", "on") != "off"
TURBINE_BASE_M2 = 400.0
TURBINE_BASE_ROUND = 0.8
TURBINE_REACH_M = 30.0      # (the turbines looked up round the tile)


def turbine_points(frame, x0, y0, x1, y1):
    """[(x, y, sized)] of the mapped turbines drawn at full size in (and TURBINE_REACH_M round) the box, anchor frame
    (windturbines.near; not the small ones)."""
    if not TURBINE_BASE_ON:
        return []
    try:
        import geotiles
        import windturbines
        m = TURBINE_REACH_M
        la, lo, _ = geotiles.world_to_geodetic(frame, [[x0 - m, y0 - m, 0], [x1 + m, y0 - m, 0],
                                                      [x0 - m, y1 + m, 0], [x1 + m, y1 + m, 0]])
        lo = frame.lon0 + (np.asarray(lo) - frame.lon0 + 180) % 360 - 180
        ts = [t for t in windturbines.near(float(np.min(la)), float((np.min(lo) + 180) % 360 - 180),
                                           float(np.max(la)), float((np.max(lo) + 180) % 360 - 180))
              if not windturbines.small(t.tags)]
        if not ts:
            return []
        x, y, _ = geotiles.geodetic_to_world(frame, np.array([t.lat for t in ts]), np.array([t.lon for t in ts]), 0.0)
        return [(float(a), float(b), windturbines.sized(t.tags)) for a, b, t in zip(x, y, ts)]
    except Exception as ex:     # (no index: no turbines, the buildings as before)
        import logging
        logging.getLogger("earthd.buildings").debug("turbine bases: %s: %s", type(ex).__name__, ex)
        return []


def turbine_base(e, n, turbines):
    """Is the footprint (e, n: its closed outer ring, anchor frame) a drawn turbine's base (TURBINE_BASE_*)?"""
    if not turbines:
        return False
    ring = np.stack([e, n], 1)
    area = abs(_area2(ring[:-1])) / 2.0
    if area > TURBINE_BASE_M2:
        return False
    path = None
    for x, y, sized in turbines:
        if not (e.min() <= x <= e.max() and n.min() <= y <= n.max()):
            continue
        if path is None:
            from polygon import Path
            path = Path(ring)
        if path.contains_points(np.array([[x, y]]))[0] and (sized or circularity(ring[:-1]) >= TURBINE_BASE_ROUND):
            return True
    return False


def _on_support(ring, supports):
    """The paint of the bridge support (support_rings) holding the footprint's centre, else None."""
    if not supports:
        return None
    from polygon import Path
    c = ring.mean(0)
    for r, pt in supports:
        if r[:, 0].min() <= c[0] <= r[:, 0].max() and r[:, 1].min() <= c[1] <= r[:, 1].max() \
                and Path(r).contains_points(c[None])[0]:
            return pt
    return None


def _covers_garage(ring, garages):
    """A footprint inside a mapped car park's area (its centre in it) or holding the car park's centre."""
    c = ring.mean(0)
    for g in garages:
        if _inside(g, c) or _inside(ring, g.mean(0)):
            return True
    return False


TUNNEL_THROUGH_M = 5.0      # a footprint a road tunnel runs into this far: its portal building, lifted over the tube
PORTAL_SKIP_CLASSES = ("service",)     # tunnels that lift no building: a driveway, loading dock or covered drop-off
                            # under a building, which stands on the ground (GitHub #60: a 15 m drive-through under an
                            # Oklahoma City church floated it 10 m up; in downtown Baltimore and Chicago 20 office and
                            # garage blocks of up to 7,156 m2 stood 7.4-10.5 m up over loading-dock tunnels, 2026-10-20)
OVER_TUNNEL_CLEAR_M = 7.5   # (the tube's vault is roadmesh.TUNNEL_H = 6.8 m over the floor; 2026-10-17: Eisenhower's
                            # east portal building closed the tunnel)


class RoadLines(list):
    """road_lines' polylines, a list as before, with .half: each line's drawn half-width (m; roads.road_specs' rural
    width / 2, None where it has no spec): _ml_false's on-the-lanes test."""
    def __init__(self, *a):
        super().__init__(*a)
        self.half = []



def road_lines(frame, road_layers, x0, y0, margin=50.0, size=256.0, classes=THROUGH_CLASSES, skip_sub=()):
    """Polylines (k, 2) relative to (x0, y0) of the public roads on the ground near the tile (for road_through), a
    RoadLines (their drawn half-widths in .half). classes: the road classes taken; skip_sub: the OSM subclasses left
    out (a car park's aisles)."""
    import roads as roadsmod
    out = RoadLines()
    for f in road_layers:
        if f is None or len(f.kind) == 0:
            continue
        cls = f.attrs.get("class")
        if cls is None:
            continue
        sub = f.attrs.get("subclass") if skip_sub else None
        bb = f.boxes(frame)             # (only the roads near the tile: the test below, all at once)
        near = np.flatnonzero((bb[:, 2] - x0 >= -margin) & (bb[:, 0] - x0 <= size + margin)
                              & (bb[:, 3] - y0 >= -margin) & (bb[:, 1] - y0 <= size + margin))
        specs = None
        for i in near.tolist():
            if str(cls[i]) not in classes or (sub is not None and str(sub[i]) in skip_sub):
                continue
            if specs is None:
                specs = roadsmod.road_specs(f)      # (remembered on the layer: the road tiles ask for them anyway)
            sp = specs[i] if i < len(specs) else None
            half = float(sp.width) / 2 if sp is not None and np.isfinite(sp.width) and sp.width > 0 else None
            if roadsmod.level_ranges(f, i):
                # its pieces on the ground (a motorway segment with a short overpass in it was left out whole, and the
                # lorries machine-learned as buildings on its lanes stayed: the A7, GitHub #239; PR #71's finding)
                lls = [np.asarray(p, np.float64) for p in roadsmod.ground_parts(f, i)]
            else:
                a = f.coord_off[f.ring_off[f.part_off[i]]]
                b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
                lls = [f.lonlat[a:b]]
            for ll in lls:
                if len(ll) < 2:
                    continue
                e, n = mapgeom.enu_xy(f, frame, ll)
                xy = np.stack([e - x0, n - y0], 1)
                if xy[:, 0].max() < -margin or xy[:, 0].min() > size + margin or xy[:, 1].max() < -margin \
                        or xy[:, 1].min() > size + margin:
                    continue
                out.append(xy)
                out.half.append(half)
    return out


RAIL_BED_CLEAR_M = 3.2     # m from a track's axis a footprint may not reach (the ballast's foot ~2.3 m): BNE_RAIL_XING; a hut
                           # stood with its wall on the ballast and the rails ran into it (Kalynivka, 2026-11-05)


def rail_lines(frame, road_layers, x0, y0, margin=50.0, size=256.0):
    """Polylines (k, 2) relative to (x0, y0) of the railway tracks on the ground near the tile (railways.drawn's, the
    ground pieces); none unless BNE_RAIL_XING is on."""
    import railxing
    if not railxing.ENABLED:
        return []
    import railways
    import roads as roadsmod
    out = []
    for f in road_layers:
        if f is None or len(f.kind) == 0 or "subtype" not in f.attrs:
            continue
        bb = f.boxes(frame)
        near = np.flatnonzero((bb[:, 2] - x0 >= -margin) & (bb[:, 0] - x0 <= size + margin)
                              & (bb[:, 3] - y0 >= -margin) & (bb[:, 1] - y0 <= size + margin))
        for i in near.tolist():
            got = railways.drawn(f, i)
            if got is None or got[0] == "tram":
                continue
            for ll in roadsmod.ground_parts(f, i):
                if len(ll) < 2:
                    continue
                e, n = mapgeom.enu_xy(f, frame, np.asarray(ll, np.float64))
                out.append(np.stack([e - x0, n - y0], 1))
    return out


def on_rail_bed(ring, rails_xy, step=1.0):
    """Whether footprint ring (k, 2) reaches RAIL_BED_CLEAR_M of a track: its outline sampled every step m, and its
    centre."""
    if not rails_xy:
        return False
    pts = [ring.mean(0)[None]]
    for a, b in zip(ring, np.roll(ring, -1, axis=0)):
        n = max(1, int(np.ceil(float(dnp.hypot(*(b - a))) / step)))
        pts.append(a + (b - a) * (np.arange(n) / n)[:, None])
    allp = np.vstack(pts)
    for ln in rails_xy:
        a, b = ln[:-1], ln[1:]
        d = b - a
        t = np.clip(((allp[:, None, :] - a[None]) * d[None]).sum(2) / np.maximum((d * d).sum(1), 1e-12)[None], 0.0, 1.0)
        q = a[None] + t[:, :, None] * d[None]
        if float(dnp.hypot(*(q - allp[:, None, :]).transpose(2, 0, 1)).min()) <= RAIL_BED_CLEAR_M:
            return True
    return False


PASSAGE_W = 11.0           # m: the passage a bridge's road takes through its tower (Tower Bridge: 2 lanes + walks) ...
PASSAGE_H = 7.0            # ... this high over the tower's mapped bottom (its deck: the part starts at the road), ...
PASSAGE_CLEAR = 6.5        # m: the way over a deck through a building (with the decks' heights)
PASSAGE_STACK_M = 15.0     # m: decks through a building this far over or under the nearest one share its way (a
                            # double-deck bridge: the Bay Bridge's lower deck ran into its towers' solid foot under
                            # the upper one's way, Discord 2026-11-10)
PASSAGE_BOTTOM_M = 20.0    # ... in a structure whose bottom is this low (not its high walkways or tops)
PASSAGE_UNDER_M = 0.2      # m: a part whose top is this far under the deck is under it (whole); up to 1 m over it was
                           # too (the Golden Gate's roadway slab mapped in its north tower, 73-74.65 m, stood ~1 m over the
                           # deck across all six lanes, GitHub #1032)
PASSAGE_MARGIN_M = 1.5     # m: with the decks' widths the way is every deck through the footprint and this either side
                           # (its parapets and walks): the Golden Gate Bridge's six lanes, ~20 m, kept a strut of its
                           # north tower over their outer lanes through PASSAGE_W (GitHub #1032)


def _clip_half(ring, p0, nrm, keep_ge):
    """The part of the ring (k, 2) on one side of the line through p0 with normal nrm (Sutherland-Hodgman)."""
    out = []
    pts = list(ring[:-1]) if np.allclose(ring[0], ring[-1]) else list(ring)
    side = [float(dnp.matmul(q - p0, nrm)) for q in pts]
    for k in range(len(pts)):
        a, b = pts[k], pts[(k + 1) % len(pts)]
        sa, sb = side[k], side[(k + 1) % len(pts)]
        ina, inb = (sa >= 0) == keep_ge or sa == 0, (sb >= 0) == keep_ge or sb == 0
        if ina:
            out.append(a)
        if (sa > 0) != (sb > 0) and sa != 0 and sb != 0:
            t = sa / (sa - sb)
            out.append(a + t * (b - a))
    if len(out) < 3:
        return None
    r = np.array(out + [out[0]], np.float64)
    return r if _area2(r) / 2 >= 2.0 else None


DECK_STAND_M = 2.0      # m: a deck this far over the ground under a footprint is what the building stands on ...
DECK_LANE_M = 1.0       # ... and it is left out where it reaches this far inside the deck's road (its width / 2) ...
DECK_REACH_M = 6.0      # m past the road's edge a footprint's centre still counts as on its deck
DECK_SEA_M = 0.5        # ... only over ground this low: water, a beach (a building on dry land under a viaduct stays)


def on_deck(ring, decks, ground):
    """(over the lanes, deck height) when footprint ring (k, 2) has its centre on a deck (decks: [(xy (k, 2), z (k,),
    width m)], the same frame) standing DECK_STAND_M or more over ground (m, its highest ground, DECK_SEA_M at most:
    over water), else None. A building
    on the old Sunshine Skyway span kept as a fishing pier (its bait shop) was raised from the seabed: its top 0.5 m
    stood over the deck (2026-10-27). Over the lanes: a corner more than DECK_LANE_M inside the road's edge. Pure."""
    r = np.asarray(ring, np.float64)[:, :2]
    c = r.mean(0)
    best = None
    for d in decks:
        if len(d) < 3 or len(d[0]) < 2:
            continue
        xy, z, w = np.asarray(d[0], np.float64), np.asarray(d[1], np.float64), float(d[2])
        a, b = xy[:-1], xy[1:]
        ab = b - a
        t = np.clip(((c - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
        dist = dnp.hypot(*(a + t[:, None] * ab - c).T)
        k = int(np.argmin(dist))
        half = w / 2 + DECK_REACH_M         # (a pier's map width is its lane: its deck reaches past it)
        if dist[k] > half:
            continue
        zd = float(z[k] + t[k] * (z[k + 1] - z[k]))
        if zd - ground < DECK_STAND_M:
            continue
        if best is None or dist[k] < best[0]:
            best = (dist[k], zd, a[k], ab[k] / max(float(dnp.hypot(*ab[k])), 1e-9), w)
    if best is None or ground > DECK_SEA_M:
        return None
    _, zd, p, u, w = best
    off = dnp.matmul(r - p, np.array([-u[1], u[0]]))            # (signed: a footprint across the lanes has corners both sides)
    inner = w / 2 - DECK_LANE_M
    return bool(off.min() < inner and off.max() > -inner), zd


SPLIT_MIN_L = 100.0         # m: a flat-roofed footprint at least this long ...
SPLIT_MAX_W = 30.0          # ... and at most this wide ...
SPLIT_DZ = 3.0              # ... over ground varying more than this along it, or ...
SPLIT_ALWAYS_L = 256.0      # ... at least this long (a road tile's side: its far end in other tiles) stands in pieces
PIECE_LEN_M = 25.0          # m: its pieces this long at most, cut at the same places by every tile (ground_pieces) and
                            # each drawn by the tile that owns its centre (tile_buildings): a 25 m x 30 m piece reaches
                            # < 20 m past its tile, within the decks' (blockworker._tile_decks) and lines' margins
PIECE_STEP_M = 5.0          # m between the ground's readings along its middle line


def ground_pieces(frame, ring, x0, y0, elev):
    """(pieces, drop m): a long, narrow footprint ring (k, 2) (relative to (x0, y0)) cut across its length into pieces
    of at most PIECE_LEN_M, each standing on its own ground, and how much its ground varies along it. A building's
    floor is its footprint's highest ground (base_z): over 3 km the SLAC Klystron Gallery's stood 13 m over its trench
    where I-280 crosses it, and its only collidable copy was its centre's road tile's, 1.1 km away (2026-11-07). The
    cuts are at the same places whichever tile asks (its oriented box, its axis pointing east / north: as the plazas'
    triangles and the container yards' grid), so each tile keeps the pieces whose centre it owns, and no piece is
    doubled or missing. ([ring], 0) when it is not long and narrow (SPLIT_MIN_L, SPLIT_MAX_W), or its ground varies
    less than SPLIT_DZ along it and it is shorter than SPLIT_ALWAYS_L; elev(lat, lon) the ground (arrays)."""
    c, u, hl, hw = oriented_box(ring)
    if 2 * hl < SPLIT_MIN_L or 2 * hw > SPLIT_MAX_W:
        return [ring], 0.0
    if u[0] < -1e-9 or (abs(u[0]) <= 1e-9 and u[1] < 0):
        u = -u
    t = np.r_[np.arange(-hl, hl, PIECE_STEP_M), hl]
    p = c + t[:, None] * u
    la, lo, _ = frame.enu_to_geodetic(p[:, 0] + x0, p[:, 1] + y0, 0.0)
    g = np.asarray(elev(np.asarray(la, np.float64), np.asarray(lo, np.float64)), np.float64)
    drop = float(np.ptp(g)) if np.isfinite(g).all() else 0.0
    if drop <= SPLIT_DZ and 2 * hl < SPLIT_ALWAYS_L:
        return [ring], 0.0
    n = int(np.ceil(2 * hl / PIECE_LEN_M))
    bounds = [-hl + 2 * hl * k / n for k in range(n + 1)]
    bounds[0], bounds[-1] = -hl - 1.0, hl + 1.0
    out = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        r = _clip_half(ring, c + u * a, u, True)
        r = _clip_half(r, c + u * b, u, False) if r is not None else None
        r = clean_ring(r) if r is not None else None    # (clipped rings are closed: their roofs half triangulated)
        if r is not None:
            out.append(r)
    return (out, drop) if len(out) > 1 else ([ring], 0.0)


def _passages(rings, attrs, looks, mins, bridges_xy, decks=None, ground_at=None):
    """Buildings a road bridge runs through split round a PASSAGE_W wide way (Tower Bridge's road ran into its towers'
    stone and its Southern Gate, the user 2026-10-18: "the buildings are still in the middle of the bridge", "this
    building is still blocking the bridge"). With the decks' heights (decks: [(xy (k, 2), z (k,))] relative to the
    tile, ground_at(c (2,)) -> the ground under a footprint's centre): only where the deck passes through the
    building's height (a building under a viaduct stays whole): solid under the deck, the way PASSAGE_CLEAR over it,
    the whole footprint above. Without them, a bridge's own structure (bridge_part / bridge_masonry) starting within
    PASSAGE_BOTTOM_M gets the way PASSAGE_H over its bottom (its deck: the part starts at the road)."""
    R, A, L, M = [], [], [], []
    lines = [d[0] for d in decks] if decks else bridges_xy
    for ring, at, look, mh in zip(rings, attrs, looks, mins):
        bridge_own = bool(at.get("bridge_part") or at.get("bridge_masonry"))
        if not lines or road_through(ring, lines) < 3.0 or (not decks and (not bridge_own or mh > PASSAGE_BOTTOM_M)):
            R.append(ring), A.append(at), L.append(look), M.append(mh)
            continue
        c = ring.mean(0)
        best = None
        for k, ln in enumerate(lines):
            for q in range(len(ln) - 1):
                a, b = ln[q], ln[q + 1]
                ab = b - a
                t = float(np.clip(dnp.matmul(c - a, ab) / max(dnp.matmul(ab, ab), 1e-9), 0, 1))
                d = float(dnp.hypot(*(a + t * ab - c)))
                if best is None or d < best[0]:
                    zd = None
                    if decks:
                        z = decks[k][1]
                        zd = float(z[q] + t * (z[q + 1] - z[q]))
                    best = (d, a + t * ab, ab / max(float(dnp.hypot(*ab)), 1e-9), zd)
        _, p0, u, zd = best
        nrm = np.array([-u[1], u[0]])
        lo_w, hi_w = -PASSAGE_W / 2, PASSAGE_W / 2
        z_lo = z_hi = zd                # (the lowest and highest deck of the stack through it: PASSAGE_STACK_M)
        if decks:                       # (as wide as every deck through it at about this height, PASSAGE_MARGIN_M more)
            a_, b_ = np.inf, -np.inf
            for k, ln in enumerate(lines):
                w_ = float(decks[k][2]) if len(decks[k]) > 2 else None
                if w_ is None or road_through(ring, [ln]) < 3.0:
                    continue
                d_ = np.diff(ln, axis=0)
                t_ = np.clip(((c - ln[:-1]) * d_).sum(1) / np.maximum((d_ * d_).sum(1), 1e-9), 0.0, 1.0)
                q_ = ln[:-1] + t_[:, None] * d_
                j_ = int(np.argmin(dnp.hypot(*(q_ - c).T)))
                dir_ = d_[j_] / max(float(dnp.hypot(*d_[j_])), 1e-9)
                if abs(float(dnp.matmul(dir_, u))) < 0.9:
                    continue
                if zd is not None:
                    z_ = decks[k][1]
                    zk = float(z_[j_] + t_[j_] * (z_[j_ + 1] - z_[j_]))
                    if not np.isfinite(zk) or abs(zk - zd) > PASSAGE_STACK_M:
                        continue
                    z_lo, z_hi = min(z_lo, zk), max(z_hi, zk)
                off = float(dnp.matmul(q_[j_] - p0, nrm))
                a_, b_ = min(a_, off - w_ / 2), max(b_, off + w_ / 2)
            if np.isfinite(a_):
                lo_w, hi_w = min(lo_w, a_ - PASSAGE_MARGIN_M), max(hi_w, b_ + PASSAGE_MARGIN_M)
        h = at.get("height")
        own = look is not None          # (a building's part carries its own height over its bottom, a building its top)
        top = (mh + float(h) if own else float(h)) if h is not None and np.isfinite(h) else mh + PASSAGE_H + 3.0
        if zd is not None and ground_at is not None and np.isfinite(zd):
            # the deck's height over the ground the part is drawn on: its footprint's highest (base_z; at its centre,
            # the Golden Gate's north tower slab, its footprint up the shore, was drawn ~2 m higher than that took,
            # over the deck, GitHub #1032)
            g_ = float(ground_at(ring, True) if at.get("portal") else ground_at(ring))
            dh_lo, dh_hi = z_lo - g_, z_hi - g_     # (a double-deck bridge: its lower deck's way too)
            if top <= dh_lo - PASSAGE_UNDER_M or mh >= dh_hi + PASSAGE_CLEAR:
                R.append(ring), A.append(at), L.append(look), M.append(mh)
                continue                            # under the decks, or over their way
            low, cut = max(mh, dh_lo - 1.5), dh_hi + PASSAGE_CLEAR
        else:
            low, cut = mh, mh + PASSAGE_H
        cut = min(cut, top)
        if low > mh + 0.5:                          # solid under the deck
            R.append(ring), A.append(dict(at, height=(low - mh) if own else low)), L.append(look), M.append(mh)
        for sgn, edge in ((1, hi_w), (-1, lo_w)):
            piece = _clip_half(ring, p0 + nrm * edge, nrm * sgn, True)
            piece = clean_ring(piece) if piece is not None else None    # (closed rings: roofs half triangulated)
            if piece is not None and cut > low + 0.5:
                R.append(piece), A.append(dict(at, height=(cut - low) if own else cut)), L.append(look), M.append(low)
        if top > cut + 1.0:
            R.append(ring), A.append(dict(at, height=(top - cut) if own else top)), L.append(look), M.append(cut)
    return R, A, L, M


ROAD_PASSAGE_ON = os.environ.get("BNE_ROAD_PASSAGE", "on") != "off"
ROAD_PASSAGE_CLASSES = {"service"}      # (the through classes are lifted over instead, ROAD_THROUGH_M)
ROAD_PASSAGE_SKIP = ("parking_aisle", "driveway", "drive-through")      # (inside a car park or a lot: no passage)
ROAD_PASSAGE_W = 7.0        # m: the way cut through a footprint for a mapped road inside it (a lane and its margins)
ROAD_PASSAGE_H = 4.5        # m: its clear height; the building goes on over it where it is taller
ROAD_PASSAGE_RUN_M = 8.0    # m of road at least this deep inside a footprint make a passage (less: the outline's slop)
ROAD_PASSAGE_DEEP_M = 2.0   # m: "deep" is this far in from the footprint's edge
ROAD_PASSAGE_STEP_M = 3.0   # m: the road's samples for that test
ROAD_PASSAGE_MAX_SEGS = 3   # a way of more straight stretches than this (a lot's winding aisles) is no passage ...
ROAD_PASSAGE_MAX_PIECES = 24    # ... and a footprint that would be cut into more pieces than this stays whole
ROAD_PASSAGE_SKIP_ATTRS = ("ship", "ml", "bridge_part", "bridge_masonry", "pier", "portal")
ROAD_PASSAGE_GARAGE_M2 = 12_000.0   # a car park's own footprint (flagged garage) this small or smaller has no passages;
                                    # a larger one is a building that holds a car park (Ulemiste keskus, 51,000 m2)


def _simplify(p, tol=1.0):
    """The polyline p (k, 2) without the points within tol m of the line between the kept ones (Douglas-Peucker)."""
    keep = np.zeros(len(p), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(p) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        d = p[b] - p[a]
        n = float(dnp.hypot(*d))
        q = p[a + 1:b] - p[a]
        dist = np.abs(q[:, 0] * d[1] - q[:, 1] * d[0]) / n if n > 1e-9 else dnp.hypot(*q.T)
        k = int(np.argmax(dist))
        if dist[k] > tol:
            keep[a + 1 + k] = True
            stack += [(a, a + 1 + k), (a + 1 + k, b)]
    return p[keep]


def _edge_dist(pts, ring):
    """Distance (m) of each of the points (k, 2) to the closed ring's edges."""
    a, b = ring[:-1], ring[1:]
    ab = b - a
    den = np.maximum((ab * ab).sum(1), 1e-12)
    out = np.full(len(pts), np.inf)
    for j, q in enumerate(pts):
        t = np.clip(((q - a) * ab).sum(1) / den, 0.0, 1.0)
        out[j] = float(dnp.hypot(*(a + t[:, None] * ab - q).T).min())
    return out


def road_spans(ring, line):
    """The stretches of the polyline (k, 2) that lie deep inside the footprint ring (ROAD_PASSAGE_DEEP_M in from its
    edge) for at least ROAD_PASSAGE_RUN_M, each with a stride of road past either end: [polyline (j, 2)]. A road
    crossing the outline's edge or running along it, a few metres off the mapped line, has none."""
    from polygon import Path
    seg = dnp.hypot(*np.diff(line, axis=0).T)
    arc = np.r_[0.0, np.cumsum(seg)]
    if arc[-1] < ROAD_PASSAGE_RUN_M:
        return []
    t = np.unique(np.r_[np.arange(0.0, arc[-1], ROAD_PASSAGE_STEP_M), arc[-1]])
    p = np.stack([np.interp(t, arc, line[:, 0]), np.interp(t, arc, line[:, 1])], 1)
    deep = Path(ring).contains_points(p)
    if not deep.any():
        return []
    deep &= _edge_dist(p, ring) >= ROAD_PASSAGE_DEEP_M
    out, k = [], 0
    while k < len(deep):
        if not deep[k]:
            k += 1
            continue
        j = k
        while j < len(deep) and deep[j]:
            j += 1
        if t[j - 1] - t[k] >= ROAD_PASSAGE_RUN_M:
            lo, hi = max(k - 2, 0), min(j + 1, len(p))          # (a stride more each end: out through the wall)
            sp = _simplify(p[lo:hi])
            if len(sp) - 1 <= ROAD_PASSAGE_MAX_SEGS:
                out.append(sp)
        k = j
    return out


def _cut_quad(ring, quad):
    """(the parts of ring outside the convex quad (4, 2), counter-clockwise; the part inside it or None)."""
    rest, outs = ring, []
    for j in range(4):
        v0, v1 = quad[j], quad[(j + 1) % 4]
        d = v1 - v0
        inw = np.array([-d[1], d[0]]) / max(float(dnp.hypot(*d)), 1e-9)
        o = _clip_half(rest, v0, -inw, True)
        if o is not None:
            outs.append(o)
        rest = _clip_half(rest, v0, inw, True)
        if rest is None:
            return outs, None
    return outs, rest


def _road_passages(rings, attrs, looks, mins, lines):
    """A road mapped inside a footprint with no tunnel or bridge tag (a mall's service roads through its covered way,
    Ulemiste keskus: the road runs in under the building at the back and comes out of its far side) is a passage, not
    a wall: the footprint is cut round the road's way, ROAD_PASSAGE_W wide, solid from the ground up beside it and
    from ROAD_PASSAGE_H over the road (lintels) where the building is taller, open where it is not. Only
    ground-level buildings with a height of their own; lines: the roads (road_lines, ROAD_PASSAGE_CLASSES, not a car
    park's aisles). Pure."""
    if not lines:
        return rings, attrs, looks, mins
    bx = line_boxes(lines)
    R, A, L, M = [], [], [], []
    for ring, at, look, mh in zip(rings, attrs, looks, mins):
        h = at.get("height")
        r = np.asarray(ring, np.float64)
        spans = []
        if (look is None and mh <= 0.5 and h is not None and np.isfinite(h)
                and not any(at.get(k) for k in ROAD_PASSAGE_SKIP_ATTRS)
                and not (at.get("garage") and abs(_area2(r)) / 2 <= ROAD_PASSAGE_GARAGE_M2)):
            lo, hi = r.min(0), r.max(0)
            near = np.flatnonzero((bx[:, 2] >= lo[0]) & (bx[:, 0] <= hi[0]) & (bx[:, 3] >= lo[1]) & (bx[:, 1] <= hi[1]))
            for li in near:
                spans += road_spans(r, np.asarray(lines[li], np.float64)[:, :2])
        if not spans:
            R.append(ring), A.append(at), L.append(look), M.append(mh)
            continue
        solid, lintels = [r], []
        for sp in spans:
            for a, b in zip(sp[:-1], sp[1:]):
                d = b - a
                n = float(dnp.hypot(*d))
                if n < 0.5:
                    continue
                u = d / n
                w = np.array([-u[1], u[0]]) * ROAD_PASSAGE_W / 2
                a_, b_ = a - u * 0.5, b + u * 0.5               # (half a metre over each end: no sliver at a bend)
                quad = np.array([a_ - w, b_ - w, b_ + w, a_ + w])
                nxt = []
                for piece in solid:
                    outs, inside = _cut_quad(piece, quad)
                    nxt += outs
                    if inside is not None:
                        lintels.append(inside)
                solid = nxt
        if len(solid) + len(lintels) > ROAD_PASSAGE_MAX_PIECES:
            R.append(ring), A.append(at), L.append(look), M.append(mh)
            continue
        for piece in solid:
            R.append(piece), A.append(at), L.append(look), M.append(mh)
        if float(h) > ROAD_PASSAGE_H + 1.0:
            for piece in lintels:
                R.append(piece), A.append(at), L.append(look), M.append(ROAD_PASSAGE_H)
    return R, A, L, M


ML_BRIDGE_M = 6.0          # m of a road bridge's line through a machine-learned footprint: the deck, not a building
ML_ROAD_M = 4.0            # m from a public road's centre line to a machine-learned footprint's centre: a vehicle on it
                           # (at least; where the road's drawn width is known, ML_ROAD_EDGE_M past its half-width)
ML_ROAD_EDGE_M = 1.0       # m past a road's drawn edge a machine-learned footprint's centre still counts as on its lanes
                           # (I-280 at Cupertino, 2026-11-08: Overture aed44c00-..., Microsoft's 8 x 30 m outline of a
                           # lorry, height 0.73 m, 7.0 m from the centre line of a 5-lane, 26 m wide carriageway stood as a
                           # house in the fast lane: ML_ROAD_M alone covered its middle two lanes)
ML_MEDIAN_M = 9.0          # m: a machine-learned footprint between two parallel roads this close on either side lies in a
                           # dual carriageway's median (GitHub #90: a house between Moscow's Bolshaya Tulskaya carriageways)


def _line_dist(p, lines):
    """Distance from point p (2,) to the nearest of the polylines lines (k, 2), or inf."""
    best = np.inf
    for ln in lines:
        a, b = ln[:-1], ln[1:]
        d = b - a
        t = np.clip(((p - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
        q = a + t[:, None] * d
        best = min(best, float(dnp.hypot(*(q - p).T).min()))
    return best


def _on_lanes(c, roads_xy):
    """Does point c (2,) lie on a public road's drawn surface: within its half-width + ML_ROAD_EDGE_M of its centre
    line (roads_xy.half, road_lines), and within ML_ROAD_M of any (a plain list without widths: that alone)?"""
    halves = getattr(roads_xy, "half", None)
    for k, ln in enumerate(roads_xy):
        h = halves[k] if halves is not None and k < len(halves) else None
        reach = max(ML_ROAD_M, h + ML_ROAD_EDGE_M) if h is not None else ML_ROAD_M
        if len(ln) < 2 or ln[:, 0].min() > c[0] + reach or ln[:, 0].max() < c[0] - reach or \
                ln[:, 1].min() > c[1] + reach or ln[:, 1].max() < c[1] - reach:
            continue
        if _line_dist(c, [ln]) <= reach:
            return True
    return False


def _nearest_on(p, ln):
    """(distance, unit direction of the segment) of polyline ln (k, 2) nearest point p (2,)."""
    a, b = ln[:-1], ln[1:]
    d = b - a
    t = np.clip(((p - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
    q = a + t[:, None] * d
    dist = dnp.hypot(*(q - p).T)
    k = int(np.argmin(dist))
    u = d[k] / max(float(dnp.hypot(*d[k])), 1e-9)
    return float(dist[k]), q[k], u


def _in_median(c, roads_xy):
    """Does point c (2,) lie between two parallel roads each within ML_MEDIAN_M, on opposite sides of it?"""
    near = []
    for ln in roads_xy:
        if len(ln) < 2 or ln[:, 0].min() > c[0] + ML_MEDIAN_M or ln[:, 0].max() < c[0] - ML_MEDIAN_M or \
                ln[:, 1].min() > c[1] + ML_MEDIAN_M or ln[:, 1].max() < c[1] - ML_MEDIAN_M:
            continue
        dist, q, u = _nearest_on(c, ln)
        if dist <= ML_MEDIAN_M:
            near.append((q - c, u))
    for i in range(len(near)):
        for j in range(i + 1, len(near)):
            (vi, ui), (vj, uj) = near[i], near[j]
            if abs(float(dnp.matmul(ui, uj))) > 0.9 and float(dnp.matmul(vi, vj)) < 0:        # parallel, one on each side
                return True
    return False


def _ml_false(ring, bridges_xy, water_xy, roads_xy=None):
    """A machine-learned footprint (Microsoft's, Google's) that is a bridge deck, a boat or a vehicle: a road bridge
    runs through it, its centre lies on mapped water (Tower Bridge's river: barges as brick buildings, 2026-10-18), on
    a public road's lanes (_on_lanes: a lorry on a Norwegian trunk road stood as a white building over the lanes,
    GitHub #2; one in I-280's fast lane, ML_ROAD_EDGE_M) or in a dual carriageway's median (_in_median, GitHub #90)."""
    if bridges_xy and road_through(ring, bridges_xy) >= ML_BRIDGE_M:
        return True
    if roads_xy and (_on_lanes(ring.mean(0), roads_xy) or _in_median(ring.mean(0), roads_xy)):
        return True
    if water_xy:
        from polygon import Path
        c = ring.mean(0)
        for w, holes in water_xy:
            if w[:, 0].min() <= c[0] <= w[:, 0].max() and w[:, 1].min() <= c[1] <= w[:, 1].max() \
                    and Path(w).contains_points(c[None])[0] \
                    and not any(Path(h).contains_points(c[None])[0] for h in holes):    # (an island: land)
                return True
    return False


ML_STEEP_DEG = 25.0         # a machine-learned footprint on ground this steep ...
ML_FAR_FROM_WAY_M = 60.0    # ... this far from any mapped way (a road, track or path) is a rock or a shadow, not a hut
ML_CLIFF_DEG = 36.0         # ... and on ground this steep, wherever it is
# (GitHub #242: 64 machine-learned outlines (Shi et al.'s East Asia set) on Mount Fuji's slopes, median 34 deg, half
# over 60 m from a trail; the real huts on 6 deg next to the trail. Steep places with real ones, 2026-10-24: La Paz 0 of
# 5453, Gorkha (Nepal) 0 of 50, Rwanda's hills 1 of 293, Medellin and Valparaiso 0)


def _ml_steep(ring, g, ways_xy):
    """Is a machine-learned footprint (ring, relative to the tile's corner; g its ground at each corner) on ground too
    steep for it (ML_*)? The slope of the plane through its corners; the distance from its centre to the nearest
    mapped way (ways_xy: way_lines, the same corner). Pure."""
    r = np.asarray(ring, np.float64)[:, :2]
    g = np.asarray(g, np.float64)
    if len(r) < 3 or len(g) != len(r):
        return False
    A = np.c_[r - r.mean(0), np.ones(len(r))]
    try:
        (a, b, _), *_ = dnp.lstsq(A, g, rcond=None)
    except np.linalg.LinAlgError:
        return False
    slope = math.degrees(dmath.atan(math.hypot(a, b)))
    if slope > ML_CLIFF_DEG:
        return True
    if slope <= ML_STEEP_DEG:
        return False
    return not ways_xy or _line_dist(r.mean(0), ways_xy) > ML_FAR_FROM_WAY_M


def way_lines(frame, road_layers, x0, y0, margin=ML_FAR_FROM_WAY_M + 20.0, size=256.0):
    """Polylines (k, 2) relative to (x0, y0) of every mapped way near the tile (roads of any class, tracks, paths,
    steps: _ml_steep's access test)."""
    out = []
    for f in road_layers:
        if f is None or len(f.kind) == 0:
            continue
        bb = f.boxes(frame)
        near = np.flatnonzero((bb[:, 2] - x0 >= -margin) & (bb[:, 0] - x0 <= size + margin)
                              & (bb[:, 3] - y0 >= -margin) & (bb[:, 1] - y0 <= size + margin))
        for i in near.tolist():
            a = f.coord_off[f.ring_off[f.part_off[i]]]
            b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
            ll = f.lonlat[a:b]
            if len(ll) < 2:
                continue
            e, n = mapgeom.enu_xy(f, frame, ll)
            out.append(np.stack([e - x0, n - y0], 1))
    return out


def bridge_lines(frame, road_layers, x0, y0, margin=50.0, size=256.0):
    """Polylines (k, 2) relative to (x0, y0) of the road bridges near the tile (their bridge ranges)."""
    import roads as roadsmod
    out = []
    for f in road_layers:
        if f is None or len(f.kind) == 0:
            continue
        bs = roadsmod.bridge_specs(f)
        for i in range(len(f.kind)):
            if bs[i] is None:
                continue
            for ring in roadsmod.bridge_parts(f, i):
                e, n = mapgeom.enu_xy(f, frame, ring)
                xy = np.stack([e - x0, n - y0], 1)
                if len(xy) < 2 or xy[:, 0].max() < -margin or xy[:, 0].min() > size + margin or \
                        xy[:, 1].max() < -margin or xy[:, 1].min() > size + margin:
                    continue
                out.append(xy)
    return out


def water_rings(frame, water_layers, x0, y0, margin=50.0, size=256.0):
    """[(outer ring (k, 2), [hole rings])] relative to (x0, y0) of the mapped water areas (sea, rivers, lakes, docks)
    near the tile (their holes: islands)."""
    out = []
    for f in water_layers:
        if f is None or len(f.kind) == 0:
            continue
        for i in np.flatnonzero(f.kind == 3):
            for part in f.parts(i):
                if not part or len(part[0]) < 4:
                    continue
                e, n = mapgeom.enu_xy(f, frame, part[0])
                xy = np.stack([e - x0, n - y0], 1)
                if xy[:, 0].max() < -margin or xy[:, 0].min() > size + margin or xy[:, 1].max() < -margin \
                        or xy[:, 1].min() > size + margin:
                    continue
                holes = []
                for h in part[1:]:
                    if len(h) >= 4:
                        he, hn = mapgeom.enu_xy(f, frame, h)
                        holes.append(np.stack([he - x0, hn - y0], 1))
                out.append((xy, holes))
    return out


_BOXES = {}                     # id(list of polylines) -> (that list, its boxes (n, 4)): the last few lists asked


def line_boxes(lines):
    """(n, 4) x0 y0 x1 y1 of each polyline (k, 2+) of the list, made once per list (a tile's buildings test the same
    lists of roads one footprint after another: ~1.2M small numpy min / max calls a Baltimore tile set)."""
    got = _BOXES.get(id(lines))
    if got is not None and got[0] is lines:
        return got[1]
    b = np.array([(ln[:, 0].min(), ln[:, 1].min(), ln[:, 0].max(), ln[:, 1].max()) for ln in lines],
                 np.float64).reshape(-1, 4)
    if len(_BOXES) >= 16:
        _BOXES.clear()
    _BOXES[id(lines)] = (lines, b)      # (the list kept: its id cannot be another list's meanwhile)
    return b


def road_through(ring, lines, step=2.0):
    """Metres of the polylines lines (k, 2) inside ring (sampled every step m)."""
    lo, hi = ring.min(0), ring.max(0)
    b = line_boxes(lines)
    near = np.flatnonzero((b[:, 2] >= lo[0]) & (b[:, 0] <= hi[0]) & (b[:, 3] >= lo[1]) & (b[:, 1] <= hi[1]))
    if not len(near):
        return 0.0
    fast.require(74, "road_through")         # (the Rust loop: every sample's even-odd test there)
    sel = [np.asarray(lines[li], np.float64)[:, :2] for li in near]
    starts = np.r_[0, np.cumsum([len(ln) for ln in sel])].astype(np.int64)
    return float(fast._ef.road_through(np.ascontiguousarray(np.asarray(ring, np.float64)[:, :2]),
                                       np.ascontiguousarray(np.concatenate(sel)), starts, float(step)))


def road_across(ring, lines, max_w=ACROSS_W):
    """Does one of the polylines lines (k, 2) cross the narrow footprint ring (at most max_w wide) along its length,
    over its long middle line?"""
    c, u, hl, hw = oriented_box(ring)
    if 2 * hw > max_w or hl <= hw:
        return False
    a, b = c - u * hl, c + u * hl
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    bx = line_boxes(lines)
    for li in np.flatnonzero((bx[:, 2] >= lo[0]) & (bx[:, 0] <= hi[0]) & (bx[:, 3] >= lo[1]) & (bx[:, 1] <= hi[1])):
        ln = lines[li]
        p, q = ln[:-1], ln[1:]
        d = q - p
        e = b - a
        den = d[:, 0] * e[1] - d[:, 1] * e[0]
        ok = np.abs(den) > 1e-9
        den = np.where(ok, den, 1.0)
        w = a - p
        t = (w[:, 0] * e[1] - w[:, 1] * e[0]) / den         # along the road segment
        s_ = (w[:, 0] * d[:, 1] - w[:, 1] * d[:, 0]) / den  # along the middle line
        if np.any(ok & (t >= 0) & (t <= 1) & (s_ >= 0) & (s_ <= 1)):
            return True
    return False


# Buildings standing on the drawn carriageways (the user, 2026-11-12: "have you looked at houses blocking the road
# sometimes?"; GitHub #523, #543, #1184, #433, #74 and Discord). Where a footprint reaches into a ground road's
# carriageway as the road tiles draw it (road_specs' width with the land cover and the measured widths, placement,
# tapers), it is cut back to the asphalt's edge (earthfast.footprint_clear: the side facing the road; a pitched roof's
# box keeps its shape): the outline's slop, a footprint machine-learned from an old photo (Fremantle's High Street:
# Microsoft's outline of a house pulled down for the 2023 roundabout, 2.3 m into its lanes) or a carriageway drawn
# wider than the street (Newhey's terraces along the A640, 2 m into its 12 m). A machine-learned footprint that a cut
# cannot clear (deep in the lanes, the road through it) is left out: a vehicle or a stale photo's house. A mapped one
# the road runs through stays: a passage (ROAD_THROUGH_M lifts it, tunnels / building_passage are not ground roads).
ROAD_CLEAR_ON = os.environ.get("BNE_ROAD_CLEAR", "on") != "off"
ROAD_CLEAR_TOL_M = 0.3          # m a footprint may reach into a carriageway untouched (a curb's width, the outline's slop)
ROAD_CLEAR_MARGIN_M = 0.25      # m a cut footprint stands back from the carriageway's edge
ROAD_CLEAR_SHARE = 0.4          # share of a mapped footprint a cut may take (more: it stays as mapped)
ROAD_CLEAR_ML_SHARE = 0.5       # ... of a machine-learned one (more: left out)
ROAD_CLEAR_ML_DEPTH_M = 1.0     # m: a machine-learned footprint reaching further into the lanes is left out, not cut (an
                                # outline traced off a photo is seldom off by more; deeper: a vehicle, or a house the
                                # road has replaced since the photo)
ROAD_CLEAR_STEP_M = 0.5         # m between the samples of a footprint
ROAD_CLEAR_PASSAGE_H = 4.5      # m: a passage's clear height under a mapped building the road runs into (ROAD_PASSAGE_H)
ROAD_CLEAR_LINTEL_M = 2.5       # m of building over it at least (a storey), else it stands beside the road in pieces
ROAD_CLEAR_PIECES_MIN = 0.15    # share of a low mapped footprint its pieces off the road keep at least, else left out
ROAD_CLEAR_SKIP_SUB = ("driveway", "parking_aisle", "drive-through")    # (a garage's drive, a lot's aisles, a drive-thru:
                                                                        # roofs over them are real)
ROAD_CLEAR_CLASSES = THROUGH_CLASSES | {"service"}
ROAD_CLEAR_SKIP_CLS = CANOPY_CLASSES | BRIDGE_CLASSES | {"ship", "toll_booth", landmarks.LANDMARK}


def carriageway_segments(frame, road_layers, x0, y0, size=256.0, margin=60.0, urban_fn=None, widths=None,
                         left_hand=False, force=False):
    """(seg (S, 4): ax ay bx by relative to (x0, y0), half (S,): each segment's half width) of the ground
    carriageways near the square [x0, x0 + size]^2 of frame, drawn as the road tiles draw them (roads.build_tile's
    ground rings: road_specs(urban_fn, widths, left_hand), the placement's lines, the tapers), of the classes
    ROAD_CLEAR_CLASSES (no driveways or lot aisles), or None (and with the clearance off, unless force)."""
    import roads as roadsmod
    cells = [f for f in road_layers if f is not None and len(f.kind)]
    if not cells or not (ROAD_CLEAR_ON or force):
        return None
    placement = roadsmod.placement_context(cells, urban_fn, widths, left_hand)
    copies = roadsmod.copies_of(cells)

    def taper_of(f):
        return roadsmod.road_tapers(f, urban_fn, widths, left_hand)
    lo, hi = -margin, size + margin
    segs, halves, seen = [], [], set()
    for f in cells:
        cls = f.attrs.get("class")
        if cls is None:
            continue
        bb = f.boxes(frame)
        near = np.flatnonzero((bb[:, 2] - x0 >= lo) & (bb[:, 0] - x0 <= hi) & (bb[:, 3] - y0 >= lo)
                              & (bb[:, 1] - y0 <= hi))
        specs = None
        for i in near.tolist():
            fid = str(f.ids[i])
            if fid in seen or str(cls[i]) not in ROAD_CLEAR_CLASSES:
                continue
            if roadsmod._rule_value(f.rules.get("subclass"), i) in ROAD_CLEAR_SKIP_SUB:
                continue
            if specs is None:
                specs = roadsmod.road_specs(f, urban_fn, widths, left_hand)
            sp = specs[i] if i < len(specs) else None
            if sp is None or not np.isfinite(sp.width) or sp.width <= 0:
                continue
            seen.add(fid)
            tapers = None
            for ring in roadsmod.ground_parts(f, i):
                if len(ring) < 2:
                    continue
                ring = placement.ring(frame, f, i, ring, sp, fid)
                e, n = mapgeom.enu_xy(f, frame, ring)
                xy = np.stack([np.asarray(e, np.float64) - x0, np.asarray(n, np.float64) - y0], 1)
                if xy[:, 0].max() < lo or xy[:, 0].min() > hi or xy[:, 1].max() < lo or xy[:, 1].min() > hi:
                    continue
                if tapers is None:
                    tapers = roadsmod.joint_desc(copies.get(fid, [(f, i)]), taper_of)
                ws = roadsmod.taper_widths(sp.width, tapers, ring[:, 0], ring[:, 1])
                segs.append(np.concatenate([xy[:-1], xy[1:]], 1))
                halves.append(np.full(len(xy) - 1, sp.width / 2.0) if ws is None else (ws[:-1] + ws[1:]) / 4.0)
    if not segs:
        return None
    return np.ascontiguousarray(np.concatenate(segs), np.float64), np.ascontiguousarray(np.concatenate(halves), np.float64)


def _clearable(spec):
    """A building the road clearance may cut or leave out: on the ground, not a canopy driven under, a bridge's frame,
    a ship, a toll booth, a landmark's model, a parking deck or a tunnel's portal building."""
    return ((spec.min_h or 0.0) <= 0.5 and spec.cls not in ROAD_CLEAR_SKIP_CLS and not spec.deck and not spec.portal
            and not spec.ship)


def _ground_up(frame, x0, y0, elev, rings):
    """The ground (the frame's up) at every corner of rings (tile-relative), one elev call; NaN where none."""
    allr = np.concatenate(rings)
    la, lo, _ = frame.enu_to_geodetic(allr[:, 0] + x0, allr[:, 1] + y0, np.zeros(len(allr)))
    la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
    g = np.asarray(elev(la, lo), np.float64)
    up = np.full(len(allr), np.nan)
    ok = np.isfinite(g)
    if ok.any():
        up[ok] = np.asarray(frame.geodetic_to_enu(la[ok], lo[ok], g[ok])[2], np.float64)
    out, at = [], 0
    for r in rings:
        out.append(up[at:at + len(r)])
        at += len(r)
    return out


def _spec_copy(spec, **kw):
    s = Spec(**{k: getattr(spec, k) for k in Spec.__slots__})
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def road_clearance(items, carriageways, frame, x0, y0, elev, stats=None, passages=None):
    """items (tile_buildings') with every clearable footprint (_clearable) kept off the carriageways
    (carriageway_segments' (seg, half), tile-relative like the rings; the ground at new corners read again):
    - cut back to their edges where that takes at most ROAD_CLEAR_SHARE of it (ROAD_CLEAR_ML_SHARE of a machine-learned
      one, which must reach no further than ROAD_CLEAR_ML_DEPTH_M into the lanes);
    - else a machine-learned one is left out (a vehicle, a house the road replaced since the photo);
    - else a mapped one the road runs into or through is a passage when it is tall enough (ROAD_CLEAR_PASSAGE_H and a
      storey over it): its pieces beside the carriageways (earthfast.footprint_pieces) stand up to the passage's height
      and the whole of it goes on over the road from there, its roof as it was; a lower one keeps its pieces (flat
      roofs: a pitched roof's box would reach back over the road), and is left out when they are under
      ROAD_CLEAR_PIECES_MIN of it (a shed or kiosk on the road's line: the map has one of them wrong).
    passages: (seg, half) of the open tunnels (_open_tunnels: a building passage on the street's level): a footprint
    one runs into is a passage as above, never cut back or left out (the map says the road goes under it).
    stats: a dict that gets the counts per outcome."""
    if not items:
        return items

    def count(spec, name):
        if stats is not None:
            key = ("ml_" if spec.ml else "osm_") + name
            stats[key] = stats.get(key, 0) + 1
    if passages is not None:        # (first: a ground road joining the passage's mouth must not cut its arch away)
        items = _clear_passages(items, passages, frame, x0, y0, elev, count)
    if ROAD_CLEAR_ON and carriageways is not None:
        items = _clear_roads(items, carriageways, frame, x0, y0, elev, count)
    return items


def _clear_rings(items):
    """(the clearable items' indices, their drawn rings, boxed flags, ring offsets) for footprint_clear / _pieces."""
    idx = [k for k, (spec, _g0, _g1) in enumerate(items) if _clearable(spec)]
    rings = [np.asarray(drawn_ring(items[k][0]), np.float64)[:, :2] for k in idx]
    boxed = np.array([drawn_ring(items[k][0]) is not items[k][0].ring for k in idx], np.uint8)
    off = np.r_[0, np.cumsum([len(r) for r in rings])].astype(np.int64)
    return idx, rings, boxed, off


def _clear_passages(items, passages, frame, x0, y0, elev, count):
    """road_clearance over the open tunnels: every clearable footprint one reaches into split round its way."""
    seg, half = passages
    idx, rings, boxed, off = _clear_rings(items)
    if not idx:
        return items
    fast.require(243, "footprint_clear")
    _xy, _off, status, _share, _depth, _over = fast._ef.footprint_clear(
        np.ascontiguousarray(np.concatenate(rings)), off, boxed, seg, half, ROAD_CLEAR_TOL_M, ROAD_CLEAR_MARGIN_M,
        max(ROAD_CLEAR_SHARE, ROAD_CLEAR_ML_SHARE), ROAD_CLEAR_STEP_M)
    split = [(k, rings[j], bool(boxed[j])) for j, k in enumerate(idx) if int(status[j])]
    if not split:
        return items
    items, drop = list(items), set()
    extra = _split_passages(items, split, seg, half, frame, x0, y0, elev, count, drop)
    if drop:
        items = [it for k, it in enumerate(items) if k not in drop]
    return items + extra


def _clear_roads(items, carriageways, frame, x0, y0, elev, count):
    """road_clearance over the ground carriageways (its cut / left out / passage rules)."""
    seg, half = carriageways
    idx, rings, boxed, off = _clear_rings(items)
    if not idx:
        return items
    fast.require(243, "footprint_clear")
    out_xy, out_off, status, share, depth, _over = fast._ef.footprint_clear(
        np.ascontiguousarray(np.concatenate(rings)), off, boxed, seg, half, ROAD_CLEAR_TOL_M, ROAD_CLEAR_MARGIN_M,
        max(ROAD_CLEAR_SHARE, ROAD_CLEAR_ML_SHARE), ROAD_CLEAR_STEP_M)
    drop, cut, split = set(), [], []
    for j, k in enumerate(idx):
        spec = items[k][0]
        st = int(status[j])
        if st == 1 and (share[j] > (ROAD_CLEAR_ML_SHARE if spec.ml else ROAD_CLEAR_SHARE)
                        or (spec.ml and depth[j] > ROAD_CLEAR_ML_DEPTH_M)):
            st = 2              # (more than its share, or a machine-learned one deep in the lanes: as one the cut could
                                # not clear)
        if st == 1:
            cut.append((k, np.asarray(out_xy[out_off[j]:out_off[j + 1]], np.float64)))
            count(spec, "trimmed")
        elif st and spec.ml:
            drop.add(k)
            count(spec, "dropped")
        elif st:
            split.append((k, rings[j], bool(boxed[j])))
    items = list(items)
    if cut:
        for (k, r), gr in zip(cut, _ground_up(frame, x0, y0, elev, [r for _, r in cut])):
            if not np.isfinite(gr).all():
                continue        # (keeps its outline: no ground read under the new one)
            spec, _g0, _g1 = items[k]
            spec.ring = r
            if spec.holes:      # (a courtyard the cut reached is no longer one)
                from polygon import Path
                pth = Path(r)
                spec.holes = [h for h in spec.holes if pth.contains_points(np.asarray(h)[:, :2]).all()] or None
            spec.gring = gr
            items[k] = (spec, float(gr.min()), float(gr.max()))
    extra = _split_passages(items, split, seg, half, frame, x0, y0, elev, count, drop) if split else []
    if drop:
        items = [it for k, it in enumerate(items) if k not in drop]
    return items + extra


def _split_passages(items, split, seg, half, frame, x0, y0, elev, count, drop):
    """The footprints split [(item index, drawn ring, boxed)] round the roads (seg, half): road_clearance's passage /
    pieces rule; items updated in place, the indices left out added to drop; returns the new pieces' items."""
    extra = []
    if split:
        srings = [r for _, r, _b in split]
        soff = np.r_[0, np.cumsum([len(r) for r in srings])].astype(np.int64)
        p_xy, p_off, owner, failed = fast._ef.footprint_pieces(
            np.ascontiguousarray(np.concatenate(srings)), soff, seg, half, ROAD_CLEAR_TOL_M, ROAD_CLEAR_MARGIN_M,
            ROAD_CLEAR_STEP_M)
        pieces = [[] for _ in split]
        for p, o in enumerate(owner):
            pieces[o].append(np.asarray(p_xy[p_off[p]:p_off[p + 1]], np.float64))
        allp = [pc for ps in pieces for pc in ps]
        grounds = iter(_ground_up(frame, x0, y0, elev, allp)) if allp else iter(())
        for j, (k, ring, was_box) in enumerate(split):
            spec, g0, g1 = items[k]
            ps = pieces[j]
            gs = [next(grounds) for _ in ps]
            tall = (spec.height or 0.0) >= ROAD_CLEAR_PASSAGE_H + ROAD_CLEAR_LINTEL_M
            if failed[j]:
                if tall:            # (crossed all over: the whole of it over the roads)
                    items[k] = (_spec_copy(spec, min_h=ROAD_CLEAR_PASSAGE_H, height=spec.height - ROAD_CLEAR_PASSAGE_H,
                                           door=None, porch=False, stoop=None, sign=None), g0, g1)
                    count(spec, "passage")
                else:
                    count(spec, "kept")
                continue
            whole = abs(_area2(ring)) / 2
            kept = sum(abs(_area2(p)) / 2 for p in ps)
            if not tall and kept < ROAD_CLEAR_PIECES_MIN * whole:
                drop.add(k)
                count(spec, "dropped")
                continue
            body = []
            for p, gp in zip(ps, gs):
                if not np.isfinite(gp).all():
                    continue
                h = ROAD_CLEAR_PASSAGE_H if tall else spec.height
                fl = max(1, int(round(h / max(spec.floor_h or FLOOR_M, 1.0))))
                body.append((_spec_copy(spec, ring=p, gring=gp, height=h, roof="flat", holes=None, floors=fl,
                                        floor_h=h / fl, door=None, porch=False, stoop=None, sign=None, steeple=False,
                                        tank=False, hf=False, skel=False, fr_house=False),
                             float(gp.min()), g1 if tall else float(gp.max())))   # (tall: its top meets the part over)
            if tall:
                items[k] = (_spec_copy(spec, min_h=ROAD_CLEAR_PASSAGE_H, height=spec.height - ROAD_CLEAR_PASSAGE_H,
                                       ring=ring if was_box else spec.ring, door=None, porch=False,
                                       stoop=None, sign=None,
                                       floors=max(1, int(spec.floors or 1) - 1)), g0, g1)
                count(spec, "passage")
            else:
                drop.add(k)
                count(spec, "pieces")
            extra += body
    return extra


CENTRE_PREFILTER_M = 1.0       # m of slack for Features.outer_centres (cumulative sums: within ~1e-5 m of the exact mean)


PART_OWN = ("roof_shape", "facade_color", "roof_color", "roof_height", "facade_material", "roof_material",
            "roof_direction", "roof_orientation")


def _parts_index(f):
    """{building id: [rows]} of a building parts layer, kept on it (the cells stay decoded in memory: every tile of
    them looks parts up again)."""
    memo = f.__dict__.get("_bnge_parts_by_bid")
    if memo is None:
        memo = {}
        col = f.attrs.get("building_id")
        if col is not None:
            for i in range(len(f.kind)):
                bid = str(col[i])
                if bid:
                    memo.setdefault(bid, []).append(i)
        f.__dict__["_bnge_parts_by_bid"] = memo
    return memo


def _parts_tops(f):
    """Each part's own height + bottom (_part_heights' sum; nan without a height) for the whole layer, kept on it."""
    tops = f.__dict__.get("_bnge_parts_tops")
    if tops is None:
        a, n = f.attrs, len(f.kind)

        def col(k):
            return np.asarray(a[k], np.float64) if k in a else np.full(n, np.nan)
        with np.errstate(invalid="ignore"):
            top, mh, nf, f0 = col("height"), col("min_height"), col("num_floors"), col("min_floor")
            mh = np.where(np.isfinite(mh) & (mh > 0), mh, 0.0)
            f0 = np.where(np.isfinite(f0) & (f0 > 0), f0, 0.0)
            floors = np.maximum(FLOOR_M, (nf - f0) * FLOOR_M) + np.where(mh > 0, mh, f0 * FLOOR_M)
            tops = np.where(np.isfinite(top) & (top > mh + PART_MIN_THICK_M), top,
                            np.where(np.isfinite(nf) & (nf > 0), floors, np.nan))
        f.__dict__["_bnge_parts_tops"] = tops
    return tops


class _PartsOf:
    """The building parts of a tile's cells by building id, each [(ring in the anchor frame, _part_heights)], made
    when asked for: a tile needs those of its own buildings, and the toll booths (booths), not all the cells' parts
    (converting every part for every tile was half of tile_buildings' time in Manhattan, 2026-10-24). A part in two
    cells' layers is taken once (the first)."""

    def __init__(self, frame, part_layers):
        self.frame = frame
        self.layers = [f for f in part_layers if f is not None and len(f.kind)]
        self.memo = {}

    def _part(self, f, i):
        pp = f.parts(i)
        if not pp or not pp[0]:
            return None
        outer = np.asarray(pp[0][0], np.float64)
        if len(outer) < 4:
            return None
        e, n = mapgeom.enu_xy(f, self.frame, outer)
        a = f.attrs
        # (its own roof shape and mapped colours: hudsontadams' PR #167)
        # (and its roof height, materials and roof direction where the layer has them: a part's own roof height was read
        # and dropped, research 2026-11-10)
        own = {k: (a[k][i] if k in a else None) for k in PART_OWN}
        return np.stack([e, n], 1), _part_heights(a, i), own

    def get(self, bid, default=None):
        if bid not in self.memo:
            out, seen = [], set()
            for f in self.layers:
                for i in _parts_index(f).get(bid, ()):
                    pid = f.ids[i]
                    if pid in seen:
                        continue
                    seen.add(pid)
                    p = self._part(f, i)
                    if p is not None:
                        out.append(p)
            self.memo[bid] = out or None
        got = self.memo[bid]
        return default if got is None else got

    def booths(self, lo, hi):
        """The parts (ring, _part_heights) whose top (own height + bottom) is within [lo, hi], of any building."""
        out, seen = [], set()
        for f in self.layers:
            index = _parts_index(f)
            if not index:
                continue
            col = f.attrs["building_id"]
            tops = _parts_tops(f)
            with np.errstate(invalid="ignore"):
                rows = np.flatnonzero((tops >= lo - 1e-6) & (tops <= hi + 1e-6))
            for i in rows.tolist():
                pid = f.ids[i]
                if pid in seen or not str(col[i]):
                    continue
                seen.add(pid)
                p = self._part(f, i)
                if p is not None:
                    out.append(p[:2])
        return out


def _hulls_nearby(frame, layers, x0, y0, x1, y1, water_xy, reach=ships.PIER_REACH_M):
    """Local rings of the moored ships (ships.is_ship) whose outline centres lie outside the tile but within reach of
    it (a quay in this tile may lie alongside a ship the next tile draws)."""
    out = []
    for f in layers:
        if f is None or len(f.kind) == 0:
            continue
        a = f.attrs
        rows = range(len(f.kind))
        if isinstance(f, mapgeom.Features):
            cx_, cy_ = f.outer_centres(frame)
            bx = f.boxes(frame)
            big = np.maximum(bx[:, 2] - bx[:, 0], bx[:, 3] - bx[:, 1]) >= ships.MIN_L / math.sqrt(2)
            rows = np.flatnonzero(big & (cx_ >= x0 - reach) & (cx_ < x1 + reach) & (cy_ >= y0 - reach)
                                  & (cy_ < y1 + reach) & ~((cx_ >= x0) & (cx_ < x1) & (cy_ >= y0) & (cy_ < y1))
                                  & (bx[:, 0] <= x1 + ships.PIER_GAP_M) & (bx[:, 2] >= x0 - ships.PIER_GAP_M)
                                  & (bx[:, 1] <= y1 + ships.PIER_GAP_M) & (bx[:, 3] >= y0 - ships.PIER_GAP_M)).tolist()
        for i in rows:
            if "src_dataset" in a and str(a["src_dataset"][i]) != "OpenStreetMap":
                continue
            if not water_xy and not ships.has_evidence({k: (a[k][i] if k in a else None)
                                                        for k in ("class", "subtype", "name")}):
                continue                        # (no water near and no ship's tags: cheaper than the outline)
            parts = f.parts(i)
            if not parts or not parts[0] or len(parts[0][0]) < 4:
                continue
            e, n = mapgeom.enu_xy(f, frame, np.asarray(parts[0][0], np.float64))
            cx, cy = float(e[:-1].mean()), float(n[:-1].mean())
            if (x0 <= cx < x1 and y0 <= cy < y1) or max(np.ptp(e), np.ptp(n)) < ships.MIN_L / math.sqrt(2):
                continue                        # (the tile's own: tested already)
            r = clean_ring(np.stack([e - x0, n - y0], 1))
            at = {k: (a[k][i] if k in a else None) for k in ("class", "subtype", "name")}
            if r is not None and ships.is_ship(r, at, lambda: ships.on_water(r.mean(0), water_xy)):
                out.append(r)
    return out


# Building parts too thin, too small or seen from afar (streets.gl comparison 2026-11-14, against the OSM data of the
# Empire State Building, the Chrysler Building, Trinity Church and the US Capitol):
# - a part mapped a metre or less thick (a cornice band, a terrace, a slab) kept its height only from PART_MIN_THICK_M
#   (was 1 m: 173 such parts in Manhattan were taken as heightless and drawn up to their building's top);
# - a part under PART_MIN_M2 is left out unless it is PART_TINY_TALL_M tall (a needle, a statue; from PART_TINY_M2);
# - the skyline draws a low building with parts (under SKY_PARTS_MIN_M) as its outline, but at its parts' mean height
#   (by area) with the parts SKY_BODY_RISE times higher on it (sky_body): the Capitol was one 87.6 m box to its statue.
PART_MIN_THICK_M = 0.05
PART_MIN_M2 = 4.0
PART_TINY_M2 = 0.25
PART_TINY_TALL_M = 10.0
SKY_BODY_RISE = 1.5             # a part this many times the body's height rises over it (and SKY_BODY_OVER_M more)
SKY_BODY_OVER_M = 6.0
# A church's tower part (steeple_parts): compact (its oriented box at most STEEPLE_TOWER_ASPECT long for its width),
# STEEPLE_TOWER_M2 in area, the top of its column (no part covering STEEPLE_SPIRE_SHARE of it on it), and rising
# STEEPLE_TOWER_RISE_M over every part not under it, or over them at all where the building's own height stands
# STEEPLE_OUTLINE_OVER_M over it (the map's or the lidar's spire tip: St. Nicholas of Myra, New York, drawn from the
# city's measured massing, a 3.3 m tower band over an 18.1 m body under a 31.4 m outline, 2026-11-14). The tower's own
# top already mapped (a pyramid, cone, dome or onion over STEEPLE_SPIRE_SHARE of it): no spire of ours.
STEEPLE_SPIRE_SHARE = 0.25
STEEPLE_TOWER_ASPECT = 1.6
STEEPLE_TOWER_M2 = (9.0, 160.0)    # (St. Bartholomew's, New York: its dome's drum, a 212 m² band in the massing)
STEEPLE_TOWER_RISE_M = 6.0
STEEPLE_OUTLINE_OVER_M = 3.0
STEEPLE_TWIN = (0.5, 0.25)      # a second tower this close in top (m) and area (share): a twin front, a spire on each


def _part_top(p):
    return p[1][0] + p[1][1]


def _local(ring):
    """A ring (k, 2+) about its first corner (an anchor frame's kilometres off its areas' last digits)."""
    r = np.asarray(ring, np.float64)[:, :2]
    return r - r[0]


def sky_body(bparts, height):
    """The skyline's parts of a building lower than SKY_PARTS_MIN_M: (the parts rising over its body, the body's
    height) where its parts' mean top (by area) is that much lower than the parts over it or than its own mapped height;
    ([], None) otherwise (the outline as mapped). bparts: tile_buildings' [(ring, (own height, bottom) or None,
    ...)]."""
    known = [p for p in bparts if p[1] is not None]
    if not known:
        return [], None
    areas = [abs(_area2(_local(p[0]))) / 2 for p in known]
    total = 0.0
    weighted = 0.0
    for p, a in zip(known, areas):              # (in the parts' order: the same sum everywhere)
        total += a
        weighted += a * _part_top(p)
    if total <= 0.0:
        return [], None
    body = weighted / total
    tall = [p for p in known if _part_top(p) > max(SKY_BODY_RISE * body, body + SKY_BODY_OVER_M)]
    if tall:
        # (and the parts over the body they stand on: the Capitol's lowest drum ring, 27-39.3 m, was left out under its
        # dome, the drum floating over a gap from the 26.3 m body, the 2026-11-14 in-game check)
        from polygon import Path
        keep = {id(p) for p in tall}
        grew = True
        while grew:
            grew = False
            for p in known:
                if id(p) in keep or _part_top(p) <= body:
                    continue
                path = None
                for q in known:
                    if id(q) not in keep or abs(q[1][1] - _part_top(p)) > 1.0:
                        continue
                    if path is None:
                        path = Path(np.asarray(p[0], np.float64)[:, :2])
                    if bool(path.contains_points(np.asarray(q[0], np.float64)[:, :2].mean(0)[None, :])[0]):
                        keep.add(id(p))
                        grew = True
                        break
        return [p for p in known if id(p) in keep], body
    h = float(height) if height is not None and np.isfinite(height) else 0.0
    if h > max(SKY_BODY_RISE * body, body + SKY_BODY_OVER_M):
        return [], body
    return [], None


def parts_masonry_colour(bparts, at):
    """A building's stone colour for its parts that carry none (plan's MASONRY_*): where the facade colours it and
    its parts carry are, by the parts' area, mostly stone's, the largest such part's (else the building's); None where
    any carries a curtain wall's material (MASONRY_NOT: a masonry one on some, the Empire State Building's concrete
    base, agrees), or none a colour."""
    mats = [str(at.get("facade_material") or "").lower()] + [str(p[2].get("facade_material") or "").lower()
                                                             for p in bparts if len(p) > 2]
    if any(m in MASONRY_NOT for m in mats):
        return None
    stone = other = 0.0
    best, best_a = None, -1.0
    for p in bparts:                            # (in the parts' order: the same sums everywhere)
        c = parse_color(p[2].get("facade_color")) if len(p) > 2 else None
        if c is None:
            continue
        a = abs(_area2(_local(p[0]))) / 2
        if masonry_colour(c):
            stone += a
            if a > best_a:
                best, best_a = c, a
        else:
            other += a
    bc = parse_color(at.get("facade_color"))
    if bc is not None and best is None:
        return bc if masonry_colour(bc) and other == 0.0 else None
    return best if best is not None and stone >= other else None


def steeple_parts(bparts, height=None):
    """(indices in bparts of a church's tower parts that get a spire of ours, the spire's rise or None for the default)
    : the top of the tallest compact column (STEEPLE_TOWER_*), a twin of it too (STEEPLE_TWIN); ((), None) where there
    is none or the map tops it itself. The rise: up to the building's own height where that stands over the tower
    (STEEPLE_OUTLINE_OVER_M), else None (STEEPLE_TOP_RISE times the tower's short side). bparts: tile_buildings' [(ring,
    (own height, bottom) or None, own attrs)], in their order (ties to the first: the same choice everywhere)."""
    from polygon import Path
    known = [k for k, p in enumerate(bparts) if p[1] is not None]
    rings = {k: np.asarray(bparts[k][0], np.float64)[:, :2] for k in known}
    areas = {k: abs(_area2(_local(rings[k]))) / 2 for k in known}
    paths = {}

    def inside(k, pt):
        if k not in paths:
            paths[k] = Path(rings[k])
        return bool(paths[k].contains_points(np.asarray(pt, np.float64)[None, :])[0])

    def roof(k):
        own = bparts[k][2] if len(bparts[k]) > 2 else {}
        return str(own.get("roof_shape") or "flat").lower() or "flat"

    def on_top(k):
        """The parts standing on part k covering STEEPLE_SPIRE_SHARE of it (their bottom at its top, centre in it)."""
        t = _part_top(bparts[k])
        return [j for j in known if j != k and abs(bparts[j][1][1] - t) <= 1.0
                and areas[j] >= STEEPLE_SPIRE_SHARE * areas[k] and inside(k, rings[j].mean(0))]

    cands = []
    for k in known:
        if roof(k) != "flat" or not STEEPLE_TOWER_M2[0] <= areas[k] <= STEEPLE_TOWER_M2[1]:
            continue
        _, _, hl, hw = oriented_box(rings[k])
        if hl > STEEPLE_TOWER_ASPECT * max(hw, 1e-6):
            continue
        cands.append(k)
    if not cands:
        return (), None
    best = None
    for k in cands:
        if best is None or _part_top(bparts[k]) > _part_top(bparts[best]) + 1e-9:
            best = k
    tops = [best] + [k for k in cands if k != best and abs(_part_top(bparts[k]) - _part_top(bparts[best]))
                     <= STEEPLE_TWIN[0] and abs(areas[k] - areas[best]) <= STEEPLE_TWIN[1] * areas[best]]
    t = _part_top(bparts[best])
    for k in tops:
        if on_top(k):               # (its own spire, dome or flat top mapped: the map's, or a higher column's)
            return (), None
    rest = [_part_top(bparts[j]) for j in known
            if j not in tops and not any(inside(k, rings[j].mean(0)) and areas[j] <= areas[k] * 1.05 + 1.0
                                         for k in tops)]       # (not the column under a tower)
    over = t - max(rest) if rest else t
    h = float(height) if height is not None and np.isfinite(height) else 0.0
    spire = h >= t + STEEPLE_OUTLINE_OVER_M
    if over < STEEPLE_TOWER_RISE_M and not (spire and over >= 1.0):
        return (), None
    return tuple(tops), (h - t if spire else None)


# A building is drawn as its parts (OSM's Simple 3D Buildings: the outline is not rendered where parts exist), but its
# parts must cover it: Ülemiste keskus, Tallinn (GitHub #608), a 51,000 m² mall mapped with height 10, has a single
# 452 m² part (height 5), and was drawn as that part alone, the rest of it bare ground. Of 409 buildings drawn as their
# parts in a player's cache (Tallinn, Baltimore, ...), 63 had parts over less than half their outline. Under
# PARTS_COVER_MIN the outline is drawn as well, the parts standing on it (one lower stays inside it), when it has a
# height or levels of its own (PARTS_COVER_MIN_H) and every part stands on the ground: a part off the ground (min_height)
# is a structure modelled in parts, its gaps open on purpose (a tower's legs, an arcade: its outline is its overall box).
PARTS_COVER_ON = os.environ.get("BNE_PARTS_COVER", "on") != "off"
PARTS_COVER_MIN = 0.5           # share of the outline the parts cover (grid samples inside it), below which it is drawn
PARTS_COVER_SAMPLES = 400       # about this many samples inside an outline
PARTS_COVER_MIN_H = 2.5         # m: an outline mapped lower than this is a base or an area, not drawn under its parts
PARTS_GROUND_M = 0.5            # m: a part whose bottom is higher than this is off the ground


def parts_cover(outline, part_rings):
    """The share of outline (k, 2) that any of part_rings [(m, 2)] covers (same frame), from about
    PARTS_COVER_SAMPLES grid points inside it; 1.0 for an outline too small to sample. Pure."""
    from polygon import Path
    o = np.asarray(outline, np.float64)[:, :2]
    area = abs(_area2(o)) / 2.0
    lo, hi = o.min(0), o.max(0)
    step = max(math.sqrt(area / PARTS_COVER_SAMPLES), 0.25)
    gx, gy = np.meshgrid(np.arange(lo[0] + step / 2, hi[0], step), np.arange(lo[1] + step / 2, hi[1], step))
    g = np.stack([gx.ravel(), gy.ravel()], 1)
    g = g[Path(o).contains_points(g)] if len(g) else g
    if not len(g):
        return 1.0
    hit = np.zeros(len(g), bool)
    for r in part_rings:
        hit |= np.asarray(Path(np.asarray(r, np.float64)[:, :2]).contains_points(g), bool)
    return float(hit.mean())


def _outline_under_parts(at, bparts):
    """May a building drawn as its parts (bparts: [(ring, (own height, bottom) or None, ...)]) have its outline drawn
    under them where they cover too little of it: its own height (or levels) mapped, PARTS_COVER_MIN_H or more, and
    no part off the ground."""
    h, fl = at.get("height"), at.get("num_floors")
    own = (h is not None and np.isfinite(h) and h >= PARTS_COVER_MIN_H) or \
        (h is None or not np.isfinite(h)) and fl is not None and np.isfinite(fl) and fl >= 1
    return bool(own) and all(p[1] is None or p[1][1] <= PARTS_GROUND_M for p in bparts)


@profile_step('Building site selection')
def _tank_items(frame, x0, y0, x1, y1, elev, tanks, used, own=None):
    """[(Spec, gmin, gmax)] of the mapped tanks (tank_rings) no building stands over, whose centre is the tile's."""
    out = []
    for t_, (ring, at) in enumerate(tanks):
        if t_ in used:
            continue
        r = np.asarray(ring, np.float64)[:, :2]
        if len(r) > 3 and np.allclose(r[0], r[-1]):
            r = r[:-1]
        c = r.mean(0) + [x0, y0]
        mine = bool(np.asarray(own.owns(np.array([c[0]]), np.array([c[1]])), bool)[0]) if own is not None \
            else (x0 <= c[0] < x1 and y0 <= c[1] < y1)
        if not mine:
            continue
        la, lo, _ = frame.enu_to_geodetic(r[:, 0] + x0, r[:, 1] + y0, np.zeros(len(r)))
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        g = np.asarray(elev(la, lo), np.float64)
        if not np.isfinite(g).all():
            continue
        up = np.asarray(frame.geodetic_to_enu(la, lo, g)[2], np.float64)
        spec = plan(r, dict(at), 0, float(la.mean()), float(lo.mean()))
        if spec.cls != "tank":          # (not round: a tank of another shape is left to the map's buildings)
            continue
        spec.min_h = 0.0
        spec.gring = up
        out.append((spec, float(up.min()), float(up.max())))
    return out


SMALL_MARGIN = 0.5         # _small_rows' slack: under half the threshold's height and area (its sums are not the loop's)


def _small_rows(f, frame, a, min_size):
    """(n,) bool: the buildings of layer f (attrs a) surely under min_size (min height m, min footprint m2), as
    tile_buildings' test has it (the height, else the floors x 3.2, else 0; the outer ring's area in frame), with
    SMALL_MARGIN of slack: the whole layer at once (the skyline's tiles tested a city's every house one by one: most of
    their time). All False where its columns are not plain floats."""
    n = len(f.kind)
    none = np.zeros(n, bool)

    def col(k):
        c = a.get(k)
        if c is None:
            return np.full(n, np.nan)
        c = np.asarray(c)
        return c.astype(np.float64) if c.dtype.kind == "f" else None
    hc, fc = col("height"), col("num_floors")
    if hc is None or fc is None or not n or len(f.lonlat) < 2:
        return none
    h = np.where(np.isfinite(hc), hc, np.where(np.isfinite(fc), fc * 3.2, 0.0))
    p0 = f.part_off[:-1]
    ok = f.part_off[1:] > p0
    r0 = f.ring_off[np.minimum(p0, len(f.ring_off) - 2)]
    ok &= f.ring_off[np.minimum(p0 + 1, len(f.ring_off) - 1)] > r0
    c0 = f.coord_off[np.minimum(r0, len(f.coord_off) - 2)]
    c1 = f.coord_off[np.minimum(r0 + 1, len(f.coord_off) - 1)]
    ok &= (c1 - c0) >= 4
    xy = f.xy_in(frame)
    t = xy[:-1, 0] * xy[1:, 1] - xy[1:, 0] * xy[:-1, 1]
    cs = np.r_[0.0, np.cumsum(t)]
    area = np.abs(cs[np.where(ok, c1 - 1, 0)] - cs[np.where(ok, c0, 0)]) / 2
    return ok & (h < SMALL_MARGIN * min_size[0]) & (area < SMALL_MARGIN * min_size[1])


def tile_buildings(frame, x0, y0, x1, y1, layers, elev, water=None, part_layers=(), west_town=False, roads_xy=None,
                   tunnels_xy=None, garages_xy=None, supports=None, bridges_xy=None, water_xy=None, decks=None,
                   ml_roads_xy=None, ways_xy=None, skip=frozenset(), min_size=None, own=None, house_land_layers=(),
                   passage_xy=None, rails_xy=None, roofs=None, photo=None, bag3d=None, tanks=None,
                   industrial=None, parts_min_top=None, nyc=None, carriageways=None, clear_stats=None):
    """[(Spec, gmin, gmax)] of the buildings whose footprint centroid is in the tile [x0, x1) x [y0, y1) (own: the
    tile's box when it says which points are its own, roads.box_owns: a geotile's)
    (anchor frame), rings relative to (x0, y0). layers: mapgeom.Features of buildings (one per map cell; a
    building in two cells is taken once, by id); elev(lat, lon) the physics ground. part_layers: the
    building parts of the same cells: a building whose parts all have heights is drawn as its parts
    (each from its bottom to its top, the building's look). garages_xy: garage_rings (a building one covers, or
    covering one's centre, is a parking structure). supports: support_rings (a building or part on one is the
    bridge's painted steel). bridges_xy / water_xy (bridge_lines, water_rings): a machine-learned footprint (not
    OSM) on a road bridge, over water or on a public road (roads_xy, or ml_roads_xy for that test alone: the far
    buildings) is a deck, a boat or a vehicle, left out (_ml_false). skip: ids of buildings left out (a gas station's
    canopy, drawn by the mod from the stock levels' props: gasstations.tile_sites). min_size (min height m, min
    footprint m²): only buildings at least that tall or that large (the skyline tiles: skyline_tile), tested before
    any other work on a building. roofs: lod2roofs.Records, the measured roofs that fill what the map leaves out.
    nyc: nycmassing.Fetch, New York's measured massing: a building without parts of its own that matches a record
    takes the record's parts (its setbacks), drawn as the map's parts are. carriageways: carriageway_segments' roads,
    the footprints kept off them (road_clearance; clear_stats its counts)."""
    parts_of = _PartsOf(frame, part_layers)     # building id -> [(ring in the anchor frame, (own height, bottom))]
    nyc_idx = nyc.index() if nyc is not None else None
    seen = set()
    turbines = None                             # (turbine_points, looked up at the first building)
    # the decks' and the tunnels' floors at their heights (decks): a building such a floor runs through gets its way at
    # that height (_passages), not the portal lift over a tunnel's line (a building passage at the street's level,
    # mapped as a short tunnel, floated the whole building 7.5 m over the street: London's mews arches, 2026-11-12)
    deck_lines = [d[0] for d in decks] if decks else []
    tun_covered, tun_passages = None, ([], [])      # (the covered tunnels' lines; the open ones' (segments, half widths))
    if tunnels_xy is not None and not hasattr(tunnels_xy, "half"):
        tunnels_xy = RoadLines(tunnels_xy)          # (a plain list of lines: no widths, ROAD_PASSAGE_W)
    rings, attrs, looks, mins = [], [], [], []
    for f in layers:
        if f is None or len(f.kind) == 0:
            continue
        a = f.attrs
        ids = getattr(f, "ids", None)
        rows = range(len(f.kind))
        if isinstance(f, mapgeom.Features):     # only the buildings whose centre may be in the tile (the exact test below)
            cx_, cy_ = f.outer_centres(frame)
            m = CENTRE_PREFILTER_M
            near_ = (cx_ >= x0 - m) & (cx_ < x1 + m) & (cy_ >= y0 - m) & (cy_ < y1 + m)
            if min_size is None:                # ... and the long ones reaching into it (its pieces: ground_pieces)
                bb_ = f.boxes(frame)
                pm = PIECE_LEN_M
                near_ |= (dnp.hypot(bb_[:, 2] - bb_[:, 0], bb_[:, 3] - bb_[:, 1]) >= SPLIT_MIN_L) \
                    & (bb_[:, 2] >= x0 - pm) & (bb_[:, 0] < x1 + pm) & (bb_[:, 3] >= y0 - pm) & (bb_[:, 1] < y1 + pm)
            if min_size is not None:            # ... and not the ones surely under min_size (_small_rows)
                near_ &= ~_small_rows(f, frame, a, min_size)
            rows = np.flatnonzero(near_).tolist()
        # (New York's measured massing: the rows whose outline takes a record, nycmassing.Records)
        nyc_rows = nyc_idx.match_layer(f, frame, rows) if nyc_idx is not None and isinstance(f, mapgeom.Features) \
            else None
        for i in rows:
            if a.get("is_underground") is not None and bool(a["is_underground"][i]):
                continue
            lv = a["level"][i] if a.get("level") is not None else None
            if lv is not None and np.isfinite(lv) and lv < 0:
                continue                # below ground (Shot Tower Metro, level -2, stood across a Baltimore street)
            key = ids[i] if ids is not None else None
            if key is not None and (key in seen or key in skip):
                continue
            parts = f.parts(i)
            if not parts or not parts[0]:
                continue
            outer = np.asarray(parts[0][0], np.float64)
            if len(outer) < 4:
                continue
            e, n = mapgeom.enu_xy(f, frame, outer)
            k = len(e) - 1             # the same reduction as np.mean, without its wrapper (36k calls a tile set)
            cx, cy = float(e[:-1].sum() / k), float(n[:-1].sum() / k)
            edge = not (x0 <= cx < x1 and y0 <= cy < y1) if own is None else not own.owns(cx, cy)
            if edge and (min_size is not None or float(dnp.hypot(np.ptp(e), np.ptp(n))) < SPLIT_MIN_L
                         or (a.get("src_record") is not None and landmarks.of(a["src_record"][i]) is not None)
                         or (key is not None and parts_of.get(str(key)))
                         or (nyc_rows and i in nyc_rows and nycmassing.takes(a, i))):
                continue                # (its centre's tile's: only a long one's pieces may be this tile's)
            if min_size is not None:
                h_ = a["height"][i] if a.get("height") is not None else None
                if h_ is None or not np.isfinite(h_):
                    fl_ = a["num_floors"][i] if a.get("num_floors") is not None else None
                    h_ = float(fl_) * 3.2 if fl_ is not None and np.isfinite(fl_) else 0.0
                if h_ < min_size[0] and abs(float(dnp.dot(e[:-1], n[1:]) - dnp.dot(e[1:], n[:-1]))) / 2 < min_size[1]:
                    continue
            if turbines is None:
                turbines = turbine_points(frame, x0, y0, x1, y1)
            if turbines and turbine_base(e, n, turbines):
                continue                # (a turbine's base: TURBINE_BASE_M2)
            at = {k: (a[k][i] if k in a else None) for k in
                  ("class", "subtype", "height", "num_floors", "min_height", "roof_shape", "roof_height", "name",
                   "facade_material", "facade_color", "roof_material", "roof_color", "roof_orientation",
                   "roof_direction", "height_src", "roof_rise")}
            if key is not None:         # (plan's random choices keyed on the feature, not where the anchor puts it)
                at["seed_id"] = str(key)
            if roofs is not None:
                roofs.fill(at, e, n)
            lm = landmarks.of(a["src_record"][i]) if a.get("src_record") is not None else None
            if lm is not None:          # a landmark (landmarks.py): its own model on its footprint, its parts left out
                ring = clean_ring(np.stack([e - x0, n - y0], 1))
                if ring is not None:
                    rings.append(ring)
                    attrs.append({"landmark": lm, "height": landmarks.height(lm)})
                    looks.append(None)
                    mins.append(0.0)
                    if key is not None:
                        seen.add(key)
                continue
            hh_ = at.get("height")
            if hh_ is not None and np.isfinite(hh_) and 0 < hh_ < FLAT_FOOTPRINT_M and not parts_of.get(str(key)):
                continue                # a mapped area, not a building (the Golden Gate toll plaza's 0.01 m footprint
                                        # drew a slab with a rim round the booths, 2026-10-21)
            bparts = parts_of.get(str(key)) if key is not None else None
            nyc_group = None
            if not bparts and nyc_rows and i in nyc_rows and nycmassing.takes(a, i):
                # (no parts of its own: New York's measured setbacks, a band per roof level, its bulkheads too; its
                # water tank is the procedural one on the top part, WATER_TANK_TOP_M: the model has none to place)
                bparts = nyc_idx.parts(nyc_rows[i]) or None
                nyc_group = ("nyc", key if key is not None else (id(f), i))
            ship_parts = None
            if bparts and "src_dataset" in a and str(a["src_dataset"][i]) == "OpenStreetMap":
                # a ship mapped with parts (the Iowa's turrets, the Queen Mary's funnels): the hull, its parts on deck
                ring_ = clean_ring(np.stack([e - x0, n - y0], 1))
                if ring_ is not None and ships.is_ship(ring_, at, lambda: ships.on_water(ring_.mean(0), water_xy)):
                    ship_parts = []     # (its ring, (own height, bottom) or None)
                    for p in bparts:
                        pr_ = clean_ring(p[0] - np.array([x0, y0]))
                        if pr_ is not None:
                            ship_parts.append((pr_, p[1]))
                    bparts = None
            outline_h = None
            if bparts and all(p[1] is None for p in bparts):
                # parts without heights (a dome's, a tower's) on a building whose height is theirs, the levels its
                # body's: the parts up to the height, the outline at its levels (the Maryland State House, 54 m to the
                # lantern over 3 storeys, came out a 54 m block, 2026-11-07)
                bh, nf = at.get("height"), at.get("num_floors")
                if bh is not None and np.isfinite(bh) and nf is not None and np.isfinite(nf) and nf >= 1 \
                        and (nf + 1) * FLOOR_M < bh \
                        and parts_cover(np.stack([e, n], 1), [p[0] for p in bparts]) < PARTS_COVER_MIN:
                    bparts = [(p[0], (float(bh), 0.0)) + tuple(p[2:]) for p in bparts]
                    outline_h = float(nf) * FLOOR_M
            sky_body_h, parts_all = None, bparts
            if bparts and parts_min_top is not None and max(
                    [p[1][0] + p[1][1] for p in bparts if p[1] is not None] + [float(at.get("height") or 0.0)]) \
                    < parts_min_top:
                # (the skyline: a low building's parts add nothing seen from afar, SKY_PARTS_MIN_M, but a body with a
                # dome, a tower or a spire rising over it is drawn as that body and those parts: sky_body)
                bparts, sky_body_h = sky_body(bparts, at.get("height"))
                if not bparts and sky_body_h is not None:   # (no part over it: the outline at its parts' height)
                    at = dict(at, height=sky_body_h, roof_height=None, roof_shape="flat", _parts_drawn=True)
                    sky_body_h = None
            if bparts and any(p[1] is not None for p in bparts):
                # drawn as its parts, with the building's class and one look for all of them; a part
                # without a height takes the building's (else its lowest known part's)
                look = _hash01(str(key), "look")
                bh = at.get("height")
                fill = float(bh) if bh is not None and np.isfinite(bh) and bh > 0 else \
                    min(p[1][0] + p[1][1] for p in bparts if p[1] is not None)
                top = max([fill] + [p[1][0] + p[1][1] for p in bparts if p[1] is not None])
                # (a part with no material or colour of its own takes its siblings' most common: half of New York by
                # Gehry's parts are "metal", the others mapped bare; of equally common ones the first part's: a set's
                # order is the process's string hashing, and a tile's buildings changed from run to run, 2026-11-12)
                sib = {}
                for k_ in ("facade_material", "facade_color"):
                    vals = [str(p[2].get(k_)) for p in parts_all if len(p) > 2 and p[2].get(k_)]
                    # (... when at least half the parts carry one: the Empire State Building's 17 m base part, mapped
                    # concrete, made its whole 443 m tower bridge concrete, and the Capitol's two bronze parts dyed its
                    # marble, streets.gl comparison 2026-11-14)
                    sib[k_] = max(dict.fromkeys(vals), key=vals.count) if vals and 2 * len(vals) >= len(parts_all) \
                        else None
                masonry_c = parts_masonry_colour(parts_all, at)
                steeple_k, steeple_rise = steeple_parts(bparts, at.get("height")) \
                    if str(at.get("class") or "").lower() in CHURCH_CLASSES else ((), None)
                for pk, ppart in enumerate(bparts):
                    pring, hh = ppart[0], ppart[1]
                    pat = ppart[2] if len(ppart) > 2 else {}
                    ph, pmh = hh if hh is not None else (fill, 0.0)
                    if ph + pmh < FLAT_FOOTPRINT_M:
                        continue        # (a part taking a flat area's height: FLAT_FOOTPRINT_M)
                    r = clean_ring(pring - np.array([x0, y0]))
                    # (small parts are left out, but not a tall slender one: a spire's needle, a statue on a dome; the
                    # Chrysler Building's top 47 m on 0.8 m²)
                    if r is None or _area2(r) / 2 < (PART_TINY_M2 if ph >= PART_TINY_TALL_M else PART_MIN_M2):
                        continue
                    rings.append(r)
                    p_rshape = str(pat.get("roof_shape") or "").lower() or "flat"
                    p_fcol = pat.get("facade_color") or at.get("facade_color") or sib["facade_color"]
                    p_rcol = pat.get("roof_color") or at.get("roof_color")
                    attrs.append({"class": at["class"], "height": ph, "num_floors": None,
                                  "roof_shape": p_rshape,
                                  "building_height": top, "_part_min": pmh, "_masonry_colour": masonry_c,
                                  "_steeple_part": (steeple_rise or True) if pk in steeple_k else False,
                                  "facade_material": pat.get("facade_material") or at.get("facade_material")
                                  or sib["facade_material"],
                                  "facade_color": p_fcol,
                                  "roof_material": pat.get("roof_material") or at.get("roof_material"),
                                  "roof_height": pat.get("roof_height"),
                                  "roof_direction": pat.get("roof_direction"),
                                  "roof_orientation": pat.get("roof_orientation"),
                                  "roof_color": p_rcol,
                                  "bridge_part": _on_support(r, supports) if str(at.get("facade_material") or "")
                                  .lower() in STEEL_MATERIALS else None,
                                  "bridge_masonry": str(at.get("facade_material") or "").lower()
                                  if _on_support(r, supports) is not None and str(at.get("facade_material") or "")
                                  .lower() not in STEEL_MATERIALS else None})
                    if nyc_group is not None:
                        attrs[-1]["_photo_group"] = nyc_group
                    looks.append(look)
                    mins.append(pmh)
                if key is not None:
                    seen.add(key)
                # (the outline kept with its parts is no building of its own: no steeple of ours on it, its parts
                # have their tower's, steeple_part; Codex review of a92a4828)
                if sky_body_h is not None:      # (the skyline's body under its tall parts: sky_body)
                    at = dict(at, height=sky_body_h, roof_height=None, roof_shape="flat", _parts_drawn=True)
                elif not (PARTS_COVER_ON and _outline_under_parts(at, bparts)
                          and parts_cover(np.stack([e, n], 1), [p[0] for p in bparts]) < PARTS_COVER_MIN):
                    continue
                # (parts over too little of it: the outline too, as a building of its own: PARTS_COVER_MIN)
                elif outline_h is not None:
                    at = dict(at, height=outline_h, roof_height=None, _parts_drawn=True)
                else:
                    at = dict(at, _parts_drawn=True)
            ring = clean_ring(np.stack([e - x0, n - y0], 1))
            if ring is None or _area2(ring) / 2 < MIN_AREA_M2:
                continue
            if rails_xy and on_rail_bed(ring, rails_xy):        # (a building on a railway's bed: BNE_RAIL_XING)
                continue
            is_ml = "src_dataset" in a and str(a["src_dataset"][i]) != "OpenStreetMap"
            if is_ml and _ml_false(ring, bridges_xy, water_xy, ml_roads_xy if ml_roads_xy is not None else roads_xy):
                continue
            if key is not None:
                seen.add(key)
            mh = at.get("min_height")
            mh = float(mh) if mh is not None and np.isfinite(mh) and mh > 0 else 0.0
            if mh <= 0.5 and roads_xy and _area2(ring) / 2 >= SITE_M2 and road_through(ring, roads_xy) >= ROAD_THROUGH_M:
                continue                # a mapped site, not a building (SITE_M2)
            flat = str(at.get("roof_shape") or "flat").lower() == "flat"
            pieces, drop = ([ring], 0.0) if is_ml or mh > 0.5 or not flat or str(at.get("class") or "") in \
                BRIDGE_CLASSES else ground_pieces(frame, ring, x0, y0, elev)
            if len(pieces) > 1:         # the pieces whose centre is this tile's (each piece in one tile)
                pc_ = np.array([r_.mean(0) for r_ in pieces]) + [x0, y0]     # (clean rings: no closing point)
                mine = (pc_[:, 0] >= x0) & (pc_[:, 0] < x1) & (pc_[:, 1] >= y0) & (pc_[:, 1] < y1) if own is None \
                    else np.asarray(own.owns(pc_[:, 0], pc_[:, 1]), bool)
                kept = [r_ for r_, k_ in zip(pieces, np.atleast_1d(mine).tolist()) if k_]
            else:
                kept = [] if edge else pieces
            h_ = at.get("height")
            if len(pieces) > 1 and h_ is not None and np.isfinite(h_) and h_ > 0:
                # its mapped height is from its lowest ground to its top (OSM's height, a lidar's top less the ground):
                # the slope along it is in it, and each piece stands on its own ground (the gallery's 18.3 m was
                # ~3 m of building over a 15.2 m grade: its pieces stood 9 m over I-280's deck, 2026-11-07)
                at = dict(at, height=max(FLOOR_M, float(h_) - drop))
            lv_ = a["level"][i] if a.get("level") is not None else None
            through_m = ROAD_THROUGH_M if lv_ is None or not np.isfinite(lv_) or lv_ < 1 else LEVEL_THROUGH_M
            if len(pieces) == 1 and len(parts[0]) > 1:
                # (its courtyards: the inner rings, research 2026-11-10: only the outer ring was drawn, a Berlin block
                # solid over its yard)
                holes_ = []
                for inner in parts[0][1:]:
                    if len(inner) < 4:
                        continue
                    he, hn = mapgeom.enu_xy(f, frame, np.asarray(inner, np.float64))
                    hr = clean_ring(np.stack([he - x0, hn - y0], 1))
                    if hr is not None and abs(_area2(hr)) / 2 >= COURTYARD_MIN_M2:
                        holes_.append(hr if _area2(hr) < 0 else hr[::-1])       # (clockwise: walls face the yard)
                if holes_:
                    at = dict(at, _holes=holes_)
            at0, mh0 = at, mh
            for ring in kept:           # (a long one on varied ground: its pieces, each on its own ground)
                at, mh = at0, mh0
                rings.append(ring)
                if mh <= 0.5 and roads_xy and (road_through(ring, roads_xy) >= through_m or road_across(ring, roads_xy)):
                    mh = OVER_ROAD_CLEAR_M
                    h = at.get("height")
                    if h is None or not np.isfinite(h) or h < mh + 3.0:
                        at = dict(at, height=mh + 3.0)      # keeps its top; at least a slab's worth over the clearance
                if mh < OVER_TUNNEL_CLEAR_M and tunnels_xy and road_through(ring, tunnels_xy) >= TUNNEL_THROUGH_M \
                        and not (deck_lines and road_through(ring, deck_lines) >= TUNNEL_THROUGH_M):
                    # over a tunnel: its floor OVER_TUNNEL_CLEAR_M over the road at the mouth near it (the lowest ground
                    # along the tunnel's line round it), from its own lowest ground (base_z); none where its ground is
                    # that high already (a house on the hill over the tube: Wabash's floated ~10 m, 2026-10-17). Over
                    # the covered ones only: an open one (a building passage) is the street's, the building stands
                    # beside its way (road_clearance's passages)
                    if tun_covered is None:
                        tun_open = _open_tunnels(frame, tunnels_xy, x0, y0, elev)
                        tun_covered = [ln for ln, o in zip(tunnels_xy, tun_open) if not o]
                        for ln, hf, o in zip(tunnels_xy, tunnels_xy.half or [ROAD_PASSAGE_W / 2] * len(tunnels_xy),
                                             tun_open):
                            if o and len(ln) >= 2:
                                ln = np.asarray(ln, np.float64)
                                tun_passages[0].append(np.concatenate([ln[:-1], ln[1:]], 1))
                                tun_passages[1].append(np.full(len(ln) - 1, float(hf)))
                    lift = 0.0
                    if tun_covered and road_through(ring, tun_covered) >= TUNNEL_THROUGH_M:
                        lift = _portal_lift(frame, ring, x0, y0, tun_covered, elev)
                    if lift > 0.5:
                        mh = lift
                        at = dict(at, portal=True)
                        h = at.get("height")
                        if h is None or not np.isfinite(h) or h < mh + 3.0:
                            at = dict(at, height=mh + 3.0)
                if mh <= 0.5 and garages_xy and _covers_garage(ring, garages_xy):
                    at = dict(at, garage=True)
                on = _on_support(ring, supports)
                if on is not None:              # a bridge's own: no flats' windows (Tower Bridge's towers, the user 2026-10-18)
                    fm_ = str(at.get("facade_material") or "").lower()
                    at = dict(at, bridge_part=on) if fm_ in STEEL_MATERIALS else dict(at, bridge_masonry=fm_)
                if is_ml:
                    at = dict(at, ml=True)          # (_ml_steep once its ground is known)
                elif on is None and mh <= 0.5 and (ship_parts is not None or ships.is_ship(
                        ring, at, lambda: ships.on_water(ring.mean(0), water_xy))):     # (the water test asked last)
                    at = dict(at, ship=True, ship_parts=ship_parts)
                attrs.append(at)
                looks.append(None)
                mins.append(mh)
    if not rings:
        return []
    # a quay structure alongside a moored ship: a low deck (its scanned height is the ship's flank). The ship may be
    # the next tile's: those are looked for only when this tile has water and a long thin candidate
    piers = [k_ for k_, (r, at) in enumerate(zip(rings, attrs)) if not at.get("ship") and not at.get("ml")
             and looks[k_] is None and mins[k_] <= 0.5 and ships.pier_shaped(r)] if water_xy else []
    if piers:
        hulls = [r for r, at in zip(rings, attrs) if at.get("ship")] +             _hulls_nearby(frame, layers, x0, y0, x1, y1, water_xy)
        for k_ in piers:
            if any(ships.pier_alongside(rings[k_], hr) for hr in hulls):
                attrs[k_] = dict(attrs[k_], pier=True)
    # what lies mostly on a ship's hull is the ship's (a second outline of it, machine-learned or a second OSM way): not
    # a building as well. Its hull may be the next tile's (those tagged a ship: asking the water for every tile by the
    # water cost a waterfront tile ~10 %)
    if any(not at.get("ship") and not at.get("pier") for at in attrs):
        hulls = [r for r, at in zip(rings, attrs) if at.get("ship")] + _hulls_nearby(frame, layers, x0, y0, x1, y1, None)
        if hulls:
            drop = {k_ for k_, (r, at) in enumerate(zip(rings, attrs)) if not at.get("ship") and not at.get("pier")
                    and any(ships.on_hull(r, hr) for hr in hulls)}
            if drop:
                keep = [k_ for k_ in range(len(rings)) if k_ not in drop]
                rings, attrs = [rings[k_] for k_ in keep], [attrs[k_] for k_ in keep]
                looks, mins = [looks[k_] for k_ in keep], [mins[k_] for k_ in keep]
                if not rings:
                    return []
    if len(rings) > 1:                  # the same building mapped twice: one of them (footprint_twins)
        rings, attrs, looks, mins = _drop_twins(rings, attrs, looks, mins)
    if bridges_xy or decks:             # a road bridge through a building: a passage (Tower Bridge's towers and gate)
        def ground_at(r, lowest=False):  # (a footprint's highest ground, base_z's (its lowest for a portal building))
            r = np.atleast_2d(np.asarray(r, np.float64))[:, :2]
            la, lo, _ = frame.enu_to_geodetic(r[:, 0] + x0, r[:, 1] + y0, np.zeros(len(r)))
            la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
            z = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)
            return float(np.nanmin(z) if lowest else np.nanmax(z))     # (the decks' frame up)
        rings, attrs, looks, mins = _passages(rings, attrs, looks, mins, bridges_xy, decks, ground_at)
    if ROAD_PASSAGE_ON and passage_xy:     # a road mapped inside a building: a passage (a mall's covered way)
        rings, attrs, looks, mins = _road_passages(rings, attrs, looks, mins, passage_xy)
    shared = _shared_walls(rings)
    cover = sum(_area2(r) / 2 for r, mh in zip(rings, mins) if mh <= 0.5) / ((x1 - x0) * (y1 - y0))
    town, dense = cover < TOWN_COVERAGE, cover > BRICK_WALK_COVERAGE
    clat, clon, _ = frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.0)
    clat, clon = float(clat), float(clon)
    alpine = False
    if cover < ALPINE_COVER:
        try:
            gz = float(np.asarray(elev(np.array([clat]), np.array([clon])), np.float64)[0])
            alpine = np.isfinite(gz) and gz > tree_line_m(clat)
        except Exception:
            alpine = False
    # the ground at every building's corners in one call (elev and the frame's conversions are point by point; a
    # call per building cost a far buildings tile ~20 % of its time, 2026-10-20)
    ends = np.cumsum([len(r) for r in rings])
    allr = np.concatenate(rings)
    la_all, lo_all, _ = frame.enu_to_geodetic(allr[:, 0] + x0, allr[:, 1] + y0, np.zeros(len(allr)))
    la_all, lo_all = np.asarray(la_all, np.float64), np.asarray(lo_all, np.float64)
    g_all = np.asarray(elev(la_all, lo_all), np.float64)
    ok = np.isfinite(g_all)
    # heights above the ellipsoid -> the anchor frame's up (the Earth's curvature, as the terrain and the roads):
    # without it a building stood (d^2 / 2R) over the ground at d from the anchor's origin, 5 m at 8 km, up to
    # 18 m at the 15 km rebase distance (the user, 2026-10-18 at Santa Maria Val Muestair: floating houses)
    up_all = np.full(len(allr), np.nan)
    if ok.any():
        up_all[ok] = np.asarray(frame.geodetic_to_enu(la_all[ok], lo_all[ok], g_all[ok])[2], np.float64)
    out = []
    house_land = francehouses.ResidentialLand(house_land_layers) if francehouses.ON and francehouses.INFER else None
    house_res = None
    if house_land is not None and len(rings):      # (every building's centre at once: ResidentialLand.at_many)
        starts = np.concatenate(([0], np.asarray(ends[:-1], np.int64)))
        cnt = np.maximum(np.asarray(ends, np.int64) - starts, 1)
        house_res = house_land.at_many(np.add.reduceat(la_all, starts) / cnt, np.add.reduceat(lo_all, starts) / cnt) \
            if len(la_all) else np.zeros(len(rings), bool)
    # (the roofs' colours in the tile's cached aerial photo, RoofPhoto: all at once)
    # (a measured massing's bands share the colour of the building's first, its lowest: the bands are stacked, each
    # ring over the whole of the ones above it, and sampling each cost the photo's pass twice the pixels, 2026-11-10)
    first_of = {}
    pick = [k_ for k_, at in enumerate(attrs) if at.get("_photo_group") is None
            or first_of.setdefault(at["_photo_group"], k_) == k_]
    roof_rgb = photo.colours([np.asarray(rings[k_], np.float64)[:, :2] + [x0, y0] for k_ in pick]) \
        if photo is not None and len(rings) else None
    if roof_rgb is not None and len(roof_rgb) != len(pick):
        roof_rgb = None
    if roof_rgb is not None and len(pick) != len(rings):
        at_ = {k_: j_ for j_, k_ in enumerate(pick)}
        roof_rgb = roof_rgb[[at_[k_ if at.get("_photo_group") is None else first_of[at["_photo_group"]]]
                             for k_, at in enumerate(attrs)]]
    # (the Netherlands' measured roofs, threedbag.Fetch: the tile's squares, asked for as the tile started)
    bag_match = bag3d.index() if bag3d is not None else None
    # (the mapped tanks, tank_rings: a building over one takes its class; the rest drawn after the loop)
    tank_paths = None
    if tanks:
        from polygon import Path
        tank_paths = [Path(np.asarray(r, np.float64)) for r, _a in tanks]
    tank_used = set()
    land_paths = None
    if industrial:
        from polygon import Path
        land_paths = [Path(np.asarray(r, np.float64)) for r in industrial]
    for k, (ring, at, sh, look, mh) in enumerate(zip(rings, attrs, shared, looks, mins)):
        if roof_rgb is not None and np.isfinite(roof_rgb[k]).all() and not at.get("roof_color"):
            at = dict(at, _roof_photo=tuple(float(v) for v in roof_rgb[k]))
        i0, i1 = (ends[k - 1] if k else 0), ends[k]
        if land_paths is not None:
            cxy = np.asarray(ring, np.float64)[:, :2].mean(0)
            if any(lp.contains_points(cxy[None])[0] for lp in land_paths):
                at = dict(at, _land="industrial")
        if tank_paths is not None and look is None and str(at.get("class") or "") not in TANK_CLASSES:
            cxy = np.asarray(ring, np.float64)[:, :2].mean(0)
            for t_, tp in enumerate(tank_paths):
                if tp.contains_points(cxy[None])[0]:
                    tank_used.add(t_)
                    t_at = tanks[t_][1]
                    at = dict(at, **{k_: v_ for k_, v_ in t_at.items()
                                     if k_ == "class" or at.get(k_) is None})
                    break
        if bag_match is not None and look is None and mh <= 0.5 and ok[i0:i1].all():
            import threedbag
            at = threedbag.apply(at, bag_match.match(float(la_all[i0:i1].mean()), float(lo_all[i0:i1].mean()),
                                                     abs(_area2(ring)) / 2, at.get("bag_id")))
        if mh > 0.5 and at.get("height") is not None and np.isfinite(at["height"]) and look is None:
            at = dict(at, height=max(2.5, float(at["height"]) - mh))     # a building's own height range
        if francehouses.ON:
            if not ok[i0:i1].all():
                continue
            hlat, hlon = float(la_all[i0:i1].mean()), float(lo_all[i0:i1].mean())
            at = dict(at, _fr_location=(hlat, hlon), _fr_altitude=float(np.nanmean(g_all[i0:i1])),
                      _fr_part=look is not None or mh > .5, _fr_neighbours=int(sh),
                      _fr_residential=bool(house_res[k]) if house_res is not None else False)
        spec = plan(ring, at, int(sh) if look is None else 0, clat, clon, look, town, dense, west_town, alpine)
        spec.min_h = mh
        # (a building drawn from its parts (the map's, New York's 3-D model's): its tank on its topmost part, the
        # building's storeys from its whole height; without, the setbacks took New York's tanks away)
        top_b = at.get("building_height")
        top_part = look is not None and top_b is not None \
            and float(top_b) - (mh + float(spec.height or 0.0)) < WATER_TANK_TOP_M     # (the main roof, under a bulkhead)
        spec.tank = ((mh <= 0.5 and look is None) or top_part) and water_tank(
            spec, clat, clon, abs(_area2(ring)) / 2, era(at.get("_year")),
            floors=int(round(float(top_b) / FLOOR_M)) if top_part else None)
        if at.get("_holes") and ((spec.roof == "flat" and not spec.hf) or spec.skel) and spec.cls not in CANOPY_CLASSES \
                and not spec.deck and not spec.fr_house and spec.cls != landmarks.LANDMARK:
            spec.holes = at["_holes"]
        spec.portal = bool(at.get("portal"))
        spec.ml = bool(at.get("ml"))
        i0, i1 = (ends[k - 1] if k else 0), ends[k]
        if not ok[i0:i1].all():
            continue
        g = up_all[i0:i1].copy()
        if at.get("ml") and ways_xy is not None and _ml_steep(ring, g, ways_xy):
            continue
        if water is not None and not at.get("ship"):
            c = ring.mean(0)
            cla, clo, _ = frame.enu_to_geodetic(c[0] + x0, c[1] + y0, 0.0)
            if water(float(cla), float(clo)):
                continue
        if decks and mh <= 0.5 and not (at.get("bridge_part") or at.get("bridge_masonry")):
            got = on_deck(ring, decks, float(g.max()))
            if got is not None:
                if got[0]:              # over the deck's lanes: a vehicle, a booth on the road
                    continue
                g = np.full(len(g), got[1])     # its floor on the deck (a pier's bait shop stood on the seabed)
        spec.gring = g              # the ground at each corner (door_ground: the door's, not the footprint's highest)
        out.append((spec, float(g.min()), float(g.max())))
    if tanks:
        out += _tank_items(frame, x0, y0, x1, y1, elev, tanks, tank_used, own)
    lines = roads_xy if roads_xy else ml_roads_xy
    if lines and out:
        isl = _toll_islands(frame, layers, parts_of, lines, x0, y0)
        if isl:
            out = _toll_mark(out, isl)
    if carriageways is not None or tun_passages[0]:
        passages = None
        if tun_passages[0]:
            passages = (np.ascontiguousarray(np.concatenate(tun_passages[0]), np.float64),
                        np.ascontiguousarray(np.concatenate(tun_passages[1]), np.float64))
        out = road_clearance(out, carriageways, frame, x0, y0, elev, clear_stats, passages=passages)
    return out


# Toll plazas (the user's report at the Golden Gate Bridge, 2026-10-21: "Toll booths need work. west coast has one,
# reference that"): OSM maps a plaza's islands as low "buildings" (0.2 m there) with the booths as building parts
# standing on them; drawn as buildings they were bare boxes on slabs. An island with a booth on it, along a road, is
# West Coast USA's toll booth instead (trees.lua group "tollbooth": the island, its booth and kerbs in one shape, 22 m
# long along its local y), placed along the island (toll_booth_rows); the island and its booth parts are not built.
FLAT_FOOTPRINT_M = 0.1     # m: a footprint mapped lower than this is an area (a plaza), not drawn
G_TOLLBOOTH = "tollbooth"
TOLLBOOTH_H = 4.96          # m: the stock shape's height (s_toll_booth_center)
TOLL_ISLAND_MAX_H = 1.0     # m: an island is a "building" no taller than this ...
TOLL_ISLAND_MIN_L = 8.0     # ... at least this long ...
TOLL_ISLAND_MAX_W = 4.0     # ... and at most this wide
TOLL_BOOTH_H = (2.0, 8.0)   # m: a booth on it: a footprint this tall ...
TOLL_BOOTH_MAX_AREA = 40.0  # ... and this small (m2), its centre on the island
TOLL_ROAD_M = 25.0          # m: a road this near the island's centre ...
TOLL_ROAD_DEG = 25.0        # ... running along it within this

def _on_box(p, box, pad=1.0):
    """Is point p (2,) within the oriented box (centre, axis u, half length, half width) grown by pad?"""
    c, u, hl, hw = box
    d = np.asarray(p, np.float64) - c
    return abs(float(dnp.matmul(d, u))) <= hl + pad and abs(float(d[0] * u[1] - d[1] * u[0])) <= hw + pad


def _toll_islands(frame, layers, parts_of, lines, x0, y0):
    """The toll islands among all the buildings of the tile's cells, as oriented boxes relative to (x0, y0): a low
    building (TOLL_ISLAND_*) with a booth (TOLL_BOOTH_*) among all the cells' building parts centred on it, along a
    road (lines, relative to (x0, y0)). Not only the tile's own buildings: a part is in its building's tile, and the
    Golden Gate's booths are parts of the plaza's footprint, centred in the next tile from most islands."""
    booths = []
    for pring, hh in parts_of.booths(*TOLL_BOOTH_H):
        if hh is None or not (TOLL_BOOTH_H[0] <= hh[0] + hh[1] <= TOLL_BOOTH_H[1]):
            continue
        r = pring - np.array([x0, y0])
        if _area2(r) / 2 <= TOLL_BOOTH_MAX_AREA:
            booths.append(r[:-1].mean(0) if len(r) > 1 and np.allclose(r[0], r[-1]) else r.mean(0))
    if not booths:
        return []
    segs = [np.concatenate([np.asarray(ln, np.float64)[:-1], np.asarray(ln, np.float64)[1:]], 1)
            for ln in lines if len(ln) >= 2]
    if not segs:
        return []
    segs = np.concatenate(segs)
    a, b = segs[:, :2], segs[:, 2:]
    ab = b - a
    L = np.maximum(dnp.hypot(ab[:, 0], ab[:, 1]), 1e-9)
    cos_max = dmath.cos(math.radians(TOLL_ROAD_DEG))
    out, seen = [], set()
    for f in layers:
        if f is None or len(f.kind) == 0 or f.attrs.get("height") is None:
            continue
        h = np.asarray(f.attrs["height"], np.float64)
        ids = getattr(f, "ids", None)
        for i in np.flatnonzero(np.isfinite(h) & (h > 0) & (h <= TOLL_ISLAND_MAX_H)).tolist():
            key = ids[i] if ids is not None else None
            if key is not None and key in seen:
                continue
            seen.add(key)
            parts = f.parts(i)
            if not parts or not parts[0] or len(parts[0][0]) < 4:
                continue
            e, n = mapgeom.enu_xy(f, frame, np.asarray(parts[0][0], np.float64))
            box = oriented_box(np.stack([e - x0, n - y0], 1)[:-1])
            c, u, hl, hw = box
            if 2 * hl < TOLL_ISLAND_MIN_L or 2 * hw > TOLL_ISLAND_MAX_W:
                continue
            if not any(_on_box(q, box) for q in booths):
                continue
            tt = np.clip(((c - a) * ab).sum(1) / (L * L), 0.0, 1.0)
            dist = dnp.hypot(*(a + tt[:, None] * ab - c).T)
            if ((dist <= TOLL_ROAD_M) & (np.abs((dnp.matmul(ab, u)) / L) >= cos_max)).any():
                out.append(box)
    return out


def _toll_mark(items, islands):
    """items with each toll island (islands: _toll_islands' boxes) marked cls "toll_booth" and the booths standing on
    them left out (West Coast USA's booth stands there instead: toll_booth_rows). Pure."""
    out = []
    for spec, gmin, gmax in items:
        p = spec.ring.mean(0)
        on = next((bx for bx in islands if _on_box(p, bx)), None)
        if on is None:
            out.append((spec, gmin, gmax))
            continue
        h = spec.height or 0.0
        if h <= TOLL_ISLAND_MAX_H:
            _c, _u, hl, hw = oriented_box(spec.ring)
            if 2 * hl >= TOLL_ISLAND_MIN_L and 2 * hw <= TOLL_ISLAND_MAX_W and (spec.min_h or 0) <= 0.5:
                spec.cls = "toll_booth"         # the island itself (not a plaza's flat footprint round it)
            out.append((spec, gmin, gmax))
        elif not (TOLL_BOOTH_H[0] <= h <= TOLL_BOOTH_H[1] and _area2(spec.ring) / 2 <= TOLL_BOOTH_MAX_AREA):
            out.append((spec, gmin, gmax))
    return out


def toll_booth_rows(items, x0, y0):
    """Prop rows (trees.lua rows, group G_TOLLBOOTH) of the toll islands (_toll_mark) among items (rings relative to
    (x0, y0)): West Coast USA's booth along each island, on its lowest ground."""
    rows = []
    for spec, gmin, _gmax in items:
        if spec.cls != "toll_booth":
            continue
        c, u, _hl, _hw = oriented_box(spec.ring)
        if u[0] < 0 or (u[0] == 0 and u[1] < 0):    # one way along the axis for every island: the booths face alike
            u = -u
        yaw = (math.degrees(dmath.atan2(u[1], u[0])) - 90.0) % 360.0       # (its length is the shape's local y)
        rows.append([G_TOLLBOOTH, round(float(c[0] + x0), 2), round(float(c[1] + y0), 2), round(gmin, 2),
                     round(yaw, 1), 1.0, TOLLBOOTH_H, 0.5])
    return rows


def collider_boxes(items, x0, y0):
    """Boxes [x0, y0, z0, x1, y1, z1] (anchor frame) around each building: the mod does not make the tile's
    mesh solid while the player's car is inside one (roads.lua playerIn)."""
    out = []
    for spec, gmin, gmax in items:
        if deck_plan(spec, gmin, gmax) is not None:     # an open parking deck: driven into on purpose
            continue
        if spec.cls in BRIDGE_CLASSES or spec.cls == landmarks.LANDMARK:   # a frame: driven through between its legs
            continue
        r = spec.ring
        out.append([round(float(r[:, 0].min() + x0), 2), round(float(r[:, 1].min() + y0), 2),
                    round(gmin - FOUNDATION_M, 2), round(float(r[:, 0].max() + x0), 2),
                    round(float(r[:, 1].max() + y0), 2), round(gmax + spec.height + 6.0, 2)])
    return out


def garage_sound_boxes(items, x0, y0, kind):
    """The sound spaces (roads.sound_boxes rows [kind, x, y, z, length, width, height, yaw], anchor frame) of the
    tile's parking structures drawn as open decks (deck_plan: the ones driven into): each its footprint's oriented
    box from its lowest ground to its top."""
    out = []
    for spec, gmin, gmax in items:
        if deck_plan(spec, gmin, gmax) is None:
            continue
        c, u, hl, hw = oriented_box(spec.ring)
        z0, z1 = gmin - 0.5, gmax + spec.height + 1.0
        out += [float(kind), round(float(c[0]) + x0, 2), round(float(c[1]) + y0, 2), round((z0 + z1) / 2, 2),
                round(2 * float(hl), 2), round(2 * float(hw), 2), round(z1 - z0, 2),
                round(dmath.atan2(float(u[1]), float(u[0])), 4)]
    return out


# The engine's LOD size of a near tile's shape below which its buildings are drawn as the lite mesh (the far tiles' one:
# no window bays, steps, porches, signs, cornices or roof clutter): the engine's own camera LOD (cdae lods), the tile's
# whole shape at once. Calibrated in game (2026-10-18, 1440p, the game's mesh detail at its default 2): a 21 m radius
# probe switched between 450 and 550 m, so a tile of ~190 m radius switches ~500 m from its centre (less with a lower
# mesh detail or screen resolution, as the stock shapes do).
BLD_LOD_PX = 2700.0


def write(path, mesh, collision_only=False):
    """The tile's .cdae (cdae.encode with one primitive per material; collision_only: a never-drawn collision detail).
    A mesh with a 7th element (build_parts with_lod) gets that lite mesh as its coarse detail below BLD_LOD_PX.
    A list of meshes (build_parts' without lite meshes: a far tile's, a pack's, a skyline's): one shape, each mesh its
    own object (cdae.encode's parts: under the engine's 16-bit indices each)."""
    import cdae
    if isinstance(mesh, list):
        if len(mesh) > 1:
            if collision_only or any(len(m) > 6 and m[6] is not None for m in mesh):
                raise ValueError("buildings.write: several meshes are drawn ones without lite meshes")
            mats = list(mesh[0][4])
            parts = []
            for Pp, UVp, Tp, Mp, matsp, Cp in (m[:6] for m in mesh[1:]):
                for m in matsp:
                    if m not in mats:
                        mats.append(m)
                remap = np.array([mats.index(m) for m in matsp], np.int64)
                parts.append({"positions": Pp, "tris": Tp, "uvs": UVp, "tri_materials": remap[np.asarray(Mp)],
                              "colors": Cp})
            P, UV, T, M, _mats, C = mesh[0][:6]
            return cdae.write(path, P, T, uvs=UV, material=mats, tri_materials=M, object_name="buildings", colors=C,
                              parts=parts)
        mesh = mesh[0]
    P, UV, T, M, mats, C = mesh[:6]
    mats = list(mats)
    lods = None
    lite = mesh[6] if len(mesh) > 6 and not collision_only else None     # (None: no lite mesh)
    if lite is not None:
        Pl, UVl, Tl, Ml, matsl, Cl = lite
        for m in matsl:
            if m not in mats:
                mats.append(m)
        remap = np.array([mats.index(m) for m in matsl], np.int64)
        lods = [(BLD_LOD_PX, {"positions": Pl, "tris": Tl, "uvs": UVl, "tri_materials": remap[np.asarray(Ml)],
                              "colors": Cl})]
    return cdae.write(path, P, T, uvs=UV, material=mats, tri_materials=M, object_name="buildings", colors=C,
                      collision_only=collision_only, lods=lods)


DOOR_OUT_M = 0.09            # m the door model's origin stands off the wall: its panel is 6.9 cm behind the origin
                             # (both Building Architect doors), so at 3 cm it was inside the wall, only the frame
                             # showing (Fells Point, 2026-10-17)
STOOP_SHARE = 0.8           # row houses with a front stoop (Baltimore's white marble steps)
# A row house's marble stoop (the user's report #47, 2026-10-17: "the steps model isnt good. Try and find one from the
# offical maps"): built as the official maps' stair blocks are (West Coast USA's stairblock1, Italy's old stairs:
# treads and risers up to a landing between stepped side walls); their own models carry race-track paint and logos
STOOP_H = 0.54              # m: three steps up to the door (Fells Point's on Mapillary, 2026-10-17: low white marble
STOOP_STEPS = 3             # steps, no side walls; ours were four between walls, big white blocks)
STOOP_RISE = 0.18           # m a step (more of them where the street falls away from the house)
STOOP_MAX_STEPS = 7
STOOP_RISE_MAX = 0.23       # m: the steepest outdoor step
STOOP_TREAD = 0.3           # m deep each step
STOOP_LANDING = 0.6         # m deep the landing at the door
STOOP_W = 1.3               # m wide the steps
DOOR_ROAD_M = 30.0          # a front door on the wall facing the nearest road within this
DOOR_CLASSES_SKIP = SHED_CLASSES | INDUSTRIAL_CLASSES
G_DOOR = "door"
FAR_SEND_M = float(os.environ.get("BNE_BLDFAR_SEND_M", "1600"))    # far building tiles (render-only, the lite mesh)
                                                                    # sent within this (Euclidean: roads.tiles_near)
FAR_DROP_M = float(os.environ.get("BNE_BLDFAR_DROP_M", str(FAR_SEND_M + 300)))     # ... dropped beyond
FAR_PER_POSE = 4
# Far building packs (2026-11-13, the expo O work: a Fells Point place held ~377 far buildings tiles, a shape file each -
# a third of the place's shape files, each ~2-5 ms to load in the game and a first draw): past PACK_INNER_M the far
# buildings of a whole skyline square (geotiles.SKY_Z, 4 x 4 road geotiles) come as one shape (kind "bldpack", prefix
# bp_), the road geotiles' own far tiles only within it (their squares, whole). PACK_INNER_M stays past the road tiles'
# drop reach (roads.DROP_M 960 m): a pack's square never holds a road geotile showing its own buildings. The mod
# shows a pack until its square's far tiles are all in and made (roads.lua farGeoHidden 'P'). hello.farPack ==
# FAR_PACK (the mod's roads.FAR_PACK) and BNE_BLDPACK != off.
FAR_PACK = 1
PACK_ON = os.environ.get("BNE_BLDPACK", "on") != "off"
PACK_INNER_M = float(os.environ.get("BNE_BLDPACK_INNER_M", "1000"))
PACK_PER_POSE = 2
FAR_GEO = 1                 # the far geotiles' protocol (hello.farGeo, the mod's roads.lua M.FAR_GEO): far buildings,
                            # skyline and far bridges in the global tiles' frames (blockworker.far_geo)


MAIN_STREET_CLASSES = {"primary", "secondary", "tertiary"}
BACK_ROAD_CLASSES = {"service", "track", "path", "footway", "cycleway", "pedestrian", "steps", "alley"}
MAIN_STREET_SETBACK_M = 7.0  # m from a main street's edge within which a front wall has a shopfront
MAIN_STREET_SHOPS = 0.75     # the share of such buildings that do


DRIVEWAY_W = 3.2            # m
DRIVEWAY = {"m": "road_asphalt_2lane", "tl": 11.0, "p": 19}     # a road tile chunk's look (roads.LOOK)


def doors(items, lines, x0, y0, drives=None, infer_fences=True):
    """Prop rows (trees.lua rows, group "door") of a front door per house: on the wall whose middle is
    nearest a road and faces it, a row house's to one side, others' in the middle. lines: [(centreline
    (k, 2) in the anchor frame, half width[, class, urban])]. A building close to an urban main street gets
    a shopfront ground floor instead (spec.shop, most of them). drives (a list) gets a driveway chunk (road
    tile chunk dicts, draped by roadmesh) from each detached house's front to the road's edge."""
    if not lines:
        return []
    segs, smain, shalf, sminor = [], [], [], []
    for ln in lines:
        pts, half = ln[0], ln[1]
        main = len(ln) > 3 and ln[3] and ln[2] in MAIN_STREET_CLASSES     # an urban main street
        p = np.asarray(pts, np.float64)
        if len(p) >= 2:
            segs.append(np.concatenate([p[:-1], p[1:]], 1))
            smain.append(np.full(len(p) - 1, bool(main)))
            shalf.append(np.full(len(p) - 1, float(half)))
            sminor.append(np.full(len(p) - 1, len(ln) > 2 and ln[2] in BACK_ROAD_CLASSES))
    if not segs:
        return []
    S = np.concatenate(segs)                             # (n, 4) x0 y0 x1 y1
    SMAIN, SHALF, SMINOR = np.concatenate(smain), np.concatenate(shalf), np.concatenate(sminor)
    A, B = S[:, :2], S[:, 2:]
    D = B - A
    L2 = np.maximum((D * D).sum(1), 1e-12)
    fast.require(240, "nearest_segments")
    A, D = np.ascontiguousarray(A), np.ascontiguousarray(D)
    rows = []
    others = [(spec, spec.ring + np.array([x0, y0])) for spec, _, _ in items if (spec.min_h or 0.0) <= 0.5]
    for spec, gmin, gmax in items:
        if (spec.min_h or 0.0) > 0.5:
            continue            # (off the ground: a bridge's building over the street, a passage's part over its way; its
                                # door stood alone in the road under it, London's Sherwood Street, 2026-11-12)
        no_door = False
        if spec.plain or spec.floors >= 7 or spec.band in (BAND_OFFICE, BAND_OFFICE2) or spec.wall:
            if not spec.wall or spec.plain or spec.floors >= 7:
                no_door = True
        # shops and big boxes: their sign faces the street, not the alley or the parking aisle behind
        storefront = (spec.shop or spec.windows is False) and not spec.plain and spec.roof == "flat"
        if no_door and not storefront:
            continue
        ring = drawn_ring(spec) + np.array([x0, y0])
        best = None
        # every wall's middle against every road segment at once (earthfast.nearest_segments: numpy's (walls,
        # segments) arrays per building, the same numbers): each wall's nearest segment K, the nearest point on it QK
        # and its distance DK
        nxt = np.concatenate((ring[1:], ring[:1]))
        E = nxt - ring
        MID = (ring + nxt) / 2
        K, DK, QK = fast._ef.nearest_segments(np.ascontiguousarray(MID, np.float64), A, D, L2)
        # the walls facing a road near them (a shop's ground floor is a shopfront there only: _walls' street edges;
        # its back and side walls are drawn as its upper floors)
        if spec.shop or storefront or not no_door:
            street = []
            for i in range(len(ring)):
                Le = float(math.hypot(*E[i]))
                if Le < 1.0 or DK[i] > DOOR_ROAD_M:
                    continue
                out = np.array([E[i][1], -E[i][0]]) / Le
                if float(dnp.dot(QK[i] - MID[i], out)) > 0:
                    street.append(i)
            spec.street = tuple(street) if street else None
        for i in range(len(ring)):
            a = ring[i]
            e = E[i]
            Le = float(math.hypot(*e))
            if Le < 2.5:
                continue
            mid = MID[i]
            dk = DK[i]
            k = int(K[i])
            if dk > DOOR_ROAD_M:
                continue
            out = np.array([e[1], -e[0]]) / Le
            if float(dnp.dot(QK[i] - mid, out)) <= 0:
                continue                                 # the wall faces away from that road
            rank = (bool(SMINOR[k]) if storefront else False, float(dk))
            if best is None or rank < best[0]:
                best = (rank, a, e / Le, Le, out, i, bool(SMAIN[k]) and dk < SHALF[k] + MAIN_STREET_SETBACK_M,
                        k)
        if best is None:
            continue
        _, a, d, Le, out, edge, on_main, kseg = best
        if spec.windows is False:
            spec.front = edge                   # a big box: its storefront faces the road
            if storefront:
                spec.sign = edge
            rows.extend(_sign_in_open(_wall_sign(spec, a, d, Le, out, gmax), spec, d, out, others))
            continue
        if spec.cls in RESTAURANT_CLASSES and spec.floors == 1:
            rows.extend(_sign_in_open(_wall_sign(spec, a, d, Le, out, gmax), spec, d, out, others))
        if spec.shop:
            if storefront and not spec.wall:
                spec.sign = edge                # the sign band over the street wall's shopfront
            continue
        if no_door:
            continue
        if on_main and not spec.wall and spec.roof == "flat" and spec.cls not in CHURCH_CLASSES \
                and _hash01(spec.seed, "shop") < MAIN_STREET_SHOPS:
            spec.shop = True                    # a main street's ground floor: a shopfront, no house door
            if not spec.plain:
                spec.sign = edge
            continue
        narrow = Le < 9.0
        f = (0.22 if _hash01(spec.seed, "door") < 0.5 else 0.78) if narrow else 0.5
        spec.door = (edge, Le * f)          # _walls leaves the ground floor's band plain there
        # brick row houses (an eastern city's): white marble steps up to the door
        if narrow and spec.band == BAND_BRICK and spec.roof == "flat" and _hash01(spec.seed, "stoop") < STOOP_SHARE:
            spec.stoop = (edge, Le * f)
        if drives is not None and spec.wall and Le >= 7.0:
            # the driveway beside the door, straight out to the road's edge
            g = 0.82 if f <= 0.5 else 0.18
            p0 = a + d * (Le * g) + out * 0.2
            t = float(np.clip(((p0 - A[kseg]) * D[kseg]).sum() / L2[kseg], 0, 1))
            q = A[kseg] + t * D[kseg]
            v = p0 - q
            dl = float(dnp.hypot(*v))
            if SHALF[kseg] + 1.0 < dl < DOOR_ROAD_M:
                p1 = q + v / dl * (SHALF[kseg] + 0.1)
                n = max(2, int(dl // 4.0) + 1)
                pts = [p0 + (p1 - p0) * (j / (n - 1)) for j in range(n)]
                drives.append({"m": DRIVEWAY["m"], "w": DRIVEWAY_W, "tl": DRIVEWAY["tl"], "p": DRIVEWAY["p"],
                               "n": [[round(float(pp[0]), 3), round(float(pp[1]), 3), 0.0] for pp in pts]})
        if infer_fences and spec.roof != "flat" and not narrow and _hash01(spec.seed, "fence") < FENCE_SHARE:
            rows.extend(_front_fence(spec, a, d, Le, f, A[kseg], D[kseg], L2[kseg], SHALF[kseg], gmin,
                                     drives is not None and spec.wall and Le >= 7.0, (A, B, SHALF)))
        p = a + d * (Le * f) + out * DOOR_OUT_M
        gd = door_ground(spec, edge, Le * f, gmax) if spec.stoop else gmax     # a stoop's door: over its own ground
        base = gd + (STOOP_H if spec.stoop else 0.0) + (PORCH_DECK_M if spec.porch else 0.0)   # (the deck / stoop)
        if spec.fr_house:
            continue                    # the regional model contains its own fitted front door
        rows.append([G_DOOR, round(float(p[0]), 2), round(float(p[1]), 2), round(base, 2),
                     round(math.degrees(dmath.atan2(d[1], d[0])) % 360.0, 1), 1.0, 2.2,
                     round(_hash01(spec.seed, "dv"), 3)])
    return rows


# Company signs on walls (West Coast USA's, for its fictional firms): (prop group, width m, height m, panel thin
# along its local x (True) or y (False)). Warehouses: Logistics or Rentabox; big retail boxes: Lemon; one-storey
# restaurants: Turboburger.
WALL_SIGNS = {"logistics": ("wallsign_logistics", 10.8, 5.8, True), "rentabox": ("wallsign_rentabox", 8.2, 5.7, True),
              "lemon": ("wallsign_lemon", 11.5, 3.6, True), "burger": ("wallsign_turboburger", 5.8, 2.4, False)}
WALL_SIGN_SHARE = 0.6
RESTAURANT_CLASSES = {"restaurant", "fast_food", "cafe"}


def _sign_in_open(rows, spec, d, out, others):
    """rows (a _wall_sign) kept only when the panel and the air in front of it are clear of the other buildings:
    a sign on a wall hidden behind a neighbour stood out through the neighbour at the wall's angle (Stelvio
    pass, a big box's sign through the shop in front of it, the user's report 2026-10-17)."""
    if not rows:
        return rows
    _, x, y, _, _, _, h, _ = rows[0]
    w = WALL_SIGNS[next(k for k, v in WALL_SIGNS.items() if v[0] == rows[0][0])][1] * h / \
        WALL_SIGNS[next(k for k, v in WALL_SIGNS.items() if v[0] == rows[0][0])][2]
    c = np.array([x, y])
    probes = [c + d * (w / 2 * f) + out * o for f in (-1.0, 0.0, 1.0) for o in (0.0, 1.5)]
    for other, ring in others:
        if other is spec:
            continue
        if any(_inside(ring, p) for p in probes):
            return []
    return rows


def _wall_sign(spec, a, d, Le, out, gmax):
    """A company sign centred high on a big box's or restaurant's street wall (WALL_SIGNS), if it fits."""
    if _hash01(spec.seed, "wallsign") >= WALL_SIGN_SHARE:
        return []
    if spec.cls in RESTAURANT_CLASSES:
        key = "burger"
    elif spec.wall and str(spec.wall).startswith(METALWALL):
        key = "logistics" if _hash01(spec.seed, "ws") < 0.5 else "rentabox"
    else:
        key = "lemon"
    group, w, h0, thin_x = WALL_SIGNS[key]
    h = min(h0, spec.height * 0.45)                     # scaled down to the wall (trees.lua scales to the height)
    w *= h / h0
    if h < 1.8 or Le < w + 2.0:
        return []
    zc = gmax + spec.height - 0.6 - h / 2               # its top 0.6 m under the parapet
    p = a + d * (Le / 2) + out * 0.35
    # the logo faces the model's local -x (Lemon, Logistics, Rentabox: seen in game 2026-10-14) or its -y
    yaw = math.degrees(dmath.atan2(out[1], out[0])) + (180.0 if thin_x else 90.0)
    return [[group, round(float(p[0]), 2), round(float(p[1]), 2), round(float(zc), 2), round(yaw % 360.0, 1), 1.0,
             round(h, 2), 0.0]]


G_FENCE = "fence"           # trees.lua: East Coast USA's low wooden fence (eca_bld_wood_fence_c, 5.4 m, along x)
FENCE_SEG_M = 5.4
FENCE_SHARE = 0.3           # detached houses with a fence along the front of their yard (hashed)
FENCE_BACK_M = 3.2          # m from the road's edge (the back of the sidewalk)
FENCE_MIN_YARD_M = 2.5      # the front yard at least this deep between the fence and the house
FENCE_CLEAR_M = 2.0         # m every piece keeps from any road's edge (a house on a bend or a corner: its fence ran
                            # along one road out across another, GitHub #105 Falcon Drive, Coquitlam)


def _front_fence(spec, a, d, Le, f, A, D, L2, half, z, driveway, roads=None):
    """Prop rows of a fence along the front of a house's yard, parallel to the road at the back of the sidewalk,
    a little wider than the house, with gaps for the path to the door and the driveway. roads: (starts (n, 2), ends
    (n, 2), half widths (n,)) of the roads near: no piece within FENCE_CLEAR_M of their edges."""
    mid = a + d * (Le / 2)
    t = float(np.clip(((mid - A) * D).sum() / L2, 0, 1))
    q = A + t * D
    v = mid - q
    dist = float(dnp.hypot(*v))
    if dist - (half + FENCE_BACK_M) < FENCE_MIN_YARD_M:
        return []
    nv = v / dist
    dr = D / math.sqrt(L2)
    centre = q + nv * (half + FENCE_BACK_M)
    yaw = round(math.degrees(dmath.atan2(dr[1], dr[0])) % 360.0, 1)
    gaps = [float(dnp.matmul(a + d * (Le * f) - centre, dr))]                          # the path to the door
    if driveway:
        gaps.append(float(dnp.matmul(a + d * (Le * (0.82 if f <= 0.5 else 0.18)) - centre, dr)))
    n = max(1, int(math.ceil((Le + 3.0) / FENCE_SEG_M)))
    rows = []
    for k in range(n):
        sk = (k - (n - 1) / 2) * FENCE_SEG_M
        if any(abs(sk - g) < FENCE_SEG_M / 2 + 0.6 for g in gaps):
            continue
        p = centre + dr * sk
        if roads is not None and len(roads[0]):
            ends = np.array([p - dr * (FENCE_SEG_M / 2), p, p + dr * (FENCE_SEG_M / 2)])
            fast.require(0, "points_near_segments")
            if np.asarray(fast._ef.points_near_segments(ends, np.ascontiguousarray(roads[0], np.float64),
                                                        np.ascontiguousarray(roads[1], np.float64),
                                                        np.asarray(roads[2], np.float64) + FENCE_CLEAR_M)).any():
                continue
        rows.append([G_FENCE, round(float(p[0]), 2), round(float(p[1]), 2), round(float(z), 2), yaw, 1.0, 1.3,
                     round(_hash01(spec.seed, "fr", k), 3)])
    return rows


FOOTPRINT_GATE_M = 40.0   # (was 150 in PR #854: a statue or tower in a park was moved onto a building that far off)  # _nearest_building_footprint: a candidate footprint farther than this from a
# model's own api point is not considered its building at all - the map's own data for a tile a model landed
# in by a wide, SEARCH_CORRECT_R_M-radius hit (far outside this) is not trusted that loosely either.
FOOTPRINT_SIZE_MIN = 0.4  # ... nor is one smaller than this fraction of the model's own major side (a garden
FOOTPRINT_SIZE_MAX = 3.0  # shed next to a cathedral) or bigger than this one (a city block round a kiosk)
FOOTPRINT_SIZE_TIE_FRAC = 0.2  # two candidates within this fraction of each other's own major side are "the
# same size" as far as _nearest_building_footprint is concerned - too close to call apart by size alone, and
# the map's own footprints carry no name this far downstream (tile_buildings drops it) to break the tie with


def _ring_major_m(ring):
    """A footprint ring's own major horizontal side, metres - the same measure _custom_tile_entries already
    takes of a model's bbox (max(dx, dy)), so the two are comparable."""
    xs, ys = ring[:, 0], ring[:, 1]
    return float(max(xs.max() - xs.min(), ys.max() - ys.min()))


def _nearest_building_footprint(items, x0, y0, px, py, model_major_m):
    """The map's own building footprint (items: road_tile_items' tile_buildings result, rings relative to
    (x0, y0)) closest to (px, py) - a 3dmr.eu model's own, uncorrected api point - among those plausibly ITS
    building: within FOOTPRINT_GATE_M and sized within [FOOTPRINT_SIZE_MIN, FOOTPRINT_SIZE_MAX] of the
    model's own major side (model_major_m). threedmr model_position's level 3 (called from _custom_tile_
    entries only for a model whose own source is "api" - no override/fix/tagged-OSM-way/desc correction
    already placed it more precisely): a mapper's upload pin regularly sits tens of metres from the real
    building (probe/position_audit.json), closer to the map's own footprint than to nothing.

    Returns the footprint's own centre (x, y) in the same frame as (px, py), or None - the api point is kept
    - when nothing passes both gates, or the closest match's own size is within FOOTPRINT_SIZE_TIE_FRAC of
    another, equally plausible candidate's: which of the two is the model's own building would be a guess,
    and a user's report of a model "jumping" to the wrong one of two near-identical row houses is worse than
    leaving it at its mapper's own (merely imprecise) point - "сомневаешься - не двигай" (owner review)."""
    cands = []
    for it in items:
        spec = it[0]
        major = _ring_major_m(spec.ring)
        if not (model_major_m * FOOTPRINT_SIZE_MIN <= major <= model_major_m * FOOTPRINT_SIZE_MAX):
            continue
        c = spec.ring.mean(0) + (x0, y0)
        dist = math.hypot(c[0] - px, c[1] - py)
        if dist > FOOTPRINT_GATE_M:
            continue
        cands.append((dist, major, float(c[0]), float(c[1])))
    if not cands:
        return None
    cands.sort(key=lambda t: t[0])
    best = cands[0]
    for other in cands[1:]:
        if abs(other[1] - best[1]) <= FOOTPRINT_SIZE_TIE_FRAC * max(other[1], best[1]):
            return None
    return best[2], best[3]


def _suppress_under_models(items, radii, x0, y0):
    """items (tile_buildings', rings relative to (x0, y0)) with every footprint whose centre falls inside one
    of radii's circles dropped: a baked 3dmr.eu model (_custom_tile_entries' radii, [(id, cx, cy, radius)]
    in the anchor frame) stands there instead, and a procedural stub under/through it would poke
    out of it (a user's report at Baghdad's Al-Shaheed Monument: two OSM "sector" footprints, the domes'
    lower tiers, stayed and intersected model 24's real mesh once it loaded); every model gets its own circle
    over any procedural buildings standing where it is placed. Toll booths (spec.cls "toll_booth": the stock
    prop a toll island is drawn as, toll_booth_rows) are never suppressed - a monument standing near a toll
    plaza should not eat its booth. Applied one model at a time (not a single logged count) so each model's
    own effect can be told apart in the log."""
    import logging

    log = logging.getLogger("earthd.buildings")
    for cid, cx, cy, r in radii:
        kept = []
        n = 0
        for it in items:
            spec = it[0]
            c = spec.ring.mean(0) + (x0, y0)
            if (
                spec.cls != "toll_booth"
                and dnp.sq(c[0] - cx) + dnp.sq(c[1] - cy) <= r * r
            ):
                n += 1
                continue
            kept.append(it)
        items = kept
        if n:
            log.info(
                "3dmr model %s: suppressed %d procedural building(s) under it", cid, n
            )
    return items


def _tex_to_png(tex, ext):
    """A glTF texture's raw bytes re-encoded to PNG (the engine's TSStatic materials need one), or None for
    an untextured material/slot (tex is None)."""
    if tex is None:
        return None
    if ext != "jpeg":
        return tex
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.open(io.BytesIO(tex)).save(buf, "PNG")
    return buf.getvalue()


def _section_mat_tex(sec_res, factors, has_alphas, alpha_cutoffs, double_sideds=None):
    """[(material, texture file name or None, factor, has_alpha, alpha_cutoff, double_sided), ...] of one write_model
    section result, single-material ("material"/"texture") or several ("materials"/"textures") - told apart
    by which key is actually there, since a split model's own later sections (threedmr._write_section's
    force_multi) always come back plural even when the model as a whole only has one material. double_sideds:
    per slot (threedmr.gltf_mesh's textures' 6th field), None all doubleSided."""
    if double_sideds is None:
        double_sideds = [True] * len(factors)
    if "materials" in sec_res:
        return list(
            zip(
                sec_res["materials"],
                sec_res["textures"],
                factors,
                sec_res["has_alpha"],
                sec_res["alpha_cutoff"],
                double_sideds,
            )
        )
    return [
        (
            sec_res["material"],
            sec_res.get("texture"),
            factors[0],
            sec_res["has_alpha"],
            sec_res["alpha_cutoff"],
            double_sideds[0],
        )
    ]


def _model_mesh(c, cache_dir):
    """road_tile_items' custom entry c's mesh as it is baked: (pos, uvs, tris, nrm, textures, tri_materials), a local
    model off disk (threedmr.load_local_model), a 3dmr.eu one fetched and decoded (threedmr.gltf_mesh), turned by
    its forced (threedmr.position_override) or OSM (threedmr.osm_yaw) yaw when c has a "latlon"."""
    import logging
    import threedmr

    log = logging.getLogger("earthd.buildings")
    cid = c["id"]
    if "local" in c:
        # the player's own mesh, already placed in threedmr_cache by hand - no fetch, no OSM-footprint
        # yaw (it is not a 3dmr.eu upload to straighten; the player orients their own STL themselves)
        return threedmr.load_local_model(cache_dir, c["local"])
    glb = threedmr.fetch_model(cid, cache_dir)
    pos, uvs, tris, nrm, textures, tri_materials = threedmr.gltf_mesh(glb, model_id=cid)
    latlon = c.get("latlon")
    if latlon is not None and len(pos):
        zmin, zmax = float(pos[:, 2].min()), float(pos[:, 2].max())
        lower = pos[pos[:, 2] <= zmin + 0.1 * (zmax - zmin)][:, :2]
        override = threedmr.position_override(cid)
        if override is not None and override[2] is not None:
            yaw = override[2]
            log.info("3dmr model %s: yaw %.1f° (forced by override)", cid, yaw)
        else:
            yaw = threedmr.osm_yaw(latlon[0], latlon[1], cid, lower, cache_dir)
            if yaw:
                log.info("3dmr model %s: yaw %.1f° (auto from OSM)", cid, yaw)
        if yaw:
            pos = threedmr.rotate_xz(pos, yaw)
    return pos, uvs, tris, nrm, textures, tri_materials


def _is_bridge(pos, info=None):
    """A model bridge: its 3dmr.eu record says so (info: threedmr.model_info's; threedmr.info_says_bridge) and it
    reaches BRIDGE_MIN_TAGGED_M, or its smallest bounding rectangle seen from above (threedmr.deck_axis: whatever way
    it runs - an axis-aligned box missed Perth's diagonal Mount Henry Bridge, GitHub #1071) is a bridge deck's shape
    (BRIDGE_ASPECT, BRIDGE_MIN_SPAN_M)."""
    import threedmr

    if not len(pos):
        return False
    _ang, major, minor = threedmr.deck_axis(np.asarray(pos, np.float64)[:, :2])
    if info is not None and threedmr.info_says_bridge(info) and major >= BRIDGE_MIN_TAGGED_M:
        return True
    return major > BRIDGE_ASPECT * minor and major > BRIDGE_MIN_SPAN_M


def _centred(pos):
    """pos centred on its own horizontal bbox, as threedmr.write_model does before anything else."""
    P = np.array(pos, np.float64).reshape(-1, 3)
    P[:, 0] -= (P[:, 0].min() + P[:, 0].max()) / 2
    P[:, 1] -= (P[:, 1].min() + P[:, 1].max()) / 2
    return P


def _custom_tile_entries(frame, custom, ground, out_dir, vfs_dir, road_ends=None, items=None, origin=None):
    """custom's (tile_buildings') 3dmr.eu models fetched, plus any local (no-3dmr-base, a "local" key -
    road_tile_items') models loaded straight off disk (threedmr.load_local_model, no fetch_model/osm_yaw),
    baked into out_dir and registered in the mod's materials (one per distinct source material a model uses
    - threedmr.gltf_mesh's/load_local_model's textures/tri_materials):
    ([{"shape", "pos"}], [(id, cx, cy, radius)]). The first (anchor-frame
    xy, z 0: write_model beds the mesh's own z to the ground, as the tile meshes' pos already does -
    roads.write_tile's "bld" etc) holds one entry per model, except a model too big for Torque's 16-bit IBO
    (threedmr.SPLIT_VERTEX_TRIGGER, threedmr.write_model's "sections"), which lands one entry per section,
    all at the same pos. The second: a suppression circle per baked model (_suppress_under_models), radius
    SUPPRESS_MARGIN_M past half the model's own horizontal bbox, clamped to [SUPPRESS_MIN_M, SUPPRESS_MAX_M]
    - the real mesh's own footprint is not known until it is downloaded and decoded, so the radius can only
    be had here (a user's report at Baghdad's Al-Shaheed Monument, model 24: two procedural "sector"
    stubs of its domes' lower tiers stayed and poked through the real mesh once it loaded).

    items, origin ((x0, y0), items' own rings relative to it - road_tile_items'/road_tile's): threedmr model_
    position's level 3, _nearest_building_footprint, run here (not in model_position - the map's own tile
    data is not that function's to read) right after a model's own mesh bbox is known and before the radius
    above, so suppression centres on the adjusted spot too. Only for a model whose custom entry's own
    "source" (road_tile_items') is "api" - no override/threedmr_position_fixes.json/tagged-OSM-way/desc
    correction already placed it more precisely - and never a "local" one (the player's own point, not a
    3dmr.eu upload to second-guess). Skipped entirely (items/origin not given, or BNGE_3DMR_NO_REPOSITION
    set) the way level 2 already is: the model stays at its own custom["xy"].

    A model whose horizontal bbox is a bridge deck's shape (BRIDGE_ASPECT, BRIDGE_MIN_SPAN_M - told apart
    from a monument's or a building's) carries the road itself (FV=24, a user's report: no stub under it, driven on, no
    friction): its deck's top is traced (threedmr.bridge_deck_surface), the model leaned so that deck meets the
    road network where the mapped bridge leaves it (road_ends: blockworker's bridge_road_ends; else the ground at
    the deck's own ends - threedmr.bridge_tilt), drawn without collision ("col": False - its two-sided lattice
    caught the car) beside an invisible closed collision slab on that deck (threedmr.write_bridge_deck, a
    "_deck" entry at the same pos); the procedural bridge under it is left out of every tile it reaches
    (bridge_footprints, bridge_stub_*). A slab that fails to write leaves the model its own collision (and the
    tile incomplete: revisit.fault). Before that: if custom's entry carries
    a "latlon" (road_tile_items' - the model's own mapped position), the mesh is rotated about its vertical
    axis to match its OSM footprint's edges (threedmr.osm_yaw/rotate_xz) - not every 3dmr.eu upload follows
    the orientation guide (a user's report: the Eiffel Tower, id 4, stood ~45 degrees off though its
    position was right), so this runs before write_model's own centering (rotating a mesh that is not yet
    centered rotates it about its own bbox centre, not an arbitrary point) and before the radius/bridge-deck
    check below (its horizontal bbox can shift a little with the rotation). A model with a
    threedmr.position_override whose own yaw_deg is not None (id 22) uses that forced yaw instead - osm_yaw's
    OSM lookup is skipped entirely, not just its cache: the override is the already-settled answer.

    A model's download or decode failing drops that one entry (logged, no suppression circle for it) rather
    than the rest of the tile's; unsupported geometry (threedmr.UnsupportedModelError - Draco) is a permanent
    skip, anything else marks the build incomplete (revisit.fault()) so the tile is rebuilt and the model
    retried on the next visit - and, meanwhile, this visit's procedural stand-ins stay (no radius for a model
    that is not actually standing there yet).

    SUPPORT_DZ_WARN_M: write_model already beds every model on its own detected support plane
    (threedmr._support_z), so this is a log line only, not a filter - a model whose support plane sits this
    far from its bare bbox floor just gets flagged (could be legitimate: a bridge deck over a riverbed, an
    elevated plaza), not dropped - rejecting on a threshold here would risk eating an author's own real,
    intentionally elevated geometry."""
    import logging
    import os
    import threedmr

    log = logging.getLogger("earthd.buildings")
    # out_dir = <user_dir>/temp/beamngEarth/<sid> (earthd.Server.__init__): the mod's own folder, three levels up
    user_dir = os.path.dirname(os.path.dirname(os.path.dirname(out_dir)))
    # fetch_model appends "threedmr_cache" itself: pass the sessions' parent (temp/beamngEarth), so
    # the cache lives beside the session dirs and survives them (a session dir is wiped per anchor)
    cache_dir = os.path.dirname(out_dir)
    # the vfs twin of cache_dir/threedmr_cache (threedmr._persistent_png_dir) - where a model's own texture(s)
    # are baked too (write_model's tex_cache_dir below), so a materials.json entry still points at a live file
    # after a revisit cache hit restores the tile's .cdae alone (revisit.shapes_in never sees a colorMap)
    tex_vfs_dir = vfs_dir.rsplit("/", 1)[0] + "/threedmr_cache"
    log.info("3dmr: baking %d custom model(s) in tile", len(custom))
    entries, radii = [], []
    for c in custom:
        cid = c["id"]
        try:
            pos, uvs, tris, nrm, textures, tri_materials = _model_mesh(c, cache_dir)
            dx = float(pos[:, 0].max() - pos[:, 0].min()) if len(pos) else 0.0
            dy = float(pos[:, 1].max() - pos[:, 1].min()) if len(pos) else 0.0
            if max(dx, dy) > MODEL_MAX_M:
                log.warning("3dmr model %s: %.0f x %.0f m, wider than %.0f m: not placed", cid, dx, dy, MODEL_MAX_M)
                continue
            # (a bridge stands where bridge_footprints puts it, onto its mapped bridge: every tile round it leaves
            # its procedural bridge out there, so no building footprint moves it)
            is_bridge = "local" not in c and _is_bridge(pos, threedmr.cached_info(cid, cache_dir))
            if (
                not is_bridge
                and items is not None
                and origin is not None
                and "local" not in c
                and c.get("source") == "api"
                and not os.environ.get(threedmr.NO_REPOSITION_ENV)
            ):
                fp = _nearest_building_footprint(
                    items, origin[0], origin[1], c["xy"][0], c["xy"][1], max(dx, dy)
                )
                if fp is not None:
                    log.info(
                        "3dmr model %s: positioned by the nearest map building footprint "
                        "(api point was %.1f,%.1f)",
                        cid, c["xy"][0], c["xy"][1],
                    )
                    c = dict(c, xy=fp)
            la, lo, _ = frame.enu_to_geodetic(
                np.array([c["xy"][0]]), np.array([c["xy"][1]]), np.zeros(1)
            )
            gz = float(np.asarray(ground(la, lo), np.float64)[0])
            tilt = surf = None
            if is_bridge:
                # the road over it is the model's: its deck traced (threedmr.bridge_deck_surface, in the frame
                # write_model bakes it in: centred, bedded), both leaned so the deck's ends meet the ground roads
                Pc = _centred(pos)
                Pc[:, 2] += (gz - 0.2) - threedmr._support_z(Pc[:, 2])
                surf = threedmr.bridge_deck_surface(Pc, tris)
                if surf is not None:

                    def _ground_at(x, y, _c=c):
                        gla, glo, _ = frame.enu_to_geodetic(
                            np.array([_c["xy"][0] + x]), np.array([_c["xy"][1] + y]), np.zeros(1)
                        )
                        return float(np.asarray(ground(gla, glo), np.float64)[0])

                    ends = [(x - c["xy"][0], y - c["xy"][1], z) for x, y, z in (road_ends or {}).get(cid, ())]
                    tilt = threedmr.bridge_tilt(surf, _ground_at, cid, road_ends=ends)
                else:
                    log.warning("3dmr model %s: bridge without a deck to drive on", cid)
            pngs = [_tex_to_png(t[0], t[1]) for t in textures]
            factors = [t[2] for t in textures]
            has_alphas = [t[3] for t in textures]
            alpha_cutoffs = [t[4] for t in textures]
            double_sideds = [bool(t[5]) if len(t) > 5 else True for t in textures]   # (one-sided: modelled back faces)
            if len(pngs) == 1:
                res = threedmr.write_model(
                    out_dir,
                    cid,
                    pos,
                    tris,
                    uvs,
                    nrm,
                    pngs[0],
                    gz,
                    has_alpha=has_alphas[0],
                    alpha_cutoff=alpha_cutoffs[0],
                    tex_cache_dir=cache_dir,
                    tilt=tilt,
                )
            else:
                res = threedmr.write_model(
                    out_dir,
                    cid,
                    pos,
                    tris,
                    uvs,
                    nrm,
                    pngs,
                    gz,
                    tri_materials=tri_materials,
                    has_alpha=has_alphas,
                    alpha_cutoff=alpha_cutoffs,
                    tex_cache_dir=cache_dir,
                    tilt=tilt,
                )
            # a mesh over Torque's 16-bit IBO limit (threedmr.SPLIT_VERTEX_TRIGGER) comes back as several
            # sections (threedmr.write_model's "sections"), each its own TSStatic entry at the same pos -
            # a model with none (the common case) is just [res] itself
            sections = res.get("sections", [res])
            pos_xy = [round(float(c["xy"][0]), 2), round(float(c["xy"][1]), 2), 0.0]
            plate = None
            if surf is not None:
                try:
                    plate = threedmr.write_bridge_deck(out_dir, cid, surf, tilt)
                except Exception:
                    import revisit

                    revisit.fault()     # (built again next visit; meanwhile the model keeps its own collision)
                    log.warning("3dmr model %s: deck slab failed", cid, exc_info=True)
            for sec in sections:
                for (
                    material,
                    tex_name,
                    factor,
                    has_alpha,
                    alpha_cutoff,
                    double_sided,
                ) in _section_mat_tex(sec, factors, has_alphas, alpha_cutoffs, double_sideds):
                    texture_vfs = f"{tex_vfs_dir}/{tex_name}" if tex_name else None
                    threedmr.update_materials(
                        user_dir, material, texture_vfs, factor, has_alpha, alpha_cutoff,
                        double_sided=double_sided,
                    )
                entry = {"shape": f"{vfs_dir}/{sec['shape']}", "pos": pos_xy}
                if plate is not None:
                    entry["col"] = False    # (drawn only: the car is carried by its deck slab)
                entries.append(entry)
            if plate is not None:
                entries.append({"shape": f"{vfs_dir}/{plate['shape']}", "pos": pos_xy})
                log.info(
                    "3dmr model %s: deck slab %s (%d tris), ends at %s",
                    cid,
                    plate["shape"],
                    len(plate["tris"]),
                    [tuple(round(v, 2) for v in e) for e in plate["ends"]],
                )
            log.info(
                "3dmr model %s: baked as %s (%d section(s))",
                cid,
                res["shape"],
                len(sections),
            )
            if abs(res.get("support_dz", 0.0)) > SUPPORT_DZ_WARN_M:
                log.warning(
                    "3dmr model %s: support plane %.0f m off the ground (author terrain?)",
                    cid,
                    res["support_dz"],
                )
            radius = min(
                max(max(dx, dy) / 2.0 + SUPPRESS_MARGIN_M, SUPPRESS_MIN_M),
                SUPPRESS_MAX_M,
            )
            radii.append((cid, float(c["xy"][0]), float(c["xy"][1]), radius))
        except threedmr.UnsupportedModelError:
            pass  # already logged (threedmr.gltf_mesh): not a transient failure, no revisit.fault()
        except Exception:
            import revisit

            revisit.fault()
            log.warning("3dmr model %s failed", cid, exc_info=True)
    return entries, radii, []


def custom_models(frame, custom, ground, out_dir, vfs_dir, road_ends=None, items=None, origin=None):
    """(entries, radii) of custom's 3dmr.eu / local models, baked into out_dir (_custom_tile_entries);
    ([], []) without custom footprints or out_dir/vfs_dir. Exposed on its own so a caller that needs
    entries/radii before the road mesh is built can bake the models once here and hand this same
    (entries, radii) to road_tile() below, rather than road_tile() baking them a second time. items, origin:
    _custom_tile_entries' own nearest-footprint level - the caller's own tile_buildings result and its tile
    origin (x0, y0), when it has them (road_tile_items' "items", roads.tile_bbox(tx, ty)[:2])."""
    if not (custom and out_dir and vfs_dir):
        return [], [], []
    return _custom_tile_entries(
        frame, custom, ground, out_dir, vfs_dir, road_ends=road_ends, items=items, origin=origin
    )


def road_tile_items(
    frame, tx, ty, md, ground, decks=None, skip=frozenset(), cache_dir=None, urban_fn=None, widths=None,
    left_hand=False, clear_stats=None
):
    """tile_buildings of road tile (tx, ty) (items, custom), from the map cells under it, plus any
    3dmr.eu models found near it by geo-search (threedmr.region_ids/model_position), appended to custom as
    {"id", "xy" (anchor frame), "latlon" (its mapped lat/lon, for threedmr.osm_yaw's OSM lookup in
    _custom_tile_entries)}, and any local (no-3dmr-base) models whose own lat/lon (threedmr.local_models) falls
    in the tile, appended the same way plus a "local" key (the catalog entry itself - _custom_tile_entries'
    signal to skip fetch_model/gltf_mesh for it and use threedmr.load_local_model instead). decks: the
    bridge decks near it [(xy (k, 2), z (k,))] in the anchor frame (roads.BridgeProfiles.parts):
    buildings they run through get a way. skip: building ids left out (gas station canopies). cache_dir: threedmr's
    cache (None skips the geo-search and the local catalog: lite/far tiles and callers that do not bake custom
    models).

    The geo-search is the tile's region's (threedmr.region_ids: its REGION_DEG square out to SEARCH_CORRECT_R_M past
    its corners, one cached search): a model counts where model_position puts it, its desc-corrected position when
    its api point is stale and far off (id 22, the Dragon Bridge: api point 26 km off in the mountains).
    urban_fn, widths, left_hand: the road tile's road_specs arguments, its carriageways as drawn (road_clearance)."""
    import roads
    import threedmr

    box = roads.tile_bbox(tx, ty)
    x0, y0, x1, y1 = box
    # (the tile's aerial photo for the roofs' colours, fetched while the footprints are put together: RoofPhotoFuture)
    photo = RoofPhotoFuture(frame, x0, y0, x1, y1, getattr(md, "cache_dir", None))
    import threedbag
    bag3d = threedbag.Fetch(frame, x0, y0, x1, y1, getattr(md, "cache_dir", None))   # (the Dutch roofs, likewise)
    nyc = nycmassing.Fetch(frame, x0, y0, x1, y1, getattr(md, "cache_dir", None))   # (New York's setbacks, likewise)
    house_layers = ("land_use",) if francehouses.needs_residential_land(frame, box) else ()
    keys = roads.cells_of_tile(frame, tx, ty, margin=0.0)
    measured_wait(md, keys)
    cds = [
        md.cell(
            k,
            layers=("buildings", "building_parts", "roads", "infrastructure", "water", "land_use"),
            kind="demand",
        )
        for k in keys
    ]
    items = tile_buildings(
        frame,
        x0,
        y0,
        x1,
        y1,
        [c.layers.get("buildings") for c in cds],
        ground,
        own=box if hasattr(box, "owns") else None,
        part_layers=[c.layers.get("building_parts") for c in cds],
        roofs=measured_roofs(md, frame, x0, y0, x1, y1),
        photo=photo,
        bag3d=bag3d,
        nyc=nyc,
        west_town=district_town(frame, tx, ty, md),
        roads_xy=road_lines(frame, [c.layers.get("roads") for c in cds], x0, y0),
        passage_xy=road_lines(frame, [c.layers.get("roads") for c in cds], x0, y0,
                              classes=ROAD_PASSAGE_CLASSES, skip_sub=ROAD_PASSAGE_SKIP),
        rails_xy=rail_lines(frame, [c.layers.get("roads") for c in cds], x0, y0),
        ways_xy=way_lines(frame, [c.layers.get("roads") for c in cds], x0, y0),
        tunnels_xy=tunnel_lines(frame, [c.layers.get("roads") for c in cds], x0, y0),
        garages_xy=garage_rings(
            frame, [c.layers.get("infrastructure") for c in cds], x0, y0
        ),
        supports=support_rings(
            frame, [c.layers.get("infrastructure") for c in cds], x0, y0
        ),
        tanks=tank_rings(frame, [c.layers.get("infrastructure") for c in cds], x0, y0),
        industrial=industrial_rings(frame, [c.layers.get("land_use") for c in cds], x0, y0),
        bridges_xy=bridge_lines(frame, [c.layers.get("roads") for c in cds], x0, y0),
        water_xy=water_rings(frame, [c.layers.get("water") for c in cds], x0, y0),
        decks=[
            (
                np.asarray(d[0], np.float64)[:, :2] - [x0, y0],
                np.asarray(d[1], np.float64),
            )
            + tuple(d[2:3])
            for d in decks
        ]
        if decks
        else None,
        skip=skip,
        house_land_layers=[c.layers.get("land_use") for c in cds] if house_layers else (),
        carriageways=carriageway_segments(frame, [c.layers.get("roads") for c in cds], x0, y0, x1 - x0,
                                          urban_fn=urban_fn, widths=widths, left_hand=left_hand),
        clear_stats=clear_stats,
    )
    custom = []
    if cache_dir is not None:
        import logging

        log = logging.getLogger("earthd.buildings")
        clat, clon, _ = frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.0)
        # (the region's models, one cached search per threedmr.REGION_DEG square: every model whose position -
        # model_position's, corrected where its api point is stale - stands in the tile; the PR's two per-tile
        # searches, tile radius and SEARCH_CORRECT_R_M, found the same models with a request per tile)
        for mid in threedmr.region_ids(float(clat), float(clon), cache_dir):
            mid = int(mid)
            try:
                mlat, mlon, _corrected, source = threedmr.model_position(mid, cache_dir)
                mx, my, _ = frame.geodetic_to_enu(mlat, mlon, 0.0)
            except Exception:
                log.warning("3dmr model %s: info failed", mid, exc_info=True)
                continue
            if x0 <= mx < x1 and y0 <= my < y1:
                custom.append(
                    {"id": mid, "xy": (float(mx), float(my)), "latlon": (mlat, mlon),
                     "source": source}
                )
        for entry in threedmr.local_models(cache_dir):
            mlat, mlon = float(entry["lat"]), float(entry["lon"])
            mx, my, _ = frame.geodetic_to_enu(mlat, mlon, 0.0)
            if x0 <= mx < x1 and y0 <= my < y1:
                custom.append(
                    {
                        "id": entry["id"],
                        "xy": (float(mx), float(my)),
                        "latlon": (mlat, mlon),
                        "local": entry,
                        "source": "local",
                    }
                )
    return items, custom


def near_rings(frame, tx, ty, md, margin=2.0, skip=frozenset()):
    """The outer rings (k, 2) in the anchor frame of every building of the map within margin m of road tile (tx, ty):
    its own and its neighbours' (road_tile_items has only the ones whose centre is in it). skip: ids left out."""
    import roads
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    out = []
    seen = set()
    for k in roads.cells_of_tile(frame, tx, ty, margin=margin):
        f = md.cell(k, layers=("buildings",), kind="demand").layers.get("buildings")
        if f is None or not len(f):
            continue
        bx = f.boxes(frame)
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in seen or fid in skip:
                continue
            b = bx[i]
            if b[2] < x0 - margin or b[0] > x1 + margin or b[3] < y0 - margin or b[1] > y1 + margin:
                continue
            seen.add(fid)
            for part in f.parts(i):
                if len(part) and len(part[0]) >= 3:
                    e, n = mapgeom.enu_xy(f, frame, part[0])
                    out.append(np.c_[e, n])
    return out


def bridge_footprints(frame, tx, ty, cache_dir, cell_fn=None):
    """[(footprint, (x, y) of its model in frame, model id)] of the 3dmr model bridges whose deck reaches road tile
    (tx, ty) (threedmr.bridge_footprint: every tile reads the same per-model answer, the one the model is baked in
    or not), found as road_tile_items finds models (threedmr.region_ids, model_position)
    within threedmr.BRIDGE_REACH_M of the tile. cell_fn (map cell key -> its roads layer): each model moved onto its
    mapped bridge (bridge_align: every tile the same, place_bridge_models bakes it there). [] without cache_dir. A
    model it could not read marks the build incomplete (revisit.fault: tried again next visit)."""
    import logging
    import revisit
    import roads
    import threedmr

    if cache_dir is None:
        return []
    log = logging.getLogger("earthd.buildings")
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    clat, clon, _ = frame.enu_to_geodetic(cx, cy, 0.0)
    reach = math.hypot((x1 - x0) / 2, (y1 - y0) / 2) + threedmr.BRIDGE_REACH_M
    out = []
    for mid in threedmr.region_ids(float(clat), float(clon), cache_dir):
        mid = int(mid)
        try:
            mlat, mlon, _corrected, _source = threedmr.model_position(mid, cache_dir)
            mx, my, _ = frame.geodetic_to_enu(mlat, mlon, 0.0)
            mx, my = float(mx), float(my)
            if math.hypot(mx - cx, my - cy) > reach:
                continue
            c = {"id": mid, "xy": (mx, my), "latlon": (mlat, mlon)}

            def mesh(_c=c):
                pos, _uvs, tris, _nrm, _tex, _tm = _model_mesh(_c, cache_dir)
                return _centred(pos), tris, _is_bridge(pos, threedmr.cached_info(_c["id"], cache_dir))

            fp = threedmr.bridge_footprint(mid, cache_dir, mesh)
            if fp.get("bridge") and cell_fn is not None:
                mx, my = bridge_align(frame, fp, (mx, my), (mid, mlat, mlon), cell_fn)
        except threedmr.UnsupportedModelError:
            continue
        except Exception:
            log.warning("3dmr model %s: bridge footprint failed", mid, exc_info=True)
            revisit.fault()
            continue
        if fp.get("bridge"):
            out.append((fp, (mx, my), mid))
    return out


def place_bridge_models(custom, fps):
    """custom (road_tile_items') with each model bridge of fps (bridge_footprints') standing where that put it (onto
    its mapped bridge): the model baked over the very spot the procedural bridge is left out of."""
    at = {mid: centre for _fp, centre, mid in fps}
    return [dict(c, xy=tuple(at[c["id"]])) if c.get("id") in at and "local" not in c else c for c in custom]


BRIDGE_ALIGN_MAX_M = 40.0  # m a model bridge is moved at most, along its deck and across it, onto its mapped bridge
BRIDGE_ALIGN_NEAR_M = 15.0  # m beside its deck (and past its ends, plus the above) a mapped bridge is taken as its own
BRIDGE_ALIGN_STEP_M = 4.0  # m between the mapped bridges' points weighed
BRIDGE_ALIGN_POINTS = 600  # ... at most this many of them
BRIDGE_ALIGN_GRID_M = (1.0, 0.5)  # m: the moves tried along / across
_ALIGN_MEMO = {}            # (model id, its position) -> its aligned (lat, lon) (per process: every tile the same)


def mapped_bridge_lines(frame, road_layers, box):
    """[xy (k, 2) every BRIDGE_ALIGN_STEP_M] of the mapped bridges (any road, railway or walkway's bridge level
    ranges: roads.level_ranges via railways._pieces) of road_layers coming within box (x0, y0, x1, y1), each feature
    once."""
    import railways

    out, seen = [], set()
    for feats in road_layers:
        if feats is None or len(feats) == 0:
            continue
        fb = feats.boxes(frame)
        hit = (fb[:, 0] <= box[2]) & (fb[:, 2] >= box[0]) & (fb[:, 1] <= box[3]) & (fb[:, 3] >= box[1])
        for i in np.nonzero(hit)[0]:
            i = int(i)
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            for kind, rings in railways._pieces(feats, i):
                if kind != "bridge":
                    continue
                for ring in rings:
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    if len(e) >= 2:
                        out.append(railways._resample(np.c_[e, n], BRIDGE_ALIGN_STEP_M))
    return out


def bridge_shift(fp, centre, lines):
    """(du, dv): the move along / across a model bridge's deck (threedmr.bridge_footprint's fp, its model at centre)
    that lays it over its mapped bridge (lines: mapped_bridge_lines', in centre's frame), or (0, 0): a 3dmr.eu upload's
    point is often tens of metres off (Perth's Narrows and Mount Henry bridges, GitHub #1071: ~18 m along, their decks
    ending short of the roads that leave them, a gap into the river). Of the moves within BRIDGE_ALIGN_MAX_M (on
    BRIDGE_ALIGN_GRID_M), those that put the most of the lines' points (BRIDGE_ALIGN_STEP_M apart, those within
    BRIDGE_ALIGN_NEAR_M of the deck) on the deck (within threedmr.BRIDGE_FOOT_MARGIN_M of its sides, where the
    procedural bridge is left out); of them the one that leaves the lines on it (half their points or more: not a
    ramp alongside) as far past one end as past the other (a mapped bridge longer than the deck: its middle), then the
    least move."""
    import threedmr

    cols = fp["cols"]
    have = np.array([i for i, c in enumerate(cols) if c], np.int64)
    if not len(have) or not lines:
        return 0.0, 0.0
    lo = np.full(len(cols), np.nan)
    hi = np.full(len(cols), np.nan)
    for i in have:
        lo[i], hi[i] = cols[i]
    ua, ub = fp["u0"] + fp["du"] * have[0], fp["u0"] + fp["du"] * have[-1]
    span = np.arange(have[0], have[-1] + 1)          # (the gaps inside the deck: its neighbours' widths)
    ok = np.isfinite(lo[span])
    lo[span] = np.interp(span, span[ok], lo[span][ok])
    hi[span] = np.interp(span, span[ok], hi[span][ok])
    reach = BRIDGE_ALIGN_MAX_M + BRIDGE_ALIGN_NEAR_M
    pts, owner = [], []
    for xy in lines:
        u, v = threedmr.deck_uv(fp, np.asarray(xy, np.float64)[:, :2] - np.asarray(centre, np.float64))
        i = np.clip(np.rint((u - fp["u0"]) / fp["du"]).astype(np.int64), have[0], have[-1])
        near = (u >= ua - reach) & (u <= ub + reach) & (v >= lo[i] - BRIDGE_ALIGN_NEAR_M) \
            & (v <= hi[i] + BRIDGE_ALIGN_NEAR_M)
        if near.mean() >= 0.5:
            pts.append(np.c_[u[near], v[near]])
            owner.append(np.full(int(near.sum()), len(owner)))
    if not pts:
        return 0.0, 0.0
    P, line = np.concatenate(pts), np.concatenate(owner)
    if len(P) > BRIDGE_ALIGN_POINTS:
        keep = slice(None, None, int(np.ceil(len(P) / BRIDGE_ALIGN_POINTS)))
        P, line = P[keep], line[keep]
    nl = len(pts)
    hot = (line[None, :] == np.arange(nl)[:, None]).astype(np.float64)
    per = hot.sum(1)
    gu, gv = BRIDGE_ALIGN_GRID_M
    dus = np.arange(-BRIDGE_ALIGN_MAX_M, BRIDGE_ALIGN_MAX_M + gu / 2, gu)
    dvs = np.arange(-BRIDGE_ALIGN_MAX_M, BRIDGE_ALIGN_MAX_M + gv / 2, gv)
    m = threedmr.BRIDGE_FOOT_MARGIN_M
    onto = np.empty((len(dus), len(dvs)))
    lean = np.empty((len(dus), len(dvs)))
    for a, du in enumerate(dus):
        u = P[:, 0] - du                            # (the points in the deck's frame once it has moved by du)
        i = np.clip(np.rint((u - fp["u0"]) / fp["du"]).astype(np.int64), have[0], have[-1])
        v = P[:, 1][:, None] - dvs[None, :]
        beside = (v >= lo[i][:, None] - m) & (v <= hi[i][:, None] + m)     # (past the ends: the end's width)
        on = beside & ((u >= ua) & (u <= ub))[:, None]
        onto[a] = on.sum(0)
        frac = (dnp.matmul(hot, on)) / per[:, None]            # (nl, dvs): each line's share on the deck
        mine = frac[line] >= 0.5                    # (each point: its line lies on the deck)
        past_a = np.where(mine, np.maximum(ua - u, 0.0)[:, None], 0.0).max(0)
        past_b = np.where(mine, np.maximum(u - ub, 0.0)[:, None], 0.0).max(0)
        lean[a] = np.abs(past_a - past_b)
    a, b = np.nonzero(onto >= onto.max())
    k = int(np.lexsort((dnp.hypot(dus[a], dvs[b]), np.round(lean[a, b])))[0])     # (centred, then the least move)
    return float(dus[a[k]]), float(dvs[b[k]])


def bridge_align(frame, fp, centre, key, cell_fn):
    """centre (x, y) of a model bridge (bridge_footprints') moved onto its mapped bridge (bridge_shift, over the mapped
    bridges of the map cells under its deck: cell_fn(key) -> their roads layer), the same in every tile (kept per
    model: _ALIGN_MEMO)."""
    import logging
    import mapcells
    import threedmr

    mid, mlat, mlon = key
    k = (int(mid), threedmr.FEATURE_VERSION, round(float(mlat), 7), round(float(mlon), 7))
    got = _ALIGN_MEMO.get(k)
    if got is None:
        cols = [(i, c) for i, c in enumerate(fp["cols"]) if c]
        us = [fp["u0"] + fp["du"] * i for i, _c in cols]
        u = np.r_[us, us]
        v = np.r_[[c[0] for _i, c in cols], [c[1] for _i, c in cols]]
        x, y = threedmr.deck_xy(fp, u, v)
        m = BRIDGE_ALIGN_MAX_M + BRIDGE_ALIGN_NEAR_M
        box = (centre[0] + x.min() - m, centre[1] + y.min() - m, centre[0] + x.max() + m, centre[1] + y.max() + m)
        la, lo, _ = frame.enu_to_geodetic(np.array([box[0], box[2]]), np.array([box[1], box[3]]), np.zeros(2))
        layers = [cell_fn(c) for c in mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()),
                                                               float(lo.max()))]
        du, dv = bridge_shift(fp, centre, mapped_bridge_lines(frame, layers, box))
        dx, dy = threedmr.deck_xy(fp, du, dv)
        nla, nlo, _ = frame.enu_to_geodetic(centre[0] + float(dx), centre[1] + float(dy), 0.0)
        got = (float(nla), float(nlo))
        if all(layer is not None for layer in layers):      # (a cell not had: asked again by the next tile)
            _ALIGN_MEMO[k] = got
        if du or dv:
            logging.getLogger("earthd.buildings").info(
                "3dmr model %s: moved %.1f m along / %.1f m across its deck onto its mapped bridge", mid, du, dv)
    x, y, _ = frame.geodetic_to_enu(got[0], got[1], 0.0)
    return float(x), float(y)


def _under_bridges(fps, xy):
    """Mask (k,) of points xy (k, 2) under any of bridge_footprints' fps."""
    import threedmr

    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    m = np.zeros(len(xy), bool)
    for fp, centre, _mid in fps:
        m |= threedmr.under_footprint(fp, centre, xy)
    return m


def _mostly_under(fps, xy):
    xy = np.asarray(xy, np.float64)
    return len(xy) > 0 and _under_bridges(fps, xy[:, :2]).mean() >= 0.5


def bridge_stub_chunks(fps, chunks):
    """chunks without the procedural bridge standing where a model bridge (bridge_footprints' fps) does: a bridge
    chunk (roads._bridge_chunks: a "deck" surface, or a "fixed" line on it - its lane markings at the stub's
    heights) half or more of whose nodes are under one (the stub's slab, abutments, parapets, skin and markings all
    come from these; the ground roads it joins stay; tunnels too)."""
    if not fps:
        return chunks
    return [c for c in chunks
            if not ((c.get("deck") or c.get("fixed")) and not c.get("tunnel") and c.get("n") is not None
                    and len(c["n"]) and _mostly_under(fps, c["n"]))]


def bridge_stub_parts(fps, parts):
    """roads.BridgeProfiles.parts' (fid, spec, xy, z, lon) without those under a model bridge (their guard rails,
    medians and junction pads stood at the stub's heights)."""
    if not fps:
        return parts
    return [p for p in parts if not _mostly_under(fps, p[2])]


def bridge_road_ends(fps, bridges, cells, frame=None, ground_xy=None):
    """{model id: [(x, y, z)]}: the ends of the mapped bridges (roads.BridgeProfiles.parts: whole, with their deck
    heights - the ground road's where they leave the bridge) under each model bridge of fps, in the tile's frame:
    where the model's deck is to meet the road network (threedmr.bridge_tilt road_ends). A model with no road bridge
    under it (a footbridge: Perth's Elizabeth Quay Bridge, GitHub #1071, its ends sank into the inlet off the deck's
    own ends; a railway's bridge), given frame and ground_xy(x (k,), y (k,)) -> z: the ends of the walkway's or the
    railway's bridge under it (mapped_bridge_lines) on the ground there."""
    import threedmr

    out = {}
    for fp, centre, mid in fps:
        cols = [(i, c) for i, c in enumerate(fp["cols"]) if c]
        if not cols:
            continue
        us = np.array([fp["u0"] + fp["du"] * i for i, _c in cols])
        x, y = threedmr.deck_xy(fp, np.r_[us, us], np.r_[[c[0] for _i, c in cols], [c[1] for _i, c in cols]])
        box = (centre[0] + float(x.min()), centre[1] + float(y.min()), centre[0] + float(x.max()),
               centre[1] + float(y.max()))
        ends = []
        for _fid, _spec, xy, z, _lon in bridges.parts(cells, box):
            xy = np.asarray(xy, np.float64)[:, :2]
            if len(xy) and threedmr.under_footprint(fp, centre, xy).mean() >= 0.5:
                ends += [(float(xy[0, 0]), float(xy[0, 1]), float(z[0])),
                         (float(xy[-1, 0]), float(xy[-1, 1]), float(z[-1]))]
        if not ends and frame is not None and ground_xy is not None:
            tips = []
            for xy in mapped_bridge_lines(frame, cells, box):
                if len(xy) >= 2 and threedmr.under_footprint(fp, centre, xy).mean() >= 0.5:
                    tips += [xy[0], xy[-1]]
            if tips:
                tips = np.asarray(tips, np.float64)
                z = np.asarray(ground_xy(tips[:, 0], tips[:, 1]), np.float64).reshape(-1)
                ends = [(float(x), float(y), float(zz)) for (x, y), zz in zip(tips, z)]
        if ends:
            out[mid] = ends
    return out


def bridge_stub_piers(fps, piers, beams):
    """(piers, beams) of the procedural bridges (piers_in / beams_in) without those under a model bridge."""
    if not fps:
        return piers, beams
    piers = [p for p in piers if not _under_bridges(fps, [[p[0], p[1]]])[0]]
    beams = [b for b in beams
             if not _under_bridges(fps, [[(b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2]])[0]]
    return piers, beams


def bridge_stub_props(fps, props):
    """props rows ([group, x, y, z, ...]) without those standing on / under a model bridge's deck."""
    if not fps or not props:
        return props
    return [r for r in props
            if not (isinstance(r, (list, tuple)) and len(r) > 3 and _under_bridges(fps, [[r[1], r[2]]])[0])]


TOWN_COVERAGE = 0.16  # a tile whose footprints cover less than this share is a town / suburb (heights)
# Small towns of the arid West (Bishop, Moab: one-storey main streets with false fronts, ranch houses), told
# from cities by the footprint share of the district around a tile (1 km: Bishop 0.14, Moab 0.11; Frederick
# 0.22, Fells Point 0.27): a tile's own share cannot (a main-street block is as dense as a city's).
DISTRICT_M = 1000.0
WEST_TOWN_COVERAGE = 0.18
WEST_TOWN_ONE_STOREY = 0.75  # share of predicted one-storey buildings there
FALSE_FRONT_M = 1.2  # m of parapet over a one-storey shop there
POLES_COVERAGE = 0.30  # ... and its streets get wired wooden poles (furniture; Ocean City's photos: poles
# everywhere; Fells Point's dense core ~0.45 keeps lamp posts)


def coverage(items, tile=256.0):
    """The share of the tile (a square of side tile m: roads.tile_side) covered by building footprints."""
    return sum(_area2(s.ring) / 2 for s, _, _ in items if (s.min_h or 0) <= 0.5) / (tile * tile)


def centres(items, tx, ty):
    """The anchor-frame (x, y) centres of a road tile's buildings (road_tile_items' rings are tile-local)."""
    import roads
    x0, y0, _, _ = roads.tile_bbox(tx, ty)
    return [(float(s.ring[:, 0].mean() + x0), float(s.ring[:, 1].mean() + y0)) for s, _, _ in items]


def road_tile(
    frame,
    tx,
    ty,
    md,
    ground,
    lines=None,
    props=None,
    items=None,
    custom=None,
    out_dir=None,
    vfs_dir=None,
    entries=None,
    radii=None,
):
    """The buildings of road tile (tx, ty) (roads.TILE squares of the anchor frame): ((mesh, collider boxes,
    collision mesh), [b3dmr entries]). The first element is None without any procedural buildings. md:
    mapdata.MapData; ground(lat, lon): the physics ground without the road lift. lines (the road centrelines,
    roads.build_tile) and props (its prop rows): the houses' front doors are added to props, after custom's
    models (if any) have suppressed the footprints under them (_suppress_under_models) - a door on a stub
    that no longer builds would be a door on nothing. items, custom: road_tile_items' result (items, custom)
    when the caller has it (else fetched here). out_dir, vfs_dir: the session folder and its game path, to
    bake custom's 3dmr.eu models into (_custom_tile_entries); without them (or without custom footprints) the
    second element is [] and nothing is suppressed. entries, radii: custom_models()' result when the caller
    already baked the models (before roads.build_tile) - baked here instead when neither is given."""
    import os
    import roads

    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    if items is None:
        cache_dir = os.path.dirname(out_dir) if out_dir else None
        items, custom = road_tile_items(
            frame, tx, ty, md, ground, cache_dir=cache_dir
        )
    if entries is None and radii is None:
        entries, radii, _boxes = custom_models(
            frame, custom, ground, out_dir, vfs_dir, items=items, origin=(x0, y0)
        )
    if radii:
        items = _suppress_under_models(items, radii, x0, y0)
    if props is not None and lines:
        props.extend(doors(items, lines, x0, y0))
    meshes = build_parts(items, with_lod=True)
    bld = (
        (meshes, collider_boxes(items, x0, y0), collision_mesh(items))
        if meshes
        else None
    )
    return bld, entries


SUPPRESS_MARGIN_M = 5.0  # a baked 3dmr.eu model suppresses procedural buildings this far past its own bbox
SUPPRESS_MIN_M = 15.0  # ... but never less than this (a tiny model, id 41 ~0.5 m, must not blank a block)
SUPPRESS_MAX_M = 150.0  # ... nor more than this (a huge one, id 29 ~61 km, must not blank the whole tile)
MODEL_MAX_M = 1000.0  # a model wider than this is not placed: a district or stray geometry in the file (3dmr.eu ids 6
# and 7: 1.4-1.9 km of it around a building; 29: 61 km), not a building or a bridge (the Dragon Bridge: 842 m)
SUPPORT_DZ_WARN_M = 30.0  # threedmr.write_model's own support_dz past this is only logged, never dropped -
# an author's own legitimate elevated deck (a bridge, a viewing platform) is also a large support_dz
BRIDGE_ASPECT = 3.0  # a model's horizontal bbox this much longer one way than the other is a bridge deck
BRIDGE_MIN_TAGGED_M = 10.0  # a model its 3dmr.eu record calls a bridge is one from this long (_is_bridge)
BRIDGE_MIN_SPAN_M = 100.0  # ... and only once it also reaches this far (a narrow wall or jetty is not a bridge
# deck's stand-in, whatever its own aspect ratio): only a model this shape gets its road-surface triangles
# carved out (_custom_tile_entries, threedmr.carve_bridge_deck_mask) so it stands as pure decoration over the
# procedural road stub it would otherwise fight for height - a monument or a building never is carved

FAR_LAYERS = ("buildings", "building_parts", "infrastructure", "roads", "water")


def measured_wait(md, keys, wait=True):
    """A building tile's wait for its cells' measured buildings that come per map cell (France's BD TOPO: MapData.cell
    joins them once they are here), all together and bounded (bdtopo.WAIT_S): their downloads run beside the cells'
    own from the tile's start (MapData.warm). wait=False: started, not waited for (the skyline's: render-only, built
    again on a later visit). A map without them: nothing."""
    w = getattr(md, "bdtopo_wait", None)
    if w is not None:
        w(keys, wait)


def measured_roofs(md, frame, x0, y0, x1, y1):
    """The measured roofs (lod2roofs.Records) of a tile, or None: none there, or a map without them."""
    src = getattr(md, "lod2_roofs", None)
    return src.tile(frame, x0, y0, x1, y1) if src is not None else None


def far_needs(frame, tile, ty):
    """The map cells and layers far_items reads for road tile (tile, ty) (blockworker: fetched together first)."""
    import roads
    box = roads.tile_bbox(tile, ty)
    house_layers = ("land_use",) if francehouses.needs_residential_land(frame, box) else ()
    return list(roads.cells_of_tile(frame, tile, ty, margin=0.0)), FAR_LAYERS + house_layers


def far_items(frame, tile, ty, md, ground, lamp_region=None, urban_fn=None, widths=None, left_hand=False):
    """The far (lite) buildings of road tile (tile, ty) (an anchor tile, or a geotile: tile a geotiles.GeoBox, ty None:
    its own buildings, those the road geotile of the same key holds) in frame: (items, lamps [[x, y, z], ...] of its
    streets in frame, the tile's box)."""
    import roads
    box = roads.tile_bbox(tile, ty)
    x0, y0, x1, y1 = box
    own = box if hasattr(box, "owns") else None
    keys, layers = far_needs(frame, tile, ty)
    nyc = nycmassing.Fetch(frame, x0, y0, x1, y1, getattr(md, "cache_dir", None))   # (New York's setbacks)
    measured_wait(md, keys)
    import mapcells
    cds = mapcells.load_cells(md, keys, layers)
    items = tile_buildings(frame, x0, y0, x1, y1, [c.layers.get("buildings") for c in cds], ground, own=own,
                           part_layers=[c.layers.get("building_parts") for c in cds], nyc=nyc,
                           house_land_layers=[c.layers.get("land_use") for c in cds],
                           roofs=measured_roofs(md, frame, x0, y0, x1, y1),
                           west_town=district_town(frame, tile, ty, md),
                           garages_xy=garage_rings(frame, [c.layers.get("infrastructure") for c in cds], x0, y0),
                           supports=support_rings(frame, [c.layers.get("infrastructure") for c in cds], x0, y0),
                           bridges_xy=bridge_lines(frame, [c.layers.get("roads") for c in cds], x0, y0),
                           water_xy=water_rings(frame, [c.layers.get("water") for c in cds], x0, y0),
                           ml_roads_xy=road_lines(frame, [c.layers.get("roads") for c in cds], x0, y0),
                           rails_xy=rail_lines(frame, [c.layers.get("roads") for c in cds], x0, y0, size=x1 - x0),
                           ways_xy=way_lines(frame, [c.layers.get("roads") for c in cds], x0, y0, size=x1 - x0),
                           carriageways=carriageway_segments(frame, [c.layers.get("roads") for c in cds], x0, y0,
                                                             x1 - x0, urban_fn=urban_fn, widths=widths,
                                                             left_hand=left_hand))
    size = max(x1 - x0, y1 - y0)
    lamps = far_lamps(frame, [c.layers.get("roads") for c in cds], x0, y0, size,
                      built_squares(items, 0.0, 0.0, size), ground, lamp_region, own=own)
    return items, lamps, box


def far_tile(frame, tx, ty, md, ground, out_dir, vfs_dir, aid, lamp_region=None, urban_fn=None, widths=None,
             left_hand=False):
    """A far (render-only) building tile written to the session folder: (vfs path of its JSON, building
    count) or None. The lite mesh of the same buildings as road_tile's."""
    import json
    import os
    items, lamps, (x0, y0, _x1, _y1) = far_items(frame, tx, ty, md, ground, lamp_region, urban_fn, widths, left_hand)
    mesh = build_parts(items, lite=True)      # (meshes under the 16-bit indices, one shape: write)
    data = {"v": 1}
    if lamps:
        data["lamps"] = lamps
    if mesh:
        shape = f"bldfar_{aid}_{tx}_{ty}.cdae"
        write(os.path.join(out_dir, shape), mesh)
        data["shape"] = f"{vfs_dir}/{shape}"
        data["pos"] = [x0, y0, 0.0]
    name = f"bldfar_{aid}_{tx}_{ty}.json"
    path = os.path.join(out_dir, name)
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        f.write(json.dumps(data))       # (json.dump to a file: the pure-Python encoder)
    staging.replace(tmp, path)
    return f"{vfs_dir}/{name}", len(items)


# ------------------------------------------------------------------------------------------- far street lamps
# The street lamps past the road tiles (the user, 2026-10-27: "extend the street and window lights as far as the
# buildings render"): the far and skyline tiles carry their streets' lamps as glow points (trees.lua's lampglow group,
# night only), placed along the mapped roads by furniture.py's spacing: the town streets in built-up squares (at least
# FAR_LAMP_MIN_BLD buildings in its 256 m square: the near rule's land cover is not at hand here), the highways there
# and every ramp. FAR_LAMP_Z over the ground, over the carriageway's edge.
FAR_LAMP_MIN_BLD = 8
FAR_LAMP_Z = 8.0
FAR_LAMP_MAX = 4000         # a tile's most (the skyline's 1 km squares: a dense city's ~3000)


def built_squares(items, x0, y0, size, sq=256.0):
    """The set of (i, j) 256 m squares of a tile (x0, y0 its corner; items' rings tile-relative) holding at least
    FAR_LAMP_MIN_BLD buildings."""
    import collections
    n = collections.Counter()
    for spec, _gmin, _gmax in items:
        c = np.asarray(spec.ring, np.float64)[:, :2].mean(0)
        n[(int(c[0] // sq), int(c[1] // sq))] += 1
    return {k for k, v in n.items() if v >= FAR_LAMP_MIN_BLD}


def far_lamps(frame, road_layers, x0, y0, size, built, ground, region=None, sq=256.0, own=None):
    """The lamps of a far tile's streets: [x, y, z] in frame (rounded; z the frame's up at FAR_LAMP_Z over the ground),
    at most FAR_LAMP_MAX. built: the tile's built-up 256 m squares (built_squares); ground(lat, lon) the ground
    (arrays); region: furniture.lamp_region's (the spacing); own: a geotile's box (geotiles.OwnedBox): only its own
    lamps (each in one tile)."""
    import furniture
    import roads as roadsmod
    import fast
    if not built:
        return []
    table = furniture.lamp_spacing(region)
    hw = "na" if furniture._na(region) else None
    pts, off, spacing, phase, side, lit_any = [], [0], [], [], [], []
    for f in road_layers:
        if f is None or len(f.kind) == 0:
            continue
        specs = roadsmod.road_specs(f)
        bb = f.boxes(frame)
        near = np.flatnonzero((bb[:, 2] - x0 >= -20) & (bb[:, 0] - x0 <= size + 20)
                              & (bb[:, 3] - y0 >= -20) & (bb[:, 1] - y0 <= size + 20))
        for i in near.tolist():
            sp = specs[i]
            if sp is None:
                continue
            highway = sp.cls in furniture.HIGHWAY_CLASSES
            if highway and not furniture.highway_lit(region, sp.link, True):
                continue
            if highway:
                every = (furniture.LINK_LAMP_SPACING if sp.link else furniture.HIGHWAY_LAMP_SPACING)[hw]
                rules = [(every, -float(sp.dir) if sp.dir else (1.0 if furniture._h01(str(f.ids[i]), "pside") < 0.5
                                                               else -1.0), 0.0)]
            elif sp.cls in table:
                every, both = table[sp.cls]
                sd = 1.0 if furniture._h01(str(f.ids[i]), "side") < 0.5 else -1.0
                rules = [(every, sd, 0.0)] + ([(every, -sd, every / 2)] if both else [])
            else:
                continue
            ph0 = furniture._h01(str(f.ids[i]), "pphase")
            reach = max(1.0, sp.width / 2 - 1.0)
            parts = roadsmod.ground_parts(f, i) if roadsmod.level_ranges(f, i) else [None]
            for part in parts:
                if part is None:
                    a = f.coord_off[f.ring_off[f.part_off[i]]]
                    b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
                    part = f.lonlat[a:b]
                if len(part) < 2:
                    continue
                e, n = mapgeom.enu_xy(f, frame, np.asarray(part, np.float64))
                xy = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], 1)
                for every, sd, shift in rules:
                    pts.append(xy)
                    off.append(off[-1] + len(xy))
                    spacing.append(every)
                    phase.append(ph0 * every + shift)
                    side.append(sd * reach)
                    lit_any.append(bool(highway and sp.link))
    if not pts:
        return []
    fast.require(87, "lamp_stations")
    st = fast._ef.lamp_stations(np.ascontiguousarray(np.concatenate(pts)), np.asarray(off, np.int64),
                                np.asarray(spacing, np.float64), np.asarray(phase, np.float64),
                                np.asarray(side, np.float64), [x0, y0, x0 + size, y0 + size])
    if not len(st):
        return []
    keep = [k for k in range(len(st)) if lit_any[int(st[k, 2])]
            or (int((st[k, 0] - x0) // sq), int((st[k, 1] - y0) // sq)) in built]
    st = st[keep]
    if own is not None and len(st):
        st = st[np.asarray(own.owns(st[:, 0], st[:, 1]), bool)]
    st = st[:FAR_LAMP_MAX]
    if not len(st):
        return []
    lat, lon, _ = frame.enu_to_geodetic(st[:, 0], st[:, 1], 0.0)
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    h = np.asarray(ground(lat, lon), np.float64) + FAR_LAMP_Z
    z = np.asarray(frame.geodetic_to_enu(lat, lon, h)[2], np.float64)     # (the frame's up: the Earth's curvature)
    return np.round(np.c_[st[:, 0], st[:, 1], z], 1).tolist()


# ------------------------------------------------------------------------------------------- the skyline
# Past the far buildings, a lighter ring out to SKY_SEND_M (the user, 2026-10-27: "see how far away we can render
# buildings"): one mesh per SKY_UNIT x SKY_UNIT road tiles (1 km), only the buildings big enough to see that far
# (SKY_MIN_H tall or SKY_MIN_AREA_M2 large), the lite mesh, the tallest first up to the 16-bit vertex budget. Far
# buildings to 4.8 km as 256 m tiles cost +37 % frame time at Federal Hill (1129 objects, ~0.7 GB of meshes); a
# skyline tile stands for 16 of them and holds a fraction of their buildings. The far buildings are sent for every
# tile of a skyline square that comes within FAR_SEND_M (vegetation.Tracker group), and the mod shows a skyline tile
# only while none of its squares' far buildings are there: no overlap, no hole.
SKY_UNIT = 4
SKY_SEND_M = float(os.environ.get("BNE_SKY_SEND_M", "4800"))
SKY_DROP_M = SKY_SEND_M + 800.0
SKY_PER_POSE = 2
SKY_MIN_AREA_M2 = 40.0      # (sheds left out ...
SKY_SLENDER_H = 30.0        # ... but not a mast or a spire this tall)
# The skyline from the buildings' parts too: Woolworth's tower (parts to 241 m over a 120 m outline) was 120 m in it, so
# from the Brooklyn Bridge (0.8 km) it was missing, and One World Trade Center a box in the outline's default look with
# no spire (its parts: blue glass, a 121 m mast; the bridge view against a 360 photo and a player's screenshot,
# 2026-11-10). BNE_SKY_PARTS=off: the outlines alone.
SKY_PARTS = os.environ.get("BNE_SKY_PARTS", "on") != "off"
SKY_PARTS_MIN_M = 150.0     # m: only a building this tall is drawn from its parts there (16 km2 of Midtown's skyline:
                            # every building's parts +60 % items and +53 % vertices; from 60 m +50 %; from 150 m +14 %
                            # items, +13 % vertices, Woolworth's and the towers' crowns and masts kept)
SKY_BOX_AREA_M2 = 600.0     # a footprint smaller than this is drawn as its oriented box
SKY_MAX_CORNERS = 12        # a larger one with more corners: every k-th kept


# The mean colour of each building material's base texture (tools: a 64 x 64 resize of each material's
# baseColorMap from the game's content zips, 2026-10-27): a skyline building is drawn in its facade's and its roof's
# mean colour times their tints (MARBLE, nearly white, sampled at one texel: the vertex colour is the colour)
MAT_MEAN = {"bne_bld_facade": (0.679, 0.676, 0.683), "bne_bld_shop": (0.669, 0.691, 0.699),
            "bne_bld_office": (0.47, 0.493, 0.492), "bne_bld_roof_flat": (0.432, 0.431, 0.43),
            "bne_bld_roof_slate": (0.359, 0.36, 0.388), "bne_bld_roof_shingle": (0.585, 0.577, 0.571),
            "bne_bld_siding": (0.822, 0.822, 0.822), "bne_bld_stucco": (0.795, 0.795, 0.795),
            "bne_bld_roof_metal": (0.642, 0.642, 0.642), "bne_bld_panel": (0.815, 0.815, 0.814),
            "bne_bld_metalwall": (0.841, 0.841, 0.841), "bne_bld_stone": (0.83, 0.818, 0.803),
            "bne_bld_marble": (0.978, 0.978, 0.978), "bne_bld_brick": (0.717, 0.569, 0.513),
            "bne_bld_roof_tiles": (0.625, 0.465, 0.361), "bne_bld_roof_slate_round": (0.473, 0.484, 0.522),
            "bne_bld_roof_slate_dark": (0.157, 0.155, 0.136), "bne_bld_roof_wood": (0.897, 0.889, 0.886),
            "bne_bld_stone_rubble": (0.649, 0.608, 0.541), "bne_bld_cinder": (0.63, 0.627, 0.621)}
SKY_UV = (0.5, 0.5)
# the skyline walls' windows (tools/build_sky_windows.py: SKY_WIN_BAYS x SKY_WIN_FLOORS of them in its map), lit at
# night (nightlights.lua; the user, 2026-10-27: "extend the street and window lights as far as the buildings render")
SKY_BAY_M = 3.5
SKY_WIN_BAYS = SKY_WIN_FLOORS = 16


def _sky_colour(look, f):
    """RGBA bytes of a LOOKS name drawn plain: its material's mean colour x its tint x the jitter f."""
    if look not in LOOKS:
        return (150, 150, 150, 255)
    mat, tint = LOOKS[look]
    mean = MAT_MEAN.get(mat, (0.7, 0.7, 0.7))
    return tuple(int(max(0, min(255, round(255 * m * t * f)))) for m, t in zip(mean, tint)) + (255,)


def _sky_mesh(items):
    """The skyline meshes of [(Spec, gmin, gmax)]: per building a quad per wall and its flat roof, in MARBLE at one
    texel, coloured per vertex (~4 per corner + the roof's corners: a row house is 20 vertices, the lite mesh ~72).
    [(positions, uvs, tris, tri material indices, MATERIALS, colours)] as build_parts (whole buildings, each mesh
    under MAX_MESH_VERTS but a building past it alone: sky_items' budget only estimates), or None."""
    m = Mesh()
    mi = MATERIALS.index(MARBLE)
    ms = MATERIALS.index(SKY_WALL)
    uv = SKY_UV
    starts = []                 # each building's first vertex (the cuts between meshes: _cut_buildings)
    for spec, gmin, gmax in items:
        starts.append(m.n)
        if spec.cls == landmarks.LANDMARK:      # (its far-away model, not a box of its footprint's size)
            landmarks.build(m, spec, gmin, _landmark_token(spec), lite=True, tokens=_landmark_tokens())
            continue
        ring = np.asarray(spec.ring, np.float64)[:, :2]
        if len(ring) < 3:
            continue
        if _area2(ring) < 0:
            ring = ring[::-1]
        f = 1.0 + JITTER * (2.0 * _hash01(spec.seed, "jit") - 1.0)
        windows = spec.windows is not False
        wall = (ms if windows else mi, _sky_colour(spec.facade, f))
        roof = (mi, _sky_colour(spec.roof_mat or ROOF_FLAT, f))
        z0 = gmin - SKIRT_LITE_M
        z1 = base_z(spec, gmin, gmax) + float(spec.min_h or 0.0) + float(spec.height or 0.0)
        if z1 <= z0 + 0.5:
            continue
        n = len(ring)
        # (a skillion: each corner's top on its plane, sky_items; else one height)
        zt = z1 + skillion_rise(spec, ring) if spec.roof == "skillion" else np.full(n, z1)
        # (windows: u along the walls in bays, v up in storeys from the ground, both over the map's count)
        zb = gmin
        fh = float(spec.floor_h or FLOOR_M) * SKY_WIN_FLOORS
        bw = SKY_BAY_M * SKY_WIN_BAYS
        # (each building from its own bay and floor of the map, whole cells: the same map at every building's corner
        # lit the same windows, S-moreo's PR #916)
        dv = int(_hash01(spec.seed, "sky v") * SKY_WIN_FLOORS) / SKY_WIN_FLOORS
        v0, v1 = (z0 - zb) / fh + dv, (z1 - zb) / fh + dv
        u = int(_hash01(spec.seed, "sky u") * SKY_WIN_BAYS) / SKY_WIN_BAYS
        # (the walls as a block: earthfast.sky_walls, the numbers the per-wall loop made)
        fast.require(242, "sky_walls")
        P_, UV_ = fast._ef.sky_walls(np.ascontiguousarray(ring), np.ascontiguousarray(zt, np.float64), float(z0),
                                     float(u), float(v0), float(fh), float(bw), bool(windows),
                                     (float(uv[0]), float(uv[1])))
        m.quads(P_, UV_, np.zeros(n, np.intp), [wall])
        m.poly([(p[0], p[1], float(z)) for p, z in zip(ring, zt)], [uv] * n, roof)
        if spec.roof == "pyramidal":    # (a tower's crown or a spire: its apex over the middle, sky_items)
            c = ring.mean(0)
            top = (c[0], c[1], z1 + oriented_box(ring)[3] * dmath.tan(math.radians(roof_pitch(spec))))
            for i in range(n):
                a, b = ring[i], ring[(i + 1) % n]
                m.tri((a[0], a[1], z1), (b[0], b[1], z1), top, uv, uv, uv, wall)
        if spec.roof in DOME_ROOFS:     # (a mapped dome: far away too, on its drum)
            _dome(m, ring, z1, oriented_box(ring)[3] * dmath.tan(math.radians(roof_pitch(spec))), roof, spec.roof,
                  lite=True)
    if not m.n:
        return None
    out = []
    _sky_close(_cut_buildings(*m.arrays(), starts, MAX_MESH_VERTS), out)
    return out or None


def _sky_close(pieces, out):
    """_sky_mesh's pieces [(P, UV, T, M, C, starts)] each closed into solids (shells.close, BNE_WATERTIGHT) and added
    to out as build's meshes, in order. The cut counts the open vertices: a piece past VERTS_MAX once closed (its caps'
    vertices) is cut again between its buildings, in halves; only a building past it alone is left out (over_limit)."""
    import shells       # each building a closed solid, its bottom sunk (shells.close; the user, 2026-11-07)
    for P, UV, T, M, C, starts in pieces:
        P2, T2, M2, UV2, C2 = P, T, M, UV, C
        if shells.ON:
            P2, T2, M2, (UV2, C2) = shells.close(P, T, M, (UV, C), skirt=False)  # (the walls already reach under
                                                                                # the ground: the bottom capped there)
        if len(P2) <= VERTS_MAX:
            out.append((P2, UV2, T2, M2, MATERIALS, C2))
        elif any(0 < k < len(P) for k in starts):
            _sky_close(_cut_buildings(P, UV, T, M, C, starts, max(1, len(P) // 2)), out)
        else:
            over_limit["buildings"] += 1


def _cut_buildings(P, UV, T, M, C, starts, limit):
    """A Mesh's arrays (positions, uvs, tris, materials, colours; each building's vertices and triangles after the
    one before's, starts their first vertices) cut between buildings into pieces of at most limit vertices each (a
    building past it: a piece alone), in order. [(P, UV, T, M, C, its buildings' starts)]; the arrays as they are
    when they fit."""
    n = len(P)
    if n <= limit:
        return [(P, UV, T, M, C, list(starts))]
    cuts, prev = [0], 0
    for b in sorted(set(int(k) for k in starts if 0 < k < n)) + [n]:
        if b - cuts[-1] > limit and prev > cuts[-1]:
            cuts.append(prev)
        prev = b
    cuts.append(n)
    T = np.asarray(T, np.int64)
    piece = np.searchsorted(np.asarray(cuts), T.min(1), side="right") - 1
    out = []
    for k in range(len(cuts) - 1):
        a, b = cuts[k], cuts[k + 1]
        sel = piece == k
        Tk = T[sel] - a
        if len(Tk) and Tk.max() >= b - a:
            raise ValueError("_cut_buildings: a triangle across two buildings")
        out.append((P[a:b], UV[a:b], Tk, np.asarray(M)[sel], C[a:b], [int(k) - a for k in starts if a <= k < b]))
    return out


def sky_items(frame, box, keys, md, ground, lamp_region=None, own=None):
    """The skyline's buildings of box (x0, y0, x1, y1) in frame over map cells keys: (picked items, lamps), or None when
    the cells are not at hand (fetched in the background: asked again later). own: a geotile's box (its own buildings
    and lamps, each in one tile)."""
    x0, y0, x1, y1 = box
    # (and the roads: their street lamps, far_lamps; and the building parts: a tower's own height and colours, SKY_PARTS)
    sky_layers = ("buildings", "building_parts", "roads") if SKY_PARTS else ("buildings", "roads")
    if not all(md.has(k, layer) for k in keys for layer in sky_layers):
        # (the background lane: never before what the player is near; in the map data's own threads, all at once: one
        # after another in the one far process they held the far buildings back, 2026-11-07)
        md.warm([(k, layer) for k in keys for layer in sky_layers], kind="prefetch")
        return None
    measured_wait(md, keys, wait=False)
    cds = [md.cell(k, layers=sky_layers, kind="background") for k in keys]
    layers = [c.layers.get("buildings") for c in cds]
    items = tile_buildings(frame, x0, y0, x1, y1, layers, ground, min_size=(SKY_SLENDER_H, SKY_MIN_AREA_M2), own=own,
                           part_layers=[c.layers.get("building_parts") for c in cds] if SKY_PARTS else (),
                           parts_min_top=SKY_PARTS_MIN_M)
    size = max(x1 - x0, y1 - y0)
    lamps = far_lamps(frame, [c.layers.get("roads") for c in cds], x0, y0, size,
                      built_squares(items, 0.0, 0.0, size), ground, lamp_region, own=own)
    for spec, _gmin, _gmax in items:        # (the simplest shape: a box or a few corners, a flat roof, nothing on it)
        ring = np.asarray(spec.ring, np.float64)
        if len(ring) > 4 and abs(_area2(ring)) / 2 < SKY_BOX_AREA_M2:
            c, u, hl, hw = oriented_box(ring)
            v = np.array([-u[1], u[0]])
            ring = np.array([c - u * hl - v * hw, c + u * hl - v * hw, c + u * hl + v * hw, c - u * hl + v * hw])
        elif len(ring) > SKY_MAX_CORNERS:     # (rings are open: the last corner is not the first again)
            ring = ring[::int(np.ceil(len(ring) / SKY_MAX_CORNERS))]
        spec.ring = ring
        # (a crown, a spire; a skillion's sloped top: One World Trade Center's taper from afar, streets.gl study)
        keep = spec.roof if spec.roof in ("pyramidal", "skillion") and len(ring) <= SKY_MAX_CORNERS else "flat"
        spec.roof, spec.steeple, spec.stoop, spec.porch, spec.sign, spec.door = keep, None, None, None, None, None
    items.sort(key=lambda it: (-(float(it[0].height or 0.0) + float(it[0].min_h or 0.0)), str(it[0].seed)))
    picked, n = [], 0
    for it in items:                        # (the tallest first, up to the 16-bit vertex budget: 5 per corner)
        k = 5 * len(it[0].ring)
        if n + k > MAX_MESH_VERTS:
            break
        picked.append(it)
        n += k
    return picked, lamps


def skyline_tile(frame, ux, uy, md, ground, out_dir, vfs_dir, aid, lamp_region=None):
    """A skyline tile (render-only) written to the session folder: (vfs path of its JSON, building count) or None
    when its cells are not at hand (fetched in the background: asked again later). Key (ux, uy): SKY_UNIT x SKY_UNIT
    road tiles from road tile (ux * SKY_UNIT, uy * SKY_UNIT)."""
    import json
    import roads
    x0, y0, _, _ = roads.tile_bbox(ux * SKY_UNIT, uy * SKY_UNIT)
    x1, y1 = x0 + SKY_UNIT * roads.TILE, y0 + SKY_UNIT * roads.TILE
    keys = sorted({k for i in range(SKY_UNIT) for j in range(SKY_UNIT)
                   for k in roads.cells_of_tile(frame, ux * SKY_UNIT + i, uy * SKY_UNIT + j, margin=0.0)}, key=str)
    got = sky_items(frame, (x0, y0, x1, y1), keys, md, ground, lamp_region)
    if got is None:
        return None
    picked, lamps = got
    mesh = _sky_mesh(picked) if picked else None
    data = {"v": 1}
    if mesh is not None:
        shape = f"bldsky_{aid}_{ux}_{uy}.cdae"
        if lamps:
            data["lamps"] = lamps
        write(os.path.join(out_dir, shape), mesh)
        data["shape"] = f"{vfs_dir}/{shape}"
        data["pos"] = [x0, y0, 0.0]
    name = f"bldsky_{aid}_{ux}_{uy}.json"
    path = os.path.join(out_dir, name)
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        f.write(json.dumps(data))
    staging.replace(tmp, path)
    return f"{vfs_dir}/{name}", len(picked) if mesh is not None else 0


# ------------------------------------------------------------------------------------------- far bridges
# The road bridges past the road tiles (their decks only came with the road tiles, ~0.8 km: a city's bridges were
# gone beyond that): one render-only slab per road tile that has bridge decks, out to BRIDGE_FAR_SEND_M, from the same
# deck solver as the road tiles (roads.BridgeProfiles), each part's segments in the tile of their midpoint (offsets
# from the whole part: tiles meet without a gap). A slab: the deck's top, its sides and its underside down to its
# girders, plain grey (MARBLE at one texel, vertex colours). The mod hides one while its road tile has a deck.
BRIDGE_FAR_SEND_M = float(os.environ.get("BNE_BRIDGE_FAR_SEND_M", "4800"))
BRIDGE_FAR_DROP_M = BRIDGE_FAR_SEND_M + 500.0
BRIDGE_FAR_PER_POSE = 2
BRIDGE_TOP_RGBA = (92, 92, 94, 255)        # the deck's asphalt from afar
BRIDGE_SIDE_RGBA = (176, 174, 168, 255)    # its concrete


FAR_NEED = 106             # earthfast.bridge_slabs


def bridge_slabs(parts, origin, box=None, owners=None):
    """The slab meshes of the bridge parts [(xy (k, 2), z (k,), width m)] (in one frame), relative to origin (x, y),
    by owner (earthfast.bridge_slabs): box (x0, y0, x1, y1): the segments whose midpoint is in [x0, x1) x [y0, y1), one
    mesh under the key None; owners (frame, cz, pz, px, py): those whose midpoint's place lies in a zoom-cz geotile
    under the zoom-pz one (px, py), a mesh per geotile {(gx, gy): mesh}. Each (positions, uvs, tris, tri materials,
    MATERIALS, colours) as build: the deck's top, its sides and its underside down to its girders, plain grey."""
    import fast
    import roadmesh
    parts = [(np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64), float(w)) for xy, z, w in parts]
    parts = [p for p in parts if len(p[0]) >= 2]
    if not parts:
        return {}
    fast.require(FAR_NEED, "bridge_slabs")
    xy = np.ascontiguousarray(np.concatenate([p[0] for p in parts]))
    z = np.ascontiguousarray(np.concatenate([p[1] for p in parts]))
    off = np.r_[0, np.cumsum([len(p[0]) for p in parts])].astype(np.int64)
    w = np.array([p[2] for p in parts], np.float64)
    if owners is None:
        lat0 = lon0 = 0.0
        cz = pz = px = py = 0
        bb = tuple(float(v) for v in box)
    else:
        frame, cz, pz, px, py = owners
        lat0, lon0, bb = float(frame.lat0), float(frame.lon0), (0.0, 0.0, 0.0, 0.0)
    got = fast._ef.bridge_slabs(xy, z, off, w, (float(origin[0]), float(origin[1])),
                                float(roadmesh.DECK_DEPTH + roadmesh.GIRDER_D), BRIDGE_TOP_RGBA, BRIDGE_SIDE_RGBA, bb,
                                lat0, lon0, int(cz), int(pz), int(px), int(py))
    mi = MATERIALS.index(MARBLE)
    out = {}
    for gx, gy, P, T, C in got:
        if not len(T):
            continue
        UV = np.tile(np.asarray(SKY_UV, np.float64), (len(P), 1))
        out[None if owners is None else (int(gx), int(gy))] = (P, UV, T, np.full(len(T), mi, np.int64), MATERIALS, C)
    return out


def far_bridge_tile(frame, tx, ty, md, bridges, out_dir, vfs_dir, aid):
    """A far bridge tile (render-only) written to the session folder: (vfs path of its JSON, part count) or None when
    its road cells are not at hand (fetched in the background: asked again later). bridges: the anchor's
    roads.BridgeProfiles."""
    import json
    import roads
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    keys = roads.cells_of_tile(frame, tx, ty, margin=0.0)
    if not all(md.has(k, "roads") for k in keys):
        for k in keys:
            md.prefetch(k, layers=("roads",))
        return None
    cells = [md.cell(k, layers=("roads",), kind="background").layers.get("roads") for k in keys]
    parts = [(xy, z, float(spec.width)) for _fid, spec, xy, z, _lon in bridges.parts(cells, (x0, y0, x1, y1))]
    mesh = bridge_slabs(parts, (x0, y0), box=(x0, y0, x1, y1)).get(None) if parts else None
    data = {"v": 1}
    if mesh is not None:
        shape = f"brfar_{aid}_{tx}_{ty}.cdae"
        write(os.path.join(out_dir, shape), mesh)
        data["shape"] = f"{vfs_dir}/{shape}"
        data["pos"] = [x0, y0, 0.0]
    name = f"brfar_{aid}_{tx}_{ty}.json"
    path = os.path.join(out_dir, name)
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        f.write(json.dumps(data))
    staging.replace(tmp, path)
    return f"{vfs_dir}/{name}", len(parts) if mesh is not None else 0


# ------------------------------------------------------------------------------------------- the ground photo

PHOTO_GROW_M = 1.5          # m a footprint is grown on the ground photo (edges, eaves) ...
PHOTO_LEAN = 0.12           # ... plus this share of its height (the photo's lean of tall buildings), at most
PHOTO_LEAN_MAX_M = 6.0      # ... this
PHOTO_FILL_M = 12.0         # m: the blur that fills them from the ground around


def photo_mask(frame, x0, y0, size, px, md):
    """A (px, px) bool mask (row 0 = north) of the ground photo's square [x0, x0 + size) x [y0, y0 + size)
    (anchor frame) where buildings stand, grown by their edges and lean: there the photo shows roofs and
    walls that the 3D buildings now stand on or next to. None without buildings."""
    from PIL import Image, ImageDraw
    import mapcells
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0 + size, x0, x0 + size]),
                                      np.array([y0, y0, y0 + size, y0 + size]), 0.0)
    keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
    img = Image.new("L", (px, px), 0)
    draw = ImageDraw.Draw(img)
    mpp = size / px
    seen = set()
    n = 0
    for k in keys:
        f = md.cell(k, layers=("buildings",), kind="demand").layers.get("buildings")
        if f is None:
            continue
        hs = f.attrs.get("height")
        ids = f.ids.tolist()
        rows, a, b = _photo_candidates(f, frame, x0, y0, size)
        keep = [j for j, i in enumerate(rows) if ids[i] not in seen]  # (a building in two cells: the first piece)
        if not keep:
            seen.update(ids)
            continue
        rows, a, b = rows[keep], a[keep], b[keep]
        # every kept outer ring grown out from its middle, all at once
        h = np.full(len(rows), 8.0) if hs is None else np.asarray(hs, np.float64)[rows]
        h = np.where(np.isfinite(h), h, 8.0)
        grow = (PHOTO_GROW_M + np.minimum(PHOTO_LEAN * h, PHOTO_LEAN_MAX_M)) / mpp
        ln = b - a
        idx = np.repeat(a - np.r_[0, np.cumsum(ln)[:-1]], ln) + np.arange(int(ln.sum()))
        xy = f.xy_in(frame)[idx]
        cx, cy = (xy[:, 0] - x0) / mpp, (y0 + size - xy[:, 1]) / mpp
        starts = np.r_[0, np.cumsum(ln)[:-1]]
        mx = np.repeat(np.add.reduceat(cx, starts) / ln, ln)
        my = np.repeat(np.add.reduceat(cy, starts) / ln, ln)
        dx, dy = cx - mx, cy - my
        g = np.repeat(grow, ln) / np.maximum(dnp.hypot(dx, dy), 1e-6)
        pts = np.stack([cx + dx * g, cy + dy * g], 1).tolist()
        for s0, m in zip(starts.tolist(), ln.tolist()):
            draw.polygon([tuple(q) for q in pts[s0:s0 + m]], fill=255)
        n += len(rows)
        seen.update(ids)
    if n == 0:
        return None
    return np.asarray(img) > 0


def _photo_candidates(f, frame, x0, y0, size):
    """Rows of f (a cell's buildings) whose outer ring's box meets the square [x0, x0 + size]² and that come first
    among the rows of their id, ascending, with their outer rings' coordinate ranges [a, b): photo_mask's buildings, its test made for all rows at once (one ring at a
    time it was ~19k Python steps a block, most of a block photo's roof mask; 2026-10-23)."""
    m = len(f.kind)
    none = (np.zeros(0, np.int64),) * 3
    if m == 0:
        return none
    p = f.part_off[:-1]
    has = (f.part_off[1:] > p)
    r = f.ring_off[np.minimum(p, len(f.ring_off) - 2)]
    has &= f.ring_off[np.minimum(p, len(f.ring_off) - 2) + 1] > r
    a = f.coord_off[np.minimum(r, len(f.coord_off) - 2)]
    b = f.coord_off[np.minimum(r, len(f.coord_off) - 2) + 1]
    has &= b > a
    _, first = np.unique(f.ids, return_index=True)
    head = np.zeros(m, bool)
    head[first] = True
    rows = np.flatnonzero(has & head)
    if len(rows) == 0:
        return none
    xy = f.xy_in(frame)
    a, b = a[rows], b[rows]
    order = np.argsort(a, kind="stable")       # (reduceat: ascending starts)
    e_lo = np.empty(len(rows)); e_hi = np.empty(len(rows)); n_lo = np.empty(len(rows)); n_hi = np.empty(len(rows))
    e_lo[order] = mapgeom._reduce(np.minimum, xy[:, 0], a[order], b[order])
    e_hi[order] = mapgeom._reduce(np.maximum, xy[:, 0], a[order], b[order])
    n_lo[order] = mapgeom._reduce(np.minimum, xy[:, 1], a[order], b[order])
    n_hi[order] = mapgeom._reduce(np.maximum, xy[:, 1], a[order], b[order])
    inside = ~((e_hi < x0) | (e_lo > x0 + size) | (n_hi < y0) | (n_lo > y0 + size))
    return rows[inside], a[inside], b[inside]


def fill_photo(rgb, mask, size, px):
    """rgb (px, px, 3) uint8 with the masked pixels replaced by a smooth fill from the unmasked ground
    around them: a push-pull pyramid, so a footprint of any size fills (one PHOTO_FILL_M blur left black
    holes in big blocks), softened by that blur (earthfast.fill_photo: the numpy / Pillow version's numbers,
    a third of its time; 2026-10-23)."""
    import fast
    fast.require(68, "the ground photo's roof fill")
    r = max(2.0, PHOTO_FILL_M / (size / px))
    return fast._ef.fill_photo(np.ascontiguousarray(rgb, np.uint8), np.ascontiguousarray(mask, bool), float(r))


BRICK_WALK_COVERAGE = 0.35  # dense old cores (row houses) of brick regions ...
BRICK_WALK_SHARE = 0.55     # ... pave this share of their streets' sidewalks with brick (hashed per street)


def brick_walks(walks, items, lat, lon, tile=256.0):
    """Marks the sidewalk runs (roads.build_tile's walks) of a dense brick-region tile that are brick pavers
    (Fells Point's historic streets): every run of a street the same."""
    if not walks or brick_share(lat, lon) < 0.7 or coverage(items, tile) < BRICK_WALK_COVERAGE:
        return
    key = _median_keys(walks)
    for n, wk in enumerate(walks):
        fid = key.get(n, wk.get("fid"))
        if fid is not None and _hash01(str(fid), "brickwalk") < BRICK_WALK_SHARE:
            wk["brick"] = True


MEDIAN_PAIR_M = 9.0         # m of ground at most between the two halves of one median (each carriageway's band)


def _median_keys(walks):
    """{walk index: key} for the median bands: the two carriageways' halves of one median (their runs within
    MEDIAN_PAIR_M of each other, edge to edge) share the smaller fid, so the median is paved one way (one half red
    brick and the other concrete: the user's report 20, Fells Point 2026-11-12). Pure."""
    med = [(n, np.asarray(wk["pts"], np.float64)[:, :2], float(wk.get("w", 0.0)), str(wk.get("fid")))
           for n, wk in enumerate(walks) if wk.get("median") and "pts" in wk and len(wk["pts"]) >= 2]
    if len(med) < 2:
        return {}
    group = list(range(len(med)))

    def root(a):
        while group[a] != a:
            group[a] = group[group[a]]
            a = group[a]
        return a
    for a in range(len(med)):
        for b in range(a + 1, len(med)):
            if med[a][3] == med[b][3]:
                continue
            pa, pb = med[a][1], med[b][1]
            reach = (med[a][2] + med[b][2]) / 2 + MEDIAN_PAIR_M
            if (pa.min(0) - reach > pb.max(0)).any() or (pb.min(0) - reach > pa.max(0)).any():
                continue
            d = dnp.hypot(*(pa[::max(1, len(pa) // 8)][:, None, :] - pb[None, :, :]).transpose(2, 0, 1)).min(1)
            if float(np.median(d)) <= reach:
                group[root(a)] = root(b)
    keys = {}
    for k in range(len(med)):
        keys.setdefault(root(k), []).append(med[k][3])
    return {med[k][0]: min(keys[root(k)]) for k in range(len(med))}


_cell_stats = {}                # map cell key -> (lon, lat, area m2) per footprint (district_coverage)
_cell_lock = None


def _cell_lock_get():
    global _cell_lock
    if _cell_lock is None:
        import threading
        _cell_lock = threading.Lock()
    return _cell_lock


def cell_footprints(key, layer):
    """(lon, lat, area m2) arrays of a cell's building footprints (outer rings), cached per cell (layer may
    be None once cached). Pure."""
    _cell_lock_get()
    with _cell_lock:
        got = _cell_stats.get(key)
    if got is not None:
        return got
    if layer is None or len(layer.ids) == 0:
        got = (np.zeros(0), np.zeros(0), np.zeros(0))
    else:
        f = layer
        first = f.ring_off[f.part_off[:-1]]
        a = f.coord_off[first]
        b = f.coord_off[first + 1]
        ll = f.lonlat
        lat0 = float(np.mean(ll[:, 1]))
        x = ll[:, 0] * 111320.0 * dmath.cos(math.radians(lat0))
        y = ll[:, 1] * 111320.0
        cross = np.r_[x[:-1] * y[1:] - x[1:] * y[:-1], 0.0]
        C = np.concatenate(([0.0], np.cumsum(cross)))
        CX, CY = np.concatenate(([0.0], np.cumsum(ll[:, 0]))), np.concatenate(([0.0], np.cumsum(ll[:, 1])))
        n = np.maximum(b - a, 1)
        last = np.maximum(b - 1, a)
        area = np.abs(C[last] - C[a] + x[last] * y[a] - x[a] * y[last]) / 2
        got = ((CX[b] - CX[a]) / n, (CY[b] - CY[a]) / n, area)
    with _cell_lock:
        if len(_cell_stats) > 64:
            _cell_stats.clear()
        _cell_stats[key] = got
    return got


def district_coverage(lat, lon, cells):
    """The share of the DISTRICT_M disc around (lat, lon) covered by building footprints; cells: [(key,
    buildings layer)] covering the disc."""
    seen = 0.0
    mx = 111320.0 * dmath.cos(math.radians(lat))
    for key, layer in cells:
        lo, la, ar = cell_footprints(key, layer)
        if len(ar):
            d2 = dnp.sq((lo - lon) * mx) + dnp.sq((la - lat) * 111320.0)
            seen += float(ar[d2 <= DISTRICT_M * DISTRICT_M].sum())
    return seen / (math.pi * DISTRICT_M * DISTRICT_M)


def district_town(frame, tx, ty, md):
    """Is road tile (tx, ty) in a small town of the arid West (WEST_TOWN_COVERAGE)? The cells of the district
    are loaded like the tile's own (the far building tiles need them anyway); an unreadable one counts as
    empty."""
    import mapcells
    import roads
    import surface
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    lat, lon, _ = frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.0)
    lat, lon = float(lat), float(lon)
    if not bool(surface.dry_west(lat, lon)):
        return False
    dlat = DISTRICT_M / 111320.0
    dlon = DISTRICT_M / (111320.0 * dmath.cos(math.radians(lat)))
    cells = []
    for key in mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon):
        with _cell_lock_get():
            known = key in _cell_stats
        if known:
            cells.append((key, None))
            continue
        try:
            cells.append((key, md.cell(key, layers=("buildings",), kind="demand").layers.get("buildings")))
        except Exception:           # not cached / unreadable: counts as empty
            continue
    return district_coverage(lat, lon, cells) < WEST_TOWN_COVERAGE
