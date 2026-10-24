-- Stubbed BeamNG GE globals for the offline tests (scene objects, vec3/quat, terrain, files).
-- Only what the mod calls; every engine behaviour here is an assumption to be verified in-game.
local S = { calls = {}, objects = {}, nextId = 100 }

function S.count(name) S.calls[name] = (S.calls[name] or 0) + 1 end

local vecmt = {}
vecmt.__index = vecmt
function vec3(x, y, z)
  if type(x) == 'table' then x, y, z = x.x, x.y, x.z end
  return setmetatable({ x = x or 0, y = y or 0, z = z or 0 }, vecmt)
end
vecmt.__add = function(a, b) return vec3(a.x + b.x, a.y + b.y, a.z + b.z) end
vecmt.__sub = function(a, b) return vec3(a.x - b.x, a.y - b.y, a.z - b.z) end
function vecmt:length() return math.sqrt(self.x ^ 2 + self.y ^ 2 + self.z ^ 2) end
function vecmt:normalized() local l = self:length(); return vec3(self.x / l, self.y / l, self.z / l) end
vecmt.__mul = function(a, b)
  if type(a) == 'number' then a, b = b, a end
  return vec3(a.x * b, a.y * b, a.z * b)
end
function vecmt:dot(b) return self.x * b.x + self.y * b.y + self.z * b.z end
function vecmt:cross(b) return vec3(self.y * b.z - self.z * b.y, self.z * b.x - self.x * b.z, self.x * b.y - self.y * b.x) end
function vecmt:toFloat3() return self end
function quat(x, y, z, w)
  if type(x) == 'table' then x, y, z, w = x.x, x.y, x.z, x.w end
  return { x = x or 0, y = y or 0, z = z or 0, w = w or 1 }
end
function quatFromDir() return quat(0, 0, 0, 1) end

function log() end
function dumps(v) return tostring(v) end

function S.newObject(class)
  S.nextId = S.nextId + 1
  local o = { id = S.nextId, class = class, fields = {}, pos = vec3(), rot = quat(), hidden = false }
  function o:getID() return self.id end
  function o:setName(n) self.name = n end
  function o:registerObject(n) self.name = n or self.name; S.objects[self.id] = self end
  function o:setField(k, _, v) self.fields[k] = v end
  function o:getField(k) return self.fields[k] end
  function o:flush() S.count('flush_' .. class) end
  function o:reload() S.count('reload_' .. class) end
  function o:setPosition(p) self.pos = vec3(p.x, p.y, p.z) end
  function o:setPosRot(x, y, z, qx, qy, qz, qw) S.count('setPosRot'); self.pos = vec3(x, y, z); self.rot = quat(qx, qy, qz, qw) end
  function o:getPosition() return vec3(self.pos.x, self.pos.y, self.pos.z) end
  function o:delete() S.objects[self.id] = nil; S.count('delete_' .. class) end
  function o:setHidden(h) self.hidden = h end
  function o:setRenderEnabled(v) S.count('setRenderEnabled'); self.renderEnabled = v end
  -- SimGroup
  function o:addObject(c) S.objects[c.id] = c; self.children = self.children or {}; self.children[#self.children + 1] = c end
  function o:getCount()
    local n = 0
    for _, c in ipairs(self.children or {}) do if S.objects[c.id] then n = n + 1 end end
    return n
  end
  function o:deleteAllObjects()
    for _, c in ipairs(self.children or {}) do if S.objects[c.id] then c:delete() end end
    self.children = {}
  end
  return o
end
function TerrainBlock()
  local tb = S.newObject('TerrainBlock')
  function tb:setTerrFileLvlFolder(p) self.folder = p end
  function tb:addMaterial() S.count('addMaterial') end
  function tb:updateMaterial(i, n)
    S.count('updateMaterial'); self.updated = self.updated or {}; self.updated[#self.updated + 1] = { i, n }
  end
  function tb:getMaterialNames()
    local r = {}
    for i, m in ipairs(self.lastImport and self.lastImport.mats or {}) do r[i] = m end
    return r
  end
  function tb:postApply() S.count('postApply_TerrainBlock'); self.applied = self.fields.terrainFile end
  function tb:importMaps(path, sq, maxH, hole, mats, maps, flip)
    S.count('importMaps'); self.lastImport = { path = path, sq = sq, maxH = maxH, mats = mats, maps = maps, flip = flip }
    return true
  end
  return tb
end
function createObject(class) S.count('createObject_' .. class); return S.newObject(class) end

scenetree = {
  MissionGroup = { addObject = function(_, o) S.objects[o.id] = o end },
  theLevelInfo = { visibleDistance = 1000 },
}
function scenetree.findObject(name)
  for _, o in pairs(S.objects) do if o.name == name then return o end end
end
function scenetree.findObjectById(id) return S.objects[id] end
function scenetree.findClassObjects(class)
  local r = {}
  for _, o in pairs(S.objects) do if o.class == class then r[#r + 1] = o.name end end
  return r
end

FS = { fileExists = function() return true end }
function loadJsonMaterialsFile() S.count('loadJsonMaterialsFile') end
be = { reloadCollision = function() S.count('reloadCollision') end }
function getCurrentLevelIdentifier() return 'beamng_earth' end

function S.liveOf(class)
  local r = {}
  for _, o in pairs(S.objects) do if o.class == class then r[#r + 1] = o end end
  return r
end

return S
