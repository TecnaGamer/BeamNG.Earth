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
T.check(G.plateText(0) == 'AAA0000' and #G.plateText(123456789) == 7, 'plates: ' .. G.plateText(123456789))

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
T.check(plates[#plates] ~= 'OLD-1' and #plates[#plates] == 7, 'new plates')

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
-- selling: 70 % of the price paid back, the place gone; a lease just ends
local b1 = W.balance()
T.check(G.sell('p1'), 'sell')
T.near(W.balance() - b1, 21000, 1e-9, 'sold for 70 % of $30,000')
T.check(G._state().owned.p1 == nil and G.at(anchor, home) == nil, 'not yours any more')
G.acquire(1, 'rent')
b1 = W.balance()
T.check(G._state().owned.p1.buy == 30000, 'a rental keeps its price')
b1 = W.balance()
T.check(G.buyout('p1') and G._state().owned.p1.kind == 'own', 'a rental bought out')
T.near(b1 - W.balance(), 30000, 1e-9, 'at its price')
T.check(G.sell('p1'), 'then sold')
G.acquire(1, 'rent')
b1 = W.balance()
T.check(G.sell('p1') and W.balance() == b1, 'a lease ends with no refund')
T.check(not G.sell('nope'), 'nothing to sell')
-- the parts shop's bill: new parts at their price, removed ones bought back at half
local price = { turbo = 2500, stock_intake = 300, rims_a = 800, rims_b = 1200 }
local c, r = G.partsBill({ engine = 'v8', intake = 'stock_intake', wheels = 'rims_a' },
                         { engine = 'v8', intake = 'turbo', wheels = 'rims_b' }, function(n) return price[n] or 0 end)
T.check(c == 3700 and r == 550, 'bill: +' .. c .. ' -' .. r)
T.check(select(1, G.partsBill({ a = 'x' }, { a = 'x' }, function() return 99 end)) == 0, 'no change: nothing')

-- the garage spot: stopping in the ring opens the phone on the place's menu
local opened = {}
guihooks = { trigger = function(n, d) if n == 'BneEarthPhone' then opened[#opened + 1] = d end end }
debugDrawer = nil
G._reset()
G._setList('repair', { { id = 'r9', name = 'Shop', lat = 40.0, lon = -80.0, mlat = 40.0001, mlon = -80.0 } })
G.update(0.1, anchor, { x = 0, y = 11.1, vx = 0, vy = 0, z = 0 })
T.check(opened[1] and opened[1].app == 'garage' and opened[1].up, 'in the ring: the phone on the Garage')
G.update(0.1, anchor, { x = 0, y = 11.1, vx = 0, vy = 0, z = 0 })
T.check(#opened == 1, 'once, not every frame')
G.update(0.1, anchor, { x = 0, y = 60, vx = 0, vy = 0, z = 0 })
G.update(0.1, anchor, { x = 0, y = 11.1, vx = 0, vy = 0, z = 0 })
T.check(#opened == 2, 'again after driving off and back')

-- the bench against the wall (server shops.wall_spot): turned, the laptop body and its screen on top at 0.67 m
local L = G.propLayout(10, 20, 0)
T.check(#L == 3 and L[2][1]:find('s_laptop%.dae$') and L[3][1]:find('s_laptop_ON%.dae$'), 'the laptop body and its screen')
T.near(L[2][4], 0.67, 1e-9, 'on the bench top')
T.near(L[2][2], 10 + G.LAPTOP_X, 1e-9, 'along the bench')
L = G.propLayout(0, 0, math.pi / 2)
T.near(L[2][2], 0.02, 1e-6, 'turned: along the bench x'); T.near(L[2][3], G.LAPTOP_X, 1e-6, 'turned: along the bench y')
local made = {}
createObject = function()
  local o = { delete = function(self) self.deleted = true end, registerObject = function() end }
  o.setPosRot = function(self, ...) self.pr = { ... } end
  made[#made + 1] = o
  return o
end
vec3 = vec3 or function(x, y, z) return { x = x, y = y, z = z } end
quatFromDir = function(d) return { x = 0, y = 0, z = d.x, w = d.y } end
be.getSurfaceHeightBelow = function() return 0 end
G._reset()
G._setList('repair', { { id = 'r7', name = 'Shop', lat = 40.0, lon = -80.0, mlat = 40.0001, mlon = -80.0,
  dlat = 40.00015, dlon = -80.0, dyaw = math.pi / 2 } })
G.update(0.1, anchor, { x = 0, y = 0, vx = 0, vy = 0, z = 0 })
T.check(#made == 3, 'three props: ' .. #made)
T.near(made[1].pr[2], 0.00015 * 111000, 1e-6, 'the bench at the wall spot, not beside the ring')
T.near(made[1].pr[6], -1, 1e-9, 'turned to face the wall (its back, +y, toward -x at a quarter turn)')
G._setList('repair', { { id = 'r7', name = 'Shop', lat = 40.0, lon = -80.0, mlat = 40.0001, mlon = -80.0,
  dlat = 40.0002, dlon = -80.0, dyaw = 0 } })
G.update(0.1, anchor, { x = 0, y = 0, vx = 0, vy = 0, z = 0 })
T.check(made[1].deleted and #made == 6, 'a moved spot: the props put again')
T.done('test_garage')
