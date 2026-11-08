"""Rasterize the actual exported service mesh, material colours and glyph UVs.

Offline QA only: no claim about BeamNG lighting, mipmaps or driving visibility.
"""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server'))
import servicesigns
from servicesigns_index import policy

ART = ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/roadside'


def render(mesh, size=(650,460)):
    points, triangles, uv, names, material_ids = mesh
    width, height = size
    canvas = np.full((height,width,3), 235., dtype=float)
    depth = np.full((height,width), np.inf)
    lo, hi = points[:,[0,2]].min(0), points[:,[0,2]].max(0)
    mid = (lo+hi)/2
    scale = min((width-30)/(hi[0]-lo[0]), (height-30)/(hi[1]-lo[1]))
    screen = np.c_[(points[:,0]-mid[0])*scale+width/2, (mid[1]-points[:,2])*scale+height/2]
    materials = json.loads((ART/'exit.materials.json').read_text())
    textures = {}
    for ids, mat in zip(triangles, material_ids):
        p = points[ids]
        if np.cross(p[1]-p[0],p[2]-p[0])[1] >= -1e-10: continue
        v = screen[ids]
        det = np.linalg.det(np.array([v[1]-v[0], v[2]-v[0]]))
        if abs(det) < 1e-9: continue
        low = np.maximum(np.floor(v.min(0)).astype(int),0)
        high = np.minimum(np.ceil(v.max(0)).astype(int),[width-1,height-1])
        if (high < low).any(): continue
        xx, yy = np.meshgrid(np.arange(low[0],high[0]+1)+.5, np.arange(low[1],high[1]+1)+.5)
        b = ((xx-v[0,0])*(v[2,1]-v[0,1])-(yy-v[0,1])*(v[2,0]-v[0,0]))/det
        c = ((v[1,0]-v[0,0])*(yy-v[0,1])-(v[1,1]-v[0,1])*(xx-v[0,0]))/det
        a = 1-b-c
        z = a*p[0,1]+b*p[1,1]+c*p[2,1]
        dst = canvas[low[1]:high[1]+1,low[0]:high[0]+1]
        db = depth[low[1]:high[1]+1,low[0]:high[0]+1]
        visible = (a>=0)&(b>=0)&(c>=0)&(z<db-1e-10)
        stage = materials[names[mat]]['Stages'][0]
        linear = np.array(stage.get('baseColorFactor',[1,1,1,1])[:3])
        rgb = np.where(linear<=.0031308,12.92*linear,1.055*linear**(1/2.4)-.055)*255
        opacity = stage.get('opacityMap')
        if opacity:
            if opacity not in textures: textures[opacity] = np.array(Image.open(ART/Path(opacity).name))/255.
            texture = textures[opacity]
            u = a*uv[ids[0],0]+b*uv[ids[1],0]+c*uv[ids[2],0]
            vv = a*uv[ids[0],1]+b*uv[ids[1],1]+c*uv[ids[2],1]
            tx = np.clip((u*texture.shape[1]).astype(int),0,texture.shape[1]-1)
            ty = np.clip(((1-vv)*texture.shape[0]).astype(int),0,texture.shape[0]-1)
            visible &= texture[ty,tx] >= 80/255
        dst[visible] = rgb
        db[visible] = z[visible]
    return Image.fromarray(np.clip(canvas,0,255).astype('uint8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('index', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    objects = json.loads(args.index.read_text(encoding='utf-8'))['objects']
    examples = []
    for obj in objects:
        boards = servicesigns.boards(obj['exit'], policy(obj['exit']['service_state']))
        for board in boards:
            key = board['category'] or 'GENERAL'
            if key in [e[0] for e in examples]: continue
            sign = dict(centre=[0,0,0], travel=[0,1], bottom=2.4, bases=[0,0], post_offsets=[-.32,.32],
                        width=board['width'], height=board['height'], label=obj['exit']['label'], board=board)
            examples.append((key, sign))
    image = Image.new('RGB',(650*3,500*((len(examples)+2)//3)), '#ffffff')
    for i,(key,sign) in enumerate(examples):
        tile = render(servicesigns.build([sign],(0,0,0)))
        x, y = (i%3)*650, (i//3)*500
        image.paste(tile,(x,y+30))
        ImageDraw.Draw(image).text((x+20,y+10),f"{key} / EXIT {sign['label']['ref']} / inferred OSM",fill='black')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    image.save(args.output)
    print(args.output)


if __name__ == '__main__': main()
