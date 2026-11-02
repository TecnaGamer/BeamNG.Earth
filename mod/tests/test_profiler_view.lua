local dir=(arg[0]:match('^(.*)[/\\]') or '.')
local T=dofile(dir..'/testlib.lua')
local V=require('beamngEarth/profiler_view')
local a={id=1,observedAtMs=1000,ms=10,cpu={},gpu={},mod={}}
local b={id=2,observedAtMs=9000,ms=30,cpu={Lua=8},gpu={Shadows=5},mod={roads=3}}
local c={id=3,observedAtMs=12000,ms=12,cpu={},gpu={},mod={}}
local view={elapsedMs=12000,frameDetail={history={a,b,c}}}
V.seconds=10
T.check(#V.history(view)==2 and V.history(view)[1]==b,'time window uses timestamps, not a fixed number of samples')
T.check(V.nearest(view,8500)==b,'cross-tab selection locates nearest observed frame')
V.select(b);T.check(V.current(view)==b,'selection retained across new live observations')
local compact,wide=V.layout(530,360),V.layout(1500,800)
T.check(not compact.wide and wide.wide,'inspector reflows below compact chart')
T.near(wide.chartWidth+wide.inspector+12,1500,0.01,'wide panels fit available width')
T.check(not V.layout(1100,800,1.5).wide,'DPI scaling changes responsive threshold')
local io={observedAt=100,history={{at=98,intervalS=1,rxBps=20},{at=100,intervalS=2,rxBps=40}}}
view.ioHistory=V.ioHistory(io,12000)
T.check(view.ioHistory[1].observedAtMs==10000 and view.ioHistory[2].observedAtMs==12000,'I/O samples align by server-relative time and receive timestamp')
T.check(V.ioAt(view,11000).rxBps==40,'selected interval preserves measured rate')
T.check(V.ioAt(view,8000)==nil,'missing I/O interval is not interpolated')
T.check(#V.ioHistory({history={{rxBps=2}}},12000)==0,'old server without timestamps cannot invent correlation')
T.check(io.history[1].observedAtMs==nil,'presentation does not mutate received telemetry')
T.check(V.mean({{gpuMs=0},{}},'gpuMs')==0 and V.mean({{}},'gpuMs')==nil,'zero and unavailable remain distinct')
view.frameDetail._layouts={private=true};view.plot={1}
local exported=V.export(view,'disk')
T.check(exported.presentation.tab=='disk' and exported.presentation.selectedFrame.id==2,'export retains selected tab/frame')
T.check(exported.plot==nil and exported.frameDetail._layouts==nil and exported._presentation==nil,'export strips rendering caches')
T.check(exported.presentation.units.network=='bytes/s','export states raw units')
V.reset();T.check(V.current(view)==c,'new capture clears old selection')
T.check(V.validTab('server') and V.tabs[#V.tabs][1]=='server' and V.tabs[#V.tabs-1][1]=='disk','Server tab follows Disk')
T.done('profiler_view')
