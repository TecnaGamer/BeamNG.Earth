-- luajit mod/tests/test_map3d_turn.lua  (the 3D map compass: map3d.setHeading(h, true) turns smoothly the short way;
-- the user, 2026-11-08: "quick and smoothly rotate to north instead of snapping")
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/stubs.lua')
package.path = (arg[0]:match('^(.*)[/\\]') or '.') .. '/../beamng_earth/lua/ge/extensions/?.lua;' .. package.path
local m3 = require('beamngEarth/map3d')

local g = {heading = 1.0}
core_camera = core_camera or {}
local savedGet = core_camera.getGlobalCameras
core_camera.getGlobalCameras = function() return {[m3.CAMERA] = g} end
-- the map open (map3d's local `active`, read by its globe())
for i = 1, 60 do
  local n, f = debug.getupvalue(m3.setHeading, i)
  if n == 'globe' then
    for j = 1, 20 do local k = debug.getupvalue(f, j); if k == 'active' then debug.setupvalue(f, j, true) end end
  end
  if not n then break end
end

m3.setHeading(0)
T.check(g.heading == 0 and not m3.turning(), 'not smooth: set at once')

g.heading = 1.0
m3.setHeading(0, true)
T.check(g.heading == 1.0 and m3.turning(), 'smooth: nothing at once, a turn under way')
local prev, steady = g.heading, true
for _ = 1, 3 do m3.turnStep(g, m3.TURN_S / 8); if not (g.heading < prev) then steady = false end; prev = g.heading end
T.check(steady and g.heading > 0, 'turning towards north, a little each frame')
for _ = 1, 20 do m3.turnStep(g, m3.TURN_S / 8) end
T.check(math.abs(g.heading) < 1e-9 and not m3.turning(), 'at north after TURN_S, the turn over')

-- the short way round: from 350 degrees to north goes up through 360, not back through 180
g.heading = math.rad(350)
m3.setHeading(0, true)
m3.turnStep(g, m3.TURN_S / 4)
T.check(g.heading > math.rad(350), 'short way: 350 -> 360, not down through 180')
for _ = 1, 10 do m3.turnStep(g, m3.TURN_S / 4) end
T.check(math.abs(math.cos(g.heading) - 1) < 1e-9 and not m3.turning(), 'ends facing north')

-- the player turning the view during the turn takes over
g.heading = 2.0
m3.setHeading(0, true)
m3.turnStep(g, m3.TURN_S / 4)
g.heading = g.heading + 0.3          -- (a drag)
local mine = g.heading
m3.turnStep(g, m3.TURN_S / 4)
T.check(g.heading == mine and not m3.turning(), 'the player\'s own turn wins')

core_camera.getGlobalCameras = savedGet
T.done('test_map3d_turn')
