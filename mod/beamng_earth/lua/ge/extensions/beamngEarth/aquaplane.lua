-- Aquaplaning, the game side (the user, 2026-11-26: "yes, plus deep-water aquaplaning"): the puddles shown near the
-- player's own car (roads.lua puddleParts: the skins' puddle quads, server roadwear.puddle_spots), how full they are
-- (wetlook's alpha threshold) and the rain's water film on the road go to the car's vehicle Lua (vehicle extension
-- beamngEarth_aquaplane), which scales each tyre's grip over water by the speed. Called from weather.lua each frame
-- while the weather shows rain or puddles with the setting 'Wet roads' on; it looks around twice a second and sends
-- only a changed list (or the same one every RESEND_S: a reset car or a new one gets it again). Nothing runs with the
-- weather Off, the setting off or a dry road. Each player's own car only (BeamMP: MPVehicleGE.isOwn), every client
-- its own: the same puddles and the same rain give the same grip on every machine.
local M = {}

M.INTERVAL_S = 0.5        -- the look around
M.RESEND_S = 3            -- an unchanged list sent again this often (a reset or replaced car)
M.NEAR_M = 30             -- puddles within NEAR_M + LOOK_S x speed of the car
M.LOOK_S = 1.5
M.MAX_SPOTS = 96          -- the nearest this many
M.FILM_TAU_S = 60         -- the film follows the rain shown with this time constant
M.FILM_SEND_MIN = 0.3     -- mm: a thinner film is sent as none (the vehicle side ignores under 0.5)
-- The water film over the road texture in rain (Gallaway et al. 1979, FHWA-RD-79-31): WD = 3.38e-3 T^-0.11 L^0.43
-- I^0.59 S^-0.42 - T (inches), texture T 0.02 in (0.5 mm), drainage path L 24 ft (two lanes from the crown), cross
-- slope S 2 %, I the rain (in/h): 5 mm/h 0.5 mm, 10 mm/h 1.0 mm, 25 mm/h 2.1 mm, 50 mm/h 3.5 mm.
M.FILM = { t = 0.02, l = 24, s = 0.02 }

local abs, exp, max, min, sqrt = math.abs, math.exp, math.max, math.min, math.sqrt

-- Pure: the water film (mm over the texture) of rain mmh (mm/h).
function M.filmMm(mmh)
  mmh = tonumber(mmh) or 0
  if mmh <= 0 then return 0 end
  local f = M.FILM
  local wd = 3.38e-3 * f.t ^ -0.11 * f.l ^ 0.43 * (mmh / 25.4) ^ 0.59 * f.s ^ -0.42 - f.t
  return max(0, wd * 25.4)
end

-- Pure: the spots of parts ({ { ox, oy, oz, spots } }: an object's position, its flat spots relative to it) within r of
-- (x, y), nearest first (ties by position: the same list whatever the parts' order), at most maxN: a flat list in
-- world coordinates.
function M.nearSpots(parts, x, y, r, maxN)
  local found = {}
  local r2 = r * r
  for _, p in ipairs(parts) do
    local ox, oy, oz, s = p[1], p[2], p[3], p[4]
    for i = 1, #s - 7, 8 do
      local wx, wy = ox + s[i], oy + s[i + 1]
      local dx, dy = wx - x, wy - y
      local d2 = dx * dx + dy * dy
      local reach = max(s[i + 5], s[i + 6])
      if d2 <= r2 + 2 * r * reach + reach * reach then
        found[#found + 1] = { d2, wx, wy, oz + s[i + 2], s[i + 3], s[i + 4], s[i + 5], s[i + 6], s[i + 7] }
      end
    end
  end
  table.sort(found, function(a, b)
    if a[1] ~= b[1] then return a[1] < b[1] end
    if a[2] ~= b[2] then return a[2] < b[2] end
    return a[3] < b[3]
  end)
  local out = {}
  for k = 1, min(#found, maxN or M.MAX_SPOTS) do
    local f = found[k]
    for j = 2, 9 do out[#out + 1] = f[j] end
  end
  return out
end

-- Pure: the vehicle command for ref (alpha threshold), film (mm) and the flat world spots.
function M.command(ref, film, list)
  local parts = {}
  for i = 1, #list - 7, 8 do
    parts[#parts + 1] = string.format('%.2f,%.2f,%.2f,%.4f,%.4f,%.2f,%.2f,%d', list[i], list[i + 1], list[i + 2],
      list[i + 3], list[i + 4], list[i + 5], list[i + 6], list[i + 7])
  end
  return string.format("if not beamngEarth_aquaplane then extensions.load('beamngEarth_aquaplane') end "
    .. "if beamngEarth_aquaplane then beamngEarth_aquaplane.set(%d, %.2f, {%s}) end", ref, film, table.concat(parts, ','))
end

------------------------------------------------------------------------------------------
-- runtime

-- Is a point under cover (ambience.shelterAt: a road tile's tunnel, arch, parking or under-bridge box)?
function M.covered(p)
  local amb = package.loaded['beamngEarth/ambience']
  if not (p and amb and type(amb.shelterAt) == 'function') then return false end
  local ok, kind = pcall(amb.shelterAt, p.x, p.y, p.z + 0.5)
  return ok and kind ~= nil or false
end

local film = 0
local timer, sinceSend = 0, 0
local lastCmd, lastId = nil, nil
M.sends = 0

-- Is vehicle id this player's own (single player: yes; BeamMP: MPVehicleGE.isOwn)?
local function own(id)
  local mp = rawget(_G, 'MPVehicleGE')
  if type(mp) == 'table' and type(mp.isOwn) == 'function' then
    local ok, v = pcall(mp.isOwn, id)
    return ok and v == true
  end
  return true
end

local function send(veh, cmd)
  if veh then
    veh:queueLuaCommand(cmd)
    M.sends = M.sends + 1
  end
end

local function clearCar(id)
  local o = id and getObjectByID and getObjectByID(id)
  if o then o:queueLuaCommand('if beamngEarth_aquaplane then beamngEarth_aquaplane.set(255, 0, {}) end') end
end

-- Every frame from weather.lua while it rains, puddles show, or this has sent water: dt, ref (wetlook
-- puddleRefShown: 255 none), rainMmH (the rain shown), on (the setting, weather on). Returns true while it has water
-- sent (weather.lua keeps calling until it is cleared).
function M.update(dt, ref, rainMmH, on)
  dt = tonumber(dt) or 0
  timer, sinceSend = timer + dt, sinceSend + dt
  if timer < M.INTERVAL_S then return lastId ~= nil end
  local veh = getPlayerVehicle and getPlayerVehicle(0) or nil
  local id = veh and veh:getID() or nil
  if id and not own(id) then veh, id = nil, nil end
  -- no rain film under cover (the puddle plan D14: a tunnel, an arch, a parking structure, under a bridge's deck)
  -- (with the new puddle look on, M.coverCheck: weather.lua; off, as before)
  local covered = M.coverCheck and veh ~= nil and (tonumber(rainMmH) or 0) > 0 and M.covered(veh:getPosition()) or false
  local target = (on and not covered) and M.filmMm(rainMmH) or 0
  film = film + (target - film) * (1 - exp(-timer / M.FILM_TAU_S))
  if not on or covered then film = 0 end
  M.coveredNow = covered
  timer = 0
  ref = on and tonumber(ref) or 255
  local f = film >= M.FILM_SEND_MIN and film or 0
  if lastId and lastId ~= id then clearCar(lastId); lastId, lastCmd = nil, nil end
  if (ref >= 255 and f == 0) or not veh then
    if lastId then clearCar(lastId) end
    lastId, lastCmd = nil, nil
    return false
  end
  local list = {}
  if ref < 255 then
    local roads = package.loaded['beamngEarth/roads']
    local parts = {}
    for _, p in ipairs(roads and roads.puddleParts and roads.puddleParts() or {}) do
      local o = scenetree.findObjectById(p.id)
      if o and not o:isHidden() then
        local op = o:getPosition()
        parts[#parts + 1] = { op.x, op.y, op.z, p.spots }
      end
    end
    local pos = veh:getPosition()
    local vel = veh.getVelocity and veh:getVelocity() or nil
    local speed = vel and sqrt(vel.x * vel.x + vel.y * vel.y) or 0
    list = M.nearSpots(parts, pos.x, pos.y, M.NEAR_M + M.LOOK_S * speed, M.MAX_SPOTS)
  end
  local cmd = M.command(ref, f, list)
  if cmd ~= lastCmd or sinceSend >= M.RESEND_S then
    send(veh, cmd)
    lastCmd, lastId, sinceSend = cmd, id, 0
  end
  return true
end

function M.film() return film end

function M.reset()
  if lastId then clearCar(lastId) end
  film, timer, sinceSend, lastCmd, lastId = 0, 0, 0, nil, nil
end

return M
