-- The Vienna Convention's round speed-limit signs (the km/h signs outside the mph countries; server
-- furniture.SIGN_KMH): everything about them that trees.lua would otherwise carry. Which groups exist, the shape and
-- height of each, how far out they show (install merges these into trees.lua's tables), and how a sign is made: two
-- items at one spot, the face (a thin quad with the sign's texture) and the body every sign shares, which alone
-- collides (body, faceSolid, addBody, remove).
local M = {}

local SIGNS = '/levels/beamng_earth/art/shapes/beamngEarth/'

-- Generated in this mod's art folder so roads can use the continent-specific limits and Germany's no-limit sign
-- without relying on stock placeholders.
M.LIMITS = { 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150 }
M.NONE = 'speedknone'          -- Germany's end-of-limit sign
M.FACE_HEIGHT_M = 2.47         -- a sign's height (m): an item's scale is the row's height over this
M.MAX_M = 300                  -- the tile distance (m) beyond which a sign is not shown
-- The sign body every sign shares (the pole, the bras and the plate's back): one model made once instead of a
-- copy of it in every sign's file.
M.BODY = SIGNS .. 'sign_round_body'

-- Pure: the group of a limit in km/h (30 -> 'speedk30').
function M.group(limit)
  return 'speedk' .. tostring(limit)
end

M.CATALOG = {}          -- group -> { { face shape, height } } (trees.CATALOG's entries)
M.GROUP_MAX_M = {}      -- group -> MAX_M (trees.GROUP_MAX_M's entries)
local isSign = {}       -- group -> true

local function add(group, face)
  M.CATALOG[group] = { { SIGNS .. face, M.FACE_HEIGHT_M } }
  M.GROUP_MAX_M[group] = M.MAX_M
  isSign[group] = true
end

for _, limit in ipairs(M.LIMITS) do add(M.group(limit), 'sign_speed' .. limit .. 'kph') end
add(M.NONE, 'sign_speed_none')

-- trees: trees.lua's module. Its CATALOG and GROUP_MAX_M take the signs' entries.
function M.install(trees)
  for g, v in pairs(M.CATALOG) do trees.CATALOG[g] = v end
  for g, m in pairs(M.GROUP_MAX_M) do trees.GROUP_MAX_M[g] = m end
end

-- Pure: the shape of the second item a group's items are made with (a sign's body), nil for any other group.
function M.body(group)
  if isSign[group] then return M.BODY end
  return nil
end

-- Pure: whether an item of the group collides, given that it would (solid). A sign's face never does: the body, the
-- pole and the back of the plate, is what the car hits.
function M.faceSolid(group, solid)
  if isSign[group] then return false end
  return solid
end

local PAIR = {}         -- the metatable of a sign's two items (face, body): one entry of a tile's made items

-- A sign's body made at its face's spot, scale and heading: the pair of the face item and the body item, or nil if
-- the body could not be made (the face stays as it is). body: its shape as the tile spells it; itemType: trees.lua's
-- (shape, solid) -> its item type.
function M.addBody(fd, face, body, m, sc, solid, itemType)
  local d = itemType(body, solid)
  if not d then return nil endcket
  local ok, it = pcall(function() return fd:createNewItem(d, m, sc) end)
  if not ok or not it then return nil end
  return setmetatable({ face, it }, PAIR)
end

-- Removes a pair made by addBody: true if it was one (both its items go), false for any other item (the caller's).
function M.remove(fd, it)
  if getmetatable(it) ~= PAIR then return false end
  for _, part in ipairs(it) do pcall(function() fd:removeItem(part) end) end
  return true
end

return M
