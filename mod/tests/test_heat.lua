-- luajit mod/tests/test_heat.lua  (police heat: plates and looks remembered, chase build-up, footage, busted)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local saved, msgs = nil, {}
jsonWriteFile = function(_, t) saved = t end
jsonReadFile = function() return nil end
guihooks = { trigger = function(_, m) msgs[#msgs + 1] = m.msg end }
local W = require('beamngEarth/wallet')
local H = require('beamngEarth/heat')

T.check(H.colourKey('0.81234 0.1 0.1 1.2') == '0.8 0.1 0.1', 'colour key: rgb to tenths')
T.check(H.build(0, 0) == 1, 'a chase starts at one star')
T.check(H.build(1, 179) == 1 and H.build(1, 180) == 2 and H.build(1, 540) == 4, 'a star every 3 minutes')
T.check(H.build(2, 0) == 2, 'a known car starts at its heat')
T.check(H.build(1, 9999) == 5, 'at most five')
T.check(H.footage(0, 1) == 150 and H.footage(120, 2) == 540, 'footage: longer and higher pays more')
local b = H.busted(1)
T.check(b.fine == 60 and b.impound == 0, 'pulled over at once, one offense: a $60 ticket')
T.check(H.busted(1, 0).fine == 30 and H.busted(1, 3).fine == 120 and H.busted(1, 9).fine == 150, 'tickets $30-150 by offenses')
T.check(H.busted(0).fine == 60, 'no heat: still a ticket')
b = H.busted(4)
T.check(b.fine == 1500 and b.impound == H.IMPOUND_FEE, 'four stars: big fine and impound')
T.check(H.busted(5).total == 3000 + 750 and H.busted(3).impound == 0, 'five stars; three: no impound')
local id = { plate = 'ABC123', look = 'pickup|0.8 0.1 0.1' }
T.near(H.heatOf(id, { ABC123 = 3 }, {}), 3, 1e-9, 'plate known')
T.near(H.heatOf({ plate = 'NEW1', look = id.look }, { ABC123 = 4 }, { [id.look] = 4 }), 2, 1e-9,
  'new plate, same look: half')
T.near(H.heatOf({ plate = 'NEW1', look = 'pickup|0.1 0.1 0.8' }, { ABC123 = 4 }, { [id.look] = 4 }), 0, 1e-9,
  'new plate and colour: a new car')

-- a chase, an escape, the plate remembered, then busted later
local plate, colour = 'ABC123', '0.8 0.1 0.1 1.2'
local veh = { JBeam = 'pickup', getDynDataFieldbyName = function() return plate end,
              getField = function() return colour end }
be = { getPlayerVehicleID = function() return 5 end, getPlayerVehicle = function() return veh end }
local started = {}
gameplay_police = { getPursuitData = function() return { mode = 2 } end,
                    getNearestPoliceVehicle = function() return 9, 30 end,
                    setPursuitMode = function(m) started[#started + 1] = m end }
W.add(-W.balance(), 'reset')
H._reset()
H.onPursuitAction(5, 'start', { mode = 2 })
for _ = 1, 400 do H.update(1) end                -- 6 min 40 s: three stars
T.check(H._chase().heat == 3, 'three stars after 6 minutes: ' .. tostring(H._chase().heat))
H.onPursuitAction(5, 'evade', {})
T.near(W.balance(), H.footage(400, 2), 1e-9, 'footage sold')
T.check(H._state().plates.ABC123 == 3, 'the plate remembered at three stars')
H.update(1)
T.check(#started == 1, 'seen by a cop: a chase starts at once')
colour = '0.1 0.1 0.8 1.2'; plate = 'ZZZ999'
started = {}
H.update(1)
T.check(#started == 0, 'new paint and plates: not recognised')
plate = 'ABC123'
H.onPursuitAction(5, 'start', { mode = 1 })
T.check(H._chase().heat == 3, 'a known plate: the chase starts at its heat')
local before = W.balance()
H.onPursuitAction(5, 'arrest', {})
T.near(before - W.balance(), 750, 1e-6, 'pulled over at three stars: $750')
T.check(H._state().plates.ABC123 == 0, 'heat cleared after paying')
H._reset()
H.onPursuitAction(5, 'start', { mode = 1 })
before = W.balance()
H.onPursuitAction(5, 'arrest', {})
T.near(before - W.balance(), 60, 1e-6, 'a new car pulling over at once: a $60 ticket')
H.onPursuitAction(7, 'start', {})
T.check(H._chase() == nil, 'another car\'s chase: ignored')
T.done('test_heat')
