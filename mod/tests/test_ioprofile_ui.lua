local dir=(arg[0]:match('^(.*)[/\\]') or '.')
local T=dofile(dir..'/testlib.lua')
local IO=require('beamngEarth/ioprofile_ui')
local texts,plots={},{}
local expectedWidth=610
local im={TextUnformatted=function(s) texts[#texts+1]=s end,PushTextWrapPos=function() end,PopTextWrapPos=function() end,
 Button=function() return false end,SameLine=function() end,CollapsingHeader1=function(s) texts[#texts+1]=s;return true end,
 TreeNodeFlags_DefaultOpen=1,ImVec2=function(x,y) return {x=x,y=y} end,
 PlotLines1=function(id,values,n,_,_,_,_,size)
   T.check(n>0 and values[0]~=nil,'I/O plot has measured samples')
   T.check(size.x==expectedWidth,'I/O plot fills resized content width');plots[#plots+1]=id
 end}
local data={receivedBytes=2048,sentBytes=10,omitted=0,services={{endpoint='https://service.example',health='reachable',
 responseAvgMs=15,requestAvgMs=20,responseP95Ms=18,jitterMs=2,receivedBytes=2048,sentBytes=10,
 requests=2,responses=2,inflight=0,ageS=1,failures=0,failureShare=0,httpErrors=0,rateLimited=0,lastStatus=200,
 responseHistory={{at=1,ms=12},{at=2,ms=18}}}},
 history={{rxBps=10,txBps=2,cacheReadBps=30,cacheOutputBps=20,processReadBps=40,processWriteBps=50}},
 volumes={{label='Cache',path='sample-cache',totalBytes=1000,usedBytes=980,freeBytes=20}},
 cache={usedBytes=200,files=5,capBytes=0,ready=true},
 disk={readBytes=300,outputBytes=200,reads=5,misses=1,errors=0,timedReads=2,readMs=6,maxReadMs=5},
 processes={{pid=123,readBps=40,writeBps=50,scope='all process I/O; not physical disk'}}}
local link={state='connected',receivedBytes=100,sentBytes=50,errors=0}
local rtt={count=3,avgMs=10,timeouts=0,p95=12,jitterMs=1}
IO.draw(im,data,link,rtt,'full',610)
local fullText=table.concat(texts,'\n')
T.check(fullText:find('service.example',1,true)~=nil,'service endpoint visible')
T.check(fullText:find('15.00 ms',1,true)~=nil,'average service latency visible')
T.check(fullText:find('Less than 5%',1,true)~=nil,'disk pressure feedback')
T.check(fullText:find('not physical disk',1,true)~=nil,'OS counter scope explained')
T.check(fullText:find('12.00 ms',1,true)~=nil,'full latency distribution displayed')
texts,plots={},{}
IO.draw(im,data,link,rtt,'essential',610)
local essentialText=table.concat(texts,'\n')
T.check(essentialText:find('Essential:',1,true)~=nil,'Essential omissions explained')
local details=false
for _,id in ipairs(plots) do if id:find('process-',1,true) or id:find('service-',1,true) then details=true end end
T.check(not details,'Essential does not draw disabled process or per-service distribution graphs')
expectedWidth=960
IO.draw(im,data,link,rtt,'full',expectedWidth)
T.done('ioprofile_ui')
