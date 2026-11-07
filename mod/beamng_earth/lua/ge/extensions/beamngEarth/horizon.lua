-- BeamNG.Earth: the line-of-sight horizon (docs/V1_4_SPEC.md 3.6 "Radius", milestone M1h; the
-- server's --horizon, announced by hello.horizon). The server's far field then reaches the real
-- horizon (status.radius, 80 km to 1500 km), and core.lua uses this module to make the scene match:
--   * LevelInfo.visibleDistance follows radius * VISIBLE_MARGIN, never below the mod's 100 km and
--     with VISIBLE_HYSTERESIS; every change is followed by postApply(): in-game (2026-10-10,
--     docs/research/view_distance/) assigning the field alone does nothing, the engine keeps the
--     level file's value until postApply.
--   * ScatterSky.atmoPlanetRadius = PLANET_RADIUS_KM. The sky is a physical atmosphere around a
--     sphere of that radius with the camera at camPos.z above it (our z ~ altitude), so its horizon
--     already dips with height; the level has 6360 km, our tiles are curved with 6371 km (the server's
--     farfield.R_EARTH), so both horizons use one sphere. The level's value comes back when horizon
--     mode ends (a server without it) or before the level unloads.
--
-- TODO(M1h, distance haze; the lead tunes it in-game during P12): real visibility is haze-limited
-- (50-150 km on most days), so mountains at 150-300 km should fade to faint silhouettes instead of
-- ending at a hard edge. The knobs, all through core_environment (core/environment.lua) or the sky:
--   * core_environment.setFogDensity(d): LevelInfo.fogDensity + postApply (the level has 4e-5;
--     core_environment.getState() reports it x1000 as `fogDensity`);
--   * core_environment.setFogAtmosphereHeight(h): LevelInfo.FogAtmosphereHeight + postApply, how
--     high the fog reaches (matters at altitude: at 10 km the camera is above it);
--   * the sky's aerial perspective: ScatterSky atmoMieScattering / atmoMieExtinction
--     (setField(name, 0, 'r g b'), as celestial.lua applyAtmosphere does) and atmoMieScaleHeight.
-- Nothing here touches them yet.

local M = {}

M.PLANET_RADIUS_KM = 6371     -- the tiles' sphere (server farfield.R_EARTH / 1000)
M.VISIBLE_MARGIN = 1.1        -- visibleDistance = radius + 10 % (server farfield.VISIBLE_MARGIN)
M.VISIBLE_HYSTERESIS = 0.1    -- re-set only when the target moved by more than 10 % of the current value
-- The sky's atmosphere is ATMO_THICK_KM thick (the level's ScatterSky.atmoThickness); a camera at or above its top
-- drew every object black (the aerial perspective's transmittance with the eye outside the shell: 400 km over Nice, and
-- 100 km looking level, 2026-11-06). Above ATMO_LIFT_SHARE of it the shell is raised to ATMO_HEADROOM_KM over the
-- camera, in ATMO_STEP_KM steps: the air up there is next to nothing (8 km and 1.2 km scale heights), so the sky looks
-- the same, the eye just stays inside it. At most ATMO_MAX_KM: the engine's tables broke past ~600 km (the limb black,
-- the far ground white at 700 and 1000 km; 500 and 600 drew the same blue limb), so eyes up to ~500 km.
M.ATMO_THICK_KM = 100
M.ATMO_LIFT_SHARE = 0.6
M.ATMO_HEADROOM_KM = 100
M.ATMO_STEP_KM = 100
M.ATMO_MAX_KM = 600
M.atmoOverrideKm = nil        -- (the 3D map high up: the shell at its top once, globe.lua, not in 100 km steps)

-- Pure: the visibleDistance (m) to set for a far-field radius (m), given the current value and the
-- minimum; nil to keep the current one (within the hysteresis, or no usable radius).
function M.visibleTarget(radius, current, minimum)
  radius = tonumber(radius)
  if not radius or radius ~= radius or radius <= 0 or radius == math.huge then return nil end
  local want = math.max(tonumber(minimum) or 0, radius * M.VISIBLE_MARGIN)
  current = tonumber(current)
  if current and current > 0 and math.abs(want - current) <= M.VISIBLE_HYSTERESIS * current then return nil end
  return want
end

-- LevelInfo.visibleDistance = v, then postApply (without it the engine keeps the level's value).
function M.setVisibleDistance(v)
  local li = scenetree.theLevelInfo
  if not li then return false end
  li.visibleDistance = v
  if li.postApply then li:postApply() end
  return true
end

local saved = nil -- { id, radius, thickness }: the sky object and its atmoPlanetRadius / atmoThickness before applySky

-- The game's sky shader patched (the launcher's skypatch.py writes it, and this flag, into the installed mod): the
-- haze from above the atmosphere is right, so the shell stays as it is at any height (the 3D map from orbit)
M.skyPatched = (function()
  local ok, flag = pcall(require, 'beamngEarth/skypatch_flag')
  return ok and flag == true
end)()

-- Pure: the atmosphere's thickness (km) for a camera camKm km above sea level, the level's base (ATMO_*).
function M.thicknessFor(camKm, base)
  base = tonumber(base) or M.ATMO_THICK_KM
  camKm = tonumber(camKm)
  if M.skyPatched then return base end
  if not camKm or camKm <= M.ATMO_LIFT_SHARE * base then return base end
  local want = math.ceil((camKm + M.ATMO_HEADROOM_KM) / M.ATMO_STEP_KM) * M.ATMO_STEP_KM
  return math.max(base, math.min(M.ATMO_MAX_KM, want))
end

local function sky()
  local names = scenetree.findClassObjects('ScatterSky')
  return names and names[1] and scenetree.findObject(names[1]) or nil
end

-- The sky's planet radius = the tiles' (remembering the level's value once per sky object). Runs on
-- every status message in horizon mode, so the field is written only when it differs.
function M.applySky()
  local s = sky()
  if not s then return false end
  if not saved or saved.id ~= s:getID() then
    saved = { id = s:getID(), radius = tonumber(s.atmoPlanetRadius), thickness = tonumber(s.atmoThickness) }
  end
  if tonumber(s.atmoPlanetRadius) ~= M.PLANET_RADIUS_KM then s.atmoPlanetRadius = M.PLANET_RADIUS_KM end
  local cam = rawget(_G, 'core_camera')
  local p = cam and cam.getPosition and cam.getPosition()
  local want = M.atmoOverrideKm or M.thicknessFor(p and p.z / 1000, saved.thickness or M.ATMO_THICK_KM)
  if tonumber(s.atmoThickness) ~= want then s.atmoThickness = want end
  return true
end

-- Back to the level's planet radius (if applySky changed it and the sky still exists).
function M.restoreSky()
  if not saved then return false end
  local s = scenetree.findObjectById(saved.id)
  if s and saved.radius then s.atmoPlanetRadius = saved.radius end
  if s and saved.thickness then s.atmoThickness = saved.thickness end
  saved = nil
  return s ~= nil
end

function M.skyApplied() return saved ~= nil end

return M
