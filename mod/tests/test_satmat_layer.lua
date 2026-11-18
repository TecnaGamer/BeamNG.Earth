-- luajit mod/tests/test_satmat_layer.lua  (the near tiles' asphalt layer: satmat setTexture, 2026-10-22)
-- A tile whose road mask exists beside its texture gets the second layer (the mask as its opacity); a pooled material
-- moved on to a tile without one goes back to one layer, its base map following the texture.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local satmat = require('beamngEarth/satmat')

local exists = {}
FS = { fileExists = function(_, p) return exists[p] == true end }
local function mat()
  local f = {}
  return { f = f,
    setField = function(self, k, l, v) f[k .. '/' .. l] = v end,
    getField = function(self, k, l) return f[k .. '/' .. l] end }
end

T.check(satmat.maskOf('/temp/s/far_3_-1_0_-1.dds') == '/temp/s/far_3_-1_0_-1_road.dds', 'mask beside the texture')
T.check(satmat.maskOf('/temp/s/far_3_-1_0_-1_r2.dds') == '/temp/s/far_3_-1_0_-1_road.dds', 'a revision: the same mask')

local m = mat()
satmat._setTexture(m, '/temp/s/far_3_0_0_0.dds')
T.check(m.f['colorMap/0'] == '/temp/s/far_3_0_0_0.dds' and m.f['activeLayers/0'] == nil, 'no mask: one plain layer')

exists['/temp/s/far_3_-1_0_-1_road.dds'] = true
satmat._setTexture(m, '/temp/s/far_3_-1_0_-1.dds')
T.check(m.f['activeLayers/0'] == '2' and m.f['version/0'] == '1.5', 'a mask: two layers')
T.check(m.f['opacityMap/1'] == '/temp/s/far_3_-1_0_-1_road.dds', 'the mask is the second layer\'s opacity')
T.check(m.f['baseColorMap/0'] == '/temp/s/far_3_-1_0_-1.dds', 'the photo underneath')
T.check(m.f['detailMap/1'] ~= nil and m.f['detailMapUseUV/1'] == '1', 'the asphalt layer tiled on UV1')
T.check(m.f['detailMap/1'] ~= nil, 'the asphalt grain on the second layer')

satmat._setTexture(m, '/temp/s/far_3_1_0_0.dds')       -- the pooled material moves on to a tile without a mask
T.check(m.f['activeLayers/0'] == '1', 'back to one layer')
T.check(m.f['colorMap/0'] == '/temp/s/far_3_1_0_0.dds' and m.f['version/0'] == '1',
  'a plain DXT1 texture: back to the 1.0 Material (a v1.5 one reads it as linear), its map following the texture')

-- sRGB-tagged textures (server imagery.FAR_SRGB, 2026-11-16): a v1.5 Material, one layer
local head = { ['/temp/s/far_3_2_0_0.dds'] = 'DDS ' .. string.rep('\0', 80) .. 'DX10' .. string.rep('\0', 60),
  ['/temp/s/far_3_3_0_0.dds'] = 'DDS ' .. string.rep('\0', 80) .. 'DXT1' .. string.rep('\0', 40) }
readFile = function(p) return head[p] end
local s = mat()
satmat._setTexture(s, '/temp/s/far_3_2_0_0.dds')
T.check(s.f['version/0'] == '1.5' and s.f['baseColorMap/0'] == '/temp/s/far_3_2_0_0.dds' and s.f['activeLayers/0'] == '1',
  'an sRGB texture: a v1.5 Material, the photo its base map')
T.check(satmat.isSrgb('/temp/s/far_3_2_0_0.dds') and not satmat.isSrgb('/temp/s/far_3_3_0_0.dds')
  and not satmat.isSrgb('/temp/s/none.dds'), 'isSrgb from the DDS header (DX10), a missing file: no')
satmat._setTexture(s, '/temp/s/far_3_3_0_0.dds')
T.check(s.f['version/0'] == '1' and s.f['colorMap/0'] == '/temp/s/far_3_3_0_0.dds', 'then a plain one: 1.0 again')
readFile = nil

T.done('test_satmat_layer')
