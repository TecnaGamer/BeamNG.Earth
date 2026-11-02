-- Native ImGui presentation; all values come from profiler snapshots.
local V = require('beamngEarth/profiler_view')
local IO = require('beamngEarth/ioprofile_ui')
local M = {}
local serviceFilter, serviceSort = 'all', 'latency'
local function text(im,s) im.TextUnformatted(tostring(s)) end
local function wrap(im,s) im.PushTextWrapPos(0);text(im,s);im.PopTextWrapPos() end
local function ms(v) return v and string.format('%.2f ms',v) or 'Unavailable' end
local function rate(v) return v and IO.bytes(v)..'/s' or 'Warming up' end
local function color(im,c,a) return im.GetColorU322(im.ImVec4(c[1],c[2],c[3],a or 1)) end
local function rect(im,dl,x,y,w,h,c)
  if w>0 and h>0 then im.ImDrawList_AddRectFilled(dl,im.ImVec2(x,y),im.ImVec2(x+w,y+h),c) end
end
local function line(im,dl,x,y,x2,y2,c)
  im.ImDrawList_AddLine(dl,im.ImVec2(x,y),im.ImVec2(x2,y2),c,1)
end
local function label(im,dl,x,y,s,c) im.ImDrawList_AddText1(dl,im.ImVec2(x,y),c,s,nil) end
local function clip(v,low,high) return math.max(low,math.min(high,v)) end
local function tableRows(im,id,headers,rows)
  if im.BeginTable(id,#headers,im.TableFlags_Resizable+im.TableFlags_RowBg+im.TableFlags_BordersInnerV) then
    for _,h in ipairs(headers) do im.TableSetupColumn(h) end
    im.TableHeadersRow()
    for _,row in ipairs(rows) do
      im.TableNextRow()
      for i,value in ipairs(row) do im.TableSetColumnIndex(i-1);wrap(im,value) end
    end
    im.EndTable()
  end
end
local function buttonRow(im,items)
  local width,used=im.GetContentRegionAvail().x,0
  for i,item in ipairs(items) do
    local need=im.CalcTextSize(item[1]).x+24
    if used>0 and used+need+8<=width then im.SameLine();used=used+8 else used=0 end
    if im.Button(item[1]) then item[2]() end
    used=used+need
  end
end
function M.pushStyle(im)
  local values={{im.Col_WindowBg,{0.095,0.105,0.12}},{im.Col_ChildBg,{0.095,0.105,0.12}},
    {im.Col_FrameBg,{0.15,0.17,0.20}},{im.Col_Button,{0.17,0.19,0.22}},
    {im.Col_ButtonHovered,{0.28,0.31,0.35}},{im.Col_ButtonActive,{0.38,0.31,0.21}},
    {im.Col_Tab,{0.13,0.15,0.18}},{im.Col_TabActive,{0.35,0.25,0.13}},
    {im.Col_TabHovered,{0.43,0.31,0.16}},{im.Col_Text,{0.91,0.93,0.96}}}
  for _,v in ipairs(values) do im.PushStyleColor2(v[1],im.ImVec4(v[2][1],v[2][2],v[2][3],1)) end
  return #values
end
function M.tabs(im,ctl)
  local requested=ctl.tabRequest
  ctl.tabRequest=nil
  if im.BeginTabBar('##earth-profiler-tabs',0) then
    for _,tab in ipairs(V.tabs) do
      if im.BeginTabItem(tab[2],nil,requested==tab[1] and im.TabItemFlags_SetSelected or 0) then
        ctl.page=tab[1]
        im.EndTabItem()
      end
    end
    im.EndTabBar()
  end
end
function M.toolbar(im,ctl,frozen,view)
  local width=im.GetContentRegionAvail().x
  buttonRow(im,{
    {frozen and 'Resume display' or 'Freeze display',function() ctl.setFrozen(not frozen) end},
    {'Essential'..(ctl.mode=='essential' and ' *' or ''),function() ctl.chooseMode('essential') end},
    {'Full'..(ctl.mode=='full' and ' *' or ''),function() ctl.chooseMode('full') end},
    {'Save snapshot',ctl.saveSnapshot}, {'Compact view',function() ctl.setCompact(true) end}, {'Stop profiling',ctl.stop}})
  buttonRow(im,{{'Follow live',function() V.select(nil);ctl.setFrozen(false) end},
    {'Inspect worst',function()
      local worst
      for _,f in ipairs(V.history(view)) do if not worst or f.ms>worst.ms then worst=f end end
      if worst then ctl.setFrozen(true);V.select(worst) end
    end},
    {'History: '..V.seconds..' s',function() V.seconds=V.seconds==5 and 10 or V.seconds==10 and 30 or 5 end},
    {string.format('Target: %d FPS',math.floor(1000/view.frameDetail.budgetMs+0.5)),function() ctl.toggleBudget() end}})
  wrap(im,string.format('%s | %s | %d sampled frames | %.1f s elapsed',
    frozen and 'DISPLAY FROZEN - collection continues' or 'CAPTURING',ctl.mode,view.capturedFrames,view.elapsedMs/1000))
end
local function metrics(im,view,tab,width)
  local h=V.history(view)
  local io=view.server and view.server.io
  local r=io and io.rates or {}
  local f=V.current(view)
  local items
  if tab=='all' then items={{'Frame avg',ms(V.mean(h,'ms'))},{'GPU avg',ms(V.mean(h,'gpuMs'))},
    {'CPU avg',ms(V.mean(h,'cpuMs'))},{'Received',rate(r.rxBps)},{'Cache reads',rate(r.cacheReadBps)}}
  elseif tab=='gpu' then items={{'GPU query',ms(f and f.gpuMs)},{'Budget',ms(view.frameDetail.budgetMs)},{'Association','Delayed query'}}
  elseif tab=='cpu' then items={{'CPU work',ms(f and f.cpuMs)},{'Frame interval',ms(f and f.ms)},
    {'Earth Lua',view.mode=='full' and ms(f and f.modMs) or 'Full mode only'},{'GPU wait',ms(f and f.waitMs)}}
  elseif tab=='network' then
    local active,failures=0,0
    for _,s in ipairs(io and io.services or {}) do active=active+(s.inflight or 0);failures=failures+(s.failures or 0) end
    items={{'Received',rate(r.rxBps)},{'Known sent',rate(r.txBps)},{'App RTT',ms(view.latency.avgMs)},
      {'In flight',io and tostring(active) or 'Unavailable'},{'Failures',io and tostring(failures) or 'Unavailable'}}
  else
    local d=io and io.disk or {}
    items={{'Cache reads',rate(r.cacheReadBps)},{'Cache output',rate(r.cacheOutputBps)},
      {'Read avg',ms((d.timedReads or 0)>0 and d.readMs/d.timedReads or nil)}}
  end
  local columns=math.max(1,math.min(#items,math.floor(width/(im.GetFontSize()*10))))
  if im.BeginTable('##profiler-metrics',columns,im.TableFlags_BordersInnerV) then
    for i,item in ipairs(items) do
      if (i-1)%columns==0 then im.TableNextRow() end
      im.TableSetColumnIndex((i-1)%columns);text(im,item[1]);text(im,item[2])
    end
    im.EndTable()
  end
end
local function graph(im,view,ctl,kind,title,width,height,scale,keys)
  text(im,title)
  local p=im.GetCursorScreenPos()
  im.InvisibleButton('##history-'..kind,im.ImVec2(width,height))
  local hovered=im.IsItemHovered()
  local first,last=V.range(view)
  local font=im.GetFontSize()
  local left=font*3.4
  local w,h=math.max(1,width-left-4),math.max(1,height-font*1.6)
  local x,y=p.x+left,p.y
  local dl=im.GetWindowDrawList()
  local white=color(im,V.colors.frame)
  local gray=color(im,V.colors.muted)
  rect(im,dl,x,y,w,h,color(im,{0.075,0.085,0.10}))
  local function tx(t) return x+clip((t-first)/(last-first),0,1)*w end
  local function ty(v) return y+h-clip(v/scale,0,1)*h end
  for _,fraction in ipairs({0,0.5,1}) do
    local yy=y+h-fraction*h
    line(im,dl,x,yy,x+w,yy,color(im,V.colors.muted,0.25))
    label(im,dl,p.x,clip(yy-font/2,y,y+h-font),string.format('%.1f',scale*fraction),gray)
  end
  local count=0
  if kind=='cpu' or kind=='gpu' then
    for _,f in ipairs(V.history(view)) do
      local values=f[kind]
      if kind=='gpu' and not next(values) and f.gpuMs then values={['GPU total']=f.gpuMs} end
      local names=V.phases(values)
      local bottom=0
      local xx=tx(f.observedAtMs-f.ms)
      local barWidth=math.max(0.7,tx(f.observedAtMs)-xx-0.35)
      for i,name in ipairs(names) do
        local value=values[name]
        local c=V.colors[kind]
        if name:find('Unattributed') or name=='WaitNextFrame' or name=='FPSLimiter' or name=='FramePresent' then c=V.colors.muted end
        local tint=0.7+0.3*((i-1)%4)/3
        rect(im,dl,xx,ty(bottom+value),math.min(barWidth,x+w-xx),ty(bottom)-ty(bottom+value),color(im,{c[1]*tint,c[2]*tint,c[3]*tint}))
        bottom=bottom+value;count=count+1
      end
    end
    if kind=='cpu' then
      local previous
      for _,f in ipairs(V.history(view)) do
        local point={x=tx(f.observedAtMs),y=ty(f.ms)}
        if previous then line(im,dl,previous.x,previous.y,point.x,point.y,white) end
        previous=point
      end
      label(im,dl,x+4,y+2,'White: frame interval',white)
    end
    local budget=view.frameDetail.budgetMs
    if budget<scale then
      for xx=x,x+w,9 do line(im,dl,xx,ty(budget),math.min(x+w,xx+5),ty(budget),white) end
      label(im,dl,x+4,ty(budget)-font,string.format('%.1f ms budget',budget),white)
    end
  else
    for ki,key in ipairs(keys) do
      local prior
      for _,sample in ipairs(view.ioHistory or {}) do
        local raw=sample[key]
        if sample.observedAtMs>=first and sample.observedAtMs<=last and raw~=nil then
          local point={x=tx(sample.observedAtMs),y=ty(raw/1048576),at=sample.observedAtMs}
          if prior and sample.intervalS and point.at-prior.at<=sample.intervalS*1000+1 then
            line(im,dl,prior.x,prior.y,point.x,point.y,color(im,V.colors[kind],ki==1 and 1 or 0.45))
          end
          rect(im,dl,point.x-1,point.y-1,2,2,color(im,V.colors[kind]))
          prior=point;count=count+1
        else prior=nil end
      end
    end
  end
  if count==0 then label(im,dl,x+8,y+h/2,'No measured samples in this range',gray) end
  local f=V.current(view)
  if f and f.observedAtMs and f.observedAtMs>=first and f.observedAtMs<=last then
    local xx=tx(f.observedAtMs);line(im,dl,xx,y,xx,y+h,white)
  end
  label(im,dl,x,y+h+2,string.format('%.1f s',first/1000),gray)
  label(im,dl,x+w-font*5,y+h+2,string.format('%.1f s',last/1000),gray)
  if hovered then
    local at=first+clip((im.GetMousePos().x-x)/w,0,1)*(last-first)
    local picked=V.nearest(view,at)
    if picked then
      im.BeginTooltip();text(im,string.format('Frame #%d | %.2f ms | %.3f s',picked.id,picked.ms,picked.observedAtMs/1000))
      if kind=='cpu' or kind=='gpu' then
        for i,row in ipairs(V.rank(picked[kind])) do if i<=8 then text(im,row.name..'  '..ms(row.ms)) end end
        if kind=='gpu' then text(im,'Latest completed query; exact CPU frame unavailable.') end
      else
        local sample=V.ioAt(view,at)
        for _,key in ipairs(keys) do text(im,key..': '..rate(sample and sample[key])) end
        text(im,'Sampled interval, approximately aligned by packet arrival.')
      end
      im.EndTooltip()
      if im.IsMouseClicked(0) then ctl.setFrozen(true);V.select(picked) end
    end
  end
end
local function ioScale(view,keys)
  local first,last=V.range(view)
  local scale=0.01
  for _,p in ipairs(view.ioHistory or {}) do
    if p.observedAtMs>=first and p.observedAtMs<=last then
      for _,key in ipairs(keys) do scale=math.max(scale,(p[key] or 0)/1048576*1.1) end
    end
  end
  return scale
end
local function charts(im,view,ctl,tab,width,height)
  local h=V.history(view)
  local scale=V.scale(h,view.frameDetail.budgetMs)
  local laneHeight=math.max(30,(height-4*(im.GetFontSize()+6))/4)
  if tab=='all' or tab=='cpu' then graph(im,view,ctl,'cpu','CPU phases + waits / ms',width,tab=='all' and laneHeight or height-30,scale) end
  if tab=='all' or tab=='gpu' then graph(im,view,ctl,'gpu','GPU / ms - delayed query',width,tab=='all' and laneHeight or height-30,scale) end
  if tab=='all' or tab=='network' then
    local keys={'rxBps','txBps'}
    graph(im,view,ctl,'network','Network / MiB/s - received + known sent',width,tab=='all' and laneHeight or height-30,ioScale(view,keys),keys)
  end
  if tab=='all' or tab=='disk' then
    local keys={'cacheReadBps','cacheOutputBps'}
    graph(im,view,ctl,'disk','Cache / MiB/s - reads + produced files',width,tab=='all' and laneHeight or height-30,ioScale(view,keys),keys)
  end
end
local function ranked(im,title,values,kind,width,limit)
  text(im,title)
  local rows=V.rank(values)
  if #rows==0 then wrap(im,'No reported timings. Detailed scopes require Full mode and a supported engine API.');return end
  local peak=rows[1].ms
  for i,r in ipairs(rows) do
    if limit and i>limit then break end
    text(im,r.name..'  '..ms(r.ms))
    local p=im.GetCursorScreenPos()
    im.Dummy(im.ImVec2(width,3))
    rect(im,im.GetWindowDrawList(),p.x,p.y,width*r.ms/peak,3,color(im,V.colors[kind]))
  end
end
local function inspector(im,view,ctl,tab,width)
  local f=V.current(view)
  if not f then wrap(im,'Collecting frame history...');return end
  text(im,(V.selection() and 'Selected frame #' or 'Latest frame #')..f.id)
  text(im,ms(f.ms)..(f.ms>view.frameDetail.budgetMs and '  Over budget' or ''))
  text(im,string.format('Observed %.3f s',f.observedAtMs/1000))
  if tab=='gpu' then
    text(im,'GPU total: '..ms(f.gpuMs));ranked(im,'Latest completed query',f.gpu,'gpu',width,8)
    wrap(im,f.gpuTiming or 'GPU timing unavailable')
  elseif tab=='cpu' or tab=='all' then
    text(im,'CPU work: '..ms(f.cpuMs));text(im,'GPU wait: '..ms(f.waitMs))
    ranked(im,'Earth scopes (inside CPU Lua)',f.mod,'cpu',width,6)
    if tab=='all' and im.Button('Open CPU') then ctl.setPage('cpu') end
    for _,r in ipairs(V.queueRows(view)) do text(im,string.format('%s: %d queued / %d running',r.name,r.queued,r.running)) end
  else
    local p=V.ioAt(view,f.observedAtMs)
    text(im,'I/O interval around selection')
    if p then
      text(im,string.format('%.2f s interval',p.intervalS))
      if tab=='network' then text(im,'Received: '..rate(p.rxBps));text(im,'Known sent: '..rate(p.txBps))
      else text(im,'Cache reads: '..rate(p.cacheReadBps));text(im,'Cache output: '..rate(p.cacheOutputBps)) end
    else wrap(im,'No I/O interval covers this selection.') end
    wrap(im,'Arrival-aligned samples, not per-frame operations.')
  end
end
local function detail(im,view,ctl,tab,width)
  local f=V.current(view)
  local io=view.server and view.server.io
  if tab=='gpu' then
    if f then ranked(im,'Reported GPU passes / selected observation',f.gpu,'gpu',width) end
    wrap(im,'CPU and GPU run in parallel. Completed GPU queries can describe an earlier render frame.')
  elseif tab=='cpu' then
    if f then
      ranked(im,'Selected frame / CPU phases (includes waits)',f.cpu,'cpu',width)
      if im.CollapsingHeader1('Earth Lua scopes / included in CPU',im.TreeNodeFlags_DefaultOpen) then ranked(im,'Measured Earth scopes',f.mod,'cpu',width) end
    end
    wrap(im,'Inclusive scopes and engine phases can overlap. Do not add Earth Lua to CPU totals.')
    if im.CollapsingHeader1('World generation / server workers') then ctl.drawWorkers(im,view,width) end
  elseif tab=='network' then
    text(im,'Services / capture aggregates at displayed snapshot')
    buttonRow(im,{{'All services',function() serviceFilter='all' end},{'Active',function() serviceFilter='active' end},
      {'Problems',function() serviceFilter='problems' end},{'Sort: '..serviceSort,function() serviceSort=serviceSort=='latency' and 'traffic' or serviceSort=='traffic' and 'name' or 'latency' end}})
    local rows={}
    for _,r in ipairs(IO.rows(io and io.services,serviceFilter,serviceSort)) do
      rows[#rows+1]={r.endpoint,ms(r.responseAvgMs),ms(r.responseP95Ms),tostring(r.inflight or 0),r.health}
    end
    if #rows>0 then tableRows(im,'##services',{'Endpoint','Avg headers','p95','Active','Status'},rows)
    else text(im,'No measured services match this filter.') end
    if im.CollapsingHeader1('Connection and service details',im.TreeNodeFlags_DefaultOpen) then IO.drawNetwork(im,io,view.localLink,view.latency,view.mode,width) end
  elseif tab=='disk' then
    if io and io.cache then
      local c=io.cache
      text(im,'Tracked cache / '..tostring(c.files)..' files')
      local cap=c.capBytes or 0
      if cap>0 then im.ProgressBar(clip((c.usedBytes or 0)/cap,0,1),im.ImVec2(width,20),IO.bytes(c.usedBytes)..' / '..IO.bytes(cap))
      else text(im,IO.bytes(c.usedBytes)..' / unlimited quota') end
    end
    IO.drawDisk(im,io,view.mode,width)
  else
    text(im,'Observed work / durations from labelled sources')
    local rows={}
    if f then
      for i,r in ipairs(V.rank(f.mod)) do if i<=4 then rows[#rows+1]={'CPU / Earth',r.name,ms(r.ms),'Selected frame; included in Lua'} end end
      for i,r in ipairs(V.rank(f.gpu)) do if i<=2 then rows[#rows+1]={'GPU',r.name,ms(r.ms),'Latest completed query'} end end
    end
    for i,r in ipairs(IO.rows(io and io.services,'all','latency')) do
      if i<=3 then rows[#rows+1]={'Network',r.endpoint,ms(r.responseAvgMs),'Capture average / headers'} end
    end
    local d=io and io.disk
    if d and (d.timedReads or 0)>0 then rows[#rows+1]={'Disk','Instrumented cache reads',ms(d.readMs/d.timedReads),'Capture average / operation'} end
    if #rows>0 then tableRows(im,'##observed-work',{'Subsystem','Activity','Duration','Scope'},rows) else text(im,'No detailed work reported yet.') end
    if im.CollapsingHeader1('Frame observations') then for _,o in ipairs(view.frameDetail.observations) do wrap(im,o.text) end end
    if im.CollapsingHeader1('World generation / server workers') then ctl.drawWorkers(im,view,width) end
  end
end
local function splitter(im,id,size,axis,value,low,high)
  local p=im.GetCursorScreenPos()
  im.InvisibleButton(id,size)
  rect(im,im.GetWindowDrawList(),p.x,p.y,size.x,size.y,color(im,V.colors.muted,0.4))
  if im.IsItemHovered() then im.SetMouseCursor(axis=='x' and im.MouseCursor_ResizeEW or im.MouseCursor_ResizeNS) end
  local active=im.IsItemActive()
  if active then
    local d=im.GetMouseDragDelta(0,0)
    value=clip(value+d[axis],low,high)
    im.ResetMouseDragDelta(0)
  end
  return value,active
end
function M.body(im,view,ctl)
  local tab=ctl.page
  local width=im.GetContentRegionAvail().x
  local scale=im.GetFontSize()/16
  if tab=='server' then
    text(im,'World server')
    local link=view.localLink or {}
    local latency=view.latency or {}
    text(im,(link.endpoint or 'Endpoint unavailable')..' | '..(link.state or 'unavailable'))
    text(im,'Application round trip: '..ms(latency.avgMs)..' average | '..tostring(latency.count or 0)..' replies')
    wrap(im,'Round trip includes game and server scheduling; it is not server CPU time.')
    local data=view.server
    if not data then wrap(im,'No server telemetry received. Connect to a compatible server to collect measurements.');return end
    text(im,'Telemetry age at displayed snapshot: '..ms(view.serverAgeMs))
    text(im,'Collection: '..tostring(data.collectionMode or view.mode))
    if view.mode=='full' then
      local running,queued,failed=0,0,0
      for _,job in ipairs(data.active or {}) do
        if job.worker then running=running+1 else queued=queued+1 end
      end
      for _,job in ipairs(data.recent or {}) do
        if job.outcome=='error' or job.outcome=='cancelled' then failed=failed+1 end
      end
      text(im,string.format('Measured jobs: %d running | %d queued | %d recent failed/cancelled',running,queued,failed))
      wrap(im,'Counts cover retained telemetry, not all server tasks. Recent history is bounded to 120 jobs.')
    end
    ctl.drawWorkers(im,view,width)
    return
  end
  metrics(im,view,tab,width)
  local available=im.GetContentRegionAvail()
  local layout=V.layout(width,available.y,scale)
  -- Keep four readable lanes in All; the outer content child handles short windows.
  if tab=='all' then layout.chartHeight=math.max(260*scale,layout.chartHeight) end
  local top=im.GetCursorScreenPos()
  if im.BeginChild1('##profiler-charts',im.ImVec2(layout.chartWidth,layout.chartHeight),false) then
    charts(im,view,ctl,tab,im.GetContentRegionAvail().x,layout.chartHeight)
  end
  im.EndChild()
  if layout.wide then
    im.SetCursorScreenPos(im.ImVec2(top.x+layout.chartWidth+3,top.y))
    -- Drag right to widen the charts, left to widen the inspector.
    local chartWidth,dragged=splitter(im,'##profiler-vsplit',im.ImVec2(5,layout.chartHeight),'x',layout.chartWidth,width*0.64,width-230*scale-12*scale)
    if dragged then V.inspectorWidth=(width-chartWidth-12*scale)/scale end
    im.SetCursorScreenPos(im.ImVec2(top.x+layout.chartWidth+12*scale,top.y))
    if im.BeginChild1('##profiler-inspector',im.ImVec2(layout.inspector,layout.chartHeight),true) then inspector(im,view,ctl,tab,im.GetContentRegionAvail().x) end
    im.EndChild()
  end
  im.SetCursorScreenPos(im.ImVec2(top.x,top.y+layout.chartHeight+4))
  local chartHeight,dragged=splitter(im,'##profiler-hsplit',im.ImVec2(width,5),'y',layout.chartHeight,180*scale,math.max(180*scale,available.y-90*scale))
  if dragged then V.chartHeight=chartHeight end
  if not layout.wide and im.CollapsingHeader1('Selection details') then inspector(im,view,ctl,tab,width) end
  detail(im,view,ctl,tab,width)
end
local function footerLines(view,tab)
  if tab=='server' then return {'Server worker timings are separate from game frames. Freeze holds the displayed server snapshot.'} end
  local lines={string.format('History: %d frames retained (bounded); blank time has no captured samples.',view.frames)}
  if tab=='gpu' or tab=='all' then lines[#lines+1]='GPU: delayed query; CPU and GPU overlap.' end
  if tab=='network' or tab=='disk' or tab=='all' then lines[#lines+1]='I/O: sampled intervals, arrival-aligned; sent bytes estimated; cache output is produced-file size.' end
  return lines
end
function M.footerHeight(im,view,tab,width)
  local height=4
  for _,s in ipairs(footerLines(view,tab)) do height=height+im.CalcTextSize(s,nil,false,width).y+4 end
  return height
end
function M.footer(im,view,tab)
  for _,s in ipairs(footerLines(view,tab)) do wrap(im,s) end
end
return M
