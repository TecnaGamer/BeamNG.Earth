"""Bake the stock rocky-dirt height erosion maps and self-contained strip material."""
import json
import sys
import zipfile
import hashlib
from pathlib import Path
import numpy as np
from PIL import Image
from io import BytesIO
from make_detail_texture import encode
ROOT = Path(__file__).resolve().parents[1]

def main():
    game = Path(sys.argv[1])
    directory = ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth'
    target = directory/'cliff'
    prefix = 'assets/materials/terrain/rock/t_dirt_rocky/t_dirt_rocky'
    with zipfile.ZipFile(game/'content/assets/materials/terrain.zip') as archive:
        height = archive.read(prefix+'_h.png')
        rgb = np.asarray(Image.open(BytesIO(archive.read(prefix+'_b.png'))).convert('RGB'), float)/255
        # Reuse the already bundled terrain assets; never reference a stock level.
        for suffix, name in (('_b.color.dds','source.color.dds'),('_nm.normal.dds','v1.normal.dds'),
                             ('_ao.data.dds','source_ao.data.dds'),('_r.data.dds','source_r.data.dds')):
            path = target/('canyon_detail_dirt_rocky_'+name)
            if not path.exists():
                path.write_bytes(archive.read(prefix+suffix))
    h = np.asarray(Image.open(BytesIO(height)), float)
    if h.ndim == 3:
        h = h[:, :, 0]
    h = .51+.49*(h-h.min())/max(1, h.max()-h.min())
    levels = [h]
    while min(levels[-1].shape) > 1:
        a = levels[-1]
        levels.append((a[::2, ::2]+a[1::2, ::2]+a[::2, 1::2]+a[1::2, 1::2])/4)
    (target/'contact_height.data.dds').write_bytes(encode(levels))
    t = np.linspace(0, 1, 1024)
    lookup = (128/255)*.5/(.505+.52*t)
    (target/'contact_threshold.data.dds').write_bytes(encode([np.tile(lookup, (8, 1))]))
    mean = np.where(rgb <= .04045, rgb/12.92, ((rgb+.055)/1.055)**2.4).mean(axis=(0,1))
    factor = 1.055*(1/mean)**(1/2.4)-.055
    root = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/'
    name = 'bne_cliff_contact_strip'
    stage = dict(baseColorMap=root+'canyon_detail_dirt_rocky_source.color.dds', baseColorFactor=[*factor,1], vertColor=True,
        normalMap=root+'canyon_detail_dirt_rocky_v1.normal.dds', normalMapStrength=.65,
        roughnessMap=root+'canyon_detail_dirt_rocky_source_r.data.dds', roughnessFactor=1, metallicFactor=0,
        ambientOcclusionMap=root+'canyon_detail_dirt_rocky_source_ao.data.dds', ambientOcclusionFactor=.35,
        opacityMap=root+'contact_height.data.dds', opacityFactor=255/128, opacityMapUseUV=0,
        opacityDetailMap=root+'contact_threshold.data.dds', opacityDetailMapUseUV=1,
        detailOpacityMapStrength=1, detailScale=[1,1])
    # Screen-space shadows ignore castShadows and trace opaque scene depth.
    # Use the forward pass without depth writes; BlendOp=None selects opaque again.
    # Scaling opacity by 255/128 and clipping at 1 preserves the old 128/255
    # cutoff. Surviving texels have alpha >= 1 (opaque blend weight), not a fade.
    material = dict(name=name, mapTo=name, **{'class':'Material'}, version=1.5, alphaTest=True, alphaRef=255,
                    translucent=True, translucentBlendOp='LerpAlpha', translucentZWrite=False,
                    translucentRecvShadows=True, doubleSided=True, castShadows=False, Stages=[stage,{},{},{}])
    (directory/'cliff_contact.materials.json').write_text(json.dumps({name:material}, indent=2)+'\n')
    (target/'contact_sources.json').write_text(json.dumps(dict(source=prefix+'_h.png',
        sha256=hashlib.sha256(height).hexdigest(), encoding='H=.51+.49*h; D(t)=(128/255)*.5/(.505+.52*t)',
        notes='Linear coverage maps; source height is embedded in the generated DDS. Albedo and PBR reuse bundled rocky-dirt assets.'),indent=2)+'\n')

if __name__ == '__main__':
    main()
