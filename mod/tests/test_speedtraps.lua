-- Simulate a car crossing a streamed French speed gate, including the light and the HUD.
local dir = arg[0]:match('^(.*)[/\\]') or '.'
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local speedtraps = require('beamngEarth/speedtraps')

T.check(not speedtraps.penalty(136.99, 130), 'under the fixed-camera margin on a motorway')
local measured, corrected, fine, criminal = speedtraps.penalty(137, 130)
T.check(measured == 137 and corrected == 130.15 and fine == 68 and not criminal, '130 motorway fine')
T.check(not speedtraps.penalty(55.99, 50), 'urban tolerance')
measured, corrected, fine, criminal = speedtraps.penalty(56, 50)
T.check(measured == 56 and corrected == 51 and fine == 135 and not criminal, '50 urban fine')
T.check(select(3, speedtraps.penalty(95, 70)) == 135, '20 km/h after tolerance, class 4')
measured, corrected, fine, criminal = speedtraps.penalty(110, 50)
T.check(fine == 300 and criminal, 'criminal fixed-rate fine')

local notices = {}
ui_message = function(message, _, channel)
  T.check(channel == 'beamngEarthSpeedTrap', 'standard HUD message channel')
  notices[#notices + 1] = message
end
local row = { '172', 0, 0, 100, 0, 5, 130, 0, -8, 101.22 }
local function drive(x, y, z, vx, id)
  speedtraps.update({ vehicleId = id or 1, x = x, y = y or 0, z = z or 101,
    vx = vx, vy = 0, vz = 0, speed = math.abs(vx) }, .08, .08)
end
speedtraps.onRows('g:tile', { row })
drive(-2, nil, nil, 140 / 3.6)
drive(2, nil, nil, 140 / 3.6)
T.check(#notices == 1 and notices[1]:find('140 km/h measured', 1, true)
  and notices[1]:find('€68', 1, true), 'crossing flashes once with measured speed and the fine')
local light
for _, o in pairs(S.objects) do if o.class == 'SpotLight' then light = o end end
T.check(light and light.fields.isEnabled == 'true', 'a real SpotLight flash is enabled')
drive(6, nil, nil, 140 / 3.6)
drive(10, nil, nil, 140 / 3.6)
T.check(light.fields.isEnabled == 'true', 'the pulse is still active at 160 ms')
drive(14, nil, nil, 140 / 3.6)
T.check(light.fields.isEnabled == 'false' and #notices == 1, 'flash expires and the pass is not counted twice')

for _, p in ipairs({ { 0, 101, -40 }, { 7, 101, 40 }, { 0, 110, 40 }, { 0, 101, 36 } }) do
  speedtraps.resetMotion()
  local forward = p[3] > 0 and 1 or -1
  drive(-2 * forward, p[1], p[2], p[3])
  drive(2 * forward, p[1], p[2], p[3])
end
T.check(#notices == 1, 'reverse, adjacent road, overpass and lawful speed are ignored')

speedtraps.resetMotion()
drive(-100, 0, 101, 40)
drive(100, 0, 101, 40)
speedtraps.resetMotion()
drive(-2, 0, 101, 40, 1)
drive(2, 0, 101, 40, 2)
T.check(#notices == 1, 'teleports and vehicle changes are ignored')

speedtraps.dropKey('g:tile')
drive(-2, 0, 101, 40)
drive(2, 0, 101, 40)
T.check(#notices == 1, 'dropped tiles do not enforce a limit')
speedtraps.clear()
T.check(S.objects[light.id] == nil, 'the flash object is cleared with the road world')
T.done('test_speedtraps')
