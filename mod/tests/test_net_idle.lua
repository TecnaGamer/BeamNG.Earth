-- luajit mod/tests/test_net_idle.lua  (off our level, no world server: the reconnects back off; GitHub #741)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local tries = 0
local fake = { settimeout = function() end, connect = function() tries = tries + 1; return nil, 'connection refused' end,
  setoption = function() end, close = function() end, getpeername = function() return nil end }
package.loaded.socket = { tcp = function() return fake end }
jsonEncode = function() return '{}' end
jsonDecode = function() return {} end
local N = require('beamngEarth/net')

local function run(seconds, step)
  for _ = 1, math.floor(seconds / step) do N.update(step) end
end
run(60, 0.1)
T.check(tries >= 25 and tries <= 31, 'in our level: every 2 s: ' .. tries)
tries = 0
N.setIdle(true)
run(120, 0.1)
T.check(tries >= 4 and tries <= 9, 'off it, no server: backing off to 30 s: ' .. tries)
local before = tries
run(300, 0.5)
T.check(tries - before >= 9 and tries - before <= 11, '... then every 30 s: ' .. (tries - before))
tries = 0
N.setIdle(false)
N.update(0.01)
T.check(tries == 1, 'our level loaded: a try at once')
run(10, 0.1)
T.check(tries >= 5 and tries <= 6, '... then every 2 s again: ' .. tries)
T.done('net_idle')
