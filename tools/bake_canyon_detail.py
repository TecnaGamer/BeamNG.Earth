"""Prepare neutral linear shader-detail data from a pinned stock terrain rock surface."""
import hashlib
import json
from pathlib import Path
import cv2
import numpy as np
from PIL import Image

OUT=Path(__file__).resolve().parents[1]/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff'
COLOR='canyon_detail_dirt_rocky_v1.data.png'
NORMAL='canyon_detail_dirt_rocky_v1.normal.dds'
SOURCE='canyon_detail_dirt_rocky_source.color.dds'
AO_SOURCE='canyon_detail_dirt_rocky_source_ao.data.dds'
ROUGHNESS_SOURCE='canyon_detail_dirt_rocky_source_r.data.dds'
AO='canyon_detail_dirt_rocky_v1_ao.data.png'
ROUGHNESS='canyon_detail_dirt_rocky_v1_roughness.data.png'


def detail_pbr(ao, roughness):
    """Encode additive detail: AO only darkens, roughness varies about neutral .5."""
    ao_detail=.5*np.clip(ao,0,1)
    roughness_detail=.5+.5*(roughness-roughness.mean())
    return ao_detail,np.clip(roughness_detail,0,1)


def bake():
    # Stock color is sRGB; detail is linear, neutral-grey additive shader data.
    rgb=np.asarray(Image.open(OUT/SOURCE).convert('RGB'),np.float32)/255
    linear=np.where(rgb<=.04045,rgb/12.92,((rgb+.055)/1.055)**2.4)
    luminance=linear@np.array([.2126,.7152,.0722],np.float32)
    # Keep the filtering footprint proportional to the source resolution.
    sigma=luminance.shape[1]/64
    pad=int(np.ceil(4*sigma))
    low=cv2.GaussianBlur(np.pad(luminance,pad,mode='wrap'),(0,0),sigma)[pad:-pad,pad:-pad]
    detail=(luminance-low)/(low+.1)
    detail-=detail.mean()
    detail*=.065/max(float(detail.std()),1e-6)
    encoded=np.clip(.5+detail,.15,.85)
    encoded+=.5-encoded.mean()
    # RGB is required: an R-only detail image would tint the surface red.
    Image.fromarray(np.uint8(np.rint(encoded*255))).convert('RGB').save(OUT/COLOR)
    ao=np.asarray(Image.open(OUT/AO_SOURCE).convert('L'),np.float32)/255
    roughness=np.asarray(Image.open(OUT/ROUGHNESS_SOURCE).convert('L'),np.float32)/255
    ao_detail,roughness_detail=detail_pbr(ao,roughness)
    # Floor AO to avoid quantizing neutral .5 above the shader's no-op value.
    Image.fromarray(np.uint8(np.floor(ao_detail*255))).convert('RGB').save(OUT/AO)
    Image.fromarray(np.uint8(np.rint(roughness_detail*255))).convert('RGB').save(OUT/ROUGHNESS)
    records=[]
    for name,source in ((SOURCE,'t_dirt_rocky_b.color.dds'),(NORMAL,'t_dirt_rocky_nm.normal.dds'),(AO_SOURCE,'t_dirt_rocky_ao.data.dds'),(ROUGHNESS_SOURCE,'t_dirt_rocky_r.data.dds'),(COLOR,None),(AO,None),(ROUGHNESS,None)):
        record=dict(file=name,sha256=hashlib.sha256((OUT/name).read_bytes()).hexdigest())
        if source:
            record.update(source_archive='terrain.zip',source_path='assets/materials/terrain/rock/t_dirt_rocky/'+source)
        else:
            if name==COLOR:
                record.update(derived_from=SOURCE,method='linear luminance, local normalization, zero-mean detail at 0.5, RGB data')
            elif name==AO:
                record.update(derived_from=AO_SOURCE,method='half of linear stock AO; additive cavities, neutral at 0.5, floor quantization')
            else:
                record.update(derived_from=ROUGHNESS_SOURCE,method='half of mean-centered linear stock roughness plus 0.5')
        records.append(record)
    (OUT/'canyon_detail_dirt_rocky_v1.sources.json').write_text(json.dumps(dict(author='BeamNG GmbH',
        origin='Installed BeamNG.drive stock t_dirt_rocky terrain detail material',files=records,
        notes='Source albedo, normal, AO and roughness copied unchanged. Detail data derived by tools/bake_canyon_detail.py. BeamNG-owned assets and derivatives excluded from the project MIT license.'),indent=2)+'\n')


if __name__=='__main__':
    bake()
