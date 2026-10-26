-- luajit mod/tests/test_garage.lua  (repair shops and property: prices, actions where you stand, renting, storage)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local saved = {}
jsonWriteFile = function(p, t) saved[p] = t end
jsonReadFile = function() return nil end
guihooks = { trigger = function() end }
local W = require('beamngEarth/wallet')
local G = require('beamngEarth/garage')
local D = require('beamngEarth/dynweather')

T.check(G.repairCost(0) == 60 and G.repairCost(1000) == 660 and G.repairCost(5000, true) == 0, 'repair prices')
T.check(G.held({ kind = 'own' }, 99) and G.held({ kind = 'rent', untilDay = 6 }, 6) and not G.held({ kind = 'rent', untilDay = 6 }, 7), 'held')
T.check(G.plateText(0) == 'AAA-0000' and #G.plateText(123456789) == 8, 'plates: ' .. G.plateText(123456789))

local anchor = { geodeticToEnu = function(_, lat, lon) return (lon + 80) * 85000, (lat - 40) * 111000, 0 end }
local replaced, plates = {}, {}
local veh = { JBeam = 'pickup', partConfig = 'my.pc', color = '0.6 0.05 0.05 1.2', getID = function() return 3 end,
              getDynDataFieldbyName = function() return 'OLD-1' end }
be = { getPlayerVehicle = function() return veh end }
map = { objects = { [3] = { damage = 1000 } } }
core_vehicles = { replaceVehicle = function(m, o) replaced[#replaced + 1] = { m, o } end,
                  setPlateText = function(t) plates[#plates + 1] = t end }
places_added = {}
package.loaded['beamngEarth/places'] = { add = function(n) places_added[#places_added + 1] = n end }
W.add(-W.balance(), 'reset')
G._reset()
local shop = { id = 'r1', name = "Joe's", lat = 40.0, lon = -80.0 }
G._setList('repair', { shop })
local far = { x = 500, y = 500 }
T.check(not G.repair(anchor, far), 'not at a shop: no repair')
local at = { x = 10, y = 10 }
T.check(G.repair(anchor, at), 'repair at the shop')
T.check(replaced[1][1] == 'pickup' and replaced[1][2].config == 'my.pc' and replaced[1][2].paint.baseColor[1] == 0.6, 'same car, parts and paint')
T.check(plates[1] == 'OLD-1', 'its plate kept')
T.near(W.balance(), -660, 1e-9, 'paid for the damage')
G.respray(anchor, at, 4)
T.check(replaced[2][2].paint.baseColor[3] == 0.5, 'new colour')
T.near(W.balance(), -660 - 450, 1e-9, 'respray paid')
G.plates(anchor, at)
T.check(plates[#plates] ~= 'OLD-1' and plates[#plates]:find('%-'), 'new plates')

-- property: rent for a week, it runs out; buy; storage there; free repairs at home
W.add(100000, 'test')
G._setList('property', { { id = 'p1', name = 'Public Storage', lat = 40.01, lon = -80.0, buy = 30000, rent = 500 } })
local b0 = W.balance()
T.check(G.acquire(1, 'rent'), 'rent')
T.near(b0 - W.balance(), 500, 1e-9, 'a week\'s rent')
T.check(places_added[1] == 'My garage: Public Storage', 'a spawn point')
local home = { x = 0, y = 1110 }
T.check(G.at(anchor, home).kind == 'home', 'at home')
T.check(G.repair(anchor, home) and math.abs(b0 - 500 - W.balance()) < 1e-9, 'repairs free at home')
for _ = 1, 8 do D._state().day = (D._state().day or 0) + 1 end
T.check(G.at(anchor, home) == nil, 'the week is over: not yours')
T.check(G.acquire(1, 'buy'), 'buy')
T.check(G.at(anchor, home).kind == 'home', 'bought: yours for good')
T.check(G.store(anchor, home), 'store a copy')
veh.JBeam = 'etk800'
T.check(G.store(anchor, home, 1), 'swap')
local cars = G._state().owned.p1.cars
T.check(cars[1].model == 'etk800' and replaced[#replaced][1] == 'pickup', 'the pickup out, the etk800 in storage')
T.check(not G.store(anchor, at), 'storage only at your property')
T.done('test_garage')
