"""Rasterize the actual roadside triangles, materials and glyph atlas for visual QA."""
from pathlib import Path
import json
import sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import roadside


def render(objects,width,height,scale,camera=(0.,-1.,0.)):
    p,t,uv,materials,mi=roadside.build(objects,(0.,0.,0.))
    camera=np.asarray(camera)
    normal=np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]])
    keep=np.flatnonzero(normal@camera>1e-10)
    screen=np.c_[width/2+p[:,0]*scale,height-25-p[:,2]*scale]
    canvas=np.full((height,width,3),(228,234,235),np.uint8)
    depth=np.full((height,width),-np.inf)
    directory=ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/roadside'
    definitions=json.loads((directory/'exit.materials.json').read_text())
    atlases={}
    for index in keep:
        tri=t[index];pts=screen[tri]
        lo=np.maximum(np.floor(pts.min(0)).astype(int),[0,0])
        hi=np.minimum(np.ceil(pts.max(0)).astype(int),[width-1,height-1])
        if (hi<lo).any():continue
        x,y=np.meshgrid(np.arange(lo[0],hi[0]+1)+.5,np.arange(lo[1],hi[1]+1)+.5)
        a,b,c=pts
        det=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
        if abs(det)<1e-10:continue
        u=((b[1]-c[1])*(x-c[0])+(c[0]-b[0])*(y-c[1]))/det
        v=((c[1]-a[1])*(x-c[0])+(a[0]-c[0])*(y-c[1]))/det
        weights=np.stack([u,v,1-u-v],axis=-1)
        z=weights@(p[tri]@camera)
        region=np.s_[lo[1]:hi[1]+1,lo[0]:hi[0]+1]
        active=(weights>=-1e-6).all(2)&(z>=depth[region]-1e-8)
        stage=definitions[materials[mi[index]]]['Stages'][0]
        col=np.array(stage.get('baseColorFactor',[.6,.63,.62,1])[:3])*255
        alpha=np.ones(active.shape)
        if 'opacityMap' in stage:
            name=Path(stage['opacityMap']).name
            if name not in atlases:atlases[name]=np.array(Image.open(directory/name).convert('L'))
            atlas=atlases[name];tex=weights@uv[tri]
            tx=np.clip((tex[:,:,0]*(atlas.shape[1]-1)).astype(int),0,atlas.shape[1]-1)
            ty=np.clip(((1-tex[:,:,1])*(atlas.shape[0]-1)).astype(int),0,atlas.shape[0]-1)
            alpha=atlas[ty,tx]/255.
        patch=canvas[region]
        patch[active]=(patch[active]*(1-alpha[active,None])+col*alpha[active,None]).astype(np.uint8)
        depth[region][active&(alpha>.5)]=z[active&(alpha>.5)]
    return Image.fromarray(canvas)


def main():
    base=dict(centre=[0.,0.,0.],travel=[0.,1.],bottom=1.75,posts=[-.34,.34],bases=[0.,0.])
    speed=dict(base,type='speed',width=1.05,height=2.65,speed=70,truck=65)
    shield=dict(base,type='shield',width=1.05,height=1.55,posts=[0.],bases=[0.],routes=[dict(kind='interstate',number='69',direction='NORTH')])
    us=dict(shield,routes=[dict(kind='us',number='127',direction='NORTH')])
    adopt=dict(base,type='adopt',width=1.65,height=2.35,posts=[-.52,.52])
    high=dict(type='lamp',centre=[0,0,0],travel=[0,1],height=35.,high=True,count=8)
    lamp=dict(high,centre=[-4,0,0],height=9.,high=False,count=1)
    canvas=Image.new('RGB',(1700,950),'#e4eaeb');draw=ImageDraw.Draw(canvas)
    font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',22)
    small=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',18)
    draw.text((30,20),'GENERATED ROADSIDE OBJECTS',fill='#172c3b',font=font)
    for x,w,label,rows,scale in ((25,240,'Speed + trucks',[speed],175),(290,240,'Interstate',[shield],175),
                               (555,240,'US highway',[us],175),(820,330,'Michigan adoption',[adopt],175),
                               (1180,490,'Mapped lamps (different scale)',[lamp,high],22)):
        canvas.paste(render(rows,w,820,scale),(x,65))
        draw.text((x,885),label,fill='#172c3b',font=small)
    draw.text((30,925),'Actual generated mesh and lettering. In-game lighting may differ.',fill='#445966',font=small)
    output=ROOT/'build/roadside-v6-preview.png';canvas.save(output);print(output)


if __name__=='__main__':main()
