local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local geodesy = require('beamngEarth/geodesy')
local resume = require('beamngEarth/resume')
local settings = require('beamngEarth/settings')
local net = require('beamngEarth/net')
local roads = require('beamngEarth/roads')
local files, sent = {}, {}
function jsonReadFile(p) return files[p] end
function jsonWriteFile(p,t) local c={};for k,v in pairs(t) do c[k]=v end;files[p]=c;return true end
net.isConnected = function() return true end
net.send = function(m) sent[#sent+1]=m end
net.update = function() return {} end
local veh = {pos=vec3(100,200,150),forward=vec3(1,0,0)}
function veh:getID() return 42 end
function veh:getPosition() return self.pos end
function veh:getVelocity() return vec3() end
function veh:getDirectionVector() return self.forward end
-- Physics applies a cluster request on the next simulation step. A positive world-Z
-- quaternion rotates BeamNG's forward vector clockwise (the engine mathlib convention).
local pending, rotations = nil, 0
function veh:setClusterPosRelRot(_,x,y,z,_,_,qz,qw)
  local yaw=2*math.atan2(qz or 0,qw or 1)
  if math.abs(yaw)>1e-8 then rotations=rotations+1 end
  pending={pos=vec3(x,y,z),bearing=math.atan2(self.forward.x,self.forward.y)+yaw}
end
function veh:applyClusterVelocityScaleAdd() end
function getPlayerVehicle() return veh end
function getAllVehicles() return {veh} end
local placed, teleportError = nil, math.pi
spawn = {safeTeleport=function(v,p,q)
  placed=vec3(p)
  -- Reproduce a reset/cluster placement that completes with a stale orientation.
  pending={pos=vec3(p),bearing=q.bearing+teleportError}
end}
local heading
quatFromDir=function(v) heading=v;return {bearing=math.atan2(v.x,v.y)} end
local core = require('beamngEarth/core')
local function step(paused)
  if pending and not paused then
    veh.pos=pending.pos;veh.forward=vec3(math.sin(pending.bearing),math.cos(pending.bearing),0);pending=nil
  end
  core.onUpdate(.1,paused and 0 or .1)
end
local function live(anchor)
  local st=core._state();st.inOurLevel=true;st.active=true;st.anchor=anchor;st.anchorId=1;st.clock=10
  st.resumeCaptureAt=nil
  return st
end
local origin=geodesy.newAnchor(39,-86)
local lat,lon,h=origin:enuToGeodetic(100,200,150)
live(origin)
core.onClientEndMission()
T.near(files[resume.FILE].lat,lat,1e-10,'exit saves the current vehicle latitude')
T.near(files[resume.FILE].lon,lon,1e-10,'exit saves the current vehicle longitude')
T.near(files[resume.FILE].h,h,1e-7,'exit saves bridge/elevation height')
core.onClientStartMission()
local request=sent[#sent]
T.check(request.type=='prepare_anchor' and request.lat==lat and request.lon==lon,'next freeroam load requests the saved location')
local target=core._state().resumeTarget
T.check(target and target.heading~=nil,'saved heading accompanies the requested spawn')
-- The initial flat-terrain placement runs while the server prepares the saved location.
-- Previously it consumed resumeTarget, so the real anchor arrived without a saved heading.
for _=1,15 do step() end
T.check(core._state().resumeTarget==target,'temporary flat spawn retains the pending saved drive')
T.check(not core._state().anchor,'server preparation can outlast the temporary spawn')
local terrain=require('beamngEarth/terrain')
terrain.load=function() return true end
net.update=function() return {{type='anchor_ready',requestId=request.requestId,anchorId=1,
  lat=lat,lon=lon,groundZ=h-2,terrain={cx=0,cy=0,size=terrain.SIZE,
    heightmap='/saved-drive.png',squareSize=2,maxHeight=1024,zBase=0}}} end
step(true)
net.update=function() return {} end
local st=core._state()
T.check(st.anchor and st.placeHeading==target.heading,'real anchor receives the saved heading after a delayed load')
T.near(st.placeZ,h+1,1e-7,'real anchor retains the saved elevation after a delayed load')
st.placeFrames=2
placed=nil
require('beamngEarth/ui').visible=false
local ready=false
roads.readyAround=function(a,x,y,r) T.check(r==12,'collision readiness includes the vehicle surroundings');return ready end
step()
T.check(not placed and st.placeFrames==2,'saved bridge spawn waits for nearby collision')
T.check(files[resume.FILE].lat==lat,'loading cannot overwrite the saved drive')
ready=true
step();step()
T.check(placed and math.abs(placed.z-h-1)<1e-8,'spawn uses the saved elevation after collision is ready')
T.check(heading and math.abs(math.atan2(heading.x,heading.y)-target.heading)<1e-8,'spawn restores heading')
T.check(st.headingRestore~=nil,'saved facing is checked after teleport reaches physics')
local savedWhileLoading=files[resume.FILE]
core.onClientPreStartMission()
T.check(files[resume.FILE]==savedWhileLoading,'unverified placement cannot overwrite the saved heading on exit')
for _=1,7 do step() end
T.check(not st.headingRestore,'restoration finishes after the actual heading is verified')
T.near(math.atan2(veh.forward.x,veh.forward.y),target.heading,1e-8,'a reversed spawn is corrected using the settled forward vector')
for _,bearing in ipairs({0,.35,math.pi/2,math.pi,3*math.pi/2,2*math.pi-.01}) do
  for _,err in ipairs({0,math.pi,.73,-1.45}) do
    st=live(geodesy.newAnchor(lat,lon));st.placeFrames=1;st.placeZ=h+1;st.placeHeading=bearing
    teleportError=err;pending=nil;st.headingRestore=nil
    local before=rotations
    step()
    local wait=st.headingRestore.wait
    step(true);step(true)
    T.check(st.headingRestore.wait==wait,'paused frames do not consume the physics wait')
    for _=1,7 do step() end
    local actual=math.atan2(veh.forward.x,veh.forward.y)
    T.near(math.atan2(math.sin(actual-bearing),math.cos(actual-bearing)),0,1e-8,'arbitrary saved bearings survive stale and reversed placement')
    T.check(rotations-before==(err==0 and 0 or 1),'correct spawns are untouched; stale spawns get one relative yaw correction')
    T.check(not st.headingRestore,'physics readback confirms the final facing')
  end
end
-- Store a direction in a different anchor frame and verify that the saved local bearing is stable.
local nextAnchor=geodesy.newAnchor(39.04,-85.96)
local oldHeading=files[resume.FILE].heading
local x,y,z=nextAnchor:geodeticToEnu(lat,lon,h)
veh.pos=vec3(x,y,z)
local dx,dy,dz=geodesy.newAnchor(lat,lon):transformTo(nextAnchor):rotate(math.sin(oldHeading),math.cos(oldHeading),0)
veh.forward=vec3(dx,dy,dz)
live(nextAnchor)
core.onClientEndMission()
T.near(files[resume.FILE].heading,oldHeading,1e-9,'rebase does not change the saved local heading')
settings.set('resumePosition',false)
local saved=files[resume.FILE]
live(origin);veh.pos=vec3(500,500,20)
core.onClientEndMission()
T.check(files[resume.FILE]==saved,'off disables position saving')
core.onClientStartMission()
request=sent[#sent]
T.check(request.lat==36.596718 and request.lon==-118.169991,'off returns to the normal starting location')
-- Explicit spawn selections from the newer level menu take priority over autosave.
settings.set('resumePosition',true)
local levelinfo=require('beamngEarth/levelinfo')
local places=require('beamngEarth/places')
local oldChosen,oldFind=levelinfo.chosen,places.find
levelinfo.chosen=function() return 'bne_place_123' end
places.find=function(id) if tostring(id)=='123' then return {lat=42,lon=-84,name='Chosen place'} end end
core.onClientEndMission();core.onClientStartMission()
request=sent[#sent]
T.check(request.type=='prepare_anchor' and request.lat==42 and request.lon==-84,'explicit saved-place selection overrides autosave')
T.check(core._state().resumeTarget==nil,'explicit place does not inherit the autosaved height or heading')
levelinfo.chosen=function() return levelinfo.RANDOM end
core.onClientEndMission();core.onClientStartMission()
request=sent[#sent]
T.check(request.type=='random_place','the upstream Random spawn option still requests a random place')
levelinfo.chosen,places.find=oldChosen,oldFind
T.done('test_resume_core')
