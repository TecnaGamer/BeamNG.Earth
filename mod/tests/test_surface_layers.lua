-- luajit mod/tests/test_surface_layers.lua  (terrain.lua with the server's surface layers, --surface on:
-- one mask per material, the materials created as clones of the level's dirt with their ground
-- models, the single dirt layer when the layers are unusable)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local terrain = require('beamngEarth/terrain')

-- the level's own dirt material (loaded from main.materials.json in the game)
local dirt = S.newObject('TerrainMaterial'); dirt:registerObject('beamngEarthDirt')
dirt:setField('baseColorBaseTex', 0, '/levels/beamng_earth/art/terrains/bngEarth_base_b.png')
dirt:setField('groundmodelName', 0, 'DIRT')
local set = S.newObject('TerrainMaterialTextureSet'); set:registerObject(terrain.TEXTURE_SET)

local MATS = { { 'bngEarthGroundAsphalt', 'ASPHALT_OLD' }, { 'bngEarthGroundGrass', 'GRASS' }, { 'bngEarthGroundDusty', 'DIRT_DUSTY' },
  { 'bngEarthGroundLoose', 'DIRT_DUSTY_LOOSE' }, { 'bngEarthGroundRock', 'ROCK' }, { 'bngEarthGroundSnow', 'SNOW' },
  { 'bngEarthGroundMud', 'MUD' }, { 'bngEarthGroundGravelRoad', 'GRAVEL' }, { 'beamngEarthDirt', 'DIRT' } }
local masks = {}
for i = 1, #MATS do masks[i] = '/temp/beamngEarth/s1/hm_1_0_0_m' .. (i - 1) .. '.png' end

T.check(terrain.load('/temp/beamngEarth/s1/hm_1_0_0.png', 2, 4096, -512, -512, 1000, { masks = masks, materials = MATS }),
  'load with surface layers')
local tb = terrain.get()
local imp = tb.lastImport
T.check(#imp.mats == #MATS and #imp.maps == #MATS, 'one material and mask per surface')
for i = 1, #MATS do
  T.check(imp.mats[i] == MATS[i][1] and imp.maps[i].path == masks[i] and imp.maps[i].material == MATS[i][1]
    and imp.maps[i].materialId == i - 1 and imp.maps[i].channel == 'R', 'map ' .. i .. ': mask, material, id')
end
for i = 1, #MATS - 1 do
  local m = scenetree.findObject(MATS[i][1])
  T.check(m and m.class == 'TerrainMaterial' and m.fields.groundmodelName == MATS[i][2]
    and m.fields.internalName == MATS[i][1] and m.fields.baseColorBaseTex == dirt.fields.baseColorBaseTex and m.canSave == false,
    'clone ' .. MATS[i][1] .. ': ground model, textures copied, not saved')
end
T.check(dirt.fields.groundmodelName == 'DIRT', 'the level dirt material untouched')
T.check(terrain.LOOKS.bngEarthGroundGravelRoad.baseColorDetailTex:find('t_gravel_b.png', 1, true) ~= nil,
  'gravel road uses the stock gravel stone texture')
-- a second load reuses the clones
local before = S.calls.createObject_TerrainMaterial or 0
terrain.load('/temp/beamngEarth/s1/hm_1_128_0.png', 2, 4096, -384, -512, 1000, { masks = masks, materials = MATS })
T.check((S.calls.createObject_TerrainMaterial or 0) == before, 'clones made once')

-- a missing mask: the single dirt layer (the block must still import)
local realExists = FS.fileExists
FS.fileExists = function(_, p) return p ~= masks[3] end
T.check(terrain.load('/temp/beamngEarth/s1/hm_1_256_0.png', 2, 4096, -256, -512, 1000, { masks = masks, materials = MATS }),
  'a missing mask still loads')
T.check(#tb.lastImport.mats == 1 and tb.lastImport.mats[1] == 'beamngEarthDirt', '... with the single dirt layer')
FS.fileExists = realExists
-- a material name taken by another kind of object (the weather's Precipitation was 'bngEarthSnow'):
-- the single dirt layer, never a layer bound to nothing (the engine makes those asphalt)
do
  local precip = S.newObject('Precipitation'); precip:registerObject('bngEarthClash')
  function precip:getClassName() return 'Precipitation' end
  local clash = { { 'bngEarthClash', 'SNOW' }, { 'beamngEarthDirt', 'DIRT' } }
  terrain.load('/temp/beamngEarth/s1/hm_1_640_0.png', 2, 4096, 128, -512, 1000, { masks = { masks[1], masks[2] }, materials = clash })
  T.check(#tb.lastImport.mats == 1 and tb.lastImport.mats[1] == 'beamngEarthDirt', 'a name taken by a non-material: the single layer')
end
-- mismatched lists: the single layer
terrain.load('/temp/beamngEarth/s1/hm_1_384_0.png', 2, 4096, -128, -512, 1000, { masks = { masks[1] }, materials = MATS })
T.check(#tb.lastImport.mats == 1, 'masks and materials of different lengths: the single layer')
-- no layers (server --surface off): as before
terrain.load('/temp/beamngEarth/s1/hm_1_512_0.png', 2, 4096, 0, -512, 1000)
T.check(#tb.lastImport.mats == 1 and tb.lastImport.mats[1] == 'beamngEarthDirt', 'no layers: the single dirt layer')

-- groundcover: objects from the stock definitions with CPU placement, and the same-tick rebuild after a
-- block change (every import / move drops all the grass on the block, in-game 2026-10-11)
do
  _G.jsonNull = _G.jsonNull or setmetatable({}, { __tostring = function() return 'null' end })
  local gc = require('beamngEarth/groundcover')
  local flushes = 0
  _G.flushGroundCoverGrids = function() flushes = flushes + 1 end
  T.check(gc.afterBlockChange() == false and flushes == 0, 'no grass objects: no rebuild')
  -- one items-file line per stock definition; jsonDecode stubbed by line
  local lines, byLine = {}, {}
  for _, d in ipairs(gc.DEFS) do
    local line = '{"class":"GroundCover","name":"' .. d.src .. '"}'
    lines[#lines + 1] = line
    byLine[line] = { name = d.src, class = 'GroundCover', material = 'm_' .. d.src, maxElements = 1000,
      useNewPlacement = true, useNewPlacementGpu = true, Types = { { layer = 'Grass', billboardUVs = { 0, 0, 1, 1 } } } }
  end
  local realDecode = _G.jsonDecode
  _G.jsonDecode = function(line) return byLine[line] end
  local realRead = _G.readFile
  _G.readFile = function(p)
    for _, src in pairs(gc.SOURCES) do if p == src.items then return table.concat(lines, '\n') end end
    return realRead and realRead(p)
  end
  T.check(gc.ensure() == true, 'ensure makes the objects')
  local o = scenetree.findObject(gc.PREFIX .. gc.DEFS[1].src)
  T.check(o and o.fields.useNewPlacement == '1' and o.fields.useNewPlacementGpu == '0', 'CPU placement over the stock GPU setting')
  T.check(o.fields.layer == gc.DEFS[1].layer and o.canSave == false, 'on our layer, not saved')
  for _, d in ipairs(gc.DEFS) do
    if d.fields then
      local od = scenetree.findObject(gc.PREFIX .. (d.name or d.src))
      local same = od ~= nil
      for k, v in pairs(d.fields) do same = same and od.fields[k] == tostring(v) end
      T.check(same, (d.name or d.src) .. ': field overrides applied')
    end
  end
  T.check(gc.status().objects == #gc.DEFS, 'one object per definition')
  T.check(gc.afterBlockChange() == true and flushes == 1, 'a block change rebuilds the grass in the same tick')
  -- a GE Lua reload: the module starts over, the level's objects stay (here with an old field)
  o.fields.useNewPlacementGpu = '1'
  local matLoads, realLoad = {}, _G.loadJsonMaterialsFile
  _G.loadJsonMaterialsFile = function(p) matLoads[#matLoads + 1] = p end
  gc.reset()
  T.check(gc.ensure() == true, 'ensure after a module reload')
  -- the level started again: its stock grass materials went with the old one and are loaded again
  T.check(#matLoads >= 1, 'the stock grass materials loaded again after a reset (' .. #matLoads .. ' files)')
  _G.loadJsonMaterialsFile = realLoad
  local o2 = scenetree.findObject(gc.PREFIX .. gc.DEFS[1].src)
  T.check(o2 ~= o and o2.fields.useNewPlacementGpu == '0' and scenetree.findObjectById(o.id) == nil,
    'a leftover object is made again from the current definition')
  -- teleport / rebase: the objects made again (fresh cells whatever the engine kept), generated now
  local before = scenetree.findObject(gc.PREFIX .. gc.DEFS[1].src)
  local fl = flushes
  T.check(gc.rebuild() == true and flushes == fl + 1, 'rebuild: generated in the same tick')
  local after = scenetree.findObject(gc.PREFIX .. gc.DEFS[1].src)
  T.check(after ~= nil and after ~= before and gc.status().objects == #gc.DEFS, 'rebuild: new objects, one per definition')
  -- the settings page: density remakes the objects with the factor; off / on act at once
  local n1 = tonumber(scenetree.findObject(gc.PREFIX .. gc.DEFS[1].src).fields.maxElements)
  T.check(gc.setDensity('low') == true and gc.status().density == 0.5, 'density low')
  local n2 = tonumber(scenetree.findObject(gc.PREFIX .. gc.DEFS[1].src).fields.maxElements)
  T.check(math.abs(n2 - n1 / 2) <= 1, 'density low: half the elements (' .. tostring(n1) .. ' -> ' .. tostring(n2) .. ')')
  T.check(gc.setDensity('nonsense') == false and gc.status().density == 0.5, 'unknown density refused')
  gc.setDensity('normal')
  gc.setEnabled(false)
  T.check(gc.status().objects == 0, 'off: objects gone')
  fl = flushes
  gc.setEnabled(true)
  T.check(gc.status().objects == #gc.DEFS and flushes == fl + 1, 'on again: made at once on the layered block')
  gc.setEnabled(false)
  T.check(gc.rebuild() == false, 'rebuild without grass: nothing')
  T.check(gc.status().objects == 0 and gc.afterBlockChange() == false, 'disabled: objects gone, no rebuild')
  gc.setEnabled(true)
  gc.reset()
  _G.readFile, _G.jsonDecode = realRead, realDecode
end

-- groundcover.fieldsFor: a stock definition re-pointed at our layer, its element count scaled
do
  local gc = require('beamngEarth/groundcover')
  local def = { name = 'g', class = 'GroundCover', persistentId = 'x', __parent = 'veg', position = { 1, 2, 3 },
    material = 'm_grass', radius = 50, maxElements = 306000, shapesCastShadows = false, windGustLength = jsonNull,
    Types = { { layer = 'Grass', billboardUVs = { 0, 0, 1, 0.5 }, probability = jsonNull, sizeMin = 0.15 },
              { layer = 'RockyDirt', sizeMax = 0.5 }, { billboardUVs = jsonNull } } }
  local calls = gc.fieldsFor(def, 'bngEarthGroundGrass', 0.5)
  local f = {}
  for _, c in ipairs(calls) do f[c[1] .. '#' .. c[2]] = c[3] end
  T.check(f['material#0'] == 'm_grass' and f['radius#0'] == '50' and f['maxElements#0'] == '153000', 'fields copied, elements scaled')
  T.check(f['shapesCastShadows#0'] == '0' and f['windGustLength#0'] == nil, 'booleans as 0/1, nulls left out')
  T.check(f['name#0'] == nil and f['position#0'] == nil and f['persistentId#0'] == nil and f['Types#0'] == nil, 'identity fields left out')
  T.check(f['layer#0'] == 'bngEarthGroundGrass' and f['layer#1'] == 'bngEarthGroundGrass' and f['layer#2'] == nil,
    'every plant type on our layer; an empty type stays empty')
  T.check(f['billboardUVs#0'] == '0 0 1 0.5' and f['sizeMin#0'] == '0.15' and f['probability#0'] == nil and f['sizeMax#1'] == '0.5',
    'type fields by index')
  local again = gc.fieldsFor(def, 'bngEarthGroundGrass', 0.5)
  local same = #again == #calls
  for i = 1, #calls do same = same and again[i][1] == calls[i][1] and again[i][2] == calls[i][2] and again[i][3] == calls[i][3] end
  T.check(same, 'deterministic order')
  T.check(gc.fieldsFor({ maxElements = 10 }, 'x', 0.01)[1][3] == '1', 'at least one element')
  local names = {}
  local unique = true
  for _, d in ipairs(gc.DEFS) do
    local n = d.name or d.src
    unique = unique and not names[n]
    names[n] = true
  end
  T.check(unique, 'one object name per definition (a stock one on two layers gets its own name)')
end

T.done('test_surface_layers')
