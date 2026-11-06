-- Conservative local suppression of inferred Forest signs replaced by sourced signs.
local M={}
function M.hidden(group,x,y,yaw,points)
 local control=group=='stop'or group=='giveway'
 local speed=type(group)=='string'and group:match('^speed')
 if not(control or speed)then return false end
 local a=math.rad(yaw);local fx,fy=math.sin(a),-math.cos(a)
 for _,p in ipairs(points or {})do
  if(control and p.kind=='control'or speed and p.kind=='speed')and(x-p.x)^2+(y-p.y)^2<=8^2 and fx*p.fx+fy*p.fy>.866 then return true end
 end
 return false
end
return M
