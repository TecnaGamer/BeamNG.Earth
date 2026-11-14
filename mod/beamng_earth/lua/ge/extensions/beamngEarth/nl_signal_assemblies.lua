-- Consumer for server-planned parts. No road reads, lane inference or AI edits.
-- Not wired to the live protocol until native mounted assemblies are available.
local M = {}
local ROOT = '/levels/beamng_earth/art/shapes/beamngEarth/groenpol/'
local function finite(v) return type(v)=='number' and v==v and math.abs(v)<1e8 end
local function array(t,limit)
  if type(t)~='table' or #t>limit then return false end
  local n=0
  for k in pairs(t) do
    if type(k)~='number' or k%1~=0 or k<1 or k>#t then return false end
    n=n+1
  end
  return n==#t
end
local function copyPart(p)
  if type(p)~='table' or type(p.key)~='string' or #p.key==0 or #p.key>128
      or type(p.shape)~='string' or p.shape:sub(1,#ROOT)~=ROOT
      or p.shape:find('..',1,true) or not p.shape:match('^[%w_/%.-]+%.dae$')
      or not array(p.pose,12) or #p.pose~=12 then return nil end
  local pose={}
  for i=1,12 do if not finite(p.pose[i]) then return nil end;pose[i]=p.pose[i] end
  -- Position followed by three basis columns. Scale is carried in those columns.
  local axes={}
  for c=0,2 do
    local b=4+c*3;local x,y,z=pose[b],pose[b+1],pose[b+2]
    local length=math.sqrt(x*x+y*y+z*z)
    if length<0.01 or length>40 then return nil end
    axes[c+1]={x/length,y/length,z/length}
  end
  for a=1,3 do for b=a+1,3 do
    local x,y=axes[a],axes[b]
    if math.abs(x[1]*y[1]+x[2]*y[2]+x[3]*y[3])>1e-4 then return nil end
  end end
  local a,b,c=axes[1],axes[2],axes[3]
  if a[1]*(b[2]*c[3]-b[3]*c[2])+a[2]*(b[3]*c[1]-b[1]*c[3])
      +a[3]*(b[1]*c[2]-b[2]*c[1])<0.99 then return nil end
  return {key=p.key,shape=p.shape,pose=pose}
end
local function same(a,b)
  if a.shape~=b.shape then return false end
  for i=1,12 do if a.pose[i]~=b.pose[i] then return false end end
  return true
end

-- Display selection only: all geometry and mounting matrices come from Rust.
-- The caller supplies its existing controller's per-lane lamp states. Never
-- guess a phase from an arrow, a timestamp, or another approach's controller.
function M.farParts(assembly,channels)
  if type(assembly)~='table' or assembly.format~=1 or not array(assembly.parts,4096)
      or type(channels)~='table' then return nil,'invalid_assembly' end
  local rows={}
  for _,p in ipairs(assembly.parts) do
    if p.role=='head' then
      local phase=type(p.lane)=='number' and channels[p.lane+1]
      if phase~='red' and phase~='amber' and phase~='green' and phase~='off' then return nil,'phase_unavailable' end
      if type(p.forest)~='table' or type(p.key)~='string' then return nil,'forest_models_unavailable' end
      local body=copyPart({key=p.key..':body',shape=p.forest.body,pose=p.pose})
      if not body then return nil,'invalid_part' end
      rows[#rows+1]=body
      if phase~='off' then
        local lens=copyPart({key=p.key..':light',shape=p.forest[phase],pose=p.pose})
        if not lens then return nil,'invalid_part' end
        rows[#rows+1]=lens
      end
    else
      local row=copyPart(p)
      if not row then return nil,'invalid_part' end
      rows[#rows+1]=row
    end
  end
  return rows
end

function M.new(backend)
  assert(backend and backend.create and backend.remove)
  local self={enabled=false,revision=-1,live={},pending=nil,errors=0}
  local function discard(p)
    if p then for _,entry in pairs(p.created) do backend.remove(entry.handle) end end
  end
  function self:setEnabled(on)
    assert(type(on)=='boolean')
    if self.enabled==on then return end
    self.enabled=on
    if not on then
      discard(self.pending);self.pending=nil
      for _,entry in pairs(self.live) do backend.remove(entry.handle) end
      self.live={};self.revision=-1
    end
  end
  function self:enqueue(packet)
    if not self.enabled then return false,'disabled' end
    if type(packet)~='table' or packet.format~=1 or not finite(packet.revision)
        or packet.revision%1~=0 or packet.revision<0 or not array(packet.parts,4096) then return false,'invalid_packet' end
    if packet.revision<=self.revision or self.pending and packet.revision<=self.pending.revision then
      return false,'stale_revision'
    end
    local rows,keys={},{}
    for _,p in ipairs(packet.parts) do
      local row=copyPart(p)
      if not row or keys[row.key] then return false,'invalid_part' end
      keys[row.key]=true;rows[#rows+1]=row
    end
    -- Fully validate before touching an accepted snapshot or an in-progress one.
    discard(self.pending)
    self.pending={revision=packet.revision,rows=rows,created={},keep={},at=1}
    return true
  end
  function self:setFrame(frame)
    if not self.enabled or not backend.setFrame then return false end
    return backend.setFrame(frame)
  end
  function self:tick(budget)
    if not self.enabled or not self.pending then return false end
    budget=math.max(0,math.min(64,math.floor(tonumber(budget) or 16)))
    local p=self.pending
    while p.at<=#p.rows and budget>0 do
      local row=p.rows[p.at];local old=self.live[row.key]
      if old and same(old.part,row) then p.keep[row.key]=old
      else
        local ok,handle=pcall(backend.create,row)
        if not ok or not handle then
          self.errors=self.errors+1;discard(p);self.pending=nil
          return false,'create_failed'
        end
        p.created[row.key]={part=row,handle=handle}
      end
      p.at=p.at+1;budget=budget-1
    end
    if p.at<=#p.rows then return false,'pending' end
    -- Stock suppression may follow only this successful complete acceptance.
    for key,entry in pairs(self.live) do
      if not p.keep[key] then backend.remove(entry.handle) end
    end
    for key,entry in pairs(p.created) do p.keep[key]=entry end
    self.live=p.keep;self.revision=p.revision;self.pending=nil
    return true,'accepted'
  end
  function self:forgetLevel()
    -- The engine tears the scene down. Never call into stale Forest userdata.
    self.live={};self.pending=nil;self.revision=-1
    if backend.forgetLevel then backend.forgetLevel() end
  end
  return self
end
return M
