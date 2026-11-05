-- Offline checks for the independent Earth lane-follow controller math and terminal states.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local events = {}
input = { event = function(name, value) events[name] = value end }
ai = { setMode = function(mode) ai.mode = mode end }
local follower = dofile(dir .. '/../beamng_earth/lua/vehicle/extensions/beamngEarth_earthLaneFollower.lua')
local geManager = dofile(dir .. '/../beamng_earth/lua/ge/extensions/beamngEarth/earthai.lua')

T.check(geManager.speedTarget(30, 1) <= 30 and geManager.speedTarget(30, 20) <= 30,
  'varied driver profiles stay at or below the mapped speed limit')

local route = { { x = 0, y = 0 }, { x = 100, y = 0 }, { x = 200, y = 0 } }
local i, distance, remaining = follower.routeState(route, 25, 4, 1)
T.check(i == 1, 'route matching finds the current segment')
T.near(distance, 4, 1e-6, 'route matching reports lateral deviation')
T.near(remaining, 175, 1e-6, 'route matching reports remaining distance')
T.check(follower.steering(0, 0, 0, { x = 20, y = 0 }) == 0, 'straight lane produces zero steering')
local steer = follower.steering(0, 0, 0, { x = 5, y = 10 })
T.check(steer > 0 and steer <= 0.7, 'curved lookahead produces bounded steering')
local throttle, brake = follower.longitudinal(10, 20)
T.check(throttle > 0 and brake == 0, 'below target speed applies throttle only')
throttle, brake = follower.longitudinal(20, 10)
T.check(throttle == 0 and brake > 0, 'above target speed applies brake only')

follower.setRoute(route, 15)
T.check(ai.mode == 'disabled', 'the separate follower leaves native AI disabled')
obj = {
  getPosition = function() return { x = 10, y = 0, z = 0 } end,
  getVelocity = function() return { x = 5, y = 0, z = 0 } end,
  getDirectionVector = function() return { x = 1, y = 0, z = 0 } end,
}
follower.updateGFX(1 / 60)
T.check(events.steering == 0 and events.throttle > 0 and events.brake == 0,
  'update drives along the route through BeamNG input events')

obj.getPosition = function() return { x = 40, y = 30, z = 0 } end
follower.updateGFX(1 / 60)
T.check(follower.state().offRoute and not follower.state().finished,
  'off-route vehicle is distinguished from reaching the end of its route')

T.done('test_earth_lane_follower.lua')
