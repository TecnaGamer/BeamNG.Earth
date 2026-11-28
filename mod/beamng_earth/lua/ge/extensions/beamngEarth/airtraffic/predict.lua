-- From devo343's Live Air Traffic add-on (with permission; CREDITS.md), moved into BeamNG.Earth for the native
-- live air traffic (beamngEarth/airtraffic.lua).
-- Live Air Traffic: the pure parts (no engine calls), so they can be tested outside the game.
--   classify(type, category)   what class of aircraft it is (its sounds and behaviour)
--   model(type, kind, hex)     which airframe it is drawn as
--   base(a, now)               an ADS-B row (the companion's compact form) as a prediction base
--   predict(b, now)            where that aircraft is now: dead reckoning along speed, track, turn rate and climb rate
--   contrailAltFt(tempC)       the lowest altitude contrails form at, from the surface temperature
local M = {}

local R_EARTH = 6371008.8
local KT = 0.514444            -- m/s per knot
local FT = 0.3048

M.MAX_EXTRAP_S = 120           -- an aircraft is moved along for at most this long after its last position
M.TURN_EXTRAP_S = 25           -- a turn is continued this long, then it flies straight (it rolls out sometime)
M.CLIMB_EXTRAP_S = 60          -- a climb / descent is continued this long (or until the selected altitude)
M.GROUND_EXTRAP_S = 15         -- taxiing aircraft: their turns are unknown, so not far

------------------------------------------------------------------------------------------ classification
local SETS = {
  heli = { 'EC20', 'EC25', 'EC30', 'EC35', 'EC45', 'EC55', 'EC75', 'H160', 'H175', 'AS32', 'AS3B', 'AS50', 'AS55',
    'AS65', 'A109', 'A119', 'A129', 'A139', 'A149', 'A169', 'A189', 'AW09', 'B06', 'B06T', 'B105', 'BO05', 'B212',
    'B222', 'B230', 'B407', 'B412', 'B427', 'B429', 'B430', 'B505', 'BK17', 'R22', 'R44', 'R66', 'S61', 'S64', 'S70',
    'S76', 'S92', 'H47', 'H53', 'H60', 'H64', 'UH1', 'UH1Y', 'MI8', 'MI17', 'MI24', 'MI26', 'H500', 'MD52', 'MD60',
    'EXPL', 'NH90', 'LYNX', 'PUMA', 'GAZL', 'ALO3', 'A600', 'EN28', 'EN48', 'CABR', 'V22' },
  jumbo = { 'A124', 'A225', 'B741', 'B742', 'B743', 'B744', 'B748', 'B74D', 'B74R', 'B74S', 'BLCF', 'A388', 'A389',
    'A38F' },
  heavy = { 'A306', 'A30B', 'A310', 'A332', 'A333', 'A337', 'A338', 'A339', 'A342', 'A343', 'A345', 'A346', 'A359',
    'A35K', 'B762', 'B763', 'B764', 'B772', 'B773', 'B77L', 'B77W', 'B778', 'B779', 'B788', 'B789', 'B78X', 'MD11',
    'DC10', 'L101', 'IL96', 'IL86', 'C5M', 'C5', 'C17', 'KC46', 'K35R', 'E3TF', 'E3CF', 'E6', 'IL76', 'A3ST',
    'MRTT', 'K46', 'B703', 'B701', 'DC85', 'DC86', 'DC87', 'DC8', 'VC10', 'KC10', 'B52' },
  narrow = { 'A318', 'A319', 'A320', 'A321', 'A19N', 'A20N', 'A21N', 'B731', 'B732', 'B733', 'B734', 'B735', 'B736',
    'B737', 'B738', 'B739', 'B37M', 'B38M', 'B39M', 'B3XM', 'B752', 'B753', 'B721', 'B722', 'BCS1', 'BCS3', 'C919',
    'E170', 'E175', 'E75L', 'E75S', 'E190', 'E195', 'E290', 'E295', 'SU95', 'T204', 'A21X', 'P8', 'E737', 'ARJ2',
    'B461', 'B462', 'B463', 'RJ70', 'RJ85', 'RJ1H', 'C40', 'MC23' },
  regional = { 'CRJ1', 'CRJ2', 'CRJ7', 'CRJ9', 'CRJX', 'E135', 'E145', 'E35L', 'E45X', 'F28', 'F70', 'F100', 'B712',
    'DC91', 'DC92', 'DC93', 'DC94', 'DC95', 'MD81', 'MD82', 'MD83', 'MD87', 'MD88', 'MD90', 'T134', 'YK42' },
  bizjet = { 'C25A', 'C25B', 'C25C', 'C25M', 'C500', 'C501', 'C510', 'C525', 'C526', 'C550', 'C551', 'C55B', 'C560',
    'C56X', 'C650', 'C680', 'C68A', 'C700', 'C750', 'CL30', 'CL35', 'CL60', 'GL5T', 'GL7T', 'GLEX', 'GLF2', 'GLF3',
    'GLF4', 'GLF5', 'GLF6', 'G150', 'G200', 'G280', 'GA5C', 'GA6C', 'GA7C', 'GA8C', 'GALX', 'ASTR', 'LJ23', 'LJ24',
    'LJ25', 'LJ31', 'LJ35', 'LJ40', 'LJ45', 'LJ55', 'LJ60', 'LJ70', 'LJ75', 'FA10', 'FA20', 'FA50', 'FA6X', 'FA7X',
    'FA8X', 'F2TH', 'F900', 'E50P', 'E55P', 'E545', 'E550', 'PRM1', 'H25A', 'H25B', 'H25C', 'HDJT', 'PC24', 'SF50',
    'EA50', 'BE40', 'WW24', 'J328', 'C25D', 'BE4W', 'E35X' },
  turboprop = { 'AT43', 'AT44', 'AT45', 'AT46', 'AT72', 'AT73', 'AT75', 'AT76', 'ATP', 'DH8A', 'DH8B', 'DH8C', 'DH8D',
    'B190', 'BE20', 'BE30', 'BE9L', 'BE9T', 'B350', 'BE99', 'BE10', 'SF34', 'SB20', 'JS31', 'JS32', 'JS41', 'PC12',
    'C208', 'D228', 'D328', 'DHC6', 'DHC7', 'SW3', 'SW4', 'E110', 'E120', 'L410', 'F27', 'F50', 'AN24', 'AN26',
    'AN28', 'AN30', 'AN32', 'C130', 'C30J', 'P180', 'PAY1', 'PAY2', 'PAY3', 'PAY4', 'C441', 'C425', 'MU2', 'TBM7',
    'TBM8', 'TBM9', 'PC6T', 'KODI', 'CN35', 'C295', 'P3', 'E2', 'C27J', 'DHC5', 'Y12', 'MA60', 'M28', 'PC21', 'T6',
    'AT8T', 'AT5T', 'AT6T', 'AT3T', 'C406', 'F406', 'DC3T', 'G222', 'B200', 'SC7', 'BN2T', 'P46T', 'EPIC',
    'A400', 'AN12', 'AN22', 'L188', 'P3', 'PC7', 'PC9', 'M600', 'TUCA', 'T34T' },
  fighter = { 'F14', 'F15', 'F15E', 'F16', 'F18', 'F18H', 'F18S', 'FA18', 'F22', 'F35', 'F4', 'F5', 'F117', 'T38',
    'T7', 'A10', 'AV8B', 'HAR', 'T45', 'HAWK', 'L39', 'L159', 'M346', 'EUFI', 'RFAL', 'GRIF', 'TOR', 'MIR2', 'MIRA',
    'SU24', 'SU25', 'SU27', 'SU30', 'SU34', 'SU35', 'SU57', 'MG29', 'MG31', 'J10', 'JF17', 'TEJ', 'A4', 'ALPH',
    'MB39', 'K8', 'T50', 'KF21', 'F1', 'JAGR', 'B1', 'B2' },
  glider = { 'GLID', 'ASK2', 'AS21', 'AS33', 'ASK1', 'DG1T', 'DG80', 'LS8', 'DISC', 'DUO', 'ARCP', 'JANU', 'NIMB',
    'VENT', 'G103', 'G109', 'SZD5', 'PIK2', 'LAK1', 'LAK7', 'SF25', 'GROB' },
  balloon = { 'BALL', 'SHIP', 'GBAL' },
}
local LOOKUP = {}
for kind, list in pairs(SETS) do
  for _, t in ipairs(list) do LOOKUP[t] = kind end
end
-- prefixes for families not listed one by one (checked after the exact table)
local PREFIX = {
  { '^B74', 'jumbo' }, { '^A38', 'jumbo' },
  { '^A33', 'heavy' }, { '^A34', 'heavy' }, { '^A35', 'heavy' }, { '^B76', 'heavy' }, { '^B77', 'heavy' },
  { '^B78', 'heavy' },
  { '^A31', 'narrow' }, { '^A32', 'narrow' }, { '^B73', 'narrow' }, { '^B75', 'narrow' }, { '^E19', 'narrow' },
  { '^E29', 'narrow' }, { '^E17', 'narrow' },
  { '^CRJ', 'regional' }, { '^MD8', 'regional' }, { '^DC9', 'regional' },
  { '^C5%d', 'bizjet' }, { '^C6%d', 'bizjet' }, { '^C25', 'bizjet' }, { '^GLF', 'bizjet' }, { '^LJ', 'bizjet' },
  { '^FA%d', 'bizjet' }, { '^CL[36]', 'bizjet' }, { '^GL', 'bizjet' },
  { '^AT[4-7]', 'turboprop' }, { '^DH8', 'turboprop' }, { '^TBM', 'turboprop' }, { '^PAY', 'turboprop' },
  { '^EC%d', 'heli' }, { '^AS[35]', 'heli' }, { '^R[24]', 'heli' },
  { '^SU%d', 'fighter' }, { '^MG%d', 'fighter' }, { '^F1[4-8]', 'fighter' },
}
-- (A6 is "high performance": fighters and fast jet trainers; B1 gliders, B2 lighter than air, B4 ultralights,
-- B6 drones)
local BY_CATEGORY = { A1 = 'light', A2 = 'turboprop', A3 = 'narrow', A4 = 'narrow', A5 = 'heavy', A6 = 'fighter',
  A7 = 'heli', B1 = 'glider', B2 = 'balloon', B4 = 'light', B6 = 'light' }

-- The model for an ICAO type designator (A320, B77W, C172 ...) and ADS-B emitter category (A1..A7, B1..): one of
-- jumbo, heavy, narrow, regional, bizjet, turboprop, light, heli, fighter, glider, balloon.
function M.classify(t, cat)
  t = type(t) == 'string' and t:upper() or ''
  cat = type(cat) == 'string' and cat:upper() or ''
  if cat == 'A7' then return 'heli' end
  if t ~= '' then
    if LOOKUP[t] then return LOOKUP[t] end
    for _, p in ipairs(PREFIX) do
      if t:find(p[1]) then return p[2] end
    end
  end
  if BY_CATEGORY[cat] then return BY_CATEGORY[cat] end
  if t ~= '' then return 'light' end   -- a type we do not know: most of those are small piston aircraft
  return nil                           -- nothing to go by: the caller decides from speed and altitude
end

------------------------------------------------------------------------------------------ airframes
-- The airframe an aircraft is drawn as (a shape set lat_<model>*.dae): the layout of its type, wings and engines
-- where they are on the real thing. Types not listed get their class's usual layout.
local MODEL_SETS = {
  a380 = { 'A388', 'A389', 'A38F' },
  quadjet = { 'A342', 'A343', 'A345', 'A346', 'A340', 'IL96', 'IL86', 'B703', 'B701', 'DC85', 'DC86', 'DC87', 'DC8',
    'VC10', 'E3TF', 'E3CF', 'E6', 'K35R', 'K35E', 'C135', 'R135', 'E8' },
  quad_high = { 'C17', 'IL76', 'A124', 'A225', 'C5M', 'C5', 'Y20', 'B52' },
  quad_regional = { 'B461', 'B462', 'B463', 'RJ70', 'RJ85', 'RJ1H' },
  trijet = { 'MD11', 'DC10', 'L101', 'KC10' },
  trijet_rear = { 'B721', 'B722', 'B72F', 'T154', 'YK42', 'T134' },
  trijet_biz = { 'FA50', 'FA7X', 'FA8X', 'F900', 'FA90' },
  twinprop_low = { 'BE20', 'BE30', 'B350', 'BE9L', 'BE9T', 'BE10', 'BE99', 'B190', 'C441', 'C425', 'PAY1', 'PAY2',
    'PAY3', 'PAY4', 'SF34', 'JS31', 'JS32', 'JS41', 'E120', 'E110', 'SW3', 'SW4', 'F406', 'C406', 'B200', 'P180',
    'C212' },
  single_tp = { 'PC12', 'TBM7', 'TBM8', 'TBM9', 'PC21', 'T6', 'EPIC', 'P46T', 'PC7', 'PC9', 'M600', 'TUCA', 'AT3T',
    'AT5T', 'AT6T', 'AT8T', 'T34T', 'EMB3' },
  quad_turboprop = { 'C130', 'C30J', 'A400', 'P3', 'AN12', 'AN22', 'L188', 'DHC7', 'AN70', 'L100' },
  light = { 'C150', 'C152', 'C162', 'C170', 'C172', 'C175', 'C177', 'C180', 'C182', 'C185', 'C206', 'C207', 'C208',
    'C210', 'P210', 'T210', 'KODI', 'PC6T', 'PA18', 'J3', 'CH7A', 'BL8', 'DHC2', 'DHC3', 'GA8', 'M7', 'HUSK', 'C140',
    'C120', 'PA22', 'P92', 'CTSW', 'A210', 'SAVG' },
  light_low = { 'P28A', 'P28B', 'P28R', 'P28T', 'P28U', 'P32R', 'P32T', 'PA32', 'PA46', 'PA24', 'PA38', 'SR20', 'SR22',
    'S22T', 'SR22T', 'BE33', 'BE35', 'BE36', 'BE23', 'BE24', 'BE19', 'M20P', 'M20T', 'M20J', 'M20K', 'DA40', 'DA20',
    'DA50', 'RV6', 'RV7', 'RV8', 'RV9', 'RV10', 'RV12', 'RV14', 'TB10', 'TB20', 'TB21', 'C350', 'C400', 'COL3',
    'COL4', 'AA5', 'AA1', 'GLAS', 'G115', 'TOBA', 'EV97', 'SIRA', 'CRUZ' },
  twin_piston = { 'PA34', 'BE58', 'BE55', 'BE56', 'BE95', 'BE76', 'PA44', 'PA31', 'PA30', 'PA23', 'PA27', 'C310',
    'C320', 'C335', 'C340', 'C414', 'C421', 'C402', 'C404', 'C303', 'DA42', 'DA62', 'PA60', 'AEST', 'P68', 'BN2P',
    'BE60', 'GA7' },
  heli_twin = { 'S76', 'S92', 'S61', 'H60', 'UH60', 'S70', 'A139', 'A169', 'A189', 'AW09', 'EC45', 'EC55', 'EC75',
    'EC25', 'H160', 'H175', 'B212', 'B412', 'UH1Y', 'MI8', 'MI17', 'MI24', 'MI26', 'NH90', 'EH10', 'H53', 'S64',
    'AS32', 'AS65', 'BK17', 'PUMA', 'LYNX', 'V22', 'B429', 'B230', 'B430', 'A149', 'KA32' },
  heli_tandem = { 'H47', 'CH47', 'H46', 'CH46' },
}
local MODEL_OF = {}
for model, list in pairs(MODEL_SETS) do
  for _, t in ipairs(list) do MODEL_OF[t] = model end
end
M.MODEL_OF = MODEL_OF
local KIND_MODEL = { jumbo = 'jumbo', heavy = 'heavy', narrow = 'narrow', regional = 'regional', bizjet = 'bizjet',
  turboprop = 'turboprop', light = 'light', heli = 'heli', fighter = 'fighter', glider = 'glider',
  balloon = 'balloon' }

local function hashStr(s)
  local h = 5381
  for i = 1, #s do h = (h * 33 + s:byte(i)) % 4294967296 end
  return h
end

-- type: ICAO type designator ('' unknown); kind: from classify; hex: the airframe's address (an unknown small
-- aircraft is a high-wing or a low-wing single, half and half, always the same one for the same aircraft).
function M.model(t, kind, hex)
  t = type(t) == 'string' and t:upper() or ''
  local m = MODEL_OF[t]
  if m then
    -- (a type listed as a light single but classed by its category as something bigger keeps its class's layout)
    return m
  end
  if kind == 'light' then
    return (hashStr(type(hex) == 'string' and hex or t) % 2 == 0) and 'light' or 'light_low'
  end
  return KIND_MODEL[kind] or 'narrow'
end

function M.guessKind(altFt, gsKt)
  if (gsKt or 0) > 330 or (altFt or 0) > 24000 then return 'narrow' end
  if (gsKt or 0) > 200 then return 'turboprop' end
  return 'light'
end

------------------------------------------------------------------------------------------ dead reckoning
local function num(v) v = tonumber(v) if v and v == v and v ~= math.huge and v ~= -math.huge then return v end end

-- An aircraft row from the companion as a prediction base, its position valid at now - a.age.
function M.base(a, now)
  local ground = a.g == true
  local altFt = num(a.ag) or num(a.ab)                  -- geometric (over the ellipsoid) first: the game's heights
  local baroFt = num(a.ab) or num(a.ag)
  local gs = num(a.gs) or 0
  local trk = num(a.trk) or num(a.hdg) or num(a.mhdg)
  return {
    lat = num(a.lat), lon = num(a.lon),
    altFt = ground and 0 or (altFt or 10000), baroFt = baroFt,
    ground = ground,
    gs = gs,
    trk = trk or 0, hasTrk = trk ~= nil,
    tr = num(a.tr) or 0,
    vs = num(a.vsg) or num(a.vs) or 0,
    sel = num(a.sel),
    t0 = now - math.max(0, num(a.age) or 0),
  }
end

local function wrap360(a) a = a % 360 if a < 0 then a = a + 360 end return a end
M.wrap360 = wrap360

-- Move a point distM metres along trkDeg (spherical earth, fine over a few km per step).
function M.move(lat, lon, trkDeg, distM)
  local tr = math.rad(trkDeg)
  local dLat = distM * math.cos(tr) / R_EARTH
  local cl = math.cos(math.rad(lat))
  if math.abs(cl) < 1e-6 then cl = 1e-6 end
  local dLon = distM * math.sin(tr) / (R_EARTH * cl)
  lat = lat + math.deg(dLat)
  lon = lon + math.deg(dLon)
  if lon > 180 then lon = lon - 360 elseif lon < -180 then lon = lon + 360 end
  return math.max(-89.9999, math.min(89.9999, lat)), lon
end

-- Where base b is at time now: lat, lon, altFt, trackDeg, plus the climb rate (ft/min) and turn rate (deg/s) in use.
function M.predict(b, now)
  local dt = math.max(0, math.min(M.MAX_EXTRAP_S, now - b.t0))
  local lat, lon, trk = b.lat, b.lon, b.trk
  local gsMs = b.gs * KT
  if b.ground then
    if b.gs >= 2 then
      local d = math.min(dt, M.GROUND_EXTRAP_S)
      lat, lon = M.move(lat, lon, trk, gsMs * d)
    end
    return lat, lon, 0, trk, 0, 0
  end
  local turnT = math.min(dt, M.TURN_EXTRAP_S)
  local tr = b.tr
  if math.abs(tr) > 6 then tr = (tr > 0 and 6 or -6) end   -- (a bad value: no aircraft turns that fast)
  if math.abs(tr) > 0.05 and gsMs > 1 then
    -- the turn, in steps of at most 2 s, then straight on
    local t, step = 0, 2
    while t < turnT do
      local h = math.min(step, turnT - t)
      lat, lon = M.move(lat, lon, trk + tr * h * 0.5, gsMs * h)
      trk = trk + tr * h
      t = t + h
    end
    if dt > turnT then lat, lon = M.move(lat, lon, trk, gsMs * (dt - turnT)) end
  elseif gsMs > 0 then
    lat, lon = M.move(lat, lon, trk, gsMs * dt)
  end
  local climbT = math.min(dt, M.CLIMB_EXTRAP_S)
  local alt = b.altFt + b.vs / 60 * climbT
  if b.sel and b.sel > 0 then
    -- level off at the altitude selected on the autopilot (the selection is barometric: compare like with like)
    local off = (b.baroFt and (b.altFt - b.baroFt)) or 0
    local selAlt = b.sel + off
    if b.vs > 0 and b.altFt < selAlt and alt > selAlt then alt = selAlt end
    if b.vs < 0 and b.altFt > selAlt and alt < selAlt then alt = selAlt end
  end
  alt = math.max(-1500, alt)
  return lat, lon, alt, wrap360(trk), (climbT < M.CLIMB_EXTRAP_S) and b.vs or 0, (dt < M.TURN_EXTRAP_S) and tr or 0
end

-- Shortest signed difference a - b in degrees (-180..180).
function M.angleDiff(a, b)
  local d = (a - b) % 360
  if d > 180 then d = d - 360 end
  return d
end

-- 0..1 smooth step
function M.smooth(x)
  if x <= 0 then return 0 elseif x >= 1 then return 1 end
  return x * x * (3 - 2 * x)
end

-- Rough distance in metres between two points (equirectangular: plenty for "which is nearer").
function M.distM(lat1, lon1, lat2, lon2)
  local x = math.rad(lon2 - lon1) * math.cos(math.rad((lat1 + lat2) * 0.5))
  local y = math.rad(lat2 - lat1)
  return math.sqrt(x * x + y * y) * R_EARTH
end

------------------------------------------------------------------------------------------ contrails
-- Contrails form where the air is colder than about -40 C (the Schmidt-Appleman criterion for modern turbofans,
-- roughly). Estimated from the surface temperature with the standard lapse rate (1.98 C per 1000 ft) up to the
-- tropopause (36,089 ft, -56.5 C in the standard atmosphere): a hot day pushes the threshold up, a cold day down.
-- Clamped to 24,000 .. 34,000 ft; with no temperature known: 27,000 ft (about the standard atmosphere's answer).
M.CONTRAIL_DEFAULT_FT = 27000
function M.contrailAltFt(tempC)
  tempC = num(tempC)
  if not tempC then return M.CONTRAIL_DEFAULT_FT end
  local h = (tempC + 40) / 0.00198
  return math.max(24000, math.min(34000, h))
end

-- Contrails from the real weather (BeamNG.Earth's): the Schmidt-Appleman criterion (Schumann 1996) for the level
-- where a jet's exhaust first freezes, with the standard atmosphere's temperature and pressure from the weather's
-- sea-level temperature and surface pressure, and a humidity aloft guessed from the high cloud (cirrus means moist
-- air up there). The threshold at liquid saturation:
--   G = EI cp p / (eps Q (1 - eta))   [Pa/K]   (EI 1.25, cp 1004, eps 0.622, Q 43.2 MJ/kg, eta 0.3)
--   T_LM = -46.46 + 9.43 ln(G - 0.053) + 0.72 ln(G - 0.053)^2   [C]
-- and a little colder in drier air (about 4 K colder in bone-dry air: SA_DRY_K). Returns feet; above SA_TOP_M (no
-- level reaches the threshold: a hot tropical day) only the highest jets would make one: SA_NONE_FT.
M.SA_DRY_K = 4.0
M.SA_BOTTOM_M, M.SA_TOP_M, M.SA_STEP_M = 5000, 14000, 100
M.SA_NONE_FT = 45000
local function isaTemp(slT, h)
  -- the tropopause rises on warm days (about 11 km at 15 C, up to ~16 km in the tropics)
  local trop = 11000 + math.max(0, math.min(5000, (slT - 15) * 250))
  return slT - 0.0065 * math.min(h, trop)
end
local function isaPressure(pSl, h)
  if h <= 11000 then return pSl * (1 - 2.25577e-5 * h) ^ 5.25588 end
  return pSl * 0.22336 * math.exp(-(h - 11000) / 6341.6)
end
function M.saThresholdC(pPa, rh)
  local G = 1.25 * 1004 * pPa / (0.622 * 43.2e6 * 0.7)
  local x = math.log(math.max(0.06, G - 0.053))
  local tlm = -46.46 + 9.43 * x + 0.72 * x * x
  return tlm - M.SA_DRY_K * (1 - math.max(0, math.min(1, rh or 0.5)))
end
-- slTempC: sea-level temperature (C); rh: 0..1 humidity aloft; pressureHpa: surface pressure; elevM: the ground's
-- height (the surface pressure is there: back to sea level).
function M.contrailFormation(slTempC, rh, pressureHpa, elevM)
  local slT = num(slTempC)
  if not slT then return M.CONTRAIL_DEFAULT_FT end
  local pSl = (num(pressureHpa) or 1013.25) * 100
  local elev = num(elevM) or 0
  if elev > 0 then pSl = pSl / (1 - 2.25577e-5 * math.min(elev, 5000)) ^ 5.25588 end
  pSl = math.max(85000, math.min(108000, pSl))
  for h = M.SA_BOTTOM_M, M.SA_TOP_M, M.SA_STEP_M do
    if isaTemp(slT, h) <= M.saThresholdC(isaPressure(pSl, h), rh) then return h / FT end
  end
  return M.SA_NONE_FT
end
-- The humidity aloft (0..1) from the high and mid cloud cover (%): dry 0.35 with none, near saturation under cirrus.
function M.humidityAloft(highPct, midPct)
  local hi = math.max(0, math.min(100, num(highPct) or 0)) / 100
  local mid = math.max(0, math.min(100, num(midPct) or 0)) / 100
  return math.min(1, 0.35 + 0.6 * hi + 0.1 * mid)
end
-- How long contrails last next to the default lifetime: short-lived in dry air (they sublimate), persistent and
-- spreading where the air aloft is ice-supersaturated (high cloud about): 0.3 .. 2.
function M.contrailLife(highPct, rh)
  local hi = math.max(0, math.min(100, num(highPct) or 0))
  local wet = M.smooth((hi - 10) / 60) * 0.7 + M.smooth(((rh or 0.5) - 0.5) / 0.4) * 0.3
  return 0.3 + 1.7 * wet
end

M.KT, M.FT = KT, FT
-- Where base b was or will be at time t, earlier than its position too (flown backwards along its track, up to
-- 120 s): the sound heard now left the aircraft distance / 343 s ago.
M.MAX_BACK_S = 120
function M.predictAt(b, t)
  if t >= b.t0 then return M.predict(b, t) end
  local back = math.min(M.MAX_BACK_S, b.t0 - t)
  if b.ground and b.gs < 2 then return b.lat, b.lon, 0, b.trk end
  local lat, lon = M.move(b.lat, b.lon, b.trk, -b.gs * KT * back)
  local alt = b.ground and 0 or math.max(-1500, b.altFt - b.vs / 60 * math.min(back, M.CLIMB_EXTRAP_S))
  return lat, lon, alt, b.trk
end

return M
