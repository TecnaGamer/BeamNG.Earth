-- luajit mod/tests/test_dev.lua  (dev tools, docs/DEVTOOLS.md)
-- The spatial index (tileindex.lua) against brute force; the dev module (dev.lua) with the real
-- farfield.lua on stubbed engine objects: zero work while everything is off, toggles and presets,
-- the index kept by farfield's watcher (add / delete / rebase / forget), the LOD tint in
-- time-sliced batches restoring every material exactly, the draw lists and their line cap, the
-- import order of the loading overlay, and the controls that change other modules (freeze LOD,
-- force rebase, physics terrain) undone when the dev tools go off.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
require('beamngEarth/farfield').SPARE_MAX = 0 -- deletion timing as before; the spare pool: test_farfield_spares.lua
local tileindex = require('beamngEarth/tileindex')
local farfield = require('beamngEarth/farfield')
local geodesy = require('beamngEarth/geodesy')
local rebase = require('beamngEarth/rebase')
local terrain = require('beamngEarth/terrain')
local dev = require('beamngEarth/dev')

------------------------------------------------------------------------------------------
-- tileindex: random tiles of every level, queries against a brute-force scan

math.randomseed(11)
local function rect(level, x, y)
  local s = 256 * 2 ^ level
  local x0, y0 = math.floor(x / s) * s, math.floor(y / s) * s
  return { level = level, x0 = x0, y0 = y0, x1 = x0 + s, y1 = y0 + s }
end
local ix = tileindex.new()
local all = {}
for id = 1, 3000 do
  local level = math.random(-2, 9)
  local r = rect(level, math.random(-200000, 200000), math.random(-200000, 200000))
  all[id] = r
  ix:insert(id, r)
end
T.check(ix:count() == 3000, 'index: 3000 inserted')
for id = 1, 3000, 3 do ix:remove(id); all[id] = nil end
T.check(ix:count() == 2000, 'index: 1000 removed')
T.check(not ix:remove(1), 'index: removing an absent id is a no-op')
local function brute(x, y, r)
  local set = {}
  for id, it in pairs(all) do if tileindex.dist2(it, x, y) <= r * r then set[id] = true end end
  return set
end
local okAll, maxVisited = true, 0
for q = 1, 200 do
  local x, y = math.random(-200000, 200000), math.random(-200000, 200000)
  local r = ({ 500, 3000, 20000, 80000 })[q % 4 + 1]
  local res, visited = ix:query(x, y, r)
  local want, got = brute(x, y, r), {}
  for _, e in ipairs(res) do got[e.id] = true end
  for id in pairs(want) do if not got[id] then okAll = false end end
  for id in pairs(got) do if not want[id] then okAll = false end end
  if r == 3000 then maxVisited = math.max(maxVisited, visited) end
end
T.check(okAll, 'index: query = brute force for 200 random discs (0.5-80 km)')
T.check(maxVisited <= 900, 'index: a 3 km query visits a bounded number of cells (' .. maxVisited .. ')')
-- moving an item: insert again under the same id
local mv = rect(0, 1000, 1000)
ix:insert(99999, mv)
local mv2 = rect(0, 50000, 50000)
ix:insert(99999, mv2)
T.check(ix:count() == 2001 and ix:get(99999) == mv2, 'index: re-insert replaces the entry')
-- at(): the finest tile containing a point
local px = tileindex.new()
local fine, coarse = rect(-2, 10, 10), rect(3, 10, 10)
px:insert(1, coarse); px:insert(2, fine)
T.check(px:at(10, 10) == fine, 'index: at() returns the finest tile')
T.check(px:at(10, 10, function(it) return it.level > 0 end) == coarse, 'index: at() with a filter')
T.check(px:at(-5000, -5000) == nil, 'index: at() outside every tile')
local near = tileindex.nearest(px:query(10, 10, 5000), 1)
T.check(#near == 1, 'index: nearest keeps n')
local v0 = px.version
px:clear()
T.check(px:count() == 0 and px:at(10, 10) == nil and px.version > v0, 'index: clear')

------------------------------------------------------------------------------------------
-- engine stubs for the dev module

local lines, texts = 0, 0
debugDrawer = {
  drawLine = function() lines = lines + 1 end,
  drawTextAdvanced = function() texts = texts + 1 end,
}
function ColorF(r, g, b, a) return { r, g, b, a } end
function ColorI(r, g, b, a) return { r, g, b, a } end
function String(s) return s end
local rays = 0
function castRayStatic(origin) rays = rays + 1; return 1000 end -- ground = the reference height

-- a core that counts every access (zero work when off)
local coreAccess = 0
beamngEarth_core = setmetatable({}, { __index = function() coreAccess = coreAccess + 1 end })

local free = { clock = function() return 0 end }
function free:try() return true end
function free:charge() end
function free:chargeMotion() end
function free:isHeavyFrame() return false end

local DT = 1 / 60
local function key(l, i, j) return l .. '_' .. i .. '_' .. j end
local function tileMsg(a, l, i, j, mat)
  local s = 256 * 2 ^ l
  local k = key(l, i, j)
  return { type = 'far_tile', anchorId = a, key = k, level = l, shape = '/t/far_' .. a .. '_' .. k .. '.cdae',
    pos = { i * s, j * s, 100 }, err = 0.5, state = 'group',
    material = mat and ('bngEarthSat_' .. a .. '_' .. k) or nil, texture = mat and ('/t/sat_' .. k .. '.dds') or nil }
end
local function loadTiles(a, list, id)
  local keys = {}
  for _, m in ipairs(list) do farfield.onTile(m); keys[#keys + 1] = m.key end
  farfield.onSwap({ type = 'far_swap', anchorId = a, id = id, add = keys, remove = {} })
  for _ = 1, 3 do farfield.update(free, DT) end
end
local function nTS() return #S.liveOf('TSStatic') end

farfield.switchAnchor(1, false, { cx = 0, cy = 0 })
-- 10 x 10 L-2 tiles (64 m) with their own imagery materials, and 4 L1 tiles without
local first = {}
for i = 0, 9 do for j = 0, 9 do first[#first + 1] = tileMsg(1, -2, i, j, true) end end
for i = 2, 3 do for j = 0, 1 do first[#first + 1] = tileMsg(1, 1, i, j, false) end end
loadTiles(1, first, 1)
T.check(nTS() == 104 and farfield._tile(key(-2, 3, 3)) ~= nil, 'setup: 104 tiles live')

------------------------------------------------------------------------------------------
-- off: no work at all

for _ = 1, 100 do dev.onUpdate(DT, DT) end
T.check(coreAccess == 0 and lines == 0 and texts == 0 and rays == 0, 'off: the hook touches nothing')
T.check(not dev._busy() and dev._index() == nil, 'off: not busy, no index')
farfield.onTile(tileMsg(1, -2, 20, 20, true))
farfield.onSwap({ type = 'far_swap', anchorId = 1, id = 2, add = { key(-2, 20, 20) }, remove = {} })
farfield.update(free, DT)
T.check(dev._index() == nil and coreAccess == 0, 'off: farfield calls no watcher')
local st0 = dev.state()
T.check(st0.active == false and st0.on.tint == false and st0.on.stats == false, 'off: state() reports everything off')

------------------------------------------------------------------------------------------
-- toggles and presets

local ok, err = dev.set('nonsense', true)
T.check(ok == nil and err:match('unknown'), 'set: unknown names are refused')
T.check(dev.preset('nope') == nil, 'preset: unknown presets are refused')
T.check(dev.set('outlines', true) == true and dev._busy(), 'set: outlines on -> busy')
T.check(dev._index():count() == nTS(), 'index built from the existing tiles (' .. dev._index():count() .. ')')
dev.preset('lod')
local s = dev.state()
T.check(s.on.tint and s.on.outlines and s.on.probe and s.on.stats and s.on.map and not s.on.loading and not s.on.physics,
  'preset lod: exactly its overlays and panels')
dev.preset('loading')
s = dev.state()
T.check(s.on.loading and s.on.graphs and s.on.log and not s.on.tint and not s.on.probe and s.lastPreset == 'loading',
  'preset loading replaces lod')
dev.toggleLast()
T.check(not dev.state().active and dev._index() == nil, 'toggleLast: everything off')
for _ = 1, 10 do dev.onUpdate(DT, DT) end -- lets the lod tint restores run (lod tinted, loading did not)
dev.toggleLast()
T.check(dev.state().on.loading and dev.state().active, 'toggleLast: the last preset (loading) back on')
dev.preset('off')
for _ = 1, 10 do dev.onUpdate(DT, DT) end
T.check(not dev._busy() and dev._tintPending() == 0, 'preset off: idle again once restores ran')

------------------------------------------------------------------------------------------
-- the index follows farfield: adds, deletes, rebase, forget

beamngEarth_core = { _state = function() return nil end, _governor = function() return nil end }
dev.set('outlines', true)
local idx = dev._index()
local n0 = idx:count()
T.check(n0 == nTS(), 'index = objects (' .. n0 .. ')')
-- a coarser tile replaces 4 x 4 L-2 tiles (the L0 tile at 0,0 covers 0..256 = L-2 0..3)
farfield.onTile(tileMsg(1, 0, 0, 0, true))
local rem = {}
for i = 0, 3 do for j = 0, 3 do rem[#rem + 1] = key(-2, i, j) end end
farfield.onSwap({ type = 'far_swap', anchorId = 1, id = 3, add = { key(0, 0, 0) }, remove = rem })
for _ = 1, 5 do farfield.update(free, DT) end
T.check(idx:count() == nTS() and idx:count() == n0 + 1 - 16, 'index: +1 add, -16 deletes (' .. idx:count() .. ')')
local hit = idx:at(10, 10)
T.check(hit and hit.level == 0, 'index: the new L0 tile is found under (10, 10)')
-- rebase: footprints move, the index is rebuilt
local a1, a2 = geodesy.newAnchor(36.5, -118.3), geodesy.newAnchor(36.5, -118.29)
local xf = a1:transformTo(a2)
local t55 = farfield._tile(key(-2, 5, 5))
farfield.transform(xf)
farfield.switchAnchor(2, true, { cx = 0, cy = 0 })
local cx, cy = 0.5 * (t55.x0 + t55.x1), 0.5 * (t55.y0 + t55.y1)
T.check(math.abs(cx - (5.5 * 64)) > 100, 'rebase moved the footprint (' .. cx .. ')')
T.check(idx:count() == nTS() and idx:at(cx, cy) == t55, 'index rebuilt with the moved footprints')
T.check(dev.tileState(t55, farfield.anchorId()) == 'old', 'state: a previous anchor tile is "old"')
-- forget (level end): empty
farfield.clear()
T.check(idx:count() == 0 and nTS() == 0, 'clear: index empty')
dev.preset('off')

------------------------------------------------------------------------------------------
-- LOD tint: time-sliced, exact restore

farfield.switchAnchor(3, false, { cx = 0, cy = 0 })
local many = {}
for i = 0, 11 do for j = 0, 15 do many[#many + 1] = tileMsg(3, -2, i, j, true) end end -- 192 materials
for i = 0, 1 do many[#many + 1] = tileMsg(3, 2, 4 + i, 0, true) end                     -- 2 coarse ones
loadTiles(3, many, 10)
local mats = {}
for k, m in ipairs(many) do
  local o = scenetree.findObject(m.material)
  mats[#mats + 1] = { name = m.material, level = m.level, o = o }
  if k % 3 == 0 then o.fields.diffuseColor = string.format('%.3f 0.5 0.25 1', k / 1000) end -- some carry a colour
end
for _, m in ipairs(mats) do m.orig = m.o.fields.diffuseColor end
local function tinted()
  local n = 0
  for _, m in ipairs(mats) do if dev._tintState(m.name) then n = n + 1 end end
  return n
end
local st = { player = { x = 300, y = 300, z = 100, vx = 0, vy = 0, speed = 0, dist = 424 }, block = { cx = 0, cy = 0 },
  followCount = 0, rebaseCount = 0, status = '' }
beamngEarth_core = { _state = function() return st end, _governor = function() return nil end }
dev.set('tint', true)
T.check(tinted() == 0 and dev._tintPending() == #mats, 'tint on: every material queued, nothing changed yet')
dev.onUpdate(DT, DT)
local n1 = tinted()
T.check(n1 >= 1 and n1 <= dev.TINT_BATCH, 'tint: at most TINT_BATCH materials in a frame (' .. n1 .. ')')
local heavy = { isHeavyFrame = function() return true end }
dev.frame(DT, DT, st, heavy)
T.check(tinted() == n1, 'tint: nothing in a governor heavy frame')
dev.frame(DT, DT, st, { isHeavyFrame = function() return false end, blocked = true })
T.check(tinted() == n1, 'tint: nothing in a governor quiet frame')
local frames = 1
while dev._tintPending() > 0 and frames < 100 do dev.onUpdate(DT, DT); frames = frames + 1 end
T.check(tinted() == #mats and frames >= 3, 'tint: all ' .. #mats .. ' tinted over ' .. frames .. ' frames')
local okCol = true
for _, m in ipairs(mats) do if m.o.fields.diffuseColor ~= dev.tintColor(m.level, -2) then okCol = false end end
T.check(okCol, 'tint: every material has its level colour')
T.check(dev.tintColor(-2, -2) ~= dev.tintColor(0, -2) and dev.tintColor(2, -2) ~= dev.tintColor(6, -2),
  'tint: levels differ in colour')
T.check((S.calls.reload_Material or 0) >= #mats, 'tint: materials reloaded after the change')
-- a tile arriving while the tint is on gets it at creation (next frames)
farfield.onTile(tileMsg(3, -2, 30, 30, true))
farfield.onSwap({ type = 'far_swap', anchorId = 3, id = 11, add = { key(-2, 30, 30) }, remove = {} })
farfield.update(free, DT)
dev.onUpdate(DT, DT)
local newMat = scenetree.findObject('bngEarthSat_3_' .. key(-2, 30, 30))
T.check(newMat.fields.diffuseColor == dev.tintColor(-2, -2), 'tint: a new tile is tinted at creation')
-- one material deleted, one replaced by a new object of the same name while tinted
local gone, repl = mats[5], mats[6]
gone.o:delete()
repl.o:delete()
local fresh = createObject('Material')
fresh:setField('diffuseColor', 0, '0.1 0.2 0.3 1')
fresh:registerObject(repl.name)
-- off: restored in batches, exactly
dev.set('tint', false)
T.check(dev._busy(), 'tint off: busy while restores are queued')
frames = 0
while dev._tintPending() > 0 and frames < 100 do dev.onUpdate(DT, DT); frames = frames + 1 end
local exact = true
for _, m in ipairs(mats) do
  if m ~= gone and m ~= repl then
    local want = m.orig or dev.DEFAULT_DIFFUSE
    if m.o.fields.diffuseColor ~= want then exact = false; print('  not restored: ' .. m.name) end
  end
end
T.check(exact and frames >= 3, 'tint off: every material restored exactly, over ' .. frames .. ' frames')
T.check(fresh.fields.diffuseColor == '0.1 0.2 0.3 1', 'tint off: a replaced material keeps its own colour')
T.check(tinted() == 0, 'tint off: no tint state left')
dev.preset('off')
for _ = 1, 10 do dev.onUpdate(DT, DT) end
T.check(not dev._busy(), 'idle after the tint restores')

------------------------------------------------------------------------------------------
-- draw lists: outlines (radius, cap), loading order, probe, physics, rings

lines, texts, rays = 0, 0, 0
dev.setParam('outlineKm', 0.2)
dev.set('outlines', true)
dev.frame(DT, DT, st, nil)
local inR = 0
for _, m in ipairs(many) do
  local t = farfield._tile(m.key)
  if t and tileindex.dist2(t, 300, 300) <= 200 * 200 then inR = inR + 1 end
end
T.check(lines == 4 * inR and #dev._lists().outlines == 4 * inR, 'outlines: 4 lines per tile within the radius (' .. inR .. ')')
lines = 0
dev.frame(DT, DT, st, nil) -- cached lists drawn again, not rebuilt
T.check(lines == 4 * inR, 'outlines: drawn every frame from the cache')
dev.setParam('outlineKm', 50)
dev.frame(DT, DT, st, nil)
T.check(dev._index():count() > dev.MAX_OUTLINES and #dev._lists().outlines == 4 * dev.MAX_OUTLINES,
  'outlines: every tile within 50 km, at most MAX_OUTLINES')
dev.set('outlines', false)
-- probe: exact level under the player, then a miss
lines, texts = 0, 0
dev.set('probe', true)
dev.frame(DT, DT, st, nil)
T.check(lines == 2 and texts == 1 and rays >= 1, 'probe: a line, the tile tick and a label')
-- physics squares and rings
lines, rays = 0, 0
dev.set('physics', true)
dev.set('rings', true)
dev.frame(DT, DT, st, nil)
local L = dev._lists()
T.check(#L.physics > 0 and rays > 0 and #L.rings == 4 * dev.RING_SEGS + 4, 'physics squares ray-sampled, rings built')
T.check(lines <= dev.MAX_LINES, 'line cap respected (' .. lines .. ')')
dev.preset('off')
-- loading: the order peekImport takes (mesh, nearest the camera first; ties in queue order)
local q = {}
for i = 1, 5 do q[i] = { x0 = 1000 * (6 - i), y0 = 0, x1 = 1000 * (6 - i) + 64, y1 = 64, z = 0, level = -2 } end
local pre = { { x0 = 0, y0 = 0, x1 = 1, y1 = 1, z = 0, level = 3 } }
local ord = dev.nextImports(q, pre, { x = 0, y = 0 }, true, 10)
T.check(ord[1] == q[5] and ord[5] == q[1] and ord[6] == pre[1] and #ord == 6, 'loading: mesh order nearest first, prefetch last')
ord = dev.nextImports(q, pre, { x = 0, y = 0 }, false, 3)
T.check(ord[1] == q[1] and ord[3] == q[3] and #ord == 3, 'loading: terrain mode keeps the queue order')
-- the loading overlay on real queued tiles: numbered labels
farfield.onTile(tileMsg(3, -2, 40, 40, true))
farfield.onTile(tileMsg(3, -2, 41, 40, true))
texts = 0
dev.setParam('outlineKm', 10)
dev.set('loading', true)
dev.frame(DT, DT, st, nil)
T.check(#dev._lists().labels == 2 and texts == 2 and #dev._lists().loading == 8, 'loading: queued tiles outlined and numbered')
dev.preset('off')

------------------------------------------------------------------------------------------
-- controls undone when the dev tools go off; panels without imgui do nothing

dev.set('controls', true)
dev.freeze(true)
T.check(farfield.isFrozen(), 'freeze LOD on')
local wasRender = terrain.isRenderEnabled()
terrain.setRenderEnabled(false) -- mesh mode
dev.set('physicsTerrain', true)
T.check(terrain.isRenderEnabled() == true, 'physics terrain: rendered')
local dist0 = rebase.distance
st.player.dist = 5000
T.check(dev.forceRebase(st) and rebase.distance == 5000 - dev.FORCE_SLACK, 'force rebase: distance drops to the player')
T.check(not dev.forceRebase(st), 'force rebase: one at a time')
st.rebaseCount = 1
dev.frame(DT, DT, st, nil)
T.check(rebase.distance == dist0, 'force rebase: distance back after the rebase')
dev.forceRebase(st)
dev.preset('off')
T.check(not farfield.isFrozen() and terrain.isRenderEnabled() == false and rebase.distance == dist0,
  'off: freeze, physics terrain and a pending forced rebase undone')
terrain.setRenderEnabled(wasRender)
-- the event log
dev.set('log', true)
dev.frame(DT, DT, st, nil)
st.followCount, st.block = 1, { cx = 256, cy = 0 }
st.status = 'Anchor #4 (rebase)'
dev.frame(DT, DT, st, nil)
local ev, evN = dev._events()
T.check(evN == 4 and ev[1].text:match('force rebase') and ev[3].text:match('terrain commit')
  and ev[4].text == 'Anchor #4 (rebase)', 'log: force rebase, commit and status events, in order')
dev.preset('off')

------------------------------------------------------------------------------------------
-- every panel through a permissive imgui stub (catches errors in the drawing code), all on

local clicks = 0
local imText = {}
ui_imgui = setmetatable({
  BoolPtr = function(v) return { [0] = v } end,
  FloatPtr = function(v) return { [0] = v } end,
  ImVec2 = function(x, y) return { x = x, y = y } end,
  ImVec4 = function(x, y, z, w) return { x, y, z, w } end,
  GetColorU322 = function() return 0 end,
  GetCursorScreenPos = function() return { x = 100, y = 100 } end,
  GetMousePos = function() return { x = 100 + dev.MAP_PX / 2 + 1, y = 100 + dev.MAP_PX / 2 - 1 } end,
  IsItemClicked = function() clicks = clicks + 1; return true end,
  Begin = function() imText = {}; return true end,
  CollapsingHeader1 = function() return true end,
  TextUnformatted = function(t) imText[#imText + 1] = t end,
  Button = function() return false end,
  Checkbox = function() return false end,
  SliderFloat = function() return false end,
  IsItemActive = function() return false end,
  IsItemDeactivatedAfterEdit = function() return false end,
}, { __index = function() return function() end end })
local gov = { target = 2, est = { delete = 0.3 }, blocked = false }
function gov:isHeavyFrame() return false end
function gov:snapshot() return { b = 1.5, target = 2, loading = false, base = 11, estCdaeTex256 = 4, estCdaeTex512 = 4.2, estMaterial = 0.3 } end
beamngEarth_core = { _state = function() return st end, _governor = function() return gov end,
  setFarBudget = function(v) return v end, setRebaseDistance = function(v) return v end, reconnect = function() end }
st.cam = { x = 300, y = 300, z = 150, fx = 1, fy = 0, fz = 0, fovDeg = 60 }
st.player.speed, st.player.vx = 10, 10
dev.preset('all')
local okFrames = true
for _ = 1, 120 do
  local okF, e = pcall(dev.onUpdate, DT, DT)
  if not okF then okFrames = false; print('  ' .. tostring(e)); break end
end
T.check(okFrames, 'all on: 120 frames with every overlay and panel')
local joined = table.concat(imText, '\n')
T.check(joined:match('fps') and joined:match('governor') and joined:match('imports') and joined:match('dev tools'),
  'stats panel lines drawn')
T.check(clicks > 0 and joined:match('L%-2') ~= nil, 'mini-map: a click inspects the tile under it')
print(string.format('  dev tools cost with every overlay on, stubbed engine: %.3f ms/frame (EMA)', dev.state().costMs))
-- the loading map: the server's zone and map cells, drawn with how far ahead they are ready
T.check(table.concat(imText, '\n'):match('Not connected') ~= nil, 'loading map: no message yet, says so')
dev.receiveLoadMap({ type = 'load_map', x = 0, y = 0, vx = 0, vy = 30, on = true, lead = 1200, queued = 3,
  zone = { { 0, 0, 0 }, { 0, 300, 30 }, { 0, 600, 60 }, { 0, 3600, 360 } },
  cells = { { -800, -800, 800, 800, 3, 4.2 }, { -800, 800, 800, 2400, 2, 0 }, { -800, 2400, 800, 4000, 0, 0 } },
  stats = { cells = 2, s = 8.4 }, asked = 2.4,
  blocks = { { -256, -256, 256, 256, 6, 0 }, { -256, 128, 256, 640, 5, 2.4 }, { -256, 512, 256, 1024, 1, 0 } } })
for _ = 1, 3 do dev.onUpdate(DT, DT) end
local lm = table.concat(imText, '\n')
T.check(lm:match('ready ahead 1%.2 km') and lm:match('queued 3') and lm:match('loading 1') and lm:match('missing 1'),
  'loading map: lead, queue and cell states drawn')
T.check(lm:match('in use 1') and lm:match('ASKED 1') and lm:match('queued 1') and lm:match('not ready yet'),
  'loading map: blocks and the wait drawn')
dev.preset('off')
for _ = 1, 20 do dev.onUpdate(DT, DT) end
T.check(not dev._busy() and dev._index() == nil, 'off at the end')
farfield.clear()

-- the wireframe view: the engine's switch on and off again
GFXDevice = { renderWireframe = false }
dev.toggleWireframe()
T.check(GFXDevice.renderWireframe == true, 'wireframe: on')
dev.toggleWireframe()
T.check(GFXDevice.renderWireframe == false, 'wireframe: off again')
for _ = 1, 20 do dev.onUpdate(DT, DT) end
T.check(not dev._busy(), 'wireframe: nothing left running')
GFXDevice = nil

T.done('test_dev')
