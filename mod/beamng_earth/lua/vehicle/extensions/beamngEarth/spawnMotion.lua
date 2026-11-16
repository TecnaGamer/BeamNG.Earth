-- Synchronise tyre rotation with BeamNG.Earth's one-shot spawn velocity boost.
-- (From PR #728 by ProxyFirewall202, "Adaptive Traffic Density and AI Safety Improvements": this module as written
-- there; navgraph.lua's respawn launch calls it. Ported with the respawn speed, 2026-11-05.)
--
-- BeamNG does not expose a supported setter for soft-body node velocities.  The vehicle VM
-- does expose per-node force application, node masses/positions/velocities, and pressure-wheel
-- tread-node IDs.  We use those APIs to apply a one-physics-step tangential impulse to each
-- tyre's tread nodes after thrusters.applyVelocity has applied the vehicle's linear impulse.
--
-- The native vehicle AI, powertrain, brakes, steering, ABS and obstacle avoidance remain intact.

local M = {}

local MIN_NODE_RADIUS_RATIO = 0.35
local MAX_NODE_RADIUS_RATIO = 1.60
local MIN_WHEEL_NODES = 6
local MIN_DELTA_V = 0.02
local VECTOR_EPSILON = 0.0001

local function finite(n)
  return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

local function xyz(value, y, z)
  if type(value) == "number" then return value, y, z end
  if value ~= nil then return value.x, value.y, value.z end
  return nil, nil, nil
end

local function readNodePosition(nodeId)
  local ok, a, b, c = pcall(obj.getNodePosition, obj, nodeId)
  if not ok then return nil end
  local x, y, z = xyz(a, b, c)
  if not (finite(x) and finite(y) and finite(z)) then return nil end
  return x, y, z
end

local function readNodeVelocity(nodeId)
  local ok, a, b, c = pcall(obj.getNodeVelocityVectorXYZ, obj, nodeId)
  if not ok then return nil end
  local x, y, z = xyz(a, b, c)
  if not (finite(x) and finite(y) and finite(z)) then return nil end
  return x, y, z
end

local function cross(ax, ay, az, bx, by, bz)
  return ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
end

local function dot(ax, ay, az, bx, by, bz)
  return ax * bx + ay * by + az * bz
end

local function addNodeIds(result, seen, source)
  if type(source) ~= "table" then return end
  for key, value in pairs(source) do
    local nodeId
    if type(value) == "number" or type(value) == "string" then
      nodeId = tonumber(value)
    elseif type(value) == "table" then
      nodeId = tonumber(value.cid or value.nodeId or value.id)
    end
    -- Also support node-ID keyed sets such as { [cid] = true }, without mistaking
    -- an array index for a node ID when an entry contains an unresolved node name.
    if not nodeId and type(value) == "boolean" then nodeId = tonumber(key) end
    if nodeId and nodeId >= 0 and nodeId % 1 == 0 and not seen[nodeId] then
      seen[nodeId] = true
      result[#result + 1] = nodeId
    end
  end
end

local function getVehicleWheelData(wheelId, wheel)
  if not (v and v.data and type(v.data.wheels) == "table") then return nil end
  local dataWheels = v.data.wheels
  local data = dataWheels[wheelId]
  if data then return data end
  if wheel and wheel.name then
    data = dataWheels[wheel.name]
    if data then return data end
    for _, candidate in pairs(dataWheels) do
      if type(candidate) == "table" and candidate.name == wheel.name then
        return candidate
      end
    end
  end
  return nil
end

local function collectWheelNodes(wheelId, wheel)
  local result, seen = {}, {}
  local dataWheel = getVehicleWheelData(wheelId, wheel)

  -- `v.data.wheels[id].treadNodes` is the wheel generator's pressure-wheel node list.
  -- The other fields are optional aliases used by some vehicle/controller implementations.
  addNodeIds(result, seen, wheel.nodes)
  addNodeIds(result, seen, wheel.treadNodes)
  addNodeIds(result, seen, wheel.hubNodes)
  addNodeIds(result, seen, wheel.wheelNodes)
  if dataWheel then
    addNodeIds(result, seen, dataWheel.treadNodes)
    addNodeIds(result, seen, dataWheel.nodes)
    addNodeIds(result, seen, dataWheel.hubNodes)
    addNodeIds(result, seen, dataWheel.wheelNodes)
  end
  return result
end

local function getPhysicsFrequency()
  -- `physicsFPS` is available in vehicle Lua; 2000 Hz is BeamNG's standard physics rate.
  local frequency = tonumber(physicsFPS)
  if not finite(frequency) or frequency < 100 or frequency > 10000 then
    frequency = 2000
  end
  return frequency
end

-- Bring the tread nodes' tangential velocity up to the rolling velocity of the body.  Applying
-- a balanced set of tangential forces (net force approximately zero) creates a wheel-spin couple
-- without adding a second forward kick to the vehicle.
function M.applyRollingSpeed(requestedTargetMps)
  local targetMps = tonumber(requestedTargetMps)
  if not finite(targetMps) or targetMps <= 0 or targetMps > 80 then
    return "invalid-target"
  end
  if not (obj and type(obj.getDirectionVector) == "function"
      and type(obj.getDirectionVectorUp) == "function"
      and type(obj.getVelocity) == "function"
      and type(obj.getNodeMass) == "function"
      and type(obj.getNodePosition) == "function"
      and type(obj.getNodeVelocityVectorXYZ) == "function"
      and type(obj.applyForceVector) == "function"
      and type(float3) == "function"
      and wheels and type(wheels.wheels) == "table") then
    return "required-node-physics-api-unavailable"
  end

  local dir = obj:getDirectionVector()
  local up = obj:getDirectionVectorUp()
  local dx, dy, dz = dir.x, dir.y, dir.z
  local ux, uy, uz = up.x, up.y, up.z
  local directionLength = math.sqrt(dx * dx + dy * dy + dz * dz)
  local upLength = math.sqrt(ux * ux + uy * uy + uz * uz)
  if directionLength < 0.5 or upLength < 0.5 then return "invalid-vehicle-orientation" end
  dx, dy, dz = dx / directionLength, dy / directionLength, dz / directionLength
  ux, uy, uz = ux / upLength, uy / upLength, uz / upLength

  local bodyVelocity = obj:getVelocity()
  local bvx, bvy, bvz = bodyVelocity.x, bodyVelocity.y, bodyVelocity.z
  if not (finite(bvx) and finite(bvy) and finite(bvz)) then return "invalid-body-velocity" end

  -- The body helper may land a little below the requested target because of its physics-step
  -- timing.  Match the wheels to the actual forward velocity when available, never above target.
  local bodyForwardSpeed = dot(bvx, bvy, bvz, dx, dy, dz)
  local rollingSpeed = targetMps
  if finite(bodyForwardSpeed) and bodyForwardSpeed > math.max(0.5, targetMps * 0.15) then
    rollingSpeed = math.min(targetMps, bodyForwardSpeed)
  end

  local stepSeconds = 1 / getPhysicsFrequency()
  local wheelCount, nodeCount, failedNodes, skippedWheels = 0, 0, 0, 0
  local maxDeltaV, peakForce = 0, 0

  for wheelId, wheel in pairs(wheels.wheels) do
    if type(wheel) == "table" then
      local radius = tonumber(wheel.radius)
      local node1, node2 = tonumber(wheel.node1), tonumber(wheel.node2)
      if finite(radius) and radius > 0.08 and node1 and node2 then
        local x1, y1, z1 = readNodePosition(node1)
        local x2, y2, z2 = readNodePosition(node2)
        if x1 and x2 then
          local ax, ay, az = x2 - x1, y2 - y1, z2 - z1
          local axisLength = math.sqrt(ax * ax + ay * ay + az * az)
          if axisLength > 0.02 then
            ax, ay, az = ax / axisLength, ay / axisLength, az / axisLength
            local cx, cy, cz = (x1 + x2) * 0.5, (y1 + y2) * 0.5, (z1 + z2) * 0.5
            local wheelNodes = {}
            local bottomRx, bottomRy, bottomRz, bottomScore = nil, nil, nil, math.huge

            for _, nodeId in ipairs(collectWheelNodes(wheelId, wheel)) do
              if nodeId ~= node1 and nodeId ~= node2 then
                local px, py, pz = readNodePosition(nodeId)
                if px then
                  local rx, ry, rz = px - cx, py - cy, pz - cz
                  local axial = dot(rx, ry, rz, ax, ay, az)
                  rx, ry, rz = rx - ax * axial, ry - ay * axial, rz - az * axial
                  local radialLength = math.sqrt(rx * rx + ry * ry + rz * rz)
                  -- Keep hub/tread nodes which participate in wheel rotation; reject axle nodes.
                  if radialLength > radius * MIN_NODE_RADIUS_RATIO
                      and radialLength < radius * MAX_NODE_RADIUS_RATIO then
                    wheelNodes[#wheelNodes + 1] = {
                      id = nodeId, rx = rx, ry = ry, rz = rz, radialLength = radialLength
                    }
                    local verticalScore = dot(rx, ry, rz, ux, uy, uz)
                    if verticalScore < bottomScore then
                      bottomRx, bottomRy, bottomRz, bottomScore = rx, ry, rz, verticalScore
                    end
                  end
                end
              end
            end

            if #wheelNodes >= MIN_WHEEL_NODES and bottomRx then
              -- Select spin sign so the bottom-of-tyre tangent points in the vehicle's forward
              -- direction, independent of whether this wheel's axle nodes are ordered L->R or R->L.
              local bottomTx, bottomTy, bottomTz = cross(ax, ay, az, bottomRx, bottomRy, bottomRz)
              local spinSign = dot(bottomTx, bottomTy, bottomTz, dx, dy, dz) >= 0 and 1 or -1
              local forceRows, sumFx, sumFy, sumFz = {}, 0, 0, 0

              for _, node in ipairs(wheelNodes) do
                local tx, ty, tz = cross(ax, ay, az, node.rx, node.ry, node.rz)
                local tangentLength = math.sqrt(tx * tx + ty * ty + tz * tz)
                if tangentLength > VECTOR_EPSILON then
                  tx, ty, tz = tx / tangentLength * spinSign,
                    ty / tangentLength * spinSign, tz / tangentLength * spinSign
                  local nvx, nvy, nvz = readNodeVelocity(node.id)
                  local massOk, mass = pcall(obj.getNodeMass, obj, node.id)
                  mass = massOk and tonumber(mass) or nil
                  if nvx and finite(mass) and mass > 0 then
                    local currentTangential = dot(nvx - bvx, nvy - bvy, nvz - bvz, tx, ty, tz)
                    local desiredTangential = rollingSpeed * (node.radialLength / radius)
                    local deltaV = desiredTangential - currentTangential
                    local fx, fy, fz = 0, 0, 0
                    if math.abs(deltaV) > MIN_DELTA_V then
                      local magnitude = mass * deltaV / stepSeconds
                      fx, fy, fz = tx * magnitude, ty * magnitude, tz * magnitude
                      if math.abs(deltaV) > maxDeltaV then maxDeltaV = math.abs(deltaV) end
                    end
                    forceRows[#forceRows + 1] = { id = node.id, x = fx, y = fy, z = fz }
                    sumFx, sumFy, sumFz = sumFx + fx, sumFy + fy, sumFz + fz
                  else
                    failedNodes = failedNodes + 1
                  end
                end
              end

              if #forceRows >= MIN_WHEEL_NODES then
                -- Remove the mean force from the ring so the rotational impulse has no net
                -- translation component even when node spacing/masses are uneven.
                local meanFx, meanFy, meanFz = sumFx / #forceRows, sumFy / #forceRows, sumFz / #forceRows
                local appliedOnWheel = 0
                for _, row in ipairs(forceRows) do
                  local fx, fy, fz = row.x - meanFx, row.y - meanFy, row.z - meanFz
                  local forceLength = math.sqrt(fx * fx + fy * fy + fz * fz)
                  if forceLength > VECTOR_EPSILON then
                    local ok = pcall(obj.applyForceVector, obj, row.id, float3(fx, fy, fz))
                    if ok then
                      appliedOnWheel = appliedOnWheel + 1
                      if forceLength > peakForce then peakForce = forceLength end
                    else
                      failedNodes = failedNodes + 1
                    end
                  end
                end
                if appliedOnWheel > 0 then
                  wheelCount = wheelCount + 1
                  nodeCount = nodeCount + appliedOnWheel
                else
                  skippedWheels = skippedWheels + 1
                end
              else
                skippedWheels = skippedWheels + 1
              end
            else
              skippedWheels = skippedWheels + 1
            end
          else
            skippedWheels = skippedWheels + 1
          end
        else
          skippedWheels = skippedWheels + 1
        end
      else
        skippedWheels = skippedWheels + 1
      end
    end
  end

  if wheelCount == 0 or nodeCount == 0 then
    return string.format("no-wheel-nodes (failed=%d skipped=%d)", failedNodes, skippedWheels)
  end
  return string.format(
    "applied wheels=%d nodes=%d rollingSpeed=%.1fm/s maxNodeDeltaV=%.1fm/s peakNodeForce=%.0fN failed=%d skipped=%d",
    wheelCount, nodeCount, rollingSpeed, maxDeltaV, peakForce, failedNodes, skippedWheels)
end

return M
