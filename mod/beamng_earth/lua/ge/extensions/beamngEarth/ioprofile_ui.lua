-- Passive external services, the local world connection, and storage diagnostics.
local M = {}
local filter, sortBy = 'all', 'latency'
local function text(im,s) im.TextUnformatted(tostring(s)) end
local function wrap(im,s) im.PushTextWrapPos(0);text(im,s);im.PopTextWrapPos() end
local function bytes(n)
  if n==nil then return 'unavailable' end
  if n>=1073741824 then return string.format('%.2f GiB',n/1073741824) end
  if n>=1048576 then return string.format('%.2f MiB',n/1048576) end
  if n>=1024 then return string.format('%.1f KiB',n/1024) end
  return string.format('%.0f B',n)
end
local function ms(n) return n and string.format('%.2f ms',n) or 'unavailable' end
local function rate(n) return n and bytes(n)..'/s' or 'warming up' end
M.bytes=bytes

function M.rows(rows, kind, order)
  local out={}
  for _,r in ipairs(rows or {}) do
    local problem=r.health=='connection error' or r.health=='service error' or r.health=='rate limited'
    if kind=='all' or kind=='active' and (r.inflight or 0)>0 or kind=='problems' and problem or kind=='idle' and r.health=='idle' then out[#out+1]=r end
  end
  table.sort(out,function(a,b)
    if order=='name' then return a.endpoint<b.endpoint end
    local av=order=='traffic' and a.receivedBytes or a.responseAvgMs or -1
    local bv=order=='traffic' and b.receivedBytes or b.responseAvgMs or -1
    return av==bv and a.endpoint<b.endpoint or av>bv
  end)
  return out
end

local function plot(im,id,history,key,label,width)
  local values,n={},0
  for _,v in ipairs(history or {}) do if v[key]~=nil then values[n]=v[key];n=n+1 end end
  if n>0 then im.PlotLines1('##io-'..id,values,n,0,label,0,nil,im.ImVec2(width,52)) end
end

function M.drawNetwork(im, data, localLink, rtt, mode, width)
  width=width or 610
  text(im,'Game / world-server connection')
  text(im,localLink.endpoint or 'Endpoint unavailable')
  text(im,string.format('%s | received %s | sent %s | average application RTT %s',
    localLink.state or 'unavailable',bytes(localLink.receivedBytes),bytes(localLink.sentBytes),ms(rtt.avgMs)))
  text(im,string.format('%d RTT replies | %d expired probes | %d socket errors | %d connection attempts',rtt.count or 0,rtt.timeouts or 0,localLink.errors or 0,localLink.connectionAttempts or 0))
  if mode=='full' then text(im,'RTT p95 '..ms(rtt.p95)..' | jitter '..ms(rtt.jitterMs)) end
  wrap(im,'RTT measures the application round trip, including game/server scheduling. Expired probes are not a measurement of network packet loss.')
  if not data then wrap(im,'Service and disk telemetry needs the matching server; no measurements have arrived yet.');return end
  if mode=='essential' then wrap(im,'Essential: basic traffic, average response/request times, failures and disk space. Worker traces, latency distributions, per-read timings and OS process I/O sampling are disabled.') end
  text(im,'External services / observed since this capture began')
  text(im,'Received '..bytes(data.receivedBytes)..' | estimated request bytes '..bytes(data.sentBytes))
  plot(im,'network-rx',data.history,'rxBps','Response payload received / bytes per second',width)
  plot(im,'network-tx',data.history,'txBps','Known request bytes / second (estimate)',width)
  for _,key in ipairs({'all','active','problems','idle'}) do
    if im.Button(key..'##service-filter') then filter=key end
    im.SameLine()
  end
  text(im,'Filter: '..filter)
  for _,key in ipairs({'latency','traffic','name'}) do
    if im.Button('Sort by '..key) then sortBy=key end
    im.SameLine()
  end
  text(im,'')
  local rows=M.rows(data.services,filter,sortBy)
  if #rows==0 then text(im,'No observed services match this filter.') end
  for _,r in ipairs(rows) do
    local heading=string.format('%s | %s | response avg %s',r.endpoint,r.health,ms(r.responseAvgMs))
    if im.CollapsingHeader1(heading..'##service-'..r.endpoint) then
      text(im,string.format('%d requests | %d replies | %d in flight | last activity %.1f s ago',r.requests,r.responses,r.inflight,r.ageS))
      if (r.unfinished or 0)>0 then text(im,tostring(r.unfinished)..' requests have stale/incomplete worker telemetry.') end
      text(im,'Average response headers '..ms(r.responseAvgMs)..' | complete request '..ms(r.requestAvgMs))
      if mode=='full' then
        text(im,'Recent response p95 '..ms(r.responseP95Ms)..' | jitter '..ms(r.jitterMs))
        plot(im,'service-'..r.endpoint,r.responseHistory,'ms','Response-header latency / recent valid samples (ms)',width)
      end
      if r.lastResponseMs then text(im,'Latest observed response headers '..ms(r.lastResponseMs)) end
      text(im,'Received '..bytes(r.receivedBytes)..' | known sent '..bytes(r.sentBytes))
      text(im,string.format('Transport failures %d (%.1f%%) | HTTP errors %d | rate limits %d',r.failures,100*(r.failureShare or 0),r.httpErrors,r.rateLimited))
      text(im,'Last status '..tostring(r.lastStatus or '-')..' | last connection error '..tostring(r.lastError or '-'))
    end
  end
  wrap(im,'Passive observations only: no pings or extra downloads. Response latency includes connection setup and service work. Session time also includes body transfer and caller work inside the download session. HTTP errors are separate from transport failures. Sent bytes are an estimate; TLS/TCP overhead and redirect-hop timings are not measured.')
  if (data.omitted or 0)>0 then text(im,tostring(data.omitted)..' old endpoint records omitted at the capacity limit.') end

end

function M.drawDisk(im, data, mode, width)
  width=width or 610
  if not data then wrap(im,'Disk telemetry needs the matching server; no measurements have arrived yet.');return end
  if im.CollapsingHeader1('Disk space and cache usage',im.TreeNodeFlags_DefaultOpen) then
    for _,v in ipairs(data.volumes or {}) do
      text(im,v.label..' / '..v.path)
      if v.unavailable then text(im,'Volume usage unavailable')
      else
        text(im,'Free '..bytes(v.freeBytes)..' / '..bytes(v.totalBytes)..' | used '..bytes(v.usedBytes))
        if v.totalBytes>0 and v.freeBytes/v.totalBytes<0.05 then wrap(im,'Less than 5% of this volume is free.') end
      end
    end
    local c=data.cache
    if c then text(im,'Tracked cache '..bytes(c.usedBytes)..' | '..tostring(c.files)..' files | quota '..(c.capBytes==0 and 'unlimited' or bytes(c.capBytes)))
      if not c.ready then text(im,'Cache inventory is still loading.') end end
    local d=data.disk or {}
    text(im,'Instrumented cache reads '..bytes(d.readBytes)..' | observed cache output '..bytes(d.outputBytes))
    text(im,string.format('%d read attempts | %d misses | %d read errors',d.reads or 0,d.misses or 0,d.errors or 0))
    plot(im,'cache-read',data.history,'cacheReadBps','Instrumented cache reads / bytes per second',width)
    plot(im,'cache-output',data.history,'cacheOutputBps','Cache output notifications / bytes per second',width)
    if mode=='full' then text(im,'Cache-read operation latency avg '..ms((d.timedReads or 0)>0 and d.readMs/d.timedReads or nil)..' | max '..ms((d.timedReads or 0)>0 and d.maxReadMs or nil)) end
    wrap(im,'Cache-read counters cover instrumented reads. Output notifications count reported produced-file sizes, not physical write traffic. These are separate from total volume capacity.')
  end
  if mode=='full' and im.CollapsingHeader1('Server / worker process I/O / sampled once per second') then
    if #(data.processes or {})==0 then text(im,'OS process I/O counters are unavailable or warming up.') end
    for _,p in ipairs(data.processes or {}) do
      text(im,string.format('Process %d | read %s | write %s',p.pid,rate(p.readBps),rate(p.writeBps)))
      text(im,p.scope)
    end
    plot(im,'process-read',data.history,'processReadBps','Process I/O read bytes per second',width)
    plot(im,'process-write',data.history,'processWriteBps','Process I/O write bytes per second',width)
    wrap(im,'On Windows these counters include process I/O across handles; they do not establish physical disk bandwidth or hardware disk latency.')
  end
end

function M.draw(im, data, localLink, rtt, mode, width)
  M.drawNetwork(im,data,localLink,rtt,mode,width)
  M.drawDisk(im,data,mode,width)
end
return M
