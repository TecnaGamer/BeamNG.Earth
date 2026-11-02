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

-- Exercise every expanded panel with the same ImGui API names used by existing mod windows.
local calls, button, chart, texts, sizes = {}, nil, nil, {}, {}
local available = {x=680, y=640}
local collapsed, closeWindow = false, false
local function note(name) calls[name] = (calls[name] or 0) + 1 end
ui_imgui = {
  TreeNodeFlags_DefaultOpen = 1, Cond_FirstUseEver = 1,
  BoolPtr = function(v) return { [0] = v } end,
  ImVec2 = function(x, y) return { x = x, y = y } end,
  ImVec4 = function() return {} end,
  GetColorU322 = function() return 1 end,
  GetCursorScreenPos = function() return { x = 0, y = 0 } end,
  GetContentRegionAvail = function() return available end,
  GetWindowSize = function() return {x=available.x+30,y=available.y+40} end,
  GetWindowDrawList = function() return {} end,
  InvisibleButton = function(id, size) sizes[id] = size end,
  ImDrawList_AddRectFilled = function() note('rect') end,
  ImDrawList_AddText1 = function() note('label') end,
  TextUnformatted = function(value) texts[#texts+1] = value end,
  PushTextWrapPos = function() end, PopTextWrapPos = function() end,
  SameLine = function() end,
  SetNextWindowSize = function(_, cond) T.check(cond==ui_imgui.Cond_FirstUseEver, 'window size never overrides user resizing') end,
  SetNextWindowSizeConstraints = function(minimum) T.check(minimum.x>=500, 'readable minimum window width') end,
  Begin = function(_, open)
    note('begin')
    if closeWindow and open then open[0]=false;closeWindow=false end
    return not collapsed
  end,
  End = function() note('end') end,
  CollapsingHeader1 = function() return true end,
  Button = function(label) if label == button then button = nil; return true end return false end,
  PlotLines1 = function(id, array, n, _, _, _, _, size)
    sizes[id] = size
    if id=='##earth-profiler-frames' then chart={n=n,first=array[0],last=array[n-1]} end
    note('plot')
  end,
}
P.draw(true)
T.check((calls.rect or 0) > 0 and (calls.label or 0) > 0 and calls.plot == 1, 'renders bars, worker lanes and chart')
T.check(chart.n==3 and chart.first==20, 'draws cached frame chart')
T.check(sizes['##earth-profiler-frames'].x==680 and sizes['##earth-frame-stack-mod'].x==680, 'frame charts fill available width')
local saved
jsonWriteFile = function(path, data) saved = { path = path, data = data }; return true end
button = 'Save snapshot'
P.draw(true)
T.check(saved and saved.path == '/settings/beamngEarth-profiler.json', 'snapshot export')
button = 'Freeze display'
P.draw(true)
now=1000
P.update(1000, {}, true, true, send)
P.receive({ enabled = true, active = {}, recent = {} })
T.check(P.snapshot().frames == 4 and #P.snapshot().server.recent == 0, 'freeze continues collecting frames and worker feed')
P.draw(true)
T.check(chart.n==3 and chart.last==30, 'frozen display keeps the inspected history')
button='Save snapshot';P.draw(true)
T.check(saved.data.frames==3 and #saved.data.server.recent==1, 'export matches frozen frame and worker snapshot')
T.check(saved.data.plot==nil and saved.data.frameDetail._layouts==nil, 'export omits UI geometry caches')
available={x=1000,y=1000};P.draw(true)
T.check(sizes['##earth-profiler-frames'].x==1000 and sizes['##earth-frame-stack-mod'].x==1000, 'frozen frame charts resize immediately')
T.check(sizes['##earth-frame-stack-mod'].y>115, 'graph height uses taller window')
P.setPage('workers');P.draw(true)
T.check(sizes['##lane-roads / 42/MainThread'].x==1000 and sizes['##profile-pool-roads'].x==1000, 'worker lanes and bars resize with content')
P.setPage('frames')
button = 'Resume display'
P.draw(true)
T.check(chart.n==4 and chart.last==1000, 'resume catches up immediately')
now=1050;P.update(15,{},true,true,send);P.draw(true)
T.check(chart.n==4, 'live view respects refresh cadence')
now=1100;P.draw(true)
T.check(chart.n==5 and chart.last==15, 'live view refreshes without a server packet')

-- Closing/collapsing never changes the collection setting or subscription.
local settingsWrites={}
package.loaded['beamngEarth/settings']={SCHEMA={runtimeProfiler=true,profilerMode=true},set=function(key,value)
  settingsWrites[#settingsWrites+1]={key,value}
  if key=='runtimeProfiler' then P.setEnabled(value) else P.setMode(value) end
end}
closeWindow=true;P.draw(true)
T.check(P.enabled and P.compact and #settingsWrites==0, 'close keeps capture active in compact view')
local before=P.snapshot().capturedFrames
now=1600;P.update(12,{},true,true,send);P.draw(true)
T.check(P.snapshot().capturedFrames==before+1 and sent[#sent].enabled, 'compact view continues sampling and subscription renewal')
button='Open profiler';P.draw(true)
T.check(not P.compact and P.enabled, 'reopen preserves the capture')
collapsed=true;now=2100;P.update(11,{},true,true,send);P.draw(true);collapsed=false
T.check(P.snapshot().capturedFrames==before+2, 'collapsed window continues sampling')
button='Essential';P.draw(true)
T.check(P.mode=='essential' and P.snapshot().frames==0, 'mode button safely refreshes the displayed capture')
button='Full';P.draw(true)
T.check(P.mode=='full', 'switch back to Full inside the window')
T.check(calls.begin==calls['end'], 'every window Begin has a matching End')

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
