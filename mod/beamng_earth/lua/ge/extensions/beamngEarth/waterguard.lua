-- Finite water columns above covered tunnel vaults. Pure geometry; no engine or player state.
-- A conservative approximation: surface water stays, deep water over the bore stops above its roof.
local M = { MIN_SIZE = 16, MAX_BLOCKS = 256, CLEARANCE = 1 }

-- Segment interval inside an expanded axis-aligned box (Liang-Barsky).
local function interval(a, b, x0, y0, x1, y1)
  local lo, hi = 0, 1
  for axis = 1, 2 do
    local p, d = a[axis], b[axis] - a[axis]
    local low, high = axis == 1 and x0 or y0, axis == 1 and x1 or y1
    if math.abs(d) < 1e-12 then
      if p < low or p > high then return end
    else
      local u, v = (low - p) / d, (high - p) / d
      if u > v then u, v = v, u end
      lo, hi = math.max(lo, u), math.min(hi, v)
      if lo > hi then return end
    end
  end
  return lo, hi
end

function M.segments(records)
  local out = {}
  for _, r in pairs(records) do
    for i = 2, #r.nodes do
      local a, b = r.nodes[i - 1], r.nodes[i]
      out[#out + 1] = { a = { a[1], a[2], a[3] + r.h }, b = { b[1], b[2], b[3] + r.h }, r = r.r }
    end
  end
  return out
end

function M.cap(t, segments, depth)
  local nx, ny = t.nx or 0, t.ny or 0
  local nz = math.sqrt(math.max(0, 1 - nx * nx - ny * ny))
  -- Rotation moves a deep block's bottom sideways; include it as well as the surface overlap.
  local half = (t.size / 2 + .5) * (1 + math.abs(nx * ny) / (1 + nz))
    + depth * math.sqrt(nx * nx + ny * ny)
  local result = depth
  for _, s in ipairs(segments) do
    local r = s.r
    local lo, hi = interval(s.a, s.b, t.x - half - r, t.y - half - r, t.x + half + r, t.y + half + r)
    if lo then
      local function roof(u)
        return nx * (s.a[1] + u * (s.b[1] - s.a[1]) - t.x)
          + ny * (s.a[2] + u * (s.b[2] - s.a[2]) - t.y)
          + nz * (s.a[3] + u * (s.b[3] - s.a[3]) - t.z)
      end
      result = math.min(result, -math.max(roof(lo), roof(hi)) - r * (math.abs(nx) + math.abs(ny)) - M.CLEARANCE)
    end
  end
  return math.max(0, result)
end

local function child(t, dx, dy)
  local size = t.size / 2
  local x, y = t.x + dx * size / 2, t.y + dy * size / 2
  local nx, ny = t.nx or 0, t.ny or 0
  local nz = math.sqrt(math.max(1e-12, 1 - nx * nx - ny * ny))
  return { x = x, y = y, z = t.z - (nx * (x - t.x) + ny * (y - t.y)) / nz,
    size = size, nx = nx, ny = ny }
end

function M.blocks(tiles, segments, depth)
  if #segments == 0 then return tiles end
  local out, count = {}, 0
  for key, t in pairs(tiles) do
    out[key] = { x = t.x, y = t.y, z = t.z, size = t.size, nx = t.nx, ny = t.ny,
      depth = M.cap(t, segments, depth) }
    count = count + 1
  end
  -- Largest affected cell first, zero-depth cells first. A bounded object count; when full, the coarser
  -- parent is still capped safely, at the cost of shallower water over a wider patch of seabed.
  while count + 3 <= math.max(count, M.MAX_BLOCKS) do
    local best, priority
    for key, t in pairs(out) do
      if t.depth < depth - .001 and t.size > M.MIN_SIZE then
        local p = t.size * (t.depth <= .01 and 1000000 or 1)
        if not priority or p > priority or (p == priority and key < best) then best, priority = key, p end
      end
    end
    if not best then break end
    local t = out[best]
    out[best] = nil
    for i = 0, 3 do
      local c = child(t, i % 2 == 0 and -1 or 1, i < 2 and -1 or 1)
      c.depth = M.cap(c, segments, depth)
      out[best .. 'q' .. i] = c
    end
    count = count + 3
  end
  return out
end

-- A spline can overshoot node depths. Affected source pieces use two-node spans with constant depth;
-- both linear and endpoint-clamped Catmull-Rom stay in the same straight corridor. Long affected spans: <=16 m.
function M.rivers(rivers, segments)
  if #segments == 0 then return rivers end
  local out = {}
  for key, r in pairs(rivers) do
    local nodes, touched = {}, false
    for i = 2, #r.nodes do
      local a, b = r.nodes[i - 1], r.nodes[i]
      local len = math.sqrt((b[1] - a[1]) ^ 2 + (b[2] - a[2]) ^ 2)
      local box = { x = (a[1] + b[1]) / 2, y = (a[2] + b[2]) / 2, z = math.min(a[3], b[3]),
        size = len + math.max(a[4], b[4]) }
      local hit = M.cap(box, segments, math.max(a[5], b[5])) < math.max(a[5], b[5])
      touched = touched or hit
      local n = hit and math.max(1, math.ceil(len / M.MIN_SIZE)) or 1
      if i == 2 then nodes[1] = { unpack(a) } end
      for j = 1, n do
        local p = {}; for axis = 1, 5 do p[axis] = a[axis] + (b[axis] - a[axis]) * j / n end
        nodes[#nodes + 1] = p
      end
    end
    if not touched then out[key] = r else
      for i = 2, #nodes do
        local a, b = nodes[i - 1], nodes[i]
        local box = { x = (a[1] + b[1]) / 2, y = (a[2] + b[2]) / 2, z = math.min(a[3], b[3]),
          size = math.max(math.abs(b[1] - a[1]), math.abs(b[2] - a[2])) + math.max(a[4], b[4]) }
        local cap = math.min(a[5], b[5], M.cap(box, segments, math.max(a[5], b[5])))
        if cap > .01 then
          local p, q = { unpack(a) }, { unpack(b) }
          p[5], q[5] = cap, cap
          out[key .. 'g' .. i] = { kind = r.kind, flow = r.flow, nodes = { p, q } }
        end
      end
    end
  end
  return out
end

return M
