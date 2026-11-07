local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local gc = require('beamngEarth/groundcover')
local fixture = dofile(dir .. '/groundcover_fixture.lua')
_G.jsonNull = {}
_G.flushGroundCoverGrids = function() end

local function fresh(options)
  gc.setEnabled(false)
  gc.reset()
  fixture(gc, options)
  gc.enabled = true
end
fresh()
T.check(gc.ensure(), 'all verified stock sources resolve')
local totals, shares = {}, {}
for _, d in ipairs(gc.DEFS) do
  local o = scenetree.findObject(gc.PREFIX .. (d.name or d.src))
  totals[d.layer] = (totals[d.layer] or 0) + tonumber(o.fields.maxElements)
  shares[d.layer] = (shares[d.layer] or 0) + d.share
  T.check(o.fields.layer == d.layer and o.fields.useNewPlacementGpu == '0', 'category layer and CPU placement: ' .. d.layer)
  local radius = d.share == 0.15 and 100 or d.share == 0.17 and 70
    or d.share == 0.05 and (d.layer:find('Dusty', 1, true) and 70 or 60) or 50
  T.check(tonumber(o.fields.radius) == radius, 'the original role radius is independent of asset source: ' .. d.layer)
end
for _, layer in ipairs({ 'bngEarthGroundGrassDry', 'bngEarthGroundGrassLight' }) do
  T.near(shares[layer], 0.47, 1e-9, 'same rural share budget ' .. layer)
  T.check(totals[layer] == totals.bngEarthGroundGrass, 'same rural population ' .. layer)
end
for _, layer in ipairs({ 'bngEarthGroundDustyLight', 'bngEarthGroundDustyLush' }) do
  T.near(shares[layer], 0.28, 1e-9, 'same scrub share budget ' .. layer)
  T.check(totals[layer] == totals.bngEarthGroundDusty, 'same scrub population ' .. layer)
end
T.check(shares.bngEarthGroundLawn == 0.1, 'lawn remains short-only with the old share')

-- Simulate identically named materials with distinct installed textures.
local docs = fixture(gc)
for token, def in pairs(docs) do
  if type(def) == 'table' and def.class == 'GroundCover' then
    def.Types[#def.Types + 1] = { layer = 'Forest', sizeMin = 0.6, sizeMax = 1.2, billboardUVs = { 0, 0, 1, 1 } }
  end
end
local shared = 'shared_grass'
for _, source in ipairs({ 'italy', 'hirochi' }) do
  local paths = gc.SOURCES[source]
  docs[paths.materials][shared] = { name = shared, mapTo = shared, version = 1.5, alphaTest = true,
    Stages = { { baseColorMap = '/textures/' .. source .. '.png', normalMap = 'relative.normal.png',
      baseColorFactor = { 0.2, 0.3, 0.4, 1 } }, { roughnessFactor = 0.7 } } }
  for token, def in pairs(docs) do
    if type(def) == 'table' and def.class == 'GroundCover' and token:find('"' .. source .. '/', 1, true) then
      def.material = shared
    end
  end
end
local fieldCalls, realCreate = {}, createObject
_G.createObject = function(class)
  local o = realCreate(class)
  if class == 'Material' then
    local set = o.setField
    o.setField = function(self, k, index, value)
      fieldCalls[#fieldCalls + 1] = { k, index, value }
      return set(self, k, index, value)
    end
  end
  return o
end
gc.setEnabled(false); gc.reset(); gc.enabled = true
T.check(gc.ensure(), 'shared stock names resolve')
_G.createObject = realCreate
local light = scenetree.findObject(gc.materialName('italy', shared))
local dark = scenetree.findObject(gc.materialName('hirochi', shared))
T.check(light and dark and light ~= dark and light.fields.baseColorMap ~= dark.fields.baseColorMap,
  'Italy and Hirochi retain distinct material textures')
T.check(scenetree.findObject(shared) == nil, 'stock material names are never overwritten')
T.check(light.fields.alphaTest == '1' and light.fields.baseColorFactor == '0.2 0.3 0.4 1',
  'material scalar and vector values preserve their field representation')
T.check(light.fields.normalMap == '/levels/italy/art/shapes/groundcover/relative.normal.png',
  'relative texture paths retain the stock material directory')
local stage1 = false
for _, c in ipairs(fieldCalls) do if c[1] == 'roughnessFactor' and c[2] == 1 and c[3] == '0.7' then stage1 = true end end
T.check(stage1, 'JSON material stage two uses engine stage index one')
T.check(tonumber(scenetree.findObject(gc.PREFIX .. 'small_grass_close_lawn').fields.sizeMax) <= 0.3,
  'lawn excludes latent tall stock types while keeping their real short sizes')

fresh({ missingSource = 'italy' })
T.check(gc.ensure() and gc.status().objects == #gc.DEFS, 'missing Italy falls back without losing the other layers')
T.check(gc.status().selected[gc.PREFIX .. 'grass_light_short'] == 'hirochi/small_grass_close', 'lighter grass falls back to installed green')
fresh({ missingSource = 'johnson' })
T.check(gc.ensure() and gc.status().selected[gc.PREFIX .. 'grass_dry_tuft'] == 'wcusa/grass_dry_1', 'missing Johnson Valley uses golden West Coast grass')
T.check(tonumber(scenetree.findObject(gc.PREFIX .. 'grass_dry_tuft').fields.radius) == 60
  and tonumber(scenetree.findObject(gc.PREFIX .. 'long_grass_distant_scrub').fields.radius) == 70,
  'fallbacks preserve each role radius independently')

fresh()
local realExists = FS.fileExists
FS.fileExists = function(_, path) return not path:find('/textures/italy.', 1, true) end
T.check(gc.ensure() and gc.status().selected[gc.PREFIX .. 'grass_light_long'] == 'hirochi/long_grass_distant',
  'missing preferred texture follows the same graceful fallback')
FS.fileExists = function(_, path) return path == '/textures/compiled.color.dds' end
T.check(gc.textureExists('/textures/compiled.color.png'), 'compiled DDS satisfies a PNG material reference')
FS.fileExists = realExists

local name = gc.PREFIX .. 'grass_dry_short'
local baseline = tonumber(scenetree.findObject(name).fields.maxElements)
gc.setDensity('low')
T.check(tonumber(scenetree.findObject(name).fields.maxElements) == baseline / 2, 'density independently halves dry grass')
gc.setDensity('normal'); gc.setDistance('far')
T.near(tonumber(scenetree.findObject(name).fields.maxElements), baseline * 2.25, 1,
  'far distance keeps population per area with distance squared')
T.check(tonumber(scenetree.findObject(name).fields.radius) == 75, 'far distance scales the stock radius')
gc.setDistance('normal')

fresh()
_G.readFile = function() return nil end
T.check(not gc.ensure() and gc.status().objects == 0 and gc.status().error ~= nil,
  'no installed sources: grass is skipped without raising or leaving stale objects')
T.done('test_groundcover_environment')
