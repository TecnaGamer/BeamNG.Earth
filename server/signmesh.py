"""Batched dynamic signs. One shared alphabet/material set, no per-name assets.

Metres, local front towards -Y. Town panels grow with the measured glyph advance;
long names are reduced to fit a bounded board. Accents and ligatures are kept.
"""
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata
import numpy as np

FONT_PATH=Path(__file__).with_name('data')/'sign_font.json'
MATERIALS=['bne_sign_'+s for s in ('white','red','black','metal','yellow','glyph','blue','green','glyph_white')]
WHITE,RED,BLACK,METAL,YELLOW,GLYPH,BLUE,GREEN,GLYPH_WHITE=range(9)
COLOUR={key:i for i,key in enumerate(('white','red','black','metal','yellow','glyph','blue','green','glyph_white'))}

@lru_cache(maxsize=1)
def font():return json.loads(FONT_PATH.read_text(encoding='utf-8'))

@lru_cache(maxsize=1)
def font_signature():
    """Repacking the atlas changes UVs even when the layout code is unchanged."""
    return hashlib.sha256(FONT_PATH.read_bytes()).hexdigest()


def clean(text,uppercase=True):
    text=unicodedata.normalize('NFC',str(text or ''))
    if uppercase:text=text.upper()
    text=' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())
    return text[:160]


def face_font(key=None):
    data=font()
    return data.get('fonts',{}).get(key,data)


def glyph_for(ch,key=None):
    face=face_font(key)
    ch=face.get('aliases',{}).get(ch,ch)
    g=face['glyphs'].get(ch)
    if g is None:g=face_font('fallback')['glyphs'].get(ch)
    return g if g is not None else face['glyphs'].get('?',face_font('fallback')['glyphs']['?'])


def font_text(text,key=None):
    f=face_font(key);aliases=f.get('aliases',{});text=''.join(aliases.get(c,c)for c in text)
    context=f.get('hyphen_context')
    if context:
        text=''.join(context['glyph']if c=='-' and 0<i<len(text)-1 and text[i-1]in context['left']and text[i+1]in context['right']else c for i,c in enumerate(text))
    return text


def measure(text,key=None,scale=1.,tracking=None):
    f=face_font(key);stretch=f['horizontal_scale']*scale
    spacing=f['letter_spacing']if tracking is None else tracking
    x=0.;left=bottom=right=top=0.;has_ink=False
    pairs=f.get('kerning',{});previous=''
    for ch in font_text(text,key):
        x+=pairs.get(previous+ch,0.)*stretch if previous else 0.
        g=glyph_for(ch,key);l,b,r,t=g['bounds']
        if r>l and t>b:
            if not has_ink:left,right,bottom,top=x+l*stretch,x+r*stretch,b,t;has_ink=True
            else:left=min(left,x+l*stretch);right=max(right,x+r*stretch);bottom=min(bottom,b);top=max(top,t)
        x+=(g['advance']+spacing)*stretch
        previous=ch
    return right-left,top-bottom,(left,bottom,right,top),x


def advance(text,key=None):
    f=face_font(key)
    text=font_text(text,key);pairs=f.get('kerning',{})
    return (sum(glyph_for(c,key)['advance']+f['letter_spacing']for c in text)+sum(pairs.get(a+b,0.)for a,b in zip(text,text[1:])))*f['horizontal_scale']


def town_layout(name,style=None,details=()):
    import townlayout
    if style is None:
        import europeinfra
        style=europeinfra.town_style('FR')
    name=clean(name,style.uppercase)
    return townlayout.compose(name,style,measure,details)


def layout(name,uppercase=True,style=None,details=()):
    plan=town_layout(clean(name,uppercase),style,details)
    return clean(name,uppercase),plan.width,plan.height,plan.lines[0].cap


class Mesh:
    def __init__(self,origin=(0,0,0)):
        self.origin=np.asarray(origin,float);self.p=[];self.uv=[];self.t=[];self.tm=[]
        self.offset=np.zeros(3);self.axes=np.eye(3)
    def transform(self,position,into):
        d=np.asarray(into,float);d=d/max(np.linalg.norm(d),1e-9)
        self.offset=np.asarray(position,float)-self.origin
        self.axes=np.array([[d[1],-d[0],0],[d[0],d[1],0],[0,0,1]])
    def face(self,mat,pts,uv=None):
        p=np.asarray(pts,float)@self.axes+self.offset;k=len(self.p)
        self.p.extend(p);self.uv.extend(uv if uv is not None else [(0,0)]*len(p))
        for i in range(1,len(p)-1):self.t.append((k,k+i,k+i+1));self.tm.append(mat)
    def box(self,mat,c,half):
        c=np.asarray(c);h=np.asarray(half)
        v=lambda a,b,d:c+h*np.array([a,b,d])
        for face in (((-1,-1,-1),(-1,-1,1),(-1,1,1),(-1,1,-1)),
                     ((1,-1,-1),(1,1,-1),(1,1,1),(1,-1,1)),
                     ((-1,-1,-1),(1,-1,-1),(1,-1,1),(-1,-1,1)),
                     ((-1,1,-1),(-1,1,1),(1,1,1),(1,1,-1)),
                     ((-1,-1,-1),(-1,1,-1),(1,1,-1),(1,-1,-1)),
                     ((-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1))):
            self.face(mat,[v(*q) for q in face])
    def rect(self,mat,w,h,z,y,r=.045):
        # Convex rounded rectangle seen from -Y, CCW.
        pts=[];r=min(r,w/2,h/2)
        for cx,cz,start in ((w/2-r,z-h/2+r,-90),(w/2-r,z+h/2-r,0),(-w/2+r,z+h/2-r,90),(-w/2+r,z-h/2+r,180)):
            for a in np.linspace(start,start+90,5):
                rad=math.radians(a);pts.append((cx+r*math.cos(rad),y,cz+r*math.sin(rad)))
        self.face(mat,pts)
    def panel_body(self,w,h,z,r):
        points=[];r=min(r,w/2,h/2)
        for cx,cz,start in ((w/2-r,z-h/2+r,-90),(w/2-r,z+h/2-r,0),(-w/2+r,z+h/2-r,90),(-w/2+r,z-h/2+r,180)):
            for a in np.linspace(start,start+90,5):
                a=math.radians(a);points.append((cx+r*math.cos(a),cz+r*math.sin(a)))
        front=[(x,-.025,zz)for x,zz in points];back=[(x,.025,zz)for x,zz in points]
        self.face(METAL,front);self.face(METAL,list(reversed(back)))
        for i,a in enumerate(front):
            j=(i+1)%len(front);self.face(METAL,[a,back[i],back[j],front[j]])
    def german_frame(self,w,h,z):
        """Separate rounded steel tube, open gap and four face clamps.

        26 mm tube and 25 mm clear gap are visual estimates from the reference,
        not claimed national mounting dimensions. The yellow plate stays 900x600.
        """
        radius=.013;gap=.025;r=.070
        hw=w/2+.0125+gap+radius;hh=h/2+.0125+gap+radius
        rings=[]
        for cx,cz,start in ((hw-r,z-hh+r,-90),(hw-r,z+hh-r,0),(-hw+r,z+hh-r,90),(-hw+r,z-hh+r,180)):
            for angle in np.linspace(start,start+90,7):
                a=math.radians(angle);out=np.array([math.cos(a),0.,math.sin(a)])
                c=np.array([cx+r*math.cos(a),0.,cz+r*math.sin(a)])
                rings.append([c+radius*(math.cos(b)*out+np.array([0.,math.sin(b),0.]))
                              for b in np.linspace(0,2*math.pi,8,endpoint=False)])
        for i,ring in enumerate(rings):
            following=rings[(i+1)%len(rings)]
            for j in range(8):
                k=(j+1)%8
                self.face(METAL,[ring[j],ring[k],following[k],following[j]])
        for x in (-w*.36,w*.36):
            for side in (-1,1):
                cz=z+side*(h/2+.012)
                self.box(METAL,(x,-.043,cz),(.012,.009,.040))
                self.box(BLACK,(x,-.053,cz-side*.020),(.004,.002,.004))

    def text(self,text,z,y,height,max_width,white=False,uppercase=True,x_offset=0,
             font_key=None,scale=1.,tracking=None,centre_ink=False):
        text=clean(text,uppercase);f=face_font(font_key);stretch=f['horizontal_scale']*scale
        spacing=f['letter_spacing']if tracking is None else tracking
        if centre_ink:
            width,_,(l,b,r,t),_=measure(text,font_key,scale,tracking)
            h=min(height,max_width/max(width,.01));x=-(l+r)*h/2+x_offset
            z-=((b+t)/2)*h
        else:
            h=min(height,max_width/max(advance(text,font_key)*scale,.01));x=-advance(text,font_key)*scale*h/2+x_offset
        pairs=f.get('kerning',{});previous=''
        for ch in font_text(text,font_key):
            x+=pairs.get(previous+ch,0.)*stretch*h if previous else 0.
            g=glyph_for(ch,font_key);l,b,r,t=g['bounds'];u0,v0,u1,v1=g['uv']
            if r>l and t>b:
                self.face(GLYPH_WHITE if white else GLYPH,[(x+l*h*stretch,y,z+b*h),(x+r*h*stretch,y,z+b*h),
                                 (x+r*h*stretch,y,z+t*h),(x+l*h*stretch,y,z+t*h)],
                          [(u0,v0),(u1,v0),(u1,v1),(u0,v1)])
            x+=(g['advance']+spacing)*stretch*h
            previous=ch

    def _skyline(self,code,w,h,z,y):
        # Flat national sign pictograms, not invented landmarks of this town.
        shapes={
          'BE':[(0,0),(0,.26),(.12,.21),(.12,.40),(.24,.45),(.24,.88),(.29,1),(.34,.88),(.34,.49),(.53,.39),(.53,.15),(.66,.28),(.76,.22),(.76,.53),(.95,.64),(1,.58),(1,0)],
          'DK':[(0,0),(0,.42),(.11,.61),(.23,.42),(.23,.28),(.35,.28),(.35,.70),(.39,1),(.43,.70),(.43,.38),(.63,.38),(.63,.63),(.76,.78),(.90,.63),(.90,.24),(1,.24),(1,0)],
          'EE':[(0,0),(0,.25),(.14,.45),(.28,.25),(.28,.50),(.40,.50),(.40,.85),(.44,1),(.48,.85),(.48,.50),(.59,.50),(.59,.32),(.70,.46),(.83,.32),(.83,.64),(1,.64),(1,0)],
          'IS':[(0,0),(0,.44),(.16,.70),(.32,.44),(.32,.22),(.42,.22),(.42,.75),(.49,1),(.56,.75),(.56,.30),(.70,.30),(.70,.52),(.85,.74),(1,.52),(1,0)],
          'PL':[(0,0),(0,.30),(.10,.45),(.20,.30),(.20,.78),(.28,1),(.35,.78),(.35,.29),(.45,.48),(.58,.48),(.68,.27),(.75,.27),(.75,.55),(.86,.65),(.94,.55),(.94,.36),(1,.36),(1,0)],
        }
        points=shapes.get(code,shapes['PL']);sw=min(w-.25,1.6);sh=min(.46,h-.44)
        bottom=z-h/2+.095
        # The skyline is concave. Split its x-monotone upper contour into
        # trapezoids; a triangle fan would fill gaps between towers incorrectly.
        for (a,az),(b,bz) in zip(points[1:-1],points[2:]):
            if b<=a:continue
            verts=[((a-.5)*sw,y,bottom),((b-.5)*sw,y,bottom),
                   ((b-.5)*sw,y,bottom+bz*sh),((a-.5)*sw,y,bottom+az*sh)]
            self.face(BLACK,verts)

    def _town_face(self,name,style,exit=False,route='',details=()):
        plan=town_layout(name,style,details);w,h,z=plan.width,plan.height,plan.centre;p=plan.profile
        bg,edge=COLOUR[style.background],COLOUR[style.border]
        if exit and plan.country in {'FR','MC'}:edge=BLACK
        self.rect(METAL,w+.025,h+.025,z,-.026,p.radius+.01)
        self.rect(bg,w,h,z,-.027,p.radius)
        self.rect(edge,w-2*p.outer,h-2*p.outer,z,-.029,max(.008,p.radius-p.outer))
        inset=p.outer+p.border
        self.rect(bg,w-2*inset,h-2*inset,z,-.031,max(.004,p.radius-inset))
        for line in plan.lines:
            self.text(line.text,line.centre,-.035,line.cap,w-2*p.pad_x,white=style.ink=='white',uppercase=False,
                      font_key=line.font,scale=line.scale,tracking=line.tracking,centre_ink=True)
        if style.footer:
            self.face(COLOUR[style.footer],[(-w/2+inset,-.034,plan.bottom+inset),(w/2-inset,-.034,plan.bottom+inset),
                (w/2-inset,-.034,plan.bottom+inset+.045),(-w/2+inset,-.034,plan.bottom+inset+.045)])
        if plan.skyline:self._skyline(style.symbol,w,h,z,-.034)
        if plan.header:
            top=plan.top-inset;bottom=top-plan.header
            self.face(YELLOW,[(-w/2+inset,-.033,bottom),(w/2-inset,-.033,bottom),
                              (w/2-inset,-.033,top),(-w/2+inset,-.033,top)])
            cx=-w/2+inset+.18;cz=(top+bottom)/2
            for material,r,y in ((RED,.145,-.035),(WHITE,.115,-.036)):
                self.face(material,[(cx+r*math.cos(a),y,cz+r*math.sin(a))for a in np.linspace(0,2*math.pi,24,endpoint=False)])
            self.text(str(style.header_speed),cz,-.038,.12,.21,x_offset=cx,font_key='din',centre_ink=True)
        if exit:
            # A slash has a real, bounded stroke width, not a trapezoid whose
            # apparent thickness changes with the name's length.
            a=np.array([-w/2+inset+.018,plan.bottom+inset+.018])
            b=np.array([w/2-inset-.018,plan.top-inset-.018])
            delta=b-a;normal=np.array([-delta[1],delta[0]])/max(np.linalg.norm(delta),1e-9)
            half=min(.045,h*.075)
            # Keep all four corners inside the inner board rectangle.
            v=[a-normal*half,b-normal*half,b+normal*half,a+normal*half]
            lo=np.array([-w/2+inset,plan.bottom+inset]);hi=np.array([w/2-inset,plan.top-inset])
            v=[np.maximum(lo,np.minimum(hi,q))for q in v]
            self.face(RED,[(q[0],-.040,q[1])for q in v])
        route=valid_route(route)if plan.country in {'FR','MC'}else''
        if route:
            rh=.235;rw=max(.40,measure(route,'l1')[0]*.145+.12);rz=plan.top+.055+rh/2
            # A short rear stay also supports the cartouche on wide two-post
            # boards; the road-number plate must not float above the main face.
            self.box(METAL,(0,.045,plan.top+.105),(.020,.020,.165))
            self.rect(METAL,rw+.018,rh+.018,rz,-.026,.025)
            self.rect(BLACK,rw,rh,rz,-.028,.022)
            self.rect(YELLOW,rw-.024,rh-.024,rz,-.030,.016)
            self.text(route,rz,-.034,.145,rw-.10,uppercase=False,font_key='l1',centre_ink=True)
        return w,h

    def town_face(self,name,exit=False,route=''):
        import europeinfra
        return self._town_face(name,europeinfra.town_style('FR'),exit,route)[0]

    def country_town_face(self,name,style,exit=False,details=()):
        return self._town_face(name,style,exit,details=details)

    def town(self,position,into,name,route='',kind='both',style=None,details=()):
        import europeinfra
        style=style or europeinfra.town_style('FR');plan=town_layout(name,style,details)
        self.transform(position,into)
        self.panel_body(plan.width+.025,plan.height+.025,plan.centre,plan.profile.radius+.01)
        if plan.country=='DE':self.german_frame(plan.width,plan.height,plan.centre)
        if kind in ('both','begin'):self._town_face(name,style,False,route,details)
        if kind in ('both','end')and style.exit!='none':
            self.transform(position,-np.asarray(into))
            self._town_face(name,style,True,route,details)
        return plan.width
    def direction(self,position,travel,labels,arrow='right',route='',body=True,start_row=0,colour='white'):
        import euguardrails
        if euguardrails.enabled():
            from frroadmesh import direction
            return direction(self,position,travel,labels,arrow,route,body,start_row,colour)
        self.transform(position,travel)
        for row,label in enumerate(labels[:4]):
            label=clean(label)
            w=max(1.1,min(3.6,advance(label)*.23+.45));z=2.5-(start_row+row)*.38
            sign=1 if arrow=='right' else -1
            p=[(-w/2,-.025,z-.16),(w/2-.20,-.025,z-.16),(w/2,-.025,z),(w/2-.20,-.025,z+.16),(-w/2,-.025,z+.16)]
            if sign<0:p=[(-x,y,zz) for x,y,zz in reversed(p)]
            self.face(BLACK,p)
            if body:
                for i,a in enumerate(p):
                    b=p[(i+1)%len(p)]
                    self.face(METAL,[a,(a[0],.025,a[2]),(b[0],.025,b[2]),b])
            p2=[(x*.975,-.028,z+(zz-z)*.88) for x,y,zz in p];self.face(WHITE,p2)
            self.text(label,z-.10,-.032,.21,w-.50)
        route=valid_route(route)
        if route:
            self.rect(YELLOW,max(.55,advance(route)*.17+.15),.28,2.85,-.025,.015)
            self.text(route,2.77,-.03,.16,1.4)
    def write(self,path):
        if not self.t:return False
        if len(self.p)>60000:raise ValueError('Regional sign tile exceeds vertex budget')
        import cdae
        cdae.write(str(path),np.asarray(self.p),np.asarray(self.t),uvs=np.asarray(self.uv),
                   material=getattr(self,'materials',MATERIALS),tri_materials=np.asarray(self.tm),object_name='regionalSigns')
        return True


def valid_route(value):
    # Only department roads in this first yellow-cartouche kit. A/N colour systems
    # are intentionally not misrepresented by painting every road number yellow.
    m=re.fullmatch(r'D\s*([0-9]{1,4}[A-Z]?)',clean(value))
    return 'D '+m.group(1) if m else ''
