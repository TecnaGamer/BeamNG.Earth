"""The whole earth as one globe for the 3D map's far-out views (the user, 2026-11-07: "zoom out far enough to view the
entire earth like on google earth"). Far tiles reach HORIZON_MAX (2600 km) and the game's sky breaks above ~600 km;
past that the 3D map draws this instead: a latitude / longitude sphere a little under sea level (the far tiles, where
they are, draw over it), textured with the world at WORLD_ZOOM, one Web-Mercator mosaic (the sphere's uv is Mercator:
no reprojection; the caps past MERC_LAT take its edge rows): the land from the project's own Sentinel-2 packs (bngs2,
ESA WorldCover's 2021 composite, CC BY 4.0: the colours of the far tiles under it) when the CDN switch has them, and
EOX's Sentinel-2 cloudless 2016 (CC BY 4.0) at sea, on the ice past the packs and without them. Never EOX's 2018+
mosaics: CC BY-NC-SA 4.0 (the licence review of 2026-11-23; the 2023 one was used until then).

The texture is made once and kept in the cache (WORLD_ZOOM's 256 EOX tiles and 4 packs: well inside EOX's daily budget,
then never again); the sphere is written per anchor, in its frame (positions to ~1 m at the earth's scale, plenty from
orbit)."""
import logging
import math
import os
import shutil

import numpy as np
import dnp

log = logging.getLogger("earthd.globe")

WORLD_ZOOM = 4                  # 16 x 16 tiles, a 4096 px world (~10 km a pixel at the equator)
WORLD_PX = 256 * 2 ** WORLD_ZOOM
WORLD_VERSION = 2               # bump: the cached world texture is made again (2: no EOX 2023 pixels)
RINGS, SEGS = 128, 256          # the sphere's latitude rings and longitude segments
MINI_R = 30000.0                # m: the small copy's radius (far out: mod earthGlobe MINI_*), centred on its origin
SINK_M = 3000.0                 # the sphere this far under sea level (under the far tiles)
MERC_LAT = 85.05112878
EOX_URL = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg"     # (2016: CC BY 4.0)
S2_K = 3                        # bngs2's packs: 8 x 8 tiles (imagery.SOURCES["bngs2"].pack)
S2_NODATA_MAX = 6               # a pack texel this dark is outside the data (imagery.NODATA_MAX: open sea)


def s2_world(fetcher):
    """WORLD_ZOOM's bngs2 tiles as one (WORLD_PX, WORLD_PX, 3) image and its texels with data, and whether every pack
    was answered (a pack that is not there: no land under it); (None, None, False) when none came."""
    import io
    import imagery
    import netfetch
    import tilepack
    from PIL import Image
    n = 1 << max(0, WORLD_ZOOM - S2_K)
    img = np.zeros((WORLD_PX, WORLD_PX, 3), np.uint8)
    got, answered = 0, 0
    for py in range(n):
        for px in range(n):
            try:
                body = fetcher.get(imagery.SOURCES["bngs2"].url.format(z=WORLD_ZOOM, px=px, py=py), kind="demand").body
                _, _, _, k, tiles = tilepack.parse(body)
            except Exception as e:      # (not there: open sea; offline, a damaged pack: the next start tries again)
                if isinstance(e, netfetch.NotFound):
                    answered += 1
                log.debug("globe: bngs2 pack %s/%s: %s: %s", px, py, type(e).__name__, e)
                continue
            answered += 1
            for i, b in tiles.items():
                _, tx, ty = tilepack.tile_at(WORLD_ZOOM, px, py, k, i)
                try:
                    t = np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))
                except (OSError, ValueError):
                    continue
                if t.shape[:2] == (256, 256):
                    img[ty * 256:(ty + 1) * 256, tx * 256:(tx + 1) * 256] = t
                    got += 1
    if not got:
        return None, None, False
    return img, img.max(axis=2) > S2_NODATA_MAX, answered == n * n


def world_texture(cache_dir, fetcher, s2=True):
    """The cached world texture's path (made once: WORLD_ZOOM's tiles as one DDS, the land from bngs2 when s2, the
    rest from EOX's 2016 mosaic), or None when no tile came."""
    import imagery
    import spaceclouds
    name = "s2wc2021-eox2016" if s2 else "eox2016"
    path = os.path.join(cache_dir, "globe", f"world_{name}_z{WORLD_ZOOM}_v{WORLD_VERSION}.dds")
    if os.path.exists(path):
        return path
    # ("demand": "background" waits behind the world's own downloads and most tiles came back deferred: a world of
    # 45 tiles out of 256 was black, and kept, 2026-11-07. Kept only whole; a partial one is used this time only.)
    img, got = spaceclouds.mosaic(fetcher, WORLD_ZOOM, 0, 0, WORLD_PX, WORLD_PX, url=EOX_URL, px=WORLD_PX,
                                  kind="demand")
    whole = got >= dnp.sq(2 ** WORLD_ZOOM)
    if s2:
        land, has, all_packs = s2_world(fetcher)
        whole = whole and all_packs
        if land is not None:
            img = np.where(has[..., None], land, img) if got else land
            got += 1
    if not got:
        return None
    out = path if whole else path + ".partial.dds"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    imagery.write_dds(out, img)
    log.info("globe: world texture made from %d tiles at zoom %d (%d px)%s", got, WORLD_ZOOM, WORLD_PX,
             "" if whole else ": not all there, made again next time")
    return out


def sphere(rings=RINGS, segs=SEGS):
    """The sphere's (lat, lon) per vertex (degrees; a column at -180 and one at +180 for the texture's seam), its
    Mercator uv and its triangles (counter-clockwise seen from outside)."""
    lat = np.linspace(90.0, -90.0, rings + 1)
    lon = np.linspace(-180.0, 180.0, segs + 1)
    LON, LAT = np.meshgrid(lon, lat)
    u = (LON + 180.0) / 360.0
    la = np.radians(np.clip(LAT, -MERC_LAT, MERC_LAT))
    v = 0.5 - dnp.log(dnp.tan(np.pi / 4 + la / 2)) / (2 * np.pi)       # 0 at the top (north), 1 at the bottom
    w = segs + 1
    j, i = np.meshgrid(np.arange(rings), np.arange(segs), indexing="ij")
    a = (j * w + i).ravel()
    b, c, d = a + 1, a + w, a + w + 1
    tris = np.stack([np.stack([a, c, b], 1), np.stack([b, c, d], 1)], 1).reshape(-1, 3)
    return LAT.ravel(), LON.ravel(), np.stack([u.ravel(), 1.0 - v.ravel()], 1), tris


def build(out_dir, vfs_dir, frame, anchor_id, cache_dir, fetcher, s2=True):
    """Write the globe for the anchor's frame into out_dir: the 'globe' message's fields (game paths under vfs_dir,
    the material's name), or None without a world texture. s2: the land from bngs2 (the CDN switch has it on)."""
    import cdae
    tex = world_texture(cache_dir, fetcher, s2)
    if tex is None:
        return None
    os.makedirs(out_dir, exist_ok=True)
    name = f"globe_a{anchor_id}"
    tex_out = os.path.join(out_dir, f"globe_world_v{WORLD_VERSION}" + (".partial" if tex.endswith(".partial.dds") else "")
                           + ".dds")
    if not os.path.exists(tex_out) or tex.endswith(".partial.dds"):
        shutil.copyfile(tex, tex_out)
    lat, lon, uv, tris = sphere()
    e, n, up = frame.geodetic_to_enu(lat, lon, np.full(len(lat), -SINK_M))
    pos = np.stack([e, n, up], axis=1).astype(np.float64)
    material = "bne_globe"
    cdae.write(os.path.join(out_dir, name + ".cdae"), pos, np.asarray(tris, np.int64), uvs=uv, material=material,
               object_name="globe")
    # the small copy: the same sphere about the earth's centre, MINI_R across its radius, in the same orientation
    # (a TSStatic scaled down at run time drew nothing, 2026-11-07: the mesh is small instead)
    centre = np.asarray(frame.ecef_to_enu(0.0, 0.0, 0.0), np.float64).reshape(3)
    scale = MINI_R / float(dnp.norm(pos - centre, axis=1).mean())
    mini = (pos - centre) * scale
    cdae.write(os.path.join(out_dir, name + "_mini.cdae"), mini, np.asarray(tris, np.int64), uvs=uv,
               material=material, object_name="globe_mini")
    return {"shape": f"{vfs_dir}/{name}.cdae", "miniShape": f"{vfs_dir}/{name}_mini.cdae",
            "texture": f"{vfs_dir}/{os.path.basename(tex_out)}", "material": material, "miniScale": scale}
