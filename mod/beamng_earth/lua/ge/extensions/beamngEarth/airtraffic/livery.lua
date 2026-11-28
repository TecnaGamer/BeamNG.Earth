-- From devo343's Live Air Traffic add-on (with permission; CREDITS.md), moved into BeamNG.Earth for the native
-- live air traffic (beamngEarth/airtraffic.lua).
-- Live Air Traffic: how each aircraft looks: its colour scheme (tail, winglets, stripe, engine covers: one of the
-- PALETTES in tools/build_aircraft_models.py, by airline) and its length (a stretched or shortened version of its
-- model, by ICAO type), and the text painted on it (flight number, registration). Pure (no engine calls).
local M = {}

M.PALETTES = 16
M.PRIVATE = { 15, 2, 9, 12, 13, 5 }       -- schemes private aircraft get (white with a stripe, or plain colours)

-- Airlines (ICAO callsign prefix) given a scheme in their general colours. Others get one picked from their prefix,
-- so every aircraft of an airline matches.
M.AIRLINES = {
  DAL = 0, AAL = 1, UAL = 2, SWA = 3, JBU = 4, FFT = 5, NKS = 6, FDX = 7, UPS = 8, ACA = 9, ASA = 10, WJA = 10,
  ENY = 1, PDT = 1, PSA = 1, EDV = 0, SKW = 12, RPA = 2, GJS = 2, AWI = 2, ASH = 2, QXE = 10, HAL = 14, SCX = 13,
  AAY = 11, MXY = 11, BAW = 0, VIR = 9, DLH = 4, AFR = 4, KLM = 11, UAE = 9, QTR = 14, EIN = 5, IBE = 9, SAS = 2,
  AAR = 12, KAL = 11, JAL = 9, ANA = 4, CPA = 10, SIA = 4, THY = 9, ETD = 8, QFA = 9, ACA2 = 9, AMX = 4, VOI = 14,
  GTI = 4, ABX = 12, CKS = 12, ATN = 8, RYR = 4, EZY = 13, WZZ = 7, TAP = 5, SWR = 9, AUA = 9, FIN = 11,
}

local function hash(s)
  local h = 0
  for i = 1, #s do h = (h * 31 + s:byte(i)) % 1000003 end
  return h
end

-- The airline prefix of a callsign (DAL1234 -> DAL), or nil (a registration, N123AB, or nothing).
function M.airline(callsign)
  if type(callsign) ~= 'string' then return nil end
  local p = callsign:match('^(%u%u%u)%d')
  return p
end

-- The colour scheme (0 .. PALETTES-1) for an aircraft: its airline's, else one from its identity.
function M.palette(callsign, reg, hex)
  local al = M.airline(callsign)
  if al then
    if M.AIRLINES[al] then return M.AIRLINES[al] end
    return hash(al) % (M.PALETTES - 1)
  end
  local key = (reg and reg ~= '' and reg) or (hex or '?')
  return M.PRIVATE[hash(key) % #M.PRIVATE + 1]
end

-- Length of a type next to its model's (1: the model as built). Shorter and longer versions of each family.
M.STRETCH = {
  A318 = 0.82, A319 = 0.88, A320 = 1.0, A20N = 1.0, A321 = 1.17, A21N = 1.17, A19N = 0.88,
  B736 = 0.83, B737 = 0.88, B738 = 1.0, B739 = 1.05, B37M = 0.94, B38M = 1.0, B39M = 1.05, B3XM = 1.1,
  B752 = 1.24, B753 = 1.42, BCS1 = 0.92, BCS3 = 1.0, E170 = 0.78, E175 = 0.83, E75L = 0.83, E75S = 0.83,
  E190 = 0.95, E195 = 1.02, E290 = 0.95, E295 = 1.07, MD90 = 1.15,
  CRJ2 = 0.82, CRJ7 = 1.0, CRJ9 = 1.12, CRJX = 1.2, E135 = 0.82, E145 = 0.92, MD82 = 1.38, MD83 = 1.38, MD88 = 1.38,
  B712 = 1.15,
  B762 = 0.76, B763 = 0.86, B764 = 0.96, B772 = 0.95, B77L = 0.95, B773 = 1.16, B77W = 1.16, B778 = 1.11,
  B779 = 1.19, B788 = 0.89, B789 = 0.98, B78X = 1.07, A332 = 0.92, A333 = 1.0, A338 = 0.92, A339 = 1.0,
  A359 = 1.04, A35K = 1.16, A306 = 0.85, MD11 = 0.95,
  B744 = 1.0, B748 = 1.07, A388 = 1.03,
  AT45 = 0.84, AT72 = 1.0, AT76 = 1.0, DH8A = 0.8, DH8B = 0.8, DH8C = 0.94, DH8D = 1.2, SF34 = 0.73,
  C25A = 0.75, C25B = 0.8, C560 = 0.82, C56X = 0.9, C68A = 0.95, C700 = 1.0, GLF4 = 1.1, GLF5 = 1.2, GLF6 = 1.25,
  GLEX = 1.2, GL7T = 1.35, CL60 = 1.03, E55P = 0.8, E50P = 0.65, PC24 = 0.85,
  C172 = 1.0, C152 = 0.88, C182 = 1.04, C206 = 1.08, PA28 = 0.88, SR22 = 0.95, DA40 = 0.98, BE36 = 1.0,
}
function M.stretch(t)
  return M.STRETCH[t] or 1.0
end

-- What is painted on it: the flight number along the forward fuselage (airline flights only, where airline titles
-- go) and the registration on the rear fuselage (on private aircraft: large, the way they are painted). Characters
-- the glyph set has no shape for are left out.
function M.texts(kind, callsign, reg, glyphs)
  local function clean(s)
    if type(s) ~= 'string' then return '' end
    s = s:upper():gsub('%s+', '')
    local out = {}
    for ch in s:gmatch('.') do if glyphs[ch] then out[#out + 1] = ch end end
    return table.concat(out)
  end
  local cs, rg = clean(callsign), clean(reg)
  local list = {}
  if kind == 'fighter' or kind == 'balloon' then return {} end     -- (military markings / a balloon's own art)
  local airliner = kind ~= 'light' and kind ~= 'heli' and kind ~= 'glider'
  if airliner and M.airline(cs) then
    list[#list + 1] = { text = cs, role = 'title' }
  end
  if rg ~= '' and rg ~= cs then
    list[#list + 1] = { text = rg, role = (kind == 'light' or kind == 'heli' or kind == 'glider') and 'regBig' or 'reg' }
  elseif not airliner and cs ~= '' then
    list[#list + 1] = { text = cs, role = 'regBig' }      -- (a private aircraft broadcasting its registration)
  end
  return list
end

-- Where a text goes on a model (fractions of its length L and fuselage radius R): centre along the fuselage, height
-- of its middle above the fuselage axis, and letter height.
M.LAYOUT = {
  title  = { y = 0.20, z = 0.55, h = 0.52 },
  reg    = { y = -0.17, z = 0.35, h = 0.24 },
  regBig = { y = -0.10, z = 0.05, h = 0.48 },
}
M.LAYOUT_HELI = { regBig = { y = 0.02, z = -0.25, h = 0.26 } }

return M
