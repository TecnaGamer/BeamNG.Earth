-- The vehicle IDs occupied by local players, not every car registered with gameplay_traffic. The game's
-- multiseat roster contains player indices (which need not be consecutive), and the engine owns their current seats.
local M = {}
local players, knownSlots = {}, {}

-- A switch hook can arrive before the input roster includes the player. Keep that slot, not its vehicle ID, and
-- re-read its current seat on every scan; an exited slot is forgotten as soon as the engine returns -1.
function M.observe(player)
  if type(player) == 'number' and player > 0 then knownSlots[player] = true end
end

-- Fill reusable lookups once per scan. `others`, when supplied, excludes player 0 (for an intentional far-car switch).
function M.snapshot(ids, others)
  for id in pairs(ids) do ids[id] = nil end
  if others then for id in pairs(others) do others[id] = nil end end
  local engine = rawget(_G, 'be')
  if not (engine and engine.getPlayerVehicleID) then return ids end
  local id = engine:getPlayerVehicleID(0)
  if id and id >= 0 then ids[id] = true end
  local ms = rawget(_G, 'core_multiseat')
  if ms and ms.getPlayers then
    for _, player in ipairs(ms.getPlayers(players) or players) do M.observe(player) end
  end
  for player in pairs(knownSlots) do
    id = engine:getPlayerVehicleID(player)
    if id and id >= 0 then
      ids[id] = true
      if others then others[id] = true end
    else
      knownSlots[player] = nil
    end
  end
  return ids
end

-- The live object flag also covers a seat transition before the input roster catches up.
function M.controlled(id, vehicle, ids)
  return ids[id] or (vehicle and vehicle.isPlayerControlled and vehicle:isPlayerControlled()) or false
end

return M
