-- Synthetic installed-level documents; no game files or proprietary data in offline tests.
return function(gc, options)
  options = options or {}
  local docs, files, lines = {}, {}, {}
  for source, paths in pairs(gc.SOURCES) do
    docs[paths.materials], lines[source] = {}, {}
    files[paths.materials] = paths.materials
  end
  local seen = {}
  for _, d in ipairs(gc.DEFS) do
    for _, c in ipairs(d.alternatives or { { d.source, d.src } }) do
      local source, src = c[1], c[2]
      local key = source .. '/' .. src
      if not seen[key] then
        seen[key] = true
        local token = '{"class":"GroundCover","name":"' .. key .. '"}'
        lines[source][#lines[source] + 1] = token
        docs[token] = { name = src, class = 'GroundCover', material = 'm_' .. src, maxElements = 1000,
          radius = 50, dissolveRadius = 30, seed = 9, useNewPlacementGpu = true,
          Types = { { layer = 'Grass', sizeMin = 0.15, sizeMax = 0.25, billboardUVs = { 0, 0, 1, 1 } } } }
        docs[gc.SOURCES[source].materials]['m_' .. src] = { name = 'm_' .. src, mapTo = 'm_' .. src,
          version = 1.5, alphaTest = true, Stages = { { baseColorMap = '/textures/' .. source .. '.png' } } }
      end
    end
  end
  for source, paths in pairs(gc.SOURCES) do files[paths.items] = table.concat(lines[source], '\n') end
  _G.readFile = function(path)
    for source, paths in pairs(gc.SOURCES) do
      if options.missingSource == source and (path == paths.items or path == paths.materials) then return nil end
    end
    return files[path]
  end
  _G.jsonDecode = function(text) return docs[text] end
  return docs, files
end
