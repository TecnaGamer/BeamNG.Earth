-- Collision rebuilds use the real governor: isolate normal rebuilds, keep urgent colliders safe,
-- and never fold a global collision rebuild's cost into the estimate for importing one mesh.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local wallMs = 0
function hptimer()
  local last = wallMs
  return { stopAndReset = function()
    local dt = wallMs - last
    last = wallMs
    return dt
  end }
end
-- A multi-threaded engine's process CPU time can greatly exceed its elapsed wall time.
os.clock = function() return wallMs * 32 / 1000 end
local originalCreate = createObject
local imports = 0
createObject = function(class)
  if class == 'TSStatic' then wallMs = wallMs + 3; imports = imports + 1 end
  return originalCreate(class)
end
be.reloadCollision = function() S.count('reloadCollision'); wallMs = wallMs + 56 end
local files = {
  road = '{ chunks = {}, road = { shape = "/t/road.cdae", pos = {0, 0, 0} } }',
  deck = '{ chunks = {}, deck = { shape = "/t/deck.cdae", pos = {0, 0, 0} } }',
}
readFile = function(p) return files[p] end
jsonDecode = function(s) return assert(load('return ' .. s))() end

local governor = require('beamngEarth/governor')
local roads = require('beamngEarth/roads')
local function newGovernor()
  local g = governor.new({ clock = function() return wallMs end })
  g:try('roadWalk')
  for _ = 1, 100 do wallMs = wallMs + 16; g:beginFrame(true) end
  return g
end
local function frame(g)
  wallMs = wallMs + 16
  g:beginFrame(roads.hasWork(), 16)
  local beforeReload, beforeImport = S.calls.reloadCollision or 0, imports
  roads.update(g, 1)
  return { reloaded = (S.calls.reloadCollision or 0) > beforeReload, imported = imports - beforeImport,
    heavy = g:isHeavyFrame(), units = g.frameUnits }
end
local function fresh()
  roads.clear()
  package.loaded['beamngEarth/roads'] = nil
  roads = require('beamngEarth/roads')
  return newGovernor()
end

-- A normal periodic rebuild must wait for a heavy slot, with two quiet frames on either side.
do
  local g = newGovernor()
  wallMs = wallMs + 7000
  roads.onTile({ anchorId = 1, key = '0_0', path = 'road' }, 1)
  roads.requestCollision() -- e.g. nearby Forest items made since the last rebuild
  local log, heavyAt = {}, nil
  for i = 1, 100 do
    log[i] = frame(g)
    if log[i].reloaded then heavyAt = i end
  end
  T.check(not log[1].reloaded, 'a regular rebuild waits for the governor to ramp streaming down')
  T.check(heavyAt and heavyAt > 1 and heavyAt < 30, 'a regular rebuild receives a bounded heavy slot')
  if heavyAt then
    local isolated = true
    for i = math.max(1, heavyAt - 2), heavyAt + 2 do
      if log[i].units ~= 0 then isolated = false end
    end
    T.check(log[heavyAt].heavy and isolated, 'no streaming units within two frames of the regular rebuild')
  end
  T.check(roads.status().roadMeshes == 1, 'queued road imports resume after the heavy slot')
  T.near(g.est.roadWalk, 3, 1e-9, 'the 56 ms rebuild does not inflate the 3 ms mesh-import estimate')
  local unit = roads.status().units.reloadCollision
  T.check(unit and unit.n == 1 and unit.ms == 56, 'isolated rebuilds retain the upstream collision cost diagnostics')
end

-- An urgent collider made this frame is rebuilt at the start of the next frame; no more meshes
-- join that frame, even at a full driving budget. Collision deadlines bypass the normal ramp.
do
  local car = { getPosition = function() return vec3(0, 0, 0) end,
    getVelocity = function() return vec3(80, 0, 0) end }
  getPlayerVehicle = function() return car end
  local g = fresh()
  roads.onTile({ anchorId = 1, key = '0_0', path = 'deck' }, 1)
  local made = frame(g)
  T.check(made.imported == 1 and not made.reloaded, 'a new urgent deck does not stack a rebuild on its import')
  roads.onTile({ anchorId = 1, key = '1_0', path = 'road' }, 1)
  local nextFrame = frame(g)
  T.check(nextFrame.reloaded and nextFrame.heavy and nextFrame.imported == 0,
    'urgent collision is ready next frame, with further mesh imports blocked')
  T.near(g.est.roadWalk, 3, 1e-9, 'mesh costs use wall time even when process CPU time runs 32 times faster')
  T.check(frame(g).units == 0 and frame(g).units == 0, 'urgent rebuild also has two quiet following frames')
  getPlayerVehicle = nil
end

-- A pending urgent rebuild still observes both the frame and wall-time terrain-swap quiet window.
do
  local g = fresh()
  roads.setHold(true)
  roads.requestCollision()
  roads.terrainSwapped()
  local quiet = true
  for _ = 1, 9 do if frame(g).reloaded then quiet = false end end
  T.check(quiet and roads.swapQuiet(), 'urgent rebuild waits at least 150 ms after a terrain swap')
  local nextFrame = frame(g)
  T.check(nextFrame.reloaded and nextFrame.heavy and not roads.swapQuiet(),
    'the urgent rebuild proceeds when the swapped terrain has settled')
  roads.setHold(false)
end

T.done('test_road_collision_budget')
