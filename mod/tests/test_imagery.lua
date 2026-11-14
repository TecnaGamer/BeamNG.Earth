-- luajit mod/tests/test_imagery.lua  (V1_3_SPEC B-E, mod side)
-- Per-tile imagery materials (satmat.lua + farfield.lua): created once before the first tile that
-- uses them, as a governor unit, shared by tiles with the same name, deleted only after the last
-- tile object using them is deleted; protocol-3 tiles keep the static material. Mesh near-visual
-- mode: no covered/sunk/hidden tiles for the block, no commit gating, finer near levels.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
-- (the v1.3.1 block, 1024² at 2 m: these checks are written for it; the 1 m default: test_lattice / test_follow)
require('beamngEarth/lattice').configure(1024, 2.0)
require('beamngEarth/follow').configure()
require('beamngEarth/rebase').configure(2048)
require('beamngEarth/farfield').SPARE_MAX = 0 -- deletion timing as before; the spare pool: test_farfield_spares.lua
local farfield = require('beamngEarth/farfield')
local governor = require('beamngEarth/governor')
local satmat = require('beamngEarth/satmat')

-- event log of engine object lifecycles, and a fake wall clock (each register costs 1 ms)
local events = {}
local now = 0
local realCreate = createObject
createObject = function(class)
  local o = realCreate(class)
  local reg, del = o.registerObject, o.delete
  o.registerObject = function(self, n) now = now + 1; events[#events + 1] = 'reg ' .. class .. ' ' .. tostring(n); return reg(self, n) end
  o.delete = function(self) events[#events + 1] = 'del ' .. class .. ' ' .. tostring(self.name); return del(self) end
  return o
end
local function clock() return now end
local function indexOf(prefix)
  for i, e in ipairs(events) do if e:sub(1, #prefix) == prefix then return i end end
end
local function lastIndexOf(prefix)
  local r
  for i, e in ipairs(events) do if e:sub(1, #prefix) == prefix then r = i end end
  return r
end
local function mats() return S.liveOf('Material') end
local function matNamed(name) for _, m in ipairs(mats()) do if m.name == name then return m end end end

local free = { clock = clock }
function free:try() return true end
function free:charge() end
function free:chargeMotion() end

local DT = 1 / 60
local function size(l) return 256 * 2 ^ l end
local function key(l, i, j) return l .. '_' .. i .. '_' .. j end
local function tile(a, l, i, j, opts)
  opts = opts or {}
  return { type = 'far_tile', anchorId = a, key = key(l, i, j), level = l,
    shape = '/temp/beamngEarth/t/far_' .. a .. '_' .. key(l, i, j) .. '.cdae',
    pos = { i * size(l), j * size(l), opts.z or 100 }, err = opts.err or 0.5, sink = opts.sink,
    state = opts.state or 'group', material = opts.material, texture = opts.texture, detail = opts.detail,
    tex = opts.tex }
end
local function sat(a, l, i, j, opts)
  opts = opts or {}
  opts.material = opts.material or ('bngEarthSat_' .. a .. '_' .. key(l, i, j))
  opts.texture = opts.texture or ('/temp/beamngEarth/t/sat_' .. a .. '_' .. key(l, i, j) .. '.dds')
  if opts.detail == nil then opts.detail = true end
  return tile(a, l, i, j, opts)
end
local function swap(a, id, add, remove, dIn, dOut)
  return { type = 'far_swap', anchorId = a, id = id, add = add, remove = remove or {}, dIn = dIn, dOut = dOut }
end
local function obj(k) local t = farfield._tile(k); return t and S.objects[t.id] end
local function visible(o) return o ~= nil and not o.hidden end
local function step(gov, cam, n) for _ = 1, n or 1 do farfield.update(gov or free, DT, cam) end end
local camNorth = { x = 0, y = 0, z = 200, fx = 0, fy = 1, fz = 0, fovDeg = 60 }
local camSouth = { x = 0, y = 0, z = 200, fx = 0, fy = -1, fz = 0, fovDeg = 60 }

------------------------------------------------------------------------------------------
-- protocol-3 tile: no per-tile material, the static one
farfield.switchAnchor(1, false, { cx = 100000, cy = 0 })
farfield.onTile(tile(1, 1, 0, 8))
step()
T.check(#mats() == 0 and S.calls.loadJsonMaterialsFile == 1, 'protocol-3 tile: static material only, no per-tile Material')
farfield.onSwap(swap(1, 1, { '1_0_8' }))
step(free, camNorth)
T.check(visible(obj('1_0_8')), 'protocol-3 tile shown')
T.check(farfield.stats().textured == 0, 'no textured tile counted')

------------------------------------------------------------------------------------------
-- a textured tile: Material first (mapTo, colorMap, detail on UV1), then the TSStatic
local nameA = 'bngEarthSat_1_0_0_16'
farfield.onTile(sat(1, 0, 0, 16))
step()
local mA = matNamed(nameA)
T.check(mA ~= nil, 'per-tile Material created')
T.check(mA and mA.fields.mapTo == nameA, 'mapTo = the material name in the shape')
T.check(mA and mA.fields.colorMap == '/temp/beamngEarth/t/sat_1_0_0_16.dds', 'colorMap = the tile texture')
T.check(mA and mA.fields.detailMap == satmat.DETAIL_MAP and mA.fields.detailMapUseUV == '1' and mA.fields.detailScale == '0.5 0.5',
  'detail map on UV1, one repeat per detail cell')
T.check(mA and mA.fields.useAnisotropic == '1' and mA.fields.roughnessFactor == '1', 'anisotropic, matte')
T.check(mA and mA.canSave == false, 'not saved with the level')
T.check(satmat.DETAIL_MAP == '/art/beamngEarth/detail_ground.dds', 'mod detail texture (fading mips)')
local iMat, iTile = indexOf('reg Material ' .. nameA), indexOf('reg TSStatic bngEarthFar_%d')
iTile = nil
for i, e in ipairs(events) do if e:match('^reg TSStatic bngEarthFar_%d+_0_0_16$') then iTile = i end end
T.check(iMat and iTile and iMat < iTile, 'the Material exists before the tile object is registered')
T.check(satmat.refs(nameA) == 1, 'one reference')
T.check(farfield.stats().textured == 1 and farfield.stats().materials == 1, 'stats: textured tile, live material')

-- the same key re-sent before its swap: the newer one wins and shares the material (no 2nd Material)
farfield.onTile(sat(1, 0, 0, 16, { z = 101 }))
step()
local nMatA = 0
for _, e in ipairs(events) do if e == 'reg Material ' .. nameA then nMatA = nMatA + 1 end end
T.check(nMatA == 1, 'shared material created once')
step(free, nil, 3) -- the superseded object is deleted (nothing else queued)
T.check(satmat.refs(nameA) == 1 and matNamed(nameA) ~= nil, 'superseded object deleted, material kept for the newer one')
farfield.onSwap(swap(1, 2, { '0_0_16' }))
step(free, camNorth)
T.check(visible(obj('0_0_16')) and obj('0_0_16').pos.z == 101, 'textured tile shown')

-- replaced in place by a re-sent key (same material name): kept, still one Material
farfield.onTile(sat(1, 0, 0, 16, { z = 102 }))
step(free, camNorth)
T.check(satmat.refs(nameA) == 2, 'two instances share the material during the swap')
farfield.onSwap(swap(1, 3, { '0_0_16' }, { '0_0_16' }, 1, 1))
step(free, camNorth, 60)
T.check(obj('0_0_16').pos.z == 102 and satmat.refs(nameA) == 1 and matNamed(nameA) ~= nil, 'replacement in place keeps the material')

-- swapped out for a protocol-3 parent: the material goes, but only after the tile object
farfield.onTile(tile(1, 1, 0, 8, { z = 99 }))
step(free, camSouth)
local tileId = obj('0_0_16').id
farfield.onSwap(swap(1, 4, { '1_0_8' }, { '0_0_16' }, 1, 1))
step(free, camSouth, 5)
T.check(S.objects[tileId] == nil, 'textured tile deleted')
T.check(matNamed(nameA) == nil and satmat.count() == 0, 'its material deleted with the last tile using it')
local dTile, dMat
for i, e in ipairs(events) do
  if e:match('^del TSStatic bngEarthFar_%d+_0_0_16$') then dTile = i end
  if e == 'del Material ' .. nameA then dMat = i end
end
T.check(dTile and dMat and dTile < dMat, 'Material deleted after its tile object')

------------------------------------------------------------------------------------------
-- governor units: 'material' then 'cdaeTex', each charged; the material may run a frame earlier
do
  local gov = governor.new({ clock = clock })
  farfield.onTile(sat(1, 0, 1, 16))
  local frames, matFrame, tileFrame = 0, nil, nil
  for f = 1, 200 do
    gov:beginFrame(farfield.hasWork(), 11.5)
    step(gov, camNorth)
    local p = farfield._pending('0_1_16')
    if not matFrame and satmat.exists('bngEarthSat_1_0_1_16') then matFrame = f end
    if not tileFrame and p and p.loaded then tileFrame = f end
    frames = f
    if tileFrame then break end
  end
  T.check(matFrame and tileFrame and matFrame <= tileFrame, 'material unit before the tile import')
  T.check(gov.used.material and gov.used.cdaeTex, 'units charged as material and cdaeTex')
  T.check(math.abs(gov.est.material - governor.PRIOR.material) > 1e-9 and math.abs(gov.est.cdaeTex - governor.PRIOR.cdaeTex) > 1e-9,
    'measured costs update the estimates')
  T.check(governor.PRIOR.material == 0.3 and governor.PRIOR.cdaeTex == 3.0, 'priors from the in-game measurements')
  T.check(gov.stats.maxFrameSpent <= governor.DEFAULT_TARGET_MS + gov:maxUnit() + 1e-9, 'within the frame budget')
end
-- FORMAT 4 (docs/V1_4_SPEC.md 3.1 M1): far_tile.tex picks the unit kind; an untextured tile stays 'cdae'
do
  local gov = governor.new({ clock = clock })
  local kinds = {}
  local realCharge = gov.charge
  function gov:charge(kind, ms) kinds[kind] = (kinds[kind] or 0) + 1; return realCharge(self, kind, ms) end
  farfield.onTile(sat(1, 0, 20, 16, { tex = 512 }))
  farfield.onTile(sat(1, 0, 21, 16, { tex = 256 }))
  farfield.onTile(sat(1, -1, 44, 32, { tex = 128 }))
  farfield.onTile(tile(1, 0, 22, 16, { tex = 512 }))       -- no material (imagery failed): 'cdae'
  for _ = 1, 400 do
    gov:beginFrame(farfield.hasWork(), 11.5)
    step(gov, camNorth)
    if farfield._pending('0_20_16').loaded and farfield._pending('0_21_16').loaded
      and farfield._pending('-1_44_32').loaded and farfield._pending('0_22_16').loaded then break end
  end
  T.check(kinds.cdaeTex512 == 1 and kinds.cdaeTex256 == 1 and kinds.cdaeTex128 == 1 and kinds.cdae == 1
    and kinds.cdaeTex == nil, 'tex 512/256/128 -> cdaeTex512/256/128; no material -> cdae')
  T.check(gov.stats.maxFrameSpent <= governor.DEFAULT_TARGET_MS + gov:maxUnit() + 1e-9, 'within the frame budget')
  farfield.onSwap(swap(1, 90, { '0_20_16', '0_21_16', '-1_44_32', '0_22_16' }))
  step(free, camNorth, 60)
end
-- out of budget for a material: neither the material nor the tile runs
do
  local noMat = { clock = clock }
  function noMat:try(kind) return kind ~= 'material' end
  function noMat:charge() end
  function noMat:chargeMotion() end
  farfield.onTile(sat(1, 0, 2, 16))
  step(noMat, camNorth, 3)
  T.check(not satmat.exists('bngEarthSat_1_0_2_16') and not farfield._pending('0_2_16').loaded, 'no material budget: nothing runs')
  step(free, camNorth)
  T.check(farfield._pending('0_2_16').loaded, 'then both run')
end

------------------------------------------------------------------------------------------
-- dropped before it spawned: the reference is released at once, the material deleted
do
  local matOnly = { clock = clock }
  function matOnly:try(kind) return kind == 'material' or kind == 'delete' or kind == 'matDelete' end
  function matOnly:charge() end
  function matOnly:chargeMotion() end
  farfield.onTile(sat(1, 0, 3, 16, { state = 'prefetch' }))
  step(matOnly, camNorth)
  T.check(satmat.exists('bngEarthSat_1_0_3_16') and farfield._pending('0_3_16').id == nil, 'material made, tile not yet')
  farfield.onRemove({ type = 'far_remove', anchorId = 1, key = '0_3_16' })
  step(matOnly, camNorth, 2)
  T.check(not satmat.exists('bngEarthSat_1_0_3_16') and matNamed('bngEarthSat_1_0_3_16') == nil, 'evicted before spawning: material deleted')
end
-- shape missing: the tile fails, its material is released
do
  local realExists = FS.fileExists
  FS.fileExists = function(_, p) return not p:match('far_1_0_4_16') end
  farfield.onTile(sat(1, 0, 4, 16))
  step(free, camNorth)
  FS.fileExists = realExists
  step(free, camNorth, 2)
  T.check(farfield._pending('0_4_16').failed and satmat.refs('bngEarthSat_1_0_4_16') == 0, 'failed spawn releases its material')
  T.check(matNamed('bngEarthSat_1_0_4_16') == nil, 'and the unused material is deleted')
end
-- texture file missing: a plain dirt-coloured material instead (no colorMap); no UV1: no detail map
do
  local realExists = FS.fileExists
  FS.fileExists = function(_, p) return not p:match('sat_1_0_5_16') end
  farfield.onTile(sat(1, 0, 5, 16))
  farfield.onTile(sat(1, 0, 6, 16, { detail = false }))
  step(free, camNorth)
  FS.fileExists = realExists
  local m5, m6 = matNamed('bngEarthSat_1_0_5_16'), matNamed('bngEarthSat_1_0_6_16')
  T.check(m5 and m5.fields.colorMap == nil and m5.fields.diffuseColor == satmat.FALLBACK_COLOR, 'missing texture: fallback colour')
  T.check(m6 and m6.fields.colorMap ~= nil and m6.fields.detailMap == nil, 'detail = false: no detail map')
end
-- a Material of that name already in the scene (extension reload) is adopted, not duplicated
do
  local m = createObject('Material')
  m:setField('mapTo', 0, 'bngEarthSat_1_0_7_16')
  m:setField('colorMap', 0, '/temp/beamngEarth/oldSession/far_1_0_7_16.dds')
  m:registerObject('bngEarthSat_1_0_7_16')
  local before = S.calls.createObject_Material
  farfield.onTile(sat(1, 0, 7, 16))
  step(free, camNorth)
  T.check(S.calls.createObject_Material == before and farfield._pending('0_7_16').loaded, 'leftover material adopted')
  T.check(m.fields.colorMap == '/temp/beamngEarth/t/sat_1_0_7_16.dds', 'adopted material points at the current texture')
end
-- the same name with another texture (a restarted server re-uses anchor ids): an unused material
-- (waiting for deletion) takes the new file; one in use keeps its own
do
  local name = 'bngEarthSat_1_0_7_16'
  satmat.unref(name) -- as if its only tile were gone (the material now waits for deletion)
  T.check(satmat.ref(name, '/temp/beamngEarth/new/a.dds') and matNamed(name).fields.colorMap == '/temp/beamngEarth/new/a.dds',
    'unused material re-pointed at the new texture')
  T.check(satmat.ref(name, '/temp/beamngEarth/new/b.dds') and matNamed(name).fields.colorMap == '/temp/beamngEarth/new/a.dds',
    'material in use keeps its texture')
  -- the texture the material keeps is the one heldSessions names (the server keeps its folder)
  local function held(tex)
    local n = 0
    satmat.eachTexture(function(t) if t == tex then n = n + 1 end end)
    return n
  end
  T.check(held('/temp/beamngEarth/new/a.dds') == 1 and held('/temp/beamngEarth/new/b.dds') == 0,
    'kept texture: eachTexture names the file in use, not the re-sent one')
  -- ... unless its file is gone (a restarted server deleted the old session): the new one, reloaded
  local realExists = FS.fileExists
  FS.fileExists = function(_, p) return p ~= '/temp/beamngEarth/new/a.dds' end
  local m = matNamed(name)
  local reloads = 0
  m.flush = function() end
  m.reload = function() reloads = reloads + 1 end
  T.check(satmat.ref(name, '/temp/beamngEarth/new/c.dds') and m.fields.colorMap == '/temp/beamngEarth/new/c.dds' and reloads == 1,
    'material in use whose file is gone: re-pointed and reloaded')
  T.check(held('/temp/beamngEarth/new/c.dds') == 1 and held('/temp/beamngEarth/new/a.dds') == 0,
    're-pointed: eachTexture (heldSessions) names the new file, no longer the gone one')
  FS.fileExists = realExists
  satmat.unref(name)
  satmat.unref(name) -- back to the one reference the pending tile holds
end

------------------------------------------------------------------------------------------
-- teleport: everything goes progressively, then every material
farfield.switchAnchor(2, false, { cx = 0, cy = 0 })
step(free, nil, 300)
T.check(#S.liveOf('TSStatic') == 0 and #mats() == 0 and satmat.count() == 0, 'teleport: tiles, then their materials, all deleted')
-- unload with live textured tiles: tiles deleted first, then the materials
farfield.onTile(sat(2, 0, 0, 16))
step(free, camNorth)
farfield.onSwap(swap(2, 1, { '0_0_16' }))
step(free, camNorth)
local n0 = #events
farfield.clear()
local tDel, mDel
for i = n0 + 1, #events do
  if events[i]:match('^del TSStatic') then tDel = i end
  if events[i]:match('^del Material') and not mDel then mDel = i end
end
T.check(#S.liveOf('TSStatic') == 0 and #mats() == 0, 'clear: tiles and materials deleted')
T.check(tDel and mDel and tDel < mDel, 'clear: materials after the tiles')

------------------------------------------------------------------------------------------
-- mesh near-visual mode
farfield.switchAnchor(3, false, { cx = 0, cy = 0 })
T.check(farfield.nearVisual() == 'terrain', 'terrain mode by default')
-- terrain mode: an L0 tile inside the block is covered (hidden)
farfield.onTile(tile(3, 0, -1, -1, { sink = 1.0, z = 50 }))
farfield.onTile(tile(3, 0, -4, 0, { sink = 1.5, z = 50 }))
step(free, camNorth)
farfield.onSwap(swap(3, 1, { key(0, -1, -1), key(0, -4, 0) }))
step(free, camNorth)
T.check(obj(key(0, -1, -1)).hidden and obj(key(0, -4, 0)).pos.z == 48.5, 'terrain mode: interior hidden, edge ring sunk')
T.check(not farfield.uncoverReady(0, 0, 256, 0), 'terrain mode: commit gated on the uncovered strip')
farfield.setNearVisual('mesh')
T.check(farfield.nearVisual() == 'mesh', 'mesh mode set')
T.check(visible(obj(key(0, -1, -1))) and obj(key(0, -1, -1)).pos.z == 50, 'mesh: formerly hidden tile shown at true height at once')
T.check(visible(obj(key(0, -4, 0))) and obj(key(0, -4, 0)).pos.z == 50, 'mesh: formerly sunk tile back at true height')
T.check(farfield.stats().covered == 0, 'mesh: nothing covered')
T.check(farfield.uncoverReady(0, 0, 256, 0) and farfield.uncoverReady(0, 0, 4096, 4096), 'mesh: commits never gated on far tiles')
local changed = farfield.onBlockCommit(256, 0)
T.check(changed == 0 and visible(obj(key(0, -4, 0))) and obj(key(0, -4, 0)).pos.z == 50, 'mesh: a block commit changes no tile')
-- finer near levels: L-2 (64 m) and L-1 (128 m) tiles inside the block are plain visible ground
farfield.onTile(sat(3, -2, 0, 2, { z = 60 }))   -- [0, 64] x [128, 192]
farfield.onTile(sat(3, -1, 1, 1, { z = 61 }))   -- [128, 256] x [128, 256]
step(free, camNorth)
farfield.onSwap(swap(3, 2, { key(-2, 0, 2), key(-1, 1, 1) }))
step(free, camNorth)
local l2 = farfield._tile(key(-2, 0, 2))
T.check(l2 and l2.x1 - l2.x0 == 64 and l2.y0 == 128, 'L-2 footprint 64 m')
T.check(visible(obj(key(-2, 0, 2))) and obj(key(-2, 0, 2)).pos.z == 60, 'L-2 tile shown at true height inside the block')
T.check(visible(obj(key(-1, 1, 1))) and farfield._tile(key(-1, 1, 1)).x1 - farfield._tile(key(-1, 1, 1)).x0 == 128, 'L-1 tile 128 m, shown')
-- an in-view near swap in mesh mode crosses over (L-2 children replace the L-1 tile)
local kids = { key(-2, 2, 2), key(-2, 3, 2), key(-2, 2, 3), key(-2, 3, 3) }
for _, k in ipairs(kids) do
  local l, i, j = k:match('^(%-?%d+)_(%-?%d+)_(%-?%d+)$')
  farfield.onTile(sat(3, tonumber(l), tonumber(i), tonumber(j), { z = 62, err = 0.1 }))
end
step(free, camNorth)
local x0 = farfield.stats().crossovers
local parent = obj(key(-1, 1, 1))
farfield.onSwap(swap(3, 3, kids, { key(-1, 1, 1) }, 0.4, 0.3))
step(free, camNorth)
T.check(farfield.stats().crossovers == x0 + 1 and math.abs(obj(kids[1]).pos.z - (62 - 0.4)) < 0.05, 'mesh: near swap crosses over')
step(free, camNorth, 40)
T.check(obj(key(-1, 1, 1)) == nil and visible(obj(kids[1])) and obj(kids[1]).pos.z == 62, 'mesh: crossover done')
T.check(S.objects[parent.id] == nil or parent.hidden, 'mesh: replaced tile gone')
-- back to terrain mode: covered tiles sink / hide again at once
farfield.setNearVisual('terrain')
T.check(obj(key(0, -1, -1)).hidden, 'terrain again: interior tile hidden')
T.check(obj(key(-2, 0, 2)).hidden, 'terrain again: an L-2 tile inside the block has no sink: hidden')
farfield.setNearVisual('mesh')

------------------------------------------------------------------------------------------
-- pooled names (server/matslots.py): never deleted; unused, the next tile's texture switches in
do
  local P = 'bngEarthFar_ab12_00007'
  T.check(satmat.pooled(P) and not satmat.pooled('bngEarthSat_1_0_0_16'), 'pooled names recognised')
  T.check(satmat.create(P, '/temp/beamngEarth/s/far_5_a.dds', true) and satmat.ref(P, '/temp/beamngEarth/s/far_5_a.dds'), 'pooled material created')
  local m = matNamed(P)
  local reloads = 0
  m.flush = function() end
  m.reload = function() reloads = reloads + 1 end
  satmat.requestRetex(P, '/temp/beamngEarth/s/far_5_a_r1.dds') -- queued for the tile that has it now
  satmat.unref(P)
  T.check(not satmat.hasDeletes() and not satmat.deleteNext() and matNamed(P) == m, 'unused pooled material kept')
  local function held(tex)
    local n = 0
    satmat.eachTexture(function(t) if t == tex then n = n + 1 end end)
    return n
  end
  T.check(held('/temp/beamngEarth/s/far_5_a.dds') == 0 and held('/temp/beamngEarth/s/far_5_a_r1.dds') == 0,
    'unused pooled material: no file held for it')
  local before = S.calls.createObject_Material
  T.check(satmat.create(P, '/temp/beamngEarth/s/far_5_b.dds', true) and satmat.ref(P, '/temp/beamngEarth/s/far_5_b.dds'),
    'the name re-used by another tile')
  T.check(S.calls.createObject_Material == before and m.fields.colorMap == '/temp/beamngEarth/s/far_5_b.dds' and reloads == 1,
    're-used: no new Material, switched to the new tile\'s texture and reloaded')
  T.check(not satmat.retexNext() and m.fields.colorMap == '/temp/beamngEarth/s/far_5_b.dds',
    'the previous tile\'s queued retexture does not apply to the new one')
  T.check(satmat.refs(P) == 1 and held('/temp/beamngEarth/s/far_5_b.dds') == 1, 'in use again: its file held')
  -- given to a third tile while the second still shows it (its removal is late): switched, time-sliced
  T.check(satmat.ref(P, '/temp/beamngEarth/s/far_9_c.dds') and satmat.refs(P) == 2
    and m.fields.colorMap == '/temp/beamngEarth/s/far_5_b.dds' and satmat.hasRetex(), 'a name still in use: switch queued')
  T.check(satmat.retexNext() and m.fields.colorMap == '/temp/beamngEarth/s/far_9_c.dds', 'switched to the newest tile')
  satmat.unref(P)
  satmat.unref(P)
end

T.done('test_imagery')
