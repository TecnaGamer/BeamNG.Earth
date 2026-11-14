local dir=(arg[0]:match('^(.*)[/\\]') or '.')
local T=dofile(dir..'/testlib.lua')
local consumer=require('beamngEarth/nl_signal_assemblies')
local root='/levels/beamng_earth/art/shapes/beamngEarth/groenpol/'
local function part(key,x)
  return {key=key,shape=root..'mast.dae',pose={x or 0,0,0,1,0,0,0,1,0,0,0,1}}
end
local made,removed,live,fail=0,0,{},nil
local reader=consumer.new({create=function(p)
  if p.key==fail then return nil end
  made=made+1;local h={part=p};live[h]=true;return h
end,remove=function(h)
  assert(live[h]);live[h]=nil;removed=removed+1
end})
local function packet(rev,rows)return {format=1,revision=rev,parts=rows} end
do
  local h=part('lane:0');h.role='head';h.lane=0
  h.forest={body=root..'body.dae',red=root..'red.dae',amber=root..'amber.dae',green=root..'green.dae'}
  local assembly={format=1,parts={part('steel'),h}}
  local red=assert(consumer.farParts(assembly,{'red'}))
  local green=assert(consumer.farParts(assembly,{'green'}))
  T.check(#red==3 and red[2].shape==green[2].shape and red[3].shape~=green[3].shape,
          'controller change replaces optical face, keeps shared body')
  T.check(#assert(consumer.farParts(assembly,{'off'}))==2,'explicit off retains body')
  T.check(not consumer.farParts(assembly,{}),'missing controller phase is never guessed')
end
T.check(not reader:enqueue(packet(0,{part('a')})) and made==0,'off does no scene work')
reader:setEnabled(true)
local p=part('a')
assert(reader:enqueue(packet(0,{p,part('b')})))
p.pose[1]=99 -- caller mutation cannot change the queued server snapshot
local ok,why=reader:tick(1)
T.check(not ok and why=='pending' and reader.revision==-1,'accept only complete snapshot')
assert(reader:tick(1))
T.check(reader.live.a.part.pose[1]==0 and made==2,'copy network input')
assert(reader:enqueue(packet(1,{part('a'),part('b')})))
assert(reader:tick(2))
T.check(made==2 and removed==0,'identical rebase/snapshot does not remake parts')
T.check(not reader:enqueue(packet(1,{})),'stale revision cannot clear scene')
assert(reader:enqueue(packet(2,{part('a',1),part('bad')})))
fail='bad'
assert(not reader:tick(2))
T.check(reader.revision==1 and reader.live.a.part.pose[1]==0 and removed==1,
        'failed replacement removes partial new work and retains accepted scene')
fail=nil
local bad=part('a');bad.pose[7]=1 -- shear and nonorthogonal basis
T.check(not reader:enqueue(packet(3,{bad})),'refuse distorted transform')
bad=part('a');bad.shape=root..'../other.dae'
T.check(not reader:enqueue(packet(3,{bad})),'refuse traversal')
bad=part('a');bad.pose[1]=0/0
T.check(not reader:enqueue(packet(3,{bad})),'refuse nonfinite position')
T.check(not reader:enqueue(packet(3,{part('a'),part('a')})),'refuse duplicate part identity')
T.check(not reader:enqueue(packet(3,{extra=part('a')})),'refuse nonarray disguised as empty clear')
local beam=part('beam');beam.pose[8]=5
assert(reader:enqueue(packet(3,{part('a'),beam})));assert(reader:tick(2))
T.check(reader.live.beam.part.pose[8]==5,'preserve tested nonuniform beam basis')
reader:setEnabled(false)
T.check(next(live)==nil and reader.revision==-1,'disable removes owned items')
reader:setEnabled(true)
assert(reader:enqueue(packet(0,{part('a')})));assert(reader:tick(1))
local before=removed
reader:forgetLevel()
T.check(removed==before and next(reader.live)==nil,'level teardown never calls stale native handles')
T.done('test_nl_signal_assemblies')
