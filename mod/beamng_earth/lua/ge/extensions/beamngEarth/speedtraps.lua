-- Directional speed gates in streamed road tiles. The sampled segment is swept
-- through the gate, so a fast vehicle need not land on it on a particular frame.
local M = {}
local tiles = {}
local previous, lightId, lightKey
local flashLeft = 0
local FLASH_S = 0.20

-- Fixed-camera technical margin (Arrêté du 4 juin 2009, article 6).
-- Compare in hundredths of km/h so 137 measured at a 130 limit gives 130.15.
function M.penalty(speed, limit)
  local measured = math.floor(speed + 1e-7)
  local corrected = measured >= 100 and measured * 95 or (measured - 5) * 100
  local excess = corrected - limit * 100
  if excess <= 0 then return nil end
  if excess >= 5000 then return measured, corrected / 100, 300, true end
  return measured, corrected / 100, (excess < 2000 and limit > 50) and 68 or 135, false
end

function M.crossing(gate, before, after)
  local bx, by = before.x - gate.x, before.y - gate.y
  local ax, ay = after.x - gate.x, after.y - gate.y
  local b = bx * gate.dx + by * gate.dy
  local a = ax * gate.dx + ay * gate.dy
  if b >= 0 or a < 0 then return nil end
  local t = b / (b - a)
  local x, y = bx + t * (ax - bx), by + t * (ay - by)
  if math.abs(x * gate.dy - y * gate.dx) > gate.half then return nil end
  local z = before.z + t * (after.z - before.z) - gate.z
  if z < -1 or z > 3.5 then return nil end
  local vx = before.vx + t * (after.vx - before.vx)
  local vy = before.vy + t * (after.vy - before.vy)
  local vz = before.vz + t * (after.vz - before.vz)
  if vx * gate.dx + vy * gate.dy < 0.75 * math.sqrt(vx * vx + vy * vy) then return nil end
  return t, math.sqrt(vx * vx + vy * vy + vz * vz) * 3.6
end

function M.onRows(key, rows)
  local gates = {}
  for _, row in ipairs(rows or {}) do
    local heading = math.rad(row[5])
    gates[#gates + 1] = { id = row[1], x = row[2], y = row[3], z = row[4],
      dx = math.cos(heading), dy = math.sin(heading), half = row[6], limit = row[7],
      lightX = row[8], lightY = row[9], lightZ = row[10] }
  end
  tiles[key] = #gates > 0 and gates or nil
end

local function stopFlash()
  local light = lightId and scenetree.findObjectById(lightId)
  if light then light:setField('isEnabled', 0, 'false') end
  lightKey, flashLeft = nil, 0
end

function M.dropKey(key)
  tiles[key] = nil
  if key == lightKey then stopFlash() end
end

function M.resetMotion()
  previous = nil
  if lightKey then stopFlash() end
end

function M.clear()
  M.resetMotion()
  local light = lightId and scenetree.findObjectById(lightId)
  if light then light:delete() end
  lightId, tiles = nil, {}
end

local function flash(key, gate)
  if not scenetree.MissionGroup then return end
  local light = lightId and scenetree.findObjectById(lightId)
  if not light then
    light = createObject('SpotLight')
    if not light then return end
    light.canSave = false
    light:setField('range', 0, '30')
    light:setField('innerAngle', 0, '65')
    light:setField('outerAngle', 0, '125')
    light:setField('brightness', 0, '48')
    light:setField('color', 0, '1 0.97 0.91 1')
    light:setField('castShadows', 0, 'false')
    light:setField('isEnabled', 0, 'false')
    light:registerObject('')
    scenetree.MissionGroup:addObject(light)
    lightId = light:getID()
  end
  local x, y, z = gate.lightX, gate.lightY, gate.lightZ
  -- Throw the pulse across the lane and into the road ahead of the driver.
  local dir = vec3(gate.x - x + gate.dx * 6, gate.y - y + gate.dy * 6, gate.z + 0.7 - z)
  local q = quatFromDir(dir, vec3(0, 0, 1))
  light:setPosRot(x, y, z, q.x, q.y, q.z, q.w)
  light:setField('isEnabled', 0, 'true')
  lightKey, flashLeft = key, FLASH_S
end

local function notify(measured, corrected, limit, fine, criminal)
  if not ui_message then return end
  local retained = string.format('%.2f', corrected):gsub('0+$', ''):gsub('%.$', '')
  local amount = '€' .. fine
  if criminal then amount = amount .. ' fixed-rate (up to €3,750 in court)' end
  ui_message(string.format('Speed camera. %d km/h measured, %s km/h after tolerance, limit %d km/h. Simulated fine %s.',
    measured, retained, limit, amount), 7, 'beamngEarthSpeedTrap')
end

function M.update(player, dtSim, dtReal)
  if lightKey then
    flashLeft = flashLeft - dtReal
    if flashLeft <= 0 then stopFlash() end
  end
  if not player or not player.vehicleId or dtSim <= 0 or next(tiles) == nil then
    previous = nil
    return
  end
  local before = previous
  previous = player
  if not before or before.vehicleId ~= player.vehicleId or dtSim > .35 then return end
  local dx, dy, dz = player.x - before.x, player.y - before.y, player.z - before.z
  local reach = math.max(4, 1.8 * math.max(player.speed, before.speed) * dtSim + 1.5)
  if dx * dx + dy * dy + dz * dz > reach * reach then return end
  local best, bestT, key, measured, corrected, fine, criminal
  for tileKey, gates in pairs(tiles) do
    for _, gate in ipairs(gates) do
      local t, speed = M.crossing(gate, before, player)
      if t and (not bestT or t < bestT or (t == bestT and gate.id < best.id)) then
        local m, c, f, offence = M.penalty(speed, gate.limit)
        if m then
          best, bestT, key, measured, corrected, fine, criminal = gate, t, tileKey, m, c, f, offence
        end
      end
    end
  end
  if best then
    notify(measured, corrected, best.limit, fine, criminal)
    local ok, err = pcall(flash, key, best)
    if not ok and log then log('W', 'beamngEarth.speedtraps', tostring(err)) end
  end
end

return M
