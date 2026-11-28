-- luajit mod/tests/test_lodoverlap.lua
-- Building LOD hand-overs on a 130 mph drive (the user, 2026-11-21: "lower LODs stick around too long ... Often you
-- can see 2 LODs overlap"). A simulated server sends the far building LODs of a strip of skyline squares by distance
-- (horizon pieces, skyline tiles, packs, the 16 far buildings tiles of a square, road geotiles with their buildings),
-- each after a random service time (seeded: the same run every time); the mod's roads.lua runs every 60 Hz frame with
-- a three-unit governor and ~2,800 far entries (as after a long drive: the round-robin hide checks visit 80 a frame).
-- Every frame each road geotile cell counts the LODs drawn over it (near buildings, its far tile, its square's pack,
-- skyline or horizon piece): two or more is an overlap frame; none after it was first covered is a hole frame.
-- BNE_LUA_DIR=<dir with beamngEarth/roads.lua ...> runs another revision of the mod (the report only, no checks).
local alt = os.getenv('BNE_LUA_DIR')
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
if alt then package.path = alt .. '/?.lua;' .. package.path end
local S = dofile(dir .. '/stubs.lua')

local function rec()
  return setmetatable({}, { __index = function(_, fn)
    if fn == 'placeRows' or fn == 'placeSpaces' then return nil end
    return function() end
  end })
end
for _, m in ipairs({ 'trees', 'nightlights', 'parking', 'gasstations', 'pumpjacks', 'ambience' }) do
  package.loaded['beamngEarth/' .. m] = rec()
end
rawset(package.loaded['beamngEarth/gasstations'], 'MATERIAL_FILES', {})
readFile = function() return nil end
jsonDecode = function(s) return assert(load('return ' .. s))() end
local car = { x = 0, y = 0, z = 0 }
getPlayerVehicle = function() return { getPosition = function() return vec3(car.x, car.y, car.z) end,
  getVelocity = function() return vec3(58.1, 0, 0) end } end

local geodesy = require('beamngEarth/geodesy')
local geotile = require('beamngEarth/geotile')
local roads = require('beamngEarth/roads')
roads.setGeo(true)
roads.setFarGeo(true)
roads.setSession('s1')

-- a seeded generator (the same drive on every run and every revision)
local seed = 12345
local function rnd()
  seed = (seed * 1103515245 + 12345) % 2147483648
  return seed / 2147483648
end
local function uniform(a, b) return a + (b - a) * rnd() end

local LAT, LON = 39.2834, -76.5934
local A = geodesy.newAnchor(LAT, LON)
local Z = geotile.Z
local gx0, gy0 = geotile.tileOf(LAT, LON)
local sy0 = math.floor(gy0 / 4)
local function worldOf(z, x, y)
  local la, lo = geotile.centre(z, x, y)
  local wx, wy = A:geodeticToWorld(la, lo, 0)
  return wx, wy, 40075016.686 * math.cos(math.rad(la)) / 2 ^ z
end
-- m from the car to tile (z, x, y) (its nearest point; the tile taken as a square round its centre)
local function gap(z, x, y)
  local wx, wy, s = worldOf(z, x, y)
  local dx = math.max(math.abs(car.x - wx) - s / 2, 0)
  local dy = math.max(math.abs(car.y - wy) - s / 2, 0)
  return math.sqrt(dx * dx + dy * dy)
end

-- the strip: skyline squares in three rows round the car's row, 30 squares (28 km) east to west
local SQ0, SQN = math.floor(gx0 / 4) - 10, math.floor(gx0 / 4) + 20
local squares = {}
for sx = SQ0, SQN do for sy = sy0 - 1, sy0 + 1 do squares[#squares + 1] = { sx, sy, geotile.key(sx, sy, Z - 2) } end end
local hzTiles = {}
for _, q in ipairs(squares) do
  local hk = geotile.key(math.floor(q[1] / 8), math.floor(q[2] / 8), Z - 5)
  hzTiles[hk] = hzTiles[hk] or { math.floor(q[1] / 8), math.floor(q[2] / 8), parts = {} }
  hzTiles[hk].parts[#hzTiles[hk].parts + 1] = q[3]
end
local hzOrder = {}
for hk in pairs(hzTiles) do hzOrder[#hzOrder + 1] = hk end
table.sort(hzOrder)

-- reach (m) and service times (s): roughly the server's (roads 640 / 960, far tiles 1,000 whole squares, packs past them
-- to 3 km, skylines past the packs to 6 km, the horizon to 12 km)
local R = { near = { 640, 960, 1, 8 }, F = { 1000, 1200, 0.3, 4 }, P = { 3000, 3200, 0.5, 6, inner = 1000 },
  S = { 6000, 6200, 0.3, 3, inner = 3000 }, H = { 12000, 12500, 0.5, 4 } }

local function farMsg(key, frame, data)
  return { anchorId = 1, key = key, path = '/s/x.json', n = 1, frame = frame, pivot = 0, data = data }
end
local clock = 0
local want = {}       -- id -> { at = delivery time, send = fn } (requested, not delivered)
local have = {}       -- id -> true (delivered, not dropped)
local function request(id, send)
  if have[id] or want[id] then return end
  local kind = id:sub(1, 1) == 'n' and 'near' or id:sub(1, 1)
  want[id] = { at = clock + uniform(R[kind][3], R[kind][4]), send = send }
end
local function drop(id, fn)
  if want[id] then want[id] = nil end
  if have[id] then have[id] = nil; fn() end
end

local function sendSquareF(sx, sy)
  for j = 0, 3 do for i = 0, 3 do
    local x, y = sx * 4 + i, sy * 4 + j
    local k = geotile.key(x, y)
    local la, lo = geotile.centre(Z, x, y)
    request('F' .. k, function()
      roads.onFarTile(farMsg(k, { la, lo }, { v = 1, key = k, frame = { la, lo }, h = 'f',
        shape = '/s/bf_' .. k .. '.cdae', pos = { 0, 0, 0 }, pivot = 0 }), 1)
    end)
  end end
end
local function dropSquareF(sx, sy)
  for j = 0, 3 do for i = 0, 3 do
    local k = geotile.key(sx * 4 + i, sy * 4 + j)
    drop('F' .. k, function() roads.onFarDrop({ anchorId = 1, key = k }) end)
  end end
end

local function server()
  for _, q in ipairs(squares) do
    local sx, sy, sk = q[1], q[2], q[3]
    local g = gap(Z - 2, sx, sy)
    local la, lo = geotile.centre(Z - 2, sx, sy)
    -- far buildings tiles: whole squares
    if g <= R.F[1] then sendSquareF(sx, sy) elseif g > R.F[2] then dropSquareF(sx, sy) end
    -- the pack (not sent within the far tiles' reach; kept until its drop reach)
    if g <= R.P[1] and g > R.P.inner then
      request('P' .. sk, function()
        roads.onFarTile(farMsg(sk, { la, lo }, { v = 1, key = sk, frame = { la, lo }, h = 'p',
          shape = '/s/bp_' .. sk .. '.cdae', pos = { 0, 0, 0 }, pivot = 0 }), 1, 'P')
      end)
    elseif g > R.P[2] then drop('P' .. sk, function() roads.onFarDrop({ anchorId = 1, key = sk }, 'P') end) end
    -- the skyline (past the packs)
    if g <= R.S[1] and g > R.S.inner then
      request('S' .. sk, function()
        roads.onFarTile(farMsg(sk, { la, lo }, { v = 1, key = sk, frame = { la, lo }, h = 's',
          shape = '/s/bs_' .. sk .. '.cdae', pos = { 0, 0, 0 }, pivot = 0 }), 1, true)
      end)
    elseif g > R.S[2] then drop('S' .. sk, function() roads.onFarDrop({ anchorId = 1, key = sk }, true) end) end
    -- road geotiles (their buildings and lean collision)
    for j = 0, 3 do for i = 0, 3 do
      local x, y = sx * 4 + i, sy * 4 + j
      local k = geotile.key(x, y)
      local gk = gap(Z, x, y)
      if gk <= R.near[1] then
        local tla, tlo = geotile.centre(Z, x, y)
        request('n' .. k, function()
          roads.onTile({ anchorId = 1, key = k, frame = { tla, tlo }, data = {
            bld = { shape = '/s/nb_' .. k .. '.cdae', pos = { 0, 0, 0 } },
            bldcol = { shape = '/s/nc_' .. k .. '.cdae', pos = { 0, 0, 0 } } } }, 1, A)
        end)
      elseif gk > R.near[2] then drop('n' .. k, function() roads.onDrop({ anchorId = 1, key = k }) end) end
    end end
  end
  for _, hk in ipairs(hzOrder) do
    local h = hzTiles[hk]
    local g = gap(Z - 5, h[1], h[2])
    if g <= R.H[1] then
      local la, lo = geotile.centre(Z - 5, h[1], h[2])
      request('H' .. hk, function()
        local parts = {}
        for _, sq in ipairs(h.parts) do parts[sq] = { shape = '/s/bh_' .. hk .. '_' .. sq .. '.cdae', pos = { 0, 0, 0 } } end
        roads.onFarTile(farMsg(hk, { la, lo }, { v = 1, key = hk, frame = { la, lo }, h = 'h', parts = parts }), 1, 'H')
      end)
    elseif g > R.H[2] then drop('H' .. hk, function() roads.onFarDrop({ anchorId = 1, key = hk }, 'H') end) end
  end
  -- deliveries due now, in a fixed order
  local due = {}
  for id, w in pairs(want) do if w.at <= clock then due[#due + 1] = id end end
  table.sort(due, function(a, b) if want[a].at ~= want[b].at then return want[a].at < want[b].at end return a < b end)
  for _, id in ipairs(due) do
    local w = want[id]
    want[id] = nil
    have[id] = true
    w.send()
  end
end

-- ~2,600 far tiles elsewhere (nothing in them): the round-robin's load after a long drive
for i = 1, 2600 do
  local x, y = gx0 + (i % 52) * 4, gy0 + 4000 + math.floor(i / 52) * 4
  local k = geotile.key(x, y)
  local la, lo = geotile.centre(Z, x, y)
  roads.onFarTile(farMsg(k, { la, lo }, { v = 1, key = k, frame = { la, lo }, h = 'x' }), 1)
end

-- the counting
local cellsOf = {}    -- square key -> its 16 cell keys
for _, q in ipairs(squares) do
  local out = {}
  for j = 0, 3 do for i = 0, 3 do out[#out + 1] = geotile.key(q[1] * 4 + i, q[2] * 4 + j) end end
  cellsOf[q[3]] = out
end
local stats = { frames = 0, overlapFrames = 0, overlapCells = 0, holeCells = 0, handovers = 0, maxRun = 0 }
local covered, top, run = {}, {}, {}
local function measure()
  local lv = {}       -- cell -> { level = true }
  local function add(cell, l) lv[cell] = lv[cell] or {}; lv[cell][l] = true end
  for _, o in pairs(S.objects) do
    if o.class == 'TSStatic' and not o.hidden then
      local sh = tostring(o.fields.shapeName)
      local kind, rest = sh:match('/s/(b[fpsh])_(.-)%.cdae$')
      local nk = sh:match('/s/nb_(.-)%.cdae$')
      if nk then add(nk, 'near')
      elseif kind == 'bf' then add(rest, 'F')
      elseif kind then
        local sq = kind == 'bh' and rest:match('_(g%d+_%-?%d+_%-?%d+)$') or rest
        for _, c in ipairs(cellsOf[sq] or {}) do add(c, kind) end
      end
    end
  end
  local any = false
  for _, q in ipairs(squares) do
    for _, c in ipairs(cellsOf[q[3]]) do
      local n, names = 0, {}
      for l in pairs(lv[c] or {}) do n = n + 1; names[#names + 1] = l end
      table.sort(names)
      local t = table.concat(names, '+')
      if n >= 2 then
        any = true
        stats.overlapCells = stats.overlapCells + 1
        run[c] = (run[c] or 0) + 1
        if run[c] > stats.maxRun then stats.maxRun = run[c] end
      else
        run[c] = 0
      end
      if n == 0 and covered[c] then stats.holeCells = stats.holeCells + 1 end
      if n > 0 then covered[c] = true end
      if n == 1 and top[c] ~= t then
        if top[c] then stats.handovers = stats.handovers + 1 end
        top[c] = t
      end
    end
  end
  stats.frames = stats.frames + 1
  if any then stats.overlapFrames = stats.overlapFrames + 1 end
end

local budget = 0
local gov = {}
function gov:try() return budget > 0 end
function gov:charge() budget = budget - 1 end
function gov:chargeMotion() end
local DT, MPS = 1 / 60, 58.1     -- 130 mph
local sx0, sy0w = worldOf(Z - 2, SQ0 + 6, sy0)
car.x, car.y = sx0, sy0w + 50
-- 20 s standing (the first load), then 130 s east at 130 mph (7.5 km)
for f = 1, 150 * 60 do
  clock = f * DT
  if clock > 20 then car.x = car.x + MPS * DT end
  server()
  budget = 3
  roads.update(gov, 1, car, nil, A)
  if clock > 20 then measure() end
end
local perSwap = stats.handovers > 0 and stats.overlapCells / stats.handovers or 0
print(string.format('lodoverlap: %d frames, overlap frames %d, overlap cell-frames %d, hand-overs %d, '
  .. 'overlap frames per hand-over %.2f, longest overlap %d frames, hole cell-frames %d',
  stats.frames, stats.overlapFrames, stats.overlapCells, stats.handovers, perSwap, stats.maxRun, stats.holeCells))
if alt then os.exit(0) end
T.check(stats.handovers > 200, 'the drive hands over many cells (' .. stats.handovers .. ')')
T.check(stats.overlapFrames == 0, 'no frame draws two building LODs over the same cell (' .. stats.overlapFrames .. ')')
T.check(stats.holeCells == 0, 'no cell left without its buildings once covered (' .. stats.holeCells .. ' cell-frames)')
T.done('test_lodoverlap')
