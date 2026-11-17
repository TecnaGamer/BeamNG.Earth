"""Modular Norwegian sign-face artwork v0.7.6.

Works only with validated, already-decoded NVDB runs. It never changes the
road direction, sign position, NVDB queries, sorting or caching.
"""
from pathlib import Path
from PIL import Image, ImageDraw
from . import shields_v074 as sh

COLORS={
 'gul':((242,211,53),(0,0,0)),
 'hvit':((250,250,248),(0,0,0)),
 'blå':((18,51,131),(255,255,255)),
 'grønn':((0,115,65),(255,255,255)),
 'brun':((121,77,43),(255,255,255)),
}

def _flabel(draw,txt,size,max_w,path):
    return sh.fit(draw,txt,size,max_w,path)

def _center_text(draw,txt,x,y,w,h,fill,path,preferred):
    f=_flabel(draw,txt,preferred,w,path)
    b=draw.textbbox((0,0),txt,font=f)
    draw.text((x+(w-(b[2]-b[0]))/2-b[0],y+(h-(b[3]-b[1]))/2-b[1]),txt,font=f,fill=fill)

def _arrow(draw,rect,side,color):
    x,y,w,h=rect
    # Filled silhouette keeps the shaft and head proportions consistent.
    points=[(.39,.92),(.61,.92),(.61,.43),(.91,.43),(.50,.05),(.09,.43),(.39,.43)]
    if side=='V': points=[(v,1-u) for u,v in points]
    elif side=='H': points=[(1-v,u) for u,v in points]
    elif side!='UP': return
    draw.polygon([(x+u*w,y+v*h) for u,v in points],fill=color)


def _runs(lines):
    # Group the recorded text/route on a physical panel; separate arrow codes
    # are layout instructions, not their own extra signboard.
    result=[]; arrow=None
    for ln in lines:
        parts=[]
        for p in ln:
            if p[0]=='arrow':
                arrow=p[1]
            elif p[0]=='route':parts.append(('route',p[1],p[2]))
            elif p[0]=='text' and p[1].strip():parts.append(('text',p[1].strip()))
        if parts:result.append(parts)
    return result,arrow

def _content(draw,parts,x,y,width,height,fg,path,*,arrow=None):
    # Route signs are compact mini-panels with fixed height and ample clearance.
    # Draw row contents from left to right, arrows to the far edge of the board.
    shield_h=int(height*.64)
    pad=max(8,int(height*.10))
    available=width-(int(height*.85) if arrow else 0)
    routeparts=[p for p in parts if p[0]=='route']
    textparts=[p for p in parts if p[0]=='text']
    start=x
    for p in routeparts[:2]:
        label=p[2]
        sw=max(int(shield_h*1.32),int(len(label)*shield_h*.29))
        sw=min(sw,int(available*.40))
        sh.shield(draw,(start,y+(height-shield_h)//2,sw,shield_h),p[1],label,path)
        start+=sw+pad
    label=' '.join(p[1] for p in textparts)
    if label:
        textwidth=max(20,int(x+available-start-pad))
        f=_flabel(draw,label,int(height*.47),textwidth,path)
        b=draw.textbbox((0,0),label,font=f)
        draw.text((start-b[0],y+(height-(b[3]-b[1]))/2-b[1]),label,font=f,fill=fg)
    if arrow:
        _arrow(draw,(x+width-int(height*.84),y+height*.11,height*.72,height*.78),arrow,fg)

def _direction_from_code(code):
    c=str(code).upper()
    if '.V' in c:return 'V'
    if '.H' in c:return 'H'
    return None

def _symbol_direction(symbol):
    return {'30':'V','32':'V','31':'H','33':'H','34':'UP'}.get(symbol)

def _badge(draw,part,x,y,h,path):
    label=str(part[2]).replace(' ','')
    w=int(h*(1.35 if len(label)<=2 else 1.65 if len(label)<=3 else 2.05))
    green=str(part[1]).startswith(('723.11','723.12','723.13','723.14'))
    bg=(0,153,112) if green else (250,250,245)
    fg=(255,255,255) if green else (0,0,0)
    draw.rectangle((x,y,x+w,y+h),fill=bg)
    inset=max(2,round(h*.06));stroke=max(1,round(h*.035))
    draw.rectangle((x+inset,y+inset,x+w-inset,y+h-inset),outline=fg,width=stroke)
    if label.startswith('E'): label='E '+label[1:]
    _center_text(draw,label,x+inset,y+inset,w-2*inset,h-2*inset,fg,path,int(h*.72))
    return w


def render(row,lines,canvas=None):
    kind=str(row['kind']);code=row['code']
    sizes={'723':(512,256),'713':(1024,256),'711':(1024,384),'725':(1024,384),'727':(768,256)}
    W,H=canvas or sizes.get(kind,(1024,384))
    # Render larger and downsample for clean borders, arrows and lettering.
    scale=2;w,h=W*scale,H*scale
    bg,fg=COLORS[row['color']]
    im=Image.new('RGB',(w,h),bg);draw=ImageDraw.Draw(im)
    path=Path(__file__).resolve().parent.parent/'data'/'fonts'/'Overpass-Bold.ttf'
    if not path.is_file(): path=Path('DejaVuSansCondensed-Bold.ttf')
    if kind=='723':
        label=sh.route_label([p for line in lines for p in line])
        if not label: raise ValueError('Route marker has no number')
        sh.shield(draw,(0,0,w-1,h-1),code,label,path)
        return im.resize((W,H),Image.Resampling.LANCZOS)
    parsed,symbol=_runs(lines)
    if not parsed: raise ValueError('No legible NVDB sign text')
    inset=max(3,int(h*.018));border=max(3,int(h*.018))
    arrow=_symbol_direction(symbol) if symbol is not None else _direction_from_code(code)
    left,right=w*.075,w*.925
    if kind=='713':
        side=_direction_from_code(code)
        if not side: raise ValueError('Unsupported arrow variant')
        # Preserve the current mesh's five-sided silhouette.
        pts=[(inset,inset),(.69*w,inset),(w-inset,h/2),(.69*w,h-inset),(inset,h-inset)]
        if side=='V': pts=[(w-x,y) for x,y in pts]
        draw.polygon(pts,fill=bg)
        draw.line(pts+[pts[0]],fill=fg,width=border,joint='curve')
        wedge=[(.69*w,inset),(w-inset,h/2),(.69*w,h-inset)]
        if side=='V': wedge=[(w-x,y) for x,y in wedge]
        draw.polygon(wedge,fill=fg)
        left,right=(w*.05,w*.66) if side=='H' else (w*.34,w*.95)
        arrow=None
    else:
        draw.rounded_rectangle((inset,inset,w-inset-1,h-inset-1),radius=h*.035,outline=fg,width=border)
    if kind=='727':
        label=' '.join(p[1] for line in parsed for p in line if p[0]=='text')
        if not label: raise ValueError('Missing place name')
        _center_text(draw,label,left,h*.14,right-left,h*.72,fg,path,int(h*.48))
        return im.resize((W,H),Image.Resampling.LANCZOS)
    if arrow:
        aw=min(w*.15,h*.52)
        if arrow=='H':
            _arrow(draw,(right-aw,h*.29,aw,h*.42),arrow,fg);right-=aw+w*.035
        else:
            _arrow(draw,(left,h*.25,aw,h*.50),arrow,fg);left+=aw+w*.035
    # Keep each recorded destination line; route-only lines become badges above.
    routes=[p for line in parsed for p in line if p[0]=='route']
    labels=[' '.join(p[1] for p in line if p[0]=='text') for line in parsed]
    labels=[t for t in labels if t]
    if not labels and not routes: raise ValueError('Empty content')
    top=h*.10;bottom=h*.90
    if routes:
        bh=min(h*.28,(right-left)/(max(1,len(routes))*2.6))
        bx=left
        for part in routes:
            bx+=_badge(draw,part,bx,top,bh,path)+bh*.22
        top+=bh+h*.045
    if labels:
        step=(bottom-top)/len(labels)
        preferred=int(min(h*.43,step*.74))
        fonts=[_flabel(draw,t,preferred,right-left,path) for t in labels]
        size=min(getattr(f,'size',preferred) for f in fonts)
        for i,t in enumerate(labels):
            f=sh.font(size,path);b=draw.textbbox((0,0),t,font=f)
            yy=top+i*step+(step-(b[3]-b[1]))/2-b[1]
            # Right-turn stacked destinations are centered like the reference.
            xx=left+((right-left-(b[2]-b[0]))/2 if arrow=='H' else 0)-b[0]
            draw.text((xx,yy),t,font=f,fill=fg)
    return im.resize((W,H),Image.Resampling.LANCZOS)
