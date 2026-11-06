local dir=arg[0]:match('^(.*)[/\\]')or'.'
package.path=dir..'/../beamng_earth/lua/ge/extensions/?.lua;'..package.path
local C=require('beamngEarth/nl_country')
for _,p in ipairs({{51.36711,6.14178},{52.37,4.895},{50.8514,5.691},{53.219,6.566},{53.47,6.162},{51.445,4.929}})do
  assert(C.contains(p[1],p[2]),'Dutch point must enable RVV-only')
end
for _,p in ipairs({{51.3167,6.2833},{51.221,4.403},{51.4416,4.9323},{50.776,6.083},{51.507,-.128},{0,0}})do
  assert(not C.contains(p[1],p[2]),'Foreign point must keep ordinary signs')
end
assert(not C.contains(nil,6)and not C.contains(0/0,6))
print('PASS: Netherlands boundary, islands, Belgian enclave, Germany, Belgium and unknown position.')
