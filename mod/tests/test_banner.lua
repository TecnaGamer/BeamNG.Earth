-- luajit mod/tests/test_banner.lua  (the connection banner, banner.lua; docs/ROADMAP.md "Robustness" 1)
-- The state machine (starting -> not running with the start command, connection lost with the attempt
-- number, waiting for hello, offline, degraded with the server's note, hidden when all is well, the
-- "connected" line after a lost server came back, the edge guard's second line), the server note
-- parser, the text rebuilt only when an input changed, the drawing (top centre, one backdrop + one
-- text, measured once per text, hidden off / outside our world / in the pause menu or big map / when
-- all is well), an imgui error turning it off once, and no allocation per frame.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local banner = require('beamngEarth/banner')

local function info(t)
  local i = { net = 'disconnected', session = false, everConnected = false, attempts = 0, note = nil,
    noteDetail = nil, edge = nil, stuck = false, port = 47613 }
  for k, v in pairs(t or {}) do i[k] = v end
  return i
end

------------------------------------------------------------------------------------------
-- compute: one state per situation

local id, level, text = banner.compute(info({ attempts = 1 }), 0.5)
T.check(id == 'starting' and level == 'info' and text:find('looking for the BeamNG.Earth server on this PC', 1, true), 'just loaded: starting')
id, level, text = banner.compute(info({ attempts = 4 }), banner.STARTING_S + 0.1)
T.check(id == 'noserver' and level == 'error', 'never connected after STARTING_S: not running')
T.check(text:find('not running on this PC', 1, true) and text:find('launcher', 1, true)
  and text:find('python3 server/earthd.py', 1, true), 'not running: on this PC, the launcher and the start command')
id, level, text = banner.compute(info({ everConnected = true, attempts = 3 }), 100)
T.check(id == 'lost' and level == 'warn' and text == 'Lost the BeamNG.Earth server on this PC - reconnecting (attempt 3). Is its launcher window still open?',
  'lost: reconnecting with the attempt number')
id, _, text = banner.compute(info({ net = 'connecting', everConnected = true, attempts = 0 }), 100)
T.check(id == 'lost' and text:find('attempt 1', 1, true), 'lost while connecting: at least attempt 1')
id, level = banner.compute(info({ net = 'connected', everConnected = true }), 100)
T.check(id == 'starting' and level == 'info', 'connected, no hello yet: starting')
id, level, text = banner.compute(info({ net = 'connected', session = true, everConnected = true }), 100)
T.check(id == 'ok' and level == 'ok' and text == '', 'all is well: ok, no text')
id, level, text = banner.compute(info({ net = 'connected', session = true, everConnected = true, note = 'offline' }), 100)
T.check(id == 'offline' and level == 'warn' and text:find('Server OK, internet data sources unreachable', 1, true), 'offline: the server is fine')
id, level, text = banner.compute(info({ net = 'connected', session = true, everConnected = true, note = 'degraded',
  noteDetail = 'usgs unreachable; 3 tiles without imagery' }), 100)
T.check(id == 'degraded' and level == 'warn' and text == 'Server OK, some internet data sources not answering: usgs unreachable; 3 tiles without imagery',
  'degraded: the server\'s note')
-- the edge guard's line
id, level, text = banner.compute(info({ everConnected = true, attempts = 2, edge = 'warn' }), 100)
T.check(id == 'lost' and level == 'warn' and text:find('\nNear the edge of the loaded ground', 1, true)
  and text:find('until the BeamNG.Earth server is back', 1, true), 'lost + near the edge: a second line')
id, level, text = banner.compute(info({ everConnected = true, attempts = 2, edge = 'hold' }), 100)
T.check(level == 'error' and text:find('\nPaused at the edge of the loaded ground', 1, true), 'held at the edge: error level')
id, level, text = banner.compute(info({ net = 'connected', session = true, everConnected = true, stuck = true, edge = 'hold' }), 100)
T.check(id == 'edge' and level == 'error' and text == 'Paused at the edge of the loaded ground until the BeamNG.Earth server answers terrain requests again (resume to drive back)',
  'connected but not answering terrain: the edge line alone')
id, _, text = banner.compute(info({ net = 'connected', session = true, everConnected = true, stuck = true }), 100)
T.check(id == 'ok' and text == '', 'not answering terrain, far from the edge: nothing to say yet')

------------------------------------------------------------------------------------------
-- the server's note

local k, d = banner.noteOf('Far field loading: 50/300 tiles | Offline - using cached data')
T.check(k == 'offline' and d == nil, 'noteOf: offline after the status text')
k = banner.noteOf('Offline - using cached data')
T.check(k == 'offline', 'noteOf: offline alone')
k, d = banner.noteOf('Anchor 2 ready | Degraded: usgs.gov unreachable; 4 tiles without imagery')
T.check(k == 'degraded' and d == 'usgs.gov unreachable; 4 tiles without imagery', 'noteOf: degraded + detail')
k, d = banner.noteOf('Degraded: 2 tiles without imagery')
T.check(k == 'degraded' and d == '2 tiles without imagery', 'noteOf: degraded alone')
T.check(banner.noteOf('Far field loading: 50/300 tiles') == nil and banner.noteOf(nil) == nil, 'noteOf: nothing')

------------------------------------------------------------------------------------------
-- update: the state over time

banner._reset()
local i = info({ attempts = 1 })
local s = banner.update(0.1, i)
T.check(s.id == 'starting' and s.visible, 'first frame: starting, visible')
local s2 = banner.update(0.1, i)
T.check(s2 == s, 'nothing changed: the same state (text not rebuilt)')
for _ = 1, 60 do s = banner.update(0.1, i) end
T.check(s.id == 'noserver', 'after STARTING_S without a server: not running')
T.check(banner.line() == s.text, 'line(): the banner text for the settings page')
i.net, i.everConnected, i.attempts = 'connected', true, 0
s = banner.update(0.1, i)
T.check(s.id == 'starting', 'connected: waiting for hello')
i.session = true
s = banner.update(0.1, i)
T.check(s.id == 'recovered' and s.level == 'ok' and s.text == 'BeamNG.Earth server connected', 'the server came: "connected" for a while')
for _ = 1, 30 do s = banner.update(0.1, i) end
T.check(s.id == 'recovered', '... still within RECOVERED_S')
for _ = 1, 20 do s = banner.update(0.1, i) end
T.check(s.id == 'ok' and not s.visible, '... then hidden')
T.check(banner.line() == 'BeamNG.Earth server connected', 'line(): all is well')
i.net, i.session, i.attempts = 'disconnected', false, 1
s = banner.update(0.1, i)
T.check(s.id == 'lost' and s.text:find('attempt 1', 1, true), 'connection lost: attempt 1')
i.attempts = 2
s = banner.update(0.1, i)
T.check(s.text:find('attempt 2', 1, true), 'the attempt number follows')
i.edge = 'warn'
s = banner.update(0.1, i)
T.check(s.text:find('Near the edge', 1, true), 'the guard warns: second line')
i.edge, i.net, i.session, i.attempts = nil, 'connected', true, 0
s = banner.update(0.1, i)
T.check(s.id == 'recovered', 'back: connected line')
i.note = 'offline'
s = banner.update(0.1, i)
T.check(s.id == 'offline', 'offline note wins over the connected line')
i.note = nil
s = banner.update(0.1, i)
T.check(s.id == 'ok', 'offline -> fine: no connected line (the server never left)')
-- a normal start: no "connected" line
banner._reset()
s = banner.update(0.1, info({ net = 'connected', everConnected = true, session = true }))
T.check(s.id == 'ok' and not s.visible, 'a normal start: nothing shown')

------------------------------------------------------------------------------------------
-- drawing

local draws, measured = {}, 0
local VW, VH, GLYPH, FONT = 1920, 1080, 8, 16
local function newImgui()
  return {
    GetMainViewport = function() return { Pos = { x = 0, y = 0 }, Size = { x = VW, y = VH } } end,
    GetFont = function() error("bad argument #3 to 'GetFont'") end, -- as in 0.39
    CalcTextSize = function(text)
      measured = measured + 1
      local w, n = 0, 0
      for ln in (text .. '\n'):gmatch('(.-)\n') do n = n + 1; w = math.max(w, #ln * GLYPH) end
      return { x = w, y = n * FONT }
    end,
    ImVec2 = function(x, y) return { x = x, y = y } end,
    ImVec4 = function(r, g, b, a) return { r, g, b, a } end,
    GetColorU322 = function(v) return string.format('%.2f,%.2f,%.2f,%.2f', v[1], v[2], v[3], v[4]) end,
    GetBackgroundDrawList1 = function() return 'bg' end,
    ImDrawList_AddRectFilled = function(dl, a, b, col) draws[#draws + 1] = { kind = 'rect', a = { x = a.x, y = a.y }, b = { x = b.x, y = b.y }, col = col } end,
    ImDrawList_AddText1 = function(dl, p, col, text) draws[#draws + 1] = { kind = 'text', x = p.x, y = p.y, col = col, text = text } end,
  }
end
ui_imgui = newImgui()
banner._reset()
local lostInfo = info({ everConnected = true, attempts = 3 })
banner.update(0.1, lostInfo)
draws = {}
T.check(banner.draw(true, false), 'lost: drawn')
T.check(#draws == 2 and draws[1].kind == 'rect' and draws[2].kind == 'text', 'one backdrop + one text')
local txt = 'Lost the BeamNG.Earth server on this PC - reconnecting (attempt 3). Is its launcher window still open?'
T.near(draws[2].x, (VW - #txt * GLYPH) / 2, 1e-9, 'centred horizontally')
T.near(draws[2].y, banner.TOP + banner.PAD_Y, 1e-9, 'at the top')
T.check(draws[1].col == '0.42,0.28,0.02,0.86', 'warn level: amber backdrop')
T.check(draws[2].text == txt, 'the state text')
for _ = 1, 10 do banner.update(0.1, lostInfo); banner.draw(true, false) end
T.check(banner.measures == 1, 'measured once for an unchanged text (11 frames)')
draws = {}
T.check(not banner.draw(false, false) and #draws == 0, 'outside our world: not drawn')
T.check(not banner.draw(true, true) and #draws == 0, 'pause menu / big map: not drawn')
banner.update(0.1, info({ net = 'connected', session = true, everConnected = true, attempts = 0 }))
for _ = 1, 50 do banner.update(0.1, info({ net = 'connected', session = true, everConnected = true })) end
T.check(not banner.draw(true, false) and #draws == 0, 'all is well: not drawn')
do
  local bad = newImgui()
  bad.ImDrawList_AddText1 = function() error('imgui broke') end
  ui_imgui = bad
  banner.update(0.1, lostInfo)
  local ok, r = pcall(banner.draw, true, false)
  T.check(ok and r == false and not banner.enabled and banner.failed:find('imgui broke', 1, true), 'an imgui error: off, nothing raised')
  T.check(not banner.draw(true, false), '... and stays off')
  ui_imgui = newImgui()
end
banner._reset()
ui_imgui = nil
banner.update(0.1, lostInfo)
T.check(not banner.draw(true, false), 'no imgui: nothing (no error)')

------------------------------------------------------------------------------------------
-- per-frame cost: no allocation while nothing changes (update + draw), counted in the interpreter

local VP = { Pos = { x = 0, y = 0 }, Size = { x = 1920, y = 1080 } }
local SIZE = { x = 400, y = 16 }
ui_imgui = {
  GetMainViewport = function() return VP end, CalcTextSize = function() return SIZE end,
  ImVec2 = function(x, y) return { x = x, y = y } end, ImVec4 = function() return 0 end,
  GetColorU322 = function() return 0 end, GetBackgroundDrawList1 = function() return 'bg' end,
  ImDrawList_AddRectFilled = function() end, ImDrawList_AddText1 = function() end,
}
banner._reset()
for _ = 1, 100 do banner.update(0.1, lostInfo); banner.draw(true, false) end
if jit then jit.off(); jit.flush() end
collectgarbage('collect')
collectgarbage('stop')
local kb0 = collectgarbage('count')
for _ = 1, 10000 do banner.update(0.016, lostInfo); banner.draw(true, false) end
local grown = (collectgarbage('count') - kb0) * 1024
collectgarbage('restart')
T.check(grown < 1024, 'shown and unchanged: no per-frame allocation (' .. grown .. ' bytes over 10000 frames)')
local okInfo = info({ net = 'connected', session = true, everConnected = true })
for _ = 1, 100 do banner.update(0.1, okInfo) end
collectgarbage('collect')
collectgarbage('stop')
kb0 = collectgarbage('count')
for _ = 1, 10000 do banner.update(0.016, okInfo); banner.draw(true, false) end
grown = (collectgarbage('count') - kb0) * 1024
collectgarbage('restart')
T.check(grown < 1024, 'all is well: no per-frame allocation (' .. grown .. ' bytes)')
if jit then jit.on() end

-- a car dropped far away and held until its ground loads (drophold.lua): its own line, info level
id, level, text = banner.compute(info({ net = 'connected', session = true, everConnected = true, dropHold = true }), 100)
T.check(id == 'drophold' and level == 'info' and text == banner.DROP_HOLD_TEXT, 'drop hold: its line ' .. tostring(id) .. ' ' .. tostring(text))


-- what a held car waits on: the downloads in flight (loadprogress.downloadText), under the hold's line
local dl = 'downloading 12.4 MB · 1.1 MB/s'
id, level, text = banner.compute(info({ net = 'connected', everConnected = true, session = true, edge = 'hold',
  stuck = true, download = dl }), 100)
T.check(id == 'edge' and text:find('\nThe world is ' .. dl, 1, true) ~= nil, 'edge hold: its downloads ' .. tostring(text))
id, _, text = banner.compute(info({ net = 'connected', everConnected = true, session = true, dropHold = true,
  download = dl }), 100)
T.check(text == banner.DROP_HOLD_TEXT .. '\nThe world is ' .. dl, 'drop hold: its downloads ' .. tostring(text))
id, _, text = banner.compute(info({ net = 'connected', everConnected = true, session = true, download = dl }), 100)
T.check(id == 'ok' and text == '', 'no hold: no download line (the loading line has it)')

T.done('test_banner')
