-- Pump jacks at oil wells (the user, 2026-10-26: "since we're adding these sounds. Could we add matching props as
-- well"): West Coast USA's animated pump jack (its five parts placed as its pumpjacks group: every part at the base's
-- yaw, at an offset along the base's own y axis; the base, the tower with the gear box and ladder, is drawn: its file
-- says isRenderEnabled false but the tower shows there, and hidden here the jack had no tower, 2026-10-26) with its two sounds (the engine at the gear
-- box, the squeak at the horsehead, as its Pump_Jack_Engine_n / Pump_Jack_Squeak_n emitters), at the wells the road
-- tiles list (server oilwells.py: OpenStreetMap's man_made=petroleum_well points, not gas, not ended, not pump=no;
-- rows [x, y, z, yaw] in the anchor frame, yaw degrees counter-clockwise as the server's).
local M = {}

local OBJ = '/levels/west_coast_usa/art/shapes/objects/'
-- { shape, along its y (m), up (m) } (West Coast USA's pumpjacks group, read 2026-10-26: every jack the same)
M.PARTS = {
  { OBJ .. 'pumpjack_a.dae', 0, 0 },
  { OBJ .. 'pumpjack_b.dae', 1.282, 2.622 },
  { OBJ .. 'pumpjack_c.dae', 1.362, 6.102 },
  { OBJ .. 'pumpjack_d.dae', -2.194, 7.677 },
  { OBJ .. 'pumpjack_e.dae', 1.356, 6.272 },
}
M.SOUNDS = { { 'event:>Ambient>Maps>West Coast>Pump Jack_Engine', 2 },   -- { event, at part n }
             { 'event:>Ambient>Maps>West Coast>Pump Jack_Squeak', 4 } }
M.SOUND_N = 6            -- the nearest this many jacks within SOUND_M of the listener have their sounds ...
M.SOUND_M = 150          -- ... (West Coast USA's emitters reach 120 m)
M.TILE_MAX = 40          -- jacks a road tile shows at most (a dense California field: a well every ~50 m)
M.onCollide = nil        -- (core: the collision rebuild, as gasstations.lua)

local tiles = {}         -- road tile key -> { jacks = { { x, y, z, th, objs = {ids}, snd = {ids} } } }
M.made = 0

-- Pure: the world positions of a jack's parts at (x, y, z), th the engine's yaw (setFromEuler z: clockwise, so the
-- part's +y axis points to (sin th, cos th)).
function M.partPositions(x, y, z, th)
  local out = {}
  for i, p in ipairs(M.PARTS) do
    out[i] = { x + p[2] * math.sin(th), y + p[2] * math.cos(th), z + p[3] }
  end
  return out
end

local function place(o, pos, th)
  local m = MatrixF(true)
  m:setFromEuler(vec3(0, 0, th))
  m:setPosition(vec3(pos[1], pos[2], pos[3]))
  o:setTransform(m)
end

local function makeJack(j)
  j.objs, j.snd = {}, {}
  for i, pos in ipairs(M.partPositions(j.x, j.y, j.z, j.th)) do
    local p = M.PARTS[i]
    local o = createObject('TSStatic')
    o:setField('shapeName', 0, p[1])
    o:setField('decalType', 0, 'None')
    o:setField('dynamic', 0, 'true')       -- (no redraw of the cached sun shadow map: farfield.lua)
    o.canSave = false
    place(o, pos, j.th)
    o:registerObject('')
    if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
    j.objs[#j.objs + 1] = o:getID()
  end
  M.made = M.made + 1
end

local function deleteIds(ids)
  for _, id in ipairs(ids or {}) do
    local o = scenetree.findObjectById(id)
    if o then o:delete() end
  end
end

local function dropJack(j)
  deleteIds(j.objs)
  deleteIds(j.snd)
  j.objs, j.snd = {}, {}
end

-- A road tile's wells (roads.lua onTile: data.wells).
function M.onRows(key, rows)
  M.dropKey(key)
  if type(rows) ~= 'table' then return end
  local t = { jacks = {} }
  for i = 1, math.min(#rows, M.TILE_MAX) do
    local r = rows[i]
    if type(r) == 'table' and #r >= 4 then
      local j = { x = r[1], y = r[2], z = r[3], th = -math.rad(r[4]) }
      local ok, err = pcall(makeJack, j)
      if ok then t.jacks[#t.jacks + 1] = j else log('W', 'beamngEarth', 'pump jack: ' .. tostring(err)) end
    end
  end
  tiles[key] = t
  if #t.jacks > 0 and M.onCollide then M.onCollide() end
end

-- The road tile goes (roads.lua dropTile).
function M.dropKey(key)
  local t = tiles[key]
  if not t then return end
  for _, j in ipairs(t.jacks) do dropJack(j) end
  tiles[key] = nil
  if #t.jacks > 0 and M.onCollide then M.onCollide() end
end

-- Origin rebase (core.lua finishRebase): every jack moved by p_new = R*p + t, its yaw turned with it.
function M.transform(xf)
  for _, t in pairs(tiles) do
    for _, j in ipairs(t.jacks) do
      local ax, ay = xf:apply(j.x + math.sin(j.th), j.y + math.cos(j.th), j.z)
      j.x, j.y, j.z = xf:apply(j.x, j.y, j.z)
      j.th = math.atan2(ax - j.x, ay - j.y)
      for i, pos in ipairs(M.partPositions(j.x, j.y, j.z, j.th)) do
        local o = j.objs[i] and scenetree.findObjectById(j.objs[i])
        if o then place(o, pos, j.th) end
      end
      deleteIds(j.snd)
      j.snd = {}
    end
  end
end

-- Every frame (core.lua, cheap): the sounds on the nearest SOUND_N jacks within SOUND_M of the listener, checked
-- every half second.
local lastSound = -1e9
function M.update()
  local now = os.clockhp()
  if now - lastSound < 0.5 or not next(tiles) then return end
  lastSound = now
  local lx, ly = SFXSystem.getPositionXYZ()
  local near = {}
  for _, t in pairs(tiles) do
    for _, j in ipairs(t.jacks) do
      local d = (j.x - lx) ^ 2 + (j.y - ly) ^ 2
      if d <= M.SOUND_M ^ 2 then near[#near + 1] = { j, d } end
    end
  end
  table.sort(near, function(a, b) return a[2] < b[2] end)
  local want = {}
  for i = 1, math.min(#near, M.SOUND_N) do want[near[i][1]] = true end
  for _, t in pairs(tiles) do
    for _, j in ipairs(t.jacks) do
      if want[j] and #j.snd == 0 then
        local pos = M.partPositions(j.x, j.y, j.z, j.th)
        for _, s in ipairs(M.SOUNDS) do
          local e = createObject('SFXEmitter')
          e:setTransform(MatrixF(true))
          e:setField('track', 0, s[1])
          e.canSave = false
          e:registerObject('')
          e:setPosition(vec3(pos[s[2]][1], pos[s[2]][2], pos[s[2]][3]))
          e:play()
          j.snd[#j.snd + 1] = e:getID()
        end
      elseif not want[j] and #j.snd > 0 then
        deleteIds(j.snd)
        j.snd = {}
      end
    end
  end
end

-- Level end / a new session: everything goes.
function M.reset()
  for key in pairs(tiles) do M.dropKey(key) end
  tiles = {}
end

function M.status()
  local n, s = 0, 0
  for _, t in pairs(tiles) do
    for _, j in ipairs(t.jacks) do
      n = n + 1
      if #j.snd > 0 then s = s + 1 end
    end
  end
  return { jacks = n, sounding = s, made = M.made }
end

return M
