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
M.BASE_TEX_SIZE = 2048   -- = the level texture set's base slot (tools/make_level_assets.py BASE_TEX): the size in use
-- The ground photo sizes (the graphics setting groundPhoto; server earthd.BASE_SIZES): the level ships the flat base
-- maps of each (tools/make_level_assets.py: 2048's under the plain names, the others with a _<px> suffix). The size is
-- chosen before a level's first block (setPhotoSize; the texture set, every layer's base maps and the block's
-- baseTexSize follow it at getOrCreate); a change while the block exists waits for the next level load.
M.BASE_DEFAULT = 2048
M.BASE_SIZES = { [1024] = true, [2048] = true, [4096] = true }
local wantBase = M.BASE_DEFAULT

local TERRAIN_DIR = '/levels/beamng_earth/art/terrains/'
local TERRAIN_DIR_BASE = TERRAIN_DIR .. 'bngEarth_base_b.png'   -- the level's flat base colour
local FLAT_H = TERRAIN_DIR .. 'bngEarth_flat_h_1024.png'          -- neutral height (detail / macro slots)
local MATERIALS_FILE = TERRAIN_DIR .. 'main.materials.json'
-- per block size (tools/make_level_assets.py): all white = material 0 everywhere; all zero, 16-bit
local LAYER_MAPS = { [1024] = TERRAIN_DIR .. 'beamngEarth_layer_1024.png', [512] = TERRAIN_DIR .. 'beamngEarth_layer_512.png' }
local FLAT_HEIGHTMAPS = { [1024] = TERRAIN_DIR .. 'beamngEarth_flat_1024.png', [512] = TERRAIN_DIR .. 'beamngEarth_flat_512.png' }
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
M.seenMats = {}         -- material names some block of this session listed (M.swap)
M.looked = {}   -- material name -> the square size its look was applied for
-- (M.swap) Off: the rebind was a guess at GitHub #115's stripes, whose cause was the terrain cache's mips (server
-- bc7.dds, 4ad1642). 1.6.0 kept it on Windows: a second rebuild of every slot's textures after each block swap (Deck
-- A/B 2026-10-23, Baltimore at 67 mph: the commit's next frame 127 ms with it, 92 without; a GTX 1060 player's
-- commits, GitHub #180: 250-340 ms of RenderShadows on every other one)
M.REBIND_AFTER_SWAP = false
local baseNow = nil
local function toSamples(metres) return math.max(1, math.floor(metres / M.square + 0.5)) end

local function applyLook(m, name)
  local l = M.LOOKS[name]
  if not l then return end
  for k, v in pairs(l) do
    if k:sub(-7) == 'TexSize' then v = toSamples(v) end
    pcall(function() m:setField(k, 0, tostring(v)) end)
  end
end

local function baseFields(path, size)
  return path or M.LEVEL_BASE.tex, path and tostring(toSamples(size or 1024)) or M.LEVEL_BASE.size
end

local function setMaterialBase(m, name, path, tex, size)
  -- Runtime-made layers need their look once per square size. Level materials carry theirs.
  if path and M.runtimeMade[name] and M.looked[name] ~= M.square then applyLook(m, name); M.looked[name] = M.square end
  m:setField('baseColorBaseTex', 0, tex)
  m:setField('baseColorBaseTexSize', 0, size)
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
function M.configure(size)
  local n = LAYER_MAPS[size] and size or 1024
  M.SIZE, LAYER_MAP, FLAT_HEIGHTMAP = n, LAYER_MAPS[n], FLAT_HEIGHTMAPS[n]
  return n
end

-- The block casts no sun shadows. Every block move redrew the sun's cached shadow map for it: on a Steam Deck Fells
-- Point drive (2026-10-24) 11 of 27 frames with RenderShadows >= 25 ms came within 3 frames of a swap, 112x the swaps'
-- share of frames; with castShadows 0 none of 20 did, shadow hitches 34 -> 18. It drew nothing into that map: with the
-- block drawn (detail mode), screenshots after a forced redraw were pixel-identical with and without, the redraw
-- itself as dear (PC 8.4 / 7.7 ms vs 8.4 / 7.5), the floor of Yosemite Valley sunlit near sunset either way; trees,
-- roads, buildings and the far meshes cast the shadows.
local function getOrCreate()
  local tb = M.get()
  if tb then return tb end
  ensureMaterials()
  M.applyBaseSize(wantBase)      -- (the settings' ground photo size, before the block: it sizes its base textures)
  tb = TerrainBlock()
  tb:setName(M.NAME)
  -- the resolution the engine bakes the block's base colour at (default 1024; West Coast USA's block 2048;
  -- set before registerObject: the engine sizes it when the block is added)
  tb:setField('baseTexSize', 0, tostring(M.BASE_TEX_SIZE))
  tb:setField('castShadows', 0, '0')     -- (no sun shadows: see above)
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

-- Pure: the level flat base map `path` at ground photo size n (another path: as it is).
function M.baseMapFor(path, n)
  if type(path) ~= 'string' then return path end
  local stem = path:match('^(.*/bngEarth_base_%a+)_%d+%.png$') or path:match('^(.*/bngEarth_base_%a+)%.png$')
  if not stem then return path end
  return n == M.BASE_DEFAULT and (stem .. '.png') or (stem .. '_' .. tostring(n) .. '.png')
end

-- The ground photo size to use (the settings page's groundPhoto): at once when no block exists, else from the next
-- level's first block. Returns the size the server should build at now (photoSize).
function M.setPhotoSize(n)
  n = tonumber(n)
  if n and M.BASE_SIZES[n] then wantBase = n end
  return M.photoSize()
end

-- The ground photo size of the block now, or the one the next block will have (none yet).
function M.photoSize()
  if M.get() then return M.BASE_TEX_SIZE end
  return wantBase
end

local BASE_MAP_FIELDS = { 'aoBaseTex', 'baseColorBaseTex', 'heightBaseTex', 'normalBaseTex', 'roughnessBaseTex' }
-- The texture set's base slot and our layers' flat base maps at size n (before the block exists: the engine sizes
-- the block's base textures when it is added; a map unlike the slot is resized at every material rebuild).
-- Returns how many fields changed (0: already so).
function M.applyBaseSize(n)
  local changed = 0
  local set = scenetree.findObject(M.TEXTURE_SET)
  local want = tostring(n) .. ' ' .. tostring(n)
  if set then
    local ok, cur = pcall(function() return set:getField('baseTexSize', 0) end)
    if not ok or tostring(cur) ~= want then
      pcall(function() set:setField('baseTexSize', 0, want) end)
      changed = changed + 1
    end
  end
  local names = scenetree.findClassObjects and scenetree.findClassObjects('TerrainMaterial') or {}
  for _, name in ipairs(names) do
    if name == M.MATERIAL or tostring(name):sub(1, 8) == 'bngEarth' then
      local m = scenetree.findObject(name)
      for _, f in ipairs(BASE_MAP_FIELDS) do
        local ok, v = pcall(function() return m:getField(f, 0) end)
        local nv = ok and M.baseMapFor(v, n) or nil
        if nv and nv ~= v then m:setField(f, 0, nv); changed = changed + 1 end
      end
    end
  end
  M.BASE_TEX_SIZE = n
  M.LEVEL_BASE.tex = M.baseMapFor(TERRAIN_DIR_BASE, n)
  if changed > 0 then
    log('I', 'beamngEarth', string.format('terrain: ground photo %d px (%d fields set)', n, changed))
  end
  return changed
end

-- Apply the render flag to the block. Terrain mode never calls the engine unless the flag was off
-- before (keeps the v1.2 path untouched).
local function applyRender(tb, force)
  if (force or not renderEnabled) and tb.setRenderEnabled then tb:setRenderEnabled(renderEnabled) end
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
function M.load(path, squareSize, maxHeight, x, y, z, layers, base, baseSize)
  if not FS:fileExists(path) then return false, 'heightmap not found: ' .. tostring(path) end
  M.square = tonumber(squareSize) or M.square
  local tb = getOrCreate()
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
  tb:setField('castShadows', 0, '0')     -- (no sun shadows: kept across imports, set again to be sure)
  tb:setPosition(vec3(x, y, z))
  applyRender(tb) -- mesh mode: hidden again after every import
  if base ~= nil then M.setBase(base or nil, baseSize) end
  return true
end

-- Move the block by swapping in the server's .ter of the new block (terrainFile + postApply, a new file
-- name each time: the engine skips a name it already has). ~11 ms for 512², and collision follows by the
-- next frame with no reloadCollision (docs/research/2026-10-12-engine-limits-and-worldgen.md §7) against
-- importMaps (~20 ms) + reloadCollision (~10 ms Lua + a ~20 ms physics frame). Only after a first
-- importMaps (the block exists with its material set-up) and at the same square size: returns false
-- (nothing changed) otherwise, and the caller imports. The .ter names its materials (server
-- surface.write_ter: layers.materials, or M.MATERIAL alone). The caller does NOT rebuild collision.
-- Fine ground (server finepatch.py, BNE_FINE=on): a block's sharp graded cells are holes in its .ter, filled by a 0.5 m
-- mesh of the same ground in pieces (fine.parts: {shape, pos, slot}, colliding), each cell drawn with its surface
-- layer's material (fine.materials: {name, layer}; a bank of four by block, finepatch.bank): the block's aerial photo
-- (its roads painted in; the pieces' UV0 spans the block from its south-west corner) and the layer's detail texture on
-- UV1 (world-locked), as the terrain draws it. The pieces are made in the frame the .ter goes in (M.swap: a hole never
-- shows without its fill, a car never meets one), the old block's deleted right after, and the static collision
-- reloaded (it applies the next frame, with the .ter's).
M.fine = { ids = {}, fine = nil, bank = nil }
local fineMade = 0

-- The game's packed copy of the block photo (its terrain texture cache, sRGB BC7, written by the server:
-- terraincache.base_entry; named by xxh64 of the photo's path three times and the layers' ao map's), or nil. The PBR
-- material reads the photo's PNG as linear colour (the pieces washed out white, 2026-11-01); this file it decodes as the
-- terrain does.
local function fineDds(base, layer)
  if type(base) ~= 'string' then return nil end
  local tm = type(layer) == 'string' and scenetree.findObject(layer) or nil
  local ok, ao = pcall(function() return tm:getField('aoBaseTex', 0) end)
  if not ok or type(ao) ~= 'string' or ao == '' then return nil end
  local p = '/temp/art/terrainmaterialcache/' .. require('beamngEarth/xxh64').hex(base .. base .. base .. ao) .. '.dds'
  return FS:fileExists(p) and p or nil
end

local function fineTexture(m, base, layer)
  local satmat = require('beamngEarth/satmat')
  local dds = fineDds(base, layer)
  if dds then
    local layers = tostring(m:getField('activeLayers', 0))
    m:setField('version', 0, '1.5')
    satmat._setTexture(m, dds, false)
    m:setField('baseColorMap', 1, dds)      -- (the macro layer's photo: fineMaterial)
    if layers == '2' then m:setField('activeLayers', 0, '2') end     -- (_setTexture set one)
  elseif type(base) == 'string' and FS:fileExists(base) then
    m:setField('version', 0, '1')      -- (the PNG: the legacy material, which reads it as the terrain does)
    satmat._setTexture(m, base, false)
  else
    m:setField('diffuseColor', 0, satmat.FALLBACK_COLOR)
  end
end

-- A fine material: the terrain's look (the engine's terrain shader, material/shadergen/terrainMat + terrain.h): the block's
-- photo plus its surface layer's detail colour (2 x detail - 1, at the layer's near strength: the mesh shader adds
-- its detail alike), the layer's detail normal, roughness and ao over the level's flat base maps, on the same
-- texture coordinates (the server's UV1: block-local metres / 2 from half a sample on). The user, 2026-11-01: "make the
-- texture of the finer mesh perfectly blend in and match with the normal terrain" (the satmat grain and a fixed
-- normal strength drew it a shade off, in squares). No layer: satmat's ground grain. Not matched: the macro textures
-- (subtle, 64 m) and the detail's fade with distance (the mesh's strengths are the terrain's at FINE_LOOK_M).
M.FINE_ROUGHNESS_BASE = TERRAIN_DIR .. 'bngEarth_base_r.png'
M.FINE_AO_BASE = TERRAIN_DIR .. 'bngEarth_base_ao.png'
-- (a strength pair "near far" as the terrain shader takes it at FINE_LOOK_M from the camera with the layer's
-- detailDistances "a b c d": near up to b, toward far by c, far from d: terrain.h's calculateStrenth)
M.FINE_LOOK_M = 20
M.FINE_MACRO = true
local function nums(v)
  local o = {}
  for x in tostring(v or ''):gmatch('[%d%.%-]+') do o[#o + 1] = tonumber(x) end
  return o
end
local function strengthAt(v, dist, default)
  local s, d = nums(v), nums(dist)
  if not s[1] then return default end
  local nearS, farS = s[1], s[2] or s[1]
  if #d < 4 then return nearS end
  local x = M.FINE_LOOK_M
  local sat = function(a) return math.max(0, math.min(1, a)) end
  local mid = d[3] > d[2] and sat((x - d[2]) / (d[3] - d[2])) or 1
  return nearS + (farS - nearS) * mid
end

-- name -> the photo its fine material was made with (fineMaterial)
local finePhoto = {}
local stage    -- (fineStep's pieces made ahead: below)

local function fineMaterial(name, layer, base, rim)
  local satmat = require('beamngEarth/satmat')
  local m = scenetree.findObject(name)
  if m and finePhoto[name] == (base or false) then return m end
  -- (another photo: a material no shown piece uses (another bank than the shown block's: finepatch.bank) is made
  -- again, its textures from the engine's cache; reloading one re-read every texture from disk, the detail maps' PNGs
  -- too: ~200 ms a material, 2-4 s a block swap on the Stelvio, 2026-11-01. One in use is reloaded)
  -- (in use: the shown block's bank, or the staged one's once it has pieces; deleting a Material a piece draws with
  -- crashed the game, 2026-11-01: a staged block's photo replaced by its full one before its swap)
  local bank = tonumber(tostring(name):match('^bngEarthFine(%d+)_'))
  local staged = stage and stage.fine and stage.fine.bank == bank and next(stage.ids) ~= nil
  if m and bank ~= M.fine.bank and not staged then
    m:delete()
    m = nil
  end
  finePhoto[name] = base or false
  local fresh = not m
  if fresh then
    m = createObject('Material')
    m:setField('mapTo', 0, name)
  end
  -- (the PBR material, as the terrain's, with the photo's sRGB copy: the legacy one (version 1) adds its detail map at
  -- full strength and in gamma space, the pieces a shade darker and grainier than the terrain beside them, 2026-11-01)
  fineTexture(m, base, layer)
  if fresh then
    for _, f in ipairs(satmat.FIELDS) do m:setField(f[1], 0, f[2]) end
    local tm = type(layer) == 'string' and scenetree.findObject(layer) or nil
    local get = function(f) local ok, v = pcall(function() return tm:getField(f, 0) end) return ok and v or '' end
    local btex = tm and get('baseColorDetailTex') or ''
    if btex ~= '' then
      -- (detail sizes in samples: one repeat every size x square metres; UV1 is metres / 2)
      local size = math.max(tonumber(get('baseColorDetailTexSize')) or 1, 0.25) * M.square
      local sc = string.format('%.5f %.5f', 2 / size, 2 / size)
      -- the macro texture (the terrain's large light and dark patches over 32-64 m) as a second layer at half opacity:
      -- the mesh shader lerps its layers, so each one's strengths doubled sum to the terrain's detail + macro (the
      -- user, 2026-11-01: "the mesh dose not have the fake shadow effect like the normal ground")
      local mtex = get('baseColorMacroTex')
      local k = (mtex ~= '' and M.FINE_MACRO) and 2 or 1
      local function S(field, dflt) return tostring(k * strengthAt(get(field), get('detailDistances'), dflt)) end
      m:setField('detailMap', 0, btex)
      m:setField('detailBaseColorMapStrength', 0, S('baseColorDetailStrength', 0.25))
      m:setField('detailMapUseUV', 0, '1')
      m:setField('detailScale', 0, sc)
      local ntex = get('normalDetailTex')
      if ntex ~= '' then
        m:setField('detailNormalMap', 0, ntex)
        m:setField('normalDetailMapUseUV', 0, '1')
        m:setField('detailNormalMapStrength', 0, S('normalDetailStrength', 1))
      end
      local rtex = get('roughnessDetailTex')
      m:setField('roughnessFactor', 0, '1')
      m:setField('roughnessMap', 0, M.FINE_ROUGHNESS_BASE)
      if rtex ~= '' then
        m:setField('roughnessDetailMap', 0, rtex)
        m:setField('detailRoughnessMapStrength', 0, S('roughnessDetailStrength', 1))
      end
      local atex = get('aoDetailTex')
      m:setField('ambientOcclusionMap', 0, M.FINE_AO_BASE)
      if atex ~= '' then
        m:setField('ambientOcclusionDetailMap', 0, atex)
        m:setField('detailAoMapStrength', 0, S('aoDetailStrength', 0.8))
      end
      if k == 2 then
        local msize = math.max(tonumber(get('baseColorMacroTexSize')) or 32, 1) * M.square
        local ms = tostring(2 * strengthAt(get('baseColorMacroStrength'), get('macroDistances'), 0.12))
        local f1 = { { 'opacityFactor', '0.5' }, { 'useAnisotropic', '1' }, { 'roughnessFactor', '1' },
          { 'roughnessMap', M.FINE_ROUGHNESS_BASE }, { 'ambientOcclusionMap', M.FINE_AO_BASE },
          { 'detailMap', mtex }, { 'detailBaseColorMapStrength', ms }, { 'detailMapUseUV', '1' },
          { 'detailScale', string.format('%.5f %.5f', 2 / msize, 2 / msize) } }
        for _, f in ipairs(f1) do m:setField(f[1], 1, f[2]) end
        m:setField('activeLayers', 0, '2')
      end
    else
      -- (satmat's neutral ground grain, mean 0.5 grey)
      m:setField('detailMap', 0, satmat.DETAIL_MAP)
      for _, f in ipairs(satmat.DETAIL_FIELDS) do m:setField(f[1], 0, f[2]) end
    end
    -- (a rim piece: drawn 1 cm over the terrain's own pale squares; its edges cast lines of shadow on the road)
    if rim then m:setField('castShadows', 0, '0') end
    m.canSave = false
    m:registerObject(name)
  else
    pcall(function() m:flush() end)
    pcall(function() m:reload() end)
  end
  return m
end

-- A block's fine materials ({{name, layer}, ...}) with its photo.
local function fineMaterials(fine, base)
  for _, e in ipairs(type(fine) == 'table' and type(fine.materials) == 'table' and fine.materials or {}) do
    if type(e) == 'table' and type(e[1]) == 'string' then fineMaterial(e[1], e[2], base, e[3] == true) end
  end
end

-- pooled pieces (server pool_shapes: slot_<n> paths): object id -> its slot, let go with the object
local pieceSlot = {}
local function pooled() return require('beamngEarth/roads') end
local function partSlot(p) return p.slot and pooled().pooledSlot(p.shape, p.slot) or nil end

-- A shown piece's collision: its own collision meshes ("Collision Mesh": the server's split into small meshes, built
-- fast on Windows), else its drawn mesh; none for a rim piece (collide false)
local function pieceCollision(p)
  if p.collide == false then return 'None' end
  return p.collision == 'mesh' and 'Collision Mesh' or 'Visible Mesh Final'
end

local function finePiece(p, staged)
  if type(p.shape) ~= 'string' or type(p.pos) ~= 'table' or not FS:fileExists(p.shape) then
    pooled().pooledRelease(partSlot(p), nil)
    return nil
  end
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, p.shape)
  -- (made ahead, staged: hidden and not colliding until its block goes in: its ground differs a little from the block
  -- in place over the same cells)
  o:setField('collisionType', 0, staged and 'None' or pieceCollision(p))
  o:setField('decalType', 0, 'Visible Mesh')
  o:setField('dynamic', 0, 'true')
  o.canSave = false
  -- (staged: drawn, FINE_STAGE_DROP_M under its place, under the ground shown there: a new shape's first draw is
  -- ~11 ms (its buffers, its materials), all of them in the swap's frame when made hidden (1.3 s of RenderShadows on
  -- the Stelvio, 2026-11-01); the shadow pass round the camera draws them now, one a frame)
  o:setPosition(vec3(p.pos[1], p.pos[2], p.pos[3] - (staged and M.FINE_STAGE_DROP_M or 0)))
  o:registerObject('')
  fineMade = fineMade + 1
  local id = o:getID()
  local sl = partSlot(p)
  if sl then
    pooled().pooledMade(id, p.shape)
    pieceSlot[id] = sl
  end
  return id
end

local function pieceDelete(id)
  local o = scenetree.findObjectById(id)
  if o then o:delete() end
  local sl = pieceSlot[id]
  if sl then
    pieceSlot[id] = nil
    pooled().pooledRelease(sl, id)
  end
end

local function fineValid(fine)
  return type(fine) == 'table' and type(fine.parts) == 'table' and type(fine.materials) == 'table'
end

-- The pieces of the next block, made a few a frame before its swap (fineStep: each new shape ~2 ms to load; all at
-- the swap held its frame 140 ms on the Stelvio, 2026-11-01): { key (its .ter), fine, ids {part index: id}, next }.
stage = nil      -- (declared before fineMaterial, which asks whether the staged pieces use a bank)
M.FINE_STEP_MS = 4
M.FINE_STAGE_DROP_M = 4

local function stageDrop()
  if not stage then return end
  for i, p in ipairs(stage.fine.parts) do
    local id = stage.ids[i]
    if id then pieceDelete(id)
    elseif id == nil then pooled().pooledRelease(partSlot(p), nil) end   -- (never made: its slot goes now)
  end
  stage = nil
end

-- A block arrived (terrain_ready, before its commit): its pieces are made ahead (fineStep).
-- sameBlock: the shown block's own refresh (core: the graded block after a teleport's provisional one), whose
-- materials are the shown ones (its photo alike: fineMaterial keeps them)
function M.finePrepare(msg, sameBlock)
  if type(msg) ~= 'table' or not fineValid(msg.fine) or type(msg.ter) ~= 'string' then return end
  M.finePrepared = msg.ter
  if stage and stage.key == msg.ter then return end
  stageDrop()
  -- (its materials take its photo now: a bank of their own, never the shown block's, finepatch.bank; a block in the
  -- shown one's bank (a far jump) is made at its swap instead)
  if M.fine.bank ~= nil and msg.fine.bank == M.fine.bank and not sameBlock then return end
  fineMaterials(msg.fine, type(msg.base) == 'string' and msg.base or nil)
  stage = { key = msg.ter, fine = msg.fine, ids = {}, next = 1 }
end

-- Make staged pieces for up to M.FINE_STEP_MS this frame (core.lua's update).
function M.fineStep()
  if not stage or stage.next > #stage.fine.parts then return end
  local sw = stopwatch.new()
  -- (one new shape a frame at most: its making and its first draw each ~11 ms)
  local made = 0
  while stage.next <= #stage.fine.parts and (made == 0 or sw() < M.FINE_STEP_MS) and made < 1 do
    local i = stage.next
    stage.ids[i] = finePiece(stage.fine.parts[i], true) or false
    stage.next = i + 1
    made = made + 1
  end
end

-- The block's fine pieces made (TSStatic ids, shown and colliding): the staged ones of this .ter, the rest now.
local function fineMake(fine, base, terPath)
  local ids = {}
  if not fineValid(fine) then stageDrop(); return ids end
  fineMaterials(fine, base)
  local st_ = (stage and stage.key == terPath) and stage or nil
  -- (a swap without its pieces made ahead: what was staged instead, for the log; core's commit line)
  M.fineMiss = (not st_) and { ter = terPath, staged = stage and stage.key or false, bank = fine.bank,
    shown = M.fine.bank, prepared = M.finePrepared } or nil
  for i, p in ipairs(fine.parts) do
    local id = st_ and st_.ids[i]
    local o = id and scenetree.findObjectById(id)
    if o then
      o.hidden = false
      o:setPosition(vec3(p.pos[1], p.pos[2], p.pos[3]))
      if p.collide ~= false then o:setField('collisionType', 0, pieceCollision(p)) end
      ids[#ids + 1] = id
      st_.ids[i] = nil
      -- (this message's own slot for it unused: the staged piece came from the earlier message's)
      if st_.fine ~= fine then pooled().pooledRelease(partSlot(p), nil) end
    else
      local nid = finePiece(p, false)
      if nid then ids[#ids + 1] = nid end
    end
  end
  M.fineStaged = st_ and (st_.next - 1) or 0
  if st_ then stage = nil end
  stageDrop()
  return ids
end

local function fineDelete(ids)
  for _, id in ipairs(ids or {}) do pieceDelete(id) end
end

-- The current block's photo changed (setBase: its stand-in, then its full photo): the fine pieces' too.
local function fineBase(base)
  if M.fine.fine then fineMaterials(M.fine.fine, base) end
end

-- Are the pieces of .ter path `ter` all made ahead (or has it none to make)?
function M.fineReady(ter, fine)
  if not fineValid(fine) then return true end
  return stage ~= nil and stage.key == ter and stage.next > #stage.fine.parts
end

function M.fineStatus()
  return { pieces = #M.fine.ids, bank = M.fine.bank, made = fineMade, staged = stage and (stage.next - 1) or 0 }
end

function M.swap(terPath, squareSize, maxHeight, x, y, z, layers, base, baseSize, fine)
  local tb = M.get()
  local sq = tonumber(squareSize)
  if not tb or not M.slots or not sq or math.abs(sq - M.square) > 1e-9 then return false end
  if type(terPath) ~= 'string' or not FS:fileExists(terPath) then return false end
  if tb:getField('terrainFile', 0) == terPath then return false end -- no reload: the caller must import/rebind
  local slots = { M.MATERIAL }
  if layers then
    if type(layers.materials) ~= 'table' or #layers.materials == 0 or not ensureSurfaceMaterials(layers.materials) then
      return false
    end
    slots = {}
    for i, nm in ipairs(layers.materials) do slots[i] = nm[1] end
  end
  local sw = stopwatch.new()
  local saved = {}
  local fineIds = fineMake(fine, base or nil, terPath)      -- (with the .ter: its holes filled from the first frame)
  local tFine = sw()
  local ok, err = pcall(function()
    if base ~= nil then
      local path = base or nil
      local tex, size = baseFields(path, baseSize)
      for _, name in ipairs(slots) do
        local m = scenetree.findObject(name)
        if m then
          if not saved[name] then
            saved[name] = { material = m, tex = m:getField('baseColorBaseTex', 0),
              size = m:getField('baseColorBaseTexSize', 0), looked = M.looked[name] }
          end
          setMaterialBase(m, name, path, tex, size)
        end
      end
    end
    tb:setField('maxHeight', 0, tostring(maxHeight))
    tb:setPosition(vec3(x, y, z))
    -- The file setter binds all material slices itself. Prepare their photo first: updateMaterial
    -- afterwards dirties the shared texture arrays again (~30 ms extra deferred render work in the
    -- stationary 0.40 probe, with identical checker appearance across all eight surface layers).
    tb:setField('terrainFile', 0, terPath)
    tb:postApply()
  end)
  local tApply = sw()
  if not ok then
    for name, old in pairs(saved) do
      pcall(function() old.material:setField('baseColorBaseTex', 0, old.tex) end)
      pcall(function() old.material:setField('baseColorBaseTexSize', 0, old.size) end)
      M.looked[name] = old.looked
    end
    log('W', 'beamngEarth', 'terrain .ter swap failed: ' .. tostring(err))
    fineDelete(fineIds)
    return false
  end
  fineDelete(M.fine.ids)
  local hadFine = #M.fine.ids > 0
  M.fine = { ids = fineIds, fine = fineValid(fine) and fine or nil, bank = fineValid(fine) and fine.bank or nil }
  if #fineIds > 0 or hadFine then be:reloadCollision() end    -- (next frame, with the .ter's own)
  -- the materials this block lists that no block of this session listed before (a first use costs the engine
  -- its textures' load: hitch diagnosis, core's commit log)
  local fresh = {}
  for _, nm in ipairs(slots) do
    if not M.seenMats[nm] then fresh[#fresh + 1] = nm; M.seenMats[nm] = true end
  end
  M.slots = slots
  if base ~= nil then baseNow = base or nil end
  M.swaps = (M.swaps or 0) + 1
  applyRender(tb)
  -- Windows: every slot rebound after the swap again, as 1.5.4 did (~18 ms more per swap; REBIND_AFTER_SWAP).
  if base ~= nil and M.REBIND_AFTER_SWAP then M.setBase(base or nil, baseSize) end
  -- ms of the last swap: the base fields' preparation and the .ter's application, then the whole call; its materials
  M.lastSwap = { apply = tApply, total = sw(), mats = #slots, fresh = #fresh > 0 and fresh or nil,
    fine = #fineIds > 0 and tFine or nil, fineStaged = #fineIds > 0 and M.fineStaged or nil }
  return true
end

-- Detail mode: every layer's base colour = the aerial photo `path` (PNG, row 0 = north, `size` m over the
-- block from its SW corner, converted to samples); nil = the level's flat base. Also gives the level's own material its look (once). The engine
-- re-reads a material on updateMaterial (index = the block's material slot); ~6 ms for a 2048² PNG.
function M.setBase(path, size)
  local tb = M.get()
  if not tb then return false end
  local tex, sz = baseFields(path, size)
  local names = M.slots or { M.MATERIAL }
  for i, name in ipairs(names) do
    local m = scenetree.findObject(name)
    if m then
      setMaterialBase(m, name, path, tex, sz)
      pcall(function() tb:updateMaterial(i - 1, name) end)
    end
  end
  baseNow = path
  fineBase(path)
  return true
end

function M.base() return baseNow end

-- Flat ground at z=0, centred on the origin, so the player has something to stand on before
-- the first anchor (the session's block size: a 512² session imports the 512² flat map).
function M.loadFlat()
  local x, y = lattice.blockOrigin(0, 0)
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
  stageDrop()
  fineDelete(M.fine.ids)
  M.fine = { ids = {}, fine = nil, bank = nil }
  local tb = M.get()
  if tb then tb:delete() end
end

function M.onLevelChanged()
  materialsChecked = false
end

return M
