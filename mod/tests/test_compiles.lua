-- luajit mod/tests/test_compiles.lua  (every mod file must compile in the game's LuaJIT: core.lua sits at the
-- 200-locals limit, and the game then loads no extension at all: "main function has more than 200 local variables")
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local dir = (arg[0]:match('^(.*)[/\\]') or '.') .. '/../beamng_earth/lua/ge/extensions/beamngEarth/'
local p = io.popen('ls "' .. dir .. '"')
local bad = 0
for name in p:lines() do
  if name:match('%.lua$') then
    local f, err = loadfile(dir .. name)
    if not f then bad = bad + 1; print(err) end
  end
end
p:close()
T.check(bad == 0, 'every extension file compiles')
T.done('test_compiles')
