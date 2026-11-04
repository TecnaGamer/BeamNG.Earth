-- Stacked per-frame histories. Layout is pure data so tests and exported previews
-- use exactly the same geometry as the ImGui window.
local frames = require('beamngEarth/frameprofile')
local M = {}
local selected, visible, fitSpikes = nil, 180, true
local palette = {
  {0.25,0.66,0.95}, {0.96,0.57,0.24}, {0.43,0.79,0.48}, {0.75,0.52,0.95},
  {0.97,0.75,0.26}, {0.23,0.82,0.79}, {0.94,0.42,0.56}, {0.55,0.70,0.96},
  {0.70,0.80,0.31}, {0.87,0.54,0.81}, {0.39,0.78,0.98}, {0.83,0.65,0.44},
}
local colorCache = {}
function M.rgb(name)
  if name:find('Unattributed') or name == 'others' then return {0.43,0.47,0.53} end
  if name == 'FPSLimiter' or name == 'WaitNextFrame' or name == 'FramePresent' then return {0.67,0.59,0.30} end
  local hash = 0
  for i = 1, #name do hash = (hash * 31 + name:byte(i)) % 65521 end
  return palette[hash % #palette + 1]
end
local function u32(im, name)
  if not colorCache[name] then local c = M.rgb(name); colorCache[name] = im.GetColorU322(im.ImVec4(c[1],c[2],c[3],1)) end
  return colorCache[name]
end
local function col(im, r,g,b,a) return im.GetColorU322(im.ImVec4(r,g,b,a or 1)) end
local function text(im, s) im.TextUnformatted(s) end
local function wrap(im, s) im.PushTextWrapPos(0); text(im, s); im.PopTextWrapPos() end
local function rect(im, dl, x,y,w,h,c)
  if w > 0 and h > 0 then im.ImDrawList_AddRectFilled(dl, im.ImVec2(x,y), im.ImVec2(x+w,y+h), c) end
end
local function ms(v) return v and string.format('%.2f ms',v) or 'unavailable' end
local function sorted(t)
  local out = {}
  for name,value in pairs(t or {}) do if value > 0 then out[#out+1] = {name=name,ms=value} end end
  table.sort(out, function(a,b) return a.ms == b.ms and a.name < b.name or a.ms > b.ms end)
  return out
end

function M.reset() selected = nil; colorCache = {} end
function M.select(frame) selected = frame end

function M.layout(history, lane, width, height, scale, limit)
  local result = { bars = {}, columns = {}, scale = scale, lane = lane }
  local first = math.max(1, #history - (limit or visible) + 1)
  local n = #history - first + 1
  if n <= 0 then return result end
  local step = width / n
  for i = first, #history do
    local f, total, names = history[i], 0, {}
    for name in pairs(history[i][lane] or {}) do names[#names+1] = name end
    table.sort(names)
    local x = (i-first)*step
    local column = { frame=f, x=x, width=step }
    result.columns[#result.columns+1] = column
    for _,name in ipairs(names) do
      local value = f[lane][name]
      local bottom, top = math.min(height,total/scale*height), math.min(height,(total+value)/scale*height)
      if top-bottom >= 0.35 then result.bars[#result.bars+1] = {name=name, x=x, y=height-top,
        width=math.max(0.5,step-0.4),height=top-bottom,ms=value,frameId=f.id} end
      total = total + value
    end
    column.totalMs, column.clipped = total, total > scale
  end
  return result
end

function M.hit(layout, x)
  if #layout.columns == 0 or x < 0 then return nil end
  for _, c in ipairs(layout.columns) do if x >= c.x and x < c.x+c.width then return c.frame end end
end

local function drawGraph(im, snapshot, lane, title, width, scale, height)
  text(im, title .. string.format(' | %.1f ms full scale', scale))
  height = height or 115
  local p = im.GetCursorScreenPos()
  im.InvisibleButton('##earth-frame-stack-'..lane, im.ImVec2(width,height))
  local hovered = im.IsItemHovered and im.IsItemHovered()
  local clicked = im.IsItemClicked and im.IsItemClicked(0)
  local dl = im.GetWindowDrawList()
  local layouts = snapshot._layouts or {}; snapshot._layouts = layouts
  local key = width..':'..height..':'..scale..':'..visible
  local cached = layouts[lane]
  if not cached or cached.key ~= key then
    cached = { key=key, layout=M.layout(snapshot.history,lane,width,height,scale) }
    layouts[lane] = cached -- Only one geometry per lane, even while resizing a frozen display.
  end
  local layout = cached.layout
  rect(im,dl,p.x,p.y,width,height,col(im,0.075,0.09,0.12))
  for _, b in ipairs(layout.bars) do rect(im,dl,p.x+b.x,p.y+b.y,b.width,b.height,u32(im,b.name)) end
  for _, c in ipairs(layout.columns) do
    if c.clipped then rect(im,dl,p.x+c.x,p.y,c.width,3,col(im,1,0.25,0.22)) end
    if selected and c.frame.id == selected.id then rect(im,dl,p.x+c.x,p.y,1,height,col(im,1,1,1)) end
  end
  if snapshot.budgetMs <= scale then
    rect(im,dl,p.x,p.y+height-snapshot.budgetMs/scale*height,width,1,col(im,0.95,0.95,0.95,0.65))
  end
  if hovered and im.GetMousePos then
    local f=M.hit(layout, im.GetMousePos().x-p.x)
    if f then
      if clicked then selected=f end
      if im.BeginTooltip then
        im.BeginTooltip()
        text(im,string.format('Frame #%d | interval %.2f ms | %s',f.id,f.ms,title))
        local list=sorted(f[lane])
        for i=1,math.min(10,#list) do text(im,string.format('%s: %.3f ms',list[i].name,list[i].ms)) end
        if lane=='gpu' then text(im,'Latest completed GPU query; may describe an earlier render frame.') end
        im.EndTooltip()
      end
    end
  end
  local legend=snapshot.series[lane]
  if #legend>0 then
    local pos=im.GetCursorScreenPos()
    im.InvisibleButton('##frame-legend-'..lane,im.ImVec2(width,36))
    local legendDl=im.GetWindowDrawList()
    for i=1,math.min(6,#legend) do
      local row=legend[i]
      local x=pos.x+((i-1)%3)*width/3
      local y=pos.y+math.floor((i-1)/3)*18
      rect(im,legendDl,x,y+3,9,9,u32(im,row.name))
      im.ImDrawList_AddText1(legendDl,im.ImVec2(x+13,y),col(im,0.88,0.91,0.96),row.name:sub(1,22),nil)
    end
  end
end

local function breakdown(im, values, lane, width)
  local rows=sorted(values)
  if #rows==0 then return end
  text(im,lane)
  local total=0; for _,row in ipairs(rows) do total=total+row.ms end
  for _,row in ipairs(rows) do
    local p=im.GetCursorScreenPos()
    im.InvisibleButton('##frame-step-'..lane..row.name,im.ImVec2(width,18))
    local dl=im.GetWindowDrawList()
    rect(im,dl,p.x,p.y,width,18,col(im,0.10,0.12,0.15))
    rect(im,dl,p.x,p.y,width*row.ms/math.max(0.001,total),18,u32(im,row.name))
    local label=string.format('%s   %.3f ms   %.1f%% of reported %s phases',row.name,row.ms,100*row.ms/math.max(0.001,total),lane)
    im.ImDrawList_AddText1(dl,im.ImVec2(p.x+5,p.y+1),col(im,0,0,0),label,nil)
    im.ImDrawList_AddText1(dl,im.ImVec2(p.x+4,p.y),col(im,1,1,1),label,nil)
  end
end

function M.draw(im, snapshot, width, graphHeight)
  if snapshot.count==0 then text(im,'Collecting frame history...'); return end
  width=width or 610
  for _,n in ipairs(snapshot.mode=='essential' and {180} or {180,300,600}) do
    if im.Button(tostring(n)..' frames') then visible=n end
    im.SameLine()
  end
  if im.Button(fitSpikes and 'Fit spikes' or 'Detail scale') then fitSpikes=not fitSpikes end
  if im.Button('Follow latest frame') then selected=nil end
  im.SameLine()
  if im.Button('Inspect worst frame') then selected=snapshot.worst end
  if im.Button('60 FPS budget') then frames.BUDGET_MS=1000/60 end
  im.SameLine()
  if im.Button('120 FPS budget') then frames.BUDGET_MS=1000/120 end
  for i=1,math.min(2,#snapshot.observations) do wrap(im,snapshot.observations[i].text) end
  local scale=snapshot.budgetMs*1.25
  local first=math.max(1,#snapshot.history-visible+1)
  if fitSpikes then
    for i=first,#snapshot.history do
      local f=snapshot.history[i]
      scale=math.max(scale,f.ms,f.cpuSumMs,f.gpuMs or f.gpuSumMs)
    end
    scale=scale*1.05
  end
  wrap(im,'Each column is one captured frame, oldest to newest. CPU and GPU run in parallel: separate lanes use the same scale. White line = frame budget; red cap = off-scale spike. Hover for timings; click to inspect.')
  if #snapshot.series.cpu>0 then drawGraph(im,snapshot,'cpu',snapshot.mode=='essential' and 'CPU work total' or 'CPU engine phases (includes waits)',width,scale,graphHeight)
  else wrap(im,'CPU phase timings are unavailable; the wall-frame history remains available.') end
  if snapshot.gpuSamples>0 then
    drawGraph(im,snapshot,'gpu',snapshot.mode=='essential' and 'GPU total' or 'GPU passes / latest completed query',width,scale,graphHeight)
    wrap(im,'GPU query results arrive later than CPU phases. These lanes share observation order; exact CPU/GPU frame association is unavailable from this API.')
  else wrap(im,'GPU pass timings are unavailable on this renderer/driver. GPU steps are hidden; totals are retained when reported.') end
  if #snapshot.series.mod>0 then drawGraph(im,snapshot,'mod','Earth Lua scopes (inside CPU Lua; do not add)',width,scale,graphHeight) end
  if im.CollapsingHeader1('Focused observations',im.TreeNodeFlags_DefaultOpen) then
    for i,o in ipairs(snapshot.observations) do
      wrap(im,o.text)
      if o.frame and im.Button('Inspect #'..o.frame..'##observation-'..i) then
        for _,f in ipairs(snapshot.history) do if f.id==o.frame then selected=f; break end end
      end
    end
    wrap(im,'Observations report measured costs and coincidence with slow frames, not proof that a worker caused a stall.')
  end
  local f=selected or snapshot.history[#snapshot.history]
  wrap(im,string.format('%s frame #%d | interval %.2f ms | CPU work %s | GPU %s | GPU wait %s',
    selected and 'Selected' or 'Latest',f.id,f.ms,ms(f.cpuMs),ms(f.gpuMs),ms(f.waitMs)))
  if im.CollapsingHeader1('Selected frame / CPU steps',im.TreeNodeFlags_DefaultOpen) then breakdown(im,f.cpu,'CPU',width) end
  if next(f.gpu) and im.CollapsingHeader1('Selected observation / GPU passes') then breakdown(im,f.gpu,'GPU',width) end
  if next(f.mod) and im.CollapsingHeader1('Selected frame / Earth steps') then breakdown(im,f.mod,'Earth',width) end
  if im.CollapsingHeader1('Phase legend / ranked costs across history') then
    for _,lane in ipairs({'cpu','gpu','mod'}) do
      for _,row in ipairs(snapshot.series[lane]) do
        text(im,string.format('%s / %s | avg %.3f ms | peak %.3f ms | %d slow frames',
          lane:upper(),row.name,row.avgMs,row.maxMs,row.slowFrames))
      end
    end
  end
end

return M
