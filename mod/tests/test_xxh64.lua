-- luajit mod/tests/test_xxh64.lua  (xxh64.lua: the game's terrain texture cache names, as server/terraincache.xxh64)
-- Known values from the server's Python XXH64: the empty string, a short one, one with 32-byte stripes and a tail,
-- and a cache key's input (a block photo's path three times and the layers' ao map's).
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local x = require('beamngEarth/xxh64')
local P = '/temp/beamngEarth/20261101-113809-6fe4/hm_2_-384_0_base.png'
for _, c in ipairs({
  { '', 'ef46db3751d8e999' },
  { 'abc', '44bc2cf5ad770999' },
  { '0123456789abcdef0123456789abcdef01234', '3f889232c2743804' },
  { P .. P .. P .. '/levels/beamng_earth/art/terrains/bngEarth_base_ao.png', '2ae804384bf1272d' },
}) do
  T.check(x.hex(c[1]) == c[2], 'xxh64 of ' .. #c[1] .. ' bytes: ' .. x.hex(c[1]) .. ' (want ' .. c[2] .. ')')
end
T.done('test_xxh64')
