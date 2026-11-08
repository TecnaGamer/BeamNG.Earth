"""Rasterize pinned, licensed blank SVGs and derive optional physical cutouts.

Requires resvg-py only for this offline build. Does not fetch any URLs.
The catalog records source SHA-256, license and a reviewed number layout.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageFilter

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))


def simplify(points,tolerance):
    if len(points)<=2:return points
    delta=points[-1]-points[0]
    t=np.clip((points-points[0])@delta/max(float(delta@delta),1e-12),0.,1.)
    distances=np.linalg.norm(points-(points[0]+t[:,None]*delta),axis=1)
    i=int(distances.argmax())
    if distances[i]<=tolerance:return points[[0,-1]]
    return np.vstack([simplify(points[:i+1],tolerance)[:-1],simplify(points[i:],tolerance)])


def contour(image):
    mask=np.asarray(image.getchannel('A'))>=128
    if mask.mean()>.94:return None
    height,width=mask.shape
    edges={}
    def edge(a,b):edges.setdefault(a,[]).append(b)
    for y,x in np.argwhere(mask):
        x,y=int(x),int(y)
        if y==0 or not mask[y-1,x]:edge((x,y),(x+1,y))
        if x==width-1 or not mask[y,x+1]:edge((x+1,y),(x+1,y+1))
        if y==height-1 or not mask[y+1,x]:edge((x+1,y+1),(x,y+1))
        if x==0 or not mask[y,x-1]:edge((x,y+1),(x,y))
    rings=[]
    while edges:
        first=next(iter(edges));point=first;ring=[]
        while point in edges:
            ring.append(point);values=edges[point];next_point=values.pop()
            if not values:del edges[point]
            point=next_point
            if point==first:
                if len(ring)>3:rings.append(np.asarray(ring,float))
                break
    if not rings:raise ValueError('SVG has no closed physical contour')
    area=lambda r:abs(float(np.sum(r[:,0]*np.roll(r[:,1],-1)-r[:,1]*np.roll(r[:,0],-1))))
    ring=max(rings,key=area)
    opposite=int(np.linalg.norm(ring-ring[0],axis=1).argmax())
    ring=np.vstack([simplify(ring[:opposite+1],1.2)[:-1],
                    simplify(np.vstack([ring[opposite:],ring[:1]]),1.2)[:-1]])
    ring=ring/[width,height]*[1.,-1.]+[-.5,1.]
    signed=np.sum(ring[:,0]*np.roll(ring[:,1],-1)-ring[:,1]*np.roll(ring[:,0],-1))
    if signed<0:ring=ring[::-1]
    from buildings import triangulate
    triangles=triangulate(ring)
    if len(triangles)!=len(ring)-2:raise ValueError('Invalid cutout contour')
    return np.round(ring,6).tolist()


def build(catalog, source_dir, data_dir):
    import resvg_py
    data_dir=Path(data_dir);source_dir=Path(source_dir)
    (data_dir/'art').mkdir(exist_ok=True);(data_dir/'svg').mkdir(exist_ok=True)
    registry=json.loads((data_dir/'networks.json').read_text(encoding='utf-8'))
    for key,spec in catalog['artwork'].items():
        if spec['license'] not in ('Public domain','CC BY-SA 4.0'):
            raise ValueError('Unapproved artwork license: '+key)
        source=source_dir/spec['file']
        raw=source.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=spec['sha256']:
            raise ValueError('Artwork checksum mismatch: '+key)
        if source.suffix=='.svg':
            root=ET.fromstring(raw)
            for node in root.iter():
                for attr,value in node.attrib.items():
                    if attr.split('}')[-1]=='href' and not value.startswith(('#','data:')):
                        raise ValueError('External SVG resource: '+key)
            viewbox=list(map(float,root.get('viewBox','').replace(',',' ').split()))
            if len(viewbox)==4:aspect=viewbox[2]/viewbox[3]
            else:
                import re
                dimension=lambda field:float(re.match(r'[0-9.]+',root.get(field))[0])
                aspect=dimension('width')/dimension('height')
            png=resvg_py.svg_to_bytes(svg_string=raw.decode('utf-8-sig'),width=512,height=512,
                    skip_system_fonts=True,font_files=[str(ROOT/'tools/fonts/RG2014EEM.ttf')])
            shutil.copyfile(source,data_dir/'svg'/f'{key}.svg')
        else:
            png=raw
            with Image.open(io.BytesIO(png)) as original:aspect=original.width/original.height
        with Image.open(io.BytesIO(png)) as picture:
            picture=picture.convert('RGBA').resize((512,512),Image.Resampling.LANCZOS)
            # Resvg preserves aspect when only one dimension is specified, but
            # this unit texture must stretch back onto the configured board.
            bbox=picture.getbbox()
            if not bbox:raise ValueError('Empty artwork: '+key)
            cutout=picture
            if spec.get('cutout_nonblack'):
                mask=np.max(np.asarray(picture)[:,:,:3],axis=2)>32
                alpha=Image.fromarray((mask*255).astype(np.uint8)).filter(ImageFilter.MaxFilter(7))
                cutout=picture.copy();cutout.putalpha(alpha)
            profile=contour(cutout)
            picture.save(data_dir/'art'/f'{key}.png')
        template=dict(image=f'art/{key}.png',number_box=spec['number_box'],aspect=aspect,
                      text_colour=spec.get('text_colour',[0,0,0]),verified=spec.get('reviewed',False),
                      number_fit='condense',modifiers=spec.get('modifiers',[]),source=spec['description_url'])
        template['license']=spec['license']
        if spec.get('modifier_plaque') is False:template['modifier_plaque']=False
        if profile:template['contour']=profile
        registry['templates'][key]=template
    for net,config in catalog['networks'].items():
        registry['networks'][net].update(config)
    (data_dir/'networks.json').write_text(json.dumps(registry,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('catalog',type=Path);ap.add_argument('source_dir',type=Path)
    args=ap.parse_args()
    build(json.loads(args.catalog.read_text(encoding='utf-8')),args.source_dir,ROOT/'server/data/shields')
