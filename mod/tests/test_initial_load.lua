-- First ground import through the real core consumer, with engine and transport boundaries stubbed.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
getPlayerVehicle = function() return nil end
getAllVehicles = function() return {} end
commands = { isFreeCamera = function() return true end }
core_celestial = { setLocation = function() end }
ui_imgui = setmetatable({ Begin = function() return true end,
  BoolPtr = function(v) return { [0] = v } end, DoublePtr = function(v) return { [0] = v } end,
  FloatPtr = function(v) return { [0] = v } end, Button = function() return false end },
  { __index = function() return function() end end })
local incoming, sent = {}, {}
local net = { PORT = 47613, attempts = function() return 1 end, everConnected = function() return true end }
net.isConnected = function() return true end
net.state = function() return 'connected' end
net.send = function(msg) sent[#sent + 1] = msg; return true end
net.update = function() local batch = incoming; incoming = {}; return batch end
net.close = function() end
net.setIdle = function() end
package.loaded['beamngEarth/net'] = net
local function received(kind)
  local out = {}; for _, m in ipairs(sent) do if m.type == kind then out[#out + 1] = m end end; return out
end
local function message(msg) incoming[#incoming + 1] = msg end
local core = require('beamngEarth/core')
local notices = {}
guihooks = { trigger = function(kind, msg) if kind == 'Message' then notices[#notices + 1] = msg end end }
require('beamngEarth/statusbar').enabled = false
local terrain = require('beamngEarth/terrain')
local now, paused = 0, false
core._governor().clock = function() return now end
local function frame(dt)
  dt = dt or .05
  now = now + dt * 1000
  core.onUpdate(dt, paused and 0 or dt)
  core.onPreRender(dt, dt)
end
local function frames(n) for _ = 1, n do frame() end end
local function hello()
  message({ type = 'hello', protocol = 4, format = 4, serverVersion = 'fixture', sessionId = 'cold',
    farFormat = 'cdae', material = 'bngEarthFar_v3', nearVisual = 'terrain', blockSize = 1024, squareSize = 1 })
  frames(2)
end
local function answer(req, aid)
  message({ type = 'anchor_ready', requestId = req.requestId, anchorId = aid, lat = req.lat, lon = req.lon,
    groundZ = 10, terrain = { cx = req.cx, cy = req.cy, size = 1024, squareSize = 1,
      heightmap = '/temp/beamngEarth/cold/first.png', zBase = 0, maxHeight = 1024 } })
end

core.onExtensionLoaded()
hello()
local req = received('prepare_anchor')[1]
T.check(req ~= nil, 'a cold first place requests its ground')
local load, imports = terrain.load, 0
terrain.load = function(...)
  imports = imports + 1
  if imports == 1 then return false, 'fixture: first file import failed' end
  return load(...)
end
answer(req, 1)
frames(2)
answer(req, 1) -- a duplicate must not consume an import attempt during its backoff
frames(2)
T.check(imports == 1, 'duplicate ready messages do not retry ahead of the local backoff')
paused = true
frames(180) -- 9 seconds of virtual time, no wall-clock sleeps
paused = false
T.check(imports >= 2 and core._state().anchorId == 1,
  'a transient first terrain import failure recovers even while paused without a vehicle')
T.check(#received('prepare_anchor') == 1, 'import retry reuses the prepared files instead of rebuilding the world')
T.check(#received('commit_anchor') == 1, 'the first anchor is committed exactly once after successful import')

local policy = require('beamngEarth/anchorload')
local function reset()
  terrain.load = load
  core.onExtensionUnloaded()
  incoming, sent = {}, {}
  core.onExtensionLoaded()
  hello()
  return received('prepare_anchor')[1]
end
local function pumpUntil(cond, limit)
  for _ = 1, limit or 800 do if cond() then return true end; frame() end
  return cond()
end
local function count(kind) return #received(kind) end

-- Permanent engine import failure: bounded local attempts, visible error, no premature anchor/physics commit.
req = reset()
imports = 0
terrain.load = function() imports = imports + 1; return false, 'fixture: permanently unavailable file' end
answer(req, 2)
frames(200)
T.check(imports == policy.IMPORT_TRIES, 'a permanent import failure stops at its bounded retry budget')
T.check(core._state().pending == nil and core._state().anchor == nil and core._state().loadingFault == 'import',
  'failed first ground has an explicit terminal state')
T.check(#notices > 0 and notices[#notices].msg:find('select the location again', 1, true),
  'a failed import supplies recovery guidance even when the loading bar is disabled')
T.check(count('prepare_anchor') == 1 and count('commit_anchor') == 0, 'failure neither rebuilds nor commits the unusable ground')
frames(200)
T.check(imports == policy.IMPORT_TRIES, 'an exhausted import does not resume spinning every frame')

-- Choosing a place again resets recovery, and late replies from the abandoned request cannot commit it.
terrain.load = load
core.teleport(45., 6.)
local nextReq = received('prepare_anchor')[2]
message({ type = 'anchor_progress', requestId = req.requestId, stage = 'land_cover', elapsed = 99 })
answer(req, 77)
frames(2)
T.check(core._state().pending.requestId == nextReq.requestId and not core._state().pending.stage,
  'stale progress and stale ready messages cannot mutate a replacement request')
answer(nextReq, 3)
frames(15)
T.check(core._state().anchorId == 3 and core._state().loadingFault == nil, 'a new map selection recovers from a terminal failure')

-- Connected server that keeps reporting activity but never finishes: the total first-load budget is bounded.
req = reset()
for _ = 1, math.ceil(policy.MAX_S / 5) + 5 do      -- (100 frames of 0.05 s each)
  message({ type = 'anchor_progress', requestId = req.requestId, stage = 'terrain', elapsed = 0 })
  frames(100)
end
T.check(core._state().pending == nil and core._state().loadingFault == 'timeout', 'heartbeats cannot extend the total loading budget forever')
T.check(count('prepare_anchor') == 1 and count('commit_anchor') == 0, 'timeout does not multiply potentially running server jobs')
answer(req, 88); frames(2)
T.check(core._state().anchor == nil, 'late ready after a deadline cannot commit an abandoned location')

req = reset()
answer(req, 89)
frame(policy.MAX_S + 1) -- a late ready is processed before updateWorld checks its timer
T.check(core._state().loadingFault == 'timeout' and count('commit_anchor') == 0,
  'ready arriving in the deadline frame cannot bypass the total time limit')

-- Generation errors retry with backoff, then end with a diagnostic instead of retrying forever.
req = reset()
for attempt = 1, policy.PREPARE_TRIES do
  req = received('prepare_anchor')[attempt]
  message({ type = 'anchor_error', requestId = req.requestId, error = 'fixture: no source data' })
  frames(2)
  if attempt < policy.PREPARE_TRIES then
    local before = count('prepare_anchor')
    frames(10)
    T.check(count('prepare_anchor') == before, 'generation retry waits for its backoff')
    T.check(pumpUntil(function() return count('prepare_anchor') == attempt + 1 end, 1500),     -- (the backoff's 60 s cap)
      'next generation attempt eventually starts')
  end
end
T.check(core._state().loadingFault == 'prepare' and core._state().teleportRetry == nil,
  'repeated generation failures end with an actionable error')
T.check(count('prepare_anchor') == policy.PREPARE_TRIES, 'the generation retry budget is enforced')

-- An exception can mean a partial commit: show the error, do not run the commit again automatically.
req = reset()
imports = 0
terrain.load = function() imports = imports + 1; error('fixture: unexpected engine exception') end
answer(req, 4)
frames(200)
T.check(imports == 1 and core._state().loadingFault == 'import', 'an exceptional commit is not retried unsafely')
T.check(count('commit_anchor') == 0 and core._state().pending == nil, 'an exceptional commit ends the pending load')

local lp = require('beamngEarth/loadprogress')
lp.start(0)
local failure = policy.failure('import')
T.check(lp.line(1000, { failure = failure }) == failure and not failure:find('Loading', 1, true),
  'the loading HUD retains the terminal error instead of hiding it behind Loading ground or its expiry')
T.check(policy.text(2, { purpose = 'teleport', sentAt = 0, stage = 'land_cover', progressAt = 1 }) == 'Loading land cover (2 s)',
  'phase identifiers become useful user-facing progress')
T.done('test_initial_load')
