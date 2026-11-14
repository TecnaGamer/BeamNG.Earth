-- Temporary renderer probe. Run in GE-Lua on a loaded Freeroam map.
-- Does not alter the Earth installation or save the level.
if _G.bneNlForestProbe then _G.bneNlForestProbe.cleanup() end
assert(scenetree.MissionGroup, 'Open a Freeroam map first')
local root = '/temp/nl_forest_probe_assets_20261114/'
local manifest = assert(jsonReadFile(root .. 'manifest.json'))
loadJsonMaterialsFile(root .. 'main.materials.json')
local state = {types = {}, count = 0}
_G.bneNlForestProbe = state
function state.cleanup()
  if state.forest then state.forest:delete(); state.forest = nil end
  for _, d in pairs(state.types) do d:delete() end
  state.types = {}
end
local function run()
  local vehicle = be:getPlayerVehicle(0)
  assert(vehicle, 'Spawn a vehicle first')
  local pos = vehicle:getPosition()
  local dir = vehicle:getDirectionVector()
  local len = math.sqrt(dir.x * dir.x + dir.y * dir.y)
  assert(len > 0.01, 'Vehicle direction unavailable')
  local tx, ty = dir.x / len, dir.y / len
  local rx, ry = ty, -tx
  local ground = pos.z
  if core_terrain and core_terrain.getTerrainHeight then
    ground = core_terrain.getTerrainHeight(pos) or ground
  end
  local base = vec3(pos.x + tx * 12, pos.y + ty * 12, ground)
  local f = assert(createObject('Forest'))
  state.forest = f
  f.canSave = false
  f:registerObject('bneNlForestProbeForest')
  scenetree.MissionGroup:addObject(f)
  local fd = assert(f:getData())
  local function add(shape, side, along, height, stretch)
    local d = state.types[shape]
    if not d then
      d = assert(createObject('TSForestItemData'))
      -- The engine resolves this virtual DAE name to the compiled CDAE beside it.
      d:setField('shapeFile', 0, root .. shape .. '.dae')
      for _, field in ipairs({'collidable', 'windScale', 'branchAmp', 'detailAmp'}) do
        d:setField(field, 0, '0')
      end
      d:registerObject('bneNlForestProbe_' .. shape)
      Sim.getDataBlockSet():addObject(d)
      state.types[shape] = d
    end
    local m = MatrixF(true)
    m:setColumn(0, vec3(-tx, -ty, 0))
    m:setColumn(1, vec3(rx * (stretch or 1), ry * (stretch or 1), 0))
    m:setColumn(2, vec3(0, 0, 1))
    m:setPosition(vec3(base.x + rx * side + tx * along,
                       base.y + ry * side + ty * along, ground + height))
    assert(fd:createNewItem(d, m, 1), 'Forest item creation failed: ' .. shape)
    state.count = state.count + 1
  end
  for i, color in ipairs({'red', 'amber', 'green'}) do
    local side = (i - 2) * 3
    add('mast', side, 0, 0)
    add('head_dark', side, 0, manifest.cap_z)
    add('lens_' .. color, side, 0, manifest.cap_z)
  end
  add('beam_unit', -3, 3, 4.5 - manifest.beam_centre_z, 1)
  add('beam_unit', 3, 3, 4.5 - manifest.beam_centre_z, 5)
end
local ok, err = xpcall(run, debug.traceback)
local result = {ok = ok, error = err, items = state.count, expectedItems = 11,
  visualCheckRequired = true, note = 'Three posts: red, amber, green; two beams: same thickness, 1x and 5x length'}
if jsonWriteFile then jsonWriteFile('/temp/nl_forest_probe_result.json', result, true) end
if not ok then state.cleanup(); error(err) end
log('I', 'nlForestProbe', 'Created 11 diagnostic items. Remove with bneNlForestProbe.cleanup()')
