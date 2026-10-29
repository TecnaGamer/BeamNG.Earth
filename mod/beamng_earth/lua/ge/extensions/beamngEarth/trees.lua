-- Trees, bushes and rocks from the server's vegetation tiles (server/vegetation.py, --trees on; the user,
-- 2026-10-13: "trees, rocks and other decoration"). Each tile file lists items
-- {group, x, y, z, yaw, scale, height, rank} in the anchor frame; they become Forest items of stock
-- East Coast USA shapes (they have LODs, billboards and collision meshes), the variant nearest the
-- item's height, scaled to it. A tile shows the items whose fixed rank is under its distance's threshold
-- (ROADMAP "deterministic generation": a level shows the same trees, thinned), made and removed a few
-- hundred per frame (~19 us each, 2026-10-13 probe).
local M = {}
local vienna = require('beamngEarth/vienna_convention_speed_signs')

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
M.GROUP_MAX_M = { shrub = 250, desert_shrub = 250, vineyard = 350, vineyard_block = 450, lavender = 350, rock = 400, desert_rock = 400, cliff = 1500, pylon = 1500, gantry = 800, sub_breaker = 400, sub_mast = 1000, sub_support = 300,
  sub_transformer = 500, sub_fence = 200, lamplens = 800, lampglow = 6000, rrcross = 300, rrcross_eu = 300, rrflash = 400,
  rrflash_eu = 400, rrgate = 400, rrgate_l = 400, rrgate_eu = 400, rrgate_eu_l = 400, door = 120, hydrant = 200, bin = 200, bench = 200, polelamp = 400,
  stop = 300, giveway = 300, signal = 500, fence = 150, wallsign_logistics = 800, wallsign_rentabox = 800, wallsign_lemon = 800, wallsign_turboburger = 500,
  speed30 = 300, speed40 = 300, speed50 = 300, speed60 = 300, speed70 = 300 }
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
M.NO_COLLIDE = { shrub = true, desert_shrub = true, vineyard = true, vineyard_block = true, lavender = true, door = true, lamplens = true, lampglow = true, wallsign_logistics = true, wallsign_rentabox = true,
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
M.FRAME_MS = 4.0
M.LOADING_FRAME_MS = 12.0
local CHECK_ROWS = 256
-- Registering a new Forest costs ~9 ms (in-game 2026-10-26: 8-10 ms each, named or not, whatever its data file), and
-- the tiles that came at once made several in one frame: FORESTS_PER_FRAME at most, and POOL_SPARE kept ready in the
-- pool, made one in a tree frame that changes no item.
M.FORESTS_PER_FRAME = 1
M.POOL_SPARE = 4
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
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_small_b', 11.2 }, { IT .. 'trees/trees_italy/maritime_pine', 11.4 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_small_a', 13.4 },
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
  signal = { { '/art/shapes/objects/s_trafficlight_boom_sn', 9.4 } },
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
  -- Provence & Mediterranean vegetation (server/ign_fr.py + server/vegetation.py): cypress trees and Mistral windbreak
  -- hedges (BD TOPO haie), vineyard rows and multi-row blocks (IGN RPG VRC/VRT & Overture vineyard), lavender furrow
  -- rows (IGN RPG LAV), and olive groves / holm oaks (IGN RPG OLI/TRU & Mediterranean scrub)
  cypress = {
    { IT .. 'trees/trees_italy/cypress_hedge_3m', 1.65 }, { IT .. 'trees/trees_italy/cypress_tree', 7.0 },
  },
  vineyard = {
    { IT .. 'trees/trees_italy/grape_vine', 1.71 },
  },
  vineyard_block = {
    { IT .. 'trees/trees_italy/grape_vine_group', 1.86 },
  },
  lavender = {
    { '/levels/beamng_earth/art/shapes/beamngEarth/bne_lavender_row', 0.74 },
  },
  olive = {
    { IT .. 'trees/trees_italy/olive', 4.0 }, { IT .. 'trees/trees_italy/holm_oak', 11.4 },
  },
}
-- the Vienna Convention's round km/h signs: their shapes and how far out they show (CATALOG, GROUP_MAX_M)
vienna.install(M)
local MATERIALS = {
  '/levels/beamng_earth/art/shapes/beamngEarth',
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
  UT .. 'ut_roadsigns', '/levels/driver_training/art/shapes/race',
  -- the toll booth's (its kerbs, glass, trim and lamps)
  '/art/shapes/garage_and_dealership/garage', '/art/shapes/garage_and_dealership', '/art/shapes/race/dragstrip',
  '/art/shapes/objects/windows_doors_reflective',
}

local stopwatch = require('beamngEarth/stopwatch')
M.clock = stopwatch.new      -- the tree frame's clock (FRAME_MS; tests: their own)
local tiles = {}        -- key "a:tx_ty" -> { anchor, cx, cy, f (flat items), s (stride), n, made = { i -> item }, shown }
local foci = {}         -- other vehicles' collision foci { x, y, r, trees } (M.setFoci, collisionfoci.lua)
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

local function setup()
  if ready then return true end
  for _, m in ipairs(MATERIALS) do
    local p = m .. '/main.materials.json'
    if not FS or FS:fileExists(p) then pcall(loadJsonMaterialsFile, p) end
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
function M.flatten(rows)
  local n, s = type(rows) == 'table' and #rows or 0, M.STRIDE
  local f = newTable(n * s, 0)
  for i = 1, n do
    local r, b = rows[i], (i - 1) * s
    if type(r) ~= 'table' or not (r[F_X] and r[F_Y] and r[F_Z]) then r = HIDDEN end
    f[b + F_GROUP], f[b + F_X], f[b + F_Y], f[b + F_Z] = r[F_GROUP] or 0, r[F_X], r[F_Y], r[F_Z]
    f[b + F_YAW], f[b + F_SCALE], f[b + F_HEIGHT], f[b + F_RANK] = r[F_YAW] or 0, r[F_SCALE] or 1, r[F_HEIGHT] or 10,
      r[F_RANK] or 0
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
local function makeItem(fd, t, i, shape, sc, solid, group, variant)
  local d = itemType(shape, vienna.faceSolid(group, solid))
  if not d then M.errors = M.errors + 1; return nil end
  local m = MatrixF(true)
  -- setFromEuler turns clockwise (the engine's headings): a row's yaw (counter-clockwise from +x, the server's) is
  -- given negated. Positive, every prop was mirrored about the x axis: doors on north-south walls backwards, on
  -- diagonal walls askew, benches and lamps turned (read back in-game 2026-10-17: rows at 141.6 rendered at -141.6)
  local f, b = t.f, (i - 1) * t.s
  m:setFromEuler(vec3(0, 0, -math.rad(f[b + F_YAW])))
  m:setPosition(vec3(f[b + F_X], f[b + F_Y], f[b + F_Z]))
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
local function addTile(anchorId, tileKey, flat, stride, groups)
  local key = anchorId .. ':' .. tostring(tileKey)
  local old = tiles[key]
  local tx, ty = tostring(tileKey):match('(%-?%d+)_(%-?%d+)$')
  -- (the shape cache is the items': every message brings items of its own)
  local t = { anchor = anchorId, f = flat, s = stride, n = math.floor(#flat / stride),
    groups = type(groups) == 'table' and groups or nil,
    made = old and old.made or {}, solid = old and old.solid or {}, near = old and old.near,
    shapes = {}, scales = {},
    madeShape = old and old.madeShape or {}, shown = old and old.shown or 0, forestId = old and old.forestId or nil,
    variant = M.tileVariant(key), cx = (tonumber(tx) or 0) * 256 + 128, cy = (tonumber(ty) or 0) * 256 + 128, drop = false }
  tiles[key] = t
end

-- A 'veg_tile' message: { anchorId, key, path, n }. Returns the tile's data (its ambience: ambience.lua).
function M.onTile(msg, currentAnchor)
  if msg.anchorId ~= currentAnchor or type(msg.path) ~= 'string' then return end
  -- the file's JSON in the message (server staging.inline_json: Windows' first read of a file the server just wrote took
  -- up to 0.5 s in the frame), else the file: { groups, stride, flat } (hello.vegFlat), or { groups, items } (rows)
  local ok, data = true, msg.data
  if type(data) ~= 'table' then ok, data = pcall(jsonDecode, readFile(msg.path) or '') end
  local stride = type(data) == 'table' and tonumber(data.stride)
  if ok and type(data) == 'table' and type(data.flat) == 'table' and stride and stride >= M.STRIDE
      and stride == math.floor(stride) then
    addTile(msg.anchorId, msg.key, data.flat, stride, data.groups)
  elseif ok and type(data) == 'table' and type(data.items) == 'table' then
    M.onRows(msg.anchorId, msg.key, data.items, data.groups)
  else
    M.errors = M.errors + 1
    return
  end
  return data
end

-- Rows of a tile ({ { group, x, y, z, yaw, scale, height, rank }, ... }): an older server's vegetation tile, a road
-- tile's street furniture (roads.lua, key "p<tx>_<ty>").
function M.onRows(anchorId, tileKey, rows, groups)
  addTile(anchorId, tileKey, M.flatten(rows), M.STRIDE, groups)
end

-- The tile "anchorId:key" goes (roads.lua's furniture).
function M.dropKey(key)
  local t = tiles[key]
  if t then t.drop = true end
end

-- A 'veg_drop' message: the tile's items go.
function M.onDrop(msg)
  local t = tiles[msg.anchorId .. ':' .. tostring(msg.key)]
  if t then t.drop = true end
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

function M.transform(xf, geodesy)
  local bx, by, bz, bw = xf:beamngQuat()
  local any = false
  for _, t in pairs(tiles) do
    if not t.drop then
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
  if any then ghostSince = stopwatch.new() end
end

-- Ghost tiles whose square the current anchor's filled tiles cover go (emptied by update's first loop).
releaseGhosts = function(anchorId, player)
  if not ghostSince then return end
  local all = ghostSince() > M.GHOST_MAX_S * 1000
  local done = {}
  if not all then
    for _, t in pairs(tiles) do
      if t.anchor == anchorId and not t.ghost and not t.drop then
        local k = math.floor(t.cx / 256) .. '_' .. math.floor(t.cy / 256)
        done[k] = (done[k] ~= false) and not t.pending and (t.shown or 0) > 0
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
        ok = math.max(dx, dy) > M.GHOST_FAR_M
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
-- view: the camera's {x, y} when it is away from the car (core: VIEW_AWAY_M), else nil: what is shown follows what the
-- player looks at, the colliding items stay round the car (the user, 2026-10-20: "the LODs still all follow the car").
-- frameMs: this frame's Lua time for the tree work (default FRAME_MS); 0: none this frame (core: the governor's heavy
-- frame or the quiet frames round it), the batch waits for the next frame.
function M.update(anchorId, player, view, frameMs)
  if not next(tiles) then return end
  if frameMs and frameMs <= 0 then return end
  if not setup() then return end
  frameN = frameN + 1
  if frameN % M.EVERY_N ~= 0 then return end
  local budget = M.PER_FRAME * M.EVERY_N
  local limit, elapsed = frameMs or M.FRAME_MS, M.clock()
  newForests = 0
  M.lastShapeItems = touchedItems        -- the previous frame's (diagnostics)
  touched, touchedItems = {}, 0
  releaseGhosts(anchorId, player)
  for key, t in pairs(tiles) do
    if (t.anchor ~= anchorId and not t.ghost) or t.drop then
      local f = t.forestId and scenetree.findObjectById(t.forestId)
      local fd = f and f:getData()
      local left = false
      for i, it in pairs(t.made) do
        if budget <= 0 or elapsed() > limit then return end
        local sh = t.madeShape[i]
        if not sh or allow(sh) then
          if fd then removeItem(fd, it, sh) end
          t.made[i], t.solid[i], t.madeShape[i] = nil, nil, nil
          budget = budget - 1
        else left = true end
      end
      if not left then
        releaseForest(t)
        tiles[key] = nil
      end
    end
  end
  if not player then return end
  -- nearest tiles first (to the camera when it is away: view); [3] the distance to the car (the colliding items)
  local order = {}
  local function boxDist(p, t)
    local dx = math.max(math.abs(p.x - t.cx) - 128, 0)
    local dy = math.max(math.abs(p.y - t.cy) - 128, 0)
    return math.sqrt(dx * dx + dy * dy)
  end
  for key, t in pairs(tiles) do
    if t.anchor ~= anchorId or t.drop then goto continue end     -- still being emptied
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
    if want ~= t.shown or band ~= t.band or near ~= t.near or t.pending then
      if rowsScanned > M.ROWS_PER_FRAME then finish(); return end      -- (its pass in a later frame: pending kept)
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
        if cap and o[2] > cap then r = 2 end     -- this group is not shown this far out
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
      if skipped then t.pending = true; t.scan = { from = skipped, want = want, band = band, near = near } end
    end
  end
  finish()
  -- nothing changed this frame: a spare Forest made for the pool (its ~9 ms alone, not in a frame of items)
  if budget == M.PER_FRAME * M.EVERY_N and newForests == 0 and #pool < M.POOL_SPARE then
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
  tiles, frameN = {}, 0
end

-- Level end: the level deletes its objects itself.
function M.reset()
  tiles, types, ready, pool, forestCount, pop = {}, {}, false, {}, 0, {}
end

function M.status()
  local n, items, pending = 0, 0, 0
  for _, t in pairs(tiles) do
    n = n + 1
    if t.pending or t.shown == 0 then pending = pending + 1 end
    for _ in pairs(t.made) do items = items + 1 end
  end
  return { tiles = n, items = items, pending = pending, made = M.made, removed = M.removed, errors = M.errors }
end

return M
