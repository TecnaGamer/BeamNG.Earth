-- The anonymous performance summary (TELEMETRY.md sections 2-4; the user, 2026-11-08: "start using the telemetry to
-- collect performance and world gen speed data similar to how were doing when you monitor my game. Collect system specs
-- too. but keep everything as anomomus as possible. No specific locations but identify how complex a sene is and what
-- the game and mod settings are set to").
-- Fed by core.lua every frame (frame) and once a second (sample); every WINDOW_S, take() returns one window's summary
-- for the world server (perf_summary), which keeps it for the report only while the player chose "Performance data".
-- Counts, histograms in fixed steps and rounded steps only: no position, place, route, tile or block key, no text.
-- Pure but for what the caller passes.
local M = {}

M.WINDOW_S = 300
M.VERSION = 1
-- frame time steps (ms, upper edges; the last bin is everything slower): fps ~125, 90, 70, 60, 45, 30, 20, 10, 4
-- (34, not 33: a steady 30 fps, 33.3 ms, is in the 30 fps bin)
M.FRAME_MS = { 8, 11, 14, 17, 22, 34, 50, 100, 250 }
-- speed bands (mph, lower edges): stopped, under 30, 30-60, 60-100, 100-150, over 150
M.SPEED_MPH = { 0, 2, 30, 60, 100, 150 }
M.SPEED_NAMES = { 'stopped', 'u30', 's30', 's60', 's100', 's150' }
M.HITCH_MS = { 50, 100, 250, 1000 }
M.CAUSE_MS = 100            -- frames this slow get a cause counted
M.CAUSE_KEYS = 12           -- distinct cause names a window keeps (the rest: 'other')
-- the scene by the buildings on the loaded road tiles (roads.scene: about 0.6 km round the car), lower edges
M.SCENE = { { 0, 'empty' }, { 1, 'rural' }, { 60, 'town' }, { 600, 'urban' }, { 2500, 'dense' } }
-- rounded steps for counts (loaded objects, queues) and sizes
M.COUNT_STEPS = { 0, 1, 10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000 }
M.MB_STEPS = { 0, 64, 128, 256, 512, 768, 1024, 1536, 2048, 3072, 4096 }
M.BLOCK_S = { 0.5, 1, 2, 4, 8, 16, 32 }       -- a ground block's wait, upper edges (s)

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

local w
function M.reset(now)
  w = { t0 = now, ms = 0, fh = zeros(#M.FRAME_MS + 1), bands = {}, hit = zeros(#M.HITCH_MS), hitLoad = zeros(#M.HITCH_MS),
    causes = {}, nCauses = 0,
    scene = {}, loaded = {}, heap = 0, garbage = 0, held = { edge = 0, drop = 0, loading = 0 }, teleports = 0,
    blocks = zeros(#M.BLOCK_S + 1), nBlocks = 0, roadq = {}, mapOpen = 0, lastSample = nil }
end
M.reset(nil)

-- One frame: its ms, the car's speed (mph), the name of its largest part (perflog.cause's, or nil) when slow, and
-- whether the world is loading after a teleport (its hitches are counted apart too: hitLoad)
function M.frame(ms, mph, cause, loading)
  if type(ms) ~= 'number' or ms ~= ms or ms <= 0 or ms > 60000 then return end
  w.ms = w.ms + ms
  local b = bin(ms, M.FRAME_MS)
  w.fh[b] = w.fh[b] + 1
  local band = 1
  for i, lo in ipairs(M.SPEED_MPH) do if (mph or 0) >= lo then band = i end end
  local name = M.SPEED_NAMES[band]
  local bd = w.bands[name]
  if not bd then bd = { s = 0, fh = zeros(#M.FRAME_MS + 1) }; w.bands[name] = bd end
  bd.s = bd.s + ms / 1000
  bd.fh[b] = bd.fh[b] + 1
  for i, e in ipairs(M.HITCH_MS) do
    if ms >= e then
      w.hit[i] = w.hit[i] + 1
      if loading then w.hitLoad[i] = w.hitLoad[i] + 1 end
    end
  end
  if ms >= M.CAUSE_MS then
    local k = M.causeKey(cause)
    if not w.causes[k] then
      if w.nCauses >= M.CAUSE_KEYS then k = 'other' else w.nCauses = w.nCauses + 1 end
    end
    w.causes[k] = (w.causes[k] or 0) + 1
  end
end

-- Once a second or so: s = { buildings (the loaded tiles' buildings: roads.scene), loaded = { name = count },
-- heapMB, garbageMB (a minute's), edge, drop, loading (bools: held at the edge / until the ground loads / after a
-- teleport), roadsQueued, mapOpen }
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
end

-- A ground block arrived: how long it was waited for (s).
function M.block(waitS)
  if type(waitS) ~= 'number' or waitS ~= waitS or waitS < 0 then return end
  local b = bin(waitS, M.BLOCK_S)
  w.blocks[b] = w.blocks[b] + 1
  w.nBlocks = w.nBlocks + 1
end

function M.teleport() w.teleports = w.teleports + 1 end

local function r(v, q) return math.floor(v / q + 0.5) * q end

-- The window's summary when WINDOW_S has passed (and a new window starts), else nil. drops: perflog.takeDrops (called
-- only then: { n, s, low }).
function M.take(now, drops)
  if not w.t0 then w.t0 = now end
  if now - w.t0 < M.WINDOW_S then return nil end
  if type(drops) == 'function' then drops = drops() end
  local out = { v = M.VERSION, s = r(now - w.t0, 1), fh = w.fh, hit = w.hit, hitLoad = w.hitLoad, causes = w.causes,
    teleports = w.teleports, blocks = w.blocks, mapOpen = r(w.mapOpen, 5) }
  local bands = {}
  for k, b in pairs(w.bands) do bands[k] = { s = r(b.s, 5), fh = b.fh } end
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
  'buildingDistance', 'groundPhoto', 'objectDetail', 'roads', 'clouds', 'skyMode', 'mapStyle' }

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
      v = ok and short(v, 24) or nil
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
