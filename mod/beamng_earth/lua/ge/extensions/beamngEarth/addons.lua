-- Native BeamNG Addon Discovery & Layer A Ground Truth Sync (addons.lua)
-- Scans /beamng_earth_addons/*/addon.json via BeamNG VFS (FS:findFiles), reads manifests +
-- sibling data_sources.json and biomes.json, tracks per-addon and per-source toggles,
-- and builds/applies sync payloads with earthd.py.

local M = {}

M.VFS_ROOT = '/beamng_earth_addons'
M.SCHEMA_VERSION = 1

local state = {
  discovered = {},
  addonEnabled = {},
  sourceToggles = {},
  syncStatus = {
    synced = false,
    active = {},
    rejected = {},
    warnings = {},
    cache_fingerprint = '',
    approval_policy = 'warn',
    addons = {},
  },
}

local function copyMap(src)
  local out = {}
  if type(src) == 'table' then
    for k, v in pairs(src) do out[k] = v end
  end
  return out
end

local function fileExists(path)
  if (type(FS) == 'table' or type(FS) == 'userdata') and type(FS.fileExists) == 'function' then
    local ok, res = pcall(FS.fileExists, FS, path)
    if ok then return res == true end
  end
  if type(jsonReadFile) == 'function' then
    local ok, res = pcall(jsonReadFile, path)
    if ok and res ~= nil then return true end
  end
  if type(readFile) == 'function' then
    local ok, res = pcall(readFile, path)
    if ok and res ~= nil then return true end
  end
  return false
end

local function readJson(path)
  if type(jsonReadFile) == 'function' then
    local ok, data = pcall(jsonReadFile, path)
    if ok and type(data) == 'table' then return data end
  end
  if type(readFile) == 'function' then
    local ok, raw = pcall(readFile, path)
    if ok and type(raw) == 'string' and #raw > 0 and type(jsonDecode) == 'function' then
      local dok, decoded = pcall(jsonDecode, raw)
      if dok and type(decoded) == 'table' then return decoded end
    end
  end
  return nil
end

local function findAddonManifests()
  if (type(FS) ~= 'table' and type(FS) ~= 'userdata') or type(FS.findFiles) ~= 'function' then
    return {}
  end
  local ok, files = pcall(FS.findFiles, FS, M.VFS_ROOT, 'addon.json', 2, true, false)
  if not ok or type(files) ~= 'table' then
    return {}
  end
  local sorted = {}
  for i = 1, #files do
    if type(files[i]) == 'string' then
      sorted[#sorted + 1] = files[i]:gsub('\\', '/')
    end
  end
  table.sort(sorted)
  return sorted
end

function M.reset()
  state.discovered = {}
  state.addonEnabled = {}
  state.sourceToggles = {}
  state.syncStatus = {
    synced = false,
    active = {},
    rejected = {},
    warnings = {},
    cache_fingerprint = '',
    approval_policy = 'warn',
    addons = {},
  }
end

function M.discover()
  local paths = findAddonManifests()
  local list = {}
  for i = 1, #paths do
    local mPath = paths[i]
    local vfsRoot = mPath:match('^(.*)/addon%.json$') or M.VFS_ROOT
    local manifest = readJson(mPath)
    if type(manifest) == 'table' then
      local addonId = tostring(manifest.id or vfsRoot:match('([^/]+)$') or ('addon_' .. i))
      local dsFile = type(manifest.data_sources_file) == 'string' and manifest.data_sources_file or 'data_sources.json'
      local biomesFile = type(manifest.biomes_file) == 'string' and manifest.biomes_file or 'biomes.json'

      local dsPath = vfsRoot .. '/' .. dsFile
      local biomesPath = vfsRoot .. '/' .. biomesFile

      local dsRaw = fileExists(dsPath) and readJson(dsPath) or nil
      local dataSources = {}
      if type(dsRaw) == 'table' then
        dataSources = type(dsRaw.data_sources) == 'table' and dsRaw.data_sources or dsRaw
      elseif type(manifest.data_sources) == 'table' then
        dataSources = manifest.data_sources
      end

      local bRaw = fileExists(biomesPath) and readJson(biomesPath) or nil
      local biomes = {}
      if type(bRaw) == 'table' then
        biomes = type(bRaw.biomes) == 'table' and bRaw.biomes or bRaw
      elseif type(manifest.biomes) == 'table' then
        biomes = manifest.biomes
      end

      if state.addonEnabled[addonId] == nil then
        state.addonEnabled[addonId] = (manifest.enabled ~= false)
      end
      if type(state.sourceToggles[addonId]) ~= 'table' then
        state.sourceToggles[addonId] = {}
      end
      for j = 1, #dataSources do
        local ds = dataSources[j]
        if type(ds) == 'table' and type(ds.id) == 'string' then
          if state.sourceToggles[addonId][ds.id] == nil then
            state.sourceToggles[addonId][ds.id] = (ds.default_enabled ~= false)
          end
        end
      end

      list[#list + 1] = {
        id = addonId,
        manifest = manifest,
        data_sources = dataSources,
        biomes = biomes,
        vfs_root = vfsRoot,
        enabled = state.addonEnabled[addonId],
        user_toggles = copyMap(state.sourceToggles[addonId]),
      }
    end
  end
  state.discovered = list
  return list
end

function M.setAddonEnabled(addonId, enabled)
  if type(addonId) ~= 'string' or #addonId == 0 then return false end
  local flag = (enabled == true)
  state.addonEnabled[addonId] = flag
  for i = 1, #state.discovered do
    if state.discovered[i].id == addonId then
      state.discovered[i].enabled = flag
    end
  end
  return flag
end

function M.setSourceEnabled(addonId, sourceId, enabled)
  if enabled == nil and type(sourceId) == 'boolean' then
    local targetSource = addonId
    local flag = sourceId
    local updated = false
    for i = 1, #state.discovered do
      local a = state.discovered[i]
      state.sourceToggles[a.id] = state.sourceToggles[a.id] or {}
      state.sourceToggles[a.id][targetSource] = flag
      a.user_toggles = copyMap(state.sourceToggles[a.id])
      updated = true
    end
    return updated
  end
  if type(addonId) ~= 'string' or type(sourceId) ~= 'string' then return false end
  local flag = (enabled == true)
  state.sourceToggles[addonId] = state.sourceToggles[addonId] or {}
  state.sourceToggles[addonId][sourceId] = flag
  for i = 1, #state.discovered do
    if state.discovered[i].id == addonId then
      state.discovered[i].user_toggles = copyMap(state.sourceToggles[addonId])
    end
  end
  return true
end

function M.buildSyncPayload(userToggles)
  if #state.discovered == 0 then
    M.discover()
  end
  if type(userToggles) == 'table' then
    for addonId, cfg in pairs(userToggles) do
      if type(cfg) == 'boolean' then
        M.setAddonEnabled(addonId, cfg)
      elseif type(cfg) == 'table' then
        if type(cfg.enabled) == 'boolean' then
          M.setAddonEnabled(addonId, cfg.enabled)
        end
        local srcMap = type(cfg.sources) == 'table' and cfg.sources or cfg
        for k, v in pairs(srcMap) do
          if k ~= 'enabled' and type(v) == 'boolean' then
            M.setSourceEnabled(addonId, k, v)
          end
        end
      end
    end
  end

  local addonsList = {}
  local togglesOut = {}
  for i = 1, #state.discovered do
    local item = state.discovered[i]
    local aid = item.id
    local en = (state.addonEnabled[aid] ~= false)
    local st = copyMap(state.sourceToggles[aid])
    addonsList[#addonsList + 1] = {
      id = aid,
      manifest = item.manifest,
      data_sources = item.data_sources,
      biomes = item.biomes,
      vfs_root = item.vfs_root,
      enabled = en,
      user_toggles = st,
    }
    togglesOut[aid] = {
      enabled = en,
      sources = st,
    }
  end

  return {
    schema_version = M.SCHEMA_VERSION,
    addons = addonsList,
    user_toggles = togglesOut,
  }
end

function M.applySyncResponse(resp)
  if type(resp) ~= 'table' then return M.getStatus() end
  state.syncStatus.synced = true
  state.syncStatus.active = type(resp.active) == 'table' and resp.active or {}
  state.syncStatus.rejected = type(resp.rejected) == 'table' and resp.rejected or {}
  state.syncStatus.warnings = type(resp.warnings) == 'table' and resp.warnings or {}
  state.syncStatus.cache_fingerprint = tostring(resp.cache_fingerprint or '')
  state.syncStatus.approval_policy = tostring(resp.approval_policy or state.syncStatus.approval_policy or 'warn')
  state.syncStatus.addons = type(resp.addons) == 'table' and resp.addons or {}
  return M.getStatus()
end

function M.getStatus()
  local toggles = {}
  for aid, en in pairs(state.addonEnabled) do
    toggles[aid] = {
      enabled = en,
      sources = copyMap(state.sourceToggles[aid]),
    }
  end
  return {
    synced = state.syncStatus.synced,
    discovered = state.discovered,
    active = state.syncStatus.active,
    rejected = state.syncStatus.rejected,
    warnings = state.syncStatus.warnings,
    cache_fingerprint = state.syncStatus.cache_fingerprint,
    approval_policy = state.syncStatus.approval_policy,
    addons = state.syncStatus.addons,
    user_toggles = toggles,
  }
end

return M
