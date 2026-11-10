-- luajit mod/tests/test_addons.lua  (the Add-ons page's game side, addons.lua)
-- The list asked once connected; consent once per add-on (for its author), kept in its file; no install in a
-- BeamMP session or for a non-mod; the server's hand-over taken only for the file the list names; the file handed to
-- the game's mod manager when it is ready (a new one renamed into place and mounted by the file watcher, else asked;
-- an update through core_modmanager.updateZipMod, a mod that was off turned off again; older copies removed);
-- enable / disable / uninstall through the mod manager; a mod manager busy too long fails the hand-over; every step
-- answered with addon_applied.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')

-- the connection
local sent, connected = {}, false
package.loaded['beamngEarth/net'] = {
  isConnected = function() return connected end,
  send = function(m) sent[#sent + 1] = m end,
}
local function last(kind)
  for i = #sent, 1, -1 do if sent[i].type == kind then return sent[i] end end
end

-- the settings files
local jsonFiles = {}
function jsonReadFile(p) return jsonFiles[p] end
function jsonWriteFile(p, d) jsonFiles[p] = d; return true end
function log() end

local A = require('beamngEarth/addons')

-- the game's file system and mod manager
local files = {}
local fsCalls = {}
local FSF = {
  fileExists = function(_, p) return files[p] == true end,
  renameFile = function(_, a, b)
    fsCalls[#fsCalls + 1] = 'rename ' .. a .. ' ' .. b
    if not files[a] then return -1 end
    files[a], files[b] = nil, true
    return 0
  end,
  removeFile = function(_, p) fsCalls[#fsCalls + 1] = 'remove ' .. p; files[p] = nil; return 0 end,
}
local db, mmCalls, ready, watcher = {}, {}, true, true
local MM = {
  isReady = function() return ready end,
  getModDB = function(n) return db[n] end,
  updateZipMod = function(old, new)
    mmCalls[#mmCalls + 1] = 'updateZipMod ' .. old .. ' ' .. new
    db[A.modName(old)] = nil
    files['/mods/' .. old] = nil
    files['/mods/' .. old], files['/mods/' .. new] = files['/mods/' .. new], nil
    if watcher then db[A.modName(old)] = { active = true } end
  end,
  deleteMod = function(n) mmCalls[#mmCalls + 1] = 'deleteMod ' .. n; db[n] = nil; files['/mods/' .. n .. '.zip'] = nil end,
  activateMod = function(n) mmCalls[#mmCalls + 1] = 'activateMod ' .. n; db[n].active = true end,
  deactivateMod = function(n) mmCalls[#mmCalls + 1] = 'deactivateMod ' .. n; db[n].active = false end,
  workOffChangedMod = function(p, kind) mmCalls[#mmCalls + 1] = 'workOff ' .. p .. ' ' .. kind; db[A.modName(p:match('[^/]+$'))] = { active = true } end,
}
A.fs = function() return FSF end
A.modmanager = function() return MM end
local mp = false
A.inMp = function() return mp end

local function has(list, s)
  for _, v in ipairs(list) do if v == s then return true end end
  return false
end

------------------------------------------------------------------------------------------
-- the list

T.check(A.request() == false and #sent == 0, 'not connected: nothing asked')
local s = A.uiState()
T.check(s.loaded == false and s.asked == false, 'not connected: the page says not loaded, and asks again later')
connected = true
s = A.uiState()
T.check(last('addons_list') ~= nil and s.asked == true, 'connected: the list asked by the page')
A.onMessage({ type = 'addons', addons = {
  { id = 'truck', kind = 'beamng-mod', author = 'Merick', file = 'truck-mod.zip', installed = false },
  { id = 'gps', kind = 'companion', author = 'Leo', file = '' } } })
s = A.uiState()
T.check(#s.addons == 2 and s.addons[1].consented == false and s.loaded, 'the cards, not agreed to yet')

------------------------------------------------------------------------------------------
-- consent and install

local ok, why = A.install('truck')
T.check(not ok and why:match('agreement'), 'no install before the notice is agreed to')
mp = true
ok, why = A.install('truck', true)
T.check(not ok and why:match('BeamMP'), 'no install in a BeamMP session')
mp = false
ok = A.install('gps', true)
local g = last('addon_install')
T.check(ok and g and g.id == 'gps', 'a companion: the server installs it (no hand-over)')
ok = A.install('truck', true)
local m = last('addon_install')
T.check(ok and m and m.id == 'truck' and m.consent == true and m.mp == false, 'agreed: asked with consent')
T.check(jsonFiles[A.CONSENT_FILE].consent.truck == 'Merick', 'the agreement kept, with the author named')
T.check(A.consented('truck') and A.uiState().addons[1].status.state == 'progress', 'agreed; the card shows progress')
A.onMessage({ type = 'addons', addons = {
  { id = 'truck', kind = 'beamng-mod', author = 'Someone else', file = 'truck-mod.zip', installed = false } } })
T.check(not A.consented('truck'), 'another author in the list: asked again')
A.onMessage({ type = 'addons', addons = {
  { id = 'truck', kind = 'beamng-mod', author = 'Merick', file = 'truck-mod.zip', installed = false } } })

-- the server's hand-over: only for the list's file
sent = {}
A.onMessage({ type = 'addon_apply', id = 'truck', op = 'install', file = 'other.zip', tmp = 'other.zip.bnge-tmp' })
T.check(last('addon_applied') and last('addon_applied').ok == false, 'another file than the list names: refused')
A.onMessage({ type = 'addon_apply', id = 'truck', op = 'install', file = 'truck-mod.zip', tmp = '../x' })
T.check(#sent == 2, 'a download name other than <file>.bnge-tmp: refused')
A.tick(0.1)
T.check(#fsCalls == 0 and #mmCalls == 0, 'nothing queued by refusals')

-- a new add-on: renamed into place when the mod manager is ready; older copies removed
files['/mods/truck-mod.zip.bnge-tmp'] = true
files['/mods/old_truck.zip'] = true
db.earth_freight = { active = true }
files['/mods/earth_freight.zip'] = true
ready = false
sent = {}
A.onMessage({ type = 'addon_apply', id = 'truck', op = 'install', file = 'truck-mod.zip', tmp = 'truck-mod.zip.bnge-tmp',
  old = { 'old_truck.zip', 'earth_freight.zip', '../../evil.zip', 'truck-mod.zip' }, tag = 'build-142', active = true,
  uiApps = { 'TruckApp' } })
A.tick(1)
T.check(files['/mods/truck-mod.zip.bnge-tmp'] and #sent == 0, 'the mod manager busy: waits')
ready = true
watcher = false
A.tick(1)
T.check(files['/mods/truck-mod.zip'] and not files['/mods/truck-mod.zip.bnge-tmp'], 'renamed into place')
T.check(has(fsCalls, 'rename /mods/truck-mod.zip.bnge-tmp /mods/truck-mod.zip'), 'through FS:renameFile')
T.check(has(fsCalls, 'remove /mods/old_truck.zip') and has(mmCalls, 'deleteMod earth_freight'),
  'older copies removed (through the mod manager when it knows them)')
T.check(not has(fsCalls, 'remove /mods/../../evil.zip'), 'never a path out of mods/')
local ap = last('addon_applied')
T.check(ap and ap.ok == true and ap.op == 'install', 'applied: ok')
local st = A.uiState().addons[1].status
T.check(st.state == 'done' and st.text:match('build%-142') and st.text:match('TruckApp'), 'done, with the UI app hint')
A.tick(1)
T.check(not has(mmCalls, 'workOff /mods/truck-mod.zip added'), 'the watcher gets its seconds first')
A.tick(A.MOUNT_CHECK_S)
T.check(has(mmCalls, 'workOff /mods/truck-mod.zip added') and db['truck-mod'], 'not mounted: the mod manager asked')

-- an update, of a mod the player had turned off: updateZipMod, then off again
mmCalls, fsCalls, sent = {}, {}, {}
watcher = true
db['truck-mod'] = { active = false }
files['/mods/truck-mod.zip.bnge-tmp'] = true
-- (its code loaded in the game: unloaded before the update, so the new version's runs; the UI reloaded after)
local loadedExt, unloaded, uiReloads = { truckX_core = true, beamngEarth_core = true }, {}, 0
A.extensions = function() return {
  isExtensionLoaded = function(n) return loadedExt[n] == true end,
  unload = function(n) loadedExt[n] = nil; unloaded[#unloaded + 1] = n end } end
A.reloadUI = function() uiReloads = uiReloads + 1 end
package.loaded['truckX/data'], package.loaded['truckX.more'], package.loaded['other/mod'] = {}, {}, {}
A.onMessage({ type = 'addon_apply', id = 'truck', op = 'update', file = 'truck-mod.zip', tmp = 'truck-mod.zip.bnge-tmp',
  tag = 'build-143', active = false, code = { extensions = { 'truckX_core' }, modules = { 'truckX/core' }, ui = true },
  oldCode = { extensions = { 'truckX_core', 'truckX_gone', 'beamngEarth_core' }, modules = { 'truckX/data', 'truckX/more' },
    ui = true } })
A.tick(0.1)
T.check(mmCalls[1] == 'updateZipMod truck-mod.zip truck-mod.zip.bnge-tmp', 'an update: the game\'s own updateZipMod')
T.check(#unloaded == 1 and unloaded[1] == 'truckX_core', 'its loaded extension unloaded first (not a loaded one of ours)')
T.check(loadedExt.beamngEarth_core == true, 'BeamNG.Earth\'s own extension never unloaded')
T.check(package.loaded['truckX/data'] == nil and package.loaded['truckX.more'] == nil and package.loaded['other/mod'] ~= nil,
  'its required modules forgotten (both name forms), others kept')
local txt = A.uiState().addons[1].status.text
T.check(txt:match('Updated to build%-143') and txt:match('UI reloads') and not txt:match('Restart'),
  'no restart asked: the UI reloads for its apps')
T.check(uiReloads == 0, 'the UI reload waits')
A.tick(A.MOUNT_CHECK_S + 0.1)
T.check(has(mmCalls, 'deactivateMod truck-mod') and db['truck-mod'].active == false, 'it was off: off again')
A.tick(A.RELOAD_UI_S)
T.check(uiReloads == 1, 'then the UI reloaded once')
package.loaded['other/mod'] = nil
T.check(not has(mmCalls, 'workOff /mods/truck-mod.zip added'), 'mounted by the watcher: not asked')
-- an apply for install while the file is there already counts as an update
files['/mods/truck-mod.zip.bnge-tmp'] = true
mmCalls = {}
A.onMessage({ type = 'addon_apply', id = 'truck', op = 'install', file = 'truck-mod.zip', tmp = 'truck-mod.zip.bnge-tmp' })
A.tick(0.1)
T.check(mmCalls[1] and mmCalls[1]:match('^updateZipMod'), 'the file is there: an update')
-- a file the mod manager does not know (put there by hand while the game ran): replaced, not through updateZipMod
db['truck-mod'] = nil
files['/mods/truck-mod.zip'], files['/mods/truck-mod.zip.bnge-tmp'] = true, true
mmCalls, fsCalls = {}, {}
A.onMessage({ type = 'addon_apply', id = 'truck', op = 'update', file = 'truck-mod.zip', tmp = 'truck-mod.zip.bnge-tmp' })
A.tick(0.1)
T.check(#mmCalls == 0 and fsCalls[1] == 'remove /mods/truck-mod.zip'
  and fsCalls[2] == 'rename /mods/truck-mod.zip.bnge-tmp /mods/truck-mod.zip', 'unknown to the mod manager: replaced by hand')
T.check(last('addon_applied').ok and last('addon_applied').op == 'update', 'reported as an update')
A.tick(A.MOUNT_CHECK_S + 0.1)
T.check(has(mmCalls, 'workOff /mods/truck-mod.zip added'), '... and the mod manager asked to mount it if the watcher did not')
db['truck-mod'] = { active = true }
-- the download gone: an error
sent = {}
A.onMessage({ type = 'addon_apply', id = 'truck', op = 'update', file = 'truck-mod.zip', tmp = 'truck-mod.zip.bnge-tmp' })
A.tick(0.1)
T.check(last('addon_applied').ok == false and A.uiState().addons[1].status.state == 'error', 'the download gone: an error')

------------------------------------------------------------------------------------------
-- enable / disable / uninstall

A.onMessage({ type = 'addons', addons = {
  { id = 'truck', kind = 'beamng-mod', author = 'Merick', file = 'truck-mod.zip', installed = true } } })
db['truck-mod'] = { active = true }
T.check(A.uiState().addons[1].active == true, 'the card shows the mod manager\'s on / off')
mmCalls = {}
T.check(A.manage('truck', 'disable'), 'disable queued')
A.tick(0.1)
T.check(mmCalls[1] == 'deactivateMod truck-mod' and A.uiState().addons[1].active == false, 'turned off')
A.manage('truck', 'enable')
A.tick(0.1)
T.check(mmCalls[2] == 'activateMod truck-mod' and last('addon_applied').op == 'enable', 'turned on, reported')
T.check(not A.manage('truck', 'format c:'), 'an unknown action refused')
mp = true
T.check(not A.manage('truck', 'uninstall'), 'nothing in a BeamMP session')
mp = false
A.manage('truck', 'uninstall')
A.tick(0.1)
T.check(mmCalls[3] == 'deleteMod truck-mod' and not files['/mods/truck-mod.zip'], 'removed through the mod manager')
T.check(last('addon_applied').op == 'uninstall' and last('addon_applied').ok, 'uninstall reported')

-- a mod manager that stays busy
ready = false
A.manage('truck', 'enable')
A.tick(A.APPLY_WAIT_S + 1)
T.check(last('addon_applied').ok == false and A.uiState().addons[1].status.text:match('busy'), 'busy too long: failed')
ready = true

-- the details
A.onMessage({ type = 'addon_detail', id = 'truck', readme = '<p>x</p>', readmeLinks = {}, releases = {} })
T.check(A.uiDetail('truck').readme == '<p>x</p>' and A.uiState().addons[1].detailSerial > 0, 'the detail kept, its serial on the card')
T.check(A.requestDetail('truck', true) and last('addon_detail').refresh == true, 'the detail asked again')

-- options: the list's values for the mods (beamngEarth_api.addonOptions), a change told as the addonOptions event
local seenOpt = {}
_G.beamngEarth_api = { _event = function(ev, info) seenOpt[#seenOpt + 1] = { ev, info } end }
local function withOpts(port)
  return { type = 'addons', addons = { { id = 'truck', kind = 'beamng-mod', file = 'truck-mod.zip', author = 'MadMerick01',
    options = { { key = 'port', type = 'int', value = port }, { key = 'on', type = 'bool', value = true } } } } }
end
A.onMessage(withOpts(47700))
local v = A.optionValues('truck')
T.check(v and v.port == 47700 and v.on == true, 'the values as { key = value }')
T.check(#seenOpt == 0, 'the first list: no change told')
A.onMessage(withOpts(47800))
T.check(#seenOpt == 1 and seenOpt[1][1] == 'addonOptions' and seenOpt[1][2].id == 'truck' and seenOpt[1][2].values.port == 47800,
  'a changed value: the addonOptions event with the new values')
A.onMessage(withOpts(47800))
T.check(#seenOpt == 1, 'unchanged: not told again')
T.check(A.setOptions('truck', { port = 47900 }) and last('addon_options').values.port == 47900, 'Save: sent to the server')
T.check(A.optionValues('nope') == nil, 'no such add-on: nil')
_G.beamngEarth_api = nil

T.done('test_addons')
