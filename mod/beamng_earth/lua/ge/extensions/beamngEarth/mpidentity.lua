-- Guest reconnect identity only. Positions and vehicles remain on the BeamMP server.
-- Each endpoint gets a server-issued random token, never a name, IP-based player ID,
-- or token shared with another server. Two files protect the identity from partial writes.
local M = {}
M.FILES = {'/settings/beamngEarthMPGuests1.json', '/settings/beamngEarthMPGuests2.json'}
local endpoint, current, slot

local function validToken(t)
  return type(t)=='string' and #t==32 and t:match('^[0-9a-f]+$') and t:find('[1-9a-f]') ~= nil
end

local function read(path)
  local ok, value = pcall(jsonReadFile, path)
  if not ok or type(value)~='table' or value.version~=1 or type(value.servers)~='table'
    or type(value.sequence)~='number' or value.sequence<1 or value.sequence%1~=0 then return nil end
  for key, token in pairs(value.servers) do
    if type(key)~='string' or #key>300 or not validToken(token) then return nil end
  end
  return value
end

local function load()
  local mp = rawget(_G,'MPCoreNetwork')
  if not mp or not mp.getCurrentServer or not rawget(_G,'jsonReadFile') or not rawget(_G,'jsonWriteFile') then return false end
  local ok, server = pcall(mp.getCurrentServer)
  if not ok or type(server)~='table' or type(server.ip)~='string' or #server.ip==0 or #server.ip>255 then return false end
  local port = tonumber(server.port)
  if not port or port<1 or port>65535 or port%1~=0 then return false end
  local key = server.ip:lower()..':'..port
  if endpoint ~= key then
    endpoint, current, slot = key, {version=1, sequence=0, servers={}}, nil
    for i,path in ipairs(M.FILES) do
      local value = read(path)
      if value and value.sequence>current.sequence then current,slot = value,i end
    end
  end
  return true
end

function M.identity()
  if not load() then return nil, nil end
  return current.servers[endpoint], 1
end

function M.store(token)
  if not validToken(token) or not load() then return false end
  if current.servers[endpoint]==token then return true end
  local out = {version=1, sequence=current.sequence+1, servers={}}
  for key,value in pairs(current.servers) do out.servers[key]=value end
  out.servers[endpoint] = token
  local nextSlot = slot==1 and 2 or 1
  local ok,result = pcall(jsonWriteFile, M.FILES[nextSlot], out, true)
  local check = ok and result~=false and read(M.FILES[nextSlot]) or nil
  if not check or check.sequence~=out.sequence or check.servers[endpoint]~=token then
    if rawget(_G,'log') then log('W','beamngEarth','Could not save multiplayer guest identity; will retry') end
    return false
  end
  current,slot = check,nextSlot
  return true
end

function M.reset() endpoint,current,slot = nil,nil,nil end
return M
