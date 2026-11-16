-- The 3D map's labels drawn by the game itself, in the frame they belong to (the user, 2026-11-07: "still a delay
-- with the label. i think apple maps draws them in 3d space instead of an overlay"). The map screen's own labels
-- (map3dLabels.js) came a frame or two after the view, the UI being composited later; these are put on ImGui's
-- background draw list from map3d's onPreRender, after the camera moved, with that very camera.
-- The rules are the screen's: every label the zoom allows (its min / max zoom, server maplabels.py), the most
-- important first, none overlapping (the ones shown last frame a little ahead: no flicker), fading in and out; white
-- text with a dark halo; towns with a dot, places with a coloured disc, regions and districts in spaced capitals,
-- water in light blue, streets along their road (letter by letter: ImGui draws no turned text).

local M = {}

local im = nil
local fonts = nil                       -- {bold = index, regular = index}
local state = {}                        -- id -> {a = alpha, shown = bool}
local frame = 0

M.FADE_RATE = 6                         -- alpha per second
M.KEEP_BONUS = 4
M.PAD = 3
M.STREETS_MAX = 28
M.SAME_TEXT_PX = 520
M.POIS_MAX = 16
M.POI_GAP_PX = 150
M.LONGER = {state = true, country = true, water = true, land = true, continent = true}
M.LONGER_Z = 1.5
M.RESERVED_TOP, M.RESERVED_BOTTOM = 86, 44      -- (UI px: the screen's controls)

local POI_COL = {food = {1, 0.62, 0.26}, shop = {0.96, 0.77, 0.09}, lodging = {0.71, 0.55, 1}, culture = {1, 0.49, 0.71},
  park = {0.3, 0.85, 0.48}, education = {0.85, 0.63, 0.4}, health = {1, 0.42, 0.42}, transit = {0.29, 0.66, 1},
  fuel = {0.5, 0.7, 1}}

local function fontIndex(names)
  for i = 0, im.IoFontsGetCount() - 1 do
    local n = ffi.string(im.IoFontsGetName(i))
    for _, want in ipairs(names) do if n == want then return i end end
  end
  return 0
end

local function init()
  im = ui_imgui
  if not im then return false end
  fonts = {bold = fontIndex({'cairo_bold', 'cairo_semibold_large'}), regular = fontIndex({'opensans_regular', 'cairo_regular'})}
  return true
end

-- style of a label: font kind, size (px at 1080 lines), caps, spaced, colour, mark ('dot' | 'icon' | nil)
local function styleOf(it)
  local k = it.k
  if k == 'continent' then return 'bold', 25, true, true, {1, 1, 1} end
  if k == 'country' then return 'bold', 19, true, true, {1, 1, 1} end
  if k == 'state' then return 'bold', 15, true, true, {1, 1, 1, 0.85} end
  if k == 'water' then
    local big = it.e == 'ocean' or it.e == 'sea'
    return 'regular', big and 20 or 16, false, false, {0.66, 0.86, 0.96}
  end
  if k == 'land' then return 'regular', 13, true, true, {0.94, 0.89, 0.81} end
  if k == 'district' then return 'bold', 14, true, true, {1, 1, 1, 0.92} end
  if k == 'street' then return 'bold', 13, true, false, {1, 1, 1} end
  if k == 'poi' then return 'bold', 14, false, false, POI_COL[it.e] or {0.9, 0.9, 0.9}, 'icon' end
  local r = it.r or 40
  if r <= 33.4 or it.e == 'capital' then return 'bold', 21, false, false, {1, 1, 1}, 'dot' end
  if r <= 34.4 then return 'bold', 18, false, false, {1, 1, 1}, 'dot' end
  if r <= 35.4 then return 'bold', 16, false, false, {1, 1, 1}, 'dot' end
  return 'regular', 14.5, false, false, {1, 1, 1}, 'dot'
end

local function priorityOf(it)
  if it.k == 'land' then return it.r - 15 end
  if it.k == 'district' then return 44 + (it.r - 36) * 0.2 end
  return it.r
end

local function spaced(t)
  local out = {}
  for ch in t:gmatch('[%z\1-\127\194-\244][\128-\191]*') do out[#out + 1] = ch end
  return table.concat(out, ' ')
end

-- a font kind at a size: ImGui's font pushed and the current window's font scale set (the draw list's plain text call
-- takes the current font and size; GetFont() is not usable from Lua here)
local function useFont(kind, size)
  im.PushFont2(fonts[kind])
  im.SetWindowFontScale(1)
  local base = im.GetFontSize()
  im.SetWindowFontScale(size / math.max(1, base))
end

local function endFont()
  im.SetWindowFontScale(1)
  im.PopFont()
end

local function col(c, a)
  return im.GetColorU322(im.ImVec4(c[1], c[2], c[3], (c[4] or 1) * a))
end

-- text with a halo at (x, y) top-left
local HALO = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}, {0.7, 0.7}, {-0.7, 0.7}, {0.7, -0.7}, {-0.7, -0.7}}
local function haloText(dl, x, y, text, c, a, scale)
  local hc = im.GetColorU322(im.ImVec4(0, 0, 0, 0.75 * a))
  local d = 1.3 * scale
  for _, o in ipairs(HALO) do
    im.ImDrawList_AddText1(dl, im.ImVec2(x + o[1] * d, y + o[2] * d), hc, text)
  end
  im.ImDrawList_AddText1(dl, im.ImVec2(x, y), col(c, a), text)
end

-- view: {p, f, u, r (vec3), fov (deg), zoom, O (earth centre vec3)}; rows: map3d's label rows (x, y, z, k, t, mz, xz,
-- r, e, dx, dy, dz, id); dt: s since the last frame
function M.draw(view, rows, dt)
  if not im and not init() then return end
  -- the game's window: ImGui's main viewport (the game turns ImGui's multi-viewports on: positions are the desktop's)
  local vp = im.GetMainViewport()
  local W, H = vp.Size.x, vp.Size.y
  if W <= 0 or H <= 0 then return end
  local scale = H / 1080                     -- (sizes are px at 1080 lines)
  -- (an invisible window of our own: the font scale is set on the current window, and outside one ImGui showed its
  -- implicit "Debug" window)
  local flags = bit.bor(im.WindowFlags_NoDecoration, im.WindowFlags_NoInputs, im.WindowFlags_NoBackground,
    im.WindowFlags_NoSavedSettings or 0, im.WindowFlags_NoFocusOnAppearing or 0, im.WindowFlags_NoBringToFrontOnFocus or 0,
    im.WindowFlags_NoNav or 0)
  im.SetNextWindowPos(vp.Pos)
  im.SetNextWindowSize(vp.Size)
  -- kept in the game's window: a window ImGui finds outside it gets an OS window of its own (opaque black where it
  -- should be see-through) with a second swap chain, made as the map opens (DirectX 12's log: "GFXd3d12WindowTarget::init
  -- begin: size=<the screen>"; ReShade crashed the game there, 2026-11-10). The game's own whole-pane overlays do the
  -- same (render/playerCompassMarkers.lua)
  if im.SetNextWindowViewport then im.SetNextWindowViewport(vp.ID) end
  im.Begin('##bngEarthMapLabels', nil, flags)
  local ok, err = pcall(M._draw, view, rows, dt, W, H, scale, vp.Pos.x, vp.Pos.y)
  im.End()
  if not ok then error(err, 0) end
end

function M._draw(view, rows, dt, W, H, scale, ox, oy)
  ox, oy = ox or 0, oy or 0           -- (the window's top left on the desktop: the draw list's positions are the desktop's)
  local dl = im.GetBackgroundDrawList1()
  local F = (H / 2) / math.tan(math.rad(view.fov or 55) / 2)
  local zoom = view.zoom
  local p, f, u, r, O = view.p, view.f, view.u, view.r, view.O
  frame = frame + 1
  local cands = {}
  for _, it in ipairs(rows) do
    local s = state[it.id]
    if not s then s = {a = 0, shown = false}; state[it.id] = s end
    s.seen = frame
    s.want = false
    local xz = it.xz + (M.LONGER[it.k] and M.LONGER_Z or 0)
    if it.x and zoom >= it.mz - 0.05 and zoom <= xz + 0.5 then
      local dx, dy, dz = it.x - p.x, it.y - p.y, it.z - p.z
      local zc = dx * f.x + dy * f.y + dz * f.z
      -- (in front of the camera and on this side of the earth)
      if zc > 1 and ((it.x - O.x) * -dx + (it.y - O.y) * -dy + (it.z - O.z) * -dz) >= 0 then
        local sx = ox + W / 2 + F * (dx * r.x + dy * r.y + dz * r.z) / zc
        local sy = oy + H / 2 - F * (dx * u.x + dy * u.y + dz * u.z) / zc
        if sx > ox - 200 and sx < ox + W + 200 and sy > oy - 100 and sy < oy + H + 100 then
          s.sx, s.sy, s.ang = sx, sy, 0
          if it.k == 'street' and it.dx then
            local ex, ey, ez = dx + it.dx, dy + it.dy, dz + it.dz
            local z2 = ex * f.x + ey * f.y + ez * f.z
            if z2 > 1 then
              local sx2 = ox + W / 2 + F * (ex * r.x + ey * r.y + ez * r.z) / z2
              local sy2 = oy + H / 2 - F * (ex * u.x + ey * u.y + ez * u.z) / z2
              local a = math.atan2(sy2 - sy, sx2 - sx)
              if a > math.pi / 2 then a = a - math.pi elseif a < -math.pi / 2 then a = a + math.pi end
              s.ang = a
            end
          end
          s.prio = priorityOf(it) - (s.shown and M.KEEP_BONUS or 0)
          s.it = it
          cands[#cands + 1] = s
        end
      end
    end
  end
  table.sort(cands, function(a, b) return a.prio < b.prio end)
  local placed, written, pois = {}, {}, {}
  local streets = 0
  local top, bottom = oy + M.RESERVED_TOP * scale, oy + H - M.RESERVED_BOTTOM * scale
  for _, s in ipairs(cands) do
    local it = s.it
    local kind, size, caps, spc, c, mark = styleOf(it)
    local text = caps and it.t:upper() or it.t
    if it.k == 'state' and it.e ~= '' and zoom < it.mz + 1.6 then text = it.e end
    if spc and #text < 40 then text = spaced(text) end
    size = size * scale
    useFont(kind, size)
    local ts = im.CalcTextSize(text)
    endFont()
    local tw, th = ts.x, ts.y
    local extra = mark == 'dot' and 10 * scale or mark == 'icon' and 21 * scale or 0
    local w, h = tw + extra, th
    local ca, sa = math.abs(math.cos(s.ang)), math.abs(math.sin(s.ang))
    local bw, bh = w * ca + h * sa, w * sa + h * ca
    local cx = mark and (s.sx + w / 2 - (mark == 'icon' and 9 or 4) * scale) or s.sx
    local pad = M.PAD * scale
    local x0, x1, y0, y1 = cx - bw / 2 - pad, cx + bw / 2 + pad, s.sy - bh / 2 - pad, s.sy + bh / 2 + pad
    local ok = x0 >= ox and x1 <= ox + W and y0 >= top and y1 <= bottom
    if ok then
      for _, q in ipairs(placed) do
        if x0 < q[2] and x1 > q[1] and y0 < q[4] and y1 > q[3] then ok = false; break end
      end
    end
    if ok and it.k == 'poi' then
      if #pois >= M.POIS_MAX then ok = false end
      for _, q in ipairs(pois) do
        if ok and math.abs(q[1] - s.sx) + math.abs(q[2] - s.sy) < M.POI_GAP_PX * scale then ok = false end
      end
    end
    if ok and it.k == 'street' then
      if streets >= M.STREETS_MAX then ok = false end
      for _, q in ipairs(written[text] or {}) do
        if ok and math.abs(q[1] - s.sx) + math.abs(q[2] - s.sy) < M.SAME_TEXT_PX * scale then ok = false end
      end
    end
    if ok then
      placed[#placed + 1] = {x0, x1, y0, y1}
      if it.k == 'poi' then pois[#pois + 1] = {s.sx, s.sy} end
      if it.k == 'street' then
        streets = streets + 1
        written[text] = written[text] or {}
        table.insert(written[text], {s.sx, s.sy})
      end
      s.want = true
      s.draw = {kind = kind, size = size, text = text, tw = tw, th = th, c = c, mark = mark, cx = cx}
    end
  end
  -- fade and draw (a label hidden this frame fades out where it was last drawn)
  local rate = M.FADE_RATE * (dt or 0.016)
  for id, s in pairs(state) do
    if s.seen ~= frame then
      state[id] = nil
    else
      local it = s.it
      local target = 0
      if s.want and it then
        local xz = it.xz + (M.LONGER[it.k] and M.LONGER_Z or 0)
        target = math.min(1, math.max(0, (zoom - it.mz) / 0.45 + 0.15), math.max(0, (xz + 0.5 - zoom) / 0.45))
      end
      s.a = s.a + math.max(-rate, math.min(rate, target - s.a))
      s.shown = s.want
      local d = s.draw
      if s.a > 0.02 and d and s.sx then
        useFont(d.kind, d.size)
        if d.mark == 'dot' then
          im.ImDrawList_AddCircleFilled(dl, im.ImVec2(s.sx, s.sy), 4.5 * scale, im.GetColorU322(im.ImVec4(0.2, 0.2, 0.2, s.a)), 12)
          im.ImDrawList_AddCircleFilled(dl, im.ImVec2(s.sx, s.sy), 3 * scale, col({1, 1, 1}, s.a), 12)
          haloText(dl, s.sx + 7 * scale, s.sy - d.th / 2, d.text, d.c, s.a, scale)
        elseif d.mark == 'icon' then
          im.ImDrawList_AddCircleFilled(dl, im.ImVec2(s.sx, s.sy), 9 * scale, col({1, 1, 1}, s.a * 0.9), 16)
          im.ImDrawList_AddCircleFilled(dl, im.ImVec2(s.sx, s.sy), 7.5 * scale, col(d.c, s.a), 16)
          haloText(dl, s.sx + 12 * scale, s.sy - d.th / 2, d.text, d.c, s.a, scale)
        elseif math.abs(s.ang) > 0.08 then
          -- along the road: each letter on the turned baseline, upright
          local n = 0
          local chars = {}
          for ch in d.text:gmatch('[%z\1-\127\194-\244][\128-\191]*') do n = n + 1; chars[n] = ch end
          local widths, total = {}, 0
          for i = 1, n do
            widths[i] = im.CalcTextSize(chars[i]).x
            total = total + widths[i]
          end
          local ca2, sa2 = math.cos(s.ang), math.sin(s.ang)
          local t = -total / 2
          for i = 1, n do
            local mid = t + widths[i] / 2
            local gx, gy = s.sx + ca2 * mid, s.sy + sa2 * mid
            haloText(dl, gx - widths[i] / 2, gy - d.th / 2, chars[i], d.c, s.a, scale)
            t = t + widths[i]
          end
        else
          haloText(dl, s.sx - d.tw / 2, s.sy - d.th / 2, d.text, d.c, s.a, scale)
        end
        endFont()
      end
    end
  end
end

function M.clear() state = {} end

return M
