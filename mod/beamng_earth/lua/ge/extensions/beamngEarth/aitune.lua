-- Better driving traffic and police (the user, 2026-10-25: "regular cars better at driving (no smashing brakes and
-- making u turns) and police don't immediately try to crush your car (better chases)"). BeamNG's own AI (lua/vehicle/
-- ai.lua, gameplay/traffic) is tuned from outside, per car, as the game's own code sets it (ai.setParameters,
-- ai.setAggression, the traffic vehicle's setAiMode):
-- - traffic: a stuck car plans the reverse direction (a U-turn on a two-way road) once blocked longer than
--   trafficWaitTime x 2 (stock 2 -> 4 s; ai.lua trafficPlan); our streamed roads put oncoming cars in a car's plan more
--   often than a hand-made map, so it waits longer (TRAFFIC.trafficWaitTime). Its target speed changes more gently
--   (targetSpeedSmootherRate: less brake stabbing) and it looks further ahead (lookAheadKv: brakes earlier, softer);
-- - police: the stock pursuit drives at the suspect (AI 'chase', aggression at least 0.8) from level 2; here levels
--   below FOLLOW_UNTIL tail the suspect ('follow', all lanes, siren), and the chase at that level is less aggressive.
-- Checked from navgraph's traffic guard (every GUARD_S); a command goes only when what a car should do changes.
local M = {}

M.TRAFFIC = { trafficWaitTime = 3, targetSpeedSmootherRate = 6, lookAheadKv = 0.75 }   -- (3: a 3 s stop, a U-turn after 6 s)
M.FOLLOW_UNTIL = 3          -- pursuit levels (1..3) below this: tail; at it: chase
M.FOLLOW_AGGRESSION = 0.55
M.CHASE_AGGRESSION = 0.5    -- (stock: at least 0.8)
M.enabled = true

local tuned = {}            -- car id -> true once its traffic parameters are set
local police = {}           -- car id -> what was last set ('follow2', 'chase3', ...)

-- Pure: what a police car in role state `state` chasing at pursuit level `mode` should do: 'follow', 'chase' or nil
-- (not chasing: the stock role decides).
function M.policeWant(state, mode)
  mode = tonumber(mode) or 0
  if state ~= 'chase' or mode <= 0 then return nil end
  return mode < M.FOLLOW_UNTIL and 'follow' or 'chase'
end

-- Pure: a Lua table literal of flat key = number / string pairs (for queueLuaCommand).
function M.literal(t)
  local keys = {}
  for k in pairs(t) do keys[#keys + 1] = k end
  table.sort(keys)
  local parts = {}
  for _, k in ipairs(keys) do
    local v = t[k]
    parts[#parts + 1] = k .. ' = ' .. (type(v) == 'string' and string.format('%q', v) or tostring(v))
  end
  return '{' .. table.concat(parts, ', ') .. '}'
end

-- traffic: gameplay_traffic.getTrafficData(); objOf(id) -> the vehicle object. Returns the commands sent.
function M.tick(traffic, objOf)
  if not M.enabled then return 0 end
  local sent = 0
  local seen = {}
  for id, v in pairs(traffic or {}) do
    seen[id] = true
    local o = objOf(id)
    if o and v.isAi then
      if not tuned[id] then
        o:queueLuaCommand('ai.setParameters(' .. M.literal(M.TRAFFIC) .. ')')
        tuned[id] = true
        sent = sent + 1
      end
      local role = v.role
      if role and role.name == 'police' then
        local want = M.policeWant(role.state, role.targetPursuitMode)
        local key = want and (want .. tostring(role.targetPursuitMode)) or nil
        if want and police[id] ~= key then
          if want == 'follow' and v.setAiMode then
            v:setAiMode('follow', true)
            o:queueLuaCommand('ai.driveInLane("off")')
          end
          o:queueLuaCommand(string.format('ai.setAggression(%.3f)',
            want == 'follow' and M.FOLLOW_AGGRESSION or M.CHASE_AGGRESSION))
          police[id] = key
          sent = sent + 1
        elseif not want then
          police[id] = nil
        end
      end
    end
  end
  for id in pairs(tuned) do if not seen[id] then tuned[id] = nil end end
  for id in pairs(police) do if not seen[id] then police[id] = nil end end
  return sent
end

function M.reset() tuned, police = {}, {} end

return M
