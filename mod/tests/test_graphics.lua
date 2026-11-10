-- luajit mod/tests/test_graphics.lua  (the graphics settings of 2026-10-28; the user: "give player much more control in
-- the graphics settings (view distance, LOD quality, ETC. Also add an extreme quality setting that puts everything at
-- max quality to the real horizon")
-- The Extreme preset (every knob at its most, the real horizon, never the first run's pick), the server's knobs
-- (settings.serverGraphics -> core.sendGraphics), a file from before the knobs (they follow its preset; the tree
-- distance its tree level's), and each knob's module: trees.lua density / distance, groundcover.lua distance,
-- enginepref.lua (the engine's shape detail, the player's value back), terrain.lua's ground photo size.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local files = {}
function jsonReadFile(path) return files[path] end
function jsonWriteFile(path, data)
  local copy = {}
  for k, v in pairs(data) do copy[k] = v end
  files[path] = copy
  return true
end
FS = { fileExists = function(_, p) return files[p] ~= nil end }

local settings = require('beamngEarth/settings')
local trees = require('beamngEarth/trees')
local groundcover = require('beamngEarth/groundcover')
local enginepref = require('beamngEarth/enginepref')
local terrain = require('beamngEarth/terrain')

------------------------------------------------------------------------------------------
-- Extreme: every knob at its most (the last value of its list: the lists run from the least to the most)

local ex = settings.PRESETS.extreme
T.check(ex ~= nil and settings.PRESET_ORDER[#settings.PRESET_ORDER] == 'extreme', 'Extreme is the last preset')
for _, k in ipairs(settings.PRESET_KEYS) do
  local s = settings.SCHEMA[k]
  if k == 'viewDistanceMode' then
    T.check(ex[k] == 'horizon', 'Extreme: the real horizon')
  elseif k == 'viewDistanceKm' then
    T.check(ex[k] == settings.SCHEMA[k].default, 'Extreme: the fixed distance unused, its default')
  elseif s.kind == 'bool' then
    T.check(ex[k] == true, 'Extreme: ' .. k .. ' on')
  elseif s.kind == 'enum' then
    T.check(ex[k] == s.values[#s.values], 'Extreme: ' .. k .. ' at its most (' .. tostring(ex[k]) .. ')')
  end
end
for _, p in ipairs(settings.PRESET_ORDER) do
  for _, k in ipairs(settings.PRESET_KEYS) do
    T.check(settings.PRESETS[p][k] ~= nil, p .. ' sets ' .. k)
  end
end
-- never the first run's pick
for _, g in ipairs({ { name = 'NVIDIA GeForce RTX 5090', memoryMB = 32768 }, { name = 'AMD Custom GPU 0405', memoryMB = 1024 },
    { name = 'Intel UHD 620', memoryMB = 512 }, {}, nil }) do
  T.check(settings.detectQuality(g) ~= 'extreme', 'the first run never picks Extreme')
end
-- the Steam Deck keeps today's server knobs on Medium (its pick), Low is lighter
T.check(settings.PRESETS.medium.terrainDetail == 'normal' and settings.PRESETS.medium.groundPhoto == '2048'
  and settings.PRESETS.medium.buildingDistance == 'normal', 'Medium (the Deck): the server knobs as before')
T.check(settings.PRESETS.low.groundPhoto == '1024' and settings.PRESETS.low.terrainDetail == 'low', 'Low: lighter')

------------------------------------------------------------------------------------------
-- the server's knobs: picking Extreme sends them once (core.sendGraphics dedupes; applyAll calls it per knob)

local sent = {}
beamngEarth_core = {
  sendGraphics = function() sent[#sent + 1] = settings.serverGraphics() end,
  _state = function() return { inOurLevel = true } end,
}
local vr = { ['$pref::TS::detailAdjust'] = 1.5 }
VariableRegistry = { get = function(k) return vr[k] end, set = function(k, v) vr[k] = v end }

settings.load()
local r = settings.setQuality('extreme')
T.check(r.ok and settings.get('quality') == 'extreme', 'Extreme picked')
local g = settings.serverGraphics()
T.check(g.lodScale == 2.0 and g.treeScale == 2.2 and g.buildingScale == 2.0 and g.photoPx == 4096,
  'Extreme: terrain detail x2, trees x2.2, buildings x2, a 4096 ground photo')
T.check(#sent >= 1 and sent[#sent].photoPx == 4096, 'the server told')
T.check(trees.quality == 'extreme' and trees.distance == 'max', 'trees: extreme density, max distance')
T.check(trees.GHOST_FAR_M == math.floor(1280 * 2.2 + 0.5) + 256, 'tree ghosts kept to the server\'s new drop distance')
T.check(groundcover.distance == 1.5 and groundcover.density == 2.0, 'grass: far and dense')
T.check(enginepref.level == 'extreme' and vr['$pref::TS::detailAdjust'] == 3.0, 'object detail: 3 in our level')
T.check(g.viewM == 0, 'Extreme: the real horizon (no far field cap)')
settings.setQuality('low')
T.check(settings.serverGraphics().viewM == 40000 and sent[#sent].viewM == 40000,
  'Low: the fixed 40 km view caps the server\'s far field too')
settings.setQuality('high')
g = settings.serverGraphics()
T.check(g.lodScale == 1.0 and g.treeScale == 1.0 and g.buildingScale == 1.0 and g.photoPx == 2048, 'High: the defaults')
T.check(vr['$pref::TS::detailAdjust'] == 1.5, 'object detail back to the game setting: the player\'s value again')
settings.set('terrainDetail', 'ultra')
T.check(settings.get('quality') == 'custom' and settings.serverGraphics().lodScale == 1.6, 'a server knob moved: Custom')
settings.set('terrainDetail', 'normal')
T.check(settings.get('quality') == 'high', 'moved back: High')

------------------------------------------------------------------------------------------
-- a file from before the knobs: they follow its preset; on Custom the tree distance its tree level had

files[settings.FILE] = { version = 1, quality = 'ultra', trees = 'high', groundCover = true, groundCoverDensity = 'high',
  rocks = true, nightLights = 'many', viewDistanceMode = 'horizon', viewDistanceKm = 100 }
local v = settings.load()
T.check(v.quality == 'ultra' and v.terrainDetail == 'high' and v.treeDistance == 'far' and v.buildingDistance == 'far',
  'an old Ultra file: Ultra\'s new knobs')
T.check(settings.matchingPreset() == 'ultra', '... and still exactly Ultra')
files[settings.FILE] = { version = 1, quality = 'custom', trees = 'ultra' }
v = settings.load()
T.check(v.quality == 'custom' and v.trees == 'ultra' and v.treeDistance == 'farther' and v.terrainDetail == 'normal',
  'an old Custom file: its tree level\'s distance, the other knobs their defaults')
files[settings.FILE] = { version = 1, quality = 'custom', trees = 'ultra', treeDistance = 'near' }
T.check(settings.load().treeDistance == 'near', 'a saved tree distance is kept')
files[settings.FILE] = nil

------------------------------------------------------------------------------------------
-- trees.lua: density and distance apart

trees.setQuality('normal'); trees.setDistance('normal')
local base = {}
for i, ring in ipairs(trees.RINGS) do base[i] = { ring[1], ring[2] } end
trees.setDistance('far')
T.check(trees.RINGS[1][1] == math.floor(base[1][1] * 1.3 + 0.5) and trees.RINGS[1][2] == base[1][2],
  'distance: the rings farther, the ranks as they were')
trees.setQuality('low')
T.check(trees.RINGS[1][1] == math.floor(base[1][1] * 1.3 + 0.5) and trees.RINGS[2][2] < base[2][2],
  'density: the ranks thinner, the distance kept')
T.check(trees.distanceScale('near') == 0.75 and trees.distanceScale('max') == 2.2, 'the server\'s reach factors')
do  -- (GitHub #714) the lowest levels: fewer trees than Low, a shorter reach than Near, within the server's clamp (0.5)
  local r, c = trees.scaledRings('lowest', { { 300, 1.0 }, { 1e9, 0.2 } }, { { 300, 6000 } }, 'nearest')
  T.check(r[1][1] == 180 and math.abs(r[1][2] - 0.3) < 1e-9 and c[1][2] == 1800, 'lowest density, nearest distance')
  T.check(trees.distanceScale('nearest') >= 0.5 and trees.distanceScale('nearest') < trees.distanceScale('near'),
    'nearest: shorter than near, not below the server\'s 0.5')
end
trees.setQuality('normal'); trees.setDistance('normal')
T.check(trees.GHOST_FAR_M == 1536, 'normal: the server\'s vegetation DROP_M')

------------------------------------------------------------------------------------------
-- groundcover.lua: the radii times the distance, the elements times its square (the same density)

local def = { radius = 50, dissolveRadius = 30, maxElements = 1000, Types = { { layer = 'x' } } }
local function field(calls, k)
  for _, c in ipairs(calls) do if c[1] == k then return tonumber(c[3]) end end
end
local calls = groundcover.fieldsFor(def, 'L', 2.25, 1.5)
T.check(field(calls, 'radius') == 75 and field(calls, 'dissolveRadius') == 45 and field(calls, 'maxElements') == 2250,
  'grass far: 1.5x the radii, 2.25x the elements')
calls = groundcover.fieldsFor(def, 'L', 1)
T.check(field(calls, 'radius') == 50 and field(calls, 'maxElements') == 1000, 'grass normal: the stock fields')
T.check(groundcover.setDistance('near') and groundcover.distance == 0.7 and not groundcover.setDistance('bogus'),
  'grass distance levels')
groundcover.setDistance('normal')

------------------------------------------------------------------------------------------
-- enginepref.lua: ours in our level only, the player's value back; theirs taken when they change it meanwhile

vr['$pref::TS::detailAdjust'] = 1.0
enginepref.setLevel('ultra', true)
T.check(vr['$pref::TS::detailAdjust'] == 2.0, 'ultra: 2 in our level')
vr['$pref::TS::detailAdjust'] = 0.75                   -- the player applied the game's graphics settings meanwhile
enginepref.update(true, true)
T.check(vr['$pref::TS::detailAdjust'] == 2.0, 'ours again')
enginepref.update(false, true)
T.check(vr['$pref::TS::detailAdjust'] == 0.75, 'another level: the player\'s newest value')
enginepref.setLevel('game', true)
T.check(vr['$pref::TS::detailAdjust'] == 0.75 and not enginepref.setLevel('bogus', true), 'game: untouched')

------------------------------------------------------------------------------------------
-- terrain.lua: the ground photo size: the level's flat base maps of that size, before the first block

T.check(terrain.baseMapFor('/levels/beamng_earth/art/terrains/bngEarth_base_ao.png', 4096)
  == '/levels/beamng_earth/art/terrains/bngEarth_base_ao_4096.png', 'the 4096 AO map')
T.check(terrain.baseMapFor('/levels/beamng_earth/art/terrains/bngEarth_base_nm_4096.png', 2048)
  == '/levels/beamng_earth/art/terrains/bngEarth_base_nm.png', '2048: the plain names')
T.check(terrain.baseMapFor('/temp/beamngEarth/x/hm_1_0_0_base.png', 4096) == '/temp/beamngEarth/x/hm_1_0_0_base.png',
  'a block photo is left alone')
T.check(terrain.baseMapFor('/levels/beamng_earth/art/terrains/bngEarth_flat_h_1024.png', 4096)
  == '/levels/beamng_earth/art/terrains/bngEarth_flat_h_1024.png', 'the detail slots\' flat height is left alone')
-- every flat map named exists in the level, for every size
for n in pairs(terrain.BASE_SIZES) do
  for _, m in ipairs({ 'b', 'nm', 'r', 'ao', 'h' }) do
    local p = terrain.baseMapFor('/levels/beamng_earth/art/terrains/bngEarth_base_' .. m .. '.png', n)
    local f = io.open(dir .. '/../beamng_earth' .. p, 'rb')
    T.check(f ~= nil, 'shipped: ' .. p)
    if f then f:close() end
  end
end
local set = S.newObject('TerrainMaterialTextureSet'); set:registerObject(terrain.TEXTURE_SET)
set:setField('baseTexSize', 0, '2048 2048')
local mat = S.newObject('TerrainMaterial'); mat:registerObject('bngEarthGroundGrass')
mat:setField('aoBaseTex', 0, '/levels/beamng_earth/art/terrains/bngEarth_base_ao.png')
mat:setField('baseColorBaseTex', 0, '/temp/beamngEarth/s/hm_1_0_0_base.png')
local other = S.newObject('TerrainMaterial'); other:registerObject('someLevelGrass')
other:setField('aoBaseTex', 0, '/levels/beamng_earth/art/terrains/bngEarth_base_ao.png')
T.check(terrain.setPhotoSize(4096) == 4096 and terrain.photoSize() == 4096, 'no block yet: the next block\'s size')
T.check(terrain.setPhotoSize(999) == 4096, 'an unknown size is ignored')
T.check(terrain.applyBaseSize(4096) == 2, 'the set and one map changed')
T.check(set.fields.baseTexSize == '4096 4096', 'the texture set\'s base slot')
T.check(mat.fields.aoBaseTex == '/levels/beamng_earth/art/terrains/bngEarth_base_ao_4096.png', 'our layer\'s AO map')
T.check(mat.fields.baseColorBaseTex == '/temp/beamngEarth/s/hm_1_0_0_base.png', 'its photo left alone')
T.check(other.fields.aoBaseTex == '/levels/beamng_earth/art/terrains/bngEarth_base_ao.png', 'another\'s material untouched')
T.check(terrain.applyBaseSize(4096) == 0 and terrain.BASE_TEX_SIZE == 4096, 'again: nothing to change')
T.check(terrain.LEVEL_BASE.tex == '/levels/beamng_earth/art/terrains/bngEarth_base_b_4096.png', 'the neutral base too')
terrain.applyBaseSize(2048)
T.check(set.fields.baseTexSize == '2048 2048' and mat.fields.aoBaseTex
  == '/levels/beamng_earth/art/terrains/bngEarth_base_ao.png', 'back to 2048: the plain names')

T.done('test_graphics')
