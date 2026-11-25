-- BeamNG.Earth: rain streaks around the camera (the weather plan 4.3 phase B, layer 2; weather phase B). The stock
-- Precipitation drops (weather.lua's bngEarthRain) give the near rain its depth; past ~15 m they thin out, and by day
-- a storm read as dry (phase A's broker shots: real rain shows as streaks against dark backgrounds and a veil far off).
-- Here: one TSStatic of three open cylinders around the camera (tools/make_weather_assets.py: bne_rain_shell.cdae,
-- 4.5 / 10 / 22 m, materials bngEarthRainShell0..2), a texture of thin streaks as their opacity, scrolled down by the
-- material itself (Material 1.5 animFlags 1); lit like any translucent mesh (the sun, the sky's ambient and the
-- strongest lights near it: headlights and lamps). The depth test hides the shells behind walls and the ground.
-- Per frame while it rains: one position and rotation write (the cylinders lean into the air's motion past the camera:
-- the wind minus the camera's own speed, so the streaks slant in a gale and stream at the car at speed); the strength
-- (instance opacity) only when it moved. Dry: hidden, nothing per frame (weather.lua calls update only while it rains
-- or the shells are shown).
local M = {}

-- the shell sets: 'full' three cylinders (4.5 / 10 / 22 m), 'near' one at 6 m (a third of the fill: the three cost ~4.3 ms a
-- frame on the Deck at 720p in the broker's perf run, wb6 2026-11-25)
M.SHAPES = { full = '/levels/beamng_earth/art/weather/bne_rain_shell.cdae',
  near = '/levels/beamng_earth/art/weather/bne_rain_shell1.cdae' }
M.SHELLS = 'near'
M.SHAPE = M.SHAPES.full     -- (the shape of the object made: M.SHAPES[M.SHELLS] at its making)
M.MATERIALS = '/levels/beamng_earth/art/weather/weather.materials.json'
M.NAME = 'bngEarthRainShells'
M.FALL_MS = 8.0          -- m/s: the drops' fall (the material's scroll: make_weather_assets.py FALL_MS)
M.TILT_MAX = math.rad(60)
M.GAIN = 0.2             -- the shells' opacity at full rain (times the rain share; 1.0 read as a white blizzard in the
                         -- first broker shots, wb1 2026-11-25: to be tuned with the streak texture)
M.ALPHA_STEP = 1 / 64    -- the instance opacity written when it moved this much
M.VEL_TAU_S = 0.3        -- the camera's speed smoothed over this long
M.JUMP_M = 50            -- a camera move this far in one frame: a cut (a teleport, a camera switch), no speed

local id, alpha, hidden = nil, 0, true
local last = nil          -- { x, y, z } the camera last frame
local vx, vy = 0, 0       -- its smoothed horizontal speed (m/s)
M.writes = 0

-- Pure: the shells' rotation (a quaternion x, y, z, w) for the air's motion past the camera (rx, ry m/s, horizontal):
-- the cylinders' axis along a drop's path seen from the camera, (rx, ry, -fall), its top upwind; at most TILT_MAX.
function M.tiltQuat(rx, ry, fall)
  fall = fall or M.FALL_MS
  local h = math.sqrt(rx * rx + ry * ry)
  if h < 1e-3 then return 0, 0, 0, 1 end
  local th = math.min(M.TILT_MAX, math.atan2(h, fall))
  -- the top goes upwind (-rx, -ry): rotate z towards it about the horizontal axis z x (-r) = (ry, -rx, 0) / h
  local ax, ay = ry / h, -rx / h
  local s = math.sin(th / 2)
  return ax * s, ay * s, 0, math.cos(th / 2)
end

local function object()
  local o = id and scenetree.findObjectById(id)
  if o and o:getField('shapeName', 0) ~= (M.SHAPES[M.SHELLS] or M.SHAPES.near) then o:delete(); o = nil end   -- (the set changed)
  if o then return o end
  id = nil
  if not scenetree.MissionGroup then return nil end
  if not scenetree.findObject('bngEarthRainShell0') and loadJsonMaterialsFile then pcall(loadJsonMaterialsFile, M.MATERIALS) end
  o = createObject('TSStatic')
  if not o then return nil end
  M.SHAPE = M.SHAPES[M.SHELLS] or M.SHAPES.near
  o:setField('shapeName', 0, M.SHAPE)
  o:setField('collisionType', 0, 'None')
  o:setField('decalType', 0, 'None')
  o:setField('castShadows', 0, 'false')
  o.useInstanceRenderData, o.canSave = true, false
  o:setField('instanceColor', 0, '1 1 1 0')
  o:setHidden(true)
  o:registerObject(M.NAME)
  scenetree.MissionGroup:addObject(o)
  id, alpha, hidden = o:getID(), 0, true
  return o
end

-- Every frame from weather.lua while it rains or the shells are shown: share = the rain's share (0..1, after the
-- shelter fade and every block), wind = { x, y } m/s (the ground wind), dt. Returns true while shown.
function M.update(share, wind, dt)
  local want = math.max(0, math.min(1, (tonumber(share) or 0) * M.GAIN))
  if want < M.ALPHA_STEP and hidden then return false end
  local cam = rawget(_G, 'core_camera')
  local p = cam and cam.getPosition and cam.getPosition()
  local o = p and object()
  if not o then return false end
  dt = math.max(1e-3, tonumber(dt) or 0.016)
  if last then
    local mx, my, mz = p.x - last.x, p.y - last.y, p.z - last.z
    if mx * mx + my * my + mz * mz > M.JUMP_M * M.JUMP_M then
      vx, vy = 0, 0
    else
      local a = math.min(1, dt / M.VEL_TAU_S)
      vx, vy = vx + (mx / dt - vx) * a, vy + (my / dt - vy) * a
    end
  end
  last = { x = p.x, y = p.y, z = p.z }
  if want < M.ALPHA_STEP then
    o:setHidden(true)
    hidden, alpha = true, 0
    return false
  end
  local wx, wy = tonumber(wind and wind.x) or 0, tonumber(wind and wind.y) or 0
  local qx, qy, qz, qw = M.tiltQuat(wx - vx, wy - vy)
  o:setPosRot(p.x, p.y, p.z, qx, qy, qz, qw)
  local q = math.floor(want / M.ALPHA_STEP + 0.5) * M.ALPHA_STEP
  if hidden or math.abs(q - alpha) >= M.ALPHA_STEP * 0.5 then
    o:setField('instanceColor', 0, string.format('1 1 1 %.4f', q))
    o:updateInstanceRenderData()
    alpha = q
    M.writes = M.writes + 1
  end
  if hidden then o:setHidden(false); hidden = false end
  return true
end

function M.shown() return not hidden end

-- The level goes (or the setting is switched off): the object deleted.
function M.reset()
  local o = id and scenetree.findObjectById(id)
  if o then o:delete() end
  id, alpha, hidden, last, vx, vy = nil, 0, true, nil, 0, 0
end

return M
