-- luajit mod/tests/test_addons.lua
-- Unit tests for mod/beamng_earth/lua/ge/extensions/beamngEarth/addons.lua:
-- VFS discovery (/beamng_earth_addons/*/addon.json), sync payload building,
-- per-addon & per-source user toggles, server sync response application, and LuaJIT bytecode check.

local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local mockFiles = {
  ['/beamng_earth_addons/provence/addon.json'] = {
    schema_version = 1,
    id = 'provence',
    name = 'BeamNG.Earth — Provence & Occitanie Ground Truth',
    version = '1.0.0',
    priority = 100,
    engine_compat = {
      min_version = '1.7.0',
      max_version = '1.8.x',
      earthfast_abi = { min = 113, max = 160 },
      revisit_schema = 2,
      plans_format = 1,
    },
    capabilities = { 'data_sources', 'biomes', 'structured_crops', 'roof_palettes' },
    data_sources_file = 'data_sources.json',
    biomes_file = 'biomes.json',
  },
  ['/beamng_earth_addons/provence/data_sources.json'] = {
    schema_version = 1,
    data_sources = {
      { id = 'ign_rpg_parcels', name = 'IGN RPG Parcels', default_enabled = true, bbox = { 43.0, 4.0, 44.6, 6.8 } },
      { id = 'ign_orthophoto_20cm', name = 'IGN Ortho 20cm', default_enabled = true, bbox = { 43.0, 4.0, 44.6, 6.8 } },
      { id = 'osm_provence_landuse', name = 'OSM Provence Landuse', default_enabled = false, bbox = { 43.0, 4.0, 44.6, 6.8 } },
    },
  },
  ['/beamng_earth_addons/provence/biomes.json'] = {
    schema_version = 1,
    biomes = {
      {
        id = 'provence_mediterranean',
        bbox = { 43.0, 4.0, 44.6, 6.8 },
        structured_crops = {
          vineyard = { row_spacing_m = 2.2, point_spacing_m = 1.2 },
          lavender = { row_spacing_m = 1.5, point_spacing_m = 0.8 },
        },
      },
    },
  },
}

function jsonReadFile(path)
  return mockFiles[path]
end

FS = {
  fileExists = function(_, p) return mockFiles[p] ~= nil end,
  findFiles = function(_, root, pattern, depth, _, _)
    if root == '/beamng_earth_addons' and pattern == 'addon.json' then
      return { '/beamng_earth_addons/provence/addon.json' }
    end
    return {}
  end,
}

local addons = require('beamngEarth/addons')
addons.reset()

-- 1. Discovery via FS:findFiles
local discovered = addons.discover()
T.check(#discovered == 1, 'discovered 1 addon')
T.check(discovered[1].id == 'provence', 'discovered addon id == provence')
T.check(discovered[1].vfs_root == '/beamng_earth_addons/provence', 'discovered vfs_root')
T.check(#discovered[1].data_sources == 3, 'loaded 3 data sources from data_sources.json')
T.check(#discovered[1].biomes == 1, 'loaded 1 biome from biomes.json')
T.check(discovered[1].enabled == true, 'addon enabled by default')
T.check(discovered[1].user_toggles.ign_rpg_parcels == true, 'ign_rpg_parcels enabled by default')
T.check(discovered[1].user_toggles.osm_provence_landuse == false, 'osm_provence_landuse respects default_enabled=false')

-- 2. Toggling addon & sources and building sync payload
T.check(addons.setSourceEnabled('provence', 'ign_orthophoto_20cm', false) == true, 'setSourceEnabled 3-arg')
local payload = addons.buildSyncPayload()
T.check(payload.schema_version == 1 and #payload.addons == 1, 'buildSyncPayload schema_version and count')
T.check(payload.user_toggles.provence.sources.ign_orthophoto_20cm == false, 'sync payload reflects disabled source')

addons.setAddonEnabled('provence', false)
payload = addons.buildSyncPayload()
T.check(payload.addons[1].enabled == false and payload.user_toggles.provence.enabled == false, 'sync payload reflects disabled addon')

-- Re-enable via userToggles argument to buildSyncPayload
payload = addons.buildSyncPayload({ provence = { enabled = true, sources = { ign_orthophoto_20cm = true } } })
T.check(payload.addons[1].enabled == true and payload.user_toggles.provence.sources.ign_orthophoto_20cm == true,
  'buildSyncPayload applies passed userToggles')

-- 3. Applying server sync response & reading status
local status = addons.applySyncResponse({
  active = { 'provence' },
  rejected = {},
  warnings = {},
  cache_fingerprint = 'a1b2c3d4e5f6',
  approval_policy = 'strict',
  addons = {
    { id = 'provence', status = 'active', approval_status = 'approved', active = true },
  },
})
T.check(status.synced == true, 'status marked synced')
T.check(status.active[1] == 'provence', 'active addon stored')
T.check(status.cache_fingerprint == 'a1b2c3d4e5f6', 'cache_fingerprint stored')
T.check(status.approval_policy == 'strict', 'approval_policy stored')
T.check(addons.getStatus().cache_fingerprint == 'a1b2c3d4e5f6', 'getStatus returns current state')

-- 4. Safe fallback when FS is nil
local savedFS = FS
FS = nil
addons.reset()
local empty = addons.discover()
T.check(type(empty) == 'table' and #empty == 0, 'safe fallback when FS is nil')
FS = savedFS

-- 5. LuaJIT bytecode check (200-local-variable limit & scoping) for addons.lua and core.lua
local extDir = dir .. '/../beamng_earth/lua/ge/extensions/beamngEarth/'
for _, fname in ipairs({ 'addons.lua', 'core.lua' }) do
  local fpath = extDir .. fname
  local fh = io.open(fpath, 'r')
  T.check(fh ~= nil, fname .. ' exists and is readable')
  if fh then
    local src = fh:read('*a')
    fh:close()
    local fn, loadErr = loadfile(fpath)
    T.check(fn ~= nil, fname .. ' compiles cleanly under LuaJIT (<200 locals): ' .. tostring(loadErr))
    T.check(#src > 100, fname .. ' non-empty source')
  end
end

T.done('test_addons')
