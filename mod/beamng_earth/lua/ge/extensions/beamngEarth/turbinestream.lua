-- Long-distance mapped turbines ride the existing server geotile tracker. Retain source frames so a rebase or
-- teleport maps their installation points exactly; windturbines.lua owns stock objects and deduplicates near/far IDs.
local M = { PROTOCOL = 1 }
local geotile = require('beamngEarth/geotile')
local renderer = require('beamngEarth/windturbines')
local tiles = {}

local function owner(key) return 'turbines:' .. tostring(key) end

local function place(key, tile, anchor)
  if not anchor then return end
  local pl = geotile.placement(tile.frame, anchor, 0, 0, tile.pivot)
  local rows, yaw = {}, geotile.yawOf(pl)
  for _, r in ipairs(tile.rows) do
    if type(r) == 'table' and type(r.x) == 'number' and type(r.y) == 'number' and type(r.z) == 'number' then
      local n = {}
      for k, v in pairs(r) do n[k] = v end
      n.x, n.y, n.z = geotile.apply(pl, r.x, r.y, r.z)
      n.yaw, n.collide = (tonumber(r.yaw) or 0) + yaw, false
      rows[#rows + 1] = n
    end
  end
  renderer.onRows(owner(key), rows)
end

function M.onTile(msg, anchor, sessionId)
  if type(msg) ~= 'table' or not geotile.isKey(msg.key) then return false end
  local ok, data = true, msg.data
  if type(data) ~= 'table' then
    if type(msg.path) ~= 'string' then return false end
    ok, data = pcall(function() return jsonDecode(readFile(msg.path) or '') end)
  end
  if not ok or type(data) ~= 'table' or type(data.turbines) ~= 'table' then return false end
  -- Geotile content belongs to no anchor, so a same-session delivery may cross a pending rebase. A restarted
  -- server's numeric anchor IDs can repeat; its session identity must match before it can acquire any owner.
  if sessionId == nil or data.sid ~= sessionId then return false end
  local frame = geotile.frameOf({ key = msg.key, frame = msg.frame or data.frame })
  if not frame then return false end
  local tile = { frame = frame, pivot = tonumber(msg.pivot) or tonumber(data.pivot) or 0, rows = data.turbines }
  tiles[msg.key] = tile
  place(msg.key, tile, anchor)
  return true
end

function M.onDrop(msg)
  if type(msg) ~= 'table' or not tiles[msg.key] then return end
  renderer.dropKey(owner(msg.key))
  tiles[msg.key] = nil
end

-- Called after the renderer's common transform and before near road rows: near owners retain placement priority.
function M.reanchor(anchor)
  for key, tile in pairs(tiles) do place(key, tile, anchor) end
end

function M.reset()
  for key in pairs(tiles) do renderer.dropKey(owner(key)) end
  tiles = {}
end

function M.status()
  local n = 0
  for _ in pairs(tiles) do n = n + 1 end
  return { tiles = n }
end

return M
