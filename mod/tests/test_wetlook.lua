-- luajit mod/tests/test_wetlook.lua
-- beamngEarth/wetlook.lua (weather phase B): the material classes, the wetness steps and the own wetting / drying model,
-- the wet values; then the runtime against stub materials: a level change writes each material once, in batches, then
-- nothing per frame; drying puts the own values back (only where ours still hold); W1's road state wins over the rain.
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
near(tonumber(col:match('^(%S+)')), 0.5 * (1 - W.WET_DARK * 0.55), 1e-4, 'soaked asphalt darker')
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
-- W1's road state wins over the rain
package.loaded['beamngEarth/winter'] = { _state = function() return { roads = { {}, {}, { 'wet' } } }, 2 end }
W.reset()
W.update(W.CHECK_S, 0, true)
check(W.source == 'road state' and W.level() == W.levelOf(0.8), 'W1 says wet: wet without rain')
package.loaded['beamngEarth/winter'] = nil

T.done('test_wetlook')
