-- BeamNG Earth-only lane-centre controller. This deliberately does not use BeamNG's traffic AI.
local M = {}

local route = {}
local speedTarget = 0
local progress = 1
local offRoute = false
local finished = false

M.OFF_ROUTE_M = 18
M.END_M = 10
M.WHEELBASE_M = 2.7
M.LOOKAHEAD_MIN_M = 7
M.LOOKAHEAD_MAX_M = 32

local function clamp(x, lo, hi) return math.max(lo, math.min(hi, x)) end

function M.routeState(points, px, py, previous)
  if type(points) ~= 'table' or #points < 2 then return nil, math.huge, math.huge end
  local best, bestD2, bestT = math.max(1, math.min(#points - 1, previous or 1)), math.huge, 0
  for i = math.max(1, (previous or 1) - 1), #points - 1 do
    local a, b = points[i], points[i + 1]
    local dx, dy = b.x - a.x, b.y - a.y
    local l2 = dx * dx + dy * dy
    if l2 > 1e-6 then
      local t = clamp(((px - a.x) * dx + (py - a.y) * dy) / l2, 0, 1)
      local ex, ey = px - (a.x + t * dx), py - (a.y + t * dy)
      local d2 = ex * ex + ey * ey
      if d2 < bestD2 then best, bestD2, bestT = i, d2, t end
    end
  end
  local remain = (1 - bestT) * math.sqrt((points[best + 1].x - points[best].x) ^ 2
    + (points[best + 1].y - points[best].y) ^ 2)
  for i = best + 1, #points - 1 do
    remain = remain + math.sqrt((points[i + 1].x - points[i].x) ^ 2 + (points[i + 1].y - points[i].y) ^ 2)
  end
  return best, math.sqrt(bestD2), remain
end

function M.lookaheadPoint(points, index, px, py, lookahead)
  local a, b = points[index], points[index + 1]
  local dx, dy = b.x - a.x, b.y - a.y
  local length = math.sqrt(dx * dx + dy * dy)
  if length < 1e-5 then return points[#points] end
  local t = clamp(((px - a.x) * dx + (py - a.y) * dy) / (length * length), 0, 1)
  local x, y = a.x + t * dx, a.y + t * dy
  local left = lookahead
  local remain = (1 - t) * length
  if left <= remain then
    local f = left / math.max(remain, 1e-5)
    return { x = x + f * (b.x - x), y = y + f * (b.y - y) }
  end
  left = left - remain
  for i = index + 1, #points - 1 do
    a, b = points[i], points[i + 1]
    dx, dy = b.x - a.x, b.y - a.y
    length = math.sqrt(dx * dx + dy * dy)
    if left <= length then
      local f = left / math.max(length, 1e-5)
      return { x = a.x + f * dx, y = a.y + f * dy }
    end
    left = left - length
  end
  return points[#points]
end

function M.steering(px, py, heading, target, wheelbase)
  local dx, dy = target.x - px, target.y - py
  local distance = math.sqrt(dx * dx + dy * dy)
  if distance < 0.1 then return 0 end
  local angle = math.atan2(dy, dx) - heading
  angle = (angle + math.pi) % (2 * math.pi) - math.pi
  local curvature = 2 * math.sin(angle) / math.max(distance, 1)
  return clamp(math.atan((wheelbase or M.WHEELBASE_M) * curvature) / 0.55, -0.7, 0.7)
end

function M.longitudinal(speed, target)
  local error = (tonumber(target) or 0) - math.max(0, tonumber(speed) or 0)
  if error >= 0 then return clamp(error * 0.12, 0, 0.65), 0 end
  return 0, clamp(-error * 0.12, 0, 0.8)
end

function M.setRoute(points, target)
  route = type(points) == 'table' and points or {}
  speedTarget = math.max(0, tonumber(target) or 0)
  progress, offRoute, finished = 1, false, #route < 2
  if ai and ai.setMode then ai.setMode('disabled') end
end

function M.stop()
  route, speedTarget = {}, 0
  progress, offRoute, finished = 1, false, true
  pcall(input.event, 'throttle', 0, 1)
  pcall(input.event, 'brake', 0, 1)
  pcall(input.event, 'steering', 0, 1)
  if ai and ai.setMode then ai.setMode('disabled') end
end

function M.state() return { offRoute = offRoute, finished = finished, progress = progress } end

function M.updateGFX(dt)
  if #route < 2 or finished or not obj then return end
  local p = obj:getPosition()
  local v = obj:getVelocity()
  local f = obj:getDirectionVector()
  local index, distance, remaining = M.routeState(route, p.x, p.y, progress)
  if not index then M.stop(); return end
  progress = math.max(progress, index)
  offRoute = distance > M.OFF_ROUTE_M
  finished = remaining <= M.END_M
  if offRoute then
    M.stop()
    offRoute, finished = true, false
    return
  end
  if finished then
    M.stop()
    finished = true
    return
  end
  local speed = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
  local lookahead = clamp(M.LOOKAHEAD_MIN_M + speed * 0.7, M.LOOKAHEAD_MIN_M, M.LOOKAHEAD_MAX_M)
  local target = M.lookaheadPoint(route, index, p.x, p.y, lookahead)
  local heading = math.atan2(f.y, f.x)
  local steer = M.steering(p.x, p.y, heading, target)
  local throttle, brake = M.longitudinal(speed, speedTarget)
  pcall(input.event, 'steering', steer, 1)
  pcall(input.event, 'throttle', throttle, 1)
  pcall(input.event, 'brake', brake, 1)
end

return M
