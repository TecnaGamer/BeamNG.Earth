-- Native geometry only. The existing traffic controllers own every lamp phase.
local M={}
local consumer=require('beamngEarth/nl_signal_assemblies')
local reader,raw,queued,refs,owned=nil,nil,nil,{},{}
local graphSerial,renderRevision,lastSignature,lastAck=nil,0,nil,nil
local send,accepted
local phaseAt,frameScope=0,nil
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
function M.onMessage(msg,anchor)
  if msg.anchorId~=anchor or type(msg.path)~='string' or msg.path:find('..',1,true) then return false end
  local ok,p=pcall(jsonReadFile,msg.path)
  if not ok or type(p)~='table' or p.format~=1 or p.anchor~=anchor or p.epoch~=msg.epoch
      or p.revision~=msg.revision or type(p.owners)~='table' or #p.owners>512
      or type(p.surfaces)~='table' or #p.surfaces>64
      or type(p.parts)~='table' or #p.parts>4096 or type(p.ground)~='string'
      or type(p.epoch)~='number' or p.epoch%1~=0 or p.epoch<0
      or type(p.revision)~='number' or p.revision%1~=0 or p.revision<0 then return false end
  for _,s in ipairs(p.surfaces)do
    if type(s)~='table' or type(s.key)~='string' or #s.key>80
        or type(s.h)~='string' or type(s.skin)~='string'
        or type(s.exact)~='table' then return false end
  end
  local latest=queued or raw
  if latest and (p.epoch<latest.epoch or p.epoch==latest.epoch and p.revision<=latest.revision)then return false end
  queued=p;M.stats.packets=M.stats.packets+1
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
  if queued and queued.anchor==anchor and queued.ground==ground
      and require('beamngEarth/roads').signalSurfacesReady(anchor,queued.surfaces or {}) then
    raw=queued;queued=nil;lastSignature=nil;phaseAt=0
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
  local now=os.clock()
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
    local token=tostring(raw.epoch)..':'..raw.revision
    if token~=lastAck then
      owned={};for _,name in ipairs(raw.owners)do owned[name]=true end
      M.stats.liveOwners=#raw.owners
      -- Linked stock objects are detached only after every new item exists.
      if accepted then accepted(owned)end
      if send then send({type='nl_signals_ack',anchorId=anchor,epoch=raw.epoch,revision=raw.revision})end
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
  M.stats.liveOwners=0
end
return M
