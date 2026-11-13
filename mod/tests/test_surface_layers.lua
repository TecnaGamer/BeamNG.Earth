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
  { 'bngEarthGroundMud', 'MUD' }, { 'beamngEarthDirt', 'DIRT' } }
local masks = {}
for i = 1, 8 do masks[i] = '/temp/beamngEarth/s1/hm_1_0_0_m' .. (i - 1) .. '.png' end

T.check(terrain.load('/temp/beamngEarth/s1/hm_1_0_0.png', 2, 4096, -512, -512, 1000, { masks = masks, materials = MATS }),
  'load with surface layers')
local tb = terrain.get()
local imp = tb.lastImport
T.check(#imp.mats == 8 and #imp.maps == 8, '8 materials and 8 maps')
for i = 1, 8 do
  T.check(imp.mats[i] == MATS[i][1] and imp.maps[i].path == masks[i] and imp.maps[i].material == MATS[i][1]
    and imp.maps[i].materialId == i - 1 and imp.maps[i].channel == 'R', 'map ' .. i .. ': mask, material, id')
end
for i = 1, 7 do
  local m = scenetree.findObject(MATS[i][1])
  T.check(m and m.class == 'TerrainMaterial' and m.fields.groundmodelName == MATS[i][2]
    and m.fields.internalName == MATS[i][1] and m.fields.baseColorBaseTex == dirt.fields.baseColorBaseTex and m.canSave == false,
    'clone ' .. MATS[i][1] .. ': ground model, textures copied, not saved')
end
T.check(dirt.fields.groundmodelName == 'DIRT', 'the level dirt material untouched')
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

-- groundcover's regional mix: the dry-grass share by climate / place / season, at a constant element total
do
  local gc = require('beamngEarth/groundcover')
  local function near(a, b) return math.abs(a - b) < 1e-9 end
  -- stock: no climate, outside Australia, or a humid class: nil (the mix as before)
  T.check(gc.dryShare(nil, nil, nil) == nil and gc.dryShare(14, 39.3, -76.6) == nil and gc.dryShare(nil, 51.5, -0.1) == nil,
    'no climate / Baltimore Cfa / London: the stock mix')
  T.check(near(gc.STOCK_DRY, 0.05 / 0.47), 'stock dry share is the DEFS one')
  local f1, f2 = gc.mixFactors(nil)
  T.check(f1 == 1 and f2 == 1, 'no dry share: factors 1')
  -- Australia: Brisbane (Cfa), Townsville (Aw), Longreach (BSh), Perth (Csa), the same without a class
  T.check(near(gc.dryShare(14, -27.47, 153.03), 0.5), 'Brisbane: half dry')
  T.check(near(gc.dryShare(3, -19.26, 146.82), 0.55), 'Townsville Aw')
  T.check(near(gc.dryShare(6, -23.44, 144.25), 0.65), 'Longreach BSh')
  T.check(gc.dryShare(nil, -33.0, 140.0) == nil, 'no class (the server sends one only for an Australian anchor): stock')
  T.check(gc.dryShare(nil, -10.5, 124.0) == nil, 'East Timor, in the Australian box: stock')
  T.check(gc.dryShare(1, -16.9, 145.77) == nil and gc.dryShare(15, -37.8, 144.96) == nil, 'Cairns Af / Melbourne Cfb: stock')
  T.check(gc.dryShare(15, -43.0, 147.3) == nil and gc.dryShare(15, -34.0, 112.0) == nil, 'Tasmania inside, the box edge')
  T.check(gc.dryShare(nil, -5.0, 130.0) == nil and gc.dryShare(nil, -40.0, 170.0) == nil, 'off the Australian box: stock')
  -- arid and savanna classes elsewhere (Phoenix BWh, Serengeti Aw), nothing seasonal in the north
  T.check(near(gc.dryShare(4, 33.45, -112.07), 0.7) and near(gc.dryShare(3, -2.5, 34.8), 0.55), 'Phoenix BWh, Serengeti Aw')
  T.check(near(gc.dryShare(4, 33.45, -112.07, 100), 0.7), 'northern hemisphere: no season term')
  -- the southern season: drier Jun-Sep, greener Dec-Mar
  local winter, summer = gc.dryShare(14, -27.47, 153.03, 213), gc.dryShare(14, -27.47, 153.03, 30)
  T.check(near(winter, 0.6) and near(summer, 0.4), 'Brisbane: 60 % dry in early August, 40 % in late January')
  T.check(gc.dryShare(14, -27.47, 153.03, 244) > gc.dryShare(14, -27.47, 153.03, 350), 'Sep drier than mid Dec')
  T.check(near(gc.dryShare(8, -31.95, 115.86, 213), 0.5) and near(gc.dryShare(8, -31.95, 115.86, 30), 0.5),
    'Perth Csa: no seasonal swing (dry in summer already)')
  -- the vegetation palette's month by month dry shares (anchor_ready.grassDry, server season.grass_dry_months): wet December to March
  local months = { 0.12, 0.12, 0.302, 0.667, 0.85, 0.85, 0.85, 0.85, 0.85, 0.85, 0.667, 0.302 }
  T.check(near(gc.dryShare(6, -23.3, 150.6, 196, months), 0.85), 'palette months: mid July dry (the climate class no longer matters)')
  T.check(near(gc.dryShare(14, -23.3, 150.6, 15, months), 0.15), 'palette months: mid January green')
  T.check(near(gc.dryShare(nil, -23.3, 150.6, nil, months), 0.6), 'palette months, no season: their mean')
  T.check(near(gc.dryShare(14, -27.47, 153.03, 213, { 1 }), 0.6), 'a malformed list is ignored')
  local lg, ld = gc.longMix(0.5)
  local lg9, ld9 = gc.longMix(0.9)
  T.check(near(lg, 1) and near(ld, 1) and near(lg9 + ld9, 2) and lg9 < 0.3 and ld9 > 1.7, 'the long grass layer trades at a constant total')
  T.check(near(select(2, gc.longMix(nil)), 2 * gc.AU_DRY), 'no dry share: the Australian default')
  -- constant element total: green down by what dry goes up
  local fg, fd = gc.mixFactors(0.6)
  local g, d = 0, 0
  for _, e in ipairs(gc.DEFS) do
    if e.mix == 'green' then g = g + e.share elseif e.mix == 'dry' then d = d + e.share end
  end
  T.check(near(g + d, g * fg + d * fd), 'the grass layer keeps its total share')
  T.check(near(d * fd / (g * fg + d * fd), 0.6) and fg < 1 and fd > 1, 'a 60 % dry mix')
  -- built objects follow: the elements of the dry definition scale up, the green ones down; the season remakes them
  _G.jsonNull = _G.jsonNull or setmetatable({}, { __tostring = function() return 'null' end })
  local lines, byLine = {}, {}
  for _, d2 in ipairs(gc.DEFS) do
    local line = '{"class":"GroundCover","name":"' .. d2.src .. '"}'
    lines[#lines + 1] = line
    byLine[line] = { name = d2.src, class = 'GroundCover', material = 'm_' .. d2.src, maxElements = 100000,
      Types = { { layer = 'Grass' } } }
  end
  local realDecode, realRead, realFlush = _G.jsonDecode, _G.readFile, _G.flushGroundCoverGrids
  _G.jsonDecode = function(line) return byLine[line] end
  _G.readFile = function(p)
    for _, src in pairs(gc.SOURCES) do if p == src.items then return table.concat(lines, '\n') end end
    return realRead and realRead(p)
  end
  local flushes = 0
  _G.flushGroundCoverGrids = function() flushes = flushes + 1 end
  local function elements(name) return tonumber(scenetree.findObject(gc.PREFIX .. name).fields.maxElements) end
  gc.reset()
  gc.setEnabled(true)
  gc.setClimate(nil, 39.3, -76.6)
  T.check(gc.ensure() and gc.status().dryShare == nil, 'built with the stock mix')
  local green0, dry0 = elements('small_grass_close'), elements('grass_dry_1')
  local total0 = green0 + elements('long_grass_distant') + dry0
  T.check(near(green0, 25000) and near(dry0, 5000), 'stock elements 25000 / 5000')
  gc.setClimate(14, -27.47, 153.03)                   -- a teleport to Brisbane: the commit sets it, core's rebuild builds it
  T.check(gc.rebuild() and near(gc.status().dryShare, 0.5), 'Brisbane: built half dry')
  local green1, dry1 = elements('small_grass_close'), elements('grass_dry_1')
  T.check(green1 < green0 and dry1 > 4 * dry0, 'less green, much more dry grass (' .. green1 .. ' / ' .. dry1 .. ')')
  gc.setClimate(nil, 39.3, -76.6)
  gc.rebuild()
  T.check(near(elements('small_grass_close') + elements('long_grass_distant') + elements('grass_dry_1'), total0),
    'back to the stock mix at a humid place, the same element total')
  gc.setClimate(14, -27.47, 153.03)
  gc.rebuild()
  local total1 = elements('small_grass_close') + elements('long_grass_distant') + elements('grass_dry_1')
  T.check(math.abs(total1 - total0) <= 3, 'Brisbane: the same element total (' .. total0 .. ' / ' .. total1 .. ')')
  -- the DRY_LONG layer's definitions keep their own total whatever the dry share, and are on their own layer
  local function longTotal() return elements('grass_dry_1_long') + elements('weed1_long') + elements('long_grass_distant_long') end
  local long1 = longTotal()
  gc.setClimate(14, -23.3, 150.6, { 0.12, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12 })
  gc.rebuild()
  T.check(near(gc.status().dryShare, 0.1) and math.abs(longTotal() - long1) <= 3, 'wet months: the long grass total as before')
  T.check(elements('long_grass_distant_long') > 3 * elements('grass_dry_1_long'), 'wet months: mostly green long grass')
  gc.setClimate(14, -27.47, 153.03)
  gc.rebuild()
  -- the lawn and the shrub definitions are not mixed
  T.check(elements('small_grass_close_lawn') == 10000 and elements('shrubs_near') == 15000, 'lawn and scrub shares untouched')
  -- seasonal: only with hello.season auto, remade when the date moves the share a step
  local day, realToday, realCheck = 213, gc.today, gc.CHECK_S
  gc.today = function() return day end
  gc.CHECK_S = 0
  T.check(gc.update(true) == false, 'not seasonal: no remake')
  gc.setSeasonal(true)
  local fl = flushes
  T.check(gc.update(true) == true and near(gc.status().dryShare, 0.6) and flushes == fl + 1, 'August: remade 60 % dry')
  T.check(gc.update(true) == false, 'the same step: no remake')
  day = 30
  T.check(gc.update(true) == true and near(gc.status().dryShare, 0.4), 'late January: remade 40 % dry')
  T.check(gc.update(false) == false, 'not in our level: nothing')
  gc.setSeasonal(false)
  T.check(gc.update(true) == false, 'season off again')
  gc.setClimate(nil, nil, nil)
  gc.setEnabled(false)
  gc.setEnabled(true)
  gc.reset()
  gc.today, gc.CHECK_S = realToday, realCheck
  _G.jsonDecode, _G.readFile, _G.flushGroundCoverGrids = realDecode, realRead, realFlush
end
-- the texture set's base slot is put back to the photos' size when the engine's own reload of the level's materials
-- took it back (checkBaseSize at every block load; the game's log 2026-11-07: 24 repacks of one photo a swap)
do
  local o = scenetree.findObject(terrain.TEXTURE_SET)
  T.check(o ~= nil, 'the texture set stub exists')
  terrain.BASE_TEX_SIZE = 4096
  o:setField('baseTexSize', 0, '2048 2048')
  T.check(terrain.checkBaseSize() == true and tostring(o:getField('baseTexSize', 0)) == '4096 4096',
    'checkBaseSize: a texture set at another size is set again')
  T.check(terrain.checkBaseSize() == false, 'checkBaseSize: nothing to do when it already matches')
  terrain.BASE_TEX_SIZE = 2048
  o:setField('baseTexSize', 0, '2048 2048')
end

T.done('test_surface_layers')
