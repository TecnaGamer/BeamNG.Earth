-- luajit mod/tests/test_wetlook.lua
-- beamngEarth/wetlook.lua (weather phase B): the material classes, the wetness steps and the own wetting / drying model,
-- the wet values; then the runtime against stub materials: a level change writes each material once, in batches, then
-- nothing per frame; drying puts the own values back (only where ours still hold); the wetter of W1's road state and the rain model.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local W = require('beamngEarth/wetlook')
local check, near = T.check, T.near

check(W.classOf('sidewalk_concrete') == 'concrete' and W.classOf('line_white') == 'marking', 'exact names')
check(W.classOf('bne_bld_roof_flat') == 'roof' and W.classOf('bne_bld_brick') == 'wall', 'roofs before walls')
check(W.classOf('bne_bld_skywin') == nil and W.classOf('bngEarthFar_v3') == nil and W.classOf(nil) == nil, 'not ours to wet')
check(W.classOf('Sidewalk_Tiles_02') == 'concrete' and W.classOf('road_kerb_glass') == nil and W.classOf('bngEarthGroundAsphalt') == nil,
  'words: other sets\' sidewalks; not glass, not our terrain layers')
check(W.levelOf(0) == 0 and W.levelOf(1) == W.LEVELS and W.levelOf(0.5) == 2, 'levels')
-- the own model: rain wets within minutes, the dry spell dries over DRY_S
local w = 0
for _ = 1, 30 do w = W.wetStep(w, 4, 60) end
check(w > 0.95, 'half an hour of 4 mm/h: soaked (' .. w .. ')')
local w2 = W.wetStep(0, 0.3, 600)
check(w2 > 0.2 and w2 < 0.6, 'drizzle: damp')
check(W.wetStep(1, 0, W.DRY_S / 2) == 0.5 and W.wetStep(0.1, 0, W.DRY_S) == 0, 'drying')
-- the wet values: darker by porosity, glossier by gloss
local col, ro = W.wetValues('asphalt', { 0.5, 0.5, 0.5, 1 }, 0.8, W.LEVELS)
near(tonumber(col:match('^(%S+)')), 0.5 * (1 - W.WET_DARK * W.CLASSES.asphalt[1]), 1e-4, 'soaked asphalt darker')
near(tonumber(ro), 0.8 * (1 - W.WET_ROUGH * 0.75), 1e-4, 'and glossier')
col, ro = W.wetValues('metal', { 0.5, 0.5, 0.5, 1 }, nil, W.LEVELS)
check(col == nil and tonumber(ro) < 1, 'metal: not darker, glossier')
check(W.wetFromCondition('wet') == 0.8 and W.wetFromCondition('dry') == 0 and W.wetFromCondition(nil) == nil, 'W1 conditions')

-- runtime
local mats = {}
for _, n in ipairs({ 'sidewalk_concrete', 'line_white', 'bne_bld_roof_flat', 'bngEarthFar_v3' }) do
  local m = S.newObject('Material')
  m:registerObject(n)
  m:setField('baseColorFactor', 0, '0.8 0.8 0.8 1')
  m:setField('roughnessFactor', 0, '0.9')
  mats[n] = m
end
check(W.update(5, 0, true) == false, 'dry: nothing to do')
W._setOwn(1)
W.update(W.CHECK_S, 4, true)          -- the level changes: the batch starts
for _ = 1, 5 do W.update(0.016, 4, true) end
check(W.level() == W.LEVELS, 'soaked: the top level')
check(mats.sidewalk_concrete.fields.baseColorFactor ~= '0.8 0.8 0.8 1' and tonumber(mats.line_white.fields.roughnessFactor) < 0.9,
  'our materials wet')
check(mats.bngEarthFar_v3.fields.baseColorFactor == '0.8 0.8 0.8 1', 'not the far imagery')
local reloads = S.calls.reload_Material or 0
for _ = 1, 50 do W.update(0.016, 4, true) end
check((S.calls.reload_Material or 0) == reloads, 'steady wet: no material writes per frame')
-- someone else changes a material meanwhile: theirs stays when we dry
mats.line_white:setField('roughnessFactor', 0, '0.4')
W.update(W.CHECK_S, 0, false)          -- the weather off: back to dry at once
for _ = 1, 5 do W.update(0.016, 0, false) end
check(W.level() == 0 and mats.sidewalk_concrete.fields.baseColorFactor == '0.8000 0.8000 0.8000 1.0000', 'dry: own colour back')
check(mats.line_white.fields.roughnessFactor == '0.4', 'someone else\'s value kept')
-- a teleport leaves the old place's rain behind (the user, 2026-11-27)
W.reset()
for _ = 1, 400 do W.update(W.CHECK_S, 8, true) end
for _ = 1, 50 do W.update(0.016, 8, true) end
check(W.wetness() > 0.5 and W.level() > 0, 'teleport: wet before')
W.onTeleport()
W.update(0.016, 0, true)
for _ = 1, 50 do W.update(0.016, 0, true) end
check(W.wetness() == 0 and W.level() == 0, 'teleport: a dry place is dry at once')
W.update(W.CHECK_S, 8, true)
check(W.wetness() > 0 and W.wetness() < 0.5, 'teleport: rain at the new place wets it again over minutes')
-- W1's road state wins over the rain
package.loaded['beamngEarth/winter'] = { _state = function() return { roads = { {}, {}, { 'wet' } } }, 2 end }
W.reset()
W.update(W.CHECK_S, 0, true)
check(W.source == 'road state' and W.level() == W.levelOf(0.8), 'W1 says wet: wet without rain')
-- ... but a dry real road under a storm shown (a preset; the 2026-11-26 playtest: 'wet lvl=0 own=0.97 src=road state'):
-- the wetter of the two
package.loaded['beamngEarth/winter'] = { _state = function() return { roads = { {}, {}, { 'dry' } } }, 2 end }
W.reset()
W._setOwn(0.97)
W.update(W.CHECK_S, 8, true)
check(W.source == 'rain' and W.level() == W.LEVELS, 'the real road dry, the storm shown: wet by the rain model ('
  .. W.debugLine() .. ')')
check(select(1, W.wetOf(true, 0.8, 0.2)) == 0.8 and select(2, W.wetOf(true, 0.8, 0.2)) == 'road state'
  and select(1, W.wetOf(true, nil, 0.3)) == 0.3 and select(1, W.wetOf(false, 1, 1)) == 0, 'wetOf: the wetter one; off: dry')
package.loaded['beamngEarth/winter'] = nil

-- puddles: none below PUDDLE_MIN, then deeper steps lower the material's alpha threshold (the deep pools first);
-- shown and hidden through roads.setPuddles at step changes only, never per frame
check(W.puddleStep(0) == 0 and W.puddleStep(W.PUDDLE_MIN - 0.01) == 0 and W.puddleStep(W.PUDDLE_MIN) == 1
  and W.puddleStep(1) == W.PUDDLE_STEPS, 'puddle steps')
check(W.puddleRef(0) == 255 and W.puddleRef(1) == W.PUDDLE_REF[1] and W.puddleRef(W.PUDDLE_STEPS) == W.PUDDLE_REF[2]
  and W.puddleRef(2) < W.puddleRef(1), 'thresholds fall as they fill')
do
  local pm = S.newObject('Material')
  pm:registerObject(W.PUDDLE_MAT)
  pm:setField('alphaRef', 0, '255')
  local shown, calls = nil, 0
  package.loaded['beamngEarth/roads'] = { setPuddles = function(on) shown = on; calls = calls + 1 end }
  W.reset()
  check(shown == false, 'a level start: the puddles made hidden')
  W._setOwn(1)
  W.update(W.CHECK_S, 8, true)
  check(shown == true and pm.fields.alphaRef == tostring(W.PUDDLE_REF[2]), 'soaked: shown, the full extent')
  local c0 = calls
  for _ = 1, 30 do W.update(0.016, 8, true) end
  W.update(W.CHECK_S, 8, true)
  check(calls == c0, 'steady: no more calls')
  W._setOwn(0.5)
  W.update(W.CHECK_S, 0, true)
  check(shown == true and tonumber(pm.fields.alphaRef) > W.PUDDLE_REF[2], 'drying: smaller')
  for _ = 1, 10 do W.update(0.016, 0, true) end      -- (the drying's material batch)
  W._setOwn(0.1)
  W.update(W.CHECK_S, 0, true)
  check(shown == false and pm.fields.alphaRef == '255', 'dry: hidden')
  W._setOwn(1)
  W.update(W.CHECK_S, 8, true)
  W.restoreAll()
  check(shown == false, 'the weather off: hidden at once')
  package.loaded['beamngEarth/roads'] = nil
end

-- grip (the user, 2026-11-26: "grip should follow the weather shown in game"): the own wetness as dry / damp / wet,
-- with a margin either side of a change; handed to winter.lua at changes only, the wet look on or off
check(W.gripCond(0) == nil and W.gripCond(0.3) == 'damp' and W.gripCond(0.9) == 'wet', 'grip conditions')
check(W.gripCond(W.GRIP_DAMP + 0.01, nil) == nil and W.gripCond(W.GRIP_DAMP + 0.01, 'damp') == 'damp'
  and W.gripCond(W.GRIP_DAMP - 0.01, 'damp') == 'damp' and W.gripCond(W.GRIP_DAMP - 0.04, 'damp') == nil,
  'damp: a margin either side')
check(W.gripCond(W.GRIP_WET + 0.01, 'damp') == 'damp' and W.gripCond(W.GRIP_WET - 0.01, 'wet') == 'wet'
  and W.gripCond(W.GRIP_WET + 0.04, 'damp') == 'wet' and W.gripCond(W.GRIP_WET - 0.04, 'wet') == 'damp',
  'wet: a margin either side')
do
  local got = {}
  package.loaded['beamngEarth/winter'] = { setShown = function(c) got[#got + 1] = tostring(c) end }
  W.reset()
  check(#got == 0, 'dry at a reset: winter not called')
  W._setOwn(0.97)
  W.update(W.CHECK_S, 8, false, true)
  check(got[#got] == 'wet' and W.grip() == 'wet' and W.level() == 0, 'the storm shown, wet look off: wet grip, no look')
  local n = #got
  for _ = 1, 20 do W.update(W.CHECK_S, 8, false, true) end
  check(#got == n, 'steady: winter not called again')
  W._setOwn(0.3)
  W.update(W.CHECK_S, 0, false, true)
  check(got[#got] == 'damp', 'drying: damp')
  W.update(W.CHECK_S, 0, false, false)
  check(got[#got] == 'nil' and W.grip() == nil and W.wetness() == 0, 'the setting off: dry grip, no memory')
  W._setOwn(0.97)
  W.update(W.CHECK_S, 8, true, true)
  W.reset()
  check(got[#got] == 'nil', 'a level end: dry grip')
  check(W.puddleRefShown() == 255, 'no puddles shown: threshold 255')
  package.loaded['beamngEarth/winter'] = nil
end

-- the new puddle look (the puddle plan R1, setting weatherPuddleLook: M.v2): the wet state is wetstate.lua's, the puddle
-- material becomes dark water mirroring a sky cubemap, its steps one a check, the material's own values back when off
do
  local WS = require('beamngEarth/wetstate')
  local now = 1764200000
  WS.clock = function() return now end
  local pm = scenetree.findObject(W.PUDDLE_MAT)
  pm:setField('baseColorFactor', 0, '0.36 0.39 0.44 1')
  pm:setField('roughnessFactor', 0, '0.06')
  pm:setField('cubemap', 0, '')
  local li = S.newObject('LevelInfo')
  li:registerObject('theLevelInfo')
  li:setField('globalEnviromentMap', 0, 'BNG_Sky_01_cubemap')
  local cube = S.newObject('CubemapData')
  cube:registerObject('bne_wetsky_overcast_city')
  local shown = nil
  package.loaded['beamngEarth/roads'] = { setPuddles = function(on) shown = on end }
  local gripGot = {}
  package.loaded['beamngEarth/winter'] = { setShown = function(c) gripGot[#gripGot + 1] = tostring(c) end }
  W.reset()
  W.setV2(true)
  check(W.v2 and pm.fields.baseColorFactor == '0.36 0.39 0.44 1', 'on: nothing written before a check')
  local storm = { rainMmH = 15, code = 95, tempC = 17, cover = { low = 100, mid = 100 } }
  local calls = 0
  local function src() calls = calls + 1; return { msg = storm, src = 'preset', key = 'storm@1', tC = 17 } end
  W.update(W.CHECK_S, 15, true, true, src)
  check(calls == 1 and pm.fields.baseColorFactor == W.V2_LOOK.base and pm.fields.roughnessFactor == W.V2_LOOK.rough
    and pm.fields.cubemap == 'bne_wetsky_overcast_city', 'the storm: dark water mirroring the overcast sky ('
    .. tostring(pm.fields.cubemap) .. ')')
  check(shown == true and pm.fields.alphaRef == tostring(W.puddleRefV2(1)) and W.wetness() == WS.wTarget(15000) / 1000
    and W.grip() == 'wet', 'a preset starts soaked: grip wet, the puddles from the first step')
  for _ = 1, 30 do W.update(0.016, 15, true, true, src) end
  check(calls == 1, 'between checks: the weather not asked again')
  for _ = 1, 20 do W.update(W.CHECK_S, 15, true, true, src) end
  check(pm.fields.alphaRef == tostring(W.V2_REF[2]) and W.puddleRefShown() == W.V2_REF[2], 'a step a check: full')
  local writes = W.writes
  for _ = 1, 10 do W.update(W.CHECK_S, 15, true, true, src) end
  check(W.writes == writes, 'steady: no material writes')
  -- the weather turns dry and sunny: the steps fall one a check as the water goes, the cubemap follows the sky
  storm = { rainMmH = 0, code = 0, tempC = 20, cover = { low = 0, mid = 0 } }
  local clear = S.newObject('CubemapData')
  clear:registerObject('bne_wetsky_clear_city')
  now = now + 14 * 3600
  W.update(W.CHECK_S, 0, true, true, src)
  check(pm.fields.cubemap == 'bne_wetsky_clear_city' and tonumber(pm.fields.alphaRef) > W.V2_REF[2], 'drying in sun: the clear sky, smaller')
  check(li.fields.globalEnviromentMap == 'bne_wetsky_clear_city', 'the level\'s environment map: ours while wet')
  -- off: the material's own values back, the old steps
  W.setV2(false)
  check(pm.fields.baseColorFactor == '0.36 0.39 0.44 1' and pm.fields.roughnessFactor == '0.06' and pm.fields.cubemap == ''
    and tonumber(pm.fields.alphaRef) == W.puddleRef(math.min(W.PUDDLE_STEPS, 11)), 'off: the old look back')
  check(WS.state() == nil, 'off: no wet state kept')
  check(li.fields.globalEnviromentMap == 'BNG_Sky_01_cubemap', 'off: the level\'s own environment map back')
  -- frozen: no water drawn
  W.reset()
  W.setV2(true)
  storm = { rainMmH = 15, code = 95, tempC = 17 }
  package.loaded['beamngEarth/winter'] = { setShown = function() end,
    _state = function() return { roads = { {}, {}, { 'packed' } } }, 2 end }
  for _ = 1, 5 do W.update(W.CHECK_S, 15, true, true, src) end
  check(pm.fields.alphaRef == '255' or W.puddleRefShown() == 255, 'packed snow: no water steps')
  -- the wet look off, the grip on: the state runs for the grip, no puddles
  package.loaded['beamngEarth/winter'] = { setShown = function() end }
  W.reset()
  W.update(W.CHECK_S, 15, false, true, src)
  check(W.grip() == 'wet' and W.puddleRefShown() == 255, 'grip only: wet, no puddles')
  W.update(W.CHECK_S, 15, false, false, src)
  check(WS.state() == nil and W.wetness() == 0, 'both off: nothing kept')
  W.setV2(false)
  package.loaded['beamngEarth/roads'] = nil
  package.loaded['beamngEarth/winter'] = nil
end
check(W.puddleRefV2(0, 12) == 255 and W.puddleRefV2(1, 12) == W.V2_REF[1] and W.puddleRefV2(12, 12) == W.V2_REF[2],
  'new steps: thresholds')
check(W.skyCube({ rainMmH = 1 }, true) == 'bne_wetsky_overcast_city' and W.skyCube({ cover = { low = 10 } }, false)
  == 'bne_wetsky_clear_open', 'the sky mirrored')

T.done('test_wetlook')
