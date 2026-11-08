local dir=arg[0]:match('^(.*)[/\\]')or'.'
local T=dofile(dir..'/testlib.lua');local S=dofile(dir..'/stubs.lua')
function MatrixF()return {setColumn=function()end,setPosition=function()end,setFromEuler=function()end}end
local create=createObject;createObject=function(class)local o=create(class);o.setTransform=function()end;return o end
local P=require('beamngEarth/nl_signals');local M=require('beamngEarth/nl_signal_mounts')
local surfaces=require('beamngEarth/nl_signal_surfaces');local scene=require('beamngEarth/nl_signal_scene')
local share=require('beamngEarth/nl_signal_shared');local space=require('beamngEarth/nl_signal_space')
local frame={worldToGeodetic=function()return 51.36351,6.12339,0 end}
local function rectangle(x0,y0,x1,y1,z)
  local rows={{x0,y0,z,x1,y0,z,x1,y1,z},{x0,y0,z,x1,y1,z,x0,y1,z}};local grid={}
  for x=math.floor(x0/8),math.floor(x1/8)do for y=math.floor(y0/8),math.floor(y1/8)do grid[x..','..y]=rows end end
  return{grid=grid}
end
local function fixture(n)
  local r=(n+1)*3.3/2
  return {'a',-5,0,10,1,0,0,-5,-r-1,10.15,0,{zone='bibeko'}},
    {nodes={a={-60,0,10,r},j={20,0,10,r}},edges={{'a','j',0,n,1,13,1,r,r}}}
end
local parts={};surfaces.provider=function()return parts,true end
local function road()return {kind='road',mesh=rectangle(-100,-6,100,6,10)}end
local a,g=fixture(2)
parts={road(),{kind='walk',mesh=rectangle(-2.4,-10,.5,-6.5,10.2)}}
local forward=P.plan(a,frame,'stock',true,g,'j')
T.check(not forward.omit and forward.placement.back_m<0,'move towards junction when original right foot has no ground')
T.near(forward.stop[1],a[2],1e-9,'physical move preserves AI stop position')
T.check(forward.placement.stop_offset_m<=3,'forward pole stays within bounded distance of stop')
parts={road(),{kind='walk',mesh=rectangle(-24,-10,-18,-6.5,10.2)}}
local back=P.plan(a,frame,'stock',true,g,'j')
T.check(not back.omit and back.placement.back_m>10,'a safe foot farther before stop is tried instead of dropping the VRI')
T.check(back.placement.stop_offset_m>=-20,'upstream search stays bounded by controller stop')
a={'a',0,0,10,1,0,0,0,-9.25,10.5,0,{zone='bibeko'}}
g={nodes={a={-60,0,10,8.25},j={30,0,10,8.25}},edges={{'a','j',0,4,1,13,1,8.25,8.25}}}
parts={{kind='road',mesh=rectangle(-100,-8.25,100,8.25,10)},
  {kind='walk',mesh=rectangle(-100,-11,100,-8.5,10.2)},
  {kind='walk',mesh=rectangle(1.8,5.3,4.5,7.2,10.4)}}
local aheadLeft=P.plan(a,frame,'stock',true,g,'j')
T.check(not aheadLeft.omit and aheadLeft.layout.head_count==4 and aheadLeft.layout.kind=='cantilever_and_pole',
  'four lanes remain covered when the independent median pole needs a forward shift')
T.check(aheadLeft.placement.alternative_feet.left.position[1]<0,'left support can advance independently towards the stop')
a,g=fixture(3)
a[12].turns={'left','through','right'}
parts={road(),{kind='walk',mesh=rectangle(-100,3.5,100,5.5,10.2)}}
local left=P.plan(a,frame,'stock',true,g,'j')
T.check(not left.omit and left.placement.side=='left'and left.layout.mirrored,'left-only verge creates a mirrored cantilever')
local leftFoot,leftBeam
for _,part in ipairs(left.parts or{})do
  if part.role=='support'then leftFoot=M.worldPose(left,part)end
  if part.role=='beam'then leftBeam=part end
end
T.check(leftFoot and leftFoot.y>3.5 and leftBeam and leftBeam.basis[5]==-1,'mirrored steel stands on the actual left verge')
T.check(left.layout.head_count==3,'left fallback preserves one primary lantern per incoming lane')
local correctLow=false;for _,part in ipairs(left.parts)do if part.key=='head_right_repeat'then correctLow=part.shape:find('arrow_left')~=nil end end
T.check(correctLow,'low repeater on a mirrored left pole follows the leftmost incoming lane')
surfaces.provider=nil
a,g=fixture(2)
local other={'b',-5,2,10,-1,0,1,-5,8,10,0,{zone='bubeko'}}
g.signals={{j='j',kind='signal',app={a,other}}}
local mixed=P.plan(a,frame,'stock',true,g,'j')
T.check(mixed.lane_plan.lens_diameter_mm==300,'one bubeko arm makes the whole junction use 300 mm')
for _,h in ipairs(mixed.lane_plan.heads)do T.check(h.variant:find('300')~=nil,'mixed-junction primary uses a 300 mm model')end
a,g=fixture(1);a[12].road_class='motorway_link'
T.check(P.plan(a,frame,'stock',true,g,'j').lane_plan.lens_diameter_mm==300,'motorway exit overrides explicit bibeko 200 mm')
local aimed=mixed.parts;local beamBasis
for _,p in ipairs(aimed)do if p.role=='beam'then beamBasis=table.concat(p.basis,',')end end
local anyAim=false
for _,part in ipairs(aimed)do if part.role=='head'and part.aim_target then
  local pos=M.worldPose(mixed,part);local dx,dy=part.aim_target[1]-pos.x,part.aim_target[2]-pos.y
  T.check(pos.basis[1]*dx+pos.basis[4]*dy>0,'each aimed head faces its own approaching traffic')
  anyAim=true
end end
T.check(anyAim and beamBasis=='1,0,0,0,1,0,0,0,1','head aiming leaves the steel basis unchanged')
local function physical(name,y,n)
  local hs={};for i=1,n do hs[i]={id='lane_'..i,lane_id=name..i,variant='vehicle_300',distance_m=i*3}end
  local layout=M.plan(hs,{kind=n==1 and'straight'or'cantilever',include_left=false,arm_length_m=n==1 and 0 or n*3})
  return{x=0,y=y,z=10,yaw=0,model='groenpol_vehicle',shape=M.catalog.heads.vehicle_300.shape,
    stop={-1,0,10},dir={-1,0},layout=layout,parts=layout.parts,signature=M.signature(layout),
    lane_plan={synchronized=true,count=n,lens_diameter_mm=300},source_foot={0,y,10},placement={feet={{0,y,10}}}}
end
local function instance(name)
  return{name=name,createSignalObject=function(_,shape)
    local o=createObject('TSStatic');o:setField('shapeName',0,shape);o:registerObject('');return o
  end}
end
local first=physical('first',0,1);local existing=assert(scene.create(first,instance('first')))
local second=physical('second',.8,1)
local angled=physical('angled',.8,1);angled.yaw=45
local angledPair=assert(share.candidates(angled,{first=existing},'angled')[1]);local angledHead
for _,part in ipairs(angledPair.parts)do if part.role=='head'then angledHead=M.worldPose(angledPair,part)end end
local angle=angledPair.mount_pair.relative_angle_degrees
T.check(angle==90 or angle==180 or angle==270,'angled housings use the authored quarter-turn mounting slots')
local angledBoard=false;for _,part in ipairs(angledPair.parts)do if part.role=='shield'then angledBoard=true end end
T.check(not angledBoard,'differently aimed heads use the no-backboard fallback')
local combined=assert(share.candidates(second,{first=existing},'second')[1])
T.check(combined.shared_support and #combined.foreign_changes==1,'nearby low poles share structure without merging controllers')
local board=0;for _,part in ipairs(combined.parts)do if part.role=='shield'then board=board+1 end end
T.check(board==1,'aligned pair receives the double source backboard')
local conflict,roles=space.conflict(combined,{first=existing},'second')
T.check(not conflict,'paired low bodies and shared post pass physical clearance: '..tostring(roles))
local beforeSupports=0;for _,o in ipairs(S.liveOf('TSStatic'))do if o.fields.bneSignalRole=='support'then beforeSupports=beforeSupports+1 end end
local failed=scene.create(combined,{name='second',createSignalObject=function()return nil end},{first=existing})
T.check(not failed and scene.alive(existing),'failed second-head creation does not delete the existing shared post')
local joined=assert(scene.create(combined,instance('second'),{first=existing}))
local countSupports=0;for _,o in ipairs(S.liveOf('TSStatic'))do if o.fields.bneSignalRole=='support'then countSupports=countSupports+1 end end
T.check(countSupports==beforeSupports,'shared post is a single live scene object')
T.check(joined.id~=existing.id,'both native head identities remain independent')
T.check(not space.conflict(existing.saved_plan,{second=joined},'first'),'shared geometry is valid from the original owners direction too')
local source={'a',-1,0,10,-1,0,0,0,0,10,0,{zone='bubeko'}}
local graph={nodes={a={60,-9,10,3.3},j={-20,-9,10,3.3}},edges={{'a','j',0,1,1,13,1,3.3,3.3}}}
local reused=P.place(source,frame,'stock',true,graph,'j',{first=existing,second=joined},'first')
T.check(reused==existing.saved_plan,'ground refresh retains the combined mast instead of shifting it apart again')
existing.visualController={name='red',states={{state='red'}},getStateData=function()return{lights={'red','black','black'}}end}
existing.visualSequence={controllerStates={red={stateIdx=1}}}
joined.visualController={name='green',states={{state='green'}},getStateData=function()return{lights={'black','black','green'}}end}
joined.visualSequence={controllerStates={green={stateIdx=1}}}
scene.syncVisual(existing);scene.syncVisual(joined)
T.check(scenetree.findObjectById(existing.id).fields.instanceColor=='1 1 1 1'and
  scenetree.findObjectById(joined.id).fields.instanceColor2=='1 1 1 1'and
  scenetree.findObjectById(joined.id).fields.instanceColor=='0 0 0 1','shared mast retains independent red and green controllers')
local original=first.shape
scene.delete(joined)
T.check(scene.alive(existing),'removing the second assembly keeps the shared post alive')
T.check(existing.parts[#existing.parts].localPose.shape==original,'single remaining head regains its original backboard')
scene.delete(existing);T.check(#S.liveOf('TSStatic')==0,'last reference releases all shared objects')
first=physical('first',0,3);existing=assert(scene.create(first,instance('first')))
second=physical('second',.8,3)
second.yaw=90;second.dir={0,-1}
combined=assert(share.candidates(second,{first=existing},'second')[1])
combined=share.withoutShields(combined,{first=existing})
T.check(combined.layout.head_count==3,'each side of a double cantilever keeps its three-head limit')
conflict,roles=space.conflict(combined,{first=existing},'second')
T.check(not conflict,'two straight cantilevers can share a post with bare nearby heads: '..tostring(roles))
joined=assert(scene.create(combined,instance('second'),{first=existing}))
local overlap,kind,ownKey,otherKey=space.conflict(existing.saved_plan,{second=joined},'first')
T.check(not overlap,'double-cantilever collision check is symmetric after adoption: '..tostring(kind)..' '..tostring(ownKey)..'/'..tostring(otherKey))
scene.delete(existing);T.check(scenetree.findObjectById(joined.parts[1].id)and scenetree.findObjectById(joined.id),'shared tall post and current native head survive removal of original owner')
scene.delete(joined);T.check(#S.liveOf('TSStatic')==0,'double cantilever releases all references on unload')
T.done('test_nl_signal_v12')
