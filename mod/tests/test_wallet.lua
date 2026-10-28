-- luajit mod/tests/test_wallet.lua  (the player's money: format, saving, fuel cost, tolls, the refuel wrapper)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local saved, msgs = nil, {}
jsonWriteFile = function(path, t) saved = { path = path, balance = t.balance } end
jsonReadFile = function() return nil end
guihooks = { trigger = function(name, m) msgs[#msgs + 1] = m end }
local W = require('beamngEarth/wallet')

T.check(W.format(1234.5) == '$1,234.50' and W.format(-3.25) == '-$3.25' and W.format(0) == '$0.00'
  and W.format(1000000) == '$1,000,000.00', 'format: ' .. W.format(1234.5))
T.check(W.balance() == 0, 'starts at $0')
W.add(-3.25, 'Toll')
T.check(W.balance() == -3.25 and saved.balance == -3.25 and saved.path == W.FILE, 'a cost saved at once')
T.check(msgs[#msgs].msg == '-$3.25  Toll', 'shown: ' .. tostring(msgs[#msgs].msg))
W.add(10, 'Job')
T.check(W.balance() == 6.75 and W.history()[1].reason == 'Job', 'history newest first')

-- fuel: 20 litres missing of gasoline (5.28 gal at 3.40), a diesel tank of another fuel type not filled
local tanks = { { energyType = 'gasoline', maxEnergy = 60 * W.LITRE_J.gasoline, currentEnergy = 40 * W.LITRE_J.gasoline },
                { energyType = 'diesel', maxEnergy = 10 * W.LITRE_J.diesel, currentEnergy = 0 } }
local cost, amt, unit = W.fuelCost(tanks, { gasoline = true }, true)
T.near(amt, 20 / W.GAL_L, 1e-9, 'gallons')
T.near(cost, 20 / W.GAL_L * 3.40, 1e-9, 'cost per gallon')
T.check(unit == 'gal', 'mph: gallons')
local c2, a2, u2 = W.fuelCost(tanks, { any = true }, false)
T.near(a2, 30, 1e-9, 'any fuel type: both tanks, litres')
T.near(c2, 30 * 1.80, 1e-9, 'per litre')
T.check(u2 == 'l', 'km/h: litres')
T.check(W.fuelCost({ { energyType = 'electricEnergy', maxEnergy = 1, currentEnergy = 0 } }, nil, true) == 0, 'no electric')

-- tolls: a gantry at (100, 0) for eastbound traffic, 8 m half span
local g = { 100, 0, 1, 0, 8 }
T.check(W.crossed(g, 95, 2, 105, 2), 'eastbound under it')
T.check(not W.crossed(g, 105, 2, 95, 2), 'westbound: not this one')
T.check(not W.crossed(g, 95, 20, 105, 20), 'beside it: no')
T.check(W.crossed(g, 95, 9, 105, 9), 'within the side margin')
W._setState({ balance = 0, history = {} })
W.onTolls(7, 'k1', { g })
W.update(7, 90, 0)
W.update(7, 99, 0)
W.update(7, 101, 0)
T.check(W.balance() == -W.TOLL, 'charged once passing under: ' .. W.balance())
W.update(7, 120, 0)
T.check(W.balance() == -W.TOLL, 'not again further on')
W.update(8, 90, 0) W.update(8, 110, 0)
T.check(W.balance() == -W.TOLL, 'another anchor: its own tolls only')
W.update(7, 90, 0) W.update(7, 500, 0)
T.check(W.balance() == -W.TOLL, 'a teleport across: no toll')

-- the refuel wrapper: our stations charge then fill, others only fill
W._setState({ balance = 0, history = {} })
local filled = {}
local gs = { refuelCar = function(st) filled[#filled + 1] = st.id end }
core_vehicleBridge = { requestValue = function(veh, cb, what) cb({ tanks }) end }
T.check(W.wrapRefuel(gs, true) and not W.wrapRefuel(gs, true), 'wrapped once')
gs.refuelCar({ id = 'bne_fuel_abc' }, { gasoline = true }, {})
T.near(W.balance(), -(20 / W.GAL_L * 3.40), 0.006, 'our station: charged')
T.check(filled[1] == 'bne_fuel_abc', 'and filled')
gs.refuelCar({ id = 'wcusa_station' }, { gasoline = true }, {})
T.check(filled[2] == 'wcusa_station' and #W.history() == 1, 'a stock station: filled, not charged')
T.done('test_wallet')
