-- BeamNG.Earth: real-world sky. Keeps the level's TimeOfDay on the real date and clock for
-- wherever the player is, so core_celestial draws the real sun, moon and stars.
--
-- core_celestial (core/celestial.lua) computes the sky from TimeOfDay year/month/day, `time`
-- (civil local clock as a 0..1 day fraction, noon = 0; celestial.lua:313-318) and the UTC offset
-- (tod.utcOffset if set, else derived from longitude; celestial.lua:285-296). We always write
-- utcOffset explicitly together with a matching local `time`, so the instant (Julian date) is
-- exact even where the timezone guess is wrong; a wrong guess only shifts the displayed clock.
-- Latitude/longitude are kept current by beamngEarth_core (core_celestial.setLocation).
-- The real UTC offset (Open-Meteo timezone=auto, from beamngEarth_weather) replaces the guess
-- within REAL_TZ_KM of the point it was reported for.
--
-- Modes: 'real'   = real UTC now, clock runs at real speed, re-synced every RESYNC_S seconds
--                   (the default; missions and the big map pause it, they set and restore their own time).
--        'manual' = hands off (use the game's own environment/time controls).
-- In the BeamNG.Earth level, a time change by anyone else (the pause menu, another mod) after the
-- level-start grace period that really moves the clock off the real one (timeChanges) switches to
-- 'manual' with one toast and saves that as the setting (beamngEarth_settings); choosing Real again
-- re-syncs. Our own re-syncs write the TimeOfDay fields directly, so core_environment's
-- onEnvironmentChanged never sees them.

local M = {}

local RESYNC_S = 30
M.REAL_TZ_KM = 200
M.LEVEL_ID = 'beamng_earth'
M.REAL_DAY_S = 86400       -- dayLength of the real clock
M.GRACE_S = 20             -- after a level start: core_environment's setState(init_env), freeroam's start time
M.TIME_EPS = 0.002         -- day fraction (~3 min): a time this close to the clock is no change
M.MISSION_SETUP_S = 2      -- missions set their time just before 'started': a change switches to manual
                           -- only when no mission started within this long
M.MISSION_RESUME_S = 5
M.BIGMAP_TAIL_S = 2        -- the big map puts its time back as it closes

local mode = 'real'
-- A BeamMP server's clock (its plugin's G.TIME, through mpsession): { hours = local time it starts at, speed = game
-- seconds per real second (0: it stays), epoch = when it started (os.time) }. In 'real' mode the sky then shows that
-- local time instead of the real one, on the real date at the player's place; nil: the real clock.
local serverClock = nil
local timer = RESYNC_S -- sync on the first update
local realTz = nil     -- { hours, lat, lon }: the real UTC offset at a point (setRealUtcOffset)
local clock, graceUntil, bigMapUntil = 0, M.GRACE_S, 0
local inOurLevel, mission = false, false
local pending = nil    -- { at, keys }: someone else changed the time; manual after MISSION_SETUP_S
local switched = nil   -- the last switch to manual: { at, keys }

-- Day of week, 0 = Sunday (Sakamoto), and the Sunday helpers mirror celestial.lua:261-283 so the
-- clock we display agrees with what core_celestial would derive on its own.
local function dow(y, m, d)
  local t = { 0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4 }
  if m < 3 then y = y - 1 end
  return (y + math.floor(y / 4) - math.floor(y / 100) + math.floor(y / 400) + t[m] + d) % 7
end
local function lastSunday(y, m) local last = (m == 4 or m == 9 or m == 11) and 30 or 31; return last - dow(y, m, last) end
local function firstSunday(y, m) return 1 + (7 - dow(y, m, 1)) % 7 end

local function dstActive(rule, y, mo, d)
  if rule == 'eu' then
    if mo < 3 or mo > 10 then return false elseif mo > 3 and mo < 10 then return true end
    return (mo == 3 and d >= lastSunday(y, 3)) or (mo == 10 and d < lastSunday(y, 10))
  elseif rule == 'us' then
    if mo < 3 or mo > 11 then return false elseif mo > 3 and mo < 11 then return true end
    return (mo == 3 and d >= firstSunday(y, 3) + 7) or (mo == 11 and d < firstSunday(y, 11))
  elseif rule == 'au' then
    if mo > 4 and mo < 10 then return false elseif mo < 4 or mo > 10 then return true end
    return (mo == 4 and d < firstSunday(y, 4)) or (mo == 10 and d >= firstSunday(y, 10))
  end
  return false
end

-- Approximate civil UTC offset (hours) for a position: nearest 15-degree meridian plus the same
-- belt-based DST guess as core_celestial's "auto" rule.
function M.utcOffsetFor(lat, lon, y, mo, d)
  local std = math.floor(lon / 15.0 + 0.5)
  local rule = 'none'
  if lat <= -30 then rule = 'au'
  elseif lat >= 30 then rule = (lon > -170 and lon < -30) and 'us' or 'eu' end
  return std + (dstActive(rule, y, mo, d) and 1 or 0)
end

local function distKm(lat1, lon1, lat2, lon2)
  local p1, p2 = math.rad(lat1), math.rad(lat2)
  local a = math.sin((p2 - p1) / 2) ^ 2 + math.cos(p1) * math.cos(p2) * math.sin(math.rad(lon2 - lon1) / 2) ^ 2
  return 2 * 6371.0088 * math.asin(math.min(1, math.sqrt(a)))
end

-- Pure: TimeOfDay fields for UTC epoch seconds `epoch` at (lat, lon); real = { hours, lat, lon }
-- (optional): a real UTC offset, used within REAL_TZ_KM of its point.
-- Returns { year, month, day, time (noon = 0), utcOffset, localHours }.
function M.todFields(epoch, lat, lon, real)
  local u = os.date('!*t', epoch)
  local off
  if real and real.hours and real.lat and real.lon and distKm(lat, lon, real.lat, real.lon) <= M.REAL_TZ_KM then
    off = real.hours
  else
    off = M.utcOffsetFor(lat, lon, u.year, u.month, u.day)
  end
  local l = os.date('!*t', epoch + math.floor(off * 3600 + 0.5))
  local localHours = l.hour + l.min / 60 + l.sec / 3600
  return {
    year = l.year, month = l.month, day = l.day,
    time = (localHours / 24 - 0.5) % 1.0,
    utcOffset = off, localHours = localHours,
  }
end

local function getTod()
  local names = scenetree.findClassObjects('TimeOfDay')
  return names and names[1] and scenetree.findObject(names[1]) or nil
end

-- Pure: the TimeOfDay keys of an onEnvironmentChanged table that move the clock off the real one:
-- cur = the TimeOfDay now (the hook fires before the change is applied), real = todFields for now
-- (nil = cur only). A time within TIME_EPS of either (the real clock moves on between a caller's
-- read and its write), the same date, play on, the real day length and the same UTC offset are no
-- change. Returns the keys, sorted.
local TIME_KEYS = { time = true, year = true, month = true, day = true, play = true, dayLength = true, utcOffset = true }
function M.timeChanges(changes, cur, real)
  local out = {}
  local function far(a, b)
    a, b = tonumber(a), tonumber(b)
    if not a or not b then return true end
    local d = math.abs(a - b) % 1
    return math.min(d, 1 - d) > M.TIME_EPS
  end
  local t = tonumber(changes.time)
  if t and far(t, cur.time) and (not real or far(t, real.time)) then out[#out + 1] = 'time' end
  for _, k in ipairs({ 'year', 'month', 'day' }) do
    local v = tonumber(changes[k])
    if v and v ~= tonumber(cur[k]) and (not real or v ~= real[k]) then out[#out + 1] = k end
  end
  local p = changes.play
  if p == false or p == 'false' or p == 0 or p == '0' then out[#out + 1] = 'play' end
  local dl = tonumber(changes.dayLength)
  if dl and math.abs(dl - M.REAL_DAY_S) > 1 then out[#out + 1] = 'dayLength' end
  local off = tonumber(changes.utcOffset)
  if off then
    local have = tonumber(cur.utcOffset)
    if (not have or math.abs(off - have) > 0.01) and (not real or math.abs(off - real.utcOffset) > 0.01) then
      out[#out + 1] = 'utcOffset'
    end
  end
  table.sort(out)
  return out
end

-- Pure: the local hours a server clock c shows at time now (os.time): its start, run on at its speed.
function M.serverHours(c, now)
  local h = tonumber(c and c.hours) or 12
  local speed = math.max(0, tonumber(c and c.speed) or 0)
  local since = math.max(0, (tonumber(now) or 0) - (tonumber(c and c.epoch) or tonumber(now) or 0))
  return (h + since * speed / 3600) % 24
end

function M.sync()
  local tod = getTod()
  if not tod then return false end
  local lat, lon = tonumber(tod.latitude) or 0, tonumber(tod.longitude) or 0
  local now = os.time()
  local f = M.todFields(now, lat, lon, realTz)
  tod.year, tod.month, tod.day = f.year, f.month, f.day
  tod.utcOffset = f.utcOffset
  if serverClock then
    local speed = math.max(0, tonumber(serverClock.speed) or 0)
    tod.time = (M.serverHours(serverClock, now) / 24 - 0.5) % 1.0
    tod.dayLength = speed > 0 and M.REAL_DAY_S / speed or M.REAL_DAY_S
    tod.play = speed > 0
    return true
  end
  tod.time = f.time
  tod.dayLength = M.REAL_DAY_S -- real-time clock between re-syncs
  tod.play = true
  return true
end

-- A BeamMP server's clock (see serverClock above; nil: the real one again). Synced at once.
function M.setServerClock(c)
  if type(c) == 'table' and tonumber(c.hours) then
    serverClock = { hours = tonumber(c.hours) % 24, speed = math.max(0, tonumber(c.speed) or 0),
      epoch = tonumber(c.epoch) or os.time() }
  else
    serverClock = nil
  end
  timer = RESYNC_S
  return serverClock
end

function M.serverClock() return serverClock end

function M.setMode(m)
  if m ~= 'real' and m ~= 'manual' then return 'mode must be "real" or "manual"' end
  if m == 'manual' and mode ~= 'manual' then M.manualBy = debug.traceback('', 2) end   -- (who: the MP status)
  mode = m
  pending = nil
  if mode == 'real' then timer = RESYNC_S end
  return 'sky mode: ' .. mode
end
function M.getMode() return mode end

-- The real UTC offset (hours) at (lat, lon), from the weather service; re-syncs on the next update.
function M.setRealUtcOffset(hours, lat, lon)
  if not (tonumber(hours) and tonumber(lat) and tonumber(lon)) then return end
  local changed = not realTz or realTz.hours ~= hours
  realTz = { hours = tonumber(hours), lat = tonumber(lat), lon = tonumber(lon) }
  if changed and mode == 'real' then timer = RESYNC_S end
end

-- Pure: the sun's elevation (degrees above the horizon, refraction left out) at local date y-mo-d, localHours
-- (0-24) in a zone utcOffset hours from UTC, at lat / lon (NOAA's low-precision formulas: within ~1 degree, enough to
-- tell day from night). The game's own clock goes in (status() below), so the map's badge shows the night in the
-- Manual sky mode too, not only on the real clock (GitHub #874: a sun icon at night under a clear sky).
function M.sunElevationDeg(y, mo, d, localHours, utcOffset, lat, lon)
  local rad = math.pi / 180
  local utc = (tonumber(localHours) or 0) - (tonumber(utcOffset) or 0)
  -- the day of the year (1 Jan = 1), the UTC hours carried into it (no os.time: the game's clock, not the machine's)
  y, mo, d = tonumber(y) or 2000, tonumber(mo) or 1, tonumber(d) or 1
  local leap = (y % 4 == 0 and y % 100 ~= 0) or y % 400 == 0
  local before = { 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334 }
  local doy = (before[mo] or 0) + d + ((leap and mo > 2) and 1 or 0) + utc / 24
  local g = 2 * math.pi / (leap and 366 or 365) * (doy - 1)
  local eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
  local decl = 0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
    + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g)
  local solarMin = utc * 60 + eqt + 4 * (tonumber(lon) or 0)       -- true solar time, minutes
  local ha = (solarMin / 4 - 180) * rad
  local phi = (tonumber(lat) or 0) * rad
  local s = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
  return math.asin(math.max(-1, math.min(1, s))) / rad
end
M.NIGHT_ELEV_DEG = -0.833   -- the sun's centre this far under the horizon: its top edge has set (refraction included)

function M.status()
  local tod = getTod()
  if not tod then return { mode = mode, pending = pending, switched = switched } end
  local h = ((tonumber(tod.time) or 0) + 0.5) % 1.0 * 24
  local sunElev = nil
  if tod.year and tod.month and tod.day and tod.latitude and tod.longitude then
    sunElev = M.sunElevationDeg(tod.year, tod.month, tod.day, h, tod.utcOffset, tod.latitude, tod.longitude)
  end
  return {
    sunElev = sunElev,
    mode = mode, pending = pending, switched = switched, inOurLevel = inOurLevel,
    grace = math.max(0, graceUntil - clock), mission = mission, lat = tod.latitude, lon = tod.longitude,
    date = string.format('%04d-%02d-%02d', tod.year or 0, tod.month or 0, tod.day or 0),
    localTime = string.format('%02d:%02d', math.floor(h), math.floor((h % 1) * 60)),
    utcOffset = tod.utcOffset,
  }
end

------------------------------------------------------------------------------------------
-- Clouds (2026-10-10: the level had no CloudLayer, so the sky was always clear).
-- The 0.39 volumetric clouds come from a CloudLayer object (core/environment.lua:858-920 reads
-- coverage, altitudeKm, windDirection, cirrusCoverage; every change needs postApply(), as for
-- LevelInfo). altitudeKm is above the world's z = 0, which in our frame is ~ sea level, so a fixed
-- altitude puts clouds under the player on high ground (the default 0.8 km was below Mount
-- Whitney): the layer follows the ground under the player, CLOUD_ABOVE_KM above it. A rebase moves
-- the world by kilometres; the weather map is shifted by the same amount so the clouds stay put
-- (CloudLayer.setWeatherOffsetKm; CLOUD_SHIFT_SIGN checked in-game). The layer is a level object
-- (tools/make_level_assets.py) so core_environment, the pause menu and weather presets find it; it
-- is created here only for a level file without one. Real weather (beamngEarth_weather) drives
-- coverage, cirrus and drift, and sets the base height above the ground (setCloudAboveKm).

M.CLOUDS_ENABLED = true
M.CLOUD_NAME = 'bngEarthClouds'
M.CLOUD_TEXTURE = 'art/skies/clouds/clouds_normal_displacement.png' -- shared art (west_coast_usa uses it)
M.CLOUD_COVERAGE, M.CLOUD_CIRRUS = 0.5, 0.3
M.CLOUD_ABOVE_KM = 1.8      -- cloud base above the ground under the player
M.CLOUD_MIN_KM = 1.5        -- never lower than this above the world's z = 0
M.CLOUD_RETUNE_KM = 0.4     -- re-set the altitude only when it is off by more than this
M.CLOUD_CHECK_S = 5
M.JUMP_M = 3000             -- a player move this large in one frame is a rebase/teleport
M.CLOUD_SHIFT_SIGN = 1

local cloudAboveKm = nil -- real weather's base above the ground (nil = CLOUD_ABOVE_KM)
local cloudFollow = true -- false while someone else sets the altitude (a manual override)

-- Pure: the cloud altitude (km above z = 0) for ground height groundZ (m), or nil without ground;
-- aboveKm: the base above the ground (default CLOUD_ABOVE_KM).
function M.cloudAltitudeKm(groundZ, aboveKm)
  if not groundZ then return nil end
  return math.max(M.CLOUD_MIN_KM, groundZ / 1000 + (aboveKm or M.CLOUD_ABOVE_KM))
end

-- Pure: the weather offset (km) after the world moved so that the player went from (ox, oy) to
-- (nx, ny) without really moving (a rebase): the map shifts by the same displacement.
function M.shiftedOffsetKm(off, ox, oy, nx, ny)
  local s = M.CLOUD_SHIFT_SIGN / 1000
  return off.x + s * (nx - ox), off.y + s * (ny - oy), off.z or 0
end

local cloudTimer, lastPos = M.CLOUD_CHECK_S, nil

local function cloudObj()
  local names = scenetree.findClassObjects('CloudLayer')
  local o = names and names[1] and scenetree.findObject(names[1])
  if o or not M.CLOUDS_ENABLED then return o end
  o = createObject('CloudLayer')
  o:setField('texture', 0, M.CLOUD_TEXTURE)
  o:setField('exposure', 0, '1.45')
  o:setField('windSpeed', 0, '0.1')
  o:setField('baseColor', 0, '0.99 0.99 0.99 0.9')
  o.canSave = false
  o:registerObject(M.CLOUD_NAME)
  scenetree.MissionGroup:addObject(o)
  o.coverage, o.cirrusCoverage = M.CLOUD_COVERAGE, M.CLOUD_CIRRUS
  o:postApply()
  -- core_environment caches 'no CloudLayer' per class (environment.lua getObject): reset it
  local env = rawget(_G, 'core_environment')
  if env and env.onEditorDeactivated then pcall(env.onEditorDeactivated) end
  return o
end

local function groundUnder(p)
  local d = castRayStatic(vec3(p.x, p.y, p.z + 50), vec3(0, 0, -1), 5000)
  if d and d < 5000 then return p.z + 50 - d end
end

local function updateClouds(dt)
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if not veh or not scenetree.MissionGroup then return end
  local p = veh:getPosition()
  -- rebase: the player jumps by kilometres in one frame without moving; shift the weather map too
  if lastPos and ((p.x - lastPos.x) ^ 2 + (p.y - lastPos.y) ^ 2) > M.JUMP_M * M.JUMP_M
    and rawget(_G, 'CloudLayer') and CloudLayer.getWeatherOffsetKm and CloudLayer.setWeatherOffsetKm then
    pcall(function()
      local off = CloudLayer.getWeatherOffsetKm()
      CloudLayer.setWeatherOffsetKm(Point3F(M.shiftedOffsetKm(off, lastPos.x, lastPos.y, p.x, p.y)))
    end)
  end
  lastPos = { x = p.x, y = p.y }
  cloudTimer = cloudTimer + (dt or 0)
  if cloudTimer < M.CLOUD_CHECK_S then return end
  cloudTimer = 0
  local o = cloudObj()
  if not o then return end
  if not cloudFollow then return end
  local alt = M.cloudAltitudeKm(groundUnder(p), cloudAboveKm)
  if alt and math.abs((tonumber(o.altitudeKm) or 0) - alt) > M.CLOUD_RETUNE_KM then
    o.altitudeKm = alt
    o:postApply()
  end
end

function M.setClouds(on)
  M.CLOUDS_ENABLED = on and true or false
  local o = scenetree.findObject(M.CLOUD_NAME)
  if not M.CLOUDS_ENABLED and o then o:delete() end
  cloudTimer = M.CLOUD_CHECK_S
  return 'clouds ' .. (M.CLOUDS_ENABLED and 'on' or 'off')
end

-- Real weather: the cloud base above the ground (km; nil = CLOUD_ABOVE_KM), and whether the
-- altitude follows the ground at all (off while the user or another mod sets it).
function M.setCloudAboveKm(km) cloudAboveKm = tonumber(km) end
function M.setCloudFollow(on)
  cloudFollow = on and true or false
  if cloudFollow then cloudTimer = M.CLOUD_CHECK_S end
end

------------------------------------------------------------------------------------------
-- Someone else changes the time (the pause menu, another mod): manual.

local function notify(text)
  log('W', 'beamngEarth', 'sky: ' .. text)
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then pcall(gh.trigger, 'toastrMsg', { type = 'warning', title = 'BeamNG.Earth time', msg = text }) end
end

-- The big map sets its own time and puts it back as it closes (see weather.lua).
local function bigMapBusy()
  if clock < bigMapUntil then return true end
  local bm = rawget(_G, 'freeroam_bigMapMode')
  if not bm then return false end
  local ok, on = pcall(function()
    return (bm.bigMapActive and bm.bigMapActive()) or (bm.isTransitionActive and bm.isTransitionActive())
  end)
  return ok and on and true or false
end

-- Saved as the setting (the settings page shows it; settings.set applies it again: a no-op now).
local function switchToManual()
  local p = pending
  M.setMode('manual')
  switched = p
  local st = rawget(_G, 'beamngEarth_settings')
  if st and st.set then
    local ok, err = pcall(st.set, 'skyMode', 'manual')
    if not ok then log('W', 'beamngEarth', 'sky: saving the mode failed: ' .. tostring(err)) end
  end
  notify('Time set to Manual because it was changed. Choose Real time in the BeamNG.Earth settings to go back.')
end

-- core_environment's hook (setTimeOfDay passes the whole TimeOfDay table, setState whatever its
-- caller gave), fired before the change is applied.
function M.onEnvironmentChanged(changes)
  if mode ~= 'real' or pending or serverClock or type(changes) ~= 'table' then return end
  local any = false
  for k in pairs(TIME_KEYS) do if changes[k] ~= nil then any = true; break end end
  if not any then return end -- most hooks are weather (ours at 4 Hz): nothing to read
  if not inOurLevel or clock < graceUntil or mission or bigMapBusy() then return end
  local tod = getTod()
  if not tod then return end
  local real = M.todFields(os.time(), tonumber(tod.latitude) or 0, tonumber(tod.longitude) or 0, realTz)
  local keys = M.timeChanges(changes, tod, real)
  if #keys > 0 then pending = { at = clock, keys = keys } end
end

function M.onAnyMissionChanged(state)
  if state == 'started' then
    mission = true
    pending = nil -- the mission's own setup, just before 'started'
  elseif state == 'stopped' then
    mission = false
    graceUntil = math.max(graceUntil, clock + M.MISSION_RESUME_S) -- its restore ran just before
    timer = RESYNC_S
  end
end

function M.onBeforeBigMapActivated() bigMapUntil = clock + M.BIGMAP_TAIL_S end
M.onBigmapStartTransition = M.onBeforeBigMapActivated
function M.onDeactivateBigMapCallback()
  bigMapUntil = clock + M.BIGMAP_TAIL_S
  timer = RESYNC_S -- its time back is the time it opened: re-sync once it is done
end

local function onLevel()
  clock, graceUntil, bigMapUntil, mission, pending = 0, M.GRACE_S, 0, false, nil
  inOurLevel = getCurrentLevelIdentifier and getCurrentLevelIdentifier() == M.LEVEL_ID or false
end
M.onClientStartMission = onLevel
function M.onClientEndMission() inOurLevel, pending = false, nil end
-- A Lua reload mid-level: settings.lua loads (and applies) before this extension, and no level
-- start follows to apply again, so the saved mode is read here.
function M.onExtensionLoaded()
  local st = rawget(_G, 'beamngEarth_settings')
  local saved = st and st.get and st.get('skyMode')
  if saved then M.setMode(saved) end
  if scenetree.MissionGroup and getCurrentLevelIdentifier and getCurrentLevelIdentifier() then onLevel() end
end

function M.onUpdate(dtReal)
  clock = clock + (dtReal or 0)
  -- other levels keep their own time and clouds, as weather.lua does (GitHub issue #1, 2026-10-17: on Hirochi Raceway
  -- the time jumped to the real clock every 30 s and the paused clock ran)
  if not inOurLevel then return end
  local ok, err = pcall(updateClouds, dtReal)
  if not ok then log('W', 'beamngEarth', 'clouds: ' .. tostring(err)) end
  if pending and clock >= pending.at + M.MISSION_SETUP_S then switchToManual() end
  if mode ~= 'real' or pending or mission or bigMapBusy() then return end
  timer = timer + (dtReal or 0)
  if timer < RESYNC_S then return end
  timer = 0
  M.sync()
end

function M.onClientPostStartMission() timer, cloudTimer, lastPos = RESYNC_S, M.CLOUD_CHECK_S, nil end

return M
