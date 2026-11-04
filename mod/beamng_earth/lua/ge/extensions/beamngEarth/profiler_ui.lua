-- Native ImGui presentation; all values come from profiler snapshots.
local V = require('beamngEarth/profiler_view')
local IO = require('beamngEarth/ioprofile_ui')
local M = {}
local ffi = require('ffi')
local serviceFilter, serviceSort = 'all', 'latency'
local activityFilter=ffi.new('char[128]')
local orange={1,0.40,0.0}
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
  local values={{im.Col_WindowBg,{0.105,0.114,0.129}},{im.Col_ChildBg,{0.105,0.114,0.129}},
    {im.Col_PopupBg,{0.105,0.114,0.129}},{im.Col_FrameBg,{0.055,0.063,0.078}},
    {im.Col_FrameBgHovered,{0.19,0.21,0.25}},{im.Col_FrameBgActive,{0.24,0.26,0.30}},
    {im.Col_Button,{0.16,0.29,0.37}},{im.Col_ButtonHovered,{0.21,0.37,0.46}},
    {im.Col_ButtonActive,{0.25,0.43,0.52}},
    {im.Col_Tab,{0.13,0.14,0.16}},{im.Col_TabActive,{0.19,0.20,0.23}},
    {im.Col_TabHovered,{0.27,0.28,0.31}},{im.Col_Text,{0.93,0.94,0.95}},
    {im.Col_TitleBg,{0.13,0.14,0.16}},{im.Col_TitleBgActive,{0.16,0.18,0.21}},
    {im.Col_TitleBgCollapsed,{0.13,0.14,0.16}},{im.Col_Border,{0.34,0.37,0.42}},
    {im.Col_CheckMark,orange},{im.Col_SliderGrab,orange},{im.Col_SliderGrabActive,orange},
    {im.Col_ResizeGrip,{0.45,0.48,0.52}},{im.Col_ResizeGripHovered,orange},{im.Col_ResizeGripActive,orange},
    {im.Col_ScrollbarBg,{0.07,0.08,0.09}},{im.Col_ScrollbarGrab,{0.72,0.29,0}},
    {im.Col_ScrollbarGrabHovered,orange},{im.Col_ScrollbarGrabActive,orange},
    {im.Col_Header,{0.16,0.18,0.21}},{im.Col_HeaderHovered,{0.25,0.27,0.31}},
    {im.Col_HeaderActive,{0.32,0.23,0.17}},{im.Col_TableHeaderBg,{0.16,0.18,0.21}},
    {im.Col_TableRowBg,{0.10,0.11,0.13}},{im.Col_TableRowBgAlt,{0.15,0.16,0.18}}}
  for _,v in ipairs(values) do im.PushStyleColor2(v[1],im.ImVec4(v[2][1],v[2][2],v[2][3],1)) end
  local s=im.GetFontSize()/16
  im.PushStyleVar1(im.StyleVar_WindowRounding,6*s)
  im.PushStyleVar1(im.StyleVar_ChildRounding,4*s)
  im.PushStyleVar1(im.StyleVar_FrameRounding,4*s)
  im.PushStyleVar1(im.StyleVar_TabRounding,3*s)
  im.PushStyleVar2(im.StyleVar_FramePadding,im.ImVec2(9*s,5*s))
  im.PushStyleVar2(im.StyleVar_ItemSpacing,im.ImVec2(7*s,5*s))
  im.PushStyleVar2(im.StyleVar_WindowPadding,im.ImVec2(10*s,8*s))
  return #values,7
end
function M.tabs(im,ctl)
  local requested=ctl.tabRequest
  ctl.tabRequest=nil
  if im.BeginTabBar('##earth-profiler-tabs',0) then
    for _,tab in ipairs(V.tabs) do
      if im.BeginTabItem(tab[2],nil,requested==tab[1] and im.TabItemFlags_SetSelected or 0) then
        ctl.page=tab[1]
        local a,b=im.GetItemRectMin(),im.GetItemRectMax()
        rect(im,im.GetWindowDrawList(),a.x,b.y-3,b.x-a.x,3,color(im,orange))
        im.EndTabItem()
      end
    end
    im.EndTabBar()
  end
end
local function action(im,title,callback,primary)
  if primary then
    im.PushStyleColor2(im.Col_Button,im.ImVec4(0.86,0.30,0,1))
    im.PushStyleColor2(im.Col_ButtonHovered,im.ImVec4(1,0.40,0,1))
    im.PushStyleColor2(im.Col_ButtonActive,im.ImVec4(0.72,0.23,0,1))
  end
  local clicked=im.Button(title)
  if primary then im.PopStyleColor(3) end
  if clicked then callback() end
end
local function selector(im,id,value,width,callback)
  local p=im.GetCursorScreenPos()
  local h=im.GetFontSize()+10*im.GetFontSize()/16
  im.PushStyleColor2(im.Col_Button,im.ImVec4(0.055,0.063,0.078,1))
  local clicked=im.Button(id,im.ImVec2(width,h))
  im.PopStyleColor(1)
  local dl=im.GetWindowDrawList()
  local c=color(im,V.colors.frame)
  label(im,dl,p.x+6,p.y+5,'<',c)
  label(im,dl,p.x+(width-im.CalcTextSize(value).x)/2,p.y+5,value,c)
  label(im,dl,p.x+width-15,p.y+5,'>',c)
  rect(im,dl,p.x+width*0.32,p.y+h-2,width*0.36,2,color(im,orange))
  if clicked then callback(im.IsItemHovered() and im.GetMousePos().x<p.x+width*0.3 and -1 or 1) end
end
function M.toolbar(im,ctl,frozen,view)
  local s=im.GetFontSize()/16
  local compact=im.GetContentRegionAvail().x<820*s
  local p=im.GetCursorScreenPos()
  rect(im,im.GetWindowDrawList(),p.x,p.y+7*s,7*s,7*s,color(im,frozen and V.colors.disk or V.colors.network))
  im.Dummy(im.ImVec2(10*s,20*s));im.SameLine();text(im,frozen and 'Held display' or 'Capturing')
  im.SameLine();action(im,frozen and 'Resume display' or 'Freeze display',function() ctl.setFrozen(not frozen) end)
  local function controls()
    selector(im,'##profiler-mode',ctl.mode=='full' and 'Full' or 'Essential',118*s,function() ctl.chooseMode(ctl.mode=='full' and 'essential' or 'full') end)
    im.SameLine()
    selector(im,'##profiler-history',string.format('%.2g s',V.seconds),92*s,function(direction)
      local choices={5,10,30}
      local index=1
      for i,value in ipairs(choices) do if V.seconds>=value then index=i end end
      V.seconds=choices[(index-1+direction)%#choices+1]
    end)
    im.SameLine()
    selector(im,'##profiler-budget',string.format('%d FPS',math.floor(1000/view.frameDetail.budgetMs+0.5)),112*s,ctl.toggleBudget)
    if not compact then im.SameLine() end
    action(im,'Export',ctl.saveSnapshot,true)
  end
  if not compact then im.SameLine();controls() end
  im.SameLine()
  if im.Button('More...') then im.OpenPopup('##profiler-options') end
  if im.BeginPopup('##profiler-options') then
    if compact then controls() end
    text(im,'Collection mode (starts a fresh capture)')
    buttonRow(im,{{'Essential',function() ctl.chooseMode('essential') end},{'Full',function() ctl.chooseMode('full') end}})
    action(im,'Status overlay',function() ctl.setCompact(true) end)
    action(im,'Stop profiling',ctl.stop)
    wrap(im,string.format('%d frames sampled / %.1f s elapsed. Freezing holds the display; collection continues.',view.capturedFrames,view.elapsedMs/1000))
    im.EndPopup()
  end
end
local function metrics(im,view,tab,width)
  local h=V.history(view)
  local io=view.server and view.server.io
  local r=io and io.rates or {}
  local f=V.current(view)
  local items
  if tab=='all' then items={{'Frame avg',ms(V.mean(h,'ms')),'frame','ms'},{'GPU avg',ms(V.mean(h,'gpuMs')),'gpu','gpuMs'},
    {'CPU avg',ms(V.mean(h,'cpuMs')),'cpu','cpuMs'},{'Received',rate(r.rxBps),'network','rxBps'},{'Cache reads',rate(r.cacheReadBps),'disk','cacheReadBps'}}
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
  local font=im.GetFontSize()
  local columns,rows,cell=V.metricLayout(width,#items,font)
  local p,dl=im.GetCursorScreenPos(),im.GetWindowDrawList()
  local height=font*(tab=='all' and 4.0 or 3.1)
  im.Dummy(im.ImVec2(width,height*rows))
  for i,item in ipairs(items) do
    local x,y=p.x+((i-1)%columns)*cell,p.y+math.floor((i-1)/columns)*height
    rect(im,dl,x,y,cell-4,height-4,color(im,{0.15,0.16,0.19}))
    local tint=V.colors[item[3]] or V.colors.frame
    rect(im,dl,x+8,y+7,5,5,color(im,tint))
    label(im,dl,x+18,y+4,item[1],color(im,V.colors.frame))
    -- Reserve room for long unavailable values at normal font size.
    local value=item[2]
    if im.CalcTextSize(value).x>cell-16 then value=value:gsub('Unavailable','N/A'):gsub('Warming up','Pending') end
    label(im,dl,x+8,y+font+7,value,color(im,V.colors.frame))
    if tab=='all' then
      local samples=(item[3]=='network' or item[3]=='disk') and (view.ioHistory or {}) or h
      local first,last=V.range(view)
      local peak=0
      for _,sample in ipairs(samples) do
        if sample.observedAtMs>=first and sample.observedAtMs<=last then peak=math.max(peak,sample[item[4]] or 0) end
      end
      local prior
      for _,sample in ipairs(samples) do
        local v=sample[item[4]]
        if v~=nil and sample.observedAtMs>=first and sample.observedAtMs<=last then
          local point={x=x+8+(cell-20)*(sample.observedAtMs-first)/(last-first),y=y+height-8-(peak>0 and v/peak or 0)*font*0.65,at=sample.observedAtMs}
          if prior and (not sample.intervalS or point.at-prior.at<=sample.intervalS*1000+1) then line(im,dl,prior.x,prior.y,point.x,point.y,color(im,tint)) end
          prior=point
        else prior=nil end
      end
    end
  end
end
local function graph(im,view,ctl,kind,title,width,height,scale,keys,axis)
  text(im,title)
  local p=im.GetCursorScreenPos()
  im.InvisibleButton('##history-'..kind,im.ImVec2(width,height))
  local hovered=im.IsItemHovered()
  local first,last=V.range(view)
  local font=im.GetFontSize()
  local left=font*3.4
  local w,h=math.max(1,width-left-4),math.max(1,height-(axis and font*1.6 or 0))
  local x,y=p.x+left,p.y
  local dl=im.GetWindowDrawList()
  local white=color(im,V.colors.frame)
  local gray=color(im,V.colors.muted)
  rect(im,dl,x,y,w,h,color(im,{0.075,0.085,0.10}))
  local function tx(t) return x+clip((t-first)/(last-first),0,1)*w end
  local function ty(v) return y+h-clip(v/scale,0,1)*h end
  for _,fraction in ipairs(h<font*3 and {0,1} or {0,0.5,1}) do
    local yy=y+h-fraction*h
    line(im,dl,x,yy,x+w,yy,color(im,V.colors.muted,0.25))
    label(im,dl,p.x,clip(yy-font/2,y,y+h-font),string.format('%.1f',scale*fraction),gray)
  end
  local count=0
  if kind=='cpu' or kind=='gpu' then
    for _,f in ipairs(V.columns(view,w,kind)) do
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
      if h>=font*3 then label(im,dl,x+4,y+2,'White: frame interval',white) end
    end
    local budget=view.frameDetail.budgetMs
    if budget<scale then
      for xx=x,x+w,9 do line(im,dl,xx,ty(budget),math.min(x+w,xx+5),ty(budget),white) end
      if h>=font*3 then label(im,dl,x+4,ty(budget)-font,string.format('%.1f ms budget',budget),white) end
    end
  else
    for ki,key in ipairs(keys) do
      local prior
      for _,sample in ipairs(view.ioHistory or {}) do
        local raw=sample[key]
        if sample.observedAtMs>=first and sample.observedAtMs<=last and raw~=nil then
          local point={x=tx(sample.observedAtMs),y=ty(raw/1048576),at=sample.observedAtMs}
          if prior and sample.intervalS and point.at-prior.at<=sample.intervalS*1000+1 then
            if ki==1 then
              im.ImDrawList_AddQuadFilled(dl,im.ImVec2(prior.x,y+h),im.ImVec2(prior.x,prior.y),im.ImVec2(point.x,point.y),im.ImVec2(point.x,y+h),color(im,V.colors[kind],0.22))
            end
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
  if axis then
    local ticks=math.max(2,math.floor(w/(font*5)))
    for i=0,ticks do
      local at=first+(last-first)*i/ticks
      local s=(i==ticks and last==view.elapsedMs) and 'Now' or string.format('%.1f s',(at-view.elapsedMs)/1000)
      label(im,dl,clip(tx(at)-im.CalcTextSize(s).x/2,x,x+w-im.CalcTextSize(s).x),y+h+2,s,gray)
    end
  end
  if hovered then
    local at=first+clip((im.GetMousePos().x-x)/w,0,1)*(last-first)
    local picked=V.nearest(view,at)
    if picked then
      im.BeginTooltip();text(im,string.format('Frame #%d | %.2f ms | %.3f s',picked.id,picked.ms,picked.observedAtMs/1000))
      if kind=='cpu' or kind=='gpu' then
        for i,row in ipairs(V.rank(picked[kind])) do if i<=8 then text(im,row.name..'  '..ms(row.ms)) end end
        text(im,string.format('Dashed line: %.2f ms budget',view.frameDetail.budgetMs))
        if kind=='cpu' then text(im,'White trace: wall-frame interval, separate from CPU work.') end
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
  local font=im.GetFontSize()
  local laneHeight=math.max(font*1.4,(height-4*(font+10*font/16)-font*1.6)/4)
  local compact=width<700*font/16
  if tab=='all' or tab=='cpu' then graph(im,view,ctl,'cpu',compact and 'CPU / ms' or 'CPU phases + waits / ms',width,tab=='all' and laneHeight or height-font*2,scale,nil,tab~='all') end
  if tab=='all' or tab=='gpu' then graph(im,view,ctl,'gpu',compact and 'GPU / ms' or 'GPU / ms - delayed query',width,tab=='all' and laneHeight or height-font*2,scale,nil,tab~='all') end
  if tab=='all' or tab=='network' then
    local keys={'rxBps','txBps'}
    graph(im,view,ctl,'network',compact and 'Network / MiB/s' or 'Network / MiB/s - received + known sent',width,tab=='all' and laneHeight or height-font*2,ioScale(view,keys),keys,tab~='all')
  end
  if tab=='all' or tab=='disk' then
    local keys={'cacheReadBps','cacheOutputBps'}
    graph(im,view,ctl,'disk',compact and 'Cache / MiB/s' or 'Cache / MiB/s - reads + produced files',width,tab=='all' and laneHeight+font*1.6 or height-font*2,ioScale(view,keys),keys,true)
  end
end
local rangeDrag
local function overview(im,view,ctl,width)
  local font=im.GetFontSize()
  local p,dl=im.GetCursorScreenPos(),im.GetWindowDrawList()
  local height=font*1.8
  im.InvisibleButton('##profiler-range',im.ImVec2(width,height))
  local history=view.frameDetail.history
  local finish=math.max(1,view.elapsedMs)
  local start=math.max(0,finish-30000)
  local first,last=V.range(view)
  local function x(t) return p.x+clip((t-start)/(finish-start),0,1)*width end
  rect(im,dl,p.x,p.y,width,height,color(im,{0.065,0.07,0.08}))
  local peak=V.scale(history,view.frameDetail.budgetMs)
  for _,f in ipairs(history) do
    if f.observedAtMs>=start then
      local hh=clip(f.ms/peak,0,1)*(height-4)
      rect(im,dl,x(f.observedAtMs),p.y+height-hh,math.min(2,p.x+width-x(f.observedAtMs)),hh,color(im,V.colors.muted))
    end
  end
  local a,b=x(first),x(last)
  rect(im,dl,a,p.y,b-a,height,color(im,orange,0.14))
  for _,xx in ipairs({a,b}) do rect(im,dl,clip(xx-2,p.x,p.x+width-4),p.y,4,height,color(im,V.colors.frame)) end
  if im.IsItemHovered() then
    im.SetMouseCursor(im.MouseCursor_ResizeEW)
    im.BeginTooltip();text(im,'Drag handles to zoom; drag inside the range to pan. Follow live resets the range.');im.EndTooltip()
    if im.IsMouseClicked(0) then
      local mouse=im.GetMousePos().x
      rangeDrag={kind=math.abs(mouse-a)<8 and 'start' or math.abs(mouse-b)<8 and 'end' or 'pan',x=mouse,first=first,last=last}
      ctl.setFrozen(true)
    end
  end
  if im.IsItemActive() and rangeDrag then
    local delta=(im.GetMousePos().x-rangeDrag.x)/width*(finish-start)
    local lo,hi=rangeDrag.first,rangeDrag.last
    if rangeDrag.kind=='start' then lo=clip(lo+delta,math.max(start,hi-30000),hi-250)
    elseif rangeDrag.kind=='end' then hi=clip(hi+delta,lo+250,math.min(finish,lo+30000))
    else delta=clip(delta,start-lo,finish-hi);lo,hi=lo+delta,hi+delta end
    V.setRange(view,lo,hi)
  end
  if not im.IsMouseDown(0) then rangeDrag=nil end
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
    text(im,'CPU work: '..ms(f.cpuMs));text(im,'GPU query: '..ms(f.gpuMs));text(im,'GPU wait: '..ms(f.waitMs))
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
    text(im,'Observed work')
    im.SetNextItemWidth(math.min(width,340*im.GetFontSize()/16))
    im.InputTextWithHint('##profiler-filter','Filter activity',activityFilter,128)
    local query=ffi.string(activityFilter)
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
    local filtered={}
    for _,row in ipairs(rows) do if V.matches(row,query) then filtered[#filtered+1]=row end end
    if #filtered>0 then tableRows(im,'##observed-work',{'Subsystem','Activity','Duration','Scope'},filtered) else text(im,query~='' and 'No activities match this filter.' or 'No detailed work reported yet.') end
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
  text(im,'Shared history');im.SameLine()
  action(im,'Follow live',function() V.follow();ctl.setFrozen(false) end)
  im.SameLine()
  action(im,'Inspect worst',function()
    local worst
    for _,f in ipairs(V.history(view)) do if not worst or f.ms>worst.ms then worst=f end end
    if worst then ctl.setFrozen(true);V.select(worst) end
  end)
  local available=im.GetContentRegionAvail()
  local layout=V.layout(width,available.y,scale)
  -- Four readable plots, with one shared axis. Short windows scroll instead of shrinking text.
  if tab=='all' then layout.chartHeight=math.max(220*scale,layout.chartHeight) end
  local overviewHeight=layout.wide and 34*scale or 0
  local top=im.GetCursorScreenPos()
  if im.BeginChild1('##profiler-charts',im.ImVec2(layout.chartWidth,layout.chartHeight+overviewHeight),false) then
    local inner=im.GetContentRegionAvail()
    charts(im,view,ctl,tab,inner.x,math.min(layout.chartHeight,inner.y-overviewHeight))
    if layout.wide then overview(im,view,ctl,im.GetContentRegionAvail().x) end
  end
  im.EndChild()
  if layout.wide then
    im.SetCursorScreenPos(im.ImVec2(top.x+layout.chartWidth+3,top.y))
    -- Drag right to widen the charts, left to widen the inspector.
    local chartWidth,dragged=splitter(im,'##profiler-vsplit',im.ImVec2(5*scale,layout.chartHeight+overviewHeight),'x',layout.chartWidth,width*0.64-12*scale,width-230*scale-12*scale)
    if dragged then V.inspectorWidth=(width-chartWidth-12*scale)/scale end
    im.SetCursorScreenPos(im.ImVec2(top.x+layout.chartWidth+12*scale,top.y))
    if im.BeginChild1('##profiler-inspector',im.ImVec2(layout.inspector,layout.chartHeight+overviewHeight),true) then inspector(im,view,ctl,tab,im.GetContentRegionAvail().x) end
    im.EndChild()
  end
  im.SetCursorScreenPos(im.ImVec2(top.x,top.y+layout.chartHeight+overviewHeight+4*scale))
  local chartHeight,dragged=splitter(im,'##profiler-hsplit',im.ImVec2(width,5*scale),'y',layout.chartHeight,160*scale,math.max(160*scale,available.y-90*scale))
  if dragged then V.chartHeight=chartHeight/scale end
  if not layout.wide then
    local f=V.current(view)
    if im.CollapsingHeader1((V.selection() and 'Selected frame  ' or 'Latest frame  ')..ms(f and f.ms)..' / Details###profiler-selection') then inspector(im,view,ctl,tab,width) end
    if im.CollapsingHeader1(tab=='all' and 'Observed work###profiler-detail' or 'Resource details###profiler-detail') then detail(im,view,ctl,tab,width) end
  else detail(im,view,ctl,tab,width) end
end
local function footerLines(view,tab)
  if tab=='server' then return {'Server timings are separate from game frames. [i]'} end
  return {'GPU: delayed query | I/O: sampled intervals | [i]'}
end
function M.footerHeight(im,view,tab,width)
  local height=4
  for _,s in ipairs(footerLines(view,tab)) do height=height+im.CalcTextSize(s,nil,false,width).y+4 end
  return height
end
function M.footer(im,view,tab)
  for _,s in ipairs(footerLines(view,tab)) do wrap(im,s) end
  if im.IsItemHovered() then
    im.BeginTooltip()
    wrap(im,string.format('%d frames retained. Blank time has no captured samples. CPU and GPU overlap; Earth Lua is included in CPU work.',view.frames))
    wrap(im,'I/O intervals are arrival-aligned, not per-frame events. Sent bytes are estimated. Cache output measures produced files, not physical writes.')
    wrap(im,'Freeze holds the display while collection continues. Server timings are separate from game frames.')
    im.EndTooltip()
  end
end
return M
