-- A guest's server-issued ID survives reconnects without leaking between endpoints.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local identity = require('beamngEarth/mpidentity')
local function copy(value)
  if type(value)~='table' then return value end
  local result={};for k,v in pairs(value) do result[k]=copy(v) end;return result
end
local files,writes,fail={},0,false
local server={ip='EXAMPLE.test',port=30814}
MPCoreNetwork={getCurrentServer=function() return server end}
jsonReadFile=function(path) return copy(files[path]) end
jsonWriteFile=function(path,data)
  writes=writes+1;files[path]=fail and 'partial' or copy(data);return not fail
end
local tokenA,tokenB=string.rep('a',32),string.rep('b',32)
local token,supported=identity.identity()
T.check(not token and supported==1,'new profile advertises guest support without inventing an identity')
T.check(identity.store(tokenA),'server-issued token written and read back')
T.check(identity.identity()==tokenA,'durable token is sent back to the server')
identity.reset()
server.ip='example.test'
T.check(identity.identity()==tokenA,'reconnect and hostname case changes retain guest identity')
T.check(identity.store(tokenA) and writes==1,'duplicate identity messages do not rewrite the file')
server.port=30815
T.check(identity.identity()==nil,'different server port does not receive another server token')
T.check(identity.store(tokenB),'second endpoint gets its own token')
identity.reset();server.port=30814
T.check(identity.identity()==tokenA,'saving another endpoint preserves the first')
fail=true
T.check(not identity.store(string.rep('c',32)) and identity.identity()==tokenA,
  'failed write never acknowledges an identity that was not persisted')
identity.reset()
T.check(identity.identity()==tokenA,'interrupted identity write recovers the previous complete file')
fail=false
T.check(identity.store(string.rep('c',32)),'failed write is retried successfully')
local before=writes
T.check(not identity.store('101') and not identity.store(string.rep('0',32)) and writes==before,
  'invalid or account-like tokens do not alter local identity')
server=nil;identity.reset()
local unknown,capability=identity.identity()
T.check(not unknown and not capability and not identity.store(tokenA),'unknown endpoint cannot mix identities between servers')
server={ip='example.test',port=30814};identity.reset()
T.check(identity.identity()==string.rep('c',32),'endpoint becoming ready reloads its existing identity')
T.done('test_mpidentity')
