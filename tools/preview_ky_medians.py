"""Orthographic QA renders of the actual generated sign mesh and lettering atlas."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server'))
import milemarkers


def render(sign, back=False):
    mesh = milemarkers.build([sign], (0.,0.,0.))
    p,t,uv,materials,mi = mesh
    camera = np.array([0., 1. if back else -1., 0.])
    normals = np.cross(p[t[:,1]]-p[t[:,0]], p[t[:,2]]-p[t[:,0]])
    keep = np.flatnonzero(normals@camera > 1e-10)
    scale = 260.
    width,height = 250,870
    projected = np.c_[width/2+p[:,0]*scale*(-1 if back else 1), height-25-p[:,2]*scale]
    canvas = np.full((height,width,3), (228,234,235), np.uint8)
    depth = np.full((height,width), -np.inf)
    atlas = np.array(Image.open(ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/roadside/exit_letters_o.data.png'))
    colours = {'bne_exit_white':(245,245,235), 'bne_exit_metal':(151,157,155),
               'bne_exit_letters':(255,255,250), 'bne_mile_blue':(17,62,170),
               'bne_mile_red':(190,31,41), 'bne_mile_green':(15,125,89)}
    for index in keep:
        tri = t[index]
        pts = projected[tri]
        low = np.maximum(np.floor(pts.min(0)).astype(int), [0,0])
        high = np.minimum(np.ceil(pts.max(0)).astype(int), [width-1,height-1])
        if (high<low).any(): continue
        x,y = np.meshgrid(np.arange(low[0],high[0]+1)+.5, np.arange(low[1],high[1]+1)+.5)
        a,b,c = pts
        det = (b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
        if abs(det) < 1e-10: continue
        w0 = ((b[1]-c[1])*(x-c[0])+(c[0]-b[0])*(y-c[1]))/det
        w1 = ((c[1]-a[1])*(x-c[0])+(a[0]-c[0])*(y-c[1]))/det
        weights = np.stack([w0,w1,1-w0-w1],axis=-1)
        z = weights@(p[tri]@camera)
        region = np.s_[low[1]:high[1]+1,low[0]:high[0]+1]
        active = (weights >= -1e-6).all(2) & (z >= depth[region]-1e-8)
        name = materials[mi[index]]
        alpha = np.ones(active.shape)
        if name == 'bne_exit_letters':
            tex = weights@uv[tri]
            u = np.clip((tex[:,:,0]*(atlas.shape[1]-1)).astype(int),0,atlas.shape[1]-1)
            v = np.clip(((1.-tex[:,:,1])*(atlas.shape[0]-1)).astype(int),0,atlas.shape[0]-1)
            alpha = atlas[v,u]/255.
        col = colours.get(name,(160,166,164))
        patch = canvas[region]
        patch[active] = (patch[active]*(1-alpha[active,None])+np.array(col)*alpha[active,None]).astype(np.uint8)
        depth[region][active & (alpha>.5)] = z[active & (alpha>.5)]
    return Image.fromarray(canvas)


def main():
    output = ROOT/'build/ky-medians-preview.png'
    sign = dict(centre=[0.,0.,0.], travel=[0.,1.], bottom=.55, width=.61,height=2.44,
                route=65,mile=46.,direction='NORTH',style='ky_blue',double_sided=True)
    canvas = Image.new('RGB',(920,1000),'#e4eaeb')
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf',26)
    small = ImageFont.truetype('C:/Windows/Fonts/arial.ttf',18)
    draw.text((35,22),'KENTUCKY MEDIAN MARKERS',fill='#172c3b',font=font)
    for i,(route,back) in enumerate(((65,False),(65,True),(75,False))):
        panel = render(dict(sign,route=route,mile=46. if route==65 else 75.2),back)
        x = 35+i*300
        canvas.paste(panel,(x,65))
        draw.text((x+35,925),('SOUTHBOUND' if back else 'NORTHBOUND'),fill='#172c3b',font=small)
    draw.text((35,968),'Actual mesh + font atlas preview. In-game lighting and textures may differ.',fill='#445966',font=small)
    output.parent.mkdir(exist_ok=True)
    canvas.save(output)
    print(output)


if __name__ == '__main__':
    main()
