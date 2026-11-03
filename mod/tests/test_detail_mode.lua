-- luajit mod/tests/test_detail_mode.lua  (the 'detail' near-visual mode, 2026-10-12)
-- The mesh-mode layout with the 512² physics block drawn as the near ground: far tiles under it are
-- covered (interior hidden, edge ring sunk) against the block's [c-512, c+512]² cover box; the raw view
-- (dev tools) shows the tiles again; the commit gate works on the 128 m grid of the forced L-1 tiles;
-- terrain.setBase puts the server's photo into every layer's base colour.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local lattice = require('beamngEarth/lattice')
lattice.configure(512, 2.0)
require('beamngEarth/follow').configure()
local farfield = require('beamngEarth/farfield')
local terrain = require('beamngEarth/terrain')

local free = { clock = function() return 0 end }
function free:try() return true end
function free:charge() end
function free:chargeMotion() end
local DT = 1 / 60
local function key(l, i, j) return l .. '_' .. i .. '_' .. j end
local function size(l) return 256 * 2 ^ l end
local function tile(a, l, i, j, opts)
  opts = opts or {}
  return { type = 'far_tile', anchorId = a, key = key(l, i, j), level = l,
    shape = '/temp/beamngEarth/t/far_' .. a .. '_' .. key(l, i, j) .. '.cdae',
    pos = { i * size(l), j * size(l), opts.z or 50 }, err = 0.5, sink = opts.sink, state = 'group' }
end
local function obj(k) local t = farfield._tile(k); return t and S.objects[t.id] end
local function visible(o) return o ~= nil and not o.hidden end
local cam = { x = 0, y = 0, z = 200, fx = 0, fy = 1, fz = 0, fovDeg = 60 }
local function step(n) for _ = 1, n or 1 do farfield.update(free, DT, cam) end end

farfield.setNearVisual('detail')
T.check(farfield.nearVisual() == 'detail', 'detail mode accepted')
farfield.switchAnchor(1, false, { cx = 0, cy = 0 })
farfield.onBlockCommit(0, 0)                         -- 512² block: cover box [-512, 512]²
local interior, edge, outside, straddle = key(-1, -1, -1), key(-1, -4, 0), key(-1, 4, 0), key(0, 1, 1)
farfield.onTile(tile(1, -1, -1, -1, { sink = 0.4 }))  -- [-128, 0]²: interior
farfield.onTile(tile(1, -1, -4, 0, { sink = 0.6 }))   -- [-512, -384] x [0, 128]: edge ring
farfield.onTile(tile(1, -1, 4, 0, { sink = 0.5 }))    -- [512, 640]: just outside
step()
farfield.onSwap({ type = 'far_swap', anchorId = 1, id = 1, add = { interior, edge, outside }, remove = {} })
step(30)
T.check(obj(interior).hidden, 'interior L-1 tile under the drawn block: hidden')
T.check(visible(obj(edge)) and math.abs(obj(edge).pos.z - (50 - 0.6)) < 1e-9, 'edge ring L-1 tile: shown, sunk by its sink')
T.check(visible(obj(outside)) and obj(outside).pos.z == 50, 'tile outside the block: true height')
T.check(farfield.stats().covered == 2, 'two covered tiles')

-- raw view: the tiles are the ground again, at their true heights, at once; and back
T.check(farfield.setRawView(true) == true, 'raw view on')
step()
T.check(visible(obj(interior)) and obj(interior).pos.z == 50, 'raw view: interior tile shown at true height')
T.check(visible(obj(edge)) and obj(edge).pos.z == 50, 'raw view: edge tile at true height')
T.check(farfield.stats().covered == 0 and farfield.stats().rawView == true, 'raw view: nothing covered')
T.check(farfield.uncoverReady(0, 0, 128, 0), 'raw view: the block is not drawn, nothing gates a move')
farfield.setRawView(false)
step(30)
T.check(obj(interior).hidden and math.abs(obj(edge).pos.z - (50 - 0.6)) < 1e-9, 'raw view off: covered again')

-- commit gate on the L-1 (128 m) grid: a 128 m move east uncovers the west column x in [-512, -384)
T.check(not farfield.uncoverReady(0, 0, 128, 0), 'gate closed: the uncovered column is not all live')
local ids = {}
for j = -4, 3 do
  if j ~= 0 then farfield.onTile(tile(1, -1, -4, j, { sink = 0.3 })); ids[#ids + 1] = key(-1, -4, j) end
end
step()
farfield.onSwap({ type = 'far_swap', anchorId = 1, id = 2, add = ids, remove = {} })
step(30)
T.check(farfield.uncoverReady(0, 0, 128, 0), 'gate open: every uncovered 128 m cell has a live tile')
-- an L0 tile across the new block's edge (its L-1 split has not run yet) keeps the gate closed
farfield.onTile(tile(1, 0, 1, 1))                     -- [256, 512]²; the block moved 128 m east covers [-384, 640]
step()
farfield.onSwap({ type = 'far_swap', anchorId = 1, id = 3, add = { straddle }, remove = {} })
step(30)
T.check(not farfield.uncoverReady(0, 0, 128, 0), 'gate closed: an L0 tile lies inside the new block (no L-1 split yet)')

-- mesh mode: nothing is covered, the gate is always open
farfield.setNearVisual('mesh')
step()
T.check(farfield.stats().covered == 0 and visible(obj(interior)), 'mesh mode: nothing covered')
T.check(farfield.uncoverReady(0, 0, 128, 0), 'mesh mode: gate open')
farfield.setNearVisual('detail')

-- terrain.setBase: the photo into every layer of the block, then back to the level's flat base
local mats = { { 'bngEarthGroundAsphalt', 'ASPHALT_OLD' }, { 'bngEarthGroundGrass', 'GRASS' }, { 'beamngEarthDirt', 'DIRT' } }
local dirt = createObject('TerrainMaterial'); dirt:registerObject('beamngEarthDirt')
FS = FS or {}
function FS:fileExists() return true end
local ok = terrain.load('/temp/hm.png', 2, 64, -512, -512, 0, { masks = { 'a.png', 'b.png', 'c.png' }, materials = mats },
  '/temp/beamngEarth/s/hm_1_0_0_base.png', 1024)
T.check(ok, 'terrain.load with a base photo')
local tb = terrain.get()
local asphalt = scenetree.findObject('bngEarthGroundAsphalt')
T.check(asphalt.fields.baseColorBaseTex == '/temp/beamngEarth/s/hm_1_0_0_base.png' and asphalt.fields.baseColorBaseTexSize == '512',
  'base photo set on a surface layer, over the block size in samples (1024 m / 2 m)')
T.check(asphalt.fields.baseColorDetailTex == '/assets/materials/terrain/asphalt/asphalt/t_asphalt_b.png',
  'surface layer created with its own detail texture')
T.check(asphalt.fields.baseColorDetailTexSize == '2' and asphalt.fields.baseColorMacroTexSize == '16',
  'repeat sizes in samples (4 m, 32 m on the 2 m block): they divide the 128 m block step')
T.check(dirt.fields.baseColorBaseTex == '/temp/beamngEarth/s/hm_1_0_0_base.png' and dirt.fields.baseColorDetailTexSize == nil,
  "the level's own material gets the photo, keeps its level look")
local u = tb.updated
T.check(#u >= 3 and u[#u - 2][1] == 0 and u[#u - 2][2] == 'bngEarthGroundAsphalt' and u[#u][1] == 2 and u[#u][2] == 'beamngEarthDirt',
  'updateMaterial per layer slot, in the block order')
T.check(terrain.base() == '/temp/beamngEarth/s/hm_1_0_0_base.png', 'terrain.base()')
terrain.load('/temp/hm.png', 2, 64, -512, -512, 0, { masks = { 'a.png', 'b.png', 'c.png' }, materials = mats }, false)
T.check(asphalt.fields.baseColorBaseTex == '/levels/beamng_earth/art/terrains/bngEarth_base_b.png'
  and asphalt.fields.baseColorBaseTexSize == '256', "false: back to the level's flat base")
local n = #tb.updated
terrain.load('/temp/hm.png', 2, 64, -512, -512, 0, { masks = { 'a.png', 'b.png', 'c.png' }, materials = mats })
T.check(#tb.updated == n, 'nil (other modes): the base is not touched')

T.done('test_detail_mode')
