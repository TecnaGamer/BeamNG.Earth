-- Police heat (the user, 2026-10-26: "Cops remember your plate & car if you get away. Changing your cars color and plates
-- will drop your heat. Escaping police rewards you some money by selling the footage of the chase. Getting busted
-- results in a fine or getting your car impounded, or both"; "the longer you go the more heat you get, more heat means
-- impound and bigger fines").
-- Heat (0..MAX) belongs to a car's identity: its plate, and its look (model + colour). It builds while police chase you
-- (RATE a second times the pursuit level) on top of what the identity already had, and fades COOL a minute after.
-- A cop who sees a car whose plate has heat >= KNOWN starts a chase at once; the same model and colour count half
-- (a new plate on the same-looking car is still suspicious); a new colour AND a new plate is a new car.
-- Getting away: the heat stays on the identity, and the chase footage sells for FOOTAGE_BASE + FOOTAGE_PER_MIN a minute
-- times the highest pursuit level. Busted: a fine of FINE_BASE + FINE_PER_HEAT per heat; at IMPOUND heat or more the
-- car is impounded too (IMPOUND_FEE), and the identity's heat is cleared either way (you paid for it).
-- Saved in FILE. The arithmetic is pure (mod/tests/test_heat.lua); the game calls are in pcalls.
local M = {}

M.FILE = '/settings/beamngEarth_heat.json'
-- Heat is stars, 1 to MAX (the user, 2026-10-26: "heat should go up every few minutes of chasing. I tried to pull over
-- and got heat 5 and lost all my money"): a chase starts at 1 star (or the car's known heat), one more every STEP_S of
-- chase; a star fades every 1 / COOL minutes without a chase. Busted: the fine of the stars (FINES), impounded too
-- from IMPOUND stars. Pulling over at once costs a ticket.
M.MAX = 5
M.STEP_S = 180              -- s of chase per star
M.COOL = 0.1                -- stars lost a minute when nobody chases you (one in 10 min)
M.KNOWN = 2                 -- a plate with this many stars is recognised on sight
M.SEE_M = 60                -- ... by a cop this close who can see you
M.FOOTAGE_BASE, M.FOOTAGE_PER_MIN = 150, 60
M.FINES = { 100, 300, 750, 1500, 3000 }
-- a 1-star stop (pulled over at once) is a ticket for what you did (the user: "pull over right away should be 30 to
-- 150"): TICKET_BASE + TICKET_PER for each offense the police logged (speeding, reckless...), TICKET_MIN..TICKET_MAX
M.TICKET_MIN, M.TICKET_MAX, M.TICKET_BASE, M.TICKET_PER = 30, 150, 30, 30
M.IMPOUND, M.IMPOUND_FEE = 4, 750

local wallet = require('beamngEarth/wallet')

local state = nil           -- { plates = { [plate] = heat }, looks = { [model|colour] = heat } }
local chase = nil           -- { id, t, level, start }
local checkT, coolT = 0, 0

local function load()
  if state then return state end
  local read = rawget(_G, 'jsonReadFile')
  local ok, got = false, nil
  if read then ok, got = pcall(read, M.FILE) end
  state = (ok and type(got) == 'table' and type(got.plates) == 'table') and got or { plates = {}, looks = {} }
  state.looks = state.looks or {}
  for _, tbl in ipairs({ state.plates, state.looks }) do       -- (older files: heat 0..100)
    for k, h in pairs(tbl) do tbl[k] = math.min(M.MAX, tonumber(h) or 0) end
  end
  return state
end

local function save()
  local write = rawget(_G, 'jsonWriteFile')
  if write and state then pcall(write, M.FILE, state, true) end
end

local function msg(text, icon)
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then gh.trigger('Message', { msg = text, category = 'bneHeat', icon = icon or 'info', ttl = 7 }) end
end

-- Pure: a colour string ("0.8 0.1 0.1 1.2") rounded to tenths, so a respray counts but float noise does not.
function M.colourKey(c)
  local out = {}
  for v in tostring(c or ''):gmatch('[-%d%.]+') do
    out[#out + 1] = string.format('%.1f', tonumber(v) or 0)
    if #out == 3 then break end
  end
  return table.concat(out, ' ')
end

-- The player's car identity: { plate, look } or nil.
function M.identity(veh)
  if not veh then return nil end
  local okP, plate = pcall(function() return veh:getDynDataFieldbyName('licenseText', 0) end)
  local okM, model = pcall(function() return veh.JBeam or veh:getJBeamFilename() end)
  local okC, colour = pcall(function() return veh:getField('color', '') end)
  plate = okP and plate and tostring(plate) ~= '' and tostring(plate):upper() or nil
  local look = (okM and model or '?') .. '|' .. M.colourKey(okC and colour or '')
  return { plate = plate, look = look }
end

-- Pure: the heat a cop sees on an identity (plates and looks tables): the plate's, or half the look's.
function M.heatOf(id, plates, looks)
  if not id then return 0 end
  local p = id.plate and tonumber(plates[id.plate]) or 0
  local l = id.look and tonumber(looks[id.look]) or 0
  return math.max(p, l / 2)
end

-- Pure: heat after dt s of a chase at level, from h.
-- Pure: the stars of a chase of t s that began at h0 stars.
function M.build(h0, t)
  return math.min(M.MAX, math.max(1, h0 or 0) + math.floor((t or 0) / M.STEP_S))
end

-- Pure: the footage's price for a chase of t s at its highest level.
function M.footage(t, level)
  return math.floor((M.FOOTAGE_BASE + M.FOOTAGE_PER_MIN * (t or 0) / 60) * math.max(1, level or 1) / 5 + 0.5) * 5
end

-- Pure: { fine, impound (fee or 0), total } when busted with heat h.
function M.busted(h, offenses)
  local stars = math.max(1, math.min(M.MAX, math.floor((h or 0) + 0.5)))
  local fine = M.FINES[stars]
  if stars == 1 then
    fine = math.max(M.TICKET_MIN, math.min(M.TICKET_MAX, M.TICKET_BASE + M.TICKET_PER * math.max(0, offenses or 1)))
  end
  local imp = stars >= M.IMPOUND and M.IMPOUND_FEE or 0
  return { fine = fine, impound = imp, total = fine + imp }
end

local function playerVeh()
  local be_ = rawget(_G, 'be')
  return be_ and be_:getPlayerVehicle(0) or nil
end

local function setHeat(id, h)
  local s = load()
  if id.plate then s.plates[id.plate] = h end
  if id.look then s.looks[id.look] = h end
end

-- The stock pursuit hook (gameplay/police.lua: onPursuitAction(id, 'start' | 'evade' | 'arrest' | 'release', data)),
-- forwarded by core for the player's car.
function M.onPursuitAction(vid, action, data)
  local be_ = rawget(_G, 'be')
  if not be_ or vid ~= be_:getPlayerVehicleID(0) then return end
  local id = M.identity(playerVeh())
  if not id then return end
  local s = load()
  if action == 'start' then
    pcall(function() require('beamngEarth/jobs').onPolice() end)       -- (no clean record bonus on this job)
    local h0 = math.max(1, math.floor(M.heatOf(id, s.plates, s.looks) + 0.5))
    chase = { id = id, t = 0, level = (data and data.mode) or 1, h0 = h0, heat = h0 }
  elseif action == 'evade' then
    local c = chase or { t = 0, level = 1, heat = M.heatOf(id, s.plates, s.looks) }
    setHeat(id, c.heat)
    save()
    local pay = M.footage(c.t, c.level)
    wallet.add(pay, 'Sold the chase footage')
    msg(string.format('You got away. The police know your plate %s: heat %d', tostring(id.plate or '?'), math.floor(c.heat)),
      'warning')
    chase = nil
  elseif action == 'arrest' then
    local h = chase and chase.heat or M.heatOf(id, s.plates, s.looks)
    local offs = data and type(data.offensesList) == 'table' and #data.offensesList or nil
    local b = M.busted(h, offs)
    wallet.add(-b.fine, 'Police fine')
    if b.impound > 0 then
      wallet.add(-b.impound, 'Impound release fee')
      msg('Busted: your car was impounded. Fine and release fee paid.', 'error')
    else
      msg('Busted: fine paid.', 'warning')
    end
    setHeat(id, 0)
    save()
    chase = nil
  end
end

-- Every frame (core): build heat during a chase, cool it after, and let cops recognise a known car.
function M.update(dt)
  dt = dt or 0
  local police = rawget(_G, 'gameplay_police')
  local be_ = rawget(_G, 'be')
  if chase then
    local okD, pd = pcall(function() return police and police.getPursuitData and police.getPursuitData() end)
    local level = okD and type(pd) == 'table' and tonumber(pd.mode) or chase.level
    if level and level >= 1 then
      chase.t = chase.t + dt
      chase.level = math.max(chase.level or 1, level)
      local before = chase.heat
      chase.heat = M.build(chase.h0, chase.t)
      if chase.heat > before then msg(string.rep('*', chase.heat) .. '  Heat ' .. chase.heat .. ': the police are calling in more units', 'warning') end
    end
    return
  end
  coolT = coolT + dt
  if coolT >= 60 then
    local s = load()
    for _, tbl in ipairs({ s.plates, s.looks }) do
      for k, h in pairs(tbl) do
        local n = (tonumber(h) or 0) - M.COOL * coolT / 60
        tbl[k] = n > 0.05 and n or nil
      end
    end
    coolT = 0
    save()
  end
  checkT = checkT + dt
  if checkT < 1 or not (police and police.getNearestPoliceVehicle and be_) then return end
  checkT = 0
  local id = M.identity(playerVeh())
  local s = load()
  local h = M.heatOf(id, s.plates, s.looks)
  if h < M.KNOWN then return end
  local pid = be_:getPlayerVehicleID(0)
  local ok, cop, dist = pcall(police.getNearestPoliceVehicle, pid, true, true)
  if ok and cop and dist and dist <= M.SEE_M then
    pcall(police.setPursuitMode, h >= M.IMPOUND and 2 or 1, pid)
    msg('A police officer recognised your car!', 'warning')
  end
end

-- For the HUD: the player's car's heat now.
function M.current()
  if chase then return chase.heat end
  local s = load()
  return M.heatOf(M.identity(playerVeh()), s.plates, s.looks)
end

function M._reset() state, chase, checkT, coolT = nil, nil, 0, 0 end   -- tests
function M._state() return load() end
function M._chase() return chase end

return M
