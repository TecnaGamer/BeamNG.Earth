-- luajit mod/tests/test_worldtraffic.lua  (the phone's world traffic switch: 15 cars, 2 police; off removes them)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local calls, provider = {}, nil
gameplay_traffic = { setupTraffic = function(n, opt) calls[#calls + 1] = { 'setup', n, opt.police } end,
  deleteVehicles = function() calls[#calls + 1] = { 'delete' } end,
  registerSpecialVehicleProvider = function(p) provider = p end }
local W = require('beamngEarth/worldtraffic')
T.check(W.set(true) and W.isOn(), 'on')
T.check(calls[1][1] == 'setup' and calls[1][2] == 15 and calls[1][3], '15 cars with police')
T.check(provider and provider.getDesiredCount(15, { police = true }) == 2, 'two of them police')
T.check(W.set(false) and not W.isOn() and calls[2][1] == 'delete', 'off: removed')
W._reset()
calls = {}
W.update(5, true)
T.check(#calls == 0, 'not right at the start')
W.update(10, true)
T.check(#calls == 1 and W.isOn(), 'the saved choice applied after the level is up')
W.update(30, true)
T.check(#calls == 1, 'once')
T.done('test_worldtraffic')
