-- luajit mod/tests/bench_liveaudio_frame.lua
-- The per-frame Lua cost of the live weather sounds in ambience.update (liveaudio.lua): Live, Pre-recorded, and
-- neither, dry and raining, with N roofed spaces near the player (inTunnel walks them all). Prints µs per frame.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
dofile(dir .. "/testlib.lua")
local S = dofile(dir .. "/stubs.lua")
local function patch(o)
  function o:setTransform(m) self.xf = m end
  function o:getId() return self.id end
  function o:play() self.playing = true end
  function o:setVolumePitchCT(v) self.vol = v end
  function o:setPosition(p) self.p = p end
  return o
end
local newObject = S.newObject
S.newObject = function(class) return patch(newObject(class)) end
createObject = function(class) return S.newObject(class) end
function simObjectExists(o) return S.objects[o.id] ~= nil end
local cpu = os.clock

package.preload['kdtreepoint3d'] = function()
  return { new = function() return { n = 0, preLoad = function(self) self.n = self.n + 1 end, build = function() end } end }
end
local clock = 0
os.clockhp = function() return clock end
SFXSystem = { getPositionXYZ = function() return 0, 0, 0 end }
MatrixF = function()
  local m = {}
  function m:setFromEuler() end
  function m:setPosition() end
  return m
end
Sim = { upcast = function(o) return o end }
-- the game's runtime pieces liveaudio reads (volumes, pause, focus)
Engine = { generateUUID = function() return 'u' end,
  Audio = { getChannelVolume = function() return 0.8 end } }
settings = { getValue = function(k) if k == 'AudioMuteOnWindowLoseFocus' then return true end return 0.8 end }
simTimeAuthority = { getPause = function() return false end }
isWindowFocused = function() return true end
extensions = { core_audioForest = { setNodeCloud = function() end, setMainEvent = function() end, setEnabled = function() end },
  core_audioRibbon = { createEmitterHost = function(ev) return { eventNameStr = ev } end, updateRibbonData = function() end,
    setRibbons = function() end, recomputeMap = function() end, clearNearFarLists = function() end,
    clearAllSFXEmitters = function() end } }

local amb = require('beamngEarth/ambience')
local la = require('beamngEarth/liveaudio')
require('beamngEarth/storm')
local seaV = { rivers = function() return {} end }

-- N roofed spaces (tunnels, arches, parking decks) round the player, none holding it
local SPACES = tonumber(arg[1]) or 300
local row = {}
for i = 1, SPACES do
  local k = ({ 0, 2, 3 })[i % 3 + 1]
  for _, v in ipairs({ k, 100 + i * 7, 50 + (i % 13) * 9, 5, 40, 9, 8, 0.3 + i * 0.01 }) do row[#row + 1] = v end
end
pcall(amb.onSpaces, '1:0_0', row)

local function run(label, frames)
  local t0 = cpu()
  for _ = 1, frames do
    clock = clock + 1 / 60
    amb.update(3, seaV)
  end
  return (cpu() - t0) / frames * 1e6
end

local function case(label, mode, raining)
  beamngEarth_weather = raining and { rainNow = 1, rainMmHNow = 5, shelter = 1, windMsNow = 4 } or
    { rainNow = 0, rainMmHNow = 0, shelter = 1, windMsNow = 4 }
  la.reset()
  la.setMode(mode)
  la.setSender(mode and function() return true end or nil)
  run(label, 600)                              -- (warm up, the fade settles)
  local base = run(label, 20000)
  print(string.format('%-34s %7.2f us/frame', label, base))
end

print(string.format('%d roofed spaces near the player', SPACES))
case('no sender (disconnected), dry', 'prerecorded', false)
case('pre-recorded, dry', 'prerecorded', false)
case('pre-recorded, raining', 'prerecorded', true)
case('live, dry', 'live', false)
case('live, raining', 'live', true)
