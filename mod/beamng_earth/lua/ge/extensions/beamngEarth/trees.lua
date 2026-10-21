-- Trees, bushes and rocks from the server's vegetation tiles (server/vegetation.py, --trees on; the user,
-- 2026-10-13: "trees, rocks and other decoration"). Each tile file lists items
-- {group, x, y, z, yaw, scale, height, rank} in the anchor frame; they become Forest items of stock
-- East Coast USA shapes (they have LODs, billboards and collision meshes), the variant nearest the
-- item's height, scaled to it. A tile shows the items whose fixed rank is under its distance's threshold
-- (ROADMAP "deterministic generation": a level shows the same trees, thinned), made and removed a few
-- hundred per frame (~19 us each, 2026-10-13 probe).
local M = {}

M.PER_FRAME = 20          -- micro-budget: 15-30 items made or removed per frame at most in governed mode
M.MICRO_PER_FRAME = 20    -- governed per-frame item cap (15-30 items/frame)
M.LEGACY_PER_FRAME = 250  -- un-governed offline unit test budget (when gov == nil)
M.MAX_WALL_MS = 0.85      -- hard wall-clock cutoff (< 1.0 ms) per frame
M.MICRO_BUDGET_MS = 0.85
M.ROWS_PER_FRAME = 1500   -- rows of changing tiles looked at per frame at most
M.MICRO_ROWS_PER_FRAME = 1500
M.MICRO_DEMOTE_ROWS_PER_FRAME = 2500
M.LEGACY_ROWS_PER_FRAME = 12000
M.EVERY_N = 1             -- sawtooth removed: smooth micro-batches on every calm frame
M.GOV_KIND = 'treeBatch'  -- governor token bucket unit kind
M.GOV_PRIOR_MS = 0.5      -- initial cost estimate (ms) in governor token bucket
M.COLLISION_DEBOUNCE_S = 2.0      -- quiet window (s) after solid tree additions before requesting collision rebuild
M.COLLISION_MAX_WAIT_S = 4.0      -- max delay (s) before flushing pending solid tree collision request
M.COLLISION_MIN_INTERVAL_S = 8.0  -- minimum interval (s) between tree-triggered collision rebuild requests
-- Tree rings: clamped at 1600m to prevent infinite accumulation of far-horizon billboard instances
-- in Torque3D's CPU shadow culling quadtree (RenderShadows), keeping active instances under 35k.
-- Active Forest rings capped to 5 rings (900 m) instead of 8 rings (1600 m):
-- distant trees beyond 900 m cannot be seen clearly and their shadow casters are culled anyway,
-- yet they create separate Forest SceneObjects that Torque3D traverses.
M.MAX_FOREST_RINGS = 5
M.MAX_RING_M = 900
M.RINGS = { { 220, 1.0 }, { 400, 0.6 }, { 580, 0.35 }, { 750, 0.2 }, { 900, 0.1 } }   -- tile distance (m): the rank shown below (5 rings, capped at 900 m)
-- A dense forest's tile can have ~20k items (Bodø, GitHub #66: 1.66 M items in the level, ~100 ms frames): a tile
-- shows at most this many items at each distance (by its own ranks, so a tile the server already capped at 30 % still
-- keeps its cap-th trees instead of 30 % of the cap). Sparse tiles (under the cap) follow RINGS as before.
M.TILE_CAP = { { 300, 6000 }, { 700, 2400 }, { 1500, 900 }, { 1e9, 400 } }
-- small props only near the player (2026-10-14, Fells Point at street level: ~10 ms a frame for the Forest
-- with a door on every house out to 300 m): the tile distance (m) beyond which a group is not shown
-- rocks only over the fine ground (farfield.NEAR_RADII: L0 within 400 m): beyond it the coarser tiles round off the
-- ridges and a rock placed on the real ground floated over the drawn one (Stelvio pass, the user's report 2026-10-17)
M.GROUP_MAX_M = { shrub = 250, desert_shrub = 250, rock = 400, desert_rock = 400, door = 120, hydrant = 200, bin = 200, bench = 200, polelamp = 400,
  stop = 300, giveway = 300, signal = 500, fence = 150, wallsign_logistics = 800, wallsign_rentabox = 800, wallsign_lemon = 800, wallsign_turboburger = 500,
  speed30 = 300, speed40 = 300, speed50 = 300, speed60 = 300, speed70 = 300,
  speedk40 = 300, speedk60 = 300, speedk80 = 300, speedk100 = 300 }
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
M.SOLID_M = 90        -- (was 300 + 100: up to ~20k solid trees in a forest, and every terrain block move rebuilt all their
                      -- collision, a ~30 ms physics frame; 200 + 50 is still 5 s ahead at 100 mph, 2026-10-15)
M.SOLID_HYST_M = 35
M.NO_COLLIDE = { shrub = true, desert_shrub = true, door = true, wallsign_logistics = true, wallsign_rentabox = true,
  wallsign_lemon = true, wallsign_turboburger = true }
-- The 100 mph stutter (2026-10-15 probes): a frame that adds or removes Forest items costs the engine's tick ~0.5 us
-- for every item in the level of each SHAPE touched (whatever its item type or Forest), so a frame making 300 items of
-- ~20 shapes among ~130k items cost ~100 ms. A frame touches shapes whose items add up to SHAPE_ITEMS_PER_FRAME at most
-- (at least one shape), and bushes (half of all items, one shape alone 34k) show near the player only.
M.SHAPE_ITEMS_PER_FRAME = 600
M.MICRO_SHAPE_ITEMS = 600
-- Cap of distinct Forest SceneObjects mutated per frame in governed mode: each mutated Forest marks its
-- ForestCell quadtree dirty and rebuilds imposter/mesh vertex batches on the next RenderShadows pass.
-- Batching all rows of 1-2 Forests per frame instead of touching 5-15 Forests avoids O(N_forests) batch rebuild spikes.
M.MAX_FORESTS_PER_FRAME = 2
M.SOLID_SHADOW_NULL_PX = 35
M.NONSOLID_SHADOW_NULL_PX = 150
-- The engine tells shapes apart by their path, so a tile's items use one of SHAPE_VARIANTS spellings of each shape's
-- path ('dir/./name', 'dir/././name', ...: the same file, a shape of its own), which splits the big populations (one
-- beech ~7.7k items: a frame touching it cost ~4-9 ms). A variant's first items cost one ~12 ms frame (its load).
M.SHAPE_VARIANTS = 1 -- locked to 1 to keep ForestItemData batch buckets minimal (reducing draw calls)

local ECA = '/levels/east_coast_usa/art/shapes/'
local WCA = '/levels/west_coast_usa/art/shapes/'
local UT = '/levels/Utah/art/shapes/'
local JV = '/levels/johnson_valley/art/shapes/'
local JRI = '/levels/jungle_rock_island/art/shapes/'
local IT = '/levels/italy/art/shapes/'
-- the stock shapes per group with their measured heights (m; TSStatic world boxes, 2026-10-13/14)
M.CATALOG = {
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
  -- ... and the round km/h signs (furniture.SIGN_KMH, outside the mph countries: Driver Training's)
  speedk40 = { { '/levels/driver_training/art/shapes/race/sign_speed40kph', 2.47 } },
  speedk60 = { { '/levels/driver_training/art/shapes/race/sign_speed60kph', 2.47 } },
  speedk80 = { { '/levels/driver_training/art/shapes/race/sign_speed80kph', 2.47 } },
  speedk100 = { { '/levels/driver_training/art/shapes/race/sign_speed100kph', 2.47 } },
  hydrant = { { '/levels/west_coast_usa/art/shapes/objects/hydrant1', 1.5 } },
  bench = { { ECA .. 'clutter/clutter_city_bench_wood', 0.9 } },
  bin = { { '/art/shapes/garage_and_dealership/Clutter/clutter_city_bin_round', 1.1 }, { WCA .. 'objects/garbage_bin_a', 1.1 } },
  polelamp = { { ECA .. 'buildings/electrical_pole_lamp', 1.1 } },
  streetlight = { { '/art/shapes/garage_and_dealership/Clutter/eca_lamppost', 9.0 } },
  pole = { { '/levels/automation_test_track/art/shapes/objects/s_powerpole_3wire', 9.2 } },
  tropical = {
    { JRI .. 'trees/bush_tropical_1/tro_bush_huge', 5.5 }, { JRI .. 'trees/bush_tropical_2/tro_bush_2_small', 6.1 },
    { JRI .. 'trees/trees_tropical_2/tro_tree_2_filler', 20.2 }, { JRI .. 'trees/trees_tropical_1/tro_tree_1_huge', 20.6 },
    { JRI .. 'trees/trees_tropical_1/tro_tree_1_filler', 33.0 },
  },
}
local MATERIALS = {
  ECA .. 'trees/trees_oak', ECA .. 'trees/trees_beech', ECA .. 'trees/trees_aspen', ECA .. 'trees/trees_douglasfir',
  ECA .. 'trees/ECA_coast_bush', ECA .. 'trees/common', ECA .. 'trees', ECA .. 'rocks',
  WCA .. 'trees/trees_conifer', WCA .. 'trees/trees_oak', WCA .. 'trees/trees_eucalyptus', WCA .. 'trees/trees_palm',
  WCA .. 'trees', UT .. 'trees/Juniper', UT .. 'trees', UT .. 'groundcover', UT .. 'rocks', JV .. 'groundcover',
  JV .. 'rocks', JV .. 'trees/deserttrees', JV .. 'trees', JRI .. 'trees/trees_palm', JRI .. 'trees/trees_tropical_1',
  JRI .. 'trees/trees_tropical_2', JRI .. 'trees/bush_tropical_1', JRI .. 'trees/bush_tropical_2', JRI .. 'trees',
  IT .. 'trees/trees_italy', IT .. 'trees', '/levels/automation_test_track/art/shapes/objects',
  '/levels/automation_test_track/art/shapes/buildings',     -- the utility pole's wood_trim / painted metal
  ECA .. 'signs_usa', '/art/shapes/objects', '/levels/east_coast_usa/art/shapes', WCA .. 'objects', ECA .. 'buildings',
  UT .. 'ut_roadsigns', '/levels/driver_training/art/shapes/race',
}

local stopwatch = require('beamngEarth/stopwatch')
local tiles = {}        -- key "a:tx_ty" -> { anchor, cx, cy, rows, made = { i -> item }, shown = rank }
local ghostSince = nil               -- since the last rebase that left ghost tiles (M.transform)
local types = {}        -- "shape" / "shape#n" (not solid) -> TSForestItemData
local pop = {}          -- shape -> its items in the level
local ready = false
M.made, M.removed, M.errors = 0, 0, 0

local pool = {}          -- ids of empty Forests to reuse (kept hidden while pooled)
local forestHidden = {}  -- forestId -> true while hidden in pool
local forestCount = 0
M.forests = 0

local function setForestHidden(id, f, hidden)
  local isHidden = (forestHidden[id] == true)
  if isHidden == hidden then return end
  forestHidden[id] = hidden or nil
  if not f then f = scenetree.findObjectById(id) end
  if not f then return end
  if f.setHidden then f:setHidden(hidden) else f.hidden = hidden end
end

-- Pure: the n-th pool Forest's name.
function M.forestName(n)
  return M.FOREST_NAME .. '_' .. tostring(n)
end

-- The tile's Forest: its own, else one from the pool, else a new one.
local function forest(t)
  local f = t.forestId and scenetree.findObjectById(t.forestId)
  while not f and #pool > 0 do
    f = scenetree.findObjectById(table.remove(pool))
    if f then f:setPosRot(0, 0, 0, 0, 0, 0, 1) end       -- a ghost's (M.transform) had the rebase's transform
  end
  if not f then
    forestCount = forestCount + 1
    f = createObject('Forest')
    f:registerObject(M.forestName(forestCount))
    if scenetree.MissionGroup then scenetree.MissionGroup:addObject(f) end
    M.forests = forestCount
  end
  local id = f:getID()
  setForestHidden(id, f, false)
  t.forestId = id
  return f
end

-- An emptied tile's Forest is hidden and returned to the pool.
local function releaseForest(t)
  local id = t.forestId
  if not id then return end
  t.forestId = nil
  setForestHidden(id, nil, true)
  pool[#pool + 1] = id
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
    local shadowNullPx = solid and (M.SOLID_SHADOW_NULL_PX or 35) or (M.NONSOLID_SHADOW_NULL_PX or 150)
    d:setField('shadowNullDetailSize', 0, tostring(shadowNullPx))
    d:registerObject(name)
    if Sim and Sim.getDataBlockSet then Sim.getDataBlockSet():addObject(d) end
  end
  types[key] = d
  return d
end

-- The variant of a group for an item of height h: those within 0.8 .. 1.25 of it (else 0.6 .. 1.6), picked by the
-- item's rank; else the nearest. Returns shape, scale. Zero-allocation using module-level scratch buffers.
local PICK_RANGES = { { 0.8, 1.25 }, { 0.6, 1.6 } }
local pickCands = {}

function M.pick(group, h, rank)
  local list = M.CATALOG[group] or M.CATALOG.deciduous
  local nCands = 0
  for rIdx = 1, 2 do
    local range = PICK_RANGES[rIdx]
    local lo, hi = h * range[1], h * range[2]
    for i = 1, #list do
      local v = list[i]
      if v[2] >= lo and v[2] <= hi then
        nCands = nCands + 1
        pickCands[nCands] = v
      end
    end
    if nCands > 0 then break end
  end
  local v
  if nCands == 0 then
    local best, bd
    local hSafe = math.max(h, 0.1)
    for i = 1, #list do
      local item = list[i]
      local d = math.abs(math.log(item[2] / hSafe))
      if not bd or d < bd then best, bd = item, d end
    end
    v = best
  else
    v = pickCands[math.floor(rank * 7919) % nCands + 1]
  end
  return v[1], math.max(0.3, math.min(2.5, h / v[2]))
end

local GROUPS = {
  'deciduous', 'conifer', 'shrub', 'rock', 'pine_west', 'palm',
  'juniper', 'desert_shrub', 'desert_rock', 'oak_west', 'tropical',
}

local VBIN_STRIDE = 16
local binRowScratch = { 0, 0, 0, 0, 0, 1, 10, 0 }
local tileStructPool = {}
for idx = 1, 256 do
  tileStructPool[idx] = {
    made = {},
    solid = {},
    shapes = {},
    scales = {},
    madeShape = {},
  }
end

local function decodeFloat32(b1, b2, b3, b4)
  if b1 == 0 and b2 == 0 and b3 == 0 and (b4 == 0 or b4 == 128) then
    return 0.0
  end
  local sign = b4 >= 128 and -1.0 or 1.0
  local exp = (b4 % 128) * 2 + math.floor(b3 / 128)
  local mant = ((b3 % 128) * 65536 + b2 * 256 + b1) / 8388608.0
  if exp == 255 then
    return 0.0
  elseif exp == 0 then
    return sign * mant * (2.0 ^ -126)
  end
  return sign * (1.0 + mant) * (2.0 ^ (exp - 127))
end

-- Decode a single 16-byte '<fffBBBB' binary tree record on demand using string.byte intrinsic.
local function decodeBinRow(bin, i)
  local base = (i - 1) * VBIN_STRIDE
  local b1, b2, b3, b4, b5, b6, b7, b8, b9, b10, b11, b12, grp, yawQ, scaleQ, flags =
    string.byte(bin, base + 1, base + 16)
  local x = decodeFloat32(b1 or 0, b2 or 0, b3 or 0, b4 or 0)
  local y = decodeFloat32(b5 or 0, b6 or 0, b7 or 0, b8 or 0)
  local z = decodeFloat32(b9 or 0, b10 or 0, b11 or 0, b12 or 0)
  local yaw = (yawQ or 0) * (360.0 / 256.0)
  local sc = (scaleQ and scaleQ > 0) and (scaleQ / 100.0) or 1.0
  local rank = (flags or 0) * (1.0 / 256.0)
  local gName = GROUPS[(grp or 0) + 1] or 'deciduous'
  local cat = M.CATALOG[gName] or M.CATALOG.deciduous
  local refH = (cat[math.ceil(#cat / 2)] and cat[math.ceil(#cat / 2)][2]) or 10.0
  binRowScratch[1] = grp or 0
  binRowScratch[2] = x
  binRowScratch[3] = y
  binRowScratch[4] = z
  binRowScratch[5] = yaw
  binRowScratch[6] = sc
  binRowScratch[7] = refH * sc
  binRowScratch[8] = rank
  return binRowScratch
end
local releaseGhosts
local groupBand
local treeClock = stopwatch.clock()
local orphanDrops = {}
local workPending = false
local lastSettledX, lastSettledY = nil, nil
local collisionPending = false
local firstSolidAddAt = nil
local lastSolidAddAt = -1e9
local lastCollisionFlushAt = -1e9

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

-- The shape (the tile's variant) and scale of a tile's row i (cached).
local function shapeOf(t, i, row)
  local s = t.shapes[i]
  if s then return s, t.scales[i] end
  local sc
  s, sc = M.pick((t.groups or GROUPS)[(row[1] or 0) + 1] or 'deciduous', row[7] or 10, row[8] or 0)
  s = M.variant(s, (M.SHAPE_VARIANTS and M.SHAPE_VARIANTS <= 1) and 0 or t.variant)
  t.shapes[i], t.scales[i] = s, sc
  return s, sc
end

local scratchMat = nil
local scratchEuler = nil
local scratchPos = nil

-- This frame's shapes and distinct Forests: may an item of this shape/Forest change now?
local touched, touchedItems, touchedCount = {}, 0, 0
local touchedForests, touchedForestCount = {}, 0
local function resetTouched()
  M.lastShapeItems = touchedItems        -- the previous frame's (diagnostics)
  M.lastForestsTouched = 0
  for k in pairs(touched) do touched[k] = nil end
  for k in pairs(touchedForests) do touchedForests[k] = nil end
  touchedItems, touchedCount, touchedForestCount = 0, 0, 0
end

local function allowForest(t, maxForests, bypassCap)
  if bypassCap then return true end
  if touchedForests[t] or (t.forestId and touchedForests[t.forestId]) then return true end
  if maxForests and touchedForestCount >= maxForests then return false end
  return true
end

local function noteForestTouched(t)
  if not touchedForests[t] and not (t.forestId and touchedForests[t.forestId]) then
    touchedForestCount = touchedForestCount + 1
    M.lastForestsTouched = touchedForestCount
  end
  touchedForests[t] = true
  if t.forestId then touchedForests[t.forestId] = true end
end

local function tileForestData(t, fd)
  if not fd then
    local fObj = forest(t)
    noteForestTouched(t)
    fd = fObj:getData()
  end
  return fd
end

local shadowctl = nil
local shadowctlResolved = false
local function notifyShadowBurst()
  local sc = shadowctl
  if not sc then
    sc = package.loaded['beamngEarth/shadowctl']
    if not sc and not shadowctlResolved then
      shadowctlResolved = true
      local ok, mod = pcall(require, 'beamngEarth/shadowctl')
      if ok and type(mod) == 'table' then sc = mod end
    end
    shadowctl = sc
  end
  if sc and sc.noteBurst then sc.noteBurst() end
end

local function allow(shape)
  if not shape or shape == '' then return true end
  if touched[shape] then return true end
  local n = math.max(pop[shape] or 0, 1)
  if touchedCount > 0 and touchedItems + n > M.SHAPE_ITEMS_PER_FRAME then return false end
  touched[shape] = true
  touchedCount = touchedCount + 1
  touchedItems = touchedItems + n
  return true
end

local function makeItem(fd, row, shape, sc, solid)
  local d = itemType(shape, solid)
  if not d then M.errors = M.errors + 1; return nil end
  scratchMat = scratchMat or MatrixF(true)
  scratchEuler = scratchEuler or vec3(0, 0, 0)
  scratchPos = scratchPos or vec3(0, 0, 0)
  if not scratchEuler.set and type(scratchEuler) == 'table' then
    local mt = getmetatable(scratchEuler)
    if mt then mt.set = function(v, x, y, z) v.x, v.y, v.z = x, y, z end end
  end
  -- setFromEuler turns clockwise (the engine's headings): a row's yaw (counter-clockwise from +x, the server's) is
  -- given negated. Positive, every prop was mirrored about the x axis: doors on north-south walls backwards, on
  -- diagonal walls askew, benches and lamps turned (read back in-game 2026-10-17: rows at 141.6 rendered at -141.6)
  scratchEuler:set(0, 0, -math.rad(row[5] or 0))
  scratchMat:setFromEuler(scratchEuler)
  scratchPos:set(row[2], row[3], row[4])
  scratchMat:setPosition(scratchPos)
  local ok, it = pcall(fd.createNewItem, fd, d, scratchMat, sc)
  if not ok then M.errors = M.errors + 1; return nil end
  M.made = M.made + 1
  local newPop = (pop[shape] or 0) + 1
  pop[shape] = newPop
  -- Track new items added to already-touched shapes so unpopulated shapes cannot leak past SHAPE_ITEMS_PER_FRAME
  if touched[shape] and newPop > 1 then
    touchedItems = touchedItems + 1
  end
  return it
end

local function removeItem(fd, it, shape)
  pcall(fd.removeItem, fd, it)
  M.removed = M.removed + 1
  if shape then pop[shape] = math.max((pop[shape] or 1) - 1, 0) end
end

-- A 'veg_tile' message: { anchorId, key, path, n }.
function M.onBin(anchorId, tileKey, bin, groups)
  local key = tostring(anchorId) .. ':' .. tostring(tileKey)
  local t = tiles[key]
  if t then
    if t.bin ~= bin or t.rows ~= nil then
      if next(t.made) ~= nil then
        orphanDrops[#orphanDrops + 1] = {
          key = key,
          forestId = t.forestId,
          made = t.made,
          solid = t.solid,
          madeShape = t.madeShape,
        }
        t.forestId = nil
        local spare = table.remove(tileStructPool)
        t.made = spare and spare.made or {}
        t.solid = spare and spare.solid or {}
        t.madeShape = spare and spare.madeShape or {}
      else
        releaseForest(t)
      end
      for k in pairs(t.shapes) do t.shapes[k] = nil end
      for k in pairs(t.scales) do t.scales[k] = nil end
      t.shown, t.band, t.near, t.shadow, t.ranks = 0, nil, nil, nil, nil
    end
  else
    t = table.remove(tileStructPool)
    if not t then
      t = { made = {}, solid = {}, shapes = {}, scales = {}, madeShape = {} }
    end
    t.shown, t.band, t.near, t.shadow, t.forestId, t.ranks = 0, nil, nil, nil, nil, nil
  end
  local tx, ty = tostring(tileKey):match('(%-?%d+)_(%-?%d+)$')
  t.anchor = anchorId
  t.bin = bin
  t.nRows = math.floor(#bin / VBIN_STRIDE)
  t.rows = nil
  t.groups = type(groups) == 'table' and groups or nil
  t.variant = M.tileVariant(key)
  t.cx = (tonumber(tx) or 0) * 256 + 128
  t.cy = (tonumber(ty) or 0) * 256 + 128
  t.drop = false
  t.ghost = nil
  t.pending = true
  t.nextRow = nil
  t.thinning = nil
  t.fq, t.ft = nil, nil
  tiles[key] = t
  workPending = true
end

function M.onTile(msg, currentAnchor)
  if not msg then return end
  local anchorId = msg.anchorId or currentAnchor or 1
  if currentAnchor ~= nil and msg.anchorId ~= nil and msg.anchorId ~= currentAnchor then return end
  local bin = type(msg.bin) == 'string' and msg.bin or nil
  if not bin and type(msg.vbin_path) == 'string' and readFile then
    bin = readFile(msg.vbin_path)
  end
  if not bin and type(msg.path) == 'string' and msg.path:sub(-5) == '.vbin' and readFile then
    bin = readFile(msg.path)
  end
  if type(bin) == 'string' then
    M.onBin(anchorId, msg.key, bin, msg.groups)
    return
  end
  if type(msg.path) ~= 'string' then return end

  local text = readFile(msg.path)
  local ok, data = pcall(jsonDecode, text or '')
  if not ok or type(data) ~= 'table' or type(data.items) ~= 'table' then
    M.errors = M.errors + 1
    return
  end
  M.onRows(msg.anchorId, msg.key, data.items, data.groups)
end

-- Rows of a tile from elsewhere (a road tile's street furniture: roads.lua, key "p<tx>_<ty>").
function M.onRows(anchorId, tileKey, rows, groups)
  local key = anchorId .. ':' .. tostring(tileKey)
  local old = tiles[key]
  if old and old.rows ~= rows then
    if next(old.made) ~= nil then
      orphanDrops[#orphanDrops + 1] = {
        key = key,
        forestId = old.forestId,
        made = old.made,
        solid = old.solid,
        madeShape = old.madeShape,
      }
      old.forestId = nil
      old.made, old.solid, old.madeShape, old.shown, old.band, old.near = {}, {}, {}, 0, nil, nil
    else
      releaseForest(old)
    end
  end
  local tx, ty = tostring(tileKey):match('(%-?%d+)_(%-?%d+)$')
  local t = { anchor = anchorId, rows = rows, groups = type(groups) == 'table' and groups or nil,
    made = old and old.made or {}, solid = old and old.solid or {}, near = old and old.near,
    shapes = old and old.rows == rows and old.shapes or {}, scales = old and old.rows == rows and old.scales or {},
    ranks = old and old.rows == rows and old.ranks or nil,
    madeShape = old and old.madeShape or {}, shown = old and old.shown or 0, forestId = old and old.forestId or nil,
    variant = M.tileVariant(key), cx = (tonumber(tx) or 0) * 256 + 128, cy = (tonumber(ty) or 0) * 256 + 128, drop = false }
  tiles[key] = t
  workPending = true
end

-- The tile "anchorId:key" goes (roads.lua's furniture).
function M.dropKey(key)
  local t = tiles[key]
  if t then t.drop = true; workPending = true end
end

-- A 'veg_drop' message: the tile's items go.
function M.onDrop(msg)
  local t = tiles[msg.anchorId .. ':' .. tostring(msg.key)]
  if t then t.drop = true; workPending = true end
end

local function threshold(d)
  if d > (M.MAX_RING_M or 900) then return 0 end
  for _, r in ipairs(M.RINGS) do
    if d <= r[1] then return r[2] end
  end
  return 0
end

-- What a tile at distance d shows: threshold(d), and at most M.TILE_CAP's rank for the tile (sorted once on t.ranks).
local function tileThreshold(d, t)
  local th = threshold(d)
  if th <= 0 or not t then return th end
  local bin = t.bin
  local rows = t.rows
  local nRows = bin and (t.nRows or math.floor(#bin / VBIN_STRIDE)) or (rows and #rows or 0)
  if nRows == 0 then return th end
  for _, r in ipairs(M.TILE_CAP) do
    if d <= r[1] then
      if nRows <= r[2] then return th end
      if not t.ranks then
        local rk = {}
        if bin then
          for i = 1, nRows do rk[i] = decodeBinRow(bin, i)[8] or 0 end
        else
          for i = 1, nRows do rk[i] = rows[i][8] or 0 end
        end
        table.sort(rk)
        t.ranks = rk
      end
      return math.min(th, t.ranks[r[2]])
    end
  end
  return th
end
M.tileThreshold = tileThreshold

-- M.suppress(group, x, y) -> true hides that item (set by navgraph.lua); M.refresh() re-runs every tile's rows
-- after it changes.
M.suppress = nil
function M.refresh()
  for _, t in pairs(tiles) do t.pending, t.nextRow = true, nil end
  workPending = true
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
      t.pending, t.nextRow = false, nil
      any = true
    end
  end
  if any then ghostSince = stopwatch.new(); workPending = true end
end

local ghostDoneScratch = {}
-- Ghost tiles whose square the current anchor's filled tiles cover go (emptied by update's first loop).
releaseGhosts = function(anchorId, player)
  if not ghostSince then return end
  local all = ghostSince() > M.GHOST_MAX_S * 1000
  local done = ghostDoneScratch
  for k in pairs(done) do done[k] = nil end
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
      if ok then t.drop = true; workPending = true else left = true end
    end
  end
  if not left then ghostSince = nil end
end

-- Does trees.lua have pending streaming/mutation work for governor.beginFrame?
function M.hasWork(anchorId, player)
  if not next(tiles) and #orphanDrops == 0 then return false end
  if workPending or #orphanDrops > 0 then return true end
  if player and lastSettledX and lastSettledY then
    local dx, dy = player.x - lastSettledX, player.y - lastSettledY
    if dx * dx + dy * dy > 16 then return true end
  end
  return false
end

local dropsScratch, dropsPool = {}, {}
local orderScratch, orderPool = {}, {}

local function acquireDropEntry(idx, key, t, solid, isOrphan)
  local e = dropsPool[idx]
  if not e then e = {}; dropsPool[idx] = e end
  e.key, e.t, e.solid, e.isOrphan = key, t, solid, isOrphan
  return e
end

local function acquireOrderEntry(idx, key, dist, prio, want, band, near, demote, sortKey, nearSolid)
  local e = orderPool[idx]
  if not e then e = {}; orderPool[idx] = e end
  e[1], e[2], e[3], e[4], e[5], e[6], e[7], e[8], e[9] = key, dist, prio, want, band, near, demote, sortKey or dist, nearSolid or false
  return e
end

local function sortDrops(a, b)
  if a.solid ~= b.solid then return a.solid end
  return tostring(a.key) < tostring(b.key)
end

local function sortOrder(a, b)
  if a[3] ~= b[3] then return a[3] < b[3] end
  return a[8] < b[8]
end

-- The distance from a point to a tile's 256 m square.
local function boxDist(p, t)
  local dx = math.max(math.abs(p.x - t.cx) - 128, 0)
  local dy = math.max(math.abs(p.y - t.cy) - 128, 0)
  return math.sqrt(dx * dx + dy * dy)
end

-- Every calm frame: tiles of other anchors and dropped ones emptied, the others filled / thinned to their
-- distance's rank within a governed micro-budget (M.PER_FRAME = 20 items/frame, < 1.0 ms wall-clock cap).
-- With view (core.viewAway: the camera away from the car), a tile's shown rank and group caps follow the nearer of
-- the camera and the car while its solidity (near) stays around the car.
local frameN = 0
function M.update(anchorId, player, view, gov)
  if type(view) == 'table' and (view.try ~= nil or view.charge ~= nil or view.chargeEngine ~= nil or view.take ~= nil or view.tokens ~= nil or view.frame ~= nil or view.burst ~= nil or view.urgent ~= nil) and gov == nil then
    gov, view = view, nil
  end
  M.lastForestsTouched = 0
  if not next(tiles) and #orphanDrops == 0 then
    workPending = false
    return
  end
  if not setup() then return end

  local governed = (gov ~= nil)
  local budget, maxRows, maxDemoteRows, maxWallMs, maxForests
  if governed then
    -- Yield on heavy frames, terrain commit quiet windows, or frames where roads/farfield already ran a unit
    if gov.blocked or gov.heavyPending or (gov.isHeavyFrame and gov:isHeavyFrame())
        or ((gov.frameUnits or 0) > 0) then
      return
    end
    if gov.est and not gov.est[M.GOV_KIND] then
      gov.est[M.GOV_KIND] = M.GOV_PRIOR_MS
    end
    if gov.try and not gov:try(M.GOV_KIND) then
      return
    end
    budget = M.PER_FRAME
    maxRows = M.ROWS_PER_FRAME
    maxDemoteRows = M.MICRO_DEMOTE_ROWS_PER_FRAME
    maxWallMs = M.MAX_WALL_MS
    maxForests = (gov.burst and not gov.urgent and 1) or M.MAX_FORESTS_PER_FRAME or 2
  else
    -- Offline unit test mode (mod/tests/test_trees.lua calls trees.update(aid, player) with gov == nil)
    frameN = frameN + 1
    local n = math.max(1, M.EVERY_N or 1)
    if frameN % n ~= 0 then return end
    budget = math.max(M.PER_FRAME, M.LEGACY_PER_FRAME) * n
    maxRows = math.max(M.ROWS_PER_FRAME, M.LEGACY_ROWS_PER_FRAME)
    maxDemoteRows = 1e9
    maxWallMs = 1e9
    maxForests = M.MAX_FORESTS_UNW or nil
  end

  local startBudget = budget
  local tStart = treeClock()
  local function overWallBudget()
    return governed and ((treeClock() - tStart) >= maxWallMs)
  end

  resetTouched()
  releaseGhosts(anchorId, player)

  local solidAdded = false
  local rowsScanned = 0
  local hasMoreWork = false

  local function finish()
    local nowMs = treeClock()
    if touchedForestCount > 0 then
      notifyShadowBurst()
    end
    if solidAdded then
      collisionPending = true
      lastSolidAddAt = nowMs
      if not firstSolidAddAt then firstSolidAddAt = nowMs end
    end
    if collisionPending then
      local nearStillFilling = false
      for _, t in pairs(tiles) do
        if t.anchor == anchorId and not t.drop and t.near and t.pending then
          nearStillFilling = true
          break
        end
      end
      local shouldFlush
      if not governed then
        shouldFlush = not nearStillFilling
      else
        local sinceLastFlush = nowMs - lastCollisionFlushAt
        local sinceLastAdd = nowMs - lastSolidAddAt
        local sinceFirstAdd = firstSolidAddAt and (nowMs - firstSolidAddAt) or 0
        shouldFlush = (sinceLastFlush >= M.COLLISION_MIN_INTERVAL_S * 1000)
          and ((not nearStillFilling and sinceLastAdd >= M.COLLISION_DEBOUNCE_S * 1000)
               or (sinceFirstAdd >= M.COLLISION_MAX_WAIT_S * 1000))
      end
      if shouldFlush then
        collisionPending = false
        firstSolidAddAt = nil
        lastCollisionFlushAt = nowMs
        local ok, roads = pcall(require, 'beamngEarth/roads')
        if ok and roads.requestCollision then roads.requestCollision() end
      end
    end
    if governed and gov.charge and (budget < startBudget or rowsScanned > 0) then
      local elapsed = math.max(0.01, nowMs - tStart)
      gov:charge(M.GOV_KIND, elapsed)
    end
  end

  local function drainDrops(limit)
    while limit > 0 and budget > 0 and not overWallBudget() do
      local nDrops = 0
      for i = #orphanDrops, 1, -1 do
        local od = orphanDrops[i]
        if next(od.made) == nil then
          releaseForest(od)
          table.remove(orphanDrops, i)
        else
          nDrops = nDrops + 1
          dropsScratch[nDrops] = acquireDropEntry(nDrops, od.key or ('orphan_' .. i), od, next(od.solid) ~= nil, i)
        end
      end
      for key, t in pairs(tiles) do
        if (t.anchor ~= anchorId and not t.ghost) or t.drop then
          if next(t.made) == nil then
            releaseForest(t)
            tiles[key] = nil
          else
            nDrops = nDrops + 1
            dropsScratch[nDrops] = acquireDropEntry(nDrops, key, t, next(t.solid) ~= nil, false)
          end
        end
      end
      for i = nDrops + 1, #dropsScratch do dropsScratch[i] = nil end
      if nDrops == 0 then return end
      table.sort(dropsScratch, sortDrops)
      -- Batch all rows of one dropped Forest together (up to `limit`) instead of slicing across all nDrops Forests
      local perTile = governed and limit or math.max(math.ceil(limit / nDrops), 64)
      local progressed = false
      for idx = 1, nDrops do
        local d = dropsScratch[idx]
        local key, t = d.key, d.t
        if not allowForest(t, maxForests, false) then
          hasMoreWork = true
        else
          local f = t.forestId and scenetree.findObjectById(t.forestId)
          local fd = f and f:getData()
          local tileLimit = perTile
          for i, it in pairs(t.made) do
            if tileLimit <= 0 or limit <= 0 or budget <= 0 or overWallBudget() then
              if overWallBudget() then budget = 0 end
              break
            end
            local sh = t.madeShape[i]
            if not sh or allow(sh) then
              noteForestTouched(t)
              if fd then removeItem(fd, it, sh) end
              t.made[i], t.solid[i], t.madeShape[i] = nil, nil, nil
              tileLimit = tileLimit - 1
              limit = limit - 1
              budget = budget - 1
              progressed = true
            end
          end
          if next(t.made) == nil then
            releaseForest(t)
            if not d.isOrphan then
              tiles[key] = nil
            end
          else
            hasMoreWork = true
          end
        end
        if limit <= 0 or budget <= 0 or overWallBudget() then
          hasMoreWork = true
          break
        end
      end
      for i = #orphanDrops, 1, -1 do
        if next(orphanDrops[i].made) == nil then
          table.remove(orphanDrops, i)
        end
      end
      if not progressed then
        hasMoreWork = true
        break
      end
    end
  end

  -- Reserve half the budget when player tiles may need updates; any unused budget drains drops after.
  drainDrops(player and math.ceil(budget * 0.5) or budget)
  if not player then
    workPending = hasMoreWork or (#orphanDrops > 0)
    finish()
    return
  end

  -- Velocity vector for road-corridor / forward-cone priority at high speed
  local vx, vy = player.vx or 0, player.vy or 0
  local spd2 = vx * vx + vy * vy
  local hasHeading = spd2 > 4.0
  local ux, uy, corridorLen = 0, 0, 250
  if hasHeading then
    local spd = math.sqrt(spd2)
    ux, uy = vx / spd, vy / spd
    corridorLen = math.min(450, math.max(180, spd * 6.0))
  end

  -- Tiles needing work: solid demotions (> SOLID_M + SOLID_HYST_M) first so colliding trees never accumulate behind
  -- the car, then road-corridor & near tiles (<= RINGS[1]), then receding tiles being thinned (shedding bushes/trees
  -- keeps pop[sh] low), then distant fills; nearest first within each tier.
  local nOrder = 0
  local nearRing = M.RINGS[1] and M.RINGS[1][1] or 300
  for key, t in pairs(tiles) do
    if t.anchor == anchorId and not t.drop then     -- not still being emptied
      local dCar = boxDist(player, t)
      local dist = view and math.min(boxDist(view, t), dCar) or dCar
      local want = tileThreshold(dist, t)
      local band = groupBand(t, dist)
      local nearSolid = dCar <= M.SOLID_M
      local near = nearSolid or (t.near and dCar <= M.SOLID_M + M.SOLID_HYST_M) or false
      local demote = (t.near or next(t.solid) ~= nil) and not near
      -- Note: Forest has no C++ isRenderShadowEnabled field (shadow culling is handled by TSForestItemData.shadowNullDetailSize)
      if want ~= t.shown or band ~= t.band or near ~= t.near or demote or t.pending then
        local prio = 3
        local sortKey = dist
        if demote then
          prio = 0
        elseif dist <= nearRing then
          prio = 1
          if hasHeading then
            local rx, ry = t.cx - player.x, t.cy - player.y
            local sPar = rx * ux + ry * uy
            local dPerp = math.abs(rx * uy - ry * ux)
            if sPar >= -60 and sPar <= corridorLen and dPerp <= 160 then
              sortKey = dPerp * 0.5 + math.max(0, sPar) * 0.25
            else
              sortKey = dist + 200
            end
          end
        elseif want < (t.shown or 0) or (t.band and band < t.band) or t.thinning then
          prio = 2
        elseif hasHeading then
          local rx, ry = t.cx - player.x, t.cy - player.y
          if (rx * ux + ry * uy) < -60 then
            prio = 4
          end
        end
        nOrder = nOrder + 1
        orderScratch[nOrder] = acquireOrderEntry(nOrder, key, dist, prio, want, band, near, demote, sortKey, nearSolid)
      end
    end
  end
  for i = nOrder + 1, #orderScratch do orderScratch[i] = nil end
  if nOrder > 1 then
    table.sort(orderScratch, sortOrder)
  end

  for idx = 1, nOrder do
    local o = orderScratch[idx]
    local t = tiles[o[1]]
    local dist, want, band, near, demote, nearSolid = o[2], o[4], o[5], o[6], o[7], o[9]
    if t and (want ~= t.shown or band ~= t.band or near ~= t.near or demote or t.pending) then
      local rowLimit = demote and maxDemoteRows or maxRows
      if rowsScanned > rowLimit or budget <= 0 or overWallBudget() then
        hasMoreWork = true
        break
      end
      if not allowForest(t, maxForests, nearSolid) then
        hasMoreWork = true
      else
        local sameTarget = (want == t.shown and band == t.band and near == t.near)
        local wasThinning = want < (t.shown or 0) or (t.band and band < t.band) or (sameTarget and t.thinning)
        local startRow = (sameTarget and t.pending and t.nextRow) or 1
        local nextPending = nil
        t.pending = false
        local fd = nil                          -- the tile's Forest data, made when it first needs an item
        local bin = t.bin
        local nTileRows = bin and math.floor(#bin / VBIN_STRIDE) or (t.rows and #t.rows or 0)
        for i = startRow, nTileRows do
          if governed and (rowsScanned >= rowLimit or (rowsScanned % 32 == 0 and overWallBudget())) then
            if not nextPending then nextPending = i end
            t.pending = true
            if overWallBudget() then budget = 0 end
            break
          end
          rowsScanned = rowsScanned + 1
          local row = bin and decodeBinRow(bin, i) or t.rows[i]
          local r = row[8] or 0
          local g = (t.groups or GROUPS)[(row[1] or 0) + 1]
          local cap = g and M.GROUP_MAX_M[g]
          if cap and dist > cap then r = 2 end     -- this group is not shown this far out
          -- an item something else stands in for (navgraph.lua's switchable signal heads)
          if g and M.suppress and M.suppress(g, row[2], row[3]) then r = 2 end
          local solid = near and not M.NO_COLLIDE[g or '']
          if r < want and (not t.made[i] or (t.solid[i] == true) ~= solid) then
            local sh, sc = shapeOf(t, i, row)
            if budget <= 0 or overWallBudget() then
              if not nextPending then nextPending = i end
              t.pending = true
              budget = 0
              break
            end
            if allow(sh) and (not t.made[i] or allow(t.madeShape[i] or sh)) then
              fd = tileForestData(t, fd)
              if t.made[i] then removeItem(fd, t.made[i], t.madeShape[i]) end  -- solid <-> not
              t.made[i] = makeItem(fd, row, sh, sc, solid)
              t.solid[i] = (t.made[i] and solid) or nil      -- true or nil
              t.madeShape[i] = t.made[i] and sh or nil
              if solid and t.made[i] then solidAdded = true end
              budget = budget - 1
            else
              if not nextPending then nextPending = i end
              t.pending = true                               -- another frame (this one's shapes are full)
            end
          elseif r >= want and t.made[i] then
            if budget <= 0 or overWallBudget() then
              if not nextPending then nextPending = i end
              t.pending = true
              budget = 0
              break
            end
            if allow(t.madeShape[i] or '') then
              fd = tileForestData(t, fd)
              removeItem(fd, t.made[i], t.madeShape[i])
              t.made[i], t.solid[i], t.madeShape[i] = nil, nil, nil
              budget = budget - 1
            else
              if not nextPending then nextPending = i end
              t.pending = true
            end
          end
        end
        if not near and next(t.solid) ~= nil and not t.pending then
          t.pending = true
          nextPending = 1
        end
        if t.pending then hasMoreWork = true end
        if next(t.made) == nil then
          releaseForest(t)
        end
        t.nextRow = t.pending and nextPending or nil
        t.thinning = (t.pending and wasThinning) or nil
        t.shown = want
        t.band = band
        t.near = near
      end
    end
  end
  if budget > 0 and not overWallBudget() then drainDrops(budget) end
  workPending = hasMoreWork or (#orphanDrops > 0)
  if not workPending then
    lastSettledX, lastSettledY = player.x, player.y
  end
  finish()
end

-- Disconnect, a new server session, mod unload: every item removed.
function M.clear()
  local hadSolid = false
  for _, od in ipairs(orphanDrops) do
    if next(od.solid) ~= nil then hadSolid = true end
    local f = od.forestId and scenetree.findObjectById(od.forestId)
    local fd = f and f:getData()
    for i, it in pairs(od.made) do if fd then removeItem(fd, it, od.madeShape[i]) end end
    releaseForest(od)
  end
  orphanDrops = {}
  for _, t in pairs(tiles) do
    if next(t.solid) ~= nil then hadSolid = true end
    local f = t.forestId and scenetree.findObjectById(t.forestId)
    local fd = f and f:getData()
    for i, it in pairs(t.made) do if fd then removeItem(fd, it, t.madeShape[i]) end end
    releaseForest(t)
  end
  tiles = {}
  pop = {}
  frameN = 0
  workPending = false
  collisionPending, firstSolidAddAt = false, nil
  if hadSolid then
    local ok, roads = pcall(require, 'beamngEarth/roads')
    if ok and roads.requestCollision then roads.requestCollision() end
  end
end

-- Level end: the level deletes its objects itself.
function M.reset()
  tiles, types, ready, pool, forestHidden, forestCount, pop = {}, {}, false, {}, {}, 0, {}
  orphanDrops = {}
  frameN = 0
  workPending = false
  collisionPending, firstSolidAddAt = false, nil
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
