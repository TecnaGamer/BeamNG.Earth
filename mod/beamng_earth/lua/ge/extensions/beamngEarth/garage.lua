-- Repair shops and property (the user, 2026-10-26: "repair shops/garages to buy new parts/fix your car"; "purchasable &
-- rentable property for storing and working on vehicles"). The phone's Garage app lists real repair shops and property
-- near you (server shops.py); Navigate sets the GPS. Stopped within AT_M of a shop's spot (or of your own property):
--   Repair: the car re-made where it stands, same parts and paint (REPAIR_BASE + the damage x REPAIR_PER; free at home)
--   Respray (RESPRAY) / new plates (PLATES): a new look for the police (heat.lua remembers plates and looks)
--   Parts: the game's parts menu, for LABOR at a shop (free at home)
-- Property: buy (once) or rent (a game week, RENT_DAYS days of dynweather's calendar; renewed by paying again) a real
-- storage or parking place; there: your cars stored (swap the one you drive for one in storage, or add it), repairs
-- and parts free, and the place added to your saved places (the level select's spawn points).
-- Saved in FILE. The prices are pure (mod/tests/test_garage.lua); the vehicle calls are the game's, in pcalls.
local M = {}

M.FILE = '/settings/beamngEarth_property.json'
M.AT_M = 45
M.REPAIR_BASE, M.REPAIR_PER = 60, 0.6        -- $ + per $ of damage (beamstate's damage is about the repair's $)
M.RESPRAY, M.PLATES, M.LABOR = 450, 120, 150
M.RENT_DAYS = 7
M.SLOTS = 6
M.COLOURS = { { 0.05, 0.05, 0.05 }, { 0.9, 0.9, 0.9 }, { 0.6, 0.05, 0.05 }, { 0.05, 0.15, 0.5 }, { 0.1, 0.35, 0.12 },
              { 0.55, 0.55, 0.58 }, { 0.85, 0.55, 0.05 }, { 0.3, 0.05, 0.4 } }

local wallet = require('beamngEarth/wallet')
local state = nil           -- { owned = { [id] = { id, name, lat, lon, kind = 'own' | 'rent', untilDay, cars = {...} } } }
local lists = { repair = {}, property = {} }
local listErr, pending = {}, {}
local reqId = 0
local shop = nil            -- the open parts shop: { parts, t } (M.parts)
local last = nil            -- the last action's message

local function load()
  if state then return state end
  local read = rawget(_G, 'jsonReadFile')
  local ok, got = false, nil
  if read then ok, got = pcall(read, M.FILE) end
  state = (ok and type(got) == 'table' and type(got.owned) == 'table') and got or { owned = {} }
  return state
end
local function save()
  local write = rawget(_G, 'jsonWriteFile')
  if write and state then pcall(write, M.FILE, state, true) end
end
local function core() return rawget(_G, 'beamngEarth_core') end
local function today()
  local ok, d = pcall(function() return require('beamngEarth/dynweather')._state().day end)
  return ok and tonumber(d) or 0
end
local function say(text)
  last = text
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then gh.trigger('Message', { msg = text, category = 'bneGarage', icon = 'info', ttl = 6 }) end
end

-- Pure: the repair's price for a damage value, free at home.
function M.repairCost(damage, home)
  if home then return 0 end
  return math.floor((M.REPAIR_BASE + M.REPAIR_PER * math.max(0, tonumber(damage) or 0)) / 5 + 0.5) * 5
end

-- Pure: is a rented place still yours on day d?
function M.held(p, d)
  return p and (p.kind == 'own' or (tonumber(p.untilDay) or -1) >= d) or false
end

-- Lists ---------------------------------------------------------------------------------------------------------
function M.refresh(kind, lat, lon)
  local c = core()
  kind = kind == 'property' and 'property' or 'repair'
  if not (c and c.requestPlacesNear and lat) then listErr[kind] = 'not connected' return false end
  reqId = reqId + 1
  pending[kind] = reqId
  listErr[kind] = nil
  return c.requestPlacesNear(reqId, lat, lon, kind)
end

local function poll()
  local c = core()
  for kind, id in pairs(pending) do
    local r = c and c.takePlacesNear and c.takePlacesNear(id)
    if r then
      pending[kind] = nil
      lists[kind] = type(r.places) == 'table' and r.places or {}
      listErr[kind] = (#lists[kind] == 0) and (r.error or 'none near here') or nil
    end
  end
end

-- Where you are ---------------------------------------------------------------------------------------------------
local function playerVeh()
  local be_ = rawget(_G, 'be')
  return be_ and be_:getPlayerVehicle(0) or nil
end

local function within(anchor, player, p)
  if not (anchor and player and p) then return false end
  local x, y = anchor:geodeticToEnu(p.lat, p.lon, 0)
  return (x - player.x) ^ 2 + (y - player.y) ^ 2 <= M.AT_M * M.AT_M
end

-- { kind = 'home' | 'shop', place } of the place you stand at, or nil.
function M.at(anchor, player)
  local s, d = load(), today()
  for _, p in pairs(s.owned) do
    if M.held(p, d) and within(anchor, player, p) then return { kind = 'home', place = p } end
  end
  for _, p in ipairs(lists.repair) do
    if within(anchor, player, p) then return { kind = 'shop', place = p } end
  end
  return nil
end

-- The car ---------------------------------------------------------------------------------------------------------
local function colourOf(veh)
  local ok, c = pcall(function() return veh.color end)
  local out = {}
  for v in tostring(ok and c or ''):gmatch('[-%d%.]+') do out[#out + 1] = tonumber(v) end
  if #out < 3 then return { 0.5, 0.5, 0.5, 1.2 } end
  return { out[1], out[2], out[3], out[4] or 1.2 }
end

local function carOf(veh)
  if not veh then return nil end
  local okM, model = pcall(function() return veh.JBeam end)
  local okC, config = pcall(function() return veh.partConfig end)
  local okP, plate = pcall(function() return veh:getDynDataFieldbyName('licenseText', 0) end)
  return { model = okM and model or nil, config = okC and config or nil, colour = colourOf(veh),
           plate = okP and plate or nil }
end

local function damageOf(veh)
  local mp = rawget(_G, 'map')
  local o = veh and mp and mp.objects and mp.objects[veh:getID()]
  return o and tonumber(o.damage) or 0
end

-- The car re-made where it stands (a repaired copy): its model, parts, the colour (or a new one), its plate.
local function remake(car, colour)
  local cv = rawget(_G, 'core_vehicles')
  if not (cv and cv.replaceVehicle and car and car.model) then return false end
  local c = colour or car.colour
  local ok = pcall(cv.replaceVehicle, car.model, { config = car.config,
    paint = { baseColor = { c[1], c[2], c[3], c[4] or 1.2 }, metallic = 0.3, roughness = 0.4, clearcoat = 0.8,
              clearcoatRoughness = 0.1 } })
  if ok and car.plate and cv.setPlateText then
    local v = playerVeh()
    if v then pcall(cv.setPlateText, car.plate, v:getID()) end
  end
  return ok
end

local function pay(amount, what)
  if amount > 0 then wallet.add(-amount, what) end
end

-- Actions (the phone's buttons) -----------------------------------------------------------------------------------
function M.repair(anchor, player)
  local here = M.at(anchor, player)
  if not here then return false, 'drive to a repair shop or your property' end
  local veh = playerVeh()
  local cost = M.repairCost(damageOf(veh), here.kind == 'home')
  if not remake(carOf(veh)) then return false, 'the repair failed' end
  pay(cost, 'Repair at ' .. tostring(here.place.name))
  say(cost > 0 and string.format('Repaired: $%d', cost) or 'Repaired')
  return true
end

function M.respray(anchor, player, i)
  local here = M.at(anchor, player)
  if not here then return false, 'drive to a repair shop or your property' end
  local c = M.COLOURS[tonumber(i) or 1] or M.COLOURS[1]
  if not remake(carOf(playerVeh()), { c[1], c[2], c[3], 1.2 }) then return false, 'the respray failed' end
  pay(M.RESPRAY, 'Respray')
  say('New paint')
  return true
end

-- Pure: a new plate text (3 letters, 4 digits; the PA plate puts its keystone between them) from a number.
function M.plateText(n)
  local L = 'ABCDEFGHJKLMNPRSTUVWXYZ'
  n = math.floor(math.abs(n or 0))
  local s = ''
  for _ = 1, 3 do local k = n % #L; s = s .. L:sub(k + 1, k + 1); n = math.floor(n / #L) end
  return s .. string.format('%04d', n % 10000)
end

function M.plates(anchor, player)
  local here = M.at(anchor, player)
  if not here then return false, 'drive to a repair shop or your property' end
  local cv, veh = rawget(_G, 'core_vehicles'), playerVeh()
  local text = M.plateText((os.time and os.time() or 0) * 7919 + math.floor(os.clock() * 1000))
  if not (cv and cv.setPlateText and veh) or not pcall(cv.setPlateText, text, veh:getID()) then
    return false, 'the plates could not be changed'
  end
  pay(M.PLATES, 'New plates')
  say('New plates: ' .. text)
  return true
end

function M.parts(anchor, player)
  local here = M.at(anchor, player)
  if not here then return false, 'parts are fitted at a repair shop or your property' end
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then gh.trigger('ChangeState', { state = 'menu.vehicleconfig.parts' }) end
  if here.kind ~= 'home' then pay(M.LABOR, 'Labor at ' .. tostring(here.place.name)) end
  shop = { parts = M.partsNow(), t = 0 }          -- (the parts shop: each change paid for, update())
  return true
end

-- The parts shop (the user, 2026-10-26: "shops offer new parts, which is just part customization that takes from the
-- money we have"): while it is open, every part fitted is paid at its jbeam value (information.value, the game's own
-- price), every part taken off sold back at SELL_PARTS of it. Closed after SHOP_S without a change, or by leaving.
M.SELL_PARTS, M.SHOP_S = 0.5, 600

-- The player car's parts: { [slot] = part name } (core_vehicle_manager's config), or nil.
function M.partsNow()
  local vm = rawget(_G, 'core_vehicle_manager')
  local ok, d = pcall(function() return vm and vm.getPlayerVehicleData and vm.getPlayerVehicleData() end)
  local cfg = ok and type(d) == 'table' and d.config
  if type(cfg) ~= 'table' or type(cfg.parts) ~= 'table' then return nil end
  local out = {}
  for slot, part in pairs(cfg.parts) do if part and part ~= '' then out[slot] = part end end
  return out
end

-- A part's price (its jbeam information.value), 0 when unknown.
function M.partValue(name)
  local vm = rawget(_G, 'core_vehicle_manager')
  local ok, v = pcall(function()
    local d = vm.getPlayerVehicleData()
    local io = require('jbeam/io')
    local part = io.getPart(d.ioCtx, name)
    return part and part.information and tonumber(part.information.value)
  end)
  return ok and v or 0
end

-- Pure: (cost, refund) of going from parts a to parts b with prices value(name).
function M.partsBill(a, b, value)
  local cost, refund = 0, 0
  local had, has = {}, {}
  for _, n in pairs(a or {}) do had[n] = (had[n] or 0) + 1 end
  for _, n in pairs(b or {}) do has[n] = (has[n] or 0) + 1 end
  for n, c in pairs(has) do if c > (had[n] or 0) then cost = cost + (c - (had[n] or 0)) * value(n) end end
  for n, c in pairs(had) do if c > (has[n] or 0) then refund = refund + (c - (has[n] or 0)) * value(n) * M.SELL_PARTS end end
  return math.floor(cost + 0.5), math.floor(refund + 0.5)
end

-- Property --------------------------------------------------------------------------------------------------------
function M.acquire(i, how)
  local p = lists.property[tonumber(i) or 0]
  if not p then return false, 'no such place' end
  local s = load()
  local have = s.owned[p.id]
  local price = how == 'buy' and p.buy or p.rent
  if how == 'buy' and have and have.kind == 'own' then return false, 'already yours' end
  pay(price, (how == 'buy' and 'Bought ' or 'Rent: ') .. tostring(p.name))
  local d = today()
  have = have or { id = p.id, name = p.name, lat = p.lat, lon = p.lon, mlat = p.mlat, mlon = p.mlon, cars = {} }
  have.buy = p.buy or have.buy                  -- (its price: a rental can be bought later, M.buyout)
  if how == 'buy' then
    have.kind = 'own'
    have.paid = price
  else
    have.kind = have.kind == 'own' and 'own' or 'rent'
    have.untilDay = math.max(tonumber(have.untilDay) or -1, d - 1) + M.RENT_DAYS
  end
  s.owned[p.id] = have
  save()
  local okP, places = pcall(require, 'beamngEarth/places')         -- a spawn point in the level select
  if okP and places.add then pcall(places.add, 'My garage: ' .. tostring(p.name), p.lat, p.lon) end
  say(how == 'buy' and ('You own ' .. tostring(p.name)) or ('Rented ' .. tostring(p.name) .. ' for a week'))
  return true
end

-- Pure: what an owned place sells for (SELL_BACK of the price paid).
M.SELL_BACK = 0.7
function M.salePrice(p) return math.floor((tonumber(p and p.paid) or 0) * M.SELL_BACK / 5 + 0.5) * 5 end

-- Sell an owned place (SELL_BACK of its price; its stored cars go with it) or end a lease (no refund).
function M.sell(id)
  local s = load()
  local p = s.owned[id]
  if not p then return false, 'not yours' end
  if p.kind == 'own' then
    local back = M.salePrice(p)
    if back > 0 then wallet.add(back, 'Sold ' .. tostring(p.name)) end
    say('Sold ' .. tostring(p.name))
  else
    say('Lease ended: ' .. tostring(p.name))
  end
  s.owned[id] = nil
  save()
  return true
end

-- Buy a place you rent (the user: "there's no option to sell my property": his was rented): its price (kept when
-- rented, else from the latest Property search), then it is yours to keep or sell.
function M.buyout(id)
  local s = load()
  local p = s.owned[id]
  if not p or p.kind == 'own' then return false, 'not a rental' end
  local price = tonumber(p.buy)
  if not price then
    for _, q in ipairs(lists.property) do if q.id == id then price = tonumber(q.buy); p.mlat = p.mlat or q.mlat; p.mlon = p.mlon or q.mlon end end
  end
  if not price then return false, 'search Property near it first to see its price' end
  pay(price, 'Bought ' .. tostring(p.name))
  p.kind, p.paid, p.buy, p.untilDay = 'own', price, price, nil
  save()
  say('You own ' .. tostring(p.name))
  return true
end

-- At your property: store the car you drive (slot empty) or swap it for stored car k.
function M.store(anchor, player, k)
  local here = M.at(anchor, player)
  if not (here and here.kind == 'home') then return false, 'drive to your property first' end
  local p = here.place
  p.cars = p.cars or {}
  local car = carOf(playerVeh())
  if not (car and car.model) then return false, 'no car' end
  k = tonumber(k)
  if k and p.cars[k] then
    local out = p.cars[k]
    p.cars[k] = car
    if not remake(out) then p.cars[k] = out return false, 'could not bring it out' end
    say('Swapped: your ' .. tostring(car.model) .. ' is in storage')
  else
    if #p.cars >= M.SLOTS then return false, 'storage full' end
    p.cars[#p.cars + 1] = car
    say('Stored a copy of your ' .. tostring(car.model))
  end
  save()
  return true
end

-- The garage spots (the user: "a blue circle or some form of option menu ... they use a desk and a laptop ... make sure
-- it's out of the way or beside a house"): at each repair shop near you and each place of yours, the spot off the road
-- (server shops.marker): a blue ring, the stock workbench with a laptop on it; stopping in the ring opens the phone on
-- that place's menu (Garage at a shop, Real Estate at yours).
M.RING_M, M.PROPS_M, M.DRAW_M = 4.0, 150, 220
M.BENCH = '/art/shapes/garage_and_dealership/garage/s_garage_workbench.dae'
M.LAPTOP = '/art/shapes/garage_and_dealership/garage/s_laptop_ON.dae'
M.BENCH_TOP = 0.9
local props, inRing = {}, nil

local function spots()
  local out = {}
  for _, p in ipairs(lists.repair) do
    if p.mlat then out[#out + 1] = { id = 'shop:' .. p.id, lat = p.mlat, lon = p.mlon, app = 'garage' } end
  end
  local s, d = load(), today()
  for _, p in pairs(s.owned) do
    if M.held(p, d) and p.mlat then out[#out + 1] = { id = 'home:' .. p.id, lat = p.mlat, lon = p.mlon, app = 'realestate' } end
  end
  return out
end

local function groundZ(x, y, z)
  local be_ = rawget(_G, 'be')
  local ok, h = pcall(function() return be_:getSurfaceHeightBelow(vec3(x, y, z + 20)) end)
  return (ok and tonumber(h) and h > -1e6) and h or z
end

local function placeProps(id, x, y, z)
  local ok = pcall(function()
    local objs = {}
    for i, shape in ipairs({ M.BENCH, M.LAPTOP }) do
      local o = createObject('TSStatic')
      o.shapeName = shape
      o:setPosRot(x, y, z + (i == 2 and M.BENCH_TOP or 0), 0, 0, 0, 1)
      o:registerObject('bne_garage_' .. id:gsub('[^%w]', '_') .. '_' .. i)
      objs[#objs + 1] = o
    end
    props[id] = objs
  end)
  if not ok then props[id] = false end          -- (no stock props: the ring alone)
end

local function removeProps(id)
  for _, o in ipairs(props[id] or {}) do pcall(function() o:delete() end) end
  props[id] = nil
end

function M.update(dt, anchor, player)
  poll()
  if shop then                                   -- the parts shop: bill each change
    shop.t = shop.t + (dt or 0)
    local now = M.partsNow()
    if now and shop.parts then
      local cost, refund = M.partsBill(shop.parts, now, M.partValue)
      if cost > 0 or refund > 0 then
        if cost > 0 then wallet.add(-cost, 'Parts') end
        if refund > 0 then wallet.add(refund, 'Parts sold back') end
        shop.parts, shop.t = now, 0
      end
    end
    if shop.t > M.SHOP_S then shop = nil end
  end
  if not (anchor and player) then return end
  local dd = rawget(_G, 'debugDrawer')
  local seen = {}
  for _, s in ipairs(spots()) do
    local x, y = anchor:geodeticToEnu(s.lat, s.lon, 0)
    local dist = math.sqrt((x - player.x) ^ 2 + (y - player.y) ^ 2)
    if dist < M.PROPS_M then
      seen[s.id] = true
      local z = groundZ(x, y, player.z or 0)
      if props[s.id] == nil then placeProps(s.id, x + 2.5, y, z) end
      if dd and dist < M.DRAW_M then
        pcall(function()
          dd:drawCylinder(vec3(x, y, z), vec3(x, y, z + 0.08), M.RING_M, ColorF(0.1, 0.45, 1.0, 0.45))
        end)
      end
      local speed = math.sqrt((player.vx or 0) ^ 2 + (player.vy or 0) ^ 2)
      if dist <= M.RING_M and speed < 1.5 then
        if inRing ~= s.id then
          inRing = s.id
          local gh = rawget(_G, 'guihooks')
          if gh and gh.trigger then gh.trigger('BneEarthPhone', { up = true, app = s.app }) end
        end
      elseif inRing == s.id and dist > M.RING_M + 3 then
        inRing = nil
      end
    end
  end
  for id in pairs(props) do if not seen[id] then removeProps(id) end end
end

-- For the phone (settings.uiGarage).
function M.uiState(anchor, player)
  local s, d = load(), today()
  local mine = {}
  for _, p in pairs(s.owned) do
    mine[#mine + 1] = { id = p.id, name = p.name, lat = p.lat, lon = p.lon, kind = p.kind, held = M.held(p, d),
                        paid = p.paid, buy = p.buy, daysLeft = p.kind == 'rent' and math.max(0, (tonumber(p.untilDay) or 0) - d + 1) or nil,
                        cars = p.cars or {} }
  end
  table.sort(mine, function(a, b) return tostring(a.name) < tostring(b.name) end)
  local here = M.at(anchor, player)
  local veh = playerVeh()
  return { repair = lists.repair, property = lists.property, errors = listErr,
           searching = { repair = pending.repair ~= nil, property = pending.property ~= nil },
           mine = mine, here = here and { kind = here.kind, name = here.place.name, id = here.place.id } or nil,
           repairCost = here and M.repairCost(damageOf(veh), here.kind == 'home') or nil,
           prices = { respray = M.RESPRAY, plates = M.PLATES, labor = M.LABOR }, colours = M.COLOURS, last = last }
end

function M._shop() return shop end
function M._reset() shop = nil; state, lists, listErr, pending, last = nil, { repair = {}, property = {} }, {}, {}, nil end
function M._setList(kind, l) lists[kind] = l end
function M._state() return load() end

return M
