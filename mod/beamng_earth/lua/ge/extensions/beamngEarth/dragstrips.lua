-- Drag strips run by the game's own drag system (gameplay/drag/*: tree, staging, ET and trap speed boards, timeslip,
-- the drag UI). The road tiles carry their strips (server/dragstrips.py, `drag`): per strip two lanes' stage and finish
-- points, the shutdown's end. Per strip this builds what West Coast USA's Alder strip has as level objects, by the names
-- the stock display.lua and saveSystem.lua look up (<stripId>_amber1_1, <stripId>_wp_stage_1, ...), from Alder's own
-- layout (dragtemplate.lua) and the global shapes in /art/shapes/race/dragstrip and /art/shapes/quarter_mile_display;
-- writes the strip's .strip.json / .dragSettings.json to the user folder, loads them through the stock
-- gameplay_drag_saveSystem.loadDragStripData, and hands the stage triggers (gameplay_drag_poi.createStagePoi) to the
-- POI list (core.lua onGetRawPoiListForLevel). The stock system's own scan of levels/<level>/dragstrips is not used:
-- it is cached once a level and in one fixed frame.
-- Single player only: nothing is built in a BeamMP session. No AI opponent (the waypoints are not in the navgraph).
-- A rebase waits while a run is on (holdRebase, core.lua); otherwise the strips go and are built again from the new
-- anchor's tiles (positions are anchor-frame).
local M = {}

M.QUARTER = 402.336
M.EIGHTH = 201.168
M.ALDER_FINISH = 402.4        -- the template's finish-line objects' y (Alder's)
M.ALDER_HALF = 4.4            -- the template's lane half spacing
M.DIR = '/settings/beamngEarth/dragstrips/'
M.CLEAR_S = 0.5
M.enabled = true
M.now = os.clock

local strips = {}             -- id -> { rec, keys = { [key] = true }, objs = { ids }, data, made, stale }
local queue = {}
local poisDirty, lastClear = false, -math.huge

local function inMP()
  local ok, mp = pcall(require, 'beamngEarth/beammp')
  return ok and mp and mp.inSession and mp.inSession() or false
end

local function v3(p) return vec3(p[1], p[2], p[3]) end

-- the track's height under (x, y) near z: a ray down onto the road mesh, else z
local function groundZ(x, y, z)
  if castRayStatic then
    local ok, d = pcall(castRayStatic, vec3(x, y, z + 4), vec3(0, 0, -1), 12)
    if ok and type(d) == 'number' and d < 12 then return z + 4 - d end
  end
  return z
end

-- The strip's frame from its record: origin the midpoint of the two stage points, F down the strip, R right, U up.
function M.frameOf(rec)
  local s1, s2, f1 = rec.lanes[1].stage, rec.lanes[2].stage, rec.lanes[1].finish
  local o = { (s1[1] + s2[1]) / 2, (s1[2] + s2[2]) / 2, (s1[3] + s2[3]) / 2 }
  local fx, fy = f1[1] - s1[1], f1[2] - s1[2]
  local l = math.sqrt(fx * fx + fy * fy)
  fx, fy = fx / l, fy / l
  return { o = o, f = { fx, fy }, r = { fy, -fx }, dist = rec.dist or M.QUARTER, half = rec.half or M.ALDER_HALF,
           len = rec['end'] and ((rec['end'][1] - o[1]) * fx + (rec['end'][2] - o[2]) * fy) or (rec.dist or M.QUARTER) }
end

-- strip-frame (x right, y down, z up) to world
function M.toWorld(fr, x, y, z)
  return { fr.o[1] + x * fr.r[1] + y * fr.f[1], fr.o[2] + x * fr.r[2] + y * fr.f[2], fr.o[3] + z }
end

local function dirWorld(fr, d)
  return vec3(d[1] * fr.r[1] + d[2] * fr.f[1], d[1] * fr.r[2] + d[2] * fr.f[2], d[3])
end

-- A template object's place on this strip: finish-line objects moved to the strip's finish, boards out to its lanes'
-- spacing; nil past the shutdown's end.
function M.placeOf(fr, t)
  local x, y, z = t.p[1], t.p[2], t.p[3]
  if y > 300 then y = y - M.ALDER_FINISH + fr.dist end
  if y > fr.len + 5 then return nil end
  if math.abs(x) > 2 then x = x * fr.half / M.ALDER_HALF end
  return x, y, z
end

local function quatFD(f, u)
  local q = quatFromDir(f, u)
  return q
end

local HIDDEN = { prestage = 1, stage = 1, amber1 = 1, amber2 = 1, amber3 = 1, green = 1, red = 1, blueLight = 1,
                 winTimeboard = 1, winDriver = 1 }

local function isHidden(n)
  local head = n:match('^(%a+%d?)')
  return HIDDEN[head] ~= nil or n:find('^display') ~= nil
end

local function zone(name, pts)
  return { name = name, color = { 1, 1, 1 }, oldId = -1, vertices = pts,
           top = { active = false, normal = { 0, 0, 1 }, pos = { 0, 0, 10 } },
           bot = { active = false, normal = { 0, 0, -1 }, pos = { 0, 0, -10 } },
           customFields = { names = {}, tags = {}, types = {}, values = {} } }
end

-- the strip's .strip.json table (stock format: West Coast USA's alderStrip_main.strip.json)
function M.stripJson(rec, fr, sid, zOf)
  local fq = quatFD(vec3(fr.f[1], fr.f[2], 0), vec3(0, 0, 1))
  local rot = { x = fq.x, y = fq.y, z = fq.z, w = fq.w }
  local function wp(lane, kind, name, y, mode, speed)
    local x = (lane == 1 and 1 or -1) * fr.half
    local p = M.toWorld(fr, x, y, 0)
    return { id = sid .. '_lane' .. lane .. '_' .. kind, name = name, type = kind,
             transform = { position = { x = p[1], y = p[2], z = zOf(p, y) }, rotation = rot, scale = { x = 1, y = 1, z = 1 } },
             waypoint = { mode = mode, speed = speed } }
  end
  local function rect(x0, x1, y0, y1)
    local out = {}
    for _, c in ipairs({ { x0, y0 }, { x1, y0 }, { x1, y1 }, { x0, y1 } }) do
      local p = M.toWorld(fr, c[1], c[2], 0)
      out[#out + 1] = { p[1], p[2], zOf(p, c[2]) }
    end
    return out
  end
  local yEnd = math.max(fr.dist + 10, fr.len)
  local lanes = {}
  for lane = 1, 2 do
    local sgn = lane == 1 and 1 or -1
    lanes[lane] = {
      id = sid .. '_lane' .. lane, laneOrder = lane,
      shortName = lane == 1 and 'Right' or 'Left', name = lane == 1 and 'Right Lane' or 'Left Lane',
      longName = lane == 1 and 'ui.drag.lane.right' or 'ui.drag.lane.left', color = lane == 1 and 'green' or 'blue',
      zone = zone(lane == 1 and 'Right Lane Zone' or 'Left Lane Zone',
        rect(sgn > 0 and 0 or -2 * fr.half, sgn > 0 and 2 * fr.half or 0, -30, fr.dist + 10)),
      waypoints = { wp(lane, 'spawn', 'drag_' .. lane .. '_spawn', -21.7, 'limit', 20),
                    wp(lane, 'stage', 'drag_' .. lane .. '_stage', 0, 'limit', 1),
                    wp(lane, 'endLine', 'drag_' .. lane .. '_endLine', fr.dist, 'set', 800) },
    }
  end
  return { id = sid, name = rec.name or 'Drag strip', description = 'BeamNG.Earth: OpenStreetMap drag strip',
           stripZone = zone('Strip Zone', rect(-2 * fr.half - 8, 2 * fr.half + 8, -45, yEnd + 20)), lanes = lanes }
end

-- the .dragSettings.json table (Alder's timers; an eighth-mile strip times to 1/8)
function M.settingsJson(rec, stripPath)
  local eighth = (rec.dist or M.QUARTER) < M.QUARTER - 1
  local t = {
    { id = 'reactionTime', label = 'Reaction Time', labelKey = 'ui.drag.winnerBy.reactionTime', shortLabel = 'R/T', type = 'distanceTimer', distance = 0.178, important = false },
    { id = 'time_60', label = '60 ft', labelKey = 'ui.drag.winnerBy.time60', shortLabel = "60'", type = 'distanceTimer', distance = 18.288, important = false },
    { id = 'time_330', label = '330 ft', labelKey = 'ui.drag.winnerBy.time330', shortLabel = "330'", type = 'distanceTimer', distance = 100.584, important = false },
    { id = 'time_1_8', label = '1/8 mile', labelKey = 'ui.drag.winnerBy.time18', shortLabel = '1/8', type = 'distanceTimer', distance = 201.168, important = eighth },
    { id = 'velAt_1_8', label = 'Speed at 1/8 mile', labelKey = 'ui.drag.winnerBy.speedAt18', type = 'velocity', distance = 201.168, important = false },
  }
  if not eighth then
    table.insert(t, 5, { id = 'time_1000', label = '1000 ft', labelKey = 'ui.drag.winnerBy.time1000', shortLabel = "1000'", type = 'distanceTimer', distance = 304.8, important = false })
    table.insert(t, 6, { id = 'time_1_4', label = '1/4 mile', labelKey = 'ui.drag.winnerBy.time14', shortLabel = '1/4', type = 'distanceTimer', distance = 402.336, important = true })
    t[#t + 1] = { id = 'velAt_1_4', label = 'Speed at 1/4 mile', labelKey = 'ui.drag.winnerBy.speedAt14', type = 'velocity', distance = 402.336, important = false }
  end
  return { id = rec.id, canBeReseted = true, canBeTeleported = true, context = 'freeroam', dragType = 'headsUpRace',
           stripId = stripPath, timers = t,
           phases = { { name = 'stage', dependency = true, startedOffset = 0 }, { name = 'countdown', dependency = true, startedOffset = 0 },
                      { name = 'race', dependency = false, startedOffset = 0 }, { name = 'stop', dependency = false, startedOffset = 0 } },
           prefabs = { christmasTree = { isUsed = true, treeType = '.500' }, displaySign = { isUsed = true },
                       paths = { isUsed = false }, decorations = { isUsed = false } } }
end

local function deleteObjs(st)
  for _, id in ipairs(st.objs or {}) do
    local o = scenetree.findObjectById(id)
    if o then o:delete() end
  end
  st.objs, st.made, st.data = {}, false, nil
end

local function ourRunOn(id)
  local core = rawget(_G, 'gameplay_drag_core')
  if not core or not core.getDragIsStarted or not core.getDragIsStarted() then return false end
  local d = core.getData and core.getData()
  local sid = d and d.strip and d.strip.id
  if id then return sid == id .. '_main' end
  return type(sid) == 'string' and sid:find('^earth_') ~= nil
end

-- Build one strip: objects, files, stock data.
local function build(id, st)
  local rec = st.rec
  local fr = M.frameOf(rec)
  local sid = id .. '_main'
  local zs, zf = {}, {}
  for l = 1, 2 do
    zs[l] = groundZ(rec.lanes[l].stage[1], rec.lanes[l].stage[2], rec.lanes[l].stage[3])
    zf[l] = groundZ(rec.lanes[l].finish[1], rec.lanes[l].finish[2], rec.lanes[l].finish[3])
  end
  local z0, z1 = (zs[1] + zs[2]) / 2, (zf[1] + zf[2]) / 2
  fr.o[3] = z0
  local function zOf(p, y)          -- along-strip height, refined by a ray where the mesh is
    local t = math.max(0, math.min(1, y / fr.dist))
    local z = z0 + (z1 - z0) * t
    return groundZ(p[1], p[2], z)
  end
  local group = createObject('SimGroup')
  group.canSave = false
  group:registerObject('bne_drag_' .. id)
  scenetree.MissionGroup:addObject(group)
  st.objs = { group:getID() }
  local tpl = require('beamngEarth/dragtemplate')
  for _, t in ipairs(tpl) do
    if t.c == 'TSStatic' and t.s then
      local x, y, z = M.placeOf(fr, t)
      if x then
        local p = M.toWorld(fr, x, y, 0)
        local q = quatFD(dirWorld(fr, t.f), dirWorld(fr, t.u))
        local o = createObject('TSStatic')
        o:setField('shapeName', 0, t.s)
        o:setField('collisionType', 0, 'None')
        o:setField('decalType', 0, 'None')
        o.canSave = false
        o:setPosRot(p[1], p[2], zOf(p, y) + z, q.x, q.y, q.z, q.w)
        if t.sc then o:setScale(vec3(t.sc[1], t.sc[2], t.sc[3])) end
        o:registerObject(sid .. '_' .. t.n)
        group:addObject(o)
        if isHidden(t.n) then o:setHidden(true) end
        st.objs[#st.objs + 1] = o:getID()
      end
    end
  end
  local sj = M.stripJson(rec, fr, sid, zOf)
  for l, lane in ipairs(sj.lanes) do
    for _, w in ipairs(lane.waypoints) do
      local o = createObject('BeamNGWaypoint')
      o.canSave = false
      local p = w.transform.position
      o:setPosition(vec3(p.x, p.y, p.z))
      o:setScale(vec3(2, 2, 2))
      o:registerObject(sid .. '_wp_' .. w.type .. '_' .. l)
      group:addObject(o)
      st.objs[#st.objs + 1] = o:getID()
    end
  end
  local stripPath = M.DIR .. id .. '_main.strip.json'
  local setPath = M.DIR .. id .. '.dragSettings.json'
  jsonWriteFile(stripPath, sj, true)
  jsonWriteFile(setPath, M.settingsJson(rec, stripPath), true)
  if not rawget(_G, 'gameplay_drag_core') then extensions.load('gameplay_drag_core') end
  local ss = rawget(_G, 'gameplay_drag_saveSystem')
  local data = ss and ss.loadDragStripData(setPath)
  if not data then error('stock drag data did not load: ' .. setPath) end
  data._fnWithoutExt, data._originFile, data.level = id, setPath, getCurrentLevelIdentifier and getCurrentLevelIdentifier()
  st.data, st.made = data, true
  poisDirty = true
end

-- A road tile's strips (anchor frame), key: the tile's key.
function M.onRows(anchorId, key, recs)
  if not M.enabled or inMP() then return end
  for _, r in ipairs(recs or {}) do
    if type(r) == 'table' and type(r.id) == 'string' and type(r.lanes) == 'table' and r.lanes[2] then
      local st = strips[r.id]
      if not st then
        st = { keys = {}, objs = {} }
        strips[r.id] = st
      end
      st.keys[key] = true
      local moved = not st.rec or math.abs(st.rec.lanes[1].stage[1] - r.lanes[1].stage[1]) > 0.05
        or math.abs(st.rec.lanes[1].stage[2] - r.lanes[1].stage[2]) > 0.05
      if moved or st.stale then
        if st.made and not ourRunOn(r.id) then deleteObjs(st) end
        st.rec, st.stale = r, false
        if not st.made then queue[#queue + 1] = r.id end
      end
    end
  end
end

function M.dropKey(key)
  for id, st in pairs(strips) do
    if st.keys[key] then
      st.keys[key] = nil
      if not next(st.keys) and not ourRunOn(id) then
        deleteObjs(st)
        strips[id] = nil
        poisDirty = true
      end
    end
  end
end

function M.update(gov)
  while #queue > 0 do
    local id = queue[1]
    local st = strips[id]
    if not st or st.made or st.stale then
      table.remove(queue, 1)
    else
      if gov and gov.try and not gov:try('drag') then break end
      local t0 = M.now()
      local ok, err = pcall(build, id, st)
      if gov and gov.charge then gov:charge('drag', (M.now() - t0) * 1000) end
      if not ok then
        log('W', 'beamngEarth', 'drag strip ' .. tostring(id) .. ': ' .. tostring(err))
        deleteObjs(st)
      end
      table.remove(queue, 1)
      break                                  -- (one strip a frame)
    end
  end
  if poisDirty and M.now() - lastClear >= M.CLEAR_S then
    poisDirty, lastClear = false, M.now()
    if gameplay_rawPois then gameplay_rawPois.clear() end
  end
end

-- core.lua: no distance rebase while a run on one of ours is on (its data is in this frame)
function M.holdRebase() return ourRunOn(nil) end

-- Origin rebase: the strips go and wait for their records in the new frame (the new anchor's tiles).
function M.transform(xf)
  for _, st in pairs(strips) do
    deleteObjs(st)
    st.stale = true
  end
  poisDirty = true
end

-- core.lua's onGetRawPoiListForLevel: a stage trigger per lane (the stock one, without the stock freeroam setting:
-- these strips exist only in Earth)
function M.pois(levelIdentifier, elements)
  local poi = rawget(_G, 'gameplay_drag_poi')
  if not M.enabled or not poi or not poi.createStagePoi then return end
  for _, st in pairs(strips) do
    if st.made and st.data and st.data.strip and st.data.strip.lanes then
      for i, lane in ipairs(st.data.strip.lanes) do
        local ok, p = pcall(poi.createStagePoi, st.data, lane, i)
        if ok and p then elements[#elements + 1] = p end
      end
    end
  end
end

function M.reset()
  for _, st in pairs(strips) do deleteObjs(st) end
  strips, queue = {}, {}
  poisDirty, lastClear = false, -math.huge
end

function M.count()
  local n = 0
  for _, st in pairs(strips) do if st.made then n = n + 1 end end
  return n
end

return M
