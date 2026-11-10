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
-- Raceway grass on lush layers, Italy's lighter grass on transitional layers, and golden Small Island /
-- West Coast grass with Johnson Valley tufts on dry layers. The server chooses layers using smoothed
-- imagery and existing surface exclusions; density and streaming stay here. Stock materials are read,
-- validated and registered with source-specific names: Italy and Hirochi use the same material names
-- for different textures. Console: beamngEarth_groundcover.setEnabled(bool), .status()

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
  italy = { materials = '/levels/italy/art/shapes/groundcover/main.materials.json',
            items = '/levels/italy/main/MissionGroup/Level_objects/vegetation/terrain_Grass/items.level.json' },
  island = { materials = '/levels/small_island/art/shapes/groundcover/main.materials.json',
             items = '/levels/small_island/main/MissionGroup/Level_objects/vegetation/items.level.json' },
  johnson = { materials = '/levels/johnson_valley/art/shapes/groundcover/main.materials.json',
              items = '/levels/johnson_valley/main/MissionGroup/level_object/vegetation/items.level.json' },
}
-- stock GroundCover (source, src name) -> our layer, element share; `name` (default src) names our
-- object, for a stock definition used on two layers; `fields` override stock fields. The shares keep
-- the block-move rebuild near +15 ms (in-game at Serengeti grassland 2026-10-11: stock-ish shares
-- +30 ms; the dense near grass costs most, the long grass is kept close, radius 70 m instead of 120).
local LONG_NEAR = { radius = 70, dissolveRadius = 45, gridSize = 8 }
local SHORT_NEAR = { radius = 50, dissolveRadius = 30, gridSize = 6 }
local ACCENT_NEAR = { radius = 60, dissolveRadius = 30, gridSize = 8 }
local SHRUB_NEAR = { radius = 100, gridSize = 8 }
local DRY_SHORT = { { 'island', 'small_grass_close' }, { 'wcusa', 'grass_dry_1' }, { 'hirochi', 'small_grass_close' } }
local DRY_LONG = { { 'wcusa', 'grass_dry_1' }, { 'island', 'medium_grass_close' }, { 'hirochi', 'long_grass_distant' } }
local DRY_TUFT = { { 'johnson', 'Grass' }, { 'wcusa', 'grass_dry_1' }, { 'hirochi', 'small_grass_close' } }
local LIGHT_SHORT = { { 'italy', 'small_grass_green_close' }, { 'hirochi', 'small_grass_close' }, { 'wcusa', 'grass_dry_1' } }
local LIGHT_LONG = { { 'italy', 'long_grass_green_distant' }, { 'hirochi', 'long_grass_distant' }, { 'wcusa', 'grass_dry_1' } }
local LUSH_SHORT = { { 'hirochi', 'small_grass_close' }, { 'italy', 'small_grass_green_close' }, { 'wcusa', 'grass_dry_1' } }
local LAWN_SHORT = { { 'hirochi', 'small_grass_close' }, { 'italy', 'small_grass_green_close' },
  { 'island', 'small_grass_close' }, { 'wcusa', 'mown_grass_close' } }
local LUSH_LONG = { { 'hirochi', 'long_grass_distant' }, { 'italy', 'long_grass_green_distant' }, { 'wcusa', 'grass_dry_1' } }
local SCRUB = { { 'wcusa', 'shrubs_near' }, { 'johnson', 'Grass' }, { 'island', 'small_grass_close' }, { 'hirochi', 'small_grass_close' } }
M.DEFS = {
  { source = 'hirochi', src = 'small_grass_close', layer = 'bngEarthGroundGrass', share = 0.25, alternatives = LUSH_SHORT },
  { source = 'hirochi', src = 'long_grass_distant', layer = 'bngEarthGroundGrass', share = 0.17, fields = LONG_NEAR, alternatives = LUSH_LONG },
  { source = 'wcusa', src = 'grass_dry_1', layer = 'bngEarthGroundGrass', share = 0.05, alternatives = DRY_LONG },
  -- mown town yards: a little short grass, nothing long (server/surface.py LAWN)
  { source = 'hirochi', src = 'small_grass_close', name = 'small_grass_close_lawn', layer = 'bngEarthGroundLawn', share = 0.1, alternatives = LAWN_SHORT, maxTypeHeight = 0.3 },
  { source = 'wcusa', src = 'shrubs_near', layer = 'bngEarthGroundDusty', share = 0.15, alternatives = SCRUB },
  -- sparse grass between the shrubs (user 2026-10-11: a hard line between grassland and shrubland)
  { source = 'island', src = 'small_grass_close', name = 'small_grass_close_scrub', layer = 'bngEarthGroundDusty', share = 0.08, alternatives = DRY_SHORT },
  { source = 'johnson', src = 'Grass', name = 'long_grass_distant_scrub', layer = 'bngEarthGroundDusty', share = 0.05,
    fields = LONG_NEAR, alternatives = DRY_TUFT },
}
-- Every environmental variant has the same share budget. A brown prairie is not thinned simply
-- because its grass has dried. The lawn remains short-only, as before.
local function variant(name, layer, share, alternatives, fields)
  M.DEFS[#M.DEFS + 1] = { name = name, layer = layer, share = share, source = alternatives[1][1],
    src = alternatives[1][2], alternatives = alternatives, fields = fields }
end
variant('grass_dry_short', 'bngEarthGroundGrassDry', 0.25, DRY_SHORT)
variant('grass_dry_long', 'bngEarthGroundGrassDry', 0.17, DRY_LONG, LONG_NEAR)
variant('grass_dry_tuft', 'bngEarthGroundGrassDry', 0.05, DRY_TUFT, LONG_NEAR)
variant('grass_light_short', 'bngEarthGroundGrassLight', 0.25, LIGHT_SHORT)
variant('grass_light_long', 'bngEarthGroundGrassLight', 0.17, LIGHT_LONG, LONG_NEAR)
variant('grass_light_dry', 'bngEarthGroundGrassLight', 0.05, DRY_LONG)
variant('scrub_light_shrubs', 'bngEarthGroundDustyLight', 0.15, SCRUB)
variant('scrub_light_short', 'bngEarthGroundDustyLight', 0.08, LIGHT_SHORT)
variant('scrub_light_long', 'bngEarthGroundDustyLight', 0.05, LIGHT_LONG, LONG_NEAR)
variant('scrub_lush_shrubs', 'bngEarthGroundDustyLush', 0.15, SCRUB)
variant('scrub_lush_short', 'bngEarthGroundDustyLush', 0.08, LUSH_SHORT)
variant('scrub_lush_long', 'bngEarthGroundDustyLush', 0.05, LUSH_LONG, LONG_NEAR)
-- Asset providers have very different stock populations (100k to 400k). Keep our old populations
-- per role instead of letting an asset swap silently change the user's grass quantity.
for _, d in ipairs(M.DEFS) do
  if d.share == 0.15 then d.elements, d.fields = 160000, SHRUB_NEAR
  elseif d.share == 0.17 then d.elements, d.fields = 192000, LONG_NEAR
  elseif d.share == 0.05 and d.layer:find('Dusty', 1, true) then d.elements, d.fields = 192000, LONG_NEAR
  elseif d.share == 0.05 then d.elements, d.fields = 256000, ACCENT_NEAR
  else d.elements, d.fields = 306000, SHORT_NEAR end
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
local layered = false     -- a block with surface layers was loaded this level (objects can exist)
local defsCache = {}      -- source -> stock definitions by name (read once per level)
local loadedMaterials = {} -- source -> true
local materialDocs = {}   -- source -> parsed stock material document, or false
local selected = {}       -- our object name -> actual source/definition (including fallback)
local lastError = nil

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
  local out = {}
  for line in text:gmatch('[^\r\n]+') do
    if line:find('"GroundCover"', 1, true) then
      local ok, d = pcall(jsonDecode, line)
      if ok and type(d) == 'table' and d.name then out[d.name] = d end
    end
  end
  return out
end

-- Material JSON declares .png while installed releases package the compiled .dds; both are
-- valid VFS texture names. Check once while resolving a definition, never per plant or frame.
function M.textureExists(path)
  if type(path) ~= 'string' or path == '' then return true end
  if not FS or not FS.fileExists then return true end
  return FS:fileExists(path) or (path:sub(-4) == '.png' and FS:fileExists(path:sub(1, -5) .. '.dds')) or false
end

function M.materialName(source, name) return M.PREFIX .. source .. '_' .. name end

function M.resourcePath(source, path)
  if path:sub(1, 1) == '/' then return path end
  if path:match('^assets/') or path:match('^levels/') then return '/' .. path end
  return M.SOURCES[source].materials:match('^(.*/)') .. path
end

-- Read the installed material and recreate its fields under a private name. Loading another
-- level's whole materials file would overwrite Hirochi's grass with Italy's identically named
-- lighter material, and would change all already created GroundCover objects.
local function materialFor(source, def)
  if type(def.material) ~= 'string' or def.material == '' then return nil, 'no billboard material' end
  if materialDocs[source] == nil then
    local text = readFile and readFile(M.SOURCES[source].materials)
    local ok, doc = pcall(jsonDecode, text or '')
    materialDocs[source] = ok and type(doc) == 'table' and doc or false
  end
  local doc = materialDocs[source]
  local mat = doc and doc[def.material]
  if type(mat) ~= 'table' then return nil, 'missing material ' .. def.material end
  for _, stage in ipairs(mat.Stages or {}) do
    for k, v in pairs(stage) do
      if k:sub(-3) == 'Map' and type(v) == 'string' and v ~= '' and not M.textureExists(M.resourcePath(source, v)) then
        return nil, 'missing texture ' .. v
      end
    end
  end
  for _, t in ipairs(def.Types or {}) do
    local shape = t.shapeFilename
    if type(shape) == 'string' and shape ~= '' and FS and not FS:fileExists(shape) then
      return nil, 'missing shape ' .. shape
    end
  end
  local name = M.materialName(source, def.material)
  if not scenetree.findObject(name) then
    local m = createObject('Material')
    local ok, err = pcall(function()
      for k, v in pairs(mat) do
        if k ~= 'name' and k ~= 'mapTo' and k ~= 'class' and k ~= 'persistentId' and k ~= 'Stages'
            and v ~= jsonNull and (type(v) ~= 'table' or #v > 0) then
          m:setField(k, 0, fieldValue(v))
        end
      end
      for i, stage in ipairs(mat.Stages or {}) do
        for k, v in pairs(stage) do
          if v ~= jsonNull then
            if k:sub(-3) == 'Map' and type(v) == 'string' and v ~= '' then v = M.resourcePath(source, v) end
            m:setField(k, i - 1, fieldValue(v))
          end
        end
      end
      m:setField('mapTo', 0, name)
      m.canSave = false
      m:registerObject(name)
    end)
    if not ok then pcall(function() m:delete() end); return nil, tostring(err) end
  end
  loadedMaterials[name] = true
  return name
end

local function resolve(d)
  local errors = {}
  for _, candidate in ipairs(d.alternatives or { { d.source, d.src } }) do
    local source, src = candidate[1], candidate[2]
    local ok, def, material = pcall(function()
      if defsCache[source] == nil then defsCache[source] = readDefs(source) or false end
      local stock = defsCache[source] and defsCache[source][src]
      if not stock then return nil, 'missing definition ' .. source .. '/' .. src end
      local name, err = materialFor(source, stock)
      if not name then return nil, err end
      return stock, name
    end)
    if ok and def then return def, material, source .. '/' .. src end
    errors[#errors + 1] = tostring(ok and material or def)
  end
  return nil, table.concat(errors, '; ')
end

-- Pure: the setField calls (name, index, value) that recreate a stock definition on `layer`, its
-- element count scaled by `share`, its radii by `dist` (default 1).
function M.fieldsFor(def, layer, share, dist)
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
  lastError = nil
  selected = {}
  for _, d in ipairs(M.DEFS) do
    local def, material, choice = resolve(d)
    local name = M.PREFIX .. (d.name or d.src)
    if def then
      local gc
      local ok, err = pcall(function()
        -- one left by an earlier module instance (a GE Lua reload keeps the level's objects) may
        -- have older fields, e.g. GPU placement: made again from the current definition
        local old = scenetree.findObject(name)
        if old then old:delete() end
        do
          gc = createObject('GroundCover')
          local dist = M.distance
          local own = {}
          for k, v in pairs(def) do own[k] = v end
          own.material, own.maxElements = material, d.elements or def.maxElements
          -- Match the old role's covered area and cell population even when a fallback stock
          -- definition has a different radius/grid. Preserve the species' UVs, sizes and clumps.
          own.radius, own.dissolveRadius, own.gridSize = nil, nil, nil
          if d.maxTypeHeight then
            own.Types = {}
            for _, t in ipairs(def.Types or {}) do
              local hi = tonumber(t.sizeMax)
              if hi and hi <= d.maxTypeHeight + 1e-6 then own.Types[#own.Types + 1] = t end
            end
          end
          for _, c in ipairs(M.fieldsFor(own, d.layer, d.share * M.density * dist * dist, dist)) do
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
      end)
      if ok then
        created[#created + 1] = name
        selected[name] = choice
      else
        if gc then pcall(function() gc:delete() end) end
        lastError = tostring(err)
        log('W', 'beamngEarth', 'ground cover ' .. name .. ': ' .. lastError)
      end
    else
      local old = scenetree.findObject(name)
      if old then old:delete() end
      lastError = tostring(material)
      log('W', 'beamngEarth', 'ground cover ' .. name .. ': ' .. lastError)
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
  materialDocs = {}
  selected = {}
  lastError = nil
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
  return { enabled = M.enabled, density = M.density, distance = M.distance, objects = #created,
    materialsLoaded = next(loadedMaterials) ~= nil, selected = selected, error = lastError }
end

return M
