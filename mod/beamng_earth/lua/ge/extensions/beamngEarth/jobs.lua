-- Jobs (the user, 2026-10-25: "we need jobs around the map. Garage to garage delivery, amazon delivery, taxi, cop,
-- paramedic driver, truck driving ..., road maintenance"); the first: garage-to-garage delivery, the base the others
-- reuse (an offer list from the server, a job with stages, a payout into the wallet).
--
-- A delivery: the Jobs app lists offers (server jobfind.py: a customer's car at a real garage near you, to take to
-- another real garage). Accepting one sets the GPS to the pickup garage. Stopping there (ARRIVE_M, slower than STOP_MS)
-- swaps your car for the customer's (core_vehicles.replaceVehicle: in place) and starts the clock; stopping at the
-- drop-off garage pays you and gives your own car back (the same model and configuration, repaired and refuelled).
-- Pay (the user: "fair but real, damage cuts pay, time limits are very fair and early gets bonus, if you have to pay
-- for a lot of tolls and gas for a trip the company will pay you fairly for that"): the offer's pay; up to EARLY_BONUS
-- more for arriving before EARLY_X times the drive's duration (less the later, none at the limit); LATE_PER_MIN less
-- a minute past the limit (at most LATE_MAX); less the share of DAMAGE_FULL the car took (of all that); plus every toll and fuel
-- bill paid from accepting to delivering (wallet.lua tells us of each).
-- payout() and the stage logic are pure (tests: mod/tests/test_jobs.lua); the vehicle and GPS calls are the game's.
local M = {}

M.ARRIVE_M = 35.0
M.STOP_MS = 3.0
M.EARLY_X = 1.2
M.EARLY_BONUS = 0.20
M.LATE_PER_MIN = 0.02
M.LATE_MAX = 0.5
M.DAMAGE_FULL = 15000.0     -- the vehicle's damage (beamstate, ~$) at which the whole pay is lost
M.REQUEST_WAIT_S = 40
M.TRUCK_WAIT_S = 150        -- (truck searches: far cells, a first download each)
-- trucking (the user: "just spawn me in the truck and the trailer nearby"; "rent a truck and pay them a small fee each
-- job"): the rented semi replaces your car, the trailer stands TRAILER_BACK_M behind it to back up to and couple; the
-- load counts as delivered when the trailer is within TRAILER_M of the drop-off; the job's rent comes off the pay
M.TRAILER_BACK_M = 16.0
-- bonuses on top of the pay (the user, 2026-10-26: "add bonuses for things, showing up early, good driver streak, no
-- fines"): SPOTLESS for no damage (under CLEAN_DMG), RECORD for no police chase or fine during the job, and STREAK_STEP
-- for each clean delivery before it in a row (no damage, no police; at most STREAK_MAX), kept in STREAK_FILE
M.SPOTLESS, M.RECORD, M.STREAK_STEP, M.STREAK_MAX = 0.15, 0.10, 0.05, 0.25
M.CLEAN_DMG = 150
M.STREAK_FILE = '/settings/beamngEarth_jobs.json'
M.TRAILER_M = 45.0

local gps = require('beamngEarth/gps')
local wallet = require('beamngEarth/wallet')

local offers, offersError, pending, pendingT, seed, pendingWait = {}, nil, nil, 0, nil, nil
local active = nil          -- { job, stage = 'pickup' | 'deliver', t, expenses, own = { model, config } }
local last = nil            -- the last finished job's result (the app shows it)
local reqId = 0

local function core() return rawget(_G, 'beamngEarth_core') end

local streakN = nil
function M.streak()
  if streakN == nil then
    local read = rawget(_G, 'jsonReadFile')
    local ok, got = false, nil
    if read then ok, got = pcall(read, M.STREAK_FILE) end
    streakN = ok and type(got) == 'table' and tonumber(got.streak) or 0
  end
  return streakN
end
local function setStreak(n)
  streakN = n
  local write = rawget(_G, 'jsonWriteFile')
  if write then pcall(write, M.STREAK_FILE, { streak = n }, true) end
end

local function msg(text, icon)
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then gh.trigger('Message', { msg = text, category = 'bneJobs', icon = icon or 'info', ttl = 8 }) end
end

-- Pure: the payout of a delivery that took t s (the clock runs from the pickup) in a car that took damage, with
-- expenses ($ of tolls and fuel) to pay back. { base, bonus, late, damage, expenses, total }.
function M.payout(job, t, damage, expenses, police, streak)
  local base = tonumber(job.pay) or 0
  local dur, limit = tonumber(job.dur) or 0, tonumber(job.limit) or 0
  local early = dur * M.EARLY_X
  local bonus, late = 0, 0
  if t <= early then
    bonus = base * M.EARLY_BONUS
  elseif t <= limit and limit > early then
    bonus = base * M.EARLY_BONUS * (limit - t) / (limit - early)
  elseif t > limit then
    late = base * math.min(M.LATE_MAX, M.LATE_PER_MIN * (t - limit) / 60)
  end
  local earned = math.max(0, base + bonus - late)          -- (a wrecked car loses its early bonus too)
  local dmg = earned * math.min(1, math.max(0, tonumber(damage) or 0) / M.DAMAGE_FULL)
  local exp = math.max(0, tonumber(expenses) or 0)
  local r = function(v) return math.floor(v * 100 + 0.5) / 100 end
  local spotless = (tonumber(damage) or 0) < M.CLEAN_DMG and base * M.SPOTLESS or 0
  local whole = 1 - math.min(1, math.max(0, tonumber(damage) or 0) / M.DAMAGE_FULL)    -- (a wreck earns no bonuses)
  local record = (not police) and base * M.RECORD * whole or 0
  local sb = base * math.min(M.STREAK_MAX, M.STREAK_STEP * math.max(0, tonumber(streak) or 0)) * whole
  local work = earned - dmg + spotless + record + sb
  local rent = math.max(0, tonumber(job.rent) or 0)
  return { base = r(base), bonus = r(bonus), late = r(late), damage = r(dmg), expenses = r(exp), rent = r(rent),
           spotless = r(spotless), record = r(record), streak = r(sb), total = r(work + exp - rent) }
end

-- Pure: m:ss
function M.clock(s)
  s = math.max(0, math.floor((tonumber(s) or 0) + 0.5))
  return string.format('%d:%02d', math.floor(s / 60), s % 60)
end

-- Offers ----------------------------------------------------------------------------------------------------------
function M.refresh(lat, lon, kind)
  local c = core()
  if not (c and c.requestDeliveryJobs) then offersError = 'BeamNG.Earth is not loaded' return false end
  if not (lat and lon) then offersError = 'no position yet' return false end
  seed = (seed or (os.time and os.time() or 0)) + 1
  reqId = reqId + 1
  if not c.requestDeliveryJobs(reqId, lat, lon, seed, kind) then offersError = 'not connected to the world server' return false end
  pending, pendingT, offersError = reqId, os.clock(), nil
  pendingWait = (kind == 'truck' or kind == 'parcel') and M.TRUCK_WAIT_S or M.REQUEST_WAIT_S
  return true
end

local function poll()
  if not pending then return end
  local c = core()
  local r = c and c.takeDeliveryJobs and c.takeDeliveryJobs(pending)
  if r then
    pending = nil
    offers = type(r.jobs) == 'table' and r.jobs or {}
    offersError = (#offers == 0) and (r.error or 'no jobs here') or nil
  elseif os.clock() - pendingT > (pendingWait or M.REQUEST_WAIT_S) then
    pending, offersError = nil, 'the world server did not answer in time: if you just updated, restart with Play BeamNG.Earth.bat'
  end
end

-- The job -----------------------------------------------------------------------------------------------------------
local function playerVeh()
  local be_ = rawget(_G, 'be')
  return be_ and be_:getPlayerVehicle(0) or nil
end

local function vehOf(veh)
  if not veh then return nil end
  local ok, model = pcall(function() return veh.JBeam or veh:getJBeamFilename() end)
  local okc, config = pcall(function() return veh.partConfig end)
  return { model = ok and model or nil, config = okc and config or nil }
end

local function replaceWith(model, config)
  local cv = rawget(_G, 'core_vehicles')
  if not (cv and cv.replaceVehicle and model) then return false end
  return pcall(cv.replaceVehicle, model, { config = config })
end

function M.accept(i)
  if active then return false, 'finish or cancel the current job first' end
  local job = offers[tonumber(i) or 0]
  if not job then return false, 'no such job' end
  active = { job = job, stage = 'pickup', t = 0, expenses = 0, police = false }
  gps.setDestination(job.pickup.lat, job.pickup.lon, job.pickup.name)
  msg('Job: pick up the ' .. tostring(job.car_name) .. ' at ' .. tostring(job.pickup.name))
  return true
end

local function removeTrailer()
  local id = active and active.trailerId
  local be_ = rawget(_G, 'be')
  local o = id and be_ and be_.getObjectByID and be_:getObjectByID(id)
  if o then pcall(function() o:delete() end) end
  if active then active.trailerId = nil end
end

local function spawnTrailer(model, config)
  local veh, cv = playerVeh(), rawget(_G, 'core_vehicles')
  if not (veh and cv and cv.spawnNewVehicle) then return nil end
  local ok, o = pcall(function()
    local p, d = veh:getPosition(), veh:getDirectionVector()
    local back = vec3(p.x - d.x * M.TRAILER_BACK_M, p.y - d.y * M.TRAILER_BACK_M, p.z + 0.5)
    local rot = quatFromDir(vec3(d.x, d.y, 0), vec3(0, 0, 1))
    -- (config: the stock trailer config carrying the load, server jobfind.LOADS: steel coils, fuel, logs...)
    return cv.spawnNewVehicle(model, { pos = back, rot = rot, autoEnterVehicle = false, config = config })
  end)
  return ok and o and o.getID and o:getID() or nil
end

-- The trailer's world position, or nil (none, or gone).
local function trailerPos()
  local id = active and active.trailerId
  local be_ = rawget(_G, 'be')
  local o = id and be_ and be_.getObjectByID and be_:getObjectByID(id)
  if not o then return nil end
  local ok, p = pcall(function() return o:getPosition() end)
  return ok and p or nil
end

-- Your own car through a job (the user, 2026-10-26: "save my cars condition and hang on to it until the job is done
-- then give it back. same fuel and all"): not re-made but parked and hidden (setActive(false): its damage, fuel and
-- everything kept) while the job's vehicle stands in its place; at the end the job's vehicle goes and yours comes back
-- where it finished, and you are put in it.
local function workVehicle(model)
  local be_, cv = rawget(_G, 'be'), rawget(_G, 'core_vehicles')
  local own = playerVeh()
  if not (be_ and cv and cv.spawnNewVehicle and own) then return false end
  local ok = pcall(function()
    local p, d = own:getPosition(), own:getDirectionVector()
    local rot = quatFromDir(vec3(d.x, d.y, 0), vec3(0, 0, 1))
    active.ownId = own:getID()
    own:setActive(0)
    local v = cv.spawnNewVehicle(model, { pos = vec3(p.x, p.y, p.z + 0.3), rot = rot, autoEnterVehicle = true })
    active.workId = v and v.getID and v:getID() or nil
  end)
  if not ok or not active.workId then
    local o = active.ownId and be_:getObjectByID(active.ownId)
    if o then pcall(function() o:setActive(1) end) end
    return false
  end
  return true
end

local function restoreOwn()
  removeTrailer()
  local be_ = rawget(_G, 'be')
  local own = active and active.ownId and be_ and be_:getObjectByID(active.ownId)
  if own then
    -- (each step on its own: one failing must not leave you without your car)
    local w = active.workId and be_:getObjectByID(active.workId)
    local okP, p, d = pcall(function() return (w or own):getPosition(), (w or own):getDirectionVector() end)
    if w then pcall(function() w:delete() end) end
    pcall(function() own:setActive(1) end)
    if okP and p and d then
      pcall(function()
        local rot = quatFromDir(vec3(d.x, d.y, 0), vec3(0, 0, 1))
        local sp = rawget(_G, 'spawn')
        if sp and sp.safeTeleport then sp.safeTeleport(own, vec3(p.x, p.y, p.z + 0.3), rot)
        else own:setPositionRotation(p.x, p.y, p.z + 0.3, rot.x, rot.y, rot.z, rot.w) end
      end)
    end
    pcall(function() be_:enterVehicle(0, own) end)
  elseif active and active.own and active.own.model then     -- (yours gone: a copy of it, as before)
    replaceWith(active.own.model, active.own.config)
  end
end

-- The phone's Start work (the user: "when we get there, add a start work button in the phone"): at the pickup's bay,
-- your car parked, the job's vehicle (and trailer) out, the clock running.
function M.startWork()
  if not (active and active.stage == 'pickup') then return false, 'no job waiting to start' end
  if not active.ready then return false, 'pull into the bay at the pickup first' end
  local job = active.job
  active.own = vehOf(playerVeh())
  if not workVehicle(job.car) then return false, 'could not bring out the ' .. tostring(job.car_name) end
  if job.trailer then
    active.trailerId = spawnTrailer(job.trailer, job.trailer_config)
    msg('Your ' .. tostring(job.cargo) .. ' trailer is behind you: back up and couple it', 'info')
  end
  active.stage, active.t, active.stop, active.ready = 'deliver', 0, 1, nil
  local first = job.stops and job.stops[1] or job.dropoff
  gps.setDestination(first.lat, first.lon, first.name)
  msg(string.format('Deliver to %s: %s, %.1f mi', tostring(first.name), M.clock(job.limit),
    (tonumber(job.dist) or 0) / 1609.344), 'check')
  return true
end

-- The delivery bays (the user: "the same feature as career where you have a spot to pull into"): a green bay drawn at
-- the pickup and at each drop-off; you are there when stopped inside it.
M.BAY_M = 5.0
local function drawBay(anchor, place, player)
  local dd = rawget(_G, 'debugDrawer')
  if not (dd and place and player) then return end
  local x, y = anchor:geodeticToEnu(place.lat, place.lon, 0)
  if (x - player.x) ^ 2 + (y - player.y) ^ 2 > 250 ^ 2 then return end
  pcall(function()
    local z = player.z or 0
    local be_ = rawget(_G, 'be')
    local okz, h = pcall(function() return be_:getSurfaceHeightBelow(vec3(x, y, z + 20)) end)
    if okz and tonumber(h) and h > -1e6 then z = h end
    dd:drawCylinder(vec3(x, y, z), vec3(x, y, z + 0.06), M.BAY_M, ColorF(0.2, 0.85, 0.35, 0.4))
  end)
end

function M.cancel()
  if not active then return false end
  if active.stage == 'deliver' then
    restoreOwn()
    if active.job.rent then wallet.add(-active.job.rent, 'Truck rental') end
  end
  msg('Job cancelled', 'warning')
  setStreak(0)
  last = { cancelled = true, job = active.job }
  active = nil
  gps.clear()
  return true
end

-- Pure: has the player arrived (within ARRIVE_M, slower than STOP_MS) at (tx, ty) from (px, py, speed)?
function M.arrived(px, py, speed, tx, ty)
  local dx, dy = tx - px, ty - py
  local r = M.BAY_M or M.ARRIVE_M                -- (inside the bay)
  return dx * dx + dy * dy <= r * r and (speed or 0) < M.STOP_MS
end

local function damageOf(veh)
  local mp = rawget(_G, 'map')
  local o = veh and mp and mp.objects and mp.objects[veh:getID()]
  return o and tonumber(o.damage) or 0
end

-- Every frame (core): dt the simulation's (stops while paused); anchor, player as gps.update's.
function M.update(dt, anchor, player)
  poll()
  if not active or not anchor or not player then return end
  local job = active.job
  local speed = math.sqrt((player.vx or 0) ^ 2 + (player.vy or 0) ^ 2)
  if active.stage == 'pickup' then
    drawBay(anchor, job.pickup, player)
    local tx, ty = anchor:geodeticToEnu(job.pickup.lat, job.pickup.lon, 0)
    local was = active.ready
    active.ready = M.arrived(player.x, player.y, speed, tx, ty) or nil
    if active.ready and not was then
      msg('At ' .. tostring(job.pickup.name) .. ': press Start work on your phone', 'check')
      local gh = rawget(_G, 'guihooks')
      if gh and gh.trigger then gh.trigger('BneEarthPhone', { up = true, app = job.stops and 'beamazon' or 'jobs' }) end
    end
    return
  end
  active.t = active.t + (dt or 0)
  local veh = playerVeh()
  active.damage = damageOf(veh)
  -- (a Beamazon route: its stops one after the other, the last the drop-off)
  local target = job.stops and job.stops[active.stop or 1] or job.dropoff
  drawBay(anchor, target, player)
  local tx, ty = anchor:geodeticToEnu(target.lat, target.lon, 0)
  if M.arrived(player.x, player.y, speed, tx, ty) and job.stops and (active.stop or 1) < #job.stops then
    active.stop = (active.stop or 1) + 1
    local nx = job.stops[active.stop]
    gps.setDestination(nx.lat, nx.lon, nx.name)
    msg(string.format('Parcel delivered (%d/%d)', active.stop - 1, #job.stops), 'check')
    return
  end
  if M.arrived(player.x, player.y, speed, tx, ty) then
    if job.trailer then
      local tp = trailerPos()
      if not tp or (tp.x - tx) ^ 2 + (tp.y - ty) ^ 2 > M.TRAILER_M ^ 2 then
        if not active.warned then msg('Bring the trailer: the load is not delivered without it', 'warning') end
        active.warned = true
        return
      end
    end
    local n = M.streak()
    local p = M.payout(job, active.t, active.damage, active.expenses, active.police, n)
    local clean = (tonumber(active.damage) or 0) < M.CLEAN_DMG and not active.police
    setStreak(clean and n + 1 or 0)
    restoreOwn()
    wallet.add(p.total, 'Delivered the ' .. tostring(job.cargo or job.car_name))
    last = { job = job, pay = p, t = active.t, streak = M.streak() }
    active = nil
    gps.clear()
  end
end

-- wallet.lua: a cost paid during a job (tolls, fuel) is the company's
function M.onSpend(amount, reason)
  -- (tolls and fuel only: a police fine is yours, and it ends the clean record)
  local why = tostring(reason or '')
  if not active then return end
  if why:find('^Toll') or why:find('^Fuel') then
    active.expenses = active.expenses + (tonumber(amount) or 0)
  elseif why:find('[Pp]olice') or why:find('[Ii]mpound') then
    active.police = true
  end
end

-- heat.lua: a chase started during the job (no clean record bonus, the streak ends)
function M.onPolice() if active then active.police = true end end
wallet.onSpend = M.onSpend

-- For the Jobs app (settings.uiJobs).
function M.uiState()
  local s = { offers = offers, error = offersError, searching = pending ~= nil, last = last, streak = M.streak() }
  if active then
    local j = active.job
    s.active = { ready = active.ready, stop = active.stop, stops = j.stops and #j.stops or nil, stage = active.stage, car = j.cargo or j.car_name, rent = j.rent, pickup = j.pickup.name, dropoff = j.dropoff.name,
                 t = active.t, limit = j.limit, pay = j.pay, dist = j.dist, expenses = active.expenses,
                 damage = active.damage or 0,
                 police = active.police,
                 estimate = active.stage == 'deliver' and M.payout(j, active.t, active.damage, active.expenses, active.police,
                   M.streak()) or nil }
  end
  return s
end

function M.isActive() return active ~= nil end
function M._reset() offers, offersError, pending, active, last, seed, streakN = {}, nil, nil, nil, nil, nil, nil end   -- tests
function M._setOffers(o) offers = o end
function M._active() return active end

return M
