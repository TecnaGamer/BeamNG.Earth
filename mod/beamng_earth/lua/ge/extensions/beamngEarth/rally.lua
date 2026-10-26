-- Rally (the user, 2026-10-26: "find a real rally road, and set up a rally race for it" -> a real US stage, timed with
-- pacenotes, in a loaned rally car, from a new Rally phone app). The stages are the STPR's (Wellsboro PA) forest roads,
-- built by the server from the map data with their pacenotes (server/rally.py). A run:
--   loading   the stage asked of the server
--   travel    teleported to the start, waiting for the world there
--   countdown your car parked and hidden as it is (jobs.lua's way), a rally car at the start line; 5..1, GO
--   run       the clock; the co-driver's calls ahead of each corner; checkpoints every CP_M; the finish line
--   done      the time (and your best); a few seconds later the rally car goes and your car is back
-- A start before GO costs JUMP_S. Leaving the road for good (OFF_M for OFF_S) ends the run.
-- progress(), callLead() and fmt() are pure (mod/tests/test_rally.lua).
local M = {}

M.FILE = '/settings/beamngEarth_rally.json'
M.STAGES = {                    -- (server/rally.py STAGES: the same ids)
  { id = 'asaph', name = 'Asaph', area = 'Tioga State Forest, PA', miles = 8.71 },
  { id = 'painter', name = 'Painter', area = 'Tioga State Forest, PA', miles = 3.91 },
}
M.WAIT_S = 60                   -- the server's answer (it may fetch the forest's map data first)
M.SETTLE_S = 6                  -- at the start this long before the car is put out (the world loading in)
M.COUNT_S = 5
M.JUMP_M, M.JUMP_S = 3, 10
M.CP_M = 1000
M.FINISH_M = 15
M.OFF_M, M.OFF_S = 150, 20
M.DONE_S = 6
M.WINDOW = 40                   -- points looked ahead for progress (no shortcuts across the forest)
-- the loaned car: a stock config with 'rally' in it, of these models first (any rally config otherwise)
M.CARS = { 'sunburst2', 'sunburst', 'covet', 'pessima', 'etki', 'bx' }

local state, last, err, reqId, bests = nil, nil, nil, 0, nil

local function core() return rawget(_G, 'beamngEarth_core') end
local function msg(text, icon, ttl, cat)
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then
    gh.trigger('Message', { msg = text, category = cat or 'bneRally', icon = icon or 'flag', ttl = ttl or 4 })
  end
end

-- Pure: m:ss.t
function M.fmt(s)
  s = math.max(0, tonumber(s) or 0)
  local m = math.floor(s / 60)
  return string.format('%d:%04.1f', m, s - m * 60)
end

-- Pure: how far ahead (m) a call is made at speed (m/s): about 3 s, never under 60 or over 200 m.
function M.callLead(speed) return math.min(200, math.max(60, (speed or 0) * 3)) end

-- Pure: progress along xs, ys (with cumulative cum) of the point (px, py), searching from index i0 to i0 + window.
-- Returns s (m along), the index of the segment, the distance off the road.
function M.progress(xs, ys, cum, px, py, i0, window)
  local n = #xs
  local best, bi, bs = math.huge, i0 or 1, cum[i0 or 1] or 0
  local a = math.max(1, (i0 or 1) - 3)
  local b = math.min(n - 1, (i0 or 1) + (window or M.WINDOW))
  for i = a, b do
    local x0, y0, dx, dy = xs[i], ys[i], xs[i + 1] - xs[i], ys[i + 1] - ys[i]
    local l2 = dx * dx + dy * dy
    local t = l2 > 0 and math.max(0, math.min(1, ((px - x0) * dx + (py - y0) * dy) / l2)) or 0
    local qx, qy = x0 + dx * t, y0 + dy * t
    local d = (px - qx) ^ 2 + (py - qy) ^ 2
    if d < best then best, bi, bs = d, i, cum[i] + t * (cum[i + 1] - cum[i]) end
  end
  return bs, bi, math.sqrt(best)
end

local function loadBests()
  if bests then return bests end
  local read = rawget(_G, 'jsonReadFile')
  local ok, got = false, nil
  if read then ok, got = pcall(read, M.FILE) end
  bests = ok and type(got) == 'table' and got or {}
  bests.best = bests.best or {}
  bests.splits = bests.splits or {}
  return bests
end
local function saveBests()
  local write = rawget(_G, 'jsonWriteFile')
  if write and bests then pcall(write, M.FILE, bests, true) end
end

local function playerVeh()
  local be_ = rawget(_G, 'be')
  return be_ and be_:getPlayerVehicle(0) or nil
end

-- the loaned rally car: model, config path
function M.pickCar()
  local cv = rawget(_G, 'core_vehicles')
  local ok, list = false, nil
  if cv and cv.getConfigList then ok, list = pcall(cv.getConfigList) end
  local configs = ok and type(list) == 'table' and (list.configs or list) or {}
  local found = {}
  for _, c in pairs(configs) do
    local key, model = tostring(c.key or ''), tostring(c.model_key or '')
    if key:lower():find('rally') then
      local rank = 99
      for i, m in ipairs(M.CARS) do if m == model then rank = i end end
      if key:lower():find('gravel') or key:lower():find('dirt') then rank = rank - 0.5 end
      found[#found + 1] = { rank = rank, model = model, key = key, name = c.Name or c.name }
    end
  end
  table.sort(found, function(a, b) return a.rank < b.rank or (a.rank == b.rank and a.key < b.key) end)
  local f = found[1]
  if f then return f.model, 'vehicles/' .. f.model .. '/' .. f.key .. '.pc', f.name end
  return 'sunburst2', nil, 'Sunburst'
end

local function rot(dx, dy) return quatFromDir(vec3(dx, dy, 0), vec3(0, 0, 1)) end

local function groundZ(x, y, z)
  local be_ = rawget(_G, 'be')
  local ok, h = pcall(function() return be_:getSurfaceHeightBelow(vec3(x, y, (z or 0) + 50)) end)
  return (ok and tonumber(h) and h > -1e6) and h or (z or 0)
end

-- your car parked and hidden (as jobs.lua's Start work), the rally car at the start line facing down the road
local function putOutCar(x, y, z, dx, dy)
  local be_, cv = rawget(_G, 'be'), rawget(_G, 'core_vehicles')
  local own = playerVeh()
  if not (be_ and cv and cv.spawnNewVehicle and own) then return false end
  local model, config, name = M.pickCar()
  local ok = pcall(function()
    state.ownId = own:getID()
    own:setActive(0)
    local v = cv.spawnNewVehicle(model, { pos = vec3(x, y, z + 0.4), rot = rot(dx, dy), autoEnterVehicle = true,
                                          config = config })
    state.carId = v and v.getID and v:getID() or nil
  end)
  if not ok or not state.carId then
    local o = state.ownId and be_:getObjectByID(state.ownId)
    if o then pcall(function() o:setActive(1) end) end
    state.ownId = nil
    return false
  end
  state.carName = name
  return true
end

local function restoreOwn()
  local be_ = rawget(_G, 'be')
  if not (state and be_) then return end
  local own = state.ownId and be_:getObjectByID(state.ownId)
  local car = state.carId and be_:getObjectByID(state.carId)
  local okP, p, d = false, nil, nil
  if car then okP, p, d = pcall(function() return car:getPosition(), car:getDirectionVector() end) end
  if car then pcall(function() car:delete() end) end
  if own then
    pcall(function() own:setActive(1) end)
    if okP and p and d then
      pcall(function()
        local r = rot(d.x, d.y)
        local sp = rawget(_G, 'spawn')
        if sp and sp.safeTeleport then sp.safeTeleport(own, vec3(p.x + d.y * 4, p.y - d.x * 4, p.z + 0.3), r)
        else own:setPositionRotation(p.x, p.y, p.z + 0.3, r.x, r.y, r.z, r.w) end
      end)
    end
    pcall(function() be_:enterVehicle(0, own) end)
  end
  state.ownId, state.carId = nil, nil
end

function M.start(id)
  if state then return false, 'finish or quit the stage first' end
  local jobs = package.loaded['beamngEarth/jobs']
  if jobs and jobs.isActive and jobs.isActive() then return false, 'finish or cancel your job first' end
  local found = nil
  for _, s in ipairs(M.STAGES) do if s.id == id then found = s end end
  if not found then return false, 'no such stage' end
  local c = core()
  if not (c and c.requestRallyStage) then return false, 'BeamNG.Earth is not loaded' end
  reqId = reqId + 1
  if not c.requestRallyStage(reqId, id) then return false, 'not connected to the world server' end
  state, err = { phase = 'loading', id = id, name = found.name, req = reqId, wait = 0 }, nil
  msg('Rally: loading the ' .. found.name .. ' stage...', 'flag', 6)
  return true
end

function M.quit()
  if not state then return false end
  restoreOwn()
  if state.phase == 'run' or state.phase == 'countdown' then msg('Stage abandoned', 'warning') end
  state = nil
  return true
end

local function onStage(st)
  state.stage = st
  state.points, state.notes = st.points or {}, st.notes or {}
  local p = state.points[1]
  local c = core()
  if not (p and c and c.teleport) then err = 'could not go to the stage' state = nil return end
  c.teleport(p[1], p[2], 'Rally: ' .. tostring(st.name), { record = false })
  state.phase, state.wait, state.near = 'travel', 0, 0
  msg(string.format('%s stage, %s: %.1f mi, %d calls. Going to the start...', tostring(st.name), tostring(st.area),
    (tonumber(st.dist) or 0) / 1609.344, #state.notes), 'flag', 8)
end

-- the stage in the anchor's frame (again after a rebase moves the anchor)
local function project(anchor)
  if state.anchorRef == anchor and state.xs then return end
  local xs, ys, cum = {}, {}, { 0 }
  for i, p in ipairs(state.points) do
    xs[i], ys[i] = anchor:geodeticToEnu(p[1], p[2], 0)
    if i > 1 then cum[i] = cum[i - 1] + math.sqrt((xs[i] - xs[i - 1]) ^ 2 + (ys[i] - ys[i - 1]) ^ 2) end
  end
  state.xs, state.ys, state.cum, state.anchorRef = xs, ys, cum, anchor
  state.total = cum[#cum] or 0
end

-- (x, y) and heading at s m along
local function along(s)
  local cum, xs, ys = state.cum, state.xs, state.ys
  for i = 1, #cum - 1 do
    if cum[i + 1] >= s then
      local t = (s - cum[i]) / math.max(1e-6, cum[i + 1] - cum[i])
      return xs[i] + (xs[i + 1] - xs[i]) * t, ys[i] + (ys[i + 1] - ys[i]) * t, xs[i + 1] - xs[i], ys[i + 1] - ys[i]
    end
  end
  local n = #xs
  return xs[n], ys[n], xs[n] - xs[n - 1], ys[n] - ys[n - 1]
end

local function drawGate(s, color, z)
  local dd = rawget(_G, 'debugDrawer')
  if not dd then return end
  local x, y, dx, dy = along(s)
  local l = math.sqrt(dx * dx + dy * dy)
  if l <= 0 then return end
  local nx, ny = -dy / l * 5, dx / l * 5
  pcall(function()
    for _, sg in ipairs({ 1, -1 }) do
      local gx, gy = x + nx * sg, y + ny * sg
      local gz = groundZ(gx, gy, z)
      dd:drawCylinder(vec3(gx, gy, gz), vec3(gx, gy, gz + 3), 0.25, color)
    end
  end)
end

local function finish()
  local b = loadBests()
  local time = state.t + (state.penalty or 0)
  local prev = tonumber(b.best[state.id])
  local record = not prev or time < prev
  if record then
    b.best[state.id] = time
    b.splits[state.id] = state.splits
    saveBests()
  end
  last = { id = state.id, name = state.name, time = time, penalty = state.penalty, prev = prev, record = record,
           car = state.carName }
  msg(string.format('FINISH %s: %s%s%s', tostring(state.name), M.fmt(time),
    (state.penalty or 0) > 0 and string.format(' (+%ds jump start)', state.penalty) or '',
    record and (prev and string.format('  NEW BEST (-%.1f)', prev - time) or '  first time set')
      or string.format('  best %s (+%.1f)', M.fmt(prev), time - prev)), 'flag', 10)
  state.phase, state.wait = 'done', 0
end

-- Every frame (core): dt the simulation's; anchor, player as jobs.update's.
function M.update(dt, anchor, player)
  if not state then return end
  dt = dt or 0
  if state.phase == 'loading' then
    state.wait = state.wait + dt
    local c = core()
    local r = c and c.takeRallyStage and c.takeRallyStage(state.req)
    if r then
      if r.stage then onStage(r.stage) else err = r.error or 'the stage could not be built'; msg('Rally: ' .. err, 'warning'); state = nil end
    elseif state.wait > M.WAIT_S then
      err = 'the world server did not answer in time'
      state = nil
    end
    return
  end
  if not (anchor and player) then return end
  project(anchor)
  if state.phase == 'travel' then
    state.wait = state.wait + dt
    local d = math.sqrt((player.x - state.xs[1]) ^ 2 + (player.y - state.ys[1]) ^ 2)
    state.near = d < 120 and state.near + dt or 0
    if state.near >= M.SETTLE_S then
      local x, y, dx, dy = along(0)
      local _, _, hx, hy = along(15)
      if not putOutCar(x, y, groundZ(x, y, player.z), hx or dx, hy or dy) then
        err = 'could not bring out the rally car'
        msg('Rally: ' .. err, 'warning')
        state = nil
        return
      end
      state.phase, state.cd, state.sx, state.sy, state.lastCall = 'countdown', M.COUNT_S, x, y, nil
      msg(tostring(state.carName) .. ' ready. Stand by...', 'flag', 3)
    elseif state.wait > 120 then
      err = 'the stage start did not load'
      msg('Rally: ' .. err, 'warning')
      restoreOwn()
      state = nil
    end
    return
  end
  drawGate(0, ColorF(0.95, 0.85, 0.1, 0.8), player.z)
  if state.phase == 'countdown' then
    state.cd = state.cd - dt
    local n = math.ceil(state.cd)
    if n ~= state.lastCall and n > 0 then state.lastCall = n; msg(tostring(n), 'flag', 1) end
    if not state.jumped and (player.x - state.sx) ^ 2 + (player.y - state.sy) ^ 2 > M.JUMP_M ^ 2 then
      state.jumped, state.penalty = true, M.JUMP_S
      msg('Jump start: +' .. M.JUMP_S .. ' s', 'warning', 3)
    end
    if state.cd <= 0 then
      state.phase, state.t, state.s, state.i, state.note, state.cp, state.splits, state.off =
        'run', 0, 0, 1, 1, 1, {}, 0
      state.penalty = state.penalty or 0
      local first = state.notes[1]
      msg('GO!', 'flag', 3)
      if first then msg(first.text, 'flag', 4, 'bneRallyNote') end
      if first then state.note = 2 end
    end
    return
  end
  if state.phase == 'run' then
    state.t = state.t + dt
    local s, i, off = M.progress(state.xs, state.ys, state.cum, player.x, player.y, state.i)
    if off < 40 then state.s, state.i = math.max(state.s, s), i end
    -- the co-driver: each call made its lead ahead of the corner
    local speed = math.sqrt((player.vx or 0) ^ 2 + (player.vy or 0) ^ 2)
    local nt = state.notes[state.note]
    if nt and state.s >= (tonumber(nt.at) or 0) - M.callLead(speed) then
      msg(nt.text, 'flag', 4, 'bneRallyNote')
      state.note = state.note + 1
    end
    -- checkpoints (splits against your best run's)
    local cpS = state.cp * M.CP_M
    if cpS < state.total - M.FINISH_M then
      drawGate(cpS, ColorF(0.2, 0.85, 0.35, 0.8), player.z)
      if state.s >= cpS then
        state.splits[state.cp] = state.t
        local bs = loadBests().splits[state.id]
        local ref = type(bs) == 'table' and tonumber(bs[state.cp]) or nil
        msg(string.format('Split %d: %s%s', state.cp, M.fmt(state.t),
          ref and string.format(' (%s%.1f)', state.t <= ref and '-' or '+', math.abs(state.t - ref)) or ''), 'flag', 3)
        state.cp = state.cp + 1
      end
    end
    drawGate(state.total, ColorF(0.9, 0.15, 0.15, 0.8), player.z)
    if state.s >= state.total - M.FINISH_M then finish() return end
    state.off = off > M.OFF_M and state.off + dt or 0
    if state.off > M.OFF_S then
      msg('Off the stage: run over (DNF)', 'warning', 6)
      last = { id = state.id, name = state.name, dnf = true }
      restoreOwn()
      state = nil
    end
    return
  end
  if state.phase == 'done' then
    state.wait = state.wait + dt
    if state.wait >= M.DONE_S then restoreOwn(); state = nil end
  end
end

-- For the Rally app (settings.uiRally).
function M.uiState()
  local b = loadBests()
  local stages = {}
  for _, s in ipairs(M.STAGES) do
    stages[#stages + 1] = { id = s.id, name = s.name, area = s.area, miles = s.miles, best = b.best[s.id] }
  end
  local a = nil
  if state then
    a = { phase = state.phase, name = state.name, t = state.t, s = state.s, total = state.total, car = state.carName,
          notes = state.notes and #state.notes or nil, penalty = state.penalty }
  end
  return { stages = stages, active = a, last = last, error = err }
end

function M.isActive() return state ~= nil end
function M._reset() state, last, err, reqId, bests = nil, nil, nil, 0, nil end   -- tests
function M._state() return state end
return M
