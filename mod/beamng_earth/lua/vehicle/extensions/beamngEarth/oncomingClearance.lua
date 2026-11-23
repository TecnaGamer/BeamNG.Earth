-- On narrow roads, stock AI can use the shoulder to separate oncoming vehicles. Its
-- node-spaced clearance rays can miss a pole between nodes. Keep normal braking and
-- path planning, but reduce lateral avoidance while a moving oncoming car is close.
local M = {}
M.enabled = false
M.AVOIDANCE_SCALE = 0.25
M.CLEARANCE_M = 0.8
M.SAMPLE_S = 0.1
local boundMap, saved, applied, elapsed, holds = nil, nil, nil, 0, 0

local function finite(n)
  return type(n) == 'number' and n == n and n ~= math.huge and n ~= -math.huge
end

function M.restricted(road, ego, objects)
  if not road or road.oneWay or type(road.lanes) ~= 'string' or not finite(road.halfWidth)
    or road.halfWidth <= 0 or not ego or not finite(ego.width) or ego.width <= 0 then return false end
  local lanes = 0
  for _ in road.lanes:gmatch('[+-]') do lanes = lanes + 1 end
  if lanes < 1 or lanes > 2 or 2 * road.halfWidth / lanes - ego.width >= M.CLEARANCE_M then return false end
  local p, d = ego.pos, ego.dir
  if not p or not d or not finite(p.x) or not finite(p.y) or not finite(d.x) or not finite(d.y) then return false end
  local length = math.sqrt(d.x * d.x + d.y * d.y)
  if length < 0.1 then return false end
  local fx, fy = d.x / length, d.y / length
  for id, other in pairs(objects or {}) do
    local q, heading, velocity = other.pos, other.dirVec, other.vel
    if tonumber(id) ~= ego.id and q and heading and velocity and finite(q.x) and finite(q.y)
      and finite(q.z) and finite(p.z)
      and finite(velocity.x) and finite(velocity.y) and finite(heading.x) and finite(heading.y) then
      local dx, dy = q.x - p.x, q.y - p.y
      local height = q.z - p.z - dx * (road.slopeX or 0) - dy * (road.slopeY or 0)
      local ahead, side = dx * fx + dy * fy, dx * fy - dy * fx
      local closing = -(velocity.x * fx + velocity.y * fy)
      local reach = math.min(80, math.max(25, ((ego.speed or 0) + closing) * 2))
      if math.abs(height) < 5 and heading.x * fx + heading.y * fy < -0.7 and closing > 1
        and ahead > -12 and ahead < reach and math.abs(side) < 2 * road.halfWidth then return true end
    end
  end
  return false
end

local function restore()
  if saved ~= nil then
    if ai and ai.getParameters and ai.setParameters and ai.getParameters().awarenessForceCoef == applied then
      ai.setParameters({ awarenessForceCoef = saved })
    end
    saved, applied = nil, nil
  end
end

function M.setEnabled(on)
  M.enabled = on == true
  if not M.enabled then restore() end
  elapsed = M.SAMPLE_S
end

function M.onExtensionLoaded()
  if obj and obj.queueGameEngineLua then
    obj:queueGameEngineLua("local S=beamngEarth_settings or package.loaded['beamngEarth/settings'];if S and be then local v=be:getObjectByID("..obj:getId()..");if v then v:queueLuaCommand(\"if beamngEarth_oncomingClearance and beamngEarth_oncomingClearance.setEnabled then beamngEarth_oncomingClearance.setEnabled(\"..tostring(S.get('narrowRoadAvoidance')==true)..\") end\")end end")
  end
end

function M.bindMap()
  local md = mapmgr and mapmgr.mapData
  if md ~= boundMap then restore(); boundMap, elapsed = md, M.SAMPLE_S end
end

local function probe()
  local md = mapmgr.mapData
  local pos, dir = obj:getPosition(), obj:getDirectionVector()
  local a, b = mapmgr.findBestRoad(pos, dir)
  local edge = a and b and md.graph[a] and md.graph[a][b]
  local pa, pb = a and md.positions[a], b and md.positions[b]
  if not edge or not pa or not pb then return false end
  local dx, dy = pb.x - pa.x, pb.y - pa.y
  local lengthSq = dx * dx + dy * dy
  if lengthSq < 0.01 then return false end
  local t = math.max(0, math.min(1, ((pos.x - pa.x) * dx + (pos.y - pa.y) * dy) / lengthSq))
  local ra, rb = edge.inRadius, edge.outRadius
  if edge.inNode == b then ra, rb = rb, ra end
  ra, rb = ra or md.radius[a], rb or md.radius[b]
  if not finite(ra) or not finite(rb) then return false end
  local radius = ra + t * (rb - ra)
  if (pos.x - pa.x - t * dx)^2 + (pos.y - pa.y - t * dy)^2 > (radius + 0.5)^2 then return false end
  local velocity = obj:getVelocity()
  local dz = pb.z - pa.z
  return M.restricted({ halfWidth = radius, lanes = edge.lanes, oneWay = edge.oneWay,
      slopeX = dx * dz / lengthSq, slopeY = dy * dz / lengthSq },
    { id = obj:getId(), pos = pos, dir = dir, width = obj:getInitialWidth(), speed = velocity:length() },
    mapmgr.getObjects())
end

function M.updateGFX(dt)
  if not M.enabled then
    if saved ~= nil then restore() end
    return
  end
  if not ai or (ai.mode ~= 'traffic' and ai.mode ~= 'manual') or not boundMap
    or not mapmgr or mapmgr.mapData ~= boundMap or not ai.getParameters or not ai.setParameters then
    restore(); return
  end
  elapsed = elapsed + (dt or 0)
  if elapsed < M.SAMPLE_S then return end
  elapsed = 0
  local ok, restrict = pcall(probe)
  if not ok or not restrict then restore(); return end
  local current = ai.getParameters().awarenessForceCoef
  if not finite(current) then restore(); return end
  if saved == nil then holds = holds + 1 end
  if saved == nil or current ~= applied then saved = current end
  applied = saved * M.AVOIDANCE_SCALE
  if current ~= applied then ai.setParameters({ awarenessForceCoef = applied }) end
end

function M.status() return { enabled = M.enabled, holding = saved ~= nil, bound = boundMap ~= nil, holds = holds } end
function M.onReset() restore(); elapsed = M.SAMPLE_S end
function M.onExtensionUnloaded() restore(); boundMap = nil end

return M
