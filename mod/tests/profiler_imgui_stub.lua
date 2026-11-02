-- Strict drawing surface for profiler regression tests; unknown APIs raise errors.
return function(T)
  local S={width=680,height=640,texts={},sizes={},clicked=nil,tab='All',begin=0,finish=0,children=0,tables=0,styles=0,cursor={x=0,y=0}}
  local im={TreeNodeFlags_DefaultOpen=1,Cond_FirstUseEver=1,TabItemFlags_SetSelected=2,
    TableFlags_Resizable=1,TableFlags_RowBg=2,TableFlags_BordersInnerV=4,MouseCursor_ResizeEW=1,MouseCursor_ResizeNS=2}
  for i,key in ipairs({'WindowBg','ChildBg','FrameBg','Button','ButtonHovered','ButtonActive','Tab','TabActive','TabHovered','Text'}) do im['Col_'..key]=i end
  function im.ImVec2(x,y) assert(x==x and y==y and math.abs(x)<math.huge and math.abs(y)<math.huge);return {x=x,y=y} end
  function im.ImVec4(...) return {...} end
  function im.BoolPtr(v) return {[0]=v} end
  function im.GetFontSize() return 16 end
  function im.CalcTextSize(s) return {x=#s*8,y=16} end
  function im.GetContentRegionAvail() return {x=S.childWidth or S.width,y=S.height} end
  function im.GetWindowSize() return {x=S.width+30,y=S.height+40} end
  function im.GetCursorScreenPos() return {x=S.cursor.x,y=S.cursor.y} end
  function im.SetCursorScreenPos(p) S.cursor=p end
  function im.GetWindowDrawList() return {} end
  function im.GetColorU322() return 1 end
  function im.ImDrawList_AddRectFilled(_,a,b) assert(b.x>=a.x and b.y>=a.y) end
  function im.ImDrawList_AddLine() end
  function im.ImDrawList_AddText1(_,_,_,s) S.texts[#S.texts+1]=s end
  function im.Dummy() end
  function im.InvisibleButton(id,size) assert(size.x>0 and size.y>0);S.sizes[id]=size;S.lastItem=id end
  function im.IsItemHovered() return S.hover==S.lastItem end
  function im.IsItemClicked() return S.pick and S.hover==S.lastItem end
  function im.IsMouseClicked() return S.pick==true end
  function im.IsItemActive() return false end
  function im.GetMousePos() return {x=S.mouseX or 200,y=0} end
  function im.GetMouseDragDelta() return {x=0,y=0} end
  function im.ResetMouseDragDelta() end
  function im.SetMouseCursor() end
  function im.IsMouseDown() return false end
  function im.TextUnformatted(s) S.texts[#S.texts+1]=s end
  function im.PushTextWrapPos() end
  function im.PopTextWrapPos() end
  function im.SameLine() end
  function im.SetNextWindowSize(_,cond) T.check(cond==im.Cond_FirstUseEver,'user window size is not overwritten') end
  function im.SetNextWindowSizeConstraints(size) T.check(size.x==560 and size.y==480,'readable minimum window size') end
  function im.Begin(_,open) S.begin=S.begin+1;if S.close and open then open[0]=false;S.close=false end;return not S.collapsed end
  function im.End() S.finish=S.finish+1 end
  function im.BeginChild1(_,size) S.children=S.children+1;S.childWidth=size.x>0 and size.x or S.width;return true end
  function im.EndChild() S.children=S.children-1;S.childWidth=nil end
  function im.BeginTabBar() return true end
  function im.BeginTabItem(label,_,flags) if flags==im.TabItemFlags_SetSelected then S.tab=label end;return S.tab==label end
  function im.EndTabItem() end
  function im.EndTabBar() end
  function im.BeginTable() S.tables=S.tables+1;return true end
  function im.EndTable() S.tables=S.tables-1 end
  function im.TableSetupColumn() end
  function im.TableHeadersRow() end
  function im.TableNextRow() end
  function im.TableSetColumnIndex() end
  function im.PushStyleColor2() S.styles=S.styles+1 end
  function im.PopStyleColor(n) S.styles=S.styles-n end
  function im.CollapsingHeader1(s) S.texts[#S.texts+1]=s;return true end
  function im.Button(label) if label==S.clicked then S.clicked=nil;return true end;return false end
  function im.PlotLines1(id,values,n,_,_,_,_,size) S.sizes[id]=size;assert(n>0 and values[0]~=nil) end
  function im.BeginTooltip() end
  function im.EndTooltip() end
  function im.ProgressBar(v) assert(v>=0 and v<=1) end
  return im,S
end
