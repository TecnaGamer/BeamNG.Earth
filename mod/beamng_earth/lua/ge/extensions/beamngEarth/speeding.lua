-- Standard traffic follows the navgraph's curve-aware target with a small per-driver 3-5 km/h overspeed on normal
-- roads. OSM-tagged rough roads get no overspeed and stay at the legal limit (or a lower curve target). Posted limits
-- used by the game's UI and enforcement are not modified.
local M = {}

M.ON = true
M.INTERVAL_S = 0.5
M.RESEND_S = 8.0
M.MIN_CHANGE = 0.4      -- m/s
M.MIN_OVER_KMH = 3
M.MAX_OVER_KMH = 5

function M.offset(u)
  return (M.MIN_OVER_KMH + (M.MAX_OVER_KMH - M.MIN_OVER_KMH) * (u or math.random())) / 3.6
end

local cars = {}         -- id -> { over, sent, at }
local timer, clock = 0, 0

local function release(id, c)
  local obj = getObjectByID(id)
  if obj and c.sent then obj:queueLuaCommand('ai.setSpeedMode("legal")') end
  cars[id] = nil
end

function M.update(dt)
  clock = clock + (dt or 0)
  timer = timer - (dt or 0)
  if timer > 0 then return end
  timer = M.INTERVAL_S
  local gt = rawget(_G, 'gameplay_traffic')
  if not (gt and gt.getTrafficData) then return end
  local data = gt.getTrafficData() or {}
  if not M.ON then
    for id, c in pairs(cars) do release(id, c) end
    return
  end
  local player = be and be.getPlayerVehicleID and be:getPlayerVehicleID(0) or -1
  for id, c in pairs(cars) do
    if not data[id] then cars[id] = nil end
  end
  for id, v in pairs(data) do
    local role = v.role
    local free = v.isAi and id ~= player and role and role.name == 'standard' and role.state == 'none'
      and role.actionName == 'none' and v.state == 'active'
    local c = cars[id]
    if not free then
      if c then release(id, c) end
    else
      if not c then
        c = { over = M.offset(), sent = nil, at = -1e9 }
        cars[id] = c
      end
      local limit = v.tracking and tonumber(v.tracking.speedLimit)
      if limit and limit > 4.2 then
        local obj = getObjectByID(id)
        local profile
        if obj and obj.getPosition then
          local p = obj:getPosition()
          local ok, road = pcall(require('beamngEarth/navgraph').speedProfileAt, p.x, p.y, p.z)
          if ok then profile = road end
        end
        local target = profile and tonumber(profile.target) or limit
        local cap = profile and profile.rough and math.min(target, limit) or target + c.over
        if not c.sent or math.abs(cap - c.sent) >= M.MIN_CHANGE or clock - c.at >= M.RESEND_S then
          if obj then
            obj:queueLuaCommand(string.format('ai.setSpeedMode("limit") ai.setSpeed(%.2f)', cap))
            c.sent, c.at = cap, clock
          end
        end
      end
    end
  end
end

function M.reset()
  for id, c in pairs(cars) do release(id, c) end
  cars = {}
end

function M._cars() return cars end

return M
