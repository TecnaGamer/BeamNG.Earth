-- luajit mod/tests/test_keybinds.lua  (our default keys, settings/inputmaps/keyboard_beamngEarth.json)
-- None of them may be one the game binds already: Ctrl+Shift+I was the game's "toggle AI traffic" and a report
-- also set the traffic driving (the user, 2026-10-14). Checked against the installed game's keyboard maps when
-- they are found (BEAMNG_DIR, or the Steam default on Linux); the modifiers are compared in any order and spelling
-- ("shift-ctrl i" == "ctrl shift i").
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')

local function read(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local s = f:read('*a')
  f:close()
  return s
end

-- Pure: "shift-ctrl i" -> "ctrl+shift i" (modifiers sorted)
local function norm(control)
  local parts = {}
  for p in control:lower():gmatch('[^%s%-]+') do parts[#parts + 1] = p end
  local key = table.remove(parts)
  table.sort(parts)
  return table.concat(parts, '+') .. ' ' .. tostring(key)
end

local function controls(text)
  local out = {}
  for obj in text:gmatch('{[^{}]-}') do
    local c = obj:match('"control"%s*:%s*"([^"]*)"')
    local a = obj:match('"action"%s*:%s*"([^"]*)"')
    if c and a then out[#out + 1] = { norm(c), a } end
  end
  return out
end

T.check(norm('shift-ctrl i') == norm('ctrl shift i'), 'modifier order and spelling do not matter')
T.check(norm('ctrl shift i') ~= norm('ctrl i'), 'the modifiers count')

local ours = controls(assert(read(dir .. '/../beamng_earth/settings/inputmaps/keyboard_beamngEarth.json')))
T.check(#ours >= 2, 'our keyboard map has the report and profiler keys')

local game = os.getenv('BEAMNG_DIR') or ((os.getenv('HOME') or '') .. '/.local/share/Steam/steamapps/common/BeamNG.drive')
local stock = {}
local found = 0
for _, name in ipairs({ 'keyboard.json', 'keyboard_editor.json' }) do
  local s = read(game .. '/settings/inputmaps/' .. name)
  if s then
    found = found + 1
    for _, c in ipairs(controls(s)) do stock[c[1]] = (stock[c[1]] and stock[c[1]] .. ', ' or '') .. c[2] end
  end
end
if found == 0 then
  print('test_keybinds: the game was not found (BEAMNG_DIR): only our own map checked')
else
  T.check(stock[norm('shift-ctrl i')] ~= nil, 'the stock maps were read (Ctrl+Shift+I is the game\'s AI traffic toggle)')
  -- keys the game binds too, which cannot want them at the same time as ours: PageUp is the phone's (ours runs in
  -- the vehicle's tlua context, the game's "moveup" in the menus)
  local shared = { pageup = 'moveup' }
  for _, c in ipairs(ours) do
    local key = c[1]:match('(%S+)$')
    local game = stock[c[1]]
    local ok = game == nil or (shared[key] ~= nil and game == shared[key])
    T.check(ok, string.format('%s (%s) is free in the game (bound to: %s)', c[1], c[2], tostring(game)))
  end
end

T.done('test_keybinds')
