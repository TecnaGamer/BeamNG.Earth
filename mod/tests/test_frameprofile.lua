local dir=(arg[0]:match('^(.*)[/\\]') or '.')
local T=dofile(dir..'/testlib.lua')
local F=require('beamngEarth/frameprofile')
local UI=require('beamngEarth/frameprofile_ui')
local raw={Render=7,Lua=2,physUsed=123456789,virtAvailable=987654321,FPSLimiter=20}
Engine={ Debug={
  getLastPerformanceMetrics=function(out) for k,v in pairs(raw) do out[k]=v end end,
  getGpuTimings=function() return {ShadowMapPass=4,PostFX=2,zero=0,bad=-1,invalid=0/0} end,
}, getCurrentFpsLimitReason=function() return {windowFocused=false} end }
VariableRegistry={get=function(key) return ({['$fps::cpuTime']='9.5',['$fps::gpuTime']='8.0',['$fps::waitForGPU']='1.5'})[key] or -1 end}
local data=F.capture()
T.check(data.cpu.Render==7 and data.cpu.Lua==2,'engine CPU milliseconds')
T.check(data.cpu.physUsed==nil and data.cpu.virtAvailable==nil,'memory counters are not CPU durations')
T.check(data.gpu.ShadowMapPass==4 and data.gpu.PostFX==2,'real GPU map format accepted')
T.check(data.gpu.zero==nil and data.gpu.bad==nil and data.gpu.invalid==nil,'inactive and invalid GPU values hidden')
T.check(data.cpuMs==9.5 and data.gpuMs==8 and data.waitMs==1.5,'registry string values converted to ms')
T.check(data.focused==false and data.gpuTiming:find('association unavailable'),'focus and deferred query semantics captured')
F.add(40,{roads=3,buildings=0},data)
data.cpu.Render=999
local f=F.history()[1]
T.check(f.cpu.Render==7 and f.mod.roads==3 and f.mod.buildings==nil,'snapshot copies values and hides inactive scopes')
T.near(f.cpu.Unattributed,11,0.001,'CPU unmeasured remainder')
T.near(f.gpu['Unattributed GPU'],2,0.001,'GPU unmeasured remainder')
T.check(f.cpuSumMs==29 and f.gpuSumMs==6,'raw CPU/GPU phase sums retained separately')
T.check(f.cpuMs==9.5 and f.ms==40,'CPU work is separate from interval including waits')

Engine.Debug.getLastPerformanceMetrics=function() end
Engine.Debug.getGpuTimings=function() error('unsupported backend') end
VariableRegistry.get=function() return -1 end
data=F.capture()
T.check(next(data.cpu)==nil and next(data.gpu)==nil,'API failure clears previous metric values')
T.check(data.gpuMs==nil and data.waitMs==nil,'unsupported counters remain missing, not zero')
Engine=nil;VariableRegistry=nil
T.check(next(F.capture().cpu)==nil,'works without engine globals')

F.add(10,{}, {cpu={Render=30},gpu={ShadowMapPass=15},gpuMs=5})
f=F.history()[2]
T.check(f.phaseOverrun and f.gpuOverrun,'overlapping/delayed totals flagged')
T.check(f.cpu.Render==30 and f.gpu.ShadowMapPass==15,'overrun durations are never scaled to fit')
local summary=F.analyze()
T.check(summary.overBudget==1 and summary.worst.id==1,'slow and worst frames identified')
T.check(summary.series.cpu[1].totalMs>=summary.series.cpu[#summary.series.cpu].totalMs,'ranked phase costs')
local overrun=false
for _,o in ipairs(summary.observations) do if o.kind=='overlap' then overrun=true end end
T.check(overrun,'data quality warning is evidence based')

local layout=UI.layout(F.history(),'cpu',200,100,40,180)
T.check(#layout.columns==2,'one column per captured frame')
T.check(UI.hit(layout,0).id==1 and UI.hit(layout,199).id==2,'frame hit testing chooses exact frame')
T.check(UI.hit(layout,-1)==nil and UI.hit(layout,200)==nil,'outside graph cannot select a frame')
local renderBar
for _,b in ipairs(layout.bars) do if b.frameId==1 and b.name=='Render' then renderBar=b end end
T.near(renderBar.height,17.5,0.001,'bar height reflects measured milliseconds')
layout=UI.layout(F.history(),'cpu',200,100,10,1)
T.check(#layout.columns==1 and layout.columns[1].frame.id==2 and layout.columns[1].clipped,'zoom retains spike clipping indicator')

F.reset()
for i=1,650 do F.add(i,{}, {cpu={},gpu={}}) end
local history=F.history()
T.check(#history==600 and history[1].id==51 and history[600].id==650,'bounded history in chronological order')
T.check(F.analyze().worst.id==650,'worst selection survives ring wrap')
F.reset()
for i=1,20 do F.add(33.3,{}, {cpu={FPSLimiter=29,Render=1},cpuMs=2,gpuMs=3,gpu={PostFX=2}}) end
summary=F.analyze()
local paced,pressure=false,false
for _,o in ipairs(summary.observations) do if o.kind=='pacing' then paced=true end; if o.text:find('candidate') then pressure=true end end
T.check(paced and not pressure,'limiter delay is not misdiagnosed as GPU pressure')
F.reset()
for i=1,20 do F.add(60,{roads=4}, {cpu={Render=3},cpuMs=5,gpuMs=50,gpu={ShadowMapPass=40,PostFX=10}}) end
summary=F.analyze()
pressure=false
for _,o in ipairs(summary.observations) do if o.kind=='gpu' and o.text:find('candidate') then pressure=true end end
T.check(pressure,'sustained GPU pressure observation requires meaningful GPU cost')
T.check(summary.series.gpu[1].name=='ShadowMapPass','largest GPU pass ranks first')
-- Frozen snapshots retain at most one geometry per lane through repeated resizes.
local graphSize
local im={TreeNodeFlags_DefaultOpen=1,
  TextUnformatted=function() end,PushTextWrapPos=function() end,PopTextWrapPos=function() end,
  Button=function() return false end,SameLine=function() end,CollapsingHeader1=function() return true end,
  ImVec2=function(x,y) return {x=x,y=y} end,ImVec4=function() return {} end,
  GetColorU322=function() return 1 end,GetCursorScreenPos=function() return {x=0,y=0} end,
  GetWindowDrawList=function() return {} end,ImDrawList_AddRectFilled=function() end,ImDrawList_AddText1=function() end,
  InvisibleButton=function(id,size) if id=='##earth-frame-stack-cpu' then graphSize=size end end}
for i=1,100 do UI.draw(im,summary,500+i,90+i) end
local cached=0;for _ in pairs(summary._layouts) do cached=cached+1 end
T.check(cached==3 and graphSize.x==600 and graphSize.y==190,'resizing all lanes keeps geometry bounded and follows both dimensions')
local resized=summary._layouts.cpu.layout
T.check(UI.hit(resized,599).id==summary.history[#summary.history].id,'resized graph hit testing still reaches latest frame')
F.reset()
T.check(F.analyze().count==0 and #F.analyze().observations==0,'empty capture has no invented observations')
T.done('frameprofile')
