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
T.check(out and out.v == 1, 'a window after WINDOW_S')
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

local hw = { cpu = { name = 'AMD Ryzen 7 5800XT 8-Core Processor', coresPhysical = 8, coresLogical = 16 },
  gpu = { name = 'AMD Radeon RX 7800 XT', memoryMB = 16368, driverVersion = 'x', vendor = 'AMD' },
  mem = { osPhysAvailable = 33554432000 }, graphicsApi = 'Vulkan', os = { type = 'linux', windowsystem = 'wayland',
  fullname = 'user@host' }, videoMode = { width = 3840, height = 2160, refreshRate = 143.9 },
  game = { versiond = '0.40.5.0' }, settings = { vsync = true, GraphicMeshQuality = 'High', SomethingElse = 'C:\\x' },
  mods = { zip = 3, unpacked = 1 } }
local vals = { quality = 'high', telemetry = true, groundPhoto = '2048' }
local st = S.setup(hw, function(k) return vals[k] end)
T.check(st.cpu == 'AMD Ryzen 7 5800XT 8-Core Processor' and st.cores == 8 and st.threads == 16, 'the CPU model and cores')
T.check(st.gpu == 'AMD Radeon RX 7800 XT' and st.vramGB == 16 and st.ramGB == 32, 'the GPU model, VRAM and RAM rounded')
T.check(st.res == '4k' and st.hz == 144 and st.api == 'Vulkan' and st.os == 'linux', 'display class, graphics API, OS')
T.check(st.gameSettings.vsync == true and st.gameSettings.GraphicMeshQuality == 'High' and st.gameSettings.SomethingElse == nil,
  'game settings: the listed ones only')
T.check(st.modSettings.quality == 'high' and st.modSettings.groundPhoto == '2048' and st.modSettings.telemetry == nil,
  'mod settings: the listed ones only')
T.check(st.otherMods == 2 and st.fullname == nil and st.driverVersion == nil, 'other mods as a step; no user, host or driver text')
T.done('test_perfsummary')
