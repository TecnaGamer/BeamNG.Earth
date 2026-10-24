-- luajit mod/tests/test_serverlist.lua  (serverlist.lua: the BeamNG.Earth servers for the tab in BeamMP's browser):
-- asked of the world server, its answer read safely, the wait bounded.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local net = require('beamngEarth/net')
local sent, up = {}, false
net.send = function(m) sent[#sent + 1] = m end
net.isConnected = function() return up end
local sl = require('beamngEarth/serverlist')

T.check(sl.refresh() == false and sl.state().state == 'error' and #sent == 0, 'no world server: said, nothing sent')
up = true
T.check(sl.refresh() and sent[1].type == 'server_list' and sl.state().state == 'loading', 'asked of the world server')
T.check(sl.refresh() and #sent == 1, 'one request at a time')
sl.onList({ type = 'server_list', servers = {
  { ip = '1.2.3.4', port = 30814, name = 'A', players = 2, maxPlayers = 8, version = '1.6.2' },
  { ip = 5, port = 1 }, { port = 2 }, 'junk', { ip = '9.9.9.9', port = '30815' } } })
local st = sl.state()
T.check(st.state == 'done' and #st.servers == 2 and st.servers[1].name == 'A' and st.servers[2].port == 30815
  and st.servers[2].players == 0, 'the answer: valid entries only, fields made safe')
sl.refresh()
sl.onList({ type = 'server_list', error = 'the server list did not answer' })
T.check(sl.state().state == 'error' and #sl.state().servers == 2, 'an error keeps the last list')
sl.refresh()
sl.onUpdate(sl.TIMEOUT_S + 1)
T.check(sl.state().state == 'error', 'no answer: a timeout')

T.done('test_serverlist')
