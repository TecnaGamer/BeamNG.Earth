-- luajit mod/tests/test_spawnMotion.lua
-- Unit-test the per-wheel rolling impulse using a small synthetic pressure wheel.
local dir = (arg and arg[0] or ''):match('^(.*)[/\\]') or '.'
package.path = dir .. '/../beamng_earth/lua/vehicle/extensions/?.lua;' .. package.path
local T = dofile(dir .. '/testlib.lua')

local positions, forces = {}, {}
local bodyVelocity = { x = 30, y = 0, z = 0 }
local wheelNodeIds = {}
positions[1] = { x = 0, y = -0.1, z = 0 }
positions[2] = { x = 0, y = 0.1, z = 0 }
for i = 1, 12 do
  local angle = (i - 1) * (2 * math.pi / 12) - math.pi / 2
  local nodeId = i + 2
  positions[nodeId] = { x = 0.3 * math.cos(angle), y = 0, z = 0.3 * math.sin(angle) }
  wheelNodeIds[#wheelNodeIds + 1] = nodeId
end

obj = {
  getDirectionVector = function() return { x = 1, y = 0, z = 0 } end,
  getDirectionVectorUp = function() return { x = 0, y = 0, z = 1 } end,
  getVelocity = function() return bodyVelocity end,
  getNodePosition = function(_, nodeId)
    local p = positions[nodeId]
    if not p then error('unknown node ' .. tostring(nodeId)) end
    return p.x, p.y, p.z
  end,
  getNodeVelocityVectorXYZ = function(_, nodeId)
    if not positions[nodeId] then error('unknown node ' .. tostring(nodeId)) end
    return bodyVelocity.x, bodyVelocity.y, bodyVelocity.z
  end,
  getNodeMass = function(_, nodeId)
    if not positions[nodeId] then error('unknown node ' .. tostring(nodeId)) end
    return 0.05
  end,
  applyForceVector = function(_, nodeId, force)
    forces[nodeId] = { x = force.x, y = force.y, z = force.z }
  end,
}
float3 = function(x, y, z) return { x = x, y = y, z = z } end
physicsFPS = 2000
wheels = { wheels = { [0] = { name = 'FL', node1 = 1, node2 = 2, radius = 0.3 } } }
v = { data = { wheels = { [0] = { treadNodes = wheelNodeIds } } } }

local motion = require('beamngEarth.spawnMotion')
local result = motion.applyRollingSpeed(35)
T.check(result:find('applied wheels=1', 1, true) ~= nil,
  'the wheel node list is discovered and a rolling impulse is applied')
T.check(result:find('nodes=12', 1, true) ~= nil,
  'all twelve tyre tread nodes receive their matched rotational impulse')
T.check(result:find('rollingSpeed=30.0m/s', 1, true) ~= nil,
  'wheel spin matches the actual 30 m/s body velocity rather than overspinning to the 35 m/s request')

local fx, fy, fz, applied = 0, 0, 0, 0
for _, force in pairs(forces) do
  fx, fy, fz = fx + force.x, fy + force.y, fz + force.z
  applied = applied + 1
end
T.check(applied == 12, 'each tyre node gets one force-vector command')
T.check(math.abs(fx) < 0.001 and math.abs(fy) < 0.001 and math.abs(fz) < 0.001,
  'the per-wheel impulse is balanced to avoid an additional net chassis translation')
T.done('spawnMotion')
