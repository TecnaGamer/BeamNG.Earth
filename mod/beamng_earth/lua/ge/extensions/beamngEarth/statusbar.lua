-- A slim status bar at the top of the screen (the user, 2026-10-15: "replace the current status lua window with a
-- status bar at the top of the screen, like BeamMP ... level loading / performance and location info like street,
-- city, country ... simple and precise"). One line, top centre:
--   Thames Street  |  Baltimore, Maryland, United States  |  Loading 120/433
-- The street comes from the server's map data (geocode.nearest_street), the town / state / country from Photon's
-- reverse geocoding with the position rounded to ~1 km (setting placeNamesOnline); asked with a 'where' message when
-- the car moved WHERE_STREET_M (street) / WHERE_PLACE_M (place), at most every WHERE_MIN_S. Loading shows while the
-- world fills in. Drawn like credits.lua (a background draw-list rectangle and one text, measured only when the text
-- or the viewport changed); hidden in the pause menu and on the map (core passes credits.hiddenByUi()).
local M = {}
M.JUMP_M = 2000      -- a move this far between requests is a teleport: the old place is cleared

M.enabled = true
M.TOP = 6                  -- px from the top of the viewport
M.PAD_X, M.PAD_Y = 10, 4
M.SEP = '  |  '
M.ROW_GAP = 3              -- px between the location row and the loading row
M.TEXT_RGBA = { 1, 1, 1, 0.92 }
M.BACK_RGBA = { 0.05, 0.06, 0.08, 0.55 }
M.WHERE_STREET_M = 30
M.WHERE_PLACE_M = 1500
M.WHERE_MIN_S = 2.0
M.WHERE_PLACE_S = 120
M.FPS_ALPHA = 0.05

local loc = { street = '', place = '', region = '', country = '' }
local last = nil           -- { x, y, t, px, py, pt } of the last request (street / place)
local nextId = 0
local fps = nil
M.SHOW_FPS = false     -- (the user, 2026-10-15: Steam has an fps overlay, we do not need our own counter)
local textU32, backU32, vecs = nil, nil, nil
M.draws, M.measures, M.requests = 0, 0, 0

-- Pure: the bar's text from the location, the loading line (nil when loaded) and the fps (nil: unknown).
function M.compose(l, loading, f)
  local parts = {}
  if l and l.street ~= nil and l.street ~= '' then parts[#parts + 1] = l.street end
  local where = {}
  for _, k in ipairs({ 'place', 'region', 'country' }) do
    local v = l and l[k]
    if v ~= nil and v ~= '' and v ~= where[#where] then where[#where + 1] = v end
  end
  if #where > 0 then parts[#parts + 1] = table.concat(where, ', ') end
  if loading and loading ~= '' then parts[#parts + 1] = loading end
  if f then parts[#parts + 1] = string.format('%d fps', math.floor(f + 0.5)) end
  return table.concat(parts, M.SEP)
end

-- Pure: the bar's short loading line from core's status text, nil once the world is ready.
function M.loadingLine(status)
  if type(status) ~= 'string' then return nil end
  local done, total = status:match('loading:%s*(%d+)/(%d+)')
  if done then return string.format('Loading %s/%s', done, total) end
  if status:find('Preparing terrain') or status:find('Building terrain') or status:find('Elevation tiles') then
    return 'Building terrain'
  end
  return nil
end

-- Every frame: the fps average (frameMs: the frame's wall time).
function M.frame(frameMs)
  if not frameMs or frameMs <= 0 then return end
  local f = 1000 / frameMs
  fps = fps and (fps + M.FPS_ALPHA * (f - fps)) or f
end

-- Every frame: the 'where' request due now, or nil. now: s; player {x, y, lat, lon}; online: place names allowed.
-- Pure: metres between two lat/lon points (equirectangular: plenty for "has the car moved 50 m").
function M.dist(lat1, lon1, lat2, lon2)
  local k = 111320
  local dx = (lon2 - lon1) * k * math.cos(math.rad((lat1 + lat2) / 2))
  return math.sqrt(dx * dx + ((lat2 - lat1) * k) ^ 2)
end

function M.request(now, player, online)
  if not M.enabled or not player or not player.lat then return nil end
  -- moved: in lat / lon, not the anchor frame's x, y (a teleport lands at x, y ~ 0 again: the bar kept the old
  -- place's street, "John Muir Trail" in Provence, 2026-10-15; a rebase shifts x, y too)
  local moved = last and M.dist(last.lat, last.lon, player.lat, player.lon) or math.huge
  if last and (now - last.t < M.WHERE_MIN_S or moved < M.WHERE_STREET_M) then return nil end
  local placeDue = online and (not last or not last.pt or now - last.pt >= M.WHERE_PLACE_S
    or M.dist(last.plat, last.plon, player.lat, player.lon) >= M.WHERE_PLACE_M)
  last = { lat = player.lat, lon = player.lon, t = now, plat = last and last.plat, plon = last and last.plon,
    pt = last and last.pt }
  if placeDue then last.plat, last.plon, last.pt = player.lat, player.lon, now end
  -- a jump (a teleport): the old street and place go at once
  if moved ~= math.huge and moved > M.JUMP_M then loc = { street = '', place = '', region = '', country = '' } end
  nextId = nextId + 1
  M.requests = M.requests + 1
  return { type = 'where', id = nextId, lat = player.lat, lon = player.lon, online = placeDue and true or false }
end

-- The server's where_result: the street always, the place parts when it sent them.
function M.onResult(msg)
  if type(msg) ~= 'table' then return end
  if msg.street ~= nil then loc.street = tostring(msg.street) end
  if msg.place ~= nil or msg.country ~= nil then
    loc.place, loc.region, loc.country = msg.place or '', msg.region or '', msg.country or ''
  end
end

function M.location() return loc end

-- A teleport or a new level: the old place goes.
function M.reset()
  loc = { street = '', place = '', region = '', country = '' }
  last = nil
end

local function u32(im, c) return im.GetColorU322(im.ImVec4(c[1], c[2], c[3], c[4])) end

local layouts = {}       -- row -> { text, vw, fsz, w, h }: measured only when its text or the viewport changed

-- One centred row (its own background) at y; returns the y under it.
local function drawRow(im, dl, vp, row, text, y)
  local vw, fsz = vp.Size.x, im.GetFontSize()
  local L = layouts[row]
  if not L or L.text ~= text or L.vw ~= vw or L.fsz ~= fsz then
    local ts = im.CalcTextSize(text)
    L = { text = text, vw = vw, fsz = fsz, w = ts.x, h = ts.y }
    layouts[row] = L
    M.measures = M.measures + 1
  end
  local x = vp.Pos.x + (vw - L.w) / 2
  local p, a, b = vecs[1], vecs[2], vecs[3]
  p.x, p.y = x, y
  a.x, a.y = x - M.PAD_X, y - M.PAD_Y
  b.x, b.y = x + L.w + M.PAD_X, y + L.h + M.PAD_Y
  im.ImDrawList_AddRectFilled(dl, a, b, backU32, 4)
  im.ImDrawList_AddText1(dl, p, textU32, text, nil)
  return y + L.h + 2 * M.PAD_Y + M.ROW_GAP
end

local function drawImgui(im, text, loading)
  local vp = im.GetMainViewport()
  if not vp then return false end
  if not textU32 then textU32, backU32 = u32(im, M.TEXT_RGBA), u32(im, M.BACK_RGBA) end
  if not vecs then vecs = { im.ImVec2(0, 0), im.ImVec2(0, 0), im.ImVec2(0, 0) } end
  local dl = im.GetBackgroundDrawList1()
  local y = vp.Pos.y + M.TOP + M.PAD_Y
  if text ~= '' then y = drawRow(im, dl, vp, 1, text, y) end
  if loading ~= '' then drawRow(im, dl, vp, 2, loading, y) end
  return true
end

-- The loading part: line (the mod's loadprogress.lua: a string, or false for none) when given, else from status.
local function loadingPart(status, line)
  if line == nil then return M.loadingLine(status) end
  return line or nil
end

-- The street the car is on now ('' unknown): the GPS app's road name beside its speed limit box (settings.uiGpsHud).
function M.street() return loc.street or '' end

-- The bar's location line now (the UI app's too: hudapps.lua), '' when there is nothing to say.
function M.text(status, line)
  return M.compose(loc, nil, M.SHOW_FPS and fps or nil)
end

-- The loading line under it (the user, 2026-10-23: "make loading on a separate line"), '' when none.
function M.loading(status, line)
  return loadingPart(status, line) or ''
end

-- Once per frame: draw the bar. active: our world is shown; hidden: the game's UI covers it; status: core's status
-- text; line: the loading part (loadprogress.lua), see M.text. An error in the imgui calls turns the bar off (logged once).
function M.draw(active, hidden, status, line)
  if not M.enabled or not active or hidden then return false end
  local text, loading = M.text(status, line), M.loading(status, line)
  if text == '' and loading == '' then return false end
  local im = rawget(_G, 'ui_imgui')
  if not im or not im.GetMainViewport then return false end
  local ok, drawn = pcall(drawImgui, im, text, loading)
  if not ok then
    M.enabled, M.failed = false, tostring(drawn)
    if log then log('E', 'beamngEarth.statusbar', 'status bar turned off: ' .. M.failed) end
    return false
  end
  if drawn then M.draws = M.draws + 1 end
  return drawn
end

return M
