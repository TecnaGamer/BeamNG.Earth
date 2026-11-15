-- Native geometry only. The existing traffic controllers own every lamp phase.
local M={}
local consumer=require('beamngEarth/nl_signal_assemblies')
local reader,raw,queued,refs,owned=nil,nil,nil,{},{}
local graphSerial,renderRevision,lastSignature,lastAck=nil,0,nil,nil
local send,accepted
local phaseAt,frameScope=0,nil
local surfaceAt,selectionToken=0,nil
M.stats={packets=0,accepted=0,errors=0,parts=0,liveOwners=0}
local function receiver()
  if not reader then reader=consumer.new(require('beamngEarth/nl_signal_forest').new()) end
  return reader
end
function M.configure(sender,onAccepted)send,accepted=sender,onAccepted end
function M.graph(serial)
  graphSerial=serial;refs={};lastSignature=nil
end
function M.register(name,inst,controller,sequence,a)
  refs[name]={inst=inst,controller=controller,sequence=sequence,
    spot={a[8]or a[2],a[9]or a[3],a[10]or a[4]}}
end
function M.owns(name)return owned[name]==true end
function M.spots()
  local out={}
  for name in pairs(owned)do if refs[name]then out[#out+1]=refs[name].spot end end
  return out
end
local function surfacesValid(rows)
  if type(rows)~='table'or #rows>64 then return false end
  for _,s in ipairs(rows)do
    if type(s)~='table'or type(s.key)~='string'or #s.key>80
      or type(s.h)~='string'or type(s.skin)~='string'or type(s.exact)~='table'then return false end
  end
  return true
end
function M.onMessage(msg,anchor)
  if msg.anchorId~=anchor or type(msg.path)~='string' or msg.path:find('..',1,true) then return false end
  local ok,p=pcall(jsonReadFile,msg.path)
  if not ok or type(p)~='table' or p.format~=1 or p.anchor~=anchor or p.epoch~=msg.epoch
      or p.revision~=msg.revision or type(p.owners)~='table' or #p.owners>512
      or type(p.surfaces)~='table' or #p.surfaces>64
      or type(p.parts)~='table' or #p.parts>4096 or type(p.ground)~='string'
      or type(p.epoch)~='number' or p.epoch%1~=0 or p.epoch<0
      or type(p.revision)~='number' or p.revision%1~=0 or p.revision<0 then return false end
  if not surfacesValid(p.surfaces)then return false end
  if p.groups~=nil then
    if type(p.groups)~='table'or #p.groups>128 then return false end
    local seen,owners,keys={},{},{}
    for _,g in ipairs(p.groups)do
      if type(g)~='table'or type(g.key)~='string'or #g.key>128 or keys[g.key]
        or type(g.first)~='number'or g.first%1~=0 or g.first<1
        or type(g.count)~='number'or g.count%1~=0 or g.count<1 or g.first+g.count-1>#p.parts
        or type(g.owners)~='table'or #g.owners<1 or #g.owners>16 or not surfacesValid(g.surfaces)then return false end
      keys[g.key]=true
      for i=g.first,g.first+g.count-1 do if seen[i]then return false end;seen[i]=true end
      for _,name in ipairs(g.owners)do
        if type(name)~='string'or owners[name]then return false end;owners[name]=true
      end
    end
    for i=1,#p.parts do if not seen[i]then return false end end
    for _,name in ipairs(p.owners)do if not owners[name]then return false end;owners[name]=nil end
    if next(owners)then return false end
  end
  local latest=queued or raw
  if latest and (p.epoch<latest.epoch or p.epoch==latest.epoch and p.revision<=latest.revision)then return false end
  queued=p;surfaceAt=0;M.stats.packets=M.stats.packets+1
  return true
end
local function phase(r)
  local c,s=r and r.controller,r and r.sequence
  local state=c and s and s.controllerStates and s.controllerStates[c.name]
  local base=state and c.states and c.states[state.stateIdx]
  local data=base and c.getStateData and c:getStateData(base.state)
  local lights=data and data.lights
  if not lights then return 'off' end
  for i,name in ipairs({'red','amber','green'})do
    if lights[i] and lights[i]~='black' then return name end
  end
  return 'off'
end
function M.update(anchor,ground,budget)
  local now=os.clock()
  if queued and (not queued.groups or now>=surfaceAt) then
    surfaceAt=now+0.25
    if queued.anchor==anchor and queued.ground==ground then
      local roads=require('beamngEarth/roads')
      if queued.groups then
        local chosen,token,waiting={},{tostring(queued.epoch),tostring(queued.revision)},nil
        for _,g in ipairs(queued.groups)do
          local ready,why=roads.signalSurfacesReady(anchor,g.surfaces)
          if ready then chosen[#chosen+1]=g;token[#token+1]=g.key else waiting=why or 'surfaces_pending' end
        end
        token=table.concat(token,'|');M.stats.waiting=waiting
        if token~=selectionToken then
          local p={format=1,bundle=true,anchor=queued.anchor,epoch=queued.epoch,revision=queued.revision,
            graphSerial=queued.graphSerial,ground=queued.ground,parts={},owners={},selection=token}
          for _,g in ipairs(chosen)do
            for i=g.first,g.first+g.count-1 do p.parts[#p.parts+1]=queued.parts[i] end
            for _,name in ipairs(g.owners)do p.owners[#p.owners+1]=name end
          end
          raw=p;selectionToken=token;lastSignature=nil;phaseAt=0
        end
        if not waiting then queued=nil end
      else
        local ready,why=roads.signalSurfacesReady(anchor,queued.surfaces)
        if ready then M.stats.waiting=nil;raw=queued;queued=nil;lastSignature=nil;phaseAt=0
        else M.stats.waiting=why or 'surfaces_pending' end
      end
    else M.stats.waiting='terrain_version' end
  end
  if not raw or raw.anchor~=anchor then return end
  local graphReady=raw.graphSerial==graphSerial
  local r=receiver()
  r:setEnabled(true)
  local scope=tostring(raw.epoch)..':'..anchor
  if scope~=frameScope then
    if not r:setScope(raw.epoch,anchor)then return end
    r:setFrame({0,0,0,0,0,0,1});frameScope=scope
  end
  if now<phaseAt and not r.pending then return end
  if now>=phaseAt then
  phaseAt=now+0.1
  local channels,signature={}, {tostring(raw.epoch),tostring(raw.revision)}
  for _,name in ipairs(raw.owners)do
    local color=graphReady and phase(refs[name])or 'off';signature[#signature+1]=name..':'..color
    local lanes={};for i=1,16 do lanes[i]=color end;channels[name]=lanes
  end
  signature=table.concat(signature,'|')
  if signature~=lastSignature then
    local parts,why=consumer.farParts(raw,channels)
    if not parts then M.stats.errors=M.stats.errors+1;return false,why end
    renderRevision=renderRevision+1
    local ok,err=r:enqueue({format=1,revision=renderRevision,epoch=raw.epoch,anchor=anchor,parts=parts})
    if not ok then M.stats.errors=M.stats.errors+1;return false,err end
    lastSignature=signature;M.stats.parts=#parts
  end
  end
  local ok,why=r:tick(budget or 16)
  if ok then
    local token=tostring(raw.epoch)..':'..raw.revision..':'..(raw.selection or '')
    if token~=lastAck then
      owned={};for _,name in ipairs(raw.owners)do owned[name]=true end
      M.stats.liveOwners=#raw.owners
      -- Linked stock objects are detached only after every new item exists.
      if accepted then accepted(owned)end
      if send then send({type='nl_signals_ack',anchorId=anchor,epoch=raw.epoch,revision=raw.revision,owners=raw.owners})end
      lastAck=token;M.stats.accepted=M.stats.accepted+1
    end
  elseif why=='create_failed' then lastSignature=nil;M.stats.errors=M.stats.errors+1 end
  return ok,why
end
function M.clear(levelGone)
  if reader then
    if levelGone then reader:forgetLevel()else reader:setEnabled(false)end
  end
  raw=nil;queued=nil;refs={};owned={};graphSerial=nil;lastSignature=nil;lastAck=nil;phaseAt=0;frameScope=nil
  surfaceAt=0;selectionToken=nil;M.stats.waiting=nil
  M.stats.liveOwners=0
end
return M
