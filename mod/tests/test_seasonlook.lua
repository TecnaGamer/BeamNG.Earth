-- luajit mod/tests/test_seasonlook.lua  (the trees' leaves in the season)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local S = require('beamngEarth/seasonlook')
local oct14 = 287
local md = S.fallAmount(oct14, 39.2)
T.check(md > 0.3 and md < 0.7, 'mid October in Maryland: turning')
T.check(S.fallAmount(182, 39.2) == 0 and S.fallAmount(oct14, 10) == 0 and S.fallAmount(oct14, -39.2) == 0,
  'summer, tropics, southern spring: none')
T.check(S.fallAmount(oct14, 46) > md, 'further north: further along')
local c = S.blend({ 0.8, 0.8, 0.8, 1 }, { 1.5, 0.5, 0.5 }, 0.5)
T.check(math.abs(c[1] - 1.0) < 1e-6 and math.abs(c[2] - 0.6) < 1e-6 and c[4] == 1, 'half the tint, times the material\'s own')
local c0 = S.blend({ 0.8, 0.8, 0.8, 1 }, { 1.5, 0.5, 0.5 }, 0)
T.check(c0[1] == 0.8 and c0[2] == 0.8, 'amount 0: the original')
local oak = S.SPECIES[1]
local _, tw = S.speciesTint(oak, 'auto', 15, 39.2)
T.check(tw == 1 and S.speciesTint(oak, 'auto', 15, 39.2) == oak.winter, 'mid January: the oaks\' dead brown leaves')
local ts, a = S.speciesTint(oak, 'auto', 112, 39.2)
T.check(ts == S.SPRING and a > 0.9, 'late April: the fresh flush')
local _, a0 = S.speciesTint(oak, 'auto', 190, 39.2)
T.check(a0 == 0, 'July: summer')
local _, aoff = S.speciesTint(oak, nil, 15, 39.2)
T.check(aoff == 0, 'season off: nothing')

-- the far imposters of the species' shapes take their tint (the user, 2026-10-29: flat far trees kept summer green)
do
  local oak = S.imposterSpecies('imposter_mat__levels_east_coast_usa_art_shapes_trees_trees_oak_tree_oak_forest_b.dae')
  T.check(oak and oak.folders[1] == 'trees_oak', 'an oak imposter: the oak tint')
  T.check(S.imposterSpecies('imposter_mat__levels_east_coast_usa_art_shapes_trees_trees_aspen_tree_aspen_forest_b.dae') ==
    S.SPECIES[3], 'an aspen imposter: the birch / aspen gold')
  T.check(S.imposterSpecies('imposter_mat__levels_west_coast_usa_art_shapes_trees_trees_conifer_pine_radiata_large.dae') == nil,
    'a conifer imposter: untouched')
  T.check(S.imposterSpecies('m_oak_leaves_distant') == nil, 'not an imposter material: none')
end

do
  -- an imposter material (version 1: baseColorFactor and diffuseColor are one colour): tinted once, and put back
  local col = '1 1 1 1'
  local mat = { getField = function(_, f) return (f == 'baseColorFactor' or f == 'diffuseColor') and col or '' end,
    setField = function(_, f, _, v) if f == 'baseColorFactor' or f == 'diffuseColor' then col = v end end,
    flush = function() end, reload = function() end }
  local name = 'imposter_mat__levels_east_coast_usa_art_shapes_trees_trees_oak_tree_oak_forest_b.dae'
  local saved = scenetree
  scenetree = { findObject = function(n) return n == name and mat or nil end,
    findClassObjects = function() return { name } end }
  S.setMode('fall')
  S.update(true, 39.2)
  local r = tonumber(col:match('^(%S+)'))
  T.check(math.abs(r - S.SPECIES[1].tint[1]) < 1e-3, 'an imposter takes the full fall tint once: ' .. col)
  S.restore()
  T.check(col == '1.000 1.000 1.000 1.000', 'leaving the level: its own colour back: ' .. col)
  S.setMode(nil)
  scenetree = saved
end

T.done('test_seasonlook')
