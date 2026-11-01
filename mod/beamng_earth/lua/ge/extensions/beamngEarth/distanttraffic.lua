-- Distant traffic's visual paths (GitHub #565): distance along a polyline, a local-hour density and smooth fades.
-- Pure Lua: no vehicles, map changes or scene objects. The renderer and the source of the paths are separate.
local M = {}
M.PROTO = 2

M.END_FADE_M = 30
M.LIGHT_HEIGHT_M = 0.65
M.DENSITY = { { 0, 0.4 }, { 4, 0.12 }, { 8, 0.7 }, { 12, 0.65 }, { 17, 1 }, { 21, 0.85 }, { 24, 0.4 } }

local sqrt, min, max = math.sqrt, math.min, math.max

function M.smooth(x)
  x = max(0, min(1, x))
  return x * x * (3 - 2 * x)
end

-- The density repeats at midnight; it describes an impression of traffic, not live traffic data.
function M.density(hour)
  hour = hour % 24
  for i = 2, #M.DENSITY do
    local a, b = M.DENSITY[i - 1], M.DENSITY[i]
    if hour <= b[1] then return a[2] + (b[2] - a[2]) * M.smooth((hour - a[1]) / (b[1] - a[1])) end
  end
end

-- Stable phases and population ranks, independent of tile/graph table iteration order.
function M.rank(id)
  local h = 1
  for i = 1, #id do h = (h * 131 + id:byte(i)) % 2147483647 end
  for _ = 1, 2 do h = (h * 48271) % 2147483647 end
  return h / 2147483647
end

local function finite(v) return type(v) == 'number' and v == v and math.abs(v) < math.huge end

-- Prepare a path once: copied XYZ points with cumulative horizontal lengths (m). Duplicate points are omitted;
-- a vertical-only segment is not a road. Path speed is m/s, offset is metres right of its direction of travel.
function M.prepare(id, points, speed, offset)
  if type(id) ~= 'string' or type(points) ~= 'table' or not finite(speed) or speed <= 0
      or (offset ~= nil and not finite(offset)) then return nil end
  local p, length = {}, 0
  for _, v in ipairs(points) do
    if type(v) ~= 'table' or not finite(v[1]) or not finite(v[2]) or not finite(v[3]) then return nil end
    local prev = p[#p]
    local d = prev and sqrt((v[1] - prev[1]) ^ 2 + (v[2] - prev[2]) ^ 2) or 0
    if not prev or d > 0.01 then
      length = length + d
      p[#p + 1] = { v[1], v[2], v[3], length }
    end
  end
  if #p < 2 then return nil end
  if offset and offset ~= 0 then
    local shifted = {}
    for i, v in ipairs(p) do
      local a, b = p[max(1, i - 1)], p[min(#p, i + 1)]
      local ax, ay = v[1] - a[1], v[2] - a[2]
      local bx, by = b[1] - v[1], b[2] - v[2]
      local al, bl = sqrt(ax * ax + ay * ay), sqrt(bx * bx + by * by)
      if al < 0.01 then ax, ay, al = bx, by, bl end
      if bl < 0.01 then bx, by, bl = ax, ay, al end
      ax, ay, bx, by = ax / al, ay / al, bx / bl, by / bl
      local den = max(0.25, 1 + ax * bx + ay * by)
      local nx, ny = (ay + by) / den, -(ax + bx) / den
      local scale = max(1, sqrt(nx * nx + ny * ny) / 2)
      shifted[i] = { v[1] + nx * offset / scale, v[2] + ny * offset / scale, v[3], 0 }
    end
    p, length = { shifted[1] }, 0
    for i = 2, #shifted do
      local a, b = p[#p], shifted[i]
      local d = sqrt((b[1] - a[1]) ^ 2 + (b[2] - a[2]) ^ 2)
      if d > 0.01 then
        length = length + d
        b[4] = length
        p[#p + 1] = b
      end
    end
    if #p < 2 then return nil end
  end
  return { id = id, points = p, length = length, speed = speed, offset = offset or 0 }
end

-- Sample without allocating (out is reused by the caller). Wrapping happens only while the ends are invisible.
-- Offset paths meet at shared corners, so a change of segment does not jump between two lane offsets.
function M.sample(path, metres, out)
  out = out or {}
  local p = path.points
  local s = metres % path.length
  local lo, hi = 2, #p
  while lo < hi do
    local mid = math.floor((lo + hi) / 2)
    if p[mid][4] < s then lo = mid + 1 else hi = mid end
  end
  local a, b = p[lo - 1], p[lo]
  local len = b[4] - a[4]
  local t = (s - a[4]) / len
  local dx, dy = (b[1] - a[1]) / len, (b[2] - a[2]) / len
  out.x, out.y = a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t
  out.z = a[3] + (b[3] - a[3]) * t + M.LIGHT_HEIGHT_M
  out.dx, out.dy = dx, dy
  out.alpha = M.smooth(min(s, path.length - s) / min(M.END_FADE_M, path.length / 2))
  return out
end

-- Distance outside the physical traffic's ground box. Expanding a graph for cosmetic cars must not enlarge physics.
function M.outside(box, x, y)
  local dx, dy = max(box[1] - x, 0, x - box[3]), max(box[2] - y, 0, y - box[4])
  return sqrt(dx * dx + dy * dy)
end

function M.distanceFade(distance, near, fadeIn, far, fadeOut)
  return M.smooth((distance - near) / fadeIn) * M.smooth((far - distance) / fadeOut)
end

-- Facing the camera: white; away: red. The soft side-on interval avoids a sudden colour switch on a bend.
function M.facing(dx, dy, vx, vy)
  local d = sqrt(vx * vx + vy * vy)
  if d < 0.01 then return 0, 0 end
  local dot = (dx * vx + dy * vy) / d
  return M.smooth((dot + 0.2) / 0.7), M.smooth((-dot + 0.2) / 0.7)
end

-- Normalized distance from the view centre to its rectangular edge (1 at an edge). Behind the camera is outside.
function M.viewEdge(x, y, z, view)
  local dx, dy, dz = x - view.cam.x, y - view.cam.y, z - view.cam.z
  local f, r, u = view.forward, view.right, view.up
  local depth = dx * f.x + dy * f.y + dz * f.z
  if depth <= 0 then return math.huge end
  return max(math.abs(dx * r.x + dy * r.y + dz * r.z) / (depth * view.tanH),
    math.abs(dx * u.x + dy * u.y + dz * u.z) / (depth * view.tanV))
end

-- A stream's peak population scales with its length. Short quiet roads may have no car rather than receiving a
-- compulsory one each; phases are evenly spaced with a little stable jitter, and do not jump when the clock changes.
function M.slots(path)
  local expected = path.length / (path.spacing or 180)
  local n = math.floor(expected)
  if M.rank(path.id .. ':count') < expected - n then n = n + 1 end
  return min(128, n)
end

function M.phase(path, i, n)
  return (i - 0.5 + (M.rank(path.id .. ':' .. i .. ':phase') - 0.5) * 0.4) * path.length / n
end

-- Preserve a small angular footprint beyond the near probe's range, where texture minification erased the dots.
function M.glowRadius(distance)
  return max(0.35, distance > 3500 and min(16, 6 + (distance - 3500) * 0.0015) or min(6, distance * 0.004))
end

-- Share a bounded pool in proportion to visible demand, reserving some distant traffic where it exists.
function M.bandQuotas(demand, limit)
  local total = demand[1] + demand[2] + demand[3]
  local target, q = min(limit, total), { 0, 0, 0 }
  if total == 0 then return q end
  for i = 1, 3 do q[i] = min(demand[i], math.floor(target * demand[i] / total)) end
  local far = min(demand[3], math.floor(target * 0.2 + 0.5))
  while q[3] < far do
    local donor = q[1] >= q[2] and 1 or 2
    if q[donor] == 0 then break end
    q[donor], q[3] = q[donor] - 1, q[3] + 1
  end
  for _ = q[1] + q[2] + q[3] + 1, target do
    local best = 1
    for i = 2, 3 do if demand[i] - q[i] > demand[best] - q[best] then best = i end end
    q[best] = q[best] + 1
  end
  return q
end

M.MAX_ROUTES = 8192
M.MAX_ROUTE_POINTS = 196608
M.MAX_TILES = 384

-- Tile ownership is separate from motion. Neighbouring tiles can name a whole road part; only one copy runs,
-- and dropping either tile leaves it running while the other still owns it. Preparation is one route per step.
function M.newRoutes()
  local s = { tiles = {}, routes = {}, queue = {}, first = 1, count = 0, points = 0, pendingPoints = 0, tileCount = 0 }

  function s:drop(key)
    local tile = self.tiles[key]
    if not tile then return end
    for id in pairs(tile.ids) do
      local route = self.routes[id]
      if route then
        route.owners[key] = nil
        if not next(route.owners) then
          self.routes[id] = nil
          self.count, self.points = self.count - 1, self.points - #route.points
        end
      end
    end
    for i = tile.at, #(tile.data or {}) do self.pendingPoints = self.pendingPoints - #tile.data[i].points end
    tile.data = nil
    tile.active = false
    self.tiles[key], self.tileCount = nil, self.tileCount - 1
  end

  function s:tile(data)
    if type(data) ~= 'table' or data.v ~= M.PROTO or type(data.key) ~= 'string' or #data.key > 64
        or type(data.paths) ~= 'table' or #data.paths > 128 or type(data.frame) ~= 'table'
        or not finite(data.frame[1]) or math.abs(data.frame[1]) > 85.1
        or not finite(data.frame[2]) or math.abs(data.frame[2]) > 180
        or not finite(data.pivot) then return false end
    if self.tiles[data.key] then return true end
    if self.tileCount >= M.MAX_TILES then return false, 'capacity' end
    local n, needed, ids = 0, 0, {}
    for _, p in ipairs(data.paths) do
      if type(p) ~= 'table' or type(p.id) ~= 'string' or #p.id > 160
          or type(p.points) ~= 'table' or #p.points > 512 then return false end
      n = n + #p.points
      if not ids[p.id] and not self.routes[p.id] then needed = needed + #p.points end
      ids[p.id] = true
    end
    if n > 8192 then return false end
    -- A successful tile owns every admitted path. Partial admission cannot be recovered after capacity frees.
    if self.points + self.pendingPoints + needed > M.MAX_ROUTE_POINTS then return false, 'capacity' end
    local tile = { key = data.key, frame = data.frame, pivot = data.pivot, ids = {}, active = true, data = {}, at = 1 }
    local queued = {}
    for _, p in ipairs(data.paths) do
      local route = type(p.id) == 'string' and self.routes[p.id]
      if route then
        route.owners[tile.key], tile.ids[p.id] = true, true
      elseif not queued[p.id] then
        tile.data[#tile.data + 1] = p
        queued[p.id] = true
        self.pendingPoints = self.pendingPoints + #p.points
      end
    end
    self.tiles[tile.key], self.tileCount = tile, self.tileCount + 1
    if #self.queue >= M.MAX_TILES then
      -- A route-cap blocked head may sleep for a long time while other tiles arrive and drop behind it.
      local pending = {}
      for i = self.first, #self.queue do
        local item = self.queue[i]
        if item.active and item.data and item.at <= #item.data then pending[#pending + 1] = item end
      end
      self.queue, self.first = pending, 1
    end
    self.queue[#self.queue + 1] = tile
    return true
  end

  function s:hasWork()
    local tile = self.queue[self.first]
    if not tile then return false end
    if not tile.active or tile.at > #tile.data then return true end
    local p = tile.data[tile.at]
    return type(p.id) ~= 'string' or #p.id > 160 or self.routes[p.id] ~= nil or self.count < M.MAX_ROUTES
  end

  function s:step()
    if not self:hasWork() then return false end
    local tile = self.queue[self.first]
    if not tile.active or tile.at > #tile.data then
      tile.data = nil
      self.first = self.first + 1
      if self.first > #self.queue then self.queue, self.first = {}, 1 end
      return true
    end
    local p = tile.data[tile.at]
    tile.data[tile.at] = false
    tile.at = tile.at + 1
    self.pendingPoints = self.pendingPoints - #p.points
    if type(p.id) ~= 'string' or #p.id > 160 then return true end
    local spacing, reach = p.spacing or 180, p.reach or 10000
    if not finite(spacing) or spacing <= 0 or not finite(reach) or reach < 1000 or reach > 10000 then return true end
    local route = self.routes[p.id]
    if not route and self.count < M.MAX_ROUTES and self.points + #p.points <= M.MAX_ROUTE_POINTS then
      route = M.prepare(p.id, p.points, p.speed, p.offset)
      if route then
        route.spacing, route.reach = spacing, reach
        route.roadClass, route.surface = p.roadClass, p.surface
        route.frame, route.pivot, route.owners = tile.frame, tile.pivot, {}
        self.routes[p.id] = route
        self.count, self.points = self.count + 1, self.points + #route.points
      end
    end
    if route then route.owners[tile.key], tile.ids[p.id] = true, true end
    return true, route
  end

  return s
end

return M
