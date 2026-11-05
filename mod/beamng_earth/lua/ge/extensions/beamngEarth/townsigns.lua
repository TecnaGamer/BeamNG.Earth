-- Town entry and exit signs (server townsigns.py, BNE_TOWNSIGNS=on; the user, 2026-10-28: the sign of every town and
-- village as you drive in). Each commune's face is a shape and a texture the server writes next to the road tile, which
-- names them ("townsigns": group -> { shape, texture, material }); the body (two posts and the plate's back) is the
-- one model every town sign shares (sign_town_body, tools/generate_town_signs.py). register makes a face's Material
-- (mapTo its material name, the texture its colorMap, as satmat.lua's imagery) and its trees.lua catalog entry before
-- the tile's props are made; trees.lua then puts the face and the body at one spot, as the speed-limit signs'
-- (vienna_convention_speed_signs.lua's addBody and remove).
--
-- A face's Material is made once and kept for the session: a commune's signs come back with every tile that has them.
-- One per commune and kind driven past (the engine's frame cost grows with every Material made, satmat.lua: a long
-- drive through France makes a few hundred, the far tiles' thousands were the trouble).
local M = {}

M.BODY = '/levels/beamng_earth/art/shapes/beamngEarth/sign_town_body'
-- the model's height (server townsigns.SIGN_H): an item's scale is the row's height over it
M.HEIGHT_M = 2.1
M.MAX_M = 400                  -- the tile distance (m) beyond which a sign is not shown
-- The face's Material (the plain 1.0 format, as the speed-limit signs' faces; a mild gloss)
M.FIELDS = {
  { 'diffuseColor', '1 1 1 1' },
  { 'specular', '0.5 0.5 0.5 1' },
  { 'specularPower', '20' },
  { 'pixelSpecular', '1' },
}
M.FALLBACK_COLOR = '0.97 0.97 0.97 1'   -- texture file missing: a blank white plate

-- The trees and bushes kept off a sign (in game 2026-10-28, Boisset-et-Gaujac: the vegetation tiles' trees, their tops
-- moved off the road onto the verge, stood in front of the signs; the street furniture's are cleared by the server,
-- townsigns.clear_around): hidden within CLEAR_M of a sign (trees.lua's hiders)
M.CLEAR_M = 6.0
M.TREE_GROUPS = { deciduous = true, conifer = true, shrub = true, pine_west = true, oak_west = true, palm = true,
  juniper = true, tropical = true, desert_shrub = true }
M.ANCHORS_KEPT = 3     -- the signs of this many latest anchors are kept (an older anchor's tiles are gone)

local faces = {}       -- group -> true: the face groups registered
local spots = {}       -- anchor -> { 'cx,cy' (CLEAR_M cells) -> { 'x,y' -> { x, y } } }: the signs' places
local anchors = {}     -- the anchors in spots, oldest first
local mats = {}        -- material name -> the texture it was made (or adopted) with
M.count = 0            -- Materials made

-- Pure: the shape a tile names (its file) as the catalog spells shapes (trees.lua adds '.dae').
function M.shapeOf(path)
  return (path:gsub('%.dae$', ''))
end

-- Create the face's Material (as satmat.create: createObject, setField, canSave = false, registerObject), or adopt
-- one of that name left from an earlier session or extension reload (pointed at this session's texture). A face seen
-- again with another texture (a restarted server writes its faces in its new session's folder and deletes the old
-- one: in game 2026-10-28, Thuir's sign showed the engine's orange "no texture") takes the new file and reloads, as
-- satmat.ref's.
local function makeMaterial(name, texture)
  local key = texture or false   -- (none: a white plate, made once too)
  if mats[name] == key then return end
  local seen = mats[name] ~= nil
  mats[name] = key
  local ok = type(texture) == 'string' and FS:fileExists(texture)
  if not ok then log('W', 'beamngEarth', 'town sign texture missing: ' .. tostring(texture)) end
  local m = scenetree.findObject(name)
  if m then
    if ok then
      m:setField('colorMap', 0, texture)
      m:setField('diffuseColor', 0, M.FIELDS[1][2])
      if seen then
        pcall(function() m:flush() end)
        pcall(function() m:reload() end)
      end
    end
    return
  end
  m = createObject('Material')
  m:setField('mapTo', 0, name)
  for _, f in ipairs(M.FIELDS) do m:setField(f[1], 0, f[2]) end
  if ok then
    m:setField('colorMap', 0, texture)
  else
    m:setField('diffuseColor', 0, M.FALLBACK_COLOR)
  end
  m.canSave = false
  m:registerObject(name)
  M.count = M.count + 1
end

-- defs: a road tile's "townsigns"; trees: trees.lua's module (its CATALOG and GROUP_MAX_M take the faces). Returns how
-- many entries it took (a malformed one is skipped).
function M.register(trees, defs)
  local n = 0
  if type(defs) ~= 'table' then return n end
  for g, d in pairs(defs) do
    if type(g) == 'string' and type(d) == 'table' and type(d.shape) == 'string' and type(d.material) == 'string' then
      if not faces[g] then
        trees.CATALOG[g] = { { M.shapeOf(d.shape), M.HEIGHT_M } }
        trees.GROUP_MAX_M[g] = M.MAX_M
        faces[g] = true
      end
      makeMaterial(d.material, d.texture)
      n = n + 1
    end
  end
  return n
end

-- Pure: the shape of the second item an entry face's items are made with (the body), nil for any other group. An exit
-- face has none: the server puts it at its entry face's spot turned round, on that body's back (server
-- townsigns._sign; the user, 2026-10-28: back to back, as on the real roads). A country whose plate is not France's
-- has its code in the group ('townin_be_<hash>', server townsigns.PLATES) and its own body (sign_town_body_be).
function M.body(group)
  if not (faces[group] and group:sub(1, 7) == 'townin_') then return nil end
  local cc = group:match('^townin_(%l%l)_')
  return cc and (M.BODY .. '_' .. cc) or M.BODY
end

-- Pure: whether an item of the group collides, given that it would (solid). A face never does: the body is what the
-- car hits.
function M.faceSolid(group, solid)
  if faces[group] then return false end
  return solid
end

-- Is the group a town sign's face (registered)?
function M.isFace(group) return faces[group] == true end

local function cellOf(x, y)
  return math.floor(x / M.CLEAR_M), math.floor(y / M.CLEAR_M)
end

-- A road tile's signs (its props' items {group index, x, y, ...} and groups, the anchor's frame): their places kept,
-- and the trees.lua tiles near the new ones looked at again, once for the tile's new signs together (their trees
-- hidden: hides; one pass over trees.lua's tiles, not one a sign). Returns how many were new.
function M.addSigns(trees, anchor, items, groups)
  local n = 0
  if type(items) ~= 'table' or type(groups) ~= 'table' then return n end
  local x0, y0, x1, y1 = math.huge, math.huge, -math.huge, -math.huge
  for _, it in ipairs(items) do
    local g = type(it) == 'table' and groups[(tonumber(it[1]) or -1) + 1]
    local x, y = type(it) == 'table' and tonumber(it[2]), type(it) == 'table' and tonumber(it[3])
    if g and faces[g] and x and y then
      if not spots[anchor] then
        spots[anchor] = {}
        anchors[#anchors + 1] = anchor
        while #anchors > M.ANCHORS_KEPT do spots[table.remove(anchors, 1)] = nil end
      end
      local cx, cy = cellOf(x, y)
      local ck = cx .. ',' .. cy
      local cell = spots[anchor][ck]
      if not cell then cell = {}; spots[anchor][ck] = cell end
      local pk = string.format('%.1f,%.1f', x, y)
      if not cell[pk] then
        cell[pk] = { x, y }
        n = n + 1
        x0, y0, x1, y1 = math.min(x0, x), math.min(y0, y), math.max(x1, x), math.max(y1, y)
      end
    end
  end
  if n > 0 and trees and trees.refreshNear then
    trees.refreshNear(anchor, (x0 + x1) / 2, (y0 + y1) / 2, math.max(x1 - x0, y1 - y0) / 2 + M.CLEAR_M)
  end
  if trees and trees.hiders then trees.hiders.townsigns = M.hides end
  return n
end

-- trees.lua's hider: is an item of the group at (x, y) of the anchor a tree or bush within CLEAR_M of a town sign?
function M.hides(group, x, y, anchor)
  if not M.TREE_GROUPS[group] then return false end
  local a = spots[anchor]
  if not a then return false end
  local cx, cy = cellOf(x, y)
  local r2 = M.CLEAR_M * M.CLEAR_M
  for dx = -1, 1 do
    for dy = -1, 1 do
      local cell = a[(cx + dx) .. ',' .. (cy + dy)]
      if cell then
        for _, p in pairs(cell) do
          if (p[1] - x) ^ 2 + (p[2] - y) ^ 2 < r2 then return true end
        end
      end
    end
  end
  return false
end

-- Forget the faces and the signs' places (a new level: its trees.lua catalog is rebuilt; the Materials stay, adopted
-- when seen again).
function M.reset()
  faces = {}
  mats = {}
  spots = {}
  anchors = {}
end

return M
