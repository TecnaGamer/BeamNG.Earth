-- luajit mod/tests/test_aitune.lua  (traffic and police AI tuning: commands once per car, police tail before chasing)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local A = require('beamngEarth/aitune')

T.check(A.policeWant('chase', 1) == 'follow' and A.policeWant('chase', 2) == 'follow' and A.policeWant('chase', 3) == 'chase',
  'levels 1-2 tail, 3 chases')
T.check(A.policeWant('flee', 2) == nil and A.policeWant('chase', 0) == nil and A.policeWant('roadblock', 3) == nil,
  'not chasing: the stock role decides')
T.check(A.literal({ b = 2, a = 'x' }) == '{a = "x", b = 2}', 'literal: ' .. A.literal({ b = 2, a = 'x' }))
local f = loadstring('return ' .. A.literal(A.TRAFFIC))
T.check(f and f().trafficWaitTime == A.TRAFFIC.trafficWaitTime, 'the parameters literal is valid Lua')

local cmds = {}
local function obj(id)
  return { queueLuaCommand = function(_, c) cmds[#cmds + 1] = id .. ':' .. c end }
end
local modes = {}
local function car(name, state, level)
  local v = { isAi = true, role = { name = name, state = state, targetPursuitMode = level } }
  function v:setAiMode(m) modes[#modes + 1] = m end
  return v
end
local traffic = { [1] = car('standard'), [2] = car('police', 'chase', 2), [3] = { isAi = false, role = { name = 'standard' } } }
A.tick(traffic, obj)
local joined = table.concat(cmds, '\n')
T.check(joined:find('1:ai.setParameters({', 1, true) and joined:find('2:ai.setParameters({', 1, true), 'AI cars tuned')
T.check(not joined:find('3:', 1, true), 'the player (not AI) left alone')
T.check(modes[1] == 'follow' and joined:find('2:ai.driveInLane("off")', 1, true), 'level 2 police: tail, all lanes')
T.check(joined:find(string.format('2:ai.setAggression(%.3f)', A.FOLLOW_AGGRESSION), 1, true), 'tailing aggression')
local n = #cmds
A.tick(traffic, obj)
T.check(#cmds == n and #modes == 1, 'nothing sent again while nothing changed')
traffic[2].role.targetPursuitMode = 3
A.tick(traffic, obj)
T.check(cmds[#cmds] == string.format('2:ai.setAggression(%.3f)', A.CHASE_AGGRESSION) and #modes == 1,
  'level 3: the stock chase kept, less aggressive')
traffic[2].role.state = 'flee'
A.tick(traffic, obj)
traffic[2].role.state = 'chase'
local before = #cmds
A.tick(traffic, obj)
T.check(#cmds == before + 1, 'back to the chase after a dodge: toned down again')
traffic[1] = nil
A.tick(traffic, obj)
traffic[1] = car('standard')
before = #cmds
A.tick(traffic, obj)
T.check(#cmds == before + 1, 'a new car with an old id is tuned again')
T.done('test_aitune')
