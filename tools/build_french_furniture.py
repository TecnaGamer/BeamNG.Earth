"""Original low-poly French furniture, generated from dimensions in metres.

Photographs guide silhouette only: no copied photo pixels, branding or real IDs.
The blue tag is decorative, not a claim about a network operator. Trial models,
not manufacturer-certified equipment. Reuse the project's COLLADA exporter.
"""
import argparse
import json
import math
from pathlib import Path
import sys
import uuid
import numpy as np
from PIL import Image
from build_rail_crossing_props import Mesh, write_dae
from build_regional_poles import face

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/fr_furniture'
VFS = '/levels/beamng_earth/art/shapes/beamngEarth/fr_furniture'
WOOD, STEEL, DARK, BLUE = ('bne_fr_'+s for s in ('wood','steel','dark','blue'))
WHITE, RED, GREEN, GLASS, REFLECT = ('bne_fr_'+s for s in ('white','red','green','glass','reflector'))
CHEVRON = 'bne_fr_chevron'
COLOURS = {WOOD:(.52,.45,.34),STEEL:(.43,.46,.46),DARK:(.025,.03,.03),BLUE:(.015,.16,.58),
           WHITE:(.88,.88,.83),RED:(.7,.025,.025),GREEN:(.10,.15,.11),GLASS:(.65,.57,.36),REFLECT:(.78,.79,.72),CHEVRON:(.88,.88,.83)}
HEIGHTS = {'wood_pole':8.5, 'wood_neutral':8.5, 'lantern':5.4, 'j1':1.3, 'j3':1.3, 'j6':1.05, 'j4_l':1.85, 'j4_r':1.85, 'signpost':2.7}


def tapered(m, mat, z0,z1,r0,r1, x=0,y=0):
    ang=np.linspace(0,2*math.pi,m.segs,endpoint=False)
    low=[(x+r0*math.cos(a),y+r0*math.sin(a),z0) for a in ang]
    hi=[(x+r1*math.cos(a),y+r1*math.sin(a),z1) for a in ang]
    for i in range(m.segs):
        j=(i+1)%m.segs
        m.face(mat,[low[i],low[j],hi[j],hi[i]],[(i/m.segs,z0/2),((i+1)/m.segs,z0/2),((i+1)/m.segs,z1/2),(i/m.segs,z1/2)])
    if r0: m.face(mat,list(reversed(low)))
    if r1: m.face(mat,hi)


def wood(detail, blue_plate=True):
    m=Mesh((12,8,5)[detail]); tapered(m,WOOD,0,8.5,.135,.082)
    m.box(STEEL,(.12,0,7.72),(.145,.03,.025));m.box(STEEL,(.25,0,7.79),(.025,.03,.07))
    m.box(DARK,(.25,0,7.85),(.045,.07,.045))
    if detail<2 and blue_plate:
        # Small blue identification plate, visible from the road (-local X).
        m.box(BLUE,(-.12,0,1.55),(.006,.052,.036))
        if detail==0:
            for y in (-.03,-.012,.006,.024):m.box(WHITE,(-.127,y,1.55),(.001,.004,.018))
            m.box(STEEL,(0,0,8.36),(.12,.035,.025))
    return m


def lantern(detail):
    m=Mesh((12,8,5)[detail]); tapered(m,GREEN,0,4.63,.064,.039)
    m.cylinder(GREEN,0,0,0,.23,.10)
    # Square lantern with outward tapered glazing, four corner bars and hipped roof.
    z0,z1=4.65,5.16; a,b=.14,.25
    for s in (-1,1):
        face(m,GLASS,[(s*a,-a,z0),(s*a,a,z0),(s*b,b,z1),(s*b,-b,z1)],(s,0,0))
        face(m,GLASS,[(-a,s*a,z0),(a,s*a,z0),(b,s*b,z1),(-b,s*b,z1)],(0,s,0))
        m.box(GREEN,(s*b,0,z1),(.018,b+.018,.02));m.box(GREEN,(0,s*b,z1),(b,.018,.02))
        m.box(GREEN,(s*a,0,z0),(.015,a+.015,.015));m.box(GREEN,(0,s*a,z0),(a,.015,.015))
    if detail<2:
        for sx in (-1,1):
            for sy in (-1,1):
                p=np.array([sx*a,sy*a,z0]);q=np.array([sx*b,sy*b,z1]);d=q-p;u=d/np.linalg.norm(d)
                v=np.cross(u,[0,1,0]);v/=np.linalg.norm(v);w=np.cross(u,v)
                m.box(GREEN,(p+q)/2,(.015,.015,np.linalg.norm(d)/2),(v,w,u))
    # Hipped roof, broad eaves, small cap (not a photoreal transparent glass simulation).
    r=.29
    base=[(-r,-r,5.19),(r,-r,5.19),(r,r,5.19),(-r,r,5.19)]
    for i in range(4):m.face(GREEN,[base[i],base[(i+1)%4],(0,0,5.34)])
    m.face(GREEN,list(reversed(base)));m.cylinder(GREEN,0,0,5.34,5.4,.04)
    return m


def marker(kind,detail):
    m=Mesh((16,10,6)[detail])
    if kind in ('j1','j3'):
        for a,b,mat in ((0,.68,WHITE),(.68,.93,RED if kind=='j3' else REFLECT),(.93,1.20,WHITE)):
            m.cylinder(mat,0,0,a,b,.10)
        tapered(m,WHITE,1.2,1.3,.10,.001)
    elif kind=='signpost':
        m.box(STEEL,(0,0,1.35),(.03,.025,1.35))
    elif kind=='j6':
        # A French roadside delineator silhouette, not an automatic country identification rule.
        pts=[(-.07,-.035,0),(.07,-.035,0),(.07,-.035,1.05),(-.07,-.035,.97)]
        m.face(WHITE,pts);m.face(WHITE,[(x,.035,z) for x,y,z in reversed(pts)])
        for i in range(4):
            a,b=pts[i],pts[(i+1)%4];m.face(WHITE,[a,(a[0],.035,a[2]),(b[0],.035,b[2]),b])
        for s in (-1,1):
            face(m,DARK,[(-.07,s*.036,.63),(.07,s*.036,.71),(.07,s*.036,.95),(-.07,s*.036,.87)],(0,s,0))
            face(m,REFLECT,[(-.025,s*.037,.73),(.025,s*.037,.73),(.025,s*.037,.85),(-.025,s*.037,.85)],(0,s,0))
    else:
        m.cylinder(STEEL,0,.06,0,1.62,.035);m.box(STEEL,(0,0,1.60),(1.0,.025,.25))
        face(m,BLUE,[(-1,-.027,1.35),(1,-.027,1.35),(1,-.027,1.85),(-1,-.027,1.85)],(0,-1,0))
        # Three left chevrons; split into convex polygons for correct triangulation.
        for c in (-.74,-.10,.54):
            for a,b in ((1.35,1.60),(1.60,1.85)):
                pts=[(c+.18,-.029,a),(c+.43,-.029,a),(c+.12,-.029,b),(c-.13,-.029,b)] if a==1.35 else [(c-.13,-.029,a),(c+.12,-.029,a),(c+.43,-.029,b),(c+.18,-.029,b)]
                face(m,CHEVRON,pts,(0,-1,0))
        if kind=='j4_r':m=m.mirrored()
    return m


def model(kind,detail=0):
    return wood(detail, kind=='wood_pole') if kind in ('wood_pole','wood_neutral') else lantern(detail) if kind=='lantern' else marker(kind,detail)


def collision(kind):
    m=Mesh(6)
    if kind in ('wood_pole','wood_neutral'):tapered(m,WOOD,0,8.5,.135,.082)
    elif kind=='lantern':m.cylinder(GREEN,0,0,0,4.65,.064)
    elif kind in ('j1','j3'):m.cylinder(WHITE,0,0,0,1.3,.1)
    elif kind=='signpost':m.box(STEEL,(0,0,1.35),(.03,.025,1.35))
    elif kind=='j6':m.box(WHITE,(0,0,.525),(.07,.035,.525))
    else:m.cylinder(STEEL,0,.06,0,1.85,.035)
    return m


def materials(out):
    rng=np.random.default_rng(329716)
    stripes=rng.normal(0,.11,(1,128)); fine=rng.normal(0,.018,(512,128))
    grain=np.clip(.83+stripes+fine,.4,1)
    Image.fromarray((np.repeat(grain[:,:,None],3,axis=2)*255).astype(np.uint8)).save(out/'wood_grain.png')
    data={}
    for name,c in COLOURS.items():
        st={'baseColorFactor':[*c,1.0],'metallicFactor':.6 if name==STEEL else 0,'roughnessFactor':.8 if name==WOOD else .48}
        if name==WOOD:st['baseColorMap']=VFS+'/wood_grain.png'
        if name==GLASS:st.update(emissiveFactor=[0,0,0],emissiveIntensityNits=-1)
        if name in (REFLECT,RED,CHEVRON):st['retroreflectivity']=.8
        data[name]={'name':name,'mapTo':name,'class':'Material','version':1.5,
                    'persistentId':str(uuid.uuid5(uuid.NAMESPACE_URL,'beamng-earth:fr-furniture:'+name)),
                    'Stages':[st],'doubleSided':False}
    (out/'main.materials.json').write_bytes((json.dumps(data,indent=2)+'\n').encode())


def build(out=OUT):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);report={}
    for kind in HEIGHTS:
        lods=[(96,model(kind,0)),(24,model(kind,1)),(2,model(kind,2))]
        path=out/('bne_fr_'+kind+'.dae');write_dae(path,'bne_fr_'+kind,lods,collision(kind))
        path.write_bytes(path.read_text().replace('tools/build_rail_crossing_props.py','tools/build_french_furniture.py').encode())
        report[kind]={'triangles':[sum(map(len,m.tri.values())) for _,m in lods], 'collision':sum(map(len,collision(kind).tri.values()))}
    materials(out);return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,default=OUT)
    print(json.dumps(build(p.parse_args().out),indent=2))
