local dir=(arg[0]:match('^(.*)[/\\]') or '.')
local T=dofile(dir..'/testlib.lua')
local now=0
package.loaded['beamngEarth/stopwatch']={clock=function() return function() return now end end}
local queries={cpu=0,gpu=0}
Engine={Debug={getLastPerformanceMetrics=function(out) queries.cpu=queries.cpu+1;out.Render=5 end,
 getGpuTimings=function() queries.gpu=queries.gpu+1;return {ShadowMapPass=7} end}}
VariableRegistry={get=function(key) return ({['$fps::cpuTime']='6',['$fps::gpuTime']='9',['$fps::waitForGPU']='1'})[key] or -1 end}
local P=require('beamngEarth/profiler')
local F=require('beamngEarth/frameprofile')
local IO=require('beamngEarth/ioprofile_ui')
local sent={}
local function send(m) sent[#sent+1]=m end
T.check(P.mode=='essential' and not P.enabled,'Essential is the default, independently of the toggle')
P.serverHello(2);P.setEnabled(true)
P.update(20,{roads=5},true,true,send)
T.check(queries.cpu==0 and queries.gpu==0,'Essential never calls detailed CPU or GPU timing APIs')
T.check(F.history()[1].cpu['CPU work']==6 and F.history()[1].gpu['GPU total']==9,'Essential retains CPU/GPU totals')
T.check(next(F.history()[1].mod)==nil,'Essential disables detailed mod scopes')
T.check(sent[1].mode=='essential' and sent[1].enabled,'requested mode reaches server')
now=25;P.pong({probe=sent[1].probe})
T.check(P.snapshot().latency.count==1 and P.snapshot().latency.avgMs==25,'local application RTT average')
T.check(P.snapshot().latency.p95==nil,'Essential skips latency distributions')
now=500;P.update(20,{},true,true,send)
T.check(#sent==1,'Essential heartbeat is once per second')
now=1000;P.update(20,{},true,true,send)
T.check(#sent==2,'Essential heartbeat renews after one second')
now=7000;P.update(20,{},true,true,send)
T.check(P.snapshot().latency.timeouts==1,'expired application probes are counted separately')
for i=1,200 do now=now+1;P.update(20,{},true,true,send) end
T.check(P.snapshot().frames==180 and #F.history()==180,'Essential history is bounded to 180 frames')
T.check(not P.setMode('invalid') and P.mode=='essential','invalid mode rejected')
P.setMode('full');P.update(20,{roads=5},true,true,send)
T.check(queries.cpu==1 and queries.gpu==1,'Full enables detailed timing APIs')
T.check(F.history()[1].cpu.Render==5 and F.history()[1].gpu.ShadowMapPass==7,'Full restores phase details')
T.check(F.history()[1].mod.roads==5,'Full restores mod stage details')
T.check(F.KEEP==600 and P.snapshot().frames==1,'mode switch resets history and adjusts bounds')
T.check(P.setPage('io') and P.page=='network','legacy I/O page opens Network')
T.check(P.setPage('workers') and P.page=='cpu' and P.setPage('frames') and P.page=='all','legacy page aliases remain navigable')
for _,tab in ipairs({'all','gpu','cpu','network','disk'}) do T.check(P.setPage(tab) and P.page==tab,'resource tab '..tab..' is navigable') end
T.check(not P.setPage('invalid'),'invalid page rejected')
local rows={{endpoint='https://a',responseAvgMs=20,receivedBytes=200,inflight=0,health='idle'},
 {endpoint='https://b',responseAvgMs=90,receivedBytes=10,inflight=1,health='active'},
 {endpoint='https://c',responseAvgMs=40,receivedBytes=100,inflight=0,health='connection error'}}
T.check(IO.rows(rows,'all','latency')[1].endpoint=='https://b','services sort by latency')
T.check(IO.rows(rows,'all','traffic')[1].endpoint=='https://a','services sort by traffic')
T.check(IO.rows(rows,'problems','name')[1].endpoint=='https://c','problem-services filter')
T.check(#IO.rows(rows,'active','name')==1 and #IO.rows(rows,'idle','name')==1,'active and idle filters')
T.check(IO.bytes(nil)=='unavailable','unknown byte counts are not zero')
P.setMode('essential');P.serverHello(1)
now=now+1000;P.update(20,{},true,true,send)
T.check(sent[#sent].enabled==false,'Essential cannot accidentally enable full collection on an old server')
P.setEnabled(false)
T.done('profiler_modes')
