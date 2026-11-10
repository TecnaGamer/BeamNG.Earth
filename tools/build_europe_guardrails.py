"""Original low-poly A, B and thin-B steel rails, with LODs and rigid collision.

The country table is a representative scenery preset, not a manufacturer model.
No photographic pixels are included. Run from the source tree to rebuild assets.
"""
from pathlib import Path
import json
import sys
import uuid
import math
import xml.etree.ElementTree as ET
import numpy as np
from build_rail_crossing_props import Mesh,write_dae
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import euguardrails as eu
from frgore import MODELS as J14_MODELS
OUT=ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/europe_road_details'
VFS='/levels/beamng_earth/art/shapes/beamngEarth/europe_road_details'
STEEL='bne_eur_steel';DARK='bne_eur_dark'
J14_GREEN='bne_fr_j14_green'
COLOURS={'white':(.93,.93,.87),'red':(.8,.025,.02),'yellow':(.96,.78,.02),'orange':(.98,.30,.015)}


def module(family,colour,part='run',detail=True):
    m=Mesh(8)
    # Cross section (depth, height): A is rounded corrugation, B has stepped flats.
    sections={
      'a':[(.018,.46),(-.055,.49),(-.075,.53),(-.018,.58),(.018,.61),(-.018,.65),(-.075,.70),(-.055,.75),(.018,.77)],
      'b':[(.012,.45),(-.075,.45),(-.075,.55),(.005,.55),(.005,.66),(-.075,.66),(-.075,.77),(.012,.77)],
      'thin':[(.008,.48),(-.045,.48),(-.045,.56),(.005,.56),(.005,.65),(-.045,.65),(-.045,.73),(.008,.73)]}
    yz=np.array(sections[family]);x0,x1=(-1.5,1.5) if part=='run' else (-3.,0.) if part=='start' else (0.,3.)
    def height(x,z):
        if part=='run':return z
        f=(x-x0)/3 if part=='start' else (x1-x)/3
        return z*(.12+.88*f)
    if not detail:yz=yz[[0,-1]]
    # Both sides are physical surfaces; a 3mm sheet has no huge backing cuboid.
    for shift,reverse in ((0.,False),(.003,True)):
        for (y,z),(yy,zz) in zip(yz[:-1],yz[1:]):
            points=[(x0,y+shift,height(x0,z)),(x1,y+shift,height(x1,z)),
                    (x1,yy+shift,height(x1,zz)),(x0,yy+shift,height(x0,zz))]
            m.face(STEEL,list(reversed(points)) if reverse else points)
    if part=='run':
        # Post and spacer behind the traffic-facing sheet.
        m.box(STEEL,(0,.08,.40),(.035,.035,.55))
        if detail:
            m.box(STEEL,(0,.052,.62),(.07,.027,.035))
            for z in (.53,.70):m.box(DARK,(0,-.08,z),(.012,.005,.012))
        if colour!='none':
            cols=colour.split('_');middle={'a':.615,'b':.610,'thin':.605}[family]
            # Flush in the central trough, below both corrugation crests.
            m.box(DARK,(0,.003,middle),(.068,.019,.027))
            for j,c in enumerate(cols):
                if c not in COLOURS:continue
                x=(j-(len(cols)-1)/2)*.059
                m.box('bne_eur_'+c,(x,-.021,middle),(.025 if len(cols)>1 else .059,.005,.021))
    return m


def collision(part):
    m=Mesh(4)
    if part=='run':m.box(STEEL,(0,0,.46),(1.5,.065,.46))
    else:
        # A sloping rigid terminal, not a deformable vehicle restraint simulation.
        x0,x1=(-3.,0.) if part=='start' else (0.,3.)
        z0,z1=(.10,.85) if part=='start' else (.85,.10)
        p=[(x0,-.08,0),(x1,-.08,0),(x1,-.08,z1),(x0,-.08,z0)]
        m.face(STEEL,p);m.face(STEEL,[(x,.08,z) for x,y,z in reversed(p)])
        m.face(STEEL,[(x0,-.08,z0),(x1,-.08,z1),(x1,.08,z1),(x0,.08,z0)])
        m.face(STEEL,[(x0,.08,0),(x1,.08,0),(x1,-.08,0),(x0,-.08,0)])
        for x,z in ((x0,z0),(x1,z1)):m.face(STEEL,[(x,-.08,0),(x,.08,0),(x,.08,z),(x,-.08,z)])
    return m


def sos_model(detail=True):
    import signmesh
    import frroadmesh
    source=signmesh.Mesh();frroadmesh.sos(source,(0,0,0),(0,1));m=Mesh(4)
    for triangle,material in zip(source.t,source.tm):
        if not detail and material in (signmesh.GLYPH,signmesh.GLYPH_WHITE):continue
        m.face(source.materials[material],[source.p[i]for i in triangle],[source.uv[i]for i in triangle])
    return m


def sos_collision():
    m=Mesh(4);m.box(STEEL,(0,0,.90),(.18,.15,.90));return m


def street_post(detail=True):
    m=Mesh(8 if detail else 5);m.cylinder(STEEL,0,0,0,2.49,.024);return m


def j14(small=False,detail=True):
    """Original semicircular J14a shell and conforming white arrows, metres."""
    import signmesh as sm
    m=Mesh(8);_,radius,peak=J14_MODELS[int(small)]
    def polygon(material,points):
        clean=[]
        for p in points:
            if not clean or np.linalg.norm(np.array(p)-clean[-1])>1e-8:clean.append(np.array(p))
        if len(clean)>1 and np.linalg.norm(clean[0]-clean[-1])<1e-8:clean.pop()
        m.face(material,clean)
    top=lambda x:peak-(.54 if small else .78)*abs(x)/radius
    angles=np.linspace(math.pi,2*math.pi,25 if detail else 13)
    for a,b in zip(angles[:-1],angles[1:]):
        x,y=radius*math.cos(a),radius*math.sin(a)
        xx,yy=radius*math.cos(b),radius*math.sin(b)
        m.face(J14_GREEN,[(x,y,0),(xx,yy,0),(xx,yy,top(xx)),(x,y,top(x))])
        polygon(J14_GREEN,[(x,y,top(x)),(xx,yy,top(xx)),(xx,0,top(xx)),(x,0,top(x))])
    # A flat rear and base close the shell.
    m.face(J14_GREEN,[(radius,0,0),(-radius,0,0),(-radius,0,top(-radius)),(0,0,peak),(radius,0,top(radius))])
    m.face(J14_GREEN,[(radius*math.cos(a),radius*math.sin(a),0)for a in angles[::-1]])
    for side in (-1,1):
        for a,b in zip(np.linspace(.13,.87,13 if detail else 7)[:-1],np.linspace(.13,.87,13 if detail else 7)[1:]):
            points=[]
            for x,high in ((a,False),(b,False),(b,True),(a,True)):
                f=(x-.13)/(.87-.13)
                z=((.29 if not high else peak-.25)*(1-f)+(.78 if not small else .66)*f)
                px=x*radius*side;py=-radius*math.sqrt(1-x*x)-.007
                points.append((px,py,z))
            if side<0:points.reverse()
            polygon(sm.MATERIALS[sm.WHITE],points)
    return m


def write_visual(path,name,lods):
    write_dae(str(path),name,lods,Mesh(4))
    ns='{http://www.collada.org/2005/11/COLLADASchema}';ET.register_namespace('',ns[1:-1])
    tree=ET.parse(path)
    for parent in tree.iter():
        for child in list(parent):
            ident=child.get('id','')
            if ident.startswith('Colmesh_')or ident=='collision-1'or ident.endswith('-col-g'):parent.remove(child)
    tree.write(path,encoding='utf-8',xml_declaration=True)


def main():
    OUT.mkdir(parents=True,exist_ok=True);materials={}
    for name,col in {STEEL:(.46,.49,.51),DARK:(.05,.055,.06),J14_GREEN:(.018,.31,.19),**{'bne_eur_'+k:v for k,v in COLOURS.items()}}.items():
        materials[name]={'name':name,'mapTo':name,'class':'Material','version':1.5,
          'persistentId':str(uuid.uuid5(uuid.NAMESPACE_URL,'beamng-earth/'+name)),
          'Stages':[{'baseColorFactor':[*col,1.],'metallicFactor':.75 if name==STEEL else 0.,'roughnessFactor':.45,
                     **({'retroreflectivity':.8}if name not in {STEEL,DARK}else {})}, {},{},{}]}
    (OUT/'main.materials.json').write_text(json.dumps(materials,indent=2)+'\n')
    entries=[]
    for family in eu.FAMILIES:
        for colour in eu.REFLECTORS:
            for part in ('run','start','end'):
                name='guardrail_eu_'+family+'_'+colour+'_'+part
                write_dae(str(OUT/('bne_'+name+'.dae')),name,
                          [(40,module(family,colour,part)),(4,module(family,colour,part,False))],collision(part))
                entries.append(f"  {name} = {{ '{VFS}/bne_{name}', .9, 1600 }},")
    name='fr_sos_phone'
    write_dae(str(OUT/('bne_'+name+'.dae')),name,[(40,sos_model()),(4,sos_model(False))],sos_collision())
    entries.append(f"  {name} = {{ '{VFS}/bne_{name}', 1.825, 800 }},")
    write_visual(OUT/'bne_fr_street_post.dae','fr_street_post',[(40,street_post()),(4,street_post(False))])
    entries.append(f"  fr_street_post = {{ '{VFS}/bne_fr_street_post', 2.49, 500, true }},")
    for index,(name,_,height) in enumerate(J14_MODELS):
        write_visual(OUT/('bne_'+name+'.dae'),name,[(40,j14(bool(index))), (4,j14(bool(index),False))])
        entries.append(f"  {name} = {{ '{VFS}/bne_{name}', {height}, 1000, true }},")
    lua=ROOT/'mod/beamng_earth/lua/ge/extensions/beamngEarth/europeRoadAssets.lua'
    lua.write_text('-- Generated by tools/build_europe_guardrails.py\nreturn {\n'+'\n'.join(entries)+'\n}\n')
    print('63 steel rails, SOS, non-colliding street post and two J14a models written')


if __name__=='__main__':main()
