"""Offline elevation of both faces, using the live-record fixture and layout."""
from pathlib import Path
import json,numpy as np
from PIL import Image,ImageDraw,ImageFont
from offline_loader import load
from norway_signs_v073 import mounting_v081 as M
D=load();D.catalog.arrow_side=lambda c:'V' if '.V' in c else 'H' if '.H' in c else None
D.catalog.double_sided_supported=lambda c:False
root=Path(__file__).parent
rows=json.loads((root/'fixtures_mounting_v081.json').read_text());records=[]
for row in rows:
 w,h=D._tile_dimensions(row);n=M.normal(row,[2,0],[0,0],[0,-1]);records.append((row,np.array([2.,0.]),n,1,w,h,0.))
plan=M.plan(records,D._stack_gap)[0]
im=Image.new('RGB',(1250,920),'#e9edf1');draw=ImageDraw.Draw(im)
font=ImageFont.truetype('DejaVuSans.ttf',23);small=ImageFont.truetype('DejaVuSans.ttf',18)
draw.text((35,25),'v0.8.1 | One support, two opposite faces',font=font,fill='black')
draw.text((35,64),'Offline layout preview from the four Åndal / Blakstad NVDB records',font=small,fill='#334155')
scale=220;gy=820
for j,batch in enumerate(plan['batches']):
 cx=315+j*620
 draw.text((cx-235,115),'Face '+str(j+1)+' — '+batch['records'][0][0]['facing'],font=small,fill='black')
 draw.rectangle((cx-7,gy-plan['top']*scale,cx+7,gy),fill='#929da3',outline='#606b72',width=2)
 for r in batch['records']:
  row,q,n,mat,w,h,ground=r;b=batch['bases'][row['id']]
  x=round(cx-w*scale/2);y=round(gy-(b+h)*scale)
  face=D._draw_face(row).resize((round(w*scale),round(h*scale)),Image.Resampling.LANCZOS)
  im.paste(face,(x,y))
 draw.text((cx-235,855),'2 rows; original dimensions retained where recorded',font=small,fill='#334155')
im.save(root/'preview_v081.png')
