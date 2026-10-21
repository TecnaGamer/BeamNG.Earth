-- BeamNG.Earth dev tools: GE extension 'beamngEarth_dev' (docs/DEVTOOLS.md). In-world overlays
-- (debugDrawer, per-tile material tint) and imgui panels to see the LODs, the loading and the
-- physics ground while driving. Opt-in: everything is off at start, nothing persists but the
-- radii (settings.lua), and with every overlay and panel off the hooks return at their first line.
--
-- Console:
--   beamngEarth_dev.set('<name>', true|false)   names: M.OVERLAYS, M.PANELS
--   beamngEarth_dev.preset('lod'|'loading'|'physics'|'all'|'off')   beamngEarth_dev.toggleLast()
--   (an input action, unbound by default: "BeamNG.Earth: toggle dev overlays")
--
-- Overlays:
--   tint      every far tile's own imagery Material gets a diffuseColor multiply by level (green =
--             the physics-exact level and finer, cyan L0, blue L1, purple L2, orange L3 -> red),
--             applied and restored in time-sliced batches (TINT_BATCH materials and at most
--             TINT_MS per frame, never in a governor heavy or quiet frame). The original value of
--             each material is kept and written back exactly. Tiles on the shared static material
--             (protocol 3, --imagery off) are not tinted.
--   outlines  footprints of the tiles within outlineKm (nearest MAX_OUTLINES), at each tile's mean
--             height, coloured by level when shown and by state otherwise (hidden-loaded grey, in a
--             crossover yellow, previous anchor magenta, doomed red).
--   loading   queued tiles within outlineKm flash; the next NEXT_IMPORTS imports are numbered in the
--             order farfield.peekImport takes them (mesh mode: nearest the camera first).
--   physics   the TerrainBlock's collidable square (the pending block dashed) and the
--             terrain-follow prepare / commit squares, sampled on the physics ground.
--   physicsTerrain  renders the invisible TerrainBlock (mesh mode) to compare it with the tiles.
--   rings     near radii around the player, the rebase and prefetch circles around the anchor
--             origin, the far-field square (server RADIUS, a Chebyshev half-size).
--   probe     a vertical line at the car: the physics ground, the tile under the car (level, its
--             crossover offset); red when the tile is not the exact level or more than PROBE_TOL off.
-- Panels (one imgui window "BeamNG.Earth Dev"): stats, graphs (60 s), mini-map (click a tile to
-- inspect it), controls (overlays, presets, freeze LOD, force rebase, budget, sky), log (events).
--
-- Cost: tiles come from a spatial index (tileindex.lua) that farfield keeps current through
-- farfield.setWatcher only while the dev tools are on; the lists to draw are rebuilt every
-- REFRESH_S, and at most MAX_LINES debug lines are drawn per frame. The stats panel shows the dev
-- tools' own cost per frame.

local M = {}
M.dependencies = { 'ui_imgui' }

local farfield = require('beamngEarth/farfield')
local tileindex = require('beamngEarth/tileindex')
local crossover = require('beamngEarth/crossover')
local lattice = require('beamngEarth/lattice')
local follow = require('beamngEarth/follow')
local rebase = require('beamngEarth/rebase')
local terrain = require('beamngEarth/terrain')
local stopwatch = require('beamngEarth/stopwatch')

local floor, min, max, abs, sqrt, huge = math.floor, math.min, math.max, math.abs, math.sqrt, math.huge

M.OVERLAYS = { 'tint', 'outlines', 'loading', 'physics', 'physicsTerrain', 'rings', 'probe' }
M.PANELS = { 'stats', 'graphs', 'map', 'controls', 'log' }
M.PRESETS = {
  off = {},
  lod = { tint = true, outlines = true, probe = true, stats = true, map = true, controls = true },
  loading = { loading = true, outlines = true, stats = true, graphs = true, map = true, log = true, controls = true },
  physics = { physics = true, rings = true, probe = true, stats = true, controls = true },
  all = { tint = true, outlines = true, loading = true, physics = true, rings = true, probe = true, stats = true,
    graphs = true, map = true, controls = true, log = true },
}
M.PRESET_ORDER = { 'off', 'lod', 'loading', 'physics', 'all' }

M.TINT_BATCH = 50        -- materials per frame at most
M.TINT_MS = 0.25         -- ... and no more material changes once a frame spent this much on them
M.DEFAULT_DIFFUSE = '1 1 1 1' -- a Material's diffuseColor when the field reads empty
M.REFRESH_S = 0.25       -- outlines / loading / mini-map lists
M.PHYS_REFRESH_S = 1.0   -- physics squares (ray-sampled)
M.STATS_S = 0.5
M.GRAPH_S = 0.25
M.GRAPH_N = 240          -- 60 s at GRAPH_S
M.FRAME_RING = 1024
M.STATS_WINDOW_S = 5
M.MAX_OUTLINES = 150
M.MAX_LOADING = 100
M.NEXT_IMPORTS = 10
M.MAX_MAP_TILES = 1600
M.MAP_PX = 320
M.MAP_MIN_PX = 1.5       -- minimum pixel size on the mini-map
M.MAX_LINES = 1200       -- debug lines per frame
M.LOG_N = 50
M.PROBE_TOL = 0.05       -- m (V1_4_SPEC exactMiss: > 5 cm off is a miss)
M.LIFT = 0.5             -- m, outlines above the tile's mean height
M.PHYS_STEP = 64         -- m between ray samples on the physics squares
M.RING_SEGS = 64
M.NEAR_RADII = { 150, 400 } -- server NEAR_RADII (both layouts use 150 / 400 m)
M.FAR_HALF = 80000       -- server farfield.RADIUS (half-size of the far-field square)
M.FORCE_TIMEOUT_S = 60
M.FORCE_SLACK = 50       -- m

-- level colours (r, g, b); exact = the physics lattice level and finer
M.LEVEL_RGB = {
  exact = { 0.15, 0.9, 0.2 }, fine = { 0.6, 0.95, 0.2 },
  [0] = { 0.1, 0.9, 0.95 }, [1] = { 0.2, 0.45, 1 }, [2] = { 0.7, 0.3, 1 }, [3] = { 1, 0.65, 0.1 },
  [4] = { 1, 0.45, 0.1 }, coarse = { 1, 0.15, 0.1 },
}
M.STATE_RGB = {
  hidden = { 0.6, 0.6, 0.6 }, crossover = { 1, 1, 0 }, old = { 0.9, 0.2, 0.75 }, doomed = { 1, 0, 0 },
  queued = { 1, 0.55, 0 }, queued2 = { 1, 1, 1 },
}

------------------------------------------------------------------------------------------
-- state

local on = {}              -- overlay / panel name -> true
local known = {}
for _, n in ipairs(M.OVERLAYS) do known[n] = 'overlay' end
for _, n in ipairs(M.PANELS) do known[n] = 'panel' end
local active = false       -- any overlay or panel on
local busy = false         -- active, or tint restores still to run (the hooks' only check)
local lastPreset = 'lod'
local params = { outlineKm = 3, mapKm = 10 }
local clock = stopwatch.clock()
local now = 0              -- s, dev clock (advanced by frame)

local idx = nil            -- tileindex while watching
local counts = { adds = 0, removes = 0 }
local matLevel = {}        -- material name -> level of the tile using it
local tintQ, tqH, tqT, tintQueued = {}, 1, 0, {}
local tintState = {}       -- material name -> { id = object id, orig = diffuseColor, col = applied tint }
local prevRender = nil     -- terrain render flag before physicsTerrain
local forced = nil         -- force-rebase in progress { prev, count, t }

local lists = { outlines = {}, loading = {}, labels = {}, physics = {}, rings = {}, map = {}, queued = {} }
local refreshT, physT, ringKey = huge, huge, nil
local stats = { lines = {}, t = 0, imports = nil, removes = nil }
local frameRing = { t = {}, ms = {}, n = 0 }
local lastFrameT = nil
local simSum, realSum = 0, 0
local graphs = { t = 0, off = 0, n = 0, ms = {}, queue = {}, imports = {}, speed = {}, msMax = 0, lastImports = nil }
local events, evN = {}, 0
local evPrev = nil
local cost = { ema = 0, last = 0 }
local selected = nil       -- mini-map pick
local mapLevelMin = nil    -- finest level the mini-map draws at its zoom
local drawn = 0            -- debug lines drawn this frame

------------------------------------------------------------------------------------------
-- small helpers

local function anyOn()
  for _ in pairs(on) do return true end
  return false
end

local function tintPending() return tqH <= tqT end

local function exactLevel() return farfield.exactLevelFor(crossover.cells()) end

local function levelRgb(level, exact)
  if level <= exact then return M.LEVEL_RGB.exact end
  if level < 0 then return M.LEVEL_RGB.fine end
  return M.LEVEL_RGB[level] or M.LEVEL_RGB.coarse
end
M.levelRgb = levelRgb

-- The diffuseColor multiply for a level: the level colour lifted towards white, so the imagery
-- stays readable under it.
function M.tintColor(level, exact)
  local c = levelRgb(level, exact or exactLevel())
  local function f(v) return 0.35 + 0.65 * v end
  return string.format('%.3f %.3f %.3f 1', f(c[1]), f(c[2]), f(c[3]))
end

-- The state a tile instance is drawn in.
function M.tileState(inst, anchor)
  if inst.doomed then return 'doomed' end
  if inst.grp then return 'crossover' end
  if anchor ~= nil and inst.anchor ~= anchor then return 'old' end
  if inst.shown then return 'shown' end
  if inst.loaded then return 'hidden' end
  return 'queued'
end

local colorCache = {}
local function colorF(rgb, a)
  local k = rgb[1] .. ',' .. rgb[2] .. ',' .. rgb[3] .. ',' .. (a or 1)
  local c = colorCache[k]
  if not c then
    c = ColorF(rgb[1], rgb[2], rgb[3], a or 1)
    colorCache[k] = c
  end
  return c
end

local function logEvent(text)
  evN = evN + 1
  events[(evN - 1) % M.LOG_N + 1] = { t = now, text = text }
end

------------------------------------------------------------------------------------------
-- tint (time-sliced material changes)

local function enqueueTint(name)
  if not name or tintQueued[name] then return end
  tintQueued[name] = true
  tqT = tqT + 1
  tintQ[tqT] = name
end

local function setColor(m, col)
  m:setField('diffuseColor', 0, col)
  pcall(function() m:flush() end)
  pcall(function() m:reload() end)
end

-- Bring one material in line with the wanted state: tinted for its level while 'tint' is on,
-- else its original colour back. A material deleted (or replaced by a new one of the same name)
-- since it was tinted has nothing to restore.
local function applyTint(name)
  local m = scenetree.findObject(name)
  local s = tintState[name]
  if s and (not m or m:getID() ~= s.id) then
    tintState[name], s = nil, nil
  end
  if not m then return end
  local level = matLevel[name]
  if on.tint and level ~= nil then
    local col = M.tintColor(level)
    if not s then
      local orig = m:getField('diffuseColor', 0)
      if orig == nil or orig == '' then orig = M.DEFAULT_DIFFUSE end
      s = { id = m:getID(), orig = orig }
      tintState[name] = s
    end
    if s.col ~= col then
      setColor(m, col)
      s.col = col
    end
  elseif s then
    setColor(m, s.orig)
    tintState[name] = nil
  end
end

-- Run queued material changes within this frame's slice. gov: the core's governor (nil in tests).
local function processTint(gov)
  if not tintPending() then return 0 end
  if gov and (gov:isHeavyFrame() or gov.blocked) then return 0 end -- never next to a heavy op
  local t0, n = clock(), 0
  while tqH <= tqT and n < M.TINT_BATCH do
    if n > 0 and clock() - t0 >= M.TINT_MS then break end
    local name = tintQ[tqH]
    tintQ[tqH] = nil
    tqH = tqH + 1
    tintQueued[name] = nil
    local ok, err = pcall(applyTint, name)
    if not ok then log('W', 'beamngEarth', 'dev tint ' .. tostring(name) .. ': ' .. tostring(err)) end
    n = n + 1
  end
  if tqH > tqT then tintQ, tqH, tqT = {}, 1, 0 end
  return n
end

------------------------------------------------------------------------------------------
-- the spatial index, kept by farfield while the dev tools are on

local lastAnchor = nil
local lastRebaseCount = nil

local function addTile(inst)
  if not inst or not inst.id or inst.doomed or inst.failed then return end
  idx:insert(inst.id, inst)
  if inst.mat then
    matLevel[inst.mat] = inst.level
    if on.tint then enqueueTint(inst.mat) end
  end
end

local function removeTile(id)
  if not idx or idx:remove(id) then counts.removes = counts.removes + 1 end
  if selected and (selected.id == id or selected.doomed or selected.failed) then selected = nil end
end

local function pruneIndex()
  if not idx then return end
  local rm = nil
  for _, lv in pairs(idx.levels) do
    for _, c in pairs(lv.cells) do
      for id, inst in pairs(c) do
        if inst.doomed or inst.failed then
          if not rm then rm = {} end
          rm[#rm + 1] = id
        end
      end
    end
  end
  if rm then
    for i = 1, #rm do removeTile(rm[i]) end
    if selected and (selected.doomed or selected.failed) then selected = nil end
  end
end

local function rebuild()
  if not idx then return end
  idx:clear()
  farfield.eachTile(addTile)
  lastAnchor = farfield.anchorId()
  refreshT = huge
  if selected and (selected.doomed or selected.failed) then selected = nil end
end

local watcher = {
  onAdd = function(inst)
    counts.adds = counts.adds + 1
    if idx and inst.id and not inst.doomed and not inst.failed then addTile(inst) end
  end,
  onRemove = function(id)
    removeTile(id)
  end,
  onReset = function()
    rebuild()
    lists.map, lists.outlines, lists.loading, lists.labels, lists.queued = {}, {}, {}, {}, {}
    refreshT, physT, ringKey = huge, huge, nil
  end,
}

------------------------------------------------------------------------------------------
-- switching

local function restoreControls()
  if farfield.isFrozen() then farfield.setFrozen(false) end
  if prevRender ~= nil then
    terrain.setRenderEnabled(prevRender)
    prevRender = nil
  end
  if forced then
    rebase.distance = forced.prev
    forced = nil
  end
end

local function updateActive()
  local was = active
  active = anyOn()
  if active and not was then
    idx = tileindex.new()
    farfield.setWatcher(watcher)
    rebuild()
    lastFrameT, evPrev, lastRebaseCount = nil, nil, nil
  elseif was and not active then
    farfield.setWatcher(nil)
    idx = nil
    lastAnchor, lastRebaseCount = nil, nil
    restoreControls()
    for k in pairs(lists) do lists[k] = {} end
    selected = nil
  end
  busy = active or tintPending()
end

-- Turn an overlay or panel on or off. Returns the new state, or nil + error for an unknown name.
function M.set(name, value)
  if not known[name] then return nil, 'unknown dev tool ' .. tostring(name) end
  value = value and true or false
  local prev = on[name] == true
  if value == prev then return value end
  on[name] = value or nil
  updateActive()
  refreshT, physT, ringKey, stats.acc = huge, huge, nil, huge -- rebuild what is drawn next frame
  if name == 'tint' then
    if value then
      if idx then
        for _, set in pairs(idx.levels) do
          for _, c in pairs(set.cells) do
            for _, inst in pairs(c) do if inst.mat then matLevel[inst.mat] = inst.level; enqueueTint(inst.mat) end end
          end
        end
      end
    else
      for name2 in pairs(tintState) do enqueueTint(name2) end
    end
    busy = active or tintPending()
  elseif name == 'physicsTerrain' then
    if value then
      prevRender = terrain.isRenderEnabled()
      terrain.setRenderEnabled(true)
    elseif prevRender ~= nil then
      terrain.setRenderEnabled(prevRender)
      prevRender = nil
    end
  end
  return value
end

function M.get(name) return on[name] == true end
function M.isActive() return active end

-- Apply a preset: exactly its overlays and panels on, everything else off.
function M.preset(name)
  local p = M.PRESETS[name]
  if not p then return nil, 'unknown preset ' .. tostring(name) end
  for n in pairs(known) do
    if not p[n] then M.set(n, false) end
  end
  for n in pairs(known) do
    if p[n] then M.set(n, true) end
  end
  if name ~= 'off' then lastPreset = name end
  return name
end

-- The key binding: everything off when anything is on, else the last preset used.
function M.toggleLast()
  if active then return M.preset('off') end
  return M.preset(lastPreset)
end

function M.setParam(name, v)
  v = tonumber(v)
  if params[name] == nil or not v then return nil end
  params[name] = v
  refreshT = huge
  return v
end

function M.state()
  local t = {}
  for n in pairs(known) do t[n] = on[n] == true end
  return { on = t, overlays = M.OVERLAYS, panels = M.PANELS, presets = M.PRESET_ORDER, lastPreset = lastPreset,
    params = { outlineKm = params.outlineKm, mapKm = params.mapKm }, frozen = farfield.isFrozen(),
    costMs = cost.ema, active = active }
end

-- Freeze LOD: swap groups wait (tiles keep loading). Off again when the dev tools go off.
function M.freeze(v) return farfield.setFrozen(v) end

-- Rebase at the player's position now: the rebase distance drops to the current distance until
-- the next rebase (or FORCE_TIMEOUT_S), then comes back.
function M.forceRebase(st)
  st = st or (rawget(_G, 'beamngEarth_core') and beamngEarth_core._state())
  local pl = st and st.player
  if not pl or forced then return false end
  forced = { prev = rebase.distance, count = st.rebaseCount or 0, t = now }
  -- (a little below the current distance: driving back towards the origin while the server
  -- prepares the anchor must not keep the commit from happening)
  rebase.distance = max(1, pl.dist - M.FORCE_SLACK)
  logEvent(string.format('force rebase at %.0f m from the origin', pl.dist))
  return true
end

------------------------------------------------------------------------------------------
-- draw lists (rebuilt every REFRESH_S)

local function seg(list, a, b, c) list[#list + 1] = { a, b, c } end

local function addBox(list, x0, y0, x1, y1, z, c)
  local p1, p2, p3, p4 = vec3(x0, y0, z), vec3(x1, y0, z), vec3(x1, y1, z), vec3(x0, y1, z)
  seg(list, p1, p2, c); seg(list, p2, p3, c); seg(list, p3, p4, c); seg(list, p4, p1, c)
end

local function groundZ(x, y, zRef)
  local ok, d = pcall(castRayStatic, vec3(x, y, zRef + 1000), vec3(0, 0, -1), 3000)
  if ok and d and d < 3000 then return zRef + 1000 - d end
  return nil
end

-- A square sampled every PHYS_STEP on the physics ground (dashed: every other segment).
local function addGroundSquare(list, x0, y0, x1, y1, zRef, c, dashed)
  local pts = {}
  local function edge(ax, ay, bx, by)
    local len = max(abs(bx - ax), abs(by - ay))
    local n = max(1, floor(len / M.PHYS_STEP + 0.5))
    for i = 0, n - 1 do
      local x, y = ax + (bx - ax) * i / n, ay + (by - ay) * i / n
      pts[#pts + 1] = vec3(x, y, (groundZ(x, y, zRef) or zRef) + M.LIFT)
    end
  end
  edge(x0, y0, x1, y0); edge(x1, y0, x1, y1); edge(x1, y1, x0, y1); edge(x0, y1, x0, y0)
  for i = 1, #pts do
    if not dashed or i % 2 == 1 then seg(list, pts[i], pts[i % #pts + 1], c) end
  end
end

local function addCircle(list, cx, cy, r, z, c)
  local prev
  for i = 0, M.RING_SEGS do
    local a = 2 * math.pi * i / M.RING_SEGS
    local p = vec3(cx + r * math.cos(a), cy + r * math.sin(a), z)
    if prev then seg(list, prev, p, c) end
    prev = p
  end
end

-- The order farfield.peekImport imports the queued group tiles in (mesh mode with a camera:
-- nearest footprint first, ties in queue order; else queue order), then the prefetch tiles.
function M.nextImports(main, pre, cam, mesh, n)
  local order = {}
  if mesh and cam then
    local tmp = {}
    for i, inst in ipairs(main) do
      local dx = max(inst.x0 - cam.x, 0, cam.x - inst.x1)
      local dy = max(inst.y0 - cam.y, 0, cam.y - inst.y1)
      tmp[i] = { inst = inst, d = dx * dx + dy * dy, i = i }
    end
    table.sort(tmp, function(a, b) if a.d ~= b.d then return a.d < b.d end return a.i < b.i end)
    for i = 1, min(n, #tmp) do order[i] = tmp[i].inst end
  else
    for i = 1, min(n, #main) do order[i] = main[i] end
  end
  for i = 1, #pre do
    if #order >= n then break end
    order[#order + 1] = pre[i]
  end
  return order
end

local function refreshTiles(px, py, st)
  local anchor = farfield.anchorId()
  if anchor ~= lastAnchor then rebuild() end
  pruneIndex()
  local exact = exactLevel()
  if on.outlines then
    local list = {}
    local res = tileindex.nearest(idx:query(px, py, params.outlineKm * 1000), M.MAX_OUTLINES)
    for _, r in ipairs(res) do
      local inst = r.item
      if not inst.doomed and not inst.failed then
        local s = M.tileState(inst, anchor)
        local rgb = s == 'shown' and levelRgb(inst.level, exact) or M.STATE_RGB[s] or M.STATE_RGB.hidden
        addBox(list, inst.x0, inst.y0, inst.x1, inst.y1, inst.z + (inst.off or 0) + M.LIFT, colorF(rgb))
      end
    end
    lists.outlines = list
  end
  if on.loading or on.map then
    local main, pre = farfield.queuedTiles()
    lists.queued = {}
    local r2 = (params.outlineKm * 1000) ^ 2
    local box, labels = {}, {}
    local flashA, flashB = colorF(M.STATE_RGB.queued), colorF(M.STATE_RGB.queued2)
    for _, set in ipairs({ main, pre }) do
      for _, inst in ipairs(set) do
        if not inst.doomed and not inst.failed then
          lists.queued[#lists.queued + 1] = inst
          if on.loading and #box < 4 * M.MAX_LOADING and tileindex.dist2(inst, px, py) <= r2 then
            local z = inst.z + M.LIFT
            local p1, p2, p3, p4 = vec3(inst.x0, inst.y0, z), vec3(inst.x1, inst.y0, z), vec3(inst.x1, inst.y1, z), vec3(inst.x0, inst.y1, z)
            box[#box + 1] = { p1, p2, flashA, flashB }; box[#box + 1] = { p2, p3, flashA, flashB }
            box[#box + 1] = { p3, p4, flashA, flashB }; box[#box + 1] = { p4, p1, flashA, flashB }
          end
        end
      end
    end
    if on.loading then
      local order = M.nextImports(main, pre, st and st.cam, farfield.nearVisual() == 'mesh', M.NEXT_IMPORTS)
      for i, inst in ipairs(order) do
        labels[i] = { pos = vec3(0.5 * (inst.x0 + inst.x1), 0.5 * (inst.y0 + inst.y1), inst.z + 10),
          text = string.format('%d  L%d', i, inst.level) }
      end
    end
    lists.loading, lists.labels = box, labels
  end
  if on.map then
    local R = params.mapKm * 1000
    local raw = idx:query(px, py, R * 1.415)
    local activeTiles = {}
    for _, r in ipairs(raw) do
      local inst = r.item
      if not inst.doomed and not inst.failed
        and not (inst.grp and inst.grp.role == 'remove' and inst.grp.g and #(inst.grp.g.adds or {}) > 0) then
        activeTiles[#activeTiles + 1] = r
      end
    end
    table.sort(activeTiles, function(a, b)
      local va = (a.item.shown or a.item.grp ~= nil) and 1 or 0
      local vb = (b.item.shown or b.item.grp ~= nil) and 1 or 0
      if va ~= vb then return va > vb end
      if a.d2 ~= b.d2 then return a.d2 < b.d2 end
      return a.id < b.id
    end)
    for i = #activeTiles, M.MAX_MAP_TILES + 1, -1 do activeTiles[i] = nil end
    local layer = { hidden = 1, old = 2, crossover = 4, shown = 4 }
    local list = {}
    for _, r in ipairs(activeTiles) do
      local inst = r.item
      local s = M.tileState(inst, anchor)
      if s ~= 'doomed' then
        local rgb = (s == 'shown' or (s == 'crossover' and inst.shown)) and levelRgb(inst.level, exact)
          or M.STATE_RGB[s] or M.STATE_RGB.hidden
        list[#list + 1] = { inst = inst, state = s, rgb = rgb }
      end
    end
    table.sort(list, function(a, b)
      local la, lb = layer[a.state] or 0, layer[b.state] or 0
      if la ~= lb then return la < lb end
      if a.inst.level ~= b.inst.level then return a.inst.level > b.inst.level end
      return (a.inst.id or 0) < (b.inst.id or 0)
    end)
    lists.map, mapLevelMin = list, nil
  end
end

local function refreshPhysics(st, pz)
  local list = {}
  local b = st and st.block
  if b then
    local x0, y0, x1, y1 = lattice.groundBox(b.cx, b.cy)
    addGroundSquare(list, x0, y0, x1, y1, pz, colorF({ 1, 1, 1 }), false)
    local p, c = follow.PREPARE_DIST, follow.COMMIT_DIST
    addGroundSquare(list, b.cx - p, b.cy - p, b.cx + p, b.cy + p, pz, colorF({ 0.3, 0.8, 1 }), true)
    addGroundSquare(list, b.cx - c, b.cy - c, b.cx + c, b.cy + c, pz, colorF({ 1, 0.6, 0.2 }), true)
  end
  local tp = st and st.terrainPending
  if tp then
    local x0, y0, x1, y1 = lattice.groundBox(tp.cx, tp.cy)
    addGroundSquare(list, x0, y0, x1, y1, pz, colorF(tp.ready and { 0.4, 1, 0.4 } or { 1, 1, 0.3 }), true)
  end
  lists.physics = list
end

local function refreshRings(px, py, pz)
  local list = {}
  local z = pz + M.LIFT
  local cNear = colorF({ 0.3, 1, 0.6 })
  for _, r in ipairs(M.NEAR_RADII) do addCircle(list, px, py, r, z, cNear) end
  addCircle(list, 0, 0, rebase.distance, z, colorF({ 1, 0.3, 0.3 }))
  addCircle(list, 0, 0, max(0, rebase.distance - rebase.PREFETCH_MARGIN), z, colorF({ 1, 0.7, 0.3 }))
  local h = M.FAR_HALF
  addBox(list, px - h, py - h, px + h, py + h, z, colorF({ 0.6, 0.6, 1 }))
  lists.rings = list
end

------------------------------------------------------------------------------------------
-- drawing (every frame, from the cached lists)

local function drawList(dd, list, flash)
  for i = 1, #list do
    if drawn >= M.MAX_LINES then return end
    local s = list[i]
    dd:drawLine(s[1], s[2], (flash and s[4]) or s[3])
    drawn = drawn + 1
  end
end

local function drawProbe(dd, st)
  local pl = st and st.player
  if not pl then return end
  local exact = exactLevel()
  local gz = groundZ(pl.x, pl.y, pl.z) or pl.z
  local inst = idx and idx:at(pl.x, pl.y, function(i) return i.shown and not i.doomed and not i.failed end)
  local miss, txt
  if not inst then
    miss, txt = true, 'no tile'
  else
    local off = inst.off or 0
    miss = inst.level ~= exact or abs(off) > M.PROBE_TOL
    txt = string.format('L%d  off %.2f m%s', inst.level, off, inst.level ~= exact and ('  (exact L' .. exact .. ')') or '')
  end
  local c = colorF(miss and { 1, 0.1, 0.1 } or { 0.2, 1, 0.3 })
  dd:drawLine(vec3(pl.x, pl.y, gz - 2), vec3(pl.x, pl.y, gz + 4), c)
  drawn = drawn + 1
  if inst then
    local vz = gz + (inst.off or 0)
    dd:drawLine(vec3(pl.x - 1, pl.y, vz), vec3(pl.x + 1, pl.y, vz), c)
    drawn = drawn + 1
  end
  if dd.drawTextAdvanced and rawget(_G, 'String') then
    dd:drawTextAdvanced(vec3(pl.x, pl.y, gz + 4.3), String(txt), ColorF(1, 1, 1, 1), true, false, ColorI(0, 0, 0, 192))
  end
end

local function drawOverlays(st)
  local dd = rawget(_G, 'debugDrawer')
  drawn = 0
  if not dd then return end
  if on.probe then drawProbe(dd, st) end
  if on.physics then drawList(dd, lists.physics) end
  if on.outlines then drawList(dd, lists.outlines) end
  if on.loading then
    drawList(dd, lists.loading, floor(now * 4) % 2 == 1)
    if dd.drawTextAdvanced and rawget(_G, 'String') then
      for _, l in ipairs(lists.labels) do
        dd:drawTextAdvanced(l.pos, String(l.text), ColorF(1, 1, 1, 1), true, false, ColorI(160, 60, 0, 220))
      end
    end
  end
  if on.rings then drawList(dd, lists.rings) end
end

------------------------------------------------------------------------------------------
-- stats, graphs and the event log

local function sampleFrame(dtReal, dtSim)
  local t = clock()
  if lastFrameT then
    local ms = t - lastFrameT
    frameRing.n = frameRing.n + 1
    local i = (frameRing.n - 1) % M.FRAME_RING + 1
    frameRing.t[i], frameRing.ms[i] = now, ms
    if ms > graphs.msMax then graphs.msMax = ms end
  end
  lastFrameT = t
  if dtReal and dtReal > 0 and dtSim and dtSim > 0 then simSum, realSum = simSum + dtSim, realSum + dtReal end
end

local function framePercentiles()
  local t = {}
  for i = 1, min(frameRing.n, M.FRAME_RING) do
    if now - frameRing.t[i] <= M.STATS_WINDOW_S then t[#t + 1] = frameRing.ms[i] end
  end
  table.sort(t)
  local n = #t
  local function pct(p) return n > 0 and t[max(1, math.ceil(p * n))] or 0 end
  local sum = 0
  for i = 1, n do sum = sum + t[i] end
  return pct(0.5), pct(0.95), n > 0 and t[n] or 0, sum > 0 and n / (sum / 1000) or 0
end

local function fmt(v, f) return v ~= nil and string.format(f or '%.1f', v) or '-' end

local function refreshStats(st, gov)
  pruneIndex()
  local fs = farfield.stats()
  local c = farfield.counters()
  local dt = max(1e-3, now - stats.t)
  local impS = stats.imports and (fs.imports - stats.imports) / dt or 0
  local delS = stats.removes and (counts.removes - stats.removes) / dt or 0
  stats.imports, stats.removes, stats.t = fs.imports, counts.removes, now
  local p50, p95, mx, fps = framePercentiles()
  local L = {}
  L[#L + 1] = string.format('fps %.0f   frame p50 %.1f / p95 %.1f / max %.1f ms (%d s)   sim %.3f', fps, p50, p95, mx,
    M.STATS_WINDOW_S, realSum > 0 and simSum / realSum or 1)
  simSum, realSum = 0, 0
  if gov then
    local g = gov:snapshot()
    L[#L + 1] = string.format('governor b %.2f / %.2f ms%s   base %s   est tex256 %s  tex512 %s  material %s  delete %s',
      g.b, g.target, g.loading and ' (loading)' or '', fmt(g.base), fmt(g.estCdaeTex256, '%.2f'),
      fmt(g.estCdaeTex512, '%.2f'), fmt(g.estMaterial, '%.2f'), fmt(gov.est and gov.est.delete, '%.2f'))
  end
  L[#L + 1] = string.format('imports %.1f/s   deletes %.1f/s   queue %d   waiting groups %d   crossovers %d%s',
    impS, delS, fs.queued, fs.waitingGroups, fs.transitions, farfield.isFrozen() and '   LOD FROZEN' or '')
  L[#L + 1] = string.format('objects: live %d  preloaded %d  old %d  deleting %d   materials %d   indexed %d',
    fs.live, fs.preloaded, fs.old, fs.deleting, fs.materials, idx and idx:count() or 0)
  L[#L + 1] = string.format('pops %d   cap waits %d   failed %d   exactMiss %d / %d frames (under: %s, exact L%d)',
    c.pops, c.capWaits, c.failed, fs.exactMiss, fs.exactFrames, fs.underLevel and ('L' .. fs.underLevel) or '-',
    fs.exactLevel)
  if st then
    local pl = st.player
    L[#L + 1] = string.format('server backlog %s   anchor #%s%s', st.serverBacklog and tostring(st.serverBacklog) or '-',
      tostring(st.anchorId), st.anchor and string.format(' (%.5f, %.5f)', st.anchor.lat, st.anchor.lon) or '')
    L[#L + 1] = string.format('rebase in %s m   last follow %s ms (%d)   last rebase %s ms (%d)',
      pl and string.format('%.0f', rebase.distance - pl.dist) or '-', fmt(st.lastFollowMs, '%.0f'), st.followCount or 0,
      fmt(st.lastRebaseMs, '%.0f'), st.rebaseCount or 0)
    L[#L + 1] = 'status: ' .. tostring(st.status or '')
  end
  L[#L + 1] = string.format('dev tools %.3f ms/frame   %d debug lines', cost.ema, drawn)
  stats.lines = L
end

local function sampleGraphs(st)
  local fs = farfield.counters()
  local i = graphs.off
  local imp = graphs.lastImports and (fs.imports - graphs.lastImports) / M.GRAPH_S or 0
  graphs.lastImports = fs.imports
  local main = farfield.queuedTiles()
  graphs.ms[i], graphs.queue[i], graphs.imports[i] = graphs.msMax, #main, imp
  graphs.speed[i] = st and st.player and st.player.speed or 0
  graphs.msMax = 0
  graphs.off = (i + 1) % M.GRAPH_N
  graphs.n = min(graphs.n + 1, M.GRAPH_N)
end

local function pollEvents(st)
  local c = farfield.counters()
  local cur = { follow = st and st.followCount or 0, rebase = st and st.rebaseCount or 0, status = st and st.status or '',
    pops = c.pops, capWaits = c.capWaits, failed = c.failed }
  local p = evPrev
  evPrev = cur
  if not p then return end
  if cur.follow > p.follow and st.block then
    logEvent(string.format('terrain commit -> (%d, %d), %s ms', st.block.cx, st.block.cy, fmt(st.lastFollowMs, '%.0f')))
  end
  if cur.rebase > p.rebase then
    logEvent(string.format('rebase -> anchor #%s, %s ms', tostring(st.anchorId), fmt(st.lastRebaseMs, '%.0f')))
  end
  if cur.pops > p.pops then logEvent(string.format('%d swap(s) popped (crossover cap, far)', cur.pops - p.pops)) end
  if cur.capWaits > p.capWaits then logEvent('near swap waited for a crossover slot') end
  if cur.failed > p.failed then logEvent(string.format('%d tile import(s) failed', cur.failed - p.failed)) end
  if cur.status ~= p.status and cur.status ~= '' then logEvent(cur.status) end
end

------------------------------------------------------------------------------------------
-- imgui panels

local im = nil
local ptrs = {}
local function text(s) im.TextUnformatted(tostring(s)) end

local u32cache = {}
local function u32(rgb, a)
  local k = rgb[1] .. ',' .. rgb[2] .. ',' .. rgb[3] .. ',' .. a
  local v = u32cache[k]
  if not v then
    v = im.GetColorU322(im.ImVec4(rgb[1], rgb[2], rgb[3], a))
    u32cache[k] = v
  end
  return v
end

local function boolPtr(name, value)
  local p = ptrs[name]
  if not p then p = im.BoolPtr(value); ptrs[name] = p end
  p[0] = value and true or false
  return p
end

local function floatPtr(name, value)
  local p = ptrs[name]
  if not p then p = im.FloatPtr(value); ptrs[name] = p end
  return p
end

local function describe(inst)
  local anchor = farfield.anchorId()
  return string.format('%s  L%d  %s  %.0f m  err %.2f m  z %.1f%s%s%s', inst.key, inst.level, M.tileState(inst, anchor),
    inst.x1 - inst.x0, inst.err or 0, inst.z or 0, inst.tex and ('  tex ' .. inst.tex .. ' px') or '',
    inst.importMs and string.format('  import %.2f ms', inst.importMs) or '',
    inst.anchor ~= anchor and ('  anchor #' .. tostring(inst.anchor)) or '')
end

local function drawMap(st)
  local pl = st and st.player
  local cam = st and st.cam
  local px, py = pl and pl.x or (cam and cam.x) or 0, pl and pl.y or (cam and cam.y) or 0
  local size = M.MAP_PX
  local R = params.mapKm * 1000
  local s = (size / 2) / R
  local kmPtr = floatPtr('mapKm', params.mapKm)
  if im.SliderFloat('Map radius (km)', kmPtr, 1, 80, '%.0f km') then M.setParam('mapKm', kmPtr[0])
  elseif not im.IsItemActive() then kmPtr[0] = params.mapKm end
  local p0 = im.GetCursorScreenPos()
  im.InvisibleButton('##bngEarthDevMap', im.ImVec2(size, size))
  local clicked = im.IsItemClicked(0)
  local dl = im.GetWindowDrawList()
  local x0, y0 = p0.x, p0.y
  local cx, cy = x0 + size / 2, y0 + size / 2
  local function sx(x) return cx + (x - px) * s end
  local function sy(y) return cy - (y - py) * s end
  im.ImDrawList_PushClipRect(dl, im.ImVec2(x0, y0), im.ImVec2(x0 + size, y0 + size), true)
  im.ImDrawList_AddRectFilled(dl, im.ImVec2(x0, y0), im.ImVec2(x0 + size, y0 + size), u32({ 0.05, 0.05, 0.06 }, 0.9))
  for _, e in ipairs(lists.map) do
    local inst = e.inst
    if not inst.doomed and not inst.failed then
      local ax, ay = sx(inst.x0), sy(inst.y1)
      local bx, by = max(ax + 1, sx(inst.x1)), max(ay + 1, sy(inst.y0))
      local a, b = im.ImVec2(ax, ay), im.ImVec2(bx, by)
      im.ImDrawList_AddRectFilled(dl, a, b, u32(e.rgb, 0.45))
      im.ImDrawList_AddRect(dl, a, b, u32(e.rgb, 0.9))
    end
  end
  for _, inst in ipairs(lists.queued) do
    if not inst.doomed then
      im.ImDrawList_AddRect(dl, im.ImVec2(sx(inst.x0), sy(inst.y1)), im.ImVec2(sx(inst.x1), sy(inst.y0)),
        u32(M.STATE_RGB.queued, 1), 0, 0, 2)
    end
  end
  local b = st and st.block
  if b then
    local bx0, by0, bx1, by1 = lattice.groundBox(b.cx, b.cy)
    im.ImDrawList_AddRect(dl, im.ImVec2(sx(bx0), sy(by1)), im.ImVec2(sx(bx1), sy(by0)), u32({ 1, 1, 1 }, 1), 0, 0, 2)
  end
  im.ImDrawList_AddCircle(dl, im.ImVec2(sx(0), sy(0)), rebase.distance * s, u32({ 1, 0.3, 0.3 }, 1), 96, 1.5)
  for _, r in ipairs(M.NEAR_RADII) do
    im.ImDrawList_AddCircle(dl, im.ImVec2(cx, cy), r * s, u32({ 0.3, 1, 0.6 }, 1), 48, 1)
  end
  -- the car: an arrow along the velocity (or the camera's heading when parked)
  local hx, hy = 0, 1
  if pl and (pl.speed or 0) > 0.5 then hx, hy = pl.vx / pl.speed, pl.vy / pl.speed
  elseif cam then local l = sqrt(cam.fx * cam.fx + cam.fy * cam.fy); if l > 1e-6 then hx, hy = cam.fx / l, cam.fy / l end end
  local function pt(f, sd) return im.ImVec2(cx + hx * f + hy * sd, cy - (hy * f - hx * sd)) end
  im.ImDrawList_AddTriangleFilled(dl, pt(9, 0), pt(-6, 5), pt(-6, -5), u32({ 1, 1, 1 }, 1))
  im.ImDrawList_PopClipRect(dl)
  if clicked then
    local m = im.GetMousePos()
    local wx, wy = px + (m.x - cx) / s, py - (m.y - cy) / s
    selected = idx and idx:at(wx, wy, function(i) return not i.doomed and not i.failed end) or nil
    if not selected then
      for _, inst in ipairs(lists.queued) do
        if not inst.doomed and wx >= inst.x0 and wx < inst.x1 and wy >= inst.y0 and wy < inst.y1 then selected = inst; break end
      end
    end
  end
  text(string.format('%d tiles drawn%s', #lists.map,
    (mapLevelMin or -99) > -2 and string.format(' (levels below L%d too small at this zoom)', mapLevelMin) or ''))
  if selected then
    im.PushTextWrapPos(0)
    text(describe(selected))
    im.PopTextWrapPos()
  else
    text('Click a tile to inspect it.')
  end
end

local function drawGraphs()
  local n = graphs.n
  local off = n < M.GRAPH_N and 0 or graphs.off
  local w = im.ImVec2(M.MAP_PX + 120, 50)
  local function plot(label, arr, fmtv)
    local last = arr[(graphs.off - 1) % M.GRAPH_N] or 0
    im.PlotLines1('##' .. label, arr, n, off, string.format(fmtv, label, last), 0, nil, w)
  end
  if n == 0 then text('collecting...') return end
  plot('frame ms (max per 0.25 s)', graphs.ms, '%s %.1f')
  plot('queue', graphs.queue, '%s %.0f')
  plot('imports/s', graphs.imports, '%s %.1f')
  plot('speed m/s', graphs.speed, '%s %.1f')
end

local function drawControls(st)
  for _, p in ipairs(M.PRESET_ORDER) do
    if im.Button(p .. '##preset') then M.preset(p) end
    im.SameLine()
  end
  text('presets')
  local function check(name, label)
    local ptr = boolPtr('on_' .. name, on[name])
    if im.Checkbox(label .. '##' .. name, ptr) then M.set(name, ptr[0]) end
  end
  for i, n in ipairs(M.OVERLAYS) do
    check(n, n)
    if i % 4 ~= 0 then im.SameLine() end
  end
  im.NewLine()
  for i, n in ipairs(M.PANELS) do
    check(n, n)
    if i < #M.PANELS then im.SameLine() end
  end
  local kmPtr = floatPtr('outlineKm', params.outlineKm)
  if im.SliderFloat('Outline radius (km)', kmPtr, 0.5, 20, '%.1f km') then M.setParam('outlineKm', kmPtr[0])
  elseif not im.IsItemActive() then kmPtr[0] = params.outlineKm end
  local fz = boolPtr('frozen', farfield.isFrozen())
  if im.Checkbox('Freeze LOD (swaps wait)', fz) then M.freeze(fz[0]) end
  im.SameLine()
  if im.Button(forced and 'Rebase pending...' or 'Force rebase now') and not forced then M.forceRebase(st) end
  local core = rawget(_G, 'beamngEarth_core')
  if core then
    -- detail mode: before / after captures (the user, 2026-10-12): raw = the aerial photo on the DEM tiles,
    -- generated = the drawn near block (photo base, surface textures, edited ground)
    if core.nearVisualMode and core.nearVisualMode() == 'detail' then
      local rv = boolPtr('rawView', core.rawView())
      if im.Checkbox('Raw data view (satellite photo + DEM instead of the generated near ground)', rv) then
        rv[0] = core.setRawView(rv[0])
      end
    end
    local gov = core._governor and core._governor()
    if gov then
      local bp = floatPtr('budget', gov.target)
      im.SliderFloat('Streaming budget (ms/frame)', bp, 1, 4, '%.1f')
      if im.IsItemDeactivatedAfterEdit() then
        bp[0] = core.setFarBudget(bp[0])
      elseif not im.IsItemActive() then
        bp[0] = gov.target
      end
    end
    local rp = floatPtr('rebaseKm', rebase.distance / 1000)
    im.SliderFloat('Rebase distance (km)', rp, rebase.MIN_DISTANCE / 1000, rebase.MAX_DISTANCE / 1000, '%.1f km')
    if im.IsItemDeactivatedAfterEdit() and not forced then
      rp[0] = core.setRebaseDistance(rp[0] * 1000) / 1000
    elseif not im.IsItemActive() then
      rp[0] = rebase.distance / 1000
    end
    if im.Button('Reconnect / resync') then core.reconnect() end
  end
  local sky = rawget(_G, 'beamngEarth_sky')
  if sky and sky.getMode then
    im.SameLine()
    local mode = sky.getMode()
    if im.Button(mode == 'real' and 'Sky: real time (switch to manual)' or 'Sky: manual (switch to real time)') then
      sky.setMode(mode == 'real' and 'manual' or 'real')
    end
    local env = rawget(_G, 'core_environment')
    if mode == 'manual' and env and env.getTimeOfDay and env.setTimeOfDay then
      local tod = env.getTimeOfDay()
      local tp = floatPtr('tod', tod and tod.time or 0)
      if im.SliderFloat('Time of day (0 = noon)', tp, 0, 1, '%.3f') then
        env.setTimeOfDay({ time = tp[0] }) -- through core_environment: the pause menu shows it too
      elseif not im.IsItemActive() and tod then
        tp[0] = tod.time or 0
      end
    end
  end
end

local function drawLog()
  if evN == 0 then text('no events yet') return end
  for k = 0, min(evN, M.LOG_N) - 1 do
    local e = events[(evN - 1 - k) % M.LOG_N + 1]
    text(string.format('%7.1f s  %s', e.t, e.text))
  end
end

local winOpen = nil
local function drawPanels(st)
  im = im or rawget(_G, 'ui_imgui')
  if not im then return end
  winOpen = winOpen or im.BoolPtr(true)
  winOpen[0] = true
  im.SetNextWindowSize(im.ImVec2(480, 0), im.Cond_FirstUseEver)
  if im.Begin('BeamNG.Earth Dev', winOpen) then
    if on.controls and im.CollapsingHeader1('Controls', im.TreeNodeFlags_DefaultOpen) then drawControls(st) end
    if on.stats and im.CollapsingHeader1('Stats', im.TreeNodeFlags_DefaultOpen) then
      for _, l in ipairs(stats.lines) do text(l) end
    end
    if on.graphs and im.CollapsingHeader1('Graphs (60 s)', im.TreeNodeFlags_DefaultOpen) then drawGraphs() end
    if on.map and im.CollapsingHeader1('Mini-map', im.TreeNodeFlags_DefaultOpen) then drawMap(st) end
    if on.log and im.CollapsingHeader1('Log', im.TreeNodeFlags_DefaultOpen) then drawLog() end
  end
  im.End()
  if not winOpen[0] then
    for _, n in ipairs(M.PANELS) do M.set(n, false) end -- closing the window closes the panels
  end
end

------------------------------------------------------------------------------------------
-- per frame

-- One frame of dev work. st: the core's state (beamngEarth_core._state(), nil = none), gov: its
-- governor. The extension hook calls this only while something is on (or tints are restoring).
function M.frame(dtReal, dtSim, st, gov)
  local t0 = clock()
  dtReal = dtReal or 0
  now = now + dtReal
  processTint(gov)
  if not active then
    busy = tintPending()
    return
  end
  local pl = st and st.player
  local cam = st and st.cam
  local px, py, pz = 0, 0, 0
  if pl then px, py, pz = pl.x, pl.y, pl.z elseif cam then px, py, pz = cam.x, cam.y, cam.z end

  if on.stats or on.graphs then sampleFrame(dtReal, dtSim) end
  if on.log then pollEvents(st) end
  if forced and ((st and (st.rebaseCount or 0) > forced.count) or now - forced.t > M.FORCE_TIMEOUT_S) then
    rebase.distance = forced.prev
    forced = nil
  end

  local curAnchor = farfield.anchorId()
  local curRebase = st and st.rebaseCount or nil
  if lastRebaseCount == nil then lastRebaseCount = curRebase end
  if curAnchor ~= lastAnchor or (curRebase ~= nil and curRebase ~= lastRebaseCount) then
    lastRebaseCount = curRebase
    rebuild()
    lists.map, lists.outlines, lists.loading, lists.labels, lists.queued = {}, {}, {}, {}, {}
    refreshT, physT, ringKey = huge, huge, nil
  end

  refreshT = refreshT + dtReal
  if (on.outlines or on.loading or on.map) and refreshT >= M.REFRESH_S then
    refreshT = 0
    refreshTiles(px, py, st)
  end
  physT = physT + dtReal
  if on.physics and physT >= M.PHYS_REFRESH_S then
    physT = 0
    refreshPhysics(st, pz)
  end
  if on.rings then
    local key = floor(px / 10) .. ',' .. floor(py / 10) .. ',' .. floor(pz) .. ',' .. rebase.distance
    if key ~= ringKey then
      ringKey = key
      refreshRings(px, py, pz)
    end
  end
  drawOverlays(st)

  if on.stats then
    stats.acc = (stats.acc or 0) + dtReal
    if stats.acc >= M.STATS_S then stats.acc = 0; refreshStats(st, gov) end
  end
  if on.graphs then
    graphs.t = graphs.t + dtReal
    if graphs.t >= M.GRAPH_S then graphs.t = 0; sampleGraphs(st) end
  end
  if on.stats or on.graphs or on.map or on.controls or on.log then drawPanels(st) end

  cost.last = clock() - t0
  cost.ema = cost.ema + 0.05 * (cost.last - cost.ema)
end

function M.onUpdate(dtReal, dtSim)
  if not busy then return end
  local core = rawget(_G, 'beamngEarth_core')
  local st = core and core._state and core._state() or nil
  local gov = core and core._governor and core._governor() or nil
  M.frame(dtReal, dtSim, st, gov)
end

-- A new level forgets every tile (farfield.forget calls onReset); tinted materials of the old
-- level are gone with it.
function M.onClientEndMission()
  tintState, tintQ, tqH, tqT, tintQueued, matLevel = {}, {}, 1, 0, {}, {}
  busy = active
end

function M.onExtensionUnloaded()
  M.preset('off')
  -- restore what can still be restored right away (an unload gets no more frames)
  while tintPending() do processTint(nil) end
  farfield.setWatcher(nil)
end

-- for tests
function M._busy() return busy end
function M._index() return idx end
function M._tintState(name) return tintState[name] end
function M._tintPending() return tqT - tqH + 1 end
function M._lists() return lists end
function M._drawn() return drawn end
function M._events() return events, evN end

return M
