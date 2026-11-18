-- luajit mod/tests/test_deckguard.lua (deckguard.lua: the car slowed before a bridge whose road tile has not come)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local G = require('beamngEarth/deckguard')

-- A bridge along y = 0 from x = 300 to x = 360 over a valley (its ground 8 m under the road's 10 m), both ends on the
-- ground; the road tile covering x >= 296 missing unless `ready`.
local function world(opts)
  opts = opts or {}
  local w = { ready = opts.ready or false, deckGround = opts.deckGround or 2, x0 = opts.x0 or 300, x1 = opts.x1 or 360 }
  w.deckAt = function(x, y)
    if w.ready or x < w.x0 - 4 then return 'ready' end
    return 'missing', 'g17_1_2'
  end
  w.groundAt = function(x, y)
    if x > w.x0 + 3 and x < w.x1 - 3 and math.abs(y) < 30 then return w.deckGround end
    return 10
  end
  return w
end

local function guard(spans, abut, aid)
  local g = G.new()
  T.check(G.onSpans(g, { anchorId = aid or 1, cx = 0, cy = 0, r = 1200, spans = spans, abut = abut or { 3 } }, 1)
    == ((aid or 1) == 1), 'spans taken for the current anchor only')
  return g
end

local SPAN = { 300, 0, 330, 0, 360, 0 }

local function look(g, w, x, y, z, vx, vy, clock, vehId)
  return G.look(g, { clock = clock or 0, x = x, y = y, z = z or 10.5, vx = vx, vy = vy or 0, deckAt = w.deckAt,
    groundAt = w.groundAt, vehId = vehId or 1 })
end

-- spans
do
  local g = guard({ SPAN }, { 3 }, 2)
  T.check(#g.spans == 0 and g.aid == nil, "another anchor's spans: none")
  local sp = G._makeSpan(SPAN, 2)
  T.check(sp and sp.n == 3 and math.abs(sp.len - 60) < 1e-9 and not sp.abut0 and sp.abut1, 'span parsed: points, length, ends')
  T.check(G._makeSpan({ 1, 2 }, 3) == nil and G._makeSpan({ 1, 2, 0 / 0, 4 }, 3) == nil, 'a bad span is left out')
end

-- the gap ahead: found, at its entry
do
  local w = world()
  local g = guard({ SPAN })
  local d = look(g, w, 120, 0, 10.5, 30, 0)
  T.check(d ~= nil, 'a bridge ahead whose tile has not come: a gap')
  T.check(d and d.d >= 175 and d.d <= 195, 'the gap where the ground under the span drops: ' .. tostring(d and d.d))
  T.check(d and d.key == 'g17_1_2', 'the tile it waits for')
  T.check(G.waitKeys(g)[1] == 'g17_1_2', 'pose.deckWait names it')
  w.ready = true
  T.check(look(g, w, 120, 0, 10.5, 30, 0, 0.25) == nil and G.waitKeys(g) == nil, 'the tile came: no gap')
end

-- nothing to guard
do
  local w = world({ deckGround = 9 })
  T.check(look(guard({ SPAN }), w, 120, 0, 10.5, 30, 0) == nil, 'the ground under the deck at its level: driven on')
  w = world()
  -- (a bridge across the path at x = 250, the road the car is on passing under it in a cut 8 m down)
  local wc = world()
  wc.groundAt = function(x, y) if math.abs(x - 250) < 6 and math.abs(y) < 50 then return 2 end return 10 end
  wc.deckAt = function() return 'missing', 'g17_5_5' end
  T.check(look(guard({ { 250, -60, 250, 60 } }), wc, 120, 0, 10.5, 30, 0) == nil, 'a bridge across the path: crossed')
  local wl = world()
  wl.groundAt, wl.deckAt = wc.groundAt, wc.deckAt
  T.check(look(guard({ { 250, -60, 250, 60 } }), wl, 250, -150, 10.5, 0, 30) ~= nil, '... the same one driven along: a gap')
  T.check(look(guard({ SPAN }), w, 120, 0, 10.5, -30, 0) == nil, 'driving away from it: none')
  T.check(look(guard({ SPAN }), w, -2000, 0, 10.5, 30, 0) == nil, 'beyond the braking distance and the lead: none')
  T.check(look(guard({ SPAN }), w, 120, 40, 10.5, 30, 0) == nil, 'a road beside it, 40 m off: none')
  -- a road under it: the path enters it at the deck's end, but well under its abutment's ground
  local g = guard({ SPAN })
  local wu = world()
  wu.groundAt = function(x, y)
    if x >= 300 - 1 and x <= 300 + 1 then return 18 end      -- (its west abutment 7.5 m over the car)
    if x >= 360 - 1 and x <= 360 + 1 then return 18 end
    return 2
  end
  T.check(look(g, wu, 250, 0, 10.5, 20, 0) == nil, 'entering it far under its abutment: a road under it')
  -- (Codex review 2026-11-15: the same road under it driven towards from far off, where the level is not tested yet: a
  -- predicted entry must not count as being on it, the level is tested again once near)
  local gs = guard({ SPAN })
  look(gs, wu, 120, 0, 10.5, 30, 0, 0)
  T.check(look(gs, wu, 250, 0, 10.5, 30, 0, 0.25) == nil, 'driving on under it from far off: no gap once near')
  T.check(look(gs, wu, 290, 0, 10.5, 30, 0, 0.5) == nil, '... nor right at its end')
  T.check(next(gs.riding) == nil, '... never taken as on it')
  -- (Codex review round 2: a road falling away under a bridge predicted from its level far off: the episode the far
  -- look made is dropped once the level check rejects the span, and the car on the road under it is never set back)
  local wd = world()
  wd.groundAt = function(x, y)
    if math.abs(x - 300) <= 15 or math.abs(x - 360) <= 15 then return 18 end   -- (its embankments)
    return 2
  end
  local gd = guard({ SPAN })
  T.check(look(gd, wd, 120, 0, 18.5, 40, 0, 0) ~= nil and gd.ep ~= nil, 'far off at its level: a gap predicted')
  T.check(look(gd, wd, 250, 0, 10.5, 40, 0, 2) == nil, 'the road fell away under its level: no gap')
  T.check(gd.ep == nil, '... and no rescue episode left')
  T.check(G.fell(gd, { clock = 4, x = 310, y = 0, z = 2.5, vx = 20, vy = 0, groundAt = wd.groundAt, vehId = 1 }) == nil,
    '... the car on the road under it never set back')
  T.check(look(guard({}), w, 120, 0, 10.5, 30, 0) == nil, 'no spans: nothing')
end

-- a road above it (Codex review 2026-11-15 round 3): within the corridor at its end but 20 m over the deck's level, the
-- bridge's tile missing: not on the bridge; nor mid-span far over it, with no level check at an end to decide
do
  local w = world()
  local g = guard({ SPAN })
  T.check(look(g, w, 298, 0, 30.5, 30, 0, 0) == nil and next(g.riding) == nil and g.ep == nil,
    'a higher road at its end: no gap, not on it, no episode')
  local _, hold = G.brake(g, { v = 30, dt = 1 / 60 })
  T.check(not hold, '... never held')
  local g2 = guard({ { 300, 0, 400, 0, 500, 0 } })
  w.groundAt = function(x, y) if math.abs(x - 300) <= 1 or math.abs(x - 500) <= 1 then return 10 end return 2 end
  T.check(look(g2, w, 400, 0, 30.5, 30, 0, 0) == nil and next(g2.riding) == nil, 'mid-span 20 m over its level: not on it')
  T.check(look(g2, w, 400, 0, 12, 30, 0, 0.25) ~= nil and g2.riding[g2.spans[1].id], 'mid-span at its level: on it')
  -- a span whose ends stand on other decks (no abutment height known): on it only coming off a deck the car was on
  local g3 = G.new()
  G.onSpans(g3, { anchorId = 1, spans = { SPAN }, abut = { 0 } }, 1)
  T.check(look(g3, world(), 298, 0, 30.5, 30, 0, 0) == nil and next(g3.riding) == nil,
    'no abutment known, not off a deck: not taken as on it')
  g3.riding = { other = true }
  T.check(look(g3, world(), 298, 0, 10.5, 30, 0, 0.25) ~= nil, '... off a deck it was on: on it, the gap ahead')
end

-- Codex review round 4: a short bridge between two path samples on every look (the 8 m sampling's phase)
do
  local w = world({ x0 = 300, x1 = 308 })
  w.groundAt = function(x, y) if x > 301 and x < 307 then return 2 end return 10 end
  local g = guard({ { 300, 0, 308, 0 } })
  local seen, x, clock = 0, 124, 0
  while x <= 300 do
    if look(g, w, x, 0, 10.5, 32, 0, clock) then seen = seen + 1 end
    x, clock = x + 8, clock + 0.25
  end
  T.check(seen >= 20, 'an 8 m bridge whose gap falls between the samples: found at every look (' .. seen .. ')')
end

-- Codex review round 4: a road under a bridge approached from far off (no braking until its height can be checked)
do
  local w = world()
  w.groundAt = function(x, y)
    if math.abs(x - 300) <= 1 or math.abs(x - 360) <= 1 then return 10 end
    return 2
  end
  local g = guard({ SPAN })
  T.check(look(g, w, 210, 0, 2.5, 30, 0, 0) == nil, 'a road 8 m under a bridge, 90 m off: no gap (its road there stays low)')
  -- ... while a road rising onto it is one
  local wr = world()
  wr.groundAt = function(x, y)
    if x > 303 and x < 357 then return 2 end
    if x <= 300 then return math.max(2, 10 - (300 - x) * 0.08) end  -- (an 8 % ramp up to the abutment)
    return 10
  end
  T.check(look(guard({ SPAN }), wr, 160, 0, 2.5, 30, 0, 0) ~= nil, 'a road rising onto it from below, 140 m off: the gap')
end

-- the cost of a look (the worst case: on a 900 m viaduct whose tile is missing and whose ground is at its level, every
-- 2 m of it looked at; 400 spans round the car)
do
  local spans = {}
  for i = 1, 399 do spans[i] = { 5000 + i * 10, 3000, 5005 + i * 10, 3000 } end
  spans[400] = { 0, 0, 450, 0, 900, 0 }
  local w = world()
  w.deckAt = function() return 'missing', 'g17_1_1' end
  w.groundAt = function() return 10 end
  local abut = {}
  for i = 1, 400 do abut[i] = 3 end
  local g = guard(spans, abut)
  local t0 = os.clock()
  for i = 1, 100 do look(g, w, 1, 0, 10.5, 70, 0, i * 0.25) end
  local ms = (os.clock() - t0) * 1000 / 100
  T.check(g.riding[g.spans[400].id] and g.stats.looks >= 100, 'on the viaduct, looked along it')
  T.check(ms < 2, string.format('a worst-case look: %.3f ms', ms))
  print(string.format('  worst-case look %.3f ms', ms))
end

-- the tile grid's walk (Codex review round 5): every cell a piece crosses, a corner clipped too
do
  local cells = {}
  G._cellsAlong(0.5, 0.5, 2.5, 1.6, function(ix, iy, t0, t1) cells[#cells + 1] = ix .. '_' .. iy; return false end)
  T.check(table.concat(cells, ' ') == '0_0 1_0 1_1 2_1', 'the cells crossed, in order: ' .. table.concat(cells, ' '))
  cells = {}
  G._cellsAlong(3.9, 0.2, 3.2, 0.9, function(ix, iy) cells[#cells + 1] = ix .. '_' .. iy end)
  T.check(table.concat(cells, ' ') == '3_0', 'within one cell: that one')
  cells = {}
  G._cellsAlong(0.1, 0.95, 1.2, 1.4, function(ix, iy) cells[#cells + 1] = ix .. '_' .. iy end)
  T.check(table.concat(cells, ' ') == '0_0 0_1 1_1', 'a corner clipped: ' .. table.concat(cells, ' '))
end

-- a missing tile's corner clipped by a diagonal bridge between two tiles that are in (Codex review round 5)
do
  local w = world()
  w.groundAt = function(x, y)
    if (math.abs(x - 300) <= 1 and math.abs(y + 10) <= 1) or (math.abs(x - 360) <= 1 and math.abs(y - 50) <= 1) then
      return 10
    end
    if x < 300 then return 10 end                   -- (its approach)
    return 2
  end
  local grid = {                                    -- (20 m tiles: only the one at x 300-320, y 0-20 missing)
    xy = function(x, y) return x / 20, y / 20 end,
    at = function(ix, iy) if ix == 15 and iy == 0 then return 'missing', 'g17_15_0' end return 'ready' end,
  }
  local g = guard({ { 300, -10, 360, 50 } })
  local d = G.look(g, { clock = 0, x = 200, y = -110, z = 10.5, vx = 30, vy = 30, groundAt = w.groundAt, grid = grid,
    vehId = 1 })
  T.check(d ~= nil and d.key == 'g17_15_0', 'the corner of the missing tile it clips: the gap')
  T.check(d and d.x >= 310 and d.x <= 320 and d.y >= 0 and d.y <= 10, 'at the corner: ' .. tostring(d and d.x) .. ', '
    .. tostring(d and d.y))
end

-- a curved bridge the path's arc leaves: its gap further along it still found (scanned along the span to the look's
-- reach); a fork's branch ahead: never braked for (which branch the car takes is not known; the fork extension of
-- round 5 dropped after Codex review rounds 6 / 7)
do
  local w = world()
  w.deckAt = function(x, y) if x >= 380 then return 'missing', 'g17_8_8' end return 'ready' end
  w.groundAt = function(x, y) if x > 303 then return 2 end return 10 end
  -- (a bridge from (300, 0) curving north: 30 m east, then north-east)
  local g = guard({ { 300, 0, 340, 0, 380, 30, 410, 70 } })
  local d = look(g, w, 150, 0, 10.5, 35, 0, 0)
  T.check(d ~= nil and d.key == 'g17_8_8' and d.d > 200 and d.d < 250, 'a curved bridge: its gap along it ' ..
    tostring(d and d.d))
  -- (A from the ground at (300, 0) to a fork at (340, 0), B on from there; B's tile missing past x 360)
  local wf = world()
  wf.deckAt = function(x, y) if x >= 360 then return 'missing', 'g17_9_9' end return 'ready' end
  wf.groundAt = function(x, y)
    if math.abs(x - 300) <= 1 or math.abs(x - 400) <= 1 then return 10 end
    if x < 300 then return 10 end
    return 2
  end
  local gf = G.new()
  G.onSpans(gf, { anchorId = 1, spans = { { 300, 0, 340, 0 }, { 340, 0, 400, 0 }, { 340, 0, 400, 40 } },
    abut = { 1, 2, 2 } }, 1)
  local df = look(gf, wf, 200, 0, 10.5, 30, 0, 0)
  T.check(df == nil, 'a fork\'s branch ahead, its tile missing: not braked for')
end

-- a fork on a bridge (Codex review rounds 6 / 7): the car taking one branch, the other's tile missing: never braked or
-- held, before, at or past the fork; once only the branch it took is round it, that one's own gap is found
do
  local function fork(missing, xs)
    local w = world()
    w.deckAt = function(x, y) if missing(x, y) then return 'missing', 'g17_7_7' end return 'ready' end
    w.groundAt = function(x, y)
      if math.abs(x - 300) <= 1 or math.abs(x - 500) <= 1 then return 10 end
      return 2
    end
    local g = G.new()
    G.onSpans(g, { anchorId = 1, spans = { { 300, 0, 340, 0 }, { 340, 0, 500, 0 }, { 340, 0, 500, 100 } },
      abut = { 1, 2, 2 } }, 1)
    local out = {}
    for i, x in ipairs(xs) do out[i] = look(g, w, x, 0, 10.5, 30, 0, i * 0.25) or false end
    return out
  end
  local xs = { 290, 320, 336, 350, 380, 420 }
  local got = fork(function(x, y) return y > 20 end, xs)
  local any = false
  for i = 1, #xs do any = any or got[i] ~= false end
  T.check(not any, 'the branch not taken missing: no gap at any look')
  got = fork(function(x, y) return x > 440 and math.abs(y) < 5 end, xs)
  T.check(not got[2] and not got[4], 'the branch taken missing: not braked for before or right at the fork')
  T.check(got[5] and got[5].x >= 440, '... once it alone is round the car: its gap ' .. tostring(got[5] and got[5].d))
end

-- a shallow fork (Codex review round 7: its branches 10 m apart after 200 m, both round the car all the way; 100 m
-- tiles, the ones north of y = 0 missing: only the branch not taken crosses into them): never braked, never held
do
  local grid = {
    xy = function(x, y) return x / 100, y / 100 end,
    at = function(ix, iy) if iy >= 0 then return 'missing', 'g17_' .. ix .. '_' .. iy end return 'ready' end,
  }
  local groundAt = function(x, y)
    if math.abs(x + 100) <= 1 or math.abs(x - 200) <= 1 then return 10 end
    return 2
  end
  local g = G.new()
  G.onSpans(g, { anchorId = 1, spans = { { -100, -2, 0, -2 }, { 0, -2, 200, -2 }, { 0, -2, 200, 8 } }, abut = { 1, 2, 2 } },
    1)
  local bad = nil
  local x, clock = -110, 0
  while x <= 220 do
    local d = G.look(g, { clock = clock, x = x, y = -2, z = 10.5, vx = 10, vy = 0, grid = grid, groundAt = groundAt,
      vehId = 1 })
    local scale, hold = G.brake(g, { v = 10, dt = 1 / 60 })
    if d or scale < 1 or hold then bad = bad or x end
    x, clock = x + 2.5, clock + 0.25
  end
  T.check(bad == nil, 'a shallow fork, the branch not taken missing: never braked or held (first at ' .. tostring(bad) .. ')')
end

-- already on a solid part of it: the next tile's part missing
do
  local w = world()
  w.deckAt = function(x, y) if x >= 340 then return 'missing', 'g17_9_9' end return 'ready' end
  local g = guard({ SPAN })
  local d = look(g, w, 250, 0, 10.5, 20, 0)
  T.check(d and d.key == 'g17_9_9' and d.d > 80, 'the deck in, its next tile missing: the gap there')
  d = look(g, w, 320, 0, 10.5, 20, 0, 0.25)
  T.check(d and d.d >= 15 and d.d <= 25, 'on the deck now: the gap ahead on it, ' .. tostring(d and d.d))
end

-- a curve: the arc finds a bridge round a bend
do
  local w = world()
  -- the car at (0, 0) going east at 30 m/s and turning left at 0.2 rad/s (r 150 m): 60 degrees round the bend (157 m)
  -- a bridge goes on along its heading there
  local sx, sy = 150 * math.sin(math.pi / 3), 150 * (1 - math.cos(math.pi / 3))
  local ex, ey = sx + 60 * math.cos(math.pi / 3), sy + 60 * math.sin(math.pi / 3)
  w.deckAt = function() return 'missing', 'g17_3_3' end
  w.groundAt = function(x, y) if y > sy + 5 and y < ey - 5 then return 0 end return 10 end
  local g = guard({ { sx, sy, ex, ey } })
  T.check(look(g, w, 0, 0, 10.5, 30, 0, 0) == nil, 'straight on (no turn known yet): it is off the path')
  local th = 0.2 * 0.25
  local d = look(g, w, 0, 0, 10.5, 30 * math.cos(th), 30 * math.sin(th), 0.25)
  T.check(d ~= nil and d.d > 140 and d.d < 175, 'turning: the arc reaches it round the bend ' .. tostring(d and d.d))
  local g2 = guard({ { 0, 140, 0, 200 } })          -- (a span due north, 140 m on: the car heading north)
  w.groundAt = function(x, y) if y > 143 and y < 197 then return 0 end return 10 end
  d = look(g2, w, 0, 0, 10.5, 0, 30, 0)
  T.check(d ~= nil and d.d >= 135 and d.d <= 152, 'straight on: the gap where its ground drops ' .. tostring(d and d.d))
end

-- braking: the car comes to rest short of the gap; the drag first
do
  local g = G.new()
  g.danger, g.dNow = { d = 100, id = 'x' }, 100
  local s = G.brake(g, { v = 30, dt = 1 / 60 })
  T.check(s < 1 and s >= (30 - G.A_MAX / 60) / 30 - 1e-9, 'over the cap: braked, at most A_MAX')
  g.danger, g.dNow = { d = 200, id = 'x' }, 200
  s = G.brake(g, { v = 30, dt = 0.1 })
  T.check(s < 1 and s > 1 - G.K_MAX * 0.1 - 1e-9, '7 s off at 30 m/s: only a little drag (' .. tostring(s) .. ')')
  g.danger, g.dNow = { d = 400, id = 'x' }, 400
  T.check(G.brake(g, { v = 30, dt = 0.1 }) == 1, '13 s off: none yet')
  g.danger, g.dNow = { d = 1000, id = 'x' }, 1000
  T.check(G.brake(g, { v = 30, dt = 0.1 }) == 1, 'far off: none')
  local _, hold = G.brake({ danger = { d = 15 }, dNow = 15 }, { v = 40, dt = 1 / 60 })
  T.check(hold, 'too near to stop at 40 m/s: held')
  _, hold = G.brake({ danger = { d = 200 }, dNow = 200 }, { v = 40, dt = 1 / 60 })
  T.check(not hold, 'room to stop: not held')
  local sb, hb = G.brake({ danger = { d = 5 }, dNow = 5 }, { v = -1.5, dt = 1 / 60 })
  T.check(sb == 1 and not hb, 'backing away from the gap at the line: never braked')
  T.check(G.brake({ danger = { d = 5 }, dNow = 5 }, { v = 0.2, dt = 1 / 60 }) < 1, 'creeping towards it at the line: stopped')
  -- a drive: 45 m/s, the gap 300 m on, throttle kept (the car would gain 2 m/s^2)
  local gd = { danger = { d = 300 }, dNow = 300 }
  local x, v, worst = 0, 45, 0
  for _ = 1, 60 * 40 do
    local sc, h = G.brake(gd, { v = v, dt = 1 / 60 })
    T.check(not h, 'braked in time: never held')
    local nv = v * sc
    worst = math.max(worst, (v - nv) * 60)
    v = nv + 2 / 60
    x = x + v / 60
    gd.danger.d, gd.dNow = 300 - x, 300 - x     -- (the look's distance again)
    if x > 300 then break end
  end
  T.check(x < 300 - G.STOP_M / 2, 'at rest short of the gap: ' .. string.format('%.1f m', 300 - x))
  T.check(worst <= G.A_MAX + 1e-6, string.format('never more than A_MAX taken off (%.2f m/s^2)', worst))
end

-- the rescue: it went over anyway
do
  local w = world()
  local g = guard({ SPAN })
  look(g, w, 200, 0, 10.5, 30, 0, 0)
  T.check(g.ep and g.ep.safe and g.ep.safe[1] == 200, 'the approach remembered')
  T.check(G.fell(g, { clock = 0.1, x = 330, y = 0, z = 4, vx = 20, vy = 0, groundAt = w.groundAt, vehId = 1 }) == nil,
    'only predicted, never entered at its level: no set-back (Codex review 2026-11-15, round 2)')
  look(g, w, 298, 0, 10.5, 30, 0, 0.25)       -- (at its end, at the deck's level: entered)
  T.check(g.ep and g.ep.confirmed and g.ep.safe[1] == 200, 'entered at its level: the episode confirmed, the approach kept')
  -- (Codex review 2026-11-15: another car of the player's, switched to in the window, is never set back)
  T.check(G.fell(g, { clock = 1, x = 330, y = 0, z = 4, vx = 20, vy = 0, groundAt = w.groundAt, vehId = 2 }) == nil,
    'another vehicle under it: not set back')
  local r = G.fell(g, { clock = 1, x = 330, y = 0, z = 4, vx = 20, vy = 0, groundAt = w.groundAt, vehId = 1 })
  T.check(r and r[1] == 200 and r[2] == 0, 'in the gap, under the deck: set back on the approach')
  T.check(G.fell(g, { clock = 1.2, x = 330, y = 0, z = 4, vx = 20, vy = 0, groundAt = w.groundAt, vehId = 1 }) == nil,
    'at most once a RESCUE_GAP_S')
  T.check(G.fell(g, { clock = 3, x = 330, y = 0, z = 9, vx = 20, vy = 0, groundAt = w.groundAt, vehId = 1 }) == nil, 'on the deck: none')
  T.check(G.fell(g, { clock = 3, x = 330, y = 0, z = 4, vx = 0, vy = 20, groundAt = w.groundAt, vehId = 1 }) == nil,
    'crossing under it: none')
  T.check(G.fell(g, { clock = 3, x = 330, y = 40, z = 4, vx = 20, vy = 0, groundAt = w.groundAt, vehId = 1 }) == nil, 'beside it: none')
  T.check(G.fell(g, { clock = 20, x = 330, y = 0, z = 4, vx = 20, vy = 0, groundAt = w.groundAt, vehId = 1 }) == nil,
    'long after the gap was seen: none')
end

-- with the engine (stubbed): nothing called while nothing is ahead; braking, holding, giving up
do
  local w = world()
  local calls = { veh = 0, scale = 0 }
  local veh = { applyClusterVelocityScaleAdd = function(_, id, s) calls.scale = calls.scale + 1; calls.last = s end }
  local paused = false
  local hold = { paused = function() return paused end, pause = function(on) paused = on end }
  local pl = { x = -3000, y = 0, z = 10.5, vx = 30, vy = 0 }
  local ctx = { clock = 0, dtSim = 1 / 60, dtReal = 1 / 60, pl = pl, anchorId = 1,
    getVeh = function() calls.veh = calls.veh + 1; return veh end, deckAt = w.deckAt, groundAt = w.groundAt, hold = hold }
  local g = guard({ SPAN })
  for i = 1, 120 do
    ctx.clock = i / 60
    T.check(G.update(g, ctx) == 'clear', 'far from the bridge: clear')
  end
  T.check(calls.veh == 0 and calls.scale == 0, 'no engine call while no gap is ahead')
  T.check(g.stats.looks <= 9, 'a look every LOOK_S only: ' .. g.stats.looks)
  pl.x = 150
  ctx.clock = 3
  local m = G.update(g, ctx)
  T.check(m == 'slow' and calls.scale == 1 and calls.last < 1, 'the gap ahead: braked (' .. tostring(m) .. ')')
  ctx.anchorId = 2
  T.check(G.update(g, ctx) == 'off', 'another anchor: off')
  ctx.anchorId = 1
  -- at rest at the line: waiting; the hold for a car too fast for the gap
  pl.x, pl.vx = 290, 0.1
  ctx.clock = 4
  T.check(G.update(g, ctx) == 'wait' and G.waiting(g), 'at rest short of the gap: waiting (pose.guardHold)')
  pl.vx = 40
  ctx.clock = 5
  T.check(G.update(g, ctx) == 'hold' and paused, 'too fast for the gap: held')
  w.ready = true
  ctx.clock = 5.5
  T.check(G.update(g, ctx) ~= 'hold' and not paused, 'the tile came: let go')
  -- give up after GIVE_UP_S at the line
  w.ready = false
  pl.vx = 0.1
  ctx.dtReal = 1
  local released = false
  for i = 1, 60 do
    ctx.clock = 10 + i
    G.update(g, ctx)
    if next(g.released) then released = true; break end
  end
  T.check(released and g.danger == nil, 'at the line GIVE_UP_S: let go')
  ctx.clock = 80
  T.check(G.update(g, ctx) == 'clear', '... and left alone while the car is on it')
  pl.x = -3000
  ctx.clock = 81
  G.update(g, ctx)
  T.check(next(g.released) == nil, '... the guard\'s again once the car is off it')
  -- a vehicle switch (Codex review 2026-11-15): the old car's gap, approach and hold are not the new one's
  pl.x, pl.vx, pl.vehicleId = 290, 40, 7
  ctx.dtReal = 1 / 60
  ctx.clock = 90
  G.update(g, ctx)
  ctx.clock = 90.5
  T.check(G.update(g, ctx) == 'hold' and paused and g.ep and g.ep.vehId == 7, 'car 7 held before the gap')
  pl.vehicleId, pl.x, pl.z, pl.vx = 8, 330, 2.5, 20     -- (on the road 8 m under it)
  ctx.clock = 90.6
  local m8 = G.update(g, ctx)
  T.check(not paused and g.held == nil, 'switched to car 8: the hold let go')
  T.check(m8 ~= 'rescue' and (g.ep == nil or g.ep.vehId == 8), 'car 8 under the span: not set back to car 7\'s approach')
  G.newFrame(g)
  T.check(#g.spans == 0 and g.aid == nil, 'a new frame: no spans until the next message')
end

T.done('test_deckguard')
