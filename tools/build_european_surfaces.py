"""Original, repeatable neutral sidewalk textures; no pixels from reference photos.

Dark asphalt footway, segmented dark grey kerb and pale tactile surface. These
are a generic scenery palette, not a national construction or accessibility spec.
"""
from pathlib import Path
import json
import uuid
import numpy as np
from PIL import Image

OUT=Path(__file__).resolve().parents[1]/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/eu_surfaces'
VFS='/levels/beamng_earth/art/shapes/beamngEarth/eu_surfaces'


def build(out=OUT):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    rng=np.random.default_rng(1711029);n=512
    grain=rng.normal(0,6,(n,n));large=rng.normal(0,2,(64,64)).repeat(8,0).repeat(8,1)
    # sRGB albedo, deliberately below the road painter's neutral (95,95,94).
    # Do not compensate for sun/shade in the reference photos by baking lighting.
    asphalt=np.clip(65+grain+large,0,255)
    Image.fromarray(np.stack((asphalt,asphalt-1,asphalt-2),axis=2).astype(np.uint8)).save(out/'footway.color.png')
    curb=np.clip(55+rng.normal(0,4,(n,n)),0,255)
    curb[:,:3]*=.55;curb[:,-2:]*=.75
    # Same eight trim-sheet bands as the stock profile's UVs, all unpainted.
    for band in range(8):curb[band*64+40:(band+1)*64]*=.83
    Image.fromarray(np.repeat(curb[:,:,None],3,axis=2).astype(np.uint8)).save(out/'kerb.color.png')
    y,x=np.mgrid[:n,:n];dx=(x%64-32)/17;dy=(y%64-32)/17;r=np.sqrt(dx*dx+dy*dy)
    bumps=np.where(r<1,16*(1-r)-7*dy,0)
    tactile=np.clip(199+bumps+rng.normal(0,1,(n,n)),0,255).astype(np.uint8)
    Image.fromarray(np.stack((tactile,tactile,np.clip(tactile.astype(int)-7,0,255).astype(np.uint8)),2)).save(out/'tactile.color.png')
    materials={}
    # BeamNG's .color.png suffix identifies sRGB base-colour textures for cooking.
    # New paths also avoid reusing the previous trial's cooked pale textures.
    for name,texture in (('bne_eu_curb','kerb.color.png'),('bne_eu_asphalt_walk','footway.color.png'),('bne_eu_tactile','tactile.color.png')):
        materials[name]={'name':name,'mapTo':name,'class':'Material','version':1.5,
          'persistentId':str(uuid.uuid5(uuid.NAMESPACE_URL,'beamng-earth:'+name)),
          'Stages':[{'baseColorMap':VFS+'/'+texture,'baseColorFactor':[1,1,1,1],
                     'metallicFactor':0,'roughnessFactor':.98}], 'doubleSided':False}
    (out/'main.materials.json').write_text(json.dumps(materials,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':build()
