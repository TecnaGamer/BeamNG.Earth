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
    g.onResult({ type = 'ground_result', id = m.id, anchorId = aid or m.anchorId, z = err and {} or z, error = err })
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
T.check(g.height(9000, 9000) == nil and g.dirty, 'an error answer: asked again')
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

-- 13. the API's call
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

T.done('test_ground')
