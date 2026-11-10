-- luajit mod/tests/test_navgraph.lua  (the server's road graph as the stock map's custom node table)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
function vec3(x, y, z) return { x = x, y = y, z = z } end
local N = require('beamngEarth/navgraph')

T.check(N.lanes(2, 1, false) == '-++' and N.lanes(2, 1, true) == '++-' and N.lanes(1, 0, false) == '+',
  'lanes: against first for right-hand traffic')

local g = {
  nodes = { a = { 0, 0, 1, 4 }, b = { 50, 0, 1, 4 }, c = { 100, 0, 1, 3 }, far = { 5000, 0, 1, 3 } },
  edges = { { 'a', 'b', 0, 1, 1, 11.2, 0.8, 4 }, { 'b', 'c', 1, 1, 0, 8.9, 0.8, 3 }, { 'c', 'far', 0, 1, 1, 11.2, 1, 3 },
            { 'b', 'a', 0, 1, 1, 11.2, 0.8, 4 } },
}
local nodes, nn, ne, cuts = N.toNodes(g, { -500, -500, 500, 500 }, 60, false)
T.check(nn == 3 and ne == 2 and not nodes.far, 'clipped to the block: the far node and its edge left out')
T.check(#cuts == 1 and cuts[1][1] == 100 and cuts[1][2] == 0, 'the cut: c, whose road to the far node was left out')
-- GitHub #131: a link 6.4 m up in 8 m (a bridge's first node over its junction) is left out; a steep real street not
local steep = { nodes = { j = { 0, 0, 22.7, 4 }, d = { 8, 0, 29.1, 3 }, h = { 0, 30, 33.7, 3 } },
  edges = { { 'j', 'd', 0, 1, 1, 11.2, 1, 3 }, { 'j', 'h', 0, 1, 1, 11.2, 1, 3 } } }
local sn, _, sne = N.toNodes(steep, { -500, -500, 500, 500 }, 60, false)
T.check(sne == 1 and sn.j.links.h and not sn.j.links.d, 'a link steeper than MAX_GRADE left out; 37 % kept: ' .. sne)
-- a halo (route planning past the box): the far node kept, the cut still at the box's edge
do
  local hn, hnn, hne, hcuts = N.toNodes(g, { -500, -500, 500, 500 }, 60, false, nil, 5000)
  T.check(hnn == 4 and hne == 3 and hn.far ~= nil, 'halo: the node past the box kept, its road too')
  T.check(#hcuts == 1 and hcuts[1][1] == 100, 'halo: the cut stays where the box ends (c)')
end
-- the fast roads' halo (Montana's I-94 / I-90, 2026-11-05): a motorway's nodes kept past the box, a street's not
do
  local fg = {
    nodes = { a = { 0, 0, 1, 4 }, b = { 400, 0, 1, 4 }, m = { 700, 0, 1, 4 }, m2 = { 1200, 0, 1, 4 },
              s = { 400, 300, 1, 3 }, s2 = { 700, 300, 1, 3 } },
    edges = { { 'a', 'b', 1, 2, 0, 29.1, 1, 4 }, { 'b', 'm', 1, 2, 0, 29.1, 1, 4 }, { 'm', 'm2', 1, 2, 0, 29.1, 1, 4 },
              { 'b', 's', 0, 1, 1, 11.2, 0.8, 3 }, { 's', 's2', 0, 1, 1, 11.2, 0.8, 3 } },
  }
  local box = { -500, -500, 500, 500 }
  local fn, _, _, fcuts = N.toNodes(fg, box, 60, false, nil, 0, 650)   -- (the halo when on: FAST_HALO_M is 0 by default)
  T.check(fn.m ~= nil and fn.m2 == nil, 'fast halo: the motorway node 200 m past the box kept, not one 700 m past')
  T.check(fn.b.links.m ~= nil, 'fast halo: its road too')
  T.check(fn.s2 == nil, 'fast halo: a street node past the box left out')
  local bn = N.toNodes(fg, box, 60, false, nil, 0, 0)
  T.check(bn.m == nil, 'no fast halo: the motorway ends at the box')
  T.check(#fcuts >= 1 and fcuts[1][1] <= 440, 'fast halo: the cut stays at the box')
end
-- a respawned car launched (PR #728's spawn velocity): at LAUNCH_SHARE of its road's limit, along a one-way road only
-- its way, not at a junction, not facing off the road, not on a slow street
T.check(math.abs(N.launchSpeed(29, 1.0, true, false) - 29 * N.LAUNCH_SHARE) < 1e-9, 'launch: 90 % of 65 mph')
T.check(N.launchSpeed(29, -1.0, true, false) == nil, 'launch: not against a one-way road')
T.check(N.launchSpeed(29, -1.0, false, false) ~= nil, 'launch: either way on a two-way road')
T.check(N.launchSpeed(29, 0.5, false, false) == nil, 'launch: not when facing off the road (60 degrees)')
T.check(N.launchSpeed(29, 1.0, true, true) == nil, 'launch: not at a junction')
T.check(N.launchSpeed(6.7, 1.0, false, false) == nil, 'launch: not on a 15 mph street')
local cmd = N.spawnVelocityCommand(26.1)
T.check(cmd and cmd:find('thrusters.applyVelocity', 1, true) and cmd:find('beamngEarth_spawnMotion', 1, true)
  and cmd:find('26.100', 1, true), 'launch: the vehicle command pushes the car and spins its wheels')
T.check(N.spawnVelocityCommand(0) == nil and N.spawnVelocityCommand(120) == nil, 'launch: no command for 0 or 120 m/s')
-- oncoming traffic (I-94, 2026-11-05): a respawn spot on the other carriageway, 26 m left of an eastbound player
do
  local function divided(x, y)          -- eastbound carriageway at y = 0, westbound at y = 26 (one-way roads)
    if math.abs(y) <= math.abs(y - 26) then return { dist = math.abs(y), dx = 1, dy = 0, oneWay = true } end
    return { dist = math.abs(y - 26), dx = -1, dy = 0, oneWay = true }
  end
  local box = { -600, -600, 600, 600 }
  local x, y, dx, dy = N.oncomingSpot(0, 0, 1, 0, box, false, divided)
  T.check(x and x > 100 and math.abs(y - 26) <= N.ONCOMING_ON_M and dx == -1 and dy == 0,
    'oncoming: a spot on the westbound carriageway ahead, facing the player: ' .. tostring(x) .. ',' .. tostring(y))
  local function undivided(x, y) return { dist = math.abs(y), dx = 1, dy = 0, oneWay = false } end
  T.check(N.oncomingSpot(0, 0, 1, 0, box, false, undivided) == nil, 'oncoming: no other carriageway, none')
  T.check(N.oncomingSpot(0, 0, 1, 0, { -100, -600, 150, 600 }, false, divided) == nil,
    'oncoming: none past the block\'s edge')
  local lx, ly = N.oncomingSpot(0, 0, 1, 0, box, true, function(x, y)
    if math.abs(y) <= math.abs(y + 26) then return { dist = math.abs(y), dx = 1, dy = 0, oneWay = true } end
    return { dist = math.abs(y + 26), dx = -1, dy = 0, oneWay = true }
  end)
  T.check(lx and math.abs(ly + 26) <= N.ONCOMING_ON_M, 'oncoming, left-hand traffic: the other carriageway is to the right')
end
-- GitHub #353: a car ahead of a player on the move, going its way, is not respawned for the cut ahead
T.check(N.followsPlayer({ x = 200, y = 0 }, { x = 1, y = 0 }, { x = 0, y = 0 }, { x = 30, y = 0 }), 'ahead, same way')
T.check(not N.followsPlayer({ x = -200, y = 0 }, { x = 1, y = 0 }, { x = 0, y = 0 }, { x = 30, y = 0 }), 'behind: not')
T.check(not N.followsPlayer({ x = 200, y = 0 }, { x = -1, y = 0 }, { x = 0, y = 0 }, { x = 30, y = 0 }), 'oncoming: not')
T.check(not N.followsPlayer({ x = 200, y = 0 }, { x = 1, y = 0 }, { x = 0, y = 0 }, { x = 2, y = 0 }), 'player slow: not')
T.check(N.headingToCut(60, 0, 1, 0, cuts, 60), 'a car 40 m from the cut, heading for it')
T.check(not N.headingToCut(60, 0, -1, 0, cuts, 60), '... heading away from it: not')
T.check(not N.headingToCut(60, 0, 0, 1, cuts, 60), '... across: not')
T.check(not N.headingToCut(-20, 0, 1, 0, cuts, 60), '... 120 m from it: not yet')
T.check(N.headingToCut(-20, 0, 1, 0, cuts, 30 * N.CUT_S), '... at 67 mph: from 120 m')
-- built over several frames (navgraph's buildNodes: a coroutine pausing every yieldEvery items): the same tables
do
  local co = coroutine.create(function() return { N.toNodes(g, { -500, -500, 500, 500 }, 60, false, 2) } end)
  local pauses, res = 0, nil
  while true do
    local ok, r = coroutine.resume(co)
    T.check(ok, 'resumes cleanly')
    if coroutine.status(co) == 'dead' then res = r break end
    pauses = pauses + 1
  end
  T.check(pauses >= 3, 'it paused along the way: ' .. pauses)
  T.check(res[2] == nn and res[3] == ne and #res[4] == #cuts and res[1].a.links.b.speedLimit == 11.2
    and not res[1].far, 'the same nodes, edges and cuts as in one go')
end
local ab = nodes.a.links.b
T.check(ab and nodes.b.links.a == ab, 'one edge table shared by both ends (double-sided)')
T.check(ab.inNode == 'a' and ab.outNode == 'b' and ab.inPos == nodes.a.pos and ab.outPos == nodes.b.pos, 'in / out nodes and positions')
T.check(ab.speedLimit == 11.2 and ab.lanes == '-+' and not ab.oneWay and ab.inRadius == 4 and ab.outRadius == 4, 'edge fields')
local bc = nodes.b.links.c
do      -- a road narrowing to a tunnel's portal: the edge's end radii (e[8] at its in node, e[9] at its out node)
  local g2 = { nodes = { p = { 0, 0, 0, 6 }, q = { 50, 0, 0, 3 } }, edges = { { 'p', 'q', 0, 1, 1, 20, 1, 6, 3 } } }
  local n2 = N.toNodes(g2, nil, 60, false)
  local pq = n2.p.links.q
  T.check(pq.inRadius == 6 and pq.outRadius == 3, 'per-end radii: ' .. tostring(pq.inRadius) .. ' ' .. tostring(pq.outRadius))
end
T.check(bc.oneWay and bc.lanes == '+' and bc.inNode == 'b', 'one-way: in -> out is the allowed direction')
T.check(N.inside(nil, 1e9, 0, 60) and not N.inside({ 0, 0, 100, 100 }, 50, 50, 60), 'inside')

-- a new signals table keeps the junctions a car may still remember (ai.lua reads them unchecked)
local got
mapmgr = { signalsData = { j1 = { a = { { action = 2 } } }, j2 = { b = { { action = 0 } }, c = { { action = 1 } } } },
           setSignals = function(d) got = d; mapmgr.signalsData = d end }
local f = loadstring(N.setSignalsLua('{ j2 = { b = { { action = 3 } } }, j3 = { d = { { action = 0 } } } }'))
f()
T.check(got.j3.d[1].action == 0 and got.j2.b[1].action == 3, 'the new signals')
T.check(got.j1.a[1].action == 2 and got.j2.c[1].action == 1, 'old junctions / approaches kept')
mapmgr = { setSignals = function(d) got = d end }
loadstring(N.setSignalsLua('{ j9 = {} }'))()
T.check(got.j9 and not got.j1, 'no old table: the new one as is')
T.check(not N.stuck(nil, 100, 500), 'moving: not stuck')
T.check(not N.stuck(60, 100, 500), 'still 40 s: a long signal, not stuck yet')
T.check(N.stuck(50, 100, 500) and not N.stuck(50, 100, 20), 'still 50 s: stuck, unless next to the player')
local o = { trafficMode = 'enabled', trafficParked = 'enabled', trafficPolice = 'disabled', trafficAmount = -1,
            trafficParkedAmount = 10 }
local c = N.takeChoice(o)
T.check(c.traffic and c.parked and not c.police and c.parkedAmount == 10, 'the configurator choice taken')
T.check(o.trafficMode == 'disabled' and o.trafficParked == 'disabled', 'freeroam spawns nothing before our roads')
T.check(N.takeChoice(o).traffic, 'taken twice (level start runs twice): the first choice kept')
N.restoreChoice()
T.check(o.trafficMode == 'enabled' and o.trafficParked == 'enabled', 'the player\'s options back afterwards')
T.check(not N.takeChoice({ trafficMode = 'fromSetting', trafficParked = 'disabled' }).traffic, 'fromSetting: off')
T.check(not N.takeChoice(nil).parked, 'no freeroam: off (the radial menu still works)')
settings = { getValue = function(k) if k == 'trafficParkedVehicles' then return false end end }
T.check(not N.takeChoice({ trafficMode = 'enabled', trafficParked = 'enabled' }).parked,
  'traffic and parked chosen, the parked vehicles setting off: none (as stock freeroam, GitHub #320)')
N.restoreChoice()
T.check(N.takeChoice({ trafficMode = 'disabled', trafficParked = 'enabled' }).parked, 'parked only: the setting does not apply')
N.restoreChoice()
settings = nil
T.check(math.abs(N.boxMoved({ -708, -452, 196, 452 }, { -1092, -452, -188, 452 }) - 384) < 1e-9,
  'the block moved 384 m: measured between the boxes\' centres')
T.check(N.boxMoved(nil, { 0, 0, 1, 1 }) == 0 and N.boxMoved({}, { 0, 0, 1, 1 }) == 0, 'no box yet: not moved')
T.check(384 >= N.RECLIP_M and 128 < N.RECLIP_M, 'a 384 m move re-clips the graph, one step of 128 m does not')
-- GitHub #353: at motorway speed a respawned car goes ahead, short of the block's edge guard; slower: behind (nil)
do
  local box = { -256, -256, 256, 256 }
  local x, y, dx, dy, lo, hi = N.aheadSpot(0, 0, 30, 0, box)
  T.check(x == 0 and dx == 1 and dy == 0 and lo == N.AHEAD_M[1] and hi == math.min(N.AHEAD_M[2], 256 - N.GUARD_M - 20),
    'aheadSpot: 67 mph east, ahead along the way: ' .. tostring(hi))
  T.check(N.aheadSpot(0, 0, 10, 0, box) == nil, 'aheadSpot: 22 mph: behind the camera as before')
  T.check(N.aheadSpot(150, 0, 30, 0, box) == nil, 'aheadSpot: near the block edge ahead: no room, behind')
  local _, _, _, _, _, h2 = N.aheadSpot(0, 0, 0, -30, box)
  T.check(h2 and h2 <= 256 - N.GUARD_M - 20, 'aheadSpot: south, limited by the box too')
end

-- a rebase moves the graph into the new anchor (core finishRebase): kept and applied again at once
do
  N.enabled = true
  local jd, rf = rawget(_G, 'jsonDecode'), rawget(_G, 'readFile')
  jsonDecode = function() return { nodes = { a = { 100, 200, 5, 3 }, b = { 150, 200, 5, 3 } },
                                   edges = { { 'a', 'b', 0, 1, 1, 13, 1, 3 } }, lines = { { 10, 20, 30, 40, 1 } } } end
  readFile = function() return '{}' end
  T.check(N.onMessage({ anchorId = 7, path = 'x', serial = 1 }, 7), 'a graph for anchor 7')
  local xf = { apply = function(_, x, y, z) return x - 1000, y + 50, z - 2 end }
  T.check(N.transform(xf, 8), 'moved into anchor 8')
  local g = N.currentGraph()
  T.check(g.anchor == 8 and g.nodes.a[1] == -900 and g.nodes.a[2] == 250 and g.nodes.a[3] == 3 and g.nodes.a[4] == 3,
    'its nodes in the new frame, radius kept')
  T.check(g.lines and g.lines[1][1] == -990 and g.lines[1][2] == 70 and g.lines[1][3] == -970 and g.lines[1][5] == 1,
    'its tunnel lines (the minimap) too')
  T.check(not N.transform(nil, 9) and N.currentGraph().anchor == 8, 'no transform: nothing')
  jsonDecode, readFile = jd, rf
end

T.done('test_navgraph')
