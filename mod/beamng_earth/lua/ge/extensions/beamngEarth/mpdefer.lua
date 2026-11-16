-- BeamMP: the cars of players far away (not in our world) are not spawned here, their edits not built (the user's
-- live session, 2026-11-15, 1.10.0-beta, ~16 players over the whole Earth: each remote spawn (BeamMP's applyVehSpawn)
-- and each remote edit / swap (applyVehEdit) froze the game 0.5-4 s (jbeam load, "GE load time: 1.5 s"), 240 spawns
-- and 70 edits in 40 minutes, most of them of cars thousands of km away that mpanchor then dropped as far and
-- mpsession switched off: paid in full for a car never shown).
--
-- BeamMP's own spawn queue does the keeping: a car whose spawn is queued is a record BeamMP knows (MPVehicleGE's
-- vehicles[serverVehicleID]: its owner, isSpawned false, gameVehicleID -1, spawnQueue = the spawn) and every other
-- part of BeamMP handles such a car (its position, inputs, electrics, resets and paint packets go to the record, not
-- to a vehicle). A far car's spawn packet is handed to BeamMP with its queue on for that one call (settings.getValue
-- answers enableSpawnQueue = true, its "queued" notice and queue count left out), and the queued spawn is then taken
-- off the record into its bngeHeld field (BeamMP's queue would spawn it as soon as we stand still). The record stays
-- BeamMP's: the server and vehicle ids map as before (getGameVehicleID gives -1, as for any queued car), the player
-- and their car are in its lists. Edits to such a car go into the kept spawn (the latest config spawns); a far car
-- that is spawned already (switched off here, mpsession) keeps its edit in bngeHeldEdit. When the car comes within
-- RELEASE_M (and our ground is under it, or GROUND_WAIT_S passed) its spawn packet is handed to BeamMP again as if it
-- had just come (at the car's latest place): BeamMP spawns or queues it by the player's own queue settings. An edit
-- kept for a spawned car goes the same way once the car is near and on again.
--
-- The far test is mpanchor's (FAR_M from our origin), from the car's own position packets (mpanchor.seen) or, before
-- any, its owner's place (mpsession's bngeAnchors). A car whose place is not known (its owner without BeamNG.Earth, or
-- not heard yet) waits UNKNOWN_WAIT_S, then spawns as before. Our own cars, and outside our level or a BeamMP session,
-- nothing changes: the hook passes every packet through.
--
-- Hook point: MPVehicleGE.handle(rawData), the entry point for every vehicle event from the server (MPGameNetwork's
-- 'O' packets: s spawn, c edit, d remove, p paint, r reset, t coupler, m camera), wrapped once at run time as
-- mpanchor wraps positionGE.handle; BeamMP's files are not changed. Feature-detected: without MPVehicleGE.handle /
-- getVehicles or settings.getValue nothing is wrapped or held. Fail-safe: no update for STALE_S (our session code
-- stopped) hands every kept spawn and edit back to BeamMP at the next packet; the data lives on BeamMP's own records,
-- so a reload of this module finds it again.
local mpanchor = require('beamngEarth/mpanchor')

local M = {}

M.RELEASE_M = 24000          -- a kept car spawns within this (m from our origin; held beyond mpanchor.FAR_M: no flapping)
M.SEEN_FRESH_S = 5           -- a car's position packet tells where it is this long (mpanchor.clock)
M.UNKNOWN_WAIT_S = 20        -- a car whose place is not known waits this long (our anchor known), then spawns as before
M.GROUND_WAIT_S = 20         -- a car come near waits this long at most for our ground under it
M.TICK_S = 0.5
M.STALE_S = 30               -- (os.time) no update this long: everything kept handed back, the hook passes through
M.FAR_BLOB_Z = -1e6          -- a kept car's record placed here when its place is not known (BeamMP's blob unseen)

local wrappedTable = nil     -- the MPVehicleGE table wrapped (BeamMP reloading it makes a new one)
local original = nil         -- BeamMP's own handle
local active, lastTick = false, nil
local tickIn = 0
local timers = {}            -- [sid] = { unknown = s, ground = s, x, y }: a kept car's waits
local stats = { held = 0, released = 0, merged = 0, edits = 0, editsReleased = 0, taken = 0, handedBack = 0 }
M.stats = stats

-- Pure: the parts of an MPVehicleGE packet: code, serverVehicleID, data, role, name ('s' ROLE:name:sid:{json},
-- 'c' sid:{json}, 'p' sid:[json], 'd' sid; others: code only). BeamMP's own patterns.
function M.parse(raw)
  if type(raw) ~= 'string' then return nil end
  local code, rest = raw:sub(1, 1), raw:sub(3)
  if code == 's' then
    local role, name, sid, data = rest:match('^(%w+)%:([%g%s]+)%:(%d+%-%d+)%:({.+})')
    if sid then return code, sid, data, role, name end
  elseif code == 'c' then
    local sid, data = rest:match('^(%d+%-%d+)%:({.+})')
    if sid then return code, sid, data end
  elseif code == 'p' then
    local sid, data = rest:match('^(%d+%-%d+)%:(%[.+%])')
    if sid then return code, sid, data end
  elseif code == 'd' then
    local sid = rest:match('^(%d+%-%d+)$')
    if sid then return code, sid end
  end
  return code
end

function M.farM() return mpanchor.FAR_M end

-- Pure: a remote car's spawn: 'hold' or 'spawn'. d: its distance (m) from our origin, nil unknown; isHeld: kept now;
-- unknownFor: seconds its place has been unknown. Kept beyond FAR_M, spawned within RELEASE_M (between: as it is).
function M.verdict(d, isHeld, unknownFor)
  if d == nil then return (unknownFor or 0) >= M.UNKNOWN_WAIT_S and 'spawn' or 'hold' end
  if isHeld then return d <= M.RELEASE_M and 'spawn' or 'hold' end
  return d > M.farM() and 'hold' or 'spawn'
end

-- Pure: may a kept car come near spawn now? groundOk: our ground is under it (or it floats there); waited: seconds.
function M.groundDue(groundOk, waited) return groundOk or (waited or 0) >= M.GROUND_WAIT_S end

-- Pure: keep an edit of a spawned car (known to be far)?
function M.holdEdit(d) return d ~= nil and d > M.farM() end
-- Pure: hand a kept edit to BeamMP (the car not known far, and on here)?
function M.editDue(d, off) return not off and (d == nil or d <= M.farM()) end

-- Pure: a kept spawn with an edit put in: the edit's fields (model, config, ...), the spawn's place and remote id
-- (BeamMP's own rule for its kept configs: onServerVehicleEdited keeps pos and rot).
function M.mergeEdit(spawn, edit)
  local out = {}
  for k, v in pairs(spawn) do out[k] = v end
  for k, v in pairs(edit) do
    if k ~= 'pos' and k ~= 'rot' and k ~= 'vid' then out[k] = v end
  end
  return out
end

-- Pure: a spawn at the car's latest place s ({ x, y, z, rot }, our frame) (BeamMP's applyVehSpawn takes the record's
-- position over the spawn's when it has one: the same, for the new record the spawn makes).
function M.withPose(spawn, s)
  local out = {}
  for k, v in pairs(spawn) do out[k] = v end
  if s and tonumber(s.x) and tonumber(s.y) then
    out.pos = { s.x, s.y, tonumber(s.z) or 0 }
    local r = s.rot
    if type(r) == 'table' and tonumber(r[1]) and tonumber(r[2]) and tonumber(r[3]) and tonumber(r[4]) then
      out.rot = { r[1], r[2], r[3], r[4] }
    end
  end
  return out
end

-- Pure: a spawn or edit with the live paint put in (BeamMP: decodedData.vcf.paints = the paint packet).
function M.withPaints(dec, paints)
  if type(dec.vcf) ~= 'table' then dec.vcf = {} end
  dec.vcf.paints = paints
  return dec
end

local function mp()
  local v = rawget(_G, 'MPVehicleGE')
  return type(v) == 'table' and v or nil
end
local function records()
  local m = mp()
  if not m or type(m.getVehicles) ~= 'function' then return nil end
  local ok, t = pcall(m.getVehicles)
  return ok and type(t) == 'table' and t or nil
end
local function myPid()
  local cfg = rawget(_G, 'MPConfig')
  if type(cfg) == 'table' and type(cfg.getPlayerServerID) == 'function' then
    local ok, id = pcall(cfg.getPlayerServerID)
    if ok then return tonumber(id) end
  end
end
local function session() return package.loaded['beamngEarth/mpsession'] end
local function pidOf(sid) return tonumber(tostring(sid):match('^(%d+)%-')) end
local function decode(s)
  local ok, d = pcall(jsonDecode, s)
  return ok and type(d) == 'table' and d or nil
end
local function encode(t)
  local ok, s = pcall(jsonEncode, t)
  return ok and type(s) == 'string' and s or nil
end

-- A remote car's distance (m) from our origin and its latest place in our frame: its own fresh position packet, else
-- its owner's place; nil: not known.
function M.distOf(sid, pid)
  local s = mpanchor.seen(sid)
  if s and mpanchor.clock() - s.t <= M.SEEN_FRESH_S then return s.d, s end
  local ms = session()
  local ok, d = pcall(function() return ms and ms.ownerDist and ms.ownerDist(pid) end)
  return ok and tonumber(d) or nil, nil
end

local function isActive() return active and lastTick ~= nil and os.time() - lastTick <= M.STALE_S end

-- BeamMP's handle run with its spawn queue on for this one call, without its notice and queue count (the spawn is
-- taken off the queue right after). settings.getValue / UI's functions put back whatever happens.
-- -> true when it ran that way (false: no settings.getValue to answer with, the packet went through as before)
local function queuedCall(fn, raw)
  local st, ui = rawget(_G, 'settings'), rawget(_G, 'UI')
  if type(st) ~= 'table' or type(st.getValue) ~= 'function' then
    fn(raw)
    return false
  end
  local hadGet = rawget(st, 'getValue')
  local getValue = st.getValue
  rawset(st, 'getValue', function(k, ...)
    if k == 'enableSpawnQueue' then return true end
    if k == 'queueSkipUnicycle' then return false end
    return getValue(k, ...)
  end)
  local saved = {}
  if type(ui) == 'table' then
    for _, k in ipairs({ 'showNotification', 'updateQueue' }) do
      if type(ui[k]) == 'function' then
        saved[k] = { raw = rawget(ui, k) }
        rawset(ui, k, function() end)
      end
    end
  end
  local ok, err = pcall(fn, raw)
  rawset(st, 'getValue', hadGet)
  for k, s in pairs(saved) do rawset(ui, k, s.raw) end
  if not ok then error(err, 0) end
  return true
end

-- BeamMP's queue count shown again after we took queued events off its records (its getQueueCounts, local there).
local function updateQueueUI(recs)
  local ui = rawget(_G, 'UI')
  if type(ui) ~= 'table' or type(ui.updateQueue) ~= 'function' then return end
  local st = rawget(_G, 'settings')
  local hl = false
  if type(st) == 'table' and type(st.getValue) == 'function' then
    local ok, v = pcall(st.getValue, 'highlightQueuedPlayers')
    hl = ok and v
  end
  local spawns, edits, players = 0, 0, {}
  for _, v in pairs(recs) do
    if type(v) == 'table' then
      if v.spawnQueue then spawns = spawns + 1; if hl and v.ownerID then players[v.ownerID] = true end end
      if v.editQueue then edits = edits + 1; if hl and v.ownerID then players[v.ownerID] = true end end
    end
  end
  pcall(ui.updateQueue, spawns, edits, players)
end

local function roleOf(v)
  local m = mp()
  local ok, ps = pcall(function() return m and m.getPlayers and m.getPlayers() end)
  local p = ok and type(ps) == 'table' and ps[v.ownerID]
  local name = type(p) == 'table' and type(p.role) == 'table' and p.role.name
  return type(name) == 'string' and name:match('^%w+$') and name or 'USER'
end

local function km(d) return d and string.format('%.0f km', d / 1000) or 'place not known' end

-- A queued spawn taken off BeamMP's record v (its queue would spawn it): kept in v.bngeHeld.
local function take(sid, v, role, d, why)
  local q = v.spawnQueue
  if type(q) ~= 'table' or type(q.data) ~= 'string' then return false end
  v.spawnQueue = nil
  local json = q.data
  if type(v.editQueue) == 'string' then         -- (an edit BeamMP queued after it: into the spawn, as edits to kept cars)
    local sd, e = decode(json), decode(v.editQueue)
    json = sd and e and encode(M.mergeEdit(sd, e)) or json
    v.editQueue = nil
  end
  v.bngeHeld = { role = role or roleOf(v), name = tostring(q.playerNickname or v.ownerName or 'player'), json = json }
  v.hideNametag = true          -- (BeamMP's own per-car flag: its tag loop skips it)
  timers[sid] = { unknown = 0, ground = 0, nopose = 0, since = mpanchor.clock() }
  stats.held = stats.held + 1
  log('I', 'beamngEarth', string.format('BeamMP: car %s (%s) %s: its spawn kept, not spawned here', sid,
    v.bngeHeld.name, why or (d and ('far (' .. km(d) .. ')') or 'place not known yet')))
  return true
end

-- A kept car's record where BeamMP draws its blob (a car not spawned): the car's place in our frame (far: unseen),
-- else its owner's, else far below. Never left at 0, 0, 0 (a blob at our origin).
local function placeRecord(v, s, pid)
  local p = v.position          -- (the game's vec3: an FFI struct, not a table)
  if p == nil then return end
  local x, y, z
  if s then
    x, y, z = s.x, s.y, s.z
  else
    local ms = session()
    local ok, lat, lon = pcall(function() return ms.ownerPlace(pid) end)
    if ok and lat then x, y, z = mpanchor.toOurs(lat, lon) end
  end
  if not x then x, y, z = 0, 0, M.FAR_BLOB_Z end
  pcall(function() p:set(x, y, z) end)
end

-- A kept spawn handed to BeamMP as if it had just come (it spawns or queues it, by the player's queue settings), at the
-- car's latest place s (nil: the spawn as sent).
local function release(sid, v, s, why)
  local h = v.bngeHeld
  v.bngeHeld, v.hideNametag, timers[sid] = nil, nil, nil
  if type(h) ~= 'table' or not original then return end
  local json = h.json
  if s then
    local dec = decode(json)
    json = dec and encode(M.withPose(dec, s)) or json
  end
  v.remoteVehID = nil           -- (else BeamMP takes the spawn for one it has: "This ID already exists")
  stats.released = stats.released + 1
  log('I', 'beamngEarth', string.format('BeamMP: car %s (%s) %s: spawned', sid, h.name, why))
  original('s:' .. h.role .. ':' .. h.name .. ':' .. sid .. ':' .. json)
  M.markPosed(sid, s ~= nil)
end

-- The record BeamMP made of a spawn handed to it with a place in our frame (now, by its owner's anchor now): a spawn
-- of it waiting in BeamMP's queue may stay there (update's sweep takes in any other of a BeamNG.Earth player's).
function M.markPosed(sid, posed)
  local recs = records()
  local v = recs and recs[sid]
  if type(v) == 'table' then v.bngePosed = posed or nil end
end

local function releaseEdit(sid, v, why)
  local raw = v.bngeHeldEdit
  v.bngeHeldEdit = nil
  if type(raw) ~= 'string' or not original then return end
  stats.editsReleased = stats.editsReleased + 1
  log('I', 'beamngEarth', string.format('BeamMP: car %s\'s kept edit %s', sid, why))
  original(raw)
end

-- Everything kept handed to BeamMP (our session code stopped: BeamMP's own behaviour again).
function M.handBack(why)
  local recs = records()
  if not recs then return end
  local list = {}
  for sid, v in pairs(recs) do
    if type(v) == 'table' and (v.bngeHeld or v.bngeHeldEdit) then list[#list + 1] = { sid, v } end
  end
  for _, a in ipairs(list) do
    stats.handedBack = stats.handedBack + 1
    if a[2].bngeHeld then pcall(release, a[1], a[2], nil, why or 'handed back') end
    if a[2].bngeHeldEdit then pcall(releaseEdit, a[1], a[2], why or 'handed back') end
  end
end

-- The wrap of MPVehicleGE.handle (see the top).
local function wrap(orig)
  return function(raw)
    if not isActive() then
      if lastTick and os.time() - lastTick > M.STALE_S and active then
        active = false
        log('W', 'beamngEarth', 'BeamMP: no session update for ' .. M.STALE_S .. ' s: far cars\' spawns handed back')
        M.handBack('handed back (session stopped)')
      end
      return orig(raw)
    end
    local code, sid, data, role, name = M.parse(raw)
    if not sid then return orig(raw) end
    local pid, mine = pidOf(sid), myPid()
    if pid == nil or mine == nil or pid == mine then return orig(raw) end
    local recs = records()
    local v = recs and recs[sid]
    if type(v) == 'table' and v.isLocal then return orig(raw) end
    local h = type(v) == 'table' and v.bngeHeld or nil
    if code == 's' then
      if h then                                   -- (kept: the newer spawn kept instead)
        h.json, h.role, h.name = data, role, name
        return
      end
      if type(v) == 'table' and v.isSpawned ~= false then return orig(raw) end    -- (BeamMP's own update of a car)
      local d, s = M.distOf(sid, pid)
      local why = nil
      if M.verdict(d, false, 0) == 'spawn' then
        if not mpanchor.knows(pid) then return orig(raw) end     -- (no BeamNG.Earth: its coordinates as BeamMP's)
        -- A BeamNG.Earth player's car is handed to BeamMP only with a place in our frame: the spawn's coordinates are
        -- in its owner's frame (another anchor), and BeamMP's queue spawns there when no position packet came first.
        -- None yet: kept till its first packet (mpdefer.update; Codex review 4, 2026-11-15).
        local dec = s and decode(data)
        local json = dec and encode(M.withPose(dec, s))
        if json then
          orig('s:' .. role .. ':' .. name .. ':' .. sid .. ':' .. json)
          M.markPosed(sid, true)
          return
        end
        why = 'near (' .. km(d) .. '), no position of its own yet'
      end
      if not queuedCall(orig, raw) then return end
      recs = records()
      v = recs and recs[sid]
      if type(v) == 'table' and not v.isLocal and v.isSpawned == false then take(sid, v, role, d, why) end
      return
    elseif code == 'c' then
      if h then                                   -- (a kept spawn takes the edit: the latest config spawns)
        local s, e = decode(h.json), decode(data)
        if s and e then
          h.json = encode(M.mergeEdit(s, e)) or h.json
          stats.merged = stats.merged + 1
          return
        end
        return orig(raw)
      end
      if type(v) == 'table' and v.isSpawned and (tonumber(v.gameVehicleID) or -1) > 0 then
        local d = M.distOf(sid, pid)
        if M.holdEdit(d) then
          if not v.bngeHeldEdit then
            log('I', 'beamngEarth', string.format('BeamMP: car %s far (%s): its edit kept, not built here', sid, km(d)))
          end
          v.bngeHeldEdit = raw
          stats.edits = stats.edits + 1
          -- (an older edit BeamMP queued while the car was near is superseded: left there, its queue would build it,
          -- or the sweep below would take it over this newer one; Codex review 2026-11-15)
          if v.editQueue ~= nil then
            v.editQueue = nil
            if recs then updateQueueUI(recs) end
          end
          return
        end
        v.bngeHeldEdit = nil                      -- (a newer edit goes through)
      end
      return orig(raw)
    elseif code == 'p' then                       -- (live paint: into what is kept too; BeamMP's own handling as before)
      local paints = decode(data)
      if paints and h then
        local s = decode(h.json)
        if s then h.json = encode(M.withPaints(s, paints)) or h.json end
      end
      if paints and type(v) == 'table' and type(v.bngeHeldEdit) == 'string' then
        local esid, edata = v.bngeHeldEdit:match('^c%:(%d+%-%d+)%:({.+})')
        local e = edata and decode(edata)
        local enc = e and encode(M.withPaints(e, paints))
        if enc then v.bngeHeldEdit = 'c:' .. esid .. ':' .. enc end
      end
      return orig(raw)
    elseif code == 'd' then
      if h then
        -- (put back in BeamMP's queue first: it removes a queued car as its own, "still in the queue")
        v.spawnQueue = { playerNickname = h.name, serverVehicleID = sid, data = h.json }
        v.bngeHeld, timers[sid] = nil, nil
      end
      if type(v) == 'table' then v.bngeHeldEdit = nil end
      return orig(raw)
    end
    return orig(raw)
  end
end

-- (mpanchor) An anchor changed: player pid's (a teleport), or ours (pid nil: a rebase or teleport). Every pose of
-- their cars kept from before is stale: the cars moved with their owner, or our frame did. A spawn waiting in BeamMP's
-- queue (one we handed back, or its own) carries such a pose (BeamMP spawns there when no packet comes first): taken
-- back at once, before its queue can run, and kept until a position in our frame comes. A kept car's wait for its
-- ground starts again at its new place. (Codex review 4, 2026-11-15)
function M.onAnchorChange(pid)
  if not isActive() then return end
  local recs = records()
  if not recs then return end
  local mine = myPid()
  local changed = false
  for sid, v in pairs(recs) do
    local owner = type(v) == 'table' and (tonumber(v.ownerID) or pidOf(sid))
    if owner and not v.isLocal and owner ~= mine and (pid == nil or owner == pid) and v.isSpawned == false
        and mpanchor.knows(owner) then
      v.bngePosed = nil
      if v.bngeHeld then
        local w = timers[sid]
        if w then w.ground, w.x, w.y, w.nopose = 0, nil, nil, 0 end
      elseif type(v.spawnQueue) == 'table' then
        if take(sid, v, nil, nil, pid and 'its owner\'s anchor changed' or 'our anchor changed') then
          stats.taken = stats.taken + 1
          changed = true
        end
      end
    end
  end
  if changed then updateQueueUI(recs) end
end

-- Wraps MPVehicleGE.handle once per MPVehicleGE table (BeamMP reloaded: wrapped again). -> installed
function M.install()
  local m = mp()
  if not m then return false end
  if m == wrappedTable then return true end
  local st = rawget(_G, 'settings')
  if type(m.handle) ~= 'function' or type(m.getVehicles) ~= 'function' or type(st) ~= 'table'
      or type(st.getValue) ~= 'function' then
    return false
  end
  original = m.handle
  m.handle = wrap(original)
  wrappedTable = m
  log('I', 'beamngEarth', 'mpdefer: installed (far BeamMP players\' cars not spawned here)')
  return true
end

-- Each frame in our level in a BeamMP session (core.lua): the hook, and every TICK_S the kept cars checked.
function M.update(dt)
  if not M.install() then return end
  active, lastTick = true, os.time()
  tickIn = tickIn - (dt or 0)
  if tickIn > 0 then return end
  tickIn = M.TICK_S
  local recs = records()
  if not recs then return end
  local mine = myPid()
  local ms = session()
  local myLat = mpanchor.own()
  local changed = false
  local spawns, edits = {}, {}  -- (handed to BeamMP after the loop: a spawn writes into its vehicles table)
  for sid, v in pairs(recs) do
    if type(v) == 'table' and not v.isLocal and v.ownerID ~= mine then
      local pid = tonumber(v.ownerID) or pidOf(sid)
      if v.bngeHeld then
        if v.isSpawned ~= false then                -- (spawned meanwhile by BeamMP itself: ours forgotten)
          v.bngeHeld, v.hideNametag, timers[sid] = nil, nil, nil
        else
          local w = timers[sid] or { unknown = 0, ground = 0, nopose = 0, since = mpanchor.clock() }
          timers[sid] = w
          local d, s = M.distOf(sid, pid)
          if d == nil then
            if myLat then w.unknown = w.unknown + M.TICK_S end    -- (no world of ours yet: wait for it)
          else
            w.unknown = 0
          end
          local verdict = M.verdict(d, true, w.unknown)
          if verdict == 'spawn' and d == nil then
            if mpanchor.knows(pid) then
              -- (a BeamNG.Earth player's car is never spawned at the spawn's coordinates, its owner's frame: it waits
              -- for a place of its own in our frame, however long. Codex review 4, 2026-11-15)
              verdict = 'hold'
            else
              -- (place not known, spawned as BeamMP would: at its latest packet as sent, not the spawn's old place,
              -- which the replay's new record would otherwise use; Codex review 2026-11-15)
              local r = mpanchor.rawSeen(sid)
              if r and r.t >= (w.since or -math.huge) then s = r end
            end
          end
          if verdict == 'spawn' and d ~= nil and not s then
            -- (near by its owner's place only: spawned only at a place of its own in our frame, its last packet
            -- (older than SEEN_FRESH_S, still near; mpanchor forgets it when its owner's anchor changes), never at the
            -- spawn's coordinates: those are in its owner's frame of then; none: it waits for a packet. Codex review
            -- 3, 2026-11-15)
            local last = mpanchor.seen(sid)
            if last and tonumber(last.d) and last.d <= M.RELEASE_M then
              s = last
            else
              if not w.nopose or w.nopose == 0 then
                log('I', 'beamngEarth', string.format('BeamMP: car %s near by its owner\'s place (%s): waits for a '
                  .. 'position of its own', sid, km(d)))
              end
              w.nopose = (w.nopose or 0) + M.TICK_S
              verdict = 'hold'
            end
          elseif s then
            w.nopose = 0
          end
          if verdict == 'spawn' and d ~= nil and s then
            local ok, g = pcall(function() return not s or not ms or not ms.groundOkAt or ms.groundOkAt(s.x, s.y) end)
            if not M.groundDue(not ok or g, w.ground) then
              if w.ground == 0 then
                log('I', 'beamngEarth', string.format('BeamMP: car %s near (%s): waits for its ground', sid, km(d)))
              end
              w.ground, w.x, w.y = w.ground + M.TICK_S, s.x, s.y
              verdict = 'hold'
            end
          end
          if verdict == 'spawn' then
            spawns[#spawns + 1] = { sid, v, s, d and ('near (' .. km(d) .. ')') or 'place not known' }
          else
            placeRecord(v, s, pid)
          end
        end
      elseif v.spawnQueue and v.isSpawned == false then
        -- (BeamMP's own queue: a far car queued before the hook was in, or while its place was not known; and any
        -- spawn of a BeamNG.Earth player's car queued without a place in our frame (before the hook, or while it
        -- passed packets through): its coordinates are in its owner's frame. Codex review 4, 2026-11-15)
        local d = M.distOf(sid, pid)
        local unposed = mpanchor.knows(pid) and not v.bngePosed
        if (unposed or (M.verdict(d, false, 0) == 'hold' and d ~= nil))
            and take(sid, v, nil, d, unposed and 'queued without a place in our frame' or nil) then
          stats.taken = stats.taken + 1
          changed = true
        end
      elseif v.editQueue and v.isSpawned and type(v.editQueue) == 'string' then
        -- (an edit queued by BeamMP while one is kept is the newer: the hook drops the queued one when it keeps an
        -- edit, so a queued edit next to a kept one came through after it)
        local d = M.distOf(sid, pid)
        if M.holdEdit(d) then
          v.bngeHeldEdit, v.editQueue = 'c:' .. sid .. ':' .. v.editQueue, nil
          stats.taken, stats.edits = stats.taken + 1, stats.edits + 1
          changed = true
          log('I', 'beamngEarth', string.format('BeamMP: car %s far (%s): its queued edit kept', sid, km(d)))
        end
      end
      if v.bngeHeldEdit then
        if not v.isSpawned then
          v.bngeHeldEdit = nil
        else
          local d = M.distOf(sid, pid)
          local okO, off = pcall(function() return ms and ms.isOff and ms.isOff(v.gameVehicleID) end)
          if M.editDue(d, okO and off) then edits[#edits + 1] = { sid, v } end
        end
      end
    end
  end
  if changed then updateQueueUI(recs) end
  for _, a in ipairs(spawns) do
    local ok, err = pcall(release, a[1], a[2], a[3], a[4])
    if not ok then log('E', 'beamngEarth', 'BeamMP: car ' .. a[1] .. ' spawn: ' .. tostring(err)) end
  end
  for _, a in ipairs(edits) do
    local ok, err = pcall(releaseEdit, a[1], a[2], 'built (near)')
    if not ok then log('E', 'beamngEarth', 'BeamMP: car ' .. a[1] .. ' edit: ' .. tostring(err)) end
  end
  recs = records() or recs
  for sid in pairs(timers) do
    if not (recs[sid] and recs[sid].bngeHeld) then timers[sid] = nil end
  end
end

-- Collision foci where kept cars come near wait for their ground (mpsession.groundFoci). [{ x, y, r, trees, kind }]
function M.foci()
  local out = {}
  local cf = require('beamngEarth/collisionfoci')
  for sid, w in pairs(timers) do
    if (w.ground or 0) > 0 and w.x then
      out[#out + 1] = { x = w.x, y = w.y, r = cf.PLAYER_R, trees = true, id = 'mp:' .. sid, kind = 'player' }
    end
  end
  return out
end

-- Kept spawns and edits now: held, heldEdits.
function M.counts()
  local recs = records()
  local n, e = 0, 0
  for _, v in pairs(recs or {}) do
    if type(v) == 'table' then
      if v.bngeHeld then n = n + 1 end
      if v.bngeHeldEdit then e = e + 1 end
    end
  end
  return n, e
end

-- (the session's end, mpsession.reset) the hook passes through until the next update; what is kept stays on BeamMP's
-- records (it deletes them as the session ends; a session going on finds them again at the next update).
function M.reset()
  active, lastTick, tickIn, timers = false, nil, 0, {}
end

-- (tests) the module's state forgotten, the hook too
function M._forget()
  M.reset()
  wrappedTable, original = nil, nil
  for k in pairs(stats) do stats[k] = 0 end
end

return M
