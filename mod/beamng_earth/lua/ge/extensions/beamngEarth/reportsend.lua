-- The in-game reports' extra data and their "Send to the developers" window (report.lua; the user, 2026-11-12:
-- "Could we update the in game reporter to send reports directly to our mini pc server?" and "update the reporter with
-- any info and data that would be useful to us"). Reports are PRIVATE: sent to the BeamNG.Earth developers only (the
-- project's own server), never published (the user's call, 2026-11-12).
--
--   view()          where the player looked: the camera (game coords, lat / lon, heading, pitch, FOV), the point under
--                   the screen's centre (a ray: its game coords, lat / lon, distance) and the object hit there (its
--                   class, name, shape file)
--   environment()   time of day, weather, sky
--   config()        the mod's settings file, the enabled mods' names, the UI scale
--   frames(...)     the last 60 s of frame times
--   The world server adds its own part when the report is taken (report_context: server.json with the build, the
--   map's ids and the roads / buildings near the aim point, the data sources there, memory, backlog; logs.json with
--   the server log's end and the game log's recent warnings and errors; server/reportsend.py), user names and home
--   folders scrubbed out (earthfast reportpack).
--
--   The send window: nothing is sent before the player clicks Send in it. It lists what goes (the text, the
--   screenshot, the location, the game / PC info, the world server's state, the logs) and lets the player leave out the
--   screenshot, the exact location (rounded to ~1 km then) and the logs. The world server sends it (in the
--   background, retried, kept on disk until sent: the folder's send.json / sent.json) and answers with a short id
--   the player can quote on Discord.
local M = {}

M.AIM_M = 3000              -- the centre ray's reach
M.FRAMES_S = 60             -- the recent frame times' window
M.PREVIEW_MAX = 262144      -- bytes of a file shown in the window

local im = nil
local win = nil             -- the open send window: { dir, name, data, shot, exact, logs, view = {} }
local status = {}           -- folder name -> { state, id, text, next }

local function try(f, ...)
  if type(f) ~= 'function' then return nil end
  local ok, v = pcall(f, ...)
  if ok then return v end
  return nil
end

-- A copy of v that jsonEncode takes: plain values, tables of them, at most `depth` deep.
local function plain(v, depth)
  local t = type(v)
  if t == 'string' or t == 'boolean' then return v end
  if t == 'number' then return (v == v and v ~= math.huge and v ~= -math.huge) and v or nil end
  if t ~= 'table' or (depth or 4) <= 0 then return nil end
  local out = {}
  local n = 0
  for k, x in pairs(v) do
    n = n + 1
    if n > 200 then break end
    if type(k) == 'string' or type(k) == 'number' then out[k] = plain(x, (depth or 4) - 1) end
  end
  return out
end
M.plain = plain

local function api() return rawget(_G, 'beamngEarth_api') end
local function geo(x, y, z)
  local a = api()
  if not (a and a.toGeo) then return nil end
  local ok, lat, lon, h = pcall(a.toGeo, x, y, z)
  if ok and lat then return lat, lon, h end
  return nil
end

-- See the module's notes.
function M.view()
  if not core_camera then return nil end
  local c = core_camera.getPosition()
  local fw = core_camera.getForward and core_camera.getForward()
  if not (c and fw) then return nil end
  local v = { camera = { pos = { c.x, c.y, c.z }, forward = { fw.x, fw.y, fw.z },
    headingDeg = math.deg(math.atan2(fw.x, fw.y)) % 360,
    pitchDeg = math.deg(math.asin(math.max(-1, math.min(1, fw.z)))),
    fovDeg = try(core_camera.getFovDeg) } }
  v.camera.lat, v.camera.lon, v.camera.h = geo(c.x, c.y, c.z)
  local ray = rawget(_G, 'castRayStatic')
  if ray then
    local ok, d = pcall(ray, vec3(c.x, c.y, c.z), vec3(fw.x, fw.y, fw.z), M.AIM_M)
    if ok and d and d < M.AIM_M then
      local p = { c.x + fw.x * d, c.y + fw.y * d, c.z + fw.z * d }
      v.aim = { pos = p, distance = d }
      v.aim.lat, v.aim.lon = geo(p[1], p[2], p[3])
    end
  end
  -- the object under the centre (render geometry, terrain included): what it is
  local E = rawget(_G, 'Engine')
  if E and E.castRay and v.aim then
    local far = vec3(c.x + fw.x * (v.aim.distance + 1), c.y + fw.y * (v.aim.distance + 1), c.z + fw.z * (v.aim.distance + 1))
    local ok, hit = pcall(E.castRay, vec3(c.x, c.y, c.z), far, true, true)
    if ok and type(hit) == 'table' then
      local pick = {}
      for k, x in pairs(hit) do
        if type(x) == 'string' or type(x) == 'number' or type(x) == 'boolean' then pick[k] = x end
      end
      local obj = hit.obj or hit.object
      if type(obj) == 'number' then obj = scenetree.findObjectById(obj) end
      if obj and type(obj) ~= 'table' or (type(obj) == 'table' and obj.getClassName) then
        pick.class = try(function() return obj:getClassName() end)
        pick.name = try(function() return obj:getName() end)
        pick.id = try(function() return obj:getID() end)
        pick.shape = try(function() return obj:getField('shapeName', 0) end)
        pick.material = try(function() return obj:getField('material', 0) end)
        pick.group = try(function() local g = obj:getGroup(); return g and g:getName() end)
      end
      v.pick = pick
    end
  end
  return v
end

-- See the module's notes.
function M.environment()
  local e = {}
  local env = rawget(_G, 'core_environment')
  if env then
    e.timeOfDay = plain(try(env.getTimeOfDay), 2)
    e.fogDensity = try(env.getFogDensity)
    e.windSpeed = try(env.getWindSpeed)
    e.gravity = try(env.getGravity)
  end
  local w = rawget(_G, 'beamngEarth_weather')
  if w then
    e.weather = plain(try(w.status), 3)
    e.weatherLine = try(w.statusLine)
  end
  local sky = rawget(_G, 'beamngEarth_sky')
  if sky then e.sky = plain(try(sky.status), 3) end
  return e
end

-- See the module's notes.
function M.config()
  local c = {}
  local S = rawget(_G, 'beamngEarth_settings')
  local read = rawget(_G, 'jsonReadFile')
  if S and S.FILE and read then c.modSettings = plain(try(read, S.FILE), 3) end
  local mm = rawget(_G, 'core_modmanager')
  local mods = mm and try(mm.getMods)
  if type(mods) == 'table' then
    local names = {}
    for name, m in pairs(mods) do
      if type(name) == 'string' and (type(m) ~= 'table' or m.active ~= false) then names[#names + 1] = name end
    end
    table.sort(names)
    c.mods = names
  end
  local s = rawget(_G, 'settings')
  if s and s.getValue then
    c.uiScale = try(s.getValue, 'uiScale')
    c.graphicsQuality = try(s.getValue, 'GraphicOverallQuality')
  end
  local core = rawget(_G, 'beamngEarth_core')
  if core then
    c.credits = try(core.credits)
    c.banner = plain(try(core.banner), 2)
  end
  return c
end

-- The frames of the last FRAMES_S seconds from report.lua's ring (newest at ringN): stats(dts) of them.
function M.frames(ring, ringN, size, stats)
  local dts, sum = {}, 0
  for k = 0, math.min(ringN, size) - 1 do
    local ms = ring[(ringN - 1 - k) % size + 1]
    if not ms then break end
    dts[#dts + 1] = ms
    sum = sum + ms
    if sum >= M.FRAMES_S * 1000 then break end
  end
  local r = stats(dts)
  r.seconds = sum / 1000
  return r
end

------------------------------------------------------------------------------------------
-- the send window

local function folderName(dir) return (dir or ''):match('([^/]+)/?$') end

local function net() return require('beamngEarth/net') end

local function readJson(path)
  local t = readFile(path)
  if not t then return nil end
  local ok, v = pcall(jsonDecode, t)
  return ok and type(v) == 'table' and v or nil
end

function M.onMessage(msg)
  if msg.type == 'report_status' and type(msg.folder) == 'string' then
    status[msg.folder] = { state = tostring(msg.state or ''), id = msg.id and tostring(msg.id) or nil,
      text = msg.text and tostring(msg.text) or nil, next = tonumber(msg.next) }
  elseif msg.type == 'report_context_done' and type(msg.folder) == 'string' then
    status[msg.folder] = status[msg.folder] or {}
    status[msg.folder].context = msg.ok == true
  end
end

function M.status(name) return status[name] end

-- The moment's server part (report.lua calls it when a report is taken).
function M.askContext(dir, data)
  local name = folderName(dir)
  local v = data.view or {}
  local function ll(t) return t and t.lat and { lat = t.lat, lon = t.lon } or nil end
  return net().send({ type = 'report_context', folder = name, camera = ll(v.camera), aim = ll(v.aim),
    player = data.lat and { lat = data.lat, lon = data.lon } or nil })
end

-- The mod's files are written: the server scrubs them (user names, home folders).
function M.saved(dir)
  net().send({ type = 'report_saved', folder = folderName(dir) })
end

local function size(dir, file)
  if not FS:fileExists(dir .. file) then return nil end
  local n = try(function() return FS:fileSize(dir .. file) end)
  if type(n) == 'number' and n >= 0 then return n end
  local st = try(function() return FS:stat(dir .. file) end)
  return type(st) == 'table' and st.filesize or 0
end

-- The window for the saved report in dir (data: report.json's table).
function M.open(dir, data)
  local name = folderName(dir)
  local prev = readJson(dir .. 'sent.json')
  if prev and not status[name] then
    status[name] = { state = tostring(prev.state or ''), id = prev.id, text = prev.text }
  end
  win = { dir = dir, name = name, data = data, shot = FS:fileExists(dir .. 'screenshot.jpg'),
    exact = true, logs = FS:fileExists(dir .. 'logs.json'), view = {}, bufs = {} }
end

function M.isOpen() return win ~= nil end

local function wrapped(s)
  im.PushTextWrapPos(0)
  im.TextUnformatted(tostring(s))
  im.PopTextWrapPos()
end

local function checkbox(label, value)
  local p = im.BoolPtr(value)
  im.Checkbox(label, p)
  return p[0]
end

-- A file's text, read-only, in a box (what is sent; the location is rounded on the way when the box is unticked).
local function preview(w, file, label)
  local n = size(w.dir, file)
  if not n then return end
  if im.CollapsingHeader1(string.format('%s (%s, %.1f KB)##bngeSend%s', label, file, n / 1024, file)) then
    if not w.bufs[file] then
      local t = readFile(w.dir .. file) or ''
      if #t > M.PREVIEW_MAX then t = t:sub(1, M.PREVIEW_MAX) .. '\n...' end
      w.bufs[file] = im.ArrayChar(#t + 1, t)
    end
    im.InputTextMultiline('##bngeSendText' .. file, w.bufs[file], im.GetLengthArrayCharPtr(w.bufs[file]),
      im.ImVec2(-1, 160), im.InputTextFlags_ReadOnly)
  end
end

local function send(w)
  local choice = { screenshot = w.shot == true, exactLocation = w.exact == true, logs = w.logs == true, at = os.time() }
  writeFile(w.dir .. 'send.json', jsonEncode(choice))
  writeFile(w.dir .. 'sent.json', jsonEncode({ state = 'queued' }))
  status[w.name] = { state = 'queued' }
  if not net().send({ type = 'report_send', folder = w.name }) then
    status[w.name].text = 'the world server is not connected: sent when it is running again'
  end
end

-- Tests (the console, MCP): the Send button of the window that is open, with its boxes set as given ({screenshot,
-- exactLocation, logs}); false when no window is open.
function M.clickSend(choices)
  local w = win
  if not w then return false end
  choices = choices or {}
  if choices.screenshot ~= nil then w.shot = choices.screenshot == true end
  if choices.exactLocation ~= nil then w.exact = choices.exactLocation == true end
  if choices.logs ~= nil then w.logs = choices.logs == true end
  send(w)
  return true
end

M.PRIVACY ='Sent privately to the BeamNG.Earth developers (the project\'s own server); not published. Kept 90 days. '
  .. 'Nothing is sent until you click Send, and sending works with anonymous usage statistics off too: each report '
  .. 'is your own choice. User names and home folders are removed; a random install id (no name or account) is sent '
  .. 'with it so we can tell reports from one computer apart.'

function M.draw(imgui)
  im = imgui
  local w = win
  if not w then return end
  im.SetNextWindowSize(im.ImVec2(620, 560), im.Cond_FirstUseEver)
  local keep = im.BoolPtr(true)
  if im.Begin('Send this report to the BeamNG.Earth developers##bngeSend', keep, 0) then
    local d = w.data or {}
    local st = status[w.name]
    wrapped(M.PRIVACY)
    im.Separator()
    im.TextUnformatted('What is sent:')
    wrapped(string.format('- The report: %s "%s"%s', tostring(d.kind or ''), tostring(d.title or ''),
      (d.note and #d.note > 0) and string.format(' and its description (%d characters)', #d.note) or ''))
    if size(w.dir, 'screenshot.jpg') then
      w.shot = checkbox(string.format('Screenshot (%.0f KB)##bngeSendShot', (size(w.dir, 'screenshot.jpg') or 0) / 1024), w.shot)
    end
    local loc = d.lat and string.format('%.5f, %.5f', d.lat, d.lon) or 'none'
    w.exact = checkbox('Exact location: ' .. loc .. '##bngeSendLoc', w.exact)
    if not w.exact and d.lat then
      wrapped(string.format('  Rounded to about 1 km instead: %.2f, %.2f (the map ids, street names and the roads and '
        .. 'buildings near the spot are left out too). The screenshot can still show where you are.', d.lat, d.lon))
    end
    if size(w.dir, 'logs.json') then
      w.logs = checkbox('Logs: the world server\'s last 300 lines and the game\'s warnings and errors of the last '
        .. '10 minutes##bngeSendLogs', w.logs)
    end
    wrapped('- Game and PC: the game and mod versions, graphics settings, CPU / GPU / memory, the mod\'s settings and '
      .. 'enabled mods\' names (hardware.json, report.json)')
    wrapped('- The view and performance: camera, what is under the screen\'s centre, the car, the last 60 s of frame '
      .. 'times, weather and time of day (report.json)')
    wrapped('- The world server\'s state: its version and settings, memory, loading backlog, the data sources and map '
      .. 'features at the spot (server.json)')
    preview(w, 'report.json', 'Report')
    preview(w, 'hardware.json', 'Game and PC')
    preview(w, 'server.json', 'World server')
    preview(w, 'logs.json', 'Logs')
    im.Separator()
    local busy = st and (st.state == 'queued' or st.state == 'sending' or st.state == 'retry')
    if st and st.state == 'sent' then
      im.TextUnformatted('Sent. Report id: ' .. tostring(st.id or '?'))
      wrapped('Quote this id on Discord or GitHub so we can find your report.')
      if st.text and #st.text > 0 then wrapped('(' .. st.text .. ')') end
    elseif busy then
      im.TextUnformatted(st.state == 'retry' and string.format('Not sent yet, trying again in %d s',
        st.next or 0) or 'Sending...')
      if st.text and #st.text > 0 then wrapped(st.text) end
      wrapped('It is kept and sent in the background (also after a restart): you can close this window.')
    else
      if st and st.state == 'failed' then wrapped('Not sent: ' .. tostring(st.text or '')) end
      if im.Button(st and st.state == 'failed' and 'Try again##bngeSendGo' or 'Send##bngeSendGo') then send(w) end
      im.SameLine()
    end
    if im.Button('Open folder##bngeSendDir') then pcall(Engine.Platform.exploreFolder, '/' .. w.dir) end
    im.SameLine()
    if im.Button('Close##bngeSendClose') then win = nil end
  end
  im.End()
  if not keep[0] then win = nil end
end

return M
