-- luajit mod/tests/test_tramsignals.lua  (tram signals, tramsignals.lua; plan 046 stage H): the phases of a signal on its own
-- cycle and bound to a car-signal sequence, the lamps of each state, a signal made per row and gone with its tile, moved by a
-- rebase, and the navgraph's third phase (the trams' window) at a junction with a tram signal near.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local clock = 0
os.clockhp = function() return clock end
MatrixF = function()
  local m = {}
  function m:setFromEuler(v) self.yaw = v.z end
  function m:setPosition(p) self.pos = p end
  return m
end
scenetree.MissionGroup = S.newObject('SimGroup')
local newObject = S.newObject
S.newObject = function(class)
  local o = newObject(class)
  function o:setTransform(m) self.xf = m; self.pos = m.pos end
  return o
end
createObject = function(class) return S.newObject(class) end
log = function() end
core_trafficSignals = nil

local ts = require('beamngEarth/tramsignals')

-- the phases of a signal on its own cycle: 60 s, green 12, offset 10
local function st(now) local s, at = ts.freeState(60, 10, 12, now); return s, at end
local s, at = st(50)           -- (50 + 10) % 60 = 0: the green starts
T.check(s == 'go' and at == 62, 'go for the green: ' .. tostring(s) .. ' ' .. tostring(at))
s, at = st(61)
T.check(s == 'go' and at == 62, 'still go at 61')
s, at = st(62)
T.check(s == 'warn' and at == 65, 'warn for ' .. ts.WARN_S .. ' s: ' .. tostring(s) .. ' ' .. tostring(at))
s, at = st(65)
T.check(s == 'stop' and at == 110, 'stop for the rest of the cycle')
s = st(109)
T.check(s == 'stop', 'stop till the cycle ends')
s = st(110)
T.check(s == 'go', 'a new cycle: go')
local goSecs, warnSecs, stopSecs = 0, 0, 0
for t = 0, 59 do
  local x = ts.freeState(60, 10, 12, t)
  if x == 'go' then goSecs = goSecs + 1 elseif x == 'warn' then warnSecs = warnSecs + 1 else stopSecs = stopSecs + 1 end
end
T.check(goSecs == 12 and warnSecs == 3 and stopSecs == 45, 'one cycle: 12 go, 3 warn, 45 stop')
-- two clients with the same wall clock agree at every second
for t = 0, 200 do
  T.check(ts.freeState(75, 33, 15, t) == ts.freeState(75, 33, 15, t), 'deterministic')
end

-- the lamps
local a, b, c = ts.lamps('stop')
T.check(a == ts.ON and b == ts.OFF and c == ts.OFF, 'stop: the horizontal bar')
a, b, c = ts.lamps('go')
T.check(a == ts.OFF and b == ts.ON and c == ts.OFF, 'go: the vertical bar')
a, b, c = ts.lamps('warn')
T.check(a == ts.ON and b == ts.ON and c == ts.OFF, 'warn: both bars')

a, b, c = ts.lamps('turn')
T.check(a == ts.OFF and b == ts.OFF and c == ts.ON, 'turn: the diagonal bar')

-- a signal with a branch: the first half of the green straight on, the second half the turn
local seen = {}
for t = 0, 59 do seen[t] = ts.freeState(60, 10, 12, t, 1) end
local goN, turnN = 0, 0
for t = 0, 59 do
  if seen[t] == 'go' then goN = goN + 1 elseif seen[t] == 'turn' then turnN = turnN + 1 end
end
T.check(goN == 6 and turnN == 6, 'a branch: 6 s go, 6 s turn: ' .. goN .. ' ' .. turnN)
local _, change = ts.freeState(60, 10, 12, 50, 1)
T.check(change == 56, 'the go changes to the turn at half the green')
T.check(ts.shapeOf(0, 0) == '/levels/beamng_earth/art/shapes/beamngEarth/bne_tramsig_bar.dae'
  and ts.shapeOf(1, 1):match('bne_tramsig_bos_l%.dae$') and ts.shapeOf(0, -1):match('bne_tramsig_bar_r%.dae$'), 'the shape by style and branch')

-- bound to a car-signal sequence: the trams' window
local win = { phase = 3, mode = 'win' }
local seq = { active = true, currPhase = 1, stateTime = 100 }
T.check(ts.boundState(seq, win, 90, 0, 12) == 'stop', 'the cars have their phase: stop')
seq.currPhase = 3
T.check(ts.boundState(seq, win, 85, 0, 12) == 'go', 'the trams window, plenty left: go')
T.check(ts.boundState(seq, win, 98, 0, 12) == 'warn', 'the last seconds: warn')
-- (the window is green + 3 s = 15 s: it ends at 100, the go is 85 .. 97, the turn of a branch its second half)
T.check(ts.boundState(seq, win, 86, 1, 12) == 'go' and ts.boundState(seq, win, 93, 1, 12) == 'turn', 'a branch: go then turn')
seq.active = false
T.check(ts.boundState(seq, win, 90) == nil and ts.boundState(nil, win, 0) == nil, 'no running sequence: nil')

-- bound to a car phase: parallel
local par = { phase = 2, mode = 'par', ctrl = 'bne_j_1' }
local ctrl = { states = { { state = 'greenTrafficLight', duration = 20 }, { state = 'yellowTrafficLight', duration = 4 },
                          { state = 'redTrafficLight', duration = 1 } } }
local pseq = { active = true, currPhase = 2, stateTime = 130, controllerStates = { bne_j_1 = { controller = ctrl, stateIdx = 1 } } }
T.check(ts.boundState(pseq, par, 115, 0, 12) == 'go', 'parallel: the car phase is green: go')
T.check(ts.boundState(pseq, par, 115, 1, 12) == 'go' and ts.boundState(pseq, par, 125, 1, 12) == 'turn', 'parallel, a branch: go, then turn in the green half')
pseq.controllerStates.bne_j_1.stateIdx = 2
T.check(ts.boundState(pseq, par, 127, 0, 12) == 'warn', 'parallel: the car phase is yellow: warn')
pseq.controllerStates.bne_j_1.stateIdx = 3
T.check(ts.boundState(pseq, par, 129.5, 0, 12) == 'stop', 'parallel: the red second: stop')
pseq.currPhase = 1
T.check(ts.boundState(pseq, par, 115, 0, 12) == 'stop', 'parallel: the other car phase: stop')
pseq.controllerStates = nil
pseq.currPhase = 2
T.check(ts.boundState(pseq, par, 115, 0, 12) == nil, 'parallel with no controller state: nil, the wall clock')

-- the phase a tram runs along, from the junction's approaches
local function heading(dx, dy) return { th = math.atan2(dx, dy) } end        -- (the engine yaw th: +y -> (sin th, cos th))
local apps = { { 'a', 0, 0, 0, 1, 0, 0 }, { 'b', 0, 0, 0, -1, 0, 0 }, { 'c', 0, 0, 0, 0, 1, 1 }, { 'd', 0, 0, 0, 0, -1, 1 } }
T.check(ts.parallelPhase(heading(1, 0), apps) == 0 and ts.parallelPhase(heading(-1, 0), apps) == 0, 'a tram along x: the x phase')
T.check(ts.parallelPhase(heading(0, 1), apps) == 1, 'a tram along y: the y phase')
T.check(ts.parallelPhase(heading(1, 1), apps) == nil, 'a tram at 45 degrees: neither: the window')
T.check(ts.parallelPhase(heading(1, 0), { { 'a', 0, 0, 0, 0, 1, 0 }, { 'b', 0, 0, 0, 0, -1, 0 } }) == nil,
  'a tram across the only phase: the window')
T.check(ts.parallelPhase(heading(1, 0), { { 'a', 0, 0, 0, 1, 0, 0 } }) == 0, 'a tram along the only phase: that phase')
T.check(ts.parallelPhase(heading(1, 0.2), apps) == 0, 'a slightly turned tram still runs along x')

-- rows -> objects
local rows = { { 10, 20, 5, 90, 1, 60, 10, 12 }, { 30, 20, 5, 270, 0, 60, 10, 12 } }
local collided = 0
ts.onCollide = function() collided = collided + 1 end
ts.onRows('tile1', rows)
T.check(ts.status().signals == 2 and ts.made == 2 and collided == 1, 'a signal per row')
local objs = {}
for _, o in pairs(S.objects) do if o.class == 'TSStatic' then objs[#objs + 1] = o end end
T.check(#objs == 2, 'two TSStatics')
table.sort(objs, function(p, q) return p.pos.x < q.pos.x end)
T.check(objs[1].fields.shapeName == ts.shapeOf(1, 0) and objs[2].fields.shapeName == ts.shapeOf(0, 0), 'the shape by the style')
T.near(objs[1].xf.yaw, -math.rad(90), 1e-9, 'the engine yaw is the row yaw negated')

-- the wall clock decides; the lamps are the instance colours
ts.step(50, nil)              -- go: the vertical bar
T.check(objs[1].fields.instanceColor == ts.OFF and objs[1].fields.instanceColor1 == ts.ON, 'go lamps set on the object')
ts.step(65, nil)
T.check(objs[1].fields.instanceColor == ts.ON and objs[1].fields.instanceColor1 == ts.OFF, 'stop lamps')
local before = objs[1].fields.instanceColor
objs[1].fields.instanceColor = 'untouched'
ts.step(66, nil)              -- not the signal's second yet
T.check(objs[1].fields.instanceColor == 'untouched', 'no setField between two changes')
objs[1].fields.instanceColor = before

-- bound: the sequence wins while it runs, the wall clock after it
local near = ts.nearList(10, 20, 40)
T.check(#near == 2 and near[1].green == 12, 'both signals within 40 m of the junction')
T.check(#ts.nearList(500, 500, 40) == 0, 'none far away')
for _, w in ipairs(near) do ts.bind(w, { seq = 'bne_j', phase = 3, mode = 'win' }, 1) end
T.check(ts.status().bound == 2 and ts.status().parallel == 0, 'bound')
local fake = { active = true, currPhase = 3, stateTime = 100 }
core_trafficSignals = { getSequenceByName = function(n) return n == 'bne_j' and fake or nil end, getTimer = function() return 90 end }
ts.step(65, core_trafficSignals)     -- the wall clock says stop: the sequence says go
T.check(objs[1].fields.instanceColor1 == ts.ON, 'bound signal follows the sequence')
fake.currPhase = 1
ts.step(65, core_trafficSignals)
T.check(objs[1].fields.instanceColor == ts.ON and objs[1].fields.instanceColor1 == ts.OFF, 'the cars phase: stop')
ts.unbindAll()
T.check(ts.status().bound == 0, 'unbound')
ts.step(50, core_trafficSignals)
T.check(objs[1].fields.instanceColor1 == ts.ON, 'back on the wall clock')

-- a closer junction keeps the signal
local one = near[1]
ts.bind(one, { seq = 'bne_far', phase = 3, mode = 'win' }, 100)
ts.bind(one, { seq = 'bne_near', phase = 3, mode = 'win' }, 4)
ts.bind(one, { seq = 'bne_farther', phase = 3, mode = 'win' }, 50)
T.check(one.bind.seq == 'bne_near', 'the nearer junction keeps a signal')
ts.unbindAll()

-- a rebase: moved by p' = p + (100, 0, 0), turned a quarter
local xf = { apply = function(_, x, y, z) return -y + 100, x, z end }      -- a rotation by 90 degrees counter-clockwise and a shift
ts.transform(xf)
local o1 = objs[1]
T.near(o1.pos.x, 100 - 20, 1e-9, 'moved x')
T.near(o1.pos.y, 10, 1e-9, 'moved y')
-- the model's +x pointed (cos th, -sin th) at th = -90 deg: (0, 1) world; turned by 90 degrees ccw: (-1, 0): th' = atan2(0, -1) = pi
T.near(math.abs(o1.xf.yaw), math.pi, 1e-9, 'turned with the world')

-- placeRows with the same count moves, else makes again
ts.placeRows('tile1', { { 1, 2, 3, 0, 1, 60, 10, 12 }, { 4, 5, 6, 0, 0, 60, 10, 12 } })
T.check(ts.made == 2 and objs[1].pos.x == 1, 'same count: moved, not made again')
ts.placeRows('tile1', { { 1, 2, 3, 0, 1, 60, 10, 12 } })
T.check(ts.made == 3 and ts.status().signals == 1, 'another count: made again')

-- gone with the tile
ts.dropKey('tile1')
local left = 0
for _, o in pairs(S.objects) do if o.class == 'TSStatic' then left = left + 1 end end
T.check(left == 0 and ts.status().signals == 0, 'the tile goes, the signals with it')

-- bad rows are skipped
ts.onRows('tile2', { { 1, 2 }, 'x', { 1, 2, 3, 4, 1, 60, 10, 12 } })
T.check(ts.status().signals == 1, 'short rows skipped')
ts.reset()
T.check(ts.status().signals == 0, 'reset')

-- the navgraph at a junction with a tram signal near: the trams' window or the phase the tram runs along
package.loaded['beamngEarth/tramsignals'] = nil
ts = require('beamngEarth/tramsignals')
local N = require('beamngEarth/navgraph')
N.MIN_GAP_S = 0
table.clear = table.clear or function(t) for k in pairs(t) do t[k] = nil end end
local graph, current = nil, { nodes = {} }
package.loaded['beamngEarth/trees'] = { refresh = function() end }
package.loaded['beamngEarth/gps'] = {}
readFile = function() return '{}' end
writeFile = function() end
jsonDecode = function() return graph end
jsonEncode = function() return '{}' end
serialize = function() return '{}' end
lpack = { encodeBin = function() return '' end }
be.queueAllObjectLua = function() end
be.sendToMailbox = function() end
path = { getPathLevelInfo = function() end }
map = { reset = function(nodes) current = { nodes = nodes } end, getMap = function() return current end }
local newSeq, newCtrl = {}, {}
core_trafficSignals = {
  resetSignals = function() end,
  setupSignals = function() end,
  getMapNodeSignals = function() return {} end,
  newController = function(opts)
    local c = { id = opts.name, name = opts.name, states = opts.states, applyDefinition = function() end }
    newCtrl[#newCtrl + 1] = c
    return c
  end,
  newSequence = function(opts) local q = { id = opts.name, name = opts.name, phases = opts.phases }; newSeq[#newSeq + 1] = q; return q end,
  newSignal = function(opts) return opts end,
  getSequenceByName = function(n) for _, q in ipairs(newSeq) do if q.name == n then return q end end end,
  getTimer = function() return 0 end,
}
local serial = 0
local function apply()
  serial = serial + 1
  -- the junction j at the origin, one approach from the south (heading +y, phase 0) and one from the west (heading +x, phase 1)
  graph = { nodes = { j = { 0, 0, 0, 4 }, s = { 0, -80, 0, 4 }, w = { -80, 0, 0, 4 } },
    edges = { { 's', 'j', 0, 1, 1, 13, 1, 4 }, { 'w', 'j', 0, 1, 1, 13, 1, 4 } },
    signals = { { j = 'j', kind = 'signal', app = { { 's', 2, -10, 0, 0, 1, 0, 8, -10, 0, 0 },
                                                    { 'w', -10, 2, 0, 1, 0, 1, -10, 8, 0, 0 } } } },
    rules = { rightHandDrive = false } }
  T.check(N.onMessage({ anchorId = 1, serial = serial, path = '/graph' }, 1), 'graph accepted')
  local b = { -500, -500, 500, 500 }
  N.update(1, b)
  N.update(1, b)
end
local function lastSeq() return newSeq[#newSeq] end
local function ctrlNamed(n) for _, c in ipairs(newCtrl) do if c.name == n then return c end end end
-- a model heading (dx, dy) as a row's yaw (degrees ccw of the model's +x = the right of the heading: the engine's th = atan2(dx, dy))
local function yawOf(dx, dy) return -math.deg(math.atan2(dx, dy)) end

apply()
T.check(#lastSeq().phases == 2, 'no tram signal near: two phases, as before')

-- a tram along y (the south approach's axis, phase 0): no third phase, it goes with phase 0's green
newSeq, newCtrl = {}, {}
ts.onRows('t', { { 15, -20, 0, yawOf(0, 1), 1, 60, 10, 12, 0 } })      -- 25 m from the junction node
apply()
T.check(#lastSeq().phases == 2 and not ctrlNamed('bne_tram_j'), 'a tram along a car phase: no window')
do
  local b = ts.nearList(15, -20, 5)[1].bind
  T.check(b and b.mode == 'par' and b.seq == 'bne_j' and b.phase == 1 and b.ctrl == 'bne_j_0', 'it follows phase 0 (the sequence phase 1, its controller bne_j_0)')
end
ts.dropKey('t')

-- a tram along x: the west approach's phase 1
newSeq, newCtrl = {}, {}
ts.onRows('t', { { 15, -20, 0, yawOf(1, 0), 0, 60, 10, 12, 1 } })
apply()
do
  local b = ts.nearList(15, -20, 5)[1].bind
  T.check(b and b.mode == 'par' and b.phase == 2 and b.ctrl == 'bne_j_1', 'a tram along x follows phase 1 (the sequence phase 2)')
end
ts.dropKey('t')

-- a tram at 45 degrees runs along neither: the all-red window as a third phase
newSeq, newCtrl = {}, {}
ts.onRows('t', { { 15, -20, 0, yawOf(1, 1), 1, 60, 10, 12, 0 } })
apply()
local q = lastSeq()
T.check(q and #q.phases == 3, 'a tram across the roads: a third phase')
local tc = ctrlNamed('bne_tram_j')
T.check(tc and tc.states[1].state == 'redTrafficLight' and tc.states[1].duration == 12 + ts.WARN_S,
  'the window: green + warning seconds, red for the cars')
T.check(q.phases[3].controllerIds[1] == 'bne_tram_j', 'the third phase is the trams controller')
do
  local b = ts.nearList(15, -20, 5)[1].bind
  T.check(b and b.mode == 'win' and b.phase == 3, 'the tram signal follows the window')
end
-- a signal far from the junction is not bound
ts.onRows('far', { { 900, 900, 0, 0, 1, 60, 10, 12, 0 } })
newSeq, newCtrl = {}, {}
apply()
T.check(ts.status().bound == 1 and ts.status().parallel == 0, 'the far signal is not bound')

T.done('test_tramsignals')
