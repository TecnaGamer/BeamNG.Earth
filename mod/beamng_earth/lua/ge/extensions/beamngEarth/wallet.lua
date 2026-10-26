-- The player's money (the user, 2026-10-25: "a usable money system, so we can later add jobs ... buy cars at real
-- dealers, pay a fee to get in the Nurburgring and win cash"). One balance, starting at $0 (the user's choice), saved
-- in FILE between sessions; it may go below zero (shown red) until there are ways to earn. Every change goes through
-- M.add(amount, reason) (negative: a cost) and shows a short message; the last HISTORY changes are kept.
-- First costs: fuel at our gas stations (gasstations.lua wraps the stock refuel: what went in the tank, at PRICE) and
-- tolls (passing under a toll gantry of the road tiles, server guidesigns.py: TOLL each).
-- Pure apart from the game's file, message and vehicle calls (runs under plain luajit with stubs).
local M = {}

M.FILE = '/settings/beamngEarth_wallet.json'
M.HISTORY = 20
M.TOLL = 3.25               -- $ per toll gantry passed (open-road tolling, a car)
M.PRICE = { gal = 3.40, l = 1.80 }      -- fuel per US gallon (mph countries) / per litre (the others)
M.LITRE_J = { gasoline = 31125000, diesel = 36000000 }   -- J per litre (BeamNG's energy densities)
M.GAL_L = 3.785411784
M.TOLL_SIDE_M = 1.5         -- a crossing counts this far beyond the gantry's span

local state = nil           -- { balance, history = { { amount, reason, t } ... } }
local tolls = {}            -- tile key -> { anchor, list = { { x, y, dx, dy, half } ... } }
local lastPos = nil         -- { anchor, x, y } of the player at the last update

local function load()
  if state then return state end
  local read = rawget(_G, 'jsonReadFile')
  local ok, got = false, nil
  if read then ok, got = pcall(read, M.FILE) end
  if ok and type(got) == 'table' and tonumber(got.balance) then
    state = { balance = tonumber(got.balance), history = type(got.history) == 'table' and got.history or {} }
  else
    state = { balance = 0, history = {} }
  end
  return state
end

local function save()
  local write = rawget(_G, 'jsonWriteFile')
  if write then pcall(write, M.FILE, state, true) end
end

-- Pure: a sum of money as shown: $1,234.50 / -$3.25.
function M.format(v)
  v = tonumber(v) or 0
  local neg = v < 0
  local cents = math.floor(math.abs(v) * 100 + 0.5)
  local whole, frac = math.floor(cents / 100), cents % 100
  local s = tostring(whole):reverse():gsub('(%d%d%d)', '%1,'):reverse():gsub('^,', '')
  return (neg and '-$' or '$') .. s .. string.format('.%02d', frac)
end

function M.balance() return load().balance end
function M.history() return load().history end

-- Changes the balance by amount (negative: a cost) for reason; shows it; saved at once. Returns the new balance.
function M.add(amount, reason)
  amount = tonumber(amount) or 0
  if amount == 0 then return load().balance end
  local s = load()
  s.balance = math.floor((s.balance + amount) * 100 + 0.5) / 100
  table.insert(s.history, 1, { amount = amount, reason = tostring(reason or ''), t = os.time and os.time() or 0 })
  while #s.history > M.HISTORY do table.remove(s.history) end
  save()
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then
    gh.trigger('Message', { msg = (amount > 0 and '+' or '') .. M.format(amount) .. '  ' .. tostring(reason or ''),
      category = 'bneWallet', icon = amount > 0 and 'check' or 'info', ttl = 5 })
  end
  return s.balance
end

function M.reset()
  state = { balance = 0, history = {} }
  save()
end

-- Fuel ------------------------------------------------------------------------------------------------------------
-- Pure: the cost of filling tanks (the stock 'energyStorage' values: energyType, currentEnergy, maxEnergy) of the
-- given fuel types: (cost, amount in the unit, unit 'gal' / 'l').
function M.fuelCost(tanks, fuelTypes, mph)
  local litres = 0
  for _, t in ipairs(tanks or {}) do
    local dens = M.LITRE_J[t.energyType]
    if dens and (fuelTypes == nil or fuelTypes.any or fuelTypes[t.energyType]) then
      local missing = (tonumber(t.maxEnergy) or 0) - (tonumber(t.currentEnergy) or tonumber(t.remainingEnergy) or 0)
      if missing > 0 then litres = litres + missing / dens end
    end
  end
  if mph then
    local gal = litres / M.GAL_L
    return gal * M.PRICE.gal, gal, 'gal'
  end
  return litres * M.PRICE.l, litres, 'l'
end

-- Our stations' refuel: the stock freeroam_gasStations.refuelCar wrapped once; for a 'bne_fuel_' station it asks the
-- car's tanks, charges what goes in, then lets the stock code fill them.
local wrapped = false
function M.wrapRefuel(gs, mph)
  if wrapped or type(gs) ~= 'table' or type(gs.refuelCar) ~= 'function' then return false end
  local stock = gs.refuelCar
  gs.refuelCar = function(station, fuelTypes, veh)
    local id = type(station) == 'table' and tostring(station.id or (station.facility and station.facility.id) or '') or ''
    local bridge = rawget(_G, 'core_vehicleBridge')
    if id:find('bne_fuel_', 1, true) == 1 and bridge and veh then
      bridge.requestValue(veh, function(ret)
        local cost, amount, unit = M.fuelCost(ret and ret[1], fuelTypes, type(mph) == 'function' and mph() or mph)
        if cost >= 0.01 then
          M.add(-cost, string.format('Fuel (%.1f %s)', amount, unit))
        end
        stock(station, fuelTypes, veh)
      end, 'energyStorage')
      return
    end
    return stock(station, fuelTypes, veh)
  end
  wrapped = true
  return true
end

-- Tolls -------------------------------------------------------------------------------------------------------------
-- A road tile's toll gantries (server write_tile 'tolls': { x, y, dx, dy, half } in its anchor's frame).
function M.onTolls(anchor, key, list)
  if type(list) == 'table' and #list > 0 then tolls[key] = { anchor = anchor, list = list }
  else tolls[key] = nil end
end

-- Pure: did a move from (ax, ay) to (bx, by) pass under gantry g (x, y, travel dx, dy, half span) in its direction?
function M.crossed(g, ax, ay, bx, by)
  local x, y, dx, dy, half = g[1], g[2], g[3], g[4], g[5]
  local sa = (ax - x) * dx + (ay - y) * dy
  local sb = (bx - x) * dx + (by - y) * dy
  if not (sa < 0 and sb >= 0) then return false end
  local t = sa / (sa - sb)
  local cx, cy = ax + (bx - ax) * t, ay + (by - ay) * t
  local lat = math.abs((cx - x) * -dy + (cy - y) * dx)
  return lat <= half + M.TOLL_SIDE_M
end

-- Every frame (core): the player's position in anchor's frame; a toll for each gantry passed since the last call.
function M.update(anchor, px, py)
  if not anchor or not px then lastPos = nil return end
  local lp = lastPos
  lastPos = { anchor = anchor, x = px, y = py }
  if not lp or lp.anchor ~= anchor then return end
  local d2 = (px - lp.x) ^ 2 + (py - lp.y) ^ 2
  if d2 < 1e-4 or d2 > 200 ^ 2 then return end        -- standing still, or a teleport
  for _, t in pairs(tolls) do
    if t.anchor == anchor then
      for _, g in ipairs(t.list) do
        if M.crossed(g, lp.x, lp.y, px, py) then M.add(-M.TOLL, 'Toll') end
      end
    end
  end
end

function M.resetTolls() tolls, lastPos = {}, nil end
function M._setState(s) state = s end         -- (tests)

return M
