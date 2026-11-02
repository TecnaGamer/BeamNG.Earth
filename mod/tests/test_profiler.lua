-- Offline LuaJIT coverage of bounded history, setting lifecycle, polling and drawing.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local now = 0
package.loaded['beamngEarth/stopwatch'] = { clock = function() return function() return now end end }
local P = require('beamngEarth/profiler')
P.setMode('full') -- detailed capture lifecycle remains covered alongside the Essential tests
local sent = {}
local function send(msg) sent[#sent + 1] = msg end
local marks = { roads = 3 }
T.check(not P.enabled, 'disabled by default')
P.update(20, marks, true, true, send)
T.check(P.snapshot().frames == 0 and #sent == 0, 'disabled does not collect or poll')
P.serverHello(1)
P.setEnabled(true)
P.update(20, marks, true, true, send)
marks.roads = 100
T.check(P.snapshot().parts[1].totalMs == 3, 'copies previous frame marks')
T.check(#sent == 1 and sent[1].enabled, 'subscribes immediately')
now = 400
P.update(10, {}, true, true, send)
T.check(#sent == 1, 'rate limited polling')
now = 500
P.update(30, {}, true, true, send)
T.check(#sent == 2, 'polls each half second')
local s = P.snapshot()
T.near(s.avgMs, 20, 0.001, 'mean frame wall time')
T.near(s.fps, 50, 0.001, 'FPS from wall time')
T.check(s.p95 == 30 and s.maxMs == 30, 'frame p95 and max')
T.check(s.plot[0] == 20 and s.plot[2] == 30, 'chart chronological order')

local job = { id = 'j', name = 'build', pool = 'roads', worker = '42/MainThread', outcome = 'ok',
  submittedOffsetMs = 0, startOffsetMs = 10, endOffsetMs = 110, runMs = 100, queueMs = 10, cpuMs = 20,
  steps = { { name = 'meshes', totalMs = 80, cpuMs = 15, count = 2, maxMs = 50 } } }
P.receive({ enabled = true, elapsedMs = 500, active = {}, recent = { job } })
s = P.snapshot()
T.check(s.pools[1].count == 1 and s.pools[1].p95 == 100, 'worker statistics')
T.check(s.stages[1].name == 'roads / meshes' and s.stages[1].count == 2, 'per-pool step aggregates')
P.receive({ enabled = true, elapsedMs = 10000, active = {}, recent = { job } })
T.check(#P.snapshot().stages == 0 and #P.snapshot().pools == 0, 'inactive workloads disappear after five seconds')
P.receive({ enabled = true, elapsedMs = 500, active = {}, recent = { job } })

-- Exercise the real tab renderer, including frozen exports and responsive children.
local im, surface = dofile(dir..'/profiler_imgui_stub.lua')(T)
ui_imgui=im
local V=require('beamngEarth/profiler_view')
local saved
jsonWriteFile=function(path,data)
  if path=='/settings/beamngEarth-profiler.json' then saved={path=path,data=data} end
  return true
end
P.draw(true)
T.check(P.page=='all' and surface.sizes['##history-cpu'] and surface.sizes['##history-gpu'] and surface.sizes['##history-network'] and surface.sizes['##history-disk'],'All renders all four resource lanes')
surface.clicked='Save snapshot';P.draw(true)
T.check(saved and saved.data.frames==3,'snapshot exports real capture')
surface.clicked='Freeze display';P.draw(true)
now=1000;P.update(1000,{},true,true,send)
P.receive({enabled=true,active={},recent={}})
surface.clicked='Save snapshot';P.draw(true)
T.check(saved.data.frames==3 and #saved.data.server.recent==1,'frozen export retains inspected frame and worker history')
T.check(P.snapshot().frames==4,'freezing the view keeps collecting')
local narrow=surface.sizes['##history-cpu'].x
local shortHeight=surface.sizes['##history-cpu'].y
surface.width,surface.height=1500,1000;P.draw(true)
T.check(surface.sizes['##history-cpu'].x>narrow and surface.sizes['##history-cpu'].x<1500,'wide view resizes charts and reserves an inspector')
T.check(surface.sizes['##history-cpu'].y>shortHeight,'chart lanes grow with available height before manual splitting')
for _,tab in ipairs({'gpu','cpu','network','disk','all'}) do
  P.setPage(tab);surface.sizes={};P.draw(true)
  T.check(P.page==tab,'tab selected: '..tab)
  if tab=='gpu' then T.check(surface.sizes['##history-gpu'] and not surface.sizes['##history-network'],'GPU excludes unrelated resource charts') end
  if tab=='disk' then T.check(surface.sizes['##history-disk'] and not surface.sizes['##history-network'],'Disk is separate from Network') end
end
surface.clicked='Inspect worst';P.draw(true)
T.check(V.selection() and V.selection().ms==30,'inspect worst selects displayed capture, not background frames')
P.setPage('gpu');P.draw(true)
surface.clicked='Save snapshot';P.draw(true)
T.check(saved.data.presentation.selectedFrame.ms==30 and saved.data.presentation.tab=='gpu','selection follows tab changes and export')
T.check(saved.data.plot==nil and saved.data.frameDetail._layouts==nil,'export omits geometry caches')
surface.clicked='Follow live';P.draw(true)
surface.clicked='Save snapshot';P.draw(true)
T.check(saved.data.frames==4 and V.selection()==nil,'Follow live resumes display and clears selection')
surface.hover='##history-gpu';surface.pick=true;P.draw(true);surface.pick=false;surface.hover=nil
T.check(V.selection()~=nil,'clicking a real chart selects a frame')
surface.clicked='Resume display';P.draw(true)
T.check(V.selection()==nil,'resume clears held selection')
now=1050;P.update(15,{},true,true,send);surface.clicked='Save snapshot';P.draw(true)
T.check(saved.data.frames==4,'live view respects refresh cadence')
now=1100;surface.clicked='Save snapshot';P.draw(true)
T.check(saved.data.frames==5,'live refresh does not require a server packet')
local settingsWrites={}
package.loaded['beamngEarth/settings']={SCHEMA={runtimeProfiler=true,profilerMode=true},set=function(key,value)
  settingsWrites[#settingsWrites+1]={key,value}
  if key=='runtimeProfiler' then P.setEnabled(value) else P.setMode(value) end
end}
surface.close=true;P.draw(true)
T.check(P.enabled and P.compact and #settingsWrites==0,'close keeps continuous capture in status overlay')
local before=P.snapshot().capturedFrames
now=1600;P.update(12,{},true,true,send);P.draw(true)
T.check(P.snapshot().capturedFrames==before+1 and sent[#sent].enabled,'compact overlay keeps subscription alive')
surface.clicked='Open profiler';P.draw(true)
T.check(not P.compact and P.enabled,'reopen preserves capture')
surface.collapsed=true;now=2100;P.update(11,{},true,true,send);P.draw(true);surface.collapsed=false
T.check(P.snapshot().capturedFrames==before+2,'collapsed window keeps collecting')
surface.clicked='Essential';P.draw(true)
T.check(P.mode=='essential' and P.snapshot().frames==0,'mode switch starts fresh capture')
surface.clicked='Full';P.draw(true)
T.check(P.mode=='full','Full mode restored')
T.check(surface.begin==surface.finish and surface.children==0 and surface.tables==0 and surface.styles==0,'balanced ImGui windows, children, tables and styles')

ui_imgui = nil
for i = 1, 1000 do now = now + 1; P.update(i, {}, true, true, send) end
s = P.snapshot()
T.check(s.frames == 600 and s.maxMs == 1000, 'frame ring bounded')
T.check(s.plot[0] == 401 and s.plot[599] == 1000, 'wrapped chart chronological order')
P.setFrozen(true)
sent={}
for i=1,21600 do now=now+1000;P.update(16,{},true,true,send);P.pong({probe=sent[#sent].probe}) end
s=P.snapshot()
T.check(P.enabled and s.capturedFrames==22600 and s.frames==600 and #s.frameDetail.history==600, 'six hours of continuous collection has bounded frame history')
T.check(#sent==21600 and sent[#sent].enabled and s.maxMs==16, 'frozen collection keeps renewing and replacing old samples')
T.check(s.elapsedMs>=21600000, 'capture reports elapsed time beyond history capacity')
P.update(math.huge,{},true,true,send)
T.check(P.snapshot().capturedFrames==22600, 'invalid frame interval cannot poison live feedback')
P.update(10, {}, true, false, send)
T.check(P.snapshot().server == nil, 'disconnect clears stale workers')
P.serverHello(nil)
local sentBefore = #sent
now = now + 1000
P.update(10, {}, true, true, send)
T.check(#sent == sentBefore, 'old server receives no profiler request')
P.serverHello(1)
T.check(P.snapshot().frames == 0, 'reconnect resets history')
ui_imgui = { BoolPtr=function(v) return {[0]=v} end, ImVec2=function(x,y) return {x=x,y=y} end,
  SetNextWindowSize=function() end, Begin=function() return true end, End=function() end,
  PushTextWrapPos=function() end, PopTextWrapPos=function() end, TextUnformatted=function() end,
  SameLine=function() end, Button=function(label) return label=='Stop profiling' end }
P.setCompact(true);P.draw(true)
T.check(sent[#sent].enabled == false, 'disabling unsubscribes')
T.check(P.snapshot().frames == 0, 'disabling clears samples')
T.check(not P.enabled and settingsWrites[#settingsWrites][1]=='runtimeProfiler' and settingsWrites[#settingsWrites][2]==false, 'Stop profiling persists the disabled setting')
T.done('profiler')
