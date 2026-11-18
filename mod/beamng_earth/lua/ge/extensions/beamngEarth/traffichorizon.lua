-- Read-only snapshots of driving traffic for the terrain/road lookahead. Never treat occupied seats,
-- parked cars, pooled vehicles or remote humans as AI. Sampling is shared by follow and navgraph.
local M = {}
local localplayers = require('beamngEarth/localplayers')
M.SAMPLE_S = 0.25
local last, source, vehicles, controlled = -math.huge, nil, {}, {}

function M.reset()
  last, source, vehicles = -math.huge, nil, {}
end

function M.sample()
  local gt = rawget(_G, 'gameplay_traffic')
  if not (gt and gt.getState and gt.getState() == 'on' and gt.getTrafficData and getObjectByID) then
    M.reset()
    return vehicles
  end
  local now = os.clock()
  if source == gt and now >= last and now - last < M.SAMPLE_S then return vehicles end
  last, source, vehicles = now, gt, {}
  localplayers.snapshot(controlled)
  for id, t in pairs(gt.getTrafficData() or {}) do
    if type(t) == 'table' and t.isAi == true
      and (t.state == 'active' or t.state == 'fadeIn' or t.state == 'locked') then
      local o = getObjectByID(id)
      if o and not localplayers.controlled(id, o, controlled) and o:getActive() then
        local p, v = o:getPosition(), o:getVelocity()
        vehicles[#vehicles + 1] = { x = p.x, y = p.y, vx = v.x, vy = v.y, sampleS = M.SAMPLE_S }
      end
    end
  end
  return vehicles
end

return M
