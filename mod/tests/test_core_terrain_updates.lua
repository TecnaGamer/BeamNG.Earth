-- Engine-call regressions: network delivery must not rebuild terrain/materials, and a pending refresh plus its
-- photo consumes one isolated heavy slot. The real core, terrain and governor run against the offline engine stubs.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local inbox = {}
package.loaded['beamngEarth/net'] = setmetatable({
  update = function() local q = inbox; inbox = {}; return q end,
  isConnected = function() return false end,
  isReady = function() return false end,
  lease = function() return nil end,
}, { __index = function() return function() end end })
function getPlayerVehicle() return nil end
function getAllVehicles() return {} end
local core = require('beamngEarth/core')
local terrain = require('beamngEarth/terrain')
local gov = core._governor()
local function deliver(...)
  inbox = { ... }
  core.onUpdate(0.016, 0.016)
end
deliver({ type = 'hello', protocol = 4, format = 4, cells = 64, sessionId = 's1', nearVisual = 'detail',
  blockSize = 1024, squareSize = 1 })
local dirt = createObject('TerrainMaterial'); dirt:registerObject(terrain.MATERIAL)
T.check(terrain.loadFlat(), 'flat block ready for swaps')
local st = core._state()
st.anchorId, st.block = 1, { cx = 0, cy = 0 }
local function player(speed) st.player = { speed = speed or 0 } end
local function calm()
  gov.frame = gov.frame + 10
  gov.b, gov.bPrev, gov.tokens, gov.blocked = 0, 0, 0, false
  gov.w1, gov.w2, gov.base = 16, 16, 16
  gov.lastStreamFrame = gov.frame - 10
end
local function refresh(path)
  return { type = 'terrain_refresh', anchorId = 1, cx = 0, cy = 0, size = 1024, squareSize = 1,
    ter = path, heightmap = path .. '.png', zBase = 0, maxHeight = 1024, base = '/old.jpg', baseSize = 1024 }
end
local function photo(path)
  return { type = 'block_base', anchorId = 1, cx = 0, cy = 0, base = path, baseSize = 1024 }
end
local swaps, updates = terrain.swaps or 0, S.calls.updateMaterial or 0
deliver(refresh('/graded.ter'), photo('/latest.jpg'))
T.check((terrain.swaps or 0) == swaps and (S.calls.updateMaterial or 0) == updates,
  'receiving terrain and photo metadata performs no engine rebuild')
T.check(st.deferredRefresh ~= nil and core._baseLater ~= nil, 'both updates retained until a heavy slot')
player(60)
calm()
T.check(core.applyBlockUpdates(), 'a combined update gets a heavy slot')
T.check(terrain.swaps == swaps + 1 and terrain.base() == '/latest.jpg', 'one terrain swap binds the newest photo')
T.check((S.calls.updateMaterial or 0) == updates and core._baseLater == nil and st.deferredRefresh == nil,
  'no separate material rebuild remains after the swap')
deliver(photo('/next.jpg'))
player()
-- A commit already ran in this frame: receiving another photo never shares that heavy frame.
gov.heavyFrame = gov.frame
T.check(not core.applyBlockUpdates() and core._baseLater ~= nil, 'another heavy operation owns this frame')
gov.heavyFrame = -1
gov.blocked = true
T.check(not core.applyBlockUpdates(), 'the quiet frames after a heavy operation are respected')
calm()
local request = gov.requestHeavy
gov.requestHeavy = function() return false end
T.check(not core.applyBlockUpdates() and core._baseLater ~= nil, 'a refused slot keeps its photo for later')
gov.requestHeavy = request
T.check(core.applyBlockUpdates() and terrain.base() == '/next.jpg', 'photo alone eventually uses a heavy slot')
T.check((S.calls.updateMaterial or 0) == updates + 1, 'only the current material slot rebuilt once')

deliver(photo('/minimized.jpg'))
player()
calm()
local canvas = createObject('GuiCanvas'); canvas:registerObject('Canvas')
function canvas:getWindowClientSizeXY() return 0, 0 end
T.check(not core.applyBlockUpdates() and core._baseLater ~= nil, 'minimized window keeps its pending photo untouched')
function canvas:getWindowClientSizeXY() return 1920, 1080 end
st.placeFrames = 2
T.check(not core.applyBlockUpdates(), 'vehicle placement owns its terrain until it finishes')
st.placeFrames = nil
st.rebasing = {}
T.check(not core.applyBlockUpdates(), 'a two-phase rebase cannot be interrupted by a photo update')
st.rebasing = nil
T.check(core.applyBlockUpdates() and terrain.base() == '/minimized.jpg', 'restored window applies its photo later')

deliver(photo('/earlier.jpg'), refresh('/newer.ter'))
player()
calm()
T.check(core.applyBlockUpdates() and terrain.base() == '/old.jpg', 'a later terrain owns its base over an older photo')
deliver(refresh('/obsolete.ter'), photo('/obsolete.jpg'))
st.block = { cx = 128, cy = 0 }
player()
calm()
swaps, updates = terrain.swaps, S.calls.updateMaterial or 0
T.check(not core.applyBlockUpdates() and not st.deferredRefresh and not core._baseLater,
  'updates of the previous block discarded before requesting a slot')
T.check(terrain.swaps == swaps and (S.calls.updateMaterial or 0) == updates, 'obsolete block does no engine work')

st.block = { cx = 0, cy = 0 }
deliver(refresh('/lost.ter'), photo('/lost.jpg'), { type = '_disconnected' })
T.check(not st.deferredRefresh and not core._baseLater, 'disconnect drops queued updates of the old session')
st.anchorId = 1
deliver(refresh('/before-hello.ter'), photo('/before-hello.jpg'),
  { type = 'hello', protocol = 4, format = 4, cells = 64, sessionId = 's2', nearVisual = 'detail' })
T.check(not st.deferredRefresh and not core._baseLater, 'hello cannot reuse an old session photo with the same anchor id')
T.done('test_core_terrain_updates')
