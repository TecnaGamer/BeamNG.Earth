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
V.setRange(view,8000,10000)
local first,last=V.range(view)
T.check(first==8000 and last==10000 and #V.history(view)==1,'zoom/pan uses observation timestamps and invalidates cached history')
V.select(b)
T.check(V.export(view,'cpu').presentation.endMs==10000,'export preserves the held zoom range')
V.follow()
T.check(V.selection()==nil and select(2,V.range(view))==12000,'follow clears time hold and frame selection together')
V.setRange(view,-10000,999999)
first,last=V.range(view)
T.check(first==0 and last==12000,'range handles cannot escape captured elapsed time')
V.setRange(view,9000,9000)
T.check(V.seconds>=0.25,'collapsed handles retain a usable minimum time range')
V.reset();V.seconds=10
local dense={elapsedMs=10000,frameDetail={history={}}}
for i=1,600 do dense.frameDetail.history[i]={id=i,observedAtMs=9000+i,ms=10,cpu={Render=8},gpu={Pass=4}} end
dense.frameDetail.history[321].ms=90
dense.frameDetail.history[321].cpu.Render=75
dense.frameDetail.history[320].gpu.Pass=55
dense.frameDetail.history[319].ms=150 -- Long wall interval must not hide CPU work in the same pixel.
local cpu,gpu=V.columns(dense,10,'cpu'),V.columns(dense,10,'gpu')
local cpuPeak,gpuPeak=false,false
for _,f in ipairs(cpu) do if f.id==321 then cpuPeak=true end end
for _,f in ipairs(gpu) do if f.id==320 then gpuPeak=true end end
T.check(#cpu<=10 and #gpu<=10 and cpuPeak and gpuPeak,'pixel-density reduction preserves each lane peak independently')
T.check(#V.export(dense,'all').frameDetail.history==600,'density reduction never removes raw exported observations')
for _,font in ipairs({12,16,24,32}) do
  local s=font/16
  for _,width in ipairs({540,800,960,1500}) do
    local columns,rows,cell=V.metricLayout(width*s,5,font)
    T.check(columns*cell<=width*s+0.01 and columns*rows>=5 and cell>=font*8,'metrics reflow without reducing type size')
    local layout=V.layout(width*s,700*s,s)
    T.check(layout.chartWidth>0 and layout.chartWidth+layout.inspector<=width*s,'scaled panels fit their parent')
  end
end
V.chartHeight=300
T.check(V.layout(2400,1800,2).chartHeight==600,'manual chart height persists in logical units across font changes')
V.chartHeight=nil
T.check(V.matches({'CPU','Building upload','18 ms'},'BUILDING') and not V.matches({'CPU','Building upload'},'terrain'),'activity filtering is case insensitive')
T.check(V.matches({'Disk','cache[1]','5% completed'},'[1]') and V.matches({'Disk','5% completed'},'%'),'activity filter treats patterns as literal input')
T.done('profiler_view')
