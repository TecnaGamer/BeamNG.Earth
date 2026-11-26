-- Snowy roads' meshes (winter W3, the server's BNE_WINTER; ~/bnge-tests/winterplan/PLAN.md 3.3, 3.4): the road skins
-- drawn with winter on carry winter copies of their paint, deck asphalt and sidewalk slabs per plow class / street kind
-- (server/snowroad.py: bne_snow_line_white_p3 ...) and snowbanks (bne_snowbank_p<class>); here those materials are
-- made (loadJsonMaterialsFile, once a level, before any skin that names them: the server sends such skins only after
-- the snowstate this module answers) and set from the snowstate's look:
--   paint (markings, crossings)  its opacity times the share that shows (none under a full cover, some under tracks)
--   decks                        a packed-snow layer over the stock asphalt at the deck's cover
--   sidewalks                    a snow layer by slot: shovelled, trampled or covered (main streets, residential
--                                frontages cleared or left, unmaintained)
--   banks                        white, grey or brown-black by their dirt bin
-- Loaded by winter.lua with the first snowstate that has a look (winter on); off, nothing here exists. Material
-- writes only on a change (a burst of setField / flush / reload, never per frame).
local M = {}

M.CLASSES = 5
M.ROOT = '/temp/beamngEarth/winter/'
M.SNOW_B = '/assets/materials/baked_props/snow/snow_d.dds'
M.SNOW_N = '/assets/materials/baked_props/snow/snow_n.dds'
M.PACKED = '/assets/materials/decalroad/wet/m_snow_road/'
M.WALK_SLOTS = { 's0', 's1c', 's1u', 's2' }
-- bank colour by dirt bin (white, grey, brown-black; plan 3.3 [R1]: banks go grey in days, near-black in weeks)
M.DIRT = { { 0.96, 0.97, 1.0, 1 }, { 0.70, 0.69, 0.68, 1 }, { 0.42, 0.38, 0.34, 1 } }
M.STEP = 0.05                -- values are written in steps of this (no churn from tiny changes)

-- The stock definitions copied (art/shapes/common/decalroads, east_coast_usa/art/road, west_coast_usa/art/shapes/roads
-- in BeamNG 0.40): the copies look exactly like the stock paint until the snow says otherwise.
local LINE = '/assets/materials/decalroad/lines/'
local function lineDef(colour)
  return { Stages = { { ambientOcclusionMap = LINE .. 'chalk_marking/t_line_white_ao.data.png',
      baseColorFactor = colour, baseColorMap = LINE .. 'line_white/t_line_white_b.color.png',
      normalMap = LINE .. 'line_white/t_line_white_nm.normal.png', opacityFactor = 0.7,
      opacityMap = LINE .. 'line_white/t_line_white_o.data.png',
      roughnessMap = LINE .. 'chalk_marking/t_line_white_r.data.png' },
    { baseColorFactor = { 0.992, 0.992, 0.992, 0.7 },
      baseColorMap = LINE .. 'line_white_discontinue/AsphaltRoad_variation_01_d.dds' }, {}, {} },
    alphaRef = 255, castShadows = false, translucent = true, translucentZWrite = true, version = 1.5,
    materialTag0 = 'RoadAndPath', materialTag1 = 'beamng' }
end
M.PAINT = {
  line_white = { def = function() return lineDef({ 0.992, 0.992, 0.992, 0.8 }) end, opacity = 0.7 },
  line_yellow = { def = function() return lineDef({ 0.941, 0.792, 0.275, 0.9 }) end, opacity = 0.7 },
  crossing_white = { opacity = 1.0, def = function()
    local C = LINE .. 'crossing_yellow/'
    return { Stages = { { ambientOcclusionMap = C .. 't_line_crossing_white_ao.data.png',
        baseColorMap = C .. 't_line_crossing_white_b.color.png', normalMap = C .. 't_line_crossing_white_nm.normal.png',
        opacityMap = C .. 't_line_crossing_white_o.data.png', roughnessFactor = 0.292, opacityFactor = 1.0 },
      {}, {}, {} }, alphaRef = 255, castShadows = false, translucent = true, translucentZWrite = true, version = 1.5,
      annotation = 'ZEBRA_CROSSING', materialTag0 = 'RoadAndPath', materialTag1 = 'beamng' }
  end },
}

local function deckDef()
  local A = '/assets/materials/tileable/road/road_asphalt_light/'
  return { Stages = { { ambientOcclusionMap = A .. 't_road_asphalt1_ao.data.png',
      baseColorFactor = { 0.837, 0.837, 0.837, 1 },
      baseColorMap = '/assets/materials/decalroad/asphalt/road_rubber_sticky/t_road_asphalt1_b.color.png',
      detailMap = A .. 'asphalt1_detail.data.png',
      detailNormalMap = '/assets/materials/tileable/road/road_asphalt/t_asphalt_track_nm.normal.png',
      detailScale = { 5, 5 }, normalMap = A .. 't_road_asphalt1_nm.normal.png',
      opacityMap = A .. 't_road_asphalt2_o.data.png',
      roughnessMap = '/assets/materials/decalroad/asphalt/road_rubber_sticky/t_asphalt1_r.data.png' },
    { baseColorMap = M.PACKED .. 't_snow_road_b.color.dds', normalMap = M.PACKED .. 't_snow_road_nm.normal.dds',
      roughnessMap = M.PACKED .. 't_snow_road_r.data.dds', opacityFactor = 0 }, {}, {} },
    activeLayers = 2, alphaRef = 150, annotation = 'ASPHALT', castShadows = false, dynamicCubemap = true,
    translucent = true, translucentZWrite = true, version = 1.5, materialTag0 = 'RoadAndPath', materialTag1 = 'beamng' }
end

local function walkDef()
  local S = '/assets/materials/tileable/concrete/sidewalk1/'
  return { Stages = { { ambientOcclusionMap = S .. 't_sidewalk1_ao.data.png',
      baseColorFactor = { 0.745, 0.745, 0.745, 1 }, baseColorMap = S .. 't_sidewalk1_b.color.png',
      metallicFactor = 0.7, metallicMap = S .. 't_sidewalk1_m.data.png', normalMap = S .. 't_sidewalk1_nm.normal.png',
      normalMapStrength = 1.2, pixelSpecular = true, roughnessMap = S .. 't_sidewalk1_r.data.png', vertColor = true },
    { baseColorMap = M.SNOW_B, normalMap = M.SNOW_N, roughnessFactor = 0.8, opacityFactor = 0 }, {}, {} },
    activeLayers = 2, alphaRef = 0, annotation = 'SIDEWALK', dynamicCubemap = true, groundType = 'CONCRETE',
    translucent = true, translucentBlendOp = 'None', version = 1.5, materialTag0 = 'beamng', materialTag1 = 'road' }
end

local function bankDef()
  return { Stages = { { baseColorMap = M.SNOW_B, normalMap = M.SNOW_N, roughnessFactor = 0.75,
      baseColorFactor = M.DIRT[1] }, {}, {}, {} }, groundType = 'SNOW', version = 1.5, annotation = 'TERRAIN',
    materialTag0 = 'beamng' }
end

function M.paintName(m, c) return string.format('bne_snow_%s_p%d', m, c) end
function M.deckName(c) return string.format('bne_snow_deck_p%d', c) end
function M.walkName(s) return 'bne_snow_walk_' .. s end
function M.bankName(c) return string.format('bne_snowbank_p%d', c) end

-- Pure: every winter road material (loadJsonMaterialsFile's table).
function M.materials()
  local out = {}
  local function add(name, def)
    def.name, def.mapTo, def.class = name, name, 'Material'
    out[name] = def
  end
  for c = 0, M.CLASSES - 1 do
    for m, p in pairs(M.PAINT) do add(M.paintName(m, c), p.def()) end
    add(M.deckName(c), deckDef())
    add(M.bankName(c), bankDef())
  end
  for _, s in ipairs(M.WALK_SLOTS) do add(M.walkName(s), walkDef()) end
  return out
end

local function q(v) return math.floor((tonumber(v) or 0) / M.STEP + 0.5) * M.STEP end

-- Pure: the material writes a look asks for: { {name, field, layer, value string}, ... } in a fixed order.
function M.targets(look)
  local out = {}
  if type(look) ~= 'table' then return out end
  for c = 0, M.CLASSES - 1 do
    local vis = q((look.marking or {})[c + 1] or 1)
    for _, m in ipairs({ 'crossing_white', 'line_white', 'line_yellow' }) do
      out[#out + 1] = { M.paintName(m, c), 'opacityFactor', 0, string.format('%.3f', M.PAINT[m].opacity * vis) }
    end
    out[#out + 1] = { M.deckName(c), 'opacityFactor', 1, string.format('%.3f', q((look.deck or {})[c + 1] or 0)) }
    local d = M.DIRT[math.max(1, math.min(#M.DIRT, (tonumber((look.dirt or {})[c + 1]) or 0) + 1))]
    out[#out + 1] = { M.bankName(c), 'baseColorFactor', 0, string.format('%.3f %.3f %.3f %.3f', d[1], d[2], d[3], d[4]) }
  end
  for i, s in ipairs(M.WALK_SLOTS) do
    local w = (look.walks or {})[i]
    local cover = type(w) == 'table' and (tonumber(w[2]) or 0) / 8 or 0
    out[#out + 1] = { M.walkName(s), 'opacityFactor', 1, string.format('%.3f', q(cover)) }
  end
  return out
end

local loaded = false
local written = {}            -- name|field|layer -> value
M.writes = 0

local function have(name)
  local st = rawget(_G, 'scenetree')
  return st and st.findObject and st.findObject(name) ~= nil
end

-- The materials made (once a level: a level load drops them): true when they exist.
function M.load()
  if loaded and have(M.paintName('line_white', 0)) then return true end
  local fs, rf, wf, lj = rawget(_G, 'FS'), rawget(_G, 'readFile'), rawget(_G, 'writeFile'), rawget(_G, 'loadJsonMaterialsFile')
  local je = rawget(_G, 'jsonEncode')
  if not (fs and rf and wf and lj and je) then return false end
  fs:directoryCreate(M.ROOT)
  local path = M.ROOT .. 'snowroad.materials.json'
  local text = je(M.materials())
  if rf(path) ~= text then wf(path, text) end
  lj(path)
  loaded, written = true, {}
  return true
end

-- Apply a look: only the writes whose value changed reach the engine. Returns the count.
function M.apply(look)
  if not M.load() then return 0 end
  local st = rawget(_G, 'scenetree')
  local n = 0
  local touched = {}
  for _, t in ipairs(M.targets(look)) do
    local key = t[1] .. '|' .. t[2] .. '|' .. t[3]
    if written[key] ~= t[4] then
      local mat = st and st.findObject and st.findObject(t[1])
      if mat then
        local ok = pcall(function() mat:setField(t[2], t[3], t[4]) end)
        if ok then
          written[key] = t[4]
          touched[t[1]] = mat
          n = n + 1
        end
      end
    end
  end
  for _, mat in pairs(touched) do
    pcall(function() mat:flush(); mat:reload() end)
  end
  M.writes = M.writes + n
  return n
end

-- winter.lua: a snowstate (its look: nil with winter's roads off on the server).
local lastLook = nil
function M.onState(msg)
  if type(msg) ~= 'table' or type(msg.look) ~= 'table' then return 0 end
  lastLook = msg.look
  return M.apply(msg.look)
end

-- winter.lua's once-a-second check: our values still on the materials? The engine can hand a shape that loads
-- later a fresh copy of a material from its definition (Windows run 2026-11-26: every write made, every value back
-- to the definition's once the skins loaded): one field of each kind is read back, and on a difference everything is
-- written again. A few getField calls a second; nothing without a look.
M.SENTINELS = { { 'bne_snow_line_white_p3', 'opacityFactor', 0 }, { 'bne_snow_deck_p3', 'opacityFactor', 1 },
  { 'bne_snow_walk_s1u', 'opacityFactor', 1 }, { 'bne_snowbank_p3', 'baseColorFactor', 0 } }
M.rewrites = 0
function M.check()
  if not lastLook or not loaded then return false end
  local st = rawget(_G, 'scenetree')
  for _, sn in ipairs(M.SENTINELS) do
    local want = written[sn[1] .. '|' .. sn[2] .. '|' .. sn[3]]
    local mat = st and st.findObject and st.findObject(sn[1])
    if want and mat then
      local ok, v = pcall(function() return mat:getField(sn[2], sn[3]) end)
      local a, b = tonumber(ok and tostring(v):match('^%S+') or ''), tonumber(want:match('^%S+'))
      if a and b and math.abs(a - b) > 0.002 then
        written = {}
        M.rewrites = M.rewrites + 1
        M.apply(lastLook)
        return true
      end
    elseif want and not mat then
      loaded = false
      M.apply(lastLook)
      return true
    end
  end
  return false
end

function M.reset()
  loaded, written, lastLook = false, {}, nil
end

function M._written() return written end      -- (tests)

return M
