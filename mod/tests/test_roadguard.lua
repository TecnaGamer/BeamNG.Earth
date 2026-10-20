-- luajit mod/tests/test_roadguard.lua  (KyzoFox 1.5.4: roadguard.lua and roads.squareState / onEmpty)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local rg = require('beamngEarth/roadguard')

-- the points: 4 s at 25 m/s = 100 m ahead
local pts = rg.points(0, 0, 25, 0)
T.check(pts[1][1] == 0 and math.abs(pts[#pts][1] - 100) < 1e-9, 'points: from the car to 4 s ahead')
T.check(#rg.points(5, 5, 0, 0) == 1, 'standing: its own square only')

-- a world: squares at x >= 200 not loaded yet
local ready = 199
local function state(x, y) return x < ready and 'ready' or 'missing' end
local g = rg.new()
T.check(rg.step(g, { x = 0, y = 0, vx = 25, vy = 0 }, false, 0, state) == nil, 'road ahead ready: driving on')
T.check(rg.step(g, { x = 120, y = 0, vx = 25, vy = 0 }, false, 1, state) == 'pause', 'a square ahead missing: hold')
T.check(g.last == 'missing', '... and why')
T.check(rg.step(g, { x = 120, y = 0, vx = 0, vy = 0 }, true, 2, state) == nil, 'held while it is missing')
ready = 1e9
T.check(rg.step(g, nil, true, 3, state) == 'resume', 'loaded: resumed')
T.check(rg.step(g, { x = 500, y = 0, vx = 25, vy = 0 }, false, 4, function() return 'missing' end) == nil,
  'no new hold within GRACE_S')
T.check(rg.step(g, { x = 500, y = 0, vx = 25, vy = 0 }, false, 20, function() return 'building' end) == 'pause',
  'a deck still queued ahead: hold')
T.check(rg.step(g, nil, true, 20 + rg.MAX_HOLD_S, function() return 'building' end) == 'resume', 'never longer than MAX_HOLD_S')

-- the player resumes by hand: the hold is over, no new one for GRACE_S
g = rg.new()
rg.step(g, { x = 0, y = 0, vx = 25, vy = 0 }, false, 0, function() return 'solidifying' end)
T.check(g.pausedByUs, 'held for a deck not solid yet')
T.check(rg.step(g, nil, false, 1, function() return 'solidifying' end) == nil and not g.pausedByUs, 'resumed by hand')
T.check(rg.step(g, { x = 0, y = 0, vx = 25, vy = 0 }, false, 2, function() return 'missing' end) == nil, '... respected')

-- slow cars and squares without roads are never held
g = rg.new()
T.check(rg.step(g, { x = 0, y = 0, vx = 2, vy = 0 }, false, 0, function() return 'missing' end) == nil, 'crawling: no hold')
T.check(rg.step(g, { x = 0, y = 0, vx = 30, vy = 0 }, false, 0, function() return 'empty' end) == nil, 'no roads there: no hold')
rg.enabled = false
T.check(rg.step(g, { x = 0, y = 0, vx = 30, vy = 0 }, false, 0, function() return 'missing' end) == nil, 'off: no hold')
rg.enabled = true

-- roads.squareState
local files = {}
readFile = function(p) return files[p] end
jsonDecode = function(s) return assert(load('return ' .. s))() end
be = be or {}
be.reloadCollision = function() end
local roads = require('beamngEarth/roads')
roads.COLLIDE_CHECK_S = 0
local car = { pos = vec3(10, 10, 0), vel = vec3(0, 0, 0) }
function car:getPosition() return self.pos end
function car:getVelocity() return self.vel end
getPlayerVehicle = function() return car end
local gov = {}
function gov:try() return self.no ~= true end
function gov:charge() end
T.check(roads.squareState(1, 10, 10) == 'missing', 'no tile yet: missing')
roads.onEmpty({ anchorId = 1, key = '1_0' })
T.check(roads.squareState(1, 300, 10) == 'empty', 'reported without roads: empty')
files['/t/b.json'] = '{ chunks = { }, deck = { shape = "/t/deck.cdae", pos = { 0, 0, 0 } } }'
gov.no = true
roads.onTile({ anchorId = 1, key = '0_0', path = '/t/b.json', n = 0 }, 1)
roads.update(gov, 1, car)
T.check(roads.squareState(1, 10, 10) == 'building', 'its deck queued: building')
gov.no = false
roads.URGENT_GAP_S, roads.RELOAD_EVERY_S = 1e9, 1e9
roads.update(gov, 1, car)
T.check(roads.squareState(1, 10, 10) == 'solidifying', 'deck made, collision not rebuilt: solidifying')
roads.RELOAD_EVERY_S = 0
roads.update(gov, 1, car)
T.check(roads.squareState(1, 10, 10) == 'ready', 'collision rebuilt: ready')
roads.onTile({ anchorId = 1, key = '1_0', path = '/t/b.json', n = 0 }, 1)
T.check(roads.squareState(1, 300, 10) ~= 'empty', 'a tile sent after all: no longer empty')

T.done('test_roadguard')
