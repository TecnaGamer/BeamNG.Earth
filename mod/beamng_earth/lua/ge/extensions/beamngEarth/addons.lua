-- The Add-ons page's game side (the settings page's "Add-ons" tab: EarthAddons.vue through settings.lua's uiAddon*
-- functions; the server's addons.py). Third-party mods and tools from the curated list, docs/ADDONS.md.
--
-- The server lists the add-ons (addons_list -> addons), sends one add-on's README and changelog as safe HTML
-- (addon_detail), and downloads a release (addon_install -> addon_status ... addon_apply): the file is checked against
-- GitHub's SHA-256 and left in the mods folder as <modFile>.bnge-tmp, a name the game's mod manager ignores. The
-- hand-over is ours, through the game's own mod manager (replacing mod files under the running game broke its UI
-- apps, 2026-10-24; the mod manager does it the way its own downloads do):
--   - a new add-on: the file renamed to its name (FS:renameFile); the mod manager's file watcher mounts and turns it on
--     (core_modmanager.onFileChanged -> workOffChangedMod); if it has not after MOUNT_CHECK_S, we ask it
--     (workOffChangedMod ourselves). Older copies under the names the list gives (oldFiles) are deleted through it.
--   - an update: core_modmanager.updateZipMod(old, new), the call the game's own download command uses ("crepe.zip",
--     "crepe.zip.tmp", core/commandhandler.lua): the old file removed and unmounted, the new one renamed into its place
--     and mounted. A mod that was off is turned off again.
--   - the code applies at once (the user, 2026-11-07: "make sure the game gets properly reloaded so it applies"): the
--     game's mod manager runs a mod's modScripts when it mounts or turns it on, but an extension already loaded stays
--     loaded (extensions.load of a loaded one does nothing, and turning off or removing unloads nothing). So before an
--     update / turning off / removing, the mod's own extensions are unloaded and its required modules forgotten (the
--     server lists them from the zip: addonrun.mod_code; BeamNG.Earth's own never), and when it has UI files the UI is
--     reloaded after (RELOAD_UI_S), so its apps come and go. The game's own files a mod replaces need a restart (said).
--   - a program beside the game (kind companion): Install / Start / Stop / Remove / autostart are the server's
--     (addon_install, addon_run): nothing of the game's changes.
--   - enable / disable / uninstall: activateMod / deactivateMod / deleteMod, as the game's own mod manager page does.
-- Each waits for core_modmanager.isReady() (APPLY_WAIT_S at most) and never runs in a BeamMP session (BeamMP turns mods
-- off and on itself). The player agrees once per add-on to a notice naming its author (consent, CONSENT_FILE); the
-- server refuses an install without it too. Every step is sent back (addon_applied) for the server's log.
local net = require('beamngEarth/net')

local M = {}

M.CONSENT_FILE = '/settings/beamngEarth_addons.json'   -- (beside beamngEarth_places.json: no folder to make)
M.APPLY_WAIT_S = 60          -- the mod manager busy (a level loading) this long: the hand-over fails
M.MOUNT_CHECK_S = 3          -- after the rename the file watcher has this long to mount the file, then we ask
M.FILE = '^[%w][%w%._%-]*%.zip$'
M.TMP_SUFFIX = '.bnge-tmp'
M.RELOAD_UI_S = 1.5          -- after a hand-over that changed a mod with UI files: the UI reloaded this much later

local list, listError, listAsked = nil, nil, false
local details = {}            -- id -> the server's addon_detail (with serial)
local status = {}             -- id -> { state, text, done, total }
local queue = {}              -- hand-overs waiting for the mod manager: { op, id, file, tmp, old, active, tag, since }
local checks = {}             -- after a rename: { name, file, at, active, id }
local consent = nil           -- id -> the author the player agreed to
local clock, serial = 0, 0
local reloadAt = nil          -- clock at which the UI is reloaded (a mod with UI files changed)
local adding = nil            -- Add from GitHub: { state = 'progress' | 'done' | 'error', text, id }
local optKnown = {}           -- id -> the option values the mods last saw (the addonOptions event)

-- (fields: the tests replace them)
M.modmanager = function() return rawget(_G, 'core_modmanager') or (rawget(_G, 'extensions') and extensions.core_modmanager) end
M.fs = function() return rawget(_G, 'FS') end
M.extensions = function() return rawget(_G, 'extensions') end
M.reloadUI = function() local f = rawget(_G, 'reloadUI'); if type(f) == 'function' then f() end end
M.inMp = function()
  local mp = rawget(_G, 'MPCoreNetwork')
  if type(mp) ~= 'table' or type(mp.isMPSession) ~= 'function' then return false end
  local ok, v = pcall(mp.isMPSession)
  return ok and v == true
end

local function logI(text) if log then log('I', 'beamngEarth', 'add-ons: ' .. text) end end

-- The game's mod name for a zip in mods/ (core_modmanager getModNameFromPath: lower case, without .zip).
function M.modName(file) return (tostring(file):lower():gsub('%.zip$', '')) end

local function entry(id)
  for _, a in ipairs(list or {}) do if a.id == id then return a end end
end

local function setStatus(id, state, text, done, total)
  status[id] = { state = state, text = text or '', done = done or 0, total = total or 0 }
  serial = serial + 1
end

local function loadConsent()
  if consent then return consent end
  consent = {}
  local ok, data = pcall(function() return jsonReadFile and jsonReadFile(M.CONSENT_FILE) end)
  if ok and type(data) == 'table' and type(data.consent) == 'table' then
    for k, v in pairs(data.consent) do if type(k) == 'string' and type(v) == 'string' then consent[k] = v end end
  end
  return consent
end

local function saveConsent()
  if jsonWriteFile then pcall(jsonWriteFile, M.CONSENT_FILE, { consent = consent }, true) end
end

-- Has the player agreed to this add-on's notice (for its author as the list names them now)?
function M.consented(id)
  local a = entry(id)
  return a ~= nil and loadConsent()[id] == a.author
end

local function applied(id, op, ok, err)
  net.send({ type = 'addon_applied', id = id, op = op, ok = ok == true, error = err })
end

------------------------------------------------------------------------------------------
-- asking the server

function M.request(refresh)
  if not net.isConnected() then return false end
  listAsked = true
  net.send({ type = 'addons_list', refresh = refresh == true })
  return true
end

function M.requestDetail(id, refresh)
  if type(id) ~= 'string' or not net.isConnected() then return false end
  net.send({ type = 'addon_detail', id = id, refresh = refresh == true })
  return true
end

-- Install or update add-on id (agreed: the player has just agreed to its notice). True, or false and why.
function M.install(id, agreed)
  local a = entry(id)
  if not a then return false, 'not in the list' end
  if a.kind ~= 'beamng-mod' and a.kind ~= 'companion' then return false, 'this add-on is not installed from here' end
  if M.inMp() then return false, 'not while a BeamMP server is joined: leave it first' end
  if not net.isConnected() then return false, 'the BeamNG.Earth app is not connected' end
  if not M.consented(id) then
    if agreed ~= true then return false, 'needs your agreement first' end
    loadConsent()[id] = a.author
    saveConsent()
  end
  setStatus(id, 'progress', 'Asking GitHub...')
  net.send({ type = 'addon_install', id = id, consent = true, mp = M.inMp() })
  return true
end

-- Uninstall, enable or disable (op: 'uninstall' | 'enable' | 'disable') an installed add-on. True, or false and why.
-- A program beside the game (kind companion): 'start' | 'stop' | 'uninstall' | 'autostart_on' | 'autostart_off', done by
-- the server.
local COMPANION_OPS = { start = 'Starting...', stop = 'Stopping...', uninstall = 'Removing...',
  autostart_on = 'Saving...', autostart_off = 'Saving...' }

function M.manage(id, op)
  local a = entry(id)
  if a and a.kind == 'companion' then
    if not COMPANION_OPS[op] then return false, 'unknown action' end
    if not net.isConnected() then return false, 'the BeamNG.Earth app is not connected' end
    setStatus(id, 'progress', COMPANION_OPS[op])
    net.send({ type = 'addon_run', id = id, op = op })
    return true
  end
  if not a or a.kind ~= 'beamng-mod' or type(a.file) ~= 'string' or not a.file:match(M.FILE) then
    return false, 'not an installed add-on'
  end
  if op ~= 'uninstall' and op ~= 'enable' and op ~= 'disable' then return false, 'unknown action' end
  if M.inMp() then return false, 'not while a BeamMP server is joined: leave it first' end
  queue[#queue + 1] = { op = op, id = id, file = a.file, since = clock, oldCode = a.code, code = a.code }
  setStatus(id, 'progress', op == 'uninstall' and 'Removing...' or (op == 'enable' and 'Turning on...' or 'Turning off...'))
  return true
end

-- Add from GitHub: the pasted repository (owner/name or its link) put on the player's own list by the server.
function M.add(repo)
  if type(repo) ~= 'string' or #repo < 3 or #repo > 300 then return false, 'paste a GitHub repository' end
  if M.inMp() then return false, 'not while a BeamMP server is joined: leave it first' end
  if not net.isConnected() then return false, 'the BeamNG.Earth app is not connected' end
  adding = { state = 'progress', text = 'Reading it on GitHub...' }
  serial = serial + 1
  net.send({ type = 'addon_add', repo = repo })
  return true
end

-- An add-on's Options tab saved: values { key = value } (the server checks each against its option).
function M.setOptions(id, values)
  local a = entry(id)
  if not a or type(values) ~= 'table' then return false, 'no such add-on' end
  if not net.isConnected() then return false, 'the BeamNG.Earth app is not connected' end
  setStatus(id, 'progress', 'Saving...')
  net.send({ type = 'addon_options', id = id, values = values })
  return true
end

-- { key = value } of an add-on's options as the list has them (beamngEarth_api.addonOptions), or nil.
function M.optionValues(id)
  local a = entry(id)
  if not a then return nil end
  local out = {}
  for _, o in ipairs(type(a.options) == 'table' and a.options or {}) do
    if type(o) == 'table' and type(o.key) == 'string' then out[o.key] = o.value end
  end
  return out
end

-- One the player added, taken off their list.
function M.forget(id)
  local a = entry(id)
  if not a or a.added ~= true then return false, 'only add-ons you added can be removed from the list' end
  if not net.isConnected() then return false, 'the BeamNG.Earth app is not connected' end
  net.send({ type = 'addon_forget', id = id })
  return true
end

------------------------------------------------------------------------------------------
-- the server's messages

function M.onMessage(msg)
  local t = msg.type
  if t == 'addons' then
    list = type(msg.addons) == 'table' and msg.addons or {}
    -- (an add-on's options changed since the mods last saw them: told, beamngEarth_api's addonOptions event; the first
    -- time an add-on's values come is no change)
    local api = rawget(_G, 'beamngEarth_api')
    for _, a in ipairs(list) do
      local now, was = M.optionValues(a.id), optKnown[a.id]
      if was and now then
        local changed = false
        for k, v in pairs(now) do if was[k] ~= v then changed = true end end
        for k in pairs(was) do if now[k] == nil then changed = true end end
        if changed and api and type(api._event) == 'function' then pcall(api._event, 'addonOptions', { id = a.id, values = now }) end
      end
      optKnown[a.id] = (now and next(now) ~= nil) and now or nil
    end
    listError = type(msg.error) == 'string' and msg.error or nil
    serial = serial + 1
  elseif t == 'addon_detail' then
    if type(msg.id) == 'string' then
      local d = msg
      serial = serial + 1
      d.serial = serial
      details[msg.id] = d
    end
  elseif t == 'addon_status' then
    if type(msg.id) == 'string' then
      setStatus(msg.id, tostring(msg.state or ''), tostring(msg.text or ''), tonumber(msg.done), tonumber(msg.total))
    end
  elseif t == 'addon_added' then
    adding = msg.ok == true and { state = 'done', text = 'Added to your list.', id = tostring(msg.id or '') }
      or { state = 'error', text = tostring(msg.error or 'could not add it') }
    serial = serial + 1
  elseif t == 'addon_apply' then
    local a = type(msg.id) == 'string' and entry(msg.id) or nil
    local file, tmp = msg.file, msg.tmp
    -- only the file the list names for this add-on, and its download beside it
    if not a or a.file ~= file or type(file) ~= 'string' or not file:match(M.FILE) or tmp ~= file .. M.TMP_SUFFIX then
      logI('refused a hand-over for ' .. tostring(msg.id) .. ' (' .. tostring(file) .. ')')
      if type(msg.id) == 'string' then applied(msg.id, 'install', false, 'refused by the mod') end
      return
    end
    local old = {}
    for _, o in ipairs(type(msg.old) == 'table' and msg.old or {}) do
      if type(o) == 'string' and o:match(M.FILE) and o:lower() ~= file:lower() then old[#old + 1] = o end
    end
    queue[#queue + 1] = { op = msg.op == 'update' and 'update' or 'install', id = msg.id, file = file, tmp = tmp,
      old = old, active = msg.active ~= false, tag = tostring(msg.tag or ''), apps = msg.uiApps, since = clock,
      code = type(msg.code) == 'table' and msg.code or nil, oldCode = type(msg.oldCode) == 'table' and msg.oldCode or nil }
  end
end

------------------------------------------------------------------------------------------
-- the hand-over to the game's mod manager

local function exists(f, path)
  local ok, v = pcall(f.fileExists, f, path)
  return ok and v == true
end

local function dbEntry(mm, name)
  local ok, e = pcall(mm.getModDB, name)
  return ok and type(e) == 'table' and e or nil
end

local function appsHint(apps)
  if type(apps) ~= 'table' or #apps == 0 then return '' end
  local names = {}
  for _, n in ipairs(apps) do if type(n) == 'string' then names[#names + 1] = n end end
  return ' Turn on its UI app (' .. table.concat(names, ', ') .. ') in the game\'s UI Apps menu.'
end

-- The mod's own code taken out of the running game (before an update / turning off / removing): its extensions
-- unloaded, its required modules forgotten, so the new (or no) code is what runs. The number of extensions unloaded.
function M.unloadCode(code)
  if type(code) ~= 'table' then return 0 end
  local ext, n = M.extensions(), 0
  if ext and type(ext.isExtensionLoaded) == 'function' and type(ext.unload) == 'function' then
    for _, name in ipairs(type(code.extensions) == 'table' and code.extensions or {}) do
      if type(name) == 'string' and not name:lower():match('^beamngearth') and ext.isExtensionLoaded(name) then
        pcall(ext.unload, name)
        n = n + 1
      end
    end
  end
  local own = {}
  for _, m in ipairs(type(code.modules) == 'table' and code.modules or {}) do
    if type(m) == 'string' and not m:lower():match('^beamngearth') then own[m] = true end
  end
  if next(own) then
    for k in pairs(package.loaded) do
      if type(k) == 'string' and own[(k:gsub('%.', '/'))] then package.loaded[k] = nil end
    end
  end
  return n
end

-- After a hand-over: the UI reloaded when the mod has UI files (its apps come or go); what still needs a restart.
local function afterCode(job)
  local note = ''
  local codes = { job.code, job.oldCode }
  local ui, over, veh = false, 0, false
  for _, c in ipairs(codes) do
    if type(c) == 'table' then
      ui = ui or c.ui == true
      over = math.max(over, tonumber(c.overrides) or 0)
      veh = veh or c.vehicle == true
    end
  end
  if ui then
    reloadAt = clock + M.RELOAD_UI_S
    note = note .. ' The UI reloads in a moment so its apps update.'
  end
  if over > 0 then note = note .. ' It changes ' .. over .. ' of the game\'s own files: restart BeamNG.drive for those.' end
  if veh then note = note .. ' Vehicle changes apply when the vehicle is spawned again.' end
  return note
end

-- One hand-over: true, or false and why.
local function run(job, mm, f)
  local name = M.modName(job.file)
  local dst = '/mods/' .. job.file
  if job.op == 'install' or job.op == 'update' then
    local src = '/mods/' .. job.tmp
    if not exists(f, src) then return false, 'the downloaded file is gone' end
    if exists(f, dst) and dbEntry(mm, name) then
      job.unloaded = M.unloadCode(job.oldCode)
      mm.updateZipMod(job.file, job.tmp)
      if exists(f, src) or not exists(f, dst) then return false, 'the mod manager did not take the new file' end
      job.op = 'update'
    else
      -- (a file the mod manager does not know: its updateZipMod would fail on it, 0.40's loop over its mods reads an
      -- undefined variable; the old file is removed and the new one put in its place as for a new add-on)
      if exists(f, dst) then
        pcall(f.removeFile, f, dst)
        if exists(f, dst) then return false, 'could not replace the old file' end
        job.op = 'update'
      end
      local ok, r = pcall(f.renameFile, f, src, dst)
      if not ok or (r ~= 0 and r ~= true) or not exists(f, dst) then return false, 'could not move the file into place' end
    end
    for _, o in ipairs(job.old or {}) do
      local on = M.modName(o)
      if dbEntry(mm, on) then pcall(mm.deleteMod, on)
      elseif exists(f, '/mods/' .. o) then pcall(f.removeFile, f, '/mods/' .. o) end
      logI('removed the older copy ' .. o)
    end
    checks[#checks + 1] = { id = job.id, name = name, file = job.file, at = clock + M.MOUNT_CHECK_S, active = job.active }
    return true
  elseif job.op == 'uninstall' then
    job.unloaded = M.unloadCode(job.oldCode)
    if dbEntry(mm, name) then pcall(mm.deleteMod, name)
    elseif exists(f, dst) then pcall(f.removeFile, f, dst) end
    if exists(f, '/mods/' .. job.file .. M.TMP_SUFFIX) then pcall(f.removeFile, f, '/mods/' .. job.file .. M.TMP_SUFFIX) end
    if exists(f, dst) then return false, 'the file could not be removed' end
    return true
  else
    if not dbEntry(mm, name) then return false, 'the game does not know this mod yet' end
    if job.op == 'disable' then job.unloaded = M.unloadCode(job.oldCode) end
    pcall(job.op == 'enable' and mm.activateMod or mm.deactivateMod, name)
    local e = dbEntry(mm, name)
    if not e or (e.active ~= false) ~= (job.op == 'enable') then return false, 'the mod manager did not change it' end
    return true
  end
end

local DONE_TEXT = {
  install = function(job) return 'Installed ' .. job.tag .. '.' .. appsHint(job.apps) end,
  update = function(job) return 'Updated to ' .. job.tag .. '.' end,
  uninstall = function() return 'Removed.' end,
  enable = function() return 'Turned on.' end,
  disable = function() return 'Turned off.' end,
}

function M.tick(dt)
  clock = clock + (tonumber(dt) or 0)
  if reloadAt and clock >= reloadAt and #queue == 0 and #checks == 0 then
    reloadAt = nil
    logI('reloading the UI (an add-on with UI files changed)')
    pcall(M.reloadUI)
  end
  if #queue == 0 and #checks == 0 then return end
  local mm, f = M.modmanager(), M.fs()
  local job = queue[1]
  if job then
    local ready = mm and f and type(mm.isReady) == 'function' and mm.isReady() and not M.inMp()
    if ready then
      table.remove(queue, 1)
      local ok, done, why = pcall(run, job, mm, f)
      if not ok then done, why = false, tostring(done) end
      logI(job.op .. ' ' .. job.id .. ': ' .. (done and 'done' or ('failed: ' .. tostring(why))))
      if done then setStatus(job.id, 'done', DONE_TEXT[job.op](job) .. afterCode(job))
      else setStatus(job.id, 'error', 'Could not finish: ' .. tostring(why)) end
      applied(job.id, job.op, done, not done and tostring(why) or nil)
    elseif clock - job.since > M.APPLY_WAIT_S then
      table.remove(queue, 1)
      local why = M.inMp() and 'a BeamMP server was joined' or 'the game\'s mod manager stayed busy'
      setStatus(job.id, 'error', 'Could not finish: ' .. why)
      applied(job.id, job.op, false, why)
    end
  end
  -- mounted yet? The file watcher normally has; otherwise the mod manager is asked to look at the file
  for i = #checks, 1, -1 do
    local c = checks[i]
    if clock >= c.at and mm then
      table.remove(checks, i)
      if not dbEntry(mm, c.name) and type(mm.workOffChangedMod) == 'function' then
        logI(c.file .. ' not mounted by the file watcher: asking the mod manager')
        pcall(mm.workOffChangedMod, '/mods/' .. c.file, 'added')
      end
      local e = dbEntry(mm, c.name)
      if e and e.active ~= false and c.active == false then pcall(mm.deactivateMod, c.name) end
      serial = serial + 1
    end
  end
end

------------------------------------------------------------------------------------------
-- the page (settings.lua's uiAddon*)

-- { addons = [card + active (the mod manager's), consented, status], error, asked, connected, mp, serial }
function M.uiState()
  if not listAsked then M.request(false) end
  local mm = M.modmanager()
  local out = {}
  for _, a in ipairs(list or {}) do
    local c = {}
    for k, v in pairs(a) do c[k] = v end
    if mm and a.installed and type(a.file) == 'string' then
      local e = dbEntry(mm, M.modName(a.file))
      if e then c.active = e.active ~= false end
    end
    c.consented = M.consented(a.id)
    c.status = status[a.id]
    c.detailSerial = details[a.id] and details[a.id].serial or 0
    out[#out + 1] = c
  end
  return { addons = out, error = listError, asked = listAsked, loaded = list ~= nil, connected = net.isConnected(),
    mp = M.inMp(), serial = serial, adding = adding }
end

function M.uiDetail(id) return details[id] end

function M.reset()
  list, listError, listAsked, details, status, queue, checks, consent, serial = nil, nil, false, {}, {}, {}, {}, nil, 0
  reloadAt, adding, optKnown = nil, nil, {}
end

return M
