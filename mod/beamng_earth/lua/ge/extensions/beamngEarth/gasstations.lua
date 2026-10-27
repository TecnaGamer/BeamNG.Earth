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
  '/levels/east_coast_usa/art/shapes/buildings/main.materials.json' }                       -- (US East's units)
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
  for _, ob in ipairs(st.objs) do
    local o = scenetree.findObjectById(ob.id)
    if o then o:delete() end
  end
  -- its colliding pumps and canopy out of the collision too (no invisible pump left to crash into)
  if st.made and M.onCollide then M.onCollide() end
  st.objs, st.pumps, st.made = {}, {}, false
end

local function spawn(id, st)
  local r = st.rec
  for _, p in ipairs(r.props or {}) do
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
    st.objs[#st.objs + 1] = { id = o:getID(), pos = { p.pos[1], p.pos[2], p.pos[3] }, q = q }
  end
  for i, a in ipairs(r.areas or {}) do
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
  st.made = true
  poisDirty = true
  if M.onCollide then M.onCollide() end
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
        elseif st.made then         -- another layout (the new anchor's frame): made again from this record
          deleteObjs(st)
          queue[#queue + 1] = r.id
          poisDirty = true
        elseif st.stale then        -- queued at a rebase: now it has a record in the current frame
          queue[#queue + 1] = r.id
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

-- Once per frame: spawn within the governor's budget, then the POI list gathered again if due.
function M.update(gov)
  while #queue > 0 and gov:try('fuel') do
    local id = table.remove(queue, 1)
    local st = stations[id]
    if st and not st.made and not st.stale then
      local t0 = M.now()
      local ok, err = pcall(spawn, id, st)
      if not ok then log('W', 'beamngEarth', 'gas station ' .. tostring(id) .. ': ' .. tostring(err)) end
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
        name = (r.name and r.name ~= '') and r.name or 'Gas station', description = r.brand or '',
        energyTypes = M.ENERGY, pumps = st.pumps })
    end
  end
end

-- The world map's "Gas stations" overlay (settings.uiMapState): every known station (made or still queued to
-- spawn) as { id, name, brand, lat, lon }, from its first prop/area's position in the current anchor's frame
-- (kept current by M.transform on a rebase) converted by `anchor` (the active one, core._state().anchor). Only
-- stations whose road tile has already reached this client - the server lays them out per road tile, so this is
-- "where you've driven", not the whole map (the user, 2026-10-27).
function M.mapPins(anchor)
  if not anchor then return {} end
  local out = {}
  for id, st in pairs(stations) do
    local p = (st.objs[1] and st.objs[1].pos) or (st.rec.props and st.rec.props[1] and st.rec.props[1].pos)
      or (st.rec.areas and st.rec.areas[1] and st.rec.areas[1].pos)
    if p then
      local lat, lon = anchor:enuToGeodetic(p[1], p[2], p[3])
      local r = st.rec
      out[#out + 1] = { id = id, name = (r.name and r.name ~= '') and r.name or 'Gas station', brand = r.brand or '',
        lat = lat, lon = lon }
    end
  end
  return out
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

function M.count()
  local n = 0
  for _, st in pairs(stations) do if st.made then n = n + 1 end end
  return n
end

return M
