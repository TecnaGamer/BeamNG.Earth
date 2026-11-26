-- luajit mod/tests/test_winterroad.lua  (winter W3's road materials, winterroad.lua): the winter copies of the paint,
-- deck asphalt, sidewalks and banks made once a level, set from the snowstate's look, only changed values written,
-- nothing loaded while winter is off or the state has no look.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')

local W = require('beamngEarth/winter')
W.onState({ type = 'snowstate', roads = { { 'dry', 0, 5, 'dry', 0, 0, 0 } }, bridges = {}, ground = { 0, 0, 0, 0, 0, 0, 0 } })
T.check(package.loaded['beamngEarth/winterroad'] == nil, 'a snowstate without a look: winterroad never loads')

local R = require('beamngEarth/winterroad')

-- the materials: every class's paint, deck and bank, every sidewalk slot; names as the server's snowroad.py writes them
do
  local mats = R.materials()
  local n = 0
  for name, m in pairs(mats) do
    n = n + 1
    T.check(m.name == name and m.mapTo == name and m.class == 'Material', name .. ': named and mapped to itself')
  end
  T.check(n == 5 * 3 + 5 + 5 + 4, 'materials: 15 paint, 5 decks, 5 banks, 4 sidewalk slots (' .. n .. ')')
  T.check(mats.bne_snow_line_white_p3 ~= nil and mats.bne_snow_crossing_white_p0 ~= nil, 'paint copies')
  T.check(mats.bne_snow_deck_p1.activeLayers == 2 and mats.bne_snow_deck_p1.Stages[2].opacityFactor == 0,
    'a deck: two layers, the snow one off until the look says')
  T.check(mats.bne_snow_walk_s1u.groundType == 'CONCRETE', 'a sidewalk keeps its ground type (collision)')
  T.check(mats.bne_snowbank_p0.groundType == 'SNOW', 'a bank is snow')
end

-- the targets of a look
local look = { marking = { 1, 1, 0.8, 0.3, 0 }, deck = { 0, 0, 0.5, 1, 1 }, banks = { 1, 1, 1, 1, 0 },
  dirt = { 2, 1, 0, 0, 0 }, walks = { { 2, 1 }, { 2, 1 }, { 1, 7 }, { 0, 8 } } }
do
  local t = R.targets(look)
  local by = {}
  for _, w in ipairs(t) do by[w[1] .. '|' .. w[2] .. '|' .. w[3]] = w[4] end
  T.check(by['bne_snow_line_white_p0|opacityFactor|0'] == '0.700', 'a bare main road: the stock paint opacity')
  T.check(by['bne_snow_line_white_p4|opacityFactor|0'] == '0.000', 'a buried service road: no paint')
  T.check(by['bne_snow_line_white_p3|opacityFactor|0'] == string.format('%.3f', 0.7 * 0.3), 'tracks: some paint')
  T.check(by['bne_snow_crossing_white_p2|opacityFactor|0'] == '0.800', 'crossings fade the same way')
  T.check(by['bne_snow_deck_p3|opacityFactor|1'] == '1.000', 'a white deck')
  T.check(by['bne_snowbank_p0|baseColorFactor|0'] == string.format('%.3f %.3f %.3f %.3f', unpack(R.DIRT[3])),
    'a dirty bank beside the salted motorway')
  T.check(by['bne_snow_walk_s0|opacityFactor|1'] == string.format('%.3f', math.floor(1 / 8 / R.STEP + 0.5) * R.STEP),
    'a shovelled main street sidewalk: a trace of snow')
  T.check(by['bne_snow_walk_s2|opacityFactor|1'] == '1.000', 'an unmaintained one: covered')
  T.check(#R.targets(nil) == 0, 'no look: nothing')
end

-- apply: materials loaded once, values written once, then only changes
do
  local made = 0
  local mats = {}
  local stOld = scenetree.findObject
  scenetree.findObject = function(name)
    if mats[name] then return mats[name] end
    return stOld(name)
  end
  _G.readFile = function() return nil end
  _G.writeFile = function() return true end
  _G.jsonEncode = function() return '{}' end
  FS.directoryCreate = function() end
  _G.loadJsonMaterialsFile = function()
    made = made + 1
    for name in pairs(R.materials()) do
      local m = { name = name, fields = {}, flushes = 0 }
      function m:setField(f, layer, v) self.fields[f .. '|' .. layer] = v end
      function m:flush() self.flushes = self.flushes + 1 end
      function m:reload() end
      mats[name] = m
    end
  end
  local n = W.onState({ type = 'snowstate', roads = { { 'packed', 30, -5, 'loose', 50, 0, 0.8 } }, bridges = {},
    ground = { 120, 3, 8, 0, 0, 12, 30 }, look = look })
  T.check(made == 1, 'the materials made with the first look')
  T.check(package.loaded['beamngEarth/winterroad'] ~= nil, 'winterroad loaded with it')
  T.check(mats.bne_snow_line_white_p4.fields['opacityFactor|0'] == '0.000', 'the paint set')
  local before = R.writes
  T.check(R.apply(look) == 0 and R.writes == before, 'the same look again: no writes')
  local look2 = {}
  for k, v in pairs(look) do look2[k] = v end
  look2.marking = { 1, 1, 0.8, 0.3, 0.5 }
  T.check(R.apply(look2) == 3, 'one class\'s paint changed: its three paint materials written')
  T.check(made == 1, 'not made again while they exist')
  mats = {}
  R.apply(look2)
  T.check(made == 2, 'a level load dropped them: made again')
  W.reset()
  T.check(R._written() and next(R._written()) == nil, 'reset: nothing remembered')
  scenetree.findObject = stOld
end

T.done('test_winterroad')
