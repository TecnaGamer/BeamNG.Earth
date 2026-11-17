"""Norwegian route-number shields — isolated from NVDB, placement and sign faces."""
import re
from PIL import ImageFont


def font(size, path=None):
    opts=[str(path)] if path else []
    opts+=['DejaVuSans.ttf']
    for f in opts:
        try:return ImageFont.truetype(f,max(9,int(size)))
        except OSError:pass
    return ImageFont.load_default()


def fit(draw,text,sz,width,path=None):
    for n in range(max(10,int(sz)),9,-1):
        f=font(n,path)
        b=draw.textbbox((0,0),text,font=f)
        if b[2]-b[0]<=width:return f
    return font(10,path)


def shield(draw,rect,code,text,path=None):
    """Draw 723.11/13 green or 723.15 white with border inside its own rectangle."""
    x,y,w,h=map(int,rect)
    if min(w,h)<=8:return
    green=str(code).startswith(('723.11','723.12','723.13','723.14'))
    bg=(0,115,65) if green else (249,249,246)
    fg=(255,255,255) if green else (8,8,8)
    label=re.sub(r'\s+','',str(text or '')).upper()
    if not re.fullmatch(r'(?:E)?\d{1,4}',label):
        # No arbitrary markup, just the route's actual characters.
        label=label[:10]
    draw.rectangle((x,y,x+w,y+h),fill=bg)
    line=max(2,int(h*.028))
    margin=max(10,int(h*.085))
    draw.rectangle((x+margin,y+margin,x+w-margin,y+h-margin),outline=fg,width=line)
    f=fit(draw,label,int(h*.60),int(w*.88),path)
    b=draw.textbbox((0,0),label,font=f)
    draw.text((x+(w-(b[2]-b[0]))/2-b[0],y+(h-(b[3]-b[1]))/2-b[1]),label,fill=fg,font=f)


def route_label(line):
    return ''.join(p[2] if p[0]=='route' else p[1] for p in line if p[0] in ('text','route')).strip()
