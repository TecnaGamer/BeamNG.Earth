-- On-screen connection banner (docs/ROADMAP.md "Robustness" 1, docs/DEVTOOLS.md "Connection banner"):
-- never silent. One or two lines at the top centre of the screen say what is wrong with the world
-- server and what the player can do; hidden while all is well. Engine-only drawing, pure state
-- machine (runs under plain luajit). core.lua feeds it once per frame (update) and draws it (draw);
-- the settings page shows the same line (line()).
--
-- States (compute), from the connection (net.lua), the server's hello and its status note:
--   starting   no connection yet, for the first STARTING_S after the mod loaded (or connected,
--              waiting for the server's hello)
--   noserver   never connected since the mod loaded: the command that starts the server
--   lost       the connection dropped: reconnecting, with the attempt number
--   offline    the server runs without internet ("Offline - using cached data", server netfetch)
--   degraded   the server's "Degraded: ..." note (hosts unreachable, tiles without imagery)
--   recovered  for RECOVERED_S after a lost / missing server came back ("BeamNG.Earth server connected")
--   ok         hidden
-- The edge guard (edgeguard.lua) adds a second line while it warns or holds the car at the edge of
-- the loaded ground, and raises the level.
--
-- Cost: nothing is built per frame unless an input changed (the text is cached by its inputs); one
-- background draw-list rectangle and one AddText1 while visible, measured only when the text or the
-- viewport width changed. Hidden in the pause menu and the big map (core passes credits.hiddenByUi()).
-- An error in the imgui calls turns the banner off (logged once) instead of failing every frame.

local M = {}

M.STARTING_S = 6          -- s after the mod loaded before "starting" becomes "not running"
M.RECOVERED_S = 4         -- s the "connected" line stays after the server came back
M.TOP = 36                -- px from the top of the viewport
M.PAD_X, M.PAD_Y = 12, 6  -- px of backdrop around the text
M.START_CMD = 'python3 server/earthd.py'
M.TEXT_RGBA = { 1, 1, 1, 0.95 }
M.BACK_RGBA = {
  info = { 0.10, 0.16, 0.26, 0.82 },
  ok = { 0.07, 0.30, 0.12, 0.82 },
  warn = { 0.42, 0.28, 0.02, 0.86 },
  error = { 0.48, 0.07, 0.05, 0.88 },
}
M.LEVEL_RANK = { ok = 0, info = 1, warn = 2, error = 3 }

M.enabled = true

local clock = 0             -- s since the mod loaded (update's dt)
local cur = { id = 'starting', level = 'info', text = '', visible = true }
M.DROP_HOLD_TEXT = 'Waiting for collision to load here: the car is held in place'
local key = nil             -- the inputs the current text was built from
local recoveredUntil = nil  -- clock until which 'recovered' shows
local lastProblem = nil     -- the last non-ok connection state ('lost', 'noserver', ...)
local layout = nil          -- { text, vw, w, h }: the measured text
local textU32, backU32 = nil, {}
local vecs = nil
M.draws = 0
M.measures = 0

-- The server's network note inside a status text (earthd: "<text> | <note>", or the note alone):
-- 'offline' (nil detail) or 'degraded' + its detail, else nil.
function M.noteOf(text)
  if type(text) ~= 'string' then return nil end
  if text:find('Offline - using cached data', 1, true) then return 'offline' end
  local d = text:match('^Degraded: (.*)$') or text:match(' | Degraded: (.*)$')
  if d then return 'degraded', d end
  return nil
end

local EDGE_TEXT = {
  warn = 'Near the edge of the loaded ground: the car will be held there until the BeamNG.Earth server %s',
  hold = 'Paused at the edge of the loaded ground until the BeamNG.Earth server %s (resume to drive back)',
}

-- The state for a set of inputs (pure):
--   info.net         'connected' | 'connecting' | 'disconnected'
--   info.session     the server's hello arrived (a session id)
--   info.everConnected, info.attempts (net.lua)
--   info.note, info.noteDetail  (noteOf of the server's last status)
--   info.download    nil | the downloads in flight ('downloading 14 files · 1.1 MB/s', loadprogress.downloadText)
--   info.edge        nil | 'warn' | 'hold' (edgeguard), info.stuck: the server does not answer
--                    terrain requests (rather than: no server)
--   t                s since the mod loaded
-- Returns id, level, text ('' for ok).
function M.compute(info, t)
  local id, level, text
  if info.net ~= 'connected' then
    if not info.everConnected then
      if (t or 0) < M.STARTING_S then
        id, level, text = 'starting', 'info', 'BeamNG.Earth: looking for the BeamNG.Earth server on this PC (127.0.0.1:' .. tostring(info.port or 47613) .. ') ...'
      else
        id, level = 'noserver', 'error'
        text = 'BeamNG.Earth server not running on this PC - start BeamNG.Earth with its launcher (or:  '
          .. M.START_CMD .. ')'
      end
    else
      id, level = 'lost', 'warn'
      -- the program on this PC, not the internet (the user, 2026-10-17: "make this more clear if it's the server or
      -- a data provider that's not connected"; data sources are the offline / degraded lines below)
      text = 'Lost the BeamNG.Earth server on this PC - reconnecting (attempt ' .. tostring(math.max(1, info.attempts or 1))
        .. '). Is its launcher window still open?'
    end
  elseif not info.session then
    id, level, text = 'starting', 'info', 'Connected - waiting for the BeamNG.Earth server ...'
  elseif info.note == 'offline' then
    id, level, text = 'offline', 'warn', 'Server OK, internet data sources unreachable - using cached data (areas never visited have no terrain)'
  elseif info.note == 'degraded' then
    id, level, text = 'degraded', 'warn', 'Server OK, some internet data sources not answering: ' .. tostring(info.noteDetail or '')
  else
    id, level, text = 'ok', 'ok', ''
  end
  local e = info.edge and EDGE_TEXT[info.edge]
  if e then
    local why = (info.stuck and info.net == 'connected' and info.session) and 'answers terrain requests again' or 'is back'
    local line = string.format(e, why)
    text = (text ~= '') and (text .. '\n' .. line) or line
    local want = info.edge == 'hold' and 'error' or 'warn'
    if M.LEVEL_RANK[want] > M.LEVEL_RANK[level] then level = want end
    if id == 'ok' then id = 'edge' end
  end
  if info.dropHold then         -- drophold.lua: a car dropped far away waits for its ground (the user, 2026-10-18)
    text = (text ~= '') and (text .. '\n' .. M.DROP_HOLD_TEXT) or M.DROP_HOLD_TEXT
    if M.LEVEL_RANK.info > M.LEVEL_RANK[level] then level = 'info' end
    if id == 'ok' then id = 'drophold' end
  end
  if info.download and (info.edge or info.dropHold) then   -- (what the held car waits on: loadprogress.downloadText)
    local line = 'The world is ' .. info.download
    text = (text ~= '') and (text .. '\n' .. line) or line
  end
  return id, level, text
end

local KEYS = { 'net', 'session', 'everConnected', 'attempts', 'note', 'noteDetail', 'edge', 'stuck', 'dropHold' }

-- Did any input change since the text was built? (Compared field by field: nothing allocated.)
local function changed(info)
  if not key then return true end
  for _, k in ipairs(KEYS) do
    if key[k] ~= info[k] then return true end
  end
  return key.starting ~= (clock < M.STARTING_S) or (recoveredUntil ~= nil and clock >= recoveredUntil)
end

-- Once per frame: dt (s), info as for compute. Returns the current state { id, level, text, visible }.
-- The text is rebuilt only when an input changed.
function M.update(dt, info)
  clock = clock + math.max(0, dt or 0)
  if not changed(info) then return cur end
  local id, level, text = M.compute(info, clock)
  if id == 'ok' then
    if lastProblem == 'lost' or lastProblem == 'noserver' then
      recoveredUntil = clock + M.RECOVERED_S
    end
    lastProblem = nil
    if recoveredUntil and clock < recoveredUntil then
      id, level, text = 'recovered', 'ok', 'BeamNG.Earth server connected'
    else
      recoveredUntil = nil
    end
  elseif id ~= 'edge' and id ~= 'starting' and id ~= 'drophold' then
    lastProblem = id
    recoveredUntil = nil
  end
  key = { starting = clock < M.STARTING_S }
  for _, k in ipairs(KEYS) do key[k] = info[k] end
  cur = { id = id, level = level, text = text, visible = text ~= '' }
  return cur
end

function M.current() return cur end

-- The settings page's state line: the banner's text, or what all-is-well means.
function M.line()
  if cur.text ~= '' then return cur.text end
  return 'BeamNG.Earth server connected'
end

local function u32(im, rgba) return im.GetColorU322(im.ImVec4(rgba[1], rgba[2], rgba[3], rgba[4])) end

-- Top-left of a w x h text block centred at the top of the viewport (position px, py; width vw).
function M.place(px, py, vw, w)
  return px + (vw - w) / 2, py + M.TOP + M.PAD_Y
end

local function drawImgui(im, state)
  local vp = im.GetMainViewport()
  if not vp then return false end
  local vw = vp.Size.x
  local L = layout
  if not L or L.text ~= state.text or L.vw ~= vw then
    local ts = im.CalcTextSize(state.text)
    L = { text = state.text, vw = vw, w = ts.x, h = ts.y }
    layout = L
    M.measures = M.measures + 1
  end
  if not textU32 then textU32 = u32(im, M.TEXT_RGBA) end
  local back = backU32[state.level]
  if not back then back = u32(im, M.BACK_RGBA[state.level] or M.BACK_RGBA.info); backU32[state.level] = back end
  if not vecs then vecs = { im.ImVec2(0, 0), im.ImVec2(0, 0), im.ImVec2(0, 0) } end
  local x, y = M.place(vp.Pos.x, vp.Pos.y, vw, L.w)
  local p, a, b = vecs[1], vecs[2], vecs[3]
  p.x, p.y = x, y
  a.x, a.y = x - M.PAD_X, y - M.PAD_Y
  b.x, b.y = x + L.w + M.PAD_X, y + L.h + M.PAD_Y
  local dl = im.GetBackgroundDrawList1() -- as credits.lua: over the 3D view, under imgui windows
  im.ImDrawList_AddRectFilled(dl, a, b, back, 4)
  -- AddText1: the UI font at its size (GetFont() fails in 0.39, see credits.lua)
  im.ImDrawList_AddText1(dl, p, textU32, L.text, nil)
  return true
end

-- Once per frame. show: our world (core: in our level or active); hidden: the pause menu / big map.
-- Returns true if drawn.
function M.draw(show, hidden)
  if not M.enabled or not show or hidden or not cur.visible then return false end
  local im = rawget(_G, 'ui_imgui')
  if not im or not im.GetMainViewport then return false end
  local ok, drawn = pcall(drawImgui, im, cur)
  if not ok then
    M.enabled, M.failed = false, tostring(drawn)
    if log then log('E', 'beamngEarth.banner', 'connection banner turned off: ' .. M.failed) end
    return false
  end
  if drawn then M.draws = M.draws + 1 end
  return drawn
end

-- For tests: a fresh state machine and imgui caches.
function M._reset()
  clock, key, recoveredUntil, lastProblem = 0, nil, nil, nil
  cur = { id = 'starting', level = 'info', text = '', visible = true }
  layout, textU32, backU32, vecs = nil, nil, {}, nil
  M.draws, M.measures, M.enabled, M.failed = 0, 0, true, nil
end

return M
