-- luajit mod/tests/test_streetlights.lua
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local night, live = false, {}
core_environment = { getLightState = function() return { nightLightsActive = night } end }
local makeObject = createObject
createObject = function(class)
  local obj = makeObject(class)
  if class == 'PointLight' then
    local delete = obj.delete
    function obj:preApply() end
    function obj:postApply() end
    function obj:setLightEnabled(enabled)
      self.enabled = enabled
      self.fields.isEnabled = enabled and 'true' or 'false'
    end
    function obj:delete() live[self.id] = nil; delete(self) end
    live[obj.id] = obj
  end
  return obj
end

local lights = require('beamngEarth/streetlights')
local function tick(player, dt) lights.update(1, player, dt or 0.25) end
local function row(x, y, z, yaw, group)
  return { group or 'streetlight', x, y, z, yaw or 0, 1, group == 'polelamp' and 1.1 or 9, 0 }
end
local function light(key)
  for _, obj in pairs(live) do if obj.name == '' and key == 'any' then return obj end end
end

-- Daylight is governed by the engine's actual night-light state; no dynamic light is created by day.
local tile = '1:p0_0'
T.check(lights.add(tile, tile .. ':1', 'streetlight', row(0, 0, 0), 1, nil, nil), 'accept an existing stock streetlight item')
tick({ x = 0, y = 0, z = 0 })
T.check(lights.status().lights == 0, 'daytime creates no PointLights')

-- Glass-centroid coordinates from the stock eca_lamppost DAE, including the Forest row yaw/scale.
night = true
tick({ x = 0, y = 0, z = 0 })
local lamp = light('any')
T.check(lamp ~= nil and lights.status().lights == 1, 'night creates one emitter for the existing lamp')
if lamp then
  T.near(lamp.pos.x, 2.71129954, 1e-6, 'streetlight glass center x')
  T.near(lamp.pos.y, -0.000320829, 1e-6, 'streetlight glass center y')
  T.near(lamp.pos.z, 7.953926, 1e-6, 'streetlight glass center z')
  T.check(lamp.enabled == true and lamp.fields.nightLight == '0', 'emitter follows night state without environment rescans')
  T.check(lamp.fields.castShadows == 'false' and lamp.fields.radius == tostring(lights.RADIUS_M), 'bounded non-shadow light fields')
end

-- A Forest origin shift moves the live emitter with the same translation and BeamNG quaternion.
local root2 = math.sqrt(0.5)
lights.setTileTransform(tile, { 50, -20, 3 }, { 0, 0, -root2, root2 })
if lamp then
  T.near(lamp.pos.x, 50.000320829, 1e-5, 'rebase rotates the head offset x')
  T.near(lamp.pos.y, -17.28870046, 1e-5, 'rebase rotates the head offset y')
  T.near(lamp.pos.z, 10.953926, 1e-5, 'rebase translates the head offset z')
end

night = false
tick({ x = 0, y = 0, z = 0 })
T.check(lights.status().lights == 0 and #S.liveOf('PointLight') == 0, 'day transition removes the night emitter')
night = true

-- The electrical-pole cobra lamp is at the actual stock mesh's glass center, not the pole base.
lights.clear()
local poleTile = '2:p0_0'
lights.add(poleTile, poleTile .. ':1', 'polelamp', row(10, 20, 7, 90, 'polelamp'), 1, nil, nil)
tick({ x = 10, y = 20, z = 7 })
local pole = light('any')
T.check(pole ~= nil, 'night creates an emitter for an existing polelamp')
if pole then
  T.near(pole.pos.x, 9.999884204, 1e-5, 'polelamp glass center rotated x')
  T.near(pole.pos.y, 17.32407915, 1e-5, 'polelamp glass center rotated y')
  T.near(pole.pos.z, 7.4351099, 1e-5, 'polelamp glass center z')
end
-- Old-anchor ghosts and a new tile at the same mapped lamp location share one emitter.
lights.clear()
local oldTile, newTile = '1:p0_0', '2:p0_0'
lights.add(oldTile, oldTile .. ':1', 'streetlight', row(0, 0, 0), 1, nil, nil)
lights.add(newTile, newTile .. ':1', 'streetlight', row(0, 0, 0), 1, nil, nil)
lights.update(2, { x = 0, y = 0, z = 0 }, 0.25)
T.check(lights.status().lights == 1, 'rebased overlapping Forest items do not double the streetlight')


-- Emitters are spatially bounded, capped, and created in a fixed per-update budget.
lights.clear()
local capTile = '3:p0_0'
for i = 0, 39 do
  local x, y = (i % 8) * 5, math.floor(i / 8) * 5
  lights.add(capTile, capTile .. ':' .. tostring(i + 1), 'streetlight', row(x, y, 0), 1, nil, nil)
end
night = true
tick({ x = 0, y = 0, z = 0 })
T.check(lights.status().lights == lights.CREATE_BUDGET, 'one update does not exceed the creation budget')
for _ = 1, 8 do tick({ x = 0, y = 0, z = 0 }) end
T.check(lights.status().lights == lights.MAX_ACTIVE, 'active emitters never exceed the cap')
T.check(#S.liveOf('PointLight') == lights.MAX_ACTIVE, 'the scene contains only the capped PointLight set')

-- A missing player (vehicle mode exit) and a day transition disable and clean the owned set immediately.
tick(nil)
T.check(lights.status().lights == 0 and #S.liveOf('PointLight') == 0, 'no player retires all emitters')
lights.clear()
night = true
lights.add(capTile, capTile .. ':far', 'streetlight', row(1000, 1000, 0), 1, nil, nil)
tick({ x = 0, y = 0, z = 0 })
T.check(lights.status().lights == 0, 'distant lamps never receive emitters')

-- Dropped tiles, removals and level cleanup cannot leave PointLights behind.
lights.clear()
local cleanupTile = '4:p0_0'
lights.add(cleanupTile, cleanupTile .. ':1', 'streetlight', row(0, 0, 0), 1, nil, nil)
tick({ x = 0, y = 0, z = 0 })
T.check(lights.status().lights == 1, 'cleanup fixture has one active emitter')
lights.remove(cleanupTile .. ':1')
tick({ x = 0, y = 0, z = 0 })
T.check(lights.status().lights == 0, 'removed Forest item retires its emitter')
lights.add(cleanupTile, cleanupTile .. ':2', 'streetlight', row(0, 0, 0), 1, nil, nil)
tick({ x = 0, y = 0, z = 0 })
lights.dropTile(cleanupTile)
tick({ x = 0, y = 0, z = 0 })
T.check(lights.status().lights == 0, 'dropped tile retires its emitter')
lights.add(cleanupTile, cleanupTile .. ':3', 'streetlight', row(0, 0, 0), 1, nil, nil)
tick({ x = 0, y = 0, z = 0 })
lights.reset()
T.check(lights.status().lights == 0 and lights.status().items == 0 and #S.liveOf('PointLight') == 0, 'level reset deletes objects and clears candidate state')

T.done('streetlights')
