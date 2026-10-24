-- luajit mod/tests/test_mpanchor.lua  (BeamMP: other players' positions into our frame, mpanchor.lua from Thai-v4's
-- PR #88): a position packet of a player whose anchor is known comes out in our frame (the same real-world point);
-- one of an unknown player, or another kind of packet, passes untouched; nothing before install.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local geodesy = require('beamngEarth/geodesy')
local ma = require('beamngEarth/mpanchor')

-- BeamMP's position handler, stubbed: what reaches it
local got = {}
positionGE = { handle = function(raw) got[#got + 1] = raw end }
local enc = {}
function jsonEncode(t)
  enc[#enc + 1] = t
  return 'ENC' .. #enc
end
function jsonDecode(s)
  local x, y, z = s:match('"pos":%[([^,]+),([^,]+),([^%]]+)%]')
  return { pos = { tonumber(x), tonumber(y), tonumber(z) }, vel = { 1, 0, 0 }, rot = { 0, 0, 0, 1 } }
end
MPCoreNetwork = { isMPSession = function() return true end }

ma.install()
T.check(positionGE.handle ~= nil and ma.status():find('installed=true', 1, true), 'installed: BeamMP\'s handler wrapped')

-- our anchor and player 4's, 3 km apart
ma.announceOwn(36.5, -118.1)
T.check(ma.ownChanged == true, 'our anchor changed: mpsession sends a pose at once')
ma.setRemote(4, 36.52, -118.08)
T.check(ma.knows(4) and not ma.knows(5), 'knows the anchors it was told')

positionGE.handle('p:4-0:{"pos":[100,200,30]}')
local out = enc[#enc]
local theirs, ours = geodesy.newAnchor(36.52, -118.08), geodesy.newAnchor(36.5, -118.1)
local ex, ey, ez = theirs:transformTo(ours):apply(100, 200, 30)
T.check(got[#got] == 'p:4-0:ENC' .. #enc and math.abs(out.pos[1] - ex) < 1e-6 and math.abs(out.pos[2] - ey) < 1e-6
  and math.abs(out.pos[3] - ez) < 1e-6, 'a known player\'s position: in our frame')
-- the same point on Earth: their frame -> geodetic == our frame -> geodetic
local la1, lo1 = theirs:enuToGeodetic(100, 200, 30)
local la2, lo2 = ours:enuToGeodetic(out.pos[1], out.pos[2], out.pos[3])
T.check(math.abs(la1 - la2) < 1e-9 and math.abs(lo1 - lo2) < 1e-9, 'the same real-world point')

-- the Earth's curvature: player 6's anchor 15 km off (a rebase apart), its car beside ours. A flat shift would put
-- it about 17.7 m too high (15 km^2 / 2R) and 0.13 deg turned; the conversion keeps the real place and height.
local aLat6 = 36.5 + 15000 / 111000
ma.setRemote(6, aLat6, -118.1)
local mine6, theirs6 = geodesy.newAnchor(36.5, -118.1), geodesy.newAnchor(aLat6, -118.1)
local te, tn, tu = mine6:toFrame(theirs6, 3, 4, 1.5)          -- a point 5 m from our origin, in their frame
positionGE.handle(string.format('p:6-0:{"pos":[%.9f,%.9f,%.9f]}', te, tn, tu))
local p6 = enc[#enc].pos
T.check(math.abs(p6[1] - 3) < 1e-4 and math.abs(p6[2] - 4) < 1e-4 and math.abs(p6[3] - 1.5) < 1e-4,
  string.format('curvature: a car beside ours 15 km from its anchor lands beside ours (%.4f, %.4f, %.4f)', p6[1], p6[2], p6[3]))
T.check(tu < -15.9 and tu > -16.5 and tn < -14900,   -- (1.5 m up - 17.7 m of drop)
  string.format("curvature: in its own frame it is %.1f m below the plane", tu))

-- a car beyond FAR_M (its player teleported across the world): not placed, isFar until it is near again
local n0 = #got
local fe, fn, fu = mine6:toFrame(theirs6, 0, 500000, 0)          -- 500 km north of us
positionGE.handle(string.format('p:6-0:{"pos":[%.6f,%.6f,%.6f]}', fe, fn, fu))
T.check(#got == n0 and ma.isFar('6-0'), 'a car 500 km off: its position dropped, far')
positionGE.handle(string.format('p:6-0:{"pos":[%.9f,%.9f,%.9f]}', te, tn, tu))
T.check(#got == n0 + 1 and not ma.isFar('6-0'), 'near again: placed, not far')

positionGE.handle('p:5-0:{"pos":[1,2,3]}')
T.check(got[#got] == 'p:5-0:{"pos":[1,2,3]}', 'an unknown player\'s position: untouched')
positionGE.handle('Xn:4-0:{}')
T.check(got[#got] == 'Xn:4-0:{}', 'another kind of packet: untouched')

-- sanitize: a non-finite number anywhere drops the packet; a teleport-fast speed is zeroed (the position kept)
T.check(ma.sanitize({ pos = { 1, 2, 3 }, vel = { 1, 0, 0 }, rot = { 0, 0, 0, 1 } }), 'sanitize: a sane packet')
T.check(not ma.sanitize({ pos = { 1, 0 / 0, 3 } }), 'sanitize: NaN position dropped')
T.check(not ma.sanitize({ pos = { 1, 2, 3 }, rvel = { math.huge, 0, 0 } }), 'sanitize: infinite angular velocity dropped')
T.check(not ma.sanitize({ pos = { 1, 2, 3 }, rot = { 0, 0, 0 / 0, 1 } }), 'sanitize: NaN rotation dropped')
local fast = { pos = { 1, 2, 3 }, vel = { 3000, 0, 0 }, rvel = { 5, 5, 5 } }
T.check(ma.sanitize(fast) and fast.vel[1] == 0 and fast.rvel[1] == 0 and fast.pos[1] == 1,
  'sanitize: a 3000 m/s teleport artefact: velocities zeroed, position kept')
local before = #got
local saved = jsonDecode
jsonDecode = function() return { pos = { 0 / 0, 0, 0 } } end
positionGE.handle('p:4-0:{"pos":[1,2,3]}')
positionGE.handle('p:9-0:{"pos":[1,2,3]}')      -- (a player with no anchor known: no conversion, still checked)
jsonDecode = saved
T.check(#got == before, 'a NaN packet never reaches BeamMP, converted or not')

-- a player's anchor changes: its packets are dropped for SETTLE_S (packets from its old place can still come, and
-- converted with the new anchor they would put the car under the ground), then pass again
local tnow = 1000
ma.clock = function() return tnow end
positionGE.handle('p:4-0:{"pos":[100,200,30]}')
local n1 = #got
ma.setRemote(4, 36.53, -118.07)
positionGE.handle('p:4-0:{"pos":[2,-6,-80]}')
T.check(#got == n1, 'the anchor just changed: the packet dropped')
tnow = tnow + ma.SETTLE_S + 0.1
positionGE.handle('p:4-0:{"pos":[100,200,30]}')
T.check(#got == n1 + 1, 'after SETTLE_S: through again')
-- the other order: a car jumping more than JUMP_M between packets (new-place packets with the old anchor): dropped
-- for SETTLE_S, then the next one is its place
tnow = tnow + 0.5
positionGE.handle('p:4-0:{"pos":[100,200,30]}')
local n2 = #got
tnow = tnow + 0.2
positionGE.handle('p:4-0:{"pos":[8100,200,30]}')
T.check(#got == n2, 'a jump of 8 km between packets: dropped')
tnow = tnow + 0.5
positionGE.handle('p:4-0:{"pos":[8110,200,30]}')
T.check(#got == n2, '... and the next ones within SETTLE_S')
tnow = tnow + ma.SETTLE_S
positionGE.handle('p:4-0:{"pos":[8120,200,30]}')
T.check(#got == n2 + 1, 'after SETTLE_S: taken as the car\'s place')
tnow = tnow + 0.1
positionGE.handle('p:4-0:{"pos":[8125,200,30]}')
T.check(#got == n2 + 2, '... and it moves on from there')
ma.clock = os.clock

ma.reset()
T.check(not ma.knows(4), 'reset: the anchors forgotten')
T.done('test_mpanchor')
