-- A ticket belongs to a delivery, even when two replies name the same cached revision. Keep it
-- through PNG import, deferred installs and a generation rollback. A transport loss releases none.
local M = {}
local seen = {}

local function session(tr)
  return tr.heightmap and tr.heightmap:match('/beamngEarth/([^/]+)/')
end

function M.received(msg)
  local tr = msg.terrain or msg
  if type(tr) == 'table' and tr.terrainTicket then seen[tr] = true end
end

local function using(st, terrain)
  local keep = {}
  local function add(tr) if tr then keep[tr] = true end end
  add(st.blockMsg)
  add(st.terrainPending and st.terrainPending.msg)
  add(st.pending and st.pending.msg and st.pending.msg.terrain)
  add(st.rebasing and st.rebasing.terrain)
  add(st.refreshLater)
  add(st.deferredRefresh)
  if st.swap then
    add(st.swap.oldTerrain)
    add(st.swap.now)
    add(st.swap.later)
    for _, tr in ipairs(st.swap.terrains or {}) do add(tr) end
  end
  -- A failed postApply or the PNG fallback can leave terrainFile pointing at a different
  -- delivery than blockMsg. The actual scene object has the last word about its bound file.
  local function keepFile(path, field)
    if not path or path == '' then return end
    path = path:gsub('\\', '/'):lower():gsub('^/', '')
    local owner
    for tr in pairs(seen) do
      local name = tr[field]
      if name and name:lower():gsub('^/', '') == path then
        if keep[tr] then return end
        if not owner or tr.terrainTicket < owner.terrainTicket then owner = tr end
      end
    end
    add(owner)
  end
  local tb = next(seen) and terrain.get and terrain.get()
  if tb then
    keepFile(tb:getField('terrainFile', 0), 'ter')
    local base = terrain.base and terrain.base()
    if base and base:match('_base_g%d+%.') then keepFile(base, 'base') end
  end
  return keep
end

-- Include queued anchor_ready messages not yet dispatched on the old transport. All deliveries
-- present in the game survive the hello; those lost in flight may now be retired by the server.
function M.snapshot(st, queue, sid, terrain)
  local keep = using(st, terrain)
  for i = queue.head, #queue.q do
    local msg = queue.q[i]
    local tr = msg.terrain or msg
    if type(tr) == 'table' and tr.terrainTicket then keep[tr] = true end
  end
  local ids = {}
  for tr in pairs(keep) do
    if tr.terrainTicket and session(tr) == sid then ids[#ids + 1] = tr.terrainTicket end
  end
  table.sort(ids)
  return ids
end

function M.sessions(list)
  for tr in pairs(seen) do
    local sid = session(tr)
    if sid then
      local found = false
      for _, s in ipairs(list) do if s == sid then found = true; break end end
      if not found then list[#list + 1] = sid end
    end
  end
  table.sort(list)
  return list
end

function M.release(st, net, terrain)
  if not next(seen) or not net.isReady() then return end
  local keep = using(st, terrain)
  local sid = net.lease()
  local ids, gone = {}, {}
  for tr in pairs(seen) do
    if not keep[tr] then
      gone[#gone + 1] = tr
      if session(tr) == sid then ids[#ids + 1] = tr.terrainTicket end
    end
  end
  table.sort(ids)
  if #ids == 0 or net.send({type = 'terrain_release', tickets = ids}) then
    for _, tr in ipairs(gone) do seen[tr] = nil end
  end
end

return M
