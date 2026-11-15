local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/../beamng_earth/lua/vehicle/extensions/beamngEarth/spawnMotion.lua')
local cars
local function car(x, y, z, dx, dy, length, width)
  return { pos = {x, y, z or 0}, dir = {dx or 0, dy or 1, 0}, up = {0, 0, 1},
    length = length or 5, width = width or 2, height = 2 }
end
obj = { getId = function() return 1 end }
local clusters = {[0]=0,[1]=0}
v = {data={nodes={[0]={cid=0},[1]={cid=1}}}}
obj.getRefNodeId = function() return 0 end
obj.getNodeCluster = function(_,cid) return clusters[cid] end
for method, field in pairs({getObjectCenterPositionXYZ='pos', getObjectDirectionVectorXYZ='dir',
  getObjectDirectionVectorUpXYZ='up'}) do
  obj[method] = function(_, id) return unpack(cars[id][field]) end
end
for method, field in pairs({getObjectInitialLength='length', getObjectInitialWidth='width',
  getObjectInitialHeight='height'}) do
  obj[method] = function(_, id) return cars[id][field] end
end
mapmgr = { getObjects = function() return cars end }
ai = { mode = 'traffic' }
electrics = { values = { gearIndex = 1, throttle = 0.5, brake = 0, parkingbrake = 0 } }
local function clear(other, speed)
  cars = { [1] = car(0, 0), [2] = other }
  return S.canLaunch(speed or 25)
end
T.check(clear(nil), 'empty road retains the spawn boost')
electrics.values.gearIndex = 0
T.check(not clear(nil), 'a respawn still waiting in neutral is not launched')
electrics.values.gearIndex = -1
T.check(not clear(nil), 'a reversing intent is respected before the car starts moving')
electrics.values.gearIndex, electrics.values.throttle = 1, 0
T.check(not clear(nil), 'an AI without a forward drive request is left at rest')
electrics.values.throttle, electrics.values.brake = 0.5, 0.1
T.check(not clear(nil), 'the boost cannot override native braking')
electrics.values.brake, electrics.values.parkingbrake = 0, 1
T.check(not clear(nil), 'the boost cannot override the parking brake')
electrics.values.parkingbrake = 0
controller = { isFrozen = true }
T.check(not clear(nil), 'a public controller hold cannot be launched')
controller, ai.mode = nil, 'disabled'
T.check(not clear(nil), 'inactive AI cannot be launched')
ai.mode = 'traffic'
local values = electrics.values
electrics.values = nil
T.check(not clear(nil), 'missing control state leaves acceleration to the native AI')
electrics.values = values
T.check(clear(nil), 'a valid forward drive request retains the optional boost')
T.check(not clear(car(0, 20)), 'recorded rear-end: a car 20 m ahead prevents a highway launch')
T.check(not clear(car(0, 95)), 'leave braking distance, not merely enough room for the initial placement')
T.check(clear(car(0, 150)), 'a distant vehicle does not disable the boost')
T.check(clear(car(0, -15)), 'a vehicle safely behind does not block the launch')
T.check(not clear(car(0, -2)), 'overlapping vehicles also prevent the launch')
T.check(clear(car(3.5, 20)), 'separate parallel lanes do not block one another')
T.check(clear(car(-3.5, 20, 0, 0, -1)), 'an oncoming car in its own lane does not block the launch')
T.check(not clear(car(0, 20, 0, 0, -1)), 'oncoming traffic in the launch lane blocks it')
T.check(not clear(car(3, 20, 0, 1, 0, 12)), 'a sideways truck projects its length across the launch lane')
T.check(clear(car(0, 20, 8)), 'a vehicle on another level does not block the launch')
T.check(not clear(car(0, 150), 40), 'the clearance requirement grows with launch speed')
T.check(not clear(nil, 0), 'invalid launch speeds fail closed')
local saved = mapmgr
mapmgr = nil
T.check(not S.canLaunch(25), 'unavailable traffic data leaves acceleration to the stock AI')
mapmgr = saved
cars = { [1] = car(0, 0), [2] = car(0, 20) }
cars[2].active = false
T.check(not S.canLaunch(25), 'map active=false does not exclude an AI-controlled obstacle')

-- Exercise the actual queued command, including its failure behavior before any impulse.
local N = require('beamngEarth/navgraph')
local impulses, rolling, forward = 0, 0, 0
local direction = setmetatable({}, { __mul = function() return {} end })
obj.getDirectionVector = function() return direction end
obj.getVelocity = function() return { dot = function() return forward end } end
thrusters = { applyVelocity = function() impulses = impulses + 1 end }
extensions = { load = function() end }
local function launch(helper)
  impulses, rolling = 0, 0
  beamngEarth_spawnMotion = helper
  assert(loadstring(N.spawnVelocityCommand(25)))()
  return impulses
end
local helper = { canLaunch = S.canLaunch, applyRollingSpeed = function() rolling = rolling + 1 end }
T.check(launch(helper) == 0 and rolling == 0, 'blocked command applies neither body nor tyre impulse')
cars[2] = nil
T.check(launch(helper) == 1 and rolling == 1, 'clear command preserves both body and tyre impulses')
controller = {getControllersByType=function(name)
  T.check(name=='braking/postCrashBrake','query the stock controller type rather than a vehicle name')
  return {{name='customCrashController'}}
end}
T.check(launch(helper)==0 and rolling==0,'impact-sensitive vehicles receive no body or tyre boost')
controller.getControllersByType=function() return {} end
T.check(launch(helper)==1 and rolling==1,'vehicles without an impact detector retain the boost')
controller=nil
clusters[1]=783
T.check(launch(helper)==0 and rolling==0,'a separate mixer-style cluster prevents both chassis and tyre impulses')
clusters[1]=0
T.check(launch(helper)==1 and rolling==1,'a normal single-cluster vehicle retains the optional boost')
local originalNodes=v.data.nodes
v.data.nodes=nil
T.check(launch(helper)==0 and rolling==0,'unknown cluster layout leaves acceleration to stock AI')
v.data.nodes=originalNodes
clusters[0]=-1
T.check(launch(helper)==0 and rolling==0,'an invalid reference cluster cannot receive a boost')
clusters[0]=0
T.check(launch(nil) == 0, 'missing helper leaves acceleration to the stock AI')
T.check(launch({canLaunch=function() error('unavailable native data') end}) == 0,
  'clearance failure cannot apply a velocity boost')
forward = -2
T.check(launch(helper) == 0, 'a reversing vehicle is not boosted')
forward = 25
T.check(launch(helper) == 0, 'a vehicle already at launch speed is not boosted')
T.done('spawn motion clearance')
