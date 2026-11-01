-- luajit mod/tests/test_terrain_swap.lua  (terrain.swap: the server's .ter swapped in, 2026-10-13)
-- Only after a first importMaps and at the same square size; sets maxHeight, the position and the file
-- (one postApply, no importMaps); the slots follow the materials the .ter names; the base photo is set.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local lattice = require('beamngEarth/lattice')
lattice.configure(512)
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
  and grass.fields.baseColorBaseTexSize == terrain.LEVEL_BASE.size, 'false prepares the flat level base before the swap')
T.check(calls.updateMaterial == updates, 'all successful swaps rely on the file setter material binding')
local oldSlots, oldSwaps = terrain.slots, terrain.swaps
T.check(not terrain.swap('/t/wrong-square.ter', 1, 512, 256, 0, 0, { materials = mats }, '/t/wrong.png', 512)
  and asphalt.fields.baseColorBaseTex == '/t/c_base.png' and terrain.slots == oldSlots,
  'square size rejection happens before touching any base fields')
local failMats = { mats[1], otherMats[1] }
T.check(not terrain.swap('/t/fail.ter', 2, 512, 256, 0, 0, { materials = failMats }, '/t/fail.png', 1024),
  'file setter failure returns to the caller import fallback')
T.check(asphalt.fields.baseColorBaseTex == '/t/c_base.png' and asphalt.fields.baseColorBaseTexSize == '512'
  and grass.fields.baseColorBaseTex == terrain.LEVEL_BASE.tex and grass.fields.baseColorBaseTexSize == terrain.LEVEL_BASE.size,
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
  and grass.fields.baseColorBaseTexSize == terrain.LEVEL_BASE.size, 'partial material preparation is rolled back')
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
-- Fine ground (server finepatch.py, 2026-11-01): a block's pieces made ahead (finePrepare / fineStep: one a frame,
-- drawn FINE_STAGE_DROP_M under their place, not colliding), raised and switched on by its swap (no new object), and
-- their pooled slots let go with them
local roads = require('beamngEarth/roads')
roads.setSession('s1')
local fine = { bank = 1, materials = { { 'bngEarthFine1_4', 'bngEarthGroundRock' } },
  parts = { { shape = '/temp/beamngEarth/s1/slot_00007.cdae', pos = { 0, 0, 0 }, slot = 3, collision = 'mesh' },
            { shape = '/temp/beamngEarth/s1/b_fine_1_0r.cdae', pos = { 64, 0, 0 }, collide = false } } }
local made0 = calls.createObject_TSStatic or 0
terrain.finePrepare({ ter = '/t/f1.ter', fine = fine, base = '/t/f1_base.png' })
terrain.fineStep()
T.check(terrain.fineStatus().staged == 1, 'one piece a frame')
terrain.fineStep()
local st0 = terrain.fineStatus()
T.check(st0.staged == 2 and (calls.createObject_TSStatic or 0) == made0 + 2, 'both pieces made ahead')
local staged = {}
for _, o in pairs(S.objects) do if o.class == 'TSStatic' and o.fields.shapeName and o.fields.shapeName:find('/s1/') then staged[#staged + 1] = o end end
local function z(o) local p = o:getPosition() return p.z end
T.check(#staged == 2 and not staged[1].hidden and staged[1].fields.collisionType == 'None'
  and math.abs(z(staged[1]) + terrain.FINE_STAGE_DROP_M) < 1e-9, 'made ahead: drawn under its place, not colliding')
T.check(scenetree.findObject('bngEarthFine1_4') ~= nil, 'its material made with the block photo')
T.check(terrain.fineReady('/t/f1.ter', fine) and not terrain.fineReady('/t/other.ter', fine), 'ready: its pieces all made ahead')
-- (its photo replaced before the swap: the staged pieces' Material is kept and changed, never deleted under them;
-- deleting it crashed the game, 2026-11-01)
local matBefore = scenetree.findObject('bngEarthFine1_4')

T.check(terrain.swap('/t/f1.ter', 2, 512, 256, 0, 0, { materials = otherMats }, '/t/f1_base_full.png', 512, fine), 'swapped')
T.check(type(terrain.fineReady) == 'function', 'fineReady still a function after a swap (its timing stat is fineStaged)')
T.check(scenetree.findObject('bngEarthFine1_4') == matBefore and not matBefore.deleted,
  'the staged pieces\' material kept through a new photo')
T.check((calls.createObject_TSStatic or 0) == made0 + 2, 'the swap makes no piece again')
local cols = {}
for _, o in ipairs(staged) do
  cols[o.fields.shapeName] = o.fields.collisionType
  T.check(not o.hidden and math.abs(z(o)) < 1e-9, 'shown, at its place')
end
T.check(cols['/temp/beamngEarth/s1/slot_00007.cdae'] == 'Collision Mesh'
  and cols['/temp/beamngEarth/s1/b_fine_1_0r.cdae'] == 'None', 'colliding but for the rim piece')
T.check(terrain.fineStatus().pieces == 2 and terrain.fineStatus().bank == 1, 'the block\'s pieces')
-- the next block without fine ground: the pieces go, the pooled one's slot is reported gone
T.check(terrain.swap('/t/f2.ter', 2, 512, 256, 0, 0, { materials = otherMats }, '/t/f2_base.png', 512), 'swapped again')
local gone = roads.takeSlotsGone('s1')
T.check(#gone == 1 and gone[1][1] == 7 and gone[1][2] == 3, 'slot 7 (serial 3) let go: ' .. #gone)
T.check(S.objects[staged[1].id] == nil and S.objects[staged[2].id] == nil, 'pieces deleted')
-- a block in the shown block's bank is not staged (its materials show that block's photo)
T.check(terrain.swap('/t/f3.ter', 2, 512, 256, 0, 0, { materials = otherMats }, '/t/f3_base.png', 512, fine), 'f3')
terrain.finePrepare({ ter = '/t/f4.ter', fine = fine, base = '/t/f4_base.png' })
terrain.fineStep()
T.check(terrain.fineStatus().staged == 0, 'same bank: made at its swap')
T.done('test_terrain_swap')
