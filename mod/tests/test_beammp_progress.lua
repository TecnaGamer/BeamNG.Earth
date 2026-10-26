-- Server persistence and reconnect lifecycle with BeamMP events and an interrupted disk write.
-- Runs under LuaJIT and the server's Lua 5.3.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local function copy(t)
  if type(t) ~= 'table' then return t end
  local out = {}; for k,v in pairs(t) do out[k]=copy(v) end; return out
end
local blobs, serial, files, fail = {}, 0, {}, false
local randomCounter = 0
local realFS = FS
local dirs, directoryFails = {}, false
FS = {CreateDirectory=function(path)
  if directoryFails then return false, 'read-only directory' end
  dirs[path]=true; return true, ''
end}
Util = {
  RandomIntRange=function(lo,hi) randomCounter=randomCounter+1;return randomCounter%hi end,
  JsonEncode=function(value) serial=serial+1; local s='json:'..serial; blobs[s]=copy(value); return s end,
  JsonDecode=function(text) assert(blobs[text], 'invalid JSON'); return copy(blobs[text]) end,
}
local realOpen = io.open
io.open = function(path, mode)
  if mode == 'r' then
    if not files[path] then return nil end
    return {read=function() return files[path] end, close=function() return true end}
  end
  return {write=function(_, text) files[path]=fail and 'partial JSON' or text; return not fail end,
    close=function() return true end}
end
local events, messages, accounts, guests, names, vehicles = {}, {}, {}, {}, {}, {}
MP = {
  RegisterEvent=function(name, callback) events[name]=callback end,
  CreateEventTimer=function() end,
  GetPlayerIdentifiers=function(pid) return {beammp=accounts[pid], ip='127.0.0.1'} end,
  IsPlayerGuest=function(pid) return guests[pid]==true end,
  GetPlayerName=function(pid) return names[pid] or 'driver' end,
  GetPlayerVehicles=function(pid) return vehicles[pid] or {} end,
  TriggerClientEvent=function(pid, name, data) messages[#messages+1]={pid=pid,name=name,data=Util.JsonDecode(data)} end,
}
local path=dir..'/../../beammp/Server/BeamNGEarth/main.lua'
local G=dofile(path)
local function event(name, ... ) return _G[events[name]](...) end
local function pose(pid, progress, extra)
  local d={v=2,aLat=40,aLon=-80,lat=40,lon=-80,resume=1,progress=progress}
  for k,v in pairs(extra or {}) do d[k]=v end
  event('bngePose',pid,Util.JsonEncode(d))
end
local function goTo(pid)
  for i=#messages,1,-1 do local m=messages[i]; if m.pid==pid and m.name=='bngeGoTo' then return m.data end end
end
local function location(lat, heading) return {lat=lat,lon=-84.9,height=230,heading=heading or 90} end

T.check(events.onShutdown and events.onPlayerDisconnect, 'flush handlers registered')
accounts[1]='101'
pose(1,location(42))
T.check(G.checkpoints['101'].lat==42 and not goTo(1), 'first-time driver checkpoint uses authenticated identity')
for _=1,4 do event('BNGE_tick') end
T.check(G.saveSequence==0, 'no disk write before interval')
event('BNGE_tick')
T.check(G.saveSequence==1, 'server timer saves at ten seconds')
T.check(dirs.data and files['data/bnge_player_positions1.json'] and not files['bnge_player_positions1.json'],
  'saves create and use the server data directory')
pose(1,location(42.01,135))
event('onPlayerDisconnect',1)
T.check(not G.players[1] and G.saveSequence==2, 'disconnect flushes last received stable pose')

-- Restart the server, rename the player, and allocate a different temporary player ID.
G=dofile(path)
T.check(G.checkpoints['101'].lat==42.01,'checkpoint survives server restart')
accounts[7],names[7]='101','renamed driver'
pose(7,location(10),{joined=true})
local target=goTo(7)
T.check(target and target.resume and target.lat==42.01 and target.heading==135, 'reconnect restores location and heading by account')
T.check(G.checkpoints['101'].lat==42.01,'initial join pose cannot overwrite saved position')
pose(7,location(11),{resumeAck='wrong-token'})
T.check(G.checkpoints['101'].lat==42.01,'unacknowledged loading pose ignored')
local messagesBefore=#messages
for _=1,5 do event('BNGE_tick') end
T.check(#messages>messagesBefore and goTo(7).token==target.token,'uncompleted restore is resent with the same token')
pose(7,location(42.02,180),{resumeAck=target.token})
T.check(G.checkpoints['101'].lat==42.02 and not G.players[7].restorePlace, 'successful restore unlocks subsequent saves')
pose(7,nil)
T.check(G.checkpoints['101'].lat==42.02,'free camera cannot replace last driving pose')

-- A different account can reuse the ID/name, without inheriting the previous player's save.
event('onPlayerDisconnect',7)
accounts[7]='202'
messages={}
pose(7,location(43),{account='101',beammp='101'})
T.check(not goTo(7) and G.checkpoints['202'].lat==43 and G.checkpoints['101'].lat==42.02,
  'reused player ID, same name, and spoofed packet identity cannot select another account')
accounts[8],guests[8]='101',true
pose(8,location(5),{joined=true})
T.check(not (goTo(8) and goTo(8).resume) and G.checkpoints['101'].lat==42.02,'guests do not load or overwrite account saves')
pose(9,location(5))
T.check(not G.players[9].account,'missing stable identity is not saved by name or IP')
local previous=G.checkpoints['202'].lat
pose(7,location(0/0))
T.check(G.checkpoints['202'].lat==previous,'NaN progress ignored')
pose(10,nil,{lat=0/0})
T.check(not G.players[10],'NaN broadcast pose rejected')

event('onShutdown')
local goodSequence=G.saveSequence
pose(7,location(44))
fail=true
event('onShutdown')
T.check(G.saveSequence==goodSequence and G.progressDirty,'failed save retains dirty state for retry')
G=dofile(path)
T.check(G.saveSequence==goodSequence and G.checkpoints['202'].lat==43,'partial write recovers previous complete file')
fail=false
event('onPlayerJoining',7)
pose(7,nil)
target=goTo(7)
pose(7,location(44),{resumeAck=target.token})
event('onShutdown')
G=dofile(path)
T.check(G.checkpoints['202'].lat==44,'saving recovers after disk failure')
messages={}
pose(7,location(44.01))
T.check(not goTo(7) and G.checkpoints['202'].lat==44.01,
  'plugin reload leaves connected drivers in place and continues saving without a restore handshake')
G.SAVE_POSITIONS=false
accounts[11]='202'; messages={}
pose(11,location(50))
T.check(not goTo(11) and G.checkpoints['202'].lat==44.01,'server setting disables both save and restore')

-- Legacy root checkpoints migrate once, without deleting either original backup.
files={}
files['bnge_player_positions1.json']=Util.JsonEncode({version=1,sequence=8,players={['101']=location(41)}})
files['bnge_player_positions2.json']=Util.JsonEncode({version=1,sequence=9,players={['101']=location(42)}})
G=dofile(path)
T.check(G.checkpoints['101'].lat==42 and G.saveSequence==10 and files[G.SAVE_FILES[1]],
  'newest legacy root save migrates immediately into data')
T.check(files['bnge_player_positions1.json'] and files['bnge_player_positions2.json'],'legacy backups preserved')
files['bnge_player_positions2.json']=Util.JsonEncode({version=1,sequence=999,players={['101']=location(15)}})
G=dofile(path)
T.check(G.checkpoints['101'].lat==42 and G.saveSequence==10,'valid data saves take precedence over legacy root files')
files[G.SAVE_FILES[1]],files[G.SAVE_FILES[2]]=nil,nil
directoryFails=true
G=dofile(path)
T.check(G.checkpoints['101'].lat==15 and G.progressDirty and not G.saveSlot,
  'migration retains loaded progress and old files if data directory cannot be created')
directoryFails=false
G.saveProgress()
T.check(not G.progressDirty and files[G.SAVE_FILES[1]],'directory failure can recover without losing the checkpoint')

-- Server-authoritative configuration of the active owned vehicle, not the first car.
local config={parts={pickup_engine='pickup_engine_v8',pickup_bumper_F=''},vars={['$tirepressure_F']=32},
  paints={{baseColor={.4,.1,.7,1},metallic=.3,roughness=.4,clearcoat=.8,clearcoatRoughness=.1}},
  licenseName='EARTH',mainPartName='pickup',partConfigFilename='vehicles/pickup/my_custom.pc',
  linkedPCFile='private.pc',format=4,vehicles={{model='unwanted'}}}
local function car(pid,vid,model,cfg)
  local encoded='{'..Util.JsonEncode({jbm=model,vcf=cfg})
  blobs[encoded]=copy(blobs[encoded:sub(2)])
  vehicles[pid]=vehicles[pid] or {};vehicles[pid][vid]='Os:'..pid..'-'..vid..':'..encoded
end
files={};G=dofile(path);messages={};accounts[21]='505'
car(21,0,'etk800',config);car(21,1,'pickup',config)
local pos=location(45,240);pos.vehicleId='21-1'
pose(21,pos,{vehicleResume=1,vehicle={model='spoofed'}})
local saved=G.checkpoints['505']
T.check(saved.vehicle.model=='pickup' and saved.vehicle.config.parts.pickup_engine=='pickup_engine_v8',
  'saves the selected owned vehicle from BeamMP server data')
T.check(saved.vehicle.config.paints[1].baseColor[3]==.7 and saved.vehicle.config.vars['$tirepressure_F']==32
  and saved.vehicle.config.licenseName=='EARTH','vehicle paints, tuning, and plate retained')
T.check(saved.vehicle.config.format==2 and not saved.vehicle.config.linkedPCFile
  and not saved.vehicle.config.partConfigFilename and not saved.vehicle.config.vehicles,
  'portable configuration excludes local paths and multi-vehicle collections')
config.parts.pickup_engine='later-edit'
T.check(saved.vehicle.config.parts.pickup_engine=='pickup_engine_v8','vehicle checkpoint is an independent snapshot')
event('onPlayerDisconnect',21);G=dofile(path);messages={};accounts[22]='505'
pose(22,nil,{joined=true,vehicleResume=1})
target=goTo(22)
T.check(target.vehicle.model=='pickup' and target.vehicle.config.paints[1].baseColor[3]==.7,
  'vehicle configuration survives disconnect, server restart, and a different player ID')
car(22,0,'pickup',config);pos=location(45,240);pos.vehicleId='22-0'
pose(22,pos,{vehicleResume=1,resumeAck=target.token})
T.check(not G.players[22].restorePlace and G.checkpoints['505'].vehicle.config.parts.pickup_engine=='later-edit',
  'accepted restored vehicle unlocks normal position and configuration updates')
pos=location(46);pos.vehicleId='21-1'
pose(22,pos,{vehicleResume=1})
T.check(G.checkpoints['505'].lat==45,'cannot save another players vehicle by spoofing its server ID')
pos.vehicleId='22-99';pose(22,pos,{vehicleResume=1})
T.check(G.checkpoints['505'].lat==45,'unconfirmed spawn cannot overwrite a good vehicle checkpoint')
car(22,2,'unicycle',config);pos.vehicleId='22-2';pose(22,pos,{vehicleResume=1})
T.check(G.checkpoints['505'].vehicle.model=='pickup','walking avatar does not replace the last driven vehicle')
T.check(not G.vehicleConfig('../private',config) and not G.vehicleConfig('pickup',{parts={},vars={bad=0/0}}),
  'malformed vehicle models and nonfinite tuning are rejected')

-- Two drivers in different states. A guest must resume their own car, never the host's position.
files={};G=dofile(path);messages={}
accounts[0]='707';car(0,0,'pickup',config)
local hostPos=location(40.35);hostPos.lon=-85.56;hostPos.vehicleId='0-0'
pose(0,hostPos,{vehicleResume=1,aLat=hostPos.lat,aLon=hostPos.lon,lat=hostPos.lat,lon=hostPos.lon})
guests[1]=true;accounts[1]='707' -- a guest cannot claim the host's account ID
event('onPlayerJoining',1)
pose(1,nil,{joined=true,guestResume=1,vehicleResume=1})
local token=G.players[1].guestToken
T.check(G.validGuestToken(token) and G.players[1].account=='guest:'..token,'guest gets a random, separate save identity')
T.check(not goTo(1),'guest without a checkpoint is not sent to the host')
local identityMessages=0
for _,m in ipairs(messages) do
  if m.name=='bngeGuestIdentity' then
    T.check(m.pid==1 and m.data.token==token,'guest token sent only to its own client')
    identityMessages=identityMessages+1
  end
  if m.name=='bngeAnchors' then
    for _,p in pairs(m.data) do T.check(not p.guestToken and not p.account,'anchors do not broadcast save identity') end
  end
end
T.check(identityMessages==1,'first guest pose issues one private identity message')
car(1,0,'semi',config)
local away=location(28.07,188);away.lon=-81.98;away.vehicleId='1-0'
pose(1,away,{guestResume=1,vehicleResume=1})
T.check(not G.checkpoints['guest:'..token] and G.players[1].guestToken==token,
  'same token retried; no guest checkpoint before client confirms durable identity')
for _=1,15 do
  pose(0,nil)
  pose(1,away,{guestResume=1,guestToken=token,vehicleResume=1})
  event('BNGE_tick')
end
T.check(G.checkpoints['guest:'..token].lat==28.07 and G.checkpoints['guest:'..token].vehicle.model=='semi',
  'guest driving in another state for thirty seconds saves its own position and vehicle')
event('onPlayerDisconnect',1)
messages={};guests[5]=true;names[5]='different guest name';event('onPlayerJoining',5)
pose(5,nil,{joined=true,guestResume=1,guestToken=token,vehicleResume=1})
target=goTo(5)
T.check(target and target.resume and target.lat==28.07 and target.lon==-81.98 and target.heading==188
  and target.vehicle.model=='semi','guest reconnect with host still online restores distant checkpoint and car')
T.check(G.checkpoints['707'].lat==40.35,'guest does not load or overwrite the authenticated host save')
G=dofile(path);messages={};guests[6]=true;event('onPlayerJoining',6)
pose(6,nil,{joined=true,guestResume=1,guestToken=token,vehicleResume=1})
T.check(goTo(6) and goTo(6).vehicle.model=='semi','guest save survives server restart in the data journal')
guests[7]=true;accounts[7]=nil;messages={};event('onPlayerJoining',7)
pose(7,nil,{joined=true,guestResume=1,guestToken='707',vehicleResume=1})
T.check(G.players[7].guestToken~=token and G.validGuestToken(G.players[7].guestToken) and not goTo(7),
  'another guest on the same IP gets a separate token; numeric account spoof is rejected')
accounts[8]='707';guests[8]=false;messages={};event('onPlayerJoining',8)
pose(8,nil,{joined=true,guestResume=1,guestToken=token,vehicleResume=1})
T.check(goTo(8).lat==40.35 and not G.players[8].guestToken,'authenticated account always wins over client guest token')
G.SAVE_GUESTS=false;guests[9]=true;accounts[9]=nil;messages={};event('onPlayerJoining',9)
pose(9,nil,{joined=true,guestResume=1,guestToken=token})
T.check(not G.players[9].account and not goTo(9),'guest persistence can be disabled independently')

-- Identifiers arriving after the first pose must still select the saved account.
accounts[10]=nil;guests[10]=false;messages={};event('onPlayerJoining',10)
pose(10,nil,{joined=true})
accounts[10]='707'
pose(10,location(36))
T.check(goTo(10) and goTo(10).lat==40.35 and G.checkpoints['707'].lat==40.35,
  'late authenticated identity restores checkpoint before temporary spawn can replace it')
G.SAVE_GUESTS=true
T.check(G.progressNotice({}, {}, true):find('outdated client runtime',1,true),
  'old cached client code is identified instead of silently skipping guest checkpoints')
T.check(G.progressNotice({}, {clientRuntime=3}, true):find('server address',1,true),
  'current client missing identity support is distinguished from a stale runtime')
T.check(G.progressNotice({guestToken=token}, {guestResume=1}, true):find('store its identity',1,true),
  'client identity write acknowledgement can be diagnosed without exposing token')
T.check(G.progressNotice({guestToken=token,saveState='vehicle-config'}, {guestResume=1,guestToken=token}, true)
  :find('vehicle configuration',1,true),'vehicle registration failures are reported separately')
T.check(not G.progressNotice({guestToken=token,saveState='active'}, {guestResume=1,guestToken=token}, true),
  'working guest checkpoints have no blocked-save warning')
FS=realFS
io.open=realOpen
T.done('test_beammp_progress')
