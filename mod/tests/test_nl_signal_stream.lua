local dir=(arg[0]:match('^(.*)[/\\]')or'.')
local T=dofile(dir..'/testlib.lua')
local root='/levels/beamng_earth/art/shapes/beamngEarth/groenpol/'
local live,created,removed,frames={},0,0,0
package.preload['beamngEarth/nl_signal_forest']=function()return {new=function()
  return {create=function(p)local h={shape=p.shape};live[h]=true;created=created+1;return h end,
    remove=function(h)assert(live[h]);live[h]=nil;removed=removed+1 end,
    setFrame=function()frames=frames+1;return true end,forgetLevel=function()end}
end}end
local surfaceReady=false
package.preload['beamngEarth/roads']=function()return {signalSurfacesReady=function()return surfaceReady end}end
local stream=require('beamngEarth/nl_signal_stream')
T.check(created==0,'loading the stream creates no forest or item')
local packets,acks={},{}
local function copy(v)if type(v)~='table'then return v end;local out={};for k,x in pairs(v)do out[k]=copy(x)end;return out end
jsonReadFile=function(path)return copy(packets[path]) end
local now=0;os.clock=function()return now end
local c={name='ctrl',states={{state='phase'}}}
local color='red'
function c:getStateData()return {lights={color=='red'and'red'or'black',color=='amber'and'amber'or'black',
  color=='green'and'green'or'black'}}end
local seq={controllerStates={ctrl={stateIdx=1}}}
local accepted=0
stream.configure(function(p)acks[#acks+1]=p end,function()accepted=accepted+1 end)
stream.graph(7)
stream.register('bne_a',{},c,seq,{'a',0,0,0,0,1,0,4,0,0})
packets['/packet']={format=1,bundle=true,anchor=1,epoch=1,revision=1,graphSerial=7,ground='a.ter',owners={'bne_a'},surfaces={},
  parts={{key='head',role='head',approach='bne_a',lane=0,pose={0,0,0,1,0,0,0,1,0,0,0,1},
    forest={body=root..'body.dae',red=root..'red.dae',amber=root..'amber.dae',green=root..'green.dae'}}}}
assert(stream.onMessage({anchorId=1,epoch=1,revision=1,path='/packet'},1))
stream.update(1,'a.ter',16)
T.check(created==0 and accepted==0,'announced surfaces cannot expose floating assemblies')
surfaceReady=true
stream.update(1,'a.ter',1)
T.check(accepted==0 and not stream.owns('bne_a'),'stock retained while the lens item is still pending')
stream.update(1,'a.ter',1)
T.check(accepted==1 and stream.owns('bne_a')and #acks==1,'complete acceptance owns stock and sends one acknowledgement')
T.check(acks[1].anchorId==1 and acks[1].epoch==1 and acks[1].revision==1,'wire acknowledgement names its exact generation')
local before=created
for i=1,30 do now=now+.01;stream.update(1,'a.ter',16)end
T.check(created==before and frames==1,'unchanged lamps and frame never remake items or reposition the forest')
color='green';now=now+1;stream.update(1,'a.ter',16)
T.check(created==before+1 and removed==1 and #acks==1,'phase update changes only its fixed optical mesh')
T.check(not stream.onMessage({anchorId=1,epoch=1,revision=1,path='/packet'},1),'stale packet cannot replace a live generation')
local p=packets['/packet'];p.revision=2;p.ground='b.ter'
assert(stream.onMessage({anchorId=1,epoch=1,revision=2,path='/packet'},1))
color='red';now=now+1;stream.update(1,'a.ter',16)
T.check(#acks==1,'new ground waits while old controllers continue updating')
stream.update(1,'b.ter',16)
T.check(#acks==2,'committed ground unlocks the queued generation')
stream.clear(false)
T.check(next(live)==nil and not stream.owns('bne_a'),'disable removes only owned items and releases suppression')
T.done('test_nl_signal_stream')
