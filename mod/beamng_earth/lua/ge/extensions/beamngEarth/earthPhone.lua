-- The Maps app (ui/modules/apps/BeamNGEarthMaps, by ianmadez, PR #299; now the BeamNG.Earth phone's Maps app: it is
-- shown inside the phone (BeamNGEarthPhone) when Maps is open, Ctrl+O or Page Up): search, a destination, the turn-by-turn card and the speed
-- limit tile. PgUp shows and hides it (actions/beamngEarth.json, keyboard_beamngEarth.json). The app is mounted when
-- its layout has it; this extension then sends it one 'BeamNGEarthMaps' hook SEND_S seconds apart while the phone
-- is on screen, and answers state() / search() / navigate() from bngApi.
--
-- Nothing here sends the vehicle's speed: the phone shows where it is, which way it points, the road's limit and the
-- route (the user, 2026-10-26). Search and routing go through the companion server (core.requestPhoneSearch,
-- gps.setDestination with the steps: server/phoneapi.py, routing.py).
local M = {}

M.SEND_S = 0.2         -- s between hooks (the app interpolates the puck between them)
M.LIMIT_M = 20         -- m the car moves before the speed limit is looked up again (edgeUnder walks the graph)
-- Where the app goes when this extension has to add it to the current app layout: the right edge, clear of the
-- speedometer (tacho2 in the .Earth layout is 300 px tall at the bottom right). app.js shrinks it if the screen is
-- too short for it (the box is clamped, and the frame scales to the box), so it never runs off the top.
M.PLACEMENT = { position = 'absolute', right = '5px', bottom = '310px', width = '340px', height = '737px' }

local net = require('beamngEarth/net')

local mounted = false      -- the app is on screen (its layout has it)
local visible = false      -- PgUp
local lastSend = -math.huge
local searchId, searchQuery, pendingSearch, searchAnswer = 0, '', nil, nil
local pin, pinId = nil, 0  -- a dropped pin { lat, lon, name, detail, id }: named by the server (phoneapi.reverse)
local limitCache = nil     -- { x, y, speed } of the last lookup

local function core() return rawget(_G, 'beamngEarth_core') end
local function gps() local ok, g = pcall(require, 'beamngEarth/gps') return ok and g or nil end
local function navgraph() local ok, g = pcall(require, 'beamngEarth/navgraph') return ok and g or nil end

local function note(level, msg)
  local l = rawget(_G, 'log')
  if l then pcall(l, level, 'beamngEarth', msg) end
end

-- Pure: the icon the app draws for a step (routing.parse_steps' type / modifier, OSRM's own names).
function M.icon(step)
  local t, m = (step and step.type) or '', (step and step.modifier) or ''
  if t == 'depart' then return 'depart' end
  if t == 'arrive' then return 'arrive' end
  if t == 'roundabout' or t == 'rotary' then return 'roundabout' end
  if t == 'merge' then return 'merge' end
  if t == 'ramp' or t == 'on ramp' or t == 'off ramp' then return 'ramp' end
  if m == 'uturn' then return 'uturn' end
  local side = m:find('left') and 'left' or (m:find('right') and 'right' or nil)
  if not side then return 'straight' end
  if m:find('slight') then return 'slight-' .. side end
  if m:find('sharp') then return 'sharp-' .. side end
  return side
end

-- Pure: the step being driven at travelled metres along the route (steps from routing.parse_steps, each with .at).
function M.stepIndex(steps, travelled)
  local best = 1
  for i = 2, #(steps or {}) do
    if steps[i].at <= (travelled or 0) then best = i else break end
  end
  return best
end

local function capital(s) return (tostring(s or ''):gsub('^%l', string.upper)) end

-- Pure: a maneuver as a navigator says it, as the clause after a distance ("in 300 m, turn right onto Main Street").
-- OSRM's step type / modifier / name (routing.parse_steps), exit on a roundabout.
function M.phrase(step)
  if not step then return 'continue straight' end
  local t, m = step.type or '', step.modifier or ''
  local road = step.road or step.name or ''
  local onto = road ~= '' and (' onto ' .. road) or ''
  local side = m:find('left') and 'left' or (m:find('right') and 'right' or nil)
  if t == 'depart' then
    return 'head ' .. (m ~= '' and m or 'out') .. (road ~= '' and (' on ' .. road) or '')
  end
  if t == 'arrive' then return 'arrive at your destination' end
  if m == 'uturn' then return 'make a U-turn' end
  if t == 'roundabout' or t == 'rotary' then
    local exit = tonumber(step.exit)
    if exit and exit > 0 then return string.format('at the roundabout, take exit %d%s', exit, onto) end
    return 'at the roundabout, continue' .. onto
  end
  if t == 'merge' then return 'merge ' .. (side or 'on') .. onto end
  if t == 'ramp' or t == 'on ramp' or t == 'off ramp' then
    return 'take the ramp' .. (side and (' on the ' .. side) or '') .. onto
  end
  if t == 'fork' then return 'keep ' .. (side or 'straight') .. onto end
  if t == 'new name' or t == 'continue' or t == 'notification' then
    return road ~= '' and ('continue on ' .. road) or 'continue straight'
  end
  if not side then return road ~= '' and ('continue on ' .. road) or 'continue straight' end
  local how = m:find('slight') and 'slightly ' or (m:find('sharp') and 'sharp ' or '')
  return 'turn ' .. how .. side .. onto
end

-- Pure: the instruction card for the route (steps from routing, travelled metres along it): the maneuver ahead,
-- what a navigator would say, the road it leads onto and the metres to it.
function M.instruction(steps, travelled)
  if type(steps) ~= 'table' or #steps == 0 then return nil end
  local i = M.stepIndex(steps, travelled)
  local ahead = steps[i + 1]
  if not ahead or ahead.type == 'arrive' then
    local here = ahead or steps[i]
    local say = M.phrase(here)
    return { icon = 'arrive', road = here.name or '', dist = math.max(0, (here.at or 0) - travelled), index = i,
      say = say, sayNow = capital(say) }
  end
  -- two turns close together are said as one ("turn right onto A, then turn left onto B"), as a navigator does
  local after = steps[i + 2]
  local then_ = (after and after.type == 'turn' and (after.at or 0) - (ahead.at or 0) <= 250) and M.phrase(after) or nil
  local say = M.phrase(ahead)
  return { icon = M.icon(ahead), road = ahead.name or '', dist = math.max(0, (ahead.at or 0) - travelled), index = i,
    say = say, sayNow = capital(say), thenSay = then_ }
end

local function vehicle() local get = rawget(_G, 'getPlayerVehicle') return get and get(0) or nil end

-- Compass heading of a vehicle (0 = north, clockwise), from true north at the car (the anchor's northAt).
function M.heading(st, veh)
  local d = veh and veh.getDirectionVector and veh:getDirectionVector()
  if not d then return nil end
  local h = math.deg(math.atan2(d.x, d.y))
  local a = st and st.anchor
  if a and a.northAt and st and st.player then
    local ok, nx, ny = pcall(a.northAt, a, st.player.x or 0, st.player.y or 0)
    if ok and tonumber(nx) and tonumber(ny) then h = h - math.deg(math.atan2(nx, ny)) end
  end
  if h ~= h then return nil end
  return math.floor((h + 360) % 360 + 0.5) % 360
end

local function edgeLimit(veh, st)
  local m = rawget(_G, 'map')
  local nodes = m and m.getMap and m.getMap().nodes
  local g = gps()
  local dir = veh and veh.getDirectionVector and veh:getDirectionVector()
  if not (nodes and g and dir and st and st.player) then return nil end
  local x, y = st.player.x, st.player.y
  if limitCache and (x - limitCache.x) ^ 2 + (y - limitCache.y) ^ 2 < M.LIMIT_M ^ 2 then return limitCache.speed end
  local a, b = g.edgeUnder(nodes, x, y, dir.x, dir.y)
  local e = a and g.edge(nodes, a, b)
  local speed = e and tonumber(e.speedLimit) or nil
  if not speed or speed <= 0 then speed = nil end
  limitCache = { x = x, y = y, speed = speed }
  return speed
end

-- Pure: the speed tile { value, unit } from a limit in m/s (the graph's own unit) and the country's posting.
function M.limitTile(speed, mph)
  if not speed or speed <= 0 then return nil end
  if mph then return { value = math.floor(speed * 2.23694 + 0.5), unit = 'mph' } end
  return { value = math.floor(speed * 3.6 + 0.5), unit = 'km/h' }
end

local function routeState()
  local g = gps()
  local s = g and g.status()
  if not s then return nil end
  local r = g.route()
  local total = r and r.dist or nil
  local out = { name = s.name, dist = s.dist, remaining = s.remaining, eta = s.eta, planning = s.planning,
    error = s.routeError, total = total, version = (r and r.version) or 0 }
  local steps = g.steps()
  if type(steps) == 'table' and total and s.remaining then
    out.step = M.instruction(steps, math.max(0, total - s.remaining))
  end
  out.options = g.options and g.options() or nil          -- the route choices (the phone's chips)
  out.off = s.off or 0                                    -- metres from the route: off it, it is planned again
  -- a re-plan with a route already on screen (the car left it), so the app can say "Recalculating route..."
  out.rerouting = (s.planning and total and total > 0) and true or (out.off > 30) or false
  return out
end

-- The planned route's geometry, which the app asks for when the polled version changes: {version, points, dist}.
function M.route()
  local g = gps()
  local r = g and g.route()
  if not r then return nil end
  return { version = r.version, points = r.points, dist = r.dist }
end

-- Pure: what the app draws (see app.js). st: core's state; veh: the player's vehicle or nil.
function M.payload(st, veh)
  local pl = st and st.player
  local nav = navgraph()
  return {
    visible = visible,
    mounted = mounted,
    connected = net.isConnected(),
    player = (pl and pl.lat) and { lat = pl.lat, lon = pl.lon, heading = M.heading(st, veh) } or nil,
    limit = M.limitTile(edgeLimit(veh, st), nav and nav.mph),
    pin = pin and { lat = pin.lat, lon = pin.lon, name = pin.name or '', detail = pin.detail or '' } or nil,
    search = pendingSearch and { query = pendingSearch, pending = true }
      or (searchAnswer and { query = searchQuery, results = searchAnswer.results, error = searchAnswer.error }) or nil,
    route = routeState(),
  }
end

local function send()
  lastSend = os.clock()
  local trigger = rawget(_G, 'guihooks') and guihooks.trigger
  if not trigger then return end
  local ok, payload = pcall(M.payload, core() and core()._state(), vehicle())
  if ok and payload then pcall(trigger, 'BeamNGEarthMaps', payload) end
end

function M.state()
  local ok, payload = pcall(M.payload, core() and core()._state(), vehicle())
  return ok and payload or { visible = visible, mounted = mounted }
end

function M.setVisible(on)
  visible = on and true or false
  if visible then M.ensureInLayout() end
  local g = gps()
  if g and g.phoneRouting then g.phoneRouting(visible) end     -- off-route re-routes sooner while it is open
  lastSend = -math.huge
  send()
  return visible
end

-- Page Up works whatever the player's app layout is: the app is in the ".Earth" layout, and if this session's
-- layout has it not (an older saved layout), it is added to it in memory (the UI Apps editor's own edit, not saved
-- to the file). Only a UI with no appLayouts at all falls back to saying where to add it by hand.
function M.ensureInLayout()
  -- (merged into the phone, 2026-10-27: Maps is drawn inside the phone app, which the layout already has; adding a
  -- standalone copy would show the map twice)
  if true then return true end
  if mounted then return true end
  local ext = rawget(_G, 'extensions')
  local al = ext and ext.ui_appLayouts
  local cur = al and al.getCurrentLayout and select(2, pcall(al.getCurrentLayout))
  if type(cur) ~= 'table' or type(cur.apps) ~= 'table' then
    pcall(function()
      ui_message('BeamNG.Earth phone: add "BeamNG.Earth: Phone" in the UI Apps editor (or pick the ".Earth" layout)',
        5, 'beamngEarthPhone')
    end)
    return false
  end
  for _, a in ipairs(cur.apps) do
    if type(a) == 'table' and a.appName == 'beamngEarthPhone' then return true end
  end
  local ok = pcall(function()
    local file = cur.filename or cur.type
    if al.addApp and type(file) == 'string' then
      al.addApp(file, 'beamngEarthPhone', M.PLACEMENT)      -- written to the layout: not needed again next start
      al.setCurrentLayout(file)
    else
      cur.apps[#cur.apps + 1] = { appName = 'beamngEarthPhone', placement = M.PLACEMENT }
      al.setCurrentLayout(cur)
    end
    local apps = ext.ui_apps
    if apps and apps.notifyLayoutsChanged then apps.notifyLayoutsChanged()
    else pcall(function() guihooks.trigger('UiAppLayoutsChanged') end) end
  end)
  note(ok and 'I' or 'W', 'phone: app added to the current layout' .. (ok and '' or ' (failed)'))
  return ok
end

function M.toggle() return M.setVisible(not visible) end

function M.mounted(on)
  mounted = on and true or false
  if mounted then send() end
  return mounted
end

function M.search(q)
  q = tostring(q or ''):sub(1, 80)
  local c = core()
  if not c or not c.requestPhoneSearch then return false end
  local st = c._state and c._state()
  local pl = st and st.player
  searchId = searchId + 1
  searchQuery, searchAnswer = q, nil
  if not c.requestPhoneSearch(searchId, q, pl and pl.lat, pl and pl.lon) then
    searchAnswer = { results = {}, error = 'the server is not connected' }
    note('W', 'phone: search "' .. q .. '" not sent (no companion server)')
    send()
    return false
  end
  pendingSearch = q
  note('I', 'phone: search "' .. q .. '" asked')
  send()
  return true
end

-- A dropped pin (a double tap on the map): shown at once, named when the server answers (onUpdate).
function M.setPin(lat, lon)
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon then return false end
  pinId = pinId + 1
  pin = { lat = lat, lon = lon, name = '', detail = '', id = pinId }
  local c = core()
  if c and c.requestPhoneReverse then
    if not c.requestPhoneReverse(pinId, lat, lon) then pin.id = nil end
  end
  lastSend = -math.huge
  send()
  return true
end

function M.clearPin()
  pin = nil
  lastSend = -math.huge
  send()
  return true
end

function M.navigate(lat, lon, name)
  local g = gps()
  if not g then return false end
  limitCache = nil
  searchAnswer, pendingSearch, pin = nil, nil, nil
  local ok = g.setDestination(lat, lon, name, true)    -- with the turn-by-turn steps
  lastSend = -math.huge
  send()
  return ok
end

function M.endRoute()
  local g = gps()
  if not g then return false end
  g.clear()
  lastSend = -math.huge
  send()
  return true
end

-- One of the route choices (the phone's chips): the GPS switches to it, arrows and Autodrive included.
function M.pickOption(i)
  local g = gps()
  if not g or not g.useRoute then return false end
  local ok = g.useRoute(i)
  lastSend = -math.huge
  send()
  return ok
end

function M.onExtensionLoaded()
  visible, mounted, pendingSearch, searchAnswer, limitCache, pin = false, false, nil, nil, nil, nil
  local g = gps()
  if g and g.phoneRouting then g.phoneRouting(false) end
  lastSend = -math.huge
  return true
end

function M.onUpdate(dtReal)
  if pendingSearch then
    local c = core()
    local r = c and c.takePhoneSearch and c.takePhoneSearch(searchId)
    if r then
      pendingSearch, searchAnswer = nil, { results = r.results or {}, error = r.error }
      note('I', string.format('phone: search "%s" -> %d places%s', searchQuery, #searchAnswer.results,
        r.error and (' (' .. r.error .. ')') or ''))
      lastSend = -math.huge
    end
  end
  if pin and pin.id then
    local c = core()
    local r = c and c.takePhoneReverse and c.takePhoneReverse(pin.id)
    if r then
      pin.name, pin.detail, pin.id = r.name or '', r.detail or '', nil
      lastSend = -math.huge
    end
  end
  if not mounted then return end
  if visible and os.clock() - lastSend >= M.SEND_S then send() end
end

function M.onExtensionUnloaded()
  visible, mounted = false, false
  local g = gps()
  if g and g.phoneRouting then g.phoneRouting(false) end
end

M.visible = function() return visible end
M.isMounted = function() return mounted end

return M
