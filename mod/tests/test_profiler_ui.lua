-- Exercise real controls and reflow with measured fixtures; no engine or playtest changes.
local dir=(arg[0]:match('^(.*)[/\\]') or '.')
local T=dofile(dir..'/testlib.lua')
local V=require('beamngEarth/profiler_view')
local UI=require('beamngEarth/profiler_ui')
local im,S=dofile(dir..'/profiler_imgui_stub.lua')(T)
local view={elapsedMs=10000,capturedFrames=600,frames=600,mode='full',latency={},localLink={},
  ioHistory={{observedAtMs=9000,intervalS=1,rxBps=1000,txBps=0,cacheReadBps=2000},
    {observedAtMs=10000,intervalS=1,rxBps=3000,txBps=0,cacheReadBps=1000}},
  server={active={},io={rates={},services={}}},
  frameDetail={budgetMs=1000/60,observations={},history={
    {id=1,observedAtMs=9000,ms=38,cpuMs=31,gpuMs=12,cpu={Render=31},gpu={Shadows=12},mod={BuildingUpload=18}},
    {id=2,observedAtMs=10000,ms=16,cpuMs=8,gpuMs=7,cpu={Render=8},gpu={Shadows=7},mod={TerrainUpdate=3}}
  }}}
local held,exports,stopped=false,0,false
local ctl={page='all',mode='full'}
function ctl.setFrozen(on) held=on;if not on then V.follow() end end
function ctl.chooseMode(mode) ctl.mode=mode end
function ctl.saveSnapshot() exports=exports+1 end
function ctl.setCompact(on) ctl.compact=on end
function ctl.stop() stopped=true end
function ctl.toggleBudget() view.frameDetail.budgetMs=1000/120 end
function ctl.setPage(page) ctl.page=page end
function ctl.drawWorkers() end
S.width=540
UI.toolbar(im,ctl,false,view)
T.check(not S.sizes['##profiler-mode'],'narrow toolbar moves secondary controls into overflow')
S.clicked='More...';UI.toolbar(im,ctl,false,view)
T.check(S.popup=='##profiler-options' and S.sizes['##profiler-mode'],'overflow exposes mode/history/target without losing controls')
S.clicked='##profiler-mode';UI.toolbar(im,ctl,false,view)
T.check(ctl.mode=='essential','inset mode selector switches collection mode')
S.clicked='##profiler-budget';UI.toolbar(im,ctl,false,view)
T.near(view.frameDetail.budgetMs,1000/120,0.001,'inset frame target selector works')
S.clicked='Export';UI.toolbar(im,ctl,false,view)
T.check(exports==1,'compact overflow can export')
S.popup=nil
UI.body(im,view,ctl)
T.check(S.sizes['##history-cpu'] and S.sizes['##history-gpu'] and S.sizes['##history-network'] and S.sizes['##history-disk'],'narrow layout retains all four histories')
T.check(not S.sizes['##profiler-vsplit'] and not S.sizes['##profiler-range'],'compact layout collapses inspector and overview')
S.width=1400
S.clicked='Inspect worst';UI.body(im,view,ctl)
T.check(held and V.selection().id==1,'worst frame button freezes and selects displayed worst frame')
T.check(S.sizes['##profiler-vsplit'] and S.sizes['##profiler-range'],'wide layout exposes draggable inspector and time range')
ctl.page='gpu';UI.body(im,view,ctl)
T.check(V.selection().id==1,'resource tab does not reset selected observation')
S.mouseX=700;S.hover='##profiler-range';S.pick=true;S.active='##profiler-range';S.mouseDown=true
UI.body(im,view,ctl)
S.pick=false;S.mouseX=600;UI.body(im,view,ctl)
local first,last=V.range(view)
T.check(first>=0 and last<=10000 and last>first,'overview dragging keeps a valid bounded time range')
S.active=nil;S.mouseDown=false;S.hover=nil
S.clicked='Follow live';UI.body(im,view,ctl)
T.check(not held and V.selection()==nil and select(2,V.range(view))==10000,'Follow live leaves held range and resumes capture display')
ctl.page='all';S.filter='BuildingUpload';V.select(view.frameDetail.history[1]);S.texts={};UI.body(im,view,ctl)
T.check(not table.concat(S.texts,'|'):find('No activities match',1,true),'activity filter includes selected measured scope')
S.filter='nonexistent activity';S.texts={};UI.body(im,view,ctl)
T.check(table.concat(S.texts,'|'):find('No activities match this filter.',1,true)~=nil,'unmatched filter has an explicit empty state')
for _,font in ipairs({12,16,24,32}) do
  S.font=font
  for _,w in ipairs({540,800,1100,1600}) do
    S.width=w*font/16;S.height=800*font/16;S.sizes={}
    local colors,vars=UI.pushStyle(im)
    UI.toolbar(im,ctl,false,view);UI.body(im,view,ctl);UI.footer(im,view,'all')
    im.PopStyleVar(vars);im.PopStyleColor(colors)
    T.check(S.styles==0 and S.vars==0 and S.children==0 and S.tables==0,'balanced native stacks at each size and font scale')
    T.check(S.sizes['##history-cpu'].x<=S.width,'history fits available scaled window width')
  end
end
T.done('profiler_ui')
