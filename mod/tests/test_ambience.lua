-- luajit mod/tests/test_ambience.lua  (forest wind and room tone, ambience.lua): a vegetation tile's sound nodes go to
-- the stock core_audioForest as one cloud round the listener (its main event the tiles' set), the room tone plays
-- and follows the listener, the set chosen by the tiles' sound items (kept until another has M.SWITCH times them),
-- a rebase moves the nodes, a teleport's old tiles go.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

-- the engine pieces ambience.lua uses
package.preload['kdtreepoint3d'] = function()
  return { new = function() return { n = 0, preLoad = function(self) self.n = self.n + 1 end, build = function() end } end }
end
local clock = 0
os.clockhp = function() return clock end
local lx, ly = 0, 0
SFXSystem = { getPositionXYZ = function() return lx, ly, 0 end }
MatrixF = function()
  local m = { yaw = 0, pos = nil }
  function m:setFromEuler(v) self.yaw = v.z end
  function m:setPosition(p) self.pos = p end
  return m
end
Sim = { upcast = function(o) return o end }
Engine = { generateUUID = function() return 'u' end }
function simObjectExists(o) return S.objects[o.id] ~= nil end
local af = { calls = {} }
function af.setNodeCloud(c) af.cloud = c end
function af.setMainEvent(e) af.main = e end
function af.setEnabled(v) af.enabled = v end
local rb = { set = nil, cleared = 0 }
function rb.createEmitterHost(ev) return { eventNameStr = ev } end
function rb.updateRibbonData(r) r.numSegs = (#r.nodes - 4) / 2 + 1 end
function rb.setRibbons(l) rb.set = l end
function rb.recomputeMap() end
function rb.clearNearFarLists() end
function rb.clearAllSFXEmitters() rb.cleared = rb.cleared + 1 end
extensions = { core_audioForest = af, core_audioRibbon = rb }
local function patch(o)
  function o:setTransform(m) self.xf = m end
  function o:getId() return self.id end
  function o:play() self.playing = true end
  function o:setVolumePitchCT(v) self.vol = v end
  return o
end
local newObject = S.newObject
S.newObject = function(class) return patch(newObject(class)) end
createObject = function(class) return S.newObject(class) end

local amb = require('beamngEarth/ambience')

-- pure: events and vote
T.check(amb.events({ 0, 0 })[1]:match('ConiferousDeciduous_01$') ~= nil, 'temperate _01')
T.check(amb.events({ 2, 1 })[2]:match('Roomtone_Desert_02$') ~= nil, 'desert _02 room tone')
T.check(amb.events({ 3, 0 })[1]:match('Wind_Tropical$') ~= nil, 'tropical')
T.check(amb.events({ 9, 0 }) == nil, 'unknown set: nothing')
local function tile(cx, cy, b, w) return { cx = cx, cy = cy, biome = b, weight = w } end
local v = amb.vote({ tile(0, 0, { 0, 0 }, 10), tile(256, 0, { 2, 0 }, 30) }, 0, 0, nil)
T.check(v[1] == 2, 'the most sound items win')
v = amb.vote({ tile(0, 0, { 0, 0 }, 25), tile(256, 0, { 2, 0 }, 30) }, 0, 0, { 0, 0 })
T.check(v[1] == 0, 'the current set stays while the other has less than SWITCH times its items')
v = amb.vote({ tile(0, 0, { 1, 0 }, 0), tile(5000, 0, { 2, 0 }, 99) }, 0, 0, nil)
T.check(v[1] == 1, 'a tile without trees still gives its set; far tiles do not vote')
-- the ground cover's climate (groundcover.lua): every tile one vote, by its Koppen class (biome[3])
local cl = amb.voteClimate({ tile(0, 0, { 0, 0, 15 }, 90), tile(256, 0, { 1, 0, 8 }, 0), tile(0, 256, { 1, 0, 8 }, 0) },
  0, 0, nil)
T.check(cl[3] == 8, 'climate: the most tiles win, trees or not')
cl = amb.voteClimate({ tile(0, 0, { 0, 0, 15 }, 0), tile(-256, 0, { 0, 0, 15 }, 0), tile(256, 0, { 1, 0, 8 }, 0),
  tile(0, 256, { 1, 0, 8 }, 0) }, 0, 0, { 0, 0, 15 })
T.check(cl[3] == 15, 'climate: the current one stays below SWITCH times its tiles')
cl = amb.voteClimate({ tile(0, 0, { 2, 1 }, 0) }, 0, 0, nil)
T.check(cl[1] == 2 and cl[3] == nil, 'climate: a tile from before the class by its set')

-- a tile's nodes as the cloud round the listener
amb.onTile(1, '0_0', { biome = { 0, 1 }, audio = { 10, 20, 5, 6, 0.5, 30, 200, 200, 5, 3, 0, 4, 2000, 0, 0, 1, 1, 1 } })
amb.update(1)
T.check(af.cloud and af.cloud.nodeCount == 2, 'the two nodes within the radius: ' .. tostring(af.cloud and af.cloud.nodeCount))
T.check(af.cloud.nodeDepth[1] == 6 and af.cloud.nodeSourceCount[1] == 30, 'depth and count kept')
T.check(af.main:match('ConiferousDeciduous_02$') ~= nil and af.enabled == true, 'main event set, enabled')
local rt = S.liveOf('SFXEmitter')
T.check(#rt == 1 and rt[1].fields.track:match('Roomtone_ConiferousDeciduous_02$') ~= nil and rt[1].playing, 'the room tone plays')

-- not built again until moved or changed (and not more often than GAP_S)
local builds = amb.builds
clock = 5
amb.update(1)
T.check(amb.builds == builds, 'nothing changed: not built again')
lx = 400
amb.update(1)
T.check(amb.builds == builds + 1, 'moved past MOVE_M: built again')
T.check(rt[1].pos.x == 400, 'the room tone follows the listener')

-- rebase: the nodes move (a ghost), and a teleport's tiles go
local xf = { apply = function(_, x, y, z) return x + 1000, y, z end }
amb.transform(xf)
lx = 1400
clock = 10
amb.update(2)
T.check(af.cloud.nodeCount == 2 and af.cloud.nodeX[1] == 1010, 'the ghost tile\'s nodes moved with the rebase')
amb.onTile(3, '0_0', { biome = { 0, 0 }, audio = {} })
clock = 15
amb.update(3)
T.check(af.cloud.nodeCount == 2, 'the ghost stays under another anchor\'s tile elsewhere')
clock = 200
amb.update(3)
T.check(af.cloud.nodeCount == 0 and af.enabled == false, 'the ghost goes after GHOST_MAX_S: no nodes, disabled')
lx = 0
clock = 202
amb.update(3)
T.check(af.main:match('_01$') ~= nil and S.liveOf('SFXEmitter')[1].fields.track:match('_01$') ~= nil,
  'back by the new tile: the wind and room tone of its set')
-- the room tone deleted by someone else: made again (no error every frame)
local old = S.liveOf('SFXEmitter')[1]
old:delete()
clock = 210
amb.update(3)
local now = S.liveOf('SFXEmitter')
T.check(#now == 1 and now[1].id ~= old.id, 'a deleted room tone: made again')

-- rivers: bank pairs across the flow at the surface, as wide as the water; made again on a change only
local bk = amb.banks({ { 0, 0, 5, 10 }, { 10, 0, 5, 10 }, { 20, 0, 4, 6 } })
T.check(#bk == 6, 'two bank points a node')
T.check(bk[1][1] == 0 and bk[1][2] == 5 and bk[2][2] == -5 and bk[1][3] == 5, 'left then right of an eastward flow, half the width each')
T.check(bk[5][2] == 3 and bk[6][2] == -3 and bk[5][3] == 4, 'the last node: its own width and z')
local seaV = { riversVersion = 1 }
local riverSet = { Ra = { kind = 'river', flow = 2, nodes = { { 0, 0, 5, 10 }, { 10, 0, 5, 10 } } },
                   Rb = { kind = 'stream', flow = 1, nodes = { { 0, 50, 6, 4 }, { 0, 60, 6, 4 }, { 0, 70, 6, 4 } } } }
seaV.rivers = function() return riverSet, 3 end
amb.update(3, seaV)
T.check(rb.set and #rb.set == 2 and rb.set[1].persistantId == 'Ra' and #rb.set[2].nodes == 6, 'a ribbon a river, in key order')
T.check(rb.set[1].emitters[2].eventNameStr == amb.RIVER_EVENT and rb.set[1].depths[1] == 10 and rb.set[1].speed == 1,
  'the stock river event, depth and speed')
local first = rb.set
seaV.riversVersion = 2
amb.update(3, seaV)
T.check(rb.set == first, 'the same rivers: not made again')
amb.update(4, seaV)
T.check(rb.set ~= first and #rb.set == 0 and rb.cleared == 1, 'another anchor\'s rivers: none, the emitters cleared')
-- only the rivers near the listener; a river's ribbon made once
riverSet.Rc = { kind = 'river', flow = 2, nodes = { { 5000, 0, 5, 10 }, { 5010, 0, 5, 10 } } }
riverSet.Rd = { kind = 'canal', flow = 0.5, nodes = { { 0, 20, 5, 10 }, { 10, 20, 5, 10 } } }   -- (still water: none)
seaV.riversVersion = 3
amb.update(3, seaV)
T.check(#rb.set == 2 and rb.set[1].persistantId == 'Ra', 'the far river left out')
local ra, made = rb.set[1], amb.ribbonsMade
lx = 4800
clock = 300
amb.update(3, seaV)
T.check(#rb.set == 1 and rb.set[1].persistantId == 'Rc', 'moved: the river there instead')
lx = 0
amb.update(3, seaV)
T.check(#rb.set == 2 and rb.set[1] == ra and amb.ribbonsMade == made + 1, 'back: the first ribbons reused, only Rc was made')

-- tunnels' and bridges' sound spaces: a box a row, its ambience by kind, gone with the tile, moved by a rebase
amb.onSpaces('1:3_4', { 0, 10, 20, 5, 42, 9.4, 8.2, 0.5, 1, 100, 0, 3, 30, 9, 24, 0, 7, 0, 0, 0, 1, 1, 1, 0 })
local sp = S.liveOf('SFXSpace')
T.check(#sp == 2, 'a space a known kind (kind 7: none): ' .. #sp)
table.sort(sp, function(p, q) return p.id < q.id end)
T.check(sp[1].fields.soundAmbience == 'Level_Tunnel_Closed' and sp[2].fields.soundAmbience == 'Level_Under_Bridge_A',
  'the tunnel\'s and the bridge\'s ambience')
T.check(sp[1].scale.x == 42 and sp[1].scale.y == 9.4 and sp[1].scale.z == 8.2 and sp[1].xf.yaw == -0.5
  and sp[1].xf.pos.x == 10 and sp[1].xf.pos.z == 5, 'placed: full size, the yaw clockwise')
amb.transform({ apply = function(_, x, y, z) return -y, x, z end })     -- (a quarter turn)
T.check(math.abs(sp[1].xf.pos.x + 20) < 1e-9 and math.abs(sp[1].xf.pos.y - 10) < 1e-9
  and math.abs(sp[1].xf.yaw + 0.5 + math.pi / 2) < 1e-9, 'a rebase turns and moves them')
amb.dropSpaces('1:3_4')
local live = S.liveOf('SFXSpace')
T.check(#live == 2 and live[1].pos.z == amb.PARK_Z and live[2].pos.z == amb.PARK_Z and amb.spacesParked == 2,
  'the tile goes: its spaces parked out of reach, never deleted (a deleted space crashed the sound world)')
local made = amb.spacesMade
amb.onSpaces('1:9_9', { 1, 50, 60, 7, 30, 12, 9, 0 })
local reused = scenetree.findObjectById(sp[2].id)
T.check(amb.spacesMade == made and #S.liveOf('SFXSpace') == 2 and amb.spacesParked == 1 and reused.xf.pos.x == 50
  and reused.fields.soundAmbience == 'Level_Under_Bridge_A', 'a new bridge space: the parked one of its ambience reused')
amb.dropSpaces('1:9_9')
amb.onSpaces('1:5_5', { 4, 3, 4, 5, 0, 0, 0, 0 })
local pe = nil
for _, o in ipairs(S.liveOf('SFXEmitter')) do if o.fields.track == amb.PORTAL_EVENT then pe = o end end
T.check(pe and pe.playing and pe.pos.x == 3 and pe.pos.z == 5, 'a portal: the stock portal event playing there')
amb.dropSpaces('1:5_5')
T.check(not scenetree.findObjectById(pe.id), 'and gone with its tile')

-- shores: a "Waves" ribbon along a shoreline near the listener, SHORE_W wide, with the rivers
seaV.shores = function() return { S1 = { kind = 'sea', nodes = { { 0, -30, 0 }, { 40, -30, 0 } } } }, 3 end
seaV.riversVersion = 9
amb.update(3, seaV)
local sh
for _, rbn in ipairs(rb.set) do if rbn.persistantId == 'S1' then sh = rbn end end
T.check(sh and sh.emitters[2].eventNameStr == amb.SHORE_EVENT and #rb.set == 3, 'the shore ribbon, with the rivers')
T.check(sh and math.abs(sh.nodes[1].y - sh.nodes[2].y) == amb.SHORE_W and amb.shoreRibbons == 1, 'SHORE_W across')
seaV.shores = nil

-- rain in layers (weather phase B): the weights by rate, the mix under a roof, in a tunnel and in the car
local w0 = amb.rainWeights(0.1)
T.check(w0.light == 1 and w0.medium == 0 and w0.heavy == 0, 'drizzle: the light layer only')
local w3 = amb.rainWeights(5)
T.check(math.abs(w3.medium - 1) < 1e-9, '5 mm/h: the medium layer')
local w6 = amb.rainWeights(10)
T.check(w6.medium > 0 and w6.heavy > 0 and math.abs(w6.medium ^ 2 + w6.heavy ^ 2 - 1) < 1e-9 and w6.light == 0,
  '10 mm/h: medium into heavy, equal power')
local wl = amb.rainWeightsLinear(10)
T.check(math.abs(w6.medium - math.sqrt(wl.medium)) < 1e-12, 'equal power: the square roots of the old linear weights')
T.check(amb.rainWeights(50).heavy == 1, 'a downpour: heavy')
local mx = amb.rainMix(0.5, 5, 1, false, false)
T.check(math.abs(mx.medium - 0.5) < 1e-9 and mx.muffled == 0 and mx.roof_medium == 0, 'in the open: the layer at the volume')
mx = amb.rainMix(0.5, 5, 0, false, false)
T.check(mx.medium == 0 and math.abs(mx.muffled - 0.5 * amb.RAIN_UNDER_ROOF) < 1e-9, 'under a roof: muffled')
mx = amb.rainMix(0.5, 5, 1, true, false)
T.check(mx.medium == 0 and math.abs(mx.muffled - 0.5 * amb.RAIN_IN_TUNNEL) < 1e-9, 'in a tunnel: muffled, quieter')
mx = amb.rainMix(0.5, 5, 1, false, true)
T.check(math.abs(mx.medium - 0.5 * amb.RAIN_CABIN) < 1e-9 and math.abs(mx.roofcab_medium - 0.5 * amb.RAIN_ROOF) < 1e-9
  and mx.roofcab_light == 0 and mx.roofcab_heavy == 0 and mx.roof_medium == 0, 'in the car: the roof through the cabin')
mx = amb.rainMix(0.5, 13, 1, false, true)
T.check(mx.roofcab_medium > 0 and mx.roofcab_heavy > 0
  and math.abs(math.sqrt(mx.roofcab_medium ^ 2 + mx.roofcab_heavy ^ 2) - 0.5 * amb.RAIN_ROOF) < 1e-9,
  'heavier rain on the roof: the medium and heavy roof loops crossfaded')
-- outside: the plain roof by the camera (hood / bumper full, orbit lightly by distance), none far off
mx = amb.rainMix(0.5, 5, 1, false, false, 1)
T.check(math.abs(mx.roof_medium - 0.5 * amb.RAIN_ROOF) < 1e-9 and mx.roofcab_medium == 0 and math.abs(mx.medium - 0.5) < 1e-9,
  'hood camera: the plain roof, the rain unmuffled')
T.check(amb.roofNearFor('onboard', 30) == 1 and amb.roofNearFor('driver', 1) == 0, 'roofNear: hood 1, inside 0')
T.check(math.abs(amb.roofNearFor('orbit', amb.ROOF_NEAR_M) - amb.ROOF_ORBIT) < 1e-12 and amb.roofNearFor('orbit', amb.ROOF_FAR_M) == 0
  and amb.roofNearFor('orbit', 8) > 0 and amb.roofNearFor('orbit', 8) < amb.ROOF_ORBIT, 'roofNear: orbit by distance')
-- under an open shelter (a bridge, an overpass): muffled B; enclosed or a tunnel: muffled A
mx = amb.rainMix(0.5, 5, 0, false, false, 0, true)
T.check(mx.muffled == 0 and math.abs(mx.muffled_open - 0.5 * amb.RAIN_UNDER_ROOF) < 1e-9, 'under an open bridge: muffled B')
mx = amb.rainMix(0.5, 5, 1, true, false, 0, true)
T.check(mx.muffled_open == 0 and mx.muffled > 0, 'a tunnel: muffled A even when the last shelter was open')
T.check(amb.openShelter('bridge') and amb.openShelter('roof') and not amb.openShelter('roof_enclosed')
  and not amb.openShelter('tunnel') and not amb.openShelter('parking'), 'shelter kinds: open and enclosed')

local rainParticles = S.newObject('Precipitation')
rainParticles:registerObject('bngEarthRain')
local function layer(file)
  for _, o in ipairs(S.liveOf('SFXEmitter')) do if o.fields.fileName == amb.RAIN_DIR .. file then return o end end
end
beamngEarth_weather = { rainNow = 1, rainMmHNow = 5, shelter = 1 }
clock = 380; amb.update(3, seaV)
clock = 381; amb.update(3, seaV)
local me = layer('bne_rain_medium.ogg')
T.check(me and me.fields.isLooping == '1' and math.abs(me.vol - amb.RAIN_GAIN / amb.RAIN_FADE_S) < 1e-9, 'rain layers: medium fading in')
T.check(layer('bne_rain_heavy.ogg') == nil and layer('bne_rain_light.ogg') == nil, 'only the layer of the rate')
T.check(me.fieldsAtRegister.description == 'AudioEnvironment' and me.fieldsAtRegister.sourceGroup == 'AudioChannelEnvironment',
  'rain layers: on the Environment channel, set before registerObject')
for i = 1, 8 do clock = 381 + i * 0.5; amb.update(3, seaV) end
T.check(math.abs(me.vol - amb.RAIN_GAIN) < 1e-9, 'rain layers: full')
beamngEarth_weather.shelter = 0
clock = 386; amb.update(3, seaV)
local mu = layer('bne_rain_muffled.ogg')
T.check(mu and math.abs(mu.vol - amb.RAIN_GAIN * amb.RAIN_UNDER_ROOF) < 1e-9 and layer('bne_rain_medium.ogg') == nil,
  'under a roof: the muffled layer instead')
beamngEarth_weather.rainNow = 0
for i = 1, 12 do clock = 386 + i * 0.5; amb.update(3, seaV) end
T.check(layer('bne_rain_muffled.ogg') == nil and layer('bne_rain_medium.ogg') == nil, 'the rain stopped: the layers gone')
-- ... and the drips after it, fading out over DRIPS_S
local dr = layer('bne_drips_1.ogg')
T.check(dr and dr.vol > 0 and dr.fieldsAtRegister.description == 'AudioEnvironment' and layer('bne_drips_3.ogg') == nil,
  'after the rain: the drips, the densest loop first')
local d1 = amb.dripsLevel(1, amb.DRIPS_S * 0.7)
T.check(math.abs(d1[1] - 1) < 1e-12 and math.abs(d1[2] - 0.7) < 1e-12, 'drips at 70 %: full level, the sparsest layer')
local em3 = amb.extraMix(0, d1, 0, 1, false, false)
T.check(em3.drips_3 > 0 and em3.drips_1 == 0, 'drips at 70 %: drips_3')
T.check(amb.dripsLevel(1, amb.DRIPS_S * 0.9)[1] < 1 and amb.dripsLevel(1, amb.DRIPS_S)[1] == 0, 'the drips fade out at the end')
clock = 386 + 6 + amb.DRIPS_S + 1; amb.update(3, seaV)
for i = 1, 8 do clock = clock + 0.5; amb.update(3, seaV) end
T.check(layer('bne_drips_1.ogg') == nil and layer('bne_drips_2.ogg') == nil and layer('bne_drips_3.ogg') == nil,
  'the drips stop after DRIPS_S')
-- wind by strength: none when calm, the layer of the speed when windy, gone when calm again
beamngEarth_weather = { rainNow = 0, rainMmHNow = 0, shelter = 1, windMsNow = 0.5 }
clock = clock + 0.5; amb.update(3, seaV)
T.check(layer('bne_wind_gen_light.ogg') == nil and next(amb.extraLayers) == nil, 'calm: no wind layer')
beamngEarth_weather.windMsNow = 8
for i = 1, 8 do clock = clock + 0.5; amb.update(3, seaV) end
local wm = layer('bne_wind_gen_moderate.ogg')
T.check(wm and math.abs(wm.vol - amb.WIND_GAIN) < 1e-9 and layer('bne_wind_gen_strong.ogg') == nil, '8 m/s: the moderate wind (generated, the default)')
beamngEarth_weather.windMsNow = 0
for i = 1, 12 do clock = clock + 0.5; amb.update(3, seaV) end
T.check(layer('bne_wind_gen_moderate.ogg') == nil and next(amb.extraLayers) == nil, 'calm again: the wind layer gone')
-- hail
beamngEarth_weather.hailNow = 1
for i = 1, 8 do clock = clock + 0.5; amb.update(3, seaV) end
T.check(layer('bne_hail.ogg') and math.abs(layer('bne_hail.ogg').vol - amb.HAIL_GAIN) < 1e-9, 'hail: its layer')
beamngEarth_weather.hailNow = 0
for i = 1, 12 do clock = clock + 0.5; amb.update(3, seaV) end
T.check(layer('bne_hail.ogg') == nil, 'the hail stopped')
-- the pure mix: equal-power wind between the centres, damped in the car, none in a tunnel
local em = amb.extraMix(5, 0, 0, 1, false, false)
T.check(math.abs(math.sqrt(em.wind_light ^ 2 + em.wind_moderate ^ 2) - amb.WIND_GAIN) < 1e-9, 'wind 5 m/s: light into moderate')
T.check(amb.extraMix(8, 0, 0, 1, true, false).wind_moderate == 0, 'in a tunnel: no wind')
T.check(math.abs(amb.extraMix(8, 0, 0, 1, false, true).wind_moderate - amb.WIND_GAIN * amb.RAIN_CABIN) < 1e-9, 'in the car: damped')
T.check(amb.extraMix(0.5, 0, 0, 1, false, false).wind_light == 0, 'under WIND_FROM_MS: silent')
T.check(math.abs(amb.dripsLevel(0.8, amb.DRIPS_S / 2)[1] - 0.8) < 1e-12 and amb.dripsLevel(1, amb.DRIPS_S + 1)[1] == 0, 'drips fade')
-- the Wind sound: generated plays bne_wind_gen_*, recorded bne_wind_*; a change swaps the playing layer
amb.setWindSet('generated')
beamngEarth_weather = { rainNow = 0, rainMmHNow = 0, shelter = 1, windMsNow = 8 }
for i = 1, 8 do clock = clock + 0.5; amb.update(3, seaV) end
T.check(layer('bne_wind_gen_moderate.ogg') ~= nil and layer('bne_wind_moderate.ogg') == nil, 'Wind sound generated: bne_wind_gen_moderate')
amb.setWindSet('recorded')
clock = clock + 0.5; amb.update(3, seaV)
T.check(layer('bne_wind_moderate.ogg') ~= nil and layer('bne_wind_gen_moderate.ogg') == nil, 'Wind sound recorded: bne_wind_moderate')
beamngEarth_weather.windMsNow = 0
for i = 1, 12 do clock = clock + 0.5; amb.update(3, seaV) end
amb.setWindSet('generated')
-- the Rain sound: recorded swaps the layers' files (the same names, bne_rain_rec_*)
amb.setRainSet('recorded')
beamngEarth_weather = { rainNow = 1, rainMmHNow = 5, shelter = 1 }
for i = 1, 6 do clock = clock + 0.5; amb.update(3, seaV) end
T.check(layer('bne_rain_rec_medium.ogg') ~= nil and layer('bne_rain_medium.ogg') == nil, 'recorded: bne_rain_rec_medium')
amb.setRainSet('generated')
clock = clock + 0.5; amb.update(3, seaV)
T.check(layer('bne_rain_medium.ogg') ~= nil and layer('bne_rain_rec_medium.ogg') == nil, 'generated again: the generated file')
beamngEarth_weather.rainNow = 0
for i = 1, 12 do clock = clock + 0.5; amb.update(3, seaV) end
amb.clear()
beamngEarth_weather = nil
clock = 392; amb.update(3, seaV)   -- (the clock and the last update as the tests below expect them)

-- the stock loop (no layer files): fades in with the weather's rain, out inside a tunnel's space, goes when it stops
-- the duck under a strike: down at once, the last second easing back
local t0 = os.clockhp()
amb.duck(3, 0.35)
T.check(math.abs(amb.duckFactor(t0) - 0.35) < 1e-9 and math.abs(amb.duckFactor(t0 + 2.5) - (0.35 + 0.65 * 0.5)) < 0.02
  and amb.duckFactor(t0 + 3.1) == 1, 'the rain ducked under a strike, back after 3 s')

-- the scheduled duck (CLOSE_STRIKE_AND_LOUDNESS.md): the depth by distance, a pre-dip from the flash, a 15 ms ramp
-- ending at the crack, the hold, the release; the music on the same envelope, back to the player's setting at the end
T.check(amb.duckDepthDb(60) == -10 and amb.duckDepthDb(280) == -5 and amb.duckDepthDb(20) == -10
  and math.abs(amb.duckDepthDb(170) - -7.5) < 1e-9, 'depth: -10 dB at 60 m to -5 dB at 280 m')
-- the thunder-over-rain options (2026-11-26): one name picks the rain's gain and whether closes duck
T.check(amb.MIX_OPTIONS[amb.MIX] and amb.RAIN_GAIN == amb.MIX_OPTIONS[amb.MIX].rainGain
  and amb.CLOSE_DUCK == amb.MIX_OPTIONS[amb.MIX].closeDuck, 'the mix option sets RAIN_GAIN and CLOSE_DUCK')
for name, o in pairs(amb.MIX_OPTIONS) do
  T.check(o.rainGain > 0 and o.rainGain <= 0.5 and type(o.closeDuck) == 'boolean', 'mix option ' .. name)
end
T.check(math.abs(20 * math.log10(amb.MIX_OPTIONS.rainlow.rainGain / 0.5) - -3) < 0.2
  and math.abs(20 * math.log10(amb.MIX_OPTIONS.both.rainGain / 0.5) - -2) < 0.1, 'rainlow -3 dB, both -2 dB')
T.check(amb.closeDuckDepthDb(300) == -6 and amb.closeDuckDepthDb(1500) == -3 and amb.closeDuckDepthDb(100) == -6
  and amb.closeDuckDepthDb(3000) == -3 and math.abs(amb.closeDuckDepthDb(900) - -4.5) < 1e-9,
  'close duck: -6 dB at 300 m to -3 dB at 1500 m')
do
  local saved = amb.CLOSE_DUCK
  amb.CLOSE_DUCK = false
  T.check(amb.ducksFor('strike') and not amb.ducksFor('close') and not amb.ducksFor('mid'), 'current: only strikes duck')
  amb.CLOSE_DUCK = true
  T.check(amb.ducksFor('strike') and amb.ducksFor('close') and not amb.ducksFor('far'), 'duck: closes too')
  local c = os.clockhp() + 10   -- (past the old timed duck above)
  amb.scheduleDuck(c, c + 0.5, 900, 'close')
  local f = amb.duckFactor(c + 0.6)
  T.check(math.abs(f - 10 ^ (-4.5 / 20)) < 1e-9, 'a close crack at 900 m: the rain -4.5 dB (' .. f .. ')')
  amb._updateDucks(c + 10)
  amb.CLOSE_DUCK = saved
end
local ev = { flash = 100, t0 = 100.5, depth = -10 }
local pre = amb.PREDIP_DB / -10
T.check(amb.duckEnvelope(ev, 99.9) == 0, 'before the flash: nothing')
T.check(math.abs(amb.duckEnvelope(ev, 100.2) - pre) < 1e-12, 'flash to the ramp: the pre-dip')
T.check(math.abs(amb.duckEnvelope(ev, 100.5 - amb.DUCK_RAMP_S / 2) - (pre + (1 - pre) / 2)) < 1e-9, 'the ramp, half way')
T.check(amb.duckEnvelope(ev, 100.5) == 1 and amb.duckEnvelope(ev, 100.5 + amb.DUCK_HOLD_S - 0.01) == 1, 'held from the crack')
local e, over = amb.duckEnvelope(ev, 100.5 + amb.DUCK_HOLD_S + amb.DUCK_RELEASE_S / 2)
T.check(math.abs(e - 0.5) < 1e-9 and not over, 'released linearly (in dB)')
e, over = amb.duckEnvelope(ev, 100.5 + amb.DUCK_HOLD_S + amb.DUCK_RELEASE_S + 0.01)
T.check(e == 0 and over, 'over after the release')
local musicSet = {}
Engine.Audio = { getChannelVolume = function(ch, actual) return 0.8 end,
  setChannelVolume = function(ch, v, temp) musicSet[#musicSet + 1] = { ch, v, temp } end }
local c0 = os.clockhp()
amb.scheduleDuck(c0, c0 + 0.5, 60)
T.check(math.abs(amb.duckFactor(c0 + 0.6) - 10 ^ (-10 / 20)) < 1e-9, 'the rain at -10 dB under a 60 m crack')
amb._updateDucks(c0 + 0.6)
local last = musicSet[#musicSet]
T.check(last and last[1] == 'AudioChannelMusic' and math.abs(last[2] - 0.8 * 10 ^ (amb.MUSIC_DB / 20)) < 1e-9 and last[3] == true,
  'the music ducked on the same envelope, from the player\'s setting, temporarily')
amb._updateDucks(c0 + 0.5 + amb.DUCK_HOLD_S + amb.DUCK_RELEASE_S + 0.1)
last = musicSet[#musicSet]
T.check(math.abs(last[2] - 0.8) < 1e-9, 'the duck over: the music back to the player\'s setting')
local n = #musicSet
amb._updateDucks(c0 + 10)
T.check(#musicSet == n, 'no duck: nothing set')
Engine.Audio = nil

amb._setLayersOk(false)
local function rainEm()
  for _, o in ipairs(S.liveOf('SFXEmitter')) do if o.fields.track == amb.RAIN_TRACK then return o end end
end
beamngEarth_weather = { rainNow = 1 }
clock = 400; amb.update(3, seaV)
clock = 401; amb.update(3, seaV)
local re = rainEm()
T.check(re and re.fields.track == amb.RAIN_TRACK and math.abs(re.vol - amb.RAIN_GAIN / amb.RAIN_FADE_S) < 1e-9,
  'rain: the loop, fading in')
T.check(scenetree.findObject('bngEarthRain') == rainParticles, 'rain sound does not shadow the particle emitter')
for i = 1, 8 do clock = 401 + i * 0.5; amb.update(3, seaV) end
T.check(math.abs(re.vol - amb.RAIN_GAIN) < 1e-9, 'full rain: RAIN_GAIN')
amb.onSpaces('3:0_0', { 0, lx, 0, 0, 40, 10, 10, 0 })
clock = 405.5; amb.update(3, seaV)
T.check(amb.inTunnel(lx, 0, 0) and re.vol < amb.RAIN_GAIN, 'in a tunnel: fading out')
amb.dropSpaces('3:0_0')
beamngEarth_weather.rainNow = 0
for i = 1, 12 do clock = 405.5 + i * 0.5; amb.update(3, seaV) end
T.check(rainEm() == nil, 'the rain stopped: the loop gone')
beamngEarth_weather = nil

amb.clear()
T.check(#S.liveOf('SFXEmitter') == 0 and af.enabled == false, 'clear: the room tone goes')
-- clear (the level, the connection): a running duck's music back to the player's setting
do
  local set = {}
  Engine.Audio = { getChannelVolume = function() return 0.7 end,
    setChannelVolume = function(ch, v, temp) set[#set + 1] = { ch, v, temp } end }
  local c = os.clockhp()
  amb.scheduleDuck(c, c + 0.5, 60)
  amb._updateDucks(c + 0.6)
  amb.clear()
  T.check(#set >= 2 and math.abs(set[#set][2] - 0.7) < 1e-9, 'clear: the music back')
  T.check(math.abs(amb.duckFactor(c + 10) - 1) < 1e-9, 'clear: no duck left')
  Engine.Audio = nil
end

-- the Generated rain (method B, 2026-11-26): every file the layers name is shipped, the cabin and the open shelter
-- with their own loops (not the outside roof's / the enclosed muffled loop as before)
do
  local base = dir .. '/../beamng_earth/levels/beamng_earth/art/sound/weather/'
  local files = { amb.RAIN_MUFFLED, amb.RAIN_MUFFLED_OPEN }
  for _, l in ipairs(amb.RAIN_LAYERS) do files[#files + 1] = l[2] end
  for _, f in pairs(amb.RAIN_ROOF_LAYERS) do files[#files + 1] = f end
  for _, f in pairs(amb.RAIN_CABIN_LAYERS) do files[#files + 1] = f end
  local missing = {}
  for _, f in ipairs(files) do
    local h = io.open(base .. f, 'rb')
    if h then h:close() else missing[#missing + 1] = f end
  end
  T.check(#files == 11 and #missing == 0, 'Generated rain: all 11 files shipped (missing: ' .. table.concat(missing, ', ') .. ')')
  T.check(amb.RAIN_CABIN_LAYERS.medium ~= amb.RAIN_ROOF_LAYERS.medium and amb.RAIN_MUFFLED_OPEN ~= amb.RAIN_MUFFLED,
    'Generated rain: own cabin and open-shelter loops')
end

T.done('test_ambience')
