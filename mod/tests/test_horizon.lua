-- luajit mod/tests/test_horizon.lua
-- beamngEarth/horizon.lua (docs/V1_4_SPEC.md 3.6 M1h): the visibleDistance target (+10 %, the
-- minimum, the hysteresis), LevelInfo with postApply, and the sky's planet radius applied and
-- restored (stubbed scene objects).
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local horizon = require('beamngEarth/horizon')

-- visibleTarget(radius, current, minimum)
T.near(horizon.visibleTarget(300000, 100000, 100000), 330000, 1e-6, 'radius 300 km: 330 km')
T.check(horizon.visibleTarget(80000, 100000, 100000) == nil, 'radius 80 km: the 100 km minimum, already set')
T.near(horizon.visibleTarget(80000, 330000, 100000), 100000, 1e-6, 'back down to the minimum')
T.check(horizon.visibleTarget(310000, 330000, 100000) == nil, '+3 %: within the hysteresis')
T.check(horizon.visibleTarget(270000, 330000, 100000) == nil, '-10 % exactly: within the hysteresis')
T.near(horizon.visibleTarget(260000, 330000, 100000), 286000, 1e-6, '-13 %: re-set')
T.near(horizon.visibleTarget(1500000, 330000, 100000), 1650000, 1e-6, 'the 1500 km cap: 1650 km')
T.near(horizon.visibleTarget(500000, nil, 100000), 550000, 1e-6, 'no current value: set')
for _, bad in ipairs({ 'x', -5, 0, 0 / 0, math.huge }) do
  T.check(horizon.visibleTarget(bad, 100000, 100000) == nil, 'no usable radius: keep ' .. tostring(bad))
end
T.check(horizon.visibleTarget(nil, 100000, 100000) == nil, 'nil radius: keep')

-- setVisibleDistance: the field, then postApply
local order = {}
scenetree.theLevelInfo = { visibleDistance = 100000 }
function scenetree.theLevelInfo:postApply() order[#order + 1] = 'postApply:' .. self.visibleDistance end
T.check(horizon.setVisibleDistance(330000) == true, 'set')
T.check(scenetree.theLevelInfo.visibleDistance == 330000 and order[1] == 'postApply:330000',
  'visibleDistance assigned, then postApply')
scenetree.theLevelInfo = nil
T.check(horizon.setVisibleDistance(1) == false, 'no LevelInfo: nothing')
scenetree.theLevelInfo = { visibleDistance = 5 }
T.check(horizon.setVisibleDistance(7) == true and scenetree.theLevelInfo.visibleDistance == 7, 'no postApply method: field only')

-- the sky's planet radius
T.check(horizon.applySky() == false and not horizon.skyApplied(), 'no ScatterSky: nothing')
T.check(horizon.restoreSky() == false, 'nothing to restore')
local sky = S.newObject('ScatterSky')
sky.atmoPlanetRadius = 6360
sky:registerObject('sunsky')
T.check(horizon.applySky() and sky.atmoPlanetRadius == 6371, 'applied: 6371 km (the tiles\' sphere)')
T.check(horizon.applySky() and sky.atmoPlanetRadius == 6371, 'applied twice')
T.check(horizon.restoreSky() and sky.atmoPlanetRadius == 6360, 'restored: the level\'s 6360 km (not the 6371 of the second apply)')
T.check(not horizon.skyApplied() and horizon.restoreSky() == false, 'restore once')
-- a new level: a new sky object is remembered on its own
horizon.applySky()
sky:delete()
local sky2 = S.newObject('ScatterSky')
sky2.atmoPlanetRadius = 6000
sky2:registerObject('sunsky2')
T.check(horizon.applySky() and sky2.atmoPlanetRadius == 6371, 'a new sky: applied')
T.check(horizon.restoreSky() and sky2.atmoPlanetRadius == 6000, 'a new sky: its own value restored')
horizon.applySky()
sky2:delete()
T.check(horizon.restoreSky() == false and not horizon.skyApplied(), 'the sky is gone: nothing to restore, state cleared')
-- applySky runs on every status message: the field is written only when it differs
local sky3 = S.newObject('ScatterSky')
local radius3, writes3 = 6360, 0
setmetatable(sky3, { __index = function(_, k) if k == 'atmoPlanetRadius' then return radius3 end end,
  __newindex = function(t, k, v) if k == 'atmoPlanetRadius' then radius3, writes3 = v, writes3 + 1 else rawset(t, k, v) end end })
sky3:registerObject('sunsky3')
horizon.applySky(); horizon.applySky(); horizon.applySky()
T.check(radius3 == 6371 and writes3 == 1, 'applied three times: one write')
T.check(horizon.restoreSky() and radius3 == 6360 and writes3 == 2, 'restored: one more write')
sky3:delete()

-- the atmosphere shell over a high camera (2026-11-06: objects drew black with the eye at / over its top)
T.check(horizon.thicknessFor(0, 100) == 100 and horizon.thicknessFor(59, 100) == 100 and horizon.thicknessFor(nil, 100) == 100,
  'thickness: the level\'s own up to 60 % of it')
T.check(horizon.thicknessFor(100, 100) == 200 and horizon.thicknessFor(400, 100) == 500 and horizon.thicknessFor(2000, 100) == 600,
  'thickness: 100 km over the camera, in 100 km steps, 600 at most')
T.done('test_horizon')
