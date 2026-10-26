-- Geographic vehicle checkpoints for server-owned multiplayer saves.
local M = {}

local function finite(v)
  return type(v) == 'number' and v == v and math.abs(v) < math.huge
end

function M.valid(p)
  return type(p) == 'table' and p.version == 1
    and finite(p.sequence) and p.sequence >= 1 and p.sequence % 1 == 0
    and finite(p.lat) and math.abs(p.lat) <= 90
    and finite(p.lon) and math.abs(p.lon) <= 180
    and finite(p.height) and finite(p.heading) and p.heading >= 0 and p.heading < 360
end

function M.capture(anchor, pos, dir)
  if not anchor or not pos or not dir then return nil end
  if not (finite(pos.x) and finite(pos.y) and finite(pos.z)
    and finite(dir.x) and finite(dir.y)) or dir.x * dir.x + dir.y * dir.y < 1e-6 then return nil end
  local lat, lon, height = anchor:enuToGeodetic(pos.x, pos.y, pos.z)
  local nx, ny = anchor:northAt(pos.x, pos.y)
  local heading = math.deg(math.atan2(dir.x, dir.y) - math.atan2(nx, ny)) % 360
  return { version = 1, sequence = 1, lat = lat, lon = lon, height = height, heading = heading }
end

function M.direction(anchor, x, y, heading)
  local nx, ny = anchor:northAt(x, y)
  local h = math.rad(heading)
  return nx * math.cos(h) + ny * math.sin(h), ny * math.cos(h) - nx * math.sin(h)
end

return M
