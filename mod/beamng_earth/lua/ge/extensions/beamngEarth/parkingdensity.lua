-- Decorative inventory is sampled against the game's local clock. Ranks come from stable
-- stall IDs on the server. The curve values describe plausible occupancy, not live counts.
local M = {}
local profiles = { general = 1, retail = 2, dealer = 3, office = 4, restaurant = 5 }
local models = { sedan = 0, hatchback = 1, suv = 2, pickup = 3 }
M.groups = { 'lot_sedan', 'lot_hatchback', 'lot_suv', 'lot_pickup' }
local hours = { 0, 5, 8, 10, 12, 15, 18, 20, 23, 24 }
local curves = {
  {.08,.06,.24,.36,.52,.45,.40,.26,.10,.08},
  {.035,.025,.12,.36,.63,.70,.84,.55,.08,.035},
  {.91,.91,.91,.91,.91,.91,.91,.91,.91,.91},
  {.04,.05,.60,.86,.76,.82,.28,.08,.045,.04},
  {.04,.025,.10,.16,.68,.27,.78,.67,.12,.04},
}

function M.fraction(profile, hour)
  local curve = curves[profile] or curves[1]
  hour = (hour or 12) % 24
  for i = 2, #hours do
    if hour <= hours[i] then
      local mix = (hour-hours[i-1])/(hours[i]-hours[i-1])
      return curve[i-1] + mix*(curve[i]-curve[i-1])
    end
  end
end

-- Leave normal parking/bus rows alone. Decoration rows never enter gameplay_parking.
function M.split(rows)
  local parking, scenery
  for i, r in ipairs(rows) do
    if r[5] == 3 then
      if not scenery then
        scenery, parking = {}, {}
        for j = 1, i-1 do parking[j] = rows[j] end
      end
      local model, height, rank = models[r[6]], tonumber(r[7]), tonumber(r[9])
      if model and height and height > 0 and rank and rank >= 0 and rank <= 1 then
        scenery[#scenery+1] = {model,r[1],r[2],r[3],r[4],1,height,rank,profiles[r[8]] or 1}
      end
    elseif parking then parking[#parking+1] = r end
  end
  return parking or rows, scenery
end

-- Start with a representative lot on stream-in. Later changes wait while close to
-- the camera or in its view, even if the player jumps the clock several hours.
function M.visible(rank, fraction, previous, protected)
  if previous == nil then return rank < fraction end
  if protected then return previous end
  if previous then return rank < fraction+.012 end
  return rank < fraction-.012
end

function M.refresh(tile, camera, player, geo)
  local hour = require('beamngEarth/nightlights').hour()
  local parking = require('beamngEarth/parking')
  local change = false
  local previous = tile.occupied or {}
  local fractions = {}
  for i = 1, tile.n do
    local b = (i-1)*tile.s
    local f = tile.f
    local profile, rank = f[b+9], f[b+8]
    if not fractions[profile] then fractions[profile] = M.fraction(profile,hour) end
    local before = previous[i]
    local after = M.visible(rank,fractions[profile],before,false)
    if before ~= nil and after ~= before then
      local x,y,z = f[b+2],f[b+3],f[b+4]
      if tile.geo then x,y,z = geo.apply(tile.pl,x,y,z) end
      local p = camera and camera.pos or player
      local close = (x-p.x)^2+(y-p.y)^2 < 55^2
      if close or (camera and parking.inView({x=x,y=y,z=z},camera)) then after = before end
    end
    if before ~= after then previous[i], change = after, true end
  end
  tile.occupied = previous
  return change
end

return M
