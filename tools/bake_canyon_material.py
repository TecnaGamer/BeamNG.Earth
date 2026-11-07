"""Bake the generated canyon source into periodic, restrained cliff PBR assets.

No model calls. Approximate height removes broad albedo variation so pale beds
are not mistaken for metres of protruding stone. Resample albedo to the requested 4K runtime size (source is 1254 square, not
native 4K). The full bake delegates near PBR to the reconstructed tile baker.
Use --far-only then --publish-far for distant normal revisions without changing near assets.
"""
import json
import sys
from pathlib import Path
import cv2
import numpy as np
from PIL import Image
from bake_scan_cliff_material import periodic, resize_periodic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import cliffmaterial as cm
from cliffcolors import srgb, linear
SOURCE = ROOT/'tools/assets/canyon_generated/source.png'
OUT = ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff'
ALBEDO_SOURCE = ROOT/'tools/assets/canyon_generated/source-layered-v4.png'
PALETTE_REFERENCE = ROOT/'tools/assets/canyon_generated/source-muted.png'
ALBEDO_NAME = 'canyon_v4_base.color.png'
PREFIX = 'canyon_v3'
# BeamNG decodes material factors and vertex colors from sRGB. Encode the
# linear gain, including values above one, so their headroom cancels exactly.
TEXTURE_SIZE = 1024  # established normal/height bake; preserve approved macro relief
ALBEDO_SIZE = 4096  # runtime size; generated source is 1254, not native 4K detail
WIDTH_M = 91.44
HEIGHT_M = 182.88
RELIEF_M = .65
FAR_NORMAL_NAME = 'canyon_far_v8.normal.png'
FAR_COOKED_NORMAL_NAME = 'canyon_far_v8_ready.normal.dds'
FAR_FINE_RELIEF_M = .8125
FAR_GROOVE_RMS_M = .784
FAR_LEDGE_RMS_M = .64
FAR_SMALL_DETAIL_GAIN = 1.25  # finer channels and stone chips, independently of broad ribs
FAR_GROOVE_WIDTH_M = 2.0  # Gaussian sigma: visible ribs span roughly 5-15 m
FAR_GROOVE_HEIGHT_M = 14.0  # coherent vertical erosion, roughly 30 m long


def smooth_periodic(field, sigma_x, sigma_y):
    """FFT Gaussian: exact wrap continuity, with independent geological scales."""
    h,w = field.shape
    fx,fy = np.fft.rfftfreq(w)[None,:],np.fft.fftfreq(h)[:,None]
    kernel = np.exp(-2*np.pi**2*((fx*sigma_x)**2+(fy*sigma_y)**2))
    return np.fft.irfft2(np.fft.rfft2(field)*kernel,s=field.shape)


def far_normal(height, lum):
    """Macro erosion and beds plus quiet fractures; metres in the wall UV chart.

    This is shading relief only. It never changes near geometry or collisions.
    Combining heights before differentiation gives a coherent normal field and
    lets broad forms survive mip filtering instead of simply amplifying grain.
    """
    h,w = height.shape
    rng = np.random.default_rng(7741)
    grooves = smooth_periodic(rng.standard_normal((h,w)),
        FAR_GROOVE_WIDTH_M*w/WIDTH_M, FAR_GROOVE_HEIGHT_M*h/HEIGHT_M)
    grooves *= FAR_GROOVE_RMS_M/max(grooves.std(),1e-6)
    # Broad bed/joint structure from the authored source, not satellite shadows.
    beds = smooth_periodic(lum,16,16)-smooth_periodic(lum,96,96)
    beds *= FAR_LEDGE_RMS_M/max(beds.std(),1e-6)
    # Warp channel direction and depth at broad scales so erosion branches and
    # varies across the face instead of forming uniformly parallel ribs.
    weather = smooth_periodic(rng.standard_normal((h,w)),
        3.0*w/WIDTH_M, 5.0*h/HEIGHT_M)
    weather /= max(weather.std(), 1e-6)
    warp = smooth_periodic(rng.standard_normal((h,w)),
        3.0*w/WIDTH_M, 9.0*h/HEIGHT_M)
    warp *= .65*w/WIDTH_M/max(warp.std(), 1e-6)
    yy,xx = np.indices((h,w),dtype=np.float32)
    grooves = cv2.remap(grooves.astype(np.float32), (xx+warp).astype(np.float32), yy,
                        cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
    # A rounded absolute-value field has smooth convex shoulders and narrow
    # concave V valleys. Slowly varying bias/depth breaks up parallel channels,
    # unlike symmetric clipping which made the shoulders look flat/extruded.
    channel = grooves/FAR_GROOVE_RMS_M + .25*np.tanh(weather)
    grooves = np.sqrt(channel*channel + .16**2)*(.9+.18*np.tanh(weather))
    grooves -= grooves.mean()
    grooves *= FAR_GROOVE_RMS_M/max(grooves.std(), 1e-6)
    small = smooth_periodic(rng.standard_normal((h,w)),
        .45*w/WIDTH_M, 3.0*h/HEIGHT_M)
    small /= max(small.std(), 1e-6)
    broken = smooth_periodic(rng.standard_normal((h,w)),
        1.5*w/WIDTH_M, 1.5*h/HEIGHT_M)
    broken /= max(broken.std(), 1e-6)
    # Interrupted sharp V cuts, at a smaller scale than the retained macro ribs.
    cuts = -.24*np.maximum(0, .7-np.abs(small))*np.clip(.7+.3*broken, 0, 1)
    tributaries = smooth_periodic(rng.standard_normal((h,w)),
        .8*w/WIDTH_M, 5.5*h/HEIGHT_M)
    tributaries /= max(tributaries.std(), 1e-6)
    tributaries = cv2.remap(tributaries.astype(np.float32),
        (xx+.6*warp).astype(np.float32), yy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
    cuts -= .38*np.maximum(0, .9-np.abs(tributaries+.3*weather))*np.clip(.6+.4*weather,0,1)
    # Source-aligned chips: bilateral filtering retains stone/joint boundaries
    # without turning every albedo grain into a high-amplitude bump.
    chips = height-smooth_periodic(height, 12, 12)
    chips = np.clip(chips/max(chips.std(),1e-6), -1.5, 1.5).astype(np.float32)
    padded = np.pad(chips, 4, mode='wrap')
    chips = cv2.bilateralFilter(padded, 9, .4, 2)[4:-4,4:-4]
    relief = grooves+beds+FAR_SMALL_DETAIL_GAIN*(cuts+.10*chips)+FAR_FINE_RELIEF_M*smooth_periodic(height,2,2)
    dx = (np.roll(relief,-1,1)-np.roll(relief,1,1))*w/(2*WIDTH_M)
    dy = (np.roll(relief,-1,0)-np.roll(relief,1,0))*h/(2*HEIGHT_M)
    normal = np.stack((-dx,dy,np.ones_like(dx)),-1)
    return normal/np.linalg.norm(normal,axis=-1,keepdims=True)


def albedo_mean(path):
    """Mean linear RGB in bounded strips, including for 4K assets."""
    total = np.zeros(3)
    with Image.open(path) as im:
        for row in range(0,im.height,128):
            strip = np.asarray(im.crop((0,row,im.width,min(row+128,im.height))).convert('RGB'),np.float32)/255
            total += linear(strip).sum(axis=(0,1),dtype=np.float64)
        return total/(im.width*im.height)


def bake():
    rgb = np.asarray(Image.open(SOURCE).convert('RGB'), np.float32)/255
    rgb = cv2.resize(rgb, (TEXTURE_SIZE, TEXTURE_SIZE), interpolation=cv2.INTER_AREA)
    rgb = periodic(rgb, 32)
    lum = rgb @ np.array((.2126, .7152, .0722), np.float32)
    smooth = cv2.GaussianBlur(lum, (0,0), 10)
    height = periodic(np.clip(.5 + (lum-smooth)*2.0, 0, 1), 32)
    ny,nx = height.shape
    # Intentionally quiet normal response; geometry supplies the broad relief.
    dx = (np.roll(height,-1,1)-np.roll(height,1,1)) * RELIEF_M*nx/(2*WIDTH_M)*.35
    dy = (np.roll(height,-1,0)-np.roll(height,1,0)) * RELIEF_M*ny/(2*HEIGHT_M)*.35
    normal = np.stack((-dx,dy,np.ones_like(dx)), -1)
    normal /= np.linalg.norm(normal,axis=-1,keepdims=True)
    OUT.mkdir(parents=True,exist_ok=True)
    def save(name,a): Image.fromarray(np.uint8(np.rint(np.clip(a,0,1)*255))).save(OUT/(PREFIX+name))
    # Keep relief from the original source: recoloring must not reshape cliffs.
    albedo = np.asarray(Image.open(ALBEDO_SOURCE).convert('RGB'), np.float32)/255
    albedo = cv2.resize(albedo, (ALBEDO_SIZE, ALBEDO_SIZE), interpolation=cv2.INTER_LANCZOS4)
    albedo = periodic(albedo,32*ALBEDO_SIZE//TEXTURE_SIZE)
    Image.fromarray(np.uint8(np.rint(np.clip(albedo,0,1)*255))).save(OUT/ALBEDO_NAME)
    # Preserve the previous near material's mean linear RGB. Geological contrast
    # changes, but its average exposure/palette and existing vertex tints do not.
    reference = np.asarray(Image.open(PALETTE_REFERENCE).convert('RGB'),np.float32)/255
    reference = periodic(cv2.resize(reference,(TEXTURE_SIZE,TEXTURE_SIZE),interpolation=cv2.INTER_AREA),32)
    reference = np.rint(np.clip(reference,0,1)*255)/255
    palette_gain = linear(reference).mean(axis=(0,1))/albedo_mean(OUT/ALBEDO_NAME)
    canyon_factor = [*srgb(cm.CANYON_COLOR_FACTOR*cm.SATELLITE_HEADROOM*palette_gain),1]
    save('.normal.png',normal*.5+.5)
    macro_normal = far_normal(height, lum)
    Image.fromarray(np.uint8(np.rint((macro_normal*.5+.5)*255))).save(OUT/FAR_NORMAL_NAME)

    save('_roughness.data.png',np.full(lum.shape,.94,np.float32))
    cpu = periodic(resize_periodic(height,(256,64)),4)
    Image.fromarray(np.uint16(np.rint(cpu*65535))).save(ROOT/'server/cliff_canyon_height.png')
    path = OUT.parent/'cliff.materials.json'
    mats=json.loads(path.read_text())
    prefix='/levels/beamng_earth/art/shapes/beamngEarth/cliff/'
    for name,base,factor,near in (
        ('bne_cliff_canyon_sat',ALBEDO_NAME,canyon_factor,True),
        ('bne_far_cliff_canyon_sat',ALBEDO_NAME,canyon_factor,False),
        ('bne_far_cliff_strata_sat','scan_v2_base.color.png',[*srgb(cm.FAR_STRATA_COLOR_FACTOR*cm.SATELLITE_HEADROOM),1],False)):
        stage=dict(baseColorMap=prefix+base,baseColorFactor=factor,metallicFactor=0,
                   roughnessFactor=.94,vertColor=True,useAnisotropic=True)
        if near:
            stage['baseColorFactor'] = [*srgb(linear(factor[:3])*cm.NEAR_CANYON_EXPOSURE),1]
            stage['normalMapStrength'] = cm.NEAR_CANYON_NORMAL_STRENGTH
            stage['normalMapUseUV'] = 1  # independent, wider horizontal normal chart
            stage.update(normalMap=prefix+PREFIX+'.normal.png',
                         roughnessMap=prefix+PREFIX+'_roughness.data.png',roughnessMapUseUV=0)
        else:
            stage['normalMap'] = prefix+(FAR_NORMAL_NAME if 'canyon' in name else 'scan_v2_flat.normal.png')
            stage['normalMapUseUV'] = 1  # separate world chart: larger features in coarse far LODs
            if 'canyon' in name:
                stage['normalMapStrength'] = cm.FAR_CANYON_NORMAL_STRENGTH
        mats[name]=dict(name=name,mapTo=name,**{'class':'Material'},version=1.5,
                        Stages=[stage,{},{},{}],groundType='ROCK',translucent=False)
    # Satellite color is the base; the same authored rock supplies quiet detail.
    # Mean-compensate the exact engine blendDetail equation, so adding this
    # texture cannot introduce a fixed pink cast or an exposure step at edges.
    for family,base,normal_name in (
        ('canyon',ALBEDO_NAME,FAR_NORMAL_NAME),
        ('strata','scan_v2_base.color.png','scan_v2_flat.normal.png')):
        strength = (cm.FAR_CANYON_ALBEDO_DETAIL_STRENGTH if family == 'canyon'
                    else cm.SATELLITE_ALBEDO_DETAIL_STRENGTH)
        total = np.zeros(3)
        with Image.open(OUT/base) as im:
            for row in range(0,im.height,128):
                strip = im.crop((0,row,im.width,min(row+128,im.height))).convert('RGB')
                detail = linear(np.asarray(strip,np.float32)/255)
                modulation = np.clip(1+(2*detail-1)*strength,0,1)
                total += modulation.sum(axis=(0,1))
            mean = total/(im.width*im.height)
        name = 'bne_far_cliff_'+family+'_blend'
        stage = dict(baseColorFactor=[*srgb(cm.SATELLITE_HEADROOM/mean),1],
                     detailMap=prefix+base,detailMapUseUV=0,detailScale=[1,1],
                     detailBaseColorMapStrength=strength,
                     normalMap=prefix+normal_name,normalMapUseUV=1,
                     roughnessFactor=.94,metallicFactor=0,vertColor=True,useAnisotropic=True)
        if family == 'canyon':
            stage['normalMapStrength'] = cm.FAR_CANYON_NORMAL_STRENGTH
        mats[name]=dict(name=name,mapTo=name,**{'class':'Material'},version=1.5,
                       Stages=[stage,{},{},{}],groundType='ROCK',translucent=False)
    path.write_text(json.dumps(mats,indent=2)+'\n')
    # Preserve the approved distant bake, then install reconstructed 4K near detail.
    from bake_canyon_tiled import bake as bake_tiled_near
    bake_tiled_near()
    print(f'Baked distant canyon from legacy source; near material uses reconstructed 4K patches.')

def bake_far_only():
    """Rebuild distant normal relief without touching near PBR, colors or CPU height."""
    rgb = np.asarray(Image.open(SOURCE).convert('RGB'), np.float32)/255
    rgb = periodic(cv2.resize(rgb, (TEXTURE_SIZE, TEXTURE_SIZE), interpolation=cv2.INTER_AREA), 32)
    lum = rgb @ np.array((.2126, .7152, .0722), np.float32)
    height = periodic(np.clip(.5+(lum-cv2.GaussianBlur(lum,(0,0),10))*2,0,1),32)
    normal = far_normal(height, lum)
    Image.fromarray(np.uint8(np.rint((normal*.5+.5)*255))).save(OUT/FAR_NORMAL_NAME)
    print('Baked', OUT/FAR_NORMAL_NAME)


def publish_far_normal(cooked_path):
    """Pin a fully imported BC5 DDS; never publish a not-yet-cooked PNG binding.

    Preload the PNG with BeamNG's ImTextureHandler, wait for the cooked DDS,
    then pass its real path here. This also makes releases independent of the
    runtime texture importer. The generated PNG remains the editable source.
    """
    import struct
    data = Path(cooked_path).read_bytes()
    expected_size = 148 + sum(max(1,(TEXTURE_SIZE >> level)//4)**2*16 for level in range(11))
    if (len(data) != expected_size or data[:4] != b'DDS ' or data[84:88] != b'DX10' or
            struct.unpack_from('<I',data,128)[0] != 83 or
            struct.unpack_from('<II',data,12) != (TEXTURE_SIZE,TEXTURE_SIZE) or
            struct.unpack_from('<I',data,28)[0] != 11):
        raise ValueError('Expected fully mipmapped 1024-square BC5_UNORM DDS')
    (OUT/FAR_COOKED_NORMAL_NAME).write_bytes(data)
    path = OUT.parent/'cliff.materials.json'
    mats = json.loads(path.read_text())
    for name in ('bne_far_cliff_canyon_sat', 'bne_far_cliff_canyon_blend'):
        mats[name]['Stages'][0]['normalMap'] = '/levels/beamng_earth/art/shapes/beamngEarth/cliff/'+FAR_COOKED_NORMAL_NAME
    path.write_text(json.dumps(mats,indent=2)+'\n')


if __name__=='__main__':
    if '--publish-far' in sys.argv:
        publish_far_normal(sys.argv[sys.argv.index('--publish-far')+1])
    elif '--far-only' in sys.argv:
        bake_far_only()
    else:
        bake()
