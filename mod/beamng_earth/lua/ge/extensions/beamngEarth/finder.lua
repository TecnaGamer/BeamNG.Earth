-- The station finder in the game (GE extension beamngEarth_finder): Nearest station from the radial menu, a key
-- (beamngEarth_nearestStation) or the world map's button, and a prompt when the fuel or charge runs low (the user,
-- 2026-11-09: "nearest button is nice as well, if we can do a quick action outside map too when fuel get low would be
-- sick as well"; of the prompt showing by itself: "i like how it does above"). The stations come from the server's
-- station index (server/stationindex.py) through stations / stations_result; the route is the mod's GPS (gps.lua).
-- Nothing runs per frame: the fuel gauge is read every CHECK_S, and only in our level with the setting on.
local M = {}

M.LOW, M.REARM = 0.15, 0.25     -- the prompt below LOW (of the tanks' capacity), again only after REARM or another car
M.CHECK_S = 3                   -- s between two reads of the fuel gauge
M.NEAR_KM = 25                  -- Nearest's search radius (the server's R_KM_MAX)
M.POLL_S = 0.5                  -- s before a partial answer is asked again
M.NEAR_TIMEOUT_S = 20           -- s a Nearest search may take; then the best found so far (rural roads: many cells)
M.ACTION = 'beamngEarth_nearestStation'
M.LIQUID = { gasoline = true, diesel = true, kerosine = true }    -- the pumps' fuels (stock freeroam/gasStations.lua)
M.NAMES = { fuel = 'Gas station', ev = 'EV charger' }

-- Replaceable for the tests
M.now = function() return os.clock() end
M.core = function() return rawget(_G, 'beamngEarth_core') end
M.gps = nil                     -- (require('beamngEarth/gps') when first used)
M.say = function(text)
  if ui_message then pcall(ui_message, text, 10, 'beamngEarthFinder') end
end
M.promptOn = function()
  local s = package.loaded['beamngEarth/settings']
  local v = s and s.get and s.get('lowFuelPrompt')
  return v ~= false
end

local function gps()
  if not M.gps then M.gps = require('beamngEarth/gps') end
  return M.gps
end

-- Pure: the kind of station the car needs ('fuel' | 'ev' | nil) from its energy storages ({ energyType, currentEnergy,
-- maxEnergy }): a battery alone is ev; any liquid fuel tank is fuel (a hybrid fills up at the pumps).
function M.kindOf(tanks)
  local ev, fuel = false, false
  for _, t in ipairs(tanks or {}) do
    if M.LIQUID[t.energyType] then fuel = true elseif t.energyType == 'electricEnergy' then ev = true end
  end
  return fuel and 'fuel' or (ev and 'ev' or nil)
end

-- Pure: how full the car's storages of that kind are (0..1), nil without one.
function M.level(tanks, kind)
  local cur, max = 0, 0
  for _, t in ipairs(tanks or {}) do
    local ok = (kind == 'ev' and t.energyType == 'electricEnergy') or (kind == 'fuel' and M.LIQUID[t.energyType])
    if ok and (tonumber(t.maxEnergy) or 0) > 0 then
      cur, max = cur + (tonumber(t.currentEnergy) or 0), max + t.maxEnergy
    end
  end
  return max > 0 and cur / max or nil
end

-- Pure: the prompt's state { said, vehId } after a reading; true when the prompt is due (once below LOW, again after
-- the level was above REARM or for another car).
function M.lowFuelStep(state, level, vehId)
  state = state or {}
  if vehId ~= state.vehId then state = { vehId = vehId } end
  if level == nil then return state, false end
  if level >= M.REARM then state.said = false return state, false end
  if level < M.LOW and not state.said then state.said = true return state, true end
  return state, false
end

-- The player's car: cb(kind, level, vehId), or cb(nil) without one (core_vehicleBridge's energyStorage, as stock
-- freeroam/gasStations.lua reads it).
M.carKind = function(cb)
  local veh = rawget(_G, 'getPlayerVehicle') and getPlayerVehicle(0)
  local bridge = rawget(_G, 'core_vehicleBridge')
  if not veh or not bridge then return cb(nil) end
  local id = veh.getID and veh:getID() or nil
  bridge.requestValue(veh, function(ret)
    local tanks = ret and ret[1] or {}
    local kind = M.kindOf(tanks)
    cb(kind, kind and M.level(tanks, kind), id)
  end, 'energyStorage')
end

-- --- asking the server ------------------------------------------------------------------------------------------------
local nextId = 0
local pending = {}              -- id -> a search waiting for its answer (Nearest, the prompt)
local results = {}              -- id -> an answer nobody waits for here (the world map's: settings.uiStationsResult)
local waiting = {}              -- searches that got a partial answer, to ask again

function M.newId()
  nextId = nextId + 1
  return nextId
end

-- Send one stations message; the id, or nil and why.
function M.send(lat, lon, rKm, kinds, mode)
  local core = M.core()
  if not core or not core.requestStations then return nil, 'BeamNG.Earth is not loaded' end
  local id = M.newId()
  if not core.requestStations(id, lat, lon, rKm, kinds, mode) then return nil, 'Companion server not connected' end
  return id
end

function M.take(id)
  local r = results[id]
  results[id] = nil
  return r
end

local function named(item)
  return (item.name ~= nil and item.name ~= '') and item.name or M.NAMES[item.kind] or 'Station'
end

local function finish(req, items)
  local best = items and items[1]
  if req.purpose == 'nearest' then
    if not best then M.say('No station within ' .. M.NEAR_KM .. ' km') return end
    if gps().setDestination(best.lat, best.lon, named(best)) then
      M.stationDest = { lat = best.lat, lon = best.lon }
      M.say(string.format('Route to %s, %.1f km', named(best), best.km or 0))
    end
  elseif req.purpose == 'prompt' and best then
    local b = rawget(_G, 'core_input_bindings')
    local key = b and b.getControlForAction and b.getControlForAction(M.ACTION)
    local how = key and ('Press ' .. tostring(key):upper() .. ' to route') or "Pick Nearest station in the radial menu to route"
    M.say(string.format('%s low: nearest %s %.1f km. %s', req.kind == 'ev' and 'Charge' or 'Fuel',
      req.kind == 'ev' and 'charger' or 'station', best.km or 0, how))
  end
end

local function search(purpose, lat, lon, kind)
  local id, err = M.send(lat, lon, M.NEAR_KM, { kind }, 'near')
  if not id then
    if purpose == 'nearest' then M.say(err) end
    return
  end
  pending[id] = { purpose = purpose, lat = lat, lon = lon, kind = kind, deadline = M.now() + M.NEAR_TIMEOUT_S }
end

function M.onStations(msg)
  local req = msg and pending[msg.id]
  if not req then
    if msg and msg.id then results[msg.id] = { items = msg.items or {}, partial = msg.partial, error = msg.error } end
    return
  end
  pending[msg.id] = nil
  local items = msg.items or {}
  if #items > 0 then req.best = items end
  if msg.partial and M.now() < req.deadline then
    req.again = M.now() + M.POLL_S
    waiting[#waiting + 1] = req
    return
  end
  finish(req, req.best)
end

local function player()
  local core = M.core()
  local st = core and core._state and core._state()
  return st, st and st.player
end

-- Nearest station for the car being driven (its kind: a charger for an electric car), the GPS set to it.
function M.nearest()
  local _, pl = player()
  if not pl or not pl.lat then M.say('No position yet') return end
  M.carKind(function(kind) search('nearest', pl.lat, pl.lon, kind or 'fuel') end)
end

local function routedToStation()
  local d, s = M.stationDest, gps().status and gps().status()
  return d and s and math.abs((s.lat or 0) - d.lat) < 1e-6 and math.abs((s.lon or 0) - d.lon) < 1e-6
end

M.lowState = {}
local checkAt = -math.huge
function M.onUpdate()
  local now = M.now()
  if #waiting > 0 then
    local keep = {}
    for _, req in ipairs(waiting) do
      if now >= req.deadline then finish(req, req.best)
      elseif now >= req.again then
        local id = M.send(req.lat, req.lon, M.NEAR_KM, { req.kind }, 'near')
        if id then pending[id] = req else finish(req, req.best) end
      else keep[#keep + 1] = req end
    end
    waiting = keep
  end
  if now < checkAt then return end
  local st, pl = player()
  if not (st and st.inOurLevel and pl and pl.lat) or not M.promptOn() then return end
  checkAt = now + M.CHECK_S
  M.carKind(function(kind, level, vehId)
    local due
    M.lowState, due = M.lowFuelStep(M.lowState, level, vehId)
    if due and kind and not routedToStation() then search('prompt', pl.lat, pl.lon, kind) end
  end)
end

-- The radial menu's entry (core_quickAccess), our level only: an action in the stock "Other" ring, beside Save / Load
-- (quickAccess.lua's '/root/sandbox/other/'). Not the top level: its items are categories with a goto, and an action
-- there broke the menu's navigation (in game, 2026-11-09: "the circle doesn't change but the click does").
M.RADIAL_LEVEL = '/root/sandbox/other/'
function M.onQuickAccessLoaded()
  local qa = rawget(_G, 'core_quickAccess')
  if not (qa and qa.addEntry) then return end
  qa.addEntry({ level = M.RADIAL_LEVEL, uniqueID = 'beamngEarthFinder', generator = function(entries)
    local st = player()
    if not (st and st.inOurLevel) then return end
    table.insert(entries, { title = 'Nearest station', icon = 'fuelPump', uniqueID = 'beamngEarthNearestStation',
      desc = 'Route the GPS to the nearest gas station (an EV charger for an electric car)',
      onSelect = function() M.nearest() return { 'hide' } end })
  end })
end
M.onExtensionLoaded = function() M.onQuickAccessLoaded() end

return M
