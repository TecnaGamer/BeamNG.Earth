-- Tram signals (plan 046 stage H; the server's tramsignals.py / kernel tram::signals): a signal on its post before the stop line
-- where a street tram crosses a signalled junction, facing the approaching tram. The road tile's "tsig" rows
-- [x, y, z, yaw, style, cycle, offset, green, branch] (anchor frame, yaw degrees counter-clockwise of the model's +x axis, branch
-- 1 / -1 / 0: a track leaves to the left / right / not at all ahead of the stop line) become one TSStatic each
-- (bne_tramsig_bar / bne_tramsig_bos, with _l / _r the diagonal bar of a branch; tools/build_tram_signals.py), made and dropped with
-- the tile like the pump jacks' parts (pumpjacks.lua). The lamps are the shape's palette-instanced emissive material, the stock
-- traffic lights' mechanism: the object's instanceColor (slot 0, the horizontal bar: stop), instanceColor1 (slot 1, the vertical
-- bar: go) and instanceColor2 (slot 2, the diagonal bar: the branch's way) are '1 1 1 1' (lit) or '0 0 0 1' (dark)
-- (core_trafficSignals SignalInstance:setLights).
--
-- The phases. A signal is "go" for `green` seconds (the vertical bar; with a branch the first half, the second half "turn": the
-- diagonal bar), "warn" (the bars together: stop expected) for WARN_S more, then "stop" for the rest of its `cycle`; offset
-- shifts it. The cycle, green and offset are the junction's (a hash of its signal node: every tile and every client agree) and the
-- clock is the wall clock (os.time), so nothing is sent or synchronised. A signal near a junction of the navgraph's car signals
-- (navgraph.lua signalData) is bound to its sequence instead, in one of two ways:
--   parallel: the tram runs along the axis of one of the junction's two car phases (M.parallelPhase): it goes while that phase is
--             green (the cars of that axis go with it), warns in its yellow and stops in the other phase; the cars turning
--             across it are not held (the AI has no turn signals);
--   window:   the tram crosses the junction's roads: the sequence gets a third phase, the trams' window, in which every car signal
--             is red, and the tram signal shows "go" while the sequence is in it ("warn" for the last WARN_S seconds), "stop" in the
--             cars' phases.
-- No bound sequence (outside the AI graph's window, or none): the wall-clock cycle. Cost: a check every CHECK_S, one comparison per
-- signal, a few setField calls at a change; nothing per frame.
local M = {}

local ART = '/levels/beamng_earth/art/shapes/beamngEarth/'
M.SHAPE = { [0] = ART .. 'bne_tramsig_bar', [1] = ART .. 'bne_tramsig_bos' }   -- the face style by the row's style (.dae)
M.WARN_S = 3             -- s the "stop expected" aspect (both bars) shows at the end of a go
M.CHECK_S = 0.25         -- s between two checks
M.JUNCTION_M = 40        -- m: a tram signal this near a navgraph signal junction follows its sequence
M.TRAM_PHASE = 3         -- the index of the trams' window among a junction's phases (navgraph.lua signalData)
M.PARALLEL = 0.8         -- the tram runs along a car phase's axis: its approaches' mean |cos| to the tram's heading is at least this ...
M.CROSS = 0.5            -- ... and the other phase's is at most this
M.ON, M.OFF = '1 1 1 1', '0 0 0 1'
M.onCollide = nil        -- (core: the collision rebuild, as pumpjacks.lua)

local tiles = {}         -- road tile key -> { sigs = { { x, y, z, th, style, cycle, offset, green, branch, id, state, nextAt, bind } } }
M.made = 0

-- Pure: the shape of a face style and branch side (1 left, -1 right, 0 none), with its extension.
function M.shapeOf(style, branch)
  local base = M.SHAPE[style] or M.SHAPE[1]
  return base .. (branch == 1 and '_l' or branch == -1 and '_r' or '') .. '.dae'
end

-- Pure: the state a go becomes at `fraction` (0 .. 1) of its time for a signal with a branch: the first half "go" (straight on), the
-- second half "turn" (the diagonal bar).
function M.goOf(branch, fraction)
  if branch and branch ~= 0 and fraction >= 0.5 then return 'turn' end
  return 'go'
end

-- Pure: a signal's state at wall-clock second `now` on its own cycle: ('go' | 'turn' | 'warn' | 'stop', the second it changes).
function M.freeState(cycle, offset, green, now, branch)
  cycle = math.max(cycle or 60, 1)
  local t = ((now or 0) + (offset or 0)) % cycle
  green = math.min(math.max(green or 0, 0), cycle)
  if t < green then
    if branch and branch ~= 0 then
      if t < green / 2 then return 'go', now + (green / 2 - t) end
      return 'turn', now + (green - t)
    end
    return 'go', now + (green - t)
  end
  local warnEnd = math.min(green + M.WARN_S, cycle)
  if t < warnEnd then return 'warn', now + (warnEnd - t) end
  return 'stop', now + (cycle - t)
end

-- Pure: the state of a signal bound to a car-signal sequence `seq` (core_trafficSignals' sequence object) by `bind` ({phase, mode,
-- ctrl}), timer `timer`, the signal's `branch` and `green`: nil when the sequence is not running.
function M.boundState(seq, bind, timer, branch, green)
  if type(seq) ~= 'table' or not seq.active or type(bind) ~= 'table' then return nil end
  if seq.currPhase ~= bind.phase then return 'stop' end
  local remaining = (seq.stateTime or 0) - (timer or 0)
  if bind.mode == 'par' then
    local cs = seq.controllerStates and seq.controllerStates[bind.ctrl]
    local st = cs and cs.controller and cs.controller.states and cs.controller.states[cs.stateIdx]
    if not st then return nil end
    if st.state == 'greenTrafficLight' or st.state == 'greenFlashingTrafficLight' then
      local total = math.max(st.duration or 0, 1e-6)
      return M.goOf(branch, math.min(math.max(1 - remaining / total, 0), 1))
    end
    if st.state == 'yellowTrafficLight' then return 'warn' end
    return 'stop'
  end
  if remaining <= M.WARN_S then return 'warn' end
  local total = math.max((green or 0), 1e-6)
  return M.goOf(branch, math.min(math.max(1 - (remaining - M.WARN_S) / total, 0), 1))
end

-- Pure: the lamps (slot 0, 1, 2) of a state: stop the horizontal bar, go the vertical one, turn the diagonal one, warn the first two.
function M.lamps(state)
  if state == 'go' then return M.OFF, M.ON, M.OFF end
  if state == 'turn' then return M.OFF, M.OFF, M.ON end
  if state == 'warn' then return M.ON, M.ON, M.OFF end
  return M.ON, M.OFF, M.OFF
end

-- Pure: the travel direction of a signal (the way the tram it faces runs): the engine yaw th turns the model's +y to (sin th, cos th).
function M.heading(s)
  return math.sin(s.th), math.cos(s.th)
end

-- Pure: the car phase (0 or 1) a tram signal `s` runs along, from the junction's approaches `apps` (the server's rows: [node, x, y, z,
-- dir x, dir y, phase, ...]), nil when it crosses them: the phase whose approaches are on its axis (mean |cos| >= PARALLEL) while
-- the other's (if it has any) are across it (<= CROSS).
function M.parallelPhase(s, apps)
  local ax, ay = M.heading(s)
  local sum, n = { [0] = 0, [1] = 0 }, { [0] = 0, [1] = 0 }
  for _, a in ipairs(apps or {}) do
    local ph = a[7] == 1 and 1 or 0
    local dl = math.sqrt((a[5] or 0) ^ 2 + (a[6] or 0) ^ 2)
    if dl > 1e-9 then
      sum[ph] = sum[ph] + math.abs(((a[5] or 0) * ax + (a[6] or 0) * ay) / dl)
      n[ph] = n[ph] + 1
    end
  end
  local mean = {}
  for ph = 0, 1 do mean[ph] = n[ph] > 0 and sum[ph] / n[ph] or nil end
  for ph = 0, 1 do
    local other = mean[1 - ph]
    if mean[ph] and mean[ph] >= M.PARALLEL and (other == nil or other <= M.CROSS) then return ph end
  end
  return nil
end

local function place(o, x, y, z, th)
  local m = MatrixF(true)
  m:setFromEuler(vec3(0, 0, th))
  m:setPosition(vec3(x, y, z))
  o:setTransform(m)
end

local function show(s, state)
  if s.state == state then return end
  s.state = state
  local o = s.id and scenetree.findObjectById(s.id)
  if not o then return end
  local a, b, c = M.lamps(state)
  o:setField('instanceColor', '0', a)
  o:setField('instanceColor1', '0', b)
  o:setField('instanceColor2', '0', c)
end

local function makeSignal(s)
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, M.shapeOf(s.style, s.branch))
  o:setField('decalType', 0, 'None')
  o:setField('dynamic', 0, 'true')       -- (no redraw of the cached sun shadow map: farfield.lua)
  o.canSave = false
  place(o, s.x, s.y, s.z, s.th)
  o:registerObject('')
  if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
  s.id = o:getID()
  s.state, s.nextAt = nil, 0
  M.made = M.made + 1
end

local function dropSignal(s)
  local o = s.id and scenetree.findObjectById(s.id)
  if o then o:delete() end
  s.id, s.bind = nil, nil
end

local function branchOf(b)
  return (b == 1 or b == -1) and b or 0
end

local function toSignal(r)
  return { x = r[1], y = r[2], z = r[3], th = -math.rad(r[4]), style = r[5], cycle = r[6], offset = r[7], green = r[8],
           branch = branchOf(r[9]), nextAt = 0 }
end

-- A road tile's signals (roads.lua onTile: data.tsig).
function M.onRows(key, rows)
  M.dropKey(key)
  if type(rows) ~= 'table' then return end
  local t = { sigs = {} }
  for _, r in ipairs(rows) do
    if type(r) == 'table' and #r >= 8 then
      local s = toSignal(r)
      local ok, err = pcall(makeSignal, s)
      if ok then t.sigs[#t.sigs + 1] = s else log('W', 'beamngEarth', 'tram signal: ' .. tostring(err)) end
    end
  end
  tiles[key] = t
  if #t.sigs > 0 and M.onCollide then M.onCollide(t.sigs[1].x, t.sigs[1].y) end
end

-- A road geotile's signals placed again (roads.lua M.replace: rows mapped into the new anchor's world): moved where the count
-- and the faces are the same, made again else.
function M.placeRows(key, rows)
  local t = tiles[key]
  local same = t and type(rows) == 'table' and #t.sigs == #rows
  if same then
    for i, s in ipairs(t.sigs) do
      local r = rows[i]
      if type(r) ~= 'table' or #r < 8 or r[5] ~= s.style or branchOf(r[9]) ~= s.branch then
        same = false
        break
      end
    end
  end
  if not same then return M.onRows(key, rows) end
  for i, s in ipairs(t.sigs) do
    local r = rows[i]
    s.x, s.y, s.z, s.th = r[1], r[2], r[3], -math.rad(r[4])
    local o = s.id and scenetree.findObjectById(s.id)
    if o then place(o, s.x, s.y, s.z, s.th) end
  end
  local s = t.sigs[1]
  if M.onCollide then M.onCollide(s and s.x, s and s.y) end
end

-- The road tile goes (roads.lua dropTile).
function M.dropKey(key)
  local t = tiles[key]
  if not t then return end
  for _, s in ipairs(t.sigs) do dropSignal(s) end
  tiles[key] = nil
  if #t.sigs > 0 and M.onCollide then M.onCollide(t.sigs[1].x, t.sigs[1].y) end
end

-- Origin rebase (core.lua finishRebase): every signal moved by p_new = R*p + t, its yaw turned with it (the model's +x axis
-- points to (cos th, -sin th) at the engine's yaw th).
function M.transform(xf)
  for _, t in pairs(tiles) do
    for _, s in ipairs(t.sigs) do
      local ax, ay = xf:apply(s.x + math.cos(s.th), s.y - math.sin(s.th), s.z)
      s.x, s.y, s.z = xf:apply(s.x, s.y, s.z)
      s.th = math.atan2(-(ay - s.y), ax - s.x)
      local o = s.id and scenetree.findObjectById(s.id)
      if o then place(o, s.x, s.y, s.z, s.th) end
    end
  end
end

-- The signals within r metres of (x, y): a list (navgraph.lua signalData).
function M.nearList(x, y, r)
  local out = {}
  local r2 = r * r
  for _, t in pairs(tiles) do
    for _, s in ipairs(t.sigs) do
      if (s.x - x) ^ 2 + (s.y - y) ^ 2 <= r2 then out[#out + 1] = s end
    end
  end
  return out
end

-- Bind signal `s` to a car-signal sequence: bind = { seq = its name, phase = the index of its phase, mode = 'par' (with the car phase
-- controller `ctrl`) or 'win' }; d2 the squared distance to the junction: the nearer junction of two keeps a signal.
function M.bind(s, bind, d2)
  if not s.bind or d2 < s.bind.d2 then
    bind.d2 = d2
    s.bind = bind
  end
end

-- A new navgraph: no signal follows an old sequence (navgraph.lua signalData, before it binds the new ones).
function M.unbindAll()
  for _, t in pairs(tiles) do
    for _, s in ipairs(t.sigs) do s.bind = nil end
  end
end

local lastCheck = -1e9
-- Every frame (core.lua, cheap): every CHECK_S the signals whose second has come; the bound ones follow their sequence.
function M.update()
  if not next(tiles) then return end
  local now = os.clockhp()
  if now - lastCheck < M.CHECK_S then return end
  lastCheck = now
  M.step(os.time(), rawget(_G, 'core_trafficSignals'))
end

-- One check at wall-clock second `wall`, ts the car signals' module (nil: none).
function M.step(wall, ts)
  for _, t in pairs(tiles) do
    for _, s in ipairs(t.sigs) do
      local state = nil
      if s.bind and ts and ts.getSequenceByName then
        local ok, seq = pcall(ts.getSequenceByName, s.bind.seq)
        if ok then state = M.boundState(seq, s.bind, ts.getTimer and ts.getTimer() or 0, s.branch, s.green) end
      end
      if state then
        show(s, state)
        s.nextAt = 0
      elseif wall >= (s.nextAt or 0) then
        local st, at = M.freeState(s.cycle, s.offset, s.green, wall, s.branch)
        show(s, st)
        s.nextAt = at
      end
    end
  end
end

-- Level end / a new session: everything goes.
function M.reset()
  for key in pairs(tiles) do M.dropKey(key) end
  tiles = {}
end

function M.status()
  local n, bound, par = 0, 0, 0
  for _, t in pairs(tiles) do
    for _, s in ipairs(t.sigs) do
      n = n + 1
      if s.bind then
        bound = bound + 1
        if s.bind.mode == 'par' then par = par + 1 end
      end
    end
  end
  return { signals = n, bound = bound, parallel = par, made = M.made }
end

return M
