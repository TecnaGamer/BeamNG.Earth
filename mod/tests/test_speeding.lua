-- luajit mod/tests/test_speeding.lua (speeding.lua: each traffic car's habit over the limit by its country)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local S = require('beamngEarth/speeding')

T.near(S.offset(0), 3 / 3.6, 1e-9, 'minimum +3 km/h offset')
T.near(S.offset(1), 5 / 3.6, 1e-9, 'maximum +5 km/h offset')

-- the loop: a free standard car gets "limit" at its road's cap; a car in an action is released to "legal"
local sent = {}
local obj = { queueLuaCommand = function(_, c) sent[#sent + 1] = c end,
  getPosition = function() return { x = 0, y = 0, z = 0 } end }
function getObjectByID() return obj end
be = { getPlayerVehicleID = function() return 99 end }
local veh = { isAi = true, state = 'active', role = { name = 'standard', state = 'none', actionName = 'none' },
  tracking = { speedLimit = 29.06 } }
gameplay_traffic = { getTrafficData = function() return { [5] = veh } end }
package.loaded['beamngEarth/navgraph'] = { speedProfileAt = function() return { target = 29.06, rough = false } end }
math.randomseed(1)
S.update(1.0)
T.check(#sent == 1 and sent[1]:find('ai.setSpeedMode%("limit"%)') ~= nil, 'a free car gets its cap')
T.check(math.abs(tonumber(sent[1]:match('ai.setSpeed%(([%d%.]+)%)')) - (29.06 + S._cars()[5].over)) < 0.01,
  'normal road follows curve target plus its per-car overspeed')
T.check(S._cars()[5].over >= 3 / 3.6 and S._cars()[5].over <= 5 / 3.6, 'overspeed stays within 3-5 km/h')
S.update(1.0)
T.check(#sent == 1, 'unchanged: not sent again')
veh.role.actionName = 'pullOver'
S.update(1.0)
T.check(#sent == 2 and sent[2]:find('legal') ~= nil and S._cars()[5] == nil, 'an action: back to legal, let go')
veh.role.actionName = 'none'
package.loaded['beamngEarth/navgraph'].speedProfileAt = function() return { target = 29.06, rough = true } end
S.update(1.0)
T.check(#sent == 3 and S._cars()[5] ~= nil, 'free again: a new habit')
T.check(math.abs(tonumber(sent[3]:match('ai.setSpeed%(([%d%.]+)%)')) - 29.06) < 0.01,
  'rough road follows the legal limit with no overspeed')
S.reset()
T.check(next(S._cars()) == nil and sent[#sent]:find('legal') ~= nil, 'reset: all back to legal')

T.done('test_speeding')
