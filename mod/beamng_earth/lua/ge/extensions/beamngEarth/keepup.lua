-- Detail keep-up recorder (a debug switch: off unless a tool starts it, tools/keepup.py; the user, 2026-11-13: "small
-- details ... break immersion a lot when they can't keep up with the player, even when terrain and roads do").
-- While on it records, in the game's own clock (os.clock: wall time in the game):
--   - the car's trace (TRACE_S), its anchor, position and velocity;
--   - each detail's FIRST appearance: a Forest item of trees.lua (trees, street lights, signs, furniture, fences, guard
--     rails: one row per item, keyed tile + row), a road geotile's mesh part of roads.lua (road with its markings,
--     sidewalks, sign meshes, decks, buildings ...: one row per tile and part), a parked car moved onto a spot near
--     the player (the stock parking system); each with the car's distance to it and how far ahead of the car it was,
--     the seconds since its road tile's road mesh was made and since its own tile's data came;
--   - the tiles' arrivals (road geotile content, its skin, vegetation / furniture rows);
--   - a probe every PROBE_S: what should stand within PROBE_R of the car and does not (trees.keepupMissing, the road
--     geotiles' parts there).
-- Nothing here runs unless M.on: the modules call the hooks only when they are set (M.start sets them).
local M = {}
M.on = false
M.TRACE_S = 0.1
M.PROBE_S = 0.5
M.PROBE_R = 50
M.MAX_ROWS = 400000      -- (each list; a long drive stops recording past it)

local clock = os.clock
local anchors, anchorIdx = {}, {}
local car = { x = 0, y = 0, hx = 0, hy = 1, sp = 0, ai = 0, t = 0 }
local items, parts, parks, arrivals, probes, misses, trace = {}, {}, {}, {}, {}, {}, {}
local seenItem, seenPart = {}, {}
local roadAt, roadArr, tileArr = {}, {}, {}
local groupIdx, groupNames = {}, {}
local lastTrace, lastProbe = -1, -1
local parkPos = {}
local t0 = 0

local function ai(a)
  if not a then return 0 end
  local i = anchorIdx[a]
  if not i then i = #anchors + 1; anchors[i] = a; anchorIdx[a] = i end
  return i
end

local function gi(g)
  g = tostring(g)
  local i = groupIdx[g]
  if not i then i = #groupNames + 1; groupNames[i] = g; groupIdx[g] = i end
  return i
end

local function push(list, row)
  if #list < M.MAX_ROWS then list[#list + 1] = row end
end

-- the car's distance to (x, y) and how far it is ahead of the car along its heading
local function rel(x, y)
  local dx, dy = x - car.x, y - car.y
  return math.sqrt(dx * dx + dy * dy), dx * car.hx + dy * car.hy
end

-- the road geotile key of a trees.lua tile key ('g17_..', 'pg17_..' furniture, 'gg17_..' guard rails)
local function roadKeyOf(key)
  key = tostring(key or '')
  local k = key:match('^[pg](g%d+_%-?%d+_%-?%d+)$')
  return k or key
end

local function kindOf(key)
  key = tostring(key or ''):gsub('^g:', '')
  if key:match('^pg%d') then return 2 end         -- street furniture (lights, signs, fences, benches ...)
  if key:match('^gg%d') then return 3 end         -- guard rails / barriers
  return 1                                        -- vegetation tile (trees, bushes, rocks)
end

-- trees.lua: an item made (t its tile, i its row, x, y where it stands now)
local function onItem(t, i, group, x, y)
  local key = t.id or t.key
  local s = seenItem[key]
  if not s then s = {}; seenItem[key] = s end
  if s[i] then return end
  s[i] = true
  local now = clock()
  local d, al = rel(x, y)
  local rk = roadKeyOf(t.key)
  local ra = roadAt[rk] and (now - roadAt[rk]) or -1
  local ta = tileArr['t:' .. tostring(key)] and (now - tileArr['t:' .. tostring(key)]) or -1
  push(items, { now, gi(group), x, y, car.ai, d, al, kindOf(key), ra, ta, car.sp })
end

-- trees.lua: a tile's rows came (addTile)
local function onTile(key, t)
  local k = 't:' .. tostring(key)
  if not tileArr[k] then tileArr[k] = clock() end
  push(arrivals, { clock(), 'tile', tostring(key), t.cx or 0, t.cy or 0, car.ai, t.n or 0 })
end

-- roads.lua: a geotile's content came (t: its tile), or its skin (skin = true)
local function onRoadTile(t, skin)
  local gk = tostring(t.gkey or '')
  local now = clock()
  if not skin and not roadArr[gk] then roadArr[gk] = now end
  local d, al = rel(t.cx or 0, t.cy or 0)
  push(arrivals, { now, skin and 'skin' or 'road', gk, t.cx or 0, t.cy or 0, car.ai, d })
end

-- roads.lua: a mesh part made (what: road / walk / signs / bld ...), t its tile
local function onPart(what, t)
  local gk = tostring(t.gkey or t.key or '')
  local k = what .. '|' .. gk
  local now = clock()
  if what == 'road' and not roadAt[gk] then roadAt[gk] = now end
  if seenPart[k] then return end
  seenPart[k] = true
  local cx, cy = t.cx or 0, t.cy or 0
  local d, al = rel(cx, cy)
  -- (the distance to the tile's square: 0 with the car in it; half its side ~120 m at zoom 17)
  local h = 120
  local bx = math.max(math.abs(car.x - cx) - h, 0)
  local by = math.max(math.abs(car.y - cy) - h, 0)
  local bd = math.sqrt(bx * bx + by * by)
  local ra = roadAt[gk] and (now - roadAt[gk]) or -1
  local ta = roadArr[gk] and (now - roadArr[gk]) or -1
  push(parts, { now, what, gk, cx, cy, car.ai, d, al, bd, ra, ta, car.sp })
end

-- the stock parking system's cars: one moved onto a spot (or made active) is a parked car appearing
local function scanParked()
  local gp = rawget(_G, 'gameplay_parking')
  if not (gp and gp.getParkedCarsList) then return end
  local ok, list = pcall(gp.getParkedCarsList)
  if not ok or type(list) ~= 'table' then return end
  local now = clock()
  for _, id in ipairs(list) do
    local o = be:getObjectByID(id)
    if o then
      local p = o:getPosition()
      local act = o:getActive()
      local old = parkPos[id]
      if act and (not old or not old.act or (p.x - old.x) ^ 2 + (p.y - old.y) ^ 2 > 400) then
        local d, al = rel(p.x, p.y)
        if old then push(parks, { now, p.x, p.y, car.ai, d, al, car.sp }) end
      end
      parkPos[id] = { x = p.x, y = p.y, act = act }
    end
  end
end

-- what should be within PROBE_R of the car and is not
local function probe(st)
  local now = clock()
  local T = package.loaded['beamngEarth/trees']
  local R = package.loaded['beamngEarth/roads']
  local nTree, nProp, nRail = 0, 0, 0
  if T and T.keepupMissing then
    local ok, list = pcall(T.keepupMissing, car.x, car.y, M.PROBE_R)
    if ok and type(list) == 'table' then
      for _, m in ipairs(list) do
        local k = kindOf(m.key)
        if k == 1 then nTree = nTree + 1 elseif k == 2 then nProp = nProp + 1 else nRail = nRail + 1 end
        local d, al = rel(m.x, m.y)
        push(misses, { now, gi(m.group), m.x, m.y, car.ai, d, al, k, tostring(m.key) .. '#' .. m.i, m.reason or '' })
      end
    end
  end
  -- the road geotiles within PROBE_R: their content, skin, road mesh, sidewalks, sign meshes; the trees' tiles there
  local noTile, noSkin, noRoad, noWalk, noSigns, noVeg, noProps = 0, 0, 0, 0, 0, 0, 0
  if R and R.geoKeysNear and R.keepupTile then
    local ok, keys = pcall(R.geoKeysNear, car.x, car.y, M.PROBE_R)
    if ok and type(keys) == 'table' then
      local tt = T and T.debugTiles and T.debugTiles() or {}
      for _, k in ipairs(keys) do
        local s = R.keepupTile(k, st and st.anchorId)
        if not s.tile then noTile = noTile + 1 else
          if not s.skin then noSkin = noSkin + 1 end
          if s.skin and not s.road then noRoad = noRoad + 1 end
          if s.walkWant and not s.walk then noWalk = noWalk + 1 end
          if s.signsWant and not s.signs then noSigns = noSigns + 1 end
          if s.props and not tt['g:p' .. k] then noProps = noProps + 1 end
        end
        if not tt['g:' .. k] then noVeg = noVeg + 1 end
      end
    end
  end
  -- ... and the tile under the car alone (a neighbour within PROBE_R may have no road tile at all: water, a park)
  local cTile, cRoad, cWalk, cVeg = 0, 0, 0, 0
  if R and R.geoKeyAt and R.keepupTile then
    local k = R.geoKeyAt(car.x, car.y)
    if k then
      local s = R.keepupTile(k)
      local tt = T and T.debugTiles and T.debugTiles() or {}
      if not s.tile then cTile = 1 else
        if not s.road then cRoad = 1 end
        if s.walkWant and not s.walk then cWalk = 1 end
      end
      if not tt['g:' .. k] then cVeg = 1 end
    end
  end
  push(probes, { now, car.x, car.y, car.ai, car.sp, nTree, nProp, nRail, noTile, noSkin, noRoad, noWalk, noSigns,
    noVeg, noProps, cTile, cRoad, cWalk, cVeg })
end

-- core.lua, each frame (after the world's updates): the car, the trace, the probe
function M.tick(st)
  if not M.on then return end
  local v = getPlayerVehicle(0)
  if not v or not st then return end
  local p, u = v:getPosition(), v:getVelocity()
  local sp = math.sqrt(u.x * u.x + u.y * u.y)
  car.x, car.y, car.sp, car.ai = p.x, p.y, sp, ai(st.anchor)
  if sp > 1 then car.hx, car.hy = u.x / sp, u.y / sp end
  local now = clock()
  car.t = now
  if now - lastTrace >= M.TRACE_S then
    lastTrace = now
    push(trace, { now, p.x, p.y, car.ai, u.x, u.y })
    scanParked()
  end
  if now - lastProbe >= M.PROBE_S then
    lastProbe = now
    local ok, err = pcall(probe, st)
    if not ok then log('W', 'beamngEarth.keepup', 'probe: ' .. tostring(err)) end
  end
end

local function hooks(on)
  local T = require('beamngEarth/trees')
  local R = require('beamngEarth/roads')
  T.onKeepupItem = on and onItem or nil
  T.onKeepupTile = on and onTile or nil
  R.onKeepupPart = on and onPart or nil
  R.onKeepupTile = on and onRoadTile or nil
end

-- Start recording (from now: what is already there counts as seen, so a run measures only what comes while it drives).
function M.start()
  items, parts, parks, arrivals, probes, misses, trace = {}, {}, {}, {}, {}, {}, {}
  seenItem, seenPart, roadAt, roadArr, tileArr, parkPos = {}, {}, {}, {}, {}, {}
  anchors, anchorIdx, groupIdx, groupNames = {}, {}, {}, {}
  lastTrace, lastProbe = -1, -1
  t0 = clock()
  -- everything shown already: seen (their first appearance was before the run)
  local T = require('beamngEarth/trees')
  for key, t in pairs(T.debugTiles() or {}) do
    local s = {}
    for i in pairs(t.made or {}) do s[i] = true end
    seenItem[t.id or key] = s
  end
  local R = require('beamngEarth/roads')
  if R.keepupSeen then
    for _, e in ipairs(R.keepupSeen()) do
      seenPart[e.what .. '|' .. e.gkey] = true
      if e.what == 'road' then roadAt[e.gkey] = t0 - 1000 end     -- (made before the run: long ready)
    end
  end
  local gp = rawget(_G, 'gameplay_parking')
  if gp and gp.getParkedCarsList then
    for _, id in ipairs(gp.getParkedCarsList() or {}) do
      local o = be:getObjectByID(id)
      if o then local p = o:getPosition(); parkPos[id] = { x = p.x, y = p.y, act = o:getActive() } end
    end
  end
  M.on = true
  hooks(true)
  return 1
end

function M.stop()
  M.on = false
  hooks(false)
  return 1
end

function M.counts()
  return { items = #items, parts = #parts, parks = #parks, arrivals = #arrivals, probes = #probes, misses = #misses,
    trace = #trace, t = clock() - t0 }
end

-- The record as CSV lines (kind first), positions as lat / lon (each row's anchor), written to path (the game's user
-- folder, e.g. '/temp/bne_keepup.csv': lower case, the game's file system lowers a new file's name). Returns the line count.
function M.save(path)
  local out = {}
  local function ll(a, x, y)
    local an = anchors[a]
    if not an then return 0, 0 end
    local la, lo = an:worldToGeodetic(x, y, 0)
    return la, lo
  end
  local f = string.format
  out[#out + 1] = 'H,' .. f('%.3f', t0) .. ',' .. table.concat(groupNames, ';')
  for _, r in ipairs(trace) do
    local la, lo = ll(r[4], r[2], r[3])
    out[#out + 1] = f('T,%.3f,%.7f,%.7f,%.2f,%.2f', r[1], la, lo, r[5], r[6])
  end
  for _, r in ipairs(items) do
    local la, lo = ll(r[5], r[3], r[4])
    out[#out + 1] = f('I,%.3f,%d,%.7f,%.7f,%.1f,%.1f,%d,%.2f,%.2f,%.1f', r[1], r[2], la, lo, r[6], r[7], r[8], r[9], r[10], r[11])
  end
  for _, r in ipairs(parts) do
    local la, lo = ll(r[6], r[4], r[5])
    out[#out + 1] = f('P,%.3f,%s,%s,%.7f,%.7f,%.1f,%.1f,%.1f,%.2f,%.2f,%.1f', r[1], r[2], r[3], la, lo, r[7], r[8], r[9], r[10], r[11], r[12])
  end
  for _, r in ipairs(parks) do
    local la, lo = ll(r[4], r[2], r[3])
    out[#out + 1] = f('K,%.3f,%.7f,%.7f,%.1f,%.1f,%.1f', r[1], la, lo, r[5], r[6], r[7])
  end
  for _, r in ipairs(arrivals) do
    local la, lo = ll(r[6], r[4], r[5])
    out[#out + 1] = f('A,%.3f,%s,%s,%.7f,%.7f,%s', r[1], r[2], r[3], la, lo, tostring(r[7]))
  end
  for _, r in ipairs(probes) do
    local la, lo = ll(r[4], r[2], r[3])
    out[#out + 1] = f('Q,%.3f,%.7f,%.7f,%.1f,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d', r[1], la, lo, r[5], r[6], r[7],
      r[8], r[9], r[10], r[11], r[12], r[13], r[14], r[15], r[16], r[17], r[18], r[19])
  end
  for _, r in ipairs(misses) do
    local la, lo = ll(r[5], r[3], r[4])
    out[#out + 1] = f('M,%.3f,%d,%.7f,%.7f,%.1f,%.1f,%d,%s,%s', r[1], r[2], la, lo, r[6], r[7], r[8], r[9], r[10])
  end
  writeFile(path or '/temp/bne_keepup.csv', table.concat(out, '\n') .. '\n')
  return #out
end

return M
