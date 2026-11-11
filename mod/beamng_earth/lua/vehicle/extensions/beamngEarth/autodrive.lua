-- Route handoffs through the stock AI API. Keep this state in the vehicle VM: GE's graph acknowledgement can
-- arrive after another rebuild, whereas mapmgr.mapData identifies the graph the AI actually has.
local M = {}
local lastPath, lastMap, lastVersion, lastSpeed, lastAggression

function M.reset()
  lastPath, lastMap, lastVersion, lastSpeed, lastAggression = nil, nil, nil, nil, nil
end
M.onReset = M.reset

-- A remaining suffix of the old path followed by new nodes. setPath appends ONLY when its first node is the
-- old path's last node. Identical remaining paths need no command; forks, loops and shorter endpoints restart.
function M.extension(old, new)
  if not old or #old == 0 or #new == 0 then return nil end
  local start, seen = nil, {}
  for i, id in ipairs(old) do
    if seen[id] then return nil end
    seen[id] = true
    if id == new[1] then start = i end
  end
  if not start then return nil end
  local oldNodes = seen
  seen = {}
  for _, id in ipairs(new) do
    if seen[id] then return nil end
    seen[id] = true
  end
  local overlap = #old - start + 1
  if #new < overlap then return nil end
  for i = 1, overlap do if new[i] ~= old[start + i - 1] then return nil end end
  for i = overlap + 1, #new do if oldNodes[new[i]] then return nil end end
  local suffix = {}
  for i = overlap, #new do suffix[#suffix + 1] = new[i] end
  return suffix
end

local function valid(ids, md)
  -- One forward waypoint is sufficient: the stock waypoint API builds the approach from the car's position.
  if not md or not md.graph or #ids == 0 then return false end
  for i, id in ipairs(ids) do
    if not md.graph[id] then return false end
    if i > 1 then
      local from = ids[i - 1]
      local e = md.graph[from][id]
      if not e or (e.oneWay and e.inNode ~= nil and e.inNode ~= from) then return false end
    end
  end
  return true
end

function M.stop(mode)
  M.reset()
  ai.setMode(mode or 'stop')
end

function M.drive(ids, version, force, speed, aggression)
  local md = mapmgr.mapData
  if not valid(ids, md) then
    M.stop()
    mapmgr.requestMap()
    return 'waiting'
  end
  local suffix = not force and ai.mode == 'manual' and md == lastMap and version == lastVersion
    and speed == lastSpeed and aggression == lastAggression and M.extension(lastPath, ids)
  local action
  if suffix then
    if #suffix > 1 then ai.setPath(suffix); action = 'extend' else action = 'keep' end
  else
    -- In BeamNG 0.40, explicit path input leaves ego.currentSegment empty. The first plan then aims at the
    -- road centre instead of the occupied lane. Waypoint input runs getPointNodePath and seeds that segment;
    -- every adjacent node remains a waypoint so the local route still follows the GPS's chosen turns.
    ai.driveUsingPath({ wpTargetList = ids, driveInLane = 'on', avoidCars = 'on', aggression = aggression,
      routeSpeedMode = speed and 'set' or 'legal', routeSpeed = speed })
    action = 'start'
  end
  -- The engine may extend its own path table in place; retain an independent copy of the requested suffix.
  lastPath = {}
  for i, id in ipairs(ids) do lastPath[i] = id end
  lastMap, lastVersion, lastSpeed, lastAggression = md, version, speed, aggression
  return action
end

return M
