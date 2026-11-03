-- Pure presentation model. Times are observation times, never CPU/GPU frame correlation.
local M = {}
M.tabs = {{'all','All'}, {'gpu','GPU'}, {'cpu','CPU'}, {'network','Network'}, {'disk','Disk'}, {'server','Server'}}
M.colors = {cpu={0.22,0.83,0.88}, gpu={0.70,0.49,0.94}, network={0.37,0.88,0.65},
  disk={0.96,0.72,0.22}, frame={0.91,0.93,0.96}, muted={0.48,0.52,0.59}}
M.seconds, M.inspectorWidth, M.chartHeight = 10, 290, nil
local selected, rangeEnd
local phaseCache=setmetatable({},{__mode='k'})
function M.reset() selected = nil;rangeEnd=nil;phaseCache=setmetatable({},{__mode='k'}) end
function M.select(frame) selected = frame end
function M.follow() selected=nil;rangeEnd=nil end
function M.selection() return selected end
function M.validTab(tab)
  for _,v in ipairs(M.tabs) do if v[1] == tab then return true end end
  return false
end
function M.layout(width, height, scale)
  scale = scale or 1
  local wide = width >= 960 * scale
  local inspector = wide and math.min(width*0.36, math.max(230*scale, M.inspectorWidth*scale)) or 0
  return {wide=wide, compact=width<800*scale, width=width, inspector=inspector,
    chartWidth=math.max(1,width-inspector-(wide and 12*scale or 0)),
    chartHeight=math.max(160*scale, math.min((M.chartHeight or height*0.60/scale)*scale, math.max(160*scale,height-65*scale)))}
end
function M.range(view)
  local finish = math.min(rangeEnd or math.huge,view.elapsedMs or 0)
  return math.max(0, finish-M.seconds*1000), math.max(1,finish)
end
function M.setRange(view,first,last)
  local finish=math.max(1,view.elapsedMs or 0)
  last=math.max(1,math.min(finish,last))
  first=math.max(0,math.min(first,last-250))
  M.seconds=math.max(0.25,math.min(30,(last-first)/1000))
  rangeEnd=last
end
function M.metricLayout(width,count,font)
  local columns=math.max(1,math.min(count,math.floor(width/(font*8.7))))
  return columns,math.floor((count+columns-1)/columns),width/columns
end
-- Coalesce sub-pixel observations without averaging away a short stall.
-- Keep the highest-cost frame in each horizontal pixel; raw export stays intact.
function M.columns(view,width,kind)
  local history=M.history(view)
  local cache=view._presentation.columns or {}
  view._presentation.columns=cache
  local pixels=math.max(1,math.floor(width))
  if cache[kind] and cache[kind].pixels==pixels then return cache[kind].frames end
  local first,last=M.range(view)
  local buckets,out={},{}
  for _,f in ipairs(history) do
    local index=math.min(pixels-1,math.floor((f.observedAtMs-first)/(last-first)*pixels))
    -- The wall-frame trace is drawn separately from raw observations, so a long
    -- idle frame must not displace a neighbouring CPU-work peak in this column.
    local cost=kind=='cpu' and M.total(f.cpu) or math.max(f.gpuMs or 0,M.total(f.gpu))
    if not buckets[index] or cost>buckets[index].cost then buckets[index]={frame=f,cost=cost} end
  end
  for index=0,pixels-1 do if buckets[index] then out[#out+1]=buckets[index].frame end end
  cache[kind]={pixels=pixels,frames=out}
  return out
end
function M.matches(row,query)
  if not query or query=='' then return true end
  return table.concat(row,' '):lower():find(query:lower(),1,true)~=nil
end
function M.history(view)
  if view._presentation and view._presentation.seconds==M.seconds and view._presentation.rangeEnd==rangeEnd then return view._presentation.history end
  local first, last = M.range(view)
  local out = {}
  for _,f in ipairs(view.frameDetail.history) do
    if f.observedAtMs and f.observedAtMs>=first and f.observedAtMs<=last then out[#out+1]=f end
  end
  view._presentation={seconds=M.seconds,rangeEnd=rangeEnd,history=out}
  return out
end
function M.phases(values)
  -- Frame maps are immutable after capture; weak keys release rolled-out frames.
  local cached=phaseCache[values]
  if cached then return cached end
  cached={}
  for name in pairs(values) do cached[#cached+1]=name end
  table.sort(cached)
  phaseCache[values]=cached
  return cached
end
function M.current(view)
  local h = view.frameDetail.history
  return selected or h[#h]
end
function M.nearest(view, time)
  local closest, distance
  for _,f in ipairs(M.history(view)) do
    local d=math.abs(f.observedAtMs-time)
    if not distance or d<distance then closest,distance=f,d end
  end
  return closest
end
function M.ioAt(view, time)
  -- A rate describes the interval ending at its timestamp; never interpolate across missing data.
  for _,p in ipairs(view.ioHistory or {}) do
    if p.intervalS and time <= p.observedAtMs and time >= p.observedAtMs-p.intervalS*1000 then return p end
  end
end
function M.rank(values)
  local out={}
  for name,value in pairs(values or {}) do
    if type(value)=='number' and value==value and value>0 and value<math.huge then out[#out+1]={name=name,ms=value} end
  end
  table.sort(out,function(a,b) return a.ms==b.ms and a.name<b.name or a.ms>b.ms end)
  return out
end
function M.mean(history,key)
  local sum,n=0,0
  for _,f in ipairs(history) do if f[key]~=nil then sum,n=sum+f[key],n+1 end end
  return n>0 and sum/n or nil
end
function M.total(values)
  local sum=0
  for _,r in ipairs(M.rank(values)) do sum=sum+r.ms end
  return sum
end
function M.scale(history,budget)
  local peak=budget*1.25
  for _,f in ipairs(history) do peak=math.max(peak,f.ms,M.total(f.cpu),M.total(f.gpu),f.gpuMs or 0) end
  return peak*1.05
end
function M.ioHistory(data, receivedMs)
  local out={}
  if not data or type(data.observedAt)~='number' or not receivedMs then return out end
  for _,p in ipairs(data.history or {}) do
    if type(p.at)=='number' and p.at<=data.observedAt then
      local q={}
      for k,v in pairs(p) do q[k]=v end
      q.observedAtMs=receivedMs-(data.observedAt-p.at)*1000
      out[#out+1]=q
    end
  end
  return out
end
function M.queueRows(view)
  local map,out={},{}
  for _,job in ipairs(view.server and view.server.active or {}) do
    local key=job.pool or 'worker'
    map[key]=map[key] or {name=key,queued=0,running=0}
    local field=job.worker and 'running' or 'queued'
    map[key][field]=map[key][field]+1
  end
  for _,v in pairs(map) do out[#out+1]=v end
  table.sort(out,function(a,b) return a.name<b.name end)
  return out
end
function M.export(view, tab)
  local capture={}
  for k,v in pairs(view) do if k~='plot' and k~='_presentation' then capture[k]=v end end
  capture.frameDetail={}
  for k,v in pairs(view.frameDetail) do if k~='_layouts' then capture.frameDetail[k]=v end end
  local first,last=M.range(view)
  capture.presentation={tab=tab,selectedFrame=selected,historySeconds=M.seconds,startMs=first,endMs=last,
    timeScope='client observation time; I/O arrival-aligned; GPU queries delayed',
    units={frame='ms',cpu='ms',gpu='ms',network='bytes/s',cache='bytes/s'}}
  return capture
end
return M
