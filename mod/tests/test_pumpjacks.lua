-- luajit mod/tests/test_pumpjacks.lua  (pump jacks at oil wells, pumpjacks.lua): a jack's parts where West Coast USA
-- has them (its pumpjacks group: the jack at -672.73, -250.08, its yaw 40.1 degrees clockwise), made per road tile with
-- every part drawn, gone with the tile, moved by a rebase; the nearest jacks have their two sounds.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local clock = 0
os.clockhp = function() return clock end
SFXSystem = { getPositionXYZ = function() return 0, 0, 0 end }
MatrixF = function()
  local m = {}
  function m:setFromEuler(v) self.yaw = v.z end
  function m:setPosition(p) self.pos = p end
  return m
end
scenetree.MissionGroup = S.newObject('SimGroup')
local newObject = S.newObject
S.newObject = function(class)
  local o = newObject(class)
  function o:setTransform(m) self.xf = m; self.pos = m.pos end
  function o:getId() return self.id end
  function o:play() self.playing = true end
  return o
end
createObject = function(class) return S.newObject(class) end
log = function() end

local pj = require('beamngEarth/pumpjacks')

-- West Coast USA's jack: base (-672.7323, -250.07933, 102.62606), parts b..e as its file has them
local th = math.rad(40.1)
local pos = pj.partPositions(-672.7323, -250.07933, 102.62606, th)
local want = { { -671.908813, -249.096283, 105.248169 }, { -671.852966, -249.038712, 108.72821 },
               { -674.149353, -251.754211, 110.302727 }, { -671.86322, -249.038132, 108.898041 } }
for i, w in ipairs(want) do
  local p = pos[i + 1]
  T.check(math.abs(p[1] - w[1]) < 0.02 and math.abs(p[2] - w[2]) < 0.02 and math.abs(p[3] - w[3]) < 0.01,
    string.format('part %d where West Coast USA has it: %.3f %.3f %.3f', i + 1, p[1], p[2], p[3]))
end

-- a tile's wells: five parts each, all drawn; the collision asked to rebuild
local collided = 0
pj.onCollide = function() collided = collided + 1 end
pj.onRows('1:0_0', { { 10, 20, 5, 90 }, { 400, 0, 5, 0 } })
local statics = S.liveOf('TSStatic')
T.check(#statics == 10, 'five parts a jack: ' .. #statics)
local hidden = 0
for _, o in ipairs(statics) do if o.fields.isRenderEnabled == 'false' then hidden = hidden + 1 end end
T.check(hidden == 0 and collided == 1, 'every part drawn (the base is the tower); one collision rebuild')

-- sounds: the jack within SOUND_M of the listener gets its engine and squeak, the far one none
pj.update()
local em = S.liveOf('SFXEmitter')
T.check(#em == 2 and em[1].playing, 'the near jack: its two sounds')
T.check(pj.status().sounding == 1 and pj.status().jacks == 2, 'one jack sounding of two')

-- a rebase moves the parts
pj.transform({ apply = function(_, x, y, z) return x + 100, y, z end })
local moved = false
for _, o in ipairs(S.liveOf('TSStatic')) do if o.pos and o.pos.x > 105 and o.pos.x < 115 then moved = true end end
T.check(moved, 'a rebase moves the parts')

pj.dropKey('1:0_0')
T.check(#S.liveOf('TSStatic') == 0 and #S.liveOf('SFXEmitter') == 0 and collided == 2, 'the tile goes: parts, sounds')
T.done('test_pumpjacks')
