-- Classify legacy traffic-sign owners, not arbitrary objects named "sign".
local M={}
function M.trafficGroup(g)
  return type(g)=='string' and (g=='stop' or g=='giveway' or g:match('^giveway_')~=nil or g=='yield' or g:match('^speed%d')~=nil or g:match('^speedk')~=nil)
end
return M
