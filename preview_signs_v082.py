from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from offline_loader import load
D=load();root=Path(__file__).parent
rows=[dict(kind='725',code='725.1',text=t,color=c) for t,c in [('@723.11@E6@@Trondheim 293@@Otta 49@@Hundtorp','gul'),('@723.15@@27@Folldal 83 Enden 35 Venabygdsfjellet 14 Venabygd 7','gul'),('@723.31@Rondane 29','brun')]]
im=Image.new('RGB',(1100,1800),'#e9edf1');d=ImageDraw.Draw(im);f=ImageFont.truetype('DejaVuSans.ttf',22)
d.text((35,24),'v0.8.2 | Distance-sign layout preview (not in-game)',fill='black',font=f)
y=80
for row in rows:
 w,h=D._tile_dimensions(row);face=D._draw_face(row).resize((950,round(950*h/w)),Image.Resampling.LANCZOS)
 im.paste(face,(75,y));y+=face.height+35
im.crop((0,0,1100,y)).save(root/'preview_v082.png')
