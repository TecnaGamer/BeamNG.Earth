-- Private copies of the installed game's rocks. Originals and their materials
-- remain untouched; copied assets live in the user's temp folder, not the MIT mod.
local M = {}
M.ROOT = '/temp/beamngEarth/shapes/cliff_talus_v2/'
M.STOCK = '/levels/johnson_valley/art/shapes/rocks/'
M.SOURCE_MAPS = '/assets/materials/decal/rock/rocks_desert_ca/'
-- Full model heights measured from engine world boxes; all three are authored
-- multi-rock clusters with LODs. One Forest instance draws a whole collection.
M.SHAPES = { { 's_rocks_desert_collection_01', 3.655 },
  { 's_rocks_desert_collection_02', 1.703 }, { 's_rocks_desert_collection_03', 2.253 } }
local loaded, texturesReady = {}, false

function M.isGroup(g)
  return type(g) == 'string' and g:match('^talus_%x%x%x%x%x%x$') ~= nil
end

function M.tint(g)
  if not M.isGroup(g) then return nil end
  local c = g:sub(7)
  return { tonumber(c:sub(1,2),16)/255, tonumber(c:sub(3,4),16)/255, tonumber(c:sub(5,6),16)/255 }
end

function M.remapDae(text, name)
  if type(text) ~= 'string' or not text:find('rocks_desert_ca',1,true) then return nil end
  -- Keep authored LODs and collision; both close and vertex-colour LOD materials
  -- are private. No stock material is changed when another region arrives.
  return (text:gsub('rocks_desert_ca',name):gsub('lod_vertcol',name..'_lod'))
end

local function copy(source, target)
  if FS:fileExists(target) then return true end
  FS:copyFile(source,target)
  return FS:fileExists(target)
end

function M.prepareTextures()
  if texturesReady then return true end
  FS:directoryCreate(M.ROOT)
  local maps = {
    { M.SOURCE_MAPS..'t_rocks_desert_ca_b.color.dds', 'base.color.dds' },
    { M.SOURCE_MAPS..'t_rocks_desert_ca_nm.normal.dds', 'normal.normal.dds' },
    { M.SOURCE_MAPS..'t_rocks_desert_ca_r.data.dds', 'roughness.data.dds' },
    { M.SOURCE_MAPS..'t_rocks_desert_ca_ao.data.dds', 'ao.data.dds' },
    { M.SOURCE_MAPS..'t_rocks_pac_detail_b.data.dds', 'detail.data.dds' },
    { M.SOURCE_MAPS..'t_rocks_pac_detail_n.normal.dds', 'detail.normal.dds' },
  }
  local changed = {}
  for _, pair in ipairs(maps) do
    local path = M.ROOT..pair[2]
    local existed = FS:fileExists(path)
    if not copy(pair[1],path) then return false end
    if not existed then changed[#changed+1] = path end
  end
  -- Inform the resource cache before material compilation (the file watcher
  -- otherwise runs next frame and initial detail texture loads can fail).
  if #changed>0 and FS.triggerFilesChanged then FS:triggerFilesChanged(changed) end
  texturesReady = true
  return true
end

function M.materials(group)
  local tint = M.tint(group)
  if not tint then return nil end
  local name = 'bne_'..group
  -- Use the cliff's regional mineral multiplier, with restrained brightness.
  -- Atlas-wide medians include unused UV padding and overbrighten lit rock faces.
  local calibration = { 2.4, 2.4, 2.4 }
  local factor = { tint[1]*calibration[1],tint[2]*calibration[2],tint[3]*calibration[3],1 }
  return {
    [name] = { name=name, mapTo=name, class='Material', version=1.5, groundType='ROCK',
      Stages={ { baseColorMap=M.ROOT..'base.color.dds', normalMap=M.ROOT..'normal.normal.dds',
        roughnessMap=M.ROOT..'roughness.data.dds', ambientOcclusionMap=M.ROOT..'ao.data.dds',
        baseColorFactor=factor, metallicFactor=0, roughnessFactor=1, useAnisotropic=true,
        detailMap=M.ROOT..'detail.data.dds', detailNormalMap=M.ROOT..'detail.normal.dds',
        detailNormalMapStrength=.22, detailScale={16,16} },{},{},{} } },
    [name..'_lod'] = { name=name..'_lod', mapTo=name..'_lod', class='Material', version=1.5,
      Stages={ { baseColorFactor={tint[1]*2,tint[2]*2,tint[3]*2,1},vertColor=true,
        roughnessFactor=1,metallicFactor=0 },{},{},{} } },
  }
end

function M.catalog(group)
  if loaded[group] then return loaded[group] end
  if not M.isGroup(group) or not M.prepareTextures() then return nil end
  local name, result = 'bne_'..group, {}
  local material = M.ROOT..group..'.materials.json'
  writeFile(material,jsonEncode(M.materials(group)))
  loadJsonMaterialsFile(material)
  for _, source in ipairs(M.SHAPES) do
    local path = M.ROOT..group..'_'..source[1]
    if not FS:fileExists(path..'.dae') then
      local text = M.remapDae(readFile(M.STOCK..source[1]..'.dae'),name)
      if not text then return nil end
      writeFile(path..'.dae',text)
    end
    result[#result+1] = {path,source[2]}
  end
  loaded[group] = result
  return result
end

return M
