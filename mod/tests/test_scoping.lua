-- luajit mod/tests/test_scoping.lua  (every mod Lua file): no global read of a name the same file declares as a local.
-- A function that uses a local declared further down reads the (nil) global of that name instead: the BeamMP session's
-- test switch errored that way (remoteOwners) and roads.lua's prewarm after a level reload never ran (2026-10-22).
-- Reads LuaJIT bytecode in-process: no external luajit executable needed by Lupa.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local root = dir .. '/../beamng_earth/lua'

local files = rawget(_G, 'BNE_TEST_LUA_FILES') or {}
if #files == 0 then
  local windows = package.config:sub(1, 1) == '\\'
  local command = windows and ('dir /s /b "' .. root:gsub('/', '\\') .. '\\*.lua"')
    or ('find "' .. root .. '" -name "*.lua"')
  local p = assert(io.popen(command))
  for f in p:lines() do files[#files + 1] = f:gsub('\\', '/') end
  p:close()
end
T.check(#files > 20, 'found the mod\'s Lua files: ' .. #files)

local util, bit = require('jit.util'), require('bit')
-- Discover GGET from a minimal chunk so the test need not hardcode an opcode.
local probe = assert(loadstring('return __beamng_scope_probe'))
local globalRead = bit.band(util.funcbc(probe, 1), 255)
local function globals(fn, out)
  for pc = 1, util.funcinfo(fn).bytecodes - 1 do
    local instruction = util.funcbc(fn, pc)
    if bit.band(instruction, 255) == globalRead then
      out[util.funck(fn, -1 - bit.rshift(instruction, 16))] = true
    end
  end
  for i = 1, util.funcinfo(fn).gcconsts do
    local value = util.funck(fn, -i)
    if type(value) == 'proto' then globals(value, out) end
  end
end
local selftest = {}
globals(assert(loadstring('local later; return function() return missing, later end')), selftest)
T.check(selftest.missing and not selftest.later, 'bytecode inspection detects nested globals, not captured locals')

local bad = {}
for _, f in ipairs(files) do
  local sourceFile = assert(io.open(f))
  local src = sourceFile:read('*a')
  sourceFile:close()
  local seen = {}
  globals(assert(loadfile(f)), seen)
  for g in pairs(seen) do
      if src:find('\n%s*local%s+' .. g .. '%f[^%w_]') or src:find('\n%s*local%s+function%s+' .. g .. '%f[^%w_]')
          or src:find('\n%s*local%s+[%w_,%s]-,%s*' .. g .. '%f[^%w_]') then
        bad[#bad + 1] = f:match('lua/(.*)$') .. ': ' .. g
      end
  end
end
T.check(#bad == 0, 'no global reads of a file\'s own locals: ' .. table.concat(bad, ', '))
T.done('test_scoping')
