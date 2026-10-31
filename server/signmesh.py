"""Batched dynamic signs. One shared alphabet/material set, no per-name assets.

Metres, local front towards -Y. Town panels grow with the measured glyph advance;
long names are reduced to fit a bounded board. Accents and ligatures are kept.
"""
from functools import lru_cache
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


def clean(text,uppercase=True):
    text=unicodedata.normalize('NFC',str(text or ''))
    if uppercase:text=text.upper()
    text=' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())
    return text[:160]


def advance(text):
    f=font();g=f['glyphs'];return sum(g.get(c,g['?'])['advance']+f['letter_spacing'] for c in text)*f['horizontal_scale']


def layout(name,uppercase=True):
    name=clean(name,uppercase)
    width=max(1.2,min(4.8,advance(name)*.34+.28))
    height=.70
    letter_h=min(.34,(width-.28)/max(advance(name),.01))
    return name,width,height,letter_h


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
    def text(self,text,z,y,height,max_width,white=False,uppercase=True,x_offset=0):
        text=clean(text,uppercase);f=font();glyphs=f['glyphs'];stretch=f['horizontal_scale']
        h=min(height,max_width/max(advance(text),.01));x=-advance(text)*h/2+x_offset
        for ch in text:
            g=glyphs.get(ch,glyphs['?']);l,b,r,t=g['bounds'];u0,v0,u1,v1=g['uv']
            if r>l and t>b:
                self.face(GLYPH_WHITE if white else GLYPH,[(x+l*h*stretch,y,z+b*h),(x+r*h*stretch,y,z+b*h),
                                 (x+r*h*stretch,y,z+t*h),(x+l*h*stretch,y,z+t*h)],
                          [(u0,v0),(u1,v0),(u1,v1),(u0,v1)])
            x+=(g['advance']+f['letter_spacing'])*stretch*h
    def town_face(self,name,exit=False,route=''):
        text,w,h,ch=layout(name);z=2.05
        self.rect(METAL,w+.04,h+.04,z,-.026)
        self.rect(WHITE,w,h,z,-.027)
        self.rect(BLACK if exit else RED,w-.055,h-.055,z,-.029)
        self.rect(WHITE,w-(.09 if exit else .16),h-(.09 if exit else .16),z,-.031)
        self.text(text,z-ch*.5,-.034,ch,w-.28)
        if exit:
            x=w/2-.08;low=z-h/2+.07;high=z+h/2-.07
            self.face(RED,[(-x,-.038,low),(-x,-.038,low+.075),(x,-.038,high),(x,-.038,high-.075)][::-1])
        route=valid_route(route)
        if route:
            self.box(METAL,(0,.04,2.55),(.026,.023,.19))
            rw=max(.50,advance(route)*.17+.15)
            self.rect(BLACK,rw,.29,2.59,-.025,.018)
            self.rect(YELLOW,rw-.025,.265,2.59,-.028,.01)
            self.text(route,2.51,-.032,.16,rw-.10)
        return w
    def country_town_face(self,name,style,exit=False):
        text,w,h,ch=layout(name,style.uppercase);z=2.05
        if style.skyline:h=1.06;ch=min(ch,.30)
        if style.header_speed is not None:h=1.10
        bg,edge=COLOUR[style.background],COLOUR[style.border]
        self.rect(METAL,w+.04,h+.04,z,-.026)
        self.rect(bg,w,h,z,-.027)
        self.rect(edge,w-.055,h-.055,z,-.029)
        self.rect(bg,w-.11,h-.11,z,-.031)
        baseline=z+(.15 if style.skyline else -ch*.5)-(.10 if style.header_speed is not None else 0)
        self.text(text,baseline,-.034,ch,w-.28,white=style.ink=='white',uppercase=style.uppercase)
        if style.footer:
            self.face(COLOUR[style.footer],[(-w/2+.055,-.034,z-h/2+.055),(w/2-.055,-.034,z-h/2+.055),
                                            (w/2-.055,-.034,z-h/2+.13),(-w/2+.055,-.034,z-h/2+.13)])
        if style.header_speed is not None:
            top=z+h/2-.06;bottom=top-.34
            self.face(YELLOW,[(-w/2+.06,-.033,bottom),(w/2-.06,-.033,bottom),(w/2-.06,-.033,top),(-w/2+.06,-.033,top)])
            cx=-w/2+.25;cz=(top+bottom)/2
            for material,r,y in ((RED,.148,-.035),(WHITE,.116,-.036)):
                self.face(material,[(cx+r*math.cos(a),y,cz+r*math.sin(a)) for a in np.linspace(0,2*math.pi,24,endpoint=False)])
            self.text(str(style.header_speed),cz-.065,-.038,.13,.21,x_offset=cx)
            for x,hh in ((-.04,.13),(.16,.22),(.34,.16)):
                self.face(BLACK,[(x,-.035,bottom+.04),(x+.13,-.035,bottom+.04),(x+.13,-.035,bottom+.04+hh),(x,-.035,bottom+.04+hh)])
        if style.skyline:
            # Generic settlement silhouette, no fabricated local landmark.
            for x,ww,hh,roof in ((-.42,.21,.25,True),(-.18,.18,.38,False),(.06,.23,.20,True),(.33,.27,.30,True)):
                x*=min(w/1.4,1.7);ww*=min(w/1.4,1.7);bottom=z-.39
                self.face(BLACK,[(x-ww/2,-.034,bottom),(x+ww/2,-.034,bottom),
                                 (x+ww/2,-.034,bottom+hh),(x-ww/2,-.034,bottom+hh)])
                if roof:self.face(BLACK,[(x-ww/2,-.034,bottom+hh),(x+ww/2,-.034,bottom+hh),(x,-.034,bottom+hh+.10)])
        if exit:
            x=w/2-.07;low=z-h/2+.065;high=z+h/2-.065
            self.face(RED,[(x,-.038,high-.065),(x,-.038,high),(-x,-.038,low+.065),(-x,-.038,low)])
        return w,h

    def town(self,position,into,name,route='',kind='both',style=None):
        self.transform(position,into)
        _,w,h,_=layout(name,True if style is None else style.uppercase)
        if style is not None and style.skyline:h=1.06
        if style is not None and style.header_speed is not None:h=1.10
        self.box(METAL,(0,0,2.05),(w/2+.02,.025,h/2+.02))
        if kind in ('both','begin'):
            if style is None:self.town_face(name,False,route)
            else:self.country_town_face(name,style)
        if kind in ('both','end') and (style is None or style.exit!='none'):
            self.transform(position,-np.asarray(into))
            if style is None:self.town_face(name,True,route)
            else:self.country_town_face(name,style,True)
        return w
    def direction(self,position,travel,labels,arrow='right',route='',body=True,start_row=0):
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
                   material=MATERIALS,tri_materials=np.asarray(self.tm),object_name='regionalSigns')
        return True


def valid_route(value):
    # Only department roads in this first yellow-cartouche kit. A/N colour systems
    # are intentionally not misrepresented by painting every road number yellow.
    m=re.fullmatch(r'D\s*([0-9]{1,4}[A-Z]?)',clean(value))
    return 'D '+m.group(1) if m else ''
