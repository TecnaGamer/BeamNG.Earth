local dir=arg[0]:match('^(.*)[/\\]')or'.'
local T=dofile(dir..'/testlib.lua');local S=dofile(dir..'/stubs.lua')
function MatrixF()return{setColumn=function()end,setPosition=function()end,setFromEuler=function()end}end
local create=createObject
createObject=function(class)local o=create(class);o.setTransform=function(self)self.moves=(self.moves or 0)+1 end;return o end
local M=require('beamngEarth/nl_signal_mounts');local scene=require('beamngEarth/nl_signal_scene')
local shared=require('beamngEarth/nl_signal_shared');local space=require('beamngEarth/nl_signal_space')
local function physical(y,n,variant,kind)
  local heads={};for i=1,n do heads[i]={id='lane_'..i,lane_id='lane_'..i,variant=variant,distance_m=i*3}end
  local layout=M.plan(heads,{kind=kind or(n==1 and'straight'or'cantilever'),include_left=kind=='portal',
    arm_length_m=n==1 and 0 or n*3,left_span_m=kind=='portal'and 18 or nil})
  assert(layout.ready)
  return{x=0,y=y,z=10,yaw=0,model='groenpol_vehicle',stop={-1,0,10},dir={-1,0},
    layout=layout,parts=layout.parts,signature=M.signature(layout),lane_plan={synchronized=true,count=n},placement={feet={{0,y,10}}}}
end
local function inst(name)return{name=name,createSignalObject=function(_,shape)
  local o=createObject('TSStatic');o:setField('shapeName',0,shape);o:registerObject('');return o
end}end
local function part(owner,key)for _,entry in ipairs(owner.parts)do local p=entry.localPose or entry;if p.key==key then return p,entry end end end
local function firstFit(plan,others,name)
  for _,p in ipairs(shared.candidates(plan,others,name))do
    if not space.conflict(p,others,name)then return p end
    local bare=shared.withoutShields(p,others)
    if not space.conflict(bare,others,name)then return bare end
  end
end
for _,variant in ipairs({'vehicle','vehicle_300'})do
  local first=physical(0,1,variant);local one=assert(scene.create(first,inst('first')))
  local second=physical(.8,1,variant);local p=assert(firstFit(second,{first=one},'second'))
  T.check(part(p,'shared_double_shield')~=nil,variant..' co-facing pair uses one double backboard')
  local two=assert(scene.create(p,inst('second'),{first=one}))
  T.check(select(2,part(one,'support_right')).id==select(2,part(two,'support_right')).id,'both lanterns share exactly one pole')
  local pos1=M.worldPose(one,part(one,'head_lane_1'));local pos2=M.worldPose(two,part(two,'head_lane_1'))
  T.near(pos1.x,pos2.x,.01,'paired heads are side by side instead of behind one another')
  T.near(pos1.z,pos2.z,1e-8,'paired heads have equal mounting height')
  T.check(math.abs(pos1.y-pos2.y)>.4,'head housings do not overlap')
  T.check(not space.conflict(two.saved_plan,{first=one},'second'),'double board clears its neighbouring assembly')
  local oldId=one.id
  one.visualController={name='red',states={{state='red'}},getStateData=function()return{lights={'red','black','black'}}end}
  one.visualSequence={controllerStates={red={stateIdx=1}}}
  two.visualController={name='green',states={{state='green'}},getStateData=function()return{lights={'black','black','green'}}end}
  two.visualSequence={controllerStates={green={stateIdx=1}}}
  scene.syncVisual(one);scene.syncVisual(two)
  T.check(S.objects[one.id].fields.instanceColor=='1 1 1 1'and S.objects[two.id].fields.instanceColor=='0 0 0 1','red and green controllers remain independent on one pole')
  T.check(one.id==oldId and one.id~=two.id,'combination preserves both native signal objects')
  scene.delete(two);scene.delete(one)
end
-- A separate approach can use a low socket on an existing three-head boom.
-- Original feet must move with the head, and overhead lane heads stay untouched.
local original=physical(0,3,'vehicle_300');local one=assert(scene.create(original,inst('boom')))
local oldFoot=part(one,'low_bracket_right');local oldBasis={unpack(oldFoot.basis)}
local oldLow=select(2,part(one,'head_right_repeat'));local oldMoves=S.objects[oldLow.id].moves
local overhead={};for i=1,3 do overhead[i]=M.worldPose(one,part(one,'head_lane_'..i))end
local second=physical(.8,1,'vehicle_300');local p=assert(firstFit(second,{boom=one},'second'),'300mm pair must fit an existing boom with bare housing fallback')
T.check(p.mount_pair.relative_angle_degrees==180 and p.mount_pair.kind=='authored_double_bracket','co-facing boom pair uses original opposed double foot sockets')
T.check(#p.foreign_changes==2,'both the first housing and its original foot are updated together')
local two=assert(scene.create(p,inst('second'),{boom=one}))
T.check(S.objects[oldLow.id].moves>oldMoves,'foreign head transform is updated immediately')
T.check(select(2,part(two,'support_right')).id==select(2,part(one,'support_right')).id,'standing lamp reuses the boom mast')
for i=1,3 do local q=M.worldPose(one,part(one,'head_lane_'..i))
  T.near(q.x,overhead[i].x,1e-9,'overhead lane X stays fixed');T.near(q.y,overhead[i].y,1e-9,'overhead lane Y stays fixed')
end
local h1=M.worldPose(one,part(one,'head_right_repeat'));local h2=M.worldPose(two,part(two,'head_lane_1'))
T.check(math.abs(h1.y-h2.y)>.4,'boom low heads sit side by side')
T.check(not space.conflict(p,{boom=one},'second'),'bare boom pair does not intersect other housings or steel')
local foot=M.worldPose(two,part(two,'shared_mount_current'));local dual=M.catalog.double_mount;local socket
for _,s in ipairs(dual.sockets)do
  local x,y=foot.x+foot.basis[1]*s[1]+foot.basis[2]*s[2],foot.y+foot.basis[4]*s[1]+foot.basis[5]*s[2]
  if(x-h1.x)^2+(y-h1.y)^2<1e-10 then socket=s end
end
T.check(socket~=nil,'original head sits exactly on an authored Cylinder.053 socket')
T.near(h1.z,10+M.catalog.low_repeat.position[3],1e-8,'compound double foot preserves normal low lantern height')
T.check(part(two,'shared_double_shield')~=nil,'co-facing boom pair can retain a common double backboard')
local stem=M.worldPose(one,part(one,'low_bracket_right'));local s=M.catalog.low_repeat.position
T.near(stem.x+stem.basis[1]*s[1]+stem.basis[2]*s[2],foot.x+foot.basis[1]*dual.bar_centre[1]+foot.basis[2]*dual.bar_centre[2],1e-7,'original stand-off joins original double bar X')
T.near(stem.z+s[3],foot.z+dual.bar_centre[3],1e-7,'original stand-off upright joins original double bar Z')
scene.delete(two)
for k=1,9 do T.near(part(one,'low_bracket_right').basis[k],oldBasis[k],1e-9,'unloading partner restores the original foot')end
T.check(one.id~=nil and scene.alive(one),'removing paired lamp leaves original boom and controller alive')
scene.delete(one)
-- Either owner may disappear first. All mechanical parts needed by the other
-- remain referenced, and the returning approach reuses that exact assembly.
one=assert(scene.create(physical(0,3,'vehicle_300'),inst('boom')))
p=assert(firstFit(physical(.8,1,'vehicle_300'),{boom=one},'second'))
two=assert(scene.create(p,inst('second'),{boom=one}))
local stemId=select(2,part(two,'shared_stand_off')).id
local doubleId=select(2,part(two,'shared_mount_current')).id;local headId=two.id
scene.delete(one);scene.reconcile(two)
T.check(S.objects[stemId]and S.objects[doubleId]and scene.alive(two),'both original mounting feet survive removal of the first controller')
local returning=assert(firstFit(physical(0,3,'vehicle_300'),{second=two},'boom'))
one=assert(scene.create(returning,inst('boom'),{second=two}))
T.check(two.id==headId and select(2,part(one,'shared_mount_current')).id==doubleId,'returning approach reuses the double foot without shifting existing lantern')
scene.delete(two);scene.reconcile(one)
T.check(scene.alive(one)and S.objects[stemId]and S.objects[doubleId],'second unload order also retains complete mechanical support')
scene.delete(one)
-- A standing lamp may also share a structural portal leg, without moving steel.
local portal=physical(0,4,'vehicle_300','portal');one=assert(scene.create(portal,inst('portal')))
local onLeg=physical(.8,1,'vehicle_300');local pair=firstFit(onLeg,{portal=one},'second')
T.check(pair and pair.shared_support,'low standing head may share a portal leg too')
if pair then two=assert(scene.create(pair,inst('second'),{portal=one}));scene.delete(two)end
scene.delete(one)
T.check(#S.liveOf('TSStatic')==0,'paired head, shield and hardware teardown leaves no orphan objects')
T.done('test_nl_signal_pair')
