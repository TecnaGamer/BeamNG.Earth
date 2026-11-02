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
local calls, button = {}, nil
local function note(name) calls[name] = (calls[name] or 0) + 1 end
ui_imgui = {
  TreeNodeFlags_DefaultOpen = 1, Cond_FirstUseEver = 1,
  BoolPtr = function(v) return { [0] = v } end,
  ImVec2 = function(x, y) return { x = x, y = y } end,
  ImVec4 = function() return {} end,
  GetColorU322 = function() return 1 end,
  GetCursorScreenPos = function() return { x = 0, y = 0 } end,
  GetWindowDrawList = function() return {} end,
  InvisibleButton = function() end,
  ImDrawList_AddRectFilled = function() note('rect') end,
  ImDrawList_AddText1 = function() note('label') end,
  TextUnformatted = function() end,
  PushTextWrapPos = function() end, PopTextWrapPos = function() end,
  SameLine = function() end, SetNextWindowSize = function() end,
  Begin = function() return true end, End = function() end,
  CollapsingHeader1 = function() return true end,
  Button = function(label) if label == button then button = nil; return true end return false end,
  PlotLines1 = function(_, array, n) T.check(n == 3 and array[0] == 20, 'draws cached frame chart'); note('plot') end,
}
P.draw(true)
T.check((calls.rect or 0) > 0 and (calls.label or 0) > 0 and calls.plot == 1, 'renders bars, worker lanes and chart')
local saved
jsonWriteFile = function(path, data) saved = { path = path, data = data }; return true end
button = 'Save snapshot'
P.draw(true)
T.check(saved and saved.path == '/settings/beamngEarth-profiler.json', 'snapshot export')
button = 'Freeze display'
P.draw(true)
P.update(1000, {}, true, true, send)
P.receive({ enabled = true, active = {}, recent = {} })
T.check(P.snapshot().frames == 3 and #P.snapshot().server.recent == 1, 'freeze retains frame and worker data')
button = 'Resume display'
P.draw(true)

ui_imgui = nil
for i = 1, 1000 do now = now + 1; P.update(i, {}, true, true, send) end
s = P.snapshot()
T.check(s.frames == 600 and s.maxMs == 1000, 'frame ring bounded')
T.check(s.plot[0] == 401 and s.plot[599] == 1000, 'wrapped chart chronological order')
P.update(10, {}, true, false, send)
T.check(P.snapshot().server == nil, 'disconnect clears stale workers')
P.serverHello(nil)
local sentBefore = #sent
now = now + 1000
P.update(10, {}, true, true, send)
T.check(#sent == sentBefore, 'old server receives no profiler request')
P.serverHello(1)
T.check(P.snapshot().frames == 0, 'reconnect resets history')
P.setEnabled(false)
T.check(sent[#sent].enabled == false, 'disabling unsubscribes')
T.check(P.snapshot().frames == 0, 'disabling clears samples')
T.done('profiler')
