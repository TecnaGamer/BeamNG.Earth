"""Content-addressed shield DDS faces, created only in the optional sign worker.

The existing dirs_faces protocol registers these materials before loading meshes.
No SVG renderer, installed fonts, network access or per-frame work at runtime.
"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import highwayroutes

DATA = highwayroutes.DATA
VERSION = 2


@lru_cache(maxsize=1)
def lettering():
    return (json.loads((DATA/'lettering.json').read_text(encoding='utf-8')),
            Image.open(DATA/'lettering.png').copy())


def text(image, value, box, colour=(38,13,13), condense=False):
    font, atlas = lettering()
    glyphs = [font['glyphs'][c] if c in font['glyphs'] else dict(advance=font['cap']*.4,box=None)
              for c in str(value).upper() if c in font['glyphs'] or c==' ']
    if not glyphs:
        return
    x0,y0,x1,y1 = box
    height_scale=(y1-y0)/font['cap']
    scale = min(height_scale, (x1-x0)/sum(g['advance'] for g in glyphs))
    yscale=height_scale if condense else scale
    x = (x0+x1-sum(g['advance'] for g in glyphs)*scale)/2
    y0=(y0+y1-font['cap']*yscale)/2
    for g in glyphs:
        if g['box'] is None:
            x+=g['advance']*scale
            continue
        mask = atlas.crop(g['box'])
        mask = mask.resize((max(1,round(mask.width*scale)),max(1,round(mask.height*yscale))),Image.Resampling.LANCZOS)
        offset=font['cap']+g['bbox'][1] if 'bbox' in g else 0
        image.paste(colour,(round(x),round(y0+offset*yscale)),mask)
        x += g['advance']*scale


@lru_cache(maxsize=4)
def _blank(path, aspect, background):
    """Reuse decoded/resized templates across numbers; callers draw on a copy."""
    size=(round(512*max(1.,aspect)),round(512*max(1.,1/aspect)))
    with Image.open(path) as raw:
        source=raw.convert('RGBA').resize(size,Image.Resampling.LANCZOS)
        image=Image.new('RGB',source.size,background)
        image.paste(source,mask=source.getchannel('A'))
    return image


@lru_cache(maxsize=64)
def raster(network, ref, variant=''):
    """Bounded cache by network, number, design variant. Return immutable RGB."""
    key=highwayroutes.template_for(network,ref,variant)
    template=highwayroutes.registry()['templates'].get(key,{})
    image=None
    if template.get('image') and (not variant or variant in template.get('modifiers',())):
        try:
            image=_blank(DATA/template['image'],template.get('aspect',1.),
                         tuple(template.get('background',[255,255,255]))).copy()
            size=image.size
            box=template['number_box']
            box=[v*size[i%2]/512 for i,v in enumerate(box)]
            text(image,ref,box,tuple(template.get('text_colour',[0,0,0])),template.get('number_fit')=='condense')
            image=image.resize((512,512),Image.Resampling.LANCZOS)
        except (OSError,ValueError,KeyError):
            image=None
    if image is None:
        # Explicitly labelled generic board: never another state's silhouette.
        image=Image.new('RGB',(512,512),'white')
        ImageDraw.Draw(image).rectangle((6,6,505,505),outline='black',width=10)
        text(image,network.replace(':',' '),(30,45,482,120))
        text(image,ref,(40,180,472,365))
    return image.tobytes()


@lru_cache(maxsize=64)
def contour(template_name):
    """Optional normalized, concave physical cutout; add state shapes via data."""
    template=highwayroutes.registry()['templates'].get(template_name,{})
    raw=template.get('contour')
    if not raw or not (DATA/template.get('image','')).is_file():
        return None
    from buildings import triangulate
    points=np.asarray(raw,dtype=float)
    if points.ndim!=2 or points.shape[1]!=2 or len(points)<3 or not np.isfinite(points).all():
        return None
    area=np.sum(points[:,0]*np.roll(points[:,1],-1)-points[:,1]*np.roll(points[:,0],-1))
    if area<0: points=points[::-1].copy()
    triangles=triangulate(points)
    return (points,triangles) if len(triangles)==len(points)-2 else None


@lru_cache(maxsize=1)
def signature():
    names={'networks.json','lettering.png','lettering.json'}
    names.update(t['image'] for t in highwayroutes.registry()['templates'].values() if 'image' in t)
    return hashlib.sha256(b''.join((DATA/p).read_bytes() if (DATA/p).is_file() else
                                  ('missing:'+p).encode() for p in sorted(names))).hexdigest()[:16]


def face(route, out_dir, vfs_dir):
    net = route['network']; ref = route['number']; variant = route.get('modifier','')
    digest=hashlib.sha256(json.dumps([VERSION,signature(),net,ref,variant]).encode()).hexdigest()[:20]
    name='bne_route_'+digest
    path=Path(out_dir)/(name+'.dds')
    if not path.exists():
        import imagery
        rgb=np.frombuffer(raster(net,ref,variant),dtype=np.uint8).reshape(512,512,3)
        imagery.write_dds(str(path),rgb)
    return name, vfs_dir+'/'+name+'.dds'
