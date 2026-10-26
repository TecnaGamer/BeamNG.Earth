-- luajit mod/tests/test_jobs.lua  (garage-to-garage deliveries: the pay, arrival, the stages, the car swap)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local msgs, replaced, added = {}, {}, {}
jsonWriteFile = function() end
jsonReadFile = function() return nil end
guihooks = { trigger = function(_, m) msgs[#msgs + 1] = m.msg end }
os.time = os.time or function() return 0 end
local W = require('beamngEarth/wallet')
local J = require('beamngEarth/jobs')
local G = require('beamngEarth/gps')

local job = { id = 'a', car = 'etk800', car_name = 'ETK 800', pay = 130, dist = 16093.44, dur = 900, limit = 1680,
              pickup = { lat = 40.0, lon = -80.0, name = 'Joe' }, dropoff = { lat = 40.1, lon = -80.0, name = 'Smith' } }

-- pay
local p = J.payout(job, 600, 0, 0)
T.near(p.bonus, 26, 1e-9, 'early: the whole bonus (20 %)')
T.near(p.total, 156, 1e-9, 'early total')
p = J.payout(job, 1680, 0, 0)
T.near(p.bonus, 0, 1e-9, 'at the limit: no bonus')
T.near(p.total, 130, 1e-9, 'at the limit: the pay')
p = J.payout(job, (900 * 1.2 + 1680) / 2, 0, 0)
T.near(p.bonus, 13, 1e-9, 'halfway between early and the limit: half the bonus')
p = J.payout(job, 1680 + 600, 0, 0)
T.near(p.late, 130 * 0.2, 1e-9, '10 min late: 20 % less')
p = J.payout(job, 1680 + 36000, 0, 0)
T.near(p.late, 65, 1e-9, 'very late: at most half')
p = J.payout(job, 600, J.DAMAGE_FULL / 10, 12.5)
T.near(p.damage, 15.6, 1e-9, 'a tenth of full damage: a tenth of the pay and bonus')
T.near(p.expenses, 12.5, 1e-9, 'tolls and fuel paid back')
T.near(p.total, 130 + 26 - 15.6 + 12.5, 1e-9, 'total')
p = J.payout(job, 600, J.DAMAGE_FULL * 3, 4)
T.near(p.total, 4, 1e-9, 'wrecked: no pay for the work, the expenses still paid back')
T.check(J.clock(65) == '1:05' and J.clock(1680) == '28:00', 'clock')
T.check(J.arrived(0, 0, 1, 20, 20) and not J.arrived(0, 0, 5, 20, 20) and not J.arrived(0, 0, 1, 40, 0), 'arrived')

-- the stages, with a flat anchor (1 degree of latitude = 111 km north) and stub vehicles
local anchor = { geodeticToEnu = function(_, lat, lon) return (lon + 80) * 85000, (lat - 40) * 111000, 0 end }
local veh = { JBeam = 'pickup', partConfig = 'vehicles/pickup/my.pc', getID = function() return 7 end }
be = { getPlayerVehicle = function() return veh end }
map = { objects = { [7] = { damage = 0 } } }
core_vehicles = { replaceVehicle = function(model, opt) replaced[#replaced + 1] = { model, opt.config } end }
local dests = {}
G.setDestination = function(lat, lon, name) dests[#dests + 1] = name return true end
G.clear = function() dests[#dests + 1] = 'clear' end
W.add(-W.balance(), 'reset')

J._reset()
J._setOffers({ job })
T.check(J.accept(1), 'accept')
T.check(not J.accept(1), 'one job at a time')
T.check(dests[#dests] == 'Joe', 'GPS to the pickup')
J.update(0.1, anchor, { x = 0, y = 0, vx = 10, vy = 0 })
T.check(J._active().stage == 'pickup' and #replaced == 0, 'driving past: nothing yet')
J.update(0.1, anchor, { x = 5, y = 5, vx = 0.5, vy = 0 })
T.check(J._active().stage == 'deliver', 'stopped at the pickup: delivering')
T.check(replaced[1][1] == 'etk800', 'your car swapped for the customer\'s')
T.check(dests[#dests] == 'Smith', 'GPS to the drop-off')
W.add(-3.25, 'Toll')          -- a toll on the way: the company pays it back
J.update(500, anchor, { x = 0, y = 5000, vx = 20, vy = 0 })
map.objects[7].damage = 1500
J.update(100, anchor, { x = 0, y = 11100, vx = 0, vy = 0 })
T.check(J._active() == nil, 'delivered')
T.check(replaced[2][1] == 'pickup' and replaced[2][2] == 'vehicles/pickup/my.pc', 'your own car back, its config')
local st = J.uiState()
T.near(st.last.pay.expenses, 3.25, 1e-9, 'the toll paid back')
T.near(st.last.pay.damage, 15.6, 1e-9, 'the damage cut')
T.near(W.balance(), -3.25 + st.last.pay.total, 1e-9, 'paid into the wallet')
T.check(dests[#dests] == 'clear', 'GPS cleared')

-- cancelling while delivering gives your car back, no pay
J._setOffers({ job })
J.accept(1)
J.update(0.1, anchor, { x = 0, y = 0, vx = 0, vy = 0 })
local n = #replaced
local bal = W.balance()
T.check(J.cancel() and replaced[n + 1][1] == 'pickup' and W.balance() == bal and J._active() == nil, 'cancel')
T.check(J.uiState().last.cancelled, 'shown as cancelled')

-- trucking: the rented semi, the trailer behind, no delivery without it, the rent off the pay
vec3 = function(x, y, z) return { x = x, y = y, z = z } end
quatFromDir = function() return {} end
local trailer = { pos = { x = 0, y = -16, z = 0 }, deleted = false }
trailer.getID = function() return 99 end
trailer.getPosition = function() return trailer.pos end
trailer.delete = function() trailer.deleted = true end
veh.getPosition = function() return { x = 0, y = 0, z = 0 } end
veh.getDirectionVector = function() return { x = 0, y = 1, z = 0 } end
local spawned = {}
core_vehicles.spawnNewVehicle = function(model, opt) spawned[#spawned + 1] = { model, opt.pos } return trailer end
be.getObjectByID = function(_, id) return id == 99 and not trailer.deleted and trailer or nil end
local haul = { id = 't', kind = 'truck', car = 'us_semi', car_name = 'Gavril T-Series semi', trailer = 'tanker',
               cargo = 'Tanker: fuel', rent = 45, pay = 300, dist = 60000, dur = 3000, limit = 5040,
               pickup = { lat = 40.0, lon = -80.0, name = 'Acme' }, dropoff = { lat = 40.5, lon = -80.0, name = 'DC' } }
map.objects[7].damage = 0
J._setOffers({ haul })
J.accept(1)
J.update(0.1, anchor, { x = 0, y = 0, vx = 0, vy = 0 })
T.check(replaced[#replaced][1] == 'us_semi', 'the rented semi')
T.check(spawned[1][1] == 'tanker' and spawned[1][2].y == -16, 'its trailer 16 m behind')
bal = W.balance()
J.update(100, anchor, { x = 0, y = 55500, vx = 0, vy = 0 })
T.check(J._active() ~= nil and W.balance() == bal, 'at the drop-off without the trailer: not delivered')
trailer.pos = { x = 0, y = 55480, z = 0 }
J.update(100, anchor, { x = 0, y = 55500, vx = 0, vy = 0 })
T.check(J._active() == nil and trailer.deleted, 'with the trailer: delivered, the trailer returned')
local lp = J.uiState().last.pay
T.near(lp.rent, 45, 1e-9, 'the rent')
T.near(lp.total, 300 * 1.2 - 45, 1e-9, 'early pay less the rent')
T.near(W.balance(), bal + lp.total, 1e-9, 'paid')
T.check(replaced[#replaced][1] == 'pickup', 'your own car back')

T.done('test_jobs')
