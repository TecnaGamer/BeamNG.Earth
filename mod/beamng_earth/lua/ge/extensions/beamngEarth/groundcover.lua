-- Grass and low shrubs near the car (docs/ROADMAP.md v1.5; server --surface on). GroundCover renders on
-- the render-disabled physics block and honours its layer map (docs/research/2026-10-11-engine-probes.md
-- §4), so binding stock GroundCover definitions to the surface layers (server/surface.py: grassland /
-- forest floor -> bngEarthGroundGrass, shrubland -> bngEarthGroundDusty) puts grass where the land
-- cover says grass, nothing on asphalt, bare ground, rock, snow or mud. Golf greens, fairways,
-- rough and bunkers are their own terrain materials (server/surface.py) and are not listed here,
-- so the 3D grass stays off them.
--
-- No flicker at block moves (in-game and binary analysis 2026-10-11, docs/ARCHITECTURE.md "Ground
-- surfaces"): every import / move of the block makes the engine drop every GroundCover cell on it
-- (no way around that from Lua), and the cells come back under a per-frame generation budget.
-- core calls M.afterBlockChange() in the same tick as each block change: flushGroundCoverGrids()
-- lifts the budget for that frame, so all the cells are regenerated before the frame is drawn. That
-- needs CPU placement (useNewPlacementGpu 0): GPU-placed cells only draw one frame later (a blank
-- frame), and GPU placement reads a layer copy the hidden block never refreshes. The price is one
-- slower frame per block move, which grows with the element count (the shares below). With the
-- optional tools/gcpatch preload library (this exact game build) the engine keeps the grass across
-- block moves and that frame goes away; where the world really moves (teleport, rebase) core calls
-- M.rebuild(), which works with or without it.
--
-- The definitions are stock ones, read at runtime from official levels' own files (nothing is copied
-- into the mod; Polish Roads is not used, CREDITS.md): their materials are loaded first (a shape whose
-- material is missing when it is first drawn stays "no material"), then each GroundCover is recreated
-- with its plant types' layer pointed at ours and its element count scaled by `share`.
--
-- By climate and season (2026-11-19, the user: "a complete vegetation overhaul, not only trees and bushes"; every
-- stock grass, flower and low plant looked at in game first): the vegetation tiles carry their Koppen class
-- (server vegetation._ambience, biome[3]); ambience.lua votes the tiles round the listener and hands the class here
-- (M.setClimate). Each climate has its own PROFILE: the temperate meadows' green grass with seeded heads, buttercups,
-- daisies and poppies in their months, nettles and spring flowers on the forest floor; the Mediterranean's green
-- winter and flowered spring, its dry summer (dry grasses, Utah's dry herb) and Italy's garrigue on the scrub; the
-- steppes' and deserts' dry tussocks and Johnson Valley's / Utah's dry plants (their spring's white flowers), Small
-- Island's yuccas; the tropics' Jungle Rock Island undergrowth and tall grass; the tundra's short dry grass; cattails
-- and reeds on herbaceous wetland (server/surface.py WET), beach grass on vegetated beach sand. Month windows are the
-- northern hemisphere's (the south half a year later); the server's fixed season (seasonlook mode) picks its month.
-- Performance (the user: "it would be good if performance did not suffer, maybe even improved compared to dev"): a
-- GroundCover's work at a block move is its element count whether or not its layer is there (each element a try at
-- a random point), so every profile spends at most the generic one's elements (M.budget; test_groundcover), and an
-- object is only made once a block lists one of its layers (M.ensure: no cattails' tries where no block has
-- wetland). Console: beamngEarth_groundcover.setEnabled(bool), .status(), .setClimate(k), .setMonth(m), .setDay(d)

local M = {}

-- tools/gcpatch acts only while this file exists (its thread looks every 250 ms): created when our level starts,
-- removed when it ends, so other levels keep the stock engine (the user, 2026-10-20: the mod effectively off there).
M.PATCH_FLAG = '/temp/beamngearth_gcpatch_on'   -- lower case: the game's file system writes new names so
function M.enginePatch(on)
  local ok, err = pcall(function()
    if on then
      writeFile(M.PATCH_FLAG, '1')
    elseif FS and FS:fileExists(M.PATCH_FLAG) then
      FS:removeFile(M.PATCH_FLAG)
    end
  end)
  if not ok then log('W', 'beamngEarth', 'grass patch flag: ' .. tostring(err)) end
end

-- The stock levels whose GroundCover definitions are used: their materials files (loaded before the first object of
-- that source; Utah's and Johnson Valley's 3D plants' materials are in the same files) and the items files holding
-- the definitions (one JSON object per line). Only the sources of the profile in use are read.
local HIROCHI, WCUSA = '/levels/hirochi_raceway/', '/levels/west_coast_usa/'
local IT, ECA, CL, IND = '/levels/italy/', '/levels/east_coast_usa/', '/levels/Cliff/', '/levels/Industrial/'
local ATT, UT, JV, JRI = '/levels/automation_test_track/', '/levels/Utah/', '/levels/johnson_valley/',
  '/levels/jungle_rock_island/'
local SI = '/levels/small_island/'
local ITV, INDV = IT .. 'main/MissionGroup/Level_objects/vegetation/', IND .. 'main/MissionGroup/Level_objects/vegetation/'
local ECAV = ECA .. 'main/MissionGroup/Level_object/vegetation/'
M.SOURCES = {
  hirochi = { materials = HIROCHI .. 'art/shapes/groundcover/main.materials.json',
              items = HIROCHI .. 'main/MissionGroup/vegetation/terrain_grass/items.level.json' },
  wcusa = { materials = WCUSA .. 'art/groundcover/main.materials.json',
            items = WCUSA .. 'main/MissionGroup/vegetation/items.level.json' },
  italy = { materials = IT .. 'art/shapes/groundcover/main.materials.json',
            items = { ITV .. 'terrain_Grass/items.level.json', ITV .. 'terrain_Grass3/items.level.json',
                      ITV .. 'terrain_forest_floor/items.level.json' } },
  -- (East Coast USA's daisies and spring flowers are Groundcover_spring: Hirochi Raceway's materials file has it)
  eca = { materials = { ECA .. 'art/shapes/groundcover/3d_grass_test/main.materials.json',
                        HIROCHI .. 'art/shapes/groundcover/main.materials.json' },
          items = { ECAV .. 'terrain_grass/items.level.json', ECAV .. 'terrain_forest_floor/items.level.json' } },
  cliff = { materials = CL .. 'art/shapes/groundcover/main.materials.json',
            items = CL .. 'main/MissionGroup/Level_objects/vegetation/items.level.json' },
  industrial = { materials = IND .. 'art/shapes/groundcover/main.materials.json',
                 items = { INDV .. 'terrain_forest/items.level.json', INDV .. 'terrain_dirt_loose/items.level.json',
                           INDV .. 'terrain_dirt_grass/items.level.json', INDV .. 'terrain_Grass/items.level.json' } },
  att = { materials = ATT .. 'art/shapes/groundcover/main.materials.json',
          items = ATT .. 'main/MissionGroup/Level_object/vegetation/items.level.json' },
  utah = { materials = UT .. 'art/shapes/groundcover/main.materials.json',
           items = UT .. 'main/MissionGroup/Level_objects/vegetation/items.level.json' },
  jv = { materials = JV .. 'art/shapes/groundcover/main.materials.json',
         items = JV .. 'main/MissionGroup/level_object/vegetation/items.level.json' },
  jri = { materials = { JRI .. 'art/shapes/groundcover/main.materials.json',
                        JRI .. 'art/shapes/groundcover/grass/main.materials.json' },
          items = JRI .. 'main/MissionGroup/Level_objects/vegetation/items.level.json' },
  island = { materials = SI .. 'art/shapes/groundcover/main.materials.json',
             items = SI .. 'main/MissionGroup/Level_objects/vegetation/items.level.json' },
}
local function list(v) return type(v) == 'table' and v or { v } end

-- our layers (server/surface.py MATERIALS)
local G, LAWN, DUSTY, FOREST = 'bngEarthGroundGrass', 'bngEarthGroundLawn', 'bngEarthGroundDusty', 'bngEarthGroundForest'
local DRY, WET, SAND = 'bngEarthGroundDryGrass', 'bngEarthGroundWet', 'bngEarthGroundSand'
M.LAYERS = { grass = G, lawn = LAWN, dusty = DUSTY, forest = FOREST, dry = DRY, wet = WET, sand = SAND }

-- stock GroundCover (source, src name) -> our layer, element share; `name` (default src) names our
-- object, for a stock definition used on two layers; `fields` override stock fields. The shares keep
-- the block-move rebuild near +15 ms (in-game at Serengeti grassland 2026-10-11: stock-ish shares
-- +30 ms; the dense near grass costs most, the long grass is kept close, radius 70 m instead of 120).
-- (The generic profile: where no climate is known yet, the vegetation off or a tile from before the climate.)
local LONG_NEAR = { radius = 70, dissolveRadius = 45 }
M.DEFS = {
  { source = 'hirochi', src = 'small_grass_close', layer = G, share = 0.25 },
  { source = 'hirochi', src = 'long_grass_distant', layer = G, share = 0.17, fields = LONG_NEAR },
  { source = 'wcusa', src = 'grass_dry_1', layer = G, share = 0.05 },
  -- mown town yards: a little short grass, nothing long (server/surface.py LAWN)
  { source = 'hirochi', src = 'small_grass_close', name = 'small_grass_close_lawn', layer = LAWN, share = 0.1 },
  { source = 'wcusa', src = 'shrubs_near', layer = DUSTY, share = 0.15 },
  -- sparse grass between the shrubs (user 2026-10-11: a hard line between grassland and shrubland)
  { source = 'hirochi', src = 'small_grass_close', name = 'small_grass_close_scrub', layer = DUSTY, share = 0.08 },
  { source = 'hirochi', src = 'long_grass_distant', name = 'long_grass_distant_scrub', layer = DUSTY, share = 0.05,
    fields = LONG_NEAR },
  -- (2026-11-16, server/surface.py FOREST / DRYGRASS: tree cover had the grass layer and its full carpet) forest floor:
  -- sparse short grass between the trunks; arid grassland: mostly West Coast USA's dry grass. Crop soil and sand: none.
  { source = 'hirochi', src = 'small_grass_close', name = 'small_grass_close_forest', layer = FOREST, share = 0.08 },
  { source = 'wcusa', src = 'grass_dry_1', name = 'grass_dry_1_dry', layer = DRY, share = 0.2 },
  { source = 'hirochi', src = 'small_grass_close', name = 'small_grass_close_dry', layer = DRY, share = 0.06 },
}

-- Month windows (the northern hemisphere's months, 1-12, from the first day of month a to the last of month b; the
-- south's half a year later). An entry fades in and out over M.FADE_DAYS either side of its window's edges (the user,
-- 2026-11-19: "smoother seasons"): its share times its weight on the day (M.weight), two windows that meet cross-fade
-- (the Mediterranean's green grass into its dry grass over four weeks of late May and June).
local MONTH_START = { 1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335 }
local function span(a, b)
  return { from = MONTH_START[a], to = (MONTH_START[b % 12 + 1] - 2) % 365 + 1 }
end
M.FADE_DAYS = 14
M.MIN_WEIGHT = 0.08     -- an entry fainter than this is left out (no object for a handful of tufts)
M.DAY_STEP = 7          -- the day the objects are made for moves in weeks (each move makes them again)

-- One profile entry: `id` names our object (unique in its profile), the stock definition (source, src), the layer its
-- active plant types go to, its element share; opt.types: plant types (0-based, the stock definition's order)
-- pointed at another layer with their own probability (p 0: off), e.g. one grass object over grassland, forest floor
-- and scrub (the same element tries, fewer objects); opt.months: its months (a span), opt.fields: stock fields set.
local function E(id, source, src, layer, share, opt)
  opt = opt or {}
  return { id = id, source = source, src = src, layer = layer, share = share, types = opt.types, months = opt.months,
           fields = opt.fields }
end
-- the stock short green grass (Hirochi Raceway; its types 0, 1 the grass, 2, 3 and 5 smaller tufts of it) on
-- grassland with shares of it on the forest floor, the scrub and arid grassland (p: those types' probabilities against
-- the grassland's 0.9)
local function smallGrass(share, f, d, y, opt)
  opt = opt or {}
  opt.types = { [2] = { layer = FOREST, p = f or 0 }, [3] = { layer = DUSTY, p = d or 0 }, [5] = { layer = DRY, p = y or 0 } }
  return E(opt.id or 'small', 'hirochi', 'small_grass_close', G, share, opt)
end
local function longGrass(share, opt)
  opt = opt or {}
  opt.fields = opt.fields or LONG_NEAR
  return E(opt.id or 'long', 'hirochi', 'long_grass_distant', G, share, opt)
end
-- West Coast USA's dry grass: its types 0, 1 grassland, 4 arid grassland, 5 forest floor, 6 scrub (stock
-- probabilities 1.2 : 1 : 0.2 : 0.1)
local function dryGrass(share, opt)
  opt = opt or {}
  opt.types = opt.types or { [4] = { layer = DRY, p = 1 }, [5] = { layer = FOREST, p = 0.2 }, [6] = { layer = DUSTY, p = 0.1 } }
  return E(opt.id or 'dry', 'wcusa', 'grass_dry_1', G, share, opt)
end
-- wetland and beach, every climate (wetland: Industrial's cattails, East Coast USA's tall reed grass; beaches: Jungle
-- Rock Island's beach grass, a few tufts on the vegetated sand)
local function wetland(cattails, reeds, beach)
  local out = {
    E('cattails', 'industrial', 'wet_weed_01', WET, cattails),
    E('reeds', 'eca', 'long_grass_clump_close_03', WET, reeds),
  }
  if beach and beach > 0 then out[#out + 1] = E('beach', 'jri', 'Beach_grass', SAND, beach) end
  return out
end
-- (the dry grass in the deserts: every type on the arid grassland, a little on the scrub)
local DESERT_DRY = { [0] = { layer = DRY, p = 0.2 }, [1] = { layer = DRY, p = 1 }, [4] = { layer = DRY, p = 1 },
  [5] = { layer = DRY, p = 0.2 }, [6] = { layer = DUSTY, p = 0.3 } }
local function join(...)
  local out = {}
  for _, t in ipairs({ ... }) do for _, x in ipairs(t) do out[#out + 1] = x end end
  return out
end

-- The temperate and continental meadows (w: their flowers' months)
local function meadow(w)
  return join({
    smallGrass(0.32, 0.27, 0.27, 0.1),
    longGrass(0.14, { months = w.green }),
    -- (autumn and winter: a share of the stock dry grass in the green)
    E('autumn', 'att', 'grass_dry_2', G, 0.1, { months = w.brown }),
    -- seeded heads in the hay meadows (East Coast USA's 3D-test grass: 1920 stock elements, sparse)
    E('heads', 'eca', 'long_grass_clump_close', G, 6, { months = w.heads }),
    E('buttercups', 'cliff', 'Buttercup1', G, 0.05, { months = w.buttercups }),
    -- Italy's flower mix: poppies, daisies, yellow, blue and violet flowers
    E('flowers', 'italy', 'flowers', G, 0.2, { months = w.flowers }),
    -- daisies on meadows and on the lawns (its type 4 on the lawns), in a 60 m disc (stock 120: 4x the density)
    E('daisies', 'eca', 'Daisies', G, 0.1, { months = w.daisies, types = { [4] = { layer = LAWN, p = 0.5 } },
      fields = { radius = 60, dissolveRadius = 40 } }),
    -- town lawns: West Coast USA's mown grass
    E('lawn', 'wcusa', 'mown_grass_close', LAWN, 0.7),
    -- nettles on the forest floor and the scrub (Industrial's: its types 2 forest, 3 scrub)
    E('nettles', 'industrial', 'Nettles_01', FOREST, 0.06, { months = w.nettles,
      types = { [3] = { layer = DUSTY, p = 0.1 } } }),
    -- the spring flowers of the forest floor (East Coast USA's violet flowers)
    E('woodflowers', 'eca', 'Purple_flowers', FOREST, 0.03, { months = w.woodflowers }),
    E('shrubs', 'wcusa', 'shrubs_near', DUSTY, 0.15),
    E('weeds', 'att', 'weed1', DUSTY, 0.03, { months = w.nettles }),
  }, wetland(0.05, 0.15, 0.01))
end

M.PROFILES = {
  generic = M.DEFS,
  -- Cfb, Cfc, Cwb, Cwc: oceanic Europe, the highlands
  temperate = meadow({ green = span(3, 11), brown = span(12, 2), heads = span(5, 9), buttercups = span(4, 6),
    flowers = span(6, 8), daisies = span(4, 9), nettles = span(5, 10), woodflowers = span(4, 5) }),
  -- Dfa, Dfb, Dsa, Dsb, Dwa, Dwb: a shorter season
  continental = meadow({ green = span(4, 10), brown = span(11, 3), heads = span(6, 8), buttercups = span(5, 6),
    flowers = span(6, 8), daisies = span(5, 8), nettles = span(5, 9), woodflowers = span(5, 5) }),
  -- Cfa, Cwa: the humid subtropics (the southeast USA, the Po valley, south China): lusher, tall roadside grass
  subtropical = join({
    smallGrass(0.3, 0.27, 0.27, 0.1),
    longGrass(0.17),
    E('tallgrass', 'eca', 'long_grass_clump_close_03', G, 0.05, { months = span(6, 10) }),
    E('weedsG', 'att', 'weed1', G, 0.02, { months = span(5, 10), types = { [3] = { layer = DUSTY, p = 2 } } }),
    E('flowers', 'italy', 'flowers', G, 0.2, { months = span(4, 6) }),
    E('daisies', 'eca', 'Daisies', G, 0.1, { months = span(3, 6), types = { [4] = { layer = LAWN, p = 0.5 } },
      fields = { radius = 60, dissolveRadius = 40 } }),
    E('lawn', 'wcusa', 'mown_grass_close', LAWN, 0.7),
    E('woodflowers', 'eca', 'Purple_flowers', FOREST, 0.03, { months = span(3, 5) }),
    E('nettles', 'industrial', 'Nettles_01', FOREST, 0.05, { months = span(4, 10) }),
    E('shrubs', 'wcusa', 'shrubs_near', DUSTY, 0.15),
  }, wetland(0.06, 0.15, 0.01)),
  -- Csa, Csb, Csc: green from the autumn rains to May, flowered in spring, dry from June to September; the scrub
  -- Italy's garrigue all year
  mediterranean = join({
    smallGrass(0.3, 0.27, 0.27, 0.1, { months = span(10, 5) }),
    smallGrass(0.05, 0, 0, 0, { id = 'smallDry', months = span(6, 9) }),
    longGrass(0.1, { months = span(11, 5) }),
    E('flowers', 'italy', 'flowers', G, 0.3, { months = span(3, 5) }),
    E('buttercups', 'cliff', 'Buttercup1', G, 0.04, { months = span(3, 4) }),
    E('daisies', 'eca', 'Daisies', G, 0.1, { months = span(2, 5), types = { [4] = { layer = LAWN, p = 0.5 } },
      fields = { radius = 60, dissolveRadius = 40 } }),
    dryGrass(0.3, { months = span(6, 10) }),
    dryGrass(0.1, { id = 'dryWet', months = span(11, 5) }),
    E('dry2', 'att', 'grass_dry_2', G, 0.15, { months = span(6, 10) }),
    -- Utah's dry herb (Italy's own forest floor uses it): its type 1 grassland, 0 forest floor, 2 scrub
    E('herb', 'italy', 'shrub_grass_02', G, 1.0, { months = span(5, 11),
      types = { [0] = { layer = FOREST, p = 0.5 }, [2] = { layer = DUSTY, p = 0.5 } } }),
    E('garrigue', 'italy', 'shrubs', DUSTY, 0.04),
    -- irrigated town lawns
    E('lawn', 'wcusa', 'mown_grass_close', LAWN, 0.6),
  }, wetland(0.05, 0.15, 0.01)),
  -- BSh, BSk: dry tussocks on the arid grassland, spring's green and flowers
  steppe = join({
    dryGrass(0.2, { types = { [0] = { layer = DRY, p = 0.2 }, [1] = { layer = DRY, p = 1 }, [4] = { layer = DRY, p = 1 },
      [5] = { layer = FOREST, p = 0.2 }, [6] = { layer = DUSTY, p = 0.2 } } }),
    E('tussocks', 'utah', 'Dry_grass_clump', DRY, 0.05),
    E('dry2', 'att', 'grass_dry_2', DRY, 0.1),
    -- Johnson Valley's dry grass (Industrial's weeds): its types 0-2 arid grassland, 3 scrub
    E('weeds', 'industrial', 'weed_03', DRY, 0.05, { types = { [3] = { layer = DUSTY, p = 1 } } }),
    -- spring's green on the arid grassland (the short grass's types 0, 1 there; 3 the scrub)
    E('green', 'hirochi', 'small_grass_close', DRY, 0.08, { months = span(3, 6), types = { [3] = { layer = DUSTY, p = 0.1 } } }),
    E('flowers', 'italy', 'flowers', DRY, 0.15, { months = span(4, 5) }),
    E('shrubs', 'wcusa', 'shrubs_near', DUSTY, 0.1),
    -- Utah's dry plants, grey bushes and white flowers (3D)
    E('plants', 'utah', 'plants', DUSTY, 0.02),
    E('lawn', 'wcusa', 'mown_grass_dry', LAWN, 0.05),
  }, wetland(0.04, 0.12, 0.005)),
  -- BWh: sparse dry tussocks and Johnson Valley's dry bushes and branches, Small Island's yuccas
  desert_hot = join({
    E('tussocks', 'utah', 'Dry_grass_clump', DRY, 0.05),
    dryGrass(0.08, { types = DESERT_DRY }),
    E('bushes', 'jv', 'dirt_grass_dry', DUSTY, 0.02, { types = { [0] = { layer = DRY, p = 0.3 } },
      fields = { radius = 100, dissolveRadius = 70 } }),
    E('branches', 'jv', 'dirt_plant_dry', DUSTY, 0.03, { fields = { radius = 100, dissolveRadius = 70 } }),
    E('plants', 'utah', 'plants', DUSTY, 0.02),
    E('yucca', 'island', 'spanish_bayonet', DUSTY, 0.05),
    E('lawn', 'wcusa', 'mown_grass_dry', LAWN, 0.04),
  }, wetland(0.03, 0.1, 0)),
  -- BWk: the cold deserts (the Great Basin, Central Asia): Utah's and Johnson Valley's
  desert_cold = join({
    E('tussocks', 'utah', 'Dry_grass_clump', DRY, 0.07),
    dryGrass(0.1, { types = DESERT_DRY }),
    E('bushes', 'jv', 'dirt_grass_dry', DUSTY, 0.02, { types = { [0] = { layer = DRY, p = 0.3 } },
      fields = { radius = 100, dissolveRadius = 70 } }),
    E('plants', 'utah', 'plants', DUSTY, 0.03),
    E('lawn', 'wcusa', 'mown_grass_dry', LAWN, 0.04),
  }, wetland(0.03, 0.1, 0)),
  -- Af, Am: Jungle Rock Island's undergrowth on the forest floor, its tall grass on grassland and scrub
  tropical = join({
    -- (the forest floor's short grass as dev's: the benchmark 2026-11-19, Manaus's floor was bare without it)
    smallGrass(0.28, 0.27, 0.27, 0),
    longGrass(0.12),
    -- (Tall_grasses2: its types 1-3 active; 3 to the scrub)
    E('tall', 'jri', 'Tall_grasses2', G, 0.05, { types = { [3] = { layer = DUSTY, p = 0.6 } } }),
    E('undergrowth', 'jri', 'forest_plants', FOREST, 0.05),
    E('shrubs', 'wcusa', 'shrubs_near', DUSTY, 0.1),
    E('lawn', 'wcusa', 'mown_grass_close', LAWN, 0.7),
  }, wetland(0.05, 0.15, 0.03)),
  -- Aw: the savannas: green and tall in the rains (May to October north), straw-dry the rest
  savanna = join({
    smallGrass(0.18, 0.15, 0.1, 0.1, { months = span(5, 10) }),
    longGrass(0.17, { months = span(5, 10) }),
    E('tallgrass', 'eca', 'long_grass_clump_close_03', G, 0.2),
    E('tall', 'jri', 'Tall_grasses2', G, 0.03, { months = span(6, 10) }),
    dryGrass(0.25, { months = span(11, 4) }),
    E('dry2', 'att', 'grass_dry_2', G, 0.15, { months = span(11, 4) }),
    E('undergrowth', 'jri', 'forest_plants', FOREST, 0.02),
    E('shrubs', 'wcusa', 'shrubs_near', DUSTY, 0.12),
    E('bush', 'att', 'Bush', DUSTY, 0.015),
    E('lawn', 'wcusa', 'mown_grass_close', LAWN, 0.5),
  }, wetland(0.05, 0.15, 0.01)),
  -- Dfc, Dfd, Dsc, Dsd, Dwc, Dwd: the taiga's short summer
  boreal = join({
    smallGrass(0.22, 0.27, 0.27, 0),
    longGrass(0.12, { months = span(5, 9) }),
    E('autumn', 'att', 'grass_dry_2', G, 0.1, { months = span(10, 4) }),
    E('buttercups', 'cliff', 'Buttercup1', G, 0.04, { months = span(6, 7) }),
    E('daisies', 'eca', 'Daisies', G, 0.06, { months = span(6, 8), types = { [4] = { layer = LAWN, p = 0.5 } },
      fields = { radius = 60, dissolveRadius = 40 } }),
    E('woodflowers', 'eca', 'Purple_flowers', FOREST, 0.03, { months = span(6, 7) }),
    E('shrubs', 'wcusa', 'shrubs_near', DUSTY, 0.12),
    E('bush', 'att', 'Bush', DUSTY, 0.01),
    E('lawn', 'wcusa', 'mown_grass_close', LAWN, 0.6),
  }, wetland(0.04, 0.15, 0.01)),
  -- ET, EF: short dry grass, dwarf shrubs, a few buttercups in July
  tundra = join({
    E('short', 'industrial', 'small_grass_dry_close', G, 1.0),
    E('mown', 'wcusa', 'mown_grass_dry', G, 0.03),
    smallGrass(0.06, 0, 0.3, 0),
    E('buttercups', 'cliff', 'Buttercup1', G, 0.03, { months = span(7, 7) }),
    E('bush', 'att', 'Bush', DUSTY, 0.01),
    E('lawn', 'wcusa', 'mown_grass_close', LAWN, 0.4),
  }, wetland(0.02, 0.1, 0)),
}

-- Pure: the profile of a Koppen-Geiger class (Beck et al. 2023, server climate.py: 1 Af .. 30 EF), or, without one
-- (a tile from before the climate), of the ambience set and variant ({set, variant}: native ambience.rs); generic
-- for neither.
local KOPPEN = { 'tropical', 'tropical', 'savanna', 'desert_hot', 'desert_cold', 'steppe', 'steppe',
  'mediterranean', 'mediterranean', 'mediterranean', 'subtropical', 'temperate', 'temperate', 'subtropical',
  'temperate', 'temperate', 'continental', 'continental', 'boreal', 'boreal', 'continental', 'continental', 'boreal',
  'boreal', 'continental', 'continental', 'boreal', 'boreal', 'tundra', 'tundra' }
function M.profileOf(k, biome)
  k = tonumber(k)
  if k and KOPPEN[k] then return KOPPEN[k] end
  if type(biome) == 'table' then
    local set, var = tonumber(biome[1]), tonumber(biome[2]) or 0
    if set == 0 then return 'temperate' end
    if set == 1 then return 'mediterranean' end
    if set == 2 then return var == 1 and 'desert_hot' or 'desert_cold' end
    if set == 3 then return 'tropical' end
  end
  return 'generic'
end

-- Pure: the profile day (the northern hemisphere's day of the year, 1-365) of calendar day doy at latitude lat (the
-- south half a year later), or the server's fixed season (seasonlook mode 'spring' / 'summer' / 'fall' / 'winter':
-- its middle), in steps of DAY_STEP days.
local SEASON_DAY = { spring = 105, summer = 196, fall = 288, autumn = 288, winter = 15 }
function M.dayFor(doy, lat, mode)
  local d = SEASON_DAY[mode]
  if not d then
    d = tonumber(doy) or 172
    if tonumber(lat) and tonumber(lat) < 0 then d = d + 182 end
  end
  d = (math.floor(d) - 1) % 365
  return math.floor(d / M.DAY_STEP) * M.DAY_STEP + math.floor(M.DAY_STEP / 2) + 1
end
-- (the day of a month's middle: the console's setMonth, the tests)
function M.dayOfMonth(m) return MONTH_START[((tonumber(m) or 6) - 1) % 12 + 1] + 14 end

-- Pure: the weight 0..1 of a window {from, to} (nil: every day) on day d: 1 inside, 0 outside, a linear fade over
-- FADE_DAYS either side of each edge (0.5 on the edge: two windows that meet sum to 1 every day).
function M.weight(win, d)
  if not win then return 1 end
  local f = M.FADE_DAYS
  local len = (win.to - win.from) % 365 + 1
  local x = (d - win.from) % 365           -- days since the window opened (len and over: outside)
  if x < len then
    return math.min(1, (x + f) / (2 * f), (len - x + f) / (2 * f))
  end
  local after, before = x - len, 365 - x
  return math.max(0, (f - after) / (2 * f), (f - before) / (2 * f))
end

-- Pure: the entries of profile `name` on day d, each with its share times its weight that day (copies; those under
-- MIN_WEIGHT left out)
function M.plan(name, d)
  local out = {}
  for _, e in ipairs(M.PROFILES[name] or M.DEFS) do
    local w = M.weight(e.months, d)
    if w >= M.MIN_WEIGHT then
      if w < 1 then
        local c = {}
        for k, v in pairs(e) do c[k] = v end
        c.share, c.weight = e.share * w, w
        e = c
      end
      out[#out + 1] = e
    end
  end
  return out
end

-- Pure: the layers an entry's plant types use (its own and its typed ones with p > 0), as a set
function M.layersOf(e)
  local s = { [e.layer] = true }
  for _, t in pairs(e.types or {}) do
    if t.layer and (t.p or 1) > 0 then s[t.layer] = true end
  end
  return s
end

-- Pure: the elements (tries) of a plan, by stock maxElements (defs: source -> name -> definition) times the shares
-- (the work of a block move's regeneration, density and distance 1)
function M.budget(planned, defs)
  local n = 0
  for _, e in ipairs(planned) do
    local d = defs[e.source] and defs[e.source][e.src]
    if d then n = n + math.floor((tonumber(d.maxElements) or 0) * e.share + 0.5) end
  end
  return n
end

M.PREFIX = 'bngEarthGC_'
-- set on every object after the stock fields (see the header)
M.PLACEMENT = { { 'useNewPlacement', '1' }, { 'useNewPlacementGpu', '0' } }
M.enabled = true
-- element count factor on top of the shares (the settings page: low / normal / high); the block-move
-- rebuild frame grows with it where tools/gcpatch is not loaded
M.DENSITY = { low = 0.5, normal = 1.0, high = 2.0 }
M.density = 1.0
-- draw distance factor (the settings page's grassDistance, 2026-10-28): every definition's radius and dissolveRadius
-- times it, its element count times its square (the same density on the larger disc: the rebuild frame grows with it)
M.DISTANCE = { near = 0.7, normal = 1.0, far = 1.5 }
M.distance = 1.0
M.RADIUS_FIELDS = { radius = true, dissolveRadius = true }

local created = {}        -- object names we made
local made = {}           -- entry id -> true (made for the current profile and month)
local layered = false     -- a block with surface layers was loaded this level (objects can exist)
local defsCache = {}      -- source -> stock definitions by name (read once per level)
local loadedMaterials = {} -- materials file -> true
local lastError = nil
local seen = nil          -- layer names listed by the blocks since the last rebuild (nil: every layer)
local lastBlock = nil     -- the latest block's layer names
local climate = { k = nil, biome = nil }
local profile = 'generic'
local month = nil         -- the profile day (DAY_STEP steps) the objects were made for
local lat = nil
local keepSeen = false    -- (remake: the same place, the layers seen kept)
M.dayOverride = nil       -- (console: a calendar day of the year instead of today's)

local SKIP = { name = true, class = true, persistentId = true, __parent = true, position = true, Types = true,
  internalName = true, canSave = true }

local function fieldValue(v)
  if type(v) == 'table' then
    local parts = {}
    for i, x in ipairs(v) do parts[i] = tostring(x) end
    return table.concat(parts, ' ')
  end
  if type(v) == 'boolean' then return v and '1' or '0' end
  return tostring(v)
end

-- The stock definitions of one source by name, from its items files (one JSON object per line).
local function readDefs(source)
  local want = {}
  for _, d in ipairs(M.DEFS) do if d.source == source then want[d.src] = true end end
  for _, p in pairs(M.PROFILES) do
    for _, e in ipairs(p) do if e.source == source then want[e.src] = true end end
  end
  local out = {}
  for _, file in ipairs(list(M.SOURCES[source].items)) do
    local text = readFile and readFile(file)
    if type(text) ~= 'string' then return nil, 'cannot read ' .. file end
    for line in text:gmatch('[^\r\n]+') do
      if line:find('"GroundCover"', 1, true) then
        local ok, d = pcall(jsonDecode, line)
        if ok and type(d) == 'table' and want[d.name] and not out[d.name] then out[d.name] = d end
      end
    end
  end
  return out
end

-- Pure: the setField calls (name, index, value) that recreate a stock definition on `layer`, its
-- element count scaled by `share`, its radii by `dist` (default 1); `types` (0-based index -> {layer, p}) point
-- plant types at another layer with their own probability.
function M.fieldsFor(def, layer, share, dist, types)
  local calls = {}
  dist = dist or 1
  for k, v in pairs(def) do
    if not SKIP[k] and v ~= jsonNull then
      if k == 'maxElements' then v = math.max(1, math.floor((tonumber(v) or 0) * share + 0.5)) end
      if M.RADIUS_FIELDS[k] and dist ~= 1 and tonumber(v) then v = tonumber(v) * dist end
      calls[#calls + 1] = { k, 0, fieldValue(v) }
    end
  end
  if type(def.Types) == 'table' then
    for i, t in ipairs(def.Types) do
      local o = types and types[i - 1]
      for k, v in pairs(t) do
        if v ~= nil and v ~= jsonNull and not (o and (k == 'layer' or k == 'probability')) then
          if k == 'layer' then v = (v ~= '' and layer or '') end
          calls[#calls + 1] = { k, i - 1, fieldValue(v) }
        end
      end
      if o then
        calls[#calls + 1] = { 'layer', i - 1, o.layer or layer }
        calls[#calls + 1] = { 'probability', i - 1, fieldValue(o.p or 1) }
      end
    end
  end
  table.sort(calls, function(a, b) if a[1] ~= b[1] then return a[1] < b[1] end return a[2] < b[2] end)
  return calls
end

local remake
local function removeAll()
  for _, name in ipairs(created) do
    local o = scenetree.findObject(name)
    if o then pcall(function() o:delete() end) end
  end
  created = {}
  made = {}
end

local function currentMonth()
  local m = M.dayOverride or (os.date('*t') or {}).yday
  local okS, sl = pcall(require, 'beamngEarth/seasonlook')
  local mode = okS and type(sl) == 'table' and sl.getMode and sl.getMode() or nil
  return M.dayFor(m, lat, mode)
end

local function source(name)
  if not defsCache[name] then
    for _, f in ipairs(list(M.SOURCES[name].materials)) do
      if not loadedMaterials[f] then
        loadJsonMaterialsFile(f)
        loadedMaterials[f] = true
      end
    end
    local d, e = readDefs(name)
    if not d then error(e) end
    defsCache[name] = d
  end
  return defsCache[name]
end

local function wanted(e)
  if not seen then return true end
  for l in pairs(M.layersOf(e)) do if seen[l] then return true end end
  return false
end

local function makeOne(e, def)
  local name = M.PREFIX .. (e.name or e.id or e.src)
  -- one left by an earlier module instance (a GE Lua reload keeps the level's objects) may
  -- have older fields, e.g. GPU placement: made again from the current definition
  local old = scenetree.findObject(name)
  if old then old:delete() end
  local gc = createObject('GroundCover')
  local dist = M.distance
  for _, c in ipairs(M.fieldsFor(def, e.layer, e.share * M.density * dist * dist, dist, e.types)) do
    gc:setField(c[1], c[2], c[3])
  end
  for k, v in pairs(e.fields or {}) do
    gc:setField(k, 0, tostring(M.RADIUS_FIELDS[k] and tonumber(v) and tonumber(v) * dist or v))
  end
  for _, f in ipairs(M.PLACEMENT) do gc:setField(f[1], 0, f[2]) end
  gc.canSave = false
  gc:registerObject(name)
  scenetree.MissionGroup:addObject(gc)
  pcall(function() gc:postApply() end) -- the first layered GroundCover once showed nothing until this
  created[#created + 1] = name
end

-- Create the GroundCovers of the profile and month (the surface layers exist); with `materials` (the block's
-- {name, ground model} list) only those of layers some block listed since the last rebuild, the others once a
-- block lists them (made then, the ones there kept). Returns true if any exist.
function M.ensure(materials)
  layered = true
  if type(materials) == 'table' then
    lastBlock = {}
    for _, m in ipairs(materials) do lastBlock[type(m) == 'table' and m[1] or m] = true end
    if seen then for l in pairs(lastBlock) do seen[l] = true end end
  end
  if not M.enabled then return #created > 0 end
  if not scenetree.MissionGroup then return false end
  if #created == 0 then
    month = currentMonth()
    if not keepSeen then
      seen = lastBlock and {} or nil
      if seen then for l in pairs(lastBlock) do seen[l] = true end end
    end
  end
  -- (each entry on its own: a stock file missing in some game version costs that plant only)
  for _, e in ipairs(M.plan(profile, month)) do
    local id = e.name or e.id or e.src
    if not made[id] and wanted(e) then
      made[id] = true
      local ok, err = pcall(function()
        local def = source(e.source)[e.src]
        if def then
          makeOne(e, def)
        else
          log('W', 'beamngEarth', 'ground cover: no ' .. e.src .. ' in ' .. e.source)
        end
      end)
      if not ok then
        lastError = tostring(err)
        log('W', 'beamngEarth', 'ground cover: ' .. e.source .. '/' .. e.src .. ': ' .. lastError)
      end
    end
  end
  return #created > 0
end

-- The physics block was just imported or moved (core, same tick): regenerate all the grass before
-- this frame is drawn. Returns true if it asked for that.
function M.afterBlockChange()
  if #created == 0 or not flushGroundCoverGrids then return false end
  flushGroundCoverGrids()
  return true
end

-- On / off (the settings page): off removes the objects; on makes them at once when the block has
-- surface layers (else with the next layered import).
function M.setEnabled(on)
  M.enabled = on and true or false
  if not M.enabled then
    removeAll()
  elseif layered and #created == 0 and M.ensure() then
    M.afterBlockChange()
  end
  return M.enabled
end

-- Density (the settings page): 'low' / 'normal' / 'high' or a factor; existing objects are made
-- again with it.
function M.setDensity(v)
  local f = M.DENSITY[v] or tonumber(v)
  if not f or f <= 0 then return false end
  if f == M.density then return true end
  M.density = f
  remake()
  return true
end

-- Draw distance (the settings page): 'near' / 'normal' / 'far' or a factor; existing objects are made again with it.
function M.setDistance(v)
  local f = M.DISTANCE[v] or tonumber(v)
  if not f or f <= 0 then return false end
  if f == M.distance then return true end
  M.distance = f
  remake()
  return true
end

-- The climate round the listener (ambience.lua's vote of the vegetation tiles: k their Koppen class, biome their
-- ambience set): another profile makes the objects again (at a climate border, seldom: the vote holds its choice).
function M.setClimate(k, biome)
  climate.k, climate.biome = tonumber(k), biome
  local p = M.profileOf(k, biome)
  if p == profile then return false end
  profile = p
  remake()
  return true
end

-- The anchor's latitude (core, every frame: the season's hemisphere); a new profile day makes the objects again.
local monthCheck = -math.huge
function M.setLatitude(v)
  local was = lat
  lat = tonumber(v)
  if #created == 0 or not month then return end
  -- (looked at once a minute, or at once when the hemisphere changes: os.date each frame is waste)
  local now = os.clock()
  if now - monthCheck < 60 and (was == nil) == (lat == nil) and (was or 0) * (lat or 0) >= 0 then return end
  monthCheck = now
  if currentMonth() ~= month then remake() end
end

-- (console) the grass of calendar month m's middle (nil: today), or of day of the year d
function M.setMonth(m)
  M.dayOverride = m and M.dayOfMonth(m) or nil
  remake()
  return currentMonth()
end
function M.setDay(d)
  M.dayOverride = tonumber(d)
  remake()
  return currentMonth()
end

-- The level ended (its objects are gone with it, the stock materials loaded for it too: a level started again in
-- the same game session found no m_grass_* / wca_groundcover and drew no grass, 2026-10-19).
function M.reset()
  created = {}
  made = {}
  defsCache = {}
  loadedMaterials = {}
  layered = false
  seen, lastBlock, month = nil, nil, nil
  profile = 'generic'
  climate = { k = nil, biome = nil }
end

-- A teleport or a floating-origin rebase moved the world under the grass: make the objects again
-- (their cells then start from nothing whatever the engine kept) and generate them this frame. The layers are the
-- latest block's again.
function M.rebuild()
  if #created == 0 then return false end
  removeAll()
  M.ensure()
  return M.afterBlockChange()
end

-- The same place, other objects (a climate, month, density or distance change): made again for every layer seen.
remake = function()
  if #created == 0 then return false end
  removeAll()
  keepSeen = true
  pcall(M.ensure)
  keepSeen = false
  return M.afterBlockChange()
end

function M.status()
  local names = {}
  for i, n in ipairs(created) do names[i] = n:sub(#M.PREFIX + 1) end
  return { enabled = M.enabled, density = M.density, distance = M.distance, objects = #created, names = names,
    profile = profile, koppen = climate.k, day = month, materialsLoaded = next(loadedMaterials) ~= nil,
    error = lastError }
end

return M
