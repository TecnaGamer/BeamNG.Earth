-- Traffic's headlights in bad weather (the weather plan's 4.11; beamngEarth_weather drives it). The stock traffic turns
-- a car's lights on at night or in a tunnel (gameplay/traffic/vehicle.lua onTrafficTick: "if (isTunnel or not
-- isDaytime) and not self.headlights", isDaytime from its checkTimeOfDay). While the weather wants lights (rain, snow,
-- fog under 1 km, a storm's dusk: weather.trafficLights / duskDark) the traffic vehicle class's checkTimeOfDay answers
-- "night" inside onTrafficTick only, so the stock code switches the lights on itself (with its own random delay per car)
-- and off again when the wrap goes; its other use (fewer cars spawned at night, onRefresh) is left alone. The class is
-- the metatable every traffic vehicle shares; the wrap is installed only while lights are wanted and taken out after
-- (nothing per frame otherwise: a check every CHECK_S).
local M = {}

M.CHECK_S = 1.0

local orig = nil         -- { C, check, tick, ourCheck, ourTick } while installed
local inTick = false
local timer = 0
M.installed = false

-- The traffic vehicle class (the shared metatable of gameplay_traffic's vehicles), nil without traffic.
local function trafficClass()
  local gt = rawget(_G, 'gameplay_traffic')
  if not (gt and gt.getTrafficData) then return nil end
  local ok, data = pcall(gt.getTrafficData)
  if not ok or type(data) ~= 'table' then return nil end
  for _, v in pairs(data) do
    local mt = type(v) == 'table' and getmetatable(v) or nil
    if type(mt) == 'table' and type(mt.checkTimeOfDay) == 'function' and type(mt.onTrafficTick) == 'function' then
      return mt
    end
  end
  return nil
end
M._trafficClass = trafficClass   -- (tests)

local function install(C)
  local o = { C = C, check = C.checkTimeOfDay, tick = C.onTrafficTick }
  o.ourTick = function(self, ...)
    inTick = true
    local res = { pcall(o.tick, self, ...) }
    inTick = false
    if not res[1] then error(res[2], 0) end
    return unpack(res, 2)
  end
  o.ourCheck = function(self, ...)
    local day = o.check(self, ...)
    if day and inTick then return false end
    return day
  end
  C.onTrafficTick, C.checkTimeOfDay = o.ourTick, o.ourCheck
  orig = o
  M.installed = true
end

-- The wrap out again (only what is still ours: another mod may have wrapped the class since).
function M.uninstall()
  if not orig then return end
  local C = orig.C
  if C.onTrafficTick == orig.ourTick then C.onTrafficTick = orig.tick end
  if C.checkTimeOfDay == orig.ourCheck then C.checkTimeOfDay = orig.check end
  orig, inTick = nil, false
  M.installed = false
end

-- Every frame from weather.lua (cheap: a timer; the class looked for every CHECK_S while lights are wanted and the
-- wrap is not in). want: the weather wants headlights now.
function M.update(dt, want)
  timer = timer + (dt or 0)
  if timer < M.CHECK_S then return end
  timer = 0
  if want then
    if orig and trafficClass() ~= orig.C and trafficClass() ~= nil then M.uninstall() end   -- (traffic reloaded)
    if not orig then
      local C = trafficClass()
      if C then install(C) end
    end
  elseif orig then
    M.uninstall()
  end
end

function M.reset()
  M.uninstall()
  timer = 0
end

return M
