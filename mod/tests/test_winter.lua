-- luajit mod/tests/test_winter.lua (winter.lua: the snowstate's conditions as live ground-model values, winter W1)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')

-- the engine side: ground models by name (aliases share one cdata, as environment.lua submits them), setGroundModel
local calls = {}
be.setGroundModel = function(_, name, gm) calls[#calls + 1] = { name = name, gm = gm } end
be.getPlayerVehicleID = function() return 7 end
local FIELDS = { staticFrictionCoefficient = 0.96, slidingFrictionCoefficient = 0.67, roughnessCoefficient = 0,
  stribeckVelocity = 4.5, hydrodynamicFriction = 0, strength = 1, defaultDepth = 0, collisiontype = 10,
  fluidDensity = 200, flowConsistencyIndex = 10000, flowBehaviorIndex = 0.5, dragAnisotropy = 0, skidMarks = true,
  shearStrength = 0 }
function ground_model() return {} end
local function stock()
  local c = {}
  for k, v in pairs(FIELDS) do c[k] = v end
  return c
end
local function models()
  local gms = {}
  local asph, old = stock(), stock()
  gms.ASPHALT = { cdata = asph, isAlias = false, parent = 'none' }
  for _, a in ipairs({ 'GROUNDMODEL_ASPHALT1', 'GRID', 'CONCRETE', 'CONCRETE2' }) do
    gms[a] = { cdata = asph, isAlias = true, parent = 'ASPHALT' }
  end
  gms.ASPHALT_OLD = { cdata = old, isAlias = false, parent = 'none' }
  gms.GROUNDMODEL_ASPHALT_OLD = { cdata = old, isAlias = true, parent = 'ASPHALT_OLD' }
  for _, n in ipairs({ 'COBBLESTONE', 'GRASS', 'DIRT', 'DIRT_DUSTY', 'DIRT_DUSTY_LOOSE', 'SAND', 'MUD', 'SNOW', 'GRAVEL',
      'ROCK' }) do
    gms[n] = { cdata = stock(), isAlias = false, parent = 'none' }
  end
  return gms
end
core_environment = { groundModels = models() }
package.preload['particles'] = function()
  return { getMaterialsParticlesTable = function() return {} end,
    getOrAddMaterialIDByName = function(_, n) return ({ SNOW = 22, ICE = 21, ASPHALT_WET = 12 })[n] or 99 end }
end
local pos = { x = 0, y = 0, z = 0 }
function getObjectByID() return { getPosition = function() return pos end } end
local road = { drivability = 0.8, posted = 11.2, target = 11.2, distance = 2 }
package.loaded['beamngEarth/navgraph'] = { speedProfileAt = function() return road end }
local speeding = require('beamngEarth/speeding')
local statusbar = require('beamngEarth/statusbar')
local W = require('beamngEarth/winter')

local function byName()
  local out = {}
  for _, c in ipairs(calls) do out[c.name] = c.gm end
  return out
end

-- pure parts
T.check(W.classOf(1.0, 29.06) == 0 and W.classOf(1.0, 20.1) == 1 and W.classOf(0.9, 15) == 2 and W.classOf(0.8, 11) == 3
  and W.classOf(0.4, 6.7) == 4 and W.classOf(nil) == 2, 'plow class from drivability and posted speed')
local s, sl = W.iceFriction(-1)
T.near(s, 0.08, 1e-9, 'ice near 0 degC: the most slippery')
T.near(sl, 0.064, 1e-9, 'sliding 0.8x')
T.near((W.iceFriction(-30)), 0.22, 1e-9, 'cold ice (clamped at -15 degC)')
T.near((W.iceFriction(-8)), 0.15, 1e-9, 'ice at -8 degC')
T.check(W.roadValues({ 'dry', 0, 1 }, 0.12) == nil, 'dry: stock')
local v = W.roadValues({ 'loose', 300, -4, 'loose', 300, 0, 0 }, 0.12)
T.check(v[1] == 0.38 and v[6] == 'SNOW' and v[7] == 0.12, 'loose 30 cm: depth capped at the mesh limit')
v = W.roadValues({ 'loose', 50, -4, 'loose', 50, 0, 0 }, 0.12)
T.near(v[7], 0.05, 1e-9, 'loose 5 cm: its depth')
T.check(W.roadValues({ 'packed', 30, -4 }, 0.2)[7] == 0, 'packed snow: no sinkage')
v = W.roadValues({ 'ice', 0, -1, 'dry', 0, 1, 1 }, 0.12)
T.check(v[1] == 0.25 and v[2] == 0.22 and v[6] == 'ICE', 'gritted ice')
T.check(W.groundValues({ 0, 0 }, 0.2) == nil, 'no snow: stock ground')
v = W.groundValues({ 400, 4, 8, 0, 0, 60, 10 }, 0.2)
T.near(v[7], 0.2, 1e-9, '40 cm of fresh snow on terrain: 80 % sinkage, capped at 0.20 m')
T.near(W.groundValues({ 100, 3, 8, 0, 1, 30, 10 }, 0.2)[7], 0.04, 1e-9, 'a crust halves the sinkage')
T.near(W.groundValues({ 400, 4, 8, 0, 0, 60, 10 }, 0.12)[7], 0.12, 1e-9, 'mesh models: 0.12 m at most')
T.check(W.groundValues({ 20, 2, 5, 0, 0, 3, 1 }, 0.2)[7] == 0, 'light cover: no depth')
T.check(W.composeReadout({ 'dry', 0, 5 }, { 'dry', 0, 4 }, { 0, 0 }) == '', 'dry, no snow: no readout')
T.check(W.composeReadout({ 'packed', 30, -6.2 }, { 'packed', 30, -7 }, { 50, 3 }) == 'Packed snow, -6\194\176C',
  'readout')
T.check(W.composeReadout({ 'wet', 0, 1 }, { 'black_ice', 0, -1 }, { 0, 0 }) == 'Wet road, 1\194\176C, bridges: black ice',
  'a worse deck is named')

-- nothing happens before a snowstate (core loads the module with the first one)
T.check(#calls == 0 and speeding.winter == nil and statusbar.extra == nil, 'nothing before a state')

local function state(road3, deck3, ground)
  local roads, bridges = {}, {}
  for i = 1, 5 do
    roads[i] = { 'dry', 0, 3, 'dry', 0, 0, 1 }
    bridges[i] = { 'dry', 0, 2, 'dry', 0, 0, 1 }
  end
  roads[4], bridges[4] = road3, deck3
  return { type = 'snowstate', v = 1, roads = roads, bridges = bridges, ground = ground or { 0, 0, 0, 0, 0, 0, 0 } }
end

-- a state: residential (P3, the default class 2 moves to 3 after the dwell) packed, its decks icy, ground 20 cm
W._setClass(3)
local asphStock = core_environment.groundModels.ASPHALT.cdata
W.onState(state({ 'packed', 40, -5, 'loose', 80, 0, 0.9 }, { 'black_ice', 0, -7, 'dusting', 10, 0, 0.9 },
  { 200, 4, 8, 0, 0, 30, 12 }))
local m = byName()
T.check(m.ASPHALT_OLD and m.ASPHALT_OLD.staticFrictionCoefficient == 0.27 and m.ASPHALT_OLD.collisiontype == 22,
  'terrain roads: packed snow (SNOW particles)')
T.check(m.GROUNDMODEL_ASPHALT_OLD and m.GROUNDMODEL_ASPHALT_OLD ~= m.ASPHALT_OLD, 'its alias set too, its own copy')
T.check(m.ASPHALT and m.ASPHALT.staticFrictionCoefficient == 0.15 and m.ASPHALT.collisiontype == 21, 'decks: black ice')
T.check(m.GRID and m.GROUNDMODEL_ASPHALT1, 'the deck aliases')
T.check(m.CONCRETE == nil and m.CONCRETE2 == nil, 'tunnel concrete (aliases of ASPHALT) keeps its grip')
T.check(asphStock.staticFrictionCoefficient == 0.96 and asphStock.collisiontype == 10, 'the stock cdata is never written')
T.check(m.SNOW.defaultDepth <= 0.2 + 1e-9 and m.SNOW.defaultDepth > 0.12, 'terrain-only snow layer: deep')
T.check(m.GRASS.defaultDepth <= 0.12 + 1e-9, 'grass (meshes use it too): 0.12 m at most')
T.check(m.ASPHALT.defaultDepth == 0, 'no depth on ice')
T.check(speeding.winter == W and W.speedFactor() == 0.8, 'the AI slows on packed snow')
T.check(statusbar.extra and statusbar.extra():find('Packed snow') and statusbar.extra():find('bridges: black ice'),
  'status bar readout')
local n = #calls
W.onState(state({ 'packed', 40, -5, 'loose', 80, 0, 0.9 }, { 'black_ice', 0, -7, 'dusting', 10, 0, 0.9 },
  { 200, 4, 8, 0, 0, 30, 12 }))
T.check(#calls == n, 'the same state again: no engine call')

-- the once-a-second check: nothing new, nothing called; a level load (new ground-model table) applies again
W.update(1.0)
T.check(#calls == n, 'steady: no calls')
core_environment.groundModels = models()
W.update(1.0)
T.check(#calls > n and byName().ASPHALT_OLD.staticFrictionCoefficient == 0.27, 'a reload: applied again')

-- the road under the car changes class: after the dwell the slots follow the new class's row
n = #calls
road.drivability, road.posted = 1.0, 29.06       -- a motorway: P0 dry
W.update(1.0)
T.check(#calls == n, 'not before the dwell')
for _ = 1, 6 do W.update(1.0) end
T.check(byName().ASPHALT_OLD == core_environment.groundModels.ASPHALT_OLD.cdata, 'P0 dry: stock cdata back')
T.check(W.speedFactor() == 1, 'dry: full speed')

-- calibration forcing
W.force('ice')
T.check(byName().ASPHALT_OLD.collisiontype == 21 and byName().ASPHALT.collisiontype == 21, 'forced ice everywhere')
T.check(byName().GRASS == core_environment.groundModels.GRASS.cdata, 'forced: open ground stock')
W.force(nil)

-- a new server hello without winter: stock again after STALE_S
W.onHello()
for _ = 1, 70 do W.update(1.0) end
local gms = core_environment.groundModels
m = byName()
T.check(m.ASPHALT == gms.ASPHALT.cdata and m.SNOW == gms.SNOW.cdata and m.GRASS == gms.GRASS.cdata, 'stale: stock')
T.check(speeding.winter == nil and statusbar.extra == nil and W.readout() == '', 'hooks gone')

-- speeding: the factor multiplies the cap
local sent = {}
local obj = { queueLuaCommand = function(_, c) sent[#sent + 1] = c end }
function getObjectByID() return obj end
be.getPlayerVehicleID = function() return 99 end
gameplay_traffic = { getTrafficData = function()
  return { [5] = { isAi = true, state = 'active', role = { name = 'standard', state = 'none', actionName = 'none' },
    tracking = { speedLimit = 20 } } }
end }
speeding.winter = { speedFactor = function() return 0.65 end }
math.randomseed(3)
speeding.update(1.0, { rules = { country = 'US' } })
local cap = tonumber(sent[#sent]:match('ai.setSpeed%(([%d%.]+)%)'))
T.check(cap and cap < 20 * 1.2 * 0.65 + 0.01 and cap > 20 * 0.9 * 0.65, 'icy: the AI cap 0.65x')
speeding.winter = nil

-- the AI's winter margins: the stock AI reads obj:getStaticFrictionCoef(), which ignores live ground models
T.check(W.aiParamsFor({ 'dry', 0, 5 }) == nil and W.aiParamsFor({ 'damp', 0, 5 }) == nil, 'dry / damp: stock AI')
T.check(W.aiParamsFor({ 'wet', 0, 5 }).staticFrictionCoefMult > 0.55, 'wet: a little care')
local ap = W.aiParamsFor({ 'ice', 0, -5, 'dry', 0, 0, 1 })
T.near(ap.staticFrictionCoefMult, 0.95 * (0.12 / 0.96) * W.AI_MARGIN, 1e-9, 'ice: the AI told of 1/8 the grip, with margin')
T.check(ap.lookAheadKv > 1.6 and ap.lookAheadKv <= 1.8, 'ice: a longer look-ahead')
T.check(W.aiParamsFor({ 'packed', 30, -5 }).staticFrictionCoefMult < 0.3, 'packed snow')
T.check(speeding.gripCommand(nil) == 'ai.setParameters({staticFrictionCoefMult = 0.950, lookAheadKv = 0.60})',
  'stock command')
local vars = { baseAggression = 0.35 }
gameplay_traffic.getTrafficVars = function() return vars end
gameplay_traffic.setTrafficVars = function(v) for k, x in pairs(v) do vars[k] = x end end
sent = {}
speeding.winter = { aiParams = function() return ap end, speedFactor = function() return 0.65 end }
speeding.update(1.0, { rules = { country = 'US' } })
local got = false
for _, c in ipairs(sent) do if c:find('staticFrictionCoefMult = 0.119', 1, true) then got = true end end
T.check(got, 'every AI car told the icy grip')
local n = #sent
speeding.update(1.0, { rules = { country = 'US' } })
T.check(#sent == n + 0 or not sent[#sent]:find('setParameters', 1, true), 'not again while unchanged')
speeding.winter = nil
speeding.update(1.0, { rules = { country = 'US' } })
got = false
for i = n + 1, #sent do if sent[i]:find('staticFrictionCoefMult = 0.950', 1, true) then got = true end end
T.check(got, 'winter gone: stock AI grip back')
-- the planning-horizon patch (stock ai.lua's getMinPlanLen floor) and its fallback cap
local stockText = 'local M = {}\n\n' .. W.AI_STOCK_FN .. '\n\nreturn M\n'
T.check(W.aiPatchLine(stockText) == 3, 'the stock function found: its line')
T.check(W.aiPatchLine(stockText:gsub('\n', '\r\n')) == 3, 'CRLF too')
T.check(W.aiPatchLine(stockText:gsub('max%(0%.2,', 'max(0.25,')) == nil, 'a changed line: not found')
T.check(W.aiPatchLine(nil) == nil, 'no file')
W._setAiText(stockText)
speeding.winter = W
W.force('ice')
local cmd = W.aiPatchCommand(true)
T.check(cmd and cmd:find('beamngEarth_winterAi.apply(3, 0.050)', 1, true), 'patch command with the line')
T.check(W.aiPatchCommand(false):find('apply(nil', 1, true), 'unpatch command')
local cap0 = W.aiSpeedCap(5)
T.near(cap0, math.sqrt(2 * 9.81 * W.aiParams().staticFrictionCoefMult * 20), 1e-9, 'unconfirmed: the fallback cap')
W.aiPatchResult(5, true, true)
T.check(W.aiSpeedCap(5) == nil and W.aiPatchedCar(5) == true, 'confirmed: no cap')
W.aiPatchResult(5, false, true)
T.check(W.aiSpeedCap(5) ~= nil, 'a failed patch: capped')
sent = {}
speeding.update(1.0, { rules = { country = 'US' } })
local sawPatch, sawCap = false, false
for _, c in ipairs(sent) do
  if c:find('beamngEarth_winterAi.apply(3', 1, true) then sawPatch = true end
  local v = tonumber(c:match('ai.setSpeed%(([%d%.]+)%)'))
  if v and v <= cap0 + 0.01 then sawCap = true end
end
T.check(sawPatch and sawCap, 'speeding: the patch sent, the unconfirmed car capped')
W._setAiText('local M = {} return M')
T.check(W.aiPatchCommand(true) == nil, 'pattern absent: no patch command')
W.aiPatchResult(5, nil, false)
T.check(W.aiSpeedCap(5) ~= nil, 'pattern absent: every car capped')
W.force(nil)
speeding.winter = nil
speeding.reset()
W._aggression(true)
T.check(vars.baseAggression == 0.25, 'slippery: traffic aggression 0.25')
W._aggression(false)
T.check(vars.baseAggression == 0.35, 'its own value back')

T.done('test_winter')
