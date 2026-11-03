-- Trace of the mod's pure rule modules (governor.lua, follow.lua, rebase.lua) on a deterministic
-- pseudo-random schedule, for server/tests/test_modsim.py, which runs the Python ports
-- (server/tests/modsim.py) on the same schedule and compares line by line. 'F' lines: the tables the modules start
-- with (the 1 km block, 1024 x 1 m). 'M' lines (M4, docs/V1_4_SPEC.md 3.5), by the block's width: the same rules
-- after core.lua's setBlockSize(1024, 2.0) (the 2 km table, an old server's v1.3.1 block), then setBlockSize(1024,
-- 1.0) (back to the 1 km follow table with its fast mode and commit-time re-check, the stale-anchor rule,
-- centreNear / aheadLead of core.lua's rebaseHint).
--   luajit modsim_trace.lua <mod lua/ge/extensions dir> <frames> <seed>
local dir, frames, seed = arg[1], tonumber(arg[2]), tonumber(arg[3])
package.path = dir .. '/?.lua;' .. package.path
local governor = require('beamngEarth/governor')
local follow = require('beamngEarth/follow')
local rebase = require('beamngEarth/rebase')
local lattice = require('beamngEarth/lattice')

-- Park-Miller: exact in doubles (products < 2^53), identical in Python
local x = seed
local function rnd() x = (x * 16807) % 2147483647; return x / 2147483647 end
local function f(v) return string.format('%.17g', v) end

local g = governor.new({ clock = function() return 0 end })
local wall = nil
local heavyWant = false
local storm = false -- a run of busy (non-calm) frames
for fr = 1, frames do
  local r1, r2, r3, r4 = rnd(), rnd(), rnd(), rnd()
  if r3 < 0.01 then heavyWant = true end
  if r3 > 0.995 then storm = not storm end
  g:setLoading(r4 < 0.05)
  g:beginFrame(r1 < 0.8, wall)
  local granted = false
  if heavyWant and g:requestHeavy(r4 > 0.995) then granted, heavyWant = true, false end
  g:chargeMotion(r2 * 0.1)
  local units, spent = 0, r2 * 0.1
  for i = 1, 12 do
    local kind = (i % 3 == 1) and 'reveal' or 'cdae'
    if not g:try(kind) then break end
    local cost = kind == 'reveal' and 0.05 or (1 + ((fr * 7 + i * 3) % 7) * 0.5)
    g:charge(kind, cost)
    units, spent = units + 1, spent + cost
  end
  wall = 11.5 + spent + (granted and 45 or 0) + ((storm and fr % 2 == 0) and 8 or 0) + r2 * 3
  print(table.concat({ 'G', fr, f(g.b), f(g.tokens), units, granted and 1 or 0, f(g.base or -1), f(g.est.cdae) }, ' '))
end

for i = 1, frames do
  local px, py = (rnd() - 0.5) * 6000, (rnd() - 0.5) * 6000
  local vx, vy = (rnd() - 0.5) * 80, (rnd() - 0.5) * 80
  local cx, cy = follow.snap(px + (rnd() - 0.5) * 1600), follow.snap(py + (rnd() - 0.5) * 1600)
  local kind, ready, gateOk = rnd(), rnd() < 0.5, rnd() < 0.5
  local pending = nil
  if kind < 0.6 then
    pending = { cx = follow.snap(px + (rnd() - 0.5) * 1600), cy = follow.snap(py + (rnd() - 0.5) * 1600), ready = ready }
  end
  local now, retry = rnd() * 10, rnd() * 10
  local a, tx, ty = follow.decide(px, py, vx, vy, cx, cy, pending, now, retry, function() return gateOk end)
  local tgx, tgy = follow.target(px, py, vx, vy)
  local dist, dd = rnd() * 18000, rnd() -- spans the default rebase distance (15 km)
  local rp = nil
  if dd < 0.3 then rp = { purpose = 'rebase', ready = dd < 0.2 } elseif dd < 0.4 then rp = { purpose = 'resync', ready = true } end
  local stale = rnd() * 900
  local ra = rebase.decide(dist, rp, stale, now, retry)
  print(table.concat({ 'F', i, tostring(a), f(tx or 0), f(ty or 0), f(tgx), f(tgy), tostring(ra) }, ' '))
end

-- The 2 km block, then back to the 1 km one (configure switches the tables and the derived locals)
for _, square in ipairs({ 2.0, 1.0 }) do
  local _, _, size = lattice.configure(1024, square)    -- (size: the block's width, m)
  follow.configure()
  rebase.configure(size)
  for i = 1, frames do
    local px, py = (rnd() - 0.5) * 3000, (rnd() - 0.5) * 3000
    local sp, hd = rnd() * 260, rnd() * 2 * math.pi -- spans the fast mode (> 150 m/s)
    local vx, vy = sp * math.cos(hd), sp * math.sin(hd)
    local cx, cy = follow.snap(px + (rnd() - 0.5) * 900), follow.snap(py + (rnd() - 0.5) * 900)
    local kind, ready, gateOk = rnd(), rnd() < 0.6, rnd() < 0.7
    local pending = nil
    if kind < 0.7 then
      pending = { cx = follow.snap(px + (rnd() - 0.5) * 900), cy = follow.snap(py + (rnd() - 0.5) * 900), ready = ready }
    end
    local lr = rnd()
    local latency = nil -- (`c and nil or x` would always be x)
    if lr >= 0.2 then latency = lr * 2 end
    local now, retry = rnd() * 10, rnd() * 10
    local a, tx, ty = follow.decide(px, py, vx, vy, cx, cy, pending, now, retry, function() return gateOk end, latency)
    local tgx, tgy = follow.target(px, py, vx, vy)
    local dist, dd = rnd() * 18000, rnd()
    local rp = nil
    if dd < 0.5 then rp = { purpose = 'rebase', ready = dd < 0.4 } end
    local stale, er = rnd() * 600, rnd()
    local edge = nil
    if er >= 0.1 then edge = (er - 0.4) * 600 end
    local ra = rebase.decide(dist, rp, stale, now, retry, edge)
    local measured = nil
    if lr >= 0.5 then measured = lr * 4 end
    local lead = rebase.aheadLead(measured)
    local nx, ny = follow.centreNear(vx * lead, vy * lead, vx * (lead - 1), vy * (lead - 1), follow.reachFor(vx, vy))
    local tt = follow.timeTo(px, py, vx, vy, cx, cy, follow.RECHECK_SAFE)
    print(table.concat({ 'M', size, i, tostring(a), f(tx or 0), f(ty or 0), f(tgx), f(tgy), tostring(ra), f(lead), f(nx), f(ny),
      tt == math.huge and 'inf' or f(tt) }, ' '))
  end
end
