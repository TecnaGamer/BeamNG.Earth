-- luajit mod/tests/test_terrain_swap.lua  (terrain.swap: the server's .ter swapped in, 2026-10-13)
-- Only after a first importMaps and at the same square size; sets maxHeight, the position and the file
-- (one postApply, no importMaps); the slots follow the materials the .ter names; the base photo is set.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local lattice = require('beamngEarth/lattice')
lattice.configure(512, 2.0)
require('beamngEarth/follow').configure()
local terrain = require('beamngEarth/terrain')
terrain.REBIND_AFTER_SWAP = false   -- (the 1.5.5 path first; the rebind at the end)
terrain.configure(512)
local calls = S.calls

T.check(terrain.swap('/t/a.ter', 2, 512, 0, 0, 0) == false, 'no block yet: no swap (the caller imports)')
local dirt = createObject('TerrainMaterial'); dirt:registerObject(terrain.MATERIAL)
T.check(terrain.load('/t/hm.png', 2, 512, -512, -512, -64), 'first block imported')
local tb = terrain.get()
local imports = calls.importMaps or 0
T.check(terrain.swap('/t/b.ter', 1, 512, 0, 0, 0) == false, 'another square size: no swap')
local missing = FS.fileExists
FS.fileExists = function(_, p) return p ~= '/t/gone.ter' end
T.check(terrain.swap('/t/gone.ter', 2, 512, 0, 0, 0) == false, 'a missing .ter: no swap')
FS.fileExists = missing
local mats = { { 'bngEarthGroundAsphalt', 'ASPHALT_OLD' }, { 'beamngEarthDirt', 'DIRT' } }
local applies = calls.postApply_TerrainBlock or 0
local updates = calls.updateMaterial or 0
local setField, prepared = tb.setField, false
function tb:setField(k, i, v)
  if k == 'terrainFile' and v == '/t/c.ter' then
    local asphalt = scenetree.findObject(mats[1][1])
    prepared = asphalt.fields.baseColorBaseTex == '/t/c_base.png' and asphalt.fields.baseColorBaseTexSize == '512'
      and dirt.fields.baseColorBaseTex == '/t/c_base.png' and dirt.fields.baseColorBaseTexSize == '512'
  end
  return setField(self, k, i, v)
end
T.check(terrain.swap('/t/c.ter', 2, 1024, -384, -512, -128, { materials = mats }, '/t/c_base.png', 1024) == true, 'swap')
T.check(prepared, 'every new slot has its photo and sample size before the terrainFile setter binds it')
T.check(calls.updateMaterial == updates, 'a successful .ter swap does not dirty the material arrays again')
T.check(tb.fields.terrainFile == '/t/c.ter' and tb.applied == '/t/c.ter', 'the .ter set, then applied')
T.check(tb.fields.maxHeight == '1024', 'maxHeight from the message')
T.check(tb.pos.x == -384 and tb.pos.y == -512 and tb.pos.z == -128, 'block moved')
T.check((calls.importMaps or 0) == imports and calls.postApply_TerrainBlock == applies + 1, 'one postApply, no importMaps')
T.check(terrain.slots[1] == 'bngEarthGroundAsphalt' and terrain.slots[2] == 'beamngEarthDirt' and #terrain.slots == 2,
  'material slots = the .ter\'s materials')
T.check(terrain.base() == '/t/c_base.png', 'base photo set')
T.check(terrain.swap('/t/d.ter', 2, 512, 0, 0, 0) == true and terrain.slots[1] == terrain.MATERIAL and #terrain.slots == 1,
  'no layers: the single dirt layer')
T.check(terrain.base() == '/t/c_base.png' and dirt.fields.baseColorBaseTex == '/t/c_base.png',
  'nil base leaves the material fields and reported base untouched')

local otherMats = { { 'bngEarthGroundGrass', 'GRASS' }, { 'beamngEarthDirt', 'DIRT' } }
prepared = false
function tb:setField(k, i, v)
  if k == 'terrainFile' and v == '/t/e.ter' then
    local grass = scenetree.findObject(otherMats[1][1])
    prepared = grass.fields.baseColorBaseTex == '/t/e_base.png' and grass.fields.baseColorBaseTexSize == '256'
      and dirt.fields.baseColorBaseTex == '/t/e_base.png' and dirt.fields.baseColorBaseTexSize == '256'
  end
  if k == 'terrainFile' and v == '/t/fail.ter' then error('injected file setter failure') end
  return setField(self, k, i, v)
end
T.check(terrain.swap('/t/e.ter', 2, 512, 128, 0, 0, { materials = otherMats }, '/t/e_base.png', 512),
  'swap with a different material list')
local grass = scenetree.findObject(otherMats[1][1])
local asphalt = scenetree.findObject(mats[1][1])
T.check(prepared and terrain.slots[1] == otherMats[1][1] and asphalt.fields.baseColorBaseTex == '/t/c_base.png',
  'preparation uses the incoming slots rather than the previous list')
T.check(not terrain.swap('/t/e.ter', 2, 512, 128, 0, 0, { materials = otherMats }, '/t/unused.png', 512)
  and terrain.base() == '/t/e_base.png' and grass.fields.baseColorBaseTex == '/t/e_base.png',
  'same file path falls back to import: its setter would skip loading the prepared fields')
T.check(terrain.swap('/t/f.ter', 2, 512, 256, 0, 0, { materials = otherMats }, false)
  and terrain.base() == nil and grass.fields.baseColorBaseTex == terrain.LEVEL_BASE.tex
  and grass.fields.baseColorBaseTexSize == tostring(terrain.LEVEL_BASE.sizeM / 2), 'false prepares the flat level base before the swap')
T.check(calls.updateMaterial == updates, 'all successful swaps rely on the file setter material binding')
local oldSlots, oldSwaps = terrain.slots, terrain.swaps
T.check(not terrain.swap('/t/wrong-square.ter', 1, 512, 256, 0, 0, { materials = mats }, '/t/wrong.png', 512)
  and asphalt.fields.baseColorBaseTex == '/t/c_base.png' and terrain.slots == oldSlots,
  'square size rejection happens before touching any base fields')
local failMats = { mats[1], otherMats[1] }
T.check(not terrain.swap('/t/fail.ter', 2, 512, 256, 0, 0, { materials = failMats }, '/t/fail.png', 1024),
  'file setter failure returns to the caller import fallback')
T.check(asphalt.fields.baseColorBaseTex == '/t/c_base.png' and asphalt.fields.baseColorBaseTexSize == '512'
  and grass.fields.baseColorBaseTex == terrain.LEVEL_BASE.tex and grass.fields.baseColorBaseTexSize == tostring(terrain.LEVEL_BASE.sizeM / 2),
  'failed file setter restores every prepared material base field')
T.check(terrain.base() == nil and terrain.slots == oldSlots and terrain.swaps == oldSwaps,
  'failed swap does not publish its base, incoming slot list, or successful-swap count')

local grassSetField, failedOnce = grass.setField, false
function grass:setField(k, i, v)
  if k == 'baseColorBaseTexSize' and not failedOnce then failedOnce = true; error('injected material field failure') end
  return grassSetField(self, k, i, v)
end
local beforeFile = tb.fields.terrainFile
T.check(not terrain.swap('/t/prep-fail.ter', 2, 512, 256, 0, 0, { materials = failMats }, '/t/prep-fail.png', 1024)
  and tb.fields.terrainFile == beforeFile, 'material preparation failure never reaches the file setter')
T.check(asphalt.fields.baseColorBaseTex == '/t/c_base.png' and grass.fields.baseColorBaseTex == terrain.LEVEL_BASE.tex
  and grass.fields.baseColorBaseTexSize == tostring(terrain.LEVEL_BASE.sizeM / 2), 'partial material preparation is rolled back')
grass.setField = grassSetField
T.check(terrain.load('/t/fallback.png', 2, 512, 256, 0, 0,
  { materials = failMats, masks = { '/t/a.png', '/t/b.png' } }, '/t/fallback_base.png', 1024),
  'normal import fallback still succeeds')
T.check(calls.updateMaterial == updates + 2 and asphalt.fields.baseColorBaseTex == '/t/fallback_base.png'
  and grass.fields.baseColorBaseTex == '/t/fallback_base.png', 'PNG import still rebinds every slot after a failed swap')

-- GitHub #115: every slot rebound after the swap, as 1.5.4 (the default)
grass.setField = grassSetField
terrain.REBIND_AFTER_SWAP = true
local before = calls.updateMaterial or 0
T.check(terrain.swap('/t/g.ter', 2, 512, 256, 0, 0, { materials = otherMats }, '/t/g_base.png', 512)
  and calls.updateMaterial == before + #otherMats, 'one updateMaterial per slot after the swap: '
  .. tostring((calls.updateMaterial or 0) - before))
terrain.REBIND_AFTER_SWAP = false

-- weather phase C: the paved layers' wet base maps ride on the next swap (set before the file, no updateMaterial),
-- and go back to their own when dry; the other layers are untouched
T.check(terrain.isWetLayer('bngEarthGroundAsphalt') and terrain.isWetLayer('bngEarthGroundCobble')
  and not terrain.isWetLayer('beamngEarthDirt') and not terrain.isWetLayer('bngEarthGroundGrass'), 'the paved layers')
local asph = scenetree.findObject('bngEarthGroundAsphalt')
asph:setField('roughnessBaseTex', 0, '/levels/beamng_earth/art/terrains/bngEarth_base_r.png')
asph:setField('aoBaseTex', 0, '/levels/beamng_earth/art/terrains/bngEarth_base_ao.png')
terrain.setWet(2)
local wetAtFile, upd0 = nil, calls.updateMaterial or 0
function tb:setField(k, i, v)
  if k == 'terrainFile' and v == '/t/w.ter' then wetAtFile = asph.fields.roughnessBaseTex end
  return setField(self, k, i, v)
end
T.check(terrain.swap('/t/w.ter', 2, 512, 0, 0, 0, { materials = mats }, nil) == true, 'a swap while wet')
T.check(wetAtFile and wetAtFile:find('bngEarth_base_nm') ~= nil and asph.fields.aoBaseTex:find('bngEarth_base_h') ~= nil,
  'the asphalt layer wet before the file is set: ' .. tostring(wetAtFile))
T.check((calls.updateMaterial or 0) == upd0, 'no updateMaterial for it')
T.check(dirt.fields.roughnessBaseTex == nil or not tostring(dirt.fields.roughnessBaseTex):find('_rw'), 'the dirt layer stays')
local sets = terrain.wetSets
terrain.swap('/t/w2.ter', 2, 512, 0, 0, 0, { materials = mats }, nil)
T.check(terrain.wetSets == sets, 'already wet: nothing set again')
terrain.setWet(0)
terrain.swap('/t/w3.ter', 2, 512, 0, 0, 0, { materials = mats }, nil)
T.check(asph.fields.roughnessBaseTex == '/levels/beamng_earth/art/terrains/bngEarth_base_r.png'
  and asph.fields.aoBaseTex == '/levels/beamng_earth/art/terrains/bngEarth_base_ao.png', 'dry: its own maps back')
-- a field list with a constant (no texture) on one layer only; a changed list is applied at the next swap, and dry
-- puts every field it touched back
local cob = createObject('TerrainMaterial'); cob:registerObject('bngEarthGroundCobble')
asph:setField('aoDetailStrength', 0, '0.8 0.2')
cob:setField('aoDetailStrength', 0, '1 0.3')
local mats2 = { { 'bngEarthGroundAsphalt', 'ASPHALT_OLD' }, { 'bngEarthGroundCobble', 'COBBLESTONE' } }
local saved = terrain.WET_FIELDS[2]
terrain.WET_FIELDS[2] = { { 'aoBaseTex', 'bngEarth_base_h.png' }, { 'aoDetailStrength', '0 0', layers = '^bngEarthGroundAsphalt' } }
terrain.setWet(2)
terrain.swap('/t/w4.ter', 2, 512, 0, 0, 0, { materials = mats2 }, nil)
T.check(asph.fields.aoDetailStrength == '0 0' and cob.fields.aoDetailStrength == '1 0.3'
  and cob.fields.aoBaseTex:find('bngEarth_base_h') ~= nil, 'a constant on the asphalt only, the base map on both')
terrain.WET_FIELDS[2] = { { 'roughnessBaseTex', 'bngEarth_base_nm.png' } }
terrain.swap('/t/w5.ter', 2, 512, 0, 0, 0, { materials = mats2 }, nil)
T.check(asph.fields.aoDetailStrength == '0.8 0.2' and asph.fields.roughnessBaseTex:find('bngEarth_base_nm') ~= nil
  and asph.fields.aoBaseTex == '/levels/beamng_earth/art/terrains/bngEarth_base_ao.png', 'a changed list: applied, the rest back')
terrain.setWet(0)
terrain.swap('/t/w6.ter', 2, 512, 0, 0, 0, { materials = mats2 }, nil)
T.check(asph.fields.roughnessBaseTex == '/levels/beamng_earth/art/terrains/bngEarth_base_r.png'
  and cob.fields.aoDetailStrength == '1 0.3' and asph.fields.aoDetailStrength == '0.8 0.2', 'dry: all back')
terrain.WET_FIELDS[2] = saved
T.done('test_terrain_swap')
