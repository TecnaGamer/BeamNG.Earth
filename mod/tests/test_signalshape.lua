-- The signal's handedness: reflected geometry, outward winding, node transforms, cached from the player's asset.
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local shape = require('beamngEarth/signalshape')
local dae = [[<COLLADA><library_materials><material name="instanced_trafficlight"/></library_materials>
<library_geometries><geometry><mesh>
<source id="positions"><float_array id="p" count="9">1 0 0 2 0 0 1 1 0</float_array>
<accessor stride="3"><param name="X"/><param name="Y"/><param name="Z"/></accessor></source>
<source id="normals"><float_array id="n" count="9">0 0 1 0 0 1 0 0 1</float_array>
<accessor stride="3"><param name="X"/><param name="Y"/><param name="Z"/></accessor></source>
<source id="uv"><float_array id="t" count="6">0 0 1 0 0 1</float_array>
<accessor stride="2"><param name="S"/><param name="T"/></accessor></source>
<triangles count="1"><input semantic="VERTEX" offset="0"/><input semantic="NORMAL" offset="1"/>
<input semantic="TEXCOORD" offset="2"/><p>0 0 0 1 1 1 2 2 2</p></triangles>
</mesh></geometry></library_geometries><library_visual_scenes><visual_scene><node>
<translate>3 4 5</translate><rotate>1 2 3 90</rotate>
<matrix>1 0 0 0 0 1 0 0 0 0 1 0 3 4 5 1</matrix><scale>1 2 3</scale>
</node></visual_scene></library_visual_scenes></COLLADA>]]
local mirrored = shape.mirroredDae(dae)
T.check(mirrored ~= nil, 'a triangulated static signal can be reflected')
T.check(mirrored:find('-1 0 0 -2 0 0 -1 1 0', 1, true) ~= nil, 'vertex positions reflected across x')
T.check(mirrored:find('<p>0 0 0 2 2 2 1 1 1</p>', 1, true) ~= nil, 'every corner tuple reversed, with its UV/normal indices')
T.check(mirrored:find('>0 0 1 0 0 1 0 0 1</float_array>', 1, true) ~= nil
  or mirrored:find('>-0 0 1 -0 0 1 -0 0 1</float_array>', 1, true) ~= nil, 'outward +z normals retained')
T.check(mirrored:find('<float_array id="t" count="6">0 0 1 0 0 1</float_array>', 1, true) ~= nil,
  'texture coordinates unchanged')
T.check(mirrored:find('<translate>-3 4 5</translate>', 1, true) ~= nil, 'node translation reflected too')
T.check(mirrored:find('<rotate>1 -2 -3 90</rotate>', 1, true) ~= nil, 'axis-angle conjugated by reflection')
T.check(mirrored:find('-3 4 5 1</matrix>', 1, true) ~= nil, 'matrix translation reflected')
T.check(mirrored:find('<scale>1 2 3</scale>', 1, true) ~= nil, 'no negative object scale')
T.check(mirrored:find('instanced_trafficlight', 1, true) ~= nil, 'switchable lamp material kept')
T.check(shape.mirroredDae(nil) == nil and shape.mirroredDae('<COLLADA/>') == nil, 'missing asset refused')
T.check(shape.mirroredDae(dae:gsub('<triangles count="1">', '<polylist count="1">')) == nil,
  'unsupported geometry is not published as a usable copy')

local files, reads, writes = {}, 0, 0
FS = { fileExists = function(_, path) return files[path] ~= nil end }
readFile = function(path) reads = reads + 1; return path == shape.STOCK .. '.dae' and dae or files[path] end
writeFile = function(path, text) writes = writes + 1; files[path] = text end
T.check(shape.ensureLeft() == shape.LEFT and files[shape.LEFT .. '.dae'] == mirrored, 'derived from the local stock asset')
T.check(shape.ensureLeft() == shape.LEFT and reads == 1 and writes == 1, 'subsequent heads reuse the cached copy')
files = {}
readFile = function() return nil end
T.check(shape.ensureLeft() == nil and next(files) == nil, 'missing source creates no broken shape file')
T.done('test_signalshape')
