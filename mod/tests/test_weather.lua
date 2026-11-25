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

-- rain/snow fraction: by what the eye sees (the weather plan 4.3 A4), linear in log(rate) between the curve's rows
check(W.precipPct(0) == 0 and W.precipPct(W.PRECIP_OFF) == 0, 'dry')
check(W.precipPct(0.03) == W.RAIN_CURVE[1][2], 'a trace: the curve\'s first share')
for _, row in ipairs(W.RAIN_CURVE) do near(W.precipPct(row[1]), row[2], 1e-12, 'rain row ' .. row[1]) end
for _, row in ipairs(W.SNOW_CURVE) do near(W.precipPct(row[1], W.SNOW_CURVE), row[2], 1e-12, 'snow row ' .. row[1]) end
near(W.precipPct(2), 0.45 + 0.3 * math.log(2) / math.log(4), 1e-12, 'log-linear between 1 and 4 mm/h')
check(W.precipPct(50) == 1, 'capped at the full pool')
do
  local p0 = 0
  for i = 1, 400 do
    local p = W.precipPct(0.02 + i * 0.05)
    check(p >= p0, 'rain share monotone at ' .. i)
    p0 = p
  end
end
check(W.precipPct(0.3) >= 0.25 and W.precipPct(1) >= 0.45 and W.precipPct(4) >= 0.75,
  'plan curve: drizzle 25 %, 1 mm/h 45 %, 4 mm/h 75 %')
-- rain / snow fog: real visibility in rain ~12 km at 1 mm/h, 4 km at 5, 1.4 km at 25; in snow ~1 km at 1 cm/h
near(W.FOG_K / W.rainSigma(1), 11850, 300, 'rain 1 mm/h: ~12 km')
check(W.FOG_K / W.rainSigma(5) > 3500 and W.FOG_K / W.rainSigma(5) < 4500, 'rain 5 mm/h: ~4 km')
check(W.FOG_K / W.rainSigma(25) > 1100 and W.FOG_K / W.rainSigma(25) < 1700, 'rain 25 mm/h: ~1.4 km')
near(W.FOG_K / W.snowSigma(1), 1003, 10, 'snow 1 cm/h: ~1 km')
check(W.rainSigma(0) == 0 and W.snowSigma(nil) == 0, 'dry air: no precipitation fog')
-- the fog of a message: the thicker of the visibility's and the rain's; the cap
near(W.fogSigmaFor({ visM = 20000, rainMmH = 10 }), W.rainSigma(10), 1e-15, 'heavy rain thickens a clear-air fog')
near(W.fogSigmaFor({ visM = 300, rainMmH = 1 }), W.fogSigma(300), 1e-15, 'thick fog stays the fog')
near(W.fogSigmaFor({ visM = 100 }, 500), W.FOG_K / 500, 1e-15, 'the fog cap: at most the limit\'s')
near(W.fogSigmaFor({ visM = 100 }, 0), W.fogSigma(100), 1e-15, 'no cap')
-- grey fog, flakes, shelter, the precipitation owner, pools
check(W.fogGreyFor(0, 0, 0, false) == 0 and W.fogGreyFor(W.FOG_GREY_RAIN_MMH, 0, 0) == 1 and W.fogGreyFor(0, 1, 0) == 1, 'grey: rain, snow')
near(W.fogGreyFor(0.5, 0, 0), 0.5 / W.FOG_GREY_RAIN_MMH, 1e-12, 'grey: light rain partly')
near(W.fogGreyFor(0, 0, 1), W.FOG_GREY_DECK, 1e-12, 'grey: an overcast deck')
near(W.fogGreyFor(0, 0, 0, true), W.FOG_GREY_FOG, 1e-12, 'grey: fog')
check(W.flakeKind(0) == 'wet' and W.flakeKind(-4) == 'snow' and W.flakeKind(-12) == 'dry' and W.flakeKind(20, true) == 'hail',
  'flakes by temperature, hail')
for k, f in pairs(W.FLAKES) do check(f.dropSize > 0 and f.maxSpeed >= f.minSpeed and f.maxMass >= f.minMass, 'flake ' .. k) end
near(W.shelterStep(1, true, W.SHELTER_FADE_OUT_S / 2), 0.5, 1e-12, 'shelter: half faded out')
check(W.shelterStep(0.1, true, 1) == 0 and W.shelterStep(0.9, false, 1) == 1, 'shelter: clamped')
near(W.shelterStep(0, false, W.SHELTER_FADE_IN_S / 4), 0.25, 1e-12, 'shelter: back slower')
check(W.precipBlock({ mode = 'real' }) == nil and W.precipBlock({ mode = 'preset', show = true }) == nil, 'precip shown')
check(W.precipBlock({ mode = 'manual' }) == 'off' and W.precipBlock({ mode = 'real', show = false }) ~= nil
  and W.precipBlock({ mode = 'real', map3d = true }) == '3D map' and W.precipBlock({ mode = 'real', bigMap = true }) ~= nil
  and W.precipBlock({ mode = 'preset', mission = true }) == 'mission' and W.precipBlock({ mode = 'real', aboveClouds = true }) ~= nil,
  'precip blocked: off, the setting, the 3D map, the big map, a mission, above the clouds')
check(W.poolFor(4000, 1.3) == 5200 and W.poolFor(4000, 100) == W.POOL_MAX and W.poolFor(4000, nil) == 4000, 'pools')

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
near(wet.rainPct, W.precipPct(2), 1e-12, 'rain 2 mm/h')
check(wet.fogHeight == W.FOG_HEIGHT_FOG and math.abs(wet.fogSigma - 3.912 / 300) < 1e-12, 'fog')
local snowy = W.targets({ rainMmH = 0, snowCmH = 0.5 })
near(snowy.snowPct, W.precipPct(0.5, W.SNOW_CURVE), 1e-12, 'snow 0.5 cm/h')
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
check(rain.hidden == false and rain.fields.numDrops == tostring(W.poolFor(W.RAIN_POOL, W.RAIN_VISIBILITY)) and rain.applied == 1,
  'rain pool set once (times the rain visibility)')
check(rain.pct > 0.9 * W.precipPct(8), 'rain fraction approached its share: ' .. tostring(rain.pct))
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
-- (times the storm light's gloom: phase B)
local gl = 1 - W.GLOOM_SUN * W.gloomFor(1, W._cur().rainMmH, 0)
check(sunB() >= 1.2 * W.sunDimFor(1) * gl - 0.02 and sunB() < 1.2 * (W.sunDimFor(1) + 0.1) * gl,
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

do  -- a fixed 1 km view in real weather, then manual weather, then the real horizon: the floor apply wrote is let go
  local savedS = rawget(_G, 'beamngEarth_settings')
  local vmode = 'fixed'
  beamngEarth_settings = { get = function(k) if k == 'viewDistanceMode' then return vmode end
                                             if k == 'viewDistanceKm' then return 1 end end }
  W.setMode('real')
  W.onWeather({ cover = { low = 0, mid = 0, high = 0 }, rainMmH = 0, snowCmH = 0, visM = 80000, slTempC = 12,   -- (clear)
    wind = { ms = 2, fromDeg = 180, gustMs = 3 }, baseAglM = 1500, lat = 36.58, lon = -118.29 })
  run(W.LERP_S * 3)
  check(select(1, W._viewFogOwned()) ~= nil, 'clear real weather, fixed 1 km: the floor owns the fog')
  local floored = env.fog
  check(floored and floored >= 0.003 * 0.98, 'real weather, fixed 1 km: at least the 1 km floor (' .. tostring(floored) .. ')')
  W.setMode('manual')
  run(1)
  vmode = 'horizon'
  run(1)
  check(env.fog < floored * 0.5, 'real -> manual -> horizon: the fixed distance\'s fog let go (' .. tostring(env.fog) .. ')')
  W.setMode('real')
  W.onWeather({ cover = { low = 100, mid = 50, high = 0 }, rainMmH = 8, snowCmH = 0, visM = 5000, slTempC = 12,
    wind = { ms = 5, fromDeg = 180, gustMs = 9 }, baseAglM = 600, lat = 36.58, lon = -118.29 })   -- (the weather before)
  beamngEarth_settings = savedS
  run(W.LERP_S * 3)
end
do  -- manual weather with a fixed 1 km view, real weather briefly, manual again, then the horizon: the density from before
  local savedS = rawget(_G, 'beamngEarth_settings')
  local vmode = 'fixed'
  beamngEarth_settings = { get = function(k) if k == 'viewDistanceMode' then return vmode end
                                             if k == 'viewDistanceKm' then return 1 end end }
  W.setMode('manual')
  env.fog = 4e-5
  run(1)
  check(env.fog >= 0.003 * 0.98, 'manual, fixed 1 km: raised to the floor')
  W.onWeather({ cover = { low = 0, mid = 0, high = 0 }, rainMmH = 0, snowCmH = 0, visM = 80000, slTempC = 12,   -- (clear)
    wind = { ms = 2, fromDeg = 180, gustMs = 3 }, baseAglM = 1500, lat = 36.58, lon = -118.29 })
  local floorNow = env.fog
  W.setMode('real')
  run(1)
  W.setMode('manual')
  run(1)
  vmode = 'horizon'
  run(1)
  -- (real weather started from the density under the floor, not from the floor: clear weather's own fog back, far under it)
  check(env.fog < floorNow * 0.2, 'manual -> real briefly -> manual -> horizon: the floor\'s fog let go (' .. tostring(env.fog) .. ')')
  W.setMode('real')
  W.onWeather({ cover = { low = 100, mid = 50, high = 0 }, rainMmH = 8, snowCmH = 0, visM = 5000, slTempC = 12,
    wind = { ms = 5, fromDeg = 180, gustMs = 9 }, baseAglM = 600, lat = 36.58, lon = -118.29 })   -- (the weather before)
  beamngEarth_settings = savedS
  run(W.LERP_S * 3)
end
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
  and toasts[#toasts][2].msg:find('Off', 1, true) and not toasts[#toasts][2].msg:find('BWM', 1, true), 'one toast')
check(rain.hidden == true and snow.hidden == true, 'manual: precipitation hidden')
near(sunB(), 1.2, 1e-6, 'manual: the level\'s sun brightness restored')
check(W.statusLine():find('off, changed in the game', 1, true) ~= nil, 'status line says why')
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
do  -- a fixed view distance: its fog (the scene ~95 % fogged where the engine stops drawing), none with the horizon
  local function get(mode, km) return function(k) if k == 'viewDistanceMode' then return mode end return km end end
  T.check(math.abs(W.viewFogFloor(get('fixed', 1)) - 0.003) < 1e-12, 'fixed 1 km: density 0.003')
  T.check(math.abs(W.viewFogFloor(get('fixed', 20)) - 1.5e-4) < 1e-12, 'fixed 20 km: 1.5e-4')
  T.check(W.viewFogFloor(get('horizon', 1)) == 0, 'the real horizon: no floor')
  T.check(W.viewFogFloor(get('fixed', 0)) == 0 and W.viewFogFloor(get('fixed', nil)) == 0, 'no distance: no floor')
  T.check(W.viewFogFloor(function() return nil end) == 0, 'no settings: no floor')
  -- (the engine thins its fog with height: 2 km up under a 1 km fog height the density is e^2 more, the same 3 optical
  -- depths at the clip; capped at the menu's maximum)
  T.check(math.abs(W.viewFogFloor(get('fixed', 1), 2000, 1000) - 0.003 * math.exp(2)) < 1e-12, 'fixed 1 km at 2 km up')
  T.check(W.viewFogFloor(get('fixed', 1), 9000, 1000) == W.FOG_DENSITY_MAX, 'capped at the menu maximum')
end
do  -- the fixed view distance's fog in manual weather (viewFog alone): raised, lowered with the floor, released
  local fog, height = 4e-5, 1000
  local e = { getFogDensity = function() return fog end, setFogDensity = function(v) fog = v end,
              getFogAtmosphereHeight = function() return height end }
  local saved = rawget(_G, 'beamngEarth_settings')
  local mode, km = 'fixed', 1
  beamngEarth_settings = { get = function(k) if k == 'viewDistanceMode' then return mode end return km end }
  W._viewFog(e, false, 0)
  T.check(math.abs(fog - 0.003) < 1e-12, 'manual weather, 1 km: raised to 0.003')
  km = 20
  W._viewFog(e, false, 0)
  T.check(math.abs(fog - 1.5e-4) < 1e-12, '20 km: lowered with the floor (not left at the 1 km fog)')
  km = 1
  W._viewFog(e, false, 2000)
  T.check(math.abs(fog - 0.003 * math.exp(2)) < 1e-12, '2 km up: the height-compensated floor')
  W._viewFog(e, false, 0)
  T.check(math.abs(fog - 0.003) < 1e-12, 'back down at sea level: lowered again')
  W._viewFog(e, true, 0)
  T.check(fog == 4e-5 and W._viewFogOwned() == nil, 'the 3D map open: the density it replaced back, released')
  W._viewFog(e, false, 0)
  mode = 'horizon'
  W._viewFog(e, false, 0)
  T.check(fog == 4e-5 and W._viewFogOwned() == nil, 'horizon: the density it replaced back')
  mode = 'fixed'
  W._viewFog(e, false, 0)
  fog = 0.02                     -- (the menu set a thicker fog meanwhile)
  W._viewFog(e, false, 0)
  mode = 'horizon'
  W._viewFog(e, false, 0)
  T.check(fog == 0.02, 'a fog set by something else is never taken back')
  beamngEarth_settings = saved
end

-- the rain hotfix (#1475 + guards): the Precipitation object is found by class when another object holds its name;
-- a tiny share is none; the object's own hidden state is used, not a cached flag
do
  local fake = S.newObject('SFXEmitter'); fake:registerObject('bngEarthRainShadowTest')
  local real = S.newObject('Precipitation'); real:registerObject('bngEarthRainShadowTest2')
  check(W._precipObject('bngEarthRainShadowTest2') == real, 'precip: found by name when it is a Precipitation')
  check(W._precipObject('bngEarthRainShadowTest') ~= fake, 'precip: another class under the name is never driven')
  check(W.PRECIP_NONE > 0 and W.PRECIP_NONE < W.PRECIP_MIN, 'precip: the none threshold sits under the drizzle floor')
end

------------------------------------------------------------------------------------------
-- the weather plan's phase A (2026-11-24): presets, intensity, the parts of the real weather, the precipitation's one
-- owner (shelter, the 3D map, missions, the setting, Off), flakes by temperature, grey fog, traffic, the radial ring

-- pure: presets, intensity, filters
do
  local seen = {}
  for _, name in ipairs(W.PRESET_ORDER) do
    seen[name] = true
    local m = W.presetMsg(name)
    check(m and m.preset == name and tonumber(m.code) and W.PRESET_LABELS[name], 'preset ' .. name .. ': message and label')
    local t = W.targets(m)
    check(t.fogSigma > 0 and t.rainPct >= 0 and t.snowPct >= 0 and W.FLAKES[t.flake], 'preset ' .. name .. ': targets')
  end
  for name in pairs(W.PRESETS) do check(seen[name], 'preset ' .. name .. ' on the page (PRESET_ORDER)') end
  for _, old in ipairs({ 'clear', 'cloudy', 'rain', 'storm', 'snow', 'fog' }) do
    check(W.PRESETS[old] ~= nil, 'the first BeamMP plugin\'s ' .. old .. ' is still a preset')
  end
  local m1, m2 = W.presetMsg('rain'), W.presetMsg('rain')
  m1.wind.ms = 99
  check(m2.wind.ms ~= 99 and W.PRESETS.rain.wind.ms ~= 99, 'preset messages are copies (deep)')
  check(W.targets(W.presetMsg('drizzle')).rainPct > 0.2 and W.targets(W.presetMsg('heavyrain')).rainPct > 0.95,
    'drizzle visible, heavy rain the whole pool')
  check(W.targets(W.presetMsg('sleet')).rainPct > 0 and W.targets(W.presetMsg('sleet')).snowPct > 0
    and W.targets(W.presetMsg('sleet')).flake == 'wet', 'sleet: rain and wet snow together')
  check(W.targets(W.presetMsg('hail')).flake == 'hail' and W.targets(W.presetMsg('blizzard')).flake == 'dry'
    and W.targets(W.presetMsg('lightsnow')).flake == 'snow', 'flake kinds of the presets')
  check(W.targets(W.presetMsg('storm')).fogGrey == 1 and W.targets(W.presetMsg('clear')).fogGrey == 0, 'grey fog: storm, clear')
  check(W.tintValue({ kind = 'mul', k = 150 }, '0.004 0.004 0.004', 1) == '0.6 0.6 0.6'
    and W.tintValue({ kind = 'mul', k = 150 }, '0.004 0.004 0.004', 0) == '0.004 0.004 0.004', 'tint: mul keeps the components')
  check(W.hailRate({ code = 96, rainMmH = 3 }) == W.HAIL_CODE_CMH and W.hailRate({ code = 96 }) == 0
    and W.hailRate({ code = 63, rainMmH = 3 }) == 0, 'hail from the hail codes only while it rains')

  local half = W.scaleMsg(W.presetMsg('storm'), 0.5)
  near(half.rainMmH, 7.5, 1e-12, 'intensity: rain'); near(half.wind.gustMs, 11, 1e-12, 'intensity: gusts')
  near(half.visM, 6000, 1e-9, 'intensity: thinner fog')
  local none = W.scaleMsg(W.presetMsg('fog'), 0)
  check(none.visM == W.FOG_VIS_MAX and not none.fog and none.wind.ms == 0, 'intensity 0: clear air, calm')
  check(W.scaleMsg(W.presetMsg('rain'), 9).rainMmH == 4 * W.INTENSITY_MAX, 'intensity capped')
  check(W.scaleMsg(W.presetMsg('rain'), 1).cover.low == 100, 'intensity keeps the clouds')

  local real = { code = 63, cover = { low = 90, mid = 80, high = 50 }, visM = 900, fog = true, rainMmH = 3, snowCmH = 0.2,
    wind = { ms = 9, gustMs = 15, fromDeg = 200 } }
  local f = W.filterReal(real, { clouds = false, fog = false, wind = false, precip = false })
  check(f.cover.low == 0 and f.cover.high == 0 and f.visM == nil and not f.fog and f.wind.ms == 0 and f.wind.fromDeg == 200
    and f.rainMmH == 0 and f.snowCmH == 0, 'every real part left out: a clear day')
  check(real.cover.low == 90 and real.rainMmH == 3, 'the real message itself is untouched')
  local g = W.filterReal(real, { fog = false })
  check(g.cover.low == 90 and g.visM == nil and g.rainMmH == 3 and g.wind.ms == 9, 'only the fog left out')
  check(W.targets(g).fogSigma == math.max(W.rainSigma(3), W.snowSigma(0.2)), 'real fog off: the rain and snow still thicken the air')

  -- traffic
  check(not W.trafficLightsFor(nil) and not W.trafficLightsFor({ rainMmH = 0, snowCmH = 0, visM = 20000 }), 'dry day: no lights')
  check(W.trafficLightsFor({ rainMmH = 0.5 }) and W.trafficLightsFor({ snowCmH = 0.1 }) and W.trafficLightsFor({ visM = 600 }),
    'lights: rain, snow, fog')
  near(W.trafficCapFor(30, { rainMmH = 2, visM = 8000 }), 27, 1e-9, 'rain: x0.9')
  near(W.trafficCapFor(30, { rainMmH = 12, visM = 3000 }), 24, 1e-9, 'heavy rain: x0.8')
  near(W.trafficCapFor(30, { snowCmH = 2 }), 30 * W.TRAFFIC_HEAVY_SNOW_F, 1e-9, 'heavy snow')
  local fogCap = W.trafficCapFor(30, { visM = 100 })
  check(fogCap < 25 and fogCap >= W.TRAFFIC_MIN_MS, 'fog 100 m: slow enough to stop in sight: ' .. fogCap)
  local v = fogCap
  near(v * W.TRAFFIC_REACT_S + v * v / (2 * W.TRAFFIC_BRAKE_MS2), 100, 1e-6, 'fog cap: stops within the visibility')
  check(W.trafficCapFor(30, nil) == 30 and W.trafficCapFor(nil, { rainMmH = 3 }) == nil, 'no weather: the cap as it was')
  check(W.darknessFor({ deck = 1, rainMmH = 8 }) == 1 and W.darknessFor({ deck = 1 }) == 0.5 and W.darknessFor({ deck = 0, rainMmH = 9 }) == 0,
    'darkness: a thick deck, more with rain')
  check(W.inNightWindow(0.5, 0.25, 0.75, 0) and not W.inNightWindow(0.2, 0.25, 0.75, 0) and W.inNightWindow(0.2, 0.25, 0.75, 0.06)
    and W.inNightWindow(0.95, 0.9, 0.1, 0) and not W.inNightWindow(0.5, 0.9, 0.1, 0.05), 'night windows, widened, wrapped')

  -- fog tint values
  local c = W.tintValue({ kind = 'color', scale = 1 }, '0.4 0.6 1 1', 1)
  local r, gg, bb = c:match('^(%S+) (%S+) (%S+)')
  check(math.abs(r - gg) < 1e-4 and math.abs(gg - bb) < 1e-4, 'tint: grey at 1')
  check(W.tintValue({ kind = 'color', scale = 1 }, '0.4 0.6 1 1', 0) == '0.4000 0.6000 1.0000 1.0000', 'tint: the base at 0')
  check(W.tintValue({ kind = 'number', value = 0 }, '1', 0.5) == '0.5000', 'tint: a number half way')
  check(W.tintValue({ kind = 'string', value = '' }, 'grad.png', 0.5) == '' and W.tintValue({ kind = 'string', value = '' }, 'grad.png', 0) == 'grad.png',
    'tint: a string while on')
  check(W.tintValue({ kind = 'color' }, 'junk', 1) == nil, 'tint: an unreadable colour is left alone')

  -- a weather message from the network ('real-host'): the weather's own fields only, each in range
  local sm = W.sanitizeMsg({ code = 300, rainMmH = -4, visM = 0 / 0, cover = { low = 250, mid = 'x' }, fog = 1,
    wind = { ms = 20, gustMs = 1e9, fromDeg = 400 }, lat = 51, tz = 'Europe/London', func = print })
  check(sm.code == 99 and sm.rainMmH == 0 and sm.visM == nil and sm.cover.low == 100 and sm.cover.mid == 0 and sm.fog == nil
    and sm.wind.gustMs == 200 and sm.wind.fromDeg == 360 and sm.lat == nil and sm.tz == nil and sm.func == nil,
    'sanitizeMsg: clamped, no place, nothing foreign')
  check(W.sanitizeMsg('x') == nil and W.sanitizeMsg({}).cover == nil, 'sanitizeMsg: not a table; missing parts stay missing')
  local th = W.sanitizeMsg({ code = 95, capeJkg = 2500, lpi = 3, rainMmH = 6 })
  check(th.capeJkg == 2500 and th.lpi == 3 and W.thunderOf(th) == 2, 'sanitizeMsg: the thunder fields relayed (real-host lightning)')
  check(type(W.targets(W.sanitizeMsg({ code = 61 })).fogSigma) == 'number', 'a sanitized message gives targets')

  -- the radial ring's choices
  local rc = W.radialChoice('rain')
  check(#rc == 2 and rc[1][1] == 'weatherPreset' and rc[1][2] == 'rain' and rc[2][2] == 'preset', 'ring: a preset')
  check(W.radialChoice('manual')[1][2] == 'manual' and #W.radialChoice('nope') == 0, 'ring: off, unknown')
  for _, r in ipairs(W.RADIAL) do check(r[1] == 'real' or r[1] == 'manual' or W.PRESETS[r[1]], 'ring entry ' .. r[1]) end
end

-- runtime
level = 'beamng_earth'
beamngEarth_settings = nil
W.onClientEndMission()
W.onClientStartMission()
W.setMode('real')
W.setChangeSpeed(0)                  -- (instant: each state at once)
local roof, amb = false, nil
core_camera.getPosition = function() return vec3(0, 0, camZ) end
castRayStatic = function(from, dir, max)
  if dir and dir.z and dir.z > 0 then return roof and 5 or (max or 60) + 1 end
  return from.z - 3990
end
package.loaded['beamngEarth/ambience'] = { shelterAt = function() return amb end }
run(W.GRACE_S + 1)
W.setMode('preset')
W.setPresetChoice('heavyrain')
run(1)
check(rain.hidden == false and math.abs(rain.pct - W.precipPct(10)) < 0.011, 'preset heavy rain: shown at once (instant)')
check(W.statusLine():find('preset: Heavy rain', 1, true) ~= nil, 'status: the preset: ' .. W.statusLine())
check(W.current().preset == 'heavyrain' and W.realMsg() ~= W.current(), 'current: the message shown; realMsg: the real one')
near(env.fog, W.fogDensityFor(W.targets(W.presetMsg('heavyrain')).fogSigma, 3990, W.FOG_HEIGHT), 1e-9 + 0.03 * env.fog,
  'heavy rain thickens the fog')
W.setIntensity(0)
run(1)
check(rain.hidden == true, 'intensity 0: no rain')
W.setIntensity(1)
run(1)
check(rain.hidden == false, 'intensity back')
-- "Show rain and snow" off: the drops go, the weather stays
W.setShowPrecip(false)
run(1)
check(rain.hidden == true and W.rainNow == 0 and W.precipState().block == 'hidden by the setting', 'show rain and snow off')
W.setShowPrecip(true)
run(1)
check(rain.hidden == false, 'shown again')
-- shelter: a roof straight above (three rays), then a road tile's space
roof = true
run(0.25)
check(rain.pct > 0 and rain.pct < W.precipPct(10), 'under a roof: fading out')
run(0.5)
check(rain.hidden == true and W.precipState().why == 'roof', 'under a roof: gone within FADE_OUT_S')
roof = false
run(0.5)
check(rain.hidden == false and rain.pct < W.precipPct(10) - 0.05, 'out again: fading back in')
run(1)
check(math.abs(rain.pct - W.precipPct(10)) < 0.011, 'out again: back in full')
amb = 'bridge'
run(1)
check(rain.hidden == true and W.precipState().why == 'bridge', 'under a bridge box: no rain')
amb = nil
run(1.5)
check(rain.hidden == false, 'out from under the bridge')
-- the 3D map: ours hidden by the owner; another mod's Precipitation hidden too and given back
local other = precip('bwmRain')
beamngEarth_map3d = { isActive = function() return true end }
run(1)
check(rain.hidden == true and other.hidden == true and W.precipState().block == '3D map', '3D map: no rain, ours or another mod\'s')
beamngEarth_map3d = nil
run(1)
check(rain.hidden == false and other.hidden == false, 'map closed: rain back, the other mod\'s object shown again')
-- a mission: hidden while it runs (the weather loop pauses, the owner does not)
W.onAnyMissionChanged('started')
run(1)
check(rain.hidden == true, 'mission: no rain from us')
W.onAnyMissionChanged('stopped')
run(W.MISSION_RESUME_S + 1)
check(rain.hidden == false, 'mission over: rain again')
-- flakes by temperature, set once per kind
W.setPresetChoice('snow')
run(1)
check(snow.hidden == false and snow.fields.dropSize == tostring(W.FLAKES.snow.dropSize), 'snow: its flakes')
local applied = snow.applied
run(2)
check(snow.applied == applied, 'flakes: not set again while the kind stays')
W.setPresetChoice('blizzard')
run(1)
check(snow.fields.dropSize == tostring(W.FLAKES.dry.dropSize) and snow.fields.minSpeed == tostring(W.FLAKES.dry.minSpeed),
  'blizzard at -9 C: small dry flakes')
check(snow.fields.numDrops == tostring(W.poolFor(W.SNOW_POOL, W.RAIN_VISIBILITY)), 'snow pool times the rain visibility')
W.setRainVisibility(2)
run(1)
check(snow.fields.numDrops == tostring(W.poolFor(W.SNOW_POOL, 2)), 'rain visibility: a bigger pool')
W.setRainVisibility(W.RAIN_VISIBILITY)
W.setPresetChoice('hail')
run(1)
check(snow.fields.dropSize == tostring(W.FLAKES.hail.dropSize) and rain.hidden == false, 'hail: pellets and rain')
-- grey fog: the sky's fog levers moved by the greyness, given back exactly when dry and clear
sun:setField('atmoMieScattering', 0, '0.004 0.004 0.004')
sun:setField('atmoMieExtinction', 0, '0.0044 0.0044 0.0044')
sun:setField('atmoMiePhaseG', 0, '0.8')
W.setPresetChoice('storm')
run(1)
local ms = { tostring(sun:getField('atmoMieScattering')):match('^(%S+) (%S+) (%S+)$') }
check(#ms == 3 and math.abs(ms[1] - 0.6) < 1e-6 and math.abs(tonumber(sun:getField('atmoMiePhaseG')) - 0.2) < 1e-6,
  'storm: the atmosphere\'s Mie haze x150 (three components kept), a flatter phase: ' .. tostring(sun:getField('atmoMieScattering')))
beamngEarth_map3d = { isActive = function() return true end }
run(1)
check(sun:getField('atmoMieScattering') == '0.004 0.004 0.004', 'over the 3D map: the sky\'s own atmosphere')
beamngEarth_map3d = nil
run(1)
W.setPresetChoice('clear')
run(1)
check(sun:getField('atmoMieScattering') == '0.004 0.004 0.004' and sun:getField('atmoMiePhaseG') == '0.8', 'clear: the sky\'s own values back')
W.setPresetChoice('storm')
run(1)
W.setMode('manual')
check(sun:getField('atmoMieExtinction') == '0.0044 0.0044 0.0044', 'Off: the sky\'s own values back')
-- traffic in this weather
W.setMode('preset')
W.setPresetChoice('rain')
check(W.trafficLights() and math.abs(W.trafficCap(30) - 27) < 1e-9, 'rain preset: traffic lights on, x0.9')
W.setPresetChoice('storm')
check(W.duskDark({ time = 0.2, nightStart = 0.25, nightEnd = 0.75, isNight = false })
  and not W.duskDark({ time = 0.05, nightStart = 0.25, nightEnd = 0.75, isNight = false }), 'storm: lamps 1.5 h before dusk, not at noon')
W.setPresetChoice('clear')
check(not W.trafficLights() and W.trafficCap(30) == 30 and W.darkness() == 0, 'clear: traffic as normal')
W.setMode('manual')
check(not W.trafficLights() and W.trafficCap(30) == 30, 'Off: traffic as normal')
-- Off: hidden, and nothing runs for the precipitation (no calls to its objects)
W.setMode('preset')
W.setPresetChoice('rain')
run(1)
check(rain.hidden == false, 'rain before Off')
W.setMode('manual')
check(rain.hidden == true and snow.hidden == true, 'Off: hidden at once')
local nRay = 0
local ray0 = castRayStatic
castRayStatic = function(from, dir, max) if dir and dir.z > 0 then nRay = nRay + 1 end return ray0(from, dir, max) end
local pctBefore = rain.pct
run(3)
check(nRay == 0 and rain.pct == pctBefore, 'Off: no shelter rays, no precipitation writes')
castRayStatic = ray0

-- phase B: the gloom, the storm's exposure on the stock HDR's bias over the player's own, given back
check(W.gloomFor(0, 0, 0) == 0 and W.gloomFor(1, 0, 0) == W.GLOOM_DECK, 'gloom: clear 0, overcast the deck\'s')
check(W.gloomFor(1, 15, 2) > W.gloomFor(1, 4, 0) and W.gloomFor(1, 100, 3) <= 1, 'a storm gloomier than rain, at most 1')
check(W.dayShare(0) == 1 and W.dayShare(0.5) == 0 and W.dayShare(nil) == 1, 'day share: noon 1, midnight 0')
near(W.stormEV(1, 1), -W.STORM_EV_MAX, 1e-12, 'full gloom by day')
near(W.stormEV(1, 0), -W.STORM_EV_MAX * W.NIGHT_EV_SHARE, 1e-12, 'a share of it at night')
check(W.thunderOf(W.presetMsg('storm')) == 2 and W.thunderOf(W.presetMsg('hail')) == 3 and W.thunderOf(W.presetMsg('rain')) == 0,
  'thunder in the storm and hail presets only')
check(W.thunderOf(W.scaleMsg(W.presetMsg('storm'), 0.1)) == 0, 'no thunder at a tiny intensity')
local hdr = S.newObject('PostEffectLocalExposure')
hdr:registerObject('PostEffectLocalExposureObject')
hdr.exposureBiasEV = 0.5
settings = { getValue = function(k) if k == 'GraphicEVCompensation' then return 0.5 end end }
W.setMode('preset')
W.setChangeSpeed(0)
W.setPresetChoice('storm')
run(1)
local wantEV = 0.5 - W.stormEV(W._cur().gloom, 1)      -- (the engine's EV: higher = darker)
check(math.abs(hdr.exposureBiasEV - wantEV) < 0.05 and hdr.exposureBiasEV > 1.5, 'storm: the bias raised (darker) over the player\'s 0.5: '
  .. tostring(hdr.exposureBiasEV))
W.setStormLight(false)
run(1)
check(hdr.exposureBiasEV == 0.5, 'storm light off: the player\'s own value back')
W.setStormLight(true)
run(1)
W.setMode('manual')
check(hdr.exposureBiasEV == 0.5, 'Off: the player\'s own value back')
-- Off with nothing left running: phase B does no work (no strikes looked up, no materials, no shells)
local stormU, wetU, strU = 0, 0, 0
local st0, we0, sr0 = package.loaded['beamngEarth/storm'].update, package.loaded['beamngEarth/wetlook'].update,
  package.loaded['beamngEarth/rainstreaks'].update
package.loaded['beamngEarth/storm'].update = function(...) stormU = stormU + 1 return st0(...) end
package.loaded['beamngEarth/wetlook'].update = function(...) wetU = wetU + 1 return we0(...) end
package.loaded['beamngEarth/rainstreaks'].update = function(...) strU = strU + 1 return sr0(...) end
run(70)           -- (winding down: a thunder still on its way, the materials going back)
local s1, w1, r1 = stormU, wetU, strU
run(5)
check(stormU == s1 and strU == r1 and wetU == w1, 'Off, wound down: no storm, streak or wet-look work per frame ('
  .. stormU - s1 .. ', ' .. strU - r1 .. ', ' .. wetU - w1 .. ')')
package.loaded['beamngEarth/storm'].update, package.loaded['beamngEarth/wetlook'].update = st0, we0
package.loaded['beamngEarth/rainstreaks'].update = sr0
settings = nil
-- the real weather's parts: real rain off keeps it dry with rain in the real message
W.setMode('real')
W.onWeather({ code = 63, cover = { low = 100, mid = 80, high = 40 }, rainMmH = 4, snowCmH = 0, visM = 8000, slTempC = 12,
  wind = { ms = 5, fromDeg = 200 }, lat = 36.58, lon = -118.29 })
run(1)
check(rain.hidden == false, 'real rain shown')
W.setRealPart('precip', false)
run(1)
check(rain.hidden == true and W.trafficLights() == false, 'real rain left out: dry, traffic as on a dry day')
W.setRealPart('precip', true)
W.setFogLimit(10000)
run(1)
check(env.fog <= W.fogDensityFor(W.FOG_K / 10000, 3990, W.FOG_HEIGHT) * 1.03, 'fog cap: no thicker than its visibility')
W.setFogLimit(0)
-- a BeamMP server's 'real-host': nothing changes until the host's weather arrives, then everyone gets it
W.setPreset('real-host')
run(1)
check(rain.hidden == false, 'real-host before the host\'s weather: as it was')
W.setHostWeather(W.presetMsg('fog'))
run(1)
check(rain.hidden == true and W.statusLine():find('server: real-host', 1, true), 'real-host: the host\'s fog for everyone')
W.setPreset(nil)
run(1)
check(rain.hidden == false and W.preset() == nil, 'server weather cleared: the player\'s real weather again')
-- the host itself: its own real weather is sent on (mpsession)
local sent = nil
beamngEarth_mpsession = { sendHostWeather = function(m) sent = m end }
W.setPreset('real-host', true)
W.onWeather({ code = 0, cover = { low = 0, mid = 0, high = 0 }, visM = 40000, slTempC = 20, wind = { ms = 2, fromDeg = 90 } })
check(sent and sent.code == 0 and W.isWeatherHost(), 'the weather host sends its real weather')
W.setPreset(nil)
beamngEarth_mpsession = nil
-- the radial ring: our level's category and its choices, saved as settings
do
  local added = {}
  core_quickAccess = { addEntry = function(a) added[#added + 1] = a end }
  W.onQuickAccessLoaded()
  check(#added == 2 and added[1].level == W.RADIAL_PARENT and added[2].level == W.RADIAL_LEVEL, 'ring: two generators')
  local cat, items = {}, {}
  added[1].generator(cat)
  added[2].generator(items)
  check(#cat == 1 and cat[1]['goto'] == W.RADIAL_LEVEL and #items == #W.RADIAL, 'ring: the category and its entries')
  local setCalls = {}
  beamngEarth_settings = { set = function(k, v) setCalls[#setCalls + 1] = k .. '=' .. tostring(v); return { ok = true } end }
  for _, it in ipairs(items) do
    if it.uniqueID == 'beamngEarthWeather_snow' then check(it.onSelect()[1] == 'hide', 'ring: hides after a choice') end
  end
  check(setCalls[1] == 'weatherPreset=snow' and setCalls[2] == 'weatherMode=preset', 'ring: saved as the settings')
  beamngEarth_settings = nil
  core_quickAccess = nil
end
-- the saved choices read back (a Lua reload)
do
  local saved = { weatherMode = 'preset', weatherPreset = 'fog', weatherIntensity = 0.5, weatherChange = '30', weatherPrecip = false,
    rainVisibility = 2, fogLimit = '500', realClouds = false, realFog = true, realWind = false, realPrecip = true }
  W.loadChoices(function(k) return saved[k] end)
  local c = W.choices()
  check(c.preset == 'fog' and c.intensity == 0.5 and c.changeS == 30 and c.show == false and c.visibility == 2
    and c.fogLimitM == 500 and c.parts.clouds == false and c.parts.wind == false and c.parts.fog and c.parts.precip,
    'saved choices loaded (false kept as false)')
  W.loadChoices(function(k) return ({ weatherPreset = 'rain', weatherIntensity = 1, weatherChange = '60', weatherPrecip = true,
    rainVisibility = W.RAIN_VISIBILITY, fogLimit = '0', realClouds = true, realWind = true })[k] end)
end
W.setChangeSpeed(W.LERP_S)
W.setMode('real')

-- traffic's headlights: the stock traffic vehicle class wrapped only while lights are wanted
do
  local X = require('beamngEarth/wxtraffic')
  local C = {}
  C.__index = C
  local lit = {}
  function C:checkTimeOfDay() return true end
  function C:onTrafficTick() lit[#lit + 1] = not self:checkTimeOfDay() end
  local car = setmetatable({}, C)
  gameplay_traffic = { getTrafficData = function() return { [7] = car } end }
  local check0, tick0 = C.checkTimeOfDay, C.onTrafficTick
  X.update(X.CHECK_S, true)
  check(X.installed and C.checkTimeOfDay ~= check0, 'wrap in while lights are wanted')
  car:onTrafficTick()
  check(lit[1] == true and car:checkTimeOfDay() == true, 'inside the traffic tick it is night (lights on); elsewhere day (spawning)')
  X.update(X.CHECK_S / 2, false)
  check(X.installed, 'checked every CHECK_S only')
  X.update(X.CHECK_S, false)
  check(not X.installed and C.checkTimeOfDay == check0 and C.onTrafficTick == tick0, 'wrap out: the class as it was')
  car:onTrafficTick()
  check(lit[2] == false, 'day again: lights off by the stock code')
  X.update(X.CHECK_S, true)
  local foreign = function() return true end
  C.checkTimeOfDay = foreign                         -- (another mod wrapped it since)
  X.uninstall()
  check(C.checkTimeOfDay == foreign and C.onTrafficTick == tick0, 'another mod\'s wrap is kept')
  C.checkTimeOfDay = check0
  gameplay_traffic = nil
end

T.done('test_weather')
