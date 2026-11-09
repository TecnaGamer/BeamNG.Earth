-- Offline tests for beamngEarth/sky.lua: the pure parts, then the switch to manual when someone else
-- changes the time (against stubs.lua). Run: cd mod/tests && luajit test_sky.lua
package.path = '../beamng_earth/lua/ge/extensions/?.lua;' .. package.path
local sky = require('beamngEarth/sky')

local pass, fail = 0, 0
local function check(cond, what)
  if cond then pass = pass + 1 else fail = fail + 1; print('FAIL: ' .. what) end
end
local function near(a, b, tol) return math.abs(a - b) <= tol end

-- 2026-10-10 03:20:09 UTC (the moment this was written)
local epoch = 1791602409

-- Grand Canyon (US rule, DST active in October): UTC-7 standard + 1 = -6
local f = sky.todFields(epoch, 36.1047, -111.8257)
check(f.utcOffset == -6, 'Grand Canyon offset -6, got ' .. f.utcOffset)
check(f.year == 2026 and f.month == 10 and f.day == 9, 'Grand Canyon local date is Oct 9')
check(near(f.localHours, 21 + 20 / 60 + 9 / 3600, 1e-6), 'local hours 21:20:09')
check(near(f.time, (f.localHours / 24 - 0.5) % 1, 1e-12), 'time is noon-zero fraction')

-- Instant is exact regardless of offset: UTC hours = local - offset
local utcHours = (f.localHours - f.utcOffset) % 24
check(near(utcHours, 3 + 20 / 60 + 9 / 3600, 1e-6), 'reconstructed UTC 03:20:09')

-- Tropics: no DST. Singapore lon 103.8 -> std +7 (nearest meridian), no DST
local s = sky.todFields(epoch, 1.35, 103.82)
check(s.utcOffset == 7, 'Singapore approx offset 7, got ' .. s.utcOffset)

-- The real offset (Open-Meteo timezone=auto, via the weather service) wins near its point:
-- Singapore is +8, not the meridian guess +7; 300 km away the guess is back
local real = { hours = 8, lat = 1.35, lon = 103.82 }
local sr = sky.todFields(epoch, 1.30, 103.85, real)
check(sr.utcOffset == 8, 'Singapore real offset 8, got ' .. sr.utcOffset)
check(near((sr.localHours - sr.utcOffset) % 24, 3 + 20 / 60 + 9 / 3600, 1e-6), 'real offset: same instant')
check(sky.todFields(epoch, 1.35, 106.6, real).utcOffset == 7, 'beyond REAL_TZ_KM: the guess')
-- half-hour zones (Delhi +5.5)
check(sky.todFields(epoch, 28.61, 77.21, { hours = 5.5, lat = 28.6, lon = 77.2 }).utcOffset == 5.5, 'Delhi +5.5')

-- Southern temperate (AU rule): Sydney in October -> DST active: std +10 + 1
local a = sky.todFields(epoch, -33.87, 151.21)
check(a.utcOffset == 11, 'Sydney offset 11, got ' .. a.utcOffset)
check(a.day == 10, 'Sydney local date Oct 10')

-- EU rule after last Sunday of October (2026-10-25): Berlin offset +1
local b = sky.todFields(epoch + 20 * 86400, 52.52, 13.40)
check(b.utcOffset == 1, 'Berlin late Oct offset 1, got ' .. b.utcOffset)
local b2 = sky.todFields(epoch, 52.52, 13.40)
check(b2.utcOffset == 2, 'Berlin early Oct offset 2, got ' .. b2.utcOffset)

-- Antimeridian / date line: lon 179.9 -> +12, lon -179.9 -> -12, dates differ by a day
local e = sky.todFields(epoch, 0, 179.9)
local w = sky.todFields(epoch, 0, -179.9)
check(e.utcOffset == 12 and w.utcOffset == -12, 'date line offsets')
check(e.day ~= w.day, 'date line: different local dates')

-- Poles: offsets stay sane
local np = sky.todFields(epoch, 89.9, 0)
check(np.utcOffset == 0 or np.utcOffset == 1, 'north pole offset sane')

-- time stays in [0,1)
for i = 0, 47 do
  local t = sky.todFields(epoch + i * 1800, 36.1, -111.8).time
  check(t >= 0 and t < 1, 'time in [0,1) at step ' .. i)
end

-- clouds: the layer follows the ground; a rebase shifts the weather map by the world's move
check(near(sky.cloudAltitudeKm(4417), 4.417 + sky.CLOUD_ABOVE_KM, 1e-9), 'Whitney: clouds above the summit')
check(near(sky.cloudAltitudeKm(5), 0.005 + sky.CLOUD_ABOVE_KM, 1e-9), 'sea level: CLOUD_ABOVE_KM up')
check(sky.cloudAltitudeKm(-1000) == sky.CLOUD_MIN_KM, 'far below sea level: the minimum altitude')
check(sky.cloudAltitudeKm(nil) == nil, 'no ground: no change')
check(near(sky.cloudAltitudeKm(33, 2.967), 3.0, 1e-9), 'real weather: the base above the ground (Baltimore mid deck)')
check(sky.cloudAltitudeKm(0, 0.3) == sky.CLOUD_MIN_KM, 'a low base still keeps the minimum altitude')
local ox, oy, oz = sky.shiftedOffsetKm({ x = 1, y = 2, z = 0 }, 15000, 0, 0, 0)
check(near(ox, 1 - 15 * sky.CLOUD_SHIFT_SIGN, 1e-9) and oy == 2 and oz == 0, 'rebase shifts the weather offset by the move (km)')

------------------------------------------------------------------------------------------
-- what is a time change: off the clock now and off the real clock, a date, play off, another day
-- length or UTC offset

local cur = { time = 0.1, year = 2026, month = 10, day = 10, utcOffset = -4 }
local realNow = { time = 0.105, year = 2026, month = 10, day = 10, utcOffset = -4 }
local function tk(changes, c, r) return table.concat(sky.timeChanges(changes, c or cur, r == nil and realNow or r or nil), ',') end
check(tk({ time = 0.1015 }) == '', 'time within TIME_EPS of the clock: no change')
check(tk({ time = 0.105 }) == '', 'time on the real clock (it moved on since the TimeOfDay was set): no change')
check(tk({ time = 0.105 }, nil, false) == 'time', 'without the real clock: a change')
check(tk({ time = 0.2 }) == 'time', 'another time: a change')
check(tk({ time = 0.0005 }, { time = 0.9995 }) == '', 'time wraps at midnight')
check(tk({ play = true, dayLength = 86400 }) == '' and tk({ play = false }) == 'play' and tk({ dayLength = 1800 }) == 'dayLength',
  'play off, another day length')
check(tk({ year = 2026, month = 10, day = 10 }) == '' and tk({ day = 11 }) == 'day' and tk({ year = 2025, month = 3 }) == 'month,year',
  'the date')
check(tk({ utcOffset = -4 }) == '' and tk({ utcOffset = -5 }) == 'utcOffset', 'the UTC offset')
check(tk({ latitude = 1, longitude = 2, celestialProfile = 'x', cloudCover = 2 }) == '', 'other keys: no change')

------------------------------------------------------------------------------------------
-- runtime (stubs.lua): our level, a TimeOfDay, the real clock under the test's control

local S = dofile('stubs.lua')
local now = epoch + 15 * 3600 -- 2026-10-10 18:20 UTC, 14:20 in Baltimore
os.time = function() return now end
local tod = S.newObject('TimeOfDay'); tod:registerObject('tod')
tod.latitude, tod.longitude = 39.29, -76.61
local toasts, saved, envCalls = {}, {}, 0
guihooks = { trigger = function(name, data) toasts[#toasts + 1] = data end }
beamngEarth_settings = { set = function(k, v)  -- as settings.lua: applied only when it differs
  saved[#saved + 1] = k .. '=' .. tostring(v)
  if k == 'skyMode' and sky.getMode() ~= v then sky.setMode(v) end
  return { ok = true, value = v }
end }
-- core_environment.setTimeOfDay: the hook first, then the fields (environment.lua setTimeOfDay)
core_environment = { setTimeOfDay = function(t)
  envCalls = envCalls + 1
  local copy = {}
  for k, v in pairs(t) do copy[k] = v end
  sky.onEnvironmentChanged(copy)
  for k, v in pairs(t) do tod[k] = v end
end }
local function run(seconds, dt)
  dt = dt or 0.1
  for _ = 1, math.floor(seconds / dt + 0.5) do sky.onUpdate(dt) end
end
local function realTime() return sky.todFields(now, tod.latitude, tod.longitude).time end

sky.onClientStartMission()
check(sky.getMode() == 'real', 'real by default')
run(0.1)
check(tod.year == 2026 and tod.month == 10 and tod.day == 10 and tod.play == true and tod.dayLength == sky.REAL_DAY_S
  and near(tod.time, realTime(), 1e-9), 'synced to the real clock')
-- level start: core_environment's setState(init_env), freeroam's start time: grace
core_environment.setTimeOfDay({ time = 0.25, play = false })
run(sky.MISSION_SETUP_S + 1)
check(sky.getMode() == 'real' and #toasts == 0, 'grace: no switch')
run(30) -- past the grace period and the next re-sync
check(near(tod.time, realTime(), 1e-9) and tod.play == true, 'the re-sync puts the real clock back')
-- our own re-syncs write the TimeOfDay directly: no hook, no switch
envCalls = 0
now = now + 90
run(2 * 30 + 1)
check(envCalls == 0 and sky.getMode() == 'real' and #toasts == 0 and near(tod.time, realTime(), 1e-9),
  'our re-syncs: no core_environment call, still real')
-- a mod (photo mode, the big map's pattern) writes back what it read, a few minutes later on the
-- real clock: no change
local before = { time = tod.time, year = tod.year, month = tod.month, day = tod.day, play = true,
  dayLength = tod.dayLength, utcOffset = tod.utcOffset, latitude = tod.latitude, longitude = tod.longitude }
now = now + 600
core_environment.setTimeOfDay(before)                         -- stale by 10 min: the TimeOfDay itself
core_environment.setTimeOfDay({ time = realTime() })          -- the real clock now
core_environment.setTimeOfDay({ time = tod.time + 0.001 })    -- within TIME_EPS
sky.onEnvironmentChanged({ cloudCover = 1.5, groundWind = { x = 1, y = 0 } }) -- weather only
run(sky.MISSION_SETUP_S + 0.5)
check(sky.getMode() == 'real' and #toasts == 0 and #saved == 0, 'repeated values, real clock progress: no switch')

-- missions: their setup (just before 'started') and their own time are no change; the re-sync waits
-- and runs once they stopped (their restore ran just before)
core_environment.setTimeOfDay({ time = 0.4 })  -- the mission's setup
sky.onAnyMissionChanged('started')
core_environment.setTimeOfDay({ time = 0.45, dayLength = 1800 })
run(31)
check(tod.time == 0.45 and sky.getMode() == 'real', 'mission: its time kept, no switch')
core_environment.setTimeOfDay(before)            -- its restore
sky.onAnyMissionChanged('stopped')
run(sky.MISSION_RESUME_S + 1)
check(sky.getMode() == 'real' and #toasts == 0 and near(tod.time, realTime(), 1e-9) and tod.dayLength == sky.REAL_DAY_S,
  'mission stopped: real again at once')

-- the big map: its own time, put back as it closes
local bigMap = false
freeroam_bigMapMode = { bigMapActive = function() return bigMap end, isTransitionActive = function() return false end }
sky.onBeforeBigMapActivated()
bigMap = true
core_environment.setTimeOfDay({ time = 0.0 })
run(31)
core_environment.setTimeOfDay({ time = before.time })
bigMap = false
sky.onDeactivateBigMapCallback()
run(sky.BIGMAP_TAIL_S + 1)
check(sky.getMode() == 'real' and #toasts == 0 and near(tod.time, realTime(), 1e-9), 'big map: no switch, re-synced after')
freeroam_bigMapMode = nil

-- the pause menu sets another time: the re-sync waits, after MISSION_SETUP_S manual, saved, one toast
local mine = (realTime() + 0.25) % 1
core_environment.setTimeOfDay({ time = mine })
run(sky.MISSION_SETUP_S - 0.5)
check(sky.getMode() == 'real' and tod.time == mine, 'a change: not switched yet, nothing written')
run(1)
check(sky.getMode() == 'manual' and #saved == 1 and saved[1] == 'skyMode=manual', 'a change: manual, saved')
check(#toasts == 1 and toasts[1].title == 'BeamNG.Earth time' and toasts[1].msg:find('Manual', 1, true), 'one toast')
check(sky.status().switched and sky.status().switched.keys[1] == 'time', 'status: what changed')
now = now + 120
run(2 * 30 + 1)
core_environment.setTimeOfDay({ play = false })
run(sky.MISSION_SETUP_S + 1)
check(tod.time == mine and tod.play == false and #toasts == 1 and #saved == 1, 'manual: hands off, no more toasts')
-- Real again (the settings page): re-synced at once
sky.setMode('real')
run(0.1)
check(near(tod.time, realTime(), 1e-9) and tod.play == true and sky.getMode() == 'real', 'real again: the real clock')
-- play off in the menu (setTimeOfDay({ play = false })): manual too
core_environment.setTimeOfDay({ play = false })
run(sky.MISSION_SETUP_S + 0.5)
check(sky.getMode() == 'manual' and #toasts == 2, 'play off: manual')
sky.setMode('real')
run(0.1)

-- a Lua reload mid-level: settings.lua loads (and applies) before this extension, so the saved mode
-- is read back here
beamngEarth_settings.get = function(k) return k == 'skyMode' and 'manual' or nil end
sky.onExtensionLoaded()
check(sky.getMode() == 'manual', 'reload: the saved Manual comes back')
local t0 = tod.time
now = now + 600
run(31)
check(tod.time == t0, 'reload: manual, not re-synced')
beamngEarth_settings.get = nil
sky.setMode('real')
sky.onExtensionLoaded()
check(sky.getMode() == 'real', 'reload without settings.get: the mode is kept')
run(0.1)

-- other levels: never switched
getCurrentLevelIdentifier = function() return 'west_coast_usa' end
sky.onClientEndMission()
sky.onClientStartMission()
run(sky.GRACE_S + 1)
core_environment.setTimeOfDay({ time = (realTime() + 0.3) % 1 })
run(sky.MISSION_SETUP_S + 1)
check(sky.getMode() == 'real' and #toasts == 2, 'other levels: no switch')
-- ... and their time left alone: a time set there (and a paused clock) stays past the resync interval (issue #1)
tod.time, tod.play = 0.5, false
run(sky.RESYNC_S and sky.RESYNC_S + 5 or 40)
check(tod.time == 0.5 and tod.play == false, 'other levels: their time is not synced to the real clock')

-- a BeamMP server's clock (G.TIME): a fixed or running local time from its start, on the real date
check(math.abs(sky.serverHours({ hours = 20.5, speed = 0, epoch = 1000 }, 5000) - 20.5) < 1e-9, 'a fixed server time stays')
check(math.abs(sky.serverHours({ hours = 23, speed = 60, epoch = 1000 }, 1000 + 120) - 1) < 1e-9, 'x60: two minutes are two hours (wraps)')
check(sky.setServerClock({ hours = 21, speed = 0 }) ~= nil and sky.serverClock().hours == 21, 'server clock set')
check(sky.setServerClock(nil) == nil and sky.serverClock() == nil, 'cleared: the real clock again')

-- the sun's elevation (GitHub #874: the map's badge shows the moon at night). Paris on the June solstice: about
-- 64.5 degrees up at 14:00 CEST (its solar noon ~13:50), well under the horizon at local midnight; Sydney's winter
-- noon about 32.7 degrees; the Grand Canyon at 21:20 MDT in October is night
check(near(sky.sunElevationDeg(2026, 6, 21, 14, 2, 48.8566, 2.3522), 64.5, 1.0), 'Paris solstice afternoon ~64.5 deg')
check(sky.sunElevationDeg(2026, 6, 21, 0, 2, 48.8566, 2.3522) < -10, 'Paris midnight: the sun well down')
check(near(sky.sunElevationDeg(2026, 6, 21, 12, 10, -33.8688, 151.2093), 32.7, 1.0), 'Sydney winter noon ~32.7 deg')
check(sky.sunElevationDeg(2026, 10, 9, 21 + 20 / 60, -6, 36.1047, -111.8257) < sky.NIGHT_ELEV_DEG, 'Grand Canyon 21:20: night')
check(sky.sunElevationDeg(2026, 10, 9, 12, -6, 36.1047, -111.8257) > 30, 'Grand Canyon noon: day')

print(string.format('test_sky: %d passed, %d failed', pass, fail))
os.exit(fail == 0 and 0 or 1)
