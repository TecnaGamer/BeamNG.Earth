-- luajit mod/tests/test_terrain_rc.lua  (terrain.lua: a ground layer whose zero-mean colour copy the server did not
-- write, server/terraintex.py, takes the stock texture it is made from before the block's first material build)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local terrain = require('beamngEarth/terrain')

local RC = '/temp/beamngEarth/terraintex/bne_rc_grass__t_grass_01__t_grass_01_b.png'
local RCM = '/temp/beamngEarth/terraintex/bne_rc_soil__macro_clumply__t_macro_clumpy_b.png'
local grass = S.newObject('TerrainMaterial'); grass:registerObject('bngEarthGroundGrass')
grass:setField('baseColorDetailTex', 0, RC)
grass:setField('baseColorMacroTex', 0, RCM)
local mud = S.newObject('TerrainMaterial'); mud:registerObject('bngEarthGroundMud')
mud:setField('baseColorDetailTex', 0, '/temp/beamngEarth/terraintex/bne_rc_mud__mud__t_mud_b.png')
FS = { fileExists = function(_, p) return p ~= RC and p ~= RCM end }   -- grass's copies missing, mud's there
terrain.checkTileTextures()
T.check(grass:getField('baseColorDetailTex', 0) == '/assets/materials/terrain/grass/t_grass_01/t_grass_01_b.png'
  and grass:getField('baseColorMacroTex', 0) == '/assets/materials/terrain/soil/macro_clumply/t_macro_clumpy_b.png',
  'missing copies: the stock textures')
T.check(mud:getField('baseColorDetailTex', 0) == '/temp/beamngEarth/terraintex/bne_rc_mud__mud__t_mud_b.png',
  'a copy that is there stays')
T.check(terrain.LOOKS.bngEarthGroundGrass.baseColorMacroTex:find('^/temp/beamngEarth/terraintex/bne_rc_') ~= nil,
  'LOOKS name the copies')
T.done('test_terrain_rc')
