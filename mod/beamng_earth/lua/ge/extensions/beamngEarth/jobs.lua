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
-- trucking (the user: "just spawn me in the truck and the trailer nearby"; "rent a truck and pay them a small fee each
-- job"): the rented semi replaces your car, the trailer stands TRAILER_BACK_M behind it to back up to and couple; the
-- load counts as delivered when the trailer is within TRAILER_M of the drop-off; the job's rent comes off the pay
M.TRAILER_BACK_M = 16.0
M.TRAILER_M = 45.0

local gps = require('beamngEarth/gps')
local wallet = require('beamngEarth/wallet')

local offers, offersError, pending, pendingT, seed = {}, nil, nil, 0, nil
local active = nil          -- { job, stage = 'pickup' | 'deliver', t, expenses, own = { model, config } }
local last = nil            -- the last finished job's result (the app shows it)
local reqId = 0

local function core() return rawget(_G, 'beamngEarth_core') end

local function msg(text, icon)
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then gh.trigger('Message', { msg = text, category = 'bneJobs', icon = icon or 'info', ttl = 8 }) end
end

-- Pure: the payout of a delivery that took t s (the clock runs from the pickup) in a car that took damage, with
-- expenses ($ of tolls and fuel) to pay back. { base, bonus, late, damage, expenses, total }.
function M.payout(job, t, damage, expenses)
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
  local work = earned - dmg
  local rent = math.max(0, tonumber(job.rent) or 0)
  return { base = r(base), bonus = r(bonus), late = r(late), damage = r(dmg), expenses = r(exp), rent = r(rent),
           total = r(work + exp - rent) }
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
  elseif os.clock() - pendingT > M.REQUEST_WAIT_S then
    pending, offersError = nil, 'the world server did not answer'
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
  active = { job = job, stage = 'pickup', t = 0, expenses = 0 }
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

local function spawnTrailer(model)
  local veh, cv = playerVeh(), rawget(_G, 'core_vehicles')
  if not (veh and cv and cv.spawnNewVehicle) then return nil end
  local ok, o = pcall(function()
    local p, d = veh:getPosition(), veh:getDirectionVector()
    local back = vec3(p.x - d.x * M.TRAILER_BACK_M, p.y - d.y * M.TRAILER_BACK_M, p.z + 0.5)
    local rot = quatFromDir(vec3(d.x, d.y, 0), vec3(0, 0, 1))
    return cv.spawnNewVehicle(model, { pos = back, rot = rot, autoEnterVehicle = false })
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

local function restoreOwn()
  removeTrailer()
  if active and active.own and active.own.model then
    replaceWith(active.own.model, active.own.config)
  end
end

function M.cancel()
  if not active then return false end
  if active.stage == 'deliver' then
    restoreOwn()
    if active.job.rent then wallet.add(-active.job.rent, 'Truck rental') end
  end
  msg('Job cancelled', 'warning')
  last = { cancelled = true, job = active.job }
  active = nil
  gps.clear()
  return true
end

-- Pure: has the player arrived (within ARRIVE_M, slower than STOP_MS) at (tx, ty) from (px, py, speed)?
function M.arrived(px, py, speed, tx, ty)
  local dx, dy = tx - px, ty - py
  return dx * dx + dy * dy <= M.ARRIVE_M * M.ARRIVE_M and (speed or 0) < M.STOP_MS
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
    local tx, ty = anchor:geodeticToEnu(job.pickup.lat, job.pickup.lon, 0)
    if M.arrived(player.x, player.y, speed, tx, ty) then
      active.own = vehOf(playerVeh())
      if not replaceWith(job.car, nil) then
        msg('Could not bring out the ' .. tostring(job.car_name) .. ': job cancelled', 'error')
        active = nil
        gps.clear()
        return
      end
      if job.trailer then
        active.trailerId = spawnTrailer(job.trailer)
        msg('Your ' .. tostring(job.cargo) .. ' trailer is behind you: back up and couple it', 'info')
      end
      active.stage, active.t = 'deliver', 0
      gps.setDestination(job.dropoff.lat, job.dropoff.lon, job.dropoff.name)
      msg(string.format('Deliver the %s to %s: %s, %.1f mi', tostring(job.car_name), tostring(job.dropoff.name),
        M.clock(job.limit), (tonumber(job.dist) or 0) / 1609.344), 'check')
    end
    return
  end
  active.t = active.t + (dt or 0)
  local veh = playerVeh()
  active.damage = damageOf(veh)
  local tx, ty = anchor:geodeticToEnu(job.dropoff.lat, job.dropoff.lon, 0)
  if M.arrived(player.x, player.y, speed, tx, ty) then
    if job.trailer then
      local tp = trailerPos()
      if not tp or (tp.x - tx) ^ 2 + (tp.y - ty) ^ 2 > M.TRAILER_M ^ 2 then
        if not active.warned then msg('Bring the trailer: the load is not delivered without it', 'warning') end
        active.warned = true
        return
      end
    end
    local p = M.payout(job, active.t, active.damage, active.expenses)
    restoreOwn()
    wallet.add(p.total, 'Delivered the ' .. tostring(job.cargo or job.car_name))
    last = { job = job, pay = p, t = active.t }
    active = nil
    gps.clear()
  end
end

-- wallet.lua: a cost paid during a job (tolls, fuel) is the company's
function M.onSpend(amount)
  if active then active.expenses = active.expenses + (tonumber(amount) or 0) end
end
wallet.onSpend = M.onSpend

-- For the Jobs app (settings.uiJobs).
function M.uiState()
  local s = { offers = offers, error = offersError, searching = pending ~= nil, last = last }
  if active then
    local j = active.job
    s.active = { stage = active.stage, car = j.cargo or j.car_name, rent = j.rent, pickup = j.pickup.name, dropoff = j.dropoff.name,
                 t = active.t, limit = j.limit, pay = j.pay, dist = j.dist, expenses = active.expenses,
                 damage = active.damage or 0,
                 estimate = active.stage == 'deliver' and M.payout(j, active.t, active.damage, active.expenses) or nil }
  end
  return s
end

function M.isActive() return active ~= nil end
function M._reset() offers, offersError, pending, active, last, seed = {}, nil, nil, nil, nil, nil end   -- tests
function M._setOffers(o) offers = o end
function M._active() return active end

return M
