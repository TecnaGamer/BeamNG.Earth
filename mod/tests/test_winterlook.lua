-- luajit mod/tests/test_winterlook.lua  (winter's snow on the ground, winterlook.lua): the snow layer wears the stock
-- snow textures while the server has winter on and its own look again after, sized in samples of the block's
-- square; the grass thins by the snow's depth; nothing is loaded or changed while winter is off.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local seasonlook = require('beamngEarth/seasonlook')
seasonlook.setSnow(nil)
T.check(package.loaded['beamngEarth/winterlook'] == nil, 'winter off: winterlook is never loaded')

local W = require('beamngEarth/winterlook')
local terrain = require('beamngEarth/terrain')
local gc = require('beamngEarth/groundcover')

-- pure parts
T.check(W.grassFactor(nil) == 1 and W.grassFactor({ mode = 'real' }) == 1, 'no cover: all the grass')
T.check(W.grassFactor({ cover = 4, depth = 1 }) == W.GRASS[1] and W.GRASS[1] < 1, 'a dusting thins it')
T.check(W.grassFactor({ cover = 8, depth = 5 }) < W.grassFactor({ cover = 7, depth = 2 }), 'deeper: less')
local look = W.snowLook(function() return true end)
T.check(look and look.baseColorDetailTex:find('t_snow_b', 1, true) ~= nil, 'the stock snow grain')
T.check(look.normalDetailTexSize == 4 and look.normalMacroTexSize == 32 and 128 % 32 == 0, 'repeats divide the block step')
T.check(W.snowLook(function(p) return not p:find('macro', 1, true) end) == nil, 'a texture missing: no look')

-- the snow layer's material, with its level look (sand)
local m = S.newObject('TerrainMaterial')
m:registerObject(W.MATERIAL)
m:setField('baseColorDetailTex', 0, '/assets/materials/terrain/sand/t_sand/t_sand_b.png')
m:setField('normalDetailTexSize', 0, '4')
terrain.square = 2      -- (the light 2 m ground: 4 m is 2 samples)
local rebuilt = 0
gc.rebuild = function() rebuilt = rebuilt + 1 end
seasonlook.setSnow({ mode = 'heavy', cover = 8, depth = 4 })
T.check(m.fields.baseColorDetailTex:find('t_snow_b', 1, true) ~= nil, 'winter on: the snow layer wears snow')
T.check(m.fields.normalDetailTexSize == '2' and m.fields.normalMacroTexSize == '16', 'sizes in samples of the square')
T.check(gc.snow == W.GRASS[4], 'the grass under heavy snow')
seasonlook.setSnow({ mode = 'light', cover = 7, depth = 2 })
T.check(gc.snow == W.GRASS[2], 'a live change: the grass follows')
seasonlook.setSnow(nil)
T.check(m.fields.baseColorDetailTex == '/assets/materials/terrain/sand/t_sand/t_sand_b.png'
  and m.fields.normalDetailTexSize == '4', 'winter gone: its own look back')
T.check(gc.snow == 1, 'and all the grass')
seasonlook.setSnow({ mode = 'heavy', cover = 8, depth = 4 })
seasonlook.restore()
T.check(m.fields.normalDetailTexSize == '4' and gc.snow == 1 and not W.looked, 'the level ended: restored')

T.done('test_winterlook')
