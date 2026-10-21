-- The near, drivable ground: one runtime TerrainBlock, created once and re-imported whenever
-- it follows the player or the anchor changes (docs/V1_1_SPEC.md 1a). Engine-only module.
-- N = 1024 samples at squareSize exactly 2.0 m (world-locked lattice, lattice.lua and
-- docs/V1_2_DESIGN.md 1.2, 12): collidable area 2046 m x 2046 m; re-import ~42 ms warm (P7).
--
-- Near visual 'mesh' (docs/V1_3_SPEC.md B, C): the block stays the collision surface but is not
-- drawn (setRenderEnabled(false): a SceneObject method, exe strings "setRenderEnabled" /
-- "isRenderEnabled", used by stock editor/particleEditor2.lua:227; a render-disabled TerrainBlock
-- stays fully collidable, ARCHITECTURE.md TerrainBlock probes). Whether importMaps keeps the flag
-- is unverified, so it is re-applied after every import.
--
-- Creation mirrors util/terrainGenerator.lua:177-181 (TerrainBlock(), setName,
-- registerObject, setTerrFileLvlFolder, MissionGroup:addObject) and the material setup
-- of terrainGenerator.lua:451-463 (addMaterial/updateMaterial, then importMaps with a
-- material-name list and one layer map per material).
--
-- Block size (docs/V1_4_SPEC.md 3.5, M4): configure(size) from the server's hello.blockSize picks
-- the 1024² or 512² layer map and flat heightmap (the layer map must match the heightmap size).

local lattice = require('beamngEarth/lattice')
local stopwatch = require('beamngEarth/stopwatch')

local M = {}

M.NAME = 'bngEarthTerrain'
M.SIZE = lattice.N              -- samples per side (configure); the layer map has the same size
M.SQUARE_SIZE = lattice.SQUARE  -- 2.0 m
M.MATERIAL = 'beamngEarthDirt'
M.TEXTURE_SET = 'beamngEarthTerrainMaterialTextureSet'
M.BASE_TEX_SIZE = 1024   -- = the level texture set's base slot (tools/make_level_assets.py BASE_TEX)

local TERRAIN_DIR = '/levels/beamng_earth/art/terrains/'
local TERRAIN_DIR_BASE = TERRAIN_DIR .. 'bngEarth_base_b.png'   -- the level's flat base colour
local FLAT_H = TERRAIN_DIR .. 'bngEarth_flat_h_1024.png'          -- neutral height (detail / macro slots)
local MATERIALS_FILE = TERRAIN_DIR .. 'main.materials.json'
-- per block size (tools/make_level_assets.py): all white = material 0 everywhere; all zero, 16-bit
local LAYER_MAPS = { [1024] = TERRAIN_DIR .. 'beamngEarth_layer_1024.png', [512] = TERRAIN_DIR .. 'beamngEarth_layer_512.png', [256] = TERRAIN_DIR .. 'beamngEarth_layer_256.png' }
local FLAT_HEIGHTMAPS = { [1024] = TERRAIN_DIR .. 'beamngEarth_flat_1024.png', [512] = TERRAIN_DIR .. 'beamngEarth_flat_512.png', [256] = TERRAIN_DIR .. 'beamngEarth_flat_256.png' }
local LAYER_MAP, FLAT_HEIGHTMAP = LAYER_MAPS[1024], FLAT_HEIGHTMAPS[1024]

-- Surface materials (server --surface on, server/surface.py): runtime clones of M.MATERIAL with other
-- ground models. The block is physics-only in mesh mode, so they only need a name and a ground model;
-- their texture fields are copied so a rendered block (terrain mode, dev "show physics terrain") still
-- draws. They join the TerrainMaterialSet by themselves (in-game probe 2026-10-11).
local TEX_FIELDS = { 'aoBaseTex', 'aoBaseTexSize', 'aoDetailStrength', 'aoDetailTex', 'aoMacroTex', 'aoMacroTexSize',
  'baseColorBaseTex', 'baseColorBaseTexSize', 'baseColorDetailStrength', 'baseColorDetailTex', 'baseColorMacroStrength',
  'baseColorMacroTex', 'baseColorMacroTexSize', 'detailDistances', 'macroDistances', 'heightBaseTex', 'heightBaseTexSize',
  'heightDetailTex', 'heightMacroTex', 'heightMacroTexSize', 'normalBaseTex', 'normalBaseTexSize', 'normalDetailStrength',
  'normalDetailTex', 'normalMacroStrength', 'normalMacroTex', 'normalMacroTexSize', 'roughnessBaseTex', 'roughnessBaseTexSize',
  'roughnessDetailStrength', 'roughnessDetailTex', 'roughnessMacroStrength', 'roughnessMacroTex', 'roughnessMacroTexSize' }

-- Detail near-visual mode (2026-10-12): the drawn block's surfaces get their own stock detail and macro
-- textures (all 1024², the texture set's detail / macro size). The base colour is the server's aerial
-- photo of the block (setBase); the base normal / roughness / ao / height stay the level's flat maps.
-- Texture sizes are in heightmap SAMPLES, not metres (the shader samples uv / size with uv = block-local
-- sample coordinates, size packed as an integer; in-game 2026-10-12 with a grid image: size 1024 on the
-- 2 m block repeated every 2048 m). LOOKS give metres; toSamples converts them with the block's square
-- size. Repeats in metres divide the 128 m block step (2 / 4 m detail, 32 / 64 m macro), or the pattern
-- jumps under the car at every terrain move (the in-game finding noted in tools/make_level_assets.py).
local T = '/assets/materials/terrain/'
local function look(detail, dsize, macro, msize, strength)
  local l = { detailDistances = '0 0 50 80', macroDistances = '0 0 150 1500' }
  for _, k in ipairs({ { 'baseColor', '_b' }, { 'normal', '_nm' }, { 'roughness', '_r' }, { 'ao', '_ao' } }) do
    l[k[1] .. 'DetailTex'] = T .. detail .. k[2] .. '.png'
    l[k[1] .. 'MacroTex'] = T .. macro .. k[2] .. '.png'
  end
  -- flat height textures: height-blended layer borders swirled across whole blocks between the land-cover
  -- classes (in-game 2026-10-12), so the borders follow the server's masks; never '' (an empty slot makes
  -- the engine build a default texture at every updateMaterial, ~0.8 s)
  l.heightDetailTex, l.heightMacroTex = FLAT_H, FLAT_H
  l.baseColorDetailTexSize, l.normalDetailTexSize, l.roughnessDetailTexSize, l.aoDetailTexSize, l.heightDetailTexSize =
    dsize, dsize, dsize, dsize, dsize
  l.baseColorMacroTexSize, l.normalMacroTexSize, l.roughnessMacroTexSize, l.aoMacroTexSize, l.heightMacroTexSize =
    msize, msize, msize, msize, msize
  -- colour: the photo carries it, the detail only adds grain (West Coast USA: 0.25 / 0.1)
  l.baseColorDetailStrength = (strength or 0.25) .. ' 0.08'
  l.normalDetailStrength, l.roughnessDetailStrength, l.aoDetailStrength = '1 0.3', '1 0.4', '0.8 0.2'
  l.baseColorMacroStrength, l.normalMacroStrength, l.roughnessMacroStrength = '0.12 0.08', '0.5 0.3', '0 0.8'
  return l
end
M.LOOKS = {
  bngEarthGroundAsphalt = look('asphalt/asphalt/t_asphalt', 4, 'asphalt/macro_asphalt/t_macro_asphalt', 32, 0.3),
  bngEarthGroundGrass = look('grass/t_grass_01/t_grass_01', 2, 'grass/t_macro_grass/t_macro_grass', 64),
  bngEarthGroundDusty = look('soil/dirt_loose_dusty/t_dirt_loose_dusty', 2, 'soil/macro_clumply/t_macro_clumpy', 64),
  bngEarthGroundLoose = look('soil/t_gravel/t_gravel', 2, 'soil/macro_clumply/t_macro_clumpy', 64),
  bngEarthGroundRock = look('rock/t_dirt_rocky/t_dirt_rocky', 4, 'rock/macro_rocky/t_macro_rocky', 64),
  bngEarthGroundSnow = look('sand/t_sand/t_sand', 4, 'sand/t_macro_sand/t_macro_sand', 64, 0.12),
  bngEarthGroundMud = look('mud/mud/t_mud', 2, 'soil/macro_clumply/t_macro_clumpy', 64),
  bngEarthGroundCobble = look('asphalt/t_asphalt_02/t_asphalt_02', 2, 'asphalt/macro_asphalt/t_macro_asphalt', 32, 0.3),
  bngEarthGroundLawn = look('grass/t_grass_01/t_grass_01', 1, 'grass/t_macro_grass/t_macro_grass', 64, 0.2),
  beamngEarthDirt = look('soil/dirt_crumbly/t_dirt_crumbly', 2, 'soil/macro_clumply/t_macro_clumpy', 64),
}
M.LEVEL_BASE = { tex = TERRAIN_DIR_BASE, size = '256' }

M.square = 2.0   -- the block's square size (m per sample), from the last load
-- surface materials made here (other levels: our level defines them in main.materials.json, made by
-- tools/make_level_assets.py, because runtime TerrainMaterials made every updateMaterial cost ~1 s,
-- in-game 2026-10-12)
M.runtimeMade = {}
local function toSamples(metres) return math.max(1, math.floor(metres / M.square + 0.5)) end

local function applyLook(m, name)
  local l = M.LOOKS[name]
  if not l then return end
  for k, v in pairs(l) do
    if k:sub(-7) == 'TexSize' then v = toSamples(v) end
    pcall(function() m:setField(k, 0, tostring(v)) end)
  end
end

-- Make sure every { name, groundmodel } exists as a TerrainMaterial (M.MATERIAL is the level's own).
-- Returns false if one could not be made.
local function ensureSurfaceMaterials(materials)
  local src = scenetree.findObject(M.MATERIAL)
  for _, nm in ipairs(materials) do
    local name, gm = nm[1], nm[2]
    local existing = scenetree.findObject(name)
    if existing then
      -- another object of that name (a weather Precipitation was once called like a material): the
      -- import would bind the layer to nothing and the engine would make it asphalt
      local okc, cls = pcall(function() return existing:getClassName() end)
      if okc and cls and cls ~= 'TerrainMaterial' then return false end
    end
    if name ~= M.MATERIAL and not existing then
      local ok = pcall(function()
        local m = createObject('TerrainMaterial')
        if src then
          for _, f in ipairs(TEX_FIELDS) do
            local okf, v = pcall(function() return src:getField(f, 0) end)
            if okf and v and v ~= '' then m:setField(f, 0, v) end
          end
        end
        applyLook(m, name)
        M.runtimeMade[name] = true
        m:setField('internalName', 0, name)
        m:setField('groundmodelName', 0, gm)
        m.canSave = false
        m:registerObject(name)
      end)
      if not ok or not scenetree.findObject(name) then return false end
    end
  end
  return true
end

local materialsChecked = false
local renderEnabled = true -- false in mesh mode; survives levels (a server setting)

-- Our level loads main.materials.json by itself (the level loader scans the level folder,
-- server/server.lua:200-212 loadJsonMaterialsFile). In any other level (testing) load it on demand.
local function ensureMaterials()
  if materialsChecked then return end
  materialsChecked = true
  if not scenetree.findObject(M.MATERIAL) or not scenetree.findObject(M.TEXTURE_SET) then
    log('I', 'beamngEarth', 'loading terrain materials from ' .. MATERIALS_FILE)
    loadJsonMaterialsFile(MATERIALS_FILE)
  end
end

function M.get()
  return scenetree.findObject(M.NAME)
end

-- The session's block size (1024 or 512, after lattice.configure): the imports' layer map and the
-- flat start map follow it. Returns the size in use.
function M.configure(size, nearVisual)
  local n = (size == 256 or nearVisual == 'mesh') and 256 or (LAYER_MAPS[size] and size or 1024)
  local assetN = (n == 256 and not (rawget(_G, 'FS') and FS.fileExists and FS:fileExists(LAYER_MAPS[256]))) and 512 or n
  M.SIZE, LAYER_MAP, FLAT_HEIGHTMAP = n, LAYER_MAPS[assetN], FLAT_HEIGHTMAPS[assetN]
  return n
end

function M.setNearVisual(mode)
  M.nearVisual = (mode == 'mesh' or mode == 'detail') and mode or 'terrain'
  M.setRenderEnabled(M.nearVisual ~= 'mesh')
  if M.nearVisual == 'mesh' then M.configure(256, 'mesh') end
  return M.nearVisual
end

-- Apply the render flag to the block. Terrain mode never calls the engine unless the flag was off
-- before (keeps the v1.2 path untouched).
local function applyRender(tb, force)
  if (force or not renderEnabled) and tb.setRenderEnabled then tb:setRenderEnabled(renderEnabled) end
end

local function getOrCreate(opts)
  local tb = M.get()
  if tb then return tb end
  ensureMaterials()
  local standbyBlock = TerrainBlock()
  tb = standbyBlock
  tb:setName(M.NAME)
  -- the resolution the engine bakes the block's base colour at (default 1024; West Coast USA's block 2048;
  -- set before registerObject: the engine sizes it when the block is added)
  tb:setField('baseTexSize', 0, tostring(M.BASE_TEX_SIZE))
  -- Pre-hide / disable render before registerObject in mesh mode so the engine does not allocate
  -- GPU render-quadtree or shadow map resources during registration.
  if (opts and opts.hidden == true) and tb.setHidden then tb:setHidden(true) end
  applyRender(tb)
  tb:registerObject(M.NAME)
  tb.canSave = false
  -- importMaps keeps its .ter data under this folder; /temp is writable and outside any level.
  tb:setTerrFileLvlFolder('/temp/beamngEarth/')
  -- v1.5 terrain materials need the block to name its TerrainMaterialTextureSet
  -- (terrainGenerator.lua:418, editor/terrainEditor.lua:1280).
  tb:setField('materialTextureSet', 0, M.TEXTURE_SET)
  scenetree.MissionGroup:addObject(tb)
  local ok, err = pcall(function()
    tb:addMaterial(M.MATERIAL, -1)
    tb:updateMaterial(0, M.MATERIAL)
  end)
  if not ok then log('W', 'beamngEarth', 'terrain addMaterial failed: ' .. tostring(err)) end
  return tb
end

-- Draw the block (terrain mode) or not (mesh mode: physics only).
function M.setRenderEnabled(on)
  on = on and true or false
  if on == renderEnabled then return end
  renderEnabled = on
  local tb = M.get()
  if tb then applyRender(tb, true) end
end

function M.isRenderEnabled() return renderEnabled end

-- layers (optional): { masks = { path, ... }, materials = { { name, groundmodel }, ... } } (same order):
-- one 0/255 mask per material instead of the single layer map
local function importMaps(tb, path, squareSize, maxHeight, withMaterial, layers)
  local materials, textureMaps = {}, {}
  if withMaterial and layers then
    for i, nm in ipairs(layers.materials) do
      materials[i] = nm[1]
      textureMaps[i] = { path = layers.masks[i], selected = false, material = nm[1], materialId = i - 1, channel = 'R', channelId = 0 }
    end
  elseif withMaterial then
    -- same table layout as terrainGenerator.lua:183-185 getTextureMap()
    materials = { M.MATERIAL }
    textureMaps = { { path = LAYER_MAP, selected = false, material = M.MATERIAL, materialId = 0, channel = 'R', channelId = 0 } }
  end
  local ok, res = pcall(function()
    return tb:importMaps(path, squareSize, maxHeight, '', materials, textureMaps, false)
  end)
  if not ok then return false, tostring(res) end
  if res == false then return false, 'importMaps returned false' end
  -- the block's material slots in order (TerrainBlock:getMaterialNames() is empty in 0.39.4, in-game
  -- 2026-10-12): setBase updates them by index
  M.slots = #materials > 0 and materials or { M.MATERIAL }
  return true
end

-- Load a heightmap PNG and place the block's SW corner at (x, y, z).
-- PNG row 0 is the north edge with flipY=false (V1_SPEC "Why terrain-based ground").
-- The caller rebuilds collision (be:reloadCollision) once all world edits of the frame are done.
-- layers (optional, server --surface on): { masks = { path, ... }, materials = { { name, gm }, ... } }
-- base / baseSize (optional, detail mode): the block's aerial photo (setBase)
function M.load(path, squareSize, maxHeight, x, y, z, layers, base, baseSize, opts)
  if not FS:fileExists(path) then return false, 'heightmap not found: ' .. tostring(path) end
  M.square = tonumber(squareSize) or M.square
  local tb = getOrCreate(opts)
  if (opts and opts.hidden == true) and tb.setHidden then tb:setHidden(true) end
  applyRender(tb)
  if layers then
    local okL = type(layers.masks) == 'table' and type(layers.materials) == 'table' and #layers.masks == #layers.materials
      and #layers.masks > 0 and ensureSurfaceMaterials(layers.materials)
    if okL then
      for _, m in ipairs(layers.masks) do if not FS:fileExists(m) then okL = false; break end end
    end
    if not okL then
      log('W', 'beamngEarth', 'surface layers unusable, importing the single dirt layer')
      layers = nil
    end
  end
  local ok, err = importMaps(tb, path, squareSize, maxHeight, true, layers)
  if not ok then
    -- Ground without a material still collides; it just renders as the placeholder.
    log('W', 'beamngEarth', 'importMaps with material failed (' .. tostring(err) .. '), retrying without')
    ok, err = importMaps(tb, path, squareSize, maxHeight, false)
    if not ok then return false, err end
  end
  tb:setPosition(vec3(x, y, z))
  applyRender(tb) -- mesh mode: hidden again after every import
  if base ~= nil then M.setBase(base or nil, baseSize) end
  return true
end

M.loadBlock = M.load

-- Move the block by swapping in the server's .ter of the new block (terrainFile + postApply, a new file
-- name each time: the engine skips a name it already has). ~11 ms for 512², and collision follows by the
-- next frame with no reloadCollision (docs/research/2026-10-12-engine-limits-and-worldgen.md §7) against
-- importMaps (~20 ms) + reloadCollision (~10 ms Lua + a ~20 ms physics frame). Only after a first
-- importMaps (the block exists with its material set-up) and at the same square size: returns false
-- (nothing changed) otherwise, and the caller imports. The .ter names its materials (server
-- surface.write_ter: layers.materials, or M.MATERIAL alone). The caller does NOT rebuild collision.
function M.swap(terPath, squareSize, maxHeight, x, y, z, layers, base, baseSize, opts)
  local tb = M.get()
  local sq = tonumber(squareSize)
  if not tb or not M.slots or not sq or math.abs(sq - M.square) > 1e-9 then return false end
  if type(terPath) ~= 'string' or not FS:fileExists(terPath) then return false end
  local slots = { M.MATERIAL }
  if layers then
    if type(layers.materials) ~= 'table' or #layers.materials == 0 or not ensureSurfaceMaterials(layers.materials) then
      return false
    end
    slots = {}
    for i, nm in ipairs(layers.materials) do slots[i] = nm[1] end
  end
  local sw = stopwatch.new()
  local ok, err = pcall(function()
    if (opts and opts.hidden == true) and tb.setHidden then tb:setHidden(true) end
    applyRender(tb)
    tb:setField('maxHeight', 0, tostring(maxHeight))
    tb:setPosition(vec3(x, y, z))
    tb:setField('terrainFile', 0, terPath)
    tb:postApply()
  end)
  local tApply = sw()
  if not ok then
    log('W', 'beamngEarth', 'terrain .ter swap failed: ' .. tostring(err))
    return false
  end
  M.slots = slots
  M.swaps = (M.swaps or 0) + 1
  applyRender(tb)
  if base ~= nil then M.setBase(base or nil, baseSize) end
  -- ms of the last swap: the .ter load (postApply) and the whole call (with the base photo)
  M.lastSwap = { apply = tApply, total = sw() }
  return true
end

-- Detail mode: every layer's base colour = the aerial photo `path` (PNG, row 0 = north, `size` m over the
-- block from its SW corner, converted to samples); nil = the level's flat base. Also gives the level's own material its look (once). The engine
-- re-reads a material on updateMaterial (index = the block's material slot); ~6 ms for a 2048² PNG.
local baseNow = nil
M.looked = {}   -- material name -> the square size its look was applied for
function M.setBase(path, size)
  local tb = M.get()
  if not tb then return false end
  local tex = path or M.LEVEL_BASE.tex
  local sz = path and tostring(toSamples(size or 1024)) or M.LEVEL_BASE.size
  local names = M.slots or { M.MATERIAL }
  for i, name in ipairs(names) do
    local m = scenetree.findObject(name)
    if m then
      -- runtime-made layers: their look once per Lua session (they outlive a Lua reload; a changed square
      -- size changes the sample sizes). The level's materials carry theirs (make_level_assets.py)
      if path and M.runtimeMade[name] and M.looked[name] ~= M.square then applyLook(m, name); M.looked[name] = M.square end
      m:setField('baseColorBaseTex', 0, tex)
      m:setField('baseColorBaseTexSize', 0, sz)
      pcall(function() tb:updateMaterial(i - 1, name) end)
    end
  end
  baseNow = path
  return true
end

function M.base() return baseNow end

-- Flat ground at z=0, centred on the origin, so the player has something to stand on before
-- the first anchor (the session's block size: a 512² session imports the 512² flat map).
function M.loadFlat()
  local x, y = lattice.blockOrigin(0, 0)
  if M.SIZE == 256 then x, y = -M.SIZE * M.SQUARE_SIZE / 2, -M.SIZE * M.SQUARE_SIZE / 2 end
  return M.load(FLAT_HEIGHTMAP, M.SQUARE_SIZE, 50, x, y, 0)
end

-- smallgrid's GroundPlanes stop colliding once a TerrainBlock exists (V1_SPEC), so hide them.
-- setHidden as in levels/smallgrid/mainLevel.lua:27.
function M.hideGroundPlanes()
  for _, name in ipairs(scenetree.findClassObjects('GroundPlane') or {}) do
    local o = scenetree.findObject(name)
    if o and o.setHidden then o:setHidden(true) end
  end
end

function M.delete()
  local tb = M.get()
  if tb then tb:delete() end
end

function M.onLevelChanged()
  materialsChecked = false
end

return M
