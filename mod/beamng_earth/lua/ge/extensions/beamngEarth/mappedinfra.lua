-- Installed infrastructure shapes; no proprietary resources are bundled.
-- Poles/towers stay upright. The stock wire's +X endpoint axis is fitted to the
-- actual two attachment points; its width and measured sag are not stretched.
local M = {}
local KIT = '/art/shapes/common/power_lines_procedural/'
local COMMON = '/art/shapes/common/common.materials.json'
local OBJECTS = '/art/shapes/objects/main.materials.json'
local POLE_MATS = { COMMON, KIT .. 'main.materials.json', '/art/shapes/garage_and_dealership/main.materials.json' }
M.ASSETS = {
  pylon = { shape = '/levels/automation_test_track/art/shapes/objects/pylon.dae', materials = {
    COMMON, '/art/shapes/garage_and_dealership/main.materials.json',
    '/levels/automation_test_track/art/shapes/buildings/main.materials.json' } },
  hvpole = { shape = KIT .. 'electric_pole_wood_old_01.dae', materials = POLE_MATS },
  celltower = { shape = '/levels/johnson_valley/art/shapes/misc/celltower.dae', materials = {
    COMMON, OBJECTS, '/levels/johnson_valley/art/shapes/misc/main.materials.json' } },
  mast = { shape = '/levels/italy/art/shapes/buildings/italy_bld_antenna.dae', materials = {
    COMMON, '/levels/italy/art/shapes/buildings/main.materials.json',
    '/art/shapes/garage_and_dealership/Clutter/main.materials.json' } },
  tower = { shape = '/levels/west_coast_usa/art/shapes/buildings/antenna.dae', materials = { COMMON, OBJECTS } },
  pole = { shape = KIT .. 'electric_pole_wood_old_01.dae', materials = POLE_MATS },
  wire30 = { shape = KIT .. 'wire_30m_01.dae', materials = POLE_MATS, span = 30 },
  wire40 = { shape = KIT .. 'wire_40m_01.dae', materials = POLE_MATS, span = 40 },
}
M.onCollide = nil
local objects, owners, warned, loaded = {}, {}, {}, {}

local function warn(key, message)
  if warned[key] then return end
  warned[key] = true
  log('W', 'beamngEarth.mappedinfra', message)
end
local function finite(x) return type(x) == 'number' and x == x and math.abs(x) < math.huge end
local function record(r)
  if type(r) ~= 'table' or type(r.id) ~= 'string' or r.id == '' or not M.ASSETS[r.asset] or
      not finite(r.x) or not finite(r.y) or not finite(r.z) or not finite(r.scale) or r.scale <= 0 or
      not finite(r.yaw) then return nil end
  local n = {}; for k, v in pairs(r) do n[k] = v end
  n.collide = not M.ASSETS[r.asset].span and r.collide ~= false
  if M.ASSETS[r.asset].span then
    if type(r['end']) ~= 'table' or not finite(r['end'][1]) or not finite(r['end'][2]) or
        not finite(r['end'][3]) then return nil end
    n['end'] = { r['end'][1], r['end'][2], r['end'][3] }
    local d = (n['end'][1] - r.x)^2 + (n['end'][2] - r.y)^2 + (n['end'][3] - r.z)^2
    if d < 1 or d > 300^2 then return nil end
  end
  return n
end

-- Two legacy bindings in Italy's supplied DAE have no installed material definition.
-- Neutral concrete / dark wire aliases prevent orange missing-material faces;
-- the antenna's own installed textures and all model geometry remain unchanged.
local function mastAliases()
  for name, color in pairs({ italy_bld_old_concrete = '0.5 0.5 0.47 1', italy_bld_electric_wire = '0.05 0.05 0.05 1' }) do
    if not scenetree.findObject(name) then
      local mat = createObject('Material')
      if not mat then return false end
      mat:setField('mapTo', 0, name)
      mat:setField('diffuseColor', 0, color)
      mat:setField('roughnessFactor', 0, '0.85')
      mat.canSave = false
      mat:registerObject(name)
      if Sim and Sim.getDataBlockSet then Sim.getDataBlockSet():addObject(mat) end
    end
  end
  return true
end
local function available(asset)
  if loaded[asset] then return true end
  local a = M.ASSETS[asset]
  -- Missing pole models also suppress their wires; no floating cable placeholders.
  for _, shape in ipairs(a.span and { a.shape, M.ASSETS.pole.shape } or { a.shape }) do
    if FS and not FS:fileExists(shape) then warn(shape, 'Installed infrastructure asset missing: ' .. shape); return false end
  end
  for _, path in ipairs(a.materials) do
    if FS and not FS:fileExists(path) then warn(path, 'Installed material file missing: ' .. path); return false end
    if not loaded[path] then
      local ok, result = pcall(loadJsonMaterialsFile, path)
      if not ok or result == false then warn(path, 'Could not load material file: ' .. path); return false end
      loaded[path] = true
    end
  end
  if asset == 'mast' and not mastAliases() then return false end
  loaded[asset] = true
  return true
end

-- BeamNG's setPosRot uses the inverse quaternion of an active mathematical rotation.
-- Return scale and quaternion separately so endpoints can be tested without engine APIs.
function M.pose(r)
  local a = M.ASSETS[r.asset]
  if not a.span then
    local h = math.rad(r.yaw) / 2
    return { r.scale, r.scale, r.scale }, { 0, 0, -math.sin(h), math.cos(h) }
  end
  local dx, dy, dz = r['end'][1] - r.x, r['end'][2] - r.y, r['end'][3] - r.z
  local xy = math.sqrt(dx * dx + dy * dy)
  local yaw, pitch = math.atan2(dy, dx) / 2, math.atan2(dz, xy) / 2
  local sy, cy, sp, cp = math.sin(yaw), math.cos(yaw), math.sin(pitch), math.cos(pitch)
  return { math.sqrt(xy * xy + dz * dz) / a.span, 1, 1 }, { -sy * sp, cy * sp, -sy * cp, cy * cp }
end
local function place(o, r)
  local scale, q = M.pose(r)
  o:setScale(vec3(scale[1], scale[2], scale[3]))
  o:setPosRot(r.x, r.y, r.z, q[1], q[2], q[3], q[4])
end
local function remove(st)
  local o = st.obj and scenetree.findObjectById(st.obj)
  if o then o:delete() end
  st.obj = nil
  return o ~= nil and st.rec.collide == true
end
local function preferred(st)
  local key, best
  for k, r in pairs(st.keys) do
    if not best or (r.collide and not best.collide) or (r.collide == best.collide and k < key) then
      key, best = k, r
    end
  end
  return best
end
local function replace(st, r)
  local before = st.rec
  local o = st.obj and scenetree.findObjectById(st.obj)
  local changed = not before or before.asset ~= r.asset or before.x ~= r.x or before.y ~= r.y or
    before.z ~= r.z or before.yaw ~= r.yaw or before.scale ~= r.scale
  if before and r['end'] and before['end'] then
    for i = 1, 3 do if before['end'][i] ~= r['end'][i] then changed = true end end
  end
  local removed = false
  if before and before.asset ~= r.asset then removed = remove(st); o = nil end
  st.rec = r
  if not o then
    local checked, ready = pcall(available, r.asset)
    if not checked then warn(r.asset, 'Could not prepare asset: ' .. tostring(ready)) end
    if not checked or not ready then return removed end
    local ok, err = pcall(function()
      o = createObject('TSStatic')
      o:setField('shapeName', 0, M.ASSETS[r.asset].shape)
      o:setField('collisionType', 0, r.collide and 'Collision Mesh' or 'None')
      o:setField('decalType', 0, 'None')
      o:setField('dynamic', 0, 'false')
      o:setField('playAmbient', 0, '0')
      o:setField('forceDetail', 0, '-1')
      o.canSave = false
      place(o, r)
      o:registerObject('bngEarthMapped_' .. r.id:gsub('[^%w]', '_'))
      if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
      st.obj = o:getID()
    end)
    if not ok then
      if o then pcall(function() o:delete() end) end
      st.obj = nil
      warn(r.id, 'Could not instantiate ' .. r.id .. ': ' .. tostring(err))
      return removed
    end
    changed = true
  else
    if before.collide ~= r.collide then
      o:setField('collisionType', 0, r.collide and 'Collision Mesh' or 'None')
      removed = true -- either enabling or disabling collision needs the existing reload scheduler
    end
    -- Shared owners and identical tile reloads often deliver the same pose.
    -- Avoid engine transform/render updates unless the geometry actually moved.
    if changed then place(o, r) end
  end
  return removed or (changed and r.collide)
end

function M.onRows(key, rows)
  if type(key) ~= 'string' then return end
  local wanted, changed = {}, false
  for _, row in ipairs(type(rows) == 'table' and rows or {}) do
    local r = record(row)
    if r and not wanted[r.id] then
      wanted[r.id] = true
      local st = objects[r.id] or { keys = {} }
      objects[r.id], st.keys[key] = st, r
      if replace(st, preferred(st)) then changed = true end
    end
  end
  for id in pairs(owners[key] or {}) do
    if not wanted[id] then
      local st = objects[id]
      if st then
        st.keys[key] = nil
        if not next(st.keys) then
          if remove(st) then changed = true end
          objects[id] = nil
        elseif replace(st, preferred(st)) then changed = true end
      end
    end
  end
  owners[key] = next(wanted) and wanted or nil
  if changed and M.onCollide then M.onCollide() end
end
function M.dropKey(key) M.onRows(key, {}) end
function M.transform(xf)
  local changed = false
  for _, st in pairs(objects) do
    for key, r in pairs(st.keys) do
      local n = record(r)
      local hx, hy = xf:apply(r.x + math.cos(math.rad(r.yaw)), r.y + math.sin(math.rad(r.yaw)), r.z)
      n.x, n.y, n.z = xf:apply(r.x, r.y, r.z)
      n.yaw = math.deg(math.atan2(hy - n.y, hx - n.x))
      if n['end'] then n['end'] = { xf:apply(unpack(r['end'])) } end
      st.keys[key] = n
    end
    -- Rebasing never retries unavailable resources or invents placeholders.
    st.rec = preferred(st)
    local o = st.obj and scenetree.findObjectById(st.obj)
    if o then
      place(o, st.rec)
      if st.rec.collide then changed = true end
    end
  end
  if changed and M.onCollide then M.onCollide() end
end
function M.reset()
  local changed = false
  for _, st in pairs(objects) do if remove(st) then changed = true end end
  objects, owners, warned, loaded = {}, {}, {}, {}
  if changed and M.onCollide then M.onCollide() end
end
function M.status()
  local out = { objects = 0, unavailable = 0, items = {} }
  for id, st in pairs(objects) do
    local live = st.obj and scenetree.findObjectById(st.obj) ~= nil
    out.objects = out.objects + (live and 1 or 0)
    out.unavailable = out.unavailable + (live and 0 or 1)
    out.items[id] = { asset = st.rec.asset, x = st.rec.x, y = st.rec.y, z = st.rec.z,
      loaded = live, collide = st.rec.collide }
  end
  return out
end
return M
