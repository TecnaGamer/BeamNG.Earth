-- Winter (winter.lua, the server's BNE_WINTER): the stock AI's planning horizon on low grip. ai.lua plans
-- getMinPlanLen(20, speed, 0.2 * staticFrictionCoef) metres ahead in traffic mode, but the function floors the braking
-- it assumes at 0.2 g (`accelg = max(0.2, accelg or 0.2)`): on ice (the AI told 0.12 of grip) it planned v^2 / 0.4 g
-- ahead while it needs v^2 / 0.24 g to stop, and past its plan it assumes clear road: it found junctions and stopped
-- cars too late (the 2026-11-24 soak's crashes).
--
-- The vehicle Lua's sandbox refuses debug.setupvalue / upvaluejoin (tried 2026-11-24: "function not allowed by
-- sandbox"), so the function cannot be swapped in place. Instead the stock file's text is loaded again with that one
-- line changed (readFile + load, as the mod rewrites the left signal's shape) and becomes the vehicle's `ai` module;
-- the game side then has the traffic system set the car's AI mode, aggression and speed mode again (gameplay_traffic
-- vehicle:setAiMode, speeding.lua's caps and margins), since a fresh module starts without them. unswap() puts the
-- stock module back the same way. The game side (winter.lua aiPatchLine) has checked the stock file's text of the
-- whole function and passes the line it starts on; here that line and the floor's line are checked once more. Any
-- failure: apply() reports false and the game side caps the car's speed instead.
local M = {}

M.FN_LINE = 'local function getMinPlanLen(limLow, speed, accelg)'
M.FLOOR_LINE = '  accelg = max(0.2, accelg or 0.2)'
M.FLOOR_OFFSET = 4          -- the floor's line after the function's first line
M.PATH = 'lua/vehicle/ai.lua'

local stockAi, patchedAi = nil, nil

-- Pure: the ai.lua text with getMinPlanLen's floor at `floor` g, the function starting at `line`; nil when the lines
-- there are not the stock ones.
function M.patchedSource(text, line, floor)
  if type(text) ~= 'string' or type(line) ~= 'number' then return nil end
  local lines, i = {}, 0
  for l in (text:gsub('\r\n', '\n') .. '\n'):gmatch('(.-)\n') do
    i = i + 1
    lines[i] = l
  end
  if lines[line] ~= M.FN_LINE or lines[line + M.FLOOR_OFFSET] ~= M.FLOOR_LINE then return nil end
  lines[line + M.FLOOR_OFFSET] = string.format('  accelg = max(%.3f, accelg or 0.2)', tonumber(floor) or 0.05)
  return table.concat(lines, '\n')
end

-- Make the patched module the vehicle's `ai`. True when done.
function M.swap(line, floor)
  if patchedAi and rawget(_G, 'ai') == patchedAi then return true end
  local cur = rawget(_G, 'ai')
  if type(cur) ~= 'table' or type(readFile) ~= 'function' or type(load) ~= 'function' then return false end
  local src = M.patchedSource(readFile(M.PATH), line, floor)
  if not src then return false end
  local chunk = load(src, '@' .. M.PATH)
  if not chunk then return false end
  local mod = chunk()
  if type(mod) ~= 'table' or type(mod.updateGFX) ~= 'function' then return false end
  stockAi = stockAi or cur
  patchedAi = mod
  _G.ai = mod
  if package and package.loaded then package.loaded.ai = mod end
  return true
end

function M.unswap()
  if stockAi and rawget(_G, 'ai') == patchedAi then
    _G.ai = stockAi
    if package and package.loaded then package.loaded.ai = stockAi end
  end
  patchedAi = nil
end

function M.isSwapped() return patchedAi ~= nil and rawget(_G, 'ai') == patchedAi end

-- winter.lua's command: swap in (line given) or back (nil), and report to the game side, which sets the car's AI up again.
function M.apply(line, floor)
  local ok = false
  if line then
    local okCall, res = pcall(M.swap, line, floor)
    ok = okCall and res == true
  else
    pcall(M.unswap)
  end
  obj:queueGameEngineLua(string.format(
    "local w = package.loaded['beamngEarth/winter'] if w and w.aiPatchResult then w.aiPatchResult(%d, %s, %s) end",
    obj:getId(), tostring(ok), tostring(line ~= nil)))
end

return M
