-- Winter (winter.lua, the server's BNE_WINTER): the stock AI's planning horizon on low grip. ai.lua plans
-- getMinPlanLen(20, speed, 0.2 * staticFrictionCoef) metres ahead in traffic mode, but the function floors the braking
-- it assumes at 0.2 g (`accelg = max(0.2, accelg or 0.2)`): on ice (the AI told 0.12 of grip) it planned v^2 / 0.4 g
-- ahead while it needs v^2 / 0.24 g to stop, and past its plan it assumes clear road: it found junctions and stopped
-- cars too late (the 2026-11-24 soak's crashes). Here that one function is replaced by the same one with a 0.05 floor,
-- in place: the closures that call it get the new one through their upvalue, the AI's state is untouched, and unpatch()
-- puts the stock function back. The game side (winter.lua aiPatchLine) has checked the stock file's text of the
-- function and passes the line it starts on; a function there with another line, or no debug library, is not
-- touched: patch() reports false and the game side caps the car's speed instead (speeding.lua).
local M = {}

local orig, patched, holders = nil, nil, {}

-- Every function reachable from the ai module's table through upvalues (depth-first, each once).
local function closures(root)
  local out, seen, stack = {}, {}, {}
  for _, v in pairs(root) do
    if type(v) == 'function' then stack[#stack + 1] = v end
  end
  while #stack > 0 do
    local f = table.remove(stack)
    if not seen[f] then
      seen[f] = true
      out[#out + 1] = f
      local i = 1
      while true do
        local name, val = debug.getupvalue(f, i)
        if not name then break end
        if type(val) == 'function' and not seen[val] then stack[#stack + 1] = val end
        i = i + 1
      end
    end
  end
  return out
end

local function upvalueIndex(f, want)
  local i = 1
  while true do
    local name = debug.getupvalue(f, i)
    if not name then return nil end
    if name == want then return i end
    i = i + 1
  end
end

-- Replace ai.lua's getMinPlanLen (defined at `line`) with the same function floored at `floor` g. True when done.
function M.patch(line, floor)
  if patched then return true end
  local ai = rawget(_G, 'ai')
  if type(ai) ~= 'table' or type(debug) ~= 'table' or not debug.getupvalue or not debug.setupvalue
      or not debug.upvaluejoin or not debug.getinfo then
    return false
  end
  local found = {}
  for _, f in ipairs(closures(ai)) do
    local i = upvalueIndex(f, 'getMinPlanLen')
    if i then
      local _, fn = debug.getupvalue(f, i)
      local info = type(fn) == 'function' and debug.getinfo(fn, 'S') or nil
      if not (info and info.linedefined == line and tostring(info.source):find('ai%.lua$')) then return false end
      orig = orig or fn
      if fn ~= orig then return false end
      found[#found + 1] = { f, i }
    end
  end
  if #found == 0 or not orig then return false end
  local iEgo, iG = upvalueIndex(orig, 'ego'), upvalueIndex(orig, 'g')
  if not iEgo or not iG then return false end
  local ego, g = nil, nil
  local max, min = math.max, math.min
  local fl = tonumber(floor) or 0.05
  local function getMinPlanLen(limLow, speed, accelg)
    limLow = limLow or 150
    speed = speed or ego.speed
    accelg = max(fl, accelg or 0.2)
    return min(550, max(limLow, 0.5 * speed * speed / (accelg * g)))
  end
  -- (ego and g shared with the stock function's own upvalues: the AI reassigning either is seen here)
  debug.upvaluejoin(getMinPlanLen, upvalueIndex(getMinPlanLen, 'ego'), orig, iEgo)
  debug.upvaluejoin(getMinPlanLen, upvalueIndex(getMinPlanLen, 'g'), orig, iG)
  for _, h in ipairs(found) do debug.setupvalue(h[1], h[2], getMinPlanLen) end
  patched, holders = getMinPlanLen, found
  return true
end

function M.unpatch()
  if not patched then return end
  for _, h in ipairs(holders) do debug.setupvalue(h[1], h[2], orig) end
  patched, holders = nil, {}
end

function M.isPatched() return patched ~= nil end

-- winter.lua's command: patch (or unpatch: line nil) and report to the game side.
function M.apply(line, floor)
  local ok = false
  if line then
    local okCall, res = pcall(M.patch, line, floor)
    ok = okCall and res == true
  else
    pcall(M.unpatch)
  end
  obj:queueGameEngineLua(string.format(
    "local w = package.loaded['beamngEarth/winter'] if w and w.aiPatchResult then w.aiPatchResult(%d, %s, %s) end",
    obj:getId(), tostring(ok), tostring(line ~= nil)))
end

return M
