-- luajit mod/tests/test_airtraffic.lua: the live air traffic (beamngEarth/airtraffic.lua): nothing per frame while
-- off, the consent, the requests, dating rows by their own UTC time, the tinted livery, lettering only on the followed
-- aircraft, the budgets, the contrail physics, the map items and the credit line. Engine stubs only: the drawing is
-- checked in game.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

-- the game's pieces the module reads
ColorF = function(r, g, b, a) return { r = r, g = g, b = b, a = a } end
ColorI = ColorF
Point2F = function(x, y) return { x = x, y = y } end
String = function(s) return s end
local drawn = { prism = 0, sphere = 0, text = 0 }
debugDrawer = {
  drawSquarePrism = function() drawn.prism = drawn.prism + 1 end,
  drawCylinder = function() drawn.prism = drawn.prism + 1 end,
  drawSphere = function() drawn.sphere = drawn.sphere + 1 end,
  drawTextAdvanced = function() drawn.text = drawn.text + 1 end,
  drawText = function() drawn.text = drawn.text + 1 end,
}
local cam = vec3(0, 0, 2)
core_camera = {
  getPosition = function() return cam end,
  getForward = function() return vec3(0, 1, 0) end,
  getFovDeg = function() return 60 end,
  getQuat = function() return quat(0, 0, 0, 1) end,
  getActiveCamName = function() return 'game' end,
  setPosRot = function() end,
}
local hookUpdates = {}
extensions = { hookUpdate = function(name) hookUpdates[#hookUpdates + 1] = name end }

-- settings (beamngEarth/settings): a plain table
local vals = { airTraffic = false, airTrafficConsent = 0, quality = 'high' }
package.loaded['beamngEarth/settings'] = {
  get = function(k) return vals[k] end,
  set = function(k, v) vals[k] = v; return { ok = true, value = v } end,
  isMapOpen = function() return false end,
}
-- the connection
local sent = {}
package.loaded['beamngEarth/net'] = { isConnected = function() return true end, send = function(m) sent[#sent + 1] = m end }
-- the world: a flat frame, 1 degree ~ 111 km round 33.6, -84.4
local LAT0, LON0 = 33.64, -84.43
local anchor = {}
function anchor:geodeticToWorld(lat, lon, h)
  return (lon - LON0) * 111320 * math.cos(math.rad(LAT0)), (lat - LAT0) * 110540, h or 0
end
local st = { inOurLevel = true, anchor = anchor, player = { lat = LAT0, lon = LON0, x = 0, y = 0, z = 0 } }
beamngEarth_core = { _state = function() return st end }
be = { getSurfaceHeightBelow = function() return 0 end }

local at = require('beamngEarth/airtraffic')
local predict = require('beamngEarth/airtraffic/predict')
local credits = require('beamngEarth/credits')
beamngEarth_airtraffic = at

-- dimsOf: a model with no dims falls back (the add-on recursed forever here)
local d = at._dimsOf({ model = 'nosuchmodel', kind = 'nosuchkind' })
T.check(d == require('beamngEarth/airtraffic/dims').narrow, 'an unknown model falls back to the narrowbody dims')
T.check(at._dimsOf({ model = 'nosuch', kind = 'heli' }) == require('beamngEarth/airtraffic/dims').heli, 'then its class')

-- off: no per-frame hook at all, nothing sent
at.onExtensionLoaded()
T.check(at.onUpdate == nil and at.onPreRender == nil, 'off: no onUpdate / onPreRender hook')
T.check(at._hooked() == false, 'off: not hooked')
-- on without the consent: stays off
vals.airTraffic = true
T.check(at.setEnabled(true) == false, 'no consent: stays off')
T.check(at.onUpdate == nil, 'no consent: still no hook')
-- with it
vals.airTrafficConsent = at.CONSENT_VERSION
T.check(at.setEnabled(true) == true, 'consent given: on')
T.check(type(at.onUpdate) == 'function' and type(at.onPreRender) == 'function', 'on: hooks')
local hu = table.concat(hookUpdates, ',')
T.check(hu:find('onUpdate') and hu:find('onPreRender'), 'hookUpdate refreshed the game hook lists: ' .. hu)

-- the first frame asks at once with the player's position and the switches
at.onUpdate(0.016)
T.check(#sent == 1 and sent[1].type == 'air_traffic', 'one request sent')
T.check(sent[1].lat == LAT0 and sent[1].lon == LON0 and sent[1].rangeNm == 60 and sent[1].routes == false,
  'the request: position for the local app, radius, routes off')
for _ = 1, 100 do at.onUpdate(0.016) end
T.check(#sent == 1, 'no second request while one is in flight')

-- the answer: rows dated by srvNow - ts
local NOW = 1764200000000
at.onResult({ type = 'air_traffic_result', id = sent[1].id, ok = true, src = 'adsb.lol',
  credit = 'Aircraft: adsb.lol (ODbL)', srvNow = NOW, ac = {
    { h = 'a1b2c3', f = 'DAL1234', t = 'A321', r = 'N123DN', c = 'A3', lat = LAT0 + 0.01, lon = LON0, g = false,
      ts = NOW - 5000, ab = 3000, ag = 3100, gs = 150, trk = 0, vs = 0 },
    { h = 'f16f16', f = 'VIPER1', t = 'F16', c = 'A6', lat = LAT0 + 0.02, lon = LON0, g = false, ts = NOW - 1000,
      ab = 5000, gs = 300, trk = 90 },
    { h = 'b0b0b0', f = 'N55', t = 'C172', lat = LAT0, lon = LON0 + 0.01, g = true, ts = NOW, gs = 0 },
  } })
local items = at._items()
T.check(items.a1b2c3 and items.f16f16 and items.b0b0b0, 'three aircraft merged')
T.near(items.a1b2c3.base.t0, items.a1b2c3.seen - 5, 1e-6, 'a row is dated by its own time (5 s old)')
T.check(credits.extraText() == 'Aircraft: adsb.lol (ODbL)', 'the source credit is on the credits line')
T.check(credits.text('Imagery: x') == 'Imagery: x\nAircraft: adsb.lol (ODbL)', 'the HUD credits app shows it too')

-- the models: one tinted livery overlay per airframe, none for the fighter
for _ = 1, 70 do at.onUpdate(0.02) end
local liv, fighterLiv, bodies, glyphs = {}, false, 0, 0
for _, o in ipairs(S.liveOf('TSStatic')) do
  local sh = o.fields.shapeName or ''
  if sh:find('_liv') then
    liv[#liv + 1] = o
    if sh:find('fighter') then fighterLiv = true end
  elseif sh:find('lat_glyph') then glyphs = glyphs + 1
  elseif sh:match('lat_[%w_]+%.dae$') and not sh:find('_gear') and not sh:find('rotor') then bodies = bodies + 1 end
end
T.check(bodies == 3, 'three bodies made: ' .. bodies)
T.check(#liv == 2, 'two livery overlays (the fighter has none): ' .. #liv)
T.check(not fighterLiv, 'no fighter livery file asked for')
for _, o in ipairs(liv) do
  T.check(o.fields.shapeName:match('_liv%.dae$') ~= nil, 'one overlay file per airframe: ' .. o.fields.shapeName)
  T.check(o.useInstanceRenderData == true and type(o.fields.instanceColor) == 'string', 'tinted by instanceColor')
  T.check(o.fields.shapeName:find('^/levels/beamng_earth/art/shapes/airtraffic/') ~= nil, 'from our shapes folder')
end
T.check(glyphs == 0, 'no lettering on aircraft the camera does not follow')

-- the map's items
local mi = at.mapItems()
T.check(#mi == 3 and mi[1].h == 'a1b2c3' and mi[1].f == 'DAL1234' and mi[2].g == true, 'map items, by hex')
local rows = at.mapRows3d({ { id = 'x' } })
T.check(#rows == 4 and rows[1].id == 'x' and rows[2].id:find('^air:') ~= nil, '3D map rows: the labels + the aircraft')

-- budgets by preset
at.applyBudgets('low')
T.check(at.cfg.maxModels == 12 and at.cfg.trailSegments == 300, 'Low: the handheld budgets')
vals.airTrafficMaxModels = 7
at.applyBudgets('ultra')
T.check(at.cfg.maxModels == 7 and at.cfg.trailSegments == 4000, 'a set model cap wins over the preset')
vals.airTrafficMaxModels = 0
at.applyBudgets('high')

-- an error answer: asked again sooner, the aircraft kept
local before = #sent
for _ = 1, 33 do at.onUpdate(0.5) end
T.check(#sent == before + 1, 'asked again after POLL_S')
at.onResult({ type = 'air_traffic_result', id = sent[#sent].id, ok = false, error = 'adsb.lol: rate limited' })
T.check(at.status().error == 'adsb.lol: rate limited' and at.status().on, 'the error is in the status')
for _ = 1, math.ceil(at.RETRY_S / 0.5) + 1 do at.onUpdate(0.5) end
T.check(#sent == before + 2, 'retried after RETRY_S')

-- paused (and no map open): no request
local pausedNow = true
simTimeAuthority = { getPause = function() return pausedNow end }
at.onResult({ type = 'air_traffic_result', id = sent[#sent].id, ok = true, srvNow = NOW, ac = {} })
local n0 = #sent
for _ = 1, 40 do at.onUpdate(0.5) end
T.check(#sent == n0, 'not asked while the game is paused')
pausedNow = false

-- off again: everything gone, the hooks out, no more requests, the credit gone
at.setEnabled(false)
T.check(at.onUpdate == nil and at.onPreRender == nil, 'off: hooks removed')
T.check(#S.liveOf('TSStatic') == 0, 'off: every object deleted')
T.check(next(at._items()) == nil, 'off: no aircraft kept')
T.check(credits.extraText() == nil, 'off: the credit line gone')
T.check(#at.mapItems() == 0, 'off: nothing on the maps')

-- contrails from the weather: the standard day's level, colder days lower, humid air longer-lived
local std = predict.contrailFormation(15, 0.5, 1013.25, 0)
T.check(std > 25000 and std < 31000, 'a standard day: contrails from ~27,000 ft: ' .. std)
T.check(predict.contrailFormation(0, 0.5, 1013, 0) < std, 'a cold day: lower')
T.check(predict.contrailFormation(30, 0.5, 1013, 0) > std, 'a hot day: higher')
T.check(predict.contrailFormation(15, 0.95, 1013, 0) <= predict.contrailFormation(15, 0.35, 1013, 0),
  'humid air aloft: no higher')
T.check(predict.contrailFormation(nil) == predict.CONTRAIL_DEFAULT_FT, 'no weather: the default')
T.check(predict.contrailLife(80, 0.95) > 1.5 and predict.contrailLife(0, 0.35) < 0.5, 'cirrus: long-lived; dry: short')
at.refreshWeather({ slTempC = 15, pressureHpa = 1013.25, elevM = 0, cover = { high = 70, mid = 0 },
  aloft = { ms = 10, fromDeg = 270 } })
local ft, life, wind = at._contrail()
T.check(math.abs(ft - predict.contrailFormation(15, predict.humidityAloft(70, 0), 1013.25, 0)) < 1, 'the weather sets the level')
T.check(life > 1, 'high cloud: longer contrails')
T.check(wind and wind.x > 15 and math.abs(wind.y) < 1e-6, 'a west wind aloft drifts them east')

-- an answer that is not for the request in flight (the place before a teleport, a late one) is dropped
at.setEnabled(true)
at.onResult({ type = 'air_traffic_result', id = 987654, ok = true, srvNow = NOW, ac = { { h = 'eeeeee', lat = 0, lon = 0, ts = NOW } } })
T.check(at._items().eeeeee == nil, 'an answer for no request in flight is dropped')

-- a test server's hello (BNE_AIRTRAFFIC_TEST): on for the session without the consent, nothing saved; the
-- settings' later apply of the saved (off) value does not turn it off; another hello without it: back to the setting
vals.airTraffic, vals.airTrafficConsent = false, 0
at.setEnabled(false)
at.onHello({ type = 'hello', airTrafficTest = true })
T.check(at.enabled and at.onUpdate ~= nil, 'test hello: on')
T.check(vals.airTraffic == false and vals.airTrafficConsent == 0, 'test hello: nothing saved')
at.setEnabled(false)
T.check(at.enabled, 'test session: the saved off does not turn it off')
at.onHello({ type = 'hello' })
T.check(not at.enabled and at.onUpdate == nil, 'a normal hello: the setting again (off)')

T.done('test_airtraffic')
