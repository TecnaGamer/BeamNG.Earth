-- The stock signal's arm reaches left from its pole. Reflect the player's own DAE for a left-kerb pole;
-- bake the reflection into positions/normals and winding instead of giving the engine a negative object scale.
-- No game asset is bundled. Both the static forest items and the switchable heads use this same derived shape.
local M = {}
M.STOCK = '/art/shapes/objects/s_trafficlight_boom_sn'
M.LEFT = '/temp/beamngEarth/shapes/s_trafficlight_boom_left_v1'

local function words(text)
  local out = {}
  for w in text:gmatch('%S+') do out[#out + 1] = w end
  return out
end
local function negate(s)
  assert(tonumber(s), 'non-numeric shape coordinate')
  return s:sub(1, 1) == '-' and s:sub(2) or '-' .. s:gsub('^%+', '')
end

function M.mirroredDae(dae)
  if type(dae) ~= 'string' or not dae:find('<COLLADA', 1, true)
    or not dae:find('instanced_trafficlight', 1, true) then return nil end
  local ok, result = pcall(function()
    -- This is the stock static, triangulated shape, not a general animated Collada importer.
    for _, tag in ipairs({ 'controller', 'animation', 'polylist', 'polygons', 'tristrips', 'trifans', 'lookat', 'skew' }) do
      assert(not dae:find('<' .. tag .. '[%s>]'), 'unsupported signal shape: ' .. tag)
    end
    local vectors, triangles = 0, 0
    dae = dae:gsub('(<source%s[^>]*>)(.-)(</source>)', function(open, body, close)
      if body:find('<param%s+name="X"') and body:find('<param%s+name="Y"') and body:find('<param%s+name="Z"') then
        body = body:gsub('(<float_array%s[^>]*>)(.-)(</float_array>)', function(a, text, b)
          local v = words(text)
          assert(#v > 0 and #v % 3 == 0, 'signal vectors are not triples')
          for i = 1, #v, 3 do v[i] = negate(v[i]) end
          vectors = vectors + 1
          return a .. table.concat(v, ' ') .. b
        end)
      end
      return open .. body .. close
    end)
    dae = dae:gsub('(<triangles%s[^>]*>)(.-)(</triangles>)', function(open, body, close)
      local stride = 0
      for offset in body:gmatch('<input%s[^>]-offset="(%d+)"') do stride = math.max(stride, tonumber(offset) + 1) end
      assert(stride > 0, 'signal triangles have no inputs')
      body = body:gsub('(<p>)(.-)(</p>)', function(a, text, b)
        local v = words(text)
        assert(#v % (3 * stride) == 0, 'incomplete signal triangle')
        for i = 1, #v, 3 * stride do
          for k = 0, stride - 1 do v[i + stride + k], v[i + 2 * stride + k] = v[i + 2 * stride + k], v[i + stride + k] end
        end
        triangles = triangles + 1
        return a .. table.concat(v, ' ') .. b
      end)
      return open .. body .. close
    end)
    -- Conjugate each node transform by the reflection, H T H. With reflected geometry this gives H T v,
    -- including the stock mesh's translated/rotated LOD nodes and its collision mesh.
    dae = dae:gsub('(<translate[^>]*>)(.-)(</translate>)', function(a, text, b)
      local v = words(text); assert(#v == 3, 'bad signal translation'); v[1] = negate(v[1])
      return a .. table.concat(v, ' ') .. b
    end)
    dae = dae:gsub('(<rotate[^>]*>)(.-)(</rotate>)', function(a, text, b)
      local v = words(text); assert(#v == 4, 'bad signal rotation'); v[2], v[3] = negate(v[2]), negate(v[3])
      return a .. table.concat(v, ' ') .. b
    end)
    dae = dae:gsub('(<matrix[^>]*>)(.-)(</matrix>)', function(a, text, b)
      local v = words(text); assert(#v == 16, 'bad signal matrix')
      for _, i in ipairs({ 2, 3, 4, 5, 9, 13 }) do v[i] = negate(v[i]) end
      return a .. table.concat(v, ' ') .. b
    end)
    assert(vectors > 0 and triangles > 0, 'signal mesh missing')
    return dae
  end)
  return ok and result or nil
end

function M.ensureLeft()
  if not FS or not readFile or not writeFile then return nil end
  local path = M.LEFT .. '.dae'
  if not FS:fileExists(path) then
    local text = M.mirroredDae(readFile(M.STOCK .. '.dae'))
    if not text then return nil end
    writeFile(path, text)
  end
  return FS:fileExists(path) and M.LEFT or nil
end
return M
