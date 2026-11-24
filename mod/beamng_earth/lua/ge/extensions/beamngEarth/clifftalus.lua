-- Private copies of the installed game's rocks. Originals and their materials
-- remain untouched; copied assets live in the user's temp folder, not the MIT mod.
local M = {}
M.ROOT = '/temp/beamngEarth/shapes/cliff_talus_v4/'
M.SOURCE_MAPS = '/assets/materials/decal/rock/rocks_desert_ca/'
-- Full model heights measured from engine world boxes; all three are authored
-- multi-rock clusters with LODs. One Forest instance draws a whole collection.
M.RUBBLE_SHAPES = { { 's_rocks_desert_collection_01', 3.655 },
  { 's_rocks_desert_collection_02', 1.703 }, { 's_rocks_desert_collection_03', 2.253 } }
M.COMPANION_SHAPES = {{'utah_boulder_1',1.885},
  {'utah_boulder_3',1.698}, {'utah_boulder_5',.560}}
for _, shape in ipairs(M.COMPANION_SHAPES) do M.RUBBLE_SHAPES[#M.RUBBLE_SHAPES+1] = shape end
-- Native 16.6 m outcrops have much denser atlas coverage than the 1.7-1.9 m
-- boulders. Only scale these down for the requested 4-10 m cliff projections.
M.OUTCROP_SHAPES = {{'utah_rock_big_2_b',16.6963},
  {'utah_rock_big_2_d',16.59375}}
local loaded, texturesReady, models, modelCacheKey = {}, false, nil, nil
local failed = {}
M.enabled = false
M.MAX_DISTANCE_M = 350

function M.available(group)
  return M.enabled and not failed[group]
end

function M.fail(group, reason)
  if not failed[group] then
    log('W', 'beamngEarth.clifftalus', 'Cliff rock group skipped: '..group..': '..tostring(reason))
  end
  failed[group] = true
end

-- These group prefixes are persisted in cached tile rows. Keep their wire names
-- stable while using one vocabulary for the three populations in code.
function M.kind(group)
  if type(group) ~= 'string' then return nil end
  if group:match('^talus_large_%x%x%x%x%x%x$') then return 'outcrop' end
  if group:match('^talus_face_%x%x%x%x%x%x$') then return 'companion' end
  if group:match('^talus_%x%x%x%x%x%x$') then return 'rubble' end
end

local groupKinds = {}
function M.isGroup(group)
  if type(group) ~= 'string' then return false end
  if groupKinds[group] == nil then groupKinds[group] = M.kind(group) ~= nil end
  return groupKinds[group]
end

-- Forest selects by native model height so a small atlas is never magnified
-- into a large outcrop. Rubble retains its authored collection distribution.
function M.pick(group, list, height, rank)
  local kind = M.kind(group)
  local maxScale = kind == 'outcrop' and 1.0 or kind == 'companion' and 1.25 or 2.0
  local candidates = list
  if kind ~= 'rubble' then
    candidates = {}
    local biggest = list[1]
    for _, variant in ipairs(list) do
      if variant[2] > biggest[2] then biggest = variant end
      if height <= variant[2]*maxScale then candidates[#candidates+1] = variant end
    end
    if #candidates == 0 then candidates[1] = biggest end
  end
  local variant = candidates[math.floor(rank * 7919) % #candidates + 1]
  return variant[1], math.max(.10, math.min(maxScale, height/variant[2]))
end

function M.tint(g)
  if not M.isGroup(g) then return nil end
  local c = g:sub(-6)
  return { tonumber(c:sub(1,2),16)/255, tonumber(c:sub(3,4),16)/255, tonumber(c:sub(5,6),16)/255 }
end

function M.materialNames(group)
  if not M.isGroup(group) then return nil end
  local tint = group:sub(-6)
  -- Match stock string lengths so all compiled mesh/LOD/collision bytes stay put.
  return {rocks_desert_ca='bneRock__'..tint, utah_rocks_b='bneU__'..tint, lod_vertcol='bneL_'..tint}
end

function M.remapModel(text, spec, group)
  local names = M.materialNames(group)
  if not names or type(text) ~= 'string' or type(spec) ~= 'table' or #text ~= spec.bytes
      or type(spec.slots) ~= 'table' or #spec.slots == 0 then return nil end
  local chunks, cursor = {}, 1
  for _, slot in ipairs(spec.slots) do
    local offset, old = slot[1], slot[2]
    local new = names[old]
    if type(offset) ~= 'number' or offset % 1 ~= 0 or offset < cursor or not new
        or #new ~= #old or text:sub(offset,offset+#old-1) ~= old then return nil end
    chunks[#chunks+1] = text:sub(cursor,offset-1)
    chunks[#chunks+1] = new
    cursor = offset+#old
  end
  chunks[#chunks+1] = text:sub(cursor)
  return table.concat(chunks)
end

local function copy(source, target)
  if FS:fileExists(target) then return true end
  FS:copyFile(source,target)
  return FS:fileExists(target)
end

function M.prepareTextures()
  if not M.enabled then return false end
  if texturesReady then return true end
  FS:directoryCreate(M.ROOT)
  local maps = {
    { M.SOURCE_MAPS..'t_rocks_desert_ca_nm.normal.dds', 'normal.normal.dds' },
    { M.SOURCE_MAPS..'t_rocks_desert_ca_r.data.dds', 'roughness.data.dds' },
    { M.SOURCE_MAPS..'t_rocks_desert_ca_ao.data.dds', 'ao.data.dds' },
    { M.SOURCE_MAPS..'t_rocks_pac_detail_b.data.dds', 'detail.data.dds' },
    { M.SOURCE_MAPS..'t_rocks_pac_detail_n.normal.dds', 'detail.normal.dds' },
  }
  local utah = '/assets/materials/rocks/utah_rocks_b/t_rocks_utah_'
  for _, pair in ipairs({{'nm.normal','normal.normal'},
    {'r.data','roughness.data'},{'ao.data','ao.data'},
    {'detail_nm.normal','detail.normal'}}) do
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
  local names, result = M.materialNames(group), {}
  -- Neutral scanned stone retains albedo detail without the arid texture's
  -- red/green patches on unrelated mesh UVs. Tint from the supporting cliff.
  local baseMap = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/natural_base.color.dds'
  local mean = {.33863778,.32368715,.30369500}
  local factor = {srgb(linear(tint[1])/mean[1]),srgb(linear(tint[2])/mean[2]),
                    srgb(linear(tint[3])/mean[3]),1}
  for _, prefix in ipairs({'', 'utah_'}) do
    local id = prefix == '' and names.rocks_desert_ca or names.utah_rocks_b
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
  result[names.lod_vertcol] = {name=names.lod_vertcol,mapTo=names.lod_vertcol,class='Material',version=1.5,
    Stages={{baseColorFactor={tint[1],tint[2],tint[3],1},vertColor=false,
      roughnessFactor=1,metallicFactor=0},{},{},{}}}
  return result
end

-- Preparation is resumed by the Forest scheduler, never by incoming tile messages.
-- Each step performs one asset operation; a group cannot monopolize a frame by
-- writing all six model copies and compiling materials together.
local preparing = {}

-- The server declares its opt-ins before sending scene content. Only stock DDS
-- copies are prepared here: no engine TextureCooker work is allowed while driving.
function M.configure(flags)
  failed = {}
  M.enabled = type(flags) == 'table' and (flags.natural == true or flags.roadCuts == true)
  if not M.enabled then return end
  local manifest = jsonReadFile(M.ROOT..'templates/models.json')
  if type(manifest) ~= 'table' or type(manifest.models) ~= 'table'
      or type(manifest.cacheKey) ~= 'string' or not manifest.cacheKey:match('^%x+$') then
    M.enabled = false
    log('W', 'beamngEarth.clifftalus', 'Cliff rocks skipped: compiled stock models were not prepared at startup.')
    return
  end
  models, modelCacheKey = manifest.models, manifest.cacheKey
  local ok, ready = pcall(M.prepareTextures)
  if not ok or not ready then
    M.enabled = false
    log('W', 'beamngEarth.clifftalus', 'Cliff rocks skipped: installed stock rock textures are unavailable.')
  end
end

function M.catalog(group, incremental)
  if not M.available(group) or not texturesReady then return nil end
  if loaded[group] then return loaded[group] end
  if not M.isGroup(group) then return nil end
  local state = preparing[group]
  if not state then
    local material = M.ROOT..group..'.materials.json'
    local encoded = jsonEncode(M.materials(group))
    if readFile(material) ~= encoded then writeFile(material,encoded) end
    loadJsonMaterialsFile(material)
    local kind = M.kind(group)
    state = {result={}, shapes=kind == 'outcrop' and M.OUTCROP_SHAPES
      or kind == 'companion' and M.COMPANION_SHAPES or M.RUBBLE_SHAPES}
    preparing[group] = state
    if incremental then return nil end
  end
  repeat
    local source = state.shapes[#state.result+1]
    local path = M.ROOT..modelCacheKey..'_'..group..'_'..source[1]..'.cdae'
    if not FS:fileExists(path) then
      local text = M.remapModel(readFile(M.ROOT..'templates/'..source[1]..'.cdae'), models[source[1]], group)
      if not text then M.fail(group, 'compiled stock model unavailable or invalid'); return nil end
      writeFile(path,text)
    end
    state.result[#state.result+1] = {path,source[2]}
    if #state.result == #state.shapes then
      loaded[group], preparing[group] = state.result, nil
      return state.result
    end
  until incremental
end

return M
