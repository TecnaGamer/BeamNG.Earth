"""Shared route badges, used both embedded and standalone (v0.7.7)."""
from functools import lru_cache
import re
from PIL import ImageFont

def font(size,path=None):
    return _cached_font(max(5,int(size)),str(path) if path else None)

@lru_cache(maxsize=128)
def _cached_font(size,path):
    for p in ([str(path)] if path else [])+['DejaVuSansCondensed.ttf','DejaVuSans.ttf']:
        try:return ImageFont.truetype(p,max(5,int(size)))
        except OSError:pass
    return ImageFont.load_default()

def fit(draw,text,sz,width,path=None):
    for n in range(max(5,int(sz)),4,-1):
        f=font(n,path);b=draw.textbbox((0,0),text,font=f)
        if b[2]-b[0]<=width:return f
    return font(5,path)

def shield(draw,rect,code,text,path=None):
    x,y,w,h=map(float,rect)
    if min(w,h)<5:return
    code=str(code);green=code.startswith(('723.11','723.12','723.13','723.14'))
    bg=(5,151,47) if green else (250,250,247);fg=(255,255,255) if green else (0,0,0)
    label=re.sub(r'\s+','',str(text)).upper()
    if label.startswith('E'):label='E '+label[1:]
    draw.rectangle((x,y,x+w,y+h),fill=bg)
    m=max(2,h*.045);stroke=max(2,round(h*.035));radius=h*.07
    box=(x+m,y+m,x+w-m,y+h-m)
    if code.endswith(('.12','.14','.16')):
        # The dashed border identifies a road leading to the indicated route.
        dash=h*.12;gap=h*.075
        for yy in (y+m,y+h-m):
            xx=x+m
            while xx<x+w-m:
                draw.line((xx,yy,min(xx+dash,x+w-m),yy),fill=fg,width=stroke);xx+=dash+gap
        for xx in (x+m,x+w-m):
            yy=y+m
            while yy<y+h-m:
                draw.line((xx,yy,xx,min(yy+dash,y+h-m)),fill=fg,width=stroke);yy+=dash+gap
    else:draw.rounded_rectangle(box,radius=radius,outline=fg,width=stroke)
    f=fit(draw,label,h*.75,w*.86,path);b=draw.textbbox((0,0),label,font=f)
    draw.text((x+(w-b[2]+b[0])/2-b[0],y+(h-b[3]+b[1])/2-b[1]),label,font=f,fill=fg)

def route_label(line):
    return ''.join(p[2] if p[0]=='route' else p[1] for p in line if p[0] in ('text','route')).strip()
