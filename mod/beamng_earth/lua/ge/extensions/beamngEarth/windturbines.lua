-- Wind turbines: installed ETK Driver Experience Centre shapes, referenced through the game's VFS.
-- Stock turbine_blades1.prefab supplies the rotor offset and playAmbient; its DAE turns once per 10 seconds.
-- No assets are bundled and no per-frame animation script is needed. The tower's extended lower skirt reaches
-- 27.08725 m below its installation origin; as on ETK, that origin is the terrain grade, not the mesh's lowest point.
local M = {}

local OBJ = '/levels/driver_training/art/shapes/objects/'
M.PARTS = {
  { shape = OBJ .. 'turbine_tower.dae', offset = { 0, 0, 0 }, animated = false },
  { shape = OBJ .. 'turbine_blades1.dae', offset = { 0, -3.52027345, 101.410286 }, animated = true },
}
M.MATERIAL_FILES = { '/art/shapes/common/common.materials.json',
  '/art/shapes/garage_and_dealership/main.materials.json' } -- lod_vertcol and m_plastic_trim
M.MODEL = { hub = 101.410286, rotorDiameter = 69.158482, tipHeight = 135.989527, skirtDepth = 27.08725 }
M.onCollide = nil

local turbines, owners, warned = {}, {}, {}
local materialsLoaded = false
local delivery, failedDelivery = 0, nil

local function warnOnce(key, message)
  if warned[key] then return end
  warned[key] = true
  log('W', 'beamngEarth', 'wind turbine: ' .. message)
end

local function finite(v)
  return type(v) == 'number' and v == v and v > -math.huge and v < math.huge
end

local function record(r)
  if type(r) ~= 'table' or type(r.id) ~= 'string' or r.id == '' or
      not finite(r.x) or not finite(r.y) or not finite(r.z) then return nil end
  local scale, yaw = r.scale == nil and 1 or r.scale, r.yaw == nil and 0 or r.yaw
  if not finite(scale) or scale <= 0 or not finite(yaw) then return nil end
  -- Own the mutable placement; rebasing must not change a road tile's decoded records.
  return { id = r.id, x = r.x, y = r.y, z = r.z, yaw = yaw, scale = scale,
    lat = r.lat, lon = r.lon, dimensions = r.dimensions, collide = r.collide ~= false }
end

local function available()
  -- A healthy installation is checked once per session, not four filesystem calls per turbine. A missing/broken
  -- installation is retried once per tile delivery, so a dense farm cannot repeat a failing resource load hundreds
  -- of times in the same batch; a later delivery can recover without an invisible placeholder.
  if materialsLoaded then return true end
  if failedDelivery == delivery then return false end
  failedDelivery = delivery
  local ok = true
  for _, p in ipairs(M.PARTS) do
    if FS and not FS:fileExists(p.shape) then
      warnOnce(p.shape, 'installed model unavailable: ' .. p.shape)
      ok = false
    end
  end
  for _, path in ipairs(M.MATERIAL_FILES) do
    if FS and not FS:fileExists(path) then
      warnOnce(path, 'installed material file unavailable: ' .. path)
      ok = false
    end
  end
  if not ok then return false end
  if not materialsLoaded then
    for _, path in ipairs(M.MATERIAL_FILES) do
      local loaded, err = pcall(loadJsonMaterialsFile, path)
      if not loaded or err == false then
        warnOnce(path, 'could not load installed materials: ' .. tostring(err))
        return false
      end
    end
    materialsLoaded = true
  end
  return true
end

-- Server yaw is degrees counter-clockwise about +z; terrain normals do not tilt the tower.
function M.partPositions(r)
  local th = math.rad(r.yaw or 0)
  local c, s, scale = math.cos(th), math.sin(th), r.scale or 1
  local out = {}
  for i, p in ipairs(M.PARTS) do
    local x, y, z = p.offset[1] * scale, p.offset[2] * scale, p.offset[3] * scale
    out[i] = { r.x + x * c - y * s, r.y + x * s + y * c, r.z + z }
  end
  return out
end

local function place(o, pos, r)
  local h = math.rad(r.yaw) / 2
  o:setScale(vec3(r.scale, r.scale, r.scale))
  o:setPosRot(pos[1], pos[2], pos[3], 0, 0, -math.sin(h), math.cos(h))
end

local function deleteParts(st)
  local changed = false
  for _, id in ipairs(st.objs) do
    local o = scenetree.findObjectById(id)
    if o then o:delete(); changed = true end
  end
  local collisionChanged = changed and st.collision == true
  st.objs, st.collision = {}, nil
  return collisionChanged
end

local function partsLive(st)
  if #st.objs ~= #M.PARTS then return false end
  for _, id in ipairs(st.objs) do if not scenetree.findObjectById(id) then return false end end
  return true
end

local function makeParts(st)
  if not available() then return false end
  local created = {}
  local ok, err = pcall(function()
    local positions = M.partPositions(st.rec)
    for i, p in ipairs(M.PARTS) do
      local o = createObject('TSStatic')
      created[#created + 1] = o
      o:setField('shapeName', 0, p.shape)
      o:setField('playAmbient', 0, p.animated and '1' or '0')
      o:setField('collisionType', 0, st.rec.collide and 'Collision Mesh' or 'None')
      o:setField('decalType', 0, 'None')
      -- As in the stock prefab, the stationary tower can use native static batching. Ambient playback already
      -- gives the rotor its own animated shape instance; forcing every part dynamic defeats that optimization.
      o:setField('dynamic', 0, 'false')
      o:setField('meshCulling', 0, '0')
      o:setField('forceDetail', 0, '-1') -- preserve native screen-space LOD, never pin hundreds of high-detail meshes
      o.canSave = false
      place(o, positions[i], st.rec)
      o:registerObject('')
      if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
      st.objs[#st.objs + 1] = o:getID()
    end
  end)
  if not ok then
    for _, o in ipairs(created) do pcall(function() o:delete() end) end
    st.objs = {}
    warnOnce(st.rec.id, 'could not instantiate ' .. st.rec.id .. ': ' .. tostring(err))
    return false
  end
  st.collision = st.rec.collide
  return true
end

local function samePlace(a, b)
  return a.x == b.x and a.y == b.y and a.z == b.z and a.yaw == b.yaw and a.scale == b.scale
end

local function replace(st, r)
  local changed = not samePlace(st.rec, r)
  st.rec = r
  if not partsLive(st) then
    local removed = deleteParts(st)
    local made = makeParts(st)
    return (made and st.collision) or removed
  end
  local collisionChanged = st.collision ~= r.collide
  if collisionChanged then
    for _, id in ipairs(st.objs) do
      scenetree.findObjectById(id):setField('collisionType', 0, r.collide and 'Collision Mesh' or 'None')
    end
    st.collision = r.collide
  end
  if changed then
    for i, pos in ipairs(M.partPositions(r)) do place(scenetree.findObjectById(st.objs[i]), pos, r) end
  end
  return collisionChanged or (changed and st.collision)
end

-- Near-road owners carry terrain-grade placement and collision; far landmark owners carry render-only placement.
-- Stable key ordering within each priority tier avoids delivery-order dependence. A retiring near tile cannot
-- remove collision while another near owner still needs the turbine.
local function preferred(st)
  local key, best
  for candidate, r in pairs(st.keys) do
    if not best or (r.collide and not best.collide) or
        (r.collide == best.collide and candidate < key) then
      key, best = candidate, r
    end
  end
  st.owner = key
  return best
end

-- A stable OSM ID may be owned by both a retiring tile and its replacement during a rebase. Attach new ownership
-- before removing old membership, and reconcile even empty records, so that neither duplicates nor stale props remain.
function M.onRows(key, rows)
  if type(key) ~= 'string' then return end
  delivery = delivery + 1
  local wanted, changed = {}, false
  for _, row in ipairs(type(rows) == 'table' and rows or {}) do
    local r = record(row)
    if r and not wanted[r.id] then
      wanted[r.id] = true
      local st = turbines[r.id]
      if not st then
        st = { rec = r, keys = {}, objs = {} }
        turbines[r.id] = st
      end
      st.keys[key] = r
      if replace(st, preferred(st)) then changed = true end
    end
  end
  for id in pairs(owners[key] or {}) do
    if not wanted[id] then
      local st = turbines[id]
      if st then
        st.keys[key] = nil
        if not next(st.keys) then
          if deleteParts(st) then changed = true end
          turbines[id] = nil
        elseif replace(st, preferred(st)) then
          changed = true
        end
      end
    end
  end
  owners[key] = next(wanted) and wanted or nil
  if changed and M.onCollide then M.onCollide() end
end

function M.placeRows(key, rows) M.onRows(key, rows) end
function M.dropKey(key) M.onRows(key, {}) end

-- Rebase the installation point and its heading, then keep the tower upright in the new local frame. Its long
-- buried skirt is a property of the stock shape, never an elevation correction or an alignment to terrain slope.
function M.transform(xf)
  local changed = false
  for _, st in pairs(turbines) do
    -- Transform every owner's retained record: dropping a near owner after a rebase must not restore the far
    -- owner's old-frame coordinates. This runs at rebases only, never once per frame for blade animation.
    for key, r in pairs(st.keys) do
      local th = math.rad(r.yaw)
      local hx, hy = xf:apply(r.x + math.cos(th), r.y + math.sin(th), r.z)
      local x, y, z = xf:apply(r.x, r.y, r.z)
      local nextRec = record(r)
      nextRec.x, nextRec.y, nextRec.z = x, y, z
      nextRec.yaw = math.deg(math.atan2(hy - y, hx - x))
      st.keys[key] = nextRec
    end
    -- An unavailable resource is retried only on tile delivery, never during the transform itself.
    local nextRec = preferred(st)
    st.rec = nextRec
    if partsLive(st) then
      for i, pos in ipairs(M.partPositions(nextRec)) do place(scenetree.findObjectById(st.objs[i]), pos, nextRec) end
      if st.collision then changed = true end
    end
  end
  if changed and M.onCollide then M.onCollide() end
end

function M.reset()
  local changed = false
  for _, st in pairs(turbines) do if deleteParts(st) then changed = true end end
  turbines, owners, warned, materialsLoaded = {}, {}, {}, false
  delivery, failedDelivery = 0, nil
  if changed and M.onCollide then M.onCollide() end
end

function M.status()
  local out = { turbines = 0, objects = 0, owners = 0, unavailable = 0, colliding = 0,
    resourcesReady = materialsLoaded, items = {} }
  for id, st in pairs(turbines) do
    out.turbines = out.turbines + 1
    local loaded, r = partsLive(st), st.rec
    if loaded then out.objects = out.objects + #st.objs else out.unavailable = out.unavailable + 1 end
    if loaded and st.collision then out.colliding = out.colliding + 1 end
    out.items[id] = { lat = r.lat, lon = r.lon, x = r.x, y = r.y, z = r.z,
      yaw = r.yaw, scale = r.scale, loaded = loaded, dimensions = r.dimensions, collide = r.collide }
  end
  for _ in pairs(owners) do out.owners = out.owners + 1 end
  return out
end

return M
