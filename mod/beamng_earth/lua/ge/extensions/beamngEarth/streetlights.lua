-- Dynamic point lights for the street lamps already placed as Forest items by trees.lua. This
-- module never places a lamp: it follows only a successfully-created mapped lamp item and keeps
-- a small, non-shadow-casting set of PointLights around the player.
local M = {}

-- These are the centroids of the transparent glass vertices in the stock DAE meshes, in their
-- local shape frames: eca_lamppost.dae and east_coast_usa's electrical_pole_lamp.dae. The Forest
-- item scale comes from trees.shapeOf, so each light stays on its rendered lamp head.
M.HEAD = {
  streetlight = { 2.71129954, -0.000320829, 7.953926 },
  polelamp = { -2.67592085, 0.000115796, 0.4351099 },
}
M.RADIUS_M = 35
M.BRIGHTNESS = 8
M.COLOR = { 1.00, 0.72, 0.34 } -- the stock editor site-lights helper's warm sodium tone
M.NEAR_M = 90
M.MAX_ACTIVE = 24
M.CREATE_BUDGET = 4
M.REMOVE_BUDGET = 4
M.UPDATE_INTERVAL_S = 0.25
M.DUPLICATE_M = 2

local tiles, records, active = {}, {}, {}
local candidates, selected, wanted, activeKeys = {}, {}, {}, {}
local elapsed = 0
M.errors = 0

local function copy3(v)
  if type(v) ~= 'table' then return { 0, 0, 0 } end
  return { tonumber(v[1]) or 0, tonumber(v[2]) or 0, tonumber(v[3]) or 0 }
end

local function copy4(q)
  if type(q) ~= 'table' then return { 0, 0, 0, 1 } end
  return { tonumber(q[1]) or 0, tonumber(q[2]) or 0, tonumber(q[3]) or 0, tonumber(q[4]) or 1 }
end

-- BeamNG's Lua quaternion-vector operator rotates by the conjugate; this is its equivalent
-- without allocating engine vec3/quaternion values while ranking candidate lamps.
local function rotate(q, x, y, z)
  local qx, qy, qz, qw = q[1], q[2], q[3], q[4]
  local cx, cy, cz = qy * z - qz * y, qz * x - qx * z, qx * y - qy * x
  local ccx, ccy, ccz = qy * cz - qz * cy, qz * cx - qx * cz, qx * cy - qy * cx
  return x - 2 * qw * cx + 2 * ccx, y - 2 * qw * cy + 2 * ccy, z - 2 * qw * cz + 2 * ccz
end

local function worldPoint(tile, x, y, z)
  local rx, ry, rz = rotate(tile.quat, x, y, z)
  return tile.offset[1] + rx, tile.offset[2] + ry, tile.offset[3] + rz
end

local function updateTileCenter(tile)
  local tx, ty = tostring(tile.key):match('(%-?%d+)_(%-?%d+)$')
  if not tx then tile.cx, tile.cy = nil, nil; return end
  tile.cx, tile.cy = worldPoint(tile, tonumber(tx) * 256 + 128, tonumber(ty) * 256 + 128, 0)
end

local function ensureTile(tileKey, offset, q)
  local tile = tiles[tileKey]
  if not tile then
    tile = { key = tileKey, items = {}, offset = { 0, 0, 0 }, quat = { 0, 0, 0, 1 } }
    tiles[tileKey] = tile
  end
  if offset ~= nil then tile.offset = copy3(offset) end
  if q ~= nil then tile.quat = copy4(q) end
  updateTileCenter(tile)
  return tile
end

local function itemPosition(tile, rec)
  local head = M.HEAD[rec.group]
  local a = math.rad(rec.yaw)
  local c, s = math.cos(a), math.sin(a)
  -- trees.lua uses MatrixF:setFromEuler(0, 0, -rowYaw); in the BeamNG frame this
  -- rotates the stock mesh by the server's counter-clockwise row yaw.
  local hx, hy = head[1] * rec.scale, head[2] * rec.scale
  local x = rec.x + c * hx - s * hy
  local y = rec.y + s * hx + c * hy
  local z = rec.z + head[3] * rec.scale
  rec.px, rec.py, rec.pz = worldPoint(tile, x, y, z)
end

local function setPosition(obj, rec)
  obj:setPosition(vec3(rec.px, rec.py, rec.pz))
end

local function setEnabled(entry, enabled)
  if entry.enabled == enabled then return end
  local obj = entry.object
  local ok = pcall(function()
    if obj.setLightEnabled then obj:setLightEnabled(enabled)
    else obj:setField('isEnabled', 0, enabled and 'true' or 'false') end
  end)
  if not ok then pcall(function() obj:setField('isEnabled', 0, enabled and 'true' or 'false') end) end
  entry.enabled = enabled
end

local function deleteEntry(entry)
  setEnabled(entry, false)
  pcall(function() entry.object:delete() end)
end

local function createEmitter(rec)
  local ok, obj = pcall(createObject, 'PointLight')
  if not ok or not obj then M.errors = M.errors + 1; rec.failed = true; return nil end
  local good = pcall(function()
    if obj.preApply then obj:preApply() end
    obj:setField('nightLight', 0, '0') -- this module follows the environment without a scene-wide rescan
    obj:setField('isEnabled', 0, 'false')
    obj:setField('radius', 0, tostring(M.RADIUS_M))
    obj:setField('brightness', 0, tostring(M.BRIGHTNESS))
    obj:setField('color', 0, string.format('%f %f %f 1', M.COLOR[1], M.COLOR[2], M.COLOR[3]))
    obj:setField('castShadows', 0, 'false')
    if obj.postApply then obj:postApply() end
    obj.canSave = false
    setPosition(obj, rec)
    obj:registerObject('')
    local group = scenetree and scenetree.MissionGroup
    if group and group.addObject then group:addObject(obj) end
  end)
  if not good then
    M.errors = M.errors + 1
    pcall(function() obj:delete() end)
    rec.failed = true
    return nil
  end
  return { object = obj, enabled = false }
end

local function nightLightsActive()
  local env = rawget(_G, 'core_environment')
  if type(env) ~= 'table' or type(env.getLightState) ~= 'function' then return false end
  local ok, state = pcall(env.getLightState)
  return ok and type(state) == 'table' and state.nightLightsActive == true
end

local function addCandidate(rec, player, anchorId)
  if rec.failed then return end
  local dx, dy, dz = rec.px - player.x, rec.py - player.y, rec.pz - player.z
  local d2 = dx * dx + dy * dy + dz * dz
  if d2 > M.NEAR_M * M.NEAR_M then return end
  rec._distance2 = d2
  rec._currentAnchor = rec.anchor == tostring(anchorId)
  candidates[#candidates + 1] = rec
end

local function candidateLess(a, b)
  if a._distance2 ~= b._distance2 then return a._distance2 < b._distance2 end
  if a._currentAnchor ~= b._currentAnchor then return a._currentAnchor end
  return a.key < b.key
end

local function choose(player, anchorId)
  for i = #candidates, 1, -1 do candidates[i] = nil end
  for i = #selected, 1, -1 do selected[i] = nil end
  for key in pairs(wanted) do wanted[key] = nil end
  local nearTile = M.NEAR_M + 181.02 -- half diagonal of trees.lua's 256 m road tiles
  local nearTile2 = nearTile * nearTile
  for _, tile in pairs(tiles) do
    if not tile.dropped then
      local consider = not tile.cx
      if tile.cx then
        local dx, dy = tile.cx - player.x, tile.cy - player.y
        consider = dx * dx + dy * dy <= nearTile2
      end
      if consider then
        for _, rec in pairs(tile.items) do addCandidate(rec, player, anchorId) end
      end
    end
  end
  table.sort(candidates, candidateLess)
  local dup2 = M.DUPLICATE_M * M.DUPLICATE_M
  for _, rec in ipairs(candidates) do
    local duplicate = false
    for _, chosen in ipairs(selected) do
      local dx, dy, dz = rec.px - chosen.px, rec.py - chosen.py, rec.pz - chosen.pz
      if dx * dx + dy * dy + dz * dz < dup2 then duplicate = true; break end
    end
    if not duplicate then
      selected[#selected + 1] = rec
      wanted[rec.key] = true
      if #selected >= M.MAX_ACTIVE then break end
    end
  end
  for i = #candidates, 1, -1 do candidates[i] = nil end
end

local function retireAll()
  for key, entry in pairs(active) do deleteEntry(entry); active[key] = nil end
end

-- Called when a Forest item has been successfully created. `scale` is the exact shape scale
-- passed to Forest:createNewItem; `offset` and `q` are the owning Forest's frame transform.
function M.add(tileKey, itemKey, group, row, scale, offset, q)
  if not M.HEAD[group] or type(row) ~= 'table' then return false end
  tileKey, itemKey = tostring(tileKey), tostring(itemKey)
  local x, y, z = tonumber(row[2]), tonumber(row[3]), tonumber(row[4])
  if not x or not y or not z then return false end
  local tile = ensureTile(tileKey, offset, q)
  local old = records[itemKey]
  if old and old.tile ~= tileKey then
    local previous = tiles[old.tile]
    if previous then previous.items[itemKey] = nil end
  end
  local rec = { key = itemKey, tile = tileKey, anchor = tileKey:match('^([^:]+):') or '', group = group,
    x = x, y = y, z = z, yaw = tonumber(row[5]) or 0, scale = tonumber(scale) or 1 }
  records[itemKey], tile.items[itemKey], tile.dropped = rec, rec, false
  itemPosition(tile, rec)
  local entry = active[itemKey]
  if entry then pcall(setPosition, entry.object, rec) end
  return true
end

-- Forest removal and tile-drop notifications are idempotent; active PointLights retire on the next bounded update.
function M.remove(itemKey)
  itemKey = tostring(itemKey)
  local rec = records[itemKey]
  if not rec then return end
  records[itemKey] = nil
  local tile = tiles[rec.tile]
  if tile then
    tile.items[itemKey] = nil
    if next(tile.items) == nil then tiles[rec.tile] = nil end
  end
end

function M.setTileTransform(tileKey, offset, q)
  local tile = tiles[tostring(tileKey)]
  if not tile then return end
  tile.offset, tile.quat = copy3(offset), copy4(q)
  updateTileCenter(tile)
  for itemKey, rec in pairs(tile.items) do
    itemPosition(tile, rec)
    local entry = active[itemKey]
    if entry then pcall(setPosition, entry.object, rec) end
  end
end

function M.dropTile(tileKey)
  tileKey = tostring(tileKey)
  local tile = tiles[tileKey]
  if not tile then return end
  tile.dropped = true
  for itemKey in pairs(tile.items) do records[itemKey] = nil end
  tile.items = {}
  tiles[tileKey] = nil
end

-- A missing player means free-roam has no active vehicle/camera anchor. Turn lights off and delete the
-- bounded active set immediately; this avoids leaving scene objects after vehicle-mode transitions.
function M.update(anchorId, player, dtReal)
  local dt = tonumber(dtReal)
  elapsed = elapsed + math.max(0, math.min(dt or M.UPDATE_INTERVAL_S, 1))
  if elapsed < M.UPDATE_INTERVAL_S then return end
  elapsed = elapsed % M.UPDATE_INTERVAL_S
  if not player or type(player.x) ~= 'number' or type(player.y) ~= 'number' or type(player.z) ~= 'number'
      or not nightLightsActive() then
    retireAll()
    return
  end

  choose(player, anchorId)
  for i = 1, #activeKeys do activeKeys[i] = nil end
  for key in pairs(active) do activeKeys[#activeKeys + 1] = key end
  table.sort(activeKeys)
  local removeBudget = M.REMOVE_BUDGET
  for _, key in ipairs(activeKeys) do
    local entry = active[key]
    if not wanted[key] then
      if entry then
        setEnabled(entry, false)
        if removeBudget > 0 then
          deleteEntry(entry)
          active[key] = nil
          removeBudget = removeBudget - 1
        end
      end
    else
      setEnabled(entry, true)
    end
  end
  local createBudget = M.CREATE_BUDGET
  for _, rec in ipairs(selected) do
    if not active[rec.key] and createBudget > 0 then
      local entry = createEmitter(rec)
      if entry then
        active[rec.key] = entry
        setEnabled(entry, true)
      end
      createBudget = createBudget - 1
    end
  end
  for i = #selected, 1, -1 do selected[i] = nil end
  for key in pairs(wanted) do wanted[key] = nil end
end

function M.clear()
  retireAll()
  tiles, records = {}, {}
  for key in pairs(wanted) do wanted[key] = nil end
  for i = #candidates, 1, -1 do candidates[i] = nil end
  for i = #selected, 1, -1 do selected[i] = nil end
  for i = #activeKeys, 1, -1 do activeKeys[i] = nil end
  elapsed = 0
end

function M.reset()
  M.clear()
  M.errors = 0
end

function M.status()
  local itemCount, tileCount, lightCount = 0, 0, 0
  for _ in pairs(records) do itemCount = itemCount + 1 end
  for _ in pairs(tiles) do tileCount = tileCount + 1 end
  for _ in pairs(active) do lightCount = lightCount + 1 end
  return { items = itemCount, tiles = tileCount, lights = lightCount, errors = M.errors }
end

return M
