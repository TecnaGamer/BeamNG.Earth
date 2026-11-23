"""Low-cost natural cliff surfaces on the distant terrain's existing triangles.

No extra geometry, colliders, downloads, or live native shape changes. Keep the
terrain's exact positions/triangles and skirts; split vertices only at material
and coherent wall-chart boundaries. Its existing LOD/streaming controls the cost.
"""
from cliffsettings import CLIFF_KERNEL
import math
import io
import struct
import logging
from functools import lru_cache
from pathlib import Path
import sys
import numpy as np
import cliffmaterial as cm
import clifftypes

WORLD_M_PER_DEG = 111320.0
EXPOSED_CLASSES = (20, 30, 60)  # shrub, grass, bare
FOREST_CLASS = 10
MATERIALS = ('bne_far_cliff_strata_blend', 'bne_far_cliff_canyon_blend', 'bne_far_cliff_arid_blend')
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
    """Per-triangle family (-1 terrain, 0 default, 1 canyon, 2 arid), no network waits.

    Known exposed land qualifies globally. Missing land cover qualifies only in
    explicit canyon regions or the arid climate rule, so dry cliff coverage does
    not depend on near-road land-cover prefetch. Sheer forest-labelled rim walls
    qualify too; ordinary forest slopes and other known exclusions still win.
    """
    p, t = built.positions, built.tris
    labels = np.full(len(t), -1, np.int8)
    import fast
    fast.require(CLIFF_KERNEL, "distant cliff selection")
    index, centers, forest_wall = fast._ef.cliff_far_candidates(
        np.ascontiguousarray(p, float), np.ascontiguousarray(t, np.int64),
        built.size / (built.grid.shape[0] - 1))
    if not len(index):
        return labels
    centers += np.asarray(built.pos)
    lat,lon,_ = frame.enu_to_geodetic(*centers.T)
    classes = np.zeros(len(index),np.uint8)
    if surface is not None:
        classes,_ = surface.classes(lat,lon,fetch=False)  # cached only; never fetches/waits
    # WorldCover observes the canopy from above: trees on a rim can label the
    # nearly vertical wall below as forest. Keep ordinary forest slopes excluded,
    # but allow its existing terrain faces to carry the lightweight rock surface.
    # This never permits crops, buildings, snow, water or wetlands.
    families = clifftypes.far_families(lat, lon, classes, (*EXPOSED_CLASSES, FOREST_CLASS))
    families[(classes == FOREST_CLASS) & ~forest_wall] = -1
    labels[index] = families
    return labels


def projection_axes(positions, tris, labels):
    """Choose coherent wall charts using the native connected-face selector."""
    import fast
    return fast._ef.cliff_far_projection_axes(np.ascontiguousarray(positions, float),
        np.ascontiguousarray(tris, np.int64), np.ascontiguousarray(labels, np.int8))


def terrain_colors(texture, uv, size, *, blur_m=PHOTO_BLUR_M):
    """Blur the tile's existing satellite DDS, sample south-up UV.

    Bake the macro photo field into existing vertices: no new GPU textures,
    materials per tile, triangles or downloads. Keep its luminance/shadows.
    """
    if texture is None:
        return None
    from PIL import Image
    import fast
    try:
        data = Path(texture).read_bytes()
        # Pillow cannot open BC1_UNORM_SRGB (72). BC1_UNORM (71) has identical
        # blocks; retag only this CPU copy to read the original encoded colours.
        # Leave the on-disk sRGB tag intact for the game's lighting pipeline.
        if (len(data) >= 148 and data[:4] == b'DDS ' and data[84:88] == b'DX10'
                and struct.unpack_from('<I', data, 128)[0] == 72):
            data = data[:128] + struct.pack('<I', 71) + data[132:]
        with Image.open(io.BytesIO(data)) as im:
            sigma = max(0.0, blur_m*im.width/max(size,1))
            rgb = fast._ef.cliff_far_photo_field(np.asarray(im.convert('RGB')), sigma)
    except (OSError, ValueError) as e:
        log.debug('cliff colour fallback for %s: %s', texture, e)
        return None
    return _sample_colors(rgb, uv, periodic=False)


def _sample_colors(grid, uv, *, periodic):
    import fast
    return fast._ef.cliff_far_sample_colors(np.ascontiguousarray(grid, np.float32),
                                           np.ascontiguousarray(uv, float), periodic)


@lru_cache(maxsize=2)
def _canyon_detail_field(family=1):
    """Low-frequency shader modulation, used only to match boundary colors.

    Read the shipped material rather than maintaining another artistic palette.
    Filter in linear light so distant mip levels retain the same mean. This is
    a CPU lookup, not an additional runtime texture or a texture modification.
    """
    import json
    from PIL import Image
    import cliffcolors
    import fast
    import modtextures
    root=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))/'mod/beamng_earth'
    material=root/'levels/beamng_earth/art/shapes/beamngEarth/cliff_materials.src.json'
    stage=json.loads(material.read_text())[MATERIALS[family]]['Stages'][0]
    # Reduce one output row at a time: a 4K asset must not create several
    # full-resolution float images in every terrain worker. Assets are POT.
    factor=cliffcolors.linear(stage['baseColorFactor'][:3])/cm.SATELLITE_HEADROOM
    filtered=np.empty((64,64,3),np.float32)
    # Installation owns DDS conversion; the server reads the portable lossless
    # source, also bundled in frozen builds, rather than a player's mod directory.
    source_root = root.parent/'texture_sources'
    entry = modtextures.manifest(source_root/'textures.json')[stage['detailMap'].lstrip('/')]
    with Image.open(source_root/entry['source']) as im:
        if im.width % 64 or im.height % 64:
            raise ValueError('cliff detail sources must have dimensions divisible by 64')
        rows=im.height//64
        for y in range(64):
            strip=im.crop((0,y*rows,im.width,(y+1)*rows)).convert('RGB')
            filtered[y]=fast._ef.cliff_far_detail_row(np.asarray(strip), 64,
                                                    stage['detailBaseColorMapStrength'], factor.tolist())
    return np.flipud(filtered).copy()


def canyon_detail_gain(uv, family=1):
    """Periodic bilinear macro albedo multiplier in the existing wall chart."""
    return _sample_colors(_canyon_detail_field(family), uv, periodic=True)


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
        import fast
        boundary = fast._ef.cliff_far_boundaries(np.ascontiguousarray(p, float),
            np.ascontiguousarray(t, np.int64), np.ascontiguousarray(labels, np.int8))
        if len(boundary):
            exact = terrain_colors(terrain_texture, terrain_uv[boundary], built.size, blur_m=0)
            if exact is not None:
                photo[boundary] = exact
    references = (cliffcolors.linear(cm.FAR_STRATA_MEDIAN_SRGB)*cm.FAR_STRATA_COLOR_FACTOR,
                  cliffcolors.linear(cm.CANYON_MEDIAN_SRGB)*cm.CANYON_COLOR_FACTOR,
                  cliffcolors.linear(np.array([.63,.57,.48])))
    world = p+np.asarray(built.pos)
    lat,lon,elev = frame.enu_to_geodetic(*world.T)
    # Shared with near cliffs: metres in the geographic chart, true elevation.
    x,y = lon*WORLD_M_PER_DEG,lat*WORLD_M_PER_DEG
    use_y = projection_axes(p,t,labels)
    normal_repeat = normal_scale(built.size)
    canyon_width = (EXTREME_NORMAL_WIDTH_SCALE if built.size >= NORMAL_SCALE_TIERS[-1][0]
                    else cm.FAR_CANYON_NORMAL_WIDTH_SCALE)
    scales = np.array([[cm.WIDTH_M, cm.height_span(cm.NATURAL), normal_repeat, normal_repeat],
                       [cm.WIDTH_M, cm.height_span(cm.CANYON), normal_repeat*canyon_width, normal_repeat],
                       [cm.WIDTH_M, cm.height_span(cm.NATURAL), 1., 1.]])
    import fast
    groups = fast._ef.cliff_far_layout(np.ascontiguousarray(t, np.int64),
        np.ascontiguousarray(labels, np.int8), np.ascontiguousarray(use_y, bool),
        np.ascontiguousarray(np.column_stack((x, y, elev))), np.ascontiguousarray(terrain_uv, float),
        np.ascontiguousarray(terrain_uv2, float) if terrain_uv2 is not None else np.zeros((len(p), 2)),
        np.ascontiguousarray(built.planar, float), scales)
    arrays = {k:[] for k in ('positions','tris','normals','uvs','uvs2','tangent_uvs','colors','tri_materials')}
    for material, used, remap, uv, uv2, tangent in groups:
        arrays['positions'].append(p[used]); arrays['normals'].append(built.normals[used])
        arrays['tris'].append(remap)
        arrays['uvs'].append(uv); arrays['uvs2'].append(uv2); arrays['tangent_uvs'].append(tangent)
        if material == 0:
            colors=np.full((len(used),4),255,np.uint8)
        else:
            target=(np.broadcast_to(references[material-1],(len(used),3)).copy()
                    if photo is None else photo[used].copy())
            if material in (2,3) and len(boundary):
                edge=np.isin(used,boundary)
                # Cancel the broad authored bed's gain at the shared vertices.
                # Raster interpolation tapers that correction across boundary
                # triangles; interior vertices keep the full stronger layering.
                # Normals, photo shadows and all geometry remain unchanged.
                target[edge]/=np.maximum((canyon_detail_gain(uv[edge]) if material == 2 else canyon_detail_gain(uv[edge],2)),.1)
            colors=cliffcolors.material_tint(target,np.ones(3),PHOTO_BLEND)
        arrays['colors'].append(colors)
        arrays['tri_materials'].append(np.full(len(remap),material,np.int32))
    result={key:np.concatenate(values) for key,values in arrays.items()}
    result['material']=[terrain_material,*MATERIALS]
    return result
