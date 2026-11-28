-- On-screen data credits (docs/DEVTOOLS.md "Data credits overlay"): a small, semi-transparent text in
-- a screen corner naming the sources that built what is around the player right now (CC BY and
-- similar licences ask for visible attribution, so it is on by default). Engine-only module; core.lua
-- feeds it the server's 'credits' messages and calls draw() once per frame.
--
-- The server (server/credits.py) sends { near = 'Imagery: ... | Elevation: ... | Weather data by
-- Open-Meteo.com', far = 'Far imagery: ...' or '' } whenever the sources of the tiles within 2 km
-- of the player (or of the far field) change, at most once a second. Before the first one (or from
-- an older server) the session's attribution line is shown instead (core's attributionText).
--
-- Cost: no window. One background draw-list rectangle and one AddText1 per frame; the text is
-- wrapped and measured (CalcTextSize) only when the text, the font size or the viewport width changed. Hidden when
-- off, outside our world, while the pause menu ('menu*' UI states, onUiChangedState via core) or the
-- big map is open.
-- Settings (settings.lua): creditsVisible, creditsCorner, creditsSize.

local M = {}

M.CORNERS = { 'bottom-left', 'bottom-right', 'top-left', 'top-right' }
M.SIZE_PX = { small = 11, normal = 14 }
M.MARGIN = 8              -- px from the screen edges
M.PAD = 3                 -- px of backdrop around the text
M.WRAP_FRACTION = 0.6     -- the text wraps at this fraction of the screen width ...
M.MIN_WRAP = 320          -- ... but never narrower (px)
M.TEXT_RGBA = { 1, 1, 1, 0.8 }
M.BACK_RGBA = { 0, 0, 0, 0.3 }

M.MAP_ROUTE = 'beamngEarthMap' -- our world map's UI state (settings.lua MAP_ROUTE)

M.enabled = true
M.corner = 'bottom-left'
M.size = 'small'

local joined = nil        -- near (+ '\n' + far) of the last credits message, nil before one
local near, far = nil, nil
local uiHidden = false    -- the game's UI state (onUiChangedState) hides the credits
local layout = nil        -- { text, vw, fsz, wrap, wrapped, w, h }: the measured text
local textU32, backU32 = nil, nil
local vecs = nil          -- reused ImVec2 (text position, backdrop corners)
M.draws = 0               -- frames drawn (tests, the dev stats)
M.measures = 0            -- CalcTextSize calls

local function nonEmpty(s) return type(s) == 'string' and s ~= '' end

-- Credit lines of features other than the world's tiles (setExtra: the live air traffic's sources), after them.
local extras, extraKeys = {}, {}
local function withExtras(base)
  if #extraKeys == 0 then return base end
  local parts = {}
  for _, k in ipairs(extraKeys) do parts[#parts + 1] = extras[k] end
  local ex = table.concat(parts, ' | ')
  if nonEmpty(base) then return base .. '\n' .. ex end
  return ex
end

-- key: who it is ('airtraffic'); text: its credit line, nil / '' to take it away.
function M.setExtra(key, text)
  if type(key) ~= 'string' then return end
  local had = extras[key]
  extras[key] = nonEmpty(text) and text or nil
  if had == extras[key] then return end
  extraKeys = {}
  for k in pairs(extras) do extraKeys[#extraKeys + 1] = k end
  table.sort(extraKeys)
  layout = nil
end
function M.extraText() return withExtras(nil) end

-- A 'credits' message from the server. Returns true if it was usable.
function M.onMessage(msg)
  if type(msg) ~= 'table' or type(msg.near) ~= 'string' then return false end
  near = msg.near
  far = nonEmpty(msg.far) and msg.far or nil
  if nonEmpty(near) and far then joined = near .. '\n' .. far
  else joined = nonEmpty(near) and near or far end
  return true
end

-- A new server session: its own credits follow (until then the attribution line).
function M.clear()
  joined, near, far = nil, nil, nil
end

-- The credits text from the server, or nil before one arrived.
function M.serverText() return joined end

-- The text shown: the server's credits, else `fallback` (a string, or a function returning one).
function M.text(fallback)
  if joined then return withExtras(joined) end
  if type(fallback) == 'function' then return withExtras(fallback()) end
  return withExtras(fallback)
end

function M.setEnabled(on) M.enabled = on and true or false; return M.enabled end

function M.setCorner(c)
  for _, v in ipairs(M.CORNERS) do
    if v == c then M.corner = c; return c end
  end
  return M.corner
end

function M.setSize(s)
  if M.SIZE_PX[s] then M.size = s; layout = nil end
  return M.size
end

-- core.lua's onUiChangedState hook: the pause menu is the 'menu' state (and its sub-states), a
-- loading screen 'loading'. Decided here, once per change, not per frame.
function M.setUiState(toState)
  uiHidden = type(toState) == 'string' and (toState:sub(1, 4) == 'menu' or toState == 'loading'
    or toState == M.MAP_ROUTE)
end

-- Hidden by the game's UI: the pause menu, a loading screen, the big map, our world map (EarthMap.vue has its own
-- credits line; the two are never on screen together, the user 2026-10-14).
function M.hiddenByUi()
  if uiHidden then return true end
  local st = rawget(_G, 'beamngEarth_settings')
  if st and st.isMapOpen then
    local ok, on = pcall(st.isMapOpen)
    if ok and on then return true end
  end
  local bm = rawget(_G, 'freeroam_bigMapMode')
  if bm and bm.bigMapActive then
    local ok, on = pcall(bm.bigMapActive)
    if ok and on then return true end
  end
  return false
end

local function u32(im, rgba) return im.GetColorU322(im.ImVec4(rgba[1], rgba[2], rgba[3], rgba[4])) end

-- Top-left screen position of a w x h text block in the corner (viewport position px, py; size vw, vh).
function M.place(corner, px, py, vw, vh, w, h)
  local m = M.MARGIN + M.PAD
  local x = (corner == 'bottom-right' or corner == 'top-right') and (px + vw - m - w) or (px + m)
  local y = (corner == 'top-left' or corner == 'top-right') and (py + m) or (py + vh - m - h)
  return x, y
end

-- The text wrapped at `wrap` px by inserting line breaks between words (AddText1 does not wrap).
-- Called only when the layout changes.
local function wrapText(im, text, wrap)
  local out = {}
  for para in (text .. '\n'):gmatch('(.-)\n') do
    local line = ''
    for word in para:gmatch('%S+') do
      local cand = (line == '') and word or (line .. ' ' .. word)
      if line ~= '' and im.CalcTextSize(cand).x > wrap then
        out[#out + 1] = line
        line = word
      else
        line = cand
      end
    end
    out[#out + 1] = line
  end
  return table.concat(out, '\n')
end

local function drawImgui(im, text)
  local vp = im.GetMainViewport()
  if not vp then return false end
  local fsz = im.GetFontSize()
  if not fsz or fsz <= 0 then return false end
  local vw, vh = vp.Size.x, vp.Size.y
  local L = layout
  if not L or L.text ~= text or L.vw ~= vw or L.fsz ~= fsz then
    local wrap = math.max(M.MIN_WRAP, vw * M.WRAP_FRACTION)
    local wrapped = wrapText(im, text, wrap)
    local ts = im.CalcTextSize(wrapped)
    L = { text = text, vw = vw, fsz = fsz, wrap = wrap, wrapped = wrapped, w = ts.x, h = ts.y }
    layout = L
    M.measures = M.measures + 1
  end
  if not textU32 then textU32, backU32 = u32(im, M.TEXT_RGBA), u32(im, M.BACK_RGBA) end
  if not vecs then vecs = { im.ImVec2(0, 0), im.ImVec2(0, 0), im.ImVec2(0, 0) } end
  local x, y = M.place(M.corner, vp.Pos.x, vp.Pos.y, vw, vh, L.w, L.h)
  local p, a, b = vecs[1], vecs[2], vecs[3]
  p.x, p.y = x, y
  a.x, a.y = x - M.PAD, y - M.PAD
  b.x, b.y = x + L.w + M.PAD, y + L.h + M.PAD
  local dl = im.GetBackgroundDrawList1() -- under every imgui window, over the 3D view
  im.ImDrawList_AddRectFilled(dl, a, b, backU32, 3)
  -- AddText1 (the current font at its size), as the game's own editor overlays draw text: the
  -- binding's GetFont() fails in 0.39 ("bad argument #3 to 'GetFont'", in-game 2026-10-11), so
  -- AddText2 with a font and a size cannot be used; the size setting waits for another way.
  im.ImDrawList_AddText1(dl, p, textU32, L.wrapped, nil)
  return true
end

-- Once per frame. active: our world is shown (core's st.active); fallback: the attribution line
-- (string or function, only called while no credits message arrived). Returns true if drawn.
-- An error in the imgui calls turns the overlay off (logged once) instead of failing every frame.
function M.draw(active, fallback)
  if not M.enabled or not active or M.hiddenByUi() then return false end
  local text = joined
  if not text then
    if type(fallback) == 'function' then text = fallback() else text = fallback end
  end
  text = withExtras(text)
  if not nonEmpty(text) then return false end
  local im = rawget(_G, 'ui_imgui')
  if not im or not im.GetMainViewport then return false end
  local ok, drawn = pcall(drawImgui, im, text)
  if not ok then
    M.enabled, M.failed = false, tostring(drawn)
    if log then log('E', 'beamngEarth.credits', 'data credits overlay turned off: ' .. M.failed) end
    return false
  end
  if drawn then M.draws = M.draws + 1 end
  return drawn
end

-- For tests: forget the cached measurement and colours (a new imgui stub).
function M._reset()
  layout, textU32, backU32, vecs, uiHidden = nil, nil, nil, nil, false
  M.draws, M.measures = 0, 0
end

return M
