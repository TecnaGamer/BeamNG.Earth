"""Low-cost natural cliff surfaces on the distant terrain's existing triangles.

No extra geometry, colliders, downloads, or live native shape changes. Keep the
terrain's exact positions/triangles and skirts; split vertices only at material
and dominant-plane UV boundaries. Its existing LOD/streaming controls the cost.
"""
import math
import logging
from functools import lru_cache
from pathlib import Path
import sys
import numpy as np
import cliffmaterial as cm
import clifftypes

MIN_SLOPE_DEG = 48.0
MIN_FACE_DROP_M = 8.0
MAX_GRID_SPACING_M = 128.0
WORLD_M_PER_DEG = 111320.0
EXPOSED_CLASSES = (20, 30, 60)  # shrub, grass, bare; never forest/crops/cities/water
MATERIALS = ('bne_far_cliff_strata_blend', 'bne_far_cliff_canyon_blend')
PHOTO_BLUR_M = 48.0  # low-frequency colour, not trees/roads stretched down the face
PHOTO_BLEND = 1.0  # macro albedo comes from the same terrain photo, without a fixed rose cast
NORMAL_SCALE_TIERS = ((3072.0, 2.0), (6144.0, 4.0))  # physical tile width -> scale; allow for Mercator tiles smaller than nominal L4/L5
EXTREME_NORMAL_WIDTH_SCALE = 1.5  # keep broad mid-distance features, halve horizon width
log = logging.getLogger('earthd.farcliffs')


def normal_scale(tile_size):
    """Larger features on coarse far tiles; follows streaming LOD, not live shapes."""
    scale = 1.0
    for width, multiplier in NORMAL_SCALE_TIERS:
        if tile_size >= width:
            scale = multiplier
    return scale


def classify(built, frame, surface=None):
    """Per-triangle family (-1 terrain, 0 natural, 1 canyon), no network waits.

    Known exposed land qualifies globally. Missing land cover qualifies only in
    the explicit dry plateau appearance region, so distant canyon coverage does
    not depend on near-road land-cover prefetch. Known exclusions still win.
    """
    p, t = built.positions, built.tris
    labels = np.full(len(t), -1, np.int8)
    if built.size/(built.grid.shape[0]-1) > MAX_GRID_SPACING_M:
        return labels
    vertices = p[t]
    normal = np.cross(vertices[:,1]-vertices[:,0], vertices[:,2]-vertices[:,0])
    # A vertical skirt has zero XY area. Never texture the hidden stitching faces.
    z = normal[:,2]
    steep = (z > 1e-5) & (np.hypot(normal[:,0],normal[:,1]) > z*math.tan(math.radians(MIN_SLOPE_DEG)))
    steep &= np.ptp(vertices[:,:,2],axis=1) >= MIN_FACE_DROP_M
    index = np.flatnonzero(steep)
    if not len(index):
        return labels
    centers = vertices[index].mean(axis=1)+np.asarray(built.pos)
    lat,lon,_ = frame.enu_to_geodetic(*centers.T)
    classes = np.zeros(len(index),np.uint8)
    if surface is not None:
        classes,_ = surface.classes(lat,lon,fetch=False)  # cached only; never fetches/waits
    labels[index] = clifftypes.far_families(lat, lon, classes, EXPOSED_CLASSES)
    return labels


def terrain_colors(texture, uv, size, *, blur_m=PHOTO_BLUR_M):
    """Blur the tile's existing satellite DDS, sample south-up UV.

    Bake the macro photo field into existing vertices: no new GPU textures,
    materials per tile, triangles or downloads. Keep its luminance/shadows.
    """
    if texture is None:
        return None
    from PIL import Image, ImageFilter
    import cliffcolors
    try:
        with Image.open(texture) as im:
            sigma = max(0.0, blur_m*im.width/max(size,1))
            blurred = im.convert('RGB').filter(ImageFilter.GaussianBlur(sigma))
            rgb = cliffcolors.linear(np.asarray(blurred,np.float32)/255).astype(np.float32)
    except (OSError, ValueError) as e:
        log.debug('cliff colour fallback for %s: %s', texture, e)
        return None
    h,w = rgb.shape[:2]
    x = np.clip(uv[:,0]*w-.5, 0, w-1)
    y = np.clip((1-uv[:,1])*h-.5, 0, h-1)  # DDS row 0 north, CDAE writer flips V
    ix,iy = np.floor(x).astype(int),np.floor(y).astype(int)
    jx,jy = np.minimum(ix+1,w-1),np.minimum(iy+1,h-1)
    fx,fy = (x-ix)[:,None],(y-iy)[:,None]
    return (1-fy)*((1-fx)*rgb[iy,ix]+fx*rgb[iy,jx])+fy*((1-fx)*rgb[jy,ix]+fx*rgb[jy,jx])


@lru_cache(maxsize=1)
def _canyon_detail_field():
    """Low-frequency shader modulation, used only to match boundary colors.

    Read the shipped material rather than maintaining another artistic palette.
    Filter in linear light so distant mip levels retain the same mean. This is
    a CPU lookup, not an additional runtime texture or a texture modification.
    """
    import json
    from PIL import Image
    import cliffcolors
    root=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))/'mod/beamng_earth'
    material=root/'levels/beamng_earth/art/shapes/beamngEarth/cliff.materials.json'
    stage=json.loads(material.read_text())['bne_far_cliff_canyon_blend']['Stages'][0]
    # Reduce one output row at a time: a 4K asset must not create several
    # full-resolution float images in every terrain worker. Assets are POT.
    factor=cliffcolors.linear(stage['baseColorFactor'][:3])/cm.SATELLITE_HEADROOM
    filtered=np.empty((64,64,3),np.float32)
    with Image.open(root/stage['detailMap'].lstrip('/')) as im:
        rows,cols=im.height//64,im.width//64
        for y in range(64):
            strip=im.crop((0,y*rows,im.width,(y+1)*rows)).convert('RGB')
            detail=cliffcolors.linear(np.asarray(strip,np.float32)/255)
            gain=np.clip(1+(2*detail-1)*stage['detailBaseColorMapStrength'],0,1)*factor
            filtered[y]=gain.reshape(rows,64,cols,3).mean(axis=(0,2))
    return np.flipud(filtered).copy()


def canyon_detail_gain(uv):
    """Periodic bilinear macro albedo multiplier in the existing wall chart."""
    grid=_canyon_detail_field()
    h,w=grid.shape[:2]
    xy=np.mod(uv,1)*np.array([w,h])-.5
    ij=np.floor(xy).astype(int);f=xy-ij
    x,y=ij.T;fx,fy=f[:,0,None],f[:,1,None]
    return ((1-fy)*((1-fx)*grid[y%h,x%w]+fx*grid[y%h,(x+1)%w])+
            fy*((1-fx)*grid[(y+1)%h,x%w]+fx*grid[(y+1)%h,(x+1)%w]))


def geometry(built, frame, terrain_uv, terrain_uv2, terrain_material, surface=None, terrain_texture=None):
    """CDAE arguments, or None when no cliff qualifies. Triangle count is exact."""
    labels = classify(built,frame,surface)
    if not np.any(labels >= 0):
        return None
    p,t = built.positions,built.tris
    import cliffcolors
    photo = terrain_colors(terrain_texture, terrain_uv, built.size)
    boundary = np.empty(0,np.int64)
    if photo is not None:
        # At a real cliff/terrain boundary use the precise adjoining photo,
        # interpolating toward the blurred interior over the existing triangles.
        # Exclude skirts: tile borders alone must not change the color filter.
        face_z = np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]])[:,2]
        boundary = np.intersect1d(np.unique(t[labels >= 0]),
                                 np.unique(t[(labels < 0) & (face_z > 1e-5)]))
        if len(boundary):
            exact = terrain_colors(terrain_texture, terrain_uv[boundary], built.size, blur_m=0)
            if exact is not None:
                photo[boundary] = exact
    references = (cliffcolors.linear(cm.FAR_STRATA_MEDIAN_SRGB)*cm.FAR_STRATA_COLOR_FACTOR,
                  cliffcolors.linear(cm.CANYON_MEDIAN_SRGB)*cm.CANYON_COLOR_FACTOR)
    world = p+np.asarray(built.pos)
    lat,lon,elev = frame.enu_to_geodetic(*world.T)
    # Shared with near cliffs: metres in the geographic chart, true elevation.
    x,y = lon*WORLD_M_PER_DEG,lat*WORLD_M_PER_DEG
    normals = np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]])
    use_y = np.abs(normals[:,0]) >= np.abs(normals[:,1])
    groups = [(0,labels < 0,None)]
    for family in (0,1):
        for axis in (False,True):
            groups.append((family+1,(labels == family)&(use_y == axis),axis))
    arrays = {k:[] for k in ('positions','tris','normals','uvs','uvs2','tangent_uvs','colors','tri_materials')}
    count=0
    normal_repeat = normal_scale(built.size)
    for material,mask,axis in groups:
        selected=t[mask]
        if not len(selected):
            continue
        used,remap=np.unique(selected,return_inverse=True)
        if axis is None:
            uv=terrain_uv[used]
            uv2=terrain_uv2[used] if terrain_uv2 is not None else np.zeros_like(uv)
            tangent=built.planar[used]
        else:
            uv=np.column_stack(((y if axis else x)[used]/cm.WIDTH_M,elev[used]/cm.height_span(cm.CANYON if material == 2 else cm.NATURAL)))
            # Derive UV1 from the absolute chart BEFORE reducing UV0's origin.
            # Dividing a tile-local UV would reset phase and expose tile seams.
            normal_width = (EXTREME_NORMAL_WIDTH_SCALE if built.size >= NORMAL_SCALE_TIERS[-1][0] else cm.FAR_CANYON_NORMAL_WIDTH_SCALE) if material == 2 else 1.0
            uv2=uv/(normal_repeat*np.array((normal_width,1.0)))
            uv2-=np.floor(uv2[0])
            uv-=np.floor(uv[0])  # colour beds keep their existing physical scale
            tangent=uv2  # positive axis scales preserve the wall tangent frame
        arrays['positions'].append(p[used]); arrays['normals'].append(built.normals[used])
        arrays['tris'].append(remap.reshape(-1,3)+count)
        arrays['uvs'].append(uv); arrays['uvs2'].append(uv2); arrays['tangent_uvs'].append(tangent)
        if axis is None:
            colors=np.full((len(used),4),255,np.uint8)
        else:
            target=(np.broadcast_to(references[material-1],(len(used),3)).copy()
                    if photo is None else photo[used].copy())
            if material == 2 and len(boundary):
                edge=np.isin(used,boundary)
                # Cancel the broad authored bed's gain at the shared vertices.
                # Raster interpolation tapers that correction across boundary
                # triangles; interior vertices keep the full stronger layering.
                # Normals, photo shadows and all geometry remain unchanged.
                target[edge]/=np.maximum(canyon_detail_gain(uv[edge]),.1)
            colors=cliffcolors.material_tint(target,np.ones(3),PHOTO_BLEND)
        arrays['colors'].append(colors)
        arrays['tri_materials'].append(np.full(len(selected),material,np.int32))
        count+=len(used)
    result={key:np.concatenate(values) for key,values in arrays.items()}
    result['material']=[terrain_material,*MATERIALS]
    result['split_meshes']=True
    return result
