"""Render the actual travel-board mesh head-on for offline visual review."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import travelboards as boards
import recreationrules


def preview(out,states=None):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    signs=[]
    states=states or [state for state in boards.registry()['states'] if boards.current(state)]
    for i,state in enumerate(states):
        extra=dict(kind='welcome',state=state)
        info=boards.welcome_info(extra)
        w,h=info['width_m'],info['height_m'];post=.62 if info.get('support')=='timber' else .32
        signs.append(dict(extra=extra,centre=[i*7.,0,0],travel=[0,1],width=w,height=h,bottom=info.get('clearance_m',.8 if info.get('support')=='monument' else 2.2),posts=[-post*w,post*w],bases=[0,0]))
    signs.append(dict(extra=dict(kind='recreation',name='Example State Park',category='STATE PARK'),centre=[14,0,0],travel=[0,1],width=4.5,height=2.2,bottom=2.2,posts=[-1.5,1.5],bases=[0,0]))
    guide=dict(kind='recreation_exit',exit='7',lines=['Custer','State Park'],instruction='NEXT RIGHT')
    signs.append(dict(extra=guide,centre=[21,0,0],travel=[0,1],width=3.66,height=3.4,bottom=2.2,posts=[-1.17,1.17],bases=[0,0]))
    for index,(name,state) in enumerate((('Yellowstone National Park','WY'),('Medicine Bow-Routt National Forest','WY'),('Pawnee National Grassland','CO'))):
        extra=dict(recreationrules.entrance(name,{},state),kind='forest_entrance')
        signs.append(dict(extra=extra,centre=[28+index*7.,0,0],travel=[0,1],width=4.8,height=3.1,bottom=.8,posts=[-1.54,1.54],bases=[0,0]))
    for sign in signs[len(states):]:sign['centre'][0]+=len(states)*7.
    faces={};p,t,uv,materials,mi=boards.render(signs,(0,0,0),out,'/preview',faces)
    textures={}
    for name,url in faces.items():textures[name]=np.asarray(Image.open(out/Path(url).name).convert('RGB'))
    atlas=np.asarray(Image.open(ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/roadside/sign_letters_o.data.dds').convert('L'))
    W,H=max(2900,len(signs)*650),850;low=p[:,[0,2]].min(0);high=p[:,[0,2]].max(0)
    scale=min((W-60)/(high[0]-low[0]),(H-100)/(high[1]-low[1]))
    canvas=np.full((H,W,3),(165,184,195),np.uint8)
    screen=np.c_[30+(p[:,0]-low[0])*scale,H-30-(p[:,2]-low[1])*scale]
    colors={'bne_forest_cream':(242,201,97),'bne_forest_timber':(79,51,26),'bne_recreation_brown_opaque':(80,43,19),'bne_exit_green':(0,91,51),'bne_exit_white':(255,255,255),'bne_exit_metal':(105,110,114),'bne_exit_blue':(15,63,126),'bne_guide_red':(178,21,32),'bne_guide_black':(0,0,0)}
    for tri_index in np.argsort(-p[t][:,:,1].mean(1)):
        tri=t[tri_index];v=p[tri]
        if np.cross(v[1]-v[0],v[2]-v[0])[1]>=-1e-9:continue
        xy=screen[tri];x0,y0=np.maximum(np.floor(xy.min(0)).astype(int),0);x1,y1=np.minimum(np.ceil(xy.max(0)).astype(int),[W-1,H-1])
        if x1<x0 or y1<y0:continue
        matrix=np.c_[xy,np.ones(3)]
        if abs(np.linalg.det(matrix))<1e-8:continue
        yy,xx=np.mgrid[y0:y1+1,x0:x1+1];bary=np.c_[xx.ravel()+.5,yy.ravel()+.5,np.ones(xx.size)]@np.linalg.inv(matrix)
        mask=np.all(bary>=-1e-7,axis=1);tex_uv=bary@uv[tri]
        name=materials[int(mi[tri_index])];region=canvas[y0:y1+1,x0:x1+1].reshape(-1,3).copy()
        if name in textures or name.startswith('bne_forest_letters') or name in ('bne_guide_letters','bne_guide_letters_black','bne_exit_letters'):
            font=name not in textures;tex=atlas if font else textures[name]
            sx=np.clip((tex_uv[:,0]*(tex.shape[1]-1)).astype(int),0,tex.shape[1]-1)
            sy=np.clip(((1-tex_uv[:,1])*(tex.shape[0]-1)).astype(int),0,tex.shape[0]-1)
            if font:
                a=(tex[sy,sx]/255.)[:,None];color=np.array([51,19,6]) if name=='bne_forest_letters_brown' else (np.array([252,222,140]) if name=='bne_forest_letters_cream' else (0 if name.endswith('black') else 255))
                region[mask]=(region[mask]*(1-a[mask])+color*a[mask]).astype(np.uint8)
            else:region[mask]=tex[sy[mask],sx[mask]]
        else:region[mask]=colors.get(name,(120,120,120))
        canvas[y0:y1+1,x0:x1+1]=region.reshape(y1-y0+1,x1-x0+1,3)
    image=Image.fromarray(canvas);draw=ImageDraw.Draw(image)
    draw.text((25,12),'Offline preview of generated meshes - not an in-game screenshot',fill='black')
    image.save(out/'travelboards.png')
    return out/'travelboards.png'

if __name__=='__main__':print(preview(sys.argv[1] if len(sys.argv)>1 else ROOT/'probes/travelboards-preview',sys.argv[2].split(',') if len(sys.argv)>2 else None))
