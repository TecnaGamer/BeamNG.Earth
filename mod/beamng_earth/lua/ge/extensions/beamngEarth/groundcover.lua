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
-- with its plant types' layer pointed at ours and its element count scaled by `share`. Green Hirochi
-- Raceway grass with a little of West Coast USA's dry grass on grassland, West Coast scrub with sparse
-- green grass between on dusty ground. One rule for every place, except (fork) the grass layer's dry / green mix:
-- the share of dry grass follows the climate and, in the southern hemisphere, the season (see "regional mix"
-- below). Console: beamngEarth_groundcover.setEnabled(bool), .status()

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

local HIROCHI, WCUSA = '/levels/hirochi_raceway/', '/levels/west_coast_usa/'
M.SOURCES = {
  hirochi = { materials = HIROCHI .. 'art/shapes/groundcover/main.materials.json',
              items = HIROCHI .. 'main/MissionGroup/vegetation/terrain_grass/items.level.json' },
  wcusa = { materials = WCUSA .. 'art/groundcover/main.materials.json',
            items = WCUSA .. 'main/MissionGroup/vegetation/items.level.json' },
}
-- stock GroundCover (source, src name) -> our layer, element share; `name` (default src) names our
-- object, for a stock definition used on two layers; `fields` override stock fields. The shares keep
-- the block-move rebuild near +15 ms (in-game at Serengeti grassland 2026-10-11: stock-ish shares
-- +30 ms; the dense near grass costs most, the long grass is kept close, radius 70 m instead of 120).
local LONG_NEAR = { radius = 70, dissolveRadius = 45 }
M.DEFS = {
  -- (fork: `mix` marks the grass layer's green / dry grass: their shares trade off at a constant total, M.mixFactors)
  { source = 'hirochi', src = 'small_grass_close', layer = 'bngEarthGroundGrass', share = 0.25, mix = 'green' },
  { source = 'hirochi', src = 'long_grass_distant', layer = 'bngEarthGroundGrass', share = 0.17, fields = LONG_NEAR, mix = 'green' },
  { source = 'wcusa', src = 'grass_dry_1', layer = 'bngEarthGroundGrass', share = 0.05, mix = 'dry' },
  -- mown town yards: a little short grass, nothing long (server/surface.py LAWN)
  { source = 'hirochi', src = 'small_grass_close', name = 'small_grass_close_lawn', layer = 'bngEarthGroundLawn', share = 0.1 },
  { source = 'wcusa', src = 'shrubs_near', layer = 'bngEarthGroundDusty', share = 0.15 },
  -- sparse grass between the shrubs (user 2026-10-11: a hard line between grassland and shrubland)
  { source = 'hirochi', src = 'small_grass_close', name = 'small_grass_close_scrub', layer = 'bngEarthGroundDusty', share = 0.08 },
  { source = 'hirochi', src = 'long_grass_distant', name = 'long_grass_distant_scrub', layer = 'bngEarthGroundDusty', share = 0.05,
    fields = LONG_NEAR },
  -- fork: sugarcane (server/surface.py CANE: WorldCover cropland in Queensland's coastal belt): the stock long grass, dense, its
  -- plants `typeScale` times as tall, over the whole layer (no short grass between: a cane field is a wall of leaf)
  { source = 'hirochi', src = 'long_grass_distant', name = 'long_grass_distant_cane', layer = 'bngEarthGroundCane', share = 0.45,
    fields = LONG_NEAR, typeScale = 2.4 },
  { source = 'hirochi', src = 'small_grass_close', name = 'small_grass_close_cane', layer = 'bngEarthGroundCane', share = 0.1,
    typeScale = 1.6 },
}
M.PREFIX = 'bngEarthGC_'

-- fork: regional mix. Stock: the dry grass is ~11 % of the grass layer's elements. Where the climate is dry (Australia,
-- the arid and savanna classes) a larger share of the same total is dry grass: the green shares scale down by as much
-- as the dry one scales up, so the element count (the block-move rebuild frame, see DEFS) stays where it was.
-- With no climate (a server without anchor_ready.koppen, a place outside the Australian box) nothing changes.
M.AU = { latMin = -44, latMax = -10, lonMin = 112, lonMax = 154 }  -- mainland + Tasmania (server/climate.py AU_*)
M.AU_GREEN = { [1] = true, [2] = true, [15] = true, [16] = true }  -- Af Am Cfb Cfc: no dry-grass floor (server AU_GREEN)
M.AU_DRY = 0.5                  -- the dry share in Australia where the Koppen class gives none (or none known)
-- Koppen-Geiger class (Beck et al. 2023, server/climate.py) -> dry share of the grass: BWh BWk BSh BSk, Aw, Csa Csb, Cwa
M.KOPPEN_DRY = { [3] = 0.55, [4] = 0.7, [5] = 0.7, [6] = 0.65, [7] = 0.6, [8] = 0.4, [9] = 0.35, [11] = 0.5 }
M.SEASON_AMP = 0.12             -- southern dry season: +/- this on the dry share, most on 1 Aug (doy 213), least on 30 Jan
M.SEASON_PEAK_DOY = 213
M.SUMMER_DRY = { [8] = true, [9] = true, [10] = true }   -- Csa Csb Csc: their dry season is the summer (no winter swing)
M.DRY_STEP = 20                 -- the dry share in steps of 1/20: the objects are remade when it moves one (a few times a year)
M.CHECK_S = 30                  -- the date is looked at this often (os.clock)

local function stockDry()
  local g, d = 0, 0
  for _, e in ipairs(M.DEFS) do
    if e.mix == 'dry' then d = d + e.share elseif e.mix == 'green' then g = g + e.share end
  end
  return d / (g + d)
end
M.STOCK_DRY = stockDry()

-- Pure: the dry share of the grass layer's elements (0..1, in DRY_STEP steps) at a place, or nil for the stock mix.
-- koppen: the anchor's class (nil = unknown); doy: day of the year when the season counts (nil = no season).
function M.dryShare(koppen, lat, lon, doy)
  if not (lat and lon) then return nil end
  local au = lat >= M.AU.latMin and lat <= M.AU.latMax and lon >= M.AU.lonMin and lon <= M.AU.lonMax
  local base = koppen and M.KOPPEN_DRY[koppen]
  if au and not (koppen and M.AU_GREEN[koppen]) then base = math.max(base or 0, M.AU_DRY) end
  if not base then return nil end
  if doy and lat < 0 and not (koppen and M.SUMMER_DRY[koppen]) then
    base = base + M.SEASON_AMP * math.cos(2 * math.pi * (doy - M.SEASON_PEAK_DOY) / 365)
  end
  base = math.min(math.max(base, 0), 0.9)
  return math.floor(base * M.DRY_STEP + 0.5) / M.DRY_STEP
end

-- Pure: the factors (green, dry) on the stock shares for a dry share (nil: 1, 1).
function M.mixFactors(dry)
  if not dry then return 1, 1 end
  local sd = M.STOCK_DRY
  return (1 - dry) / (1 - sd), dry / sd
end
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
local layered = false     -- a block with surface layers was loaded this level (objects can exist)
local defsCache = {}      -- source -> stock definitions by name (read once per level)
local loadedMaterials = {} -- source -> true
local lastError = nil
local climate = {}            -- fork: the anchor's koppen class, lat, lon (setClimate)
local seasonal = false        -- fork: the dry share follows today's date (the server's --season auto)
local wantDry = nil           -- fork: the dry share a build would use now (nil: the stock mix)
local builtDry = nil          -- fork: the one the objects were made with
local lastCheck = -math.huge

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

-- The stock definitions of one source by name, from its items file (one JSON object per line).
local function readDefs(source)
  local file = M.SOURCES[source].items
  local text = readFile and readFile(file)
  if type(text) ~= 'string' then return nil, 'cannot read ' .. file end
  local want = {}
  for _, d in ipairs(M.DEFS) do if d.source == source then want[d.src] = true end end
  local out = {}
  for line in text:gmatch('[^\r\n]+') do
    if line:find('"GroundCover"', 1, true) then
      local ok, d = pcall(jsonDecode, line)
      if ok and type(d) == 'table' and want[d.name] then out[d.name] = d end
    end
  end
  return out
end

-- Pure: the setField calls (name, index, value) that recreate a stock definition on `layer`, its
-- element count scaled by `share`, its radii by `dist` (default 1); fork: typeScale (default 1) multiplies each plant type's size.
function M.fieldsFor(def, layer, share, dist, typeScale)
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
      for k, v in pairs(t) do
        if v ~= nil and v ~= jsonNull then
          if k == 'layer' then v = (v ~= '' and layer or '') end
          if typeScale and (k == 'sizeMin' or k == 'sizeMax') and tonumber(v) then v = tonumber(v) * typeScale end   -- fork
          calls[#calls + 1] = { k, i - 1, fieldValue(v) }
        end
      end
    end
  end
  table.sort(calls, function(a, b) if a[1] ~= b[1] then return a[1] < b[1] end return a[2] < b[2] end)
  return calls
end

local function removeAll()
  for _, name in ipairs(created) do
    local o = scenetree.findObject(name)
    if o then pcall(function() o:delete() end) end
  end
  created = {}
end

-- Create the GroundCovers (once per level; the surface layers exist). Returns true if made.
function M.ensure()
  layered = true
  if not M.enabled or #created > 0 then return #created > 0 end
  if not scenetree.MissionGroup then return false end
  local ok, err = pcall(function()
    local defs = defsCache
    for _, d in ipairs(M.DEFS) do
      if not defs[d.source] then
        if not loadedMaterials[d.source] then
          loadJsonMaterialsFile(M.SOURCES[d.source].materials)
          loadedMaterials[d.source] = true
        end
        local e
        defs[d.source], e = readDefs(d.source)
        if not defs[d.source] then error(e) end
      end
      local def = defs[d.source][d.src]
      if def then
        local name = M.PREFIX .. (d.name or d.src)
        -- one left by an earlier module instance (a GE Lua reload keeps the level's objects) may
        -- have older fields, e.g. GPU placement: made again from the current definition
        local old = scenetree.findObject(name)
        if old then old:delete() end
        do
          local gc = createObject('GroundCover')
          local dist = M.distance
          local fg, fd = M.mixFactors(wantDry)      -- fork: the grass layer's green / dry mix
          local share = d.share * (d.mix == 'dry' and fd or d.mix == 'green' and fg or 1)
          for _, c in ipairs(M.fieldsFor(def, d.layer, share * M.density * dist * dist, dist, d.typeScale)) do
            gc:setField(c[1], c[2], c[3])
          end
          for k, v in pairs(d.fields or {}) do
            gc:setField(k, 0, tostring(M.RADIUS_FIELDS[k] and tonumber(v) and tonumber(v) * dist or v))
          end
          for _, f in ipairs(M.PLACEMENT) do gc:setField(f[1], 0, f[2]) end
          gc.canSave = false
          gc:registerObject(name)
          scenetree.MissionGroup:addObject(gc)
          pcall(function() gc:postApply() end) -- the first layered GroundCover once showed nothing until this
        end
        created[#created + 1] = name
      else
        log('W', 'beamngEarth', 'ground cover: no ' .. d.src .. ' in ' .. d.source)
      end
    end
  end)
  if not ok then
    lastError = tostring(err)
    log('W', 'beamngEarth', 'ground cover: ' .. lastError)
    removeAll()
  end
  builtDry = wantDry        -- fork
  return #created > 0
end

-- fork: the day of the year (a test replaces it)
function M.today() return tonumber(os.date('*t').yday) end

local function refreshDry()
  wantDry = M.dryShare(climate.koppen, climate.lat, climate.lon, seasonal and M.today() or nil)
end

-- fork: where the grass is (core, at each anchor commit: the anchor_ready's koppen class, absent from an older server,
-- and its lat / lon). Sets what the next build (the teleport's / rebase's rebuild, the level's first block) uses.
function M.setClimate(koppen, lat, lon)
  climate.koppen, climate.lat, climate.lon = tonumber(koppen), tonumber(lat), tonumber(lon)
  refreshDry()
end

-- fork: the dry share follows the date (hello.season == 'auto', the imagery's season; seasonlook.lua)
function M.setSeasonal(on)
  seasonal = on and true or false
  refreshDry()
end

-- fork: every frame in our level: the grass made again when the season moved the dry share a step (a few times a year).
function M.update(inOurLevel)
  if not inOurLevel or not seasonal or #created == 0 then return false end
  local now = os.clock()
  if now - lastCheck < M.CHECK_S then return false end
  lastCheck = now
  refreshDry()
  if wantDry == builtDry then return false end
  return M.rebuild()
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
  if #created > 0 then M.rebuild() end
  return true
end

-- Draw distance (the settings page): 'near' / 'normal' / 'far' or a factor; existing objects are made again with it.
function M.setDistance(v)
  local f = M.DISTANCE[v] or tonumber(v)
  if not f or f <= 0 then return false end
  if f == M.distance then return true end
  M.distance = f
  if #created > 0 then M.rebuild() end
  return true
end

-- The level ended (its objects are gone with it, the stock materials loaded for it too: a level started again in
-- the same game session found no m_grass_* / wca_groundcover and drew no grass, 2026-10-19).
function M.reset()
  created = {}
  defsCache = {}
  loadedMaterials = {}
  layered = false
end

-- A teleport or a floating-origin rebase moved the world under the grass: make the objects again
-- (their cells then start from nothing whatever the engine kept) and generate them this frame.
function M.rebuild()
  if #created == 0 then return false end
  removeAll()
  M.ensure()
  return M.afterBlockChange()
end

function M.status()
  return { enabled = M.enabled, density = M.density, distance = M.distance, objects = #created, materialsLoaded = next(loadedMaterials) ~= nil, error = lastError,
           dryShare = builtDry }   -- fork: the dry-grass share the objects were made with (nil: the stock mix)
end

return M
