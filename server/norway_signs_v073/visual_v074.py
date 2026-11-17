"""Modular Norwegian sign-face artwork v0.7.9.

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
    # Canonical upright arrow: tall shaft, narrower head than v077.
    # Rotate this same silhouette, preserving identical proportions L/R/up.
    points=[(.50,.02),(.18,.44),(.38,.44),(.38,.98),
            (.62,.98),(.62,.44),(.82,.44)]
    if side=='V':points=[(v,1-u) for u,v in points]
    elif side=='H':points=[(1-v,u) for u,v in points]
    elif side!='UP':return
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
    if c.endswith('.R'):return 'UP'
    return None

def _symbol_direction(symbol):
    return {'30':'V','32':'V','31':'H','33':'H','34':'UP'}.get(symbol)

def _icon(im,draw,part,rect,path,direction=None):
    x,y,w,h=rect
    if part[0]=='route':
        sh.shield(draw,rect,part[1],part[2],path);return
    if part[1]=='723.31':
        mask=Image.open(Path(__file__).parent/'tourist_route_mask.png').convert('L')
        mask=mask.resize((max(1,round(w)),max(1,round(h))),Image.Resampling.LANCZOS)
        draw.rectangle((x,y,x+w,y+h),fill=im.getpixel((0,0)) if im.getpixel((0,0))==COLORS['brun'][0] else (143,70,28))
        im.paste((255,255,255),(round(x),round(y)),mask);return
    if part[1] not in ('771','772','790.15'):return
    black=(8,8,8);white=(250,250,247)
    draw.rounded_rectangle((x,y,x+w,y+h),radius=h*.055,fill=white,outline=black,width=max(2,round(h*.025)))
    # All symbol geometry is inside a padded square, independent of board size.
    x+=w*.10;y+=h*.10;w*=.80;h*=.80
    def poly(points,fill=black):draw.polygon([(x+u*w,y+v*h) for u,v in points],fill=fill)
    def line(points,width=.045):draw.line([(x+u*w,y+v*h) for u,v in points],fill=black,width=max(2,round(h*width)))
    if part[1]=='771':
        pts=[(.06,.44),(.34,.44),(.65,.05),(.77,.05),(.60,.44),(.80,.44),(.89,.27),(.97,.27),(.90,.50),(.97,.73),(.89,.73),(.80,.56),(.60,.56),(.77,.95),(.65,.95),(.34,.56),(.06,.56),(.01,.52),(.01,.48)]
        if direction=='H':pts=[(1-u,v) for u,v in pts]
        poly(pts)
    elif part[1]=='772':
        # Main rotor/mast, sloped cockpit, tapering tail, ring tail rotor, skid.
        line([(.06,.17),(.81,.17)],.045);line([(.43,.17),(.43,.31)],.045)
        poly([(.06,.68),(.15,.42),(.22,.31),(.56,.31),(.84,.29),(.89,.35),(.57,.49),(.52,.68)])
        poly([(.16,.52),(.21,.37),(.28,.37),(.28,.52)],white)
        poly([(.32,.37),(.40,.37),(.40,.52),(.32,.52)],white)
        poly([(.44,.37),(.51,.37),(.51,.52),(.44,.52)],white)
        draw.ellipse((x+w*.80,y+h*.22,x+w*.98,y+h*.42),outline=black,width=max(2,round(h*.035)))
        line([(.23,.68),(.23,.77)],.035);line([(.46,.68),(.46,.77)],.035)
        draw.rounded_rectangle((x+w*.06,y+h*.77,x+w*.59,y+h*.87),radius=h*.045,fill=black)
    else:
        # 790.15 industrial area: sawtooth factory roof and tall chimney.
        poly([(.08,.85),(.08,.45),(.28,.58),(.28,.38),(.48,.53),(.48,.31),(.70,.48),(.74,.48),(.74,.12),(.85,.12),(.85,.85)])


def render(row,lines,canvas=None):
    from .layout_v077 import content_rows,wrap
    kind=str(row['kind']);code=str(row['code'])
    W,H=canvas or (1024,384)
    if min(W,H)<8:raise ValueError('Canvas too small')
    w,h=W*2,H*2
    tourist=code=='723.31'
    bg,fg=((143,70,28),(255,255,255)) if tourist else COLORS[row['color']]
    im=Image.new('RGB',(w,h),bg);draw=ImageDraw.Draw(im)
    path=Path(__file__).resolve().parent.parent/'data'/'fonts'/'Overpass-Bold.ttf'
    if not path.is_file():path=Path('DejaVuSansCondensed.ttf')
    rows=content_rows(lines,kind)
    if kind=='723' and not tourist:
        label=sh.route_label([p for line in lines for p in line])
        if not label:raise ValueError('Route marker has no number')
        sh.shield(draw,(0,0,w-1,h-1),code,label,path)
        return im.resize((W,H),Image.Resampling.LANCZOS)
    if not rows:raise ValueError('No legible NVDB content')
    _,symbol=_runs(lines)
    arrow=_symbol_direction(symbol) if symbol is not None else _direction_from_code(code)
    unit=min(w,h);inset=max(3,unit*.018);border=max(3,round(unit*.014))
    left,right=w*.055,w*.945;top,bottom=h*.09,h*.91
    side=None
    if kind=='713':
        side=_direction_from_code(code)
        if not side:raise ValueError('Unsupported arrow variant')
        # The whole physical plate is rectangular. A light point penetrates
        # the dark end field; there is no projecting black triangular wing.
        draw.rounded_rectangle((inset,inset,w-inset-1,h-inset-1),radius=unit*.03,fill=fg)
        inner=inset+border*1.6;depth=min(h*.47,w*.18)
        pts=[(inner,inner),(w-inner-depth,inner),(w-inner,h/2),
             (w-inner-depth,h-inner),(inner,h-inner)]
        if side=='V':pts=[(w-x,y) for x,y in pts]
        draw.polygon(pts,fill=bg)
        # Outer board edge and inner arrow field reproduce the double contour.
        draw.rounded_rectangle((inset,inset,w-inset-1,h-inset-1),radius=unit*.03,outline=bg,width=max(2,round(border*.65)))
        left,right=(inner+h*.11,w-inner-depth-h*.06) if side=='H' else (inner+depth+h*.06,w-inner-h*.11)
        arrow=None
    else:draw.rounded_rectangle((inset,inset,w-inset-1,h-inset-1),radius=unit*.03,outline=fg,width=border)
    if arrow:
        size=min(h*.68,w*.13)
        if arrow=='H':_arrow(draw,(right-size,(h-size)/2,size,size),arrow,fg);right-=size+unit*.06
        else:_arrow(draw,(left,(h-size)/2,size,size),arrow,fg);left+=size+unit*.06
    # One route common to a panel spans its destination rows; different routes
    # stay attached to their own row instead of being collected at the top.
    all_badges=[b for _,_,bs in rows for b in bs]
    shared=all_badges if all_badges and all(not r[2] for r in rows[1:]) else []
    if shared:
        badges=shared;rows=[(t,d,[]) for t,d,_ in rows]
        if kind=='725' and len(rows)>1:
            size=min(h*.20,(right-left)*.25)
            bx=left
            for part in badges:
                bw=size*(1.50 if part[0]=='route' else 1)
                _icon(im,draw,part,(bx,top,bw,size),path);bx+=bw+unit*.07
            top+=size+unit*.065
        else:
            size=min(h*(.65 if kind=='713' else .56 if kind=='711' else .43),(right-left)/max(3,len(badges)*2.3))
            if tourist:size=min(h*.70,(right-left)*.22)
            for part in badges:
                bw=size*(1.50 if part[0]=='route' else 1)
                if kind=='713' and side=='V' and part[0]=='route':
                    _icon(im,draw,part,(right-bw,(h-size)/2,bw,size),path,direction=side);right-=bw+unit*.09
                else:
                    _icon(im,draw,part,(left,(h-size)/2,bw,size),path,direction=side or arrow);left+=bw+unit*.09
    # Balance wrapping at a readable size before reducing the uniform font.
    preferred=min(h*(.56 if kind in ('711','713') else .34),(bottom-top)/max(1,len(rows))*.65)
    if kind=='725':preferred=min(h*(.50 if len(rows)==1 else .23),(bottom-top)/max(1,len(rows))*.62)
    chosen=None
    for size in range(max(6,int(preferred)),5,-1):
        f=sh.font(size,path);wrapped=[];max_width=0
        for text,dist,badges in rows:
            reserve=size*1.9*len(badges)
            dw=draw.textlength(dist,font=f)+size*.8 if dist else 0
            width=max(10,right-left-reserve-dw)
            ls=wrap(draw,text,f,width)
            for j,line in enumerate(ls):
                wrapped.append((line,dist if j==len(ls)-1 else '',badges if j==0 else [],reserve,dw))
                max_width=max(max_width,draw.textlength(line,font=f)-width)
        line_height=size*1.27
        if len(wrapped)*line_height<=bottom-top and max_width<=0:
            chosen=(f,wrapped,line_height,size);break
    if chosen is None:raise ValueError('Content cannot fit without clipping')
    f,wrapped,line_height,size=chosen
    if kind=='725':line_height=(bottom-top)/max(1,len(wrapped))
    yy=top+((bottom-top)-line_height*len(wrapped))/2
    for text,dist,badges,reserve,dw in wrapped:
        bx=left
        for part in badges:
            bh=size*1.03;bw=bh*(1.5 if part[0]=='route' else 1)
            _icon(im,draw,part,(bx,yy+(line_height-bh)/2,bw,bh),path);bx+=bw+size*.25
        b=draw.textbbox((0,0),text,font=f)
        xx=left+reserve
        if arrow=='H' and len(wrapped)==1 and not all_badges:xx=right-(b[2]-b[0])
        if kind=='727' and not all_badges:xx=left+(right-left-(b[2]-b[0]))/2
        baseline=yy+(line_height-(b[3]-b[1]))/2-b[1]
        draw.text((xx-b[0],baseline),text,font=f,fill=fg)
        if dist:draw.text((right-draw.textlength(dist,font=f),baseline),dist,font=f,fill=fg)
        yy+=line_height
    return im.resize((W,H),Image.Resampling.LANCZOS)
