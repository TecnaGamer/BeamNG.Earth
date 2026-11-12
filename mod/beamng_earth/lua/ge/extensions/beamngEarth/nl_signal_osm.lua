-- Explicit OSM car signals enrich the client graph; source files/caches stay unchanged.
local M={MAX_ROAD_MATCH_M=18,MAX_JUNCTION_PATH_M=45}
local context=require('beamngEarth/nl_signal_context')
local country=require('beamngEarth/nl_country')
local function copy(t)local r={};for k,v in pairs(t)do r[k]=v end;return r end
local function distance(a,b)return math.sqrt((a[1]-b[1])^2+(a[2]-b[2])^2)end
local function nearest(g,x,y)
  local best
  for i,e in ipairs(g.edges)do
    local a,b=g.nodes[e[1]],g.nodes[e[2]]
    if a and b and (e[4] or 0)>0 and (e[7] or 0)>.1 then
      local dx,dy=b[1]-a[1],b[2]-a[2];local len=dx*dx+dy*dy
      if len>1 then
        local t=math.max(0,math.min(1,((x-a[1])*dx+(y-a[2])*dy)/len))
        local px,py=a[1]+t*dx,a[2]+t*dy;local d=(x-px)^2+(y-py)^2
        if d<=M.MAX_ROAD_MATCH_M^2 and (not best or d<best.d)then best={index=i,edge=e,t=t,x=px,y=py,d=d,len=math.sqrt(len)} end
      end
    end
  end
  return best
end
local function adjacency(g)
  local a={}
  for _,e in ipairs(g.edges)do
    a[e[1]]=a[e[1]] or {};a[e[2]]=a[e[2]] or {}
    a[e[1]][#a[e[1]]+1]=e;a[e[2]][#a[e[2]]+1]=e
  end
  return a
end
local function junction(g,adj,match,existing)
  local choices={}
  for _,start in ipairs({{match.edge[1],match.t*match.len},{match.edge[2],(1-match.t)*match.len}})do
    local name,walk,previous=start[1],start[2]
    for _=1,12 do
      if walk>M.MAX_JUNCTION_PATH_M or not adj[name]then break end
      if #adj[name]>=3 or existing[name]then choices[#choices+1]={name=name,d=walk};break end
      local nextName
      for _,e in ipairs(adj[name])do
        local n=e[1]==name and e[2] or e[1]
        if n~=previous and not (previous==nil and (n==match.edge[1] or n==match.edge[2]))then nextName=n;break end
      end
      if not nextName then break end
      previous,name=name,nextName;walk=walk+distance(g.nodes[previous],g.nodes[name])
    end
  end
  table.sort(choices,function(a,b)if a.d~=b.d then return a.d<b.d end;return a.name<b.name end)
  return choices[1] and choices[1].name
end
local function approaches(g,adj,name,roadClass)
  local j=g.nodes[name];local arms={};local wide=0
  for _,e in ipairs(adj[name] or {})do
    local nb,n
    if e[2]==name then nb,n=e[1],e[4]
    elseif e[1]==name and e[3]~=1 then nb,n=e[2],e[5] end
    if nb and n and n>0 then
      local node=g.nodes[nb];local len=distance(j,node)
      if len>6 and not(len<25 and #(adj[nb] or {})>=3)then
        local radius=e[2]==name and (e[9] or e[8]) or e[8]
        arms[#arms+1]={name=nb,dx=(j[1]-node[1])/len,dy=(j[2]-node[2])/len,len=len,radius=radius,
          road_class=type(e[10])=='table' and e[10].road_class or nil}
        wide=math.max(wide,radius or 0)
      end
    end
  end
  table.sort(arms,function(a,b)return a.name<b.name end)
  local out={};local axis=arms[1]
  for _,arm in ipairs(arms)do
    local back=math.min(wide+2,arm.len*.6);local rx,ry=arm.dy,-arm.dx
    local cx,cy=j[1]-arm.dx*back,j[2]-arm.dy*back
    local phase=math.abs(arm.dx*axis.dx+arm.dy*axis.dy)>.7 and 0 or 1
    out[#out+1]={arm.name,cx+rx*arm.radius/2,cy+ry*arm.radius/2,j[3],arm.dx,arm.dy,phase,
      cx-arm.dx*.5+rx*(arm.radius+1),cy-arm.dy*.5+ry*(arm.radius+1),j[3]+.15,
      math.deg(math.atan2(ry,rx)),{road_class=arm.road_class,osm_source_road_class=roadClass,signal_source='OSM_car_signal_node'}}
  end
  return out
end

function M.augment(source,frame,points)
  if not(frame and frame.worldToGeodetic and frame.geodeticToWorld)then return source,{reason='frame_unavailable'} end
  local minx,miny,maxx,maxy=math.huge,math.huge,-math.huge,-math.huge
  for _,n in pairs(source.nodes)do minx,miny,maxx,maxy=math.min(minx,n[1]),math.min(miny,n[2]),math.max(maxx,n[1]),math.max(maxy,n[2]) end
  if points==nil then
    if minx==math.huge then return source,{reason='empty_graph'} end
    local lat,lon=frame:worldToGeodetic((minx+maxx)/2,(miny+maxy)/2,0)
    points=context.signalPoints(lat,lon,math.min(2300,math.max(maxx-minx,maxy-miny)/2+100))
  end
  local g=copy(source);g.nodes=copy(source.nodes);g.edges={};g.signals={}
  for i,e in ipairs(source.edges)do g.edges[i]=copy(e) end
  local existing={}
  for i,j in ipairs(source.signals or {})do
    local c=copy(j);c.app={};for k,a in ipairs(j.app or {})do c.app[k]=a end
    g.signals[i]=c;existing[j.j]=c
  end
  local adj=adjacency(g)
  local report={source_points=#points,added_junctions=0,upgraded_stop_junctions=0,added_approaches=0,completed_approaches=0,unmatched=0,outside_graph_window=0,unmatched_near_graph=0}
  local function unmatched(x,y)
    report.unmatched=report.unmatched+1
    local key=x<minx-18 or x>maxx+18 or y<miny-18 or y>maxy+18
    key=key and 'outside_graph_window' or 'unmatched_near_graph';report[key]=report[key]+1
  end
  for _,point in ipairs(points)do
    local x,y=frame:geodeticToWorld(point.lat,point.lon,0)
    local match=nearest(g,x,y)
    if match and country.contains(point.lat,point.lon)then
      local name=junction(g,adj,match,existing)
      if not name then
        if match.t<.08 then name=match.edge[1]
        elseif match.t>.92 then name=match.edge[2]
        else
          name='osm_signal_'..point.id
          local a,b=g.nodes[match.edge[1]],g.nodes[match.edge[2]]
          local radius=(match.edge[8] or 3)*(1-match.t)+(match.edge[9] or match.edge[8] or 3)*match.t
          g.nodes[name]={match.x,match.y,a[3]+(b[3]-a[3])*match.t,radius}
          local first,second=copy(match.edge),copy(match.edge)
          first[2],second[1]=name,name;first[9],second[8]=radius,radius
          g.edges[match.index]=first;g.edges[#g.edges+1]=second;adj=adjacency(g)
        end
      end
      if name and #(adj[name] or {})>=2 then
        local found=existing[name];local fresh=approaches(g,adj,name,point.road_class)
        if not found and #fresh>0 then
          found={j=name,kind='signal',app={},source='OSM_car_signal_node',osm_node_id=point.id}
          existing[name]=found;g.signals[#g.signals+1]=found;report.added_junctions=report.added_junctions+1
        end
        if found then
          found.osm_confirmed=true;found.osm_node_ids=found.osm_node_ids or {};found.osm_node_ids[point.id]=true
          if found.kind~='signal' then found.kind='signal';found.app={};report.upgraded_stop_junctions=report.upgraded_stop_junctions+1 end
          local seen={};for i,a in ipairs(found.app)do seen[a[1]]=i end
          for _,a in ipairs(fresh)do
            local index=seen[a[1]]
            if not index then
              found.app[#found.app+1]=a;seen[a[1]]=#found.app;report.added_approaches=report.added_approaches+1
            else
              local old=found.app[index]
              -- Some native approaches contain only their seven controller fields.
              -- Being present in the controller does not mean a visible prop exists.
              if type(old[8])~='number'or type(old[9])~='number'or type(old[10])~='number'then
                local fixed=copy(old)
                for k=2,7 do if type(fixed[k])~='number'then fixed[k]=a[k]end end
                fixed[8],fixed[9],fixed[10]=a[8]+fixed[2]-a[2],a[9]+fixed[3]-a[3],a[10]+fixed[4]-a[4]
                fixed[11]=a[11];fixed[12]=old[12]or a[12]
                found.app[index]=fixed;report.completed_approaches=report.completed_approaches+1
              end
            end
          end
        end
      else unmatched(x,y) end
    else unmatched(x,y) end
  end
  local verified={};report.removed_unconfirmed_junctions=0
  for _,j in ipairs(g.signals)do
    local node=g.nodes[j.j];local dutch=false
    if node then local lat,lon=frame:worldToGeodetic(node[1],node[2],node[3]);dutch=country.contains(lat,lon)end
    if j.kind~='signal' or not dutch or j.osm_confirmed then verified[#verified+1]=j
    else report.removed_unconfirmed_junctions=report.removed_unconfirmed_junctions+1 end
  end
  g.signals=verified
  return g,report
end
return M
