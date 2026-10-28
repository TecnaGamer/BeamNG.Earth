-- Photos arriving before/after terrain commit and during an anchor change must reach that exact block.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local core = require('beamngEarth/core')
local terrain = require('beamngEarth/terrain')
local receive
for i = 1, 100 do
  local name, value = debug.getupvalue(core.onUpdate, i)
  if not name then break end
  if name == 'onMessage' then receive = value; break end
end
assert(receive, 'core message handler')
receive({type='hello', protocol=4, format=4, cells=64, blockSize=512, serverVersion='test', sessionId='photos',
         nearVisual='detail', farFormat='cdae', material='bngEarthFar_v3'})
local st = core._state()
st.anchorId, st.block = 1, {cx=0,cy=0}
local calls = {}
terrain.setBase = function(base, size) calls[#calls+1] = {base, size} end
FS.fileExists = function() return true end
local function photo(a, x, y, path)
  receive({type='block_base',anchorId=a,cx=x,cy=y,base=path,baseSize=1024})
end
photo(1,0,0,'current.png')
T.check(#calls==1 and calls[1][1]=='current.png' and calls[1][2]==1024, 'current block updated')
st.terrainPending = {msg={cx=128,cy=0}}
photo(1,128,0,'next.png')
T.check(st.terrainPending.msg.base=='next.png' and st.terrainPending.msg.baseSize==1024, 'ready block retains full photo')
T.check(#calls==1, 'future block does not repaint current block')
st.pending = {msg={anchorId=2,terrain={cx=0,cy=128}}}
photo(2,0,128,'anchor.png')
T.check(st.pending.msg.terrain.base=='anchor.png', 'new anchor photo retained before commit')
st.rebasing = {msg={anchorId=3},terrain={cx=0,cy=0}}
photo(3,0,0,'rebase.png')
T.check(st.rebasing.terrain.base=='rebase.png' and st.rebasing.terrain.baseSize==1024, 'photo retained between rebase phases')
photo(99,0,0,'stale.png')
photo(3,128,0,'wrong-block.png')
T.check(#calls==1 and st.rebasing.terrain.base=='rebase.png', 'wrong anchor or coordinates ignored')
FS.fileExists = function() return false end
photo(1,0,0,'missing.png')
T.check(#calls==1, 'missing texture never applied to live block')
T.done('test_block_base')
