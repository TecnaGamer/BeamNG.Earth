"""Original, repeatable sidewalk textures from procedural grain and paving patterns."""
from pathlib import Path
import json
import uuid
import numpy as np
from PIL import Image

OUT=Path(__file__).resolve().parents[1]/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/eu_surfaces'
VFS='/levels/beamng_earth/art/shapes/beamngEarth/eu_surfaces'


def _additional(out, n):
    rng = np.random.default_rng(3110526)
    y, x = np.mgrid[:n, :n] / n
    grain = rng.normal(0., 4., (n, n))
    broad = 1.4 * np.sin(2 * np.pi * (2 * x + y)) + 2.2 * np.cos(2 * np.pi * (x - 3 * y))

    def save(name, rgb):
        Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).save(out / f"{name}.color.png")

    def plain(rgb, texture):
        return np.asarray(rgb) + texture[:, :, None]

    def paving(rgb, columns, rows, seam, shift=.5, second=None, irregular=False):
        row, dy = np.divmod(y * rows, 1.)
        jitter = rng.uniform(-.12, .12, rows)[row.astype(int)] if irregular else 0.
        col, dx = np.divmod(x * columns + shift * (row % 2) + jitter, 1.)
        variation = rng.normal(0., 6.5, (rows, columns))[row.astype(int), col.astype(int) % columns]
        # Soften the joints so the repeat remains legible in the distance.
        edge = np.minimum.reduce((dx, 1. - dx, dy, 1. - dy))
        mortar = np.clip((seam + .014 - edge) / .028, 0., 1.)
        tone = plain(rgb, variation + grain * .55 + broad)
        if second is not None:
            tone = np.where((row % 8 < 3)[:, :, None], plain(second, variation + grain * .55 + broad), tone)
        return tone - mortar[:, :, None] * 30.

    save("light_asphalt", plain((112, 110, 105), grain * 1.25 + broad))
    save("concrete", plain((156, 152, 144), grain * .75 + broad))
    save("slabs", paving((146, 144, 137), 6, 6, .018))
    save("pavers", paving((160, 154, 140), 10, 20, .028))
    save("pavers_mixed", paving((148, 146, 137), 10, 24, .028, second=(157, 123, 115)))
    save("stone", paving((183, 177, 157), 24, 26, .044, irregular=True))
    grit = np.where(rng.random((n, n)) < .18, rng.normal(10., 7., (n, n)), 0.)
    save("gravel", plain((131, 119, 100), grain * 1.3 + grit + broad))


FRONTAGE_ASPHALT = {
    "name": "bne_eu_frontage_asphalt",
    "mapTo": "bne_eu_frontage_asphalt",
    "class": "Material",
    "version": 1.5,
    "persistentId": "a0a8621b-4a8c-4f03-b894-978f1490662b",
    "translucent": True,
    "translucentZWrite": True,
    "Stages": [
        {
            "baseColorFactor": [
                0.35,
                0.35,
                0.35,
                1
            ],
            "detailMap": "/assets/materials/terrain/asphalt/asphalt/t_asphalt_b.png",
            "detailMapUseUV": 0,
            "detailScale": [
                1,
                1
            ],
            "detailBaseColorMapStrength": 1,
            "detailNormalMap": "/assets/materials/terrain/asphalt/asphalt/t_asphalt_nm.png",
            "normalDetailMapUseUV": 0,
            "detailNormalMapStrength": 0.8,
            "roughnessFactor": 0.9,
            "metallicFactor": 0,
            "useAnisotropic": True,
            "opacityMap": "/levels/beamng_earth/art/shapes/beamngEarth/dirt_fade_o.data.png",
            "opacityMapUseUV": 1
        }
    ]
}


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
    _additional(out, n)
    materials={}
    # BeamNG's .color.png suffix identifies sRGB base-colour textures for cooking.
    # New paths also avoid reusing the previous trial's cooked pale textures.
    textures = [('bne_eu_curb','kerb.color.png'), ('bne_eu_asphalt_walk','footway.color.png'),
                ('bne_eu_tactile','tactile.color.png')]
    textures.extend((f"bne_eu_walk_{name}", f"{name}.color.png")
                    for name in ("light_asphalt", "concrete", "slabs", "pavers", "pavers_mixed", "stone", "gravel"))
    for name,texture in textures:
        materials[name]={'name':name,'mapTo':name,'class':'Material','version':1.5,
          'persistentId':str(uuid.uuid5(uuid.NAMESPACE_URL,'beamng-earth:'+name)),
          'Stages':[{'baseColorMap':VFS+'/'+texture,'baseColorFactor':[1,1,1,1],
                     'metallicFactor':0,'roughnessFactor':.98}], 'doubleSided':False}
    # PR #505's paved frontages (streetfront, roadmesh.FRONTAGE_ASPHALT): the stock asphalt's detail maps, faded at
    # their exposed edges by the dirt strips' opacity map; no texture of its own
    materials['bne_eu_frontage_asphalt']=FRONTAGE_ASPHALT
    (out/'main.materials.json').write_text(json.dumps(materials,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':build()
