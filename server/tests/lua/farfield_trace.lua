-- Runs the mod's farfield.lua (with the offline engine stubs of mod/tests/stubs.lua and the real
-- governor.lua) on a scripted message stream and prints its state after every frame, for
-- server/tests/test_modsim.py, which runs the Python port (modsim.FarField) on the same script.
--   luajit farfield_trace.lua <mod lua/ge/extensions dir> <mod tests dir> <script file>
-- Script lines (space separated):
--   A aid keepOld cx cy [s]    switchAnchor(aid, keepOld == 1, {cx, cy}, s): s, the server session, also
--                              names the folder of the following tiles' files (/temp/beamngEarth/<s>/; 'x')
--   B cx cy                    onBlockCommit(cx, cy)
--   C x y z fx fy fz fov       camera for the next frames
--   T aid key level x y z err sink|- [material|- [tex]]   far_tile (shape <key>.cdae, state 'group'; with
--                              a material: texture <key>.dds, detail = true; tex: FORMAT 4 texture px)
--   S aid id dIn|- dOut|- nAdd adds... nRem removes...                far_swap
--   R aid key                  far_remove        D aid    far_done
--   U ocx ocy ncx ncy          print uncoverReady
--   N mesh|terrain             setNearVisual
--   F                          one frame: governor beginFrame + farfield.update
--   H                          print heldSessions and heldAnchors
-- Cost model: registerObject (a TSStatic or a per-tile Material) costs IMPORT_MS, a delete DELETE_MS
-- (the governor measures them with its clock); everything else is free.
local dir, testsDir, script = arg[1], arg[2], arg[3]
package.path = dir .. '/?.lua;' .. package.path
local S = dofile(testsDir .. '/stubs.lua')
local IMPORT_MS, DELETE_MS, BASE_MS, DT = 1.75, 0.25, 11.5, 1 / 60 -- exact binary fractions

local now = 0
local orig = createObject
createObject = function(class)
  local o = orig(class)
  local reg, del = o.registerObject, o.delete
  o.registerObject = function(self, n) now = now + IMPORT_MS; return reg(self, n) end
  o.delete = function(self) now = now + DELETE_MS; return del(self) end
  return o
end

local governor = require('beamngEarth/governor')
local farfield = require('beamngEarth/farfield')
-- the Python port (modsim.FarField) models no spares (lazy unloading, farfield.lua SPARE_MAX): removed
-- tiles are deleted, as before 2026-10-13
farfield.SPARE_MAX = 0
local gov = governor.new({ clock = function() return now end })

local function hash(s, h)
  for i = 1, #s do h = (h * 31 + s:byte(i)) % 2147483647 end
  return h
end

local keys, keyList, dirty = {}, {}, false
local cam, wall = nil, nil
local frame = 0
local function num(v) if v == '-' then return nil end return tonumber(v) end
local folder = 'x'

for line in io.lines(script) do
  local w = {}
  for t in line:gmatch('%S+') do w[#w + 1] = t end
  local c = w[1]
  if c == 'A' then
    farfield.switchAnchor(tonumber(w[2]), w[3] == '1', { cx = tonumber(w[4]), cy = tonumber(w[5]) }, w[6])
    if w[6] then folder = w[6] end
  elseif c == 'B' then
    farfield.onBlockCommit(tonumber(w[2]), tonumber(w[3]))
  elseif c == 'X' then              -- crossover.CROSS, crossover.COVER_SINK_S (off by default since 2026-11-21)
    local xo = require('beamngEarth/crossover')
    xo.CROSS, xo.COVER_SINK_S = tonumber(w[2]), tonumber(w[3])
  elseif c == 'C' then
    cam = { x = tonumber(w[2]), y = tonumber(w[3]), z = tonumber(w[4]), fx = tonumber(w[5]), fy = tonumber(w[6]),
      fz = tonumber(w[7]), fovDeg = tonumber(w[8]) }
  elseif c == 'T' then
    local key = w[3]
    if not keys[key] then keys[key] = true; keyList[#keyList + 1] = key; dirty = true end
    local mat = (w[10] and w[10] ~= '-') and w[10] or nil
    farfield.onTile({ type = 'far_tile', anchorId = tonumber(w[2]), key = key, level = tonumber(w[4]),
      shape = '/temp/beamngEarth/' .. folder .. '/far_' .. key .. '.cdae', pos = { tonumber(w[5]), tonumber(w[6]), tonumber(w[7]) },
      err = tonumber(w[8]), sink = num(w[9]), state = 'group', material = mat,
      texture = mat and ('/temp/beamngEarth/' .. folder .. '/far_' .. key .. '.dds') or nil, detail = mat ~= nil,
      tex = num(w[11] or '-') })
  elseif c == 'S' then
    local i, add, rem = 6, {}, {}
    for _ = 1, tonumber(w[i]) do i = i + 1; add[#add + 1] = w[i] end
    i = i + 1
    for _ = 1, tonumber(w[i]) do i = i + 1; rem[#rem + 1] = w[i] end
    farfield.onSwap({ type = 'far_swap', anchorId = tonumber(w[2]), id = tonumber(w[3]), add = add, remove = rem,
      dIn = num(w[4]), dOut = num(w[5]) })
  elseif c == 'R' then
    farfield.onRemove({ type = 'far_remove', anchorId = tonumber(w[2]), key = w[3] })
  elseif c == 'D' then
    farfield.onDone({ type = 'far_done', anchorId = tonumber(w[2]), count = 0 })
  elseif c == 'N' then
    farfield.setNearVisual(w[2])
  elseif c == 'H' then
    print('H ' .. table.concat(farfield.heldSessions(), ',') .. ' ' .. table.concat(farfield.heldAnchors(), ','))
  elseif c == 'U' then
    print('U ' .. tostring(farfield.uncoverReady(tonumber(w[2]), tonumber(w[3]), tonumber(w[4]), tonumber(w[5]))))
  elseif c == 'F' then
    frame = frame + 1
    local t0 = now
    gov:beginFrame(farfield.hasWork(), wall)
    farfield.update(gov, DT, cam)
    wall = BASE_MS + (now - t0)
    if dirty then table.sort(keyList); dirty = false end
    local h, nv = 7, 0
    for _, k in ipairs(keyList) do
      local t = farfield._tile(k)
      local o = t and t.id and S.objects[t.id]
      if o and not o.hidden then h = hash(k .. ',', h); nv = nv + 1 end
    end
    local s = farfield.stats()
    print(string.format('F %d %.9f %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d', frame, gov.b, s.frameUnits, s.live,
      s.covered, s.preloaded, s.queued, s.groups, s.transitions, s.old, s.deleting, s.imports, s.crossovers,
      s.instant, nv, h, s.materials))
  end
end
