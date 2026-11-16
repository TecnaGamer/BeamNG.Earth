-- luajit mod/tests/test_mpleave.lua  (beammp.lua restore job + codeswap.lua, end to end): joining a BeamMP server turns
-- the player's own BeamNG.Earth off; leaving must turn it back on, whatever copy of the mod the server sent (the user,
-- 2026-11-15 on 1.10.1: "when i left the server it disabled the mod": a third-party server's 1.10.0 copy).
--
-- Stand-ins follow the real code paths (read 2026-11-15):
--  * core_modmanager (game lua/ge/extensions/core/modmanager.lua): activateMod mounts, runs the copy's modScripts,
--    hooks onModActivated, loadManualUnloadExtensions; deactivateMod unmounts, active=false, hooks onModDeactivated;
--    deleteMod unmounts and forgets the entry with NO hook and no extension unload (the session copy's Lua stays
--    loaded).
--  * BeamMP MPModManager: setServerMods ("multiplayer" .. name), loadServerMods (a job: workOffChangedMod 'added' per
--    zip, then checkAllMods: a mod not sent and active is deactivated), onModActivated -> checkMod in a session,
--    onBeamMPServerLeave -> cleanUpSessionMods (deleteMod of every /mods/multiplayer/ mod but multiplayerbeammp),
--    reloadLuaReloadWithDelay -> Lua reload 0.5 s later (the goBack leave).
--  * BeamMP MPCoreNetwork: setMods sets isMpSession/isGoingMpSession; leaveServer hooks onBeamMPServerLeave BEFORE it
--    clears them; onClientEndMission (not going into a session) -> leaveServer(false) (Esc > Main menu, the user's
--    log); leaveServer(true) -> endActiveGameMode(callback = reloadLuaReloadWithDelay).
--  * core_jobsystem: coroutines resumed each frame (job, args...), sleep(0) one frame.
--  * The copies' Lua: "new" = this tree's beammp.lua / codeswap.lua / modScript.lua and a core with core.lua's wiring
--    (checked against core.lua's source below); "1.10.0" = its restore code verbatim (restoreTick behind core
--    onUpdate's menu early return); "1.8" = no code swap, no restore at all.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local SRC = dir .. '/../beamng_earth/'

local function readFile(p)
  local f = assert(io.open(p, 'rb'))
  local s = f:read('*a')
  f:close()
  return s
end

-- core.lua's wiring, which the "new" core below copies: the restore armed before the code swap; no restoreTick left
do
  local core = readFile(SRC .. 'lua/ge/extensions/beamngEarth/core.lua')
  local body = core:match('M%.onModDeactivated = function%(mod%)(.-)\nend')
  T.check(body ~= nil, 'core.lua: onModDeactivated found')
  local a, b = body and body:find('beammp.onModDeactivated', 1, true), body and body:find('_codeswap.check', 1, true)
  T.check(a ~= nil and b ~= nil and a < b, 'core.lua: the restore armed before the code swap')
  T.check(core:find('beammp.onLoad', 1, true) == nil, 'core.lua: no arming at load (no provenance there)')
  T.check(core:find('beammp.restoreTick', 1, true) == nil, 'core.lua: no restoreTick behind the menu early return')
  T.check(core:find('pcall(beammp.onServerLeave)', 1, true) ~= nil, 'core.lua: the leave hook')
end

------------------------------------------------------------------------------------------
-- the copies' code

local NEW_CORE = [[
local beammp = require('beamngEarth/beammp')
local cs = require('beamngEarth/codeswap')
local M = { copyCode = 'new' }
local st = { inOurLevel = false, active = false }
pcall(cs.remember)
function M.onClientStartMission() st.inOurLevel = getCurrentLevelIdentifier() == 'beamng_earth'; st.active = st.inOurLevel end
function M.onClientEndMission() st.inOurLevel, st.active = false, false end
function M.onUpdate(dt) if not st.inOurLevel and not st.active then return end end
function M.onBeamMPServerLeave() pcall(beammp.onServerLeave) end
M.onModActivated = function(mod)
  pcall(cs.check, 'turned on: ' .. tostring(type(mod) == 'table' and mod.modname or '?'), true)
end
M.onModDeactivated = function(mod)
  pcall(beammp.onModDeactivated, mod)
  pcall(cs.check, 'turned off: ' .. tostring(type(mod) == 'table' and mod.modname or '?'), true)
end
return M
]]

-- 1.10.0 (v1.10.0-beta): core.lua's onUpdate returned for the menus before beammp.restoreTick; onModDeactivated only
-- the code swap
local OLD_CORE = [[
local beammp = require('beamngEarth/beammp')
local cs = require('beamngEarth/codeswap')
local M = { copyCode = '1.10.0' }
local st = { inOurLevel = false, active = false }
pcall(cs.remember)
function M.onClientStartMission() st.inOurLevel = getCurrentLevelIdentifier() == 'beamng_earth'; st.active = st.inOurLevel end
function M.onClientEndMission() st.inOurLevel, st.active = false, false end
function M.onUpdate(dtReal)
  if not st.inOurLevel and not st.active then return end
  pcall(beammp.restoreTick, dtReal)
end
function M.onBeamMPServerLeave() pcall(beammp.onServerLeave) end
M.onModActivated = function(mod) pcall(cs.check, 'turned on: ' .. tostring(mod.modname), true) end
M.onModDeactivated = function(mod) pcall(cs.check, 'turned off: ' .. tostring(mod.modname), true) end
return M
]]

-- 1.10.0's beammp.lua restore part, verbatim
local OLD_BEAMMP = [[
local M = {}
M.LOCAL_MOD = 'beamng_earth'
M.RESTORE_TRIES = 3
M.RESTORE_GAP_S = 1.0
local restoreDue, restoreTries, restoreWait = false, 0, 0
local function localEntry()
  local mm = rawget(_G, 'core_modmanager')
  if type(mm) ~= 'table' or type(mm.getModDB) ~= 'function' or type(mm.activateMod) ~= 'function' then return nil end
  local ok, entry = pcall(mm.getModDB, M.LOCAL_MOD)
  if not ok or type(entry) ~= 'table' then return nil end
  return entry, mm
end
function M.onServerLeave()
  local entry = localEntry()
  if not entry or entry.active then return false end
  restoreDue, restoreTries, restoreWait = true, 0, 0
  return M.restoreTick(0)
end
function M.restoreTick(dt)
  if not restoreDue then return false end
  restoreWait = restoreWait - (dt or 0)
  if restoreWait > 0 or M.inSession() then return false end
  local entry, mm = localEntry()
  if not entry or entry.active or restoreTries >= M.RESTORE_TRIES then
    restoreDue = false
    return false
  end
  restoreTries = restoreTries + 1
  restoreWait = M.RESTORE_GAP_S
  log('I', 'beamngEarth', 'BeamMP: left the server: BeamNG.Earth (' .. M.LOCAL_MOD .. ') switched back on')
  return (pcall(mm.activateMod, M.LOCAL_MOD))
end
function M.inSession()
  local mp = rawget(_G, 'MPCoreNetwork')
  if type(mp) ~= 'table' or type(mp.isMPSession) ~= 'function' then return false end
  local ok, v = pcall(mp.isMPSession)
  return ok and v == true
end
return M
]]

-- 1.8: no code swap, no restore
local VERYOLD_CORE = [[
local M = { copyCode = '1.8' }
function M.onUpdate() end
return M
]]
local VERYOLD_MODSCRIPT = "setExtensionUnloadMode('beamngEarth_core', 'manual')"

-- a copy: its code kind and its files (name -> size: what codeswap's fingerprint sees)
local function copy(code, sizes)
  return { code = code, sizes = sizes }
end
local function filesNew() return { ['core.lua'] = 4000, ['beammp.lua'] = 900, ['codeswap.lua'] = 300, ['busparked.lua'] = 50 } end
local function files110() return { ['core.lua'] = 3900, ['beammp.lua'] = 800, ['codeswap.lua'] = 300 } end
local function files18() return { ['core.lua'] = 3000, ['beammp.lua'] = 500 } end

-- (the files a fingerprint counts, and the code that differs between copies; any other module of ours: every copy)
local TRACKED = { ['core.lua'] = true, ['beammp.lua'] = true, ['codeswap.lua'] = true, ['busparked.lua'] = true }
-- the copy's file at a path (lua/ge/extensions/beamngEarth/x.lua, scripts/beamng_earth/modScript.lua) -> a chunk
local function chunkOf(c, rel)
  local name = rel:match('^lua/ge/extensions/beamngEarth/([%w_]+)%.lua$')
  if name and not TRACKED[name .. '.lua'] then
    return assert(load('return {}', '=' .. c.code .. ':' .. rel))      -- (another module of ours: an empty extension)
  end
  if rel == 'scripts/beamng_earth/modScript.lua' then
    if c.code == '1.8' then return assert(load(VERYOLD_MODSCRIPT, '=1.8:modScript')) end
    return assert(loadfile(SRC .. rel))                -- (the same modScript in 1.10.0 and now)
  end
  if name == 'core' then
    local s = c.code == 'new' and NEW_CORE or c.code == '1.10.0' and OLD_CORE or VERYOLD_CORE
    return assert(load(s, '=' .. c.code .. ':core'))
  end
  if name == 'beammp' then
    if c.code == 'new' then return assert(loadfile(SRC .. rel)) end
    if c.code == '1.10.0' then return assert(load(OLD_BEAMMP, '=1.10.0:beammp')) end
    return nil
  end
  if name == 'codeswap' then
    if c.code == '1.8' then return nil end
    return assert(loadfile(SRC .. rel))                -- (1.10.0's is this one but its comments)
  end
  return nil
end

------------------------------------------------------------------------------------------
-- the game

local G                     -- the game's Lua state (a Lua reload: a new one; the mods db and files stay)
local disk                  -- { mods = { name -> entry } }: the mods db (db.json) and the copies in the mods folders
local logs = {}

local function deepcopy(t)
  if type(t) ~= 'table' then return t end
  local o = {}
  for k, v in pairs(t) do o[k] = (k == 'copy') and v or deepcopy(v) end
  return o
end

local function winner(rel)           -- the first mounted copy that has the file (the player's copy mounted first)
  local name = rel:match('^lua/ge/extensions/beamngEarth/([%w_]+%.lua)$')
  for _, c in ipairs(G.mounted) do
    if name and (c.sizes[name] or not TRACKED[name]) then return c end
    if rel == 'scripts/beamng_earth/modScript.lua' then return c end
  end
  return nil
end

local function vfsChunk(path)
  local rel = path:gsub('^/', '')
  local c = winner(rel)
  if not c then return nil end
  return chunkOf(c, rel)
end

local function hook(name, ...)
  local list = {}
  for _, n in ipairs(G.order) do list[#list + 1] = n end
  for _, n in ipairs(list) do
    local e = G.loaded[n]
    if e and type(e[name]) == 'function' then e[name](...) end
  end
end

local function loadExt(n)
  if G.loaded[n] then return end
  local m
  if n:match('^beamngEarth_') then
    local okR, mod = pcall(require, 'beamngEarth/' .. n:sub(#'beamngEarth_' + 1))
    if not okR then logs[#logs + 1] = 'load failed ' .. n .. ': ' .. tostring(mod); return end
    m = mod
  else
    m = G.factories[n] and G.factories[n]()
  end
  if type(m) ~= 'table' then return end
  G.loaded[n] = m
  G.order[#G.order + 1] = n
  rawset(_G, n, m)
  if type(m.onExtensionLoaded) == 'function' then m.onExtensionLoaded() end
end

local function unloadExt(n)
  local m = G.loaded[n]
  if not m then return end
  if type(m.onExtensionUnloaded) == 'function' then pcall(m.onExtensionUnloaded) end
  G.loaded[n] = nil
  for i, x in ipairs(G.order) do if x == n then table.remove(G.order, i) break end end
  rawset(_G, n, nil)
end

local function mount(c) for _, x in ipairs(G.mounted) do if x == c then return end end; G.mounted[#G.mounted + 1] = c end
local function unmount(c) for i, x in ipairs(G.mounted) do if x == c then table.remove(G.mounted, i) return end end end

-- core_modmanager (the game's code paths)
local function newModManager()
  local mm = {}
  local function stateChanged() end        -- (db.json: disk.mods is the db itself)
  local function runModScripts(entry)
    local okC, ch = pcall(vfsChunk, '/scripts/beamng_earth/modScript.lua')
    -- (the mod manager runs the copy's own modScript by its virtual path: whichever copy's file wins)
    if okC and ch then
      local ok, err = pcall(ch)
      if not ok then logs[#logs + 1] = 'modScript failed: ' .. tostring(err) end
    end
  end
  function mm.getModDB(n) return disk.mods[n] end
  function mm.activateMod(n)
    local e = disk.mods[n]
    if not e then logs[#logs + 1] = 'mod not existing ' .. tostring(n); return end
    mount(e.copy)
    runModScripts(e)
    e.active = true
    hook('onModActivated', deepcopy(e))
    loadManualUnloadExtensions()
    stateChanged()
  end
  function mm.deactivateMod(n)
    local e = disk.mods[n]
    if not e then return end
    unmount(e.copy)
    e.active = false
    hook('onModDeactivated', deepcopy(e))
    stateChanged()
  end
  function mm.deleteMod(n)
    local e = disk.mods[n]
    if not e then return end
    unmount(e.copy)
    disk.mods[n] = nil          -- (no hook, no extension unloaded)
    stateChanged()
    return true
  end
  function mm.workOffChangedMod(path, kind)   -- /mods/multiplayer/x.zip 'added'
    local base = path:match('/mods/multiplayer/(.-)%.zip$')
    local n = 'multiplayer' .. base:lower()
    local e = disk.mods[n]
    if not e then
      e = { modname = n, dirname = '/mods/multiplayer/', copy = disk.multiplayer[base] }
      disk.mods[n] = e
    end
    if e.active ~= false then mm.activateMod(n) end
  end
  return mm
end

-- core_jobsystem
local function newJobSystem()
  local js = { jobs = {} }
  function js.create(fct, maxdt, ...)
    local job = { args = { ... } }
    job.sleep = function(secs)
      local t0 = G.clock
      repeat coroutine.yield() until G.clock - t0 >= (secs or 0)
    end
    job.yield = function() coroutine.yield() end
    job.t = coroutine.create(fct)
    js.jobs[#js.jobs + 1] = job
    return job
  end
  function js.onUpdate()
    for _, j in ipairs(js.jobs) do
      if coroutine.status(j.t) ~= 'dead' then
        local ok, err = coroutine.resume(j.t, j, unpack(j.args))
        if not ok then logs[#logs + 1] = 'job error: ' .. tostring(err) end
      end
    end
  end
  function js.getRunningJobCount()
    local n = 0
    for _, j in ipairs(js.jobs) do if coroutine.status(j.t) ~= 'dead' then n = n + 1 end end
    return n
  end
  return js
end

-- BeamMP's MPModManager
local function newMPModManager()
  local M = {}
  local serverMods, hasMods, requestLuaReload, reloadTimer = {}, false, false, 0
  local whitelist = { multiplayerbeammp = true, beammp = true, translations = true }
  local function isModAllowed(n) for _, v in ipairs(serverMods) do if v == n:lower() then return true end end return false end
  local function checkMod(mod)
    local n = mod.modname
    if whitelist[n:lower()] then return end
    local allowed = isModAllowed(n)
    if not allowed and mod.active then
      logs[#logs + 1] = 'This mod should not be running: ' .. n
      core_modmanager.deactivateMod(n)
      if mod.dirname == '/mods/multiplayer/' then core_modmanager.deleteMod(n) end
    elseif allowed and not mod.active then
      core_modmanager.activateMod(n)
    end
  end
  function M.checkAllMods()
    local names = {}
    for n in pairs(disk.mods) do names[#names + 1] = n end
    table.sort(names)
    for _, n in ipairs(names) do if disk.mods[n] then checkMod(disk.mods[n]) end end
  end
  function M.cleanUpSessionMods()
    local names = {}
    for n, e in pairs(disk.mods) do
      if e.dirname == '/mods/multiplayer/' and n ~= 'multiplayerbeammp' then names[#names + 1] = n end
    end
    for _, n in ipairs(names) do hasMods = true; core_modmanager.deleteMod(n) end
    logs[#logs + 1] = #names .. ' Mods cleaned up'
  end
  function M.setServerMods(s)
    serverMods = {}
    for mod in s:gmatch('([^;]+)') do serverMods[#serverMods + 1] = ('multiplayer' .. mod:gsub('%.zip;*$', '')):lower() end
  end
  function M.loadServerMods(zips, after)
    core_jobsystem.create(function(job)
      for _, z in ipairs(zips) do core_modmanager.workOffChangedMod('/mods/multiplayer/' .. z, 'added') end
      job.sleep(0)
      M.checkAllMods()
      after()
    end)
  end
  function M.onModActivated(mod) if MPCoreNetwork.isMPSession() then checkMod(mod) end end
  function M.onBeamMPServerLeave()
    if MPCoreNetwork.isMPSession() or MPCoreNetwork.isGoingMPSession() then
      serverMods = {}
      M.cleanUpSessionMods()
    end
  end
  function M.reloadLuaReloadWithDelay() if hasMods then requestLuaReload = true end end
  function M.onUpdate(dt)
    if requestLuaReload then
      reloadTimer = reloadTimer + dt
      if reloadTimer >= 0.5 then G.reloadRequested = true end
    end
  end
  function M.onExtensionLoaded() M.cleanUpSessionMods() end
  return M
end

-- BeamMP's MPCoreNetwork
local function newMPCoreNetwork()
  local M = {}
  local isMp, isGoing = false, false
  function M.isMPSession() return isMp end
  function M.isGoingMPSession() return isGoing end
  function M.leaveServer(goBack)
    hook('onBeamMPServerLeave')
    isMp, isGoing = false, false
    if goBack then
      G.endActiveGameMode(function() MPModManager.reloadLuaReloadWithDelay() end)
    end
  end
  -- the join: the server's mods list, the launcher's download, loadServerMods, the map, runPostJoin
  function M.join(server)
    isMp, isGoing = true, true
    local names = {}
    for z in pairs(server.mods) do names[#names + 1] = z end
    table.sort(names)
    MPModManager.setServerMods(table.concat(names, ';'))
    for z, c in pairs(server.mods) do disk.multiplayer[z:gsub('%.zip$', '')] = c end
    MPModManager.loadServerMods(names, function() G.pendingLevel = server.level end)
  end
  function M.onClientStartMission()
    if isMp and isGoing then
      hook('onBeamMPPostJoin')
      isGoing = false
    end
  end
  function M.onClientEndMission() if not isGoing then M.leaveServer(false) end end
  return M
end

local boots = 0
local function boot()
  boots = boots + 1
  -- a fresh Lua state: the startup extensions, then every active mod (mounted, its modScript), the manual extensions
  package.loaded['beamngEarth/beammp'], package.loaded['beamngEarth/codeswap'], package.loaded['beamngEarth/core'] = nil
  for k in pairs(package.loaded) do if type(k) == 'string' and k:match('^beamngEarth') then package.loaded[k] = nil end end
  rawset(_G, 'beamngEarthCodeFingerprint', nil)
  rawset(_G, 'beamngEarthMPRestore', nil)
  for _, n in ipairs(G and G.order or {}) do rawset(_G, n, nil) end     -- (the old state's extension globals)
  G = { loaded = {}, order = {}, mounted = {}, manual = {}, clock = 0, level = nil, factories = {} }
  _G.core_jobsystem = newJobSystem()
  _G.core_modmanager = newModManager()
  _G.MPCoreNetwork = newMPCoreNetwork()
  _G.MPModManager = newMPModManager()
  G.factories.core_jobsystem = function() return core_jobsystem end
  G.factories.MPCoreNetwork = function() return MPCoreNetwork end
  G.factories.MPModManager = function() return MPModManager end
  G.manual = { 'core_jobsystem', 'MPCoreNetwork', 'MPModManager' }
  function G.endActiveGameMode(cb)
    G.endLevel()
    for _, n in ipairs({ unpack(G.order) }) do
      local keep = false
      for _, m in ipairs(G.manual) do if m == n then keep = true end end
      if not keep then unloadExt(n) end
    end
    cb()
  end
  function G.endLevel()
    if not G.level then return end
    G.level = nil
    hook('onClientEndMission')
  end
  loadExt('core_jobsystem')
  loadExt('MPCoreNetwork')
  local names = {}
  for n, e in pairs(disk.mods) do if e.active then names[#names + 1] = n end end
  table.sort(names)
  for _, n in ipairs(names) do
    mount(disk.mods[n].copy)
    local ch = vfsChunk('/scripts/beamng_earth/modScript.lua')
    if ch then pcall(ch) end
  end
  loadManualUnloadExtensions()
  loadExt('MPModManager')
end

-- the game's globals
_G.log = function(level, tag, msg) logs[#logs + 1] = tostring(msg) end
_G.dumps = tostring
_G.deepcopy = deepcopy
_G.guihooks = { trigger = function() end }
_G.getCurrentLevelIdentifier = function() return G.level end
_G.hptimer = function()
  local t0 = G.clock
  return { stop = function() return (G.clock - t0) * 1000 end }
end
_G.setExtensionUnloadMode = function(n, mode)
  if mode ~= 'manual' then return end
  for _, x in ipairs(G.manual) do if x == n then return end end
  G.manual[#G.manual + 1] = n
end
_G.loadManualUnloadExtensions = function() for _, n in ipairs({ unpack(G.manual) }) do loadExt(n) end end
_G.extensions = {
  getLoadedExtensionsNames = function() return { unpack(G.order) } end,
  unload = unloadExt,
  load = function(names) for _, n in ipairs(type(names) == 'table' and names or { names }) do loadExt(n) end end,
}
_G.FS = {
  findFiles = function(_, d)
    local out, seen = {}, {}
    for _, c in ipairs(G.mounted) do
      for f in pairs(c.sizes) do
        if not seen[f] then seen[f] = true; out[#out + 1] = d .. f end
      end
    end
    return out
  end,
  fileSize = function(_, p)
    local c = winner(p:gsub('^/', ''))
    return c and c.sizes[p:match('([^/]+)$')] or -1
  end,
  fileExists = function() return false end,
}
-- dofile of a virtual path (the modScript's codeswap.lua): the mounted copy's file
local realDofile = dofile
_G.dofile = function(p)
  if type(p) == 'string' and p:sub(1, 1) == '/' then
    local ch = vfsChunk(p)
    if not ch then error('cannot open ' .. p) end
    return ch()
  end
  return realDofile(p)
end
-- require of our modules: the mounted copy's file (the game searches the mounted mods)
table.insert(package.loaders, 2, function(name)
  local rel = 'lua/ge/extensions/' .. name:gsub('%.', '/') .. '.lua'
  if not name:match('^beamngEarth[/.]') then return nil end
  local okC, ch = pcall(vfsChunk, '/' .. rel)
  if okC and ch then return ch end
  return function() error('no mounted copy has ' .. rel) end     -- (never this tree's other files)
end)

local DT = 1 / 60
local function frames(n, dt)
  for _ = 1, n do
    G.clock = G.clock + (dt or DT)
    hook('onUpdate', dt or DT)
    if G.pendingLevel then
      G.level, G.pendingLevel = G.pendingLevel, nil
      hook('onClientStartMission')
    end
    if G.reloadRequested then boot() end         -- (Lua:requestReload: a new state)
  end
end

local function coreCode() local c = rawget(_G, 'beamngEarth_core'); return c and c.copyCode end
local function ours() return disk.mods.beamng_earth end

-- a fresh game with the player's own copy (playerCode) installed and on, in the main menu
local function start(playerCode, playerFiles)
  logs = {}
  disk = { mods = {}, multiplayer = {} }
  disk.mods.beamng_earth = { modname = 'beamng_earth', dirname = '/mods/unpacked/', active = true,
    copy = copy(playerCode, playerFiles) }
  boot()
  frames(2)
end

local function hasLog(s)
  for _, l in ipairs(logs) do if l:find(s, 1, true) then return true end end
  return false
end

-- (1) the user's case on 1.10.0's own code: a server's 1.10.0 copy, Esc > Main menu: left off (the bug, reproduced)
start('1.10.0', files110())
MPCoreNetwork.join({ mods = { ['beamng_earth.zip'] = copy('1.10.0', files110()) }, level = 'beamng_earth' })
frames(10)
T.check(G.level == 'beamng_earth' and ours().active == false, '1.10.0 player: in the session, ours off')
G.endLevel()                         -- (Esc > Main menu: the level ends, MPCoreNetwork.onClientEndMission leaves)
frames(300)
T.check(hasLog('1 Mods cleaned up'), '1.10.0 player: the session copy deleted')
T.check(ours().active == false, '1.10.0 code: left off after the leave (the user\'s report, reproduced)')

-- (2) the same with the fixed player copy: the job switches it on within a few frames, its code back in
start('new', filesNew())
T.check(coreCode() == 'new', 'new player: its code runs')
MPCoreNetwork.join({ mods = { ['beamng_earth.zip'] = copy('1.10.0', files110()) }, level = 'beamng_earth' })
frames(10)
T.check(G.level == 'beamng_earth' and ours().active == false and coreCode() == '1.10.0',
  'old server copy: ours off, the server copy\'s code swapped in: ' .. tostring(coreCode()))
T.check(rawget(_G, 'beamngEarthMPRestore') and rawget(_G, 'beamngEarthMPRestore').live,
  'old server copy: the restore armed by our code before the swap')
frames(600)
T.check(ours().active == false, 'old server copy: still off while in the session')
G.endLevel()
T.check(disk.mods.multiplayerbeamng_earth == nil, 'left: the server copy deleted')
frames(3)
T.check(ours().active == true, 'old server copy, Esc > Main menu: ours on again within a few frames')
T.check(coreCode() == 'new', 'old server copy: the player\'s own code back: ' .. tostring(coreCode()))
frames(120)
T.check(ours().active == true and not rawget(_G, 'beamngEarthMPRestore').live, 'still on; the job ended')

-- (3) an old server copy, BeamMP's "Leave" (goBack: the level ends, then a Lua reload 0.5 s later)
start('new', filesNew())
MPCoreNetwork.join({ mods = { ['beamng_earth.zip'] = copy('1.10.0', files110()) }, level = 'beamng_earth' })
frames(30)
MPCoreNetwork.leaveServer(true)
frames(3)
T.check(ours().active == true, 'old server copy, Leave: on before BeamMP\'s Lua reload')
local b0 = boots
frames(60)
T.check(boots == b0 + 1, 'Leave: BeamMP reloaded the Lua')
T.check(ours().active == true and coreCode() == 'new', 'after the Lua reload: on, our code: ' .. tostring(coreCode()))
-- ... a 2 s hitch right after the Leave (BeamMP's 0.5 s delay passed in one frame): still on first
start('new', filesNew())
MPCoreNetwork.join({ mods = { ['beamng_earth.zip'] = copy('1.10.0', files110()) }, level = 'beamng_earth' })
frames(30)
MPCoreNetwork.leaveServer(true)
b0 = boots
frames(1, 2.0)
T.check(boots == b0 + 1 and ours().active == true, 'Leave + a 2 s frame: on before the Lua reload')

-- (4) the server sends our own (fixed) copy: no swap; on again after the leave
start('new', filesNew())
MPCoreNetwork.join({ mods = { ['beamng_earth.zip'] = copy('new', filesNew()) }, level = 'beamng_earth' })
frames(10)
T.check(ours().active == false and coreCode() == 'new' and not hasLog('another copy of BeamNG.Earth'),
  'own copy as the session copy: no code swap: ' .. tostring(ours().active) .. ' ' .. tostring(coreCode()) .. ' '
  .. tostring(hasLog('another copy of BeamNG.Earth')))
G.endLevel()
frames(3)
T.check(ours().active == true and coreCode() == 'new', 'own copy as the session copy: on again')

-- (5) a stock-map server (no BeamNG.Earth sent): ours off, no copy mounted (no swap), on again after the leave
start('new', filesNew())
MPCoreNetwork.join({ mods = {}, level = 'west_coast_usa' })
frames(10)
T.check(G.level == 'west_coast_usa' and ours().active == false and coreCode() == 'new', 'stock-map server: ours off')
G.endLevel()
frames(3)
T.check(ours().active == true, 'stock-map server: on again')

-- (6) a 1.8 server copy (no code swap, no restore code of its own): the job alone does it
start('new', filesNew())
MPCoreNetwork.join({ mods = { ['beamng_earth.zip'] = copy('1.8', files18()) }, level = 'beamng_earth' })
frames(10)
T.check(coreCode() == '1.8', '1.8 server copy: its code in the session: ' .. tostring(coreCode()))
MPCoreNetwork.leaveServer(true)
frames(60)
T.check(ours().active == true and coreCode() == 'new', '1.8 server copy: on again, our code: ' .. tostring(coreCode()))

-- (7) a 1.10.0 player on a fixed server's copy: the fixed code came in after BeamMP turned ours off and can't tell that
-- from the player's own choice, so it stays off in this run (the launcher's next start turns it on:
-- install.ensure_mod_active); updating the player's own copy is what fixes it
start('1.10.0', files110())
MPCoreNetwork.join({ mods = { ['beamng_earth.zip'] = copy('new', filesNew()) }, level = 'beamng_earth' })
frames(10)
T.check(coreCode() == 'new' and ours().active == false, '1.10.0 player, fixed server copy: its code in the session')
G.endLevel()
frames(120)
T.check(ours().active == false, '1.10.0 player, fixed server copy, Esc: left off (a known limit)')

-- (8) the player turns the mod off outside a session: it stays off
start('new', filesNew())
core_modmanager.deactivateMod('beamng_earth')
frames(120)
T.check(ours().active == false and rawget(_G, 'beamngEarthMPRestore') == nil, 'turned off by the player: stays off')
-- ... also across a level's end (MPCoreNetwork.onClientEndMission -> leaveServer(false) in single player too)
G.level = 'west_coast_usa'
hook('onClientStartMission')
frames(10)
G.endLevel()
frames(120)
T.check(ours().active == false, 'turned off by the player, then a level ended: stays off')

-- (8b) turned off by the player before joining (and the game restarted: none of our code runs), then a server that
-- sends a copy (new or old) or none: BeamMP never turned ours off, so it stays off after the leave (Codex review)
for _, sent in ipairs({ { ['beamng_earth.zip'] = copy('new', filesNew()) },
    { ['beamng_earth.zip'] = copy('1.10.0', files110()) }, {} }) do
  for _, goBack in ipairs({ false, true }) do
    start('new', filesNew())
    core_modmanager.deactivateMod('beamng_earth')
    boot()                               -- (a restart: the mod off, nothing of ours loaded)
    T.check(coreCode() == nil, 'player-off, restarted: none of our code')
    MPCoreNetwork.join({ mods = sent, level = next(sent) and 'beamng_earth' or 'west_coast_usa' })
    frames(10)
    T.check(ours().active == false, 'player-off: off in the session')
    if goBack then MPCoreNetwork.leaveServer(true) else G.endLevel() end
    frames(120)
    -- (a server's 1.10.0 copy's own leave hook switches it on after a Leave: BeamMP's second leaveServer from
    -- onClientEndMission, after the session flags are cleared; that copy's code can't be changed)
    local oldOwnRestore = goBack and next(sent) and sent['beamng_earth.zip'].code == '1.10.0'
    T.check(oldOwnRestore or (ours().active == false and rawget(_G, 'beamngEarthMPRestore') == nil),
      'player-off before the join: still off after the leave (' .. (next(sent) and sent['beamng_earth.zip'].code or
      'no copy') .. ', ' .. (goBack and 'Leave' or 'Esc') .. ')')
  end
end
-- ... and turned off in this run (our code still loaded), then a server's new copy: still off after the leave
start('new', filesNew())
core_modmanager.deactivateMod('beamng_earth')
MPCoreNetwork.join({ mods = { ['beamng_earth.zip'] = copy('new', filesNew()) }, level = 'beamng_earth' })
frames(10)
G.endLevel()
frames(120)
T.check(ours().active == false, 'player-off in this run, new server copy: still off after the leave')

-- (8c) turned off by the player right after the restore (within RESTORE_GAP_S): stays off (Codex review round 2)
start('new', filesNew())
MPCoreNetwork.join({ mods = {}, level = 'west_coast_usa' })
frames(10)
G.endLevel()
frames(3)
T.check(ours().active == true, 'stock-map server left: on')
core_modmanager.deactivateMod('beamng_earth')
frames(120)
T.check(ours().active == false and not rawget(_G, 'beamngEarthMPRestore').live,
  'turned off by the player just after the restore: stays off, the job ended')

-- (9) leaving and joining another server: off again in the new session, on after it ends
start('new', filesNew())
MPCoreNetwork.join({ mods = { ['beamng_earth.zip'] = copy('1.10.0', files110()) }, level = 'beamng_earth' })
frames(10)
G.endLevel()
frames(3)
T.check(ours().active == true, 'first server left: on')
MPCoreNetwork.join({ mods = {}, level = 'west_coast_usa' })
frames(10)
T.check(ours().active == false, 'second server: off again')
G.endLevel()
frames(3)
T.check(ours().active == true, 'second server left: on')

for _, l in ipairs(logs) do if l:find('job error', 1, true) or l:find('load failed', 1, true) then print(l) end end
T.done('test_mpleave')
