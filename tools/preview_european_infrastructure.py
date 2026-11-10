"""Offline technical contact sheets rendered from the actual generated triangles.

This tiny orthographic renderer is for mesh/font inspection, NOT a BeamNG render.
It samples the shipped glyph opacity atlas, so previews expose missing letters,
mirrored faces, bad UVs and unreadable layouts. No user photographs are edited.
"""
import argparse
import math
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw,ImageFont
import build_european_infrastructure as a
import europeinfra as ei
import signmesh

FONT=a.ROOT/'tools/fonts/DejaVuSans-Bold.ttf'
MASK=np.asarray(Image.open(a.OUT.parent/'regional_signs/sign_font_opacity.data.png'))

def render(mesh,w,h,angle=0,crop=False):
    points=np.asarray(mesh.p,float);uv=np.asarray(mesh.uv,float)
    if isinstance(mesh.tri,dict):
        triangles=[(np.asarray(t),mat) for mat,tt in mesh.tri.items() for t in tt]
    else:raise ValueError('Expected static mesh')
    angle=math.radians(angle);u=np.array([math.cos(angle),math.sin(angle),0.])
    look=np.array([math.sin(angle),-math.cos(angle),.10 if angle else 0]);look/=np.linalg.norm(look)
    v=np.cross(look,u);screen=np.c_[points@u,points@v]
    low=screen.min(0);high=screen.max(0)
    scale=min((w-20)/max(high[0]-low[0],.1),(h-20)/max(high[1]-low[1],.1))
    xy=(screen-(low+high)/2)*[scale,-scale]+[w/2,h/2];depth=points@look
    canvas=np.full((h,w,3),244.,float);zbuf=np.full((h,w),-np.inf)
    for indices,mat in triangles:
        p=points[indices];normal=np.cross(p[1]-p[0],p[2]-p[0]);normal/=max(np.linalg.norm(normal),1e-12)
        if normal@look<=1e-7:continue
        q=xy[indices];xmin,ymin=np.maximum(np.floor(q.min(0)).astype(int),0);xmax,ymax=np.minimum(np.ceil(q.max(0)).astype(int),[w-1,h-1])
        if xmin>xmax or ymin>ymax:continue
        xx,yy=np.meshgrid(np.arange(xmin,xmax+1)+.5,np.arange(ymin,ymax+1)+.5)
        denominator=(q[1,1]-q[2,1])*(q[0,0]-q[2,0])+(q[2,0]-q[1,0])*(q[0,1]-q[2,1])
        if abs(denominator)<1e-10:continue
        b0=((q[1,1]-q[2,1])*(xx-q[2,0])+(q[2,0]-q[1,0])*(yy-q[2,1]))/denominator
        b1=((q[2,1]-q[0,1])*(xx-q[2,0])+(q[0,0]-q[2,0])*(yy-q[2,1]))/denominator;b2=1-b0-b1
        zz=b0*depth[indices[0]]+b1*depth[indices[1]]+b2*depth[indices[2]]
        sub=zbuf[ymin:ymax+1,xmin:xmax+1];ok=(b0>=0)&(b1>=0)&(b2>=0)&(zz>=sub-1e-7)
        if 'glyph' in mat:
            tex=b0[...,None]*uv[indices[0]]+b1[...,None]*uv[indices[1]]+b2[...,None]*uv[indices[2]]
            tx=np.clip((tex[:,:,0]*MASK.shape[1]).astype(int),0,MASK.shape[1]-1)
            ty=np.clip(((1-tex[:,:,1])*MASK.shape[0]).astype(int),0,MASK.shape[0]-1)
            ok&=MASK[ty,tx]>80
            colour=np.array([.95]*3 if 'white' in mat else [.008]*3)
        elif mat.startswith('bne_eui_'):colour=np.array(a.COLOURS[mat[8:]])
        else:
            colour=np.array({'white':(.93,.93,.89),'red':(.76,.015,.02),'black':(.01,.012,.013),
                             'metal':(.46,.49,.5),'yellow':(.95,.69,.05),'blue':(.015,.1,.4),'green':(.025,.23,.08)}[mat[9:]])
        light=np.array([-.3,-.7,.6]);light/=np.linalg.norm(light)
        rgb=np.power(np.clip(colour*(.65+.35*max(float(normal@light),0)),0,1),1/2.2)*255
        canvas[ymin:ymax+1,xmin:xmax+1][ok]=rgb;sub[ok]=zz[ok]
    return Image.fromarray(canvas.astype('uint8'))

def dynamic(mesh):
    out=a.Mesh()
    for triangle,mat in zip(mesh.t,mesh.tm):out.face(signmesh.MATERIALS[mat],[mesh.p[i] for i in triangle],[mesh.uv[i] for i in triangle])
    return out

def sheet(items,cols,cell,title,path):
    cw,ch=cell;rows=(len(items)+cols-1)//cols
    image=Image.new('RGB',(cols*cw,rows*ch+110),'#f4f4f4');draw=ImageDraw.Draw(image)
    draw.text((24,16),title,font=ImageFont.truetype(str(FONT),26),fill='#142338')
    draw.text((24,54),'Maillages du générateur • aperçu technique hors jeu • éclairage simplifié',font=ImageFont.truetype(str(FONT),16),fill='#53616b')
    for i,(label,m,angle) in enumerate(items):
        x=(i%cols)*cw;y=100+(i//cols)*ch
        im=render(m,(cw-16)*2,(ch-55)*2,angle).resize((cw-16,ch-55),Image.Resampling.LANCZOS)
        image.paste(im,(x+8,y));draw.text((x+cw/2,y+ch-43),label,anchor='mt',font=ImageFont.truetype(str(FONT),14),fill='#142338')
    image.save(path)

def build(out):
    out.mkdir(parents=True,exist_ok=True)
    labels={'wood_side':'Bois / isolateurs latéraux','wood_t':'Bois / traverse','wood_a':'Bois / support en A','wood_h':'Bois / portique',
            'concrete_round':'Béton rond','concrete_open':'Béton ajouré','concrete_pair':'Béton double','concrete_three':'Béton / consoles','steel_lattice':'Treillis métallique'}
    sheet([(labels[k],a.pole(k),18) for k in ei.POLE_LAYOUTS],9,(220,580),'Poteaux européens — 9 familles nouvelles',out/'poteaux_europe.png')
    sheet([(k,a.marker(k),12) for k in a.MARKERS],10,(145,315),'Balises — 19 familles, sans collision',out/'balises_europe.png')
    boards=[]
    for scheme in sorted({k.chevron for k in ei.KITS.values()}):
        boards.append((scheme.replace('_',' / '),a.chevron(scheme,True),0))
    boards.extend([(label,a.stop(label),0) for label in ('ARRÊT','DUR')])
    sheet(boards,3,(380,300),'Chevrons et panneaux d’arrêt — modèles ajoutés',out/'chevrons_arret_dur.png')
    towns=[]
    for code,name in [('FR','LEMBACH'),('DE','Wilster'),('AT','Bregenz'),('NL','Helmond'),('PL','Piastów'),('SE','NYKÖPING'),
                      ('CZ','PLZEŇ'),('SK','Ružomberok'),('DK','Lyngby'),('IS','Mosfellsbær'),('RU','ЛИПЕЦК'),('UA','ОДЕСА'),
                      ('TR','DEMİRÖREN'),('GR','ΘΕΣΣΑΛΟΝΙΚΗ'),('RO','Vălenii de Munte'),('IT','TARANTO')]:
        mesh=signmesh.Mesh();mesh.town((0,0,0),(0,1),name,style=None if code=='FR' else ei.town_style(code,speed=50 if code=='RO' else None))
        towns.append((code,dynamic(mesh),0))
    sheet(towns,4,(400,200),'Panneaux de localité — exemples du rendu partagé',out/'entrees_villes.png')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('output',type=Path);build(p.parse_args().output)
