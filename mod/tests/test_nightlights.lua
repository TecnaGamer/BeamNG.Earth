-- luajit mod/tests/test_nightlights.lua  (night lighting, nightlights.lua): the road tiles' lamp lenses kept per tile,
-- the nearest within reach, moved by a rebase, gone with their tile; day / night from the stock environment.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
os.clockhp = function() return 0 end

local nl = require('beamngEarth/nightlights')
local groups = { 'streetlight', 'lamplens', 'pole' }
nl.onRows('1:0_0', { { 0, 0, 0, 0 }, { 1, 10, 0, 8 }, { 1, 50, 0, 8 }, { 2, 5, 5, 0 }, { 1, 400, 0, 8 } }, groups)
nl.onRows('1:1_0', { { 1, 20, 3, 8 } }, groups)
T.check(nl.status().lenses == 4, 'the lens rows only: ' .. nl.status().lenses)
local near = nl.nearest(0, 0, 150, 2)
T.check(#near == 2 and near[1][1] == 10 and near[2][1] == 20, 'the two nearest within reach, nearest first')
T.check(#nl.nearest(0, 0, 150, 10) == 3, 'the one 400 m away left out')
local ahead = nl.nearest(0, 0, 500, 10, -1, 0, 30)
T.check(#ahead == 2 and ahead[1][1] == 10, 'looking away: only those within reach all round: ' .. #ahead)
T.check(#nl.nearest(0, 0, 500, 10, 1, 0, 30) == 4, 'looking at them: out to the far reach')
nl.transform({ apply = function(_, x, y, z) return x + 100, y, z end })
T.check(nl.nearest(110, 0, 1, 1)[1] ~= nil and nl.nearest(10, 0, 1, 1)[1] == nil, 'a rebase moves them')
nl.dropKey('1:0_0')
T.check(nl.status().lenses == 1, 'its tile goes: its lenses')
nl.onRows('1:1_0', { { 0, 1, 1, 1 } }, { 'streetlight' })
T.check(nl.status().lenses == 0, 'a tile without lenses: none kept')

core_environment = { getLightState = function() return { isNight = true } end }
T.check(nl.isNight() == true, 'night from the stock environment')
core_environment = nil
T.check(nl.isNight() == nil, 'no environment (the menu): unknown')
T.done('test_nightlights')
