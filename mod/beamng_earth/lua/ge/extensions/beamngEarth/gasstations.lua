-- Gas stations with refuelling (the user, 2026-10-23: "the refueling is the main part, we can already refuel in beam so
-- I want to include it on this impressive map mod"). The road tiles carry their stations (server/gasstations.py,
-- `fuel`): West Coast USA's canopy and pump shapes as TSStatics, and per pump a BeamNGGameplayArea + a
-- BeamNGPointOfInterest, the pair the stock freeroam/gasStations.lua reads a level's pumps as
-- (west_coast_usa/facilities/facilities.facilities.json). The stations reach it through onGetRawPoiListForLevel
-- (core.lua hands that hook to M.pois): drive slowly into an area, the stock "Refuel" prompt fills the tank.
-- gameplay/markers/gasStationMarker.lua reads the areas' positions once (setup), so the POI list is gathered again
-- (gameplay_rawPois.clear) after stations come or go and after a rebase.
-- A station is kept while any road tile has it (a rebase's ghost tile and the new anchor's tile: one set of objects).
local M = {}

M.MATERIAL_FILES = { '/levels/west_coast_usa/art/shapes/buildings/main.materials.json',     -- m_gasstation_tyrannos
  '/levels/west_coast_usa/art/shapes/objects/main.materials.json',                          -- corrugation
  '/levels/east_coast_usa/art/shapes/buildings/main.materials.json',                        -- (US East's units)
  '/levels/italy/art/shapes/buildings/main.materials.json' }    -- (Europe's EV chargers: docs/research/2026-10-29-ev-chargers.md)
M.CLEAR_S = 0.5            -- the POI list gathered again at most this often (s)
M.ENERGY = { 'gasoline', 'diesel' }
M.now = os.clock
M.onCollide = nil          -- called once per spawned station (core: the collision rebuild, batched with the roads')
M.enabled = true           -- (always on: the settings page's switch was removed, the user 2026-10-25; tests turn it off)

local stations = {}        -- id -> { rec, keys = { [tile key] = true }, objs = { { id, pos, q } }, pumps, made }
local queue = {}           -- station ids to spawn
local poisDirty, lastClear = false, -math.huge

-- yaw in degrees counter-clockwise from +x (the server's), as setPosRot's quaternion (geodesy.lua beamngQuat: the
-- conjugate of the standard one)
local function yawQuat(yaw)
  local h = math.rad(yaw or 0) / 2
  return { 0, 0, -math.sin(h), math.cos(h) }
end

local function place(o, pos, q)
  o:setPosRot(pos[1], pos[2], pos[3], q[1], q[2], q[3], q[4])
end

local function deleteObjs(st)
  local any = #st.objs > 0
  for _, ob in ipairs(st.objs) do
    local o = scenetree.findObjectById(ob.id)
    if o then o:delete() end
  end
  -- its colliding pumps and canopy out of the collision too (no invisible pump left to crash into); a station half
  -- made (spawnStep) may have some in it already, by another rebuild
  if any and M.onCollide then M.onCollide() end
  st.objs, st.pumps, st.made, st.cursor = {}, {}, false, nil
end

-- a prop's identity across records of its station in one frame: its shape and its place to 0.5 m
local function propKey(p)
  return string.format('%s|%d|%d', p.shape, math.floor(p.pos[1] * 2 + 0.5), math.floor(p.pos[2] * 2 + 0.5))
end

local function makeProp(st, p)
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, p.shape)
  -- the shapes' own collision meshes (West Coast USA leaves its fuel shapes at the default, 'Collision Mesh'), not
  -- their visible triangles: every collidable triangle costs each global collision rebuild (CLAUDE.md)
  o:setField('collisionType', 0, p.collide and 'Collision Mesh' or 'None')
  o:setField('decalType', 0, 'None')
  o.canSave = false
  local s = p.scale or { 1, 1, 1 }
  o:setScale(vec3(s[1], s[2], s[3]))
  local q = yawQuat(p.yaw)
  place(o, p.pos, q)
  o:registerObject('')
  scenetree.MissionGroup:addObject(o)
  st.objs[#st.objs + 1] = { id = o:getID(), pos = { p.pos[1], p.pos[2], p.pos[3] }, q = q, key = propKey(p) }
end

local function makeArea(id, st, i, a)
  local an, inm = 'bne_fuel_' .. id .. '_a' .. i, 'bne_fuel_' .. id .. '_i' .. i
  local ar = createObject('BeamNGGameplayArea')
  ar.canSave = false
  ar:setScale(vec3(a.half[1], a.half[2], a.half[3]))
  local q = yawQuat(a.yaw)
  place(ar, a.pos, q)
  ar:registerObject(an)
  scenetree.MissionGroup:addObject(ar)
  local ic = createObject('BeamNGPointOfInterest')
  ic.canSave = false
  place(ic, a.icon, { 0, 0, 0, 1 })
  ic:registerObject(inm)
  scenetree.MissionGroup:addObject(ic)
  st.objs[#st.objs + 1] = { id = ar:getID(), pos = { a.pos[1], a.pos[2], a.pos[3] }, q = q }
  st.objs[#st.objs + 1] = { id = ic:getID(), pos = { a.icon[1], a.icon[2], a.icon[3] }, q = { 0, 0, 0, 1 } }
  st.pumps[#st.pumps + 1] = { an, inm }
end

-- One step of a station's spawn: its next prop, or its next refuel area with its icon (st.cursor: the steps done). A
-- big station (the Buc-ee's in New Braunfels: 354 props, 23.8 ms in one frame) so spreads over frames within the
-- governor's budget, a step a slot. Made (its POIs, one collision rebuild) after its last step. True when done.
local function spawnStep(id, st)
  local r = st.rec
  local props, areas = r.props or {}, r.areas or {}
  local k = (st.cursor or 0) + 1
  if k <= #props then
    makeProp(st, props[k])
  elseif k <= #props + #areas then
    makeArea(id, st, k - #props, areas[k - #props])
  end
  st.cursor = k
  if k < #props + #areas then return false end
  st.cursor = nil
  st.made = true
  poisDirty = true
  if M.onCollide then M.onCollide() end
  return true
end

-- Re-place a made station's objects from a record of the same station (the same props and areas in the same order,
-- in the current anchor's frame).
local function replace(st, r)
  local k = 0
  local function nxt(pos, q)
    k = k + 1
    local ob = st.objs[k]
    local o = ob and scenetree.findObjectById(ob.id)
    if o then
      ob.pos, ob.q = { pos[1], pos[2], pos[3] }, q
      place(o, pos, q)
    end
  end
  for _, p in ipairs(r.props or {}) do nxt(p.pos, yawQuat(p.yaw)) end
  for _, a in ipairs(r.areas or {}) do
    nxt(a.pos, yawQuat(a.yaw))
    nxt(a.icon, { 0, 0, 0, 1 })
  end
  poisDirty = true
end

-- A made station's new record in the same frame with another layout (a road tile sent again once a neighbour's
-- buildings arrived: a prop more or fewer): the props kept by shape and place (propKey) and re-placed, the others
-- deleted and the missing ones made at once, so nothing blinks; the refuel areas and icons (invisible) made again,
-- named in the record's order. False, touching nothing, when no prop matches (another frame: made again, as before).
local function relayout(id, st, r)
  local pool, olds = {}, 0
  for k, ob in ipairs(st.objs) do
    if ob.key then
      olds = olds + 1
      pool[ob.key] = pool[ob.key] or {}
      table.insert(pool[ob.key], k)
    end
  end
  local props = r.props or {}
  local hit = {}
  for i, p in ipairs(props) do
    local list = pool[propKey(p)]
    if list and #list > 0 then hit[i] = table.remove(list, 1) end
  end
  if next(hit) == nil and (olds > 0 or #props > 0) then return false end
  local old, changed = st.objs, false
  local keep = {}
  for _, k in pairs(hit) do keep[k] = true end
  for k, ob in ipairs(old) do
    if not keep[k] then                   -- (an unmatched prop, and every area and icon)
      local o = scenetree.findObjectById(ob.id)
      if o then o:delete() end
      if ob.key then changed = true end
    end
  end
  st.objs, st.pumps = {}, {}
  for i, p in ipairs(props) do
    local k = hit[i]
    if k then
      local ob, q = old[k], yawQuat(p.yaw)
      ob.pos, ob.q = { p.pos[1], p.pos[2], p.pos[3] }, q
      local o = scenetree.findObjectById(ob.id)
      if o then place(o, ob.pos, q) end
      st.objs[#st.objs + 1] = ob
    else
      makeProp(st, p)
      changed = true
    end
  end
  for i, a in ipairs(r.areas or {}) do makeArea(id, st, i, a) end
  poisDirty = true
  if changed and M.onCollide then M.onCollide() end
  return true
end

-- A road tile's stations (server roads_tile `fuel`).
function M.onRows(aid, key, recs)
  if type(recs) ~= 'table' or not M.enabled then return end
  for _, r in ipairs(recs) do
    if type(r) == 'table' and r.id then
      local st = stations[r.id]
      if not st then
        stations[r.id] = { rec = r, keys = { [key] = true }, objs = {}, pumps = {}, made = false }
        queue[#queue + 1] = r.id
      else
        st.keys[key] = true
        if st.made and #(st.rec.props or {}) == #(r.props or {}) and #(st.rec.areas or {}) == #(r.areas or {}) then
          replace(st, r)
        elseif st.made and relayout(r.id, st, r) then   -- another layout in this frame: the difference only
        elseif st.made then         -- another layout (the new anchor's frame): made again from this record
          deleteObjs(st)
          queue[#queue + 1] = r.id
          poisDirty = true
        elseif st.stale then        -- queued at a rebase: now it has a record in the current frame
          -- (half made with another layout: made again from this record; with the same, finished from it)
          if st.cursor and (#(st.rec.props or {}) ~= #(r.props or {}) or #(st.rec.areas or {}) ~= #(r.areas or {})) then
            deleteObjs(st)
          end
          queue[#queue + 1] = r.id
        elseif st.cursor then       -- still spawning (spawnStep), still queued: a record in this frame
          -- (with another layout: started again from it, not carried on into it; final review)
          local a, b = st.rec.props or {}, r.props or {}
          local same = #a == #b and #(st.rec.areas or {}) == #(r.areas or {})
          for i = 1, math.min(st.cursor, #a) do
            if not same then break end
            same = propKey(a[i]) == propKey(b[i])
          end
          if not same then deleteObjs(st) end
        end
        st.rec, st.stale = r, nil
      end
    end
  end
end

-- A road tile gone: its stations go with their last tile.
function M.dropKey(key)
  for id, st in pairs(stations) do
    if st.keys[key] then
      st.keys[key] = nil
      if not next(st.keys) then
        deleteObjs(st)
        stations[id] = nil
        poisDirty = true
      end
    end
  end
end

-- Once per frame: spawn within the governor's budget, a step a slot (spawnStep; the station at the queue's head until
-- it is done), then the POI list gathered again if due.
function M.update(gov)
  while #queue > 0 do
    local id = queue[1]
    local st = stations[id]
    if not st or st.made or st.stale then
      table.remove(queue, 1)              -- (gone, made, or waiting for its record in a new frame: onRows queues it again)
    else
      if not gov:try('fuel') then break end
      local t0 = M.now()
      local ok, done = pcall(spawnStep, id, st)
      if not ok then
        log('W', 'beamngEarth', 'gas station ' .. tostring(id) .. ': ' .. tostring(done))
        deleteObjs(st)
        table.remove(queue, 1)
      elseif done then
        table.remove(queue, 1)
      end
      gov:charge('fuel', (M.now() - t0) * 1000)
    end
  end
  if poisDirty and M.now() - lastClear >= M.CLEAR_S then
    poisDirty, lastClear = false, M.now()
    if gameplay_rawPois then gameplay_rawPois.clear() end
  end
end

-- Origin rebase (core.lua finishRebase): every object moved by p_new = R*p + t (as roads.transform). A station not
-- made yet waits for its record in the new frame (the new anchor's road tile): its old one would put it kilometres off.
function M.transform(xf)
  local geodesy = require('beamngEarth/geodesy')
  local bx, by, bz, bw = xf:beamngQuat()
  for _, st in pairs(stations) do
    if not st.made then st.stale = true end
    for _, ob in ipairs(st.objs) do
      local x, y, z = xf:apply(ob.pos[1], ob.pos[2], ob.pos[3])
      local qx, qy, qz, qw = geodesy.quatMul(ob.q[1], ob.q[2], ob.q[3], ob.q[4], bx, by, bz, bw)
      ob.pos, ob.q = { x, y, z }, { qx, qy, qz, qw }
      local o = scenetree.findObjectById(ob.id)
      if o then place(o, ob.pos, ob.q) end
    end
  end
  poisDirty = true
end

-- core.lua's onGetRawPoiListForLevel: the made stations as stock gas station POIs. Only where the stock module lists a
-- level's own (freeroam/gasStations.lua onGetRawPoiListForLevel): in career, or with the game's freeroam gas stations
-- setting on (toggling it gathers the list again: settings.lua, showMissionMarkersToggled).
function M.pois(levelIdentifier, elements)
  if not freeroam_gasStations or not M.enabled then return end
  local career = career_career and career_career.isActive()
  if not career and not (settings and settings.getValue('enableGasStationsInFreeroam')) then return end
  for id, st in pairs(stations) do
    if st.made and #st.pumps > 0 then
      local r = st.rec
      elements[#elements + 1] = freeroam_gasStations.formatGasStationPoi({
        id = 'bne_fuel_' .. id, type = 'gasStation',
        name = (r.name and r.name ~= '') and r.name or (r.energy and 'EV charger' or 'Gas station'),
        description = r.brand or '',
        -- (an EV charger's record says electricEnergy: the stock charge prompt and bolt icon, freeroam/gasStations.lua)
        energyTypes = r.energy or M.ENERGY, pumps = st.pumps })
    end
  end
end

-- Level end / new session: everything goes.
function M.reset()
  for _, st in pairs(stations) do deleteObjs(st) end
  stations, queue = {}, {}
  poisDirty, lastClear = false, -math.huge
end

-- The shapes of the anchor's region's stations (the server's anchor_ready fuelShapes: gasstations.style_shapes; only
-- that region's, with a Steam Deck's memory in mind, the user 2026-10-23), loaded as hidden TSStatics kept for the
-- session: a shape's first load stalls the frame (probe PP1: 625 ms for a prefab's first West Coast shapes,
-- docs/research/2026-10-11-engine-probes.md), which mid-drive at speed would be a hitch. Each shape once; another
-- region's new ones added at its first anchor (a teleport's loading, or a rebase over the region's border); made
-- again after the level is loaded again. Nothing named (gas stations off): nothing loaded.
local warm = {}            -- shape -> the prewarm TSStatic's id

function M.prewarm(shapes)
  if type(shapes) ~= 'table' or not M.enabled or not scenetree.MissionGroup then return end
  for _, shape in ipairs(shapes) do
    local id = warm[shape]
    if type(shape) == 'string' and not (id and scenetree.findObjectById(id)) then
      local ok, err = pcall(function()
        local o = createObject('TSStatic')
        o:setField('shapeName', 0, shape)
        o:setField('collisionType', 0, 'None')
        o:setField('decalType', 0, 'None')
        o.canSave = false
        o:setPosition(vec3(0, 0, -5000))
        o:registerObject('')
        o:setHidden(true)
        scenetree.MissionGroup:addObject(o)
        warm[shape] = o:getID()
      end)
      if not ok then log('W', 'beamngEarth', 'gas stations: prewarm ' .. shape .. ': ' .. tostring(err)) end
    end
  end
end

-- The night lights' places (nightlights.lua): over each made station's pump islands, under its canopy (stations were
-- dark at night, 2026-10-27). Kept per station and made again when its record changes.
M.LIGHT_UP_M = 4.6
function M.lightPoints()
  local out = {}
  for _, st in pairs(stations) do
    if st.made then
      if st.lightRec ~= st.rec then
        st.lights = {}
        for _, p in ipairs(st.rec.props or {}) do
          if type(p.shape) == 'string' and p.shape:lower():find('pump') and type(p.pos) == 'table' then
            st.lights[#st.lights + 1] = { p.pos[1], p.pos[2], p.pos[3] + M.LIGHT_UP_M }
          end
        end
        st.lightRec = st.rec
      end
      for _, l in ipairs(st.lights) do out[#out + 1] = l end
    end
  end
  return out
end

function M.count()
  local n = 0
  for _, st in pairs(stations) do if st.made then n = n + 1 end end
  return n
end

return M
