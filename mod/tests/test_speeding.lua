-- luajit mod/tests/test_speeding.lua (speeding.lua: each traffic car's habit over the limit by its country)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local S = require('beamngEarth/speeding')

local us, de = S.profile('US'), S.profile('de')
T.check(us[2] > de[2] and S.profile('BR') == S.PROFILES.default and S.profile(nil) == S.PROFILES.default, 'profiles')
local f, s = S.habit(us, 0.9, 0.5, 0.25)      -- u1 over the share: keeps to the limit
T.check(f <= 0 and f >= -0.03 and f == s, 'a driver who keeps to the limit')
f, s = S.habit(us, 0.1, 0.5, 0.25)            -- z = 0: the mean
T.near(f, us[2], 1e-9, 'a speeder at the mean')
T.near(s, us[2] * us[4], 1e-9, 'less on streets')
f = S.habit(us, 0.1, 1e-9, 0)                 -- a far tail: capped under the police check
T.check(f <= S.MAX_OVER and S.cap(30, f, 0) < 30 * 1.2, 'never 1.2x the limit')
T.near(S.cap(29.06, 0.10, 0.05), 31.966, 1e-3, '65 mph +10 % on a freeway')
T.near(S.cap(11.18, 0.10, 0.05), 11.739, 1e-3, '25 mph street: the street habit')

-- the loop: a free standard car gets "limit" at its road's cap; a car in an action is released to "legal"
local sent = {}
local obj = { queueLuaCommand = function(_, c) sent[#sent + 1] = c end }
function getObjectByID() return obj end
be = { getPlayerVehicleID = function() return 99 end }
local veh = { isAi = true, state = 'active', role = { name = 'standard', state = 'none', actionName = 'none' },
  tracking = { speedLimit = 29.06 } }
gameplay_traffic = { getTrafficData = function() return { [5] = veh } end }
math.randomseed(1)
S.update(1.0, { rules = { country = 'US' } })
T.check(#sent == 1 and sent[1]:find('ai.setSpeedMode%("limit"%)') ~= nil, 'a free car gets its cap')
S.update(1.0, { rules = { country = 'US' } })
T.check(#sent == 1, 'unchanged: not sent again')
veh.role.actionName = 'pullOver'
S.update(1.0, { rules = { country = 'US' } })
T.check(#sent == 2 and sent[2]:find('legal') ~= nil and S._cars()[5] == nil, 'an action: back to legal, let go')
veh.role.actionName = 'none'
S.update(1.0, { rules = { country = 'US' } })
T.check(#sent == 3 and S._cars()[5] ~= nil, 'free again: a new habit')
-- a rough road (OSM smoothness=bad or worse, navgraph's edge flag): a speeder keeps to the limit there (PR #1122)
local c5 = S._cars()[5]
c5.fast, c5.street, c5.sent = 0.10, 0.05, nil
obj.getPosition = function() return { x = 0, y = 0, z = 0 } end
package.loaded['beamngEarth/navgraph'] = { speedProfileAt = function() return { target = 29.06, rough = false } end }
S.update(1.0, { rules = { country = 'US' } })
T.near(tonumber(sent[#sent]:match('ai.setSpeed%(([%d%.]+)%)')), 29.06 * 1.10, 0.01, 'a smooth road: its habit')
package.loaded['beamngEarth/navgraph'].speedProfileAt = function() return { target = 29.06, rough = true } end
S.update(1.0, { rules = { country = 'US' } })
T.near(tonumber(sent[#sent]:match('ai.setSpeed%(([%d%.]+)%)')), 29.06, 0.01, 'a rough road: the limit, no speeding')
package.loaded['beamngEarth/navgraph'].speedProfileAt = function() error('no graph') end
T.check(S._rough(obj) == false, 'navgraph failing: not rough')
package.loaded['beamngEarth/navgraph'] = nil
T.check(S._rough(obj) == false, 'no navgraph: not rough')
S.reset()
T.check(next(S._cars()) == nil and sent[#sent]:find('legal') ~= nil, 'reset: all back to legal')

T.done('test_speeding')
