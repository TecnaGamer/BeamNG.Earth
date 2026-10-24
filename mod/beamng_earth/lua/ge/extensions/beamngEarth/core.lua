-- BeamNG.Earth core: GE extension 'beamngEarth_core' (lua/ge/extensions/beamngEarth/core.lua).
-- Ties together the server connection, the two world-moving mechanisms - terrain-follow (frequent,
-- same anchor) and origin rebase (rare, new anchor) - the far field, the sky location and the UI.
-- Contract: docs/V1_3_SPEC.md (protocol 4) over docs/V1_2_DESIGN.md (its section 12 wins), over
-- docs/V1_1_SPEC.md and docs/V1_SPEC.md.
-- Every frame first opens the governor's frame (governor.lua): far-tile streaming gets a small,
-- ramped per-frame budget, and the heavy operations (terrain commit, rebase, teleport) run in
-- isolated heavy slots with no streaming in or around them.
-- Near visual (server hello.nearVisual): 'mesh' = the far-tile system draws all the ground and the
-- TerrainBlock is physics-only (render disabled, re-applied after every import); 'terrain' = v1.2.
-- Block size (server hello.blockSize, docs/V1_4_SPEC.md 3.5 M4; none = 1024, v1.3.1): 512 sets the
-- lattice, terrain-follow table, layer / flat maps and rebase staleness rule of the session
-- (setBlockSize), before anything else reads them.
-- Horizon (server hello.horizon, docs/V1_4_SPEC.md 3.6 M1h; none = off, v1.3.1): LevelInfo.visibleDistance
-- follows the server's far-field radius (status.radius, +10 %) and the sky's planet radius is the
-- tiles' (horizon.lua); the level's values come back when it ends.
-- Robustness (docs/ROADMAP.md "Robustness: no server, lost server, lost internet" 1-3): the
-- connection banner (banner.lua) says what is wrong with the world server, the edge guard
-- (edgeguard.lua) holds the car on the last physics block while the server cannot move it (never
-- while the server is healthy), and hello / pose name the session folders whose files the far
-- tiles still use (farfield.heldSessions), so a restarted server keeps them (server/README.md).
--
-- World logic is active in our level (levels/beamng_earth) from the start, and in any other
-- level once the player teleports (for testing). Console helpers:
--   beamngEarth_core.teleport(lat, lon)   beamngEarth_core.toggleUI()   beamngEarth_core.status()
--   beamngEarth_core.setRebaseDistance(m)   beamngEarth_core.setFarBudget(ms)  (streaming budget, 1-4 ms)

local M = {}
-- terrain-follow moves swap in the server's .ter when it sends one (terrain.swap); false: always importMaps
M.TER_SWAP = true
M.dependencies = { 'ui_imgui' }

-- Helper modules live next to this file. 'lua/ge/extensions/?.lua' is on package.path
-- (main.lua:58 extensions.addModulePath), so they resolve as 'beamngEarth/<name>'.
local geodesy = require('beamngEarth/geodesy')
local net = require('beamngEarth/net')
local terrain = require('beamngEarth/terrain')
local farfield = require('beamngEarth/farfield')
local crossover = require('beamngEarth/crossover')
local rebase = require('beamngEarth/rebase')
local follow = require('beamngEarth/follow')
local lattice = require('beamngEarth/lattice')
local governor = require('beamngEarth/governor')
local camshift = require('beamngEarth/camshift')
local stopwatch = require('beamngEarth/stopwatch')
local horizon = require('beamngEarth/horizon')
local ui = require('beamngEarth/ui')
local settings = require('beamngEarth/settings') -- the pause-menu page's values (docs/DEVTOOLS.md)
local credits = require('beamngEarth/credits') -- the on-screen data credits (docs/DEVTOOLS.md)
local statusbar = require('beamngEarth/statusbar') -- the slim bar at the top: street, place, loading, fps
local loadprogress = require('beamngEarth/loadprogress') -- the bar's loading line after a teleport
local places = require('beamngEarth/places') -- saved and recent places (the settings page)
local levelinfo = require('beamngEarth/levelinfo') -- the level select's spawn points: the places, Random
local banner = require('beamngEarth/banner') -- the connection banner (docs/DEVTOOLS.md)
local hudapps = require('beamngEarth/hudapps') -- the same three as BeamNG UI apps (the ".Earth" app layout)
local edgeguard = require('beamngEarth/edgeguard') -- never fall off the last block (docs/ROADMAP.md)
local drophold = require('beamngEarth/drophold') -- a car dropped far away waits for its ground
local groundcover = require('beamngEarth/groundcover') -- grass on the surface layers (docs/ROADMAP.md v1.5)
local roads = require('beamngEarth/roads') -- DecalRoads from the server's road tiles (docs/ROADMAP.md v1.6)
local sea = require('beamngEarth/sea') -- the sea's WaterBlock tiles (server seatiles.py)
local trees = require('beamngEarth/trees') -- trees, bushes, rocks (server vegetation.py)
local parking = require('beamngEarth/parking') -- parked cars on the road tiles' spots (stock gameplay_parking)
local navgraph = require('beamngEarth/navgraph')
local postfx = require('beamngEarth/postfx')       -- no far depth-of-field blur in our level
local gps = require('beamngEarth/gps')               -- the game's GPS route to a place picked on the world map
local collisionfoci = require('beamngEarth/collisionfoci') -- other vehicles' collision islands
local terrainpatch = require('beamngEarth/terrainpatch')   -- ground under the islands off the terrain block
local farcars = require('beamngEarth/farcars')             -- far player cars saved, taken out and put back
local fociBroken = false
-- The pose's foci: the last collision foci (collisionfoci.lua), rounded, at most 16 (earthd FOCI_MAX); nil when none.
local function poseFoci()
  local f = collisionfoci.last
  if not f or #f == 0 then return nil end
  local out = {}
  for i = 1, math.min(#f, 16) do
    out[i] = { x = math.floor(f[i].x + 0.5), y = math.floor(f[i].y + 0.5), r = f[i].r, trees = f[i].trees }
  end
  return out
end
local beammp = require('beamngEarth/beammp')           -- BeamMP sessions: the install recommendation
local mpsession = require('beamngEarth/mpsession')     -- BeamMP sessions (the plugin's anchors, damage, joining)
local mpanchor = require('beamngEarth/mpanchor')       -- BeamMP: other players' positions into our frame (PR #88)
local seasonlook = require('beamngEarth/seasonlook')   -- the trees' leaves in the season (hello.season) -- the AI road graph for the stock traffic and AI (map.reset)
local perflog = require('beamngEarth/perflog')         -- hitches and frame-rate drops into server.log (perflog.lua)

local LEVEL_ID = 'beamng_earth'
local MOD_VERSION = '0.4.0'
M.MOD_VERSION = MOD_VERSION
local lastHello = nil     -- the last server hello (scalars only; report.lua's hardware.json)
local PROTOCOL = 4
-- Far-tile artefact formats the mod reads: 3 (v1.3.1, 32-cell tiles) and 4 (docs/V1_4_SPEC.md 3.1
-- M1: 64-cell tiles down to L-1, hello.cells, far_tile.tex). hello.format = the newest (FORMAT).
local FORMAT = 4
local FORMATS = { 3, 4 }
local POSE_INTERVAL = 0.5      -- ~2 Hz
local SKY_INTERVAL = 5.0       -- TimeOfDay lat/lon follow
local VISIBLE_DISTANCE = 100000
local LAG_PROBE_FRAMES = 10   -- simulated frames to watch for a rebase's vehicle move to show up
local PLACE_DELAY_FRAMES = 2   -- let the collision rebuild settle before placing the vehicle
local FLOOR_WAIT_S = 60        -- s a teleport into a tunnel waits for the tube's floor before giving up (car stays on top)
local FLOOR_HIT_M = 0.8        -- m the static collision found under floorZ + 2 may differ from floorZ: the tube is there
local PRE_LIFT = 5             -- m above groundZ + 1 to hold the vehicle until it is placed
local LOADING_AFTER_TELEPORT_S = 3 -- governor loading mode (8 ms budget) this long after a teleport
local SLOW_SPEED, SLOW_S = 1, 3    -- ... and while the player is below 1 m/s for over 3 s
local DEFAULT_LAT, DEFAULT_LON = 36.596718, -118.169991 -- Whitney Portal Road under Mt Whitney (the summit, 36.5785,
                                                         -- -118.2923, was a start on a 20 % slope 3 km from any road)
-- The server cannot move the block when it is not connected, has not said hello, or does not answer
-- terrain requests: one unanswered for STUCK_S, or a terrain_error within ERROR_HOLD_S. Only then
-- does the edge guard run.
local STUCK_S = 5
local ERROR_HOLD_S = 10
local ATTRIBUTION = 'Elevation: Mapterhorn (CC-BY etc., see mapterhorn.com), AWS Terrain Tiles / Mapzen'
-- shown once satellite-textured tiles arrive (docs/V1_3_SPEC.md C; EOX's required wording)
local IMAGERY_ATTRIBUTION = 'Imagery: USGS The National Map (public domain); EOxCloudless https://cloudless.eox.at by '
  .. 'EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2023 / 2016 & 2017; CC BY-NC-SA 4.0 / CC BY 4.0)'
local FULL_ATTRIBUTION = IMAGERY_ATTRIBUTION .. '\n' .. ATTRIBUTION

local st -- all runtime state, see resetState()
local rebaseLag = 0 -- frames between a vehicle move request and its visibility (measured, see commitRebase)
local roadsSession = nil -- the server session whose road tiles are shown (roads.lua)
local gov = governor.new() -- outlives levels (keeps its target and cost estimates)
local warned = {}          -- one-time warnings
local nearVisual = 'terrain' -- from the server's hello; outlives levels like the connection
local serverAttribution = nil -- hello.attribution (protocol 4): the credits for the sources it uses
local serverCreditsByArea, serverOtherCredits = nil, nil  -- hello.imageryCredits / otherCredits (the map)
local blockSize = 1024        -- the session's terrain block size (hello.blockSize); outlives levels
local guardFailed = false     -- the edge guard threw once: off for the rest of the game (logged)
local bannerInfo = { port = net.PORT } -- banner.update's inputs, filled every frame (no allocation)
local horizonOn = false       -- hello.horizon (M1h); outlives levels like the connection
local farRadius = nil         -- status.radius: the server's far-field radius (m), horizon mode only
local cacheInfo = nil         -- the server's last 'cache' message (download cache usage); outlives levels
local updateInfo = nil       -- a newer version on GitHub: { current, latest, url } (server updates.py)
local updateHidden = false   -- the notice closed for this session (its releases-page button)
local cacheClears = 0         -- answers to a clear so far (the settings page waits for the next one)

local function resetState()
  loadprogress.reset()
  st = {
    inOurLevel = false,
    active = false,          -- world logic running (rebase, far field, sky, pose)
    anchor = nil,            -- geodesy anchor of the committed frame
    anchorId = nil,
    anchorKey = nil,         -- server's opaque anchor key (protocol 3), for display
    anchorSession = nil,     -- the server session (hello.sessionId) the committed anchor belongs to
    resync = false,          -- a server said hello while an anchor is committed: the block cannot
                             -- follow until the resync (same origin, that server's anchor) commits
    pending = nil,           -- anchor request { requestId, purpose = 'teleport'|'rebase'|'resync', ready, msg }
    nextRequestId = 1,
    retryAt = 0,
    block = nil,             -- { cx, cy } centre of the committed terrain block, current anchor frame
    terrainPending = nil,    -- terrain-follow request { requestId, anchorId, cx, cy, ready, msg }
    -- frames a follow request spent waiting, by cause (the harness's view of why the block lags):
    -- server = not answered yet, gate = ready but its far tiles not live, governor = no heavy slot yet
    followWait = { server = 0, gate = 0, governor = 0 },
    followHeld = false, followHolds = 0, groundAhead = nil, -- the altitude rule (follow.altitudeHold)
    -- the last COMMIT_LOG follow commits, each { load, coll, cover (ms in the commit), f = { the wall ms of
    -- the commit frame and the two after it } } (what a block move costs, and where: GPU uploads land in
    -- the frames after)
    commits = {}, commitFrames = nil,
    terrainRetryAt = 0,
    rebasing = nil,          -- a rebase between its two phases (see commitRebase)
    lagProbe = nil,          -- watches, at onPreRender, when the last rebase's vehicle move became visible
    paused = false,
    lastFollowMs = nil, lastRebaseMs = nil, lastRebaseWait = nil, followCount = 0, rebaseCount = 0,
    clock = 0,
    sessionId = nil,
    status = '',
    placeFrames = nil,       -- countdown to placing the player after a teleport
    placeZ = nil,
    floorZ = nil,            -- a teleport into a tunnel: its floor (the car moved down once the tube collides)
    floorUntil = nil,
    poseTimer = 0,
    skyTimer = SKY_INTERVAL,
    autoTeleportDone = false,
    player = nil,            -- { x, y, z, vx, vy, lat, lon, dist, speed } of this frame
    cam = nil,               -- { x, y, z, fx, fy, fz, fovDeg } of this frame (core_camera), or nil
    loadingUntil = 0,        -- governor loading mode until this clock (after a teleport)
    slowFor = 0,             -- s the player has been below SLOW_SPEED
    serverBacklog = nil,     -- status.backlog of the server's last status (v1.4 M0), nil before / from v1.3.1
    anchorLatency = nil,     -- s from prepare_anchor to anchor_ready of the last rebase request (M4 stale rule)
    terrainLatency = nil,    -- s from prepare_terrain to terrain_ready of the last one (M4 follow re-check)
    visibleDistance = nil,   -- LevelInfo.visibleDistance set for the horizon (M1h); nil = VISIBLE_DISTANCE
    flat = false,            -- the level-start flat block is loaded (centred on the origin), no anchor block yet
    serverNote = nil,        -- the network note in the server's last status ('offline' / 'degraded', banner.noteOf)
    serverNoteDetail = nil,
    terrainErrorAt = nil,    -- clock of the last terrain_error since the last terrain arrived
    guard = edgeguard.new(), -- the edge guard's state (mode 'off' while the block can follow)
    guardBox = {},           -- the collidable area it guards (reused)
  }
end
resetState()

local function warnOnce(key, text)
  if warned[key] then return end
  warned[key] = true
  log('W', 'beamngEarth', text)
end

local function setStatus(text)
  st.status = text
  log('I', 'beamngEarth', text)
end

local function hypot(x, y) return math.sqrt(x * x + y * y) end

------------------------------------------------------------------------------------------
-- anchors

local function nextId()
  local id = st.nextRequestId
  st.nextRequestId = id + 1
  return id
end

local teleportTo -- defined with M.teleport (the level start's own teleport is one too)

-- the level start's teleport: to the place picked in the level select (levelinfo.lua: a saved place, or a random
-- road from the server), else to the window's lat/lon (the default start until it is changed)
local RANDOM_WAIT_S = 15 -- s for the server's random place (an older server never answers: the default start)
local function defaultStart()
  local lat, lon = ui.getLatLon(DEFAULT_LAT, DEFAULT_LON)
  return teleportTo(lat, lon, nil, 'start')
end
local function startTeleport()
  local choice = levelinfo.chosen()
  local id = levelinfo.placeId(choice)
  local p = id and places.find(id)
  if p then return teleportTo(p.lat, p.lon, p.name, 'start') end
  if choice == levelinfo.RANDOM and net.isConnected() then
    st.randomStart = { id = nextId(), t0 = st.clock }
    net.send({ type = 'random_place', id = st.randomStart.id })
    setStatus('Picking a random place...')
    return true
  end
  return defaultStart()
end

-- cx, cy: where the anchor's first terrain should be centred in the NEW frame
-- ((0,0) for a teleport, snap256(predicted) for a rebase; V1_1_SPEC 3).
-- place: { x, y } the player's spot in the new frame when it is not the origin (a teleport onto another BeamMP
-- player's anchor, mpsession.adoptAnchor).
local function requestAnchor(lat, lon, purpose, cx, cy, place)
  if not net.isConnected() then
    setStatus('Companion server not connected (127.0.0.1:' .. net.PORT .. ')')
    return false
  end
  local id = nextId()
  st.pending = { requestId = id, purpose = purpose, lat = lat, lon = lon, sentAt = st.clock }
  net.send({ type = 'prepare_anchor', requestId = id, lat = lat, lon = lon, cx = cx or 0, cy = cy or 0,
    px = place and place.x or nil, py = place and place.y or nil })
  return true
end

-- Where the player will be, in the new frame, when the rebase commits: the new anchor is the
-- player's position now, and the commit comes once the distance reaches rebase.distance.
-- M4 stale-anchor rule (rebase.KEEP_EDGE > 0, the 512² block): at or past the distance (a re-request
-- after a stale anchor was discarded) the terrain goes where the player will be when the answer
-- arrives, p + v * (measured anchor latency + 1 s), so a slow server cannot make it stale forever.
local function rebaseHint(pl)
  local rem = math.max(0, rebase.distance - pl.dist)
  if rem <= 0 and rebase.KEEP_EDGE > 0 then
    local lead = rebase.aheadLead(st.anchorLatency)
    local lat = lead - rebase.LEAD_EXTRA_S
    return follow.centreNear(pl.vx * lead, pl.vy * lead, pl.vx * lat, pl.vy * lat, follow.reachFor(pl.vx, pl.vy))
  end
  local px, py = 0, 0
  if pl.speed > 1 then px, py = pl.vx / pl.speed * rem, pl.vy / pl.speed * rem end
  return follow.target(px, py, pl.vx, pl.vy)
end

-- Terrain centre for a resync (same frame): where terrain-follow would put it now.
local function resyncHint()
  local pl = st.player
  if pl then return follow.target(pl.x, pl.y, pl.vx, pl.vy) end
  if st.block then return st.block.cx, st.block.cy end
  return 0, 0
end

-- R = I and t = 0 (a resync re-registers the same origin).
local function isIdentity(xf)
  local r, t = xf.r, xf.t
  return math.abs(t[1]) + math.abs(t[2]) + math.abs(t[3]) < 1e-6
    and math.abs(r[2]) + math.abs(r[3]) + math.abs(r[4]) + math.abs(r[6]) + math.abs(r[7]) + math.abs(r[8]) < 1e-12
end

-- The terrain an anchor_ready carries (V1_1_SPEC 3), or nil + error.
local function anchorTerrain(msg)
  local tr = msg.terrain
  if type(tr) ~= 'table' then return nil, 'anchor_ready without terrain' end
  if tr.size ~= terrain.SIZE then return nil, 'unsupported terrain size ' .. tostring(tr.size) end -- the session's (M4)
  if type(tr.heightmap) ~= 'string' or not FS:fileExists(tr.heightmap) then
    return nil, 'heightmap not found: ' .. tostring(tr.heightmap)
  end
  return tr
end

-- Import a terrain message ({cx, cy, heightmap, squareSize, zBase, maxHeight, q}) as the block.
-- The caller rebuilds collision. squareSize must be the 2.0 m lattice (lattice.lua); maxHeight is
-- q * 65536 with zBase a multiple of q (design 12, P1), checked here and logged once if not.
-- The server's .ter (terrain.swap) is taken whenever the block exists: ~12 ms, no collision rebuild
-- (the third result is then true; teleports and rebases rebuild it anyway), and it carries the edge
-- holes (heightmap.write_ter). The first import of a session takes the .ter right after it too.
local function loadBlock(tr)
  local x, y = follow.blockOrigin(tr.cx, tr.cy)
  local sq = tonumber(tr.squareSize) or follow.SQUARE_SIZE
  if math.abs(sq - follow.SQUARE_SIZE) > 1e-9 then
    warnOnce('square', string.format('server squareSize %.12g, mod lattice %.12g: terrain-follow geometry will be off',
      sq, follow.SQUARE_SIZE))
  end
  local maxH, warn = lattice.heightParams(tr)
  if warn then warnOnce('height', 'terrain height scale: ' .. warn) end
  local layers = (type(tr.layers) == 'table' and type(tr.materials) == 'table')
    and { masks = tr.layers, materials = tr.materials } or nil -- server --surface on (surface.py)
  if layers then groundcover.ensure() end
  -- detail mode: the block's aerial photo as its base colour (server write_base); else the level's
  -- (false: detail mode without a photo -> the level's flat base; nil: other modes, untouched)
  local base = nil
  if nearVisual == 'detail' then base = type(tr.base) == 'string' and tr.base or false end
  local ter = M.TER_SWAP and type(tr.ter) == 'string'
  if ter and terrain.swap(tr.ter, sq, maxH, x, y, tr.zBase, layers, base, tonumber(tr.baseSize)) then
    roads.terrainSwapped()      -- its collision follows by itself next frame: no other rebuild meanwhile
    groundcover.afterBlockChange()
    return true, nil, true
  end
  local ok, err = terrain.load(tr.heightmap, sq, maxH, x, y, tr.zBase, layers, base, tonumber(tr.baseSize))
  if ok and ter and terrain.swap(tr.ter, sq, maxH, x, y, tr.zBase, layers, base, tonumber(tr.baseSize)) then
    roads.terrainSwapped()
  end
  -- every block import / move drops all the grass on it; rebuild it before this frame is drawn
  groundcover.afterBlockChange()
  return ok, err
end

-- M1h: in horizon mode LevelInfo.visibleDistance follows the server's radius (+10 %, with
-- hysteresis, never below VISIBLE_DISTANCE, each change with postApply) and the sky uses the tiles'
-- planet radius; otherwise both go back to what they were (only if horizon mode changed them).
local function applyHorizon()
  if not st.active then return end
  if horizonOn then
    horizon.applySky()
    local v = horizon.visibleTarget(farRadius, st.visibleDistance or VISIBLE_DISTANCE, VISIBLE_DISTANCE)
    if v and horizon.setVisibleDistance(v) then st.visibleDistance = v end
  else
    horizon.restoreSky()
    if st.visibleDistance and horizon.setVisibleDistance(VISIBLE_DISTANCE) then st.visibleDistance = nil end
  end
end

local function activateWorld()
  if st.active then return end
  st.active = true
  -- bigMapMode.lua:866-867 sets the same LevelInfo field at runtime
  if scenetree.theLevelInfo then scenetree.theLevelInfo.visibleDistance = VISIBLE_DISTANCE end
  terrain.hideGroundPlanes()
  applyHorizon()
end

local function sendCommits(anchorId, cx, cy)
  net.send({ type = 'commit_anchor', anchorId = anchorId })
  net.send({ type = 'commit_terrain', anchorId = anchorId, cx = cx, cy = cy })
end

-- Teleport: switch to a ready anchor and place the player at its origin.
-- Motion blur across a frame change. The blur's velocity pass derives every pixel's motion from
-- the camera's world-space move since the last frame, with no teleport check
-- (shaders/common/postFx/motionBlur/clear.cs.hlsl:25-37): a rebase or teleport moves the camera
-- by kilometres and the frame smears at full strength (the "flash" seen in-game at rebases).
-- Its strength goes to 0 for a few frames and back (strength, not isEnabled: as the graphics
-- settings do it, core/settings/graphic.lua:1392). docs/research/2026-10-10-origin-distance.md.
local BLUR_HOLD_FRAMES = 4
local blurHold = { frames = 0, saved = nil }

local function holdMotionBlur()
  local fx = scenetree.PostFxMotionBlur
  if not fx then return end
  if blurHold.frames <= 0 then
    blurHold.saved = tonumber(fx.strength)
    if not blurHold.saved or blurHold.saved <= 0 then blurHold.saved = nil; return end
    fx.strength = 0
  end
  blurHold.frames = BLUR_HOLD_FRAMES
end

local function tickMotionBlur()
  if blurHold.frames <= 0 then return end
  blurHold.frames = blurHold.frames - 1
  if blurHold.frames > 0 then return end
  local fx = scenetree.PostFxMotionBlur
  -- the player may have changed the setting meanwhile (then it is no longer 0): leave theirs
  if fx and blurHold.saved and (tonumber(fx.strength) or 0) == 0 then fx.strength = blurHold.saved end
  blurHold.saved = nil
end

local function commitTeleport(msg)
  local tr, err = anchorTerrain(msg)
  gov:heavyNow() -- the user asked for a jump: no waiting for a calm frame
  local sw = stopwatch.new()
  local ok
  if tr then ok, err = loadBlock(tr) end
  -- a new place: grass made fresh (with tools/gcpatch the engine keeps it across block moves)
  if ok then groundcover.rebuild() end
  if not ok then
    setStatus('Terrain import failed: ' .. tostring(err))
    st.retryAt = st.clock + rebase.RETRY_S -- don't re-request every frame
    return false
  end
  farfield.switchAnchor(msg.anchorId, false, { cx = tr.cx, cy = tr.cy }, st.sessionId)
  st.loadingUntil = st.clock + LOADING_AFTER_TELEPORT_S
  st.placeFrames = PLACE_DELAY_FRAMES
  st.placeZ = (msg.groundZ or 0) + 1.0
  st.placeX, st.placeY = tonumber(msg.px) or 0, tonumber(msg.py) or 0
  -- in a tunnel: set down on the ground over it first (placed in the tube before its road tile came, the car fell
  -- through the world under the one-sided terrain), moved down onto the floor once that collides (2026-10-17)
  st.floorZ = msg.floorZ
  st.floorUntil = msg.floorZ and (st.clock + FLOOR_WAIT_S) or nil
  -- The player's old coordinates may now lie anywhere on the new terrain.
  -- Lift it above the new origin in this same frame (velocity zeroed as in spawn.lua:532-534)
  -- so physics never sees that; placePlayer() then sets it down properly.
  local veh = getPlayerVehicle(0)
  holdMotionBlur()
  if veh then
    veh:setClusterPosRelRot(-1, st.placeX, st.placeY, st.placeZ + PRE_LIFT, 0, 0, 0, 1)
    veh:applyClusterVelocityScaleAdd(-1, 0, 0, 0, 0)
  end
  be:reloadCollision()

  st.anchor, st.anchorId, st.anchorKey = geodesy.newAnchor(msg.lat, msg.lon), msg.anchorId, msg.anchorKey
  st.anchorSession, st.resync = st.sessionId, false
  mpanchor.announceOwn(msg.lat, msg.lon)
  st.block, st.terrainPending, st.terrainErrorAt = { cx = tr.cx, cy = tr.cy }, nil, nil
  edgeguard.newFrame(st.guard)
  sendCommits(msg.anchorId, tr.cx, tr.cy)
  activateWorld()
  st.skyTimer, st.poseTimer = SKY_INTERVAL, POSE_INTERVAL -- refresh both right away
  setStatus(string.format('Anchor #%s at %.5f, %.5f (teleport, %.0f ms)', tostring(msg.anchorId),
    msg.lat, msg.lon, sw()))
  return true
end

-- Second phase of a rebase: everything except the vehicles, in one frame, from onUpdate.
local function finishRebase()
  local rb = st.rebasing
  st.rebasing = nil
  gov:heavyNow() -- phase 2 must follow phase 1's vehicle move; its frame is a heavy slot anyway
  local sw = stopwatch.new()
  local msg, tr, xf = rb.msg, rb.terrain, rb.xf
  st.recouple = rebase.recoupleStart(rb.couplings, st.clock)   -- (trailers hitched again: rebase.lua)
  local ok, err = loadBlock(tr)
  -- the world shifts under the grass: made fresh (with tools/gcpatch the engine would keep it)
  groundcover.rebuild()
  if not ok then log('E', 'beamngEarth', 'rebase terrain import failed: ' .. tostring(err)) end
  farfield.transform(xf)
  -- roads, buildings, street furniture and trees move with it and stay until the new anchor's tiles cover them
  roads.transform(xf, geodesy)
  trees.transform(xf, geodesy)
  sea.transform(xf, msg.anchorId)
  -- a resync with a restarted server keeps the previous session's tiles the same way (st.sessionId
  -- is the new session's): they go once the new session's tiles cover them
  farfield.switchAnchor(msg.anchorId, true, { cx = tr.cx, cy = tr.cy }, st.sessionId)
  local cs = camshift.shift(xf, rb.playerOld[1], rb.playerOld[2], rb.playerOld[3])
  be:reloadCollision()

  st.anchor, st.anchorId, st.anchorKey = rb.newAnchor, msg.anchorId, msg.anchorKey
  st.anchorSession, st.resync = st.sessionId, false
  mpanchor.announceOwn(st.anchor.lat, st.anchor.lon)
  st.block, st.terrainPending, st.terrainErrorAt = { cx = tr.cx, cy = tr.cy }, nil, nil
  edgeguard.newFrame(st.guard)
  sendCommits(msg.anchorId, tr.cx, tr.cy)
  activateWorld()
  st.skyTimer, st.poseTimer = SKY_INTERVAL, POSE_INTERVAL
  st.lastRebaseMs, st.lastRebaseWait = rb.phase1Ms + sw(), rb.waited
  st.rebaseCount = st.rebaseCount + 1
  setStatus(string.format('Anchor #%s at %.5f, %.5f (%s, %.0f ms, %.3f deg, phase 2 after %d frame(s), camera: %d points%s)%s',
    tostring(msg.anchorId), msg.lat, msg.lon, rb.purpose, st.lastRebaseMs, math.deg(xf:angle()), rb.waited,
    cs.points + cs.triples, cs.viaDebug and '' or ', no camData', ok and '' or ' TERRAIN FAILED'))
end

-- Is the player's move visible to GE yet (veh:getPosition, the value the camera reads at
-- cameraUpdate.lua:145)? Nearer the new position than the old one = moved.
local function moveVisible(probe)
  local veh = getPlayerVehicle(0)
  local m = probe.playerId and probe.moved[probe.playerId]
  if not veh or not m or veh:getID() ~= probe.playerId then return true end
  local p = veh:getPosition()
  local function d2(q) return (p.x - q[1]) ^ 2 + (p.y - q[2]) ^ 2 + (p.z - q[3]) ^ 2 end
  return d2(m.new) < d2(m.old)
end

-- Origin rebase (V1_1_SPEC 1b), purpose 'rebase' or 'resync'.
--
-- Timing. GE calls update() -> onUpdate "after input and before physics" and luaPreRender() ->
-- onPreRender "right before the rendering, and after running the physics" (main.lua:819-824, 920),
-- and the camera is computed in core_camera's onPreRender (camera.lua: M.onPreRender =
-- cameraUpdate.onPreRender). setClusterPosRelRot is a request to the physics side, so the vehicle
-- is expected to show up at its new position in the onPreRender of the SAME frame. Everything else
-- (terrain + collision, far tiles, the camera's state) is changed directly by Lua and must change
-- in the onUpdate of the frame whose physics step applies the move: then physics never sees the
-- car and the ground in different frames, and the camera never renders the car in one frame with
-- its own history (or the world) in the other.
-- Phase 1 (vehicles) runs now; phase 2 (finishRebase) runs `st.rebaseLag` simulated frames later,
-- in onUpdate. rebaseLag starts at 0 (same frame) and is measured on every rebase: onPreRender
-- records after how many frames the move became visible, and the next rebase uses that.
local function commitRebase(msg, purpose)
  local tr, err = anchorTerrain(msg)
  if not tr then
    setStatus('Rebase failed: ' .. tostring(err))
    st.retryAt = st.clock + rebase.RETRY_S
    return false
  end
  local sw = stopwatch.new()
  local newAnchor = geodesy.newAnchor(msg.lat, msg.lon)
  local xf = st.anchor:transformTo(newAnchor)
  -- nothing to move for an identity transform (resync); setClusterPosRelRot from the GE-side
  -- position would only pull the car back to where GE last saw it
  local moved, playerId = {}, nil
  local couplings = {}
  if not isIdentity(xf) then
    holdMotionBlur()
    couplings = rebase.couplings()      -- (re-hitched after phase 2: rebase.recoupleStart)
    moved, playerId = rebase.moveVehicles(xf, geodesy)
  end
  local m = playerId and moved[playerId]
  st.rebasing = { msg = msg, terrain = tr, purpose = purpose, newAnchor = newAnchor, xf = xf,
    wait = rebaseLag, waited = 0,
    playerOld = m and m.old or { st.player and st.player.x or 0, st.player and st.player.y or 0, st.player and st.player.z or 0 },
    couplings = couplings }
  st.lagProbe = { moved = moved, playerId = playerId, frames = 0 }
  st.rebasing.phase1Ms = sw()
  if moveVisible(st.lagProbe) then
    st.lagProbe = nil -- applied synchronously: no lag at all
    st.rebasing.wait = 0
  end
  if st.rebasing.wait <= 0 then finishRebase() end
  return true
end

local function commitAnchor(msg, purpose)
  if st.rebasing then finishRebase() end -- never mix two frame changes
  if purpose == 'teleport' or not st.anchor then return commitTeleport(msg) end
  return commitRebase(msg, purpose)
end

-- Terrain-follow commit (V1_1_SPEC 1a, V1_2_DESIGN 2.3), in a governor heavy slot, atomic in one
-- frame: importMaps + setPosition + reloadCollision + the far tiles' cover change. Vehicles and
-- camera stay where they are; far tiles only sink / hide / come back (no imports in this frame).
local COMMIT_LOG = 20
local COMMIT_SLOW_MS = 300     -- a commit frame this slow is logged with its materials
local frameTimer = hptimer and hptimer() or nil

local function commitTerrain(msg)
  local sw = stopwatch.new()
  local ok, err, swapped = loadBlock(msg)
  if not ok then
    setStatus('Terrain-follow import failed: ' .. tostring(err))
    st.terrainRetryAt = st.clock + follow.RETRY_S
    return false
  end
  local tLoad = sw()
  if not swapped then             -- a swapped .ter collides by the next frame by itself
    be:reloadCollision()
    roads.collisionRebuilt()      -- the sidewalks made so far collide now too
  end
  local tColl = sw()
  farfield.onBlockCommit(msg.cx, msg.cy)
  local ls = swapped and terrain.lastSwap or nil
  local c = { load = tLoad, coll = tColl - tLoad, cover = sw() - tColl, f = {}, swap = swapped or nil,
    ter = ls and ls.apply or nil,   -- the .ter load alone (ms)
    mats = ls and ls.mats or nil, fresh = ls and ls.fresh or nil, at = { msg.cx, msg.cy } }
  table.insert(st.commits, c)
  if #st.commits > COMMIT_LOG then table.remove(st.commits, 1) end
  st.commitFrames = c
  st.block = { cx = msg.cx, cy = msg.cy }
  net.send({ type = 'commit_terrain', anchorId = st.anchorId, cx = msg.cx, cy = msg.cy })
  st.lastFollowMs = sw()
  st.followCount = st.followCount + 1
  log('I', 'beamngEarth', string.format('terrain block now at (%d, %d), %.1f ms', msg.cx, msg.cy, st.lastFollowMs))
  return true
end

-- Put the player vehicle on the new ground at the origin, upright, keeping its heading.
-- spawn.safeTeleport with resetVehicle=true resets the vehicle, drops its bounding box onto
-- the ground (spawn.lua:494 getBBOnGround) and zeroes velocity (spawn.lua:533-534).
-- The heading-preserving quat follows main.lua:1306.
local function placePlayer(z)
  local veh = getPlayerVehicle(0)
  if not veh then return end
  local dir = veh:getDirectionVector()
  dir = vec3(dir.x, dir.y, 0)
  if dir:length() < 1e-3 then dir = vec3(0, 1, 0) end
  local x, y = st.placeX or 0, st.placeY or 0
  if x ~= 0 or y ~= 0 then
    -- away from the origin (a BeamMP player's anchor): the server's height there is the raw elevation data; set down
    -- on the collision under it (a graded road can be a few metres off)
    local ray = rawget(_G, 'castRayStatic')
    local d = ray and ray(vec3(x, y, z + 60), vec3(0, 0, -1), 200)
    if d and d < 200 then z = z + 60 - d + 1.0 end
  end
  spawn.safeTeleport(veh, vec3(x, y, z), quatFromDir(dir:normalized(), vec3(0, 0, 1)), nil, nil, false, nil, true)
end

-- A teleport into a tunnel: once a ray down from just over its floor (floorZ) meets collision at the floor (the tube's
-- road tile is in), the player goes down onto it; after FLOOR_WAIT_S it stays where it is (on the ground over it).
function M.floorCheck()
  if st.clock > (st.floorUntil or 0) then
    st.floorZ, st.floorUntil = nil, nil
    return
  end
  if not roads.settledFor(st.anchorId, 0, 0) then return end
  local ray = rawget(_G, 'castRayStatic')
  if not ray then return end
  local from = vec3(0, 0, st.floorZ + 2)
  local d = ray(from, vec3(0, 0, -1), 6)
  if d and math.abs((from.z - d) - st.floorZ) < FLOOR_HIT_M then
    local z = st.floorZ
    st.floorZ, st.floorUntil = nil, nil
    log('I', 'beamngEarth', string.format('teleport: down onto the tunnel floor at %.1f', z))
    placePlayer(z + 1.0)
  end
end

------------------------------------------------------------------------------------------
-- messages

local function formatKnown(f)
  for _, v in ipairs(FORMATS) do if v == f then return true end end
  return false
end

local geocodeResults = {}       -- the world map's address search answers: id -> { results, error }
local routeResults = {}         -- the GPS's planned routes: id -> { points, dist, dur, error }

-- The pooled shape slots (far tiles, road / building tiles) let go since the last pose, for this server; nil if none.
local function slotsGoneNow()
  local out = farfield.takeSlotsGone(st.sessionId) or {}
  for _, g in ipairs(roads.takeSlotsGone(st.sessionId)) do out[#out + 1] = g end
  return #out > 0 and out or nil
end

local function onMessage(msg)
  local t = msg.type
  if t == '_connected' then
    -- sessions: the session folders our far tiles still use (a restarted server keeps them)
    -- slotsHeld: the pooled shape slots our objects hold, if the server is the one that sent them (a reconnect)
    local held
    if farfield.session() then
      held = farfield.slotsHeld(farfield.session())
      for _, h in ipairs(roads.slotsHeld(farfield.session())) do held[#held + 1] = h end
    end
    net.send({ type = 'hello', protocol = PROTOCOL, format = FORMAT, modVersion = MOD_VERSION,
      sessions = farfield.heldSessions(), slotSession = farfield.session(), slotsHeld = held })
    setStatus('Connected to companion server')
  elseif t == '_disconnected' then
    st.pending, st.sessionId, st.terrainPending = nil, nil, nil
    cacheInfo = nil
    st.serverNote, st.serverNoteDetail, st.terrainErrorAt = nil, nil, nil
    -- an auto-teleport lost with the connection is retried on the next hello
    if not st.anchor then st.autoTeleportDone = false end
    sea.clear()
    trees.clear()
    setStatus('Disconnected from companion server: ' .. tostring(msg.error))
  elseif t == 'hello' then
    st.sessionId = msg.sessionId
    st.refreshLater = nil       -- (a restarted server numbers its anchors from 1 again)
    roads.setSession(msg.sessionId)   -- the road / building tiles' pooled shape files belong to this server
    lastHello = {}           -- the server's version and options, for the reports' hardware.json
    for k, v in pairs(msg) do if k ~= 'attribution' and type(v) ~= 'table' then lastHello[k] = v end end
    -- road tiles belong to the session that sent them: a restarted server (anchor ids start again
    -- at 1) never drops the old session's tiles, so they stayed forever (2026-10-11: meshes of four
    -- sessions in the scene, some built by older code)
    if roadsSession ~= msg.sessionId then
      roads.clear()
      roadsSession = msg.sessionId
    end
    roads.prewarm(msg.prewarm)  -- the materials of the server's shapes set up now, not at their first tile
    st.serverNote, st.serverNoteDetail = nil, nil
    -- a new server session: its own gravity message follows the first pose (none with --gravity off)
    local w = rawget(_G, 'beamngEarth_weather')
    if w and w.clearGravity then w.clearGravity() end
    st.deshadow = msg.deshadow -- server-side imagery de-shadowing ('lambert' / 'full'; nil = off), shown on the settings page
    st.colormatch = msg.colormatch -- server-side imagery colour-seam removal ('s2'; nil = off), likewise
    seasonlook.setMode(msg.season) -- the imagery's season (server --season): the leaves follow it
    st.mapPort = msg.mapPort -- the world map's tile endpoint on 127.0.0.1 (server/maptiles.py; nil = an older server)
    -- the download cache: the new server's usage follows its hello; it gets the page's size cap (the
    -- server keeps it for its next start too)
    cacheInfo = nil
    local settings = rawget(_G, 'beamngEarth_settings')
    if settings and settings.cacheCapGb then net.send({ type = 'cache', capGb = settings.cacheCapGb() }) end
    -- the on-screen credits: this session's own follow (the attribution line until then)
    credits.clear()
    -- M4: the terrain block size first (everything below may use the lattice / follow table); a
    -- server without hello.blockSize has the v1.3.1 1024² block
    local size, okSize = M.setBlockSize(msg.blockSize or 1024)
    if not okSize then
      setStatus('Unsupported terrain block size ' .. tostring(msg.blockSize) .. ': using ' .. size)
    end
    if msg.protocol ~= PROTOCOL then
      setStatus('Protocol mismatch: server ' .. tostring(msg.protocol) .. ', mod ' .. PROTOCOL)
    elseif msg.format ~= nil and not formatKnown(msg.format) then
      setStatus('Format mismatch: server ' .. tostring(msg.format) .. ', mod ' .. table.concat(FORMATS, '/'))
    end
    -- M1 (FORMAT 4): the tiles' cells per edge; a FORMAT 3 server sends none (32). It sets the
    -- crossover's fallback error and the exact (physics lattice) level of the exactMiss metric.
    local cells = crossover.setCells(msg.cells)
    farfield.setExactLevel(farfield.exactLevelFor(cells))
    -- every far tile references the server's material name; this mod ships (and loads) one
    -- static material (farfield.MATERIALS_FILE). A different name renders every tile without it.
    if msg.material ~= nil and msg.material ~= farfield.MATERIAL then
      warnOnce('material', 'far-tile material mismatch: server ' .. tostring(msg.material) .. ', mod ' .. farfield.MATERIAL)
      setStatus('Far-tile material mismatch: server ' .. tostring(msg.material) .. ', mod ' .. farfield.MATERIAL)
    end
    -- protocol 4: who draws the near ground (a protocol-3 server sends nothing: the terrain does),
    -- and the credit line for the imagery / elevation sources the server actually uses
    M.setNearVisual(msg.nearVisual)
    -- M1h: horizon mode (a server without hello.horizon: the level's sky and 100 km view distance)
    M.setHorizon(msg.horizon == true)
    serverAttribution = (type(msg.attribution) == 'string' and msg.attribution ~= '') and msg.attribution or nil
    -- per imagery source: its credit and its area (the world map credits what its view shows)
    serverCreditsByArea = type(msg.imageryCredits) == 'table' and msg.imageryCredits or nil
    serverOtherCredits = type(msg.otherCredits) == 'string' and msg.otherCredits or nil
    pcall(places.syncLevel)     -- the level select's place pictures (the server makes the missing ones)
    if st.anchor then
      -- A (re)started server knows nothing of our anchor: re-register the SAME geodetic origin under
      -- a new anchorId. The transform is then the identity, so nothing in the world moves (a shift
      -- here could happen close to the origin, where camshift cannot tell positions from offsets).
      -- Until it commits the block cannot follow (st.resync: the edge guard runs, a failed resync is
      -- asked again in updateWorld).
      st.resync = true
      requestAnchor(st.anchor.lat, st.anchor.lon, 'resync', resyncHint())
    elseif st.inOurLevel and not st.anchor and not st.autoTeleportDone then
      st.autoTeleportDone = true
      startTeleport()
    end
  elseif t == 'status' then
    st.status = tostring(msg.text)
    -- the server's network note ("Offline - using cached data" / "Degraded: ..."): the banner shows it
    st.serverNote, st.serverNoteDetail = banner.noteOf(msg.text)
    -- v1.4 M0: the server's far-tile backlog (desired - shown) rides on every status; nil from a
    -- v1.3.1 server. For the harness (status()), nothing here acts on it.
    st.serverBacklog = tonumber(msg.backlog)
    -- M1h: the far-field radius rides on every status in horizon mode (the view distance follows it)
    if msg.radius ~= nil then M.setFarRadius(msg.radius) end
  elseif t == 'load_progress' then      -- the loading line's counts (server Connection.send_progress)
    st.loadProgress = msg
  elseif t == 'share' then              -- our world server serving our BeamMP group (mpsession.lua, peerwork.py)
    pcall(mpsession.onShare, msg)
  elseif t == 'terrain_patch' then
    pcall(terrainpatch.onPatch, msg)
  elseif t == 'anchor_ready' then
    local p = st.pending
    if not p or msg.requestId ~= p.requestId then return end -- stale answer
    if p.purpose == 'rebase' or (p.purpose == 'resync' and st.anchor) then
      p.ready, p.msg = true, msg -- committed in updateWorld() (distance rule / when not paused)
      if p.purpose == 'rebase' and p.sentAt then st.anchorLatency = st.clock - p.sentAt end
    else
      st.pending = nil
      commitAnchor(msg, p.purpose)
    end
  elseif t == 'anchor_error' then
    local p = st.pending
    if not p or msg.requestId ~= p.requestId then return end
    st.pending = nil
    st.retryAt = st.clock + rebase.RETRY_S
    setStatus('Anchor request failed: ' .. tostring(msg.error))
  elseif t == 'terrain_ready' then
    local p = st.terrainPending
    if not p or msg.requestId ~= p.requestId or msg.anchorId ~= st.anchorId then return end -- stale
    if msg.size ~= terrain.SIZE or msg.cx ~= p.cx or msg.cy ~= p.cy then
      st.terrainPending, st.terrainRetryAt = nil, st.clock + follow.RETRY_S
      setStatus('Unexpected terrain_ready (size ' .. tostring(msg.size) .. ')')
      return
    end
    p.ready, p.msg = true, msg -- committed by the distance rule in updateWorld()
    if p.sentAt then st.terrainLatency = st.clock - p.sentAt end
    st.terrainErrorAt = nil
  elseif t == 'terrain_refresh' then
    -- the graded block in place of a teleport's provisional one (server make_provisional; the user, 2026-10-22:
    -- "place the car as soon as possible"): swapped in while that block is still ours, else dropped (the block
    -- moved on: its replacements are built graded anyway)
    -- A refresh for an anchor not committed yet is kept for the commit: a cached place's graded block came 1 s after
    -- the provisional one while the teleport's commit waited out a 1.9 s level-load frame, and the provisional block
    -- (no road grading, ground through the roads) stayed until the car drove off (BeamMP join, 2026-10-24)
    if msg.anchorId ~= st.anchorId then
      st.refreshLater = msg
    elseif st.block and msg.cx == st.block.cx and msg.cy == st.block.cy and msg.size == terrain.SIZE then
      commitTerrain(msg)
    end
  elseif t == 'terrain_error' then
    local p = st.terrainPending
    if not p or msg.requestId ~= p.requestId then return end
    st.terrainPending, st.terrainRetryAt = nil, st.clock + follow.RETRY_S
    st.terrainErrorAt = st.clock -- the edge guard holds the car meanwhile
    setStatus('Terrain request failed: ' .. tostring(msg.error))
  elseif t == 'roads_tile' then
    roads.onTile(msg, st.anchorId)
  elseif t == 'roads_drop' then
    roads.onDrop(msg)
  elseif t == 'far_tile' then
    farfield.onTile(msg)
  elseif t == 'far_swap' then
    farfield.onSwap(msg)
  elseif t == 'far_remove' then
    farfield.onRemove(msg)
  elseif t == 'far_done' then
    farfield.onDone(msg)
  elseif t == 'far_retex' then
    farfield.onRetex(msg) -- a shown tile's re-rendered texture (--colormatch / --deshadow)
  elseif t == 'weather' then
    -- real weather (server weather.py): applied by the separate beamngEarth_weather extension
    local w = rawget(_G, 'beamngEarth_weather')
    if w and w.onWeather then w.onWeather(msg) end
  elseif t == 'gravity' then
    -- real gravity at the player (server gravity.py): applied by beamngEarth_weather's setGravity path
    local w = rawget(_G, 'beamngEarth_weather')
    if w and w.onGravity then w.onGravity(msg) end
  elseif t == 'server_list' then
    -- the BeamNG.Earth servers for the tab in BeamMP's server browser (serverlist.lua)
    local sl = rawget(_G, 'beamngEarth_serverlist')
    if sl and sl.onList then sl.onList(msg) end
  elseif t == 'block_base' then
    -- detail mode: a block's base photo rendered again with its NAIP tiles (server base_refresh): the block
    -- in use takes it now, a ready one not committed yet takes it at its commit
    if nearVisual == 'detail' and msg.anchorId == st.anchorId and type(msg.base) == 'string' then
      local b, tp = st.block, st.terrainPending
      if b and b.cx == msg.cx and b.cy == msg.cy and FS:fileExists(msg.base) then
        terrain.setBase(msg.base, tonumber(msg.baseSize))
        st.baseRefreshes = (st.baseRefreshes or 0) + 1
      end
      if tp and tp.msg and tp.msg.cx == msg.cx and tp.msg.cy == msg.cy then tp.msg.base = msg.base end
    end
  elseif t == 'ground_ahead' then
    -- the highest ground near the player's path (server groundahead.py): the altitude rule (follow.lua)
    if tonumber(msg.z) then st.groundAhead = { anchorId = msg.anchorId, z = tonumber(msg.z), at = st.clock } end
  elseif t == 'veg_tile' then
    -- trees, bushes and rocks of a tile (server vegetation.py, --trees on): Forest items (trees.lua)
    trees.onTile(msg, st.anchorId)
  elseif t == 'veg_drop' then
    trees.onDrop(msg)
  elseif t == 'bldfar_tile' then
    -- far buildings (server buildings.far_tile, --buildings on): render-only, roads.lua
    roads.onFarTile(msg, st.anchorId)
  elseif t == 'bldfar_drop' then
    roads.onFarDrop(msg)
  elseif t == 'where_result' then
    statusbar.onResult(msg)
  elseif t == 'place_previews_ready' then      -- the level select's place pictures are there (levelinfo.lua)
    pcall(places.syncLevel)
  elseif t == 'random_place_result' then
    local r = st.randomStart
    if r and msg.id == r.id then
      st.randomStart = nil
      local lat, lon = tonumber(msg.lat), tonumber(msg.lon)
      if lat and lon and not st.anchor then teleportTo(lat, lon, msg.name, 'start')
      elseif not st.anchor then defaultStart() end
    end
  elseif t == 'route_result' then
    if msg.id then routeResults[msg.id] = { points = msg.points or {}, dist = msg.dist, dur = msg.dur, error = msg.error } end
  elseif t == 'geocode_result' then
    if msg.id then geocodeResults[msg.id] = { results = msg.results or {}, error = msg.error } end
  elseif t == 'map_tiles_ready' then
    -- a world map tile file is written (settings.mapTiles: the map polls every 250 ms while it misses tiles)
  elseif t == 'navgraph' then
    -- the AI road graph around the player (server navgraph.py, --traffic on): map.reset (navgraph.lua)
    navgraph.onMessage(msg, st.anchorId)
  elseif t == 'sea' then
    -- the sea's water tiles near the player (server seatiles.py, --sea on): sea.lua
    sea.onMessage(msg)
  elseif t == 'credits' then
    -- the sources around the player (server credits.py): msg.near, msg.far, drawn by credits.lua
    credits.onMessage(msg)
  elseif t == 'update' then
    -- a newer version on GitHub (server updates.py): said once a session in game, and on the settings page
    if type(msg.latest) == 'string' then
      updateInfo = { current = tostring(msg.current or ''), latest = msg.latest, url = tostring(msg.url or '') }
    end
  elseif t == 'update_status' then
    -- the "Update now" download (server selfupdate.py): progress, ready, error; shown in the update notice
    if updateInfo then
      updateInfo.status = { state = tostring(msg.state or ''), text = tostring(msg.text or ''),
        done = tonumber(msg.done) or 0, total = tonumber(msg.total) or 0 }
      if msg.state == 'ready' or msg.state == 'error' then updateHidden = false end
    end
  elseif t == 'cache' then
    -- the download cache (server cache.py): its usage for the settings page; clearedBytes answers a clear
    local prev = cacheInfo
    cacheInfo = { usedBytes = tonumber(msg.usedBytes) or 0, capBytes = tonumber(msg.capBytes) or 0,
      files = tonumber(msg.files) or 0, ready = msg.ready == true, managed = msg.managed ~= false,
      clearedBytes = prev and prev.clearedBytes, clears = cacheClears }
    if msg.clearedBytes ~= nil then
      cacheClears = cacheClears + 1
      cacheInfo.clearedBytes, cacheInfo.clears = tonumber(msg.clearedBytes) or 0, cacheClears
    end
  end
end

------------------------------------------------------------------------------------------
-- per frame

-- The screen's height in pixels (GFXDevice.getVideoMode, looked up every 2 s): the server's screen-space LOD.
local screenH, screenAt = nil, -math.huge
local function screenHeight()
  local t = os.clock()
  if t - screenAt > 2 then
    screenAt = t
    local ok, vm = pcall(function() return GFXDevice.getVideoMode() end)
    screenH = ok and type(vm) == 'table' and tonumber(vm.height) or screenH
  end
  return screenH
end

-- The rendering camera (core_camera, core/camera.lua:1927-1937: getPositionXYZ, getForwardXYZ,
-- getFovDeg), for the server's view-angle priority and the off-screen swap test, and the screen's height (h)
-- for its screen-space LOD. nil if unavailable.
local function cameraState()
  local cam = rawget(_G, 'core_camera')
  if not cam or not cam.getPositionXYZ then return nil end
  local ok, x, y, z = pcall(cam.getPositionXYZ)
  if not ok or type(x) ~= 'number' then return nil end
  local okF, fx, fy, fz = pcall(cam.getForwardXYZ)
  if not okF or type(fx) ~= 'number' then return nil end
  local okV, fov = pcall(cam.getFovDeg)
  return { x = x, y = y, z = z, fx = fx, fy = fy, fz = fz, fovDeg = (okV and type(fov) == 'number') and fov or 60,
    h = screenHeight() }
end

-- The camera's {x, y} when it is more than VIEW_AWAY_M from the car (a free camera, a far chase camera): what is drawn
-- follows it (trees here; the server's far field, tree and far building tiles from the pose's cam), what the car drives
-- on stays round the car. nil otherwise.
M.VIEW_AWAY_M = 50
function M.viewAway()
  local c, p = st.cam, st.player
  if not c or not p or not c.x or not p.x then return nil end
  local dx, dy = c.x - p.x, c.y - p.y
  if dx * dx + dy * dy <= M.VIEW_AWAY_M * M.VIEW_AWAY_M then return nil end
  return { x = c.x, y = c.y }
end

local function updatePlayer(dt)
  st.player = nil
  st.cam = cameraState()
  local veh = getPlayerVehicle(0)
  if not st.anchor then return end
  if not veh then
    -- no car at all (a BeamMP server is joined without one, a free camera after the car was removed): the world
    -- follows the camera, standing still (poses, terrain, rebases; nothing moves a vehicle)
    local c = st.cam
    if not c then return end
    local lat, lon = st.anchor:enuToGeodetic(c.x, c.y, c.z)
    st.player = { x = c.x, y = c.y, z = c.z, vx = 0, vy = 0, vz = 0, lat = lat, lon = lon, dist = hypot(c.x, c.y),
      speed = 0, camera = true }
    st.slowFor = st.slowFor + dt
    return
  end
  local p = veh:getPosition()
  local v = veh:getVelocity()
  local lat, lon = st.anchor:enuToGeodetic(p.x, p.y, p.z)
  st.player = { x = p.x, y = p.y, z = p.z, vx = v.x, vy = v.y, vz = v.z, lat = lat, lon = lon, dist = hypot(p.x, p.y),
    speed = v:length() }
  st.slowFor = st.player.speed < SLOW_SPEED and (st.slowFor + dt) or 0
end

-- Drop hold (drophold.lua): a car dropped far off the block (the game's drop-at-camera) is held at rest where it was
-- dropped until the block that follows it is committed and has ground under it.
local dropHold = drophold.new()
local dropHoldFailed = false
M.dropHoldState = function() return dropHold end      -- (tests, tools)

-- The game's own pause (simTimeAuthority, what the pause key does): holding the car by setting its position every frame
-- strained it (its damage rose 0 -> 93k in 7 s of holding, the user 2026-10-18: "the hold seems to damage my car",
-- "can you use the actual pause system"). Resumed only if the hold paused it; the player resuming it ends the hold.
-- (in a BeamMP session: our own car held, not every car on the screen: simhold.lua)
local simhold = require('beamngEarth/simhold')
local function simPause(on) simhold.pause(on) end
local function simPaused() return simhold.paused() end

local function setCarAt(veh, x, y, z)
  -- the cluster moves by the reference node: set it where it is relative to the vehicle's position, at the target
  local ref = veh:getRefNodeId()
  local rel = veh:getNodePosition(ref)
  veh:setClusterPosRelRot(ref, x + rel.x, y + rel.y, z + rel.z, 0, 0, 0, 1)
  veh:applyClusterVelocityScaleAdd(ref, 0, 0, 0, 0)
end

local function endHold(h)
  if h.pausedByUs and simPaused() then simPause(false) end
  h.pausedByUs = nil
  roads.setHold(false)
end

local function updateDropHold(dt)
  local pl = st.player
  local veh = getPlayerVehicle(0)
  if not pl or not veh then return end
  local box = nil
  if st.block then
    local b = st.dropBox or {}
    st.dropBox = b
    b[1], b[2], b[3], b[4] = lattice.groundBox(st.block.cx, st.block.cy)
    box = b
  end
  local h = dropHold
  if h.active and h.last and h.last.anchorId ~= st.anchorId then drophold.moved(h, pl.x, pl.y, pl.z) end
  roads.setHold(h.active)
  local busy = st.placeFrames ~= nil or st.floorZ ~= nil
  -- (no server: no road tiles will come, nothing to wait for)
  local nearReady = function(x, y) return not net.isConnected() or roads.readyAround(st.anchorId, x, y, drophold.NEAR_M) end
  if not st.paused and drophold.observe(h, pl.x, pl.y, pl.z, pl.speed, dt, st.anchorId, box, busy, nearReady) == 'start' then
    log('I', 'beamngEarth', string.format('drop hold: the car is %.0f m from the block; paused until its ground, bridges ' ..
      'and buildings load', box and math.max(box[1] - pl.x, pl.x - box[3], box[2] - pl.y, pl.y - box[4], 0) or -1))
    loadprogress.start(st.clock)    -- a jump the mod did not make (F7, the world editor): the bar says "Loading" too
    if not simPaused() then
      simPause(true)
      h.pausedByUs = true
    end
  end
  roads.setHold(h.active)
  if not h.active then return end
  if h.pausedByUs and not simPaused() then         -- the player resumed: their call
    drophold.cancel(h)
    h.pausedByUs = nil
    roads.setHold(false)
    log('I', 'beamngEarth', 'drop hold: resumed by the player')
    return
  end
  local gz = nil
  if box and drophold.inside(box, h.x, h.y, drophold.EDGE_MARGIN) then
    local ray = rawget(_G, 'castRayStatic')
    if ray then
      -- what is under the drop point first (a drop under a bridge: the road below, not the deck); nothing (the drop
      -- point under the ground): from high above, the top surface
      for _, up in ipairs({ 2.5, 600 }) do
        local from = vec3(h.x, h.y, h.z + up)
        local d = ray(from, vec3(0, 0, -1), 3000)
        if d and d < 3000 then gz = from.z - d; break end
      end
    end
  end
  local serial = st.block and (st.block.cx .. ',' .. st.block.cy .. ',' .. tostring(st.followCount)) or nil
  local building = (st.terrainPending ~= nil or (st.pending ~= nil and not st.pending.ready)) and net.isConnected()
  local near = nearReady(h.x, h.y)
  local act, z = drophold.step(h, st.clock, box, serial, gz, building, near)
  if act == nil then return end
  if act == 'place' then
    if simhold.carHeld() then simPause(false) end     -- (BeamMP: the car active again before it is placed)
    setCarAt(veh, h.x, h.y, z)
    log('I', 'beamngEarth', string.format('drop hold: set on the ground at %.1f', z))
  elseif act == 'timeout' then
    log('W', 'beamngEarth', 'drop hold: no ground after ' .. drophold.MAX_S .. ' s, resumed')
  else
    log('I', 'beamngEarth', 'drop hold: ground loaded, resumed')
  end
  endHold(h)
end

local function updateSky(dt)
  st.skyTimer = st.skyTimer + dt
  if st.skyTimer < SKY_INTERVAL or not st.player then return end
  st.skyTimer = 0
  local lat, lon = st.player.lat, st.player.lon
  if core_celestial and core_celestial.setLocation then
    core_celestial.setLocation(lat, lon) -- core/celestial.lua:762, writes TimeOfDay.latitude/longitude
  else
    local names = scenetree.findClassObjects('TimeOfDay')
    local tod = names and names[1] and scenetree.findObject(names[1])
    if tod then tod.latitude, tod.longitude = lat, lon end
  end
end

-- Does the connected server know our anchor? After a server restart the committed anchor's id is
-- the old session's, and the new server numbers its own anchors from 1 again: until the resync
-- commits, a pose / prepare_terrain naming it would name one of the new server's anchors (its resync
-- anchor 1 is the usual one), which it would switch to and stream tiles for that the far field files
-- under the old session.
local function anchorKnown()
  return st.anchorId ~= nil and st.anchorSession == st.sessionId
end

local function updateWorld(dt)
  if st.floorZ and not st.placeFrames then M.floorCheck() end
  if st.placeFrames then
    st.placeFrames = st.placeFrames - 1
    if st.placeFrames <= 0 then
      st.placeFrames = nil
      placePlayer(st.placeZ)
    end
    return -- no rebase decisions while the player is being placed
  end
  local pl = st.player
  if not pl then return end

  st.poseTimer = st.poseTimer + dt
  -- no pose before the resync with a restarted server commits (anchorKnown; hello named the
  -- session folders meanwhile)
  -- nor between a rebase's two phases (st.rebasing): the car is already in the new frame while st.anchorId is still the
  -- old one, and the server, reading that position in the old frame, dropped every road tile around the car at each
  -- rebase (2026-10-17, 100 mph on the salt flats: 42 road tiles -> 0 at the rebase)
  if st.poseTimer >= POSE_INTERVAL and net.isConnected() and anchorKnown() and not st.rebasing then
    st.poseTimer = 0
    local c = st.cam
    -- held: previous anchors of this server's session only (the far field's older sessions' anchor
    -- ids are another server's); sessions: every session folder in use
    net.send({ type = 'pose', anchorId = st.anchorId, x = pl.x, y = pl.y, z = pl.z, vx = pl.vx, vy = pl.vy,
      lat = pl.lat, lon = pl.lon, speed = pl.speed, follow = not st.followHeld,
      held = (farfield.session() == st.sessionId) and farfield.heldAnchors() or {},
      sessions = farfield.heldSessions(),
      slotsGone = slotsGoneNow(), -- pooled shape slots let go (server/matslots.py)
      foci = poseFoci(),          -- other vehicles' collision islands (collisionfoci.lua; earthd parse_foci)
      cam = c and { x = c.x, y = c.y, z = c.z, fx = c.fx, fy = c.fy, fz = c.fz, fovDeg = c.fovDeg, h = c.h } or nil })
  end

  -- a resync the server could not build (anchor_error: offline, never visited) is asked again
  if st.resync and not st.pending and st.sessionId and st.clock >= st.retryAt then
    requestAnchor(st.anchor.lat, st.anchor.lon, 'resync', resyncHint())
  end

  -- origin rebase first: when it commits, the terrain-follow state belongs to the old frame
  local p = st.pending
  local staleDist, edgeDist
  if p and p.ready and (p.purpose == 'rebase' or p.purpose == 'resync') then
    p.anchorObj = p.anchorObj or geodesy.newAnchor(p.msg.lat, p.msg.lon)
    p.xf = p.xf or st.anchor:transformTo(p.anchorObj)
    local nx, ny = p.xf:apply(pl.x, pl.y, pl.z)
    local tr = p.msg.terrain or {}
    staleDist = follow.cheb(nx, ny, tr.cx or 0, tr.cy or 0)
    -- M4 stale-anchor rule: how far inside the prepared terrain's collidable area the player is
    local x0, y0, x1, y1 = lattice.groundBox(tr.cx or 0, tr.cy or 0)
    edgeDist = math.min(nx - x0, x1 - nx, ny - y0, y1 - ny)
  end
  local action = rebase.decide(pl.dist, p, staleDist, st.clock, st.retryAt, edgeDist)
  if p and p.ready and p.purpose == 'resync' then
    -- the player may have driven off the terrain the server prepared meanwhile: ask again
    if staleDist > rebase.STALE_DIST then
      action = nil
      -- at most one re-request per RETRY_S: if the server can't centre the block where we ask,
      -- re-asking every frame turns into a request storm
      if st.clock >= st.retryAt then
        st.retryAt = st.clock + rebase.RETRY_S
        requestAnchor(st.anchor.lat, st.anchor.lon, 'resync', resyncHint())
      end
    else
      action = 'resync'
    end
  end
  if (action == 'commit' or action == 'resync') and st.paused then action = nil end -- vehicles move only with physics running
  if action == 'prefetch' then
    if net.isConnected() then requestAnchor(pl.lat, pl.lon, 'rebase', rebaseHint(pl)) end
  elseif action == 'commit' or action == 'resync' then
    -- a heavy op: it runs once the governor has ramped streaming down (asked again every frame);
    -- terrain-follow waits meanwhile, its state belongs to the frame that is about to change
    if gov:requestHeavy() then
      st.pending = nil
      commitAnchor(p.msg, p.purpose)
    end
    return
  elseif action == 'discard' then
    st.pending = nil -- the next frame prefetches at the player's position
  end

  -- an unstable car (NaN position or speed, the frames before the game resets it): no block asked for from it (the
  -- server refused "cx is not finite", a Steam Deck test drive 2026-10-23)
  local function finite(v) return type(v) == 'number' and v == v and v > -math.huge and v < math.huge end
  if not (finite(pl.x) and finite(pl.y) and finite(pl.vx or 0) and finite(pl.vy or 0)) then return end
  -- terrain-follow, unless the altitude rule holds the block (high above the ground)
  local ga = st.groundAhead
  local gz = ga and ga.anchorId == st.anchorId and st.clock - ga.at <= follow.GROUND_STALE_S and ga.z or nil
  local held = follow.altitudeHold(st.followHeld, pl.z, pl.vz, gz)
  if dropHold.active then held = false end -- a car dropped high up still needs the block under it (2026-10-18)
  if held ~= st.followHeld then
    st.followHeld = held
    st.followHolds = st.followHolds + (held and 1 or 0)
    log('I', 'beamngEarth', string.format('terrain follow %s at %.0f m above the ground ahead',
      held and 'held' or 'resumed', pl.z - (gz or pl.z)))
  end
  if held then return end
  local b = st.block
  -- commit gating: the far tiles of the strip the move uncovers must be live, and no coarse tile
  -- may straddle the new block (design 2.3, farfield.uncoverReady)
  local gate = b and function(ncx, ncy) return farfield.uncoverReady(b.cx, b.cy, ncx, ncy) end
  local fa, tx, ty = follow.decide(pl.x, pl.y, pl.vx, pl.vy, b and b.cx, b and b.cy, st.terrainPending,
    st.clock, st.terrainRetryAt, gate, st.terrainLatency)
  if fa == 'prepare' then
    if net.isConnected() and anchorKnown() then
      local id = nextId()
      st.terrainPending = { requestId = id, anchorId = st.anchorId, cx = tx, cy = ty, sentAt = st.clock }
      net.send({ type = 'prepare_terrain', requestId = id, anchorId = st.anchorId, cx = tx, cy = ty })
    end
  elseif fa == 'commit' then
    -- isolated heavy slot (governor.lua); until then the request repeats. Past the hard commit
    -- distance it is urgent: the car's ground comes before the frame budget (a far-tile import backlog
    -- held commits for seconds at 90 m/s, 2026-10-12)
    local urgent = b ~= nil and follow.cheb(pl.x, pl.y, b.cx, b.cy) > follow.HARD_COMMIT_DIST
    if gov:requestHeavy(urgent) then
      local tp = st.terrainPending
      st.terrainPending = nil
      commitTerrain(tp.msg)
    else
      st.followWait.governor = st.followWait.governor + 1
    end
  elseif fa == 'discard' then
    st.terrainPending = nil
  elseif st.terrainPending and b and follow.cheb(pl.x, pl.y, b.cx, b.cy) > follow.COMMIT_DIST then
    -- past the commit distance and still not committing: why
    local w = st.followWait
    if not st.terrainPending.ready then w.server = w.server + 1 else w.gate = w.gate + 1 end
  end
end

------------------------------------------------------------------------------------------
-- robustness: the edge guard and the connection banner (docs/ROADMAP.md "Robustness" 1, 2)

local function terrainStuck()
  local tp = st.terrainPending
  if tp and not tp.ready and tp.sentAt and st.clock - tp.sentAt > STUCK_S then return true end
  return st.terrainErrorAt ~= nil and st.clock - st.terrainErrorAt < ERROR_HOLD_S
end

-- Can the server move the physics block? Connected, its hello arrived, an anchor block committed
-- (terrain-follow never moves the level-start flat block), no resync pending (a server that came
-- back: until it commits, nothing follows) and terrain requests answered.
local function serverHealthy()
  return net.isConnected() and st.sessionId ~= nil and st.block ~= nil and not st.resync and not terrainStuck()
end

M.serverHealthy = function() return serverHealthy() end
M.mpStatus = function() return mpanchor.status() end -- console: beamngEarth_core.mpStatus()

-- The collidable area of the block in use (the anchor's, else the level-start flat block), or nil.
local function guardBox()
  local cx, cy
  if st.block then cx, cy = st.block.cx, st.block.cy
  elseif st.flat then cx, cy = 0, 0
  else return nil end
  local box = st.guardBox
  box[1], box[2], box[3], box[4] = lattice.groundBox(cx, cy)
  return box
end

-- After the frame's world update (a block commit has already moved the block): the guard runs only
-- while the server cannot move the block, with physics running and the player placed (also on the
-- level-start flat block, before any anchor: no server at all).
local function updateGuard()
  local g = st.guard
  -- a rebase / resync ready to commit moves the car with physics running: a guard pause gives way to it (it re-centres
  -- the block on the car), and no new pause for RESUME_GRACE_S (the commit waits for the next unpaused frame)
  local p = st.pending
  if g.pausedByUs and p and p.ready and (p.purpose == 'rebase' or p.purpose == 'resync') then
    edgeguard.release(g)
    g.resumedAt = st.clock
  end
  -- a guard pause runs on until the car is well inside the (followed) block or the player resumes (edgeguard.update).
  -- With a healthy server too (2026-10-19, "I always want the ground ... to have collision ... going at high speeds"):
  -- if the next block is late (a first visit, a busy server) the car is paused at the edge of the collidable ground
  -- instead of driving off it; normally the block follows long before (the pause needs the edge within PAUSE_LEAD_S).
  g.healthy = serverHealthy()
  if not g.pausedByUs and (guardFailed or st.placeFrames or st.paused) then
    g.mode = 'off'
    return
  end
  local veh = getPlayerVehicle(0)
  local box = guardBox()
  if not veh or not box then
    g.mode = 'off'
    return
  end
  local ok, err = pcall(edgeguard.update, g, veh, box, st.clock)
  if not ok then
    guardFailed, g.mode = true, 'off'
    pcall(edgeguard.release, g)
    log('E', 'beamngEarth', 'edge guard turned off: ' .. tostring(err))
  end
end

local function updateBanner(dt)
  local i = bannerInfo
  i.net, i.session = net.state(), st.sessionId ~= nil
  i.everConnected, i.attempts = net.everConnected(), net.attempts()
  i.note, i.noteDetail = st.serverNote, st.serverNoteDetail
  local m = st.guard.mode
  -- (healthy: no warning, the block follows; a pause there waits for the late block like a drop hold)
  i.edge = (not st.guard.healthy and (m == 'warn' or m == 'hold')) and m or nil
  i.dropHold = (dropHold.active or (st.guard.healthy and st.guard.pausedByUs)) or nil
  -- connected and said hello, yet the guard runs: the server does not (yet) build the terrain
  i.stuck = (i.session and net.isConnected()) and true or false
  banner.update(dt, i)
end

-- The credits line: the server's own (protocol 4 hello) when it sent one; else ours, with the
-- imagery credits once satellite-textured tiles arrived.
local function attributionText(fs)
  if serverAttribution then return serverAttribution end
  return (fs.textured > 0) and FULL_ATTRIBUTION or ATTRIBUTION
end

local function buildView()
  local pl = st.player
  local fs = farfield.stats()
  local b = st.block
  local toPrepare, toCommit
  if pl and b then toPrepare, toCommit = follow.thresholds(pl.x, pl.y, b.cx, b.cy) end
  local tp = st.terrainPending
  return {
    defaultLat = DEFAULT_LAT, defaultLon = DEFAULT_LON,
    connection = net.state() .. (st.sessionId and ('  (session ' .. tostring(st.sessionId) .. ')') or '')
      .. ((not net.isConnected() and net.lastError) and ('  - ' .. tostring(net.lastError)) or ''),
    anchorId = st.anchorId,
    anchorLat = st.anchor and st.anchor.lat, anchorLon = st.anchor and st.anchor.lon,
    playerLat = pl and pl.lat, playerLon = pl and pl.lon,
    dist = pl and pl.dist, toRebase = pl and (rebase.distance - pl.dist),
    rebasePending = st.pending and st.pending.purpose == 'rebase' and (st.pending.ready and 'ready' or 'preparing') or nil,
    blockCx = b and b.cx, blockCy = b and b.cy,
    toFollowPrepare = toPrepare, toFollowCommit = toCommit,
    followPending = tp and string.format('(%d, %d) %s', tp.cx, tp.cy, tp.ready and 'ready' or 'preparing') or nil,
    farText = st.anchor and string.format('Far tiles (%s near ground): %d live (%d covered), %d loading, %d swaps waiting, '
      .. '%d moving, %d old/deleting%s, %d imagery materials',
      fs.nearVisual, fs.live, fs.covered, fs.queued, fs.groups, fs.transitions, fs.old + fs.deleting,
      fs.done and ' (done)' or '', fs.materials),
    lastFollowMs = st.lastFollowMs, followCount = st.followCount,
    lastRebaseMs = st.lastRebaseMs, rebaseCount = st.rebaseCount,
    rebaseDistance = rebase.distance, rebaseMin = rebase.MIN_DISTANCE, rebaseMax = rebase.MAX_DISTANCE,
    status = st.status,
    attribution = attributionText(fs),
  }
end

-- The on-screen credits before the server's first credits message (or from an older server): the
-- attribution line. Called every frame then, so no farfield.stats() table: its counters only.
local function creditsFallback() return attributionText(farfield.counters()) end

-- The status bar's loading part (loadprogress.lua): false when no load is on; worked out at most every LOAD_LINE_S.
local LOAD_LINE_S = 0.25
local loadLineAt, loadLineText = -math.huge, false
local function loadLine()
  if not loadprogress.active() then return false end
  if st.clock - loadLineAt < LOAD_LINE_S then return loadLineText end
  loadLineAt = st.clock
  local p = st.pending
  local placed = st.anchor ~= nil and not (p and (p.purpose == 'teleport' or p.purpose == 'resync'))
    and st.placeFrames == nil and not dropHold.active
  local lp = st.loadProgress
  local pend = {}
  local okR, rs = pcall(roads.status)
  if okR and rs then pend.roads = (rs.queued or 0) + (rs.walksQueued or 0) end
  local okT, ts = pcall(trees.status)
  if okT and ts then pend.trees = ts.pending or 0 end
  local okF, fs = pcall(farfield.stats)
  if okF and fs then pend.far = fs.queued or 0 end
  loadLineText = loadprogress.line(st.clock, { placed = placed,
    progress = lp and lp.anchorId == st.anchorId and lp or nil, pending = pend }) or false
  return loadLineText
end

-- The UI apps' texts (hudapps.update calls it at most every SEND_S while an app is on screen).
local hudInput = { banner = nil }
local function hudTexts()
  local shown = st.inOurLevel and st.active
  hudInput.status = shown and statusbar.enabled and statusbar.text(st.status, loadLine()) or ''
  hudInput.loading = shown and statusbar.enabled and statusbar.loading(st.status, loadLine()) or ''
  hudInput.credits = st.active and credits.enabled and credits.text(creditsFallback) or ''
  hudInput.banner = (st.inOurLevel or st.active) and banner.enabled and banner.current() or nil
  hudInput.hidden = credits.hiddenByUi()
  return hudInput
end

-- The imgui window, the on-screen credits (one text draw; the attribution line until the server
-- sends credits) and the connection banner (in our level or world only; hidden like the credits).
-- A newer version on GitHub (the server's update check, updates.py): a small window that stays until the player opens the
-- releases page (hidden for this session) or dismisses it (hidden until a newer version; settings updateDismissed). It was
-- a 15 s toast (the user, 2026-10-18: "Make it persistant and add a dismiss buttion or a buttion to open the releases
-- page").
M.RELEASES_URL = 'https://github.com/TecnaGamer/BeamNG.Earth/releases/latest'
local function drawUpdateNotice()
  if updateHidden or not updateInfo or settings.get('updateDismissed') == tostring(updateInfo.latest) then return end
  local im = ui_imgui
  if not im then return end
  local vp = im.GetMainViewport()
  if vp then
    -- the top left corner (the user, 2026-10-18)
    im.SetNextWindowPos(im.ImVec2(vp.Pos.x + 12, vp.Pos.y + 12), im.Cond_Always, im.ImVec2(0, 0))
  end
  local flags = im.WindowFlags_AlwaysAutoResize + im.WindowFlags_NoCollapse + im.WindowFlags_NoSavedSettings
    + im.WindowFlags_NoTitleBar
  if im.Begin('BeamNG.Earth update##beamngEarthUpdate', nil, flags) then
    im.Text(string.format('BeamNG.Earth %s is available (you have %s).', tostring(updateInfo.latest),
      tostring(updateInfo.current)))
    local us = updateInfo.status
    if us and us.text ~= '' then
      if us.state == 'progress' and us.total > 0 then
        im.Text(string.format('%s %d of %d MB', us.text, math.floor(us.done / 1048576), math.floor(us.total / 1048576)))
      else
        im.TextWrapped(us.text)
      end
    end
    -- the packaged app downloads and checks the new version itself (GitHub #140); a git copy pulls
    if not (us and (us.state == 'progress' or us.state == 'ready')) and net.isConnected() then
      if im.Button('Update now') then
        updateInfo.status = { state = 'progress', text = 'Asking GitHub...', done = 0, total = 0 }
        net.send({ type = 'update_now' })
      end
      im.SameLine()
    end
    if im.Button('Open releases page') then
      local url = (updateInfo.url ~= '' and updateInfo.url) or M.RELEASES_URL
      -- the server opens it (the game's openWebBrowser does nothing in the native Linux build); the game's own
      -- call only without a server
      if net.isConnected() then net.send({ type = 'open_page', url = url })
      elseif openWebBrowser then pcall(openWebBrowser, url) end
      updateHidden = true
    end
    im.SameLine()
    if im.Button('Dismiss') then settings.set('updateDismissed', tostring(updateInfo.latest)) end
  end
  im.End()
end

local beammpBroken = false   -- an error in the BeamMP window turns it off for the session
local mpsessionBroken = false -- the same for the BeamMP session by area (mpsession.lua)
-- Collision foci besides the vehicles' (collisionfoci.update's extra): the stored far cars about to come back
-- (farcars.lua) and, in BeamMP, where other players' cars switched off here are coming back on (mpsession.groundFoci)
local function extraFoci()
  local out = farcars.foci()
  if beammp.inSession() then
    for _, f in ipairs(mpsession.groundFoci()) do out[#out + 1] = f end
  end
  return out
end
-- the minimap's label (hudapps.hookCityZone)
-- (the car's coordinates, formatted again only when they change at the label's 5 decimals)
local coordsKey, coordsText = nil, nil
local function minimapName()
  local p = st.player
  if not (p and p.lat and p.lon) then return hudapps.minimapName(statusbar.location()) end
  local k = math.floor(p.lat * 1e5 + 0.5) * 1e7 + math.floor(p.lon * 1e5 + 0.5)
  if k ~= coordsKey then coordsKey, coordsText = k, hudapps.coordsLabel(p.lat, p.lon) end
  return coordsText
end
-- the minimap compass's true north at the camera (hudapps.hookMinimapNorth)
local function minimapNorth()
  if not st.anchor then return nil end
  local cx, cy = core_camera.getPositionXYZ()
  if not cx then return nil end
  return st.anchor:northAt(cx, cy)
end
local function drawUi(dt)
  ui.draw(buildView(), { teleport = M.teleport, setRebaseDistance = M.setRebaseDistance })
  -- each element drawn by imgui unless its UI app is on screen (hudapps.lua)
  if not hudapps.mounted('credits') then credits.draw(st.active, creditsFallback) end
  updateBanner(dt)
  if not hudapps.mounted('banner') then banner.draw(st.inOurLevel or st.active, credits.hiddenByUi()) end
  if not hudapps.mounted('status') then
    statusbar.draw(st.inOurLevel and st.active, credits.hiddenByUi(), st.status, loadLine())
  end
  hudapps.update(st.clock or 0, hudTexts)
  if st.inOurLevel then
    hudapps.hookCityZone(minimapName)
    hudapps.hookMinimapNorth(minimapNorth)
  end
  if updateInfo and st.inOurLevel and st.active and not credits.hiddenByUi()
      and (st.clock or 0) > (st.loadingUntil or 0) then
    local ok, err = pcall(drawUpdateNotice)
    if not ok then updateInfo = nil; log('E', 'beamngEarth', 'update notice: ' .. tostring(err)) end
  end
end

-- BeamMP: its window and the session (other players' anchors, cars, damage). Timed as its own part ('beammp'): inside
-- drawUi a BeamMP car's spawn was logged as a 1.1 s "draw" hitch (2026-10-24, a three-player test).
local function updateBeamMP(dt)
  if not beammpBroken then
    local ok, err = pcall(beammp.update, dt, st.inOurLevel, net.isConnected(), settings)
    if not ok then beammpBroken = true; log('E', 'beamngEarth', 'BeamMP window: ' .. tostring(err)) end
  end
  if not mpsessionBroken and st.inOurLevel and net.isConnected() and beammp.inSession() then
    local ok, err = pcall(mpsession.update, dt, st.anchor, st.player or st.cam)
    if not ok then mpsessionBroken = true; log('E', 'beamngEarth', 'BeamMP session: ' .. tostring(err)) end
  end
end

-- Hitch attribution (the user, 2026-10-14: "there's definitely some performance issues sometimes"): each frame
-- times the mod's own parts; a frame over HITCH_MS keeps them and the engine's per-subsystem breakdown, so a
-- recorded drive (tools/autodrive_perf.py) says whether we or the engine (rendering, collision, streaming) took it.
M.HITCH_MS = 50
M.HITCH_KEEP = 64
local hitchClock = stopwatch.clock()
local marks, prevMarks = {}, {}
local hitches = {}
local function mark(name, t0)
  local t = hitchClock()
  marks[name] = (marks[name] or 0) + (t - t0)
  return t
end
local function noteHitch(frameMs)
  local h = { ms = math.floor(frameMs + 0.5), at = math.floor(st.clock * 10 + 0.5) / 10, mod = {} }
  -- marks: the mod's parts in the frame that just ended (onUpdate swaps them to prevMarks after this call; reading
  -- prevMarks named the frame before it, and most Lua-led hitches showed no part at all, 2026-10-18)
  for k, v in pairs(marks) do if v >= 1 then h.mod[k] = math.floor(v * 10 + 0.5) / 10 end end
  if Engine and Engine.Debug and Engine.Debug.getLastPerformanceMetrics then
    local m = {}
    if pcall(Engine.Debug.getLastPerformanceMetrics, m) then
      local arr = {}
      for k, v in pairs(m) do
        if type(v) == 'number' and not k:find('^phys') and not k:find('^virt') then arr[#arr + 1] = { k, v } end
      end
      table.sort(arr, function(a, b) return a[2] > b[2] end)
      h.engine = {}
      for i = 1, math.min(6, #arr) do h.engine[arr[i][1]] = math.floor(arr[i][2] * 10 + 0.5) / 10 end
    end
  end
  hitches[#hitches + 1] = h
  if #hitches > M.HITCH_KEEP then table.remove(hitches, 1) end
  return h
end

-- The performance log (perflog.lua): where the player is and what the mod is doing, for its lines.
local perfMetrics = {}
local function perfEngine()
  local dbg = Engine and Engine.Debug
  if not (dbg and dbg.getLastPerformanceMetrics) then return nil end
  for k in pairs(perfMetrics) do perfMetrics[k] = nil end
  if not pcall(dbg.getLastPerformanceMetrics, perfMetrics) then return nil end
  local out = {}
  for k, v in pairs(perfMetrics) do
    if type(v) == 'number' and not k:find('^phys') and not k:find('^virt') then out[k] = v end
  end
  return out
end
local function perfContext()
  local c = {}
  local p = st.player
  if p then
    c.lat, c.lon = p.lat, p.lon
    c.mph = math.sqrt((p.vx or 0) ^ 2 + (p.vy or 0) ^ 2) * 2.23694
  end
  if st.block then c.block = string.format('%d,%d', st.block.cx, st.block.cy) end
  local s = {}
  if dropHold.active then s[#s + 1] = 'held until its ground loads' end
  if st.guard and st.guard.pausedByUs then s[#s + 1] = 'held at the edge of the ground' end
  if st.rebasing then s[#s + 1] = 'moving the world origin' end
  if st.clock < (st.loadingUntil or 0) or st.placeFrames then s[#s + 1] = 'loading after a teleport' end
  if not net.isConnected() then s[#s + 1] = 'world server not connected' end
  if beammp.inSession() then s[#s + 1] = 'BeamMP' end
  c.state = table.concat(s, ', ')
  local d = {}
  if st.commitFrames then d[#d + 1] = 'ground block swap' end
  if st.terrainPending and st.terrainPending.sentAt then
    d[#d + 1] = string.format('next block asked %.1fs ago', st.clock - st.terrainPending.sentAt)
  end
  local okF, fs = pcall(farfield.stats)
  if okF and fs then
    if (fs.queued or 0) > 0 then d[#d + 1] = 'far tiles ' .. fs.queued .. ' queued' end
    if not fs.done then d[#d + 1] = 'far field loading' end
  end
  local okR, rs = pcall(roads.status)
  if okR and rs and ((rs.queued or 0) + (rs.walksQueued or 0)) > 0 then
    d[#d + 1] = 'road tiles ' .. ((rs.queued or 0) + (rs.walksQueued or 0)) .. ' queued'
  end
  local okT, ts = pcall(trees.status)
  if okT and ts and (ts.pending or 0) > 0 then d[#d + 1] = 'tree tiles ' .. ts.pending .. ' pending' end
  if (st.serverBacklog or 0) > 0 then d[#d + 1] = 'server backlog ' .. st.serverBacklog end
  c.doing = table.concat(d, ', ')
  c.extra = string.format('Lua %.0f MB%s', collectgarbage('count') / 1024,
    st.terrainLatency and string.format(', last block %.1fs', st.terrainLatency) or '')
  return c
end
local machineSent = false       -- the machine's lines sent to this server connection
local machineTryAt = nil
local function perfMachine()
  local rep = rawget(_G, 'beamngEarth_report')
  local okH, hw = pcall(function() return rep and rep.collectHardware and rep.collectHardware() end)
  if not okH or not hw then return {} end
  local okF, md = pcall(require('beamngEarth/issuefmt').formatHardware, hw)
  if not okF or type(md) ~= 'table' then return {} end
  local out = {}
  for _, l in ipairs(md) do                -- its Markdown list lines, without the list and bold marks
    local item = type(l) == 'string' and l:match('^%- (.*)$')
    if item then out[#out + 1] = 'machine: ' .. (item:gsub('%*%*', '')) end
  end
  return out
end
local function perfFrame(ms, hrec)
  local lines = perflog.frame(ms, st.clock, perfContext, hrec, marks, perfEngine)
  if not net.isConnected() then
    machineSent = false
  elseif not machineSent and st.clock >= (machineTryAt or 0) then
    local m = perfMachine()
    if #m > 0 then
      machineSent = true
      net.send({ type = 'perf_log', lines = m })
    else
      machineTryAt = st.clock + 10           -- (the game's report extension not loaded yet)
    end
  end
  if #lines == 0 then return end
  for _, l in ipairs(lines) do log('I', 'beamngEarth', l) end
  if net.isConnected() then net.send({ type = 'perf_log', lines = lines }) end
end
-- The hitches since the last call: [{ms, at (s), mod = {part = ms}, engine = {subsystem = ms}}].
function M.hitches()
  local out = hitches
  hitches = {}
  return out
end

-- The previous frame's engine tick (ms), 0 without the metrics.
local tickMetrics = {}
local function engineTickMs()
  local dbg = Engine and Engine.Debug
  if not (dbg and dbg.getLastPerformanceMetrics) then return 0 end
  if not pcall(dbg.getLastPerformanceMetrics, tickMetrics) then return 0 end
  return tonumber(tickMetrics.Ticking) or 0
end

local function onUpdate(dtReal, dtSim)
  dtReal = dtReal or 0
  local frameMs0 = frameTimer and frameTimer:stopAndReset() or dtReal * 1000
  if not st.inOurLevel and not st.active then
    -- Other levels and the menus: nothing but the connection to the world server, kept open (on Linux the
    -- launcher's sign that the game still runs, launcher.game_connected; its messages only store what the
    -- server is). The user, 2026-10-20: the mod should be effectively off when not playing the BeamNG.Earth map.
    for _, msg in ipairs(net.update(dtReal)) do
      local ok, err = pcall(onMessage, msg)
      if not ok then log('E', 'beamngEarth', 'message ' .. tostring(msg.type) .. ' failed: ' .. tostring(err)) end
    end
    return
  end
  local hrec = nil
  if frameMs0 > M.HITCH_MS and st.inOurLevel then
    local okH, h = pcall(noteHitch, frameMs0)
    if okH then hrec = h end
  end
  if st.inOurLevel and not st.paused then
    local okP, errP = pcall(perfFrame, frameMs0, hrec)
    if not okP then log('W', 'beamngEarth', 'perf log: ' .. tostring(errP)) end
  end
  prevMarks, marks = marks, prevMarks
  for k in pairs(marks) do marks[k] = nil end
  local t = hitchClock()
  postfx.update(st.inOurLevel)
  local stg = rawget(_G, 'beamngEarth_settings')
  if stg and stg.redirectBigMap then stg.redirectBigMap(st.inOurLevel) end
  if st.inOurLevel then gps.update(st.anchor, st.player) end
  seasonlook.update(st.inOurLevel, st.anchor and st.anchor.lat)
  t = mark('ui', t)
  local frameMs = frameMs0
  statusbar.frame(frameMs0)
  if st.inOurLevel and net.isConnected() then
    local stg2 = rawget(_G, 'beamngEarth_settings')
    local q = statusbar.request(st.clock, st.player, not stg2 or not stg2.get or stg2.get('placeNamesOnline') ~= false)
    if q then net.send({ type = 'where', id = q.id, lat = q.lat, lon = q.lon, online = q.online }) end
  end
  local cf = st.commitFrames
  if cf then                    -- the commit frame and the two after it (wall ms between updates)
    cf.f[#cf.f + 1] = frameMs
    if #cf.f == 1 and Engine and Engine.Debug and Engine.Debug.getLastPerformanceMetrics then
      -- the engine's per-subsystem breakdown of the commit frame (the frame that just ended): its 8 largest
      local m = {}
      if pcall(Engine.Debug.getLastPerformanceMetrics, m) then
        local arr = {}
        for k, v in pairs(m) do
          if type(v) == 'number' and not k:find('^phys') and not k:find('^virt') then arr[#arr + 1] = { k, v } end
        end
        table.sort(arr, function(a, b) return a[2] > b[2] end)
        cf.engine = {}
        for i = 1, math.min(8, #arr) do cf.engine[arr[i][1]] = math.floor(arr[i][2] * 10 + 0.5) / 10 end
      end
    end
    if #cf.f >= 3 then
      st.commitFrames = nil
      if cf.f[1] and cf.f[1] > COMMIT_SLOW_MS then
        -- a terrain commit's slow first frame (the user's freezes driving through towns, 2026-10-21: 2.7 s of
        -- RenderShadows in one): what it listed, for the log
        local e = {}
        for k, v in pairs(cf.engine or {}) do e[#e + 1] = k .. ' ' .. tostring(v) end
        log('W', 'beamngEarth', string.format('slow terrain commit at %s,%s: %.0f ms (%s), %s materials, new: %s',
          tostring(cf.at and cf.at[1]), tostring(cf.at and cf.at[2]), cf.f[1], table.concat(e, ', '),
          tostring(cf.mats), cf.fresh and table.concat(cf.fresh, ' ') or 'none'))
      end
    end
  end
  st.clock = st.clock + dtReal
  if st.randomStart and st.clock - st.randomStart.t0 > RANDOM_WAIT_S then    -- no random place came: the default
    st.randomStart = nil
    if not st.anchor then defaultStart() end
  end
  -- the governor's frame opens first: its clock is the wall time since the last onUpdate (hptimer)
  gov:setLoading(st.placeFrames ~= nil or st.clock < st.loadingUntil or st.slowFor > SLOW_S)
  gov:beginFrame(farfield.hasWork() or roads.hasWork())
  gov:chargeEngine(engineTickMs())     -- what the engine spent on last frame's new tiles (governor.lua)
  for _, msg in ipairs(net.update(dtReal)) do
    local ok, err = pcall(onMessage, msg)
    if not ok then log('E', 'beamngEarth', 'message ' .. tostring(msg.type) .. ' failed: ' .. tostring(err)) end
  end
  t = mark('net', t)
  if not scenetree.MissionGroup then return end -- no level loaded
  tickMotionBlur()

  st.paused = (dtSim or dtReal) <= 0
  local rb = st.rebasing
  if rb then
    -- phase 2 of a rebase is due once its vehicle move reaches this frame's physics step
    if not st.paused then
      rb.wait, rb.waited = rb.wait - 1, rb.waited + 1
    end
    if rb.wait <= 0 then
      finishRebase()
    else
      drawUi(dtReal)
      return -- positions are in neither frame for decisions this frame
    end
  end
  local rl = st.refreshLater
  if rl and rl.anchorId == st.anchorId then     -- (its anchor committed: see 'terrain_refresh')
    st.refreshLater = nil
    if st.block and rl.cx == st.block.cx and rl.cy == st.block.cy and rl.size == terrain.SIZE then commitTerrain(rl) end
  end
  if st.recouple then                   -- a rebase's trailers hitched again over a few seconds (rebase.lua)
    local okR, plan = pcall(rebase.recoupleTick, st.recouple, st.clock)
    st.recouple = okR and plan or nil
  end

  updatePlayer(dtReal)
  if st.active then
    if not dropHoldFailed and (not st.paused or dropHold.active) then     -- (it runs on while it paused the game)
      local ok, err = pcall(updateDropHold, dtReal)
      if not ok then          -- never break the frame: off for the session, said once
        dropHoldFailed = true
        drophold.cancel(dropHold)
        endHold(dropHold)
        log('E', 'beamngEarth', 'drop hold turned off: ' .. tostring(err))
      end
    end
    t = mark('player', t)
    updateWorld(dtReal)
    t = mark('world', t)
    -- the roads before the far tiles: they share the governor's budget and the first to ask gets it; after a
    -- teleport the far field's hundreds of tiles took it frame after frame and the road meshes under the car
    -- came after it had passed (Brasov, Sinaia 2026-10-15: the car drove bare ground)
    roads.update(gov, st.anchorId, st.player, M.viewAway())
    t = mark('roads', t)
    farfield.update(gov, (gov.w1 or dtReal * 1000) / 1000, st.cam, st.player)
    t = mark('far', t)
    sea.update(st.anchorId, st.player)
    trees.update(st.anchorId, st.player, M.viewAway())
    t = mark('sea+trees', t)
    local pb = nil
    if st.block then
      pb = st.parkBox or {}
      st.parkBox = pb
      pb[1], pb[2], pb[3], pb[4] = lattice.groundBox(st.block.cx, st.block.cy)
    end
    if not fociBroken then
      local ok, err = pcall(collisionfoci.update, dtReal, { roads.setFoci, trees.setFoci }, extraFoci)
      if not ok then fociBroken = true; log('E', 'beamngEarth', 'collision foci: ' .. tostring(err)) end
      t = mark('foci', t)
      ok, err = pcall(terrainpatch.update, dtReal, st.anchorId, collisionfoci.last, st.player, pb, net.send)
      if not ok then fociBroken = true; log('E', 'beamngEarth', 'terrain patches: ' .. tostring(err)) end
      ok, err = pcall(farcars.update, dtReal)
      if not ok then fociBroken = true; log('E', 'beamngEarth', 'far cars: ' .. tostring(err)) end
      t = mark('farcars', t)
    end
    parking.update(st.anchorId, not (st.player and st.player.camera) and st.player or nil, pb, gov)  -- (no car: none parked)
    t = mark('parking', t)
    navgraph.update(st.anchorId, pb, gov)
    t = mark('navgraph', t)
    updateSky(dtReal)
    updateGuard()
    t = mark('sky', t)
  end
  drawUi(dtReal)
  t = mark('draw', t)
  updateBeamMP(dtReal)
  mark('beammp', t)
end

-- After physics, before rendering (main.lua:819-824): measure when a rebase's vehicle move became
-- visible. Hook order relative to core_camera does not matter here; this only measures.
local function onPreRender(dtReal, dtSim)
  local probe = st.lagProbe
  if not probe then return end
  if moveVisible(probe) then
    st.lagProbe = nil
    if probe.frames ~= rebaseLag then
      log('W', 'beamngEarth', string.format('rebase: vehicle move became visible after %d frame(s), expected %d; '
        .. 'using %d from now on (this rebase may have shown one mismatched frame)', probe.frames, rebaseLag, probe.frames))
    else
      log('I', 'beamngEarth', 'rebase: vehicle move visible after ' .. probe.frames .. ' frame(s), as scheduled')
    end
    rebaseLag = math.min(probe.frames, 3)
    st.lastMoveLag = probe.frames
  elseif (dtSim or dtReal or 0) > 0 then
    probe.frames = probe.frames + 1
    if probe.frames > LAG_PROBE_FRAMES then
      st.lagProbe = nil
      log('W', 'beamngEarth', 'rebase: vehicle move not visible after ' .. LAG_PROBE_FRAMES .. ' frames')
    end
  end
end

-- The game's UI state (pause menu, loading screens): the credits hide in the pause menu.
local function onUiChangedState(toState, fromState)
  credits.setUiState(toState)
end

------------------------------------------------------------------------------------------
-- level lifecycle

local function onLevelStart()
  -- the connection outlives levels: keep its session and the request id sequence
  local sessionId, nextRequestId, mapPort = st.sessionId, st.nextRequestId, st.mapPort
  resetState()
  st.sessionId, st.nextRequestId, st.mapPort = sessionId, nextRequestId, mapPort   -- the connection's (hello)
  farfield.forget()
  terrain.onLevelChanged()
  st.inOurLevel = getCurrentLevelIdentifier() == LEVEL_ID
  -- the window opens by itself only in our level; elsewhere use beamngEarth_core.toggleUI(). With
  -- the pause-menu settings page it is a compact HUD, hidden unless the settings show it.
  ui.visible = st.inOurLevel and not settings.hudHiddenAtStart()
  groundcover.enginePatch(st.inOurLevel)
  if not st.inOurLevel then return end
  -- the player's traffic / parked cars choice, before freeroam spawns them on a map without our roads yet
  navgraph.takeChoice(rawget(_G, 'freeroam_freeroam') and freeroam_freeroam.spawningOptionsHelper)

  activateWorld()
  -- the connected server's shape materials (its hello came at the main menu, before this level's objects)
  if lastHello and lastHello.prewarm then roads.prewarm(lastHello.prewarm) end
  -- Something to stand on until the first anchor arrives (our level has no GroundPlane).
  local ok, err = terrain.loadFlat()
  if ok then
    st.flat = true
    be:reloadCollision()
    st.placeFrames, st.placeZ = PLACE_DELAY_FRAMES, 1.0
  else
    setStatus('Flat terrain failed: ' .. tostring(err))
  end
  if net.isConnected() then
    st.autoTeleportDone = true
    startTeleport()
  end
end

local function onClientStartMission()
  onLevelStart()
end

-- After the game's own freeroam setGameState (lua/ge/main.lua clientPostStartMission): our level gets the ".Earth" app
-- layout (type beamngEarth: the player's own choice for that type, if any, is kept by the UI's layout map).
M.APP_LAYOUT = 'beamngEarth'
local function onClientPostStartMission(levelPath)
  if type(levelPath) ~= 'string' or not levelPath:find('/' .. LEVEL_ID .. '/', 1, true) then return end
  local gs = rawget(_G, 'core_gamestate')
  if gs and gs.setGameState then pcall(gs.setGameState, 'freeroam', M.APP_LAYOUT, 'freeroam') end
end

-- BeamMP finished joining a server (its own hook): the combined app layout (beammp.lua); the first pose asks where
-- the others are (mpsession.lua).
function M.onBeamMPPostJoin() beammp.onPostJoin(); mpsession.onPostJoin() end

-- BeamMP left the server (its own hook): the other players' cars we deactivated back on.
function M.onBeamMPServerLeave() pcall(beammp.onServerLeave); pcall(mpsession.reset) end

-- The UI apps (ui/modules/apps/BeamNGEarth*) say when they are on screen: hudapps.lua.
function M.hudApp(name, on) return hudapps.setMounted(name, on) end

-- Before the level loads (freeroam spawns its traffic at camera-ready, which can come before our
-- onClientStartMission): the player's traffic choice, for our level only.
local function onClientPreStartMission(levelPath)
  if type(levelPath) == 'string' and levelPath:find('/' .. LEVEL_ID .. '/', 1, true) then
    navgraph.takeChoice(rawget(_G, 'freeroam_freeroam') and freeroam_freeroam.spawningOptionsHelper)
  end
end

local function onClientEndMission()
  hudapps.unhookCityZone()
  hudapps.unhookMinimapNorth()
  postfx.restore()
  pcall(terrainpatch.clear)
  pcall(farcars.reset)
  pcall(mpsession.reset)
  gps.reset()
  do local stg = rawget(_G, 'beamngEarth_settings') if stg and stg.redirectBigMap then stg.redirectBigMap(false) end end
  seasonlook.restore()
  -- runs before the level deletes its objects (server/server.lua:19 vs :34): delete the far tiles
  -- ourselves first, so their per-tile materials (not level objects) can go after them
  farfield.clear()
  horizon.restoreSky() -- M1h: the level's sky planet radius (its LevelInfo goes with the level)
  groundcover.reset() -- its objects go with the level
  groundcover.enginePatch(false)
  roads.reset()
  sea.reset()
  trees.reset()
  parking.reset()
  navgraph.reset()
  local sessionId, nextRequestId, mapPort = st.sessionId, st.nextRequestId, st.mapPort
  resetState()
  st.sessionId, st.nextRequestId, st.mapPort = sessionId, nextRequestId, mapPort   -- the connection's (hello)
end

local function onExtensionLoaded()
  log('I', 'beamngEarth', 'beamngEarth_core loaded')
  groundcover.enginePatch(false)    -- a flag a crashed game left: stock until our level starts
  -- the level select's spawn points: the saved places (levelinfo.lua), from the main menu on
  pcall(places.list)
  pcall(places.syncLevel)
  -- loaded while a level is already running (e.g. extensions.reload from the console)
  if scenetree.MissionGroup and getCurrentLevelIdentifier() then onLevelStart() end
end

-- The places file changed on disk (the player edited it, or our own save): read it again, so the level select's spawn
-- points follow (levelinfo.lua writes only when they changed).
local function onFilesChanged(files)
  for _, f in pairs(type(files) == 'table' and files or {}) do
    local name = type(f) == 'table' and f.filename or nil
    if type(name) == 'string' and name:lower() == places.FILE:lower() then
      local ok, err = pcall(places.load)
      if not ok then log('W', 'beamngEarth', 'places: reload failed: ' .. tostring(err)) end
      pcall(places.syncLevel)
      return
    end
  end
end
M.onFilesChanged = onFilesChanged

local function onExtensionUnloaded()
  hudapps.unhookCityZone()
  hudapps.unhookMinimapNorth()
  groundcover.enginePatch(false)
  net.close()
  farfield.clear()
  roads.clear()
  sea.clear()
  trees.clear()
  terrain.delete()
  -- M1h: the level stays loaded; give it back its sky and view distance
  horizon.restoreSky()
  if st.visibleDistance and horizon.setVisibleDistance(VISIBLE_DISTANCE) then st.visibleDistance = nil end
end

------------------------------------------------------------------------------------------
-- public / console API

-- kind: 'teleport' (M.teleport) or 'start' (the level start's), for the recent places list; 'tool'
-- (M.teleport with opts.record == false: test tools like tools/benchmark.lua) is not recorded
teleportTo = function(lat, lon, name, kind)
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon or lat < -90 or lat > 90 or lon < -180 or lon > 180 then
    setStatus('Teleport: invalid lat/lon')
    return false
  end
  ui.setLatLon(lat, lon)
  -- BeamMP: next to other players, onto their anchor (their tiles are ours: built once for the group, peerwork.py)
  local aLat, aLon, place, cx, cy = lat, lon, nil, 0, 0
  local okA, adLat, adLon = pcall(mpsession.adoptAnchor, lat, lon)
  if okA and adLat then
    local x, y = geodesy.newAnchor(adLat, adLon):geodeticToEnu(lat, lon, 0)
    aLat, aLon, place = adLat, adLon, { x = x, y = y }
    cx, cy = follow.target(x, y, 0, 0)
    log('I', 'beamngEarth', string.format('teleport onto the anchor %.5f, %.5f (%.0f, %.0f there)', aLat, aLon, x, y))
  end
  if requestAnchor(aLat, aLon, 'teleport', cx, cy, place) then
    setStatus(string.format('Preparing terrain at %.5f, %.5f ...', lat, lon))
    loadprogress.reset()
    loadprogress.start(st.clock)        -- the bar says "Loading" from now (loadprogress.lua)
    parking.onTeleport()
    navgraph.onTeleport()
    do local ok, err = pcall(farcars.onTeleport) if not ok then log('W', 'beamngEarth', 'far cars: ' .. tostring(err)) end end
    if kind ~= 'tool' then
      statusbar.reset()
      local ok, err = pcall(places.noteTeleport, lat, lon, name, kind)
      if not ok then log('W', 'beamngEarth', 'recent places: ' .. tostring(err)) end
    end
    return true
  end
  return false
end

-- Move the world's origin to (lat, lon) where the player is (BeamMP: every player of a group shares one anchor,
-- beammp.lua). The same smooth two-phase change as a rebase (commitRebase), asked as a resync so it commits as soon as
-- the server has it ready; the terrain centred where the player will be in the new frame. False if one is under way,
-- the server is not connected or the anchor is already that one.
function M.rebaseTo(lat, lon)
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon then return false, 'bad lat/lon' end
  if not st.anchor then return false, 'no anchor' end
  if st.pending then return false, 'anchor request under way (' .. tostring(st.pending.purpose) .. ')' end
  if st.rebasing then return false, 'rebase under way' end
  if not net.isConnected() then return false, 'server not connected' end
  if math.abs(st.anchor.lat - lat) < 1e-9 and math.abs(st.anchor.lon - lon) < 1e-9 then return false, 'same anchor' end
  local pl = st.player
  local x, y, z = 0, 0, 0
  if pl then x, y, z = pl.x, pl.y, pl.z end
  local nx, ny = st.anchor:transformTo(geodesy.newAnchor(lat, lon)):apply(x, y, z)
  local cx, cy = follow.target(nx, ny, pl and pl.vx or 0, pl and pl.vy or 0)
  return requestAnchor(lat, lon, 'resync', cx, cy)
end

-- name (optional): the place's name for the recent places (places.lua; the settings page's Go);
-- opts (optional): { record = false } keeps it out of the recent places (test tools)
function M.teleport(lat, lon, name, opts)
  local kind = (type(opts) == 'table' and opts.record == false) and 'tool' or 'teleport'
  return teleportTo(lat, lon, name, kind)
end

function M.toggleUI() ui.visible = not ui.visible end

-- The session's terrain block size (docs/V1_4_SPEC.md 3.5, M4): 1024 (v1.3.1) or 512, from the
-- server's hello.blockSize. Sets the lattice, the terrain-follow table, the importMaps layer / flat
-- maps and the rebase staleness rule together. Returns the size in use and whether `size` was valid.
function M.setBlockSize(size)
  local n, ok = lattice.configure(tonumber(size))
  follow.configure(n)
  terrain.configure(n)
  rebase.configure(n)
  blockSize = n
  return n, ok
end

-- Who draws the near ground: 'mesh' (far tiles; the TerrainBlock is physics-only, render disabled),
-- 'detail' (the mesh layout with the block drawn: aerial photo base, surface textures; 2026-10-12) or
-- 'terrain' (v1.2; anything else). Set by the server's hello; callable from the console to test.
local rawView = false
function M.setNearVisual(mode)
  nearVisual = (mode == 'mesh' or mode == 'detail') and mode or 'terrain'
  terrain.setRenderEnabled(nearVisual == 'terrain' or (nearVisual == 'detail' and not rawView))
  farfield.setNearVisual(nearVisual)
  return nearVisual
end

-- Detail mode, dev tools: on = show the raw data (the far tiles, aerial photo on the DEM) instead of the
-- generated near ground (the drawn block), for before / after captures. No server round trip: the tiles
-- under the block are loaded anyway (hidden or sunk). Returns the state.
function M.setRawView(on)
  rawView = on and true or false
  farfield.setRawView(rawView)
  terrain.setRenderEnabled(nearVisual == 'terrain' or (nearVisual == 'detail' and not rawView))
  return rawView
end

function M.rawView() return rawView end

function M.nearVisualMode() return nearVisual end

-- Horizon mode (docs/V1_4_SPEC.md 3.6 M1h), from the server's hello.horizon: the view distance follows
-- the far-field radius (setFarRadius, from status.radius) and the sky uses the tiles' planet radius.
-- Off restores both. Returns the mode in use.
function M.setHorizon(on)
  horizonOn = on and true or false
  if not horizonOn then farRadius = nil end
  applyHorizon()
  return horizonOn
end

-- The server's far-field radius in metres (status.radius, horizon mode). Returns the view distance in use.
function M.setFarRadius(m)
  farRadius = tonumber(m)
  applyHorizon()
  return st.visibleDistance or VISIBLE_DISTANCE
end

-- Origin rebase distance in metres (2000-25000, default 10000). Returns the value in use.
function M.setRebaseDistance(m)
  local d = rebase.setDistance(m)
  log('I', 'beamngEarth', 'rebase distance ' .. d .. ' m')
  return d
end

-- Far-tile streaming budget per frame in ms (the governor's target, 1-4 ms; default 2). Returns the value in use.
function M.setFarBudget(ms)
  return gov:setTarget(ms)
end

-- The connected server's hello (its version and options; nil before one arrived), for the reports.
function M.serverHello()
  if not lastHello then return nil end
  local c = {}
  for k, v in pairs(lastHello) do c[k] = v end
  return c
end

function M.status()
  return dumps({ net = net.state(), lastError = net.lastError, session = st.sessionId, active = st.active,
    inOurLevel = st.inOurLevel, nearVisual = nearVisual, rawView = rawView, terrainRendered = terrain.isRenderEnabled(),
    anchorId = st.anchorId, anchorKey = st.anchorKey, pending = st.pending and st.pending.purpose,
    block = st.block, terrainPending = st.terrainPending and { st.terrainPending.cx, st.terrainPending.cy },
    followWait = st.followWait, commits = st.commits,
    followHeld = st.followHeld, followHolds = st.followHolds, groundAhead = st.groundAhead and st.groundAhead.z,
    baseRefreshes = st.baseRefreshes,
    rebaseDistance = rebase.distance, lastFollowMs = st.lastFollowMs, lastRebaseMs = st.lastRebaseMs,
    lastRebaseWaitFrames = st.lastRebaseWait, rebaseLag = rebaseLag, lastMoveLag = st.lastMoveLag, player = st.player, far = farfield.stats(), governor = gov:snapshot(), status = st.status,
    serverBacklog = st.serverBacklog, followCount = st.followCount, rebaseCount = st.rebaseCount,
    cells = crossover.cells(), blockSize = blockSize, anchorLatency = st.anchorLatency,
    terrainLatency = st.terrainLatency, horizon = horizonOn, farRadius = farRadius,
    visibleDistance = st.visibleDistance or VISIBLE_DISTANCE, banner = banner.current().id, guard = st.guard.mode,
    sessions = farfield.heldSessions(), tileSession = farfield.session(), sea = sea.status(), trees = trees.status() })
end

-- The connection state line (the settings page shows it): the banner's { id, level, text } (text
-- 'World server connected' while all is well) and the edge guard's mode.
function M.banner()
  local b = banner.current()
  return { id = b.id, level = b.level, text = banner.line(), guard = st.guard.mode }
end

-- net.close() drops its '_disconnected' event, so reset the connection state here.
function M.reconnect()
  net.close()
  onMessage({ type = '_disconnected', error = 'reconnect requested' })
end

-- The credits line in use (the settings page shows it).
function M.attribution() return attributionText(farfield.stats()) end
-- The map's credits by area: { imagery = [[credit, w, s, e, n] or [credit]], other = credit } or nil.
function M.creditsByArea()
  if not serverCreditsByArea then return nil end
  return { imagery = serverCreditsByArea, other = serverOtherCredits or '' }
end

-- The on-screen data credits in use: the server's (near + far), else the attribution line.
function M.credits() return credits.text(creditsFallback) end
-- The update the server found on GitHub ({ current, latest, url }) or nil (settings page).
function M.updateInfo() return updateInfo end

-- The download cache (server cache.py), for the settings page: the last usage the server sent (nil
-- before any: no server, or one without a cache), and the page's requests to the server: the size cap
-- in GB (0 = unlimited), a clear (answered with clearedBytes), fresh usage numbers. Each returns
-- whether it went out (false without a server session; a clear also needs the server's numbers, and
-- says why it did not go out).
function M.cacheState() return cacheInfo end
local function cacheReady() return net.isConnected() and st.sessionId ~= nil end
function M.cacheSetCap(gb)
  if not cacheReady() then return false end
  net.send({ type = 'cache', capGb = gb })
  return true
end
function M.cacheClear()
  if not cacheReady() then return false, 'server not connected' end
  -- a server without a cache sends no 'cache' after hello and would never answer the clear
  if not cacheInfo then return false, 'the server has no download cache' end
  net.send({ type = 'cache', clear = true })
  return true
end
function M.cacheQuery()
  if not cacheReady() then return false end
  net.send({ type = 'cache' })
  return true
end

-- The world map's search box: addresses and places from the server (geocode.py); the answer is geocode_result.
function M.requestGeocode(id, q, lat, lon)
  if not net.isConnected() then return false end
  net.send({ type = 'geocode', id = id, q = q, lat = lat, lon = lon })
  return true
end
function M.requestRoute(id, lat0, lon0, lat1, lon1, bearing)
  if not net.isConnected() then return false end
  net.send({ type = 'route', id = id, lat0 = lat0, lon0 = lon0, lat1 = lat1, lon1 = lon1, bearing = bearing })
  return true
end
function M.takeRoute(id)
  local r = routeResults[id]
  routeResults[id] = nil
  return r
end
function M.takeGeocode(id)
  local r = geocodeResults[id]
  geocodeResults[id] = nil
  return r
end

-- The world map asks for its tiles as files (settings.mapTiles; {{z, x, y, layer}, ...}, the most wanted first;
-- an empty list cancels the last one). Whether it went out.
function M.requestMapTiles(tiles)
  if not net.isConnected() then return false end
  net.send({ type = 'map_tiles', tiles = tiles })
  return true
end

function M._state() return st end -- read-only: the dev tools and the settings page
function M._rebaseLag() return rebaseLag end -- for tests
function M._governor() return gov end

-- The Roads setting: off removes them; on again asks the server to send the tiles once more.
function M.setRoadsEnabled(on)
  if roads.setEnabled(on) then net.send({ type = 'roads_reset' }) end
end

M.onUpdate = onUpdate
M.onPreRender = onPreRender
M.onUiChangedState = onUiChangedState
M.onClientStartMission = onClientStartMission
M.onClientPreStartMission = onClientPreStartMission
M.onNavgraphReloaded = function() navgraph.onReloaded() end
M.onVehicleMapmgrUpdate = function(id) gps.onVehicleMapmgrUpdate(id) end
M.onClientEndMission = onClientEndMission
M.onClientPostStartMission = onClientPostStartMission
M.onExtensionLoaded = onExtensionLoaded
M.onExtensionUnloaded = onExtensionUnloaded

return M
