"""Modular Norwegian sign-face artwork v0.7.5.

Works only with validated, already-decoded NVDB runs. It never changes the
road direction, sign position, NVDB queries, sorting or caching.
"""
from pathlib import Path
from PIL import Image, ImageDraw
from . import shields_v074 as sh

COLORS={
 'gul':((248,207,31),(0,0,0)),
 'hvit':((250,250,248),(0,0,0)),
 'blå':((6,72,155),(255,255,255)),
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
    if min(w,h)<8:return
    cx=x+w*.50;cy=y+h*.50
    thickness=max(4,int(h*.075))
    if side=='H':
        draw.line(((x+w*.12,cy),(x+w*.83,cy)),fill=color,width=thickness)
        draw.polygon([(x+w*.83,cy),(x+w*.57,cy-h*.29),(x+w*.57,cy+h*.29)],fill=color)
    elif side=='V':
        draw.line(((x+w*.84,cy),(x+w*.17,cy)),fill=color,width=thickness)
        draw.polygon([(x+w*.17,cy),(x+w*.43,cy-h*.29),(x+w*.43,cy+h*.29)],fill=color)
    elif side=='UP':
        draw.line(((cx,y+h*.84),(cx,y+h*.18)),fill=color,width=thickness)
        draw.polygon([(cx,y+h*.13),(cx-w*.22,y+h*.42),(cx+w*.22,y+h*.42)],fill=color)

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

def render(row,lines,canvas=None):
    kind=row['kind'];code=row['code']
    shapes={'723':(512,256),'713':(1024,256),'711':(1024,384),
            '725':(1024,384),'727':(768,256)}
    W,H=canvas or shapes.get(kind,(1024,384))
    bg,fg=COLORS[row['color']]
    im=Image.new('RGB',(W,H),bg);draw=ImageDraw.Draw(im)
    path=Path(__file__).resolve().parent.parent/'data'/'fonts'/'Overpass-Bold.ttf'
    if kind=='723':
        label=sh.route_label([p for line in lines for p in line])
        if not label:raise ValueError('Route marker has no number')
        sh.shield(draw,(0,0,W-1,H-1),code,label,path)
        return im
    parsed,symbol=_runs(lines)
    if not parsed:raise ValueError('No legible NVDB sign text')
    # Arrow wings follow the same *five-sided mesh UV* as the game. Do not
    # add another arrow glyph on a physically arrow-shaped 713 wing.
    inset=max(8,int(H*.035));thin=max(5,int(H*.027))
    if kind=='713':
        side=_direction_from_code(code)
        if not side:raise ValueError('Unsupported arrow variant')
        if side=='H':
            outer=[(inset,inset),(.69*W,inset),(W-inset,H/2),(.69*W,H-inset),(inset,H-inset)]
            inner=[(inset+thin,inset+thin),(.69*W-thin,inset+thin),
                   (W-inset-thin*2,H/2),(.69*W-thin,H-inset-thin),(inset+thin,H-inset-thin)]
            left,right=int(W*.052),int(W*.685)
        else:
            outer=[(W-inset,inset),(.31*W,inset),(inset,H/2),(.31*W,H-inset),(W-inset,H-inset)]
            inner=[(W-inset-thin,inset+thin),(.31*W+thin,inset+thin),
                   (inset+thin*2,H/2),(.31*W+thin,H-inset-thin),(W-inset-thin,H-inset-thin)]
            left,right=int(W*.316),int(W*.945)
        draw.polygon(outer,fill=fg)
        draw.polygon(inner,fill=bg)
        # Norwegian 713 fingerboards have a solid black arrow-tip insert,
        # *not* an extra painted arrow over the destination text.
        # Keep the tip contained within the existing five-sided UV silhouette.
        if side=='H':
            tip=[(W-inset-thin*2,H/2),(.82*W,H*.27),(.82*W,H*.73)]
        else:
            tip=[(inset+thin*2,H/2),(.18*W,H*.27),(.18*W,H*.73)]
        draw.polygon(tip,fill=fg)
        # Use the recorded order and height, not a guessed oversized billboard.
        _content(draw,[p for part in parsed for p in part],left,int(H*.11),right-left,int(H*.78),fg,path)
        return im
    # 711 / 725 / 727 are rectangular plates with a narrow dark outline.
    edge=max(4,int(H*.014))
    draw.rectangle((edge,edge,W-edge-1,H-edge-1),outline=fg,width=edge)
    x=int(W*.048); usable=int(W*.904)
    if kind=='727':
        text=' '.join(p[1] for line in parsed for p in line if p[0]=='text')
        if not text:raise ValueError('Missing place name')
        _center_text(draw,text,x,int(H*.13),usable,int(H*.73),fg,path,int(H*.44))
        return im
    # A symbol code gives the board a left/right/straight arrow. If missing,
    # no fabricated arrow is added to the artwork.
    arrow=_symbol_direction(symbol) if symbol is not None else _direction_from_code(code)
    rows=parsed[:3]
    margin=int(H*.075);avail=H-2*margin
    step=avail/len(rows)
    for i,part in enumerate(rows):
        _content(draw,part,x,int(margin+i*step),usable,int(step),fg,path,arrow=arrow if i==0 else None)
    return im
