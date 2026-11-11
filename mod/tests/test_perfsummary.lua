-- luajit mod/tests/test_perfsummary.lua  (the anonymous performance summary, perfsummary.lua; TELEMETRY.md)
-- Frame histograms overall and per speed band, hitches and their causes (plain names only), the scene by the loaded
-- buildings, loaded counts and memory in rounded steps, holds, blocks, drops; a window every WINDOW_S; the setup's
-- model names, rounded amounts and settings with nothing else in it.
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local S = require('beamngEarth/perfsummary')
S.reset(0)
local now = 0
for _ = 1, 600 do S.frame(1000 / 60, 0); now = now + 1 / 60 end          -- 10 s stopped at 60 fps
for _ = 1, 300 do S.frame(1000 / 30, 70); now = now + 1 / 30 end         -- 10 s at 70 mph, 30 fps
S.frame(300, 70, 'terrainCommit 280', true)
S.frame(120, 70, nil)
S.frame(1200, 70, 'C:\\Users\\me\\mod.lua 900')
for i = 1, 20 do S.sample(i, { buildings = 800, loaded = { trees = 12345, roadTiles = 40 }, heapMB = 300, garbageMB = 150,
  edge = i <= 5, loading = i <= 10, roadsQueued = i }) end
S.block(0.3); S.block(3); S.block(100)
S.teleport()
T.check(S.take(100, function() return { n = 9 } end) == nil, 'before WINDOW_S: nothing')
local out = S.take(S.WINDOW_S + 1, function() return { n = 2, s = 23, low = 17 } end)
T.check(out and out.v == 2, 'a window after WINDOW_S')
local n = 0
for _, v in ipairs(out.fh) do n = n + v end
T.check(n == 903 and #out.fh == #S.FRAME_MS + 1, 'every frame in the histogram')
T.check(out.bands.stopped and out.bands.s60 and math.abs(out.bands.stopped.s - 10) < 0.1 + 5 and out.bands.s60.fh[6] == 300,
  'per speed band: seconds and its own histogram (30 fps in the 22-33 ms bin)')
T.check(out.hit[1] == 3 and out.hit[2] == 3 and out.hit[3] == 2 and out.hit[4] == 1, 'hitches over 50 / 100 / 250 / 1000 ms')
T.check(out.hitLoad[1] == 1 and out.hitLoad[3] == 1 and out.hitLoad[4] == 0, 'the hitches while loading after a teleport apart')
T.check(out.causes.terrainCommit == 1 and out.causes.outside == 1 and out.causes.other == 1,
  'causes: the part name, outside the timers, anything not a plain name is "other" (no path)')
T.check(out.scene.urban and out.scene.urban >= 15, 'the scene by buildings (800: urban)')
T.check(out.loaded.trees == 10000 and out.loaded.roadTiles == 10, 'loaded counts in rounded steps')
T.check(out.lua.heapMB == 256 and out.lua.garbageMB == 128, 'memory in steps')
T.check(out.held.edge == 5 and out.held.loading == 10, 'held seconds (rounded to 5)')
T.check(out.blocks[1] == 1 and out.blocks[4] == 1 and out.blocks[#out.blocks] == 1, 'block waits in steps')
T.check(out.teleports == 1 and out.drops.n == 2 and out.drops.s == 25 and out.drops.low == 15, 'teleports, drops rounded')
T.check(out.roadsQueuedP90 == 10, 'the road queue p90 in steps')
T.check(out.age == 0 and S.ageBand(29 * 60) == 0 and S.ageBand(45 * 60) == 30 and S.ageBand(90 * 60) == 60
  and S.ageBand(3 * 3600) == 120 and S.ageBand(9 * 3600) == 240, 'session length: its band\'s lower edge in minutes')
for k, v in pairs(out) do
  T.check(k ~= 'lat' and k ~= 'lon' and type(v) ~= 'string', 'no position, no text: ' .. k)
end
local again = S.take(S.WINDOW_S * 2 + 2)
T.check(again and again.fh[1] == 0 and again.teleports == 0, 'the next window starts empty')

-- version 2: the window's own percentiles, the fine histogram, a slow frame's context, time since teleport, idle, memory
S.reset(0)
S.teleport()
for _ = 1, 990 do S.frame(1000 / 60, 40) end
for _ = 1, 10 do S.frame(100, 40) end
local v2 = S.take(S.WINDOW_S)
T.check(v2.p50 == 16.625 and v2.p99 == 100.125 and v2.p999 == 100.125,
  'p50 / p99 / p99.9 from the 0.25 ms slots (a binary fraction: no long decimals)')
T.check(v2.over2x == 10 and v2.over4x == 10, 'frames over 2x and 4x the median')
T.check(v2.ft['5'] == 990 and v2.ft['12'] == 10 and v2.ft['0'] == nil, 'the fine histogram, sparse, 0-based keys')
T.check(v2.tp.t10[1] == 10 and v2.tp.t30[1] > 0 and v2.tp.t10[2] == 0 and v2.tp.t30[3] == 10,
  'seconds and hitches by time since the teleport (the 100 ms frames came at ~16 s)')
T.check(v2.ctx.none[1] == 10 and v2.ctx.none[2] == 10 and v2.ctx.none[3] == 0, 'slow frames with nothing noted: none')
T.check(v2.bands.s30.hit[2] == 10 and v2.bands.s30.hit[4] == 0, 'hitches per speed band')
T.check(v2.idle == nil and v2.cause1s == nil and v2.mach == nil, 'nothing idle, no 1 s+ frames, no memory: left out')

S.reset(0)
S.note('swap'); S.frame(120, 0, 'terrainCommit 100')
S.frame(80, 0)                                   -- the frame after a swap
S.note('far'); S.note('roads'); S.frame(10, 0)   -- a fast frame: its notes dropped
S.frame(60, 0)
S.note('C:\\Users\\x'); S.note('trees'); S.frame(1500, 0, 'Lua:trees 1200')
S.note('far'); S.dropNotes(); S.frame(55, 0)
local c = S.take(S.WINDOW_S).ctx
T.check(c.swap[1] == 1 and c.swap[2] == 1 and c.swapNext[1] == 1 and c.swapNext[2] == 0, 'the swap and the frame after')
T.check(c.far == nil and c.roads == nil, 'a fast frame\'s notes are not carried to the next slow one; dropNotes')
T.check(c.none[1] == 2 and c.trees[4] == 1, 'none; the 1 s+ count per tag')
local nk = 0
for k in pairs(c) do nk = nk + 1; T.check(S.TAGS and (k == 'none' or k:match('^%a+$')), 'tags are fixed names: ' .. k) end
T.check(nk == 4, 'swap, swapNext, none, trees (no path) and nothing else: ' .. nk)

S.reset(0)
S.frame(1500, 0, 'Lua:trees 1200'); S.frame(2500, 0, 'FramePresent 2400'); S.frame(3000, 0, 'C:\\x 2000')
for _ = 1, 997 do S.frame(16, 0) end
local w3 = S.take(S.WINDOW_S)
T.check(w3.cause1s['Lua:trees'] == 1 and w3.cause1s.FramePresent == 1 and w3.cause1s.other == 1, 'the 1 s+ causes apart')
T.check(w3.p999 == 3000 and w3.p99 == 16.125, 'frames over 2 s kept exactly for the percentiles')
T.check(w3.over4x == 3 and w3.over2x == 3, 'the slow ones counted over the median')

S.reset(0)
local cam = { x = 0, y = 0, z = 0, fx = 0, fy = 1, fz = 0 }
for t = 1, 200 do S.sample(t, { mph = 0, cam = cam, gameMB = 5000 + t, freeMB = 3000 - t, vramMB = 7000 }) end
T.check(S.isIdle(), 'no movement and the camera still for IDLE_S: idle')
S.frame(70, 0)
cam.fy, cam.fx = 0.9, 0.43
S.sample(201, { mph = 0, cam = cam })
T.check(not S.isIdle(), 'the camera turned: active again')
local w4 = S.take(S.WINDOW_S)
T.check(w4.idle and w4.idle[2] == 1 and w4.mach.gameMB == 4096 and w4.mach.freeMB == 2048 and w4.mach.vramMB == 6144,
  'idle frames apart; the game\'s most memory, the least free, the most VRAM in steps')

local hw = { cpu = { name = 'AMD Ryzen 7 5800XT 8-Core Processor', coresPhysical = 8, coresLogical = 16 },
  gpu = { name = 'AMD Radeon RX 7800 XT', memoryMB = 16368, driverVersion = 'x', vendor = 'AMD' },
  mem = { osPhysAvailable = 33554432000 }, graphicsApi = 'Vulkan', os = { type = 'linux', windowsystem = 'wayland',
  fullname = 'user@host' }, videoMode = { width = 3840, height = 2160, refreshRate = 143.9 },
  game = { versiond = '0.40.5.0' }, settings = { vsync = true, GraphicMeshQuality = 'High', SomethingElse = 'C:\\x' },
  mods = { zip = 3, unpacked = 1 } }
local vals = { quality = 'high', telemetry = true, groundPhoto = '2048', framePacing = false, teleportPriority = true, trees = false }
local st = S.setup(hw, function(k) return vals[k] end)
T.check(st.cpu == 'AMD Ryzen 7 5800XT 8-Core Processor' and st.cores == 8 and st.threads == 16, 'the CPU model and cores')
T.check(st.gpu == 'AMD Radeon RX 7800 XT' and st.vramGB == 16 and st.ramGB == 32, 'the GPU model, VRAM and RAM rounded')
T.check(st.res == '4k' and st.hz == 144 and st.api == 'Vulkan' and st.os == 'linux', 'display class, graphics API, OS')
T.check(st.gameSettings.vsync == true and st.gameSettings.GraphicMeshQuality == 'High' and st.gameSettings.SomethingElse == nil,
  'game settings: the listed ones only')
T.check(st.modSettings.quality == 'high' and st.modSettings.groundPhoto == '2048' and st.modSettings.telemetry == nil,
  'mod settings: the listed ones only')
T.check(st.otherMods == 2 and st.fullname == nil and st.driverVersion == nil, 'other mods as a step; no user, host or driver text')
T.check(st.modSettings.framePacing == false and st.modSettings.trees == false and st.modSettings.teleportPriority == true,
  'settings switched off are reported too (false); frame pacing and full-priority loading among them')
T.done('test_perfsummary')
