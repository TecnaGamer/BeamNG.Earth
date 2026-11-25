-- Run with: luatex --luaonly norway_foliage_v07/tests/lua_backdrop_test.lua
-- The lightweight stubs let us test the real BeamNG Lua module off-line.
package.preload['beamngEarth/vienna_convention_speed_signs'] = function()
  return { install = function() end }
end
package.preload['beamngEarth/geotile'] = function() return {} end
package.preload['beamngEarth/townsigns'] = function() return {} end
for _, name in ipairs({ 'frFenceAssets', 'frRadarAssets', 'europeAssets',
    'europeRoadAssets', 'europeStopAssets' }) do
  package.preload['beamngEarth/' .. name] = function() return {} end
end
package.preload['beamngEarth/stopwatch'] = function()
  return { new = function() return function() return 0 end end }
end

local source = 'norway_foliage_v07/mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua'
local M = assert(loadfile(source))()

local function tile(groups)
  local rows = {}
  for i = 1, 200 do
    local g = groups and (i <= 100 and 0 or 1) or 0
    local rank = i / 201
    for _, v in ipairs({ g, i, 0, 0, 0, 1, 20, rank }) do rows[#rows + 1] = v end
  end
  return { f = rows, s = 8, n = 200,
    groups = groups or { 'norway_conifer_backdrop', 'other' } }
end

local t = tile()
assert(M.norwayBackdropThreshold(200, t, 1.0) == 1.0, 'near full-detail forest must not change')
local distant = M.norwayBackdropThreshold(1000, t, 0.35)
assert(distant > 0.35 and distant <= 0.86, 'dense farther forest threshold invalid')
local bonus = 0
for i = 1, t.n do
  local rank = t.f[(i - 1) * t.s + 8]
  if rank >= 0.35 and rank < distant then bonus = bonus + 1 end
end
assert(bonus <= 104, 'distant bonus exceeded its strict per-tile cap')
assert(bonus > 40, 'distant bonus had no material effect')

local middle = M.norwayBackdropThreshold(500, tile(), 0.60)
assert(middle > 0.60 and middle <= 0.90, 'middle-distance forest threshold invalid')

M.quality = 'low'
local low = M.norwayBackdropThreshold(1000, tile(), 0.175)
local nlow = 0
for i = 1, t.n do
  local r = t.f[(i - 1) * t.s + 8]
  if r >= 0.175 and r < low then nlow = nlow + 1 end
end
assert(nlow <= 52, 'Low graphics quality bonus exceeded scaled cap')
assert(low <= 0.43, 'Low graphics quality did not scale density down')

M.quality = 'normal'
local other = tile({ 'pine_west', 'deciduous' })
assert(M.norwayBackdropThreshold(1000, other, 0.35) == 0.35, 'non-Norway groups changed')
assert(M.NO_COLLIDE.norway_conifer_backdrop and M.NO_COLLIDE.norway_deciduous_backdrop,
  'backdrop collision enabled')
M.NORWAY_DENSE_FOREST = false
assert(M.norwayBackdropThreshold(1000, t, 0.35) == 0.35, 'disable switch not working')
print('v0.7 Norway-only distance foliage test passed')
