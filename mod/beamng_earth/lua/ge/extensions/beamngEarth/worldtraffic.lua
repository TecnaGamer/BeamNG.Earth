-- World traffic (the user, 2026-10-26: "Spawn 15 traffic cars 2 of which are police ... a button somewhere to start
-- the world around you ... phone settings > Turn on world traffic > iphone style slide on off"). The phone's switch:
-- on = the stock traffic system with CARS vehicles, POLICE of them police (the stock police provider asks a quarter:
-- re-registered here asking POLICE); off = every traffic vehicle removed. The choice is kept in the phone's settings and
-- switched on again START_S after the level loads.
local M = {}

M.CARS, M.POLICE = 15, 2
M.START_S = 12

local on, startT = false, nil

local function police()
  local tr, utils = rawget(_G, 'gameplay_traffic'), rawget(_G, 'gameplay_traffic_trafficUtils')
  if not (tr and tr.registerSpecialVehicleProvider) then return end
  pcall(tr.registerSpecialVehicleProvider, {
    name = 'police', priority = 10, minTotalAmount = 3,
    getDesiredCount = function(total, options) return options.police and M.POLICE or 0 end,
    buildGroup = function(count)
      local group = utils and utils.createPoliceGroup and utils.createPoliceGroup(count) or {}
      if not group[1] then for _ = 1, count do group[#group + 1] = { model = 'fullsize', config = 'police' } end end
      return group
    end,
  })
end

function M.set(want)
  local tr = rawget(_G, 'gameplay_traffic')
  if not tr then return false end
  if want then
    police()
    local ok = pcall(tr.setupTraffic, M.CARS, { police = true })
    on = ok
    return ok
  end
  pcall(tr.deleteVehicles)
  on = false
  return true
end

function M.isOn() return on end

-- core, every frame in our level: the saved choice applied once, START_S after the level is up.
function M.update(dt, wanted)
  if not wanted or on then return end
  startT = (startT or 0) + (dt or 0)
  if startT >= M.START_S then
    startT = -math.huge                 -- (once a session)
    M.set(true)
  end
end

function M._reset() on, startT = false, nil end
return M
