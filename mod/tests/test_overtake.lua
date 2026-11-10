-- luajit mod/tests/test_overtake.lua  (overtaking on multi-lane highways: the pure decisions)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local O = require('beamngEarth/overtake')

T.check(O.lanesMyWay('++', true) == 2 and O.lanesMyWay('-++', true) == 2 and O.lanesMyWay('-++', false) == 1,
  'lanes my way: the + lanes along, the - lanes against')
T.check(O.lanesMyWay(nil, true) == 0, 'no lanes string: none')

-- a 2-lane carriageway 7.2 m wide (radius 3.6): the right lane's centre at +1.8, the left's at -1.8 (as on I-94)
local k, w = O.laneOf(1.8, 3.6, 2)
T.check(k == 1 and math.abs(w - 3.6) < 1e-9, 'right lane: index 1, 3.6 m wide')
T.check(O.laneOf(-1.5, 3.6, 2) == 0, 'left lane: index 0')
T.check(O.laneOf(9, 3.6, 2) == 1 and O.laneOf(-9, 3.6, 2) == 0, 'past the edges: the outer lanes')
T.check(O.laneOf(0, 0, 2) == nil, 'no width: nil')

T.check(O.passShift(1, 2, 3.6, false) == -3.6, 'right-hand traffic: from the right lane, one lane left')
T.check(O.passShift(0, 2, 3.6, false) == nil, '... from the left lane: nowhere to pass')
T.check(O.passShift(0, 2, 3.6, true) == 3.6, 'left-hand traffic: from the left lane, one lane right')
T.check(O.passShift(0, 1, 3.6, false) == nil, 'one lane: no passing')

-- held: behind a truck at 42 mph (18.8 m/s) on an 80 mph road (35.8 m/s), 30 m ahead, the car at 19 m/s
T.check(O.isHeld(19, 35.8, 30, 18.8), 'held behind a slow truck')
T.check(not O.isHeld(19, 35.8, 90, 18.8), 'not held: the truck 90 m ahead')
T.check(not O.isHeld(33, 35.8, 30, 32), 'not held: the one ahead near the limit')
T.check(not O.isHeld(10, 35.8, 30, 20), 'not held: the one ahead pulling away')

-- the left lane clear? others in the car's frame (ahead m, side m right of it, speed)
local shift = -3.6
T.check(O.laneClear({ { ahead = 30, side = 0, v = 18 } }, shift, 25, 70, 19), 'the truck in our lane does not block the left one')
T.check(not O.laneClear({ { ahead = 40, side = -3.5, v = 30 } }, shift, 25, 70, 19), 'a car in the left lane 40 m ahead: not clear')
T.check(not O.laneClear({ { ahead = -20, side = -3.6, v = 20 } }, shift, 25, 70, 19), 'one beside us, 20 m back: not clear')
T.check(O.laneClear({ { ahead = -60, side = -3.6, v = 20 } }, shift, 25, 70, 19), 'one 60 m back at our speed: clear')
T.check(not O.laneClear({ { ahead = -60, side = -3.6, v = 36 } }, shift, 25, 70, 19),
  'one 60 m back closing at 17 m/s: not clear (it needs the room)')

T.done('test_overtake')
