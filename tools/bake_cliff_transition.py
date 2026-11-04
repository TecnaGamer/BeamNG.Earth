"""Bake shared opaque cliff transition materials and their mean-colour references.

Run after rebaking a source cliff material. This is a numerical lookup mask, not
an art texture: all four families reuse the same 16x16 ramp on UV1.
"""
import copy
import json
from pathlib import Path
import numpy as np
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
ART=ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth'
NAMES=('bne_cliff_strata','bne_cliff_roadcut','bne_cliff_roadcut_arid','bne_cliff_canyon_sat')

def linear(v):
    return np.where(v<=.04045,v/12.92,((v+.055)/1.055)**2.4)

def bake():
    path=ART/'cliff.materials.json'
    mats=json.loads(path.read_text())
    mask='/levels/beamng_earth/art/shapes/beamngEarth/cliff/terrain_rim.data.png'
    Image.fromarray(np.tile(np.rint(np.linspace(0,255,16)).astype(np.uint8),(16,1))).save(ART/'cliff/terrain_rim.data.png')
    means=[]
    for name in NAMES:
        mat=copy.deepcopy(mats[name]);stage=mat['Stages'][0]
        with Image.open(ART/'cliff'/Path(stage['baseColorMap']).name) as im:
            mean=linear(np.asarray(im.convert('RGB'),float)/255).mean(axis=(0,1))
        means.append((mean*linear(np.array(stage['baseColorFactor'][:3]))).tolist())
        mat['name']=mat['mapTo']=name+'_terrain'
        stage['normalMapUseUV']=0
        mat['Stages'][1]=dict(baseColorFactor=[1,1,1,1],vertColor=True,metallicFactor=0,
            roughnessFactor=.94,opacityFactor=1,opacityMap=mask,opacityMapUseUV=1)
        mat['activeLayers']=2
        mat['translucent']=False
        mats[mat['name']]=mat
    path.write_text(json.dumps(mats,indent=2)+'\n')
    (ROOT/'server/cliff_transition_means.json').write_text(json.dumps(means,indent=2)+'\n')

if __name__=='__main__':bake()
