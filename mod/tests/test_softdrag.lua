-- luajit mod/tests/test_softdrag.lua (softdrag.lua: a little drag only while the next block is late)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local D = require('beamngEarth/softdrag')

local box = { -1000, -1000, 1000, 1000 }
local function s(x, vx, cap) return D.step({ x = x, y = 0, vx = vx, vy = 0, box = box, warnCap = cap, dt = 0.1 }) end
T.check(s(900, 60, nil) == 1, 'no healthy server (no cap): no drag')
T.check(s(0, 60, 800) == 1, 'far from the edge: no drag')
T.check(s(900, 60, 1000) < 1, 'nearer than an on-time block lets it get, 1.4 s to the line: drag')
T.check(s(900, 60, 50) == 1, 'as near as an on-time block lets it get: none')
T.check(s(900, 10, 1000) == 1, 'slow: none')
T.check(s(900, -60, 1000) == 1, 'driving away from the edge: none')
local full, half = s(900, 60, 1000), s(700, 60, 1000)
T.check(full < half and half < 1, 'more the nearer the edge')
T.check(math.abs((1 - full) - D.K_MAX * 0.1) < 1e-9, 'at most K_MAX of the speed per second')
T.check(s(500, 60, 1000) == 1, '8 s away: none yet')
T.done('test_softdrag')
