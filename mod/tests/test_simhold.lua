-- luajit mod/tests/test_simhold.lua  (the mod's holds: the game's pause alone, our own car only in a BeamMP session)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
log = log or function() end
local H = require('beamngEarth/simhold')

local pausedSim = false
simTimeAuthority = { pause = function(on) pausedSim = on end, getPause = function() return pausedSim end }
local car = { id = 7, active = 1, cmds = {}, vel = 5 }
function car:setActive(a) self.active = a end
function car:queueLuaCommand(c) self.cmds[#self.cmds + 1] = c end
function car:getRefNodeId() return 0 end
function car:applyClusterVelocityScaleAdd(node, scale) self.vel = self.vel * scale end
core_environment = { getGravity = function() return -9.81 end }
function car:getID() return self.id end
getPlayerVehicle = function() return car end
getObjectByID = function(id) return id == car.id and car or nil end

-- single player: the game's pause
H.pause(true)
T.check(pausedSim and H.paused() and not H.carHeld() and car.active == 1, 'single player: the game paused, the car as it is')
H.pause(false)
T.check(not pausedSim and not H.paused(), 'single player: resumed')

-- a BeamMP session: only our car held (the other players' cars keep moving on our screen)
MPCoreNetwork = { isMPSession = function() return true end }
H.pause(true)
T.check(not pausedSim and H.paused() and H.carHeld(), 'BeamMP: our car held, the game not paused')
T.check(car.active == 1 and car.cmds[1] == 'obj:setGravity(0)' and car.vel == 0,
  'BeamMP: held by its gravity off and its velocity zeroed, never deactivated (it came back invisible)')
H.pause(false)
T.check(car.active == 1 and car.cmds[2] == 'obj:setGravity(-9.81)' and not H.paused() and not H.carHeld(),
  'BeamMP: our car let go, the level\'s gravity back')
MPCoreNetwork = nil
T.done('test_simhold')
