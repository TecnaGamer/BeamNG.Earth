-- Load only the enabled material families. The authored maps live in the mod;
-- optional stock-derived ground detail lives in the player's private cache.
local M = {}
local SOURCE = '/levels/beamng_earth/art/shapes/beamngEarth/cliff_materials.src.json'
local ROOT = '/temp/beamngEarth/shapes/'
local GROUND = ROOT..'cliff_ground_v1/'
local DETAIL = {
  detailMap = 'detailBaseColorMapStrength', detailNormalMap = 'detailNormalMapStrength',
  ambientOcclusionDetailMap = 'detailAoMapStrength', roughnessDetailMap = 'detailRoughnessMapStrength',
}

local function enabled(name, flags)
  if name:find('roadcut_arid', 1, true) then return flags.natural or flags.roadCuts end
  if name:find('roadcut', 1, true) then return flags.roadCuts end
  return flags.natural
end

function M.configure(flags)
  if not flags or not (flags.natural or flags.roadCuts) then return end
  local text = readFile(SOURCE)
  local ok, materials = pcall(jsonDecode, text or '')
  if not ok or type(materials) ~= 'table' then
    log('E', 'beamngEarth.cliffmaterials', 'Cliff materials are unavailable; reinstall the mod with the cliff flags enabled.')
    return
  end
  local ground = true
  for _, file in ipairs({'sources.json', 'detail.data.dds', 'normal.dds', 'ao.data.dds', 'roughness.data.dds'}) do
    if not FS:fileExists(GROUND..file) then ground = false; break end
  end
  local selected = {}
  for name, material in pairs(materials) do
    if enabled(name, flags) then
      if not ground then
        for _, stage in ipairs(material.Stages or {}) do
          for map, strength in pairs(DETAIL) do
            if type(stage[map]) == 'string' and stage[map]:sub(1, #GROUND) == GROUND then
              stage[map], stage[strength] = '', 0
            end
          end
        end
      end
      selected[name] = material
    end
  end
  if not ground then
    log('W', 'beamngEarth.cliffmaterials', 'Private ground detail is unavailable; using authored cliff maps without that decoration.')
  end
  local path = ROOT..'cliff_session.materials.json'
  local encoded = jsonEncode(selected)
  FS:directoryCreate(ROOT, true)
  if readFile(path) ~= encoded then writeFile(path, encoded) end
  loadJsonMaterialsFile(path)
end

return M
