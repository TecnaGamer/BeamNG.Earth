-- Direction signs before junctions (server dirsigns.py; the settings page's Direction signs, BNE_DIRSIGNS=on). A board's
-- face is a picture the server drew (the sign tile's "dirs_faces": the Material name its mesh uses -> the texture's VFS
-- path); the mesh itself (panel, back, posts) is in the tile's "dirs" shape. register makes the Materials before the
-- shape is made (as townsigns.lua / satmat.lua: a Lua-created Material with mapTo = the mesh's material name is what the
-- mesh shows), once for each name: a board seen again (another tile, a revisit) shares it.
--
-- The engine's frame cost grows with every Material made (satmat.lua): the server makes a board's name from what it shows
-- (identical boards share one) and at most dirsigns.MAX_FACES a tile. Their number in a long drive is measured in game
-- (docs/new/001_direction-signs...: Material cost); nothing is deleted here yet.
local M = {}

M.FIELDS = {                 -- the plain 1.0 Material, a mild gloss: a painted retroreflective board
  { 'diffuseColor', '1 1 1 1' },
  { 'specular', '0.35 0.35 0.35 1' },
  { 'specularPower', '16' },
  { 'pixelSpecular', '1' },
  { 'useAnisotropic', '1' },
}
M.FALLBACK_COLOR = '0.0 0.33 0.65 1'   -- picture missing: a plain blue board

local mats = {}              -- name -> { texture, uses, seq }
local order = 0
M.count = 0                  -- Materials made

-- Pure: does the table look like a tile's dirs_faces ({ name -> vfs path })?
function M.valid(faces)
  if type(faces) ~= 'table' then return false end
  for k, v in pairs(faces) do
    if type(k) ~= 'string' or type(v) ~= 'string' then return false end
  end
  return true
end

local function make(name, texture)
  require('beamngEarth/texdbg').log('dirsign', name, texture)
  local ok = FS and FS:fileExists(texture)
  if not ok then log('W', 'beamngEarth', 'direction sign picture missing: ' .. tostring(texture)) end
  local m = scenetree.findObject(name)
  if m then                                   -- left from an earlier session or extension reload: adopt it
    if ok then
      m:setField('colorMap', 0, texture)
      m:setField('diffuseColor', 0, M.FIELDS[1][2])
    end
    return
  end
  m = createObject('Material')
  m:setField('mapTo', 0, name)
  for _, f in ipairs(M.FIELDS) do m:setField(f[1], 0, f[2]) end
  if ok then m:setField('colorMap', 0, texture) else m:setField('diffuseColor', 0, M.FALLBACK_COLOR) end
  m.canSave = false
  m:registerObject(name)
  M.count = M.count + 1
end

-- The bare metal of the boards' backs and posts (the server's mesh material 0, dirsigns.METAL): one Material, made once
-- (in game 2026-11-02: "Unable to find material mapping to: bne_dir_metal", the back drew as IMPORT FAILED).
M.METAL = 'bne_dir_metal'
local function ensureMetal()
  if scenetree.findObject(M.METAL) then return end
  local m = createObject('Material')
  m:setField('mapTo', 0, M.METAL)
  m:setField('diffuseColor', 0, '0.55 0.57 0.58 1')
  m:setField('specular', 0, '0.5 0.5 0.5 1')
  m:setField('specularPower', 0, '24')
  m:setField('pixelSpecular', 0, '1')
  m.canSave = false
  m:registerObject(M.METAL)
end

-- The tile's boards' Materials. Returns how many names it took.
function M.register(faces)
  if not M.valid(faces) then return 0 end
  ensureMetal()
  local n = 0
  for name, texture in pairs(faces) do
    local got = mats[name]
    if not got then
      order = order + 1
      mats[name] = { texture = texture, seq = order }
      make(name, texture)
      n = n + 1
    elseif got.texture ~= texture then       -- a restarted server's session folder: the new file
      got.texture = texture
      make(name, texture)
    end
  end
  return n
end

-- Forget the names (a new level: the Materials stay, adopted when seen again).
function M.reset()
  mats = {}
  order = 0
end

-- The number of names registered.
function M.size()
  local n = 0
  for _ in pairs(mats) do n = n + 1 end
  return n
end

return M
