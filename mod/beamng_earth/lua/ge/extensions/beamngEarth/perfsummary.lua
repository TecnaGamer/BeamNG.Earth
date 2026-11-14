-- The anonymous performance summary (TELEMETRY.md sections 2-4; the user, 2026-11-08: "start using the telemetry to
-- collect performance and world gen speed data similar to how were doing when you monitor my game. Collect system specs
-- too. but keep everything as anomomus as possible. No specific locations but identify how complex a sene is and what
-- the game and mod settings are set to").
-- Fed by core.lua every frame (frame) and once a second (sample); every WINDOW_S, take() returns one window's summary
-- for the world server (perf_summary), which keeps it for the report only while the player chose "Performance data".
-- Counts, histograms in fixed steps and rounded steps only: no position, place, route, tile or block key, no text.
-- Pure but for what the caller passes.
--
-- Version 2 (the user, 2026-11-11: "improve telemetry ... so we can measure hitches and 1% lows properly"): each window
-- also carries its own p50 / p99 / p99.9 frame time (from a 0.25 ms count array: no frame list kept, no sort of tens of
-- thousands of frames), the frames over 2x and 4x its median, a log-spaced frame-time histogram (FT_MS, sparse), what
-- happened in each slow frame (note(): block swap, collision rebuild, far imports ... TAGS), the slow frames by time
-- since the last teleport and while idle, the 1 s+ frames' causes apart, and the machine's memory (game, free, VRAM).
local M = {}

M.WINDOW_S = 300
M.VERSION = 2
-- frame time steps (ms, upper edges; the last bin is everything slower): fps ~125, 90, 70, 60, 45, 30, 20, 10, 4
-- (34, not 33: a steady 30 fps, 33.3 ms, is in the 30 fps bin). Kept beside FT_MS for collectors before version 2.
M.FRAME_MS = { 8, 11, 14, 17, 22, 34, 50, 100, 250 }
-- the fine steps (ms, upper edges, about log-spaced from 4 ms to 2 s; every FRAME_MS edge among them, so a collector
-- can fold them into the coarse ones): sent sparse as { ["<0-based bin>"] = frames }
M.FT_MS = { 4, 6, 8, 11, 14, 17, 22, 28, 34, 42, 50, 70, 100, 140, 180, 250, 350, 500, 700, 1000, 2000 }
-- speed bands (mph, lower edges): stopped, under 30, 30-60, 60-100, 100-150, over 150
M.SPEED_MPH = { 0, 2, 30, 60, 100, 150 }
M.SPEED_NAMES = { 'stopped', 'u30', 's30', 's60', 's100', 's150' }
M.HITCH_MS = { 50, 100, 250, 1000 }
M.CAUSE_MS = 100            -- frames this slow get a cause counted
M.CAUSE_KEYS = 12           -- distinct cause names a window keeps (the rest: 'other')
M.CAUSE1S_KEYS = 6          -- ... of the 1 s+ frames (kept apart too: a freeze is not a 120 ms hitch)
-- What can have happened in a slow frame (note(tag), core.lua adds the frame's state): the ground block swapped (this
-- frame, or the frame before it: the new block's first draw), a collision rebuild, far-tile imports / materials, far
-- retextures, road units, distant-traffic units, tree items / new tree shapes or Forests, the server's messages handled
-- (5 ms or more), a vehicle spawned / removed, the server's catch-up boost on, the map or our UI open, the world origin
-- moving, a Lua garbage sweep, loading after a teleport, the car held (at the edge, until its ground loads). A frame
-- with none of them counts as 'none'.
M.TAGS = { 'swap', 'swapNext', 'collision', 'far', 'retex', 'roads', 'traffic', 'trees', 'treeShapes', 'decode', 'spawn',
  'despawn', 'catchup', 'ui', 'rebase', 'gc', 'loading', 'held' }
local TAGSET = {}
for _, t in ipairs(M.TAGS) do TAGSET[t] = true end
-- time since the last teleport (s of counted frames, upper edges) and the bucket names; never teleported: the last one
M.TP_S = { 10, 30, 120 }
M.TP_NAMES = { 't10', 't30', 't120', 'long' }
M.IDLE_S = 120              -- no car movement and no camera change this long: idle (away from the keyboard)
-- the scene by the buildings on the loaded road tiles (roads.scene: about 0.6 km round the car), lower edges
M.SCENE = { { 0, 'empty' }, { 1, 'rural' }, { 60, 'town' }, { 600, 'urban' }, { 2500, 'dense' } }
-- rounded steps for counts (loaded objects, queues) and sizes
M.COUNT_STEPS = { 0, 1, 10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000 }
M.MB_STEPS = { 0, 64, 128, 256, 512, 768, 1024, 1536, 2048, 3072, 4096 }
M.BLOCK_S = { 0.5, 1, 2, 4, 8, 16, 32 }       -- a ground block's wait, upper edges (s)
-- the machine's memory over a window (MB, lower edges): the game process's most, the system's least free, the most VRAM
-- in use
M.GAME_MB = { 0, 2048, 3072, 4096, 6144, 8192, 12288, 16384, 24576 }
M.FREE_MB = { 0, 512, 1024, 2048, 4096, 8192, 16384 }
M.VRAM_MB = { 0, 1024, 2048, 3072, 4096, 6144, 8192, 12288, 16384 }

-- the largest step not above v
function M.step(v, steps)
  if type(v) ~= 'number' or v ~= v then return nil end
  local out = steps[1]
  for _, s in ipairs(steps) do if v >= s then out = s end end
  return out
end

-- the index of v in upper-edge bins (#edges + 1: above the last)
local function bin(v, edges)
  for i, e in ipairs(edges) do if v <= e then return i end end
  return #edges + 1
end
M.bin = bin

local function zeros(n) local t = {} for i = 1, n do t[i] = 0 end return t end

-- A cause name as a report may carry it: the mod's part or the engine's subsystem (fixed names in code), never free
-- text; 'outside' for a frame outside both timers.
function M.causeKey(name)
  if type(name) ~= 'string' then return 'outside' end
  name = name:match('^(%S+)') or ''
  if #name == 0 or #name > 32 or name:find('[^%w%+%._:%-]') then return 'other' end
  return name
end

-- The window's exact percentiles: frames counted in slots of 0.25 ms under EX_FINE_MS and of 1 ms up to EX_MAX_MS (one
-- array, reused: 2,750 numbers), slower ones kept as they are (rare). A percentile is then read off the slow end.
local EX_FINE_MS, EX_FINE_Q, EX_MAX_MS = 250, 4, 2000
local EX_FINE_N = EX_FINE_MS * EX_FINE_Q
local EX_N = EX_FINE_N + (EX_MAX_MS - EX_FINE_MS)
local exact = zeros(EX_N)
local exactUsed = false
local function exSlot(ms)
  if ms < EX_FINE_MS then return math.floor(ms * EX_FINE_Q) + 1 end
  if ms < EX_MAX_MS then return EX_FINE_N + math.floor(ms - EX_FINE_MS) + 1 end
  return nil
end
-- a slot's middle (ms): a binary fraction (x.125, x.375, ...), so it is sent without a long decimal tail
local function exMs(i)
  if i <= EX_FINE_N then return (i - 0.5) / EX_FINE_Q end
  return EX_FINE_MS + (i - EX_FINE_N) - 0.5
end

-- Across windows: what the current frame noted, whether the frame before swapped the block, the played seconds since the
-- last teleport, and the player's activity (idle).
local notes = {}
local prevSwap = false
local sinceTp = math.huge
local lastActive, lastCam, idleNow = nil, nil, false

-- Something happened in this frame (one of TAGS; anything else is ignored): counted with the frame if it is slow.
function M.note(tag)
  if TAGSET[tag] then notes[tag] = true end
end
-- The notes of a frame that is not counted (paused, in the background): dropped.
function M.dropNotes()
  for k in pairs(notes) do notes[k] = nil end
end

local w
function M.reset(now)
  if exactUsed then
    for i = 1, EX_N do exact[i] = 0 end
    exactUsed = false
  end
  w = { t0 = now, ms = 0, n = 0, fh = zeros(#M.FRAME_MS + 1), ft = zeros(#M.FT_MS + 1), over = {}, bands = {},
    hit = zeros(#M.HITCH_MS), hitLoad = zeros(#M.HITCH_MS), causes = {}, nCauses = 0, cause1s = {}, nCause1s = 0,
    ctx = {}, tp = {}, idle = nil,
    scene = {}, loaded = {}, heap = 0, garbage = 0, held = { edge = 0, drop = 0, loading = 0 }, teleports = 0,
    blocks = zeros(#M.BLOCK_S + 1), nBlocks = 0, roadq = {}, mapOpen = 0, lastSample = nil,
    gameMB = nil, freeMB = nil, vramMB = nil }
end
M.reset(nil)

local function countCause(map, nKey, cause, keys)
  local k = M.causeKey(cause)
  if not map[k] then
    if w[nKey] >= keys then k = 'other' else w[nKey] = w[nKey] + 1 end
  end
  map[k] = (map[k] or 0) + 1
end

-- One frame: its ms, the car's speed (mph), the name of its largest part (perflog.cause's, or nil) when slow, and
-- whether the world is loading after a teleport (its hitches are counted apart too: hitLoad). What was noted since the
-- last frame (note) is counted with it when it is slow, then dropped.
function M.frame(ms, mph, cause, loading)
  if type(ms) ~= 'number' or ms ~= ms or ms <= 0 or ms > 60000 then M.dropNotes(); return end
  w.ms, w.n = w.ms + ms, w.n + 1
  local b = bin(ms, M.FRAME_MS)
  w.fh[b] = w.fh[b] + 1
  local fb = bin(ms, M.FT_MS)
  w.ft[fb] = w.ft[fb] + 1
  local slot = exSlot(ms)
  if slot then exact[slot] = exact[slot] + 1 else w.over[#w.over + 1] = ms end
  exactUsed = true
  local band = 1
  for i, lo in ipairs(M.SPEED_MPH) do if (mph or 0) >= lo then band = i end end
  local name = M.SPEED_NAMES[band]
  local bd = w.bands[name]
  if not bd then bd = { s = 0, fh = zeros(#M.FRAME_MS + 1), hit = zeros(#M.HITCH_MS) }; w.bands[name] = bd end
  bd.s = bd.s + ms / 1000
  bd.fh[b] = bd.fh[b] + 1
  sinceTp = sinceTp + ms / 1000
  local ti = #M.TP_NAMES
  for i, e in ipairs(M.TP_S) do if sinceTp < e then ti = i; break end end
  local tp = w.tp[M.TP_NAMES[ti]]
  if not tp then tp = zeros(#M.HITCH_MS + 1); w.tp[M.TP_NAMES[ti]] = tp end
  tp[1] = tp[1] + ms / 1000
  local idle = nil
  if idleNow then
    idle = w.idle
    if not idle then idle = zeros(#M.HITCH_MS + 1); w.idle = idle end
    idle[1] = idle[1] + ms / 1000
  end
  local swapNow = notes.swap == true
  if prevSwap then notes.swapNext = true end
  prevSwap = swapNow
  if loading then notes.loading = true end
  if ms >= M.HITCH_MS[1] then
    for i, e in ipairs(M.HITCH_MS) do
      if ms >= e then
        w.hit[i] = w.hit[i] + 1
        bd.hit[i] = bd.hit[i] + 1
        tp[i + 1] = tp[i + 1] + 1
        if idle then idle[i + 1] = idle[i + 1] + 1 end
        if loading then w.hitLoad[i] = w.hitLoad[i] + 1 end
      end
    end
    local any = false
    for tag in pairs(notes) do
      any = true
      local c = w.ctx[tag]
      if not c then c = zeros(#M.HITCH_MS); w.ctx[tag] = c end
      for i, e in ipairs(M.HITCH_MS) do if ms >= e then c[i] = c[i] + 1 end end
    end
    if not any then
      local c = w.ctx.none
      if not c then c = zeros(#M.HITCH_MS); w.ctx.none = c end
      for i, e in ipairs(M.HITCH_MS) do if ms >= e then c[i] = c[i] + 1 end end
    end
    if ms >= M.CAUSE_MS then countCause(w.causes, 'nCauses', cause, M.CAUSE_KEYS) end
    if ms >= 1000 then countCause(w.cause1s, 'nCause1s', cause, M.CAUSE1S_KEYS) end
  end
  M.dropNotes()
end

-- The player's activity, once a second: the car moving (1 mph or more) or the camera moved / turned is activity; none
-- for IDLE_S: idle (the frames then are counted apart too: a parked game left running is not how it plays).
-- cam: { x, y, z, fx, fy, fz } or nil.
function M.activity(now, mph, cam)
  local active = (tonumber(mph) or 0) >= 1
  if type(cam) == 'table' and tonumber(cam.x) and tonumber(cam.y) and tonumber(cam.z) then
    local c = lastCam
    if c then
      local d2 = (cam.x - c.x) ^ 2 + (cam.y - c.y) ^ 2 + (cam.z - c.z) ^ 2
      local ax, ay, az = tonumber(cam.fx) or 0, tonumber(cam.fy) or 0, tonumber(cam.fz) or 0
      local la, lb = math.sqrt(ax * ax + ay * ay + az * az), math.sqrt(c.fx * c.fx + c.fy * c.fy + c.fz * c.fz)
      local cos = (la > 0 and lb > 0) and (ax * c.fx + ay * c.fy + az * c.fz) / (la * lb) or 1
      if d2 > 0.25 or cos < 0.9995 then active = true end
    else
      c = {}
      lastCam = c
    end
    c.x, c.y, c.z = cam.x, cam.y, cam.z
    c.fx, c.fy, c.fz = tonumber(cam.fx) or 0, tonumber(cam.fy) or 0, tonumber(cam.fz) or 0
  end
  if active or not lastActive then lastActive = now end
  idleNow = (now - lastActive) >= M.IDLE_S
  return idleNow
end
function M.isIdle() return idleNow end

-- Once a second or so: s = { buildings (the loaded tiles' buildings: roads.scene), loaded = { name = count },
-- heapMB, garbageMB (a minute's), edge, drop, loading (bools: held at the edge / until the ground loads / after a
-- teleport), roadsQueued, mapOpen, gameMB, freeMB, vramMB (the game process's memory, the system's free memory, the
-- VRAM in use; any may be missing), mph and cam (activity) }
function M.sample(now, s)
  local dt = w.lastSample and math.min(5, math.max(0, now - w.lastSample)) or 1
  w.lastSample = now
  s = s or {}
  if type(s.buildings) == 'number' then
    local name = 'empty'
    for _, e in ipairs(M.SCENE) do if s.buildings >= e[1] then name = e[2] end end
    w.scene[name] = (w.scene[name] or 0) + dt
  end
  for k, v in pairs(s.loaded or {}) do
    if type(v) == 'number' and v > (w.loaded[k] or -1) then w.loaded[k] = v end
  end
  if type(s.heapMB) == 'number' and s.heapMB > w.heap then w.heap = s.heapMB end
  if type(s.garbageMB) == 'number' and s.garbageMB > w.garbage then w.garbage = s.garbageMB end
  if s.edge then w.held.edge = w.held.edge + dt end
  if s.drop then w.held.drop = w.held.drop + dt end
  if s.loading then w.held.loading = w.held.loading + dt end
  if s.mapOpen then w.mapOpen = w.mapOpen + dt end
  if type(s.roadsQueued) == 'number' then w.roadq[#w.roadq + 1] = s.roadsQueued end
  local function ok(v) return type(v) == 'number' and v == v and v >= 0 and v < 1e9 end
  if ok(s.gameMB) and s.gameMB > (w.gameMB or -1) then w.gameMB = s.gameMB end
  if ok(s.freeMB) and s.freeMB < (w.freeMB or math.huge) then w.freeMB = s.freeMB end
  if ok(s.vramMB) and s.vramMB > (w.vramMB or -1) then w.vramMB = s.vramMB end
  if s.mph ~= nil or s.cam ~= nil then M.activity(now, s.mph, s.cam) end
end

-- A ground block arrived: how long it was waited for (s).
function M.block(waitS)
  if type(waitS) ~= 'number' or waitS ~= waitS or waitS < 0 then return end
  local b = bin(waitS, M.BLOCK_S)
  w.blocks[b] = w.blocks[b] + 1
  w.nBlocks = w.nBlocks + 1
end

-- A teleport (also the level start's first place): counted, and the time since it starts again.
function M.teleport()
  w.teleports = w.teleports + 1
  sinceTp = 0
end

local function r(v, q) return math.floor(v / q + 0.5) * q end

-- How long the game has run with the mod (now: core's clock, seconds since it loaded) at a window's end, as a coarse
-- band: its lower edge in minutes, 0 / 30 / 60 / 120 / 240 (the user, 2026-11-09: many players play for hours; the
-- reports said nothing of how a long session runs)
M.AGE_MIN = { 30, 60, 120, 240 }
function M.ageBand(now)
  local m, low = (tonumber(now) or 0) / 60, 0
  for _, edge in ipairs(M.AGE_MIN) do
    if m < edge then return low end
    low = edge
  end
  return low
end

-- The k-th slowest frame of the window (ms; over: its frames over EX_MAX_MS, sorted slowest first).
local function kthSlowest(k, over)
  if k <= #over then return r(over[k], 1) end
  k = k - #over
  for i = EX_N, 1, -1 do
    local c = exact[i]
    if c > 0 then
      if k <= c then return exMs(i) end
      k = k - c
    end
  end
  return nil
end

-- p50 / p99 / p99.9 of the window's frame times (ms; p99: the frame time the slowest 1 % reach, the window's own 1 %
-- low) and how many frames took over 2x and 4x its median.
local function percentiles(out)
  local n = w.n
  if n <= 0 then return end
  table.sort(w.over, function(a, b) return a > b end)
  local p50 = kthSlowest(math.max(1, math.ceil(n * 0.5)), w.over)
  out.p50 = p50
  out.p99 = kthSlowest(math.max(1, math.ceil(n * 0.01)), w.over)
  out.p999 = kthSlowest(math.max(1, math.ceil(n * 0.001)), w.over)
  if not p50 then return end
  local c2, c4 = 0, 0
  for _, v in ipairs(w.over) do
    if v > 2 * p50 then c2 = c2 + 1 end
    if v > 4 * p50 then c4 = c4 + 1 end
  end
  for i = EX_N, 1, -1 do
    local c = exact[i]
    if c > 0 then
      local m = exMs(i)
      if m <= 2 * p50 then break end
      c2 = c2 + c
      if m > 4 * p50 then c4 = c4 + c end
    end
  end
  out.over2x, out.over4x = c2, c4
end

-- The window's summary when WINDOW_S has passed (and a new window starts), else nil. drops: perflog.takeDrops (called
-- only then: { n, s, low }).
function M.take(now, drops)
  if not w.t0 then w.t0 = now end
  if now - w.t0 < M.WINDOW_S then return nil end
  if type(drops) == 'function' then drops = drops() end
  local out = { v = M.VERSION, s = r(now - w.t0, 1), fh = w.fh, hit = w.hit, hitLoad = w.hitLoad, causes = w.causes,
    teleports = w.teleports, blocks = w.blocks, mapOpen = r(w.mapOpen, 5), age = M.ageBand(now) }
  percentiles(out)
  local ft = {}
  for i, c in ipairs(w.ft) do if c > 0 then ft[tostring(i - 1)] = c end end
  if next(ft) then out.ft = ft end
  if next(w.ctx) then out.ctx = w.ctx end
  if next(w.cause1s) then out.cause1s = w.cause1s end
  local tp = {}
  for k, v in pairs(w.tp) do v[1] = r(v[1], 1); tp[k] = v end
  if next(tp) then out.tp = tp end
  if w.idle then w.idle[1] = r(w.idle[1], 1); out.idle = w.idle end
  local bands = {}
  for k, b in pairs(w.bands) do
    bands[k] = { s = r(b.s, 5), fh = b.fh, hit = b.hit[1] > 0 and b.hit or nil }
  end
  out.bands = bands
  local scene = {}
  for k, v in pairs(w.scene) do scene[k] = r(v, 5) end
  out.scene = scene
  local loaded = {}
  for k, v in pairs(w.loaded) do loaded[k] = M.step(v, M.COUNT_STEPS) end
  out.loaded = loaded
  out.lua = { heapMB = M.step(w.heap, M.MB_STEPS), garbageMB = M.step(w.garbage, M.MB_STEPS) }
  out.held = { edge = r(w.held.edge, 5), drop = r(w.held.drop, 5), loading = r(w.held.loading, 5) }
  if #w.roadq > 0 then
    table.sort(w.roadq)
    out.roadsQueuedP90 = M.step(w.roadq[math.max(1, math.ceil(#w.roadq * 0.9))], M.COUNT_STEPS)
  end
  if w.gameMB or w.freeMB or w.vramMB then
    out.mach = { gameMB = w.gameMB and M.step(w.gameMB, M.GAME_MB), freeMB = w.freeMB and M.step(w.freeMB, M.FREE_MB),
      vramMB = w.vramMB and M.step(w.vramMB, M.VRAM_MB) }
  end
  if type(drops) == 'table' and (drops.n or 0) > 0 then
    out.drops = { n = drops.n, s = r(drops.s or 0, 5), low = drops.low and r(drops.low, 5) or nil }
  end
  M.reset(now)
  return out
end

-- The setup a session's first report carries (TELEMETRY.md section 2), from report.collectHardware()'s table and the
-- BeamNG.Earth settings: model names, rounded amounts, settings values. No user name, path, id or serial.
M.RES = { { 0, 'other' }, { 1280, '720p' }, { 1920, '1080p' }, { 2560, '1440p' }, { 3440, 'ultrawide' }, { 3840, '4k' } }
M.GAME_SETTINGS = { 'GraphicOverallQuality', 'GraphicMeshQuality', 'GraphicTextureQuality', 'GraphicTerrainQuality',
  'GraphicLightingQuality', 'GraphicShadowsQuality', 'GraphicAnisotropic', 'GraphicAntialiasType', 'GraphicAntialias',
  'vsync', 'PostFXDOFGeneralEnabled', 'PostFXSSAOGeneralEnabled', 'PostFXMotionBlurEnabled',
  'GraphicDynReflectionEnabled', 'GraphicGrassDensity', 'GraphicDisplayModes' }
M.MOD_SETTINGS = { 'quality', 'trees', 'treeDistance', 'groundCover', 'groundCoverDensity', 'grassDistance', 'rocks',
  'nightLights', 'distantTraffic', 'distantTrafficQuality', 'viewDistanceMode', 'viewDistanceKm', 'terrainDetail',
  'buildingDistance', 'groundPhoto', 'objectDetail', 'roads', 'clouds', 'skyMode', 'mapStyle', 'framePacing',
  'teleportPriority' }

local function short(v, n)
  if type(v) == 'boolean' or type(v) == 'number' then return v end
  if type(v) ~= 'string' then return nil end
  v = v:gsub('[%c]', ''):gsub('^%s+', ''):gsub('%s+$', '')
  return v:sub(1, n or 64)
end

function M.setup(hw, settingsGet)
  hw = type(hw) == 'table' and hw or {}
  local out = { v = M.VERSION }
  local cpu = hw.cpu or {}
  out.cpu = short(cpu.name or cpu.brand or cpu.model, 80)
  out.cores = tonumber(cpu.coresPhysical)
  out.threads = tonumber(cpu.coresLogical or cpu.threads or cpu.coresTotal)
  local gpu = hw.gpu or {}
  out.gpu = short(gpu.name or gpu.description, 80)
  local vram = tonumber(gpu.memoryMB)
  if vram and vram > 0 then out.vramGB = M.step(vram / 1024 + 0.5, { 0, 1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 32, 48 }) end
  local mem = hw.mem or {}
  local ram = tonumber(mem.osPhysAvailable or mem.totalPhys)
  if ram and ram > 0 then
    -- (+1.5: a 32 GB machine tells ~31.2 GiB)
    out.ramGB = M.step(ram / 1073741824 + 1.5, { 0, 4, 8, 12, 16, 24, 32, 48, 64, 96, 128 })
  end
  out.api = short(hw.graphicsApi, 16)
  local os_ = hw.os or {}
  out.os = short(os_.type, 16)
  out.windowSystem = short(os_.windowsystem, 16)
  local vm = hw.videoMode or {}
  if tonumber(vm.width) and vm.width > 0 then
    local name = 'other'
    for _, e in ipairs(M.RES) do if vm.width >= e[1] then name = e[2] end end
    out.res = name
    local hz = tonumber(vm.refreshRate)
    if hz and hz > 0 then out.hz = M.step(hz + 0.5, { 0, 30, 50, 60, 75, 90, 100, 120, 144, 165, 240 }) end
  end
  local game = hw.game or {}
  out.game = short(game.versiond or game.versionb, 24)
  local gs = {}
  for _, k in ipairs(M.GAME_SETTINGS) do
    local v = short((hw.settings or {})[k], 24)
    if v ~= nil then gs[k] = v end
  end
  out.gameSettings = gs
  local ms = {}
  if type(settingsGet) == 'function' then
    for _, k in ipairs(M.MOD_SETTINGS) do
      local ok, v = pcall(settingsGet, k)
      -- (not `ok and short(v) or nil`: that dropped every setting switched off, false, from the reports)
      if ok then v = short(v, 24) else v = nil end
      if v ~= nil then ms[k] = v end
    end
  end
  out.modSettings = ms
  local mods = hw.mods or {}
  local nm = (tonumber(mods.zip) or 0) + (tonumber(mods.unpacked) or 0)
  out.otherMods = M.step(nm, { 0, 1, 2, 5, 10, 20, 50 })
  return out
end

return M
