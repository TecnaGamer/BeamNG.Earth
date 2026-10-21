-- luajit mod/tests/test_farfield_spares.lua  (far field lazy unloading: the spare pool, 2026-10-13)
-- A removed tile of the current anchor is kept hidden and revived when the server sends it again
-- (same key, same shape file) without a new import; spares expire (SPARE_S), are capped (SPARE_MAX),
-- keep their session folder held, and go at an anchor switch / rebase.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local farfield = require('beamngEarth/farfield')

local free = { clock = function() return 0 end }
function free:try() return true end
function free:charge() end
function free:chargeMotion() end
local DT = 1 / 60
local function key(l, i, j) return l .. '_' .. i .. '_' .. j end
local function tile(a, l, i, j, sess)
  return { type = 'far_tile', anchorId = a, key = key(l, i, j), level = l,
    shape = '/temp/beamngEarth/' .. (sess or 's1') .. '/far_' .. a .. '_' .. key(l, i, j) .. '.cdae',
    pos = { i * 256 * 2 ^ l, j * 256 * 2 ^ l, 100 }, err = 0.5, state = 'group' }
end
local function swap(a, id, add, remove)
  return { type = 'far_swap', anchorId = a, id = id, add = add, remove = remove or {}, dIn = 1, dOut = 1 }
end
local function step(n) for _ = 1, n or 1 do farfield.update(free, DT, nil) end end
local function creates() return S.calls.createObject_TSStatic or 0 end
local function settle() step(120) end

T.check(farfield.SPARE_MAX == 64 and farfield.SPARE_S == 30, 'default spare pool capped at 64 tiles / 30 s')
farfield.SPARE_MAX, farfield.SPARE_S = 400, 120
farfield.switchAnchor(1, false, { cx = 0, cy = 0 }, 's1')

-- a tile, then its four children replace it: the parent becomes a spare (hidden, not deleted)
farfield.onTile(tile(1, 1, 0, 8)); step()
farfield.onSwap(swap(1, 1, { key(1, 0, 8) })); settle()
local parent = farfield._tile(key(1, 0, 8))
local pid = parent.id
local kids = { key(0, 0, 16), key(0, 1, 16), key(0, 0, 17), key(0, 1, 17) }
for _, k in ipairs(kids) do local l, i, j = k:match('(%-?%d+)_(%-?%d+)_(%-?%d+)'); farfield.onTile(tile(1, 0, tonumber(i), tonumber(j))) end
step()
farfield.onSwap(swap(1, 2, kids, { key(1, 0, 8) })); settle()
T.check(S.objects[pid] ~= nil and S.objects[pid].hidden, 'the replaced tile is kept, hidden (a spare)')
T.check(farfield.stats().spares == 1 and farfield.counters().parked == 1, 'one spare parked')

-- the parent comes back (the player turned around): revived, no new TSStatic
local before = creates()
farfield.onTile(tile(1, 1, 0, 8)); step()
T.check(creates() == before, 'revived without a new import')
T.check(farfield._pending(key(1, 0, 8)).id == pid, 'the same object')
T.check(S.objects[pid].fields.isRenderShadowEnabled == 'false' and S.objects[pid].fields.castShadows == '0', 'revived spare keeps shadow casting disabled')
T.check(farfield.counters().revived == 1 and farfield.stats().spares == 0, 'counted, out of the pool')
farfield.onSwap(swap(1, 3, { key(1, 0, 8) }, kids)); settle()
T.check(S.objects[pid] ~= nil and not S.objects[pid].hidden, 'shown again by its swap')
T.check(farfield.stats().spares == 4, 'the four children are spares now')

-- a different shape file for the same key (a new server session) is imported, not revived
before = creates()
farfield.onTile(tile(1, 0, 0, 16, 's2')); step()
T.check(creates() == before + 1, 'another shape file: imported')
farfield.onRemove({ type = 'far_remove', anchorId = 1, key = key(0, 0, 16) }); step()  -- withdrawn

-- spares expire after SPARE_S
farfield.SPARE_S = 0
step(2)
T.check(farfield.stats().spares == 0, 'expired spares leave the pool')
settle()
T.check(#S.liveOf('TSStatic') <= 6, 'and are deleted')
farfield.SPARE_S = 120

-- the cap: the oldest go first (enforced immediately in park() as well as in update())
farfield.SPARE_MAX = 2
for _, k in ipairs(kids) do local l, i, j = k:match('(%-?%d+)_(%-?%d+)_(%-?%d+)'); farfield.onTile(tile(1, 0, tonumber(i), tonumber(j))) end
step()
farfield.onSwap(swap(1, 4, kids, { key(1, 0, 8) })); settle() -- parent -> spare (1)
T.check(farfield.stats().spares <= 2, 'at most SPARE_MAX spares')
farfield.onTile(tile(1, 1, 0, 8)); step()                      -- revive parent
farfield.onSwap(swap(1, 5, { key(1, 0, 8) }, kids)); settle() -- 4 kids parked -> immediately capped to 2 in park()
T.check(farfield.stats().spares == 2, 'park() enforces SPARE_MAX immediately when multiple tiles park at once')

-- a spare's folder stays held (the server must not delete its files)
farfield.SPARE_MAX = 400
local found = false
for _, f in ipairs(farfield.heldSessions()) do if f == 's1' then found = true end end
T.check(found, 'spare folders are held')

-- an anchor switch drops every spare into the delete queue
local lone = key(2, 9, 9)
farfield.onTile(tile(1, 2, 9, 9)); step()
farfield.onSwap(swap(1, 6, { lone })); settle()
farfield.onRemove({ type = 'far_remove', anchorId = 1, key = lone }); settle()
local n = farfield.stats().spares
farfield.switchAnchor(2, true, { cx = 0, cy = 0 }, 's1')
T.check(n >= 1 and farfield.stats().spares == 0, 'spares dropped at an anchor switch')

-- pooled shape files (server/matslots.py): a spare still holding a slot path is deleted before another
-- tile loads that path (the engine would share its cached shape)
settle()
local function slotTile(a, l, i, j, n, serial)
  local t = tile(a, l, i, j)
  t.shape = '/temp/beamngEarth/s1/slot_' .. string.format('%05d', n) .. '.cdae'
  t.slotSerial = serial
  return t
end
farfield.takeSlotsGone('s1') -- nothing pooled before this point
T.check(farfield.pooledShape('/temp/beamngEarth/s1/slot_00007.cdae') and not farfield.pooledShape(tile(2, 0, 0, 0).shape),
  'pooled shape paths recognised')
local A, B = key(0, 30, 30), key(0, 31, 30)
farfield.onTile(slotTile(2, 0, 30, 30, 7, 11)); step()
farfield.onSwap(swap(2, 7, { A })); settle()
local aid = farfield._tile(A).id
farfield.onRemove({ type = 'far_remove', anchorId = 2, key = A }); step()
T.check(S.objects[aid] ~= nil and farfield.stats().spares >= 1, 'the slot tile is a spare')
T.check(farfield.takeSlotsGone('s1') == nil, 'a spare still holds its slot: nothing reported')
local held = farfield.slotsHeld('s1')
T.check(#held == 1 and held[1][1] == 7 and held[1][2] == 11, 'held slots listed (hello after a reconnect)')
farfield.onTile(slotTile(2, 0, 31, 30, 7, 12)); step()      -- the server gave slot 7 to another tile
T.check(S.objects[aid] == nil, 'the spare holding the path was deleted first')
T.check(farfield._pending(B) and farfield._pending(B).loaded and farfield._pending(B).id ~= aid, 'the new tile loaded the path')
T.check((farfield.counters().slotFreed or 0) == 1 and (farfield.counters().slotBusy or 0) == 0, 'counted')
local gone = farfield.takeSlotsGone('s1')
T.check(gone and #gone == 1 and gone[1][1] == 7 and gone[1][2] == 11, 'the deleted spare reported its send gone')
T.check(farfield.takeSlotsGone('other') == nil, 'reports are taken once')
-- a pooled tile withdrawn before it ever spawned is reported too
farfield.onTile(slotTile(2, 0, 32, 30, 9, 13))
farfield.onRemove({ type = 'far_remove', anchorId = 2, key = key(0, 32, 30) }); settle()
gone = farfield.takeSlotsGone('s1') or {}
local found = false
for _, g in ipairs(gone) do if g[1] == 9 and g[2] == 13 then found = true end end
T.check(found, 'a never-spawned instance reports its slot gone')
settle()

T.done('test_farfield_spares')
