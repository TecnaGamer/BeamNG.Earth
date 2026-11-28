-- luajit mod/tests/test_ground.lua  (ground.lua: the ground's height anywhere the world is drawn, api.groundHeight 1.13,
-- and the stock core_terrain.getTerrainHeight on our level; ~/bnge-tests/compat/AWS_COMPAT.md)
-- On the block: the terrain's own height, unchanged, nothing asked. Past it (where the stock query answers the block's
-- floor): nil until the app answers, then the land's height bilinear on a 16 m lattice (exact for a plane), a moving
-- point continuous across cells; one request carries the corners asked in a frame (BATCH at most, IN_FLIGHT at most),
-- a request not answered in WAIT_S is given up; no elevation there: nil and the stock answer; the asking cap; a new
-- anchor drops everything (an answer for the old one ignored). The wrap goes on at our level's start and comes off at
-- its end; a wrap another mod put over ours is left alone and never loops.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local lattice = require('beamngEarth/lattice')

-- the world: our level, a block at (0, 0) (collidable [-512, 511] at 1 m), the land past it a plane
local st = { inOurLevel = true, anchorId = 7, block = { cx = 0, cy = 0 } }
beamngEarth_core = { _state = function() return st end }
local ZBASE = -64
local function blockGround(x, y) return 120 + 0.02 * x - 0.01 * y end
local function land(x, y) return 300 + 0.05 * x + 0.03 * y end
local x0, y0, x1, y1 = lattice.groundBox(0, 0)
local tb = { getHeight = function(_, p)
  if p.x >= x0 and p.x <= x1 and p.y >= y0 and p.y <= y1 then return blockGround(p.x, p.y) end
  return ZBASE            -- (the engine past the block: its floor)
end }
local stock = function(p) return tb:getHeight(p) end
core_terrain = { getTerrain = function() return tb end, getTerrainHeight = stock }

local clock = 0
local sent = {}
local connected = true
local g = require('beamngEarth/ground')
g._now = function() return clock end
g._net = { isConnected = function() return connected end, send = function(m) sent[#sent + 1] = m end }
local noLand = {}          -- "x,y" -> true: no elevation there
-- the app: answers every request sent so far (earthd ground), optionally for another anchor
local function answer(aid, err)
  local reqs = sent
  sent = {}
  for _, m in ipairs(reqs) do
    local z = {}
    for q = 1, #m.pts / 2 do
      local x, y = m.pts[2 * q - 1], m.pts[2 * q]
      z[q] = not noLand[x .. ',' .. y] and land(x, y) or false       -- (false: no elevation there)
    end
    (package.loaded['beamngEarth/ground'] or g).onResult({ type = 'ground_result', id = m.id, anchorId = aid or m.anchorId,
      z = err and {} or z, error = err })
  end
  return #reqs
end

-- 1. off our level: nothing, and the stock query untouched
st.inOurLevel = false
T.check(g.height(2000, 0) == nil, 'off our level: no answer')
T.check(g.setLevel(false) == false and core_terrain.getTerrainHeight == stock, 'off our level: the stock query as it is')
st.inOurLevel = true
T.check(g.setLevel(true) == true and core_terrain.getTerrainHeight ~= stock and g.wrapped(), 'our level: wrapped')
T.check(g.setLevel(true) == true and g.wrapped(), 'wrapping twice: still one wrap')

-- 2. on the block: the terrain itself, the same as the stock query; nothing asked
local z, kind = g.height(100, -50)
T.check(kind == 'terrain', 'on the block: the terrain')
T.near(z, blockGround(100, -50), 1e-9, 'on the block: its height')
T.near(core_terrain.getTerrainHeight(vec3(100, -50, 0)), stock(vec3(100, -50, 0)), 1e-12, 'getTerrainHeight on the block unchanged')
T.check(not g.dirty and #sent == 0, 'nothing asked on the block')

-- 3. past the block: asked, nil meanwhile (getTerrainHeight the stock answer), then the land
T.check(stock(vec3(2000, 300, 0)) == ZBASE, 'the stock query past the block: the floor (the bug)')
T.check(g.height(2000, 300) == nil and g.dirty, 'past the block: not known yet, asked')
T.check(core_terrain.getTerrainHeight(vec3(2000.5, 300.25, 0)) == ZBASE, 'meanwhile getTerrainHeight: the stock answer')
g.height(2001, 301)
g.flush()
T.check(#sent == 1 and sent[1].type == 'ground' and sent[1].anchorId == 7 and #sent[1].pts == 8,
  'one request for the cell\'s four corners (asked twice in the frame)')
T.check(sent[1].pts[1] == 2000 and sent[1].pts[2] == 288, 'corners on the 16 m lattice')
answer()
T.check(not g.dirty, 'answered: nothing waiting')
z, kind = g.height(2000, 300)
T.check(kind == 'land', 'past the block: the land')
T.near(z, land(2000, 300), 1e-9, 'past the block: its height (bilinear is exact on a plane)')
T.near(core_terrain.getTerrainHeight(vec3(2000.5, 300.25, 0)), land(2000.5, 300.25), 1e-9,
  'getTerrainHeight past the block: the land too')
-- a point moving across cells: continuous (no step at the edge)
g.height(2015.999, 310); g.height(2016.001, 310); g.flush(); answer()
local a, b = g.height(2015.999, 310), g.height(2016.001, 310)
T.check(a and b and math.abs(a - b) < 0.01, string.format('no step at a cell edge (%s vs %s)', tostring(a), tostring(b)))

-- 4. many corners at once: BATCH a request, IN_FLIGHT at most, the rest next time
local inFlight = g.IN_FLIGHT
g.IN_FLIGHT = 2
for k = 1, 400 do g.height(5000 + k * 40, -6000) end
g.flush()
T.check(#sent == 2 and #sent[1].pts == 2 * g.BATCH, string.format('%d requests of %d corners', #sent, #sent[1].pts / 2))
T.check(g.dirty and g.stats().queued == 1600 - 2 * g.BATCH, 'the rest queued: ' .. g.stats().queued)
answer()
g.flush()
T.check(#sent == 2 and #sent[2].pts == 2 * (1600 - 3 * g.BATCH), 'the rest sent once answered')
g.IN_FLIGHT = inFlight
answer()
T.check(g.height(5040, -6000) ~= nil, 'answered: known')

-- 5. no elevation there: nil and the stock answer; an error answer: asked again later
noLand['-5008,7008'] = true
g.height(-5000, 7000); g.flush(); answer()
T.check(g.height(-5000, 7000) == nil and core_terrain.getTerrainHeight(vec3(-5000, 7000, 0)) == ZBASE,
  'no elevation at a corner: nil, the stock answer')
g.height(9000, 9000); g.flush(); answer(nil, 'OSError')
local r0 = g.stats().requests
for _ = 1, 50 do g.height(9000, 9000); g.flush() end
T.check(g.height(9000, 9000) == nil and #sent == 0 and g.stats().requests == r0,
  'an error answer: no height, and not asked again at once (an offline DEM tile: no request a frame)')
clock = clock + g.FAIL_S + 1
T.check(g.height(9000, 9000) == nil and g.dirty, 'FAIL_S later: asked again')
g.flush(); answer()
T.check(g.height(9000, 9000) ~= nil, 'then known')

-- 6. a lost request: given up after WAIT_S, its corners asked again
g.height(12000, 0); g.flush()
local lost = #sent
sent = {}
clock = clock + g.WAIT_S + 1
g.flush()
T.check(lost == 1 and g.stats().lost == 1, 'not answered in WAIT_S: given up')
g.height(12000, 0); g.flush()
T.check(#sent == 1, 'asked again')
answer()

-- 7. the asking cap: a whole map at once gets nil past it, no flood
clock = clock + 10
local before = g.stats().asked
for k = 1, 2000 do g.height(-20000 - k * 40, 20000) end
local asked = g.stats().asked - before
T.check(asked <= g.ASK_BURST and g.stats().capped > 0, string.format('capped: %d corners asked for 2000 new points', asked))
while g.dirty do g.flush(); answer() end
clock = clock + 2

-- 8. not connected: nothing asked
connected = false
T.check(g.height(40000, 40000) == nil and not g.dirty, 'no app: nothing asked')
connected = true

-- 9. a rebase or teleport (a new anchor): everything afresh, a late answer for the old one ignored
g.height(30000, 0); g.flush()
st.anchorId = 8
T.check(g.height(2000, 300) == nil, 'a new anchor: not known')
answer(7)
g.flush()
T.check(#sent == 1 and sent[1].anchorId == 8, 'asked for the new anchor')
answer()
T.check(g.height(2000, 300) ~= nil, 'known in the new anchor')

-- 10. the block moved: the new block's ground is the terrain
st.block = { cx = 2048, cy = 0 }
x0, y0, x1, y1 = lattice.groundBox(2048, 0)
z, kind = g.height(2000, 300)
T.check(kind == 'terrain', 'the block followed the player: terrain there now')
st.block = { cx = 0, cy = 0 }
x0, y0, x1, y1 = lattice.groundBox(0, 0)

-- 11. the level's end: the stock query back
g.setLevel(false)
T.check(core_terrain.getTerrainHeight == stock and not g.wrapped(), 'the level\'s end: the stock query back')

-- 12. another mod wraps over ours: left alone at the end, never a loop
g.setLevel(true)
g.height(2000, 300); g.flush(); answer()
local ours = core_terrain.getTerrainHeight
local theirs = function(p) return ours(p) end
core_terrain.getTerrainHeight = theirs
T.near(core_terrain.getTerrainHeight(vec3(2000, 300, 0)), land(2000, 300), 1e-9, 'their wrap over ours: ours under it')
g.setLevel(false)
T.check(core_terrain.getTerrainHeight == theirs, 'the level\'s end: their wrap left in place')
st.inOurLevel = false
T.check(core_terrain.getTerrainHeight(vec3(2000, 300, 0)) == ZBASE, 'off our level: through theirs to the stock answer')
st.inOurLevel = true
g.setLevel(true)            -- (the next level start: ours over theirs over our old one)
g.height(2000, 300); g.flush(); answer()
T.near(core_terrain.getTerrainHeight(vec3(2000, 300, 0)), land(2000, 300), 1e-9, 'wrapped again over theirs: no loop')
T.near(core_terrain.getTerrainHeight(vec3(10, 10, 0)), blockGround(10, 10), 1e-9, 'and on the block: no loop')
g._reset()
core_terrain.getTerrainHeight = stock

-- 13. be:getSurfaceHeightBelow: the physics' flat surface at z = 0 past the block answered as the land (once known);
-- the block, under the land, from below 0 and every other method as they were; another object sharing the metatable
-- not touched; the stock binding back at the level's end
do
  local physics = function(self, p)
    if p.z < 0 then return -1e20 end                     -- (from below the plane: its miss value)
    if p.x >= x0 and p.x <= x1 and p.y >= y0 and p.y <= y1 then return blockGround(p.x, p.y) end
    return 0                                             -- (past the block: the plane at 0)
  end
  local count = function() return 3 end
  local idx = function(t, k)
    if k == 'getSurfaceHeightBelow' then return physics end
    if k == 'getObjectCount' then return count end
  end
  local mt = { __index = idx }
  local oldBe = rawget(_G, 'be')
  be = setmetatable({}, mt)
  local other = setmetatable({}, mt)                      -- (another bound object, the same metatable)
  g._reset()
  g.setLevel(true)
  T.check(g.surfaceWrapped() and mt.__index ~= idx, 'our level: getSurfaceHeightBelow wrapped')
  T.near(be:getSurfaceHeightBelow(vec3(100, -50, 2000)), blockGround(100, -50), 1e-9, 'on the block: unchanged')
  T.check(be:getSurfaceHeightBelow(vec3(2000, 300, 3000)) == 0, 'past the block, not known yet: the plane (asked)')
  g.flush(); answer()
  T.near(be:getSurfaceHeightBelow(vec3(2000, 300, 3000)), land(2000, 300), 1e-9, 'past the block: the land, not 0')
  T.check(be:getSurfaceHeightBelow(vec3(2000, 300, 100)) == 0, 'a point under the land: the plane as it was')
  T.check(be:getSurfaceHeightBelow(vec3(2000, 300, -50)) == -1e20, 'from below the plane: its miss as it was')
  T.check(be:getObjectCount() == 3, 'every other method as it was')
  T.check(other.getSurfaceHeightBelow == physics, 'another object with the same metatable: not wrapped')
  g.setLevel(false)
  T.check(mt.__index == idx and not g.surfaceWrapped(), 'the level\'s end: the stock binding back')
  T.check(be:getSurfaceHeightBelow(vec3(2000, 300, 3000)) == 0, 'off our level: the plane')
  -- AWS's groundZ (aws.lua:205-216): be:getSurfaceHeightBelow from 2000 m over its hint, else getTerrainHeight
  g.setLevel(true)
  local function awsGround(x, y, zHint)
    local ok, h = pcall(be.getSurfaceHeightBelow, be, vec3(x, y, zHint + 2000))
    if ok and type(h) == 'number' and h > -1e6 then return h end
    ok, h = pcall(core_terrain.getTerrainHeight, vec3(x, y, 0))
    if ok and type(h) == 'number' and h > -1e6 then return h end
    return zHint
  end
  awsGround(6000, -3000, 1600); g.flush(); answer()
  T.near(awsGround(6000, -3000, 1600), land(6000, -3000), 1e-9, 'AWS\'s groundZ 6 km away: the land')
  T.near(awsGround(10, 10, 1600), blockGround(10, 10), 1e-9, 'AWS\'s groundZ on the block: the terrain')
  g._reset()
  be = oldBe
end

-- 14. the API's call
package.loaded['beamngEarth/net'] = { isConnected = function() return true end, send = function() end }
local api = require('beamngEarth/api')
g.setLevel(true)
api.groundHeight(2000, 300); g.flush(); answer()
z, kind = api.groundHeight(2000, 300)
T.check(kind == 'land' and math.abs(z - land(2000, 300)) < 1e-9, 'api.groundHeight: the land')
z, kind = api.groundHeight(5, 5)
T.check(kind == 'terrain', 'api.groundHeight on the block: terrain')
T.check(api.groundHeight('x', nil) == nil, 'api.groundHeight: bad coordinates, nil')
T.check(api._groundWrapped() == true and type(api._groundStats()) == 'table', 'probes: wrapped, stats')
g._reset()

-- 15. the review's points (2026-11-27): the queue's cap; a long queue sent in order by its head (no copies); an error
-- in our part of a wrap never reaches the caller; repeated level starts and a hot reload of the module stack no
-- second layer; a layer left under another mod's wrap is a plain pass-through
do
  local physics = function(self, p) if p.z < 0 then return -1e20 end return 0 end
  local idx = function(t, k) if k == 'getSurfaceHeightBelow' then return physics end end
  local mt = { __index = idx }
  local oldBe = rawget(_G, 'be')
  be = setmetatable({}, mt)
  g._reset()
  clock = clock + 100
  g.setLevel(true)
  -- the cap: past MAX_QUEUE corners waiting, a new one is not asked
  local cap, burst, perS, inF = g.MAX_QUEUE, g.ASK_BURST, g.ASK_PER_S, g.IN_FLIGHT
  g.MAX_QUEUE, g.ASK_BURST, g.ASK_PER_S, g.IN_FLIGHT = 100, 1e9, 1e9, 1
  g._reset(); g.setLevel(true)
  for k = 1, 200 do g.height(-30000 - k * 40, 30000) end
  T.check(g.stats().queued == 100 and g.stats().capped > 0, 'MAX_QUEUE: 100 corners waiting at most, the rest not asked')
  -- the head: sent in order, 512 a time, nothing copied
  g.MAX_QUEUE = 20000
  g._reset(); g.setLevel(true)
  for k = 1, 3000 do g.height(-30000 - k * 40, 31000) end
  local first = nil
  local sentKeys = 0
  while g.stats().queued > 0 do
    g.flush()
    first = first or sent[1].pts[1]
    sentKeys = sentKeys + #sent[#sent].pts / 2
    answer()
  end
  T.check(sentKeys == 12000 and first == math.floor((-30000 - 40) / 16) * 16, 'a long queue sent whole, in order: ' .. sentKeys)
  g.MAX_QUEUE, g.ASK_BURST, g.ASK_PER_S, g.IN_FLIGHT = cap, burst, perS, inF

  -- an error in our part (a broken block record) gives the engine's answer, never an error
  st.block = { cx = 'broken', cy = 0 }
  local okS, h = pcall(function() return be:getSurfaceHeightBelow(vec3(2000, 300, 3000)) end)
  local okT, h2 = pcall(function() return core_terrain.getTerrainHeight(vec3(2000, 300, 0)) end)
  T.check(okS and h == 0 and okT and h2 == ZBASE, 'an error in our part: the engine\'s answers, no error')
  st.block = { cx = 0, cy = 0 }

  -- level after level: one layer each, the stock functions back at the end
  for _ = 1, 5 do g.setLevel(true); g.setLevel(false) end
  T.check(mt.__index == idx and core_terrain.getTerrainHeight == stock, 'five level loads: no layer left, none stacked')

  -- a hot reload of the module while our level runs: the new module finds the old layers, adds none
  g.setLevel(true)
  local layerT, layerS = core_terrain.getTerrainHeight, mt.__index
  package.loaded['beamngEarth/ground'] = nil
  local g2 = require('beamngEarth/ground')
  g2._now, g2._net = g._now, g._net
  g2.setLevel(true)
  T.check(core_terrain.getTerrainHeight == layerT and mt.__index == layerS, 'a hot reload: the same layers, none stacked')
  g2.height(2000, 300); g2.flush(); answer()
  T.near(core_terrain.getTerrainHeight(vec3(2000, 300, 0)), land(2000, 300), 1e-9, 'the old layer asks the new module')
  g2.setLevel(false)
  T.check(mt.__index == idx and core_terrain.getTerrainHeight == stock, 'the reloaded module takes the old layers off')

  -- under another mod's wrap: our layer turned off is a plain pass-through, and on again next level, not doubled
  g2.setLevel(true)
  local ourIdx = mt.__index
  local theirIdx = function(t, k) return ourIdx(t, k) end
  mt.__index = theirIdx
  g2.setLevel(false)
  T.check(mt.__index == theirIdx and be:getSurfaceHeightBelow(vec3(2000, 300, 3000)) == 0, 'under their wrap, off: a pass-through')
  T.check(not g2.surfaceWrapped(), 'off')
  g2.setLevel(true)
  T.check(mt.__index == theirIdx and g2.surfaceWrapped(), 'on again under theirs: no second layer')
  g2.height(2000, 300); g2.flush(); answer()
  T.near(be:getSurfaceHeightBelow(vec3(2000, 300, 3000)), land(2000, 300), 1e-9, 'and working through theirs')
  g2._reset()
  mt.__index = idx
  rawset(_G, g2.SURFACE_KEY, nil)
  package.loaded['beamngEarth/ground'] = g
  be = oldBe
end

T.done('test_ground')
