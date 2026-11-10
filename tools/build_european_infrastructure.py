"""Reproducible original European scenery meshes, in metres, front towards -Y.

No photographic pixels are copied. Country choices live in europeinfra.py.
Three visual LODs; roadside delineators have NO collision mesh. Text uses the
shared sign alphabet, including the accent in ARRÊT. Run build_sign_font first.
"""
import json
import math
from pathlib import Path
import sys
import uuid
import xml.etree.ElementTree as ET
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from build_rail_crossing_props import Mesh,write_dae
from build_french_furniture import tapered
from build_regional_poles import face
import europeinfra as ei
import signmesh

OUT=ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/europe_infrastructure'
VFS='/levels/beamng_earth/art/shapes/beamngEarth/europe_infrastructure'
COLOURS={'white':(.89,.89,.86),'black':(.018,.02,.022),'red':(.70,.015,.02),
         'blue':(.012,.09,.43),'yellow':(.95,.75,.015),'orange':(.96,.27,.015),
         'steel':(.46,.49,.50),'concrete':(.52,.51,.48),'wood':(.42,.33,.22),
         'green':(.03,.31,.10),'ceramic':(.13,.20,.17),'reflect':(.91,.92,.88)}
def mat(key):return 'bne_eui_'+key


def beam(m,material,a,b,r=.035):
    a,b=np.asarray(a,float),np.asarray(b,float);u=b-a;length=np.linalg.norm(u)
    u/=length;v=np.cross(u,[0,1,0]);v/=np.linalg.norm(v);w=np.cross(u,v)
    m.box(material,(a+b)/2,(r,r,length/2),(v,w,u))


def insulator(m,point,detail):
    x,y,z=point
    m.cylinder(mat('steel'),x,y,z-.22,z+.025,.012)
    if detail==2:
        m.cylinder(mat('ceramic'),x,y,z-.17,z,.045)
    else:
        for dz in (-.16,-.10,-.04):
            m.cylinder(mat('ceramic'),x,y,z+dz,z+dz+.035,.058)
        m.cylinder(mat('ceramic'),x,y,z-.18,z,.028)


def pole(kind,detail=0,collision=False):
    m=Mesh((10,7,5)[detail]);height,anchors=ei.POLE_LAYOUTS[kind]
    wood=mat('wood');concrete=mat('concrete');steel=mat('steel')
    if collision:
        if kind in ('wood_a','wood_h'):
            for s in (-1,1):beam(m,wood,(s*(.90 if kind=='wood_h' else .65),0,0),
                                (s*(.90 if kind=='wood_h' else .10),0,height),.10)
        else:m.cylinder(concrete if kind.startswith('concrete') else wood,0,0,0,height,.14)
        return m
    if kind in ('wood_side','wood_t'):
        tapered(m,wood,0,height,.145,.085)
    elif kind=='wood_a':
        for s in (-1,1):beam(m,wood,(s*.65,0,0),(s*.10,0,height),.10)
        beam(m,wood,(-.48,0,2.5),(.48,0,2.5),.06)
    elif kind=='wood_h':
        for s in (-1,1):tapered(m,wood,0,height,.145,.085,x=s*.90)
        beam(m,wood,(-.90,0,7.88),(.90,0,7.88),.07)
    elif kind in ('concrete_round','concrete_three'):
        tapered(m,concrete,0,height,.185,.105)
        if detail==0:
            for z in (1.3,3.4,5.5):m.cylinder(concrete,0,0,z,z+.018,.185-(.08*z/height)+.005)
    elif kind=='concrete_pair':
        for s in (-1,1):tapered(m,concrete,0,height,.11,.07,x=s*.18)
        for z in np.arange(.6,7.8,1.2):m.box(concrete,(0,0,float(z)),(.29,.12,.065))
    elif kind=='concrete_open':
        # Real through-openings between two tapering rails; no black painted holes.
        for s in (-1,1):beam(m,concrete,(s*.18,0,0),(s*.10,0,7.6),.058)
        for z in np.arange(.3,7.7,.60):
            half=.18-.08*z/7.6+.058;m.box(concrete,(0,0,float(z)),(half,.058,.055))
        m.box(concrete,(0,0,8.05),(.15,.09,.45))
    elif kind=='steel_lattice':
        for s in (-1,1):beam(m,steel,(s*.26,0,0),(s*.10,0,height),.025)
        for i,z in enumerate(np.arange(.2,8.0,.65)):
            w=.26-.16*z/height;w2=.26-.16*(z+.65)/height
            beam(m,steel,(-w,0,z),(w,0,z),.018)
            beam(m,steel,((-1 if i%2 else 1)*w,0,z),((1 if i%2 else -1)*w2,0,z+.65),.014)
    if kind in ('wood_side','concrete_round','concrete_three'):
        for x,y,z in anchors:
            beam(m,steel,(0,0,z-.20),(x,0,z-.20),.025)
    else:
        xs=[p[0] for p in anchors];z=anchors[0][2]-.20
        beam(m,steel,(min(xs)-.12,0,z),(max(xs)+.12,0,z),.035)
        if detail<2:
            for s in (-1,1):beam(m,steel,(0,0,z-.60),(s*.70,0,z),.018)
    for p in anchors:insulator(m,p,detail)
    return m


# Body, marking, right-hand approaching-face reflector, reverse reflector.
# Circle pair vs rectangle and sloping caps remain visible at the second LOD.
MARKERS={
    'ad':('white','black','orange','white','slope'),
    'at':('white','cap','red','white','flat'),
    'be':('white','none','orange','white','round'),
    'ch':('white','black','white','two_white','round'),
    'cz':('white','black','two_orange','white','flat'),
    'de':('white','black','white','two_white','slope'),
    'dk':('white','red_top','yellow','white','slope'),
    'es':('white','black','yellow','two_white','slope'),
    'fo':('yellow','red','white','white','flat'),
    'gb':('white','black','red','white','round'),
    'ie':('white','green','white','white','round'),
    'is':('yellow','none','white','white','slope'),
    'nl':('white','none','red','white','round'),
    'nordic':('white','black','white','two_yellow','slope'),
    'pl':('white','red','red','white','slope'),
    'red_black':('white','black','red','white','slope'),
    'rs':('white','cap','red','white','flat'),
    'si':('white','cap','red','white','flat'),
    'tr':('white','none','red','white','slope'),
}


def marker(kind,detail=0):
    m=Mesh((12,8,6)[detail]);body,band,front,back,cap=MARKERS[kind]
    w,d,h=.12,.055,1.15
    if cap=='round':
        m.cylinder(mat(body),0,0,0,h-.08,.075)
        tapered(m,mat(body),h-.08,h,.075,.045)
    else:
        top=[h-.07 if cap=='slope' else h,h]
        pts=[(-w/2,-d,0),(w/2,-d,0),(w/2,-d,top[1]),(-w/2,-d,top[0])]
        face(m,mat(body),pts,(0,-1,0));face(m,mat(body),[(x,d,z) for x,y,z in pts],(0,1,0))
        for i in range(4):
            a,b=pts[i],pts[(i+1)%4];m.face(mat(body),[a,(a[0],d,a[2]),(b[0],d,b[2]),b])
    for side,ref in ((-1,front),(1,back)):
        y=side*(.076 if cap=='round' else d+.001)
        def panel(col,x0,x1,z0,z1,slant=0,depth=0):
            face(m,mat(col),[(x0,y+side*depth,z0),(x1,y+side*depth,z0+slant),
                              (x1,y+side*depth,z1+slant),(x0,y+side*depth,z1)],(0,side,0))
        if band!='none':
            col='black' if band in ('black','cap') else 'red' if band=='red_top' else band
            if band=='cap':panel(col,-.060,.060,1.01,1.12)
            elif band=='red_top':panel('red',-.060,.060,1.04,1.075,.05)
            else:panel(col,-.060,.060,.67,1.00,.045 if cap=='slope' else 0)
        # Reflector rectangles or discs on opposite faces; no reversed/mirrored text.
        if ref.startswith('two_'):
            col=ref[4:];col='reflect' if col=='white' else col
            for z in (.78,.91):
                if ref=='two_orange':panel(col,-.043,.043,z-.045,z+.045,depth=.003)
                else:m.disc_y(mat(col),0,y+side*.003,z,.032,front=side==-1)
        else:panel('reflect' if ref=='white' else ref,-.027,.027,.735,.95,depth=.003)
    if kind=='ie':
        # Green/white ringed bollard, not a continental flat delineator.
        for z in (.30,.64,1.0):m.cylinder(mat('green'),0,0,z,min(z+.13,1.14),.077)
    if kind=='gb':m.cylinder(mat('black'),0,0,0,.18,.076)
    return m


def chevron(scheme,right=False,detail=0):
    fg,bg=scheme.split('_');m=Mesh((10,7,5)[detail]);z=1.60
    # A broad three-chevron board, three convex strips per direction.
    for x in (-.70,.70):m.cylinder(mat('steel'),x,.07,0,z,.032)
    m.box(mat('steel'),(0,0,z),(1.,.022,.25))
    face(m,mat(bg),[(-1,-.024,z-.25),(1,-.024,z-.25),(1,-.024,z+.25),(-1,-.024,z+.25)],(0,-1,0))
    for c in (-.69,-.05,.59):
        for lo,hi in ((z-.25,z),(z,z+.25)):
            pts=[(c+.14,-.026,lo),(c+.36,-.026,lo),(c+.06,-.026,hi),(c-.16,-.026,hi)] if lo<z else [(c-.16,-.026,lo),(c+.06,-.026,lo),(c+.36,-.026,hi),(c+.14,-.026,hi)]
            face(m,mat(fg),pts,(0,-1,0))
    return m.mirrored() if right else m


def stop(label,detail=0):
    m=Mesh((10,7,5)[detail]);z=2.61;r=.39
    m.cylinder(mat('steel'),0,.055,0,2.70,.033)
    def octagon(material,radius,y):
        pts=[(radius*math.cos(math.radians(22.5+45*i)),y,z+radius*math.sin(math.radians(22.5+45*i))) for i in range(8)]
        face(m,material,pts,(0,-1,0));return pts
    pts=octagon(mat('white'),r,-.020)
    octagon(mat('red'),r-.025,-.022)
    face(m,mat('steel'),[(x,.020,zz) for x,y,zz in pts],(0,1,0))
    for i,a in enumerate(pts):
        b=pts[(i+1)%8];m.face(mat('steel'),[a,(a[0],.020,a[2]),(b[0],.020,b[2]),b])
    lettering=signmesh.Mesh();lettering.text(label,z-.095,-.024,.205,.63,white=True)
    for triangle,material in zip(lettering.t,lettering.tm):
        m.face(signmesh.MATERIALS[material],[lettering.p[i] for i in triangle],[lettering.uv[i] for i in triangle])
    return m


def materials():
    data={}
    for key,colour in COLOURS.items():
        name=mat(key);stage={'baseColorFactor':[*colour,1],'metallicFactor':.65 if key=='steel' else 0.,'roughnessFactor':.85 if key in ('wood','concrete') else .48}
        if key=='wood':stage['baseColorMap']='/levels/beamng_earth/art/shapes/beamngEarth/fr_furniture/wood_grain.png'
        if key=='concrete':stage['baseColorMap']='/levels/beamng_earth/art/shapes/beamngEarth/regional_poles/concrete_grain.png'
        if key in ('white','yellow','red','orange','reflect'):stage['retroreflectivity']=.8
        data[name]={'name':name,'mapTo':name,'class':'Material','version':1.5,'Stages':[stage],
                    'persistentId':str(uuid.uuid5(uuid.NAMESPACE_URL,'beamng-earth:europe-infrastructure:'+name)),'doubleSided':False}
    (OUT/'main.materials.json').write_text(json.dumps(data,indent=2)+'\n')


def write_model(name,lods,col):
    path=OUT/('bne_'+name+'.dae')
    write_dae(path,'bne_'+name,lods,col or Mesh())
    if col is None:
        # Remove collision declarations entirely as well as NO_COLLIDE in Lua.
        ns='{http://www.collada.org/2005/11/COLLADASchema}';ET.register_namespace('',ns[1:-1])
        tree=ET.parse(path)
        for parent in tree.iter():
            for child in list(parent):
                ident=child.get('id','')
                if ident.startswith('Colmesh_') or ident=='collision-1' or ident.endswith('-col-g'):parent.remove(child)
        tree.write(path,encoding='utf-8',xml_declaration=True)
    return [sum(map(len,m.tri.values())) for size,m in lods]


def build():
    OUT.mkdir(parents=True,exist_ok=True);catalog={};report={}
    def add(name,height,fn,col,dist=500,nocollide=False):
        report[name]=write_model(name,[(96,fn(0)),(24,fn(1)),(2,fn(2))],col)
        catalog[name]={'height':height,'distance':dist,'no_collision':nocollide,'path':VFS+'/bne_'+name}
    for kind in ei.POLE_LAYOUTS:
        add('eu_pole_'+kind,ei.POLE_LAYOUTS[kind][0],lambda d,k=kind:pole(k,d),pole(kind,2,True),800)
    for kind in MARKERS:
        add('eu_marker_'+kind,1.15,lambda d,k=kind:marker(k,d),None,300,True)
    for scheme in sorted({k.chevron for k in ei.KITS.values()}):
        for right in (False,True):
            col=Mesh(5)
            for x in (-.70,.70):col.cylinder(mat('steel'),x,.07,0,1.60,.032)
            add('eu_chevron_'+scheme+('_r' if right else '_l'),1.85,
                lambda d,s=scheme,r=right:chevron(s,r,d),col,400)
    for key,label in (('arret','ARRÊT'),('dur','DUR')):
        col=Mesh(5);col.cylinder(mat('steel'),0,.055,0,2.70,.033)
        add('stop_'+key,3.0,lambda d,l=label:stop(l,d),col,300)
    materials()
    (OUT/'catalog.json').write_text(json.dumps(catalog,indent=2,sort_keys=True)+'\n')
    # Generated Lua data stays in the normal trees.lua asset-loading path.
    lua=['-- Generated by tools/build_european_infrastructure.py; edit the builder.','return {']
    for key,value in sorted(catalog.items()):
        lua.append("  %s = { '%s', %s, %s, %s },"%(key,value['path'],value['height'],value['distance'],str(value['no_collision']).lower()))
    lua.append('}\n')
    (ROOT/'mod/beamng_earth/lua/ge/extensions/beamngEarth/europeAssets.lua').write_text('\n'.join(lua))
    print(json.dumps(report,indent=2));return catalog


if __name__=='__main__':build()
