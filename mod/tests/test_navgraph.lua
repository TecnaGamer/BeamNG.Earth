-- luajit mod/tests/test_navgraph.lua  (the server's road graph as the stock map's custom node table)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
function vec3(x, y, z) return { x = x, y = y, z = z } end
local N = require('beamngEarth/navgraph')

T.check(N.lanes(2, 1, false) == '-++' and N.lanes(2, 1, true) == '++-' and N.lanes(1, 0, false) == '+',
  'lanes: against first for right-hand traffic')
do
  local rg = { nodes = { a = { 0, 0, 0, 4 }, b = { 40, 0, 0, 4 } },
    edges = { { 'a', 'b', 0, 1, 1, 20, 1, 4, nil, nil, false, true } } }
  local rn = N.toNodes(rg, nil, 60, false)
  T.check(rn.a.links.b.rumble == true, 'rumble-line eligibility survives graph decoding')
end

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
T.check(N.followsPlayer({ x = 200, y = 0 }, { x = 1, y = 0 }, { x = 0, y = 0 }, { x = 30, y = 0 }, 31),
  'ahead, as fast: follows')
T.check(not N.followsPlayer({ x = 200, y = 0 }, { x = 1, y = 0 }, { x = 0, y = 0 }, { x = 25, y = 0 }, 31),
  'ahead and faster: outruns the graph, not following (GitHub #855)')
T.check(N.stopReach(31) > 200 and N.stopReach(10) < 60 + 1, 'a dead end seen as far as the car needs to stop')
T.check(N.headingToCut(60, 0, 1, 0, cuts, 60), 'a car 40 m from the cut, heading for it')
T.check(not N.headingToCut(60, 0, -1, 0, cuts, 60), '... heading away from it: not')
T.check(not N.headingToCut(60, 0, 0, 1, cuts, 60), '... across: not')
T.check(not N.headingToCut(-20, 0, 1, 0, cuts, 60), '... 120 m from it: not yet')
T.check(N.headingToCut(-20, 0, 1, 0, cuts, 30 * N.CUT_S), '... at 67 mph: from 120 m')
-- GitHub #855: a car that reached the cut no longer heads for it — stopped at the dead end at an angle, or turning
-- round across the road — and is respawned at once, not after STUCK_S
T.check(N.nearCut(95, 3, cuts, N.CUT_NEAR_M), 'a car 6 m from the cut is near it, whichever way it points')
T.check(not N.nearCut(0, 0, cuts, N.CUT_NEAR_M), '... 100 m away: not')
T.check(N.cutStopped(0.2, 0, 0.98, false, true), 'stopped facing the dead end')
T.check(N.cutStopped(0.2, 0, 0.1, false, false), 'stopped mid-turn, across its road')
T.check(N.cutStopped(0.3, 0, -0.9, true, false), 'stood turned round on a one-way road')
T.check(not N.cutStopped(0.2, 0, 0.98, false, false),
  'stopped aligned with its road, facing past the cut (a red light on a through road): left alone')
T.check(not N.cutStopped(0.2, 0, nil, nil, false), 'stopped off any road the map knows, not facing a cut: left alone')
T.check(not N.cutStopped(0.3, 0, -0.9, false, false),
  'stood turned round on a two-way road: reads as ordinary oncoming traffic (uncaught, by design)')
T.check(N.cutStopped(2.0, 1.9, 0.2, false, false), 'crawling through the turn, across its road')
T.check(N.cutStopped(4, -3.8, nil, nil, false), 'backing through it')
T.check(N.cutStopped(6, 5.5, 0.2, false, false), 'turning across the road at speed (78 degrees off its axis)')
T.check(N.cutStopped(6, 5.5, -0.9, true, false), 'driving along a one-way road against it')
T.check(not N.cutStopped(8, 7.9, 0.95, false, false), 'driving on along the road: left alone')
T.check(not N.cutStopped(8, 7.9, -0.95, false, false), 'back the other way on a two-way road: left alone')
-- a long frame (0.3-5 s hitches in session logs) widens the window by the missed time at the car's speed
T.near(N.cutReach(15, false, 0), 75, 1e-9, 'reach at 15 m/s: the stopping distance')
T.near(N.cutReach(15, false, 4.5), 142.5, 1e-9, 'a 5 s frame: widened by the missed 4.5 s at 15 m/s')
T.near(N.cutReach(0, true, 0), N.CUT_FRESH_M, 1e-9, 'a fresh car standing: CUT_FRESH_M as before')
T.check(not N.headingToCut(-40, 0, 1, 0, cuts, N.cutReach(20, false, 0)),
  'a car 140 m from the cut at 20 m/s: outside the normal window')
T.check(N.headingToCut(-40, 0, 1, 0, cuts, N.cutReach(20, false, 2)),
  '... after a 2.5 s frame: the widened window still catches it')
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

-- adaptive traffic amount (PR #728 / #773): the graph's trafficProfile kept through a rebase; the target capped at the
-- player's amount, whole cars, one car a step; off by default
do
  T.check(N.ADAPTIVE == false, 'adaptive traffic off by default')
  T.check(N.adaptiveTarget(4.4, 10) == 4 and N.adaptiveTarget(4.6, 10) == 5 and N.adaptiveTarget(64, 12) == 12
    and N.adaptiveTarget(-1, 10) == 0 and N.adaptiveTarget(nil, 10) == nil and N.adaptiveTarget(0 / 0, 10) == nil,
    'adaptive target: rounded, capped at the player\'s amount, never negative')
  T.check(N.adaptiveStep(12, 7) == 11 and N.adaptiveStep(3, 8) == 4 and N.adaptiveStep(5, 5) == 5,
    'adaptive step: one car at a time')
  local jd, rf = rawget(_G, 'jsonDecode'), rawget(_G, 'readFile')
  jsonDecode = function() return { nodes = { a = { 0, 0, 5, 3 }, b = { 50, 0, 5, 3 } },
    edges = { { 'a', 'b', 0, 1, 1, 13, 1, 3, nil, nil, true } }, trafficProfile = { target = 6.5, band = 'suburban' } } end
  readFile = function() return '{}' end
  T.check(N.onMessage({ anchorId = 3, path = 'x', serial = 2 }, 3) and N.currentGraph().trafficProfile.target == 6.5,
    'the graph keeps its traffic profile')
  local roughNodes = N.toNodes(jsonDecode(), nil, 60, false)
  T.check(roughNodes.a.links.b.rough == true, 'the graph keeps the rough-road traffic speed flag')
  N.transform({ apply = function(_, x, y, z) return x, y, z end }, 4)
  T.check(N.currentGraph().trafficProfile.band == 'suburban', '... through a rebase')
  local st = N.trafficStatus()
  T.check(st.enabled == false and st.profile and st.profile.target == 6.5, 'traffic status: off, the profile shown')
  N.setAdaptive(true)
  T.check(N.ADAPTIVE == true and N.trafficStatus().enabled, 'the setting turns it on')
  N.setAdaptive(false)
  -- the stock traffic's ceiling moved toward the profile's 6.5 -> 7 cars, one every ADAPTIVE_STEP_S
  local gt0, log0 = rawget(_G, 'gameplay_traffic'), rawget(_G, 'log')
  local vars = { activeAmount = 10 }
  gameplay_traffic = { getState = function() return 'on' end, getTrafficVars = function() return vars end,
    getNumOfTraffic = function() return 10 end, setActiveAmount = function(n) vars.activeAmount = n end,
    getTrafficData = function() return {} end }
  if not log0 then log = function() end end
  N.takeChoice({ trafficMode = 'enabled', trafficParked = 'disabled' })
  N._startTraffic()
  N._adaptiveTick(100)
  T.check(vars.activeAmount == 10, 'off: the player\'s amount untouched')
  N.setAdaptive(true)
  N._adaptiveTick(101)
  T.check(vars.activeAmount == 9, 'on: one car down toward the target: ' .. tostring(vars.activeAmount))
  N._adaptiveTick(103)
  T.check(vars.activeAmount == 9, '... not again within ADAPTIVE_STEP_S')
  for t = 106, 140, 5 do N._adaptiveTick(t) end
  T.check(vars.activeAmount == 7 and N.trafficStatus().target == 7, 'settled at the target: ' .. tostring(vars.activeAmount))
  vars.activeAmount = 0                       -- (the player toggled the traffic off)
  for t = 146, 170, 5 do N._adaptiveTick(t) end
  T.check(vars.activeAmount == 0 and N.trafficStatus().paused, 'toggled off: left off')
  vars.activeAmount = 4                       -- (on again with another amount: theirs now, under the target)
  for t = 176, 200, 5 do N._adaptiveTick(t) end
  T.check(vars.activeAmount == 4 and N.trafficStatus().base == 4, 'the player\'s new amount caps it: ' ..
    tostring(vars.activeAmount))
  vars.activeAmount = 10
  for t = 206, 230, 5 do N._adaptiveTick(t) end
  T.check(vars.activeAmount == 7, 'raised again: back down to the target')
  N.setAdaptive(false)
  N._adaptiveTick(240)
  T.check(vars.activeAmount == 10, 'off again: the player\'s amount back: ' .. tostring(vars.activeAmount))
  N._adaptiveTick(245)
  T.check(vars.activeAmount == 10 and N.trafficStatus().base == nil, 'and left alone')
  N.reset()
  gameplay_traffic = gt0
  if not log0 then log = nil end
  jsonDecode, readFile = jd, rf
end

-- regional traffic vehicles: the road rules' country -> the stock levels' region, the game's own filters; none
-- elsewhere or without smart selections (the game's own group then)
do
  T.check(N.regionOf('US') == 'northAmerica' and N.regionOf('de') == 'europe' and N.regionOf('JP') == 'asiaPacific'
    and N.regionOf('BR') == nil and N.regionOf(nil) == nil, 'regions by country')
  local set = { trafficSmartSelections = true, trafficSimpleVehicles = false, trafficAllowMods = false }
  local get = function(k) return set[k] end
  local p, region = N.regionalGroupParams('DE', get)
  T.check(region == 'europe' and p.filters.Region.europe == 1 and p.filters.Region.default == 1
    and p.filters['Config Type'].Police == 0 and p.country == 'Germany' and p.minPop == 50, 'europe group params')
  T.check(N.regionalGroupParams('NL', get).country == 'default', 'a country the game has no name for: default')
  T.check(N.regionalGroupParams('BR', get) == nil, 'no region: the game\'s own group')
  set.trafficSmartSelections = false
  T.check(N.regionalGroupParams('US', get) == nil, 'smart selections off: the game\'s own group')
  set.trafficSmartSelections, set.trafficSimpleVehicles = true, true
  local ps, rs = N.regionalGroupParams('US', get)
  T.check(rs == 'northAmerica' and ps.filters.Type.proptraffic == 1 and ps.filters.Type.car == nil
    and ps.filters.Region.northAmerica == 1 and ps.minPop == 0 and ps.allConfigs, 'simple vehicles: of the region too')
  set.trafficSimpleVehicles, set.trafficAllowMods = false, true
  local pm = N.regionalGroupParams('US', get)
  T.check(pm.allMods and pm.filters.Type.automation == 1 and pm.minPop == 0, 'mods allowed: as the game')
end

-- GitHub #855 / #72 / #279 end to end: the guard respawns a car stopped or turning at a cut at once, honours
-- COOLDOWN_S, leaves cars far from cuts alone, and a long frame cannot carry a car past the catch window
do
  N.reset()
  N.takeChoice(nil)
  N.enabled = true
  local names = { 'jsonDecode', 'readFile', 'writeFile', 'jsonEncode', 'map', 'be', 'path',
                  'getObjectByID', 'gameplay_traffic' }
  local sg = {}
  for _, k in ipairs(names) do sg[k] = rawget(_G, k) end
  local log0 = rawget(_G, 'log')
  if not log0 then log = function() end end
  local clock0 = os.clock
  local now = 1000
  os.clock = function() return now end
  local function v(x, y, z)
    return { x = x, y = y, z = z or 0, length = function(s) return math.sqrt(s.x * s.x + s.y * s.y + s.z * s.z) end }
  end
  -- c is a junction: the a-b-c road loses its leg to `far` (the cut), and the c-s road runs through north
  jsonDecode = function() return { nodes = { a = { 0, 0, 1, 4 }, b = { 50, 0, 1, 4 }, c = { 100, 0, 1, 3 },
      far = { 5000, 0, 1, 3 }, s = { 100, 200, 1, 4 } },
    edges = { { 'a', 'b', 0, 1, 1, 11.2, 0.8, 4 }, { 'b', 'c', 0, 1, 1, 11.2, 0.8, 3 },
              { 'c', 'far', 0, 1, 1, 11.2, 1, 3 }, { 'c', 's', 0, 1, 1, 11.2, 0.8, 4 } } } end
  readFile = function() return '{}' end
  writeFile = function() end
  jsonEncode = function() return '{}' end
  path = { getPathLevelInfo = function() return '' end }
  local world = {}
  map = { reset = function(n) world.nodes = n end, getMap = function() return world end,
          findClosestRoad = function(p) if p.x > 97 and p.y > 5 then return 'c', 's' end return 'b', 'c' end }
  be = { getPlayerVehicleID = function() return -1 end, queueAllObjectLua = function() end,
         sendToMailbox = function() end }
  local cars, ports = {}, {}
  getObjectByID = function(id)
    local c = cars[id]
    if not c then return nil end
    return { getActive = function() return true end, getPosition = function() return c.pos end,
             getVelocity = function() return c.vel end, getDirectionVector = function() return c.dir end }
  end
  gameplay_traffic = {
    getState = function() return 'on' end,
    getTrafficData = function()
      local d = {}
      for id in pairs(cars) do d[id] = { isAi = true } end
      return d
    end,
    getTrafficList = function()
      local l = {}
      for id in pairs(cars) do l[#l + 1] = id end
      return l
    end,
    forceTeleport = function(id) ports[id] = (ports[id] or 0) + 1 end,
  }
  local box = { -500, -500, 500, 500 }
  T.check(N.onMessage({ anchorId = 1, path = 'x', serial = 1 }, 1), 'guard: a graph whose clip cuts c at (100, 0)')
  N.update(1, box)              -- applies the graph; the guard runs from the next frame on
  T.check(N.status().cuts == 1, 'guard: applied, one cut')
  cars[101] = { pos = v(95, 2), vel = v(0, 0), dir = v(0, 1) }      -- stopped at the dead end, turned off it
  cars[102] = { pos = v(92, -2), vel = v(-3, 0), dir = v(1, 0) }    -- backing through its turn, nose at the cut
  cars[103] = { pos = v(0, 0), vel = v(0, 0), dir = v(1, 0) }       -- stopped far from any cut (a signal queue)
  now = 1001
  N.update(1, box)
  T.check(ports[101] == nil and ports[102] == nil, 'guard: both just placed (FRESH_S): not yet')
  now = 1007                    -- past FRESH_S; stood 6 s, far less than STUCK_S = 45
  N.update(1, box)
  T.check(ports[101] == 1, 'guard: stopped at the cut, turned off it: respawned at once, not after STUCK_S')
  T.check(ports[102] == 1, 'guard: turning round at the cut (backing): respawned at once')
  T.check(ports[103] == nil, 'guard: a car stopped far from any cut: untouched')
  now = 1009                    -- within COOLDOWN_S of those respawns
  N.update(1, box)
  T.check(ports[101] == 1 and ports[102] == 1, 'guard: the same car within COOLDOWN_S: not again')
  now = 1016                    -- past COOLDOWN_S, still wedged at the cut
  N.update(1, box)
  T.check(ports[101] == 2, 'guard: still at the cut past COOLDOWN_S: again')
  -- a long frame: a car outside the window drives to the cut, stops and turns during the 5 s the guard missed.
  -- Before the lag widening, the 110 m it covered read as a respawn's jump (JUMP_M = 30): seenAt reset, the car
  -- "fresh" and uncaught for FRESH_S more, and headingToCut false once turned — it stood blocking the road.
  cars[104] = { pos = v(-30, 0), vel = v(20, 0), dir = v(1, 0) }
  now = 1017
  N.update(1, box)              -- first seen: 130 m from the cut, outside cutReach(20 m/s) = 110 m (+ a frame's lag)
  T.check(ports[104] == nil, 'guard: a car 130 m from the cut at 20 m/s: outside the window')
  cars[104].pos, cars[104].vel, cars[104].dir = v(80, 0), v(0.3, 0.4), v(0, 1)
  now = 1022                    -- one 5 s frame later: it reached the cut, stopped and began its turn meanwhile
  N.update(1, box)
  T.check(ports[104] == 1, 'guard: a 5 s frame carried it to the cut, stopped and turning: still caught at once')
  -- a car braking for a red light on the THROUGH road of the cut junction, aligned with its road and facing past
  -- the dead end, is not the cut's and stays put
  cars[105] = { pos = v(100, 25), vel = v(0, 8), dir = v(0, 1) }   -- northbound on the c -> s road, at the stop line
  now = 1023
  N.update(1, box)              -- first seen
  now = 1029                    -- past FRESH_S
  N.update(1, box)
  T.check(ports[105] == nil, 'guard: driving the through road past the cut junction: untouched')
  cars[105].vel = v(0, 2)       -- braking for the light, under CUT_NEAR_MS
  now = 1030
  N.update(1, box)
  cars[105].vel = v(0, 0)       -- stopped at the light, 25 m from the cut node
  now = 1031
  N.update(1, box)
  T.check(ports[105] == nil, 'guard: stopped at a red light on the through road, facing past the dead end: stays')
  -- a car stopped FACING the dead end is still taken at once (headingToCut's facing fallback, as before)
  cars[106] = { pos = v(93, 0), vel = v(0, 0), dir = v(1, 0) }
  now = 1032
  N.update(1, box)
  T.check(ports[106] == 1, 'guard: stopped facing the dead end: respawned at once')
  -- paused: nothing is taken while the player sits in the menu (as the stuck and launch paths)
  local tc0 = rawget(table, 'clear')
  table.clear = tc0 or function(t) for k in pairs(t) do t[k] = nil end end
  simTimeAuthority = { getPause = function() return true end }
  local p101 = ports[101]
  now = 1047                    -- car 101 still wedged at the cut, past COOLDOWN_S of its last respawn
  N.update(1, box)
  T.check(ports[101] == p101, 'guard: paused: the car wedged at the cut is left alone')
  simTimeAuthority = nil
  now = 1048
  N.update(1, box)
  T.check(ports[101] == p101 + 1, 'guard: unpaused: taken again')
  table.clear = tc0
  for _, k in ipairs(names) do _G[k] = sg[k] end
  if not log0 then log = nil end
  os.clock = clock0
  N.reset()
end

T.done('test_navgraph')
