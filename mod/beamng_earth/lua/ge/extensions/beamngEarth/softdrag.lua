-- Soft drag (the settings page's "Soft slow-down when the world is behind", off by default; the user, 2026-11-11: "add
-- it as a setting and ill test it myself later"). GTA III's trick (its streamer added a little drag when it fell
-- behind instead of stopping the player): when the next terrain block is late and the car nears the edge of the
-- collidable ground, the car loses a little speed, ramped in smoothly, so it reaches the edge later and the block has
-- time to come; the edge guard's pause (edgeguard.lua) stays the last resort.
--   on      only with a healthy server whose block is late: the car nearer the edge than an on-time block lets it get
--           (edgeguard's warnCap, follow.warnCap) and the edge within START_S at its speed towards it
--   amount  k(t) of the speed per second, t the time to the stop line: 0 at START_S, K_MAX at FULL_S and closer,
--           smoothstep between (no jolt); at 100 mph K_MAX is ~1.8 mph lost per second
--   applied the whole vehicle's velocity scaled (applyClusterVelocityScaleAdd(-1, scale, 0, 0, 0): every node, so the
--           car's own motion stays as it is), once a frame
-- step() is pure (luajit tests: mod/tests/test_softdrag.lua); update() does the engine call.
local M = {}

M.ON = false
M.START_S = 6.0         -- s to the stop line where the drag starts
M.FULL_S = 1.5          -- s to the stop line where it is full
M.K_MAX = 0.04          -- of the speed lost per second at full drag
M.MIN_SPEED = 15.0      -- m/s (34 mph): slower, never
M.STOP_M = 15           -- m inside the edge (edgeguard.STOP_M)
M.MAX_DT = 0.1          -- s: a longer frame applies this much at most

M.stats = { frames = 0, lost = 0 }      -- frames dragged, m/s taken in all (the status / tests)

local min, max, sqrt = math.min, math.max, math.sqrt

function M.setEnabled(on) M.ON = on == true end

-- Pure: the scale to apply to the velocity this frame (1: none). inp = { x, y, vx, vy, box = { x0, y0, x1, y1 },
-- warnCap (m: how near the edge an on-time block lets the car get; nil: no healthy server: no drag), dt }.
function M.step(inp)
  local cap = inp.warnCap
  if not cap then return 1 end
  local x, y, vx, vy, b = inp.x, inp.y, inp.vx, inp.vy, inp.box
  local speed = sqrt(vx * vx + vy * vy)
  if speed < M.MIN_SPEED then return 1 end
  -- towards each edge it moves to: the distance to its stop line and the time to it
  local t = math.huge
  local d = min(x - b[1], b[3] - x, y - b[2], b[4] - y)
  if d >= cap then return 1 end         -- (as near as an on-time block lets it get: not late)
  local function edge(dist, s)
    if s > 0 then t = min(t, max(0, dist - M.STOP_M) / s) end
  end
  edge(b[3] - x, vx); edge(x - b[1], -vx); edge(b[4] - y, vy); edge(y - b[2], -vy)
  if t >= M.START_S then return 1 end
  local u = min(1, max(0, (M.START_S - t) / (M.START_S - M.FULL_S)))
  local k = M.K_MAX * u * u * (3 - 2 * u)
  local dt = min(max(inp.dt or 0, 0), M.MAX_DT)
  return 1 - k * dt
end

local last = nil
-- One frame: veh the player's vehicle, box the collidable area, warnCap as step's, clock (s).
function M.update(veh, box, warnCap, clock)
  if not M.ON or not veh or not box then last = nil; return 1 end
  local dt = last and (clock - last) or 0
  last = clock
  local p, v = veh:getPosition(), veh:getVelocity()
  local scale = M.step({ x = p.x, y = p.y, vx = v.x, vy = v.y, box = box, warnCap = warnCap, dt = dt })
  if scale < 1 then
    veh:applyClusterVelocityScaleAdd(-1, scale, 0, 0, 0)
    M.stats.frames = M.stats.frames + 1
    M.stats.lost = M.stats.lost + sqrt(v.x * v.x + v.y * v.y) * (1 - scale)
  end
  return scale
end

return M
