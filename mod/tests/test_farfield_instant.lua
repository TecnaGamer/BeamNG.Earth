-- luajit mod/tests/test_farfield_instant.lua
-- The far ground's LOD swaps by default (crossover.CROSS = 0, COVER_SINK_S = 0; 2026-11-21, the user: "lower LODs
-- stick around too long for buildings and terrain. Often you can see 2 LODs overlap"): an in-view swap group hides
-- its old tile in the frame its new tiles show, and a far tile the terrain block newly covers drops to its cover
-- state in the block's commit frame. Counts the frames both LODs of the same ground are drawn, per swap, with the
-- default and with the old C2 crossover (for the report).
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
require('beamngEarth/lattice').configure(1024, 2.0)
require('beamngEarth/follow').configure()
require('beamngEarth/rebase').configure(2048)
local farfield = require('beamngEarth/farfield')
farfield.SPARE_MAX = 0
local crossover = require('beamngEarth/crossover')
T.check(crossover.CROSS == 0 and crossover.COVER_SINK_S == 0, 'defaults: instant swaps, no cover sink')

local clk = 0
local free = { clock = function() clk = clk + 1; return clk end }
function free:try() return true end
function free:charge() end
function free:chargeMotion() end
local DT = 1 / 60
local function size(l) return 256 * 2 ^ l end
local function key(l, i, j) return l .. '_' .. i .. '_' .. j end
local function tile(a, l, i, j, opts)
  opts = opts or {}
  return { type = 'far_tile', anchorId = a, key = key(l, i, j), level = l,
    shape = '/temp/beamngEarth/t/far_' .. a .. '_' .. key(l, i, j) .. '.cdae',
    pos = { i * size(l), j * size(l), opts.z or 100 }, err = opts.err or 0.5, sink = opts.sink, state = 'group' }
end
local function swap(a, id, add, remove, dIn, dOut)
  return { type = 'far_swap', anchorId = a, id = id, add = add, remove = remove or {}, dIn = dIn, dOut = dOut }
end
local function obj(k) local t = farfield._tile(k); return t and S.objects[t.id] end
local function visible(o) return o ~= nil and S.objects[o.id] ~= nil and not o.hidden end
local cam = { x = 0, y = 0, z = 200, fx = 0, fy = 1, fz = 0, fovDeg = 60 }
local function step(n) for _ = 1, n or 1 do farfield.update(free, DT, cam) end end

-- Splits of L1 tiles into their four L0 children north of the camera, in view; returns the frames (out of 60 after
-- each group starts) that drew the parent and a child together, summed over `n` splits, and the splits run.
local nextId, nextI = 1, 0
local function splits(n)
  local overlap, runs = 0, 0
  for _ = 1, n do
    local pi, pj = nextI, 8
    nextI = nextI + 1
    farfield.onTile(tile(1, 1, pi, pj))
    step()
    farfield.onSwap(swap(1, nextId, { key(1, pi, pj) })); nextId = nextId + 1
    step(2)
    local parent = obj(key(1, pi, pj))
    local kids = { key(0, 2 * pi, 2 * pj), key(0, 2 * pi + 1, 2 * pj), key(0, 2 * pi, 2 * pj + 1), key(0, 2 * pi + 1, 2 * pj + 1) }
    for _, k in ipairs(kids) do
      local l, i, j = k:match('^(%-?%d+)_(%-?%d+)_(%-?%d+)$')
      farfield.onTile(tile(1, tonumber(l), tonumber(i), tonumber(j), { z = 101, err = 0.3 }))
    end
    step()
    farfield.onSwap(swap(1, nextId, kids, { key(1, pi, pj) }, 1.0, 1.0)); nextId = nextId + 1
    local started = false
    for _ = 1, 60 do
      step()
      local anyKid = false
      for _, k in ipairs(kids) do if visible(obj(k)) then anyKid = true end end
      if anyKid then started = true end
      if anyKid and visible(parent) then overlap = overlap + 1 end
    end
    if started then runs = runs + 1 end
  end
  return overlap, runs
end

farfield.switchAnchor(1, false, { cx = 0, cy = 0 })
farfield.setNearVisual('mesh')
local x0 = farfield.stats().crossovers
local o0, r0 = splits(5)
T.check(r0 == 5, 'every split ran (' .. r0 .. ')')
T.check(o0 == 0, 'default: no frame draws a parent and its children together (' .. o0 .. ' frames in 5 splits)')
T.check(farfield.stats().crossovers == x0, 'no crossover ran')
crossover.CROSS = 1
local o1, r1 = splits(5)
crossover.CROSS = 0
print(string.format('farfield swaps in view: overlap frames per swap %.1f with the C2 crossover, %.1f now',
  r1 > 0 and o1 / r1 or 0, r0 > 0 and o0 / r0 or 0))
T.check(o1 > 0, 'the C2 crossover (CROSS = 1) still runs on request')

-- the terrain block newly covering a far tile: it drops to its cover state in the commit frame (detail / terrain
-- mode: the block drawn). An edge-ring tile sinks under the block at once, an interior one hides at once.
local function coverFrames(sinkS)
  crossover.COVER_SINK_S = sinkS
  farfield.setNearVisual('terrain')
  farfield.onBlockCommit(-4096, 0)          -- (the block far west: the tiles below uncovered)
  local e = key(0, 4, 0)                    -- [1024, 1280]: the edge ring of the block at (1024, 0)
  local i = key(0, 6, 0)                    -- [1536, 1792]: interior of it
  for _, k in ipairs({ e, i }) do
    if not farfield._tile(k) then
      local l, a, b = k:match('^(%-?%d+)_(%-?%d+)_(%-?%d+)$')
      farfield.onTile(tile(1, tonumber(l), tonumber(a), tonumber(b), { sink = 2.0, z = 50 }))
      step()
      farfield.onSwap(swap(1, nextId, { k })); nextId = nextId + 1
      step(2)
    end
  end
  T.check(visible(obj(e)) and obj(e).pos.z == 50 and visible(obj(i)), 'uncovered: shown at true height')
  farfield.onBlockCommit(1024, 0)
  local n = 0
  for _ = 1, 30 do
    local over = (visible(obj(e)) and obj(e).pos.z > 50 - 2.0 + 1e-9) or visible(obj(i))
    if over then n = n + 1 end
    step()
  end
  return n
end
local c1 = coverFrames(0.3)
local c0 = coverFrames(0)
print(string.format('farfield cover at a block commit: frames a covered tile stands over the block %d with a 0.3 s sink, %d now', c1, c0))
T.check(c0 == 0, 'default: a newly covered tile is under the block (edge) or hidden (interior) in the commit frame')
T.check(c1 > 0, 'the old 0.3 s sink kept it over the block for frames')
T.done('test_farfield_instant')
