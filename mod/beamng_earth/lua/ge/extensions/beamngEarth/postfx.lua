-- Post effects in our level. The stock depth of field (Options > Graphics > Post FX, on by default) blurs
-- everything past a few hundred metres; on stock maps their haze (5-25 km view distances, dense fog) hides it,
-- in ours (100+ km, thin haze) the whole distance looked soft: the user's "terrain from half distance outward
-- looks low quality" at 4K (2026-10-14: the same view with the far blur off is crisp). In our level the far
-- blur is 0, the near blur and every other DOF setting the player's own; the player's settings are never
-- written, and leaving the level (or M.restore) puts the stock parameters back (dof.lua updateDOFSettings).
local M = {}

M.FAR_BLUR = 0          -- the far blur in our level (the stock preset: 0.25)
local applied = false

local function dofObject()
  return scenetree and scenetree.findObject and scenetree.findObject('DOFPostEffect') or nil
end

-- Pure: the focus parameters (dof.lua setFocusParams' order) from the player's DOF variables `v` (a function
-- name -> value), with the far blur replaced.
function M.focusParams(v, farBlur)
  return tonumber(v('$DOFPostFx::BlurMin')) or 0, farBlur, tonumber(v('$DOFPostFx::FocusRangeMin')) or 0,
    tonumber(v('$DOFPostFx::FocusRangeMax')) or 0, -(tonumber(v('$DOFPostFx::BlurCurveNear')) or 0),
    tonumber(v('$DOFPostFx::BlurCurveFar')) or 0
end

-- In our level: the far blur off (call again after the player changes post-effect settings).
function M.apply()
  local dof = dofObject()
  if not dof or not VariableRegistry then return false end
  local get = function(k) return VariableRegistry.get(k) end
  local a, b, c, d, e, f = M.focusParams(get, M.FAR_BLUR)
  -- dof.lua's setFocusParams, on the object's fields (its module keeps it local)
  dof.nearBlurMax, dof.farBlurMax, dof.minRange, dof.maxRange, dof.nearSlope, dof.farSlope = a, b, c, d, e, f
  applied = true
  return true
end

-- Every frame in our level: re-applied when something reset it (the player's post-effect preset runs dof.lua's
-- updateDOFSettings), checked every CHECK_S.
M.CHECK_S = 2
local lastCheck = -math.huge
function M.update(inOurLevel)
  if not inOurLevel then return end
  local t = os.clock()
  if t - lastCheck < M.CHECK_S then return end
  lastCheck = t
  local dof = dofObject()
  if dof and (not applied or dof.farBlurMax ~= M.FAR_BLUR) then M.apply() end
end

-- Leaving our level: the stock DOF parameters from the player's settings again.
function M.restore()
  if not applied then return end
  applied = false
  local ok, dofmod = pcall(require, 'client/postFx/dof')
  if ok and type(dofmod) == 'table' and dofmod.updateDOFSettings then pcall(dofmod.updateDOFSettings) end
end

return M
