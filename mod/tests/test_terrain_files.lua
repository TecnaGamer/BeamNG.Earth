-- luajit mod/tests/test_terrain_files.lua
package.path = (arg[0]:match('^(.*)[/\\]') or '.') .. '/../beamng_earth/lua/ge/extensions/?.lua;' .. package.path
local files = require('beamngEarth/terrainfiles')
local st, bound, released = {}, nil, {}
local net = {isReady = function() return true end, lease = function() return 'a' end,
  send = function(m) assert(m.type == 'terrain_release'); released = m.tickets; return true end}
local terrain = {get = function() return {getField = function() return bound end} end}
local function tr(i, name)
  local path = '/temp/beamngEarth/a/' .. (name or tostring(i))
  local msg = {terrainTicket = i, ter = path .. '.ter', heightmap = path .. '.png'}
  files.received(msg)
  return msg
end
local function check(expected)
  released = {}
  files.release(st, net, terrain)
  assert(table.concat(released, ',') == expected, table.concat(released, ',') .. ' expected ' .. expected)
end
st.blockMsg = tr(1)
st.deferredRefresh = tr(2)
st.terrainPending = {msg = tr(3)}
st.pending = {msg = {terrain = tr(4)}}
st.refreshLater = tr(5)
st.rebasing = {terrain = tr(6)}
st.swap = {oldTerrain = tr(7), terrains = {tr(8)}}
bound = tr(9).ter
tr(10)
check('10')
-- Lost connection after the server staged the swap. The old socket also left an anchor reply in the budget queue.
local queue = {head = 1, q = {{terrain = {heightmap = st.blockMsg.heightmap, terrainTicket = 11}}}}
assert(table.concat(files.snapshot(st, queue, 'a', terrain), ',') == '1,2,3,4,5,6,7,8,9,11')
assert(table.concat(files.sessions({'b'}), ',') == 'a,b')
assert(table.concat(files.sessions({'a', 'b'}), ',') == 'a,b')
st.swap, st.blockMsg, st.deferredRefresh = nil, st.deferredRefresh, nil
check('1,7,8')
-- Fallback imported the PNG but the engine still names 9. A second delivery of that file need not pin both.
tr(12, '9')
check('12')
bound = st.blockMsg.ter
check('9')
st = {}
check('3,4,5,6')
bound = nil
check('2')
-- A stand-in base can outlive the revision's terrain binding. Level teardown destroys the object.
local photo = tr(13)
photo.base = '/temp/beamngEarth/a/hm_1_0_0_base_g13.png'
terrain.base = function() return photo.base end
check('')
terrain.get = function() return nil end
check('13')
assert(#files.sessions({}) == 0)
print('terrain releases match installed, pending, bound, rollback and queued revisions')
