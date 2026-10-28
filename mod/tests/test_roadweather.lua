-- luajit mod/tests/test_roadweather.lua  (rain and snow on the roads: amounts over time, grip, the look, restore)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local R = require('beamngEarth/roadweather')

-- soaking, drying
local w, s = 0, 0
for _ = 1, 180 do w, s = R.step(w, s, 1, 100, 0, 15) end
T.near(w, 1, 1e-9, 'full rain soaks a road in WET_UP_S')
for _ = 1, 600 do w, s = R.step(w, s, 1, 0, 0, 15) end
T.check(w > 0.4 and w < 0.9, 'dries slowly after the rain: ' .. w)
for _ = 1, 3000 do w, s = R.step(w, s, 1, 0, 0, 15) end
T.check(w == 0, 'dry in the end')
-- snow below freezing, none above it; melting
w, s = 0, 0
for _ = 1, 900 do w, s = R.step(w, s, 1, 0, 100, -3) end
T.near(s, 1, 1e-9, 'full snow covers a road in SNOW_UP_S')
T.check(w >= 0.399, 'slush under it')
local s0 = s
for _ = 1, 100 do w, s = R.step(w, s, 1, 0, 0, -3) end
T.check(s == s0, 'no melting below freezing')
w, s = 0, 0
for _ = 1, 900 do w, s = R.step(w, s, 1, 0, 100, 5) end
T.check(s == 0, 'snow at 5 C does not settle')
w, s = 0, 1
for _ = 1, 600 do w, s = R.step(w, s, 1, 0, 0, 10) end
T.check(s < 0.5 and w >= 0.6, 'melts at 10 C and wets the road: ' .. s)

-- grip
T.near(R.grip(0, 0, 15), 1, 1e-9, 'dry: full grip')
T.near(R.grip(1, 0, 15), 0.75, 1e-9, 'wet: 25 % less')
T.near(R.grip(0.4, 1, -3), 0.3, 1e-9, 'snow over a damp freezing road')
T.near(R.grip(1, 1, -3), 0.25, 1e-9, 'snow on ice: at the floor')
T.near(R.grip(1, 0, -2), 0.5, 1e-9, 'wet and freezing: ice')

-- the look
local c, r = R.look({ 1, 1, 1, 1 }, 0.8, 0, 0)
T.check(c[1] == 1 and r == 0.8, 'dry: unchanged')
c, r = R.look({ 1, 1, 1, 1 }, 0.8, 1, 0)
T.near(c[1], 0.65, 1e-9, 'wet: darker')
T.near(r, 0.32, 1e-9, 'wet: glossier')
c, r = R.look({ 1, 1, 1, 1 }, 0.8, 0.4, 1)
T.near(c[1], R.SNOW_TINT, 1e-9, 'snow: pale')
T.near(r, 0.85, 1e-9, 'snow: matte')

-- grip and look applied to the game's objects, then put back
local set = {}
be = { setGroundModel = function(self, name, gm) set[name] = gm end }
ground_model = function() return {} end
core_environment = { groundModels = {
  ASPHALT = { cdata = { staticFrictionCoefficient = 0.98, slidingFrictionCoefficient = 0.7, stribeckVelocity = 6 } },
  WATER = { cdata = { staticFrictionCoefficient = 0.1, slidingFrictionCoefficient = 0.1 } } } }
local mats = {}
local function mat(fields)
  local o = { f = fields, flushed = 0 }
  function o:getField(k) return self.f[k] or '' end
  function o:setField(k, _, v) self.f[k] = v end
  function o:flush() self.flushed = self.flushed + 1 end
  function o:reload() end
  return o
end
mats.road_asphalt_2lane = mat({ baseColorFactor = '1 1 1 1', roughnessFactor = '0.8' })
mats.line_white = mat({ diffuseColor = '0.9 0.9 0.9 1' })
scenetree = { findObject = function(n) return mats[n] end }
local rain, snowP, temp = 100, 0, 12
package.loaded['beamngEarth/weather'] = { roadInputs = function() return rain, snowP, temp end }
local t = 0
for _ = 1, 200 do t = t + 1; R.update(1, t) end
T.check(set.ASPHALT and set.ASPHALT.staticFrictionCoefficient < 0.98 * 0.8, 'rain: asphalt grip lowered: '
  .. tostring(set.ASPHALT and set.ASPHALT.staticFrictionCoefficient))
T.check(set.ASPHALT.stribeckVelocity == 6, 'the other fields copied')
T.check(set.WATER == nil, 'water left alone')
T.check(mats.road_asphalt_2lane.f.baseColorFactor ~= '1.000 1.000 1.000 1.000', 'the road looks wet')
T.check(mats.line_white.f.diffuseColor:sub(1, 5) ~= '0.900', 'old-style material tinted too')
local calls = mats.road_asphalt_2lane.flushed
R.update(0.1, t + 0.1)
T.check(mats.road_asphalt_2lane.flushed == calls, 'not written every frame')
R.restore()
T.near(set.ASPHALT.staticFrictionCoefficient, 0.98, 1e-9, 'restored: grip as loaded')
T.check(mats.road_asphalt_2lane.f.baseColorFactor == '1.000 1.000 1.000 1.000'
  and mats.road_asphalt_2lane.f.roughnessFactor == '0.800', 'restored: the look as loaded')
T.check(mats.line_white.f.diffuseColor == '0.900 0.900 0.900 1.000', 'restored: the old-style one too')
T.done('test_roadweather')
