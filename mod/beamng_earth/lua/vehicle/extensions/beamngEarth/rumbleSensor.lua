-- Reports only the player vehicle's tyre contact points to the GE rumble-strip cue.
-- Loaded on demand by beamngEarth/rumble.lua; it does not inspect traffic vehicles or apply physics forces.
local M = {}

M.SAMPLE_S = 0.10
M.MAX_WHEELS = 16

local enabled = false
local elapsed = 0

local function finite(n)
  return type(n) == 'number' and n == n and n ~= math.huge and n ~= -math.huge
end

-- wheel.node1 is the wheel centre. Moving down by its radius gives a stable approximation of the tyre's road contact.
function M.contactPoint(center, up, radius)
  if type(center) ~= 'table' or type(up) ~= 'table' then return nil end
  local r = tonumber(radius)
  if not finite(r) or r < 0.1 or r > 2.5 then r = 0.3 end
  return { x = center.x - up.x * r, y = center.y - up.y * r, z = center.z - up.z * r }
end

local function nodePosition(nodeId)
  if not obj or nodeId == nil then return nil end
  local getter = obj.getAbsNodePosition or obj.getNodePosition
  if type(getter) ~= 'function' then return nil end
  local ok, a, b, c = pcall(getter, obj, nodeId)
  if not ok then return nil end
  if (type(a) == 'table' or type(a) == 'userdata') and finite(a.x) and finite(a.y) and finite(a.z) then
    return { x = a.x, y = a.y, z = a.z }
  end
  if finite(a) and finite(b) and finite(c) then return { x = a, y = b, z = c } end
  return nil
end

local function vehicleId()
  if not obj then return nil end
  for _, name in ipairs({ 'getID', 'getId' }) do
    local getter = obj[name]
    if type(getter) == 'function' then
      local ok, id = pcall(getter, obj)
      id = ok and tonumber(id) or nil
      if finite(id) and id >= 0 and id % 1 == 0 then return id end
    end
  end
  return nil
end

local function report()
  if not (obj and obj.queueGameEngineLua and wheels and type(wheels.wheels) == 'table') then return end
  local id = vehicleId()
  if not id then return end

  local up = { x = 0, y = 0, z = 1 }
  pcall(function()
    local directionUp = obj:getDirectionVectorUp()
    if directionUp and finite(directionUp.x) and finite(directionUp.y) and finite(directionUp.z) then
      up = { x = directionUp.x, y = directionUp.y, z = directionUp.z }
    end
  end)

  local keys = {}
  for key in pairs(wheels.wheels) do keys[#keys + 1] = key end
  table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
  local entries = {}
  for _, key in ipairs(keys) do
    if #entries >= M.MAX_WHEELS then break end
    local wheel = wheels.wheels[key]
    if type(wheel) == 'table' and wheel.node1 ~= nil then
      local center = nodePosition(wheel.node1)
      local contact = center and M.contactPoint(center, up, wheel.radius)
      if contact then
        entries[#entries + 1] = string.format('{index=%d,x=%.3f,y=%.3f,z=%.3f}',
          #entries + 1, contact.x, contact.y, contact.z)
      end
    end
  end

  local code = string.format('extensions.beamngEarth_core.rumbleSensorData(%d,{%s})', id, table.concat(entries, ','))
  pcall(obj.queueGameEngineLua, obj, code)
end

function M.setEnabled(on)
  enabled = on == true
  elapsed = 0
  if enabled then report() end
end

function M.updateGFX(dt)
  if not enabled then return end
  elapsed = elapsed + math.max(0, tonumber(dt) or 0)
  if elapsed < M.SAMPLE_S then return end
  elapsed = elapsed % M.SAMPLE_S
  report()
end

function M.onExtensionUnloaded()
  enabled = false
  elapsed = 0
end

return M
