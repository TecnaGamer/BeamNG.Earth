-- luajit mod/tests/test_credits.lua  (the on-screen data credits, credits.lua; docs/DEVTOOLS.md)
-- Messages (usable / not), the attribution fallback, one backdrop + one text draw per frame on the
-- background draw list, the four corners and two sizes, the measurement cached until the text, size
-- or screen width changes, hidden when off / outside our world / in the pause menu or big map /
-- without imgui, no allocation per frame, and the settings that drive it (defaults on, bottom-left,
-- small; persisted).
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local credits = require('beamngEarth/credits')

------------------------------------------------------------------------------------------
-- an imgui stub: a 1920 x 1080 viewport at (0, 0), a 16 px font whose glyphs are 8 px wide
-- (CalcTextSize honours the wrap width per line), every draw recorded

local draws, measured = {}, {}
local VW, VH, FONT_PX, GLYPH = 1920, 1080, 16, 8
local function lines(text, wrap)
  local n, w = 0, 0
  for ln in (text .. '\n'):gmatch('(.-)\n') do
    local lw = #ln * GLYPH
    local rows = (wrap and wrap > 0) and math.max(1, math.ceil(lw / wrap)) or 1
    n = n + rows
    w = math.max(w, math.min(lw, (wrap and wrap > 0) and wrap or lw))
  end
  return w, n
end
local function newImgui()
  return {
    GetMainViewport = function() return { Pos = { x = 0, y = 0 }, Size = { x = VW, y = VH } } end,
    GetFont = function() error("bad argument #3 to 'GetFont' (table expected, got nil)") end, -- as in 0.39
    GetFontSize = function() return FONT_PX end,
    CalcTextSize = function(text, _, _, wrap)
      measured[#measured + 1] = { text = text, wrap = wrap }
      local w, n = lines(text, wrap)
      return { x = w, y = n * FONT_PX }
    end,
    ImVec2 = function(x, y) return { x = x, y = y } end,
    ImVec4 = function(r, g, b, a) return { r, g, b, a } end,
    GetColorU322 = function(v) return string.format('%.2f,%.2f,%.2f,%.2f', v[1], v[2], v[3], v[4]) end,
    GetBackgroundDrawList1 = function() return 'bg' end,
    ImDrawList_AddRectFilled = function(dl, a, b, col, r)
      draws[#draws + 1] = { kind = 'rect', dl = dl, a = { x = a.x, y = a.y }, b = { x = b.x, y = b.y }, col = col }
    end,
    ImDrawList_AddText1 = function(dl, pos, col, text, textEnd)
      draws[#draws + 1] = { kind = 'text', dl = dl, x = pos.x, y = pos.y, col = col, text = text }
    end,
  }
end
ui_imgui = newImgui()

local function frame(active, fallback)
  draws = {}
  return credits.draw(active, fallback)
end
local function textDraw() for _, d in ipairs(draws) do if d.kind == 'text' then return d end end end

------------------------------------------------------------------------------------------
-- defaults and messages

T.check(credits.enabled and credits.corner == 'bottom-left' and credits.size == 'small', 'defaults: on, bottom-left, small')
T.check(credits.SIZE_PX.small == 11, 'small = 11 px')
T.check(credits.serverText() == nil, 'no message yet')
T.check(not credits.onMessage({ type = 'credits' }) and not credits.onMessage(nil), 'a message without near is refused')
T.check(credits.serverText() == nil, '... and changes nothing')

-- before any credits message: the session's attribution line (a function, called only then)
local fbCalls = 0
local function fallback() fbCalls = fbCalls + 1; return 'Imagery: everything; Elevation: everything' end
T.check(frame(true, fallback) and textDraw().text == 'Imagery: everything; Elevation: everything',
  'no credits yet: the attribution line')
T.check(fbCalls == 1, 'the fallback is asked for once per frame while there is no message')

local NEAR = 'Imagery: USGS The National Map | Elevation: Mapterhorn (see mapterhorn.com/attribution) | Weather data by Open-Meteo.com'
local FAR = 'Far imagery: EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2016 & 2017)'
T.check(credits.onMessage({ type = 'credits', v = 1, near = NEAR, far = FAR, ids = {}, farIds = {} }), 'credits message taken')
T.check(credits.serverText() == NEAR .. '\n' .. FAR, 'near and far on two lines')
fbCalls = 0
T.check(frame(true, fallback), 'drawn')
T.check(fbCalls == 0, 'with credits from the server the fallback is never called')
T.check(#draws == 2 and draws[1].kind == 'rect' and draws[2].kind == 'text', 'one backdrop + one text draw per frame')
local t = textDraw()
T.check(t.dl == 'bg' and draws[1].dl == 'bg', 'on the background draw list (under every imgui window)')
T.check(t.text == NEAR .. '\n' .. FAR, 'the server\'s text (AddText1: the UI font at its size; GetFont() fails in 0.39)')
T.check(t.col == '1.00,1.00,1.00,0.80' and draws[1].col == '0.00,0.00,0.00,0.30', 'semi-transparent text on a faint backdrop')
credits.onMessage({ type = 'credits', near = NEAR, far = '' })
T.check(credits.serverText() == NEAR, 'an empty far part: one line')
credits.onMessage({ type = 'credits', near = '', far = FAR })
T.check(credits.serverText() == FAR, 'only a far part: that line')
credits.onMessage({ type = 'credits', near = NEAR, far = FAR })

------------------------------------------------------------------------------------------
-- layout: corners, measuring only on change, wrapping by inserted line breaks

local m = credits.MARGIN + credits.PAD
local w = math.max(#NEAR, #FAR) * GLYPH
local h = 2 * FONT_PX
credits._reset()
frame(true)
t = textDraw()
T.near(t.x, m, 1e-9, 'bottom-left: x')
T.near(t.y, VH - m - h, 1e-9, 'bottom-left: y (two lines above the bottom edge)')
T.near(draws[1].a.x, t.x - credits.PAD, 1e-9, 'backdrop around the text (left)')
T.near(draws[1].b.y, t.y + h + credits.PAD, 1e-9, 'backdrop around the text (bottom)')
T.check(credits.measures == 1, 'measured once')
for _ = 1, 10 do frame(true) end
T.check(credits.measures == 1, 'unchanged text: not measured again (10 frames)')
T.check(credits.setCorner('top-right') == 'top-right', 'corner top-right')
frame(true)
t = textDraw()
T.near(t.x, VW - m - w, 1e-9, 'top-right: x (right-aligned block)')
T.near(t.y, m, 1e-9, 'top-right: y')
credits.setCorner('top-left'); frame(true); t = textDraw()
T.check(math.abs(t.x - m) < 1e-9 and math.abs(t.y - m) < 1e-9, 'top-left')
credits.setCorner('bottom-right'); frame(true); t = textDraw()
T.check(math.abs(t.x - (VW - m - w)) < 1e-9 and math.abs(t.y - (VH - m - h)) < 1e-9, 'bottom-right')
T.check(credits.setCorner('middle') == 'bottom-right', 'an unknown corner is refused')
T.check(credits.measures == 1, 'a corner change needs no new measurement')
T.check(credits.setSize('normal') == 'normal' and credits.setSize('huge') == 'normal', 'the size setting is kept (no effect with AddText1 yet)')
credits.setSize('small')
credits.setCorner('bottom-left')
-- a narrow window wraps the text onto more lines (line breaks between words)
VW = 800
ui_imgui = newImgui()
frame(true); t = textDraw()
local nLines = select(2, t.text:gsub('\n', '\n')) + 1
T.check(credits.measures > 1 and nLines > 2, 'a resized screen: wrapped again onto ' .. nLines .. ' lines')
for ln in (t.text .. '\n'):gmatch('(.-)\n') do
  T.check(#ln * GLYPH <= math.max(credits.MIN_WRAP, 800 * credits.WRAP_FRACTION), 'each line fits the wrap width: ' .. #ln)
end
T.check(t.text:gsub('%s+', ' ') == (NEAR .. ' ' .. FAR):gsub('%s+', ' '), 'wrapping only changes the whitespace')
T.near(t.y, VH - m - nLines * FONT_PX, 1e-9, 'bottom-left: the wrapped block still sits on the bottom edge')
VW = 1920
ui_imgui = newImgui()
-- a new message: measured again
credits.onMessage({ type = 'credits', near = 'Imagery: EOX', far = '' })
local before = credits.measures
frame(true)
T.check(credits.measures == before + 1 and textDraw().text == 'Imagery: EOX', 'a new text is measured once')

-- an imgui error turns the overlay off once instead of failing every frame (in-game 2026-10-11:
-- GetFont() threw every frame and flooded the log)
do
  local good = ui_imgui
  local bad = newImgui()
  bad.ImDrawList_AddText1 = function() error('imgui broke') end
  ui_imgui = bad
  local ok, r = pcall(frame, true)
  T.check(ok and r == false and not credits.enabled and credits.failed:find('imgui broke', 1, true), 'an imgui error: off, no error raised')
  T.check(not frame(true), '... and stays off')
  ui_imgui = good
  credits.setEnabled(true)
  credits._reset()
end

------------------------------------------------------------------------------------------
-- hidden

credits.setEnabled(false)
T.check(not frame(true) and #draws == 0, 'off: nothing drawn')
credits.setEnabled(true)
T.check(not frame(false) and #draws == 0, 'outside our world: nothing drawn')
credits.setUiState('menu')
T.check(not frame(true) and #draws == 0, 'pause menu (UI state menu): hidden')
credits.setUiState('menu.options.graphics')
T.check(not frame(true), 'menu sub-states: hidden')
credits.setUiState('loading')
T.check(not frame(true), 'loading screen: hidden')
credits.setUiState('play')
T.check(frame(true), 'back to play: shown')
local bigMap = true
freeroam_bigMapMode = { bigMapActive = function() return bigMap end }
T.check(not frame(true) and #draws == 0, 'big map open: hidden')
bigMap = false
T.check(frame(true), 'big map closed: shown')
freeroam_bigMapMode = { bigMapActive = function() error('broken') end }
T.check(frame(true), 'a failing big-map query does not hide it (or throw)')
freeroam_bigMapMode = nil
local mapOpen = true
beamngEarth_settings = { isMapOpen = function() return mapOpen end }
T.check(not frame(true) and #draws == 0, 'our world map open (it has its own credits line): hidden')
mapOpen = false
T.check(frame(true), 'our world map closed: shown')
beamngEarth_settings = nil
credits.setUiState('beamngEarthMap')
T.check(not frame(true), 'the world map UI state: hidden')
credits.setUiState('play')
credits.clear()
T.check(not frame(true, nil) and #draws == 0, 'no credits and no fallback: nothing')
T.check(not frame(true, '') and #draws == 0, 'an empty fallback: nothing')
local saved = ui_imgui
ui_imgui = nil
T.check(not frame(true, 'x'), 'no imgui: nothing (no error)')
ui_imgui = { GetMainViewport = function() return nil end }
T.check(not frame(true, 'x'), 'no viewport: nothing')
ui_imgui = saved
T.check(credits.text('fb') == 'fb' and credits.text(function() return 'fn' end) == 'fn', 'text(): the fallback without credits')

------------------------------------------------------------------------------------------
-- per-frame cost: no Lua table or string per frame once measured (an imgui stub that allocates
-- nothing itself; the real one boxes a few cdata pointers, as every imgui call does). Counted in the
-- interpreter: compiling traces allocates GC objects of its own, and the JIT only removes allocations.

local VP = { Pos = { x = 0, y = 0 }, Size = { x = 1920, y = 1080 } }
local SIZE = { x = 100, y = 16 }
local nDraw = 0
ui_imgui = {
  GetMainViewport = function() return VP end,
  GetFontSize = function() return FONT_PX end, CalcTextSize = function() return SIZE end,
  ImVec2 = function(x, y) return { x = x, y = y } end, ImVec4 = function() return 0 end,
  GetColorU322 = function() return 0 end, GetBackgroundDrawList1 = function() return 'bg' end,
  ImDrawList_AddRectFilled = function() end,
  ImDrawList_AddText1 = function() nDraw = nDraw + 1 end,
}
credits._reset()
credits.onMessage({ type = 'credits', near = NEAR, far = FAR })
credits.setUiState('play')
freeroam_bigMapMode = { bigMapActive = function() return false end }
credits.draw(true)
if jit then jit.off(); jit.flush() end
collectgarbage('collect')
collectgarbage('stop')
local kb0 = collectgarbage('count')
for _ = 1, 10000 do credits.draw(true) end
local grown = (collectgarbage('count') - kb0) * 1024
collectgarbage('restart')
print(string.format('  10000 frames with credits: %d bytes allocated', grown))
T.check(nDraw == 10001 and grown < 1024, 'no per-frame allocation (' .. grown .. ' bytes over 10000 frames)')
-- the attribution fallback (no credits message yet) as core passes it: a constant string
credits.clear()
local fbText = 'Elevation: Mapterhorn'
local function fb() return fbText end
kb0 = collectgarbage('count')
collectgarbage('stop')
for _ = 1, 10000 do credits.draw(true, fb) end
grown = (collectgarbage('count') - kb0) * 1024
collectgarbage('restart')
T.check(grown < 1024, 'fallback text: no per-frame allocation either (' .. grown .. ' bytes)')
if jit then jit.on() end
freeroam_bigMapMode = nil

------------------------------------------------------------------------------------------
-- the settings that drive it

local files = {}
function jsonReadFile(path) return files[path] end
function jsonWriteFile(path, data) local c = {}; for k, v in pairs(data) do c[k] = v end; files[path] = c; return true end
FS = { fileExists = function(_, p) return files[p] ~= nil end }
local settings = require('beamngEarth/settings')
settings.load()
local d = settings.defaults()
T.check(d.creditsVisible == true and d.creditsCorner == 'bottom-left' and d.creditsSize == 'small',
  'settings defaults: on, bottom-left, small')
T.check(settings.validate('creditsCorner', 'top-right') == 'top-right' and settings.validate('creditsCorner', 'left') == nil,
  'corner: the four corners only')
T.check(settings.validate('creditsSize', 'normal') == 'normal' and settings.validate('creditsSize', 'big') == nil, 'size: small / normal')
settings.set('creditsCorner', 'top-left')
settings.set('creditsSize', 'normal')
settings.set('creditsVisible', false)
T.check(credits.corner == 'top-left' and credits.size == 'normal' and credits.enabled == false, 'set: applied to credits.lua')
T.check(files[settings.FILE].creditsCorner == 'top-left' and files[settings.FILE].creditsVisible == false, 'set: saved')
credits.setCorner('bottom-left'); credits.setSize('small'); credits.setEnabled(true)
settings.onExtensionLoaded()
T.check(credits.corner == 'top-left' and credits.size == 'normal' and credits.enabled == false, 'load: the saved values applied')
settings.uiReset()
T.check(credits.corner == 'bottom-left' and credits.size == 'small' and credits.enabled == true, 'reset: back to the defaults')
beamngEarth_core = { _state = function() return {} end, attribution = function() return 'a' end,
  credits = function() return 'Imagery: EOX' end }
T.check(settings.uiState().status.credits == 'Imagery: EOX', 'uiState: the credits on screen for the page')
beamngEarth_core = nil

T.done('test_credits')
