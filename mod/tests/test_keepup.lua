-- luajit mod/tests/test_keepup.lua  (keepup.lua: the detail keep-up recorder, a debug switch for tools/keepup.py)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')

local written = {}
writeFile = function(p, s) written[p] = s; return true end
local carPos, carVel = vec3(0, 0, 0), vec3(0, 20, 0)
getPlayerVehicle = function() return { getPosition = function() return carPos end, getVelocity = function() return carVel end } end
log = log or function() end

local trees = require('beamngEarth/trees')
local roads = require('beamngEarth/roads')
local K = require('beamngEarth/keepup')
local anchor = { worldToGeodetic = function(_, x, y) return 39 + y / 110540, -76 + x / 86000 end }
local st = { anchor = anchor, anchorId = 1 }

T.check(K.on == false and trees.onKeepupItem == nil and roads.onKeepupPart == nil, 'off by default: no hooks set')
K.start()
T.check(K.on and trees.onKeepupItem ~= nil and roads.onKeepupPart ~= nil, 'start: the hooks set')
K.tick(st)
-- an item made 150 m ahead, one 50 m behind; the same item again (a solid swap) counts once
local tile = { id = 'g:pg17_1_2', key = 'g17_1_2' }
trees.onKeepupItem(tile, 1, 'polelamp', 0, 150)
trees.onKeepupItem(tile, 1, 'polelamp', 0, 150)
trees.onKeepupItem(tile, 2, 'hydrant', 0, -50)
-- a road tile's road mesh, then its sidewalks
roads.onKeepupPart('road', { gkey = 'g17_1_2', cx = 0, cy = 300 })
roads.onKeepupPart('walk', { gkey = 'g17_1_2', cx = 0, cy = 300 })
roads.onKeepupPart('walk', { gkey = 'g17_1_2', cx = 0, cy = 300 })
local c = K.counts()
T.check(c.items == 2 and c.parts == 2 and c.trace == 1, 'first appearances only (' .. c.items .. ' items, ' .. c.parts .. ' parts)')
local n = K.save('/temp/k.csv')
local s = written['/temp/k.csv'] or ''
T.check(n > 4 and s:match('^H,') ~= nil, 'saved: a header and rows')
local d, al = s:match('\nI,[%d.]+,%d+,[%d.%-]+,[%d.%-]+,([%d.%-]+),([%d.%-]+),2,')
T.check(tonumber(d) == 150 and tonumber(al) == 150, 'an item: its distance and lead along the heading, kind 2 (furniture)')
T.check(s:match('\nP,[%d.]+,walk,g17_1_2,') ~= nil, 'a part: what and its road tile')
K.stop()
T.check(not K.on and trees.onKeepupItem == nil and roads.onKeepupPart == nil, 'stop: the hooks gone')
T.done('test_keepup')
