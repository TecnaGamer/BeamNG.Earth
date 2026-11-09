-- BeamNG.Earth: the engine's shape detail in our level (the settings page's objectDetail; the user, 2026-10-28: "give
-- player much more control in the graphics settings (view distance, LOD quality, ETC.)"). The game's own Mesh Quality
-- setting sets $pref::TS::detailAdjust (core/settings/meshQuality.lua: Lowest 0.5, Low 0.75, Normal 1, High 1.5, Ultra
-- 2): the factor on every shape's LOD switch sizes (trees, buildings, props, vehicles; a higher value keeps the finer
-- detail level farther out, ~7000 x radius / distance at 1440p with 2: mesh-render notes). 'game' (the default) never
-- touches it; another level sets it while our level runs, the player's value back when leaving it or going back to
-- 'game'. (TSStatic forceDetail is a no-op in 0.39: this is the engine's only knob.) Checked every CHECK_S: the game's
-- graphics settings put theirs back when the player applies them.
local M = {}

M.PREF = '$pref::TS::detailAdjust'
M.LEVELS = { low = 0.75, normal = 1.0, high = 1.5, ultra = 2.0, extreme = 3.0 }
M.CHECK_S = 2
M.level = 'game'

local saved = nil           -- the player's value before ours (nil: ours not set)
local lastCheck = -math.huge

local function reg() return rawget(_G, 'VariableRegistry') end

-- Pure: the value to set for a level, or nil ('game', unknown).
function M.valueFor(level) return M.LEVELS[level] end

function M.restore()
  local vr = reg()
  if saved ~= nil and vr then pcall(vr.set, M.PREF, saved) end
  saved = nil
end

-- Our value in our level (every CHECK_S, and at once after setLevel); the player's elsewhere.
function M.update(inOurLevel, force)
  local t = os.clock()
  if not force and t - lastCheck < M.CHECK_S then return end
  lastCheck = t
  local want = M.valueFor(M.level)
  if not inOurLevel or want == nil then M.restore() return end
  local vr = reg()
  if not vr then return end
  local ok, cur = pcall(vr.get, M.PREF)
  cur = ok and tonumber(cur) or nil
  if cur ~= nil and math.abs(cur - want) < 1e-6 then return end
  if cur ~= nil then saved = cur end      -- (not ours: the player's, or a value their graphics settings set since)
  pcall(vr.set, M.PREF, want)
end

function M.setLevel(level, inOurLevel)
  if level ~= 'game' and not M.LEVELS[level] then return false end
  M.level = level
  M.update(inOurLevel, true)
  return true
end

function M.status() return { level = M.level, value = M.valueFor(M.level), saved = saved } end

return M
