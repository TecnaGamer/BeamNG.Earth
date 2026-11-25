-- BeamNG.Earth: real weather at the player. GE extension 'beamngEarth_weather' (loaded by
-- scripts/beamng_earth_weather/modScript.lua). docs/research/2026-10-10-real-weather.md §4.2, measured
-- facts in docs/research/2026-10-10-weather-probe-results.md.
--
-- The server (server/weather.py) sends a 'weather' message: Open-Meteo's current conditions at the
-- player, every 10 min or 25 km; beamngEarth_core hands it to M.onWeather. Every value moves towards
-- its new target smoothly (exponential approach, 95 % in LERP_S) and is written through
-- core_environment, so the pause menu's Time & Weather panel, missions and other mods see the real
-- values: cloud cover (COVERAGE_CURVE, a calibration table), cirrus from high cloud, the clouds'
-- drift from the wind aloft, fog from visibility (density compensated for the height of the ground:
-- the engine's fog thins with absolute z, shaders/common/sceneFog.h.hlsl), ground wind with gusts, lulls and direction swings
-- (core_environment.setGroundWind reaches every vehicle), rain and snow (the level's Precipitation
-- objects; core_environment.setPrecipitation writes 'numOfDrops', a field the engine does not
-- have), and the air temperature (LevelInfo temperatureCurveC, which core_environment turns into
-- the engine's sea-level temperature every frame; the engine lowers it with height itself). The
-- real UTC offset goes to beamngEarth_sky (its clock), and the cloud base above the ground too (sky
-- owns the layer's ground-following altitude and the rebase weather-offset shift).
-- The sun dims under a thick cloud deck (ScatterSky.brightness, the direct light only: the game's
-- clouds cast no shadows, so under overcast the car kept a sharp sun shadow; in-game 2026-10-10 the
-- auto exposure keeps the scene's brightness while the shadows fade). The level's own value is put
-- back when the weather stops driving it (manual mode, a mission).
-- Gravity follows the player (GRAVITY_ENABLED, independent of the mode): the server's 'gravity'
-- message (server/gravity.py: WGS84 normal + EGM2008 anomaly at the player, every 2 km / 200 m of
-- height) with the free-air gradient for the height since; normal gravity by latitude and height
-- until the first message (or with a server without it). Changes are written in steps of at most
-- GRAVITY_STEP per check, so a new value is approached smoothly. A gravity change by anyone else
-- (the menu's presets) is kept until setMode('real') (S.gravityOverride); it does not touch the mode.
-- Everything runs in the BeamNG.Earth level only.
--
-- Modes (the settings' Weather page; the weather plan 2026-11-24, phase A): 'real' (the default) = drive the real
-- weather (each of its clouds, fog, wind and rain/snow can be left out: setRealParts, they then stay as on a clear
-- day); 'preset' = one of M.PRESETS (a fixed synthetic weather message, deterministic); 'manual' (the page's "Off") =
-- hands off (the game's own controls), our rain and snow hidden. Real and preset take the intensity (scaleMsg) and
-- move at the chosen change speed (setChangeSpeed: instant, 30 s, 60 s as before, 2 min).
-- A change made by anyone else (the pause menu, another mod such as BWM_Weather) after the start-up
-- grace period that really changes a value (the engine's value is compared: a setState repeating
-- the current values is no change) switches to 'manual' with one toast and saves that as the
-- setting (beamngEarth_settings, so the settings page shows it); choosing Real again goes back.
-- 'auto' (the old default: real unless BWM_Weather was changing the weather) means 'real'.
-- Missions and the big map pause it (they set and restore their own weather).
-- A BeamMP server's weather (setPreset: a preset name, or 'real-host' = the weather the server relays from its
-- weather host, setHostWeather) overrides real and preset; Off stays off.
-- Rain and snow (the level's two Precipitation objects) have ONE owner, precipBlock: shown only in real / preset mode,
-- with "Show rain and snow" on, outside the 3D map, the big map and missions, below the cloud base; and faded out in
-- shelter (a road tile's tunnel, arch, parking structure or under-bridge space, or a roof straight above the camera:
-- shelterStep). Rain thickens the fog (rainSigma / snowSigma) and turns its colour grey (fogGrey: the sky's sun
-- term in the lit fog dimmed, applyFogTint). Traffic reads trafficDark / trafficSpeedFactor (headlights, slower).
-- Console: beamngEarth_weather.setMode('real'|'preset'|'manual')   .setPresetChoice(name)   .status()
--          .setGravityEnabled(bool)

local M = {}

M.LEVEL_ID = 'beamng_earth'
M.LERP_S = 60              -- a new target is 95 % reached after this long
M.APPLY_S = 0.5            -- writes at 2 Hz, only of values that changed
M.WIND_APPLY_S = 0.25      -- setGroundWind queues Lua into every vehicle: 4 Hz (a gust rises in 1-3 s),
                           -- and only when the wind moved by WIND_EPS_MS
M.GROUND_CHECK_S = 5       -- ground height under the player (fog compensation)
M.GRACE_S = 20             -- after a level start: BWM_Weather's start transition, env's setState(init_env)
M.USER_QUIET_S = 3         -- after someone else's change: core_environment lerps it over 1.15 s and
                           -- each of its setters cancels a running lerp, so write nothing meanwhile
M.MISSION_SETUP_S = 2      -- missions set their weather just before 'started': a change switches to
                           -- manual only when no mission started within this long
M.MISSION_RESUME_S = 5
M.BIGMAP_TAIL_S = 2        -- the big map restores fog, clouds and time as it closes
M.TEMP_WRITE_S = 10        -- temperature curve writes at most this often
M.TEMP_EPS_C = 0.2

-- Calibration: sky fraction of the volumetric deck (0..1, Open-Meteo max(low, mid) / 100) ->
-- CloudLayer.coverage, linear between rows. From the probe (camera straight up, base ~2 km above
-- the ground): nothing below ~0.7, 7 % at 0.79, 19 % at 0.90; looking 20 deg up, a band of small
-- cumulus at 0.7-0.95. Rows above 0.19 are guesses (overcast was never seen up to 0.95; the stock
-- UI goes to 3, BWM_Weather to 2.9): retune here.
M.COVERAGE_CURVE = {
  { 0.00, 0.00 },
  { 0.02, 0.70 },
  { 0.07, 0.79 },
  { 0.19, 0.90 },
  { 0.50, 1.10 },   -- uncalibrated
  { 0.80, 1.50 },   -- uncalibrated
  { 1.00, 2.00 },   -- uncalibrated
}
-- Cirrus: invisible while cirrusDensity is 0 (the CloudLayer default); 0.5 density + 0.5 coverage
-- gave faint streaks in the probe.
M.CIRRUS_DENSITY_MIN, M.CIRRUS_DENSITY_MAX = 0.3, 0.6
-- Fog: extinction sigma = 3.912 / visibility (Koschmieder, 2 % contrast); the engine's density at
-- z = 0 is sigma * exp(groundZ / height), so the player's ground gets sigma.
M.FOG_K = 3.912
M.FOG_VIS_MIN, M.FOG_VIS_MAX = 50, 100000
M.FOG_VIS_DEFAULT = 60000  -- no visibility in the data
M.FOG_HEIGHT = 1000        -- fogAtmosphereHeight, m (the menu's range is 1-1000)
M.FOG_HEIGHT_FOG = 500     -- with fog codes; lower makes valleys below the player opaque
M.FOG_FOLLOW_GROUND = true
M.FOG_DENSITY_MAX = 0.05   -- the menu's maximum (50 / 1000)
-- High above the ground (the free camera, a plane): the engine's fog is a height fog over the flat world's z, ever denser
-- below z = 0, and our ground is the curved Earth's, kilometres under z = 0 a few hundred km out (7 km at 300 km): from
-- 100 km up everything past ~150 km was one white-grey wall (2026-11-06, the user: "improve how the earth looks at
-- extremely high altitudes"). The fog fades out between FOG_FADE_FROM_M and FOG_FADE_TO_M of the camera over the
-- ground; above, the sky's own atmosphere (ScatterSky, physical, curved: horizon.lua) draws the haze.
M.FOG_FADE_FROM_M = 1500
M.FOG_FADE_TO_M = 6000
-- ... and the cloud layer goes above CLOUD_HIDE_M (back below CLOUD_SHOW_M): far over the clouds the engine's
-- volumetric clouds composite a stale picture of themselves into the sky (the same clouds at the same place on screen
-- whichever way the camera looks, Nice from 15 and 100 km, 2026-11-06)
M.CLOUD_HIDE_M = 12000
M.CLOUD_SHOW_M = 10000
-- Clouds drift with the wind aloft; CloudLayer.windSpeed is a texture-scroll scalar (levels 0.1-0.2).
M.CLOUD_WIND_SCALE, M.CLOUD_WIND_MIN, M.CLOUD_WIND_MAX = 0.02, 0.02, 0.5
M.CLOUD_ABOVE_MIN_KM, M.CLOUD_ABOVE_MAX_KM = 0.3, 6
-- Rain/snow fraction of the drop pool by what the eye sees, not the rate (the weather plan 4.3 A4: drizzle and light
-- rain, the most common rain, were 8-35 % of an unlit pool and read as dry): linear in log(rate) between the rows
-- { rate, share }; none at or below PRECIP_OFF. Rain in mm/h, snow (and hail) in cm/h.
M.RAIN_CURVE = { { 0.05, 0.15 }, { 0.1, 0.25 }, { 1, 0.45 }, { 4, 0.75 }, { 10, 1.0 } }
M.SNOW_CURVE = { { 0.03, 0.15 }, { 0.1, 0.3 }, { 0.5, 0.55 }, { 1.5, 0.8 }, { 3, 1.0 } }
M.PRECIP_OFF = 0.02
M.PRECIP_MIN = 0.15        -- the least share while anything falls (the curves' first rows)
M.RAIN_NAME = 'bngEarthRain'      -- level objects (tools/make_level_assets.py)
M.SNOW_NAME = 'bngEarthSnow'
M.RAIN_POOL, M.SNOW_POOL = 4000, 3000                       -- drops (probe: 9000 cost +0.2 ms)
-- "Rain visibility" (the settings, decision 6 of the plan: real light rain is nearly invisible by day, games
-- exaggerate it): the pools times this; the default a little above real
M.RAIN_VISIBILITY = 1.3
M.POOL_MAX = 12000
-- Rain and snow in the air thicken the fog (the weather plan 4.3 A4; real visibility in rain: ~12 km at 1 mm/h, 4 km at
-- 5, 1.4 km at 25; in snow ~1 km at 1 cm/h, under 0.5 km at 3): extinction per km = k * rate^p (rate as above); the
-- larger of this and the visibility's own sigma counts (the model's visibility may hold the rain already).
M.RAIN_SIGMA_K, M.RAIN_SIGMA_P = 0.33, 0.67
M.SNOW_SIGMA_K, M.SNOW_SIGMA_P = 3.9, 0.8
-- Grey fog (the "sandstorm" look: the lit fog takes the low sun's colour, the LA storm shots of the study): how grey
-- the fog is, 0..1, from the rain (FOG_GREY_RAIN_MMH gives 1), the snow, the fog codes and an overcast deck.
-- (rain: full grey at FOG_GREY_RAIN_MMH; 2 gave light rain the storm's thick milky haze, unlike the camera-matched street
-- photos of 1-2 mm/h rain, where only buildings 300-500 m off fade: phase B, wb11)
M.FOG_GREY_RAIN_MMH, M.FOG_GREY_SNOW_CMH, M.FOG_GREY_DECK, M.FOG_GREY_FOG = 6, 0.5, 0.25, 0.8
-- The fog cap (the settings' "Thickest fog"; players found real fog too thick, the plan's B11): the least visibility
-- in m, 0 = none (the real fog down to FOG_VIS_MIN)
M.FOG_LIMIT_M = 0
-- Snow flakes by temperature (plan 4.3 A5): big slow wet flakes near 0 C, small fast dry ones below -8 C, and hail
-- (small white pellets, fast, no drift). Fields of the snow object, set only when the kind changes (each is a
-- postApply: the flakes are made again). Sizes in m (a real flake is 1-3 cm, a wet clump up to 5: drawn larger so they
-- read on screen), speeds in the engine's fall-speed units, mass against the wind (heavier drifts less).
M.FLAKES = {
  wet = { dropSize = 0.11, minSpeed = 0.9, maxSpeed = 1.3, minMass = 1.0, maxMass = 1.4, maxTurbulence = 0.25, turbulenceSpeed = 0.15 },
  snow = { dropSize = 0.08, minSpeed = 0.7, maxSpeed = 1.1, minMass = 0.75, maxMass = 1.0, maxTurbulence = 0.45, turbulenceSpeed = 0.2 },
  dry = { dropSize = 0.065, minSpeed = 0.9, maxSpeed = 1.5, minMass = 0.5, maxMass = 0.8, maxTurbulence = 0.6, turbulenceSpeed = 0.3 },
  hail = { dropSize = 0.035, minSpeed = 6.0, maxSpeed = 8.0, minMass = 5.0, maxMass = 6.0, maxTurbulence = 0, turbulenceSpeed = 0 },
}
M.FLAKE_WET_C, M.FLAKE_DRY_C = -1, -8
-- Shelter (plan 4.3 A1): rain and snow fade out under a roof over FADE_OUT_S and come back over FADE_IN_S; checked every
-- SHELTER_S while anything falls: a road tile's roofed space or under-bridge box (ambience.shelterAt), or the SHELTER_RAYS
-- straight up from the camera (and SHELTER_SIDE_M to either side of it) all hitting within SHELTER_RAY_M.
M.SHELTER_S, M.SHELTER_RAY_M, M.SHELTER_SIDE_M = 0.25, 60, 3
M.SHELTER_FADE_OUT_S, M.SHELTER_FADE_IN_S = 0.5, 1.0
M.PRECIP_S = 0.25          -- the drops' share written at 4 Hz while anything falls (the fades)
M.TRAFFIC_S = 1.0          -- traffic's headlights looked at this often
M.GUSTS = true
M.GUST_TAU_S, M.GUST_STD = 4, 0.4
-- The gust signal u (-1..1): above 0 the speed rises towards the gust speed, below 0 it falls to a
-- lull, at u = -1 to (1 - GUST_LULL) of the mean.
M.GUST_LULL = 0.5
-- The direction swings with its own slower signal; its spread grows with the gustiness
-- (gust / mean): DIR_STD_MIN at steady wind, + DIR_GUSTINESS_DEG per unit of gust factor above 1,
-- at most DIR_STD_MAX (real gusty wind veers 10-30 degrees). None below DIR_CALM_MS.
M.DIR_TAU_S = 8
M.DIR_STD_MIN_DEG, M.DIR_STD_MAX_DEG, M.DIR_GUSTINESS_DEG = 4, 25, 15
M.DIR_CALM_MS = 0.5
M.WIND_EPS_MS = 0.15
-- Sun under the deck: brightness factor 1 - SUN_DIM_MAX * deck^SUN_DIM_EXP (a few clouds barely
-- matter, overcast leaves 0.3; probe at noon: 0.35 made the car's shadow faint).
M.SUN_DIM_MAX, M.SUN_DIM_EXP = 0.7, 1.5
M.SUN_EPS = 0.02           -- write only when the factor moved this much (each write is a postApply)
-- Storm light (the weather plan's item 6, phase B; phase A's broker shots: a noon thunderstorm looked like a sunny day
-- in grey haze, the auto exposure undoing every dimming of the sun and the sky): the gloom (0..1) of a thick deck, rain
-- and thunder lowers the camera's exposure through the stock HDR's own bias (PostEffectLocalExposureObject's
-- exposureBiasEV: the auto exposure adapts towards its biased target, so the offset holds; the player's own
-- GraphicEVCompensation stays underneath) and the direct sun further (no sharp shadows under a storm deck). By day the
-- full STORM_EV_MAX; at night (the eye adapted to the dark, the lamps the light) a share of it, NIGHT_EV_SHARE.
-- gloom = DECK * deck^2 + RAIN * sqrt(min(1, rain / GLOOM_RAIN_MMH)) + THUNDER * class / 3
M.GLOOM_DECK, M.GLOOM_RAIN, M.GLOOM_THUNDER, M.GLOOM_RAIN_MMH = 0.15, 0.3, 0.35, 8   -- (the deck alone little: overcast photos are bright, refs)
M.STORM_EV_MAX = 2.0       -- EV at gloom 1 by day (overcast ~-0.6, rain ~-1, a thunderstorm ~-1.6)
M.NIGHT_EV_SHARE = 0.35
M.GLOOM_SUN = 0.85         -- the direct sun's brightness factor times (1 - GLOOM_SUN * gloom)
M.EV_EPS = 0.03            -- EV: written when it moved this much
M.EV_POSTAPPLY = false
M.STORM_LIGHT = true       -- the settings' "Storm light" (off: none of the above)
M.LIGHTNING = true         -- the settings' "Lightning and thunder"
M.STREAKS = false          -- the settings' "Rain streaks" (rainstreaks.lua): off by default (real rain hardly shows as streaks by day:
                           -- the reference photos, ~/bnge-tests/weather/b/refs; one shell costs ~1.5 ms a frame on the Deck)
M.WET_LOOK = true          -- the settings' "Wet look" (wetlook.lua)
M.GRAVITY_ENABLED = true
M.GRAVITY_EPS = 1e-4       -- m/s^2; ~30 m of climb (setGravity queues Lua into every vehicle)
M.GRAVITY_CHECK_S = 1
M.GRAVITY_DEFAULT = 9.81   -- the level's value, restored when switched off
M.GRAVITY_STEP = 1e-3      -- m/s^2 per check at most (100 mGal/s) once ours is set: a smooth approach
M.FREE_AIR = 3.086e-6      -- m/s^2 per m of height (normal free-air gradient)

-- core_environment's onEnvironmentChanged keys that are real weather -> the core_environment getter
-- of the engine's value (the hook fires before a value is applied) and the tolerance within which
-- an incoming value is no change. Vectors by component; fogDensity relative, in either scale
-- (setState carries the menu's x1000, setFogDensity the engine's); numOfDrops is no engine field, so
-- getPrecipitation may have nothing to compare with (a change). The menu's sliders (0.39
-- EnvWeather.vue) send their value rounded to their step when the number field loses focus, unedited:
-- a tolerance is at least half a step (cover 0.01, cloud speed 0.1, direction 5 deg, fog 0.005 x1000),
-- and below one step.
M.FOG_STEP_HALF = 2.6e-6   -- the fog tolerance never goes below half the menu's step (engine scale)
M.WEATHER_KEYS = {
  cloudCover = { 'getCloudCover', 0.006 },
  cloudCirrusCoverage = { 'getCloudCirrusCoverage', 0.01 },
  cloudCirrusDensity = { 'getCloudCirrusDensity', 0.01 },
  cloudCirrusAltitudeKm = { 'getCloudCirrusAltitudeKm', 0.05 },
  cloudWindDirection = { 'getCloudWindDirection', 0.045 }, -- unit vector: ~2.6 deg
  windSpeed = { 'getWindSpeed', 0.051 },
  fogDensity = { 'getFogDensity', 0.02 },
  fogAtmosphereHeight = { 'getFogAtmosphereHeight', 1 },
  groundWind = { 'getGroundWind', 0.2 },
  cloudAltitudeKm = { 'getCloudAltitudeKm', 0.05 },
  numOfDrops = { 'getPrecipitation', 0.5 },
}

------------------------------------------------------------------------------------------
-- pure

local function clamp(v, lo, hi) return math.max(lo, math.min(hi, v)) end

-- Sky fraction (0..1) -> CloudLayer.coverage over COVERAGE_CURVE.
function M.coverageFor(f, curve)
  curve = curve or M.COVERAGE_CURVE
  f = tonumber(f) or 0
  if f <= curve[1][1] then return curve[1][2] end
  for i = 2, #curve do
    local a, b = curve[i - 1], curve[i]
    if f <= b[1] then return a[2] + (b[2] - a[2]) * (f - a[1]) / (b[1] - a[1]) end
  end
  return curve[#curve][2]
end

-- Sky fraction of the deck (0..1) -> the sun's brightness factor.
function M.sunDimFor(deck)
  return 1 - M.SUN_DIM_MAX * clamp(tonumber(deck) or 0, 0, 1) ^ M.SUN_DIM_EXP
end

-- High cloud cover (%) -> cirrusCoverage, cirrusDensity.
function M.cirrusFor(highPct)
  local c = clamp((tonumber(highPct) or 0) / 100, 0, 1)
  if c <= 0 then return 0, 0 end
  return c, M.CIRRUS_DENSITY_MIN + (M.CIRRUS_DENSITY_MAX - M.CIRRUS_DENSITY_MIN) * c
end

-- Visibility (m, nil = unknown) -> extinction per metre.
function M.fogSigma(visM)
  local v = tonumber(visM) or M.FOG_VIS_DEFAULT
  return M.FOG_K / clamp(v, M.FOG_VIS_MIN, M.FOG_VIS_MAX)
end

-- The engine's fogDensity that gives extinction sigma at height groundZ (nil = no compensation).
function M.fogDensityFor(sigma, groundZ, height)
  local d = sigma
  if M.FOG_FOLLOW_GROUND and groundZ then d = sigma * math.exp(groundZ / height) end
  return math.min(d, M.FOG_DENSITY_MAX)
end

-- The fog a fixed view distance needs (the settings' viewDistanceMode 'fixed'): the engine draws nothing past it, so
-- the scene is VIEW_FOG_DEPTH optical depths into the fog there (exp(-3): ~95 %), not cut off against a clear sky; 0
-- with the real horizon. 1 km: 0.003; 20 km: 1.5e-4; from 75 km the level's own 4e-5 is more.
M.VIEW_FOG_DEPTH = 3.0
-- The engine's density for that (groundZ, fogHeight: as fogDensityFor, the engine thins its fog with height: 2 km up
-- under a 1 km fog height a raw 3 / D would give 0.4 optical depths, Codex 2026-11-14), at most FOG_DENSITY_MAX.
function M.viewFogFloor(get, groundZ, fogHeight)
  get = get or (function(k)
    local st = rawget(_G, 'beamngEarth_settings')
    return st and st.get and st.get(k)
  end)
  if get('viewDistanceMode') ~= 'fixed' then return 0 end
  local km = tonumber(get('viewDistanceKm'))
  if not km or km <= 0 then return 0 end
  return M.fogDensityFor(M.VIEW_FOG_DEPTH / (km * 1000), groundZ, tonumber(fogHeight) or 1000)
end

-- Pure: the share of the fog kept with the camera h m over the ground (1 low down, 0 high up; FOG_FADE_*).
function M.fogAltitudeShare(h)
  h = tonumber(h)
  if not h then return 1 end
  return clamp(1 - (h - M.FOG_FADE_FROM_M) / (M.FOG_FADE_TO_M - M.FOG_FADE_FROM_M), 0, 1)
end

-- Pure: whether the cloud layer is hidden with the camera h m over the ground, given whether it is now (hysteresis).
function M.cloudsHiddenAt(h, hiddenNow)
  h = tonumber(h)
  if not h then return hiddenNow or false end
  if hiddenNow then return h > M.CLOUD_SHOW_M end
  return h > M.CLOUD_HIDE_M
end


-- Ground wind: meteorological "from" direction (deg clockwise from north) and speed -> the world
-- vector the air moves along (ENU: +x east, +y north; the UI's x = s sin(dir), y = s cos(dir) with
-- dir = towards). A west wind (from 270) blows towards +x.
function M.windVec(ms, fromDeg)
  local t = math.rad(tonumber(fromDeg) or 0)
  ms = tonumber(ms) or 0
  return -ms * math.sin(t), -ms * math.cos(t)
end

-- Unit vector the clouds drift towards.
function M.cloudDir(fromDeg)
  return M.windVec(1, fromDeg)
end

-- Rate -> fraction of the drop pool over curve (M.RAIN_CURVE / M.SNOW_CURVE): linear in log(rate) between its rows,
-- its first share down to PRECIP_OFF and nothing at or below, its last share above.
function M.precipPct(rate, curve)
  rate = tonumber(rate) or 0
  curve = curve or M.RAIN_CURVE
  if rate <= M.PRECIP_OFF then return 0 end
  if rate <= curve[1][1] then return curve[1][2] end
  for i = 2, #curve do
    local a, b = curve[i - 1], curve[i]
    if rate <= b[1] then
      return a[2] + (b[2] - a[2]) * math.log(rate / a[1]) / math.log(b[1] / a[1])
    end
  end
  return curve[#curve][2]
end

-- Extinction per metre of rain (mm/h) and snow (cm/h) in the air.
function M.rainSigma(mmh)
  mmh = tonumber(mmh) or 0
  if mmh <= M.PRECIP_OFF then return 0 end
  return M.RAIN_SIGMA_K * mmh ^ M.RAIN_SIGMA_P / 1000
end
function M.snowSigma(cmh)
  cmh = tonumber(cmh) or 0
  if cmh <= M.PRECIP_OFF then return 0 end
  return M.SNOW_SIGMA_K * cmh ^ M.SNOW_SIGMA_P / 1000
end

-- How grey the fog is (0..1) for rain (mm/h), snow (cm/h), the deck's sky fraction (0..1) and a fog code.
function M.fogGreyFor(rain, snow, deck, fog)
  local g = math.max(clamp((tonumber(rain) or 0) / M.FOG_GREY_RAIN_MMH, 0, 1),
    clamp((tonumber(snow) or 0) / M.FOG_GREY_SNOW_CMH, 0, 1),
    M.FOG_GREY_DECK * clamp(tonumber(deck) or 0, 0, 1) ^ 2,
    fog and M.FOG_GREY_FOG or 0)
  return clamp(g, 0, 1)
end

-- Pure: the thunder class (0..3) of a weather message (storm.lua's: the WMO code, the lightning potential, CAPE with
-- rain), none in a message scaled below THUNDER_MIN_K (scaleMsg's noThunder).
M.THUNDER_MIN_K = 0.25
local storm = require('beamngEarth/storm')
function M.thunderOf(msg)
  if type(msg) ~= 'table' or msg.noThunder then return 0 end
  return storm.thunderClass(msg)
end

-- Pure: the gloom (0..1) of a deck's sky fraction (0..1), rain (mm/h) and a thunder class (0..3).
function M.gloomFor(deck, rain, thunder)
  local d = clamp(tonumber(deck) or 0, 0, 1)
  local r = clamp((tonumber(rain) or 0) / M.GLOOM_RAIN_MMH, 0, 1)
  return clamp(M.GLOOM_DECK * d * d + M.GLOOM_RAIN * math.sqrt(r) + M.GLOOM_THUNDER * clamp(tonumber(thunder) or 0, 0, 3) / 3,
    0, 1)
end

-- Pure: the daylight share (0 night .. 1 day) of core_environment's time of day (0 = noon, 0.5 = midnight).
function M.dayShare(tod)
  tod = tonumber(tod)
  if not tod then return 1 end
  return clamp(0.5 + 1.5 * math.cos(2 * math.pi * tod), 0, 1)
end

-- Pure: the exposure offset (EV, <= 0) for a gloom and a daylight share.
function M.stormEV(gloom, day)
  local d = clamp(tonumber(day) or 1, 0, 1)
  return -M.STORM_EV_MAX * clamp(tonumber(gloom) or 0, 0, 1) * (M.NIGHT_EV_SHARE + (1 - M.NIGHT_EV_SHARE) * d)
end

-- The snow object's flake kind for the temperature (C) and hail.
function M.flakeKind(tC, hail)
  if hail then return 'hail' end
  tC = tonumber(tC) or 0
  if tC >= M.FLAKE_WET_C then return 'wet' end
  if tC < M.FLAKE_DRY_C then return 'dry' end
  return 'snow'
end

-- Pure: the next shelter fade (1 = in the open, 0 = under a roof) after dt seconds, sheltered or not.
function M.shelterStep(f, sheltered, dt)
  f = tonumber(f) or 1
  dt = math.max(0, tonumber(dt) or 0)
  if sheltered then return math.max(0, f - dt / M.SHELTER_FADE_OUT_S) end
  return math.min(1, f + dt / M.SHELTER_FADE_IN_S)
end

-- Pure: why rain and snow are not drawn now, or nil when they are. s = { mode, show (the setting), map3d, bigMap,
-- mission, aboveClouds }. The one owner of their visibility (the plan's 4.3 A7: stuck-on rain came from several paths
-- each showing or hiding the objects on their own).
function M.precipBlock(s)
  if s.mode == 'manual' then return 'off' end
  if s.show == false then return 'hidden by the setting' end
  if s.map3d then return '3D map' end
  if s.bigMap then return 'big map' end
  if s.mission then return 'mission' end
  if s.aboveClouds then return 'above the clouds' end
  return nil
end

-- The drop pool for a base pool and the rain visibility.
function M.poolFor(base, vis)
  return math.floor(clamp(base * (tonumber(vis) or 1), 0, M.POOL_MAX) + 0.5)
end

-- Normal gravity (WGS84 Somigliana) at geodetic latitude latDeg, minus the free-air gradient for
-- height h (m above the ellipsoid ~ sea level ~ our z). m/s^2, positive.
function M.gravityAt(latDeg, h)
  local s = math.sin(math.rad(tonumber(latDeg) or 0))
  local s2 = s * s
  return 9.7803253359 * (1 + 0.00193185265241 * s2) / math.sqrt(1 - 0.00669437999013 * s2) - M.FREE_AIR * (tonumber(h) or 0)
end

-- The server's gravity msg (g at height msg.h) carried to height h by the free-air gradient.
function M.gravityFrom(msg, h)
  return msg.g - M.FREE_AIR * ((tonumber(h) or 0) - (tonumber(msg.h) or 0))
end

-- The next value to write from have towards want: at most step away from have (no step limit
-- without a previous value of ours).
function M.gravityToward(have, want, step)
  if not have then return want end
  return have + math.max(-step, math.min(step, want - have))
end

-- One step of the gust process (Ornstein-Uhlenbeck around 0, std GUST_STD, time constant tau);
-- n is a unit-variance random number. The speed is ms + (gust - ms) * max(0, u).
function M.gustStep(u, dt, tau, n)
  local s = M.GUST_STD * math.sqrt(2 / tau)
  return clamp(u - u * dt / tau + s * math.sqrt(dt) * n, -1, 1)
end

-- Wind speed for the gust signal u.
function M.gustSpeed(ms, gustMs, u)
  if u >= 0 then return ms + (math.max(ms, gustMs) - ms) * u end
  return ms * (1 + M.GUST_LULL * math.max(u, -1))
end

-- Spread (1 sigma, degrees) of the direction swings.
function M.dirStdDeg(ms, gustMs)
  if ms < M.DIR_CALM_MS then return 0 end
  return clamp(M.DIR_STD_MIN_DEG + M.DIR_GUSTINESS_DEG * (math.max(ms, gustMs) / ms - 1), M.DIR_STD_MIN_DEG, M.DIR_STD_MAX_DEG)
end

-- Hail in a message: its own rate (cm/h of pellets, the presets), or the thunderstorm-with-hail codes 96 / 99 while it
-- rains (Open-Meteo has those only over central Europe).
M.HAIL_CODE_CMH = 0.5
-- (hailCmH: the presets' own field, no world server message has it: m, not msg, for the protocol check)
function M.hailRate(m)
  local h = tonumber(m.hailCmH)
  if h then return h end
  local code = tonumber(m.code)
  if (code == 96 or code == 99) and (tonumber(m.rainMmH) or 0) > M.PRECIP_OFF then return M.HAIL_CODE_CMH end
  return 0
end

-- The fog's extinction for a message: the visibility's, or the rain's / snow's when thicker; at most the fog cap's
-- (limitM: the least visibility, 0 or nil none).
function M.fogSigmaFor(msg, limitM)
  local s = math.max(M.fogSigma(msg.visM), M.rainSigma(msg.rainMmH), M.snowSigma(msg.snowCmH))
  limitM = tonumber(limitM) or 0
  if limitM > 0 then s = math.min(s, M.FOG_K / limitM) end
  return s
end

-- The weather message -> the targets every value moves towards (limitM: the fog cap, M.FOG_LIMIT_M by default).
function M.targets(msg, limitM)
  local cover, wind = msg.cover or {}, msg.wind or {}
  local aloft = msg.aloft or wind
  local deck = math.max(tonumber(cover.low) or 0, tonumber(cover.mid) or 0) / 100
  local cc, cd = M.cirrusFor(cover.high)
  local wx, wy = M.cloudDir(wind.fromDeg)
  local cx, cy = M.cloudDir(aloft.fromDeg)
  local ms = tonumber(wind.ms) or 0
  local hail = M.hailRate(msg)
  local snow = tonumber(msg.snowCmH) or 0
  local tempC = tonumber(msg.slTempC) or 15
  return {
    coverage = M.coverageFor(deck),
    sunDim = M.sunDimFor(deck),
    cirrusCoverage = cc, cirrusDensity = cd,
    cloudAboveKm = clamp((tonumber(msg.baseAglM) or 1800) / 1000, M.CLOUD_ABOVE_MIN_KM, M.CLOUD_ABOVE_MAX_KM),
    cloudDirX = cx, cloudDirY = cy,
    cloudWindSpeed = clamp((tonumber(aloft.ms) or 0) * M.CLOUD_WIND_SCALE, M.CLOUD_WIND_MIN, M.CLOUD_WIND_MAX),
    fogSigma = M.fogSigmaFor(msg, limitM == nil and M.FOG_LIMIT_M or limitM),
    fogHeight = msg.fog and M.FOG_HEIGHT_FOG or M.FOG_HEIGHT,
    fogGrey = M.fogGreyFor(msg.rainMmH, math.max(snow, hail), deck, msg.fog),
    -- (phase B: the storm light, lightning, the wet look; no thunder under THUNDER_MIN_K intensity: scaleMsg)
    thunder = M.thunderOf(msg),
    gloom = M.gloomFor(deck, msg.rainMmH, M.thunderOf(msg)),
    rainMmH = tonumber(msg.rainMmH) or 0,
    windMs = ms, gustMs = math.max(ms, tonumber(wind.gustMs) or ms),
    windDirX = wx, windDirY = wy,
    rainPct = M.precipPct(msg.rainMmH, M.RAIN_CURVE),
    -- (hail falls through the snow object, as white pellets: flakeKind)
    snowPct = M.precipPct(math.max(snow, hail), M.SNOW_CURVE),
    flake = M.flakeKind(tempC, hail > snow),
    tempC = tempC,
  }
end

-- Exponential approach of every numeric field of cur towards tgt: 95 % after T seconds (T <= 0: at once). Other fields
-- (the flake kind) are taken as they are.
function M.step(cur, tgt, dt, T)
  local a = (not T or T <= 0) and 1 or (1 - math.exp(-3 * dt / T))
  for k, v in pairs(tgt) do
    local c = cur[k]
    cur[k] = (type(c) == 'number') and (c + (v - c) * a) or v
  end
  return cur
end

-- x, y of a vector (vec3 cdata or the menu's { x, y, z } table), or nil.
local function xyOf(v)
  local t = type(v)
  if t ~= 'table' and t ~= 'cdata' and t ~= 'userdata' then return nil end
  local ok, x, y = pcall(function() return tonumber(v.x), tonumber(v.y) end)
  if ok and x and y then return x, y end
end

-- Does the incoming value v of key differ from the engine's value have by more than tol?
function M.valueDiffers(key, v, have, tol)
  local vx, vy = xyOf(v)
  if vx then
    local hx, hy = xyOf(have)
    return not hx or math.abs(vx - hx) > tol or math.abs(vy - hy) > tol
  end
  v, have = tonumber(v), tonumber(have)
  if v == nil then return false end -- nothing the setter would apply
  if have == nil then return true end
  if key == 'fogDensity' then
    local t = math.max(tol * have, M.FOG_STEP_HALF)
    return math.abs(v - have) > t and math.abs(v / 1000 - have) > t
  end
  return math.abs(v - have) > tol
end

-- The real-weather keys of an onEnvironmentChanged table whose values really change, sorted. e:
-- core_environment (its getters); ref: values to compare with instead of the engine's (key -> value).
function M.changedKeys(changes, e, ref)
  local out = {}
  for k, v in pairs(changes) do
    local spec = M.WEATHER_KEYS[k]
    if spec then
      local have = ref and ref[k]
      if have == nil and e and type(e[spec[1]]) == 'function' then
        local ok, h = pcall(e[spec[1]])
        if ok then have = h end
      end
      if M.valueDiffers(k, v, have, spec[2]) then out[#out + 1] = k end
    end
  end
  table.sort(out)
  return out
end

-- BWM_Weather (the user's weather mod) changes the weather itself: automatic weather, or a
-- preset other than clear (its getWeatherState()). Named in the toast of a switch to manual.
function M.bwmFighting(state)
  if type(state) ~= 'table' then return false end
  if state.automaticWeather == true then return true end
  for _, k in ipairs({ 'targetPreset', 'currentPreset' }) do
    local p = state[k]
    if p ~= nil and p ~= '' and p ~= 'clear' then return true end
  end
  return false
end

local CODES = { [0] = 'clear', [1] = 'mainly clear', [2] = 'partly cloudy', [3] = 'overcast', [45] = 'fog', [48] = 'fog',
  [56] = 'freezing drizzle', [57] = 'freezing drizzle', [66] = 'freezing rain', [67] = 'freezing rain', [68] = 'sleet',
  [69] = 'sleet', [96] = 'thunderstorm with hail', [99] = 'thunderstorm with hail' }
function M.describe(code)
  code = tonumber(code) or -1
  if CODES[code] then return CODES[code] end
  if code >= 95 then return 'thunderstorm' elseif code >= 85 then return 'snow showers'
  elseif code >= 80 then return 'showers' elseif code >= 71 and code <= 77 then return 'snow'
  elseif code >= 61 and code <= 67 then return 'rain' elseif code >= 51 and code <= 57 then return 'drizzle' end
  return '?'
end

-- The presets (the Weather page, the radial menu and a BeamMP server's G.WEATHER, through mpsession): each a weather
-- message like the world server's real one (WMO code, cloud cover %, wind m/s, visibility m, rain mm/h, snow cm/h,
-- hail cm/h, sea-level temperature C), so every player with the same preset gets the same weather. The six of the
-- first BeamMP plugin (clear, cloudy, rain, storm, snow, fog) keep their names. M.PRESET_ORDER: the page's order.
M.PRESETS = {
  clear = { code = 0, cover = { low = 0, mid = 0, high = 5 }, wind = { ms = 2, fromDeg = 270 }, visM = 40000,
    slTempC = 20 },
  fewclouds = { code = 1, cover = { low = 20, mid = 10, high = 25 }, wind = { ms = 3, fromDeg = 260 }, visM = 35000,
    slTempC = 19 },
  cloudy = { code = 2, cover = { low = 55, mid = 45, high = 40 }, wind = { ms = 4, fromDeg = 250 }, visM = 25000,
    slTempC = 16 },
  overcast = { code = 3, cover = { low = 100, mid = 95, high = 60 }, wind = { ms = 4, gustMs = 7, fromDeg = 240 },
    visM = 15000, slTempC = 12 },
  drizzle = { code = 53, cover = { low = 100, mid = 85, high = 40 }, wind = { ms = 3, gustMs = 5, fromDeg = 230 },
    visM = 7000, rainMmH = 0.3, slTempC = 10 },
  rain = { code = 63, cover = { low = 100, mid = 90, high = 60 }, wind = { ms = 6, gustMs = 10, fromDeg = 230 },
    visM = 8000, rainMmH = 4, slTempC = 11 },
  heavyrain = { code = 65, cover = { low = 100, mid = 100, high = 70 }, wind = { ms = 8, gustMs = 14, fromDeg = 225 },
    visM = 3500, rainMmH = 10, slTempC = 13 },
  storm = { code = 95, cover = { low = 100, mid = 100, high = 80 }, wind = { ms = 13, gustMs = 22, fromDeg = 220 },
    visM = 3000, rainMmH = 15, slTempC = 17 },
  fog = { code = 45, cover = { low = 100, mid = 20, high = 0 }, wind = { ms = 1, fromDeg = 180 }, visM = 300,
    fog = true, slTempC = 8 },
  lightsnow = { code = 71, cover = { low = 100, mid = 80, high = 50 }, wind = { ms = 3, gustMs = 5, fromDeg = 330 },
    visM = 8000, snowCmH = 0.3, slTempC = -2 },
  snow = { code = 73, cover = { low = 100, mid = 90, high = 60 }, wind = { ms = 5, gustMs = 8, fromDeg = 340 },
    visM = 4000, snowCmH = 1.5, slTempC = -4 },
  blizzard = { code = 75, cover = { low = 100, mid = 100, high = 80 }, wind = { ms = 15, gustMs = 25, fromDeg = 350 },
    visM = 400, snowCmH = 3, slTempC = -9 },
  sleet = { code = 68, cover = { low = 100, mid = 95, high = 60 }, wind = { ms = 6, gustMs = 10, fromDeg = 300 },
    visM = 3000, rainMmH = 1.5, snowCmH = 0.6, slTempC = 1 },
  freezingrain = { code = 67, cover = { low = 100, mid = 95, high = 60 }, wind = { ms = 4, gustMs = 7, fromDeg = 60 },
    visM = 4000, rainMmH = 2, slTempC = -2 },
  hail = { code = 96, cover = { low = 100, mid = 100, high = 80 }, wind = { ms = 10, gustMs = 18, fromDeg = 250 },
    visM = 2500, rainMmH = 6, hailCmH = 1.2, slTempC = 14 },
}
M.PRESET_ORDER = { 'clear', 'fewclouds', 'cloudy', 'overcast', 'drizzle', 'rain', 'heavyrain', 'storm', 'fog',
  'lightsnow', 'snow', 'blizzard', 'sleet', 'freezingrain', 'hail' }
M.PRESET_LABELS = { clear = 'Clear', fewclouds = 'Few clouds', cloudy = 'Cloudy', overcast = 'Overcast',
  drizzle = 'Drizzle', rain = 'Rain', heavyrain = 'Heavy rain', storm = 'Thunderstorm', fog = 'Fog',
  lightsnow = 'Light snow', snow = 'Snow', blizzard = 'Blizzard', sleet = 'Sleet', freezingrain = 'Freezing rain',
  hail = 'Hail shower' }

local function copyDeep(t)
  if type(t) ~= 'table' then return t end
  local o = {}
  for k, v in pairs(t) do o[k] = copyDeep(v) end
  return o
end

-- Pure: the weather message of preset name (nil: not one).
function M.presetMsg(name)
  local p = M.PRESETS[tostring(name or ''):lower()]
  if not p then return nil end
  local m = copyDeep(p)
  m.preset = tostring(name):lower()
  return m
end

-- Pure: a copy of a weather message at intensity k (0..1.5, the settings): its rain, snow and hail rates and wind times
-- k, its visibility divided by k (more intensity: thicker fog; 0: the clearest). Clouds stay.
M.INTENSITY_MAX = 1.5
function M.scaleMsg(msg, k)
  if type(msg) ~= 'table' then return msg end
  k = clamp(tonumber(k) or 1, 0, M.INTENSITY_MAX)
  local m = copyDeep(msg)
  if k == 1 then return m end
  for _, f in ipairs({ 'rainMmH', 'snowCmH', 'hailCmH' }) do
    if tonumber(m[f]) then m[f] = m[f] * k end
  end
  if type(m.wind) == 'table' then
    if tonumber(m.wind.ms) then m.wind.ms = m.wind.ms * k end
    if tonumber(m.wind.gustMs) then m.wind.gustMs = m.wind.gustMs * k end
  end
  local v = tonumber(m.visM) or M.FOG_VIS_DEFAULT
  m.visM = k > 0 and math.min(M.FOG_VIS_MAX, v / k) or M.FOG_VIS_MAX
  if k == 0 then m.fog = nil end
  if k < M.THUNDER_MIN_K then m.noThunder = true end
  return m
end

-- Pure: a weather message from the network ('real-host': another player's, relayed by a BeamMP server) reduced to the
-- fields the weather reads, each a number in its range (or the fog flag), so a malformed or hostile message can do no
-- more than an odd weather; nil when it is not a table. No place, time zone or anything else is kept.
local NUM_FIELDS = { code = { 0, 99 }, visM = { 0, 1e6 }, rainMmH = { 0, 500 }, snowCmH = { 0, 100 },
  hailCmH = { 0, 100 }, slTempC = { -90, 60 }, tempC = { -90, 60 }, baseAglM = { 0, 20000 }, capeJkg = { 0, 10000 },
  lpi = { 0, 1000 } }
local function inRange(v, r)
  v = tonumber(v)
  if not v or v ~= v then return nil end
  return clamp(v, r[1], r[2])
end
function M.sanitizeMsg(t)
  if type(t) ~= 'table' then return nil end
  local m = {}
  for k, r in pairs(NUM_FIELDS) do m[k] = inRange(t[k], r) end
  if type(t.cover) == 'table' then
    m.cover = { low = inRange(t.cover.low, { 0, 100 }) or 0, mid = inRange(t.cover.mid, { 0, 100 }) or 0,
      high = inRange(t.cover.high, { 0, 100 }) or 0 }
  end
  for _, k in ipairs({ 'wind', 'aloft' }) do
    if type(t[k]) == 'table' then
      m[k] = { ms = inRange(t[k].ms, { 0, 150 }) or 0, gustMs = inRange(t[k].gustMs, { 0, 200 }),
        fromDeg = inRange(t[k].fromDeg, { 0, 360 }) or 0 }
    end
  end
  m.fog = t.fog == true or nil
  return m
end

-- Pure: a copy of a real weather message with the parts the player left out (parts = { clouds, fog, wind, precip },
-- false = left out) as on a clear day: no cloud deck, the clear-air visibility, calm, dry. (The settings' separate
-- switches under Real weather: players asked to keep the real rain without the real fog.)
function M.filterReal(msg, parts)
  if type(msg) ~= 'table' then return msg end
  local m = copyDeep(msg)
  parts = parts or {}
  if parts.clouds == false then m.cover = { low = 0, mid = 0, high = 0 } end
  if parts.fog == false then m.visM, m.fog = nil, nil end
  if parts.wind == false then
    m.wind = { ms = 0, gustMs = 0, fromDeg = type(m.wind) == 'table' and m.wind.fromDeg or 0 }
  end
  if parts.precip == false then m.rainMmH, m.snowCmH, m.hailCmH = 0, 0, 0 end
  return m
end

-- Traffic in bad weather (the plan's 4.11): the weather as traffic sees it, from the message shown (not the drops on
-- screen: "Show rain and snow" is the player's view only). t = { rainMmH, snowCmH, visM (with the rain's own fog),
-- deck (0..1) } or nil (weather off).
M.TRAFFIC_RAIN_MMH, M.TRAFFIC_SNOW_CMH = 0.1, 0.05      -- falling: headlights on
M.TRAFFIC_FOG_VIS_M = 1000                              -- fog (visibility under this): headlights on
-- the speed cap's factors: rain, heavy rain (from TRAFFIC_HEAVY_MMH) or rain in fog; snow, heavy snow (W1's winter
-- grip factor comes on top of these through the same speeding.lua hook later)
M.TRAFFIC_RAIN_F, M.TRAFFIC_HEAVY_F, M.TRAFFIC_HEAVY_MMH = 0.9, 0.8, 8
M.TRAFFIC_SNOW_F, M.TRAFFIC_HEAVY_SNOW_F, M.TRAFFIC_HEAVY_CMH = 0.8, 0.65, 1.5
-- in fog: no faster than a car that stops within the visible distance (reaction TRAFFIC_REACT_S, braking
-- TRAFFIC_BRAKE_MS2), never below TRAFFIC_MIN_MS
M.TRAFFIC_REACT_S, M.TRAFFIC_BRAKE_MS2, M.TRAFFIC_MIN_MS = 1.5, 4.0, 6.7
-- street lamps and headlights before the clock says night under a dark sky (photocells switch at a light level a
-- thunderstorm reaches well before dusk): the night window widened by DUSK_EXT_H hours times the darkness
M.DUSK_EXT_H = 1.5

-- Pure: are traffic's headlights on in this weather (t as above)?
function M.trafficLightsFor(t)
  if type(t) ~= 'table' then return false end
  if (tonumber(t.rainMmH) or 0) >= M.TRAFFIC_RAIN_MMH or (tonumber(t.snowCmH) or 0) >= M.TRAFFIC_SNOW_CMH then return true end
  local v = tonumber(t.visM)
  return v ~= nil and v < M.TRAFFIC_FOG_VIS_M
end

-- Pure: the speed cap (m/s) in this weather for a cap of cap m/s.
function M.trafficCapFor(cap, t)
  cap = tonumber(cap)
  if not cap or type(t) ~= 'table' then return cap end
  local rain, snow, vis = tonumber(t.rainMmH) or 0, tonumber(t.snowCmH) or 0, tonumber(t.visM)
  local f = 1
  if rain >= M.TRAFFIC_RAIN_MMH then
    f = (rain >= M.TRAFFIC_HEAVY_MMH or (vis and vis < M.TRAFFIC_FOG_VIS_M)) and M.TRAFFIC_HEAVY_F or M.TRAFFIC_RAIN_F
  end
  if snow >= M.TRAFFIC_SNOW_CMH then
    f = math.min(f, snow >= M.TRAFFIC_HEAVY_CMH and M.TRAFFIC_HEAVY_SNOW_F or M.TRAFFIC_SNOW_F)
  end
  local out = cap * f
  if vis and vis < M.TRAFFIC_FOG_VIS_M then
    local a, tr = M.TRAFFIC_BRAKE_MS2, M.TRAFFIC_REACT_S
    local v = a * (-tr + math.sqrt(tr * tr + 2 * vis / a))      -- v * tr + v^2 / (2a) = vis
    out = math.min(out, math.max(M.TRAFFIC_MIN_MS, v))
  end
  return out
end

-- Pure: how dark the sky is for the lamps (0..1): a thick deck, darker with rain or snow.
function M.darknessFor(t)
  if type(t) ~= 'table' then return 0 end
  local deck = clamp(tonumber(t.deck) or 0, 0, 1)
  local p = clamp((tonumber(t.rainMmH) or 0) / 4 + (tonumber(t.snowCmH) or 0) / 1, 0, 1)
  return deck * deck * (0.5 + 0.5 * p)
end

-- Pure: is time t (the environment's 0..1, 0 = noon) inside the night window (s, e) widened by ext on both sides?
function M.inNightWindow(t, s, e, ext)
  t, s, e, ext = tonumber(t), tonumber(s), tonumber(e), tonumber(ext) or 0
  if not (t and s and e) or s == e then return false end
  s, e = (s - ext) % 1, (e + ext) % 1
  t = t % 1
  if s < e then return t > s and t < e end
  return t > s or t < e
end

function M.compass(deg)
  local names = { 'N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW' }
  return names[math.floor(((tonumber(deg) or 0) % 360) / 45 + 0.5) % 8 + 1]
end

------------------------------------------------------------------------------------------
-- runtime

local wxtraffic = require('beamngEarth/wxtraffic')    -- (traffic's headlights in bad weather)

local mode = 'real'
local lastMsg = nil                     -- the latest real message (the world server's), kept across level loads
local preset = nil                      -- a BeamMP server's weather (setPreset): its message, in place of the real one
local hostMsg = nil                     -- ... 'real-host': the weather the server relays from its weather host
local serverHost = false                -- ... 'real-host' and this player is the host (its own real weather is sent)
local lastGravity = nil                 -- the latest 'gravity' message (server), kept the same way
local applying = false                  -- our own core_environment calls (their hook is not a change)
-- the player's choices (the settings' Weather page: settings.lua applies them through the setters below)
local choice = {
  preset = 'rain', intensity = 1, changeS = M.LERP_S, show = true, visibility = M.RAIN_VISIBILITY,
  parts = { clouds = true, fog = true, wind = true, precip = true }, fogLimitM = M.FOG_LIMIT_M,
}
local S

-- The weather message shown now (nil: none, or Off): a BeamMP server's (its preset; 'real-host': the relayed host
-- weather, or this player's own real weather when it is the host), else the player's preset at its intensity, else the
-- real weather with the parts left out cleared, at its intensity.
local function shownMsgOf()
  if mode == 'manual' then return nil end
  if preset then
    if preset.preset ~= 'real-host' then return preset end
    if serverHost then return lastMsg end
    return hostMsg
  end
  if mode == 'preset' then return M.scaleMsg(M.presetMsg(choice.preset) or M.presetMsg('clear'), choice.intensity) end
  if not lastMsg then return nil end
  return M.scaleMsg(M.filterReal(lastMsg, choice.parts), choice.intensity)
end

-- Pure-ish: the weather as traffic sees it from a shown message (nil: none).
function M.trafficStateOf(m, limitM)
  if type(m) ~= 'table' then return nil end
  local cover = m.cover or {}
  local sigma = M.fogSigmaFor(m, limitM)
  return { rainMmH = tonumber(m.rainMmH) or 0, snowCmH = math.max(tonumber(m.snowCmH) or 0, M.hailRate(m)),
    visM = sigma > 0 and M.FOG_K / sigma or nil,
    deck = math.max(tonumber(cover.low) or 0, tonumber(cover.mid) or 0) / 100 }
end

local function resetState()
  S = {
    clock = 0, graceUntil = M.GRACE_S, quietUntil = 0, mission = false, bigMapUntil = 0,
    gravityOverride = nil, cur = nil, tgt = nil,
    applyTimer = 0, windTimer = 0, groundTimer = M.GROUND_CHECK_S, gravTimer = 0,
    groundZ = nil, gust = 0, veer = 0, gravity = nil, windSent = nil, -- windSent: our last ground wind
    temp = { at = -math.huge, value = nil, method = nil }, dirtyFrames = 0,
    precip = { [M.RAIN_NAME] = {}, [M.SNOW_NAME] = {} },
    pending = nil,  -- { at, keys }: someone else changed the weather; manual after MISSION_SETUP_S
    switched = nil, -- the last switch to manual: { at, keys, bwm }
    inOurLevel = false,
    sun = { base = nil, applied = nil }, -- the level's ScatterSky.brightness, the value we wrote
    ev = { applied = nil, ev = nil }, -- the storm light's exposure offset (writeExposure)
    shelter = 1, shelterTimer = 0, sheltered = false, shelterWhy = nil, precipTimer = 0, block = nil,
    flake = nil,    -- the snow object's flake kind applied (M.FLAKES)
    tint = nil,     -- the fog tint: { base = the sky's own values, applied = what we wrote }
    others = nil,   -- other Precipitation objects hidden over the 3D map: { id = true }
    traffic = nil,  -- the weather as traffic sees it (trafficLightsFor / trafficCapFor), nil when off
  }
  local m = shownMsgOf()
  S.tgt = m and M.targets(m, choice.fogLimitM) or nil
  S.traffic = M.trafficStateOf(m, choice.fogLimitM)
end
resetState()

-- The targets again from the message shown now (a new real message, another choice, the server's weather). With
-- nothing to show (no data yet; a relayed host weather not here yet) the targets stay.
local function retarget()
  local m = shownMsgOf()
  S.traffic = M.trafficStateOf(m, choice.fogLimitM)
  if m then S.tgt = M.targets(m, choice.fogLimitM) end
end

local function env() return rawget(_G, 'core_environment') end
local function sky() return rawget(_G, 'beamngEarth_sky') end

local function envCall(fn, ...)
  if type(fn) ~= 'function' then return false end
  applying = true
  local ok, err = pcall(fn, ...)
  applying = false
  if not ok then log('W', 'beamngEarth', 'weather: ' .. tostring(err)) end
  return ok
end

local function notify(text)
  log('W', 'beamngEarth', 'weather: ' .. text)
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then pcall(gh.trigger, 'toastrMsg', { type = 'warning', title = 'BeamNG.Earth weather', msg = text }) end
end

local function levelInfo()
  local li = scenetree.theLevelInfo
  if li then return li end
  local names = scenetree.findClassObjects('LevelInfo')
  return names and names[1] and scenetree.findObject(names[1]) or nil
end

local function playerPos()
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  return veh and veh:getPosition() or nil
end

local function groundUnder(p)
  local d = castRayStatic(vec3(p.x, p.y, p.z + 50), vec3(0, 0, -1), 5000)
  if d and d < 5000 then return p.z + 50 - d end
end

local function num(v, default) return tonumber(v) or default end

-- The values the engine has now, as the lerp's starting point (targets where unreadable).
local function readEngine(e, tgt)
  local c = {}
  for k, v in pairs(tgt) do c[k] = v end
  c.coverage = num(e.getCloudCover and e.getCloudCover(), tgt.coverage)
  c.cirrusCoverage = num(e.getCloudCirrusCoverage and e.getCloudCirrusCoverage(), tgt.cirrusCoverage)
  c.cirrusDensity = num(e.getCloudCirrusDensity and e.getCloudCirrusDensity(), tgt.cirrusDensity)
  c.cloudWindSpeed = num(e.getWindSpeed and e.getWindSpeed(), tgt.cloudWindSpeed)
  local d = e.getFogDensity and num((e.getFogDensity()), nil)
  -- (the fog a fixed view distance's floor wrote is not the weather's: the density underneath it, viewFog's base)
  if d and S.viewFogSet and S.viewFogBase and math.abs(d - S.viewFogSet) <= 1e-9 * math.max(1, d) then d = S.viewFogBase end
  local h = e.getFogAtmosphereHeight and num(e.getFogAtmosphereHeight(), nil)
  if d and h and h > 0 then
    c.fogHeight = h
    c.fogSigma = (M.FOG_FOLLOW_GROUND and S.groundZ) and d * math.exp(-S.groundZ / h) or d
  end
  local gw = e.getGroundWind and e.getGroundWind()
  if gw and gw.x then
    local l = math.sqrt(gw.x * gw.x + gw.y * gw.y)
    c.windMs = l
    if l > 0.01 then c.windDirX, c.windDirY = gw.x / l, gw.y / l end
  end
  local cw = e.getCloudWindDirection and e.getCloudWindDirection()
  if cw and cw.x and (cw.x ~= 0 or cw.y ~= 0) then c.cloudDirX, c.cloudDirY = cw.x, cw.y end
  c.rainPct, c.snowPct = 0, 0
  local sun = S.sun
  c.sunDim = (sun.applied and sun.base and sun.base > 0) and sun.applied / sun.base or 1
  return c
end

local function setIf(get, set, want, eps)
  local have = get and tonumber(get())
  if have and math.abs(have - want) <= eps then return end
  envCall(set, want)
end

-- Temperature: a flat temperatureCurveC at the sea-level temperature. core_environment re-reads
-- the curve when LevelInfo is editor-dirty (environment.lua onUpdate), so the flag is set for a few
-- frames. Which setter form the engine takes is found once per level (read back).
local function curveIs(li, tC)
  local ok, c = pcall(function() return li:getTemperatureCurveC() end)
  if not ok then return true end -- no getter: trust a setter that did not fail
  return type(c) == 'table' and type(c[1]) == 'table' and math.abs((tonumber(c[1][2]) or -999) - tC) < 0.01
end
local TEMP_METHODS = {
  table = function(li, tC) li:setTemperatureCurveC({ { 0, tC }, { 1, tC } }) end,
  field = function(li, tC) li:setField('temperatureCurveC', 0, string.format('0 %.2f 1 %.2f', tC, tC)) end,
}
local function writeTemperature(tC)
  local li = levelInfo()
  if not li then return false end
  local t = S.temp
  local order = t.method and { t.method } or { 'table', 'field' }
  for _, name in ipairs(order) do
    if pcall(TEMP_METHODS[name], li, tC) and curveIs(li, tC) then
      t.method = name
      pcall(function() li:setEditorDirty(true) end)
      S.dirtyFrames = 3
      return true
    end
  end
  t.method = 'n/a'
  return false
end

-- The level's Precipitation object of that name. (By class too: another object registered under the same name, the
-- rain sound's emitter until #1475, shadowed it, and every write went to the sound, so the rain froze at ~2.5 % and
-- could not be switched off, 1.7.0 to 1.10.1; the weather study 2026-11-24, 9 player reports.)
function M._precipObject(name)
  local o = scenetree.findObject(name)
  local okc, cls = pcall(function() return o and o:getClassName() end)
  if o and okc and cls == 'Precipitation' then return o end
  for _, nm in ipairs((scenetree.findClassObjects and scenetree.findClassObjects('Precipitation')) or {}) do
    local p = scenetree.findObject(nm)
    local okn, pn = pcall(function() return p and p:getName() end)
    if p and (nm == name or (okn and pn == name)) then return p end
  end
  return nil
end

M.PRECIP_NONE = 0.005    -- a share below this is none (a stuck tiny value kept a faint rain on)

-- The snow object's flake kind (M.FLAKES): its fields set and the flakes made again (postApply), only on a change.
local function applyFlakes(o, kind, st)
  local f = M.FLAKES[kind]
  if not f or S.flake == kind then return end
  for k, v in pairs(f) do o:setField(k, 0, tostring(v)) end
  o:postApply()
  S.flake = kind
  st.pct = nil            -- (the share written again after the object is made again)
end

-- The near fade (phase B, M.NEAR_FADE; off until the probe shows it draws on DX11 and Vulkan): our own drop vertex
-- shader (shaders/beamngEarth/precipNearV.hlsl: the stock one plus a fade from 2.5 m to 0.8 m from the camera, so a
-- drop passing the lens is no blob) as a ShaderData of our own, named in both datablocks, the objects made again.
-- Once per level, at the first drops.
M.NEAR_FADE = false
M.NEAR_SHADER = 'bngEarthPrecipNearShader'
local function setupNearFade(o)
  if not M.NEAR_FADE or S.nearFade ~= nil then return end
  S.nearFade = false
  if not scenetree.findObject(M.NEAR_SHADER) then
    local sd = createObject('ShaderData')
    if not sd then return end
    sd.DXVertexShaderFile = 'shaders/beamngEarth/precipNearV.hlsl'
    sd.DXPixelShaderFile = 'shaders/common/precipP.hlsl'
    sd.pixVersion = 5.0
    sd:registerObject(M.NEAR_SHADER)
  end
  for _, db in ipairs({ 'bngEarthRainDrop', 'bngEarthSnowflake' }) do
    local d = scenetree.findObject(db)
    if d then d:setField('dropShader', 0, M.NEAR_SHADER) end
  end
  for _, nm in ipairs({ M.RAIN_NAME, M.SNOW_NAME }) do
    local p = M._precipObject(nm)
    if p then
      local dbName = p:getField('dataBlock', 0)
      p:setField('dataBlock', 0, dbName)
      p:postApply()
    end
  end
  S.nearFade = true
  log('I', 'beamngEarth', 'weather: near fade shader in use (' .. tostring(o and o:getField('dataBlock', 0)) .. ')')
end

local function drivePrecip(name, pct, pool, flake)
  local o = M._precipObject(name)
  if not o then return end
  if pct > 0 and M.NEAR_FADE and S.nearFade == nil then pcall(setupNearFade, o) end
  if pct < M.PRECIP_NONE then pct = 0 end
  local st = S.precip[name]
  if pct > 0 and flake then applyFlakes(o, flake, st) end
  if pct > 0 and st.pool ~= pool then
    o.numDrops = pool
    o:setField('numDrops', 0, tostring(pool))
    o:postApply()
    st.pool = pool
    st.pct = nil
  end
  if st.pct == nil or math.abs(pct - st.pct) > 0.01 or (pct == 0 and st.pct ~= 0) then
    if type(o.setPercentage) == 'function' then o:setPercentage(pct) end
    st.pct = pct
  end
  -- (the object's own state, not a cached flag: a cached "hidden" kept a rain shown that should have stopped)
  local hide = pct <= 0
  local okh, cur = pcall(function() return o:isHidden() end)
  if not okh then cur = st.hidden end
  if cur ~= hide then o:setHidden(hide) end
  st.hidden = hide
end

local function cameraZ()
  local cam = rawget(_G, 'core_camera')
  if not cam or not cam.getPositionXYZ then return nil end
  local ok, _, _, z = pcall(cam.getPositionXYZ)
  return ok and tonumber(z) or nil
end

local function map3dOn()
  local m3 = rawget(_G, 'beamngEarth_map3d')
  return (m3 and m3.isActive and m3.isActive()) and true or false
end

-- Is the camera under a roof? A road tile's roofed space or under-bridge box (ambience.shelterAt, the boxes the rain
-- sound already uses), else the rays straight up from it and SHELTER_SIDE_M to either side of the view all hit
-- something static within SHELTER_RAY_M (a deck, a building's overhang, a stock level's roof; three rays, as the stock
-- traffic's tunnel check, so a pole, a wire or a sign above is no roof).
local function shelterCheck()
  local cam = rawget(_G, 'core_camera')
  local p = cam and cam.getPosition and cam.getPosition()
  if not p then return false, nil end
  local amb = package.loaded['beamngEarth/ambience']
  if amb and type(amb.shelterAt) == 'function' then
    local ok, kind = pcall(amb.shelterAt, p.x, p.y, p.z)
    if ok and kind then return true, kind end
  end
  if type(castRayStatic) ~= 'function' then return false, nil end
  local rx, ry = 1, 0
  local okr, r = pcall(function() return cam.getRight and cam.getRight() end)
  if okr and r and r.x then
    local l = math.sqrt(r.x * r.x + r.y * r.y)
    if l > 1e-3 then rx, ry = r.x / l, r.y / l end
  end
  local up = vec3(0, 0, 1)
  for _, s in ipairs({ 0, -M.SHELTER_SIDE_M, M.SHELTER_SIDE_M }) do
    local d = castRayStatic(vec3(p.x + rx * s, p.y + ry * s, p.z + 0.2), up, M.SHELTER_RAY_M)
    if not (d and d < M.SHELTER_RAY_M) then return false, nil end
  end
  return true, 'roof'
end

-- The one owner of the rain and snow objects (every PRECIP_S while they fall or are shown, dt: the time since): what
-- the weather wants (S.cur's shares) unless precipBlock says no, faded by the shelter.
local bigMapBusy       -- (below)
local function ownPrecip(e, dt)
  local c = S.cur
  local rain, snow = c and c.rainPct or 0, c and c.snowPct or 0
  local alt = e and e.getCloudAltitudeKm and tonumber(e.getCloudAltitudeKm())
  local cz = cameraZ()
  local block = M.precipBlock({ mode = mode, show = choice.show, map3d = map3dOn(), bigMap = bigMapBusy(),
    mission = S.mission, aboveClouds = (alt and cz and cz > alt * 1000) and true or false })
  S.block = block
  if block then rain, snow = 0, 0 end
  M.rainNow = rain                          -- (its sound: ambience.lua; api.weather)
  M.rainMmHNow = (not block and c and tonumber(c.rainMmH)) or 0   -- (the rain sound's layers)
  M.snowNow = snow
  local falling = rain >= M.PRECIP_NONE or snow >= M.PRECIP_NONE
  local r, sn = S.precip[M.RAIN_NAME], S.precip[M.SNOW_NAME]
  if not falling and r.hidden == true and sn.hidden == true then        -- (dry and hidden: nothing to do)
    S.sheltered, S.shelterWhy, S.shelter, M.shelter = false, nil, 1, 1
    return
  end
  if falling then
    S.sheltered, S.shelterWhy = shelterCheck()
    S.shelter = M.shelterStep(S.shelter, S.sheltered, dt)
  else
    S.sheltered, S.shelterWhy, S.shelter = false, nil, 1
  end
  M.shelter = S.shelter
  local vis = choice.visibility
  drivePrecip(M.RAIN_NAME, rain * S.shelter, M.poolFor(M.RAIN_POOL, vis))
  drivePrecip(M.SNOW_NAME, snow * S.shelter, M.poolFor(M.SNOW_POOL, vis), c and c.flake)
end
M._ownPrecip = function(e, dt) ownPrecip(e or rawget(_G, 'core_environment'), dt or M.PRECIP_S) end -- tests
M._cur = function() return S.cur end    -- (tests, probes)

-- The sun: ScatterSky.brightness = the level's own value * factor. Restored only while it still
-- holds our value (another mod may have set its own since).
local function sunObject()
  local names = scenetree.findClassObjects and scenetree.findClassObjects('ScatterSky')
  return names and names[1] and scenetree.findObject(names[1]) or nil
end
local function writeSun(factor)
  local o = sunObject()
  if not o then return end
  local sun = S.sun
  local have = tonumber(o:getField('brightness', 0))
  if not have then return end
  if not sun.base or (sun.applied and math.abs(have - sun.applied) > 1e-3) then
    sun.base = have -- first write, or someone else changed it: their value is the new base
  end
  local want = sun.base * factor
  if sun.applied and math.abs(want - sun.applied) < M.SUN_EPS * sun.base then return end
  o:setField('brightness', 0, string.format('%.4f', want))
  o:postApply()
  sun.applied = tonumber(o:getField('brightness', 0)) or want
end
local function restoreSun()
  local sun = S.sun
  if not sun.applied then return end
  local o = sunObject()
  local have = o and tonumber(o:getField('brightness', 0))
  if have and math.abs(have - sun.applied) <= 1e-3 then
    o:setField('brightness', 0, string.format('%.4f', sun.base))
    o:postApply()
  end
  sun.applied = nil
end
M._restoreSun = function() restoreSun() end -- tests

-- The storm's exposure offset (M.stormEV) on the stock HDR's bias: the player's own GraphicEVCompensation + ours. When
-- the bias holds something else than we wrote (the player changed the option: graphic.lua writes it), the player's
-- value is read again (it is always the base). Put back (to the player's value) only while it still holds ours.
local function exposureObject() return scenetree.findObject and scenetree.findObject('PostEffectLocalExposureObject') end
local function userEV()
  local st = rawget(_G, 'settings')
  local ok, v = pcall(function() return st and st.getValue and st.getValue('GraphicEVCompensation', 0) end)
  return ok and tonumber(v) or 0
end
local function writeExposure(ev)
  local o = exposureObject()
  if not o then return end
  local okh, have = pcall(function() return tonumber(o.exposureBiasEV) end)
  if not okh then return end
  -- (the engine's bias is in its exposure EV, higher = darker: the broker shots wb7, a bias of -1.6 took the metered EV from
  -- 12.0 to 9.5 and the picture got brighter; so a storm's darkening, stormEV < 0, goes in with its sign turned)
  local want = userEV() - ev
  local e = S.ev
  if e.applied and have and math.abs(have - want) <= M.EV_EPS and math.abs(have - e.applied) <= 1e-3 then return end
  if not e.applied and math.abs(ev) < M.EV_EPS then return end
  o.exposureBiasEV = want                -- (the stock hdr.lua's way; the field too, in case the property is Lua-side only)
  pcall(function() o:setField('exposureBiasEV', 0, string.format('%.3f', want)) end)
  if M.EV_POSTAPPLY then pcall(function() o:postApply() end) end      -- (probe: does the bias need it?)
  e.applied = tonumber(o.exposureBiasEV) or want
  e.ev = ev
end
local function restoreExposure()
  local e = S.ev
  if not e.applied then return end
  local o = exposureObject()
  local okh, have = pcall(function() return o and tonumber(o.exposureBiasEV) end)
  if okh and have and math.abs(have - e.applied) <= 1e-3 then
    local u = userEV()
    o.exposureBiasEV = u
    pcall(function() o:setField('exposureBiasEV', 0, string.format('%.3f', u)) end)
  end
  e.applied, e.ev = nil, nil
end
M._restoreExposure = function() restoreExposure() end -- tests

-- Grey fog in rain (the weather plan's item 5: the fog is a lit medium, shaders/common/sceneLitFog.h.hlsl, lit by the
-- sky's physical atmosphere, so under a low sun a rainstorm's fog took the sunrise's orange, the study's LA
-- "sandstorm"). M.FOG_TINT: the ScatterSky fields moved towards their rain values by the fog's greyness g (0..1):
-- { field, kind = 'color' (towards its own luminance times scale) | 'number' (towards value) | 'string' (value while
-- g >= on) | 'mul' (every component times 1 .. k) }. Written at most every apply (2 Hz) when a value moved by TINT_EPS
-- (relative), put back when g is 0 again (only while the field still holds what we wrote: someone else's is theirs).
-- Broker shots 2026-11-24/25 (LA at a 7.6 degree sun, storm): fogScale, its gradient, sunScale, ambientScale and the fog
-- fallback changed nothing visible; the atmosphere's Mie scattering and extinction x150 with a flatter phase (0.8 ->
-- 0.2) turned the orange sky and fog a pale grey (water droplets scatter all colours alike), x15 hardly, x50 partly.
-- Not over the 3D map nor high above the ground (applyFogTint's share: the same fade as the fog's).
M.FOG_TINT = {
  { field = 'atmoMieScattering', kind = 'mul', k = 150 },
  { field = 'atmoMieExtinction', kind = 'mul', k = 150 },
  { field = 'atmoMiePhaseG', kind = 'number', value = 0.2 },
}
M.TINT_EPS, M.TINT_ON = 0.05, 0.02

local function parseColor(s)
  local t = {}
  for v in tostring(s or ''):gmatch('[-%d%.eE]+') do t[#t + 1] = tonumber(v) end
  return #t >= 3 and t or nil
end

-- Pure: a lever's value at greyness g from its base (the sky's own value, a string as getField gives it).
function M.tintValue(lever, base, g)
  g = clamp(tonumber(g) or 0, 0, 1)
  if lever.kind == 'color' then
    local c = parseColor(base)
    if not c then return nil end
    local y = (0.2126 * c[1] + 0.7152 * c[2] + 0.0722 * c[3]) * (lever.scale or 1)
    local out = {}
    for i = 1, 3 do out[i] = c[i] + (y - c[i]) * g end
    return string.format('%.4f %.4f %.4f %.4f', out[1], out[2], out[3], c[4] or 1)
  elseif lever.kind == 'number' then
    local b = tonumber(base)
    if not b then return nil end
    return string.format('%.4f', b + (lever.value - b) * g)
  elseif lever.kind == 'string' then
    return g >= (lever.on or M.TINT_ON) and lever.value or base
  elseif lever.kind == 'mul' then          -- (every component times 1 .. k: the atmosphere's Point3F fields)
    local c = {}
    for v in tostring(base or ''):gmatch('[-%d%.eE]+') do c[#c + 1] = tonumber(v) end
    if #c == 0 then return nil end
    local f = 1 + ((lever.k or 1) - 1) * g
    local out = {}
    for i, v in ipairs(c) do out[i] = string.format('%.6g', v * f) end
    return table.concat(out, ' ')
  end
end

-- (relative: the atmosphere's Mie coefficients are thousandths)
local function differs(x, y) return math.abs(x - y) > M.TINT_EPS * math.max(math.abs(x), math.abs(y), 1e-3) end
local function valuesDiffer(a, b)
  local ca, cb = parseColor(a), parseColor(b)
  if ca and cb then
    for i = 1, 3 do if differs(ca[i], cb[i]) then return true end end
    return false
  end
  local na, nb = tonumber(a), tonumber(b)
  if na and nb then return differs(na, nb) end
  return tostring(a) ~= tostring(b)
end

local function restoreFogTint()
  local t = S.tint
  if not t then return end
  local o = sunObject()
  if o then
    local changed = false
    for _, lv in ipairs(M.FOG_TINT) do
      local base, ours = t.base[lv.field], t.applied[lv.field]
      if base ~= nil and ours ~= nil and not valuesDiffer(o:getField(lv.field, 0), ours) then
        o:setField(lv.field, 0, base)
        changed = true
      end
    end
    if changed then o:postApply() end
  end
  S.tint = nil
end
M._restoreFogTint = function() restoreFogTint() end -- tests

local function applyFogTint(g)
  if g < M.TINT_EPS then
    if S.tint then restoreFogTint() end
    return
  end
  local o = sunObject()
  if not o then return end
  local t = S.tint
  if not t then
    t = { base = {}, applied = {} }
    for _, lv in ipairs(M.FOG_TINT) do t.base[lv.field] = o:getField(lv.field, 0) end
    S.tint = t
  end
  local changed = false
  for _, lv in ipairs(M.FOG_TINT) do
    local have = o:getField(lv.field, 0)
    if t.base[lv.field] == nil or (t.applied[lv.field] ~= nil and valuesDiffer(have, t.applied[lv.field])) then
      t.base[lv.field] = have          -- (someone else set it since, or unread before: their value is the base)
    end
    local want = M.tintValue(lv, t.base[lv.field], g)
    if want ~= nil and (t.applied[lv.field] == nil or valuesDiffer(want, t.applied[lv.field])) then
      o:setField(lv.field, 0, want)
      t.applied[lv.field] = o:getField(lv.field, 0) or want
      changed = true
    end
  end
  if changed then o:postApply() end
end

-- The camera's height over the ground under the player (nil without one): what the fog and clouds go by.
local function cameraOverGround()
  local cam = rawget(_G, 'core_camera')
  -- (the 3D map: its camera's height over its target, not the world z: over the far side of the earth from the
  -- origin the camera lies thousands of km under z = 0, the fog took it for deep underground and the screen went grey,
  -- the user 2026-11-07: "If i rotate the globe under the horizon line while zoomed all the way out the screen turns
  -- gray")
  local m3 = rawget(_G, 'beamngEarth_map3d')
  if m3 and m3.isActive and m3.isActive() and cam and cam.getGlobalCameras then
    local g = cam.getGlobalCameras()[m3.CAMERA]
    if g and g.dist and g.pitch then return g.dist * math.sin(g.pitch) end
  end
  local p = cam and cam.getPosition and cam.getPosition()
  if not p or not S.groundZ then return nil end
  return p.z - S.groundZ
end

local cloudsHidden = false
local function hideCloudsHigh(h)
  local want = M.cloudsHiddenAt(h, cloudsHidden)
  local m3 = rawget(_G, 'beamngEarth_map3d')
  if m3 and m3.isActive and m3.isActive() then want = true end      -- (no clouds over the 3D map, the user 2026-11-07)
  if want == cloudsHidden and not want then return end
  if want == cloudsHidden then        -- (hidden: any cloud layer made since, an origin move's weather, hidden too)
    for _, nm in ipairs((scenetree.findClassObjects and scenetree.findClassObjects('CloudLayer')) or {}) do
      local o = scenetree.findObject(nm)
      if o and o.setHidden and not o:isHidden() then o:setHidden(true) end
    end
    return
  end
  local names = scenetree.findClassObjects and scenetree.findClassObjects('CloudLayer')
  for _, nm in ipairs(names or {}) do
    local o = scenetree.findObject(nm)
    if o and o.setHidden then o:setHidden(want) end
  end
  cloudsHidden = want
  log('I', 'beamngEarth', want and 'clouds hidden: the camera far above them' or 'clouds shown again')
end
M._cloudsHidden = function() return cloudsHidden end -- tests

-- No rain or snow over the 3D map either (its streaks fell across the whole view from miles up, 2026-11-07): ours
-- through ownPrecip; another mod's Precipitation objects (BWM_Weather's rain) hidden while the map is open and shown
-- again as it closes, only those this hid (a dry level's object stays as its owner left it).
local function hideOthersInMap(mapOn)
  if mapOn == (S.others ~= nil) then return end
  if mapOn then
    S.others = {}
    for _, nm in ipairs((scenetree.findClassObjects and scenetree.findClassObjects('Precipitation')) or {}) do
      local o = scenetree.findObject(nm)
      local okn, name = pcall(function() return o and o:getName() end)
      name = okn and name or nm
      if o and name ~= M.RAIN_NAME and name ~= M.SNOW_NAME and o.setHidden then
        local okh, hidden = pcall(function() return o:isHidden() end)
        if not (okh and hidden) then
          o:setHidden(true)
          S.others[o:getID()] = true
        end
      end
    end
  else
    for id in pairs(S.others) do
      local o = scenetree.findObjectById and scenetree.findObjectById(id)
      if o and o.setHidden then o:setHidden(false) end
    end
    S.others = nil
  end
end

local viewFog       -- (below: a fixed view distance's fog)
local function apply(e)
  local c = S.cur
  setIf(e.getCloudCover, e.setCloudCover, c.coverage, 0.005)
  local gloom = M.STORM_LIGHT and (c.gloom or 0) or 0
  writeSun(c.sunDim * (1 - M.GLOOM_SUN * gloom))
  -- (the storm's exposure: not over the 3D map; gloom 0 puts the player's own value back)
  if gloom > 0 and not map3dOn() then
    local tod = e.getTimeOfDay and e.getTimeOfDay()
    writeExposure(M.stormEV(gloom, M.dayShare(type(tod) == 'table' and tod.time or nil)))
  else
    restoreExposure()
  end
  setIf(e.getCloudCirrusCoverage, e.setCloudCirrusCoverage, c.cirrusCoverage, 0.005)
  setIf(e.getCloudCirrusDensity, e.setCloudCirrusDensity, c.cirrusDensity, 0.005)
  local l = math.sqrt(c.cloudDirX ^ 2 + c.cloudDirY ^ 2)
  if l > 1e-3 then
    local x, y = c.cloudDirX / l, c.cloudDirY / l
    local have = e.getCloudWindDirection and e.getCloudWindDirection()
    if not (have and have.x and (have.x * x + have.y * y) > 0.9986) then envCall(e.setCloudWindDirection, x, y, 0) end -- 3 deg
  end
  setIf(e.getWindSpeed, e.setWindSpeed, c.cloudWindSpeed, 0.005)
  local camH = cameraOverGround()
  hideCloudsHigh(camH)
  local m3 = rawget(_G, 'beamngEarth_map3d')
  local mapHigh = m3 and m3.isActive and m3.isActive() and camH and camH >= M.FOG_FADE_TO_M
  if mapHigh then
    -- (the 3D map high up: no fog at all, whatever the ground's height is known: the engine fogs by distance, and the
    -- level's own 4e-5 over thousands of km turned the whole screen grey after a view's origin move, 2026-11-07)
    local have = e.getFogDensity and tonumber((e.getFogDensity()))
    if not have or have ~= 0 then envCall(e.setFogDensity, 0) end
  elseif S.groundZ then
    local want = M.fogDensityFor(c.fogSigma, S.groundZ, c.fogHeight) * M.fogAltitudeShare(camH)
    local floor = (m3 and m3.isActive and m3.isActive()) and 0 or M.viewFogFloor(nil, S.groundZ, c.fogHeight)
    local have = e.getFogDensity and tonumber((e.getFogDensity()))
    if floor > want then        -- (a fixed view distance's fog: owned, the weather's own density its base: viewFog)
      if not have or math.abs(have - floor) > 0.02 * floor then envCall(e.setFogDensity, floor) end
      S.viewFogBase, S.viewFogSet = want, (e.getFogDensity and tonumber((e.getFogDensity()))) or floor
    else
      S.viewFogBase, S.viewFogSet = nil, nil
      if not have or math.abs(have - want) > 0.02 * want then envCall(e.setFogDensity, want) end
    end
    setIf(e.getFogAtmosphereHeight, e.setFogAtmosphereHeight, c.fogHeight, 1)
  end
  local sk = sky()
  if sk and sk.setCloudAboveKm then sk.setCloudAboveKm(c.cloudAboveKm) end
  if S.clock - S.temp.at >= M.TEMP_WRITE_S and (not S.temp.value or math.abs(S.temp.value - c.tempC) > M.TEMP_EPS_C) then
    S.temp.at = S.clock
    if writeTemperature(c.tempC) then S.temp.value = c.tempC end
  end
  -- (the haze only where the fog is: none over the 3D map, faded out high above the ground)
  applyFogTint(map3dOn() and 0 or (c.fogGrey or 0) * M.fogAltitudeShare(camH))
end

local function applyWind(e, dt)
  local c = S.cur
  local speed, dx, dy = c.windMs, c.windDirX, c.windDirY
  if M.GUSTS then
    S.gust = M.gustStep(S.gust, dt, M.GUST_TAU_S, (math.random() * 2 - 1) * math.sqrt(3))
    S.veer = M.gustStep(S.veer, dt, M.DIR_TAU_S, (math.random() * 2 - 1) * math.sqrt(3))
    speed = M.gustSpeed(c.windMs, c.gustMs, S.gust)
    local a = math.rad(S.veer / M.GUST_STD * M.dirStdDeg(c.windMs, c.gustMs))
    local ca, sa = math.cos(a), math.sin(a)
    dx, dy = c.windDirX * ca - c.windDirY * sa, c.windDirX * sa + c.windDirY * ca
  end
  local l = math.sqrt(dx ^ 2 + dy ^ 2)
  local x, y = 0, 0
  if l > 1e-3 then x, y = speed * dx / l, speed * dy / l end
  local have = e.getGroundWind and e.getGroundWind()
  if have and have.x and math.abs(have.x - x) < M.WIND_EPS_MS and math.abs(have.y - y) < M.WIND_EPS_MS then return end
  if envCall(e.setGroundWind, x, y, 0) then S.windSent = { x = x, y = y } end
end

local function applyGravity(e)
  if S.gravityOverride or not e.setGravity then return end
  local want = S.gravity
  if M.GRAVITY_ENABLED then
    local p = playerPos()
    if not p then return end
    if lastGravity then
      want = M.gravityFrom(lastGravity, p.z)
    else
      local names = scenetree.findClassObjects('TimeOfDay')
      local tod = names and names[1] and scenetree.findObject(names[1])
      if not tod then return end
      want = M.gravityAt(tonumber(tod.latitude) or 0, p.z)
    end
  elseif S.gravity == nil then
    return -- never set: nothing to restore
  else
    want = M.GRAVITY_DEFAULT
  end
  local have = e.getGravity and tonumber(e.getGravity())
  if have and math.abs(-have - want) <= M.GRAVITY_EPS then
    if not M.GRAVITY_ENABLED then S.gravity = nil end
    return
  end
  if M.GRAVITY_ENABLED then want = M.gravityToward(S.gravity, want, M.GRAVITY_STEP) end
  if envCall(e.setGravity, -want) then S.gravity = M.GRAVITY_ENABLED and want or nil end
end

local function hidePrecip()
  for name in pairs(S.precip) do pcall(drivePrecip, name, 0, 0) end
  M.rainNow = 0
  M.snowNow = 0
  S.shelter, M.shelter = 1, 1
  pcall(restoreSun)
  pcall(restoreExposure)
  pcall(restoreFogTint)
end

-- Is BWM_Weather changing the weather itself right now?
local function bwmNow()
  local b = rawget(_G, 'BWM_Weather_main')
  if not (b and b.getWeatherState) then return false end
  local ok, st = pcall(b.getWeatherState)
  return ok and M.bwmFighting(st) or false
end

-- The big map sets fog, clouds and time for its view and puts them back as it closes: not a change
-- by the player (and nothing is written meanwhile).
bigMapBusy = function()
  if S.clock < S.bigMapUntil then return true end
  local bm = rawget(_G, 'freeroam_bigMapMode')
  if not bm then return false end
  local ok, on = pcall(function()
    return (bm.bigMapActive and bm.bigMapActive()) or (bm.isTransitionActive and bm.isTransitionActive())
  end)
  return ok and on and true or false
end

-- Someone else changed the weather: real weather steps back to manual, saved as the setting (the
-- settings page shows it; settings.set applies the mode again, which is a no-op now), one toast.
local function switchToManual()
  local p = S.pending
  p.bwm = bwmNow()
  M.setMode('manual')
  S.switched = p
  local st = rawget(_G, 'beamngEarth_settings')
  if st and st.set then
    local ok, err = pcall(st.set, 'weatherMode', 'manual')
    if not ok then log('W', 'beamngEarth', 'weather: saving the mode failed: ' .. tostring(err)) end
  end
  -- what changed, as a warning: the reports of GitHub #418 (Real weather back to Manual every ~2 min, nothing touched)
  -- carried the warnings alone, never which values came in; who sent them (the stack) stays in the full log
  log('W', 'beamngEarth', 'weather: changed by someone else: ' .. tostring(p.values))
  log('I', 'beamngEarth', 'weather: the change came from' .. tostring(p.by))
  notify('Weather set to Off because it was changed'
    .. (p.bwm and ' (BWM_Weather is changing the weather: set it to Clear with automatic weather off to keep real weather)' or '')
    .. '. Choose Real or a preset on the Weather page of the BeamNG.Earth settings (or Weather in the radial menu) to go back.')
end

-- A fixed view distance's fog in every weather mode (real weather's apply takes it into its own density too; manual or
-- paused weather never runs apply): raised to the floor while driving, the density it replaced put back when the floor
-- goes (the setting, or the 3D map open), unless something else set the fog meanwhile.
viewFog = function(e, mapOn)
  if not (e.getFogDensity and e.setFogDensity) then return end
  local have = tonumber((e.getFogDensity()))
  if not have then return end
  -- (owned: the density now is the one written for the floor, by here or by apply; anything else set the fog since)
  if S.viewFogSet and math.abs(have - S.viewFogSet) > 1e-9 * math.max(1, have) then S.viewFogBase, S.viewFogSet = nil, nil end
  local h = e.getFogAtmosphereHeight and tonumber((e.getFogAtmosphereHeight())) or nil
  local floor = mapOn and 0 or M.viewFogFloor(nil, S.groundZ, h)
  local base = S.viewFogBase
  if base == nil then
    if floor > 0 and have < floor * 0.98 then base = have else return end    -- (not owned and nothing to raise)
  end
  local target = math.max(base, floor)          -- (up or down with the floor; the base again when it goes)
  if math.abs(have - target) > 1e-9 * math.max(1, target) then envCall(e.setFogDensity, target) end
  if floor > base then
    S.viewFogBase, S.viewFogSet = base, tonumber((e.getFogDensity())) or target
  else
    S.viewFogBase, S.viewFogSet = nil, nil      -- (released: the base is the fog again)
  end
end

M._viewFog = function(e, mapOn, groundZ)       -- tests (groundZ: the ground under the car)
  if groundZ ~= nil then S.groundZ = groundZ end
  viewFog(e, mapOn)
end
M._viewFogOwned = function() return S.viewFogBase, S.viewFogSet end    -- tests

-- Are our rain or snow objects shown (the owner must run until they are hidden)?
local function precipShown()
  for _, st in pairs(S.precip) do
    if st.hidden == false then return true end
  end
  return false
end

-- Weather phase B, every frame (each part only while it has something to do: dry, calm and Off cost a few compares):
-- the rain streak shells (rainstreaks.lua: the rain share the owner shows, after the shelter fade), lightning and
-- thunder (storm.lua: the target's thunder class, none with the weather Off, in a mission, over the 3D map or the big
-- map), the wet look (wetlook.lua: the rain shown, or W1's road state).
local streaks = require('beamngEarth/rainstreaks')
local wetlook = require('beamngEarth/wetlook')
local function phaseB(e, dt)
  local rain = (M.rainNow or 0) * (S.shelter or 1)
  if (M.STREAKS and rain > 0) or streaks.shown() then
    local ok, err = pcall(streaks.update, M.STREAKS and rain or 0, S.windSent, dt)
    if not ok then log('W', 'beamngEarth', 'weather streaks: ' .. tostring(err)) end
  end
  local cls = 0
  if M.LIGHTNING and mode ~= 'manual' and S.tgt and (S.tgt.thunder or 0) > 0 and not S.mission then
    if not (map3dOn() or bigMapBusy()) then cls = S.tgt.thunder end
  end
  local held = storm.holdSpec and M.LIGHTNING and mode ~= 'manual'
  if cls > 0 or S.storming or held then
    local sig = S.cur and tonumber(S.cur.fogSigma)
    local ok, on = pcall(storm.update, dt, cls, { groundZ = S.groundZ, sheltered = S.sheltered, hold = held,
      visM = sig and sig > 0 and M.FOG_K / sig or nil,
      cloudAboveM = (S.cur and S.cur.cloudAboveKm or 1.5) * 1000 })
    S.storming = ok and on or false
    if not ok then log('W', 'beamngEarth', 'weather storm: ' .. tostring(on)) end
  end
  local wetOn = M.WET_LOOK and mode ~= 'manual' and not S.mission
  if wetOn or S.wetActive then
    local ok, on = pcall(wetlook.update, dt, S.cur and S.cur.rainMmH or 0, wetOn)
    S.wetActive = ok and on or false
    if not ok then log('W', 'beamngEarth', 'weather wet look: ' .. tostring(on)) end
  end
end
M._phaseB = function(e, dt) phaseB(e, dt) end    -- (tests)

local function update(dt)
  if not S.inOurLevel or not scenetree.MissionGroup then return end
  local e = env()
  if not e then return end
  if S.pending and S.clock >= S.pending.at + M.MISSION_SETUP_S then switchToManual() end
  S.groundTimer = S.groundTimer + dt
  if S.groundTimer >= M.GROUND_CHECK_S then
    S.groundTimer = 0
    local p = playerPos()
    if p then S.groundZ = groundUnder(p) or S.groundZ end
  end
  -- the 3D map high up: no fog, no clouds, whatever the weather mode or the map's own pause (apply below may not run:
  -- the fog stayed at the level's 4e-5 and the screen went grey from orbit, 2026-11-07)
  S.mapTimer = (S.mapTimer or 0) + dt
  if S.mapTimer >= 0.25 then
    S.mapTimer = 0
    local mapOn = map3dOn()
    if mapOn then
      local camH = cameraOverGround()
      hideCloudsHigh(camH)
      if camH and camH >= M.FOG_FADE_TO_M then
        local have = e.getFogDensity and tonumber((e.getFogDensity()))
        if not have or have ~= 0 then envCall(e.setFogDensity, 0) end
      end
    end
    hideOthersInMap(mapOn)
    viewFog(e, mapOn)
  end
  -- rain and snow: their one owner, in every state (grace, missions, the big map: hidden there) but Off with nothing
  -- left shown (Off: no work at all)
  S.precipTimer = S.precipTimer + dt
  if S.precipTimer >= M.PRECIP_S then
    if mode ~= 'manual' or precipShown() then ownPrecip(e, S.precipTimer) end
    S.precipTimer = 0
  end
  phaseB(e, dt)
  -- traffic's headlights (wxtraffic): every TRAFFIC_S while the weather is on or the wrap is in
  S.trafficTimer = (S.trafficTimer or 0) + dt
  if S.trafficTimer >= M.TRAFFIC_S and (mode ~= 'manual' or wxtraffic.installed) then
    local want = false
    if mode ~= 'manual' and not S.mission then
      want = M.trafficLights()
      if not want and e.getLightState then
        local ok, ls = pcall(e.getLightState)
        want = ok and M.duskDark(ls) or false
      end
    end
    S.lightsWant = want
    wxtraffic.update(wxtraffic.CHECK_S, want)
    S.trafficTimer = 0
  end
  if S.clock < S.graceUntil or S.mission then return end
  S.gravTimer = S.gravTimer + dt
  if S.gravTimer >= M.GRAVITY_CHECK_S then S.gravTimer = 0; applyGravity(e) end
  if mode == 'manual' or not S.tgt or S.clock < S.quietUntil or bigMapBusy() then return end
  if not S.cur then S.cur = readEngine(e, S.tgt) end
  M.step(S.cur, S.tgt, dt, choice.changeS)
  S.applyTimer = S.applyTimer + dt
  if S.applyTimer >= M.APPLY_S then S.applyTimer = 0; apply(e) end
  S.windTimer = S.windTimer + dt
  if S.windTimer >= M.WIND_APPLY_S then applyWind(e, S.windTimer); S.windTimer = 0 end
end

------------------------------------------------------------------------------------------
-- hooks and console API

-- (the hook for the plan's time-lapse and forecast, WX6: a player of the hourly records hands each one in here with the
-- change speed shortened, setChangeSpeed, and the sky clock moved through sky.setServerClock when Sun = Real,
-- decision 7; nothing of it is built in phase A)
function M.onWeather(msg)
  if type(msg) ~= 'table' then return end
  lastMsg = msg
  retarget()
  local mp = rawget(_G, 'beamngEarth_mpsession')        -- ('real-host': the host's own real weather goes to the others)
  if serverHost and mp and mp.sendHostWeather then pcall(mp.sendHostWeather, msg) end
  local sk = sky()
  local off = tonumber(msg.utcOffsetS)
  if sk and sk.setRealUtcOffset and off then sk.setRealUtcOffset(off / 3600, tonumber(msg.lat), tonumber(msg.lon)) end
end

-- The server's real gravity at the player (server/gravity.py; beamngEarth_core hands it over).
function M.onGravity(msg)
  if type(msg) ~= 'table' or type(msg.g) ~= 'number' or msg.g < 9 or msg.g > 10.5 then return end
  lastGravity = { g = msg.g, h = tonumber(msg.h) or 0, source = msg.source, lat = msg.lat, lon = msg.lon }
  S.gravTimer = M.GRAVITY_CHECK_S -- applied on the next update
end

-- A new server session (core, on hello): back to normal gravity until its message arrives.
function M.clearGravity() lastGravity = nil end

-- One line for the settings page: the gravity in use and where it comes from.
function M.gravityLine()
  if not M.GRAVITY_ENABLED then return 'off (level default ' .. M.GRAVITY_DEFAULT .. ' m/s2)' end
  local src = lastGravity and (lastGravity.source == 'egm2008' and 'EGM2008 (server)' or 'normal (server)')
    or 'normal by latitude (no server value)'
  return (S.gravity and string.format('%.5f m/s2', S.gravity) or 'not set yet') .. ', ' .. src
end

function M.onUpdate(dtReal)
  dtReal = dtReal or 0
  S.clock = S.clock + dtReal
  if S.dirtyFrames > 0 then
    S.dirtyFrames = S.dirtyFrames - 1
    if S.dirtyFrames == 0 then
      local li = levelInfo()
      if li then pcall(function() li:setEditorDirty(false) end) end
    end
  end
  local ok, err = pcall(update, dtReal)
  if not ok then log('W', 'beamngEarth', 'weather: ' .. tostring(err)) end
end

-- core_environment's hook, fired before a change is applied (setState: the whole table its caller
-- passed, possibly with unchanged values). Our own writes are not changes (applying).
function M.onEnvironmentChanged(changes)
  if applying or type(changes) ~= 'table' then return end
  if S.clock < S.graceUntil or S.mission or not S.inOurLevel then return end
  if changes.gravity ~= nil then -- gravity is its own setting: the game's value is kept
    S.gravityOverride = S.clock
    S.quietUntil = S.clock + M.USER_QUIET_S
  end
  if bigMapBusy() then return end
  -- a cloud altitude someone else sets is kept, in manual mode too: the layer stops following the ground
  if changes.cloudAltitudeKm ~= nil and #M.changedKeys({ cloudAltitudeKm = changes.cloudAltitudeKm }, env()) > 0
    and sky() and sky().setCloudFollow then sky().setCloudFollow(false) end
  if mode ~= 'real' then return end
  -- the ground wind: setGroundWind applies before its hook, so compare with what we wrote last
  local keys = M.changedKeys(changes, env(), { groundWind = S.windSent })
  if #keys == 0 then return end
  S.quietUntil = S.clock + M.USER_QUIET_S
  if not S.pending then
    local vals = {}         -- (what and who, for the log: GitHub #418)
    for _, k in ipairs(keys) do vals[#vals + 1] = k .. '=' .. tostring(changes[k]) end
    S.pending = { at = S.clock, keys = keys, values = table.concat(vals, ', '), by = debug.traceback('', 2) }
  end
end

function M.onAnyMissionChanged(state)
  if state == 'started' then
    S.mission = true
    pcall(restoreSun)
    pcall(restoreExposure)
    pcall(restoreFogTint)
    if S.pending then -- the mission's own setup, just before 'started': no switch
      S.pending = nil
      local sk = sky()
      if sk and sk.setCloudFollow then sk.setCloudFollow(true) end
    end
    if S.gravityOverride and S.gravityOverride >= S.clock - M.MISSION_SETUP_S then S.gravityOverride = nil end
  elseif state == 'stopped' then
    S.mission = false
    S.graceUntil = math.max(S.graceUntil, S.clock + M.MISSION_RESUME_S) -- its restore ran just before
    S.cur = nil
  end
end

function M.onBeforeBigMapActivated() S.bigMapUntil = S.clock + M.BIGMAP_TAIL_S end
M.onBigmapStartTransition = M.onBeforeBigMapActivated
function M.onDeactivateBigMapCallback()
  S.bigMapUntil = S.clock + M.BIGMAP_TAIL_S
  S.cur = nil -- from the values it put back
end

local function onLevel()
  pcall(storm.reset); pcall(streaks.reset); pcall(wetlook.reset)
  resetState()
  S.inOurLevel = getCurrentLevelIdentifier and getCurrentLevelIdentifier() == M.LEVEL_ID
  local sk = sky()
  if sk and sk.setCloudFollow then sk.setCloudFollow(true) end
end
M.onClientStartMission = onLevel
function M.onClientEndMission()
  pcall(wxtraffic.reset)
  pcall(storm.reset); pcall(streaks.reset); pcall(wetlook.reset)
  resetState()
end
-- A Lua reload mid-level: settings.lua loads (and applies) before this extension, and no level
-- start follows to apply again, so the saved mode is read here.
function M.onExtensionLoaded()
  pcall(M.onQuickAccessLoaded)
  local st = rawget(_G, 'beamngEarth_settings')
  if st and st.get then M.loadChoices(st.get) end
  local saved = st and st.get and st.get('weatherMode')
  if saved then M.setMode(saved) end
  if scenetree.MissionGroup and getCurrentLevelIdentifier and getCurrentLevelIdentifier() then onLevel() end
end

-- 'real' (the default), 'preset' (the player's preset: setPresetChoice) or 'manual' (the page's Off); 'auto' (the old
-- default) means 'real'. Safe to repeat (the settings apply the saved mode again after a switch to manual).
M.MODES = { real = true, preset = true, manual = true }
function M.setMode(m)
  if m == 'auto' then m = 'real' end
  if m == 'off' then m = 'manual' end
  if not M.MODES[m] then return 'mode must be "real", "preset" or "manual"' end
  if m == 'manual' and mode ~= 'manual' then M.manualBy = debug.traceback('', 2) end   -- (who: the MP status)
  local was = mode
  mode = m
  S.pending = nil
  if mode ~= 'manual' then
    if was == 'manual' then
      S.gravityOverride, S.cur, S.quietUntil, S.windSent = nil, nil, 0, nil
      local sk = sky()
      if sk and sk.setCloudFollow then sk.setCloudFollow(true) end
    end
    if mode == 'real' then S.gravityOverride = nil end
    retarget()
  else
    hidePrecip()
    S.traffic = nil
  end
  return 'weather mode: ' .. mode
end
function M.getMode() return mode end

-- The player's choices (the settings' Weather page). Each takes effect at once (the targets again).
function M.setPresetChoice(name)
  name = tostring(name or ''):lower()
  if not M.PRESETS[name] then return 'unknown preset: ' .. name end
  choice.preset = name
  retarget()
  return 'weather preset: ' .. name
end
function M.setIntensity(k)
  choice.intensity = clamp(tonumber(k) or 1, 0, M.INTENSITY_MAX)
  retarget()
end
-- the change speed in seconds (95 % of a change reached after it; 0: at once)
function M.setChangeSpeed(sec)
  choice.changeS = math.max(0, tonumber(sec) or M.LERP_S)
end
function M.setShowPrecip(on) choice.show = on and true or false end
-- phase B's switches (the settings' Weather page): each takes effect in the next frame (a part switched off winds down:
-- the exposure and the materials go back, a running flash ends)
function M.setStormLight(on) M.STORM_LIGHT = on and true or false end
function M.setLightning(on) M.LIGHTNING = on and true or false end
function M.setStreaks(on) M.STREAKS = on and true or false end
function M.setWetLook(on) M.WET_LOOK = on and true or false end
function M.setRainVisibility(v) choice.visibility = clamp(tonumber(v) or M.RAIN_VISIBILITY, 0.25, 3) end
-- the parts of the real weather kept (clouds, fog, wind, precip; false: as on a clear day)
function M.setRealPart(part, on)
  if choice.parts[part] == nil then return end
  choice.parts[part] = on and true or false
  retarget()
end
function M.setFogLimit(m)
  choice.fogLimitM = math.max(0, tonumber(m) or 0)
  retarget()
end
-- The saved choices (get: beamngEarth_settings.get), as settings.lua applies them one by one at its load.
function M.loadChoices(get)
  local function g(k)
    local ok, v = pcall(get, k)
    if ok then return v end
  end
  if g('weatherPreset') then choice.preset = M.PRESETS[g('weatherPreset')] and g('weatherPreset') or choice.preset end
  if g('weatherIntensity') ~= nil then choice.intensity = clamp(tonumber(g('weatherIntensity')) or 1, 0, M.INTENSITY_MAX) end
  if g('weatherChange') ~= nil then choice.changeS = math.max(0, tonumber(g('weatherChange')) or M.LERP_S) end
  if g('weatherPrecip') ~= nil then choice.show = g('weatherPrecip') ~= false end
  if g('rainVisibility') ~= nil then choice.visibility = clamp(tonumber(g('rainVisibility')) or M.RAIN_VISIBILITY, 0.25, 3) end
  if g('fogLimit') ~= nil then choice.fogLimitM = math.max(0, tonumber(g('fogLimit')) or 0) end
  for key, f in pairs({ weatherStormLight = 'STORM_LIGHT', weatherLightning = 'LIGHTNING', weatherStreaks = 'STREAKS',
      weatherWetLook = 'WET_LOOK' }) do
    if g(key) ~= nil then M[f] = g(key) ~= false end
  end
  for part, key in pairs({ clouds = 'realClouds', fog = 'realFog', wind = 'realWind', precip = 'realPrecip' }) do
    if g(key) ~= nil then choice.parts[part] = g(key) ~= false end
  end
  retarget()
end

function M.choices()
  return { mode = mode, preset = choice.preset, intensity = choice.intensity, changeS = choice.changeS,
    show = choice.show, visibility = choice.visibility, fogLimitM = choice.fogLimitM,
    parts = { clouds = choice.parts.clouds, fog = choice.parts.fog, wind = choice.parts.wind, precip = choice.parts.precip } }
end

-- A BeamMP server's weather: a preset name (M.PRESETS), 'real-host' (every player gets the weather the server relays
-- from its weather host: setHostWeather; host = true on that player, whose own real weather is sent, onWeather), or
-- nil / 'real' (each player's own again). Overrides the player's real and preset modes; Off stays off.
function M.setPreset(name, host)
  local n = tostring(name or ''):lower()
  if n == 'real-host' then
    local was = serverHost
    preset, serverHost = { preset = 'real-host' }, host and true or false
    retarget()
    local mp = rawget(_G, 'beamngEarth_mpsession')      -- (a new host sends what it has at once, not in 10 min)
    if serverHost and not was and lastMsg and mp and mp.sendHostWeather then pcall(mp.sendHostWeather, lastMsg) end
    return true
  end
  local m = M.presetMsg(n)
  preset, serverHost = m, false
  if not m then hostMsg = nil end
  retarget()
  return m ~= nil
end

-- 'real-host': the host's weather message, relayed by the server (mpsession).
function M.setHostWeather(msg)
  if type(msg) ~= 'table' then return end
  hostMsg = msg
  if preset and preset.preset == 'real-host' then retarget() end
end

function M.preset() return preset and preset.preset or nil end
function M.isWeatherHost() return serverHost end

-- Traffic in bad weather (trafficvis.lua / speeding.lua / nightlights.lua): headlights on, the speed cap, and the lamps'
-- dusk widened under a dark sky. All off (false / the cap as it was / 0) with the weather Off.
function M.trafficLights() return M.trafficLightsFor(S.traffic) end
function M.trafficCap(cap) return M.trafficCapFor(cap, S.traffic) end
function M.darkness() return M.darknessFor(S.traffic) end
-- Is it dark enough for lamps and headlights by the weather, before the clock's night (ls: core_environment's
-- getLightState: { time, nightStart, nightEnd, isNight })? false when the weather is off or bright.
function M.duskDark(ls)
  local d = M.darkness()
  if d <= 0 or type(ls) ~= 'table' or ls.isNight then return false end
  return M.inNightWindow(ls.time, ls.nightStart, ls.nightEnd, M.DUSK_EXT_H / 24 * d)
end

function M.setGravityEnabled(on)
  M.GRAVITY_ENABLED = on and true or false
  S.gravTimer = M.GRAVITY_CHECK_S
  return 'real gravity ' .. (M.GRAVITY_ENABLED and 'on' or 'off')
end

-- The radial menu's Weather ring (the weather plan's 4.2 B: two presses, no menu): a category in the freeroam ring
-- (categories there carry a goto; finder.lua's note: an action at that level broke the menu), its entries the common
-- choices, each saved as the setting (the page shows it). Our level only.
M.RADIAL_PARENT = '/root/sandbox/'
M.RADIAL_LEVEL = '/root/sandbox/bngEarthWeather/'
M.RADIAL = { { 'real', 'Real weather', 'globe' }, { 'clear', 'Clear', 'sunRise' }, { 'cloudy', 'Cloudy', 'weather' },
  { 'rain', 'Rain', 'droplet' }, { 'storm', 'Thunderstorm', 'droplet' }, { 'fog', 'Fog', 'fogLight' },
  { 'snow', 'Snow', 'snowflake' }, { 'manual', 'Off', 'abandon' } }

-- Pure: the setting changes for a ring choice (a preset name, 'real' or 'manual'): { { key, value }, ... }.
function M.radialChoice(id)
  if id == 'real' or id == 'manual' then return { { 'weatherMode', id } } end
  if M.PRESETS[id] then return { { 'weatherPreset', id }, { 'weatherMode', 'preset' } } end
  return {}
end

local function choose(id, label)
  local st = rawget(_G, 'beamngEarth_settings')
  for _, kv in ipairs(M.radialChoice(id)) do
    if st and st.set then pcall(st.set, kv[1], kv[2])
    elseif kv[1] == 'weatherMode' then M.setMode(kv[2])
    elseif kv[1] == 'weatherPreset' then M.setPresetChoice(kv[2]) end
  end
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then
    pcall(gh.trigger, 'toastrMsg', { type = 'info', title = 'BeamNG.Earth weather', msg = 'Weather: ' .. label
      .. (preset and ' (the server sets the weather in this session)' or '') })
  end
end
M._radialSelect = choose     -- (tests)

local function isCurrent(id)
  if id == 'manual' or id == 'real' then return mode == id end
  return mode == 'preset' and choice.preset == id
end

function M.onQuickAccessLoaded()
  local qa = rawget(_G, 'core_quickAccess')
  if not (qa and qa.addEntry) then return end
  qa.addEntry({ level = M.RADIAL_PARENT, uniqueID = 'beamngEarthWeatherRing', generator = function(entries)
    if not S.inOurLevel then return end
    table.insert(entries, { title = 'Weather', icon = 'weather', ['goto'] = M.RADIAL_LEVEL, uniqueID = 'beamngEarthWeather',
      categoryOrder = 25, desc = 'BeamNG.Earth: real weather, a preset or off' })
  end })
  qa.addEntry({ level = M.RADIAL_LEVEL, uniqueID = 'beamngEarthWeatherChoices', generator = function(entries)
    if not S.inOurLevel then return end
    for i, r in ipairs(M.RADIAL) do
      local id, label = r[1], r[2]
      table.insert(entries, { title = label .. (isCurrent(id) and ' *' or ''), icon = r[3], priority = i,
        uniqueID = 'beamngEarthWeather_' .. id, desc = 'Weather: ' .. label,
        onSelect = function() choose(id, label) return { 'hide' } end })
    end
  end })
end

-- One line for the BeamNG.Earth window (the weather shown, and where it comes from).
local function sourceText()
  if mode == 'manual' then return 'off' .. (S.switched and ', changed in the game' or '') end
  if preset then return 'server: ' .. preset.preset end
  if mode == 'preset' then return 'preset: ' .. (M.PRESET_LABELS[choice.preset] or choice.preset) end
  return 'real'
end
function M.statusLine()
  local m = shownMsgOf() or lastMsg
  local head = 'Weather (' .. sourceText() .. ')'
  if not m then return head .. ': no data yet' end
  local w = m.wind or {}
  local t = tonumber(m.tempC) or tonumber(m.slTempC) or 0
  return string.format('%s: %.0f C, %s, wind %.0f m/s from %s, visibility %s', head, t,
    (m.preset and M.PRESET_LABELS[m.preset] and M.PRESET_LABELS[m.preset]:lower()) or M.describe(m.code),
    tonumber(w.ms) or 0, M.compass(w.fromDeg), m.visM and string.format('%.0f km', m.visM / 1000) or '?')
end

-- The weather message shown (code, cover, wind, tempC, ...; with the weather Off the real one) as a plain table, unlike
-- status() (a dumps() string for the console): the world map's weather badge (settings.uiMapState) reads it every
-- second, too often to parse. M.realMsg: the latest real one.
function M.current() return shownMsgOf() or lastMsg end
function M.realMsg() return lastMsg end

-- The precipitation owner's state (status, tests).
function M.precipState()
  return { block = S.block, shelter = S.shelter, sheltered = S.sheltered, why = S.shelterWhy, flake = S.flake,
    rain = S.precip[M.RAIN_NAME], snow = S.precip[M.SNOW_NAME] }
end

M.storm = storm         -- (the console: beamngEarth_weather.storm.hold({ d = 3000, bolt = 1 }) shows a held flash)
function M.status()
  return dumps({ mode = mode, choices = M.choices(), server = preset and preset.preset, weatherHost = serverHost,
    pending = S.pending, switched = S.switched, bwmFighting = bwmNow(),
    gravityOverride = S.gravityOverride ~= nil, bigMap = bigMapBusy(), inOurLevel = S.inOurLevel, grace = math.max(0, S.graceUntil - S.clock), mission = S.mission,
    groundZ = S.groundZ, gravity = S.gravity, gravityMsg = lastGravity, temperatureCurve = S.temp.method, tempC = S.temp.value,
    precip = M.precipState(),
    phaseB = { stormLight = M.STORM_LIGHT, exposure = S.ev, gloom = S.cur and S.cur.gloom, thunder = S.tgt and S.tgt.thunder,
      storm = storm.status(), streaks = streaks.shown(), wet = { level = wetlook.level(), wetness = wetlook.wetness(),
      source = wetlook.source, writes = wetlook.writes } },
    traffic = S.traffic, lights = S.lightsWant, lightsWrap = wxtraffic.installed, tint = S.tint,
    msg = lastMsg, shown = shownMsgOf(), target = S.tgt, current = S.cur, line = M.statusLine() })
end

return M
