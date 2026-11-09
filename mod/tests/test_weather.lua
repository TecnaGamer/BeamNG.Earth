-- luajit mod/tests/test_weather.lua
-- beamngEarth/weather.lua: the pure conversions (calibration curve, fog, wind, rain, gravity, gusts,
-- the targets of the two recorded Open-Meteo fixtures as server/weather.py derives them), then the
-- runtime against a stubbed core_environment: grace period, smooth approach, writes only on change,
-- the switch to manual when someone else changes the weather (and what is no change: repeated values,
-- our own writes, grace, missions, the big map, gravity), BWM_Weather, gravity, rain objects and the
-- temperature curve.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local W = require('beamngEarth/weather')
local check, near = T.check, T.near

------------------------------------------------------------------------------------------
-- pure

-- the calibration curve: its own rows, monotone, clamped at both ends
for _, row in ipairs(W.COVERAGE_CURVE) do near(W.coverageFor(row[1]), row[2], 1e-12, 'curve row ' .. row[1]) end
local prev = -1
for i = 0, 100 do
  local c = W.coverageFor(i / 100)
  check(c >= prev, 'coverage monotone at ' .. i)
  prev = c
end
check(W.coverageFor(-1) == 0 and W.coverageFor(2) == W.COVERAGE_CURVE[#W.COVERAGE_CURVE][2], 'curve clamps')
near(W.coverageFor(0.13), (0.79 + 0.90) / 2, 1e-9, 'linear between probe rows')
near(W.coverageFor(0.5, { { 0, 0 }, { 1, 3 } }), 1.5, 1e-12, 'a curve can be passed in')

-- cirrus: none without high cloud; density in its range
local cc, cd = W.cirrusFor(0)
check(cc == 0 and cd == 0, 'no high cloud: no cirrus')
cc, cd = W.cirrusFor(100)
check(cc == 1 and math.abs(cd - W.CIRRUS_DENSITY_MAX) < 1e-12, 'full high cloud')
cc, cd = W.cirrusFor(30)
check(math.abs(cc - 0.3) < 1e-12 and cd > W.CIRRUS_DENSITY_MIN and cd < W.CIRRUS_DENSITY_MAX, 'partial cirrus')

-- fog: Koschmieder, and the height compensation round trip (density at z = 0 thins to sigma at groundZ)
near(W.fogSigma(3912), 1e-3, 1e-15, 'sigma = 3.912 / V')
near(W.fogSigma(nil), W.FOG_K / W.FOG_VIS_DEFAULT, 1e-15, 'no visibility: the default')
near(W.fogSigma(1), W.FOG_K / W.FOG_VIS_MIN, 1e-15, 'visibility clamped low')
local sigma, zg, H = W.fogSigma(36300), 4000, 1000
local d = W.fogDensityFor(sigma, zg, H)
near(d * math.exp(-zg / H), sigma, 1e-15, 'compensated density gives sigma at the ground')
near(W.fogDensityFor(sigma, nil, H), sigma, 1e-15, 'no ground: no compensation')
check(W.fogDensityFor(1e-2, 8000, 500) == W.FOG_DENSITY_MAX, 'density capped at the menu maximum')

-- wind: a west wind (from 270) blows towards +x (east); a north wind towards -y
local x, y = W.windVec(10, 270)
near(x, 10, 1e-9, 'west wind x'); near(y, 0, 1e-9, 'west wind y')
x, y = W.windVec(5, 0)
near(x, 0, 1e-9, 'north wind x'); near(y, -5, 1e-9, 'north wind y')
x, y = W.cloudDir(90)
near(x, -1, 1e-9, 'east wind: clouds drift west'); near(math.sqrt(x * x + y * y), 1, 1e-12, 'unit vector')

-- rain/snow fraction
check(W.precipPct(0, 8) == 0 and W.precipPct(0.04, 8) == 0, 'dry')
check(W.precipPct(0.051, 8) == W.PRECIP_MIN, 'drizzle: at least PRECIP_MIN')
near(W.precipPct(2, 8), 0.5, 1e-12, 'sqrt law')
check(W.precipPct(50, 8) == 1, 'capped at the full pool')

-- normal gravity (Somigliana): equator, pole, 45 deg, and the free-air gradient
near(W.gravityAt(0, 0), 9.7803253359, 1e-9, 'equator')
near(W.gravityAt(90, 0), 9.8321849379, 1e-7, 'pole')
near(W.gravityAt(-90, 0), W.gravityAt(90, 0), 1e-12, 'symmetric')
near(W.gravityAt(45, 0), 9.8061977694, 1e-9, '45 deg (WGS84 Somigliana)')
near(W.gravityAt(36.58, 4400), W.gravityAt(36.58, 0) - 3.086e-6 * 4400, 1e-12, 'free-air gradient')

-- gusts: bounded, and decay without noise
local u = 0.8
for _ = 1, 40 do u = W.gustStep(u, 0.5, W.GUST_TAU_S, 0) end
check(math.abs(u) < 0.01, 'gust decays to 0 without noise, got ' .. u)
-- gust speed: mean at 0, the gust at +1, a lull down to (1 - GUST_LULL) of the mean at -1
near(W.gustSpeed(8, 16, 0), 8, 1e-12, 'gust 0: the mean')
near(W.gustSpeed(8, 16, 1), 16, 1e-12, 'gust +1: the gust speed')
near(W.gustSpeed(8, 16, -1), 8 * (1 - W.GUST_LULL), 1e-12, 'gust -1: the lull')
near(W.gustSpeed(8, 5, 1), 8, 1e-12, 'a gust below the mean is ignored')
check(W.gustSpeed(8, 16, -3) == W.gustSpeed(8, 16, -1), 'the lull clamps')
-- direction spread: small for steady wind, larger when gusty, capped, none when calm
near(W.dirStdDeg(10, 10), W.DIR_STD_MIN_DEG, 1e-12, 'steady wind: the minimum spread')
check(W.dirStdDeg(8.6, 16.4) > 15 and W.dirStdDeg(8.6, 16.4) <= W.DIR_STD_MAX_DEG, 'gusty wind: wider spread ' .. W.dirStdDeg(8.6, 16.4))
check(W.dirStdDeg(2, 20) == W.DIR_STD_MAX_DEG, 'spread capped')
check(W.dirStdDeg(0.2, 3) == 0, 'calm: no swings')
u = 0
for i = 1, 2000 do
  u = W.gustStep(u, 1, W.GUST_TAU_S, (i % 2 == 0) and 1.7 or -1.6)
  if u < -1 or u > 1 then break end
end
check(u >= -1 and u <= 1, 'gust bounded')

-- step: 95 % of a jump after LERP_S, exact for any number of steps
local cur, tgt = { a = 0, b = 10 }, { a = 1, b = 10 }
for _ = 1, 600 do W.step(cur, tgt, W.LERP_S / 600, W.LERP_S) end
near(cur.a, 1 - math.exp(-3), 1e-9, 'step: 95 % after LERP_S')
check(cur.b == 10, 'step: unchanged field stays')
local c1 = W.step({ a = 0 }, { a = 1 }, W.LERP_S, W.LERP_S)
near(c1.a, cur.a, 1e-9, 'step: frame-rate independent')

-- describe / compass
check(W.describe(3) == 'overcast' and W.describe(45) == 'fog' and W.describe(63) == 'rain'
  and W.describe(73) == 'snow' and W.describe(95) == 'thunderstorm' and W.describe(81) == 'showers', 'describe')
check(W.compass(232) == 'SW' and W.compass(98) == 'E' and W.compass(359) == 'N', 'compass')

-- what is a change: the incoming value against the engine's (getters), per key
local stubEnv = { getCloudCover = function() return 1 end, getFogDensity = function() return 0.002, {} end,
  getGroundWind = function() return vec3(1, 2, 0) end, getCloudWindDirection = function() return vec3(0, 1, 0) end }
local function keys(t) return table.concat(t, ',') end
check(keys(W.changedKeys({ cloudCover = 1.005, fogDensity = 2, time = 0.3, cloudWeatherSeed = 4 }, stubEnv)) == '',
  'repeated values (fog in the menu\'s x1000 scale) and non-weather keys: no change')
check(keys(W.changedKeys({ cloudCover = 1.5, fogDensity = 25 }, stubEnv)) == 'cloudCover,fogDensity', 'changed values')
check(keys(W.changedKeys({ fogDensity = 0.002 }, stubEnv)) == '' and keys(W.changedKeys({ fogDensity = 0.004 }, stubEnv)) == 'fogDensity',
  'fog in the engine\'s scale (setFogDensity)')
check(keys(W.changedKeys({ groundWind = { x = 1, y = 2.1, z = 0 }, cloudWindDirection = vec3(0, 1, 0) }, stubEnv)) == '',
  'vectors within the tolerance (the menu sends tables)')
check(keys(W.changedKeys({ groundWind = vec3(3, 2, 0), cloudWindDirection = { x = 1, y = 0 } }, stubEnv)) == 'cloudWindDirection,groundWind',
  'vectors that changed')
check(keys(W.changedKeys({ groundWind = vec3(1, 2, 0) }, stubEnv, { groundWind = { x = 5, y = 0 } })) == 'groundWind',
  'a reference value wins over the engine\'s')
check(keys(W.changedKeys({ numOfDrops = 500, cloudAltitudeKm = 2 }, stubEnv)) == 'cloudAltitudeKm,numOfDrops', 'no getter: a change')
check(not W.valueDiffers('cloudCover', 'x', 1, 0.01) and W.valueDiffers('cloudCover', 1, nil, 0.01), 'unreadable values')
-- the menu's sliders (0.39 EnvWeather.vue) send their value rounded to the step when the number field
-- loses focus, unedited: within half a step is no change, one step is
local function deg(a) return { x = math.sin(math.rad(a)), y = math.cos(math.rad(a)), z = 0 } end
local menuEnv = { getCloudCover = function() return 0.913 end, getWindSpeed = function() return 0.112 end,
  getCloudWindDirection = function() local d = deg(32.6); return vec3(d.x, d.y, 0) end,
  getFogDensity = function() return 6.74e-5, {} end }
check(keys(W.changedKeys({ cloudCover = 0.91, windSpeed = 0.1, cloudWindDirection = deg(35), fogDensity = 0.065 }, menuEnv)) == '',
  'the menu\'s rounding to its step (cover 0.01, cloud speed 0.1, direction 5 deg, fog 0.005): no change')
check(keys(W.changedKeys({ cloudCover = 0.92, windSpeed = 0.2, cloudWindDirection = deg(40), fogDensity = 0.075 }, menuEnv))
  == 'cloudCover,cloudWindDirection,fogDensity,windSpeed', 'one step of the menu\'s sliders: a change')
-- the BWM_Weather rule (named in the toast)
check(not W.bwmFighting(nil), 'no BWM')
check(not W.bwmFighting({ currentPreset = 'clear', targetPreset = 'clear', automaticWeather = false }), 'BWM on clear')
check(W.bwmFighting({ currentPreset = 'clear', targetPreset = 'clear', automaticWeather = true }), 'BWM automatic')
check(W.bwmFighting({ currentPreset = 'cloudy', targetPreset = 'cloudy' }), 'BWM preset')
check(W.bwmFighting({ currentPreset = 'clear', targetPreset = 'lightRain' }), 'BWM transitioning to rain')

-- The two recorded fixtures (server/tests/fixtures/openmeteo_*.json, 2026-10-10 ~20:45 UTC) as
-- server/weather.py derive() turns them into messages (test_weather.py checks the same messages
-- through this module under luajit).
local BALTIMORE = { type = 'weather', v = 1, t = 1791665100, lat = 39.29, lon = -76.53, code = 3, isDay = true,
  cover = { total = 100, low = 0, mid = 100, high = 100 }, baseM = 3000, baseAglM = 2967, rainMmH = 0, snowCmH = 0,
  visM = 36300, fog = false, thunder = false, wind = { ms = 3.11, fromDeg = 98, gustMs = 6.7 }, tempC = 20.5,
  dewC = 11.4, elevM = 33, slTempC = 20.71, pressureHpa = 1023.8, credit = 'Weather data by Open-Meteo.com',
  aloft = { ms = 5.62, fromDeg = 211 }, utcOffsetS = -14400, tz = 'America/New_York' }
local WHITNEY = { type = 'weather', v = 1, t = 1791665100, lat = 36.58, lon = -118.29, code = 1, isDay = true,
  cover = { total = 43, low = 0, mid = 43, high = 30 }, baseM = 6317, baseAglM = 2275, rainMmH = 0, snowCmH = 0,
  visM = 66300, fog = false, thunder = false, wind = { ms = 8.81, fromDeg = 232, gustMs = 13.2 }, tempC = 3.5,
  dewC = -14.7, elevM = 4042, slTempC = 29.77, pressureHpa = 619.4, credit = 'Weather data by Open-Meteo.com',
  aloft = { ms = 5.37, fromDeg = 215 }, utcOffsetS = -25200, tz = 'America/Los_Angeles' }

local b = W.targets(BALTIMORE)
near(b.coverage, W.COVERAGE_CURVE[#W.COVERAGE_CURVE][2], 1e-12, 'Baltimore: overcast mid deck -> the top of the curve')
near(b.cirrusCoverage, 1, 1e-12, 'Baltimore: full cirrus')
near(b.cloudAboveKm, 2.967, 1e-9, 'Baltimore: mid-deck base ~3 km above the ground')
near(b.fogSigma, 3.912 / 36300, 1e-15, 'Baltimore: sigma from 36.3 km')
check(b.fogHeight == W.FOG_HEIGHT, 'Baltimore: no fog code')
near(b.windMs, 3.11, 1e-12, 'Baltimore wind'); near(b.gustMs, 6.7, 1e-12, 'Baltimore gust')
check(b.windDirX < -0.9 and math.abs(b.windDirY) < 0.2, 'Baltimore: easterly wind blows west')
check(b.cloudDirX > 0 and b.cloudDirY > 0, 'Baltimore: SW wind aloft drifts clouds NE')
near(b.cloudWindSpeed, 5.62 * W.CLOUD_WIND_SCALE, 1e-12, 'Baltimore cloud drift from the 700 hPa wind')
check(b.rainPct == 0 and b.snowPct == 0, 'Baltimore: dry')
near(b.tempC, 20.71, 1e-12, 'Baltimore: sea-level temperature')

local wt = W.targets(WHITNEY)
-- the sun under the deck
near(W.sunDimFor(0), 1, 1e-12, 'sun: clear sky keeps the full sun')
near(W.sunDimFor(1), 1 - W.SUN_DIM_MAX, 1e-12, 'sun: overcast dims to 1 - SUN_DIM_MAX')
check(W.sunDimFor(-1) == 1 and W.sunDimFor(2) == W.sunDimFor(1), 'sun: the factor clamps')
check(W.sunDimFor(0.2) > 0.9, 'sun: a few clouds barely dim it')
near(b.sunDim, W.sunDimFor(1), 1e-12, 'Baltimore overcast: dimmed sun')
near(wt.sunDim, W.sunDimFor(0.43), 1e-12, 'Whitney: 43 % deck')
near(wt.coverage, W.coverageFor(0.43), 1e-12, 'Whitney: 43 % mid deck')
check(wt.coverage > 0.9 and wt.coverage < 1.1, 'Whitney: partly cloudy on the curve')
near(wt.cirrusCoverage, 0.3, 1e-12, 'Whitney cirrus')
near(wt.cloudAboveKm, 2.275, 1e-9, 'Whitney base above the ground')
x, y = W.windVec(8.81, 232)
near(wt.windDirX * 8.81, x, 1e-9, 'Whitney wind x'); near(wt.windDirY * 8.81, y, 1e-9, 'Whitney wind y')
check(x > 0 and y > 0, 'Whitney: SW wind blows NE')
near(wt.tempC, 29.77, 1e-12, 'Whitney: summit 3.5 C at 4042 m is 29.8 C at sea level')

-- rain, snow and fog messages
local wet = W.targets({ rainMmH = 2, snowCmH = 0, fog = true, visM = 300, cover = { low = 90, mid = 20 } })
near(wet.rainPct, 0.5, 1e-12, 'rain 2 mm/h')
check(wet.fogHeight == W.FOG_HEIGHT_FOG and math.abs(wet.fogSigma - 3.912 / 300) < 1e-12, 'fog')
local snowy = W.targets({ rainMmH = 0, snowCmH = 0.5 })
near(snowy.snowPct, 0.5, 1e-12, 'snow 0.5 cm/h')
check(snowy.cloudDirX ~= nil and snowy.tempC == 15 and snowy.windMs == 0, 'missing fields: defaults')

------------------------------------------------------------------------------------------
-- runtime (stubbed engine)

local frames = 0
local env = { cover = 0.5, cirrusCov = 0.3, cirrusDen = 0, windSpeed = 0.1, cloudDir = vec3(1, 0, 0), fog = 4e-5,
  fogH = 1000, ground = vec3(0, 0, 0), gravity = -9.81, cloudAlt = 1.5, calls = {}, hooks = 0 }
local weatherExt -- the extension (for the hook)
local function hook(changes)
  env.hooks = env.hooks + 1
  weatherExt.onEnvironmentChanged(changes)
end
local function count(n) env.calls[n] = (env.calls[n] or 0) + 1 end
core_environment = {
  getCloudCover = function() return env.cover end,
  setCloudCover = function(v) count('cover'); hook({ cloudCover = v }); env.cover = v end,
  getCloudCirrusCoverage = function() return env.cirrusCov end,
  setCloudCirrusCoverage = function(v) count('cirrusCov'); hook({ cloudCirrusCoverage = v }); env.cirrusCov = v end,
  getCloudCirrusDensity = function() return env.cirrusDen end,
  setCloudCirrusDensity = function(v) count('cirrusDen'); hook({ cloudCirrusDensity = v }); env.cirrusDen = v end,
  getWindSpeed = function() return env.windSpeed end,
  setWindSpeed = function(v) count('windSpeed'); hook({ windSpeed = v }); env.windSpeed = v end,
  getCloudWindDirection = function() return vec3(env.cloudDir.x, env.cloudDir.y, 0) end,
  setCloudWindDirection = function(x, y) count('cloudDir'); hook({ cloudWindDirection = vec3(x, y, 0) }); env.cloudDir = vec3(x, y, 0) end,
  getFogDensity = function() return env.fog, {} end,
  setFogDensity = function(v) count('fog'); hook({ fogDensity = v }); env.fog = v end,
  getFogAtmosphereHeight = function() return env.fogH end,
  setFogAtmosphereHeight = function(v) count('fogH'); env.fogH = v end, -- no hook (environment.lua)
  getGroundWind = function() return vec3(env.ground.x, env.ground.y, env.ground.z) end,
  setGroundWind = function(x, y, z) count('ground'); hook({ groundWind = vec3(x, y, z) }); env.ground = vec3(x, y, z) end,
  getGravity = function() return env.gravity end,
  setGravity = function(v) count('gravity'); hook({ gravity = v }); env.gravity = v end,
  getCloudAltitudeKm = function() return env.cloudAlt end,
}
local sky = { offsets = {}, above = nil, follow = true }
beamngEarth_sky = {
  setRealUtcOffset = function(h, lat, lon) sky.offsets[#sky.offsets + 1] = { h, lat, lon } end,
  setCloudAboveKm = function(km) sky.above = km end,
  setCloudFollow = function(on) sky.follow = on end,
}
local toasts = {}
guihooks = { trigger = function(name, data) toasts[#toasts + 1] = { name, data } end }
local camZ = 10
core_camera = { getPositionXYZ = function() return 0, 0, camZ end }
local veh = { pos = vec3(0, 0, 4000) }
function veh:getPosition() return vec3(self.pos.x, self.pos.y, self.pos.z) end
function getPlayerVehicle() return veh end
function castRayStatic(from) return from.z - 3990 end -- ground at z = 3990
local level = 'beamng_earth'
function getCurrentLevelIdentifier() return level end

-- level objects: TimeOfDay, LevelInfo (temperature curve with a table setter), the precipitation
local tod = S.newObject('TimeOfDay'); tod:registerObject('tod'); tod.latitude = 36.58
local li = S.newObject('LevelInfo'); li:registerObject('theLevelInfo')
li.curve = { { 0, 15 }, { 1, 15 } }
function li:getTemperatureCurveC() return self.curve end
function li:setTemperatureCurveC(c) self.curve = c end
function li:setEditorDirty(v) self.dirty = v end
scenetree.theLevelInfo = li
local function precip(name)
  local o = S.newObject('Precipitation'); o:registerObject(name)
  function o:setPercentage(p) self.pct = p end
  function o:postApply() self.applied = (self.applied or 0) + 1 end
  return o
end
local rain, snow = precip(W.RAIN_NAME), precip(W.SNOW_NAME)
local sun = S.newObject('ScatterSky'); sun:registerObject('sunsky')
sun:setField('brightness', 0, '1.2')
function sun:postApply() self.applied = (self.applied or 0) + 1 end
local function sunB() return tonumber(sun:getField('brightness')) end

weatherExt = W
local function run(seconds, dt)
  dt = dt or 0.1
  for _ = 1, math.floor(seconds / dt + 0.5) do frames = frames + 1; W.onUpdate(dt) end
end

W.onClientStartMission()
check(W.getMode() == 'real', 'default mode real (no BWM)')
W.onWeather(WHITNEY)
check(#sky.offsets == 1 and sky.offsets[1][1] == -7 and sky.offsets[1][2] == 36.58, 'real UTC offset to sky')

-- grace: nothing written, and env changes are not overrides
run(W.GRACE_S - 1)
check(next(env.calls) == nil, 'no writes during the grace period')
W.onEnvironmentChanged({ cloudCover = 0, fogDensity = 0 })          -- BWM's start transition
run(2)
check(env.calls.cover and env.calls.fog, 'writes after the grace period')
check(env.hooks > 0, 'our own writes fire the hook ...')
local st = W.status()
check(type(st) == 'string', 'status is a string')

-- smooth approach: coverage moves from 0.5 towards Whitney's target, 95 % after LERP_S
local want = wt.coverage
run(W.LERP_S)
check(math.abs(env.cover - want) < 0.06 * math.abs(want - 0.5) + 0.006, 'coverage approached the target: ' .. env.cover)
near(env.cirrusCov, wt.cirrusCoverage, 0.03, 'cirrus coverage')
check(env.cirrusDen > W.CIRRUS_DENSITY_MIN * 0.9, 'cirrus density set')
-- fog: compensated for the ground at 3990 m
local fogWant = W.fogDensityFor(wt.fogSigma, 3990, W.FOG_HEIGHT)
check(math.abs(env.fog - fogWant) < 0.1 * fogWant, 'fog density compensated for the ground')
near(env.fogH, W.FOG_HEIGHT, 1e-9, 'fog height')
near(sky.above, wt.cloudAboveKm, 0.1, 'cloud base to sky')
-- ground wind towards NE at about the mean speed (gusts on top)
local gw = env.ground
local speed = math.sqrt(gw.x ^ 2 + gw.y ^ 2)
check(gw.x > 0 and gw.y > 0 and speed >= 8.81 * (1 - W.GUST_LULL) - 0.2 and speed <= 13.2 + 0.2,
  'ground wind NE, lull..gust: ' .. speed)
-- over a few minutes the wind visits lulls and gusts and swings around its mean direction
do
  local lo, hi, amin, amax = math.huge, 0, math.huge, -math.huge
  local mean = math.atan2(wt.windDirY, wt.windDirX)
  for _ = 1, 1200 do
    run(0.25)
    local g = env.ground
    local sp = math.sqrt(g.x ^ 2 + g.y ^ 2)
    lo, hi = math.min(lo, sp), math.max(hi, sp)
    local d = math.deg(math.atan2(g.y, g.x) - mean)
    d = (d + 180) % 360 - 180
    amin, amax = math.min(amin, d), math.max(amax, d)
  end
  check(lo < 8.81 * 0.9 and hi > 8.81 * 1.15, string.format('gusts and lulls: %.1f..%.1f m/s', lo, hi))
  local lim = 2.5 * W.dirStdDeg(8.81, 13.2) + 1
  check(amax - amin > 10 and amin >= -lim and amax <= lim, string.format('direction swings %.0f..%.0f deg', amin, amax))
end
-- temperature curve (table setter, editor-dirty for a few frames, then cleared)
check(math.abs(li.curve[1][2] - 29.77) < 1 and li.curve[2][2] == li.curve[1][2], 'flat temperature curve near 29.8')
check(li.dirty == false, 'editor-dirty cleared')
-- gravity: latitude 36.58, height 4000
near(-env.gravity, W.gravityAt(36.58, 4000), W.GRAVITY_EPS, 'gravity by latitude and height')
-- dry: the precipitation stays hidden, no pool
check(rain.hidden == true and snow.hidden == true and rain.applied == nil, 'dry: precipitation hidden')

-- writes only on change: once converged, no calls (the ground wind's gusts keep changing)
run(2 * W.LERP_S)
local before = {}
for k, v in pairs(env.calls) do before[k] = v end
run(30)
check(env.calls.cover == before.cover and env.calls.fog == before.fog and env.calls.gravity == before.gravity,
  'steady state: no cloud, fog or gravity writes')

-- rain arrives: the pool is set once and the fraction follows
W.onWeather({ cover = { low = 100, mid = 50, high = 0 }, rainMmH = 8, snowCmH = 0, visM = 5000, slTempC = 12,
  wind = { ms = 5, fromDeg = 180, gustMs = 9 }, baseAglM = 600, lat = 36.58, lon = -118.29 })
run(W.LERP_S)
check(rain.hidden == false and rain.fields.numDrops == tostring(W.RAIN_POOL) and rain.applied == 1, 'rain pool set once')
check(rain.pct > 0.9, 'rain fraction approached 1: ' .. tostring(rain.pct))
check(snow.hidden == true, 'no snow')
-- the camera above the cloud base: no rain
camZ = 5000
run(1)
check(rain.hidden == true, 'no rain above the clouds')
camZ = 10
run(1)
check(rain.hidden == false, 'rain again below the clouds')

-- our own writes fire the hook all the time: never a change
check(W.getMode() == 'real' and #toasts == 0 and env.hooks > 100, 'our own writes: still real, no toast (' .. env.hooks .. ' hooks)')

-- the sun follows the deck (the rain message: low 100 %, still approaching): the level's 1.2 times
-- the factor
run(40)
check(sunB() >= 1.2 * W.sunDimFor(1) - 1e-6 and sunB() < 1.2 * (W.sunDimFor(1) + 0.1),
  'sun dimmed under the overcast deck: ' .. tostring(sunB()))
local sunWrites = sun.applied
run(10)
check(sun.applied == sunWrites, 'sun: no writes once it is there')

-- manual mode: hands off, precipitation hidden, the level's sun back
W.setMode('manual')
local n0 = env.calls.cover
run(5)
check(env.calls.cover == n0 and rain.hidden == true, 'manual: no writes, rain hidden')
near(sunB(), 1.2, 1e-6, 'manual: the level\'s sun brightness restored')
W.setMode('real')
run(W.LERP_S)
check(sunB() < 1.2, 'real again: the sun is dimmed again')
-- someone else sets the sun (another mod): that becomes the base, and a restore leaves theirs alone
sun:setField('brightness', 0, '0.9')
W.setMode('manual')
near(sunB(), 0.9, 1e-6, 'manual: a value someone else set is left alone')
W.setMode('real')

-- the settings (beamngEarth_settings.set applies the mode again, as settings.lua does)
local saved = {}
beamngEarth_settings = { set = function(k, v)
  saved[#saved + 1] = k .. '=' .. tostring(v)
  if k == 'weatherMode' then W.setMode(v) end
  return { ok = true, value = v }
end }

-- a setState that only repeats the engine's values (getState x1000 fog, vectors as tables, time keys):
-- no change
local nWind, nToasts = env.calls.ground, #toasts
W.onEnvironmentChanged({ cloudCover = env.cover, fogDensity = env.fog * 1000, fogAtmosphereHeight = env.fogH,
  windSpeed = env.windSpeed, cloudCirrusCoverage = env.cirrusCov, cloudCirrusDensity = env.cirrusDen,
  cloudWindDirection = { x = env.cloudDir.x, y = env.cloudDir.y, z = 0 }, cloudAltitudeKm = env.cloudAlt,
  groundWind = { x = env.ground.x, y = env.ground.y, z = 0 }, time = 0.3, play = true })
run(5)
check(W.getMode() == 'real' and #toasts == nToasts and #saved == 0 and env.calls.ground > nWind,
  'repeated values: still real, still driven')

-- a change in the pause menu (setState with the changed field, before it is applied): writes stop at
-- once, after MISSION_SETUP_S (no mission started) the weather is manual, saved, one toast
local fogCalls = env.calls.fog
W.onEnvironmentChanged({ fogDensity = 25 })
env.fog = 0.025
run(W.MISSION_SETUP_S - 0.5)
check(W.getMode() == 'real' and env.calls.fog == fogCalls and #toasts == nToasts, 'a change: writes wait, not switched yet')
W.onEnvironmentChanged({ fogDensity = 30 })        -- the slider moves on
env.fog = 0.03
local logged, oldLog = {}, log
log = function(lvl, _, msg) logged[#logged + 1] = lvl .. ' ' .. tostring(msg) end
run(1)
log = oldLog
check(W.getMode() == 'manual', 'a change: manual')
local warned = false
for _, l in ipairs(logged) do
  if l:find('^W weather: changed by someone else: fogDensity=25') then warned = true end
end
check(warned, 'what changed is in a warning (GitHub #418: the reports carried the warnings alone)')
check(#saved == 1 and saved[1] == 'weatherMode=manual', 'manual saved as the setting')
check(#toasts == nToasts + 1 and toasts[#toasts][2].title == 'BeamNG.Earth weather'
  and toasts[#toasts][2].msg:find('Manual', 1, true) and not toasts[#toasts][2].msg:find('BWM', 1, true), 'one toast')
check(rain.hidden == true and snow.hidden == true, 'manual: precipitation hidden')
near(sunB(), 1.2, 1e-6, 'manual: the level\'s sun brightness restored')
check(W.statusLine():find('manual, changed in the game', 1, true) ~= nil, 'status line says why')
local nCover = env.calls.cover
W.onEnvironmentChanged({ cloudCover = 0.1 })
env.cover = 0.1
run(10)
check(env.calls.cover == nCover and env.fog == 0.03 and #toasts == nToasts + 1 and #saved == 1,
  'manual: hands off, further changes no toast')
-- manual: a cloud altitude set in the menu is kept (the layer stops following the ground); the same
-- altitude repeated is not
sky.follow = true
W.onEnvironmentChanged({ cloudAltitudeKm = env.cloudAlt })
check(sky.follow == true, 'manual: a repeated cloud altitude leaves the ground following on')
W.onEnvironmentChanged({ cloudAltitudeKm = env.cloudAlt + 1 })
check(sky.follow == false, 'manual: a cloud altitude set in the menu stops the ground following')
-- Real again (the settings page): driven again, the sun dims again
W.setMode('real')
run(W.LERP_S)
check(env.calls.fog > fogCalls and env.calls.cover > nCover and env.cover > 1.5 and sunB() < 1.2 and rain.hidden == false,
  'real again: driven again')
check(W.getMode() == 'real' and #toasts == nToasts + 1, 'real again: stays real')

-- another mod's setGroundWind: core_environment applies it before its hook, so the hook carries the
-- engine's own (new) value: compared with what we wrote last
nToasts = #toasts
W.onEnvironmentChanged({ groundWind = vec3(env.ground.x, env.ground.y, 0) }) -- our own value repeated
run(W.MISSION_SETUP_S + 0.5)
check(W.getMode() == 'real', 'ground wind: our own value repeated is no change')
env.ground = vec3(20, 0, 0)
W.onEnvironmentChanged({ groundWind = env.ground })
run(W.MISSION_SETUP_S + 0.5)
check(W.getMode() == 'manual' and #toasts == nToasts + 1 and env.ground.x == 20, 'ground wind from another mod: manual')
W.setMode('real')
run(5)

-- missions: their own setup (just before 'started') is no change, nothing is written while they run,
-- changes during them are theirs
local nSaved = #saved
W.onEnvironmentChanged({ cloudCover = 0.2 })   -- the mission's setup, just before 'started'
env.cover = 0.2
W.onAnyMissionChanged('started')
local n0 = env.calls.cover
W.onEnvironmentChanged({ fogDensity = 40 })    -- during the mission
run(10)
check(env.calls.cover == n0, 'mission: paused')
W.onAnyMissionChanged('stopped')
run(W.MISSION_RESUME_S + 2)
check(env.calls.cover > n0 and env.cover > 0.3, 'mission stopped: resumed')
check(W.getMode() == 'real' and #saved == nSaved, 'mission: its setup and changes did not switch to manual')

-- the big map: it sets fog, clouds and time and puts them back as it closes: no change, no writes
local bigMap = false
freeroam_bigMapMode = { bigMapActive = function() return bigMap end, isTransitionActive = function() return false end }
W.onBeforeBigMapActivated()
W.onEnvironmentChanged({ fogDensity = 0 }); env.fog = 0     -- before it is active (instant open)
bigMap = true
W.onEnvironmentChanged({ cloudCover = 0 }); env.cover = 0
n0 = env.calls.cover
run(10)
check(env.calls.cover == n0 and env.cover == 0, 'big map: nothing written')
W.onEnvironmentChanged({ cloudCover = 1.9 }); env.cover = 1.9 -- its restore, still active
bigMap = false
W.onDeactivateBigMapCallback()
run(W.BIGMAP_TAIL_S + 2)
check(W.getMode() == 'real' and #saved == nSaved and env.calls.cover >= n0, 'big map: still real afterwards')
freeroam_bigMapMode = nil

-- gravity: a manual gravity change (the menu's presets) is the user's; switching it off restores 9.81
W.setMode('real')
W.setGravityEnabled(false)
run(2)
near(env.gravity, -W.GRAVITY_DEFAULT, 1e-12, 'gravity off: the level default')
W.setGravityEnabled(true)
run(2)
near(-env.gravity, W.gravityAt(36.58, 4000), W.GRAVITY_EPS, 'gravity on again')
nToasts = #toasts
W.onEnvironmentChanged({ gravity = -1.62 })
env.gravity = -1.62
run(5)
check(env.gravity == -1.62, 'gravity override respected')
check(W.getMode() == 'real' and #toasts == nToasts, 'a gravity change does not switch the weather')

-- the server's real gravity (server/gravity.py 'gravity' message): carried to the player's height
-- by the free-air gradient, approached in GRAVITY_STEP steps per check, back to normal gravity on
-- a new session (clearGravity)
near(W.gravityFrom({ g = 9.79, h = 100 }, 600), 9.79 - 500 * W.FREE_AIR, 1e-12, 'gravityFrom: free air')
check(W.gravityToward(nil, 9.8, 1e-3) == 9.8 and W.gravityToward(9.8, 9.7995, 1e-3) == 9.7995,
  'gravityToward: no previous value or within a step: the value itself')
near(W.gravityToward(9.79, 9.8, 1e-3), 9.791, 1e-12, 'gravityToward: one limited step')
W.setMode('real')                                   -- clears the gravity override
run(2)
local gNormal = W.gravityAt(36.58, 4000)
near(-env.gravity, gNormal, W.GRAVITY_EPS, 'override cleared: normal gravity')
check(W.gravityLine():find('no server value', 1, true) ~= nil, 'gravity line: normal by latitude')
local gmsg = { type = 'gravity', v = 1, g = gNormal + 0.0035, h = 3900, lat = 36.58, lon = -118.29, source = 'egm2008' }
local gWant = W.gravityFrom(gmsg, 4000)
W.onGravity(gmsg)
run(0.1)
near(-env.gravity, gNormal + W.GRAVITY_STEP, 1e-9, 'server gravity: the first step only')
run(W.GRAVITY_CHECK_S + 0.05)
near(-env.gravity, gNormal + 2 * W.GRAVITY_STEP, 1e-9, 'server gravity: one step per check')
run(5)
near(-env.gravity, gWant, 1e-12, 'server gravity reached (free air for the 100 m since)')
check(W.gravityLine():find('EGM2008', 1, true) ~= nil, 'gravity line: EGM2008')
local nGrav = env.calls.gravity
W.onGravity({ g = 'x' })
W.onGravity({ g = 1.62 })
run(3)
check(env.calls.gravity == nGrav and math.abs(-env.gravity - gWant) < 1e-12, 'invalid gravity messages ignored')
veh.pos = vec3(0, 0, 4300)                          -- a climb: free air from the server's value
run(2)
near(-env.gravity, W.gravityFrom(gmsg, 4300), W.GRAVITY_EPS, 'climb: free-air gradient')
veh.pos = vec3(0, 0, 4000)
W.clearGravity()
run(5)
near(-env.gravity, gNormal, W.GRAVITY_EPS, 'new session: normal gravity until its message')

-- 'auto' (the old default, a saved setting) is real; BWM_Weather no longer switches anything by itself,
-- but its weather changes are changes like the player's (and the toast says so)
W.setMode('manual')
check(W.setMode('auto') == 'weather mode: real' and W.getMode() == 'real', "setMode('auto') = real")
check(W.setMode('stormy'):find('must be', 1, true) and W.getMode() == 'real', 'an unknown mode is refused')
W.onClientEndMission()
W.onClientStartMission()
BWM_Weather_main = { getWeatherState = function() return { currentPreset = 'cloudy', targetPreset = 'cloudy', automaticWeather = false } end }
nToasts = #toasts
run(W.GRACE_S - 1)
check(W.getMode() == 'real' and #toasts == nToasts, 'BWM set to a preset: still real, no toast')
W.onEnvironmentChanged({ cloudCover = 0, fogDensity = 0 })          -- its start transition: grace
run(2)
check(W.getMode() == 'real', 'grace: no switch')
W.onEnvironmentChanged({ cloudCover = env.cover + 0.5 })            -- BWM changes the clouds
run(W.MISSION_SETUP_S + 0.5)
check(W.getMode() == 'manual' and #toasts == nToasts + 1 and toasts[#toasts][2].msg:find('BWM_Weather', 1, true),
  'BWM changes the weather: manual, the toast names BWM_Weather')
BWM_Weather_main = nil
W.setMode('real')
run(W.LERP_S)
check(W.getMode() == 'real' and env.calls.cover > 0, 'real again after BWM')

-- a Lua reload mid-level: settings.lua loads (and applies) before this extension, so the saved mode
-- is read back here; without the settings extension (or a get) the default stays
beamngEarth_settings = { get = function(k) return k == 'weatherMode' and 'manual' or nil end, set = beamngEarth_settings.set }
W.onExtensionLoaded()
check(W.getMode() == 'manual', 'reload: the saved Manual comes back')
nToasts, nSaved = #toasts, #saved
run(W.GRACE_S + 5)
W.onEnvironmentChanged({ cloudCover = env.cover + 0.5 })
run(W.MISSION_SETUP_S + 0.5)
check(#toasts == nToasts and #saved == nSaved, 'reload: manual, no switch and no toast')
beamngEarth_settings = { set = beamngEarth_settings.set }
W.setMode('real')
W.onExtensionLoaded()
check(W.getMode() == 'real', 'reload without settings.get: the mode is kept')

-- other levels: nothing happens there
level = 'west_coast_usa'
W.onClientEndMission()
W.onClientStartMission()
for k in pairs(env.calls) do env.calls[k] = 0 end
run(W.GRACE_S + 5)
local any = 0
for _, v in pairs(env.calls) do any = any + v end
check(any == 0, 'no weather or gravity outside the BeamNG.Earth level')

-- a BeamMP server's weather preset (G.WEATHER): the same message for every player; real messages wait meanwhile
do
  T.check(W.presetMsg('nope') == nil and W.presetMsg('Rain').rainMmH > 0 and W.presetMsg('rain').preset == 'rain',
    'presets: known names only, any case')
  for name in pairs(W.PRESETS) do
    local t = W.targets(W.presetMsg(name))
    T.check(type(t.coverage) == 'number' and type(t.fogSigma) == 'number', 'preset ' .. name .. ' gives targets')
  end
  T.check(W.setPreset('snow') and W.preset() == 'snow', 'snow set')
  W.onWeather({ cover = { low = 0, mid = 0, high = 0 }, visM = 50000, slTempC = 25, code = 0 })
  T.check(W.statusLine():find('server: snow', 1, true) ~= nil and W.statusLine():find('snow', 1, true) ~= nil, 'a real message does not replace the server\'s preset: ' .. W.statusLine())
  T.check(W.setPreset(nil) == false and W.preset() == nil, 'cleared')
end

-- high above the ground (2026-11-06): the fog fades out with the camera's height, the clouds go far above them
do
  local W = require('beamngEarth/weather')
  T.check(W.fogAltitudeShare(0) == 1 and W.fogAltitudeShare(W.FOG_FADE_FROM_M) == 1, 'fog: all of it low down')
  T.check(math.abs(W.fogAltitudeShare((W.FOG_FADE_FROM_M + W.FOG_FADE_TO_M) / 2) - 0.5) < 1e-9, 'fog: half way up, half')
  T.check(W.fogAltitudeShare(100000) == 0 and W.fogAltitudeShare(nil) == 1, 'fog: none from 100 km; no camera, all')
  T.check(not W.cloudsHiddenAt(5000, false) and W.cloudsHiddenAt(15000, false), 'clouds: hidden far above them')
  T.check(W.cloudsHiddenAt(11000, true) and not W.cloudsHiddenAt(9000, true), 'clouds: shown again lower (hysteresis)')
end
T.done('test_weather')
