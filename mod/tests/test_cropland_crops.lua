local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local gc = require('beamngEarth/groundcover')
jsonNull = {}
flushGroundCoverGrids = function() end

local defs, lines = {}, {}
local stockShares = { 0.25, 0.17, 0.05, 0.1, 0.15, 0.08, 0.05, 0.08, 0.2, 0.06 }
T.check(#gc.DEFS == 11, 'generic cropland adds one definition')
for i, share in ipairs(stockShares) do
  T.check(gc.DEFS[i].share == share and gc.DEFS[i].source ~= 'crops',
    'existing vegetation keeps its population share ' .. i)
end
local cropRole = gc.DEFS[11]
T.check(cropRole.source == 'crops' and cropRole.src == 'corn_like_crop'
    and cropRole.layer == 'bngEarthGroundGenericCrop' and cropRole.share == 1,
  'generic crops bind only to their cropland carrier')
for _, role in ipairs(gc.DEFS) do
  T.check(role.layer ~= 'bngEarthGroundCrop', 'reserved special-crop soil does not receive generic plants')
end
local terrain = require('beamngEarth/terrain')
local sameLook = true
for field, value in pairs(terrain.LOOKS.bngEarthGroundCrop) do
  sameLook = sameLook and terrain.LOOKS.bngEarthGroundGenericCrop[field] == value
end
for field, value in pairs(terrain.LOOKS.bngEarthGroundGenericCrop) do
  sameLook = sameLook and terrain.LOOKS.bngEarthGroundCrop[field] == value
end
T.check(sameLook, 'generic carrier retains every runtime crop-soil texture, repeat and strength')
for _, d in ipairs(gc.DEFS) do
  local key = d.source .. '/' .. d.src
  local token = '{"class":"GroundCover","name":"' .. key .. '"}'
  if not defs[token] then
    lines[d.source] = lines[d.source] or {}
    lines[d.source][#lines[d.source] + 1] = token
    defs[token] = { name = d.src, class = 'GroundCover', material = 'stock_material',
      maxElements = 1000, radius = 50, dissolveRadius = 30,
      Types = { { layer = 'Grass', sizeMin = 0.15, sizeMax = 0.3 } } }
    if d.source == 'crops' then
      local def = defs[token]
      def.material, def.maxElements, def.dissolveRadius = 'bngEarthCropPlant', 12000, 35
      def.gridSize, def.shapeCullRadius, def.shapesCastShadows = 6, 50, false
      def.Types = {}
      for i = 1, 3 do
        def.Types[i] = { layer = 'Crop', shapeFilename = '/art/shapes/crops/crop_0' .. i .. '.dae',
          sizeMin = 0.85, sizeMax = 1.15, probability = 1 }
      end
    end
  end
end
jsonDecode = function(text) return defs[text] end
readFile = function(path)
  for source, locations in pairs(gc.SOURCES) do
    if path == locations.items then return table.concat(lines[source], '\n') end
  end
end
local loaded, made = {}, {}
loadJsonMaterialsFile = function(path) loaded[path] = true end
local originalCreate = createObject
createObject = function(class)
  local object = originalCreate(class)
  local originalSet = object.setField
  object.writes = {}
  object.setField = function(self, key, index, value)
    self.writes[key .. '#' .. index] = value
    originalSet(self, key, index, value)
  end
  made[#made + 1] = object
  return object
end
T.check(gc.ensure(), 'crop and stock definitions load together')
T.check(loaded['/art/shapes/crops/main.materials.json'], 'crop material is loaded from the shipped asset source')
local function crop() return scenetree.findObject(gc.PREFIX .. 'corn_like_crop') end
T.check(crop() and crop().fields.material == 'bngEarthCropPlant', 'crop object resolves its unique material')
for i = 0, 2 do
  T.check(crop().writes['layer#' .. i] == 'bngEarthGroundGenericCrop'
      and crop().writes['shapeFilename#' .. i] == '/art/shapes/crops/crop_0' .. (i + 1) .. '.dae',
    'crop variant ' .. (i + 1) .. ' is restricted to cropland')
end
T.check(crop().fields.useNewPlacement == '1' and crop().fields.useNewPlacementGpu == '0'
    and crop().fields.shapesCastShadows == '0', 'crop uses existing placement and has no plant shadows')

for _, density in ipairs({ 'low', 'normal', 'high' }) do
  for _, distance in ipairs({ 'near', 'normal', 'far' }) do
    gc.setDensity(density); gc.setDistance(distance)
    local object, factor, reach = crop(), gc.DENSITY[density], gc.DISTANCE[distance]
    T.check(tonumber(object.fields.maxElements) == math.floor(12000 * factor * reach * reach + 0.5),
      density .. '/' .. distance .. ': crop population follows existing controls')
    T.near(tonumber(object.fields.radius), 50 * reach, 1e-6, 'crop draw radius follows distance')
    T.near(tonumber(object.fields.dissolveRadius), 35 * reach, 1e-6, 'crop fades before culling')
    T.near(tonumber(object.fields.shapeCullRadius), 50 * reach, 1e-6, 'crop mesh cull radius follows distance')
    T.check(gc.status().objects == #gc.DEFS, 'settings keep one object per definition')
  end
end
gc.setEnabled(false)
T.check(crop() == nil, 'disabling ground cover removes generic crop objects')
gc.reset()
T.done('test_cropland_crops')
