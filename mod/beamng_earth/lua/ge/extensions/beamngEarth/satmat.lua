-- Per-tile satellite-imagery materials (docs/V1_3_SPEC.md C, E). Engine-only calls, but the
-- bookkeeping runs under plain luajit with the stubs of mod/tests/stubs.lua.
--
-- A protocol-4 far tile carries `material` (the material name written into its .cdae), `texture`
-- (the imagery file next to it, a VFS path) and `detail` (the shape has UV1 = world-locked detail
-- UVs). Before the tile's TSStatic is created, a Material with mapTo = that name and colorMap =
-- the texture must exist: a Lua-created Material with mapTo = the shape's material name overrides
-- the shape's own (probe P2) and renders the imagery (lead probe probes/v12/p_sat.lua: material
-- 0.13-0.46 ms, tile spawn incl. texture load 2.5-3.5 ms).
-- One Material per name, reference-counted by the tile instances that use it (farfield.lua):
--   create  on first use (farfield runs it as a governor unit 'material' before the tile import);
--   ref     each instance that will spawn with it (a re-sent key shares its predecessor's material);
--   unref   when the instance's object is deleted (or it is dropped before it ever spawned);
--   delete  once no instance refers to it any more - queued, run by farfield as a governor unit
--           'matDelete' AFTER the objects using it are gone (never delete a Material in use).
-- Tiles without `material` (protocol 3, `--imagery off`) keep the static far-tile material and
-- never come here.
-- Pooled names (bngEarthFar_<session>_<n>, server/matslots.py, 2026-10-16): the server hands a name to
-- another tile once no tile of ours uses it, so such a Material is never deleted; unused, it waits for
-- its next tile and then switches to that tile's texture (ref). The engine's per-frame cost grows with
-- every Material ever created, deleted ones included (2026-10-16: the far field's per-tile Materials
-- slowed the game with every teleport).
--
-- Live retexture (server far_retex, --colormatch / --deshadow): the server rendered a shown tile's
-- texture again under the next revision's name (far_<a>_<key>_r<n>.dds; the engine caches textures by
-- path). requestRetex queues the material's switch to it, retexNext (a governor unit 'retex', run by
-- farfield.lua) does one: setField colorMap + flush + reload, as dev.lua's tint does. The newest
-- revision wins; an older one is ignored. A material in use that is re-sent (same name, a tile sent
-- again) with a newer revision of its own texture switches the same way, time-sliced, instead of
-- keeping the old file (ref).

local M = {}

-- Ground grain for close-up views: our own procedural texture (tools/make_detail_texture.py), mean
-- exactly 0.5 grey. The v1.0 material adds the detail map with no distance fade, so a stock detail
-- texture showed as a fabric-like repeat on hills hundreds of metres away (in-game, v1.3); this one's
-- coarser mips fade to flat grey, so the GPU's mip selection fades the grain out with distance.
-- Applied on UV1 (detailMapUseUV = 1), which the server writes as world-locked (x, y) / DETAIL_M, so
-- the grain is continuous across tiles; detailScale 0.5 0.5 = one repeat per 2 * DETAIL_M = 4 m.
M.DETAIL_MAP = '/art/beamngEarth/detail_ground.dds'

-- Material fields (setField(name, 0, value)). colorMap is the field the in-game probe verified
-- (p_sat.lua, a version-1.0 Material). useAnisotropic sharpens the imagery at grazing angles
-- (as stock flowgraph/nodes/environment/planet.lua:80); roughness 1 = matte ground.
M.FIELDS = {
  { 'useAnisotropic', '1' },
  { 'roughnessFactor', '1' },
  { 'castShadows', '0' },
}
M.DETAIL_FIELDS = {
  { 'detailMapUseUV', '1' },
  { 'detailScale', '0.5 0.5' },
}
M.FALLBACK_COLOR = '0.39 0.35 0.27 1' -- texture file missing: the terrain's macro dirt colour

-- The near tiles' asphalt (mesh near-visual mode: the tiles are the ground seen, and the roads are painted into their
-- photos, server surface.road_mask): where the server wrote a road mask beside the texture (far_<a>_<key>_road.dds,
-- finer than the photo) the material gets a second layer, shown through the mask: the terrain block's own asphalt
-- (bngEarthGroundAsphalt's t_asphalt_b / _nm) as detail maps on UV1 (world-locked, DETAIL_M = 2 m) scaled to its 5 m
-- repeat over a flat grey, as West Coast USA's island_terrain layers its grass and dirt. Tried 2026-10-22: the tile's
-- photo as the layer's base drew the road pale grey; the decal road's colour map tiled every 2 m as dense streaks.
-- In the Fells Point street view: 114 grey vs the terrain asphalt's 126 in detail mode. No mask: one layer.
M.ASPHALT_LAYER = true
M.ASPHALT_FIELDS = {
  { 'baseColorMap', '' },
  { 'baseColorFactor', '0.35 0.35 0.35 1' },
  { 'detailMap', '/assets/materials/terrain/asphalt/asphalt/t_asphalt_b.png' },
  { 'detailMapUseUV', '1' },
  { 'detailScale', '0.4 0.4' },
  { 'detailBaseColorMapStrength', '1' },
  { 'detailNormalMap', '/assets/materials/terrain/asphalt/asphalt/t_asphalt_nm.png' },
  { 'normalDetailMapUseUV', '1' },
  { 'detailNormalMapStrength', '0.8' },
  { 'roughnessFactor', '0.9' },
  { 'useAnisotropic', '1' },
}

-- The road mask beside a far tile's texture (its revision suffix dropped), or nil.
function M.maskOf(texture)
  if type(texture) ~= 'string' then return nil end
  local base = texture:gsub('_r%d+%.dds$', '.dds')
  return (base:gsub('%.dds$', '_road.dds'))
end

-- Point material m at texture, with the asphalt layer where the tile has a road mask and world-locked UV1 (detail:
-- the layer's grain is laid out on it), and without it otherwise (a pooled material moves between tiles).
local function setTexture(m, texture, detail)
  m:setField('colorMap', 0, texture)
  local mask = M.maskOf(texture)
  local on = M.ASPHALT_LAYER and detail ~= false and mask ~= nil and FS:fileExists(mask)
  local v15 = tostring(m:getField('version', 0)) == '1.5'
  if not on and not v15 then return end
  m:setField('version', 0, '1.5')
  m:setField('baseColorMap', 0, texture)
  if on then
    m:setField('activeLayers', 0, '2')
    m:setField('opacityMap', 1, mask)
    for _, f in ipairs(M.ASPHALT_FIELDS) do m:setField(f[1], 1, f[2]) end
  else
    m:setField('activeLayers', 0, '1')
  end
end
M._setTexture = setTexture  -- (tests)

local mats = {}       -- name -> { refs = n, texture = path, fallback = diffuse set for a missing file }
local delQ = {}       -- names whose refs reached 0, to delete (FIFO); checked again when run
local queued = {}     -- name -> true while in delQ
local nMats = 0
local retexQ, rqH, rqT = {}, 1, 0 -- names with a texture switch to run (FIFO)
local retexWant = {}  -- name -> the texture to switch to (the newest asked for)
local nRetex = 0      -- switches done
local detailChecked, detailOk = false, false
local warned = {}

-- Is the name a pooled one (kept and re-used, never deleted)?
function M.pooled(name) return type(name) == 'string' and name:find('^bngEarthFar_') ~= nil end

local function warnOnce(key, text)
  if warned[key] then return end
  warned[key] = true
  log('W', 'beamngEarth', text)
end

local function hasDetail()
  if not detailChecked then
    detailChecked = true
    detailOk = FS:fileExists(M.DETAIL_MAP) and true or false
    if not detailOk then warnOnce('detail', 'imagery detail texture missing: ' .. M.DETAIL_MAP) end
  end
  return detailOk
end

-- Is the Material for this name created (and still ours)?
function M.exists(name) return mats[name] ~= nil end

-- A tile texture's revision: far_<a>_<key>_r<n>.dds -> n, the first render (no suffix) -> 0.
function M.revision(path)
  return tonumber(type(path) == 'string' and path:match('_r(%d+)%.dds$') or nil) or 0
end

-- The tile a texture belongs to: its path without the revision suffix (same session folder, same
-- anchor and key).
local function tileOf(path)
  if type(path) ~= 'string' then return nil end
  return (path:gsub('_r%d+%.dds$', '.dds'))
end

-- Is `new` a later revision of the same tile's texture than `old`?
function M.isNewer(old, new)
  local t = tileOf(old)
  return t ~= nil and t == tileOf(new) and M.revision(new) > M.revision(old)
end

-- Create the Material (as planet.lua:77-91 / p_sat.lua: createObject, setField, canSave = false,
-- registerObject). Object name = mapTo name, like the static far material. Returns true on success.
-- A Material of that name left over from an earlier session (e.g. an extension reload) is adopted,
-- pointed at our texture: its colorMap may name an older server session's file (anchor ids restart
-- at 1 with every server, so names repeat), and tiles loaded from now on read the current field.
function M.create(name, texture, detail)
  if mats[name] then return true end
  local m = scenetree.findObject(name)
  local fallback = false
  if m then
    if texture and FS:fileExists(texture) then setTexture(m, texture, detail) end
    m:setField('castShadows', 0, '0')
  else
    m = createObject('Material')
    m:setField('mapTo', 0, name)
    if texture and FS:fileExists(texture) then
      setTexture(m, texture, detail)
    else
      warnOnce('tex:' .. tostring(texture), 'imagery texture missing: ' .. tostring(texture))
      m:setField('diffuseColor', 0, M.FALLBACK_COLOR)
      fallback = true
    end
    for _, f in ipairs(M.FIELDS) do m:setField(f[1], 0, f[2]) end
    if detail and hasDetail() then
      m:setField('detailMap', 0, M.DETAIL_MAP)
      for _, f in ipairs(M.DETAIL_FIELDS) do m:setField(f[1], 0, f[2]) end
    end
    m.canSave = false
    m:registerObject(name)
  end
  mats[name] = { refs = 0, texture = texture, fallback = fallback, detail = detail }
  nMats = nMats + 1
  return true
end

-- One more instance uses the material (it must exist). A different texture under the same name
-- (a restarted server re-uses anchor ids, so names repeat across its sessions): an unused material
-- (waiting for deletion) is pointed at the new file; one still in use keeps its texture, since its
-- tiles' loaded shapes keep theirs anyway - unless its file is gone (the restarted server deleted
-- the old session's folder: in-game 2026-10-11 every tile showed the engine's orange "no texture"),
-- then it takes the new file too and reloads. A later revision of the same tile's texture (the
-- server re-rendered it, --colormatch / --deshadow) is what every instance of the tile should show:
-- an in-use material switches to it through the retex queue (time-sliced, see requestRetex).
function M.ref(name, texture)
  local e = mats[name]
  if not e then return false end
  if M.pooled(name) and texture and e.texture and texture ~= e.texture and e.refs > 0
    and not M.isNewer(texture, e.texture) and not M.isNewer(e.texture, texture) then
    -- a pooled name the server gave to another tile while ours still shows it (the swap that removes
    -- it is late): the new tile's texture, time-sliced; the leaving one shows it until it goes
    M.requestRetex(name, texture)
    e.refs = e.refs + 1
    return true
  end
  if texture and e.texture and texture ~= e.texture and e.refs > 0 and M.isNewer(e.texture, texture)
    and FS:fileExists(e.texture) then
    M.requestRetex(name, texture)
    e.refs = e.refs + 1
    return true
  end
  if texture and e.texture and texture ~= e.texture then
    local gone = not FS:fileExists(e.texture)
    local m = (e.refs == 0 or gone) and scenetree.findObject(name)
    if m and FS:fileExists(texture) then
      setTexture(m, texture, e.detail)
      if e.fallback then -- created without its file: the fallback colour would tint the imagery
        m:setField('diffuseColor', 0, '1 1 1 1')
        e.fallback = false
      end
      if (gone and e.refs > 0) or M.pooled(name) then -- a pooled name shown before: another tile's now
        pcall(function() m:flush() end)
        pcall(function() m:reload() end)
      end
      if e.refs == 0 then retexWant[name] = nil end -- a switch queued for the tile that had it is stale
      e.texture = texture
    else
      warnOnce('retex:' .. name, 'material ' .. name .. ' re-sent with another texture; keeping ' .. tostring(e.texture))
    end
  end
  e.refs = e.refs + 1
  return true
end

-- An instance using the material is gone (its object deleted, or it never spawned).
function M.unref(name)
  local e = mats[name]
  if not e then return end
  e.refs = e.refs - 1
  if e.refs <= 0 then
    e.refs = 0
    retexWant[name] = nil -- nobody shows it: a queued switch is dropped when it comes up
    if not queued[name] and not M.pooled(name) then
      queued[name] = true
      delQ[#delQ + 1] = name
    end
  end
end

-- Queue the material's switch to `texture` (far_retex, or a re-sent tile's newer revision). Returns
-- false when there is no such material (a tile whose material is not created yet takes the texture
-- from its own record when it is) or when `texture` is older than the one it shows or will show.
function M.requestRetex(name, texture)
  local e = mats[name]
  if not e or type(texture) ~= 'string' or texture == '' then return false end
  local cur = retexWant[name] or e.texture
  if texture == cur then return true end
  if cur and M.isNewer(texture, cur) then return false end -- an older revision: the newer one stays
  if retexWant[name] == nil then
    rqT = rqT + 1
    retexQ[rqT] = name
  end
  retexWant[name] = texture
  return true
end

-- Texture switches waiting.
function M.hasRetex() return rqH <= rqT end

-- Run the next texture switch (a governor unit 'retex'): colorMap + flush + reload. Returns true if a
-- material was switched, false if the entry was stale (material deleted meanwhile, already on that
-- texture, or the file missing: it keeps its texture).
function M.retexNext()
  if rqH > rqT then return false end
  local name = retexQ[rqH]
  retexQ[rqH] = nil
  rqH = rqH + 1
  if rqH > rqT then retexQ, rqH, rqT = {}, 1, 0 end
  local want = retexWant[name]
  retexWant[name] = nil
  local e = mats[name]
  if not e or not want or want == e.texture then return false end
  -- the material took a later revision meanwhile (re-sent while unused: ref points it at the file at
  -- once): the queued switch is stale, never back to an older file
  if M.isNewer(want, e.texture) then return false end
  if not FS:fileExists(want) then
    warnOnce('retexfile:' .. name, 'retextured imagery missing: ' .. want .. '; keeping ' .. tostring(e.texture))
    return false
  end
  local m = scenetree.findObject(name)
  if not m then return false end
  setTexture(m, want, e.detail)
  if e.fallback then -- created without its file: the fallback colour would tint the imagery
    m:setField('diffuseColor', 0, '1 1 1 1')
    e.fallback = false
  end
  pcall(function() m:flush() end)
  pcall(function() m:reload() end)
  e.texture = want
  nRetex = nRetex + 1
  return true
end

-- The texture a material shows (nil: no such material); switches done so far.
function M.texture(name) local e = mats[name]; return e and e.texture end
function M.retexCount() return nRetex end

-- Materials waiting for deletion that nobody uses again.
function M.hasDeletes()
  for _, name in ipairs(delQ) do
    local e = mats[name]
    if e and e.refs == 0 then return true end
  end
  return false
end

-- Delete the next unused material (a governor unit). Returns true if one was deleted, false if
-- the queue held only re-used names (they are dropped from it).
function M.deleteNext()
  while #delQ > 0 do
    local name = table.remove(delQ, 1)
    queued[name] = nil
    local e = mats[name]
    if e and e.refs == 0 then
      mats[name] = nil
      retexWant[name] = nil -- its queued switch (if any) is dropped when it comes up
      nMats = nMats - 1
      local m = scenetree.findObject(name)
      if m then m:delete() end -- editor/api/material.lua:234 deletes Materials the same way
      return true
    end
  end
  return false
end

-- Delete every material we made. Only when no object uses them any more (extension unload after
-- the tiles were deleted, or a new level whose predecessor deleted its objects).
function M.clearAll()
  for name in pairs(mats) do
    local m = scenetree.findObject(name)
    if m then m:delete() end
  end
  mats, delQ, queued, nMats = {}, {}, {}, 0
  retexQ, rqH, rqT, retexWant = {}, 1, 0, {}
  detailChecked = false
end

function M.count() return nMats end

-- Call fn(texture) for the texture of every material we hold (in use or waiting for deletion), and
-- for the texture a queued switch (far_retex, requestRetex) will point it at: farfield.heldSessions
-- (the server keeps those files while the material exists). The material shows e.texture until
-- retexNext runs, so that one is named until then; the queued one is named too, so a server that
-- adopted the folder it lies in (a game that outlived the server that sent far_retex) keeps it until
-- the switch has run or been dropped (retexNext checks the file, then points the material at it).
function M.eachTexture(fn)
  for name, e in pairs(mats) do
    if e.texture and not (e.refs == 0 and M.pooled(name)) then fn(e.texture) end -- an unused pooled one needs no file
    local want = retexWant[name]
    if want and want ~= e.texture then fn(want) end
  end
end
function M.refs(name) local e = mats[name]; return e and e.refs or 0 end -- for tests

return M
