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
M.UTAH = '/levels/utah/art/shapes/rocks/'
M.SHAPES[#M.SHAPES+1] = {'utah_boulder_1',1.885,M.UTAH,'utah'}
M.SHAPES[#M.SHAPES+1] = {'utah_boulder_3',1.698,M.UTAH,'utah'}
M.SHAPES[#M.SHAPES+1] = {'utah_boulder_5',.560,M.UTAH,'utah'}
-- Native 16.6 m outcrops have much denser atlas coverage than the 1.7-1.9 m
-- boulders. Only scale these down for the requested 4-10 m cliff projections.
M.LARGE_SHAPES = {{'utah_rock_big_2_b',16.6963,M.UTAH,'utah'},
  {'utah_rock_big_2_d',16.59375,M.UTAH,'utah'}}
local loaded, texturesReady = {}, false

function M.isGroup(g)
  return type(g) == 'string' and (g:match('^talus_%x%x%x%x%x%x$') ~= nil
    or g:match('^talus_large_%x%x%x%x%x%x$') ~= nil
    or g:match('^talus_face_%x%x%x%x%x%x$') ~= nil)
end

function M.tint(g)
  if not M.isGroup(g) then return nil end
  local c = g:sub(-6)
  return { tonumber(c:sub(1,2),16)/255, tonumber(c:sub(3,4),16)/255, tonumber(c:sub(5,6),16)/255 }
end

function M.remapDae(text, name)
  if type(text) ~= 'string' then return nil end
  if text:find('utah_rocks_b',1,true) then
    return (text:gsub('utah_rocks_b',name..'_utah'):gsub('lod_vertcol',name..'_lod'))
  end
  if not text:find('rocks_desert_ca',1,true) then return nil end
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
  local utah = '/assets/materials/rocks/utah_rocks_b/t_rocks_utah_'
  for _, pair in ipairs({{'b.color','base.color'},{'nm.normal','normal.normal'},
    {'r.data','roughness.data'},{'ao.data','ao.data'},
    {'detail_b.data','detail.data'},{'detail_nm.normal','detail.normal'}}) do
    maps[#maps+1] = {utah..pair[1]..'.dds','utah_'..pair[2]..'.dds'}
  end
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

local function linear(v)
  return v <= .04045 and v/12.92 or ((v+.055)/1.055)^2.4
end
local function srgb(v)
  return v <= .0031308 and v*12.92 or 1.055*v^(1/2.4)-.055
end

function M.materials(group)
  local tint = M.tint(group)
  if not tint then return nil end
  local name, result = 'bne_'..group, {}
  -- Neutral scanned stone retains albedo detail without the arid texture's
  -- red/green patches on unrelated mesh UVs. Tint from the supporting cliff.
  for _, prefix in ipairs({'', 'utah_'}) do
    local baseMap = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/scan_v2_base.color.png'
    local mean = {.33863778,.32368715,.30369500}
    local id = name..(prefix == '' and '' or '_utah')
    local factor = {srgb(linear(tint[1])/mean[1]),srgb(linear(tint[2])/mean[2]),
                    srgb(linear(tint[3])/mean[3]),1}
    result[id] = {name=id,mapTo=id,class='Material',version=1.5,groundType='ROCK',
      Stages={{baseColorMap=baseMap,
        normalMap=M.ROOT..prefix..'normal.normal.dds',
        roughnessMap=M.ROOT..prefix..'roughness.data.dds',
        ambientOcclusionMap=M.ROOT..prefix..'ao.data.dds',
        baseColorFactor=factor,vertColor=false,metallicFactor=0,roughnessFactor=1,useAnisotropic=true,
        detailMap=M.ROOT..'detail.data.dds',detailBaseColorMapStrength=.18,
        detailNormalMap=M.ROOT..prefix..'detail.normal.dds',
        detailNormalMapStrength=.22,detailScale={16,16}},{},{},{}}}
  end
  -- The coarse collection LOD is shaded geometry with the same mean albedo;
  -- its baked vertex colours belong to the original desert, not this cliff.
  result[name..'_lod'] = {name=name..'_lod',mapTo=name..'_lod',class='Material',version=1.5,
    Stages={{baseColorFactor={tint[1],tint[2],tint[3],1},vertColor=false,
      roughnessFactor=1,metallicFactor=0},{},{},{}}}
  return result
end

function M.catalog(group)
  if loaded[group] then return loaded[group] end
  if not M.isGroup(group) or not M.prepareTextures() then return nil end
  local name, result = 'bne_'..group, {}
  local material = M.ROOT..group..'.materials.json'
  writeFile(material,jsonEncode(M.materials(group)))
  loadJsonMaterialsFile(material)
  local shapes = group:match('^talus_large_') and M.LARGE_SHAPES
    or group:match('^talus_face_') and {M.SHAPES[4],M.SHAPES[5],M.SHAPES[6]} or M.SHAPES
  for _, source in ipairs(shapes) do
    local path = M.ROOT..group..'_'..source[1]
    if not FS:fileExists(path..'.dae') then
      local text = M.remapDae(readFile((source[3] or M.STOCK)..source[1]..'.dae'),name)
      if not text then return nil end
      writeFile(path..'.dae',text)
    end
    result[#result+1] = {path,source[2]}
  end
  loaded[group] = result
  return result
end

return M
