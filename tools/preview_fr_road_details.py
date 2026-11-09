"""Offline previews of the actual generated triangles and atlas, not game captures."""
from pathlib import Path
import argparse
import copy
import json
import math
import sys
import tempfile
import numpy as np
from PIL import Image,ImageDraw,ImageFont

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'tools')]
import signmesh as sm
import frroadmesh as fr
import euguardrails as eu
import build_europe_guardrails as assets
import preview_town_signs as preview

FONT=ROOT/'tools/fonts/town/DejaVuSans.ttf'


def render(mesh,size,azimuth=0,elevation=0):
    mesh=copy.copy(mesh)
    az,el=map(math.radians,(azimuth,elevation))
    right=np.array([math.cos(az),math.sin(az),0.])
    depth=np.array([-math.sin(az)*math.cos(el),math.cos(az)*math.cos(el),-math.sin(el)])
    up=np.cross(right,depth)
    mesh.p=np.asarray(mesh.p)@np.array([right,depth,up]).T
    original=sm.MATERIALS
    try:
        sm.MATERIALS=list(getattr(mesh,'materials',original));mesh.tm=list(mesh.tm)
        with tempfile.TemporaryDirectory() as d:
            materials=json.loads((preview.ART/'main.materials.json').read_text())
            materials.update(json.loads((assets.OUT/'main.materials.json').read_text()))
            # A fixed preview light makes the different corrugations visible.
            # Geometry, materials and letter masks still come from the assets.
            light=np.array([-.4,-.6,.7]);light/=np.linalg.norm(light);shades={}
            for i,(tri,mat)in enumerate(zip(mesh.t,mesh.tm)):
                name=sm.MATERIALS[mat]
                if name not in {'bne_sign_metal','bne_eur_steel','bne_eur_dark'}:continue
                a,b,c=mesh.p[np.asarray(tri)];normal=np.cross(b-a,c-a);normal/=max(np.linalg.norm(normal),1e-9)
                factor=round(.25+.75*max(0.,float(normal@light)),3);key=(name,factor)
                if key not in shades:
                    newname='preview_shade_'+str(len(shades));shades[key]=len(sm.MATERIALS);sm.MATERIALS.append(newname)
                    colour=materials[name]['Stages'][0]['baseColorFactor']
                    materials[newname]={'Stages':[{'baseColorFactor':[v*factor for v in colour[:3]]+[1.]}]}
                mesh.tm[i]=shades[key]
            (Path(d)/'main.materials.json').write_text(json.dumps(materials))
            return preview.render(mesh,size,Path(d),preview.ART/'sign_font_opacity.data.png')
    finally:sm.MATERIALS=original


def append_model(mesh,model,position=(0,0,0),axes=None):
    if not hasattr(mesh,'materials'):mesh.materials=list(sm.MATERIALS)
    k=len(mesh.p);p=np.asarray(model.p)
    mesh.p.extend(p@(np.eye(3)if axes is None else axes)+position)
    mesh.uv.extend(model.uv)
    for name,triangles in model.tri.items():
        if name not in mesh.materials:mesh.materials.append(name)
        mat=mesh.materials.index(name)
        for t in triangles:mesh.t.append(tuple(k+i for i in t));mesh.tm.append(mat)


def caption(img,title,note):
    out=Image.new('RGB',(img.width,img.height+82),'#eaeff2');out.paste(img,(0,70))
    d=ImageDraw.Draw(out)
    d.text((22,10),title,font=ImageFont.truetype(str(FONT),23),fill='#172d3b')
    d.text((22,42),note,font=ImageFont.truetype(str(FONT),15),fill='#3c5363')
    return out


def main(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    assembly={'centre':np.zeros(3),'travel':np.array([0.,1.]),'bottom':5.5,
              'panels':[{'label':{'names':['MULHOUSE','ST DIÉ-COLMAR','OFFENBURG'],'routes':['A35','E25']},'exit':False},
                        {'label':{'names':['STRASBOURG','ROBERTSAU'],'ref':'51','routes':[]},'exit':True}],
              'poles':[-8.,8.],'bases':[0.,0.],'distance':'500 m','side':1}
    p,t,uv,mats,tm=fr.build_highways([],[assembly],(0,0,0));m=sm.Mesh()
    m.p=list(p);m.t=list(t);m.uv=list(uv);m.tm=list(tm);m.materials=mats
    caption(render(m,(1300,820),-8,2),'Portique français · exemple de composition',
            'Maillage réel hors jeu · garde au-dessus de la chaussée 5,50 m · positions des portiques estimées').save(output/'portique_france.png')
    m=sm.Mesh();fr.direction(m,(0,0,0),(0,1),['TOUTES DIRECTIONS'],'right','M884',colour='green')
    fr.direction(m,(0,0,0),(0,1),['LINGOLSHEIM','OSTWALD'],'right',start_row=1)
    fr.direction(m,(0,0,0),(0,1),["L’illiade"],'right',start_row=3)
    caption(render(m,(1100,600)),'Panneaux de direction · couleurs et noms cartographiés',
            'L1 pour capitales ; L1 blanche et D-DIN mixte sont des substituts L2/L4 · exemple de rendu').save(output/'directions_france.png')
    m=sm.Mesh();fr.street(m,(0,0,0),(0,1),'Rue du Houblon')
    caption(render(m,(900,310)),'Plaque de rue sur poteau',
            'Nom de la route dans les données · emplacement au coin du carrefour estimé').save(output/'plaque_rue_france.png')
    cards=[]
    for family,label in zip(eu.FAMILIES,('Profil A','Profil B','Profil B mince')):
        m=sm.Mesh();append_model(m,assets.module(family,'white'))
        cards.append(caption(render(m,(680,300),-28,14),label,'Modèle original · deux niveaux de détail · collision rigide'))
    img=Image.new('RGB',(680*3,cards[0].height),'white')
    for i,c in enumerate(cards):img.paste(c,(680*i,0))
    img.save(output/'profils_glissieres_europe.png')
    m=sm.Mesh();fr.sos(m,(0,-7,0),(0,-1))
    rows=[[eu.group_index('FR'),float(x),-4.5,0.,180.,0.,.9,0.,-1.,0.,0.,0.,-1.,0.,0.,0.,1.]
          for x in np.arange(-21.,21.01,2.8)]
    phones=[{'point':np.array([0.,-7.,0.]),'travel':np.array([1.,0.]),'normal':np.array([0.,-1.])}]
    rows=eu.sos_access(rows,phones)
    for row in rows:
        part=('run','start','end')[(int(row[0])-9)%3]
        append_model(m,assets.module('a','white',part),np.array(row[1:4]),np.array(row[8:17]).reshape(3,3))
    wide=render(m,(1100,430),165,12)
    close=sm.Mesh();fr.sos(close,(0,0,0),(0,1));zoom=render(close,(300,430),-10,2)
    scene=Image.new('RGB',(1400,430),'#eaeff2');scene.paste(wide,(0,0));scene.paste(zoom,(1100,0))
    caption(scene,'Borne SOS française · renfoncement et passage',
            'La borne conserve sa position cartographiée · la glissière métallique s’adapte localement · exemple synthétique').save(output/'borne_sos_glissiere.png')
    print('5 previews written to',output)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('output',type=Path)
    main(p.parse_args().output)
