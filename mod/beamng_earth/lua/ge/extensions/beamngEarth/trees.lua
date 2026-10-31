-- Trees, bushes and rocks from the server's vegetation tiles (server/vegetation.py, --trees on; the user,
-- 2026-10-13: "trees, rocks and other decoration"). Each tile file lists items
-- {group, x, y, z, yaw, scale, height, rank} in the anchor frame; they become Forest items of stock
-- East Coast USA shapes (they have LODs, billboards and collision meshes), the variant nearest the
-- item's height, scaled to it. A tile shows the items whose fixed rank is under its distance's threshold
-- (ROADMAP "deterministic generation": a level shows the same trees, thinned), made and removed a few
-- hundred per frame (~19 us each, 2026-10-13 probe).
local M = {}
local vienna = require('beamngEarth/vienna_convention_speed_signs')
local geotile = require('beamngEarth/geotile')
-- Geotiles (server --geo-tiles on, hello.geoTiles): a tile keyed 'g17_x_y' holds its items in its own frame (msg.frame,
-- msg.pivot); it belongs to no anchor. Its items are made where its placement into the current anchor puts them
-- (geotile.placement); at a rebase its Forest takes the move from the old placement to the new one (rendering at once,
-- nothing made again) and the server sends nothing again. A Forest's transform moves only the drawing, not the
-- collision (probe 2026-10-17), so a moved tile that must collide (within SOLID_M) has its items made again at the
-- new placement first (flush), and only those: the far ones never collide.
M.geoMoved, M.geoFlushed = 0, 0

M.PER_FRAME = 250       -- items made or removed per frame at most
M.ROWS_PER_FRAME = 12000 -- rows of changing tiles looked at per frame at most (Lua time: 30000 averaged 2.5 ms)
-- 2026-10-14: was { 300, 1 }, { 550, 0.5 }, { 900, 0.25 }, { 1e9, 0.12 }; the engine already draws far trees as
-- billboards (their LODs), so our extra thinning made far forest sparse. Columbia: 48k -> 78k items, no frame cost seen.
M.RINGS = { { 300, 1.0 }, { 700, 0.6 }, { 1500, 0.35 }, { 1e9, 0.2 } }   -- tile distance (m): the rank shown below
-- ... and at most this many items a tile (the lowest ranks: the hashed ranks thin it evenly). Dense forest has 10-20k
-- a tile: Bodø's 145 tiles made 1.66 M items (GitHub #66: 28 fps standing on a Steam Deck, 23 ms of each frame waiting
-- on the GPU, and the engine's tick hitched at every tile change while driving: 19.8 fps over a drive, 38.4 with trees
-- off; 2026-10-20). Cities and farmland stay under it (Columbia: ~540 a tile).
M.TILE_CAP = { { 300, 6000 }, { 700, 2400 }, { 1500, 900 }, { 1e9, 400 } }
-- small props only near the player (2026-10-14, Fells Point at street level: ~10 ms a frame for the Forest
-- with a door on every house out to 300 m): the tile distance (m) beyond which a group is not shown
-- rocks only over the fine ground (farfield.NEAR_RADII: L0 within 400 m): beyond it the coarser tiles round off the
-- ridges and a rock placed on the real ground floated over the drawn one (Stelvio pass, the user's report 2026-10-17)
M.GROUP_MAX_M = { shrub = 250, desert_shrub = 250, rock = 400, desert_rock = 400, cliff = 1500, pylon = 1500, gantry = 800, sub_breaker = 400, sub_mast = 1000, sub_support = 300,
  sub_transformer = 500, sub_fence = 200, lamplens = 800, lampglow = 6000, rrcross = 300, rrcross_eu = 300, rrflash = 400,
  rrflash_eu = 400, rrgate = 400, rrgate_l = 400, rrgate_eu = 400, rrgate_eu_l = 400, door = 120, hydrant = 200, bin = 200, bench = 200, polelamp = 400,
  stop = 300, giveway = 300, signal = 500, fence = 150, wallsign_logistics = 800, wallsign_rentabox = 800, wallsign_lemon = 800, wallsign_turboburger = 500,
  speed30 = 300, speed40 = 300, speed50 = 300, speed60 = 300, speed70 = 300, guardrail = 4000, guardrail_start = 4000, guardrail_end = 4000,
  guardrail_back = 4000, guardrail_back_start = 4000, guardrail_back_end = 4000 }
M.FOREST_NAME = 'bngEarthForest'      -- a prefix: small Forests from a pool ('bngEarthForest_<n>')
-- A small Forest per tile, from a pool (2026-10-15, 100 mph drives: one shared Forest with ~240k items made and ~220k
-- removed over a session made frames with any tree change cost ~100 ms in the engine's tick, and a fresh game ran the
-- same drive at 71 fps instead of 30-42). A dropped tile's items are removed (cheap in its small Forest) and the empty
-- Forest goes back to the pool: deleting a Forest whose items are in the physics' static collision hung the game.
-- Only tiles this near the player get colliding items (their trees, rocks and street furniture), the rest are made of
-- item types that never collide, a tile's items re-made when it crosses (back beyond SOLID_M + SOLID_HYST_M).
-- Solid items made ask for a collision rebuild (roads.lua, throttled): a Forest item collides only after
-- be:reloadCollision, and where no road tile arrives (the Sahara's rocks, 2026-10-14) none came at all.
-- 2026-10-15 probes: with ~150k trees all colliding, a frame changing 10 items cost the engine's tick ~124 ms, ~68 ms
-- when the changed items' type did not collide: much of a Forest change's cost grew with the colliding items.
M.SOLID_M = 200       -- (was 300 + 100: up to ~20k solid trees in a forest, and every terrain block move rebuilt all their
                      -- collision, a ~30 ms physics frame; 200 + 50 is still 5 s ahead at 100 mph, 2026-10-15)
M.SOLID_HYST_M = 50
M.NO_COLLIDE = { shrub = true, desert_shrub = true, door = true, lamplens = true, lampglow = true, wallsign_logistics = true, wallsign_rentabox = true,
  wallsign_lemon = true, wallsign_turboburger = true }
-- The 100 mph stutter (2026-10-15 probes): a frame that adds or removes Forest items costs the engine's tick ~0.5 us
-- for every item in the level of each SHAPE touched (whatever its item type or Forest), so a frame making 300 items of
-- ~20 shapes among ~130k items cost ~100 ms. A frame touches shapes whose items add up to SHAPE_ITEMS_PER_FRAME at most
-- (at least one shape), and bushes (half of all items, one shape alone 34k) show near the player only.
M.SHAPE_ITEMS_PER_FRAME = 4000
-- A tree frame's Lua time (checked after every item made or removed, and every CHECK_ROWS rows looked at): the item
-- budget alone let frames run 30-180 ms when their items' shapes loaded for the first time or new Forests were made
-- (2026-10-26, a Fells Point drive: 119 tree frames of 8 ms or more, 24.8 ms on average, two thirds of it
-- making Forests). More while the world loads (core: the governor's loading mode, a teleport or the car standing), none
-- in the governor's heavy frames.
M.FRAME_MS = 4.0          -- (2.5 for an evening, 2026-10-29: no steadier game time, and the trees, poles and
                          -- signs fell 2,000+ items behind at 70-120 mph, their collision late)
M.LOADING_FRAME_MS = 12.0
local CHECK_ROWS = 256
-- Registering a new Forest costs ~9 ms (in-game 2026-10-26: 8-10 ms each, named or not, whatever its data file), and
-- the tiles that came at once made several in one frame: FORESTS_PER_FRAME at most, and POOL_SPARE kept ready in the
-- pool, made one in a tree frame that changes no item.
M.FORESTS_PER_FRAME = 1
M.POOL_SPARE = 4
-- ... and up to POOL_SPARE_CALM while the world loads or the car stands (the tree frames' LOADING_FRAME_MS: a teleport,
-- the level's start, a stop): a drive through town took ~20 new Forests a minute, a ~9 ms frame every 3 s (the game's
-- time then ran unevenly, 2026-10-29); while driving none is made for the pool, only one a tile needs at once.
M.POOL_SPARE_CALM = 32
-- The engine tells shapes apart by their path, so a tile's items use one of SHAPE_VARIANTS spellings of each shape's
-- path ('dir/./name', 'dir/././name', ...: the same file, a shape of its own), which splits the big populations (one
-- beech ~7.7k items: a frame touching it cost ~4-9 ms). A variant's first items cost one ~12 ms frame (its load).
M.SHAPE_VARIANTS = 4

local ECA = '/levels/east_coast_usa/art/shapes/'
local WCA = '/levels/west_coast_usa/art/shapes/'
local UT = '/levels/Utah/art/shapes/'
local JV = '/levels/johnson_valley/art/shapes/'
local JRI = '/levels/jungle_rock_island/art/shapes/'
local IT = '/levels/italy/art/shapes/'
-- the stock shapes per group with their measured heights (m; TSStatic world boxes, 2026-10-13/14)
M.CATALOG = {
  -- rock faces along rural road cuts (server roads.cliff_rows: Italy's rock-face kit, as its own cut slopes are made)
  cliff = {
    { IT .. 'rocks/italy_rockface_short_wide', 2.9 }, { IT .. 'rocks/italy_rockface_tall_wide', 13.2 },
  },
  deciduous = {
    { ECA .. 'trees/trees_oak/tree_oak_sml_a', 8.6 }, { ECA .. 'trees/trees_oak/tree_oak_sml_b', 9.5 },
    { ECA .. 'trees/trees_aspen/tree_aspen_small_a', 8.3 }, { ECA .. 'trees/trees_aspen/tree_aspen_small_c', 9.7 },
    { ECA .. 'trees/trees_beech/tree_beech_small_c', 9.9 }, { ECA .. 'trees/trees_oak/tree_oak_large_a', 13.2 },
    { ECA .. 'trees/trees_oak/tree_oak_large_b', 15.9 }, { ECA .. 'trees/trees_oak/tree_oak_large_c', 15.5 },
    { ECA .. 'trees/trees_oak/tree_oak_forest_a', 15.9 }, { ECA .. 'trees/trees_oak/tree_oak_forest_b', 16.0 },
    { ECA .. 'trees/trees_beech/tree_beech_large_c', 17.4 }, { ECA .. 'trees/trees_aspen/tree_aspen_forest_b', 18.5 },
    { ECA .. 'trees/trees_beech/tree_beech_forest_a', 20.2 }, { ECA .. 'trees/trees_beech/tree_beech_large_b', 21.1 },
  },
  conifer = {
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_small_b', 11.2 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_small_a', 13.4 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_c', 18.9 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_a', 19.6 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_b', 21.4 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_d', 21.7 },
    { IT .. 'trees/trees_italy/scots_pine', 18.9 },
  },
  shrub = {
    { ECA .. 'trees/ECA_coast_bush/generibush', 1.8 }, { ECA .. 'trees/trees_aspen/tree_aspen_bush_a', 2.2 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_bush_a', 2.7 }, { ECA .. 'trees/trees_oak/tree_oak_bush_c', 3.1 },
    { ECA .. 'trees/trees_beech/tree_beech_bush_b', 3.3 }, { ECA .. 'trees/trees_beech/tree_beech_bush_c', 3.9 },
    { ECA .. 'trees/trees_oak/tree_oak_bush_b', 4.1 },
  },
  rock = {
    { ECA .. 'rocks/eca_rock_small', 0.6 }, { ECA .. 'rocks/rock_01_a_large_a', 0.7 }, { ECA .. 'rocks/eca_rock_small_2', 0.9 },
    { ECA .. 'rocks/eca_rock_medium_1', 1.5 }, { ECA .. 'rocks/eca_rock_medium_2', 1.6 }, { ECA .. 'rocks/eca_rock_big_narrow', 1.9 },
    { ECA .. 'rocks/eca_rock_big_wide', 1.9 },
  },
  pine_west = {
    { WCA .. 'trees/trees_conifer/pine_radiata_small', 8.6 }, { WCA .. 'trees/trees_conifer/pine_radiata_forest', 13.3 },
    { WCA .. 'trees/trees_conifer/pine_radiata_large', 13.2 }, { WCA .. 'trees/trees_conifer/pine_radiata_weathered', 13.2 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_a', 19.6 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_d', 21.7 },
    { IT .. 'trees/trees_italy/scots_pine', 18.9 }, { WCA .. 'trees/trees_conifer/redwood_tall', 38.5 },
    { WCA .. 'trees/trees_conifer/redwood_split', 38.1 }, { WCA .. 'trees/trees_conifer/redwood_broad', 38.5 },
  },
  oak_west = {
    { WCA .. 'trees/trees_oak/oak_d', 7.1 }, { WCA .. 'trees/trees_oak/oak_b', 8.2 }, { WCA .. 'trees/trees_oak/oak_c', 8.4 },
    { WCA .. 'trees/trees_oak/oak_a', 9.6 }, { WCA .. 'trees/trees_eucalyptus/eucalyptus_double', 14.7 },
    { WCA .. 'trees/trees_eucalyptus/eucalyptus_tall', 22.5 },
  },
  palm = {
    { JRI .. 'trees/trees_palm/palm_small', 8.9 }, { JRI .. 'trees/trees_palm/palm_short', 9.1 },
    { WCA .. 'trees/trees_palm/fanpalm_short', 10.6 }, { WCA .. 'trees/trees_palm/fanpalm_skirt', 11.7 },
    { WCA .. 'trees/trees_palm/fanpalm_full', 13.8 }, { JRI .. 'trees/trees_palm/palm_medium', 18.0 },
    { WCA .. 'trees/trees_palm/fanpalm_tall', 18.5 }, { WCA .. 'trees/trees_palm/fanpalm_slim', 20.8 },
    { JRI .. 'trees/trees_palm/palm_tall', 25.3 },
  },
  juniper = {
    { UT .. 'trees/Juniper/Juniper_bush_large_01', 2.6 }, { UT .. 'trees/Juniper/Juniper_tree_04', 4.5 },
    { UT .. 'trees/Juniper/Juniper_tree_03', 5.2 }, { UT .. 'trees/Juniper/Juniper_tree_01', 6.3 },
    { UT .. 'trees/Juniper/Juniper_tree_02', 7.4 }, { UT .. 'trees/Juniper/Juniper_ancient_01', 7.5 },
  },
  -- (2026-10-16: not JV's deserttree_bush_tiny: it ships no imposter, and the one the engine baked into its temp
  -- folder while the leaf material did not resolve is solid no-texture orange, kept for good: orange bushes at range)
  desert_shrub = {
    { UT .. 'groundcover/s_gc_utah_grey_bush_01', 0.7 }, { JV .. 'groundcover/s_gc_jv_dry_bush_01', 0.7 },
    { UT .. 'groundcover/s_gc_utah_grey_bush_03', 1.0 },
    { JV .. 'groundcover/s_gc_jv_dry_bush_03', 1.0 }, { UT .. 'trees/Juniper/Juniper_bush_small_01', 1.2 },
    { UT .. 'trees/Juniper/Juniper_bush_small_dry_01', 2.0 }, { UT .. 'trees/Juniper/Juniper_bush_medium_01', 2.4 },
    { JV .. 'trees/deserttrees/deserttree_bush_medium', 2.9 },
  },
  desert_rock = {
    { UT .. 'rocks/rock_01_a_small_a', 0.3 }, { UT .. 'rocks/utah_rock_flat_a', 0.5 }, { UT .. 'rocks/utah_boulder_5', 0.6 },
    { JV .. 'rocks/s_rocks_desert_small_01', 0.9 }, { JV .. 'rocks/s_rocks_desert_medium_03', 1.4 },
    { UT .. 'rocks/utah_boulder_3', 1.7 }, { UT .. 'rocks/utah_boulder_1', 1.9 }, { JV .. 'rocks/s_rocks_desert_medium_01', 2.1 },
  },
  door = {     -- the stock Building Architect kit (art_shapes.zip; global materials)
    { '/art/shapes/common/building_architect_modules/doors/R_HS_A_DR_01_100x210', 2.2 },
    { '/art/shapes/common/building_architect_modules/doors/R_HS_B_DR_03_100x210', 2.2 },
  },
  signal = { { '/art/shapes/objects/s_trafficlight_boom_sn', 9.4 } },      -- (setup: M.STATIC_SIGNAL)
  stop = { { '/levels/east_coast_usa/art/shapes/signs_usa/sign_stop', 3.0 } },
  giveway = { { '/art/shapes/garage_and_dealership/Clutter/italy_traf_sign_give_way', 3.0 } },
  -- a low wooden yard fence, 5.4 m along its x (server buildings._front_fence)
  fence = { { ECA .. 'buildings/eca_bld_wood_fence_c', 1.3 } },
  -- company signs on big boxes, warehouses and restaurants (server buildings._wall_sign; West Coast USA's firms)
  wallsign_logistics = { { WCA .. 'objects/sign_logistics', 5.8 } }, wallsign_rentabox = { { WCA .. 'objects/sign_rentabox', 5.7 } },
  wallsign_lemon = { { WCA .. 'objects/sign_lemon', 3.6 } }, wallsign_turboburger = { { WCA .. 'objects/sign_turboburger', 2.4 } },
  -- speed-limit signs (server furniture.speed_signs: the road graph's limit, where a model exists)
  speed30 = { { UT .. 'ut_roadsigns/ut_30_speed_limit', 2.6 } }, speed40 = { { UT .. 'ut_roadsigns/ut_40_speed_limit', 2.6 } },
  speed50 = { { UT .. 'ut_roadsigns/ut_50_speed_limit', 2.6 } }, speed60 = { { UT .. 'ut_roadsigns/ut_60_speed_limit', 2.6 } },
  speed70 = { { UT .. 'ut_roadsigns/ut_70_speed_limit', 2.6 } },
  -- (the round km/h signs, speedk30 .. speedknone: vienna_convention_speed_signs.lua, installed below)
  hydrant = { { '/levels/west_coast_usa/art/shapes/objects/hydrant1', 1.5 } },
  -- a toll plaza's island with its booth (server buildings.toll_booth_rows; the user, 2026-10-21: "west coast has one,
  -- reference that"): 22 m along its local y, 1.6 m wide
  tollbooth = { { WCA .. 'objects/s_toll_booth_center', 4.96 } },
  bench = { { ECA .. 'clutter/clutter_city_bench_wood', 0.9 } },
  bin = { { '/art/shapes/garage_and_dealership/Clutter/clutter_city_bin_round', 1.1 }, { WCA .. 'objects/garbage_bin_a', 1.1 } },
  polelamp = { { ECA .. 'buildings/electrical_pole_lamp', 1.1 } },
  streetlight = { { '/art/shapes/garage_and_dealership/Clutter/eca_lamppost', 9.0 } },
  pole = { { '/levels/automation_test_track/art/shapes/objects/s_powerpole_3wire', 9.2 } },
  -- high-voltage lines' towers (server powerlines.py): the stock lattice tower (West Coast USA's is the same shape)
  pylon = { { '/levels/automation_test_track/art/shapes/objects/pylon', 35.8 } },
  -- substations (server powerlines.py): West Coast USA's switchgear (its yard's parts), the gantries the lines end at,
  -- the chain-link fence around them (2.04 m panels)
  gantry = { { WCA .. 'objects/substation_truss', 12.73 } },
  sub_breaker = { { WCA .. 'objects/substation_circuitbreaker', 5.48 } },
  sub_mast = { { WCA .. 'objects/substation_lightning', 25.29 } },
  sub_support = { { WCA .. 'objects/substation_support', 4.94 } },
  sub_transformer = { { WCA .. 'objects/substation_transformer', 6.18 } },
  sub_fence = { { WCA .. 'objects/s_chainlink_old', 1.76 } },
  -- the street lamps' lenses (furniture.py G_LENS; lit at night: nightlights.lua)
  lamplens = { { '/levels/beamng_earth/art/shapes/beamngEarth/bne_lamp_lens', 1.0 } },
  lampglow = { { '/levels/beamng_earth/art/shapes/beamngEarth/bne_lamp_glow', 1.0 } },
  -- level crossings' equipment (server railways.crossing_props; tools/build_rail_crossing_props.py): a crossbuck or a
  -- St Andrew's cross on its post, a mast with lamps, a gate (its arm raised; _l mirrored for left-hand traffic)
  rrcross = { { '/levels/beamng_earth/art/shapes/beamngEarth/bne_rr_crossbuck', 3.263 } }, rrcross_eu = { { '/levels/beamng_earth/art/shapes/beamngEarth/bne_rr_cross_eu', 3.263 } },
  rrflash = { { '/levels/beamng_earth/art/shapes/beamngEarth/bne_rr_flasher', 4.7 } }, rrflash_eu = { { '/levels/beamng_earth/art/shapes/beamngEarth/bne_rr_flasher_eu', 4.7 } },
  rrgate = { { '/levels/beamng_earth/art/shapes/beamngEarth/bne_rr_gate', 8.25 } }, rrgate_l = { { '/levels/beamng_earth/art/shapes/beamngEarth/bne_rr_gate_l', 8.25 } },
  rrgate_eu = { { '/levels/beamng_earth/art/shapes/beamngEarth/bne_rr_gate_eu', 8.25 } }, rrgate_eu_l = { { '/levels/beamng_earth/art/shapes/beamngEarth/bne_rr_gate_eu_l', 8.25 } },
  tropical = {
    { JRI .. 'trees/bush_tropical_1/tro_bush_huge', 5.5 }, { JRI .. 'trees/bush_tropical_2/tro_bush_2_small', 6.1 },
    { JRI .. 'trees/trees_tropical_2/tro_tree_2_filler', 20.2 }, { JRI .. 'trees/trees_tropical_1/tro_tree_1_huge', 20.6 },
    { JRI .. 'trees/trees_tropical_1/tro_tree_1_filler', 33.0 },
  },
}
-- Italy's authored three-metre crash barrier and two end treatments. The back-facing variants reuse
-- the same mesh with its longitudinal/lateral axes reversed; the caps are swapped to flare outwards.
M.CATALOG.guardrail = { { '/art/shapes/objects/italy_guardrails_basic', 0.9 } }
M.CATALOG.guardrail_start = { { '/art/shapes/objects/italy_guardrails_basic_end_cw', 0.9 } }
M.CATALOG.guardrail_end = { { '/art/shapes/objects/italy_guardrails_basic_end_ccw', 0.9 } }
M.CATALOG.guardrail_back = { { '/art/shapes/objects/italy_guardrails_basic', 0.9 } }
M.CATALOG.guardrail_back_start = { { '/art/shapes/objects/italy_guardrails_basic_end_ccw', 0.9 } }
M.CATALOG.guardrail_back_end = { { '/art/shapes/objects/italy_guardrails_basic_end_cw', 0.9 } }
-- Collision is enabled only inside M.SOLID_M by the common Forest-item path; farther rails stay render-only.
M.GROUP_MAX_M.guardrail = 4000
M.GROUP_MAX_M.guardrail_start = 4000
M.GROUP_MAX_M.guardrail_end = 4000
M.GROUP_MAX_M.guardrail_back = 4000
M.GROUP_MAX_M.guardrail_back_start = 4000
M.GROUP_MAX_M.guardrail_back_end = 4000
-- Median barriers (server medians.py) and mapped jersey / cable barriers (guardrails.py), in the same rows: the stock
-- 3 m jersey barrier (it runs along its y axis: the rows' basis says so) and its sloped end; the mod's cable barrier
-- (tools/build_median_props.py: four ropes and a post every 3 m).
M.CATALOG.jersey = { { '/art/shapes/objects/jerseybarrier_3m', 0.88 } }
M.CATALOG.jersey_end = { { '/art/shapes/objects/jerseybarrier_end', 0.88 } }
M.CATALOG.cable = { { '/levels/beamng_earth/art/shapes/beamngEarth/bne_cable_barrier', 1.0 } }
M.GROUP_MAX_M.jersey = 4000
M.GROUP_MAX_M.jersey_end = 4000
M.GROUP_MAX_M.cable = 1500
-- Mapped chain-link fences and noise walls (server fences.py): West Coast USA's chain-link panel (2.04 m along
-- its x, the origin at the forward end) and its concrete noise blocker (4 m along its y).
M.CATALOG.chainlink = { { '/art/shapes/objects/s_chainlink_old', 1.76 } }
M.CATALOG.noisewall = { { WCA .. 'objects/s_concrete_noiseblocker', 2.4 } }
M.GROUP_MAX_M.chainlink = 800
M.GROUP_MAX_M.noisewall = 2000
-- Picnic tables and grills in parks (server picnic.py): Utah's table (2 x 1.4 m, 0.85 m high) and East
-- Coast's grill. The table's materials live with Utah's buildings.
M.CATALOG.picnictable = { { UT .. 'buildings/ut_picnic_table_a', 0.85 } }
M.CATALOG.picnicgrill = { { ECA .. 'buildings/eca_BBQ', 1.48 } }
M.GROUP_MAX_M.picnictable = 400
M.GROUP_MAX_M.picnicgrill = 400
-- Bus shelters (server shelters.py): East Coast's glass stop. Its signed face is local +x, its long
-- side local y, and it sits 0.2 m below its origin.
M.CATALOG.busshelter = { { '/art/shapes/objects/s_busstop_ecu', 2.90 } }
M.GROUP_MAX_M.busshelter = 600
-- Cell and water towers, shipping containers, the drag-strip billboard (poster included) and a stock barn
-- (server worldprops.py).
-- The height is the mesh's vertical size, so a row of that height is the model at 1:1.
M.CATALOG.celltower = { { JV .. 'misc/celltower', 28.93 } }
M.CATALOG.watertower = { { WCA .. 'objects/industrial_watertower', 26.16 } }
M.CATALOG.container = {
  { WCA .. 'objects/s_containers_20_white', 2.59 }, { WCA .. 'objects/s_containers_20_brown', 2.59 },
  { WCA .. 'objects/s_containers_40_white', 2.59 }, { WCA .. 'objects/s_containers_40_brown', 2.59 },
}
M.CATALOG.billboard = { { WCA .. 'objects/dragbillboard', 11.95 } }
M.CATALOG.barn = { { '/levels/hirochi_raceway/art/shapes/buildings/farm_barn', 8.41 } }
M.GROUP_MAX_M.celltower = 2500
M.GROUP_MAX_M.watertower = 2000
M.GROUP_MAX_M.container = 500
M.GROUP_MAX_M.billboard = 900
M.GROUP_MAX_M.barn = 800
-- Pure: whether a group is a road barrier's (guard rails, jersey and cable barriers): continuous runs, not thinned
-- below the tile cap.
function M.isBarrier(g)
  return g ~= nil and (g:sub(1, 9) == 'guardrail' or g:sub(1, 6) == 'jersey' or g == 'cable')
end

-- the Vienna Convention's round km/h signs: their shapes and how far out they show (CATALOG, GROUP_MAX_M)
vienna.install(M)
local MATERIALS = {
  ECA .. 'trees/trees_oak', ECA .. 'trees/trees_beech', ECA .. 'trees/trees_aspen', ECA .. 'trees/trees_douglasfir',
  ECA .. 'trees/ECA_coast_bush', ECA .. 'trees/common', ECA .. 'trees', ECA .. 'rocks',
  WCA .. 'trees/trees_conifer', WCA .. 'trees/trees_oak', WCA .. 'trees/trees_eucalyptus', WCA .. 'trees/trees_palm',
  WCA .. 'trees', UT .. 'trees/Juniper', UT .. 'trees', UT .. 'groundcover', UT .. 'rocks', JV .. 'groundcover',
  JV .. 'rocks', JV .. 'trees/deserttrees', JV .. 'trees', JRI .. 'trees/trees_palm', JRI .. 'trees/trees_tropical_1',
  JRI .. 'trees/trees_tropical_2', JRI .. 'trees/bush_tropical_1', JRI .. 'trees/bush_tropical_2', JRI .. 'trees',
  JRI .. 'trees/vines_tropical',     -- tro_tree_1's vines and aerial roots (tro_vines_2d; orange without it)
  IT .. 'trees/trees_italy', IT .. 'trees', IT .. 'rocks', '/levels/automation_test_track/art/shapes/objects',
  '/levels/automation_test_track/art/shapes/buildings',     -- the utility pole's wood_trim / painted metal
  ECA .. 'signs_usa', '/art/shapes/objects', '/levels/east_coast_usa/art/shapes', WCA .. 'objects', ECA .. 'buildings',
  UT .. 'ut_roadsigns', UT .. 'buildings', '/levels/driver_training/art/shapes/race',
  JV .. 'misc',     -- the cell tower
  '/levels/hirochi_raceway/art/shapes/buildings',     -- the stock barn
  -- the toll booth's (its kerbs, glass, trim and lamps)
  '/art/shapes/garage_and_dealership/garage', '/art/shapes/garage_and_dealership', '/art/shapes/race/dragstrip',
  '/art/shapes/objects/windows_doors_reflective',
}

local stopwatch = require('beamngEarth/stopwatch')
M.clock = stopwatch.new      -- the tree frame's clock (FRAME_MS; tests: their own)
local tiles = {}        -- key "a:tx_ty" -> { anchor, cx, cy, f (flat items), s (stride), n, made = { i -> item }, shown }
local foci = {}         -- other vehicles' collision foci { x, y, r, trees } (M.setFoci, collisionfoci.lua)
local dirty = true      -- a tree pass has work (M.update: a pass with none is skipped)
local lastPass = nil    -- { anchor, x, y, vx, vy, clock } of the last full pass (M.update)
local ghostSince = nil               -- since the last rebase that left ghost tiles (M.transform)
local types = {}        -- "shape" / "shape#n" (not solid) -> TSForestItemData
local pop = {}          -- shape -> its items in the level
local ready = false
M.made, M.removed, M.errors = 0, 0, 0
M.unknownGroups = {}      -- groups seen without a CATALOG entry (logged once each)

local pool = {}          -- ids of empty Forests to reuse
local forestCount = 0
local newForests = 0     -- Forests made this tree frame (FORESTS_PER_FRAME)
M.forests = 0

-- Groups the player turned off (settings: Rocks off, GitHub #5): not shown, removed where made. (After
-- MihneaTeodorStoica's PR #321.)
-- (lampglow: the lamps' far glow, night only: nightlights.lua turns it on after dusk)
M.groupOff = { lampglow = true }
-- The settings' tree density (settings.lua 'trees', the quality presets): scales of the rings' ranks (how many of a
-- tile's items show at a distance) and of the tiles' item caps; and the tree draw distance (settings 'treeDistance',
-- split from the density 2026-10-28, the user: "give player much more control in the graphics settings"): the rings'
-- and caps' distances, and the server's tree tile reach (core.sendGraphics treeScale: vegetation.SEND_M times it;
-- GHOST_FAR_M follows). 'normal': the defaults above. (Before the split 'trees' also set the distance: low 0.75,
-- medium 0.9, high 1.3, ultra 1.6; settings.lua migrates a saved level so.)
M.QUALITY_LEVELS = {
  low = { rank = 0.5, cap = 0.5 },
  medium = { rank = 0.7, cap = 0.7 },
  normal = { rank = 1.0, cap = 1.0 },
  high = { rank = 1.35, cap = 1.5 },
  ultra = { rank = 1.7, cap = 2.2 },
  extreme = { rank = 2.5, cap = 2.5 },      -- (rank 2.5: every item the server sends out to ~700 m x distance)
}
M.DISTANCES = { near = 0.75, medium = 0.9, normal = 1.0, far = 1.3, farther = 1.6, max = 2.2 }
M.TILE_SEND_M, M.TILE_DROP_M = 1280, 1536     -- the server's vegetation SEND_M / DROP_M at distance 1
local BASE_RINGS, BASE_CAP = nil, nil   -- (the defaults, copied at the first setQuality / setDistance)
M.quality = 'normal'
M.distance = 'normal'

-- Pure: the rings and caps of a density level and a distance level from the base ones.
function M.scaledRings(level, rings, caps, distance)
  local q = M.QUALITY_LEVELS[level] or M.QUALITY_LEVELS.normal
  local d = M.DISTANCES[distance or 'normal'] or 1.0
  local r, c = {}, {}
  for i, v in ipairs(rings) do
    r[i] = { v[1] >= 1e8 and v[1] or math.floor(v[1] * d + 0.5), math.min(1.0, v[2] * q.rank) }
  end
  for i, v in ipairs(caps) do
    c[i] = { v[1] >= 1e8 and v[1] or math.floor(v[1] * d + 0.5), math.floor(v[2] * q.cap + 0.5) }
  end
  return r, c
end

-- The server's tree tile reach factor for a distance level (core.sendGraphics treeScale).
function M.distanceScale(distance) return M.DISTANCES[distance or M.distance] or 1.0 end

local function rescale()
  if not BASE_RINGS then
    BASE_RINGS, BASE_CAP = {}, {}
    for i, v in ipairs(M.RINGS) do BASE_RINGS[i] = { v[1], v[2] } end
    for i, v in ipairs(M.TILE_CAP) do BASE_CAP[i] = { v[1], v[2] } end
  end
  M.RINGS, M.TILE_CAP = M.scaledRings(M.quality, BASE_RINGS, BASE_CAP, M.distance)
  M.GHOST_FAR_M = math.floor(M.TILE_SEND_M * M.distanceScale() + 0.5) + (M.TILE_DROP_M - M.TILE_SEND_M)
  M.refresh()
end

function M.setQuality(level)
  if not M.QUALITY_LEVELS[level] or level == M.quality then return false end
  M.quality = level
  rescale()
  return true
end

function M.setDistance(level)
  if not M.DISTANCES[level] or level == M.distance then return false end
  M.distance = level
  rescale()
  return true
end

function M.setGroupEnabled(groups, on)
  local changed = false
  for _, g in ipairs(groups) do
    if (M.groupOff[g] == true) == (on == true) then M.groupOff[g] = (not on) or nil; changed = true end
  end
  if changed then
    M.refresh()
    -- (their colliding items near the car: a collision rebuild once they are gone)
    local ok, roads = pcall(require, 'beamngEarth/roads')
    if ok and roads.requestCollision then roads.requestCollision() end
  end
  return changed
end

-- Pure: the n-th pool Forest's name.
function M.forestName(n)
  return M.FOREST_NAME .. '_' .. tostring(n)
end

local function newForest()
  forestCount = forestCount + 1
  newForests = newForests + 1
  local f = createObject('Forest')
  f:registerObject(M.forestName(forestCount))
  if scenetree.MissionGroup then scenetree.MissionGroup:addObject(f) end
  M.forests = forestCount
  return f
end

-- The tile's Forest: its own, else one from the pool, else a new one; nil when this frame made its new ones already.
local function forest(t)
  local f = t.forestId and scenetree.findObjectById(t.forestId)
  while not f and #pool > 0 do
    f = scenetree.findObjectById(table.remove(pool))
    if f then f:setPosRot(0, 0, 0, 0, 0, 0, 1) end       -- a ghost's (M.transform) had the rebase's transform
  end
  if not f then
    if newForests >= M.FORESTS_PER_FRAME then return nil end
    f = newForest()
  end
  t.forestId = f:getID()
  return f
end

-- An emptied tile's Forest back to the pool.
local function releaseForest(t)
  if t.forestId then pool[#pool + 1] = t.forestId end
  t.forestId = nil
end

-- The stock boom's lamps (instanced_trafficlight) light by the object's instance colour, and a forest item has none:
-- every lamp glowed at once (GitHub #415). The static booms (the ones no switchable head of navgraph.lua stands in
-- for) use a copy of the shape whose lamps are dark lenses: written once into the user folder from the game's own
-- file, its lamp material renamed (STATIC_SIGNAL_MAT, unlit).
M.SIGNAL_STOCK = '/art/shapes/objects/s_trafficlight_boom_sn'
M.STATIC_SIGNAL = '/temp/beamngEarth/shapes/s_trafficlight_boom_static_v1'
M.STATIC_SIGNAL_MAT = 'bne_trafficlight_static'
function M.staticSignalDae(dae)      -- pure: the stock dae's text with its lamp material renamed (nil: not found)
  if type(dae) ~= 'string' or not dae:find('instanced_trafficlight', 1, true) then return nil end
  return (dae:gsub('instanced_trafficlight', M.STATIC_SIGNAL_MAT))
end
function M.staticSignalMaterial()
  return { [M.STATIC_SIGNAL_MAT] = { name = M.STATIC_SIGNAL_MAT, mapTo = M.STATIC_SIGNAL_MAT, class = 'Material',
    version = 1.5, Stages = { { baseColorMap = '/assets/materials/utility/trafficLight/instanced_trafficlight_lights.png',
      baseColorFactor = { 0.06, 0.06, 0.06, 1 }, roughnessFactor = 0.6, vertColor = true }, {}, {}, {} },
    translucent = true, translucentBlendOp = 'Add', castShadows = false } }
end
local function staticSignal()
  local dae, mat = M.STATIC_SIGNAL .. '.dae', M.STATIC_SIGNAL .. '.materials.json'
  if not FS:fileExists(dae) then
    local text = M.staticSignalDae(readFile(M.SIGNAL_STOCK .. '.dae'))
    if not text then return false end
    writeFile(dae, text)
  end
  if not FS:fileExists(mat) then writeFile(mat, jsonEncode(M.staticSignalMaterial())) end
  loadJsonMaterialsFile(mat)
  return FS:fileExists(dae)
end

local function setup()
  if ready then return true end
  for _, m in ipairs(MATERIALS) do
    local p = m .. '/main.materials.json'
    if not FS or FS:fileExists(p) then pcall(loadJsonMaterialsFile, p) end
  end
  if FS and readFile and writeFile then
    local ok, made = pcall(staticSignal)
    if ok and made then M.CATALOG.signal[1][1] = M.STATIC_SIGNAL
    else log('W', 'beamngEarth.trees', 'static signal shape: ' .. tostring(made)) end
  end
  ready = true
  return true
end

-- Other vehicles' collision foci (collisionfoci.lua, through core.lua): solid trees round the ones with trees.
function M.setFoci(f) foci = f or {} end

-- Pure: the item type's name of a shape, solid (collides) or not.
function M.typeName(shape, solid)
  return 'bngEarthTree_' .. shape:gsub('[^%w]', '_') .. (solid and '' or '_n')
end

local function itemType(shape, solid)
  local key = solid and shape or (shape .. '#n')
  local d = types[key]
  if d then return d end
  local name = M.typeName(shape, solid)
  d = scenetree.findObject(name)
  if not d then
    d = (worldEditorCppApi and worldEditorCppApi.createObject) and worldEditorCppApi.createObject('TSForestItemData')
      or createObject('TSForestItemData')
    if not d then return nil end
    d:setField('shapeFile', 0, shape .. '.dae')
    d:setField('collidable', 0, solid and '1' or '0')
    d:setField('windScale', 0, '0.4'); d:setField('branchAmp', 0, '0.1'); d:setField('detailAmp', 0, '0.2')
    d:registerObject(name)
    if Sim and Sim.getDataBlockSet then Sim.getDataBlockSet():addObject(d) end
  end
  types[key] = d
  return d
end

-- The variant of a group for an item of height h: those within 0.8 .. 1.25 of it (else 0.6 .. 1.6), picked by the
-- item's rank; else the nearest. Returns shape, scale.
function M.pick(group, h, rank)
  local list = M.CATALOG[group] or M.CATALOG.deciduous
  local cands = {}
  for _, range in ipairs({ { 0.8, 1.25 }, { 0.6, 1.6 } }) do      -- near its size first, then wider
    for _, v in ipairs(list) do
      if v[2] >= h * range[1] and v[2] <= h * range[2] then cands[#cands + 1] = v end
    end
    if #cands > 0 then break end
  end
  if #cands == 0 then
    local best, bd
    for _, v in ipairs(list) do
      local d = math.abs(math.log(v[2] / math.max(h, 0.1)))
      if not bd or d < bd then best, bd = v, d end
    end
    cands = { best }
  end
  local v = cands[math.floor(rank * 7919) % #cands + 1]
  return v[1], math.max(0.3, math.min(2.5, h / v[2]))
end

local GROUPS = { 'deciduous', 'conifer', 'shrub', 'rock' }
local releaseGhosts
local groupBand

-- A tile's items are one flat array of numbers, STRIDE numbers an item: { group, x, y, z, yaw, scale, height, rank, ... }
-- (server vegetation.tile_items; scale unused: M.pick scales by height). They were a table per item, and a forest holds
-- ~300k (Bodø, GitHub #66: 1.66 M before the tile cap): every garbage collection walks each one, and freeing a place's
-- items held a frame. In-game 2026-10-26, Green Ridge, Maryland (311k items in 138 tiles; same items placed): Lua heap
-- 176 -> 149 MB, a full collection 92-96 -> 76-78 ms, the longest garbage collection frame of a teleport away
-- 148-152 -> 37-38 ms. A server that knows the mod reads it (hello.vegFlat) sends the array itself ('flat'), kept as
-- decoded; rows ({ item }, ...: an older server's vegetation tiles, roads.py's street furniture) are packed into one
-- (M.flatten).
M.STRIDE = 8
local F_GROUP, F_X, F_Y, F_Z, F_YAW, F_SCALE, F_HEIGHT, F_RANK = 1, 2, 3, 4, 5, 6, 7, 8      -- (offsets in an item)
local HIDDEN = { 0, 0, 0, 0, 0, 1, 10, 2 }     -- an item never shown (rank 2): what a broken row becomes
local newTable = (function()
  local ok, f = pcall(require, 'table.new')
  return ok and f or function() return {} end
end)()

-- Pure: rows packed into a flat array (STRIDE numbers a row). A missing yaw, scale, height or rank is 0, 1, 10, 0 (as
-- the rows were read); a row that is not a table or has no position is never shown (HIDDEN).
function M.flatten(rows, stride)
  local n, s = type(rows) == 'table' and #rows or 0, math.floor(tonumber(stride) or M.STRIDE)
  if s < M.STRIDE then s = M.STRIDE end
  local f = newTable(n * s, 0)
  for i = 1, n do
    local r, b = rows[i], (i - 1) * s
    if type(r) ~= 'table' or not (r[F_X] and r[F_Y] and r[F_Z]) then r = HIDDEN end
    f[b + F_GROUP], f[b + F_X], f[b + F_Y], f[b + F_Z] = r[F_GROUP] or 0, r[F_X], r[F_Y], r[F_Z]
    f[b + F_YAW], f[b + F_SCALE], f[b + F_HEIGHT], f[b + F_RANK] = r[F_YAW] or 0, r[F_SCALE] or 1, r[F_HEIGHT] or 10,
      r[F_RANK] or 0
    if s > M.STRIDE and type(r) == 'table' then
      for j = M.STRIDE + 1, s do f[b + j] = type(r[j]) == 'number' and r[j] or 0 end
    end
  end
  return f
end

-- Pure: the variant v's spelling of a shape path (0: itself).
function M.variant(shape, v)
  if not v or v <= 0 then return shape end
  return (shape:gsub('/([^/]+)$', '/' .. string.rep('./', v) .. '%1'))
end

-- Pure: a tile's shape variant from its key ("a:tx_ty" / "a:ptx_ty"): neighbours differ.
function M.tileVariant(key)
  local tx, ty = tostring(key):match('(%-?%d+)_(%-?%d+)$')
  if not tx then return 0 end
  return (tonumber(tx) * 3 + tonumber(ty) * 5) % math.max(M.SHAPE_VARIANTS, 1)
end

-- M.variant's spellings, made once (a gsub an item was ~16 % of the tree work's own Lua time, an offline drive)
local spellings = {}    -- shape -> { variant -> its spelling }
local function spelled(shape, v)
  local byV = spellings[shape]
  if not byV then byV = {}; spellings[shape] = byV end
  local s = byV[v]
  if not s then s = M.variant(shape, v); byV[v] = s end
  return s
end

-- The shape (the tile's variant) and scale of a tile's row i (cached: M.pick makes tables).
local function shapeOf(t, i)
  local s = t.shapes[i]
  if s then return s, t.scales[i] end
  local f, b, sc = t.f, (i - 1) * t.s, nil
  s, sc = M.pick((t.groups or GROUPS)[f[b + F_GROUP] + 1] or 'deciduous', f[b + F_HEIGHT], f[b + F_RANK])
  s = spelled(s, t.variant)
  t.shapes[i], t.scales[i] = s, sc
  return s, sc
end

-- group: a sign's group (vienna_convention_speed_signs.lua) also makes a second item at the same spot and scale, its
-- body, and takes the collision. Only the first shape is counted in pop (allow's budget): the body is shared by every
-- sign, spelled in the tile's variant like the rest, so its population is split.
-- One matrix and two vectors for every item made (from Madderose's PR #41): createNewItem copies the transform into
-- the item, as the stock foliage scatter tool relies on (editor/toolUtilities/foliageScatter.lua reuses its pose and
-- vectors for every item it plants). A new MatrixF and two to five vec3 an item were garbage for the Lua collector,
-- thousands a second while a forest loads. Every item sets the whole rotation and the position: nothing of the item
-- before stays.
local itemMat, itemVec, itemPos = nil, nil, nil
local function makeItem(fd, t, i, shape, sc, solid, group, variant)
  local d = itemType(shape, vienna.faceSolid(group, solid))
  if not d then M.errors = M.errors + 1; return nil end
  if not itemMat then itemMat, itemVec, itemPos = MatrixF(true), vec3(0, 0, 0), vec3(0, 0, 0) end
  local m = itemMat
  -- setFromEuler turns clockwise (the engine's headings): a row's yaw (counter-clockwise from +x, the server's) is
  -- given negated. Positive, every prop was mirrored about the x axis: doors on north-south walls backwards, on
  -- diagonal walls askew, benches and lamps turned (read back in-game 2026-10-17: rows at 141.6 rendered at -141.6)
  local f, b = t.f, (i - 1) * t.s
  local yawAdd = 0
  if t.geo then             -- (made where the tile's placement put it when its first items were made: madePl)
    local x, y, z = geotile.apply(t.madePl, f[b + F_X], f[b + F_Y], f[b + F_Z])
    itemPos:set(x, y, z)
    yawAdd = t.madeYaw or geotile.yawOf(t.madePl)
    t.madeYaw = yawAdd
  else
    itemPos:set(f[b + F_X], f[b + F_Y], f[b + F_Z])
  end
  if t.s >= 17 and type(f[b + 17]) == 'number' then
    -- Guardrails carry their orthonormal world-space basis; local X follows the road chord. (A geotile's: in its
    -- frame, turned by its placement's rotation.)
    local r = t.geo and t.madePl and t.madePl.r
    for c = 0, 2 do
      local vx, vy, vz = f[b + 9 + 3 * c], f[b + 10 + 3 * c], f[b + 11 + 3 * c]
      if r then
        vx, vy, vz = r[1] * vx + r[2] * vy + r[3] * vz, r[4] * vx + r[5] * vy + r[6] * vz, r[7] * vx + r[8] * vy + r[9] * vz
      end
      itemVec:set(vx, vy, vz); m:setColumn(c, itemVec)
    end
    m:setPosition(itemPos)
  else
    -- Existing props use a flat yaw (counter-clockwise in server coordinates).
    itemVec:set(0, 0, -math.rad(f[b + F_YAW] + yawAdd))
    m:setFromEuler(itemVec)
    m:setPosition(itemPos)
  end
  local ok, it = pcall(function() return fd:createNewItem(d, m, sc) end)
  if not ok then M.errors = M.errors + 1; return nil end
  local body = vienna.body(group)
  if body then
    local pair = vienna.addBody(fd, it, spelled(body, variant), m, sc, solid, itemType)
    if pair then it = pair else M.errors = M.errors + 1 end
  end
  M.made = M.made + 1
  pop[shape] = (pop[shape] or 0) + 1
  return it
end

local function removeItem(fd, it, shape)
  if not vienna.remove(fd, it) then pcall(function() fd:removeItem(it) end) end
  M.removed = M.removed + 1
  if shape then pop[shape] = math.max((pop[shape] or 1) - 1, 0) end
end

-- This frame's shapes: may an item of this shape change now? (see SHAPE_ITEMS_PER_FRAME)
local touched, touchedItems = {}, 0
local function allow(shape)
  if touched[shape] then return true end
  local n = pop[shape] or 0
  if next(touched) ~= nil and touchedItems + n > M.SHAPE_ITEMS_PER_FRAME then return false end
  touched[shape], touchedItems = true, touchedItems + n
  return true
end

-- A tile's items as a flat array (STRIDE numbers each, or more: stride), shown from the next update on.
local function addTile(anchorId, tileKey, flat, stride, groups, geo)
  local key = geo and ('g:' .. tostring(tileKey)) or (anchorId .. ':' .. tostring(tileKey))
  local old = tiles[key]
  local tx, ty = tostring(tileKey):match('(%-?%d+)_(%-?%d+)$')
  -- (the shape cache is the items': every message brings items of its own)
  local t = { anchor = anchorId, f = flat, s = stride, n = math.floor(#flat / stride),
    groups = type(groups) == 'table' and groups or nil,
    made = old and old.made or {}, solid = old and old.solid or {}, near = old and old.near,
    shapes = {}, scales = {},
    madeShape = old and old.madeShape or {}, shown = old and old.shown or 0, forestId = old and old.forestId or nil,
    variant = M.tileVariant(key), cx = (tonumber(tx) or 0) * 256 + 128, cy = (tonumber(ty) or 0) * 256 + 128, drop = false }
  if geo then
    -- (its items as made so far stay where they were made: madePl, the Forest's move since in t.moved)
    t.geo, t.frame, t.pivot, t.half = true, geo.frame, geo.pivot, 128
    t.pl = geo.pl
    t.madePl = old and old.madePl or geo.pl
    t.moved = old and old.moved or nil
    M.placeGeo(t)
  end
  tiles[key] = t
  dirty = true
end

-- Pure-ish: a geotile's centre and half size in the current placement (t.pl), for the distances.
function M.placeGeo(t)
  local z, x, y = geotile.parse(t.key or '')
  local cx, cy = geotile.apply(t.pl, 0, 0, t.pivot)
  t.cx, t.cy = cx, cy
  if z and t.anchorObj then
    local q = geotile.quad(t.anchorObj, z, x, y)
    local hx = 0
    for _, c in ipairs(q) do hx = math.max(hx, math.abs(c[1] - cx), math.abs(c[2] - cy)) end
    t.half = hx
  end
end

-- A geotile's placement into anchor a.
local function geoPlacement(frame, a, pivot)
  return geotile.placement(frame, a, 0, 0, pivot)
end
M.geoPlacement = geoPlacement

-- A 'veg_tile' message: { anchorId, key, path, n }. Returns the tile's data (its ambience: ambience.lua).
function M.onTile(msg, currentAnchor, anchorObj)
  local isGeo = geotile.isKey(msg.key)
  if isGeo then
    if not anchorObj or type(msg.path) ~= 'string' then return end
  elseif msg.anchorId ~= currentAnchor or type(msg.path) ~= 'string' then return end
  -- the file's JSON in the message (server staging.inline_json: Windows' first read of a file the server just wrote took
  -- up to 0.5 s in the frame), else the file: { groups, stride, flat } (hello.vegFlat), or { groups, items } (rows)
  local ok, data = true, msg.data
  if type(data) ~= 'table' then ok, data = pcall(jsonDecode, readFile(msg.path) or '') end
  local stride = type(data) == 'table' and tonumber(data.stride)
  local geo = nil
  if isGeo and ok and type(data) == 'table' then
    local frame = geotile.frameOf({ frame = msg.frame or data.frame, key = msg.key })
    local pivot = tonumber(msg.pivot) or tonumber(data.pivot) or 0
    if not frame then M.errors = M.errors + 1; return end
    geo = { frame = frame, pivot = pivot, pl = geoPlacement(frame, anchorObj, pivot) }
  end
  if ok and type(data) == 'table' and type(data.flat) == 'table' and stride and stride >= M.STRIDE
      and stride == math.floor(stride) then
    addTile(msg.anchorId, msg.key, data.flat, stride, data.groups, geo)
  elseif ok and type(data) == 'table' and type(data.items) == 'table' then
    addTile(msg.anchorId, msg.key, M.flatten(data.items, M.STRIDE), M.STRIDE, data.groups, geo)
  else
    M.errors = M.errors + 1
    return
  end
  if geo then
    local t = tiles['g:' .. tostring(msg.key)]
    t.key, t.anchorObj = msg.key, anchorObj
    M.placeGeo(t)
    -- the ambience's nodes (x, y, z, depth, size, count rows) placed into this anchor's world (ambience.lua)
    if type(data.audio) == 'table' then
      local a, out = data.audio, {}
      for i = 1, #a - 5, 6 do
        local x, y, z = geotile.apply(geo.pl, a[i], a[i + 1], a[i + 2])
        out[i], out[i + 1], out[i + 2], out[i + 3], out[i + 4], out[i + 5] = x, y, z, a[i + 3], a[i + 4], a[i + 5]
      end
      data = { audio = out, biome = data.biome, geo = { cx = t.cx, cy = t.cy } }
    end
  end
  return data
end

-- Rows of a tile ({ { group, x, y, z, yaw, scale, height, rank }, ... }): an older server's vegetation tile, a road
-- tile's street furniture (roads.lua, key "p<tx>_<ty>").
function M.onRows(anchorId, tileKey, rows, groups, stride)
  stride = math.floor(tonumber(stride) or M.STRIDE)
  if stride < M.STRIDE then stride = M.STRIDE end
  addTile(anchorId, tileKey, M.flatten(rows, stride), stride, groups)
end

-- Rows of a road geotile (roads.lua: its street furniture 'p<key>', guard rails 'g<key>'), in the tile's frame
-- (frame: its anchor object, pivot), placed into anchor a as the vegetation's geotiles are (and with them at a rebase:
-- M.transform). Dropped with M.dropKey('g:' .. tileKey).
function M.onGeoRows(tileKey, gkey, rows, groups, stride, frame, pivot, a)
  if not frame or not a then return end
  stride = math.floor(tonumber(stride) or M.STRIDE)
  if stride < M.STRIDE then stride = M.STRIDE end
  local geo = { frame = frame, pivot = pivot or 0, pl = geoPlacement(frame, a, pivot or 0) }
  addTile(nil, tileKey, M.flatten(rows, stride), stride, groups, geo)
  local t = tiles['g:' .. tostring(tileKey)]
  t.key, t.anchorObj = gkey, a
  M.placeGeo(t)
end

-- The tile "anchorId:key" goes (roads.lua's furniture).
function M.dropKey(key)
  local t = tiles[key]
  if t then t.drop = true; dirty = true end
end

-- A 'veg_drop' message: the tile's items go.
function M.onDrop(msg)
  local t = tiles[(geotile.isKey(msg.key) and 'g' or msg.anchorId) .. ':' .. tostring(msg.key)]
  if t then t.drop = true; dirty = true end
end

local function threshold(d)
  for _, r in ipairs(M.RINGS) do
    if d <= r[1] then return r[2] end
  end
  return 0
end

-- The rank tile t shows below at distance d: threshold(d), lowered to keep it within M.TILE_CAP (the cap-th lowest
-- of its rows' ranks: exact whatever share of the area's items the server sent, vegetation.TILE_ROWS_MAX).
local function tileThreshold(d, t)
  local want = threshold(d)
  local n = t.n or 0
  for _, r in ipairs(M.TILE_CAP) do
    if d <= r[1] then
      if n <= r[2] then return want end
      if not t.ranks then
        local rk, f, s = newTable(n, 0), t.f, t.s
        for i = 1, n do rk[i] = f[(i - 1) * s + F_RANK] end
        table.sort(rk)
        t.ranks = rk
      end
      return math.min(want, t.ranks[r[2] + 1])
    end
  end
  return want
end
M.tileThreshold = tileThreshold

-- M.suppress(group, x, y) -> true hides that item (set by navgraph.lua); M.refresh() re-runs every tile's rows
-- after it changes.
M.suppress = nil
function M.refresh()
  for _, t in pairs(tiles) do t.pending, t.scan = true, nil end      -- (every row again: an interrupted pass too)
  dirty = true
end

-- Which of the capped groups a tile at distance d still shows (changes re-run its rows).
groupBand = function(t, d)
  local k = 0
  for _, cap in pairs(M.GROUP_MAX_M) do if d <= cap then k = k + cap end end
  return k
end

-- Origin rebase (core.lua finishRebase): each shown tile's Forest takes the rebase's transform (p_new = R*p + t), so
-- its items show where they belong in the new frame at once, and the tile stays as a "ghost" until the new anchor's
-- tiles over it are filled (or it is GHOST_FAR_M behind, or GHOST_MAX_S). Before, every tree went at the
-- rebase and came back over ~25 s (2026-10-17, the user: "trees take several seconds to regenerate after a rebase").
-- A Forest's transform moves its items' rendering but not their collision (probe 2026-10-17: castRayStatic still hit
-- the old spot), which stays in the old frame's place, ~15 km from the player after a rebase, until the ghost goes.
-- A ghost also goes once it is farther from the player than GHOST_FAR_M (the server's vegetation DROP_M: where its tile
-- would have been dropped anyway) or after GHOST_MAX_S. (Not after a quiet spell without new tiles: at 90 mph the new
-- anchor's first tiles took longer and every tree went, 2026-10-17 salt flats.)
M.GHOST_MAX_S = 120
M.GHOST_FAR_M = 1536
M.GHOST_FAR_WAIT_S = 2

function M.transform(xf, geodesy, newAnchor)
  local bx, by, bz, bw = xf:beamngQuat()
  local any = false
  for _, t in pairs(tiles) do
    if t.geo and not t.drop and newAnchor then
      M.replaceGeo(t, newAnchor)
    elseif t.geo then
      t.drop = true; dirty = true       -- (no anchor to place it in: the server sends it again)
    elseif not t.drop then
      local q = t.fq or { 0, 0, 0, 1 }
      local x, y, z, w = geodesy.quatMul(q[1], q[2], q[3], q[4], bx, by, bz, bw)
      t.fq = { x, y, z, w }
      local o = t.ft or { 0, 0, 0 }
      local ox, oy, oz = xf:apply(o[1], o[2], o[3])
      t.ft = { ox, oy, oz }
      local f = t.forestId and scenetree.findObjectById(t.forestId)
      if f then f:setPosRot(ox, oy, oz, x, y, z, w) end
      t.cx, t.cy = xf:apply(t.cx, t.cy, 0)
      t.ghost = true
      t.pending = false
      any = true
    end
  end
  if any then ghostSince = stopwatch.new(); dirty = true end
end

-- A geotile into anchor a's world: its Forest takes the move from where its items were made (madePl) to the new
-- placement (the drawing at once); a tile that must collide is flushed and made again there (update).
function M.replaceGeo(t, a)
  t.anchorObj = a
  t.pl = geoPlacement(t.frame, a, t.pivot)
  M.placeGeo(t)
  local f = t.forestId and scenetree.findObjectById(t.forestId)
  if next(t.made) == nil then
    t.madePl, t.madeYaw, t.moved = t.pl, nil, nil       -- (nothing made yet: made at the new placement)
    if f then f:setPosRot(0, 0, 0, 0, 0, 0, 1) end
  else
    local d = geotile.between(t.madePl, t.pl)
    local x, y, z, w = d:beamngQuat()
    t.moved = { d.t[1], d.t[2], d.t[3], x, y, z, w }
    if f then f:setPosRot(d.t[1], d.t[2], d.t[3], x, y, z, w) end
  end
  t.pending = true
  M.geoMoved = M.geoMoved + 1
  dirty = true
end

-- A teleport's new anchor (core.lua commitTeleport): the geotiles here placed into its world (the server drops the far
-- ones; a teleport near keeps what is around).
function M.reanchor(a)
  for _, t in pairs(tiles) do
    if t.geo and not t.drop and a then M.replaceGeo(t, a) end
  end
end

-- Ghost tiles whose square the current anchor's filled tiles cover go (emptied by update's first loop).
releaseGhosts = function(anchorId, player)
  if not ghostSince then return end
  local all = ghostSince() > M.GHOST_MAX_S * 1000
  local done = {}
  if not all then
    for _, t in pairs(tiles) do
      if (t.anchor == anchorId or t.geo) and not t.ghost and not t.drop then
        local ok = not t.pending and (t.shown or 0) > 0
        local h = t.geo and t.half or 128
        for gx = math.floor((t.cx - h) / 256), math.floor((t.cx + h - 1e-6) / 256) do
          for gy = math.floor((t.cy - h) / 256), math.floor((t.cy + h - 1e-6) / 256) do
            local k = gx .. '_' .. gy
            done[k] = (done[k] ~= false) and ok
          end
        end
      end
    end
  end
  local left = false
  for _, t in pairs(tiles) do
    if t.ghost and not t.drop then
      local ok = all
      if not ok and player and ghostSince() > M.GHOST_FAR_WAIT_S * 1000 then     -- (roads.lua GHOST_FAR_WAIT_S)
        local dx = math.max(math.abs(player.x - t.cx) - 128, 0)
        local dy = math.max(math.abs(player.y - t.cy) - 128, 0)
        ok = dx * dx + dy * dy > M.GHOST_FAR_M * M.GHOST_FAR_M      -- (a disc, as the server's drop: roads.tiles_near)
      end
      if not ok then
        ok = true
        for gx = math.floor((t.cx - 128) / 256), math.floor((t.cx + 128 - 1e-6) / 256) do
          for gy = math.floor((t.cy - 128) / 256), math.floor((t.cy + 128 - 1e-6) / 256) do
            if not done[gx .. '_' .. gy] then ok = false end
          end
        end
      end
      if ok then t.drop = true else left = true end
    end
  end
  if not left then ghostSince = nil end
end

-- Every frame: tiles of other anchors and dropped ones emptied, the others filled / thinned to their
-- distance's rank, M.PER_FRAME items at most.
-- Tree work every EVERY_N-th frame, N times the items: a frame with any Forest change costs the engine's tick a few
-- ms whatever the count (2026-10-15, 100 mph forest drive: tick 3.5 -> 1.65 ms a frame on average, 50 -> 54 fps)
M.EVERY_N = 3
local frameN = 0
-- A pass with nothing to do is skipped (the user, 2026-10-28: "the game speed/time is inconsistent"): it still sorted
-- every tile by distance and ranked each (1331 tiles in Baltimore: ~2.5 ms of Lua and its garbage every third frame,
-- the frame-to-frame swing the engine's smoothed time step turned into game speed pulsing +-25 % over 0.25 s). A pass
-- runs when a tile came, went or waits (dirty), the car or the view moved RECHECK_M since the last one, another car's
-- trees are near (foci) or RECHECK_S went by.
M.RECHECK_M = 5
M.RECHECK_S = 1.0
-- (dirty, lastPass: declared with the tiles, which set them)
-- view: the camera's {x, y} when it is away from the car (core: VIEW_AWAY_M), else nil: what is shown follows what the
-- player looks at, the colliding items stay round the car (the user, 2026-10-20: "the LODs still all follow the car").
-- frameMs: this frame's Lua time for the tree work (default FRAME_MS); 0: none this frame (core: the governor's heavy
-- frame or the quiet frames round it), the batch waits for the next frame.
-- Spare Forests wanted in the pool now: POOL_SPARE_CALM while calm (the loading frame budget), none while driving (a
-- frame budget below it); POOL_SPARE without one (no governor: tools, tests).
function M.spareWanted(frameMs)
  if frameMs == nil then return M.POOL_SPARE end
  return frameMs >= M.LOADING_FRAME_MS and math.max(M.POOL_SPARE, M.POOL_SPARE_CALM) or 0
end

function M.update(anchorId, player, view, frameMs)
  if not next(tiles) then return end
  if frameMs and frameMs <= 0 then return end
  if not setup() then return end
  frameN = frameN + 1
  if frameN % M.EVERY_N ~= 0 then return end
  if not dirty and not ghostSince and #foci == 0 and lastPass and lastPass.anchor == anchorId and player
      and math.abs(player.x - lastPass.x) + math.abs(player.y - lastPass.y) < M.RECHECK_M
      and (view and lastPass.vx and math.abs(view.x - lastPass.vx) + math.abs(view.y - lastPass.vy) < M.RECHECK_M
        or not view and not lastPass.vx)
      and lastPass.clock() < M.RECHECK_S * 1000 then
    M.skipped = (M.skipped or 0) + 1      -- (diagnostics)
    -- nothing to change: a spare Forest made for the pool (its ~9 ms alone, not in a frame of items), as a pass that
    -- changed nothing does below; only while calm (spareWanted)
    if #pool < M.spareWanted(frameMs) then pool[#pool + 1] = newForest():getID() end
    return
  end
  dirty = false                           -- (set again below by anything this pass leaves undone)
  lastPass = player and { anchor = anchorId, x = player.x, y = player.y, vx = view and view.x, vy = view and view.y,
    clock = stopwatch.new() } or nil
  local budget = M.PER_FRAME * M.EVERY_N
  local limit, elapsed = frameMs or M.FRAME_MS, M.clock()
  newForests = 0
  M.lastShapeItems = touchedItems        -- the previous frame's (diagnostics)
  touched, touchedItems = {}, 0
  releaseGhosts(anchorId, player)
  for key, t in pairs(tiles) do
    if (not t.geo and t.anchor ~= anchorId and not t.ghost) or t.drop or t.flush then
      local f = t.forestId and scenetree.findObjectById(t.forestId)
      local fd = f and f:getData()
      local left = false
      for i, it in pairs(t.made) do
        if budget <= 0 or elapsed() > limit then dirty = true; return end
        local sh = t.madeShape[i]
        if not sh or allow(sh) then
          if fd then removeItem(fd, it, sh) end
          t.made[i], t.solid[i], t.madeShape[i] = nil, nil, nil
          budget = budget - 1
        else left = true end
      end
      if not left and t.flush and not t.drop then
        -- (a moved geotile emptied: made again at its placement, the Forest back where it was made)
        if f then f:setPosRot(0, 0, 0, 0, 0, 0, 1) end
        t.flush, t.moved, t.madePl, t.madeYaw = nil, nil, t.pl, nil
        t.shown, t.band, t.near, t.pending, t.scan = 0, nil, nil, true, nil
        M.geoFlushed = M.geoFlushed + 1
        dirty = true
      elseif not left then
        releaseForest(t)
        tiles[key] = nil
      else dirty = true end
    end
  end
  if not player then return end
  -- nearest tiles first (to the camera when it is away: view); [3] the distance to the car (the colliding items)
  local order = {}
  local function boxDist(p, t)
    local h = t.half or 128
    local dx = math.max(math.abs(p.x - t.cx) - h, 0)
    local dy = math.max(math.abs(p.y - t.cy) - h, 0)
    return math.sqrt(dx * dx + dy * dy)
  end
  for key, t in pairs(tiles) do
    if (not t.geo and t.anchor ~= anchorId) or t.drop or t.flush then goto continue end     -- still being emptied
    local dCar = boxDist(player, t)
    order[#order + 1] = { key, view and math.min(boxDist(view, t), dCar) or dCar, dCar }
    ::continue::
  end
  table.sort(order, function(a, b) return a[2] < b[2] end)
  local nearMade = false
  local rowsScanned = 0
  -- solid round other players' cars too (collisionfoci.lua: the foci with trees; traffic has none)
  local function nearFocus(t)
    for _, f in ipairs(foci) do
      if f.trees and boxDist(f, t) <= M.SOLID_M + (t.near and M.SOLID_HYST_M or 0) then return true end
    end
    return false
  end
  local function finish()
    M.lastRows = rowsScanned                -- (diagnostics)
    if nearMade then
      local ok, roads = pcall(require, 'beamngEarth/roads')
      if ok and roads.requestCollision then roads.requestCollision() end
    end
  end
  -- a pass over a tile's rows stopped (its frame's time or items, or no Forest yet): the next pass for the same targets
  -- goes on from row i, or from the first row it left for another frame (skipped: its shapes were full), not from
  -- the tile's first row (a dense tile's 6000 rows looked at again in every frame of its pass)
  local function stop(t, i, skipped, want, band, near)
    t.pending = true
    dirty = true
    t.scan = { from = math.min(i, skipped or i), want = want, band = band, near = near }
    finish()
  end
  for _, o in ipairs(order) do
    local t = tiles[o[1]]
    local fd = nil                          -- the tile's Forest data, made when it first needs an item
    local function data()
      if not fd then
        local fo = forest(t)
        fd = fo and fo:getData()
      end
      return fd
    end
    local want = tileThreshold(o[2], t)
    local band = groupBand(t, o[2])
    local near = o[3] <= M.SOLID_M or (t.near and o[3] <= M.SOLID_M + M.SOLID_HYST_M) or nearFocus(t) or false
    if near and t.moved then
      -- a moved geotile that must collide: its items made again at its placement first (the first loop, next pass)
      t.flush = true
      dirty = true
      goto nexttile
    end
    if want ~= t.shown or band ~= t.band or near ~= t.near or t.pending then
      if rowsScanned > M.ROWS_PER_FRAME then dirty = true; finish(); return end   -- (its pass in a later frame: pending kept)
      t.pending = false
      local scan = t.scan
      local from = scan and scan.want == want and scan.band == band and scan.near == near and scan.from or 1
      t.scan = nil
      local f, s, names = t.f, t.s, t.groups or GROUPS
      rowsScanned = rowsScanned + math.max(0, t.n - from + 1)
      local skipped = nil
      for i = from, t.n do
        if (i - from) % CHECK_ROWS == CHECK_ROWS - 1 and elapsed() > limit then
          stop(t, i, skipped, want, band, near); return
        end
        local b = (i - 1) * s
        local r = f[b + F_RANK]
        local g = names[f[b + F_GROUP] + 1]
        local cap = g and M.GROUP_MAX_M[g]
        local isGuardrail = M.isBarrier(g)
        if isGuardrail then
          -- Preserve continuous 3 m runs until a tile cap actually requires thinning.
          local rowCap
          for _, limit in ipairs(M.TILE_CAP) do
            if o[2] <= limit[1] then rowCap = limit[2]; break end
          end
          if cap and o[2] > cap then
            r = 2
          elseif not rowCap or t.n <= rowCap then
            r = 0
          end
        elseif cap and o[2] > cap then
          r = 2     -- this group is not shown this far out
        end
        if g and M.groupOff[g] then r = 2 end    -- a group the player turned off (setGroupEnabled)
        -- a group this mod has no shapes for (a newer server's): left out, not drawn as a tree
        if g and not M.CATALOG[g] then
          r = 2
          if not M.unknownGroups[g] then M.unknownGroups[g] = true; log('W', 'beamngEarth.trees', 'no shapes for group ' .. g) end
        end
        -- an item something else stands in for (navgraph.lua's switchable signal heads)
        if g and M.suppress and M.suppress(g, f[b + F_X], f[b + F_Y]) then r = 2 end
        local solid = near and not M.NO_COLLIDE[g or '']
        if r < want and (not t.made[i] or (t.solid[i] == true) ~= solid) then
          local sh, sc = shapeOf(t, i)
          if budget <= 0 then stop(t, i, skipped, want, band, near); return end
          if allow(sh) then
            local d = data()
            if not d then stop(t, i, skipped, want, band, near); return end      -- (a new Forest: next frame)
            if t.made[i] then removeItem(d, t.made[i], t.madeShape[i]); budget = budget - 1 end  -- solid <-> not
            t.made[i] = makeItem(d, t, i, sh, sc, solid, g, t.variant)
            t.solid[i] = (t.made[i] and solid) or nil      -- true or nil
            t.madeShape[i] = t.made[i] and sh or nil
            if solid then nearMade = true end
            budget = budget - 1
            if elapsed() > limit then stop(t, i + 1, skipped, want, band, near); return end
          else skipped = skipped or i end          -- another frame (this one's shapes are full)
        elseif r >= want and t.made[i] then
          if budget <= 0 then stop(t, i, skipped, want, band, near); return end
          if allow(t.madeShape[i] or '') then
            local d = data()
            if not d then stop(t, i, skipped, want, band, near); return end
            removeItem(d, t.made[i], t.madeShape[i])
            t.made[i], t.solid[i], t.madeShape[i] = nil, nil, nil
            budget = budget - 1
            if elapsed() > limit then stop(t, i + 1, skipped, want, band, near); return end
          else skipped = skipped or i end
        end
      end
      t.shown = want
      t.band = band
      t.near = near
      if skipped then t.pending = true; t.scan = { from = skipped, want = want, band = band, near = near }; dirty = true end
    end
    ::nexttile::
  end
  finish()
  -- nothing changed this frame: a spare Forest made for the pool (its ~9 ms alone, not in a frame of items)
  if budget == M.PER_FRAME * M.EVERY_N and newForests == 0 and #pool < M.spareWanted(frameMs) then
    pool[#pool + 1] = newForest():getID()
  end
end

-- Disconnect, a new server session, mod unload: every item removed.
function M.clear()
  for _, t in pairs(tiles) do
    local f = t.forestId and scenetree.findObjectById(t.forestId)
    local fd = f and f:getData()
    for i, it in pairs(t.made) do if fd then removeItem(fd, it, t.madeShape[i]) end end
    releaseForest(t)
  end
  tiles, frameN, dirty, lastPass = {}, 0, true, nil
end

-- Level end: the level deletes its objects itself.
function M.reset()
  tiles, types, ready, pool, forestCount, pop = {}, {}, false, {}, 0, {}
  dirty, lastPass = true, nil
end

function M.status()
  local n, items, pending = 0, 0, 0
  for _, t in pairs(tiles) do
    n = n + 1
    if t.pending or t.shown == 0 then pending = pending + 1 end
    for _ in pairs(t.made) do items = items + 1 end
  end
  return { tiles = n, items = items, pending = pending, made = M.made, removed = M.removed, errors = M.errors,
    geoMoved = M.geoMoved, geoFlushed = M.geoFlushed }   -- (geotiles: placed again at a rebase, made again)
end

return M
