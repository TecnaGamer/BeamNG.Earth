-- Drive a real bus line (GE extension beamngEarth_busline; GitHub #1363 "Bus routes missions in city"; the user,
-- 2026-11-17: "yes start the bus lines"). The lines near the player come from OpenStreetMap through the server
-- (buslines.py: number, colour, destination, stops in order). Picking one puts it on the city bus's own displays (the
-- stock citybus controller's bus_setLineInfo: the destination sign, the next-stops screen) and routes the GPS through
-- the stops ahead. A stop counts as served when the bus stands at it with its doors open for DWELL_S (any other car:
-- standing); driving past it is a missed stop. The bus hears the stock stop events (bus_onAtStop,
-- bus_onDepartedStop), so its stop screen moves on as in the game's own bus routes.
--
-- Open: the radial menu's Other ring ("Bus lines"), the input action beamngEarth_busLines, or
-- beamngEarth_busline.open() in the console. Nothing runs per frame while no line is driven and the window is shut.
local M = {}

M.STOP_RADIUS_M = 30        -- m (level) from a stop on the road the bus is at the stop: Västtrafik's points are 1-24 m
                            -- from OpenStreetMap's stop (where the sign stands; Göteborg, 2026-11-18), and the bus is 12 m
M.PLATFORM_RADIUS_M = 35    -- ... from a stop mapped only by its platform, which is beside the road
M.LEAVE_M = 20              -- m beyond that radius the bus has left the stop (served or missed); with 18 m and no margin
                            -- a stop cleared on the way to its sign (the user, 2026-11-18)
M.STANDING_MS = 0.5         -- m/s: standing still
M.DWELL_S = 3               -- s standing (doors open, on a bus) for a stop to count
M.TICK_S = 0.25             -- s between two checks while a line is driven
M.SKIP_AHEAD = 3            -- at one of this many later stops: the ones before it were missed
M.ROUTE_AHEAD = 8           -- stops after the next the GPS routes through: 9 and the bus's place, Valhalla's
                            -- most (routing.VALHALLA_MAX_LOCATIONS: "Exceeded max locations: 10", 2026-11-17)
M.REQUEST_TIMEOUT_S = 200   -- s: allow the first GTFS wait (45 s) and both Overpass endpoints (4 s connect + 65 s
                            -- read each, plus throttling); a valid fallback answer must not arrive after we give up
M.BUSES = { citybus = true,  -- vehicles with a bus controller (signs, doors): the stock Wentward DT40L
  capsule = true }           -- the Cycjos Capsule (repo mod), its own controller with the stock events
                            -- (any other vehicle that answers bus_onTriggerTick with its doors counts too)
M.TRIGGER = 'bne_bus_'      -- the stops' names on the bus's line (tasklist[i][1])

-- Replaceable for the tests
M.core = function() return rawget(_G, 'beamngEarth_core') end
M.api = function() return rawget(_G, 'beamngEarth_api') end
M.gps = function() return require('beamngEarth/gps') end
M.say = function(text, seconds)
  if ui_message then pcall(ui_message, text, seconds or 6, 'beamngEarthBus') end
end
M.log = function(text) if log then pcall(log, 'I', 'beamngEarth.busline', text) end end
M.vehicle = function() return rawget(_G, 'getPlayerVehicle') and getPlayerVehicle(0) or nil end

local lines, linesError, attribution, pending, nextId = nil, nil, nil, nil, 0
local pendingAge, pendingSession = 0, nil  -- the pending lookup's age (s) and the server session it went to
local run = nil             -- the line being driven: { line, idx, served, missed, inside, dwell, here, time, bus, doors }
local tick = 0
local winOpen = nil

-- The destination sign draws from a sprite atlas of ASCII letters (citybusDestinationDisplay.html): å ä ö é ... as
-- their base letters there; the stop screen is HTML and keeps the names as they are.
local FOLD = { ['å'] = 'a', ['ä'] = 'a', ['ö'] = 'o', ['Å'] = 'A', ['Ä'] = 'A', ['Ö'] = 'O', ['é'] = 'e', ['É'] = 'E',
  ['ü'] = 'u', ['Ü'] = 'U', ['ø'] = 'o', ['Ø'] = 'O', ['æ'] = 'ae', ['Æ'] = 'AE', ['ß'] = 'ss' }
function M.ascii(s)
  s = tostring(s or '')
  for k, v in pairs(FOLD) do s = s:gsub(k, v) end
  return (s:gsub('[\128-\255]+', ''))
end

local function stopName(st) return (st.name ~= nil and st.name ~= '') and st.name or 'Stop' end
function M.triggerName(line, i) return M.TRIGGER .. tostring(line.id) .. '_' .. i end

------------------------------------------------------------------------------------------
-- the lines near the player

local function sessionId()
  local core = M.core()
  local ok, st = pcall(function() return core and core._state and core._state() end)
  return ok and st and st.sessionId or nil
end

-- A pending lookup that will never be answered: the server restarted (a new session: a server without bus lines
-- ignored it, or it was lost with the old one) or no answer within REQUEST_TIMEOUT_S. Then the window was stuck on
-- "Finding bus lines" and Find lines here did nothing until Lua reloaded (2026-11-18).
function M.stale()
  if not pending then return nil end
  if sessionId() ~= pendingSession then return 'restarted' end
  if pendingAge >= M.REQUEST_TIMEOUT_S then return 'timeout' end
  return nil
end

function M.request()
  if pending and not M.stale() then return false end -- repeated clicks must not start more lookups
  pending = nil
  local api, core = M.api(), M.core()
  local lat, lon
  if api and api.position then lat, lon = api.position() end
  if not lat then linesError = 'Not in the BeamNG.Earth world yet' M.log(linesError) return false end
  if not (core and core.requestBusLines) then linesError = 'BeamNG.Earth is not loaded' M.log(linesError) return false end
  nextId = nextId + 1
  if not core.requestBusLines(nextId, lat, lon) then
    linesError = 'Companion server not connected' M.log(linesError) return false
  end
  M.log(string.format('lookup %d at %.5f, %.5f (session %s)', nextId, lat, lon, tostring(sessionId())))
  pending, linesError, lines = nextId, nil, nil
  pendingAge, pendingSession = 0, sessionId()
  return true
end

function M.onLines(msg)
  if type(msg) ~= 'table' then return end
  M.log(string.format('answer %s: %d lines%s%s', tostring(msg.id), type(msg.lines) == 'table' and #msg.lines or -1,
    msg.error and (', error ' .. tostring(msg.error)) or '', msg.id ~= pending and ' (not the pending one: ignored)' or ''))
  if msg.id ~= pending then return end
  pending = nil
  lines = type(msg.lines) == 'table' and msg.lines or {}
  linesError = msg.error and ('Could not get the bus lines: ' .. tostring(msg.error)) or nil
  attribution = msg.attribution
end

function M.lines() return lines, linesError, pending ~= nil end

------------------------------------------------------------------------------------------
-- driving a line

-- The stop's x, y in the game (nil before the world is there); z: the bus's own, for the bus's 75 m approach check
local function gamePos(st, z)
  local api = M.api()
  if not (api and api.toGame) then return nil end
  local x, y = api.toGame(st.lat, st.lon, 0)      -- (not 'api and api.toGame(...)': that keeps only x)
  if not x then return nil end
  return x, y, z
end

local function busPos()
  local veh = M.vehicle()
  if not veh then return nil end
  local p = veh:getPosition()
  local v = veh.getVelocity and veh:getVelocity() or nil
  local speed = v and math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z) or 0
  return p.x, p.y, p.z, speed, veh
end

local function vehCommand(veh, event, data)
  if veh and veh.queueLuaCommand and serialize then
    veh:queueLuaCommand('controller.onGameplayEvent(' .. string.format('%q', event) .. ',' .. serialize(data) .. ')')
  end
end

-- The line on the bus's displays, from the next stop on (the stock bus_setLineInfo resets its stop count: the list
-- it gets always starts at the next stop)
function M.lineInfo(r, z)
  local tasks = {}
  for i = r.idx, #r.line.stops do
    local st = r.line.stops[i]
    local x, y = gamePos(st, z)
    tasks[#tasks + 1] = { M.triggerName(r.line, i), stopName(st), { x or 0, y or 0, z or 0 } }
  end
  return { routeID = r.line.ref ~= '' and r.line.ref or '?', direction = M.ascii(r.line.to), routeColor = r.line.colour,
           variance = tostring(r.line.id), tasklist = tasks }
end

local function sendLine()
  if not run then return end
  local _, _, z, _, veh = busPos()
  vehCommand(veh, 'bus_setLineInfo', M.lineInfo(run, z))
end

local function route()
  if not run then return end
  local pts = {}
  for i = run.idx, math.min(#run.line.stops, run.idx + M.ROUTE_AHEAD) do
    local st = run.line.stops[i]
    pts[#pts + 1] = { lat = st.lat, lon = st.lon, name = stopName(st) }
  end
  local gps = M.gps()
  if gps and #pts > 0 then
    gps.setRoute(pts, 'bus')                     -- (Valhalla's bus route: the bus-only streets a car may not use)
  end
end

local function isBus()
  local veh = M.vehicle()
  return M.BUSES[veh and veh.getJBeamFilename and veh:getJBeamFilename() or ''] == true
end

function M.start(line, from)
  if type(line) ~= 'table' or type(line.stops) ~= 'table' or #line.stops < 2 then return false end
  run = { line = line, idx = math.max(1, math.min(#line.stops, from or line.near_i and line.near_i + 1 or 1)),
          served = 0, missed = 0, inside = false, dwell = 0, here = false, time = 0, bus = isBus(), doors = false }
  tick = 0
  sendLine()
  route()
  local st = line.stops[run.idx]
  M.say(string.format('Line %s to %s: first stop %s%s', line.ref, line.to, stopName(st),
    run.bus and '' or ' (a city bus shows the line on its signs)'), 8)
  return true
end

function M.stop()
  if not run then return end
  run = nil
  local gps = M.gps()
  if gps and gps.clear then gps.clear() end
end

function M.running() return run end

local function finish()
  local r = run
  run = nil
  local t = r.time
  M.say(string.format('Line %s to %s done: %d of %d stops served, %d missed, %d:%02d', r.line.ref, r.line.to, r.served,
    r.served + r.missed, r.missed, math.floor(t / 60), math.floor(t % 60)), 12)
  local gps = M.gps()
  if gps and gps.clear then gps.clear() end
end

-- Leave stop idx (served or missed), on to the next
local function advance(veh, missed)
  local st = run.line.stops[run.idx]
  vehCommand(veh, 'bus_onDepartedStop', { triggerName = M.triggerName(run.line, run.idx) })
  if missed then
    run.missed = run.missed + 1
    M.say('Missed stop: ' .. stopName(st), 5)
  end
  run.idx, run.inside, run.dwell, run.here, run.doors = run.idx + 1, false, 0, false, false
  if run.idx > #run.line.stops then finish() return end
  sendLine()
  route()
end

local function radius(st) return st.on_road == false and M.PLATFORM_RADIUS_M or M.STOP_RADIUS_M end

-- One check (every TICK_S while a line is driven): at the stop, standing, doors; past it; at a later one
function M.step(dt)
  if not run then return end
  run.time = run.time + dt
  local x, y, z, speed, veh = busPos()
  if not x then return end
  local st = run.line.stops[run.idx]
  local sx, sy = gamePos(st, z)
  if not sx then return end
  local d = math.sqrt((sx - x) ^ 2 + (sy - y) ^ 2)
  if d <= radius(st) then
    run.inside = true
    local name = M.triggerName(run.line, run.idx)
    vehCommand(veh, 'bus_onTriggerTick', { triggerName = name })     -- the bus answers with its doors (onBusUpdate)
    if not run.here and speed < M.STANDING_MS and (run.doors or not run.bus) then
      run.dwell = run.dwell + dt
      if run.dwell >= M.DWELL_S then
        run.here, run.served = true, run.served + 1
        vehCommand(veh, 'bus_onAtStop', { triggerName = name })
        M.say(string.format('%s (%d of %d)', stopName(st), run.idx, #run.line.stops), 4)
      end
    elseif not run.here then
      run.dwell = 0
    end
    return
  end
  if run.inside and d <= radius(st) + M.LEAVE_M then return end      -- just outside: not left yet
  if run.inside then advance(veh, not run.here) return end
  for k = run.idx + 1, math.min(#run.line.stops, run.idx + M.SKIP_AHEAD) do
    local s2 = run.line.stops[k]
    local kx, ky = gamePos(s2, z)
    if kx and math.sqrt((kx - x) ^ 2 + (ky - y) ^ 2) <= radius(s2) then
      while run and run.idx < k do advance(veh, true) end
      return
    end
  end
end

-- The bus's answer to bus_onTriggerTick (the stock citybus controller: extensions.hook('onBusUpdate', ...))
function M.onBusUpdate(state)
  if run and type(state) == 'table' and state.event == 'onTriggerTick'
     and state.triggerName == M.triggerName(run.line, run.idx) then
    run.bus = true                  -- it answered: a bus controller, whatever the model (from now on its doors count)
    run.doors = state.bus_dooropen == true
  end
end

-- The world moved (a rebase) or the player took another car: the stops' places on the bus again
function M.onBeamNGEarthRebased() sendLine() end
function M.onVehicleSwitched() if run then run.bus = isBus() sendLine() end end
function M.onVehicleSpawned(vid)
  local veh = M.vehicle()
  if run and veh and veh.getID and veh:getID() == vid then run.bus = isBus() sendLine() end
end

-- A city bus in place of the player's car (the stock replaceVehicle), the line on its signs once it is there
function M.getBus()
  local cv = rawget(_G, 'core_vehicles')
  if cv and cv.replaceVehicle then pcall(cv.replaceVehicle, 'citybus', {}) end
end

------------------------------------------------------------------------------------------
-- the window

function M.open()
  M.log('window opened')
  winOpen = true
  if not lines and not pending then M.request() end
end
function M.toggle() if winOpen then winOpen = false else M.open() end end

local function draw()
  local im = rawget(_G, 'ui_imgui')
  if not im then return end
  local p = im.BoolPtr(true)
  im.SetNextWindowSize(im.ImVec2(460, 420), im.Cond_FirstUseEver)
  if im.Begin('Bus lines##beamngEarth', p) then
    if run then
      local st = run.line.stops[run.idx]
      im.TextUnformatted(string.format('Driving line %s to %s', run.line.ref, run.line.to))
      im.TextUnformatted(string.format('Next stop: %s (%d of %d)   served %d, missed %d', st and stopName(st) or '-',
        run.idx, #run.line.stops, run.served, run.missed))
      if im.Button('Stop driving the line') then M.stop() end
      im.Separator()
    end
    if im.Button('Find lines here') then M.request() end
    im.SameLine()
    if im.Button('Get a city bus') then M.getBus() end
    if pending then im.TextUnformatted('Finding bus lines (may take up to three minutes) ...') end
    if linesError then im.TextUnformatted(linesError) end
    if lines and #lines == 0 and not pending and not linesError then im.TextUnformatted('No bus lines mapped here') end
    if lines and lines[1] and lines[1].saved then        -- (buslines._nearby: Overpass down, an earlier lookup's)
      im.TextUnformatted('OpenStreetMap is not answering: lines saved from an earlier lookup near here')
    end
    for i, l in ipairs(lines or {}) do
      local st = l.stops[(l.near_i or 0) + 1]
      if im.Button('Drive##busline' .. i) then M.start(l) end
      im.SameLine()
      local label = l.mode == 'trolleybus' and ('Trolleybus ' .. l.ref) or l.ref
      im.TextUnformatted(string.format('%s  %s -> %s   %d stops, nearest %s %d m', label, l.from, l.to, #l.stops,
        st and stopName(st) or '-', math.floor(l.near_m or 0)))
    end
    if attribution then im.Separator() im.TextUnformatted(attribution) end
  end
  im.End()
  if not p[0] then winOpen = false end
end

function M.onUpdate(dtReal)
  if pending then
    pendingAge = pendingAge + (dtReal or 0)
    local why = M.stale()
    if why == 'restarted' and winOpen then M.request()  -- the new server: ask it
    elseif why then
      M.log('lookup ' .. tostring(pending) .. ' given up: ' .. why)
      pending = nil
      linesError = why == 'timeout' and 'No answer from the companion server: try Find lines here again'
        or 'The companion server restarted: try Find lines here again'
    end
  end
  if winOpen then draw() end
  if not run then return end
  tick = tick + (dtReal or 0)
  if tick >= M.TICK_S then
    local dt = tick
    tick = 0
    M.step(dt)
  end
end

-- The radial menu's entry: beside Nearest station in the stock "Other" ring (finder.lua)
function M.onQuickAccessLoaded()
  local qa = rawget(_G, 'core_quickAccess')
  if not (qa and qa.addEntry) then return end
  qa.addEntry({ level = '/root/sandbox/other/', uniqueID = 'beamngEarthBusLines', generator = function(entries)
    local core = M.core()
    local ok, st = pcall(function() return core and core._state and core._state() end)
    if not (ok and st and st.inOurLevel) then return end
    table.insert(entries, { title = 'Bus lines', icon = 'road', uniqueID = 'beamngEarthBusLinesOpen',
      desc = 'Drive a real bus line from here: its stops in order, on the city bus\'s signs',
      onSelect = function() M.open() return { 'hide' } end })
  end })
end
M.onExtensionLoaded = function() M.onQuickAccessLoaded() end

function M._reset()
  lines, linesError, attribution, pending, run, tick, winOpen = nil, nil, nil, nil, nil, 0, nil
  pendingAge, pendingSession = 0, nil
end

return M
