"""The ground photos' paint as surface.py did it per square in Python before the paint lists (try598 a01679fd's
surface.paint_square and its photo_* helpers, verbatim): the reference test_paintlist.py compares the native paint
(paintlist.py, earthfast paintlist.rs) with, pixel for pixel. Not used by the server.

Every name these functions read that is not defined here is surface.py's (its constants and the helpers the paint
lists still call: road specs, gores, roundabouts, corners, lots, fields' phase and axis)."""
import math

import numpy as np
from PIL import Image, ImageDraw

import fast
import mapgeom
import roads as roadsmod
import surface as _sf

globals().update({k: v for k, v in vars(_sf).items()
                  if not k.startswith("__") and k not in ("np", "math", "Image", "ImageDraw", "fast", "mapgeom",
                                                          "roadsmod")})


def _road_segments(frame, x0, y0, size, w, h, feats_list, urban_fn=None, widths=None, roads_of=None):
    """(segs (m, 4), half widths (m,), kind index into PHOTO_ROAD_RGBA (m,)) of the carriageways of feats_list in pixel
    coordinates of a w x h photo over [x0, x0 + size] x [y0, y0 + size] (row 0 north), or None when none is in it.
    roads_of: a list that gets, per segment, (its road's number, its road has no curb: roads.has_sidewalks)."""
    px_m = w / size
    kinds = list(PHOTO_ROAD_RGBA)
    segs, hws, kind = [], [], []
    nroad = 0
    seen = set()
    copies = roadsmod.copies_of(feats_list)     # (each joint's taper from its own cell: roads.joint_desc)
    placement = roadsmod.placement_context(feats_list, urban_fn, widths)   # (where the road tiles draw them)

    def taper_of(f):
        return roadsmod.road_tapers(f, urban_fn, widths)
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        specs = roadsmod.road_specs(feats, urban_fn, widths)
        box = _feature_boxes(feats, frame)
        ids = feats.ids.tolist()
        for i in range(len(feats)):
            fid = str(ids[i])
            sp = specs[i]
            if sp is None or fid in seen or sp.kind not in PHOTO_ROAD_RGBA:
                continue
            kname = "concrete" if sp.kind == "paved" and sp.surf == "concrete" else sp.kind
            seen.add(fid)
            m = sp.width / 2        # (all of the feature off the photo: none of its rings is in it)
            if box[i, 1] < x0 - m or box[i, 0] > x0 + size + m or box[i, 3] < y0 - m or box[i, 2] > y0 + size + m:
                continue
            hw = (sp.width / 2 + PHOTO_ROAD_EDGE_M) * px_m
            for ring in roadsmod.ground_parts(feats, i):
                ring = placement.ring(frame, feats, i, ring, sp, fid)
                e, n = mapgeom.enu_xy(feats, frame, ring)
                c = (np.asarray(e) - x0) * px_m
                r = (y0 + size - np.asarray(n)) * px_m
                if len(c) < 2 or c.max() < -hw or c.min() > w + hw or r.max() < -hw or r.min() > h + hw:
                    continue
                segs.append(np.stack([c[:-1], r[:-1], c[1:], r[1:]], axis=1))
                ws = roadsmod.taper_widths(sp.width, roadsmod.joint_desc(copies.get(fid, [(feats, i)]), taper_of),
                                           ring[:, 0], ring[:, 1])   # (tapered: road_tapers, each joint's own cell)
                if ws is None:
                    hws.append(np.full(len(c) - 1, hw))
                else:
                    hp = (ws / 2 + PHOTO_ROAD_EDGE_M) * px_m
                    hws.append((hp[:-1] + hp[1:]) / 2)
                kind.append(np.full(len(c) - 1, kinds.index(kname), np.uint8))
                if roads_of is not None:
                    roads_of.append((np.full(len(c) - 1, nroad, np.int64),
                                     np.full(len(c) - 1, not roadsmod.has_sidewalks(sp) and sp.kind == "paved")))
            nroad += 1
    # ramp gores (markings.gores: the wedge between a ramp and its motorway is paved): as more paved segments
    for hw_, mid in gore_fills(frame, feats_list, (x0 - 300, y0 - 300, x0 + size + 300, y0 + size + 300), urban_fn,
                               widths):
        c = (mid[:, 0] - x0) * px_m
        r = (y0 + size - mid[:, 1]) * px_m
        segs.append(np.stack([c[:-1], r[:-1], c[1:], r[1:]], axis=1))
        hws.append(np.maximum(hw_[:-1], hw_[1:]) * px_m)
        kind.append(np.full(len(c) - 1, kinds.index("paved"), np.uint8))
        if roads_of is not None:
            roads_of.append((np.full(len(c) - 1, nroad, np.int64), np.zeros(len(c) - 1, bool)))
            nroad += 1
    if not segs:
        return None
    return np.concatenate(segs), np.concatenate(hws), np.concatenate(kind)


def _island_pixel_polys(frame, x0, y0, size, w, h, feats_list, urban_fn=None, widths=None):
    """Pixel polygons (row 0 = north) of roundabout footprints among feats_list, for a w x h photo over
    [x0, x0 + size]² of frame."""
    px_m = w / size
    out = []
    for xy in _roundabout_footprints(_roads_list_enu(frame, feats_list, urban_fn, widths,
                                                     (x0, y0, x0 + size, y0 + size))):
        c = (xy[:, 0] - x0) * px_m
        r = (y0 + size - xy[:, 1]) * px_m
        if c.max() < 0 or c.min() > w or r.max() < 0 or r.min() > h:
            continue
        out.append(list(zip(c.tolist(), r.tolist())))
    return out


def _fill_island_photos(rgb, polys):
    """rgb with each roundabout footprint filled: median of its greener photo pixels, else ISLAND_GRASS. Covers the
    old through-road scar; carriageways paint over the ring afterwards. A footprint the photo shows paved (under
    ISLAND_MIN_GREEN of it green) is left as it is."""
    if not polys:
        return rgb
    out = rgb.copy()
    im = Image.fromarray(out)
    draw = ImageDraw.Draw(im)
    g = out.astype(np.int16)
    green = (2 * g[..., 1] - g[..., 0] - g[..., 2]) >= LAWN_GREEN
    for poly in polys:
        mask = Image.new("L", (out.shape[1], out.shape[0]), 0)
        ImageDraw.Draw(mask).polygon(poly, fill=255)
        m = np.asarray(mask) > 0
        if not m.any():
            continue
        sel = m & green
        if sel.sum() < ISLAND_MIN_GREEN * m.sum():    # (a paved island: its photo as it is)
            continue
        if sel.sum() >= 8:
            fill = tuple(int(x) for x in np.median(out[sel], axis=0))
        else:
            fill = ISLAND_GRASS
        fill = tuple(int(round(f * (1 - ISLAND_LAWN_SHARE) + c * ISLAND_LAWN_SHARE)) for f, c in zip(fill, ISLAND_LAWN))
        draw.polygon(poly, fill=fill)
    return np.asarray(im)


def photo_roads(rgb, frame, x0, y0, size, feats_list, urban_fn=None, widths=None, simple=False):
    """The photo rgb (H x W x 3, row 0 = north, covering [x0, x0 + size] x [y0, y0 + size] of frame) with the
    carriageways of feats_list (road Features: roads.road_specs widths, ground parts only) painted in, antialiased
    (earthfast.paint_lines, PHOTO_ROAD_RGBA). Roundabout footprints are filled with grass first so the photo's old
    through-road scar does not show; carriageways paint over the ring. simple: the carriageways alone (a stand-in
    photo, shown a second or two at speed: the junctions' corners were 1.3 s of its 2 s). Pure (a new array)."""
    fast.require(49, "roads in the ground")
    h, w = rgb.shape[:2]
    if simple:
        filled = np.ascontiguousarray(rgb, np.uint8)
    else:
        filled = _fill_island_photos(np.ascontiguousarray(rgb, np.uint8),
                                     _island_pixel_polys(frame, x0, y0, size, w, h, feats_list, urban_fn, widths))
    got = _road_segments(frame, x0, y0, size, w, h, feats_list, urban_fn, widths)
    if got is None:
        out = filled
    else:
        colors = np.array([PHOTO_ROAD_RGBA[k] for k in PHOTO_ROAD_RGBA], np.float32)
        fast.require(231, "roads in the photo's own tone, concrete's light")
        tone = np.array([PHOTO_ROAD_TONE[k] for k in PHOTO_ROAD_RGBA], np.float32)
        radius = max(1, int(round(PHOTO_TONE_RADIUS_M * w / size)))
        out = fast._ef.paint_lines_toned(np.ascontiguousarray(filled, np.uint8), *got, colors, tone, radius,
                                         PHOTO_TONE_SAMPLE_LUMA, PHOTO_TONE_SAMPLE_SAT, PHOTO_TONE_GREEN)
    if simple:
        return out
    # the rounded corners (roadcorners.py: curbless junctions and the curb returns' fills) over the roads, in the tone
    # of the painted road around each (the paint's corner follows the curb's arc)
    return photo_corners(out, frame, x0, y0, size, corner_pads(frame, x0, y0, size, feats_list, urban_fn, widths),
                         before=filled)


def photo_golf(rgb, frame, x0, y0, size, feats_list):
    """The photo with golf surfaces tinted (GOLF_PHOTO), under the lots and the carriageways. Pure: rgb is not changed."""
    if rgb is None or size <= 0 or not feats_list:
        return rgb
    h, w = rgb.shape[:2]
    if h == 0 or w == 0:
        return rgb
    px_x, px_y = w / float(size), h / float(size)
    out = None
    for classes, colour in GOLF_PHOTO:
        rings = _golf_rings(feats_list, classes)
        if not rings:
            continue
        im = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(im)
        n = 0
        for ring in rings:
            e, north, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], np.zeros(len(ring)))
            c = (np.asarray(e, np.float64) - x0) * px_x
            r = (y0 + size - np.asarray(north, np.float64)) * px_y
            if c.max() < 0 or c.min() > w or r.max() < 0 or r.min() > h:
                continue
            draw.polygon(list(zip(c.tolist(), r.tolist())), fill=255)
            n += 1
        if n == 0:
            continue
        box = im.getbbox()              # (only the courses' box is blended: a whole 4096² photo in float32 was
        if box is None:                 # ~200 MB a temporary, for a course in one corner)
            continue
        if out is None:
            out = np.array(rgb, np.uint8, copy=True)
        c0, r0, c1, r1 = box
        a = (np.asarray(im.crop(box), np.float32) / 255.0 * float(colour[3]))[..., None]
        paint = np.array(colour[:3], np.float32)
        sub = out[r0:r1, c0:c1]
        sub[:] = np.clip(np.round(sub.astype(np.float32) * (1.0 - a) + paint * a), 0, 255).astype(np.uint8)
    return rgb if out is None else out


def paint_square(rgb, frame, x0, y0, size, surface, widths=None, simple=False, pending=None):
    """photo_roads for a ground photo over [x0, x0 + size]² of frame with the roads of surface's cached cells (a
    Surface; never waits); as it was when its texels are coarser than PHOTO_ROADS_MAX_TEXEL or no cell is cached.
    Crop rows are painted first (photo_fields: dirt furrows, the photo left between them; a dirt road is left bare,
    since its paint is translucent), golf surfaces (photo_golf), then parking lots, gas-station pads, then the
    carriageways over them.
    simple: the carriageways alone (a stand-in photo)."""
    if surface is None or size / max(rgb.shape[1], 1) > PHOTO_ROADS_MAX_TEXEL:
        return rgb
    roads = _square_roads(frame, x0, y0, size, surface, pending)
    if not simple:          # (simple, a stand-in's: the carriageways alone, photo_roads)
        feats = _square_lots(frame, x0, y0, size, surface)
        rgb = photo_fields(rgb, frame, x0, y0, size, feats, roads, getattr(surface, "urban", None), widths)
        rgb = photo_golf(rgb, frame, x0, y0, size, feats)
        rgb = photo_lots(rgb, frame, x0, y0, size, feats)
        rgb = photo_pads(rgb, frame, x0, y0, size, _square_pads(frame, x0, y0, size, surface))     # (under the roads)
    if not roads:
        return rgb
    return photo_roads(rgb, frame, x0, y0, size, roads, surface.urban, widths, simple=simple)


def photo_corners(rgb, frame, x0, y0, size, pads, before=None):
    """photo_pads for many small pads spread over the photo (the rounded junction corners), each in its own window.
    before: the photo before the roads were painted into rgb: each corner then takes the colour of the painted road
    pixels in its window (their median; the roads are toned, photo_roads), else the flat paved colour. Pure."""
    if not pads:
        return rgb
    h, w = rgb.shape[:2]
    r_, g_, b_, op = PHOTO_ROAD_RGBA["paved"]
    flat = np.array([r_, g_, b_], np.float32)
    out = None
    for r0, c0, a in _windows_px(frame, x0, y0, size, w, h, pads):
        if out is None:
            out = rgb.copy()
        r1, c1 = r0 + a.shape[0], c0 + a.shape[1]
        colour = flat
        if before is not None:
            win = rgb[r0:r1, c0:c1]
            painted = np.any(win != before[r0:r1, c0:c1], axis=2) & (a < 0.5)
            if painted.sum() >= 6:
                colour = np.median(win[painted].astype(np.float32), axis=0)
        sub = (a * op)[..., None]
        out[r0:r1, c0:c1] = np.clip(np.round(out[r0:r1, c0:c1].astype(np.float32) * (1.0 - sub) + colour * sub),
                                    0, 255).astype(np.uint8)
    return rgb if out is None else out


def photo_pads(rgb, frame, x0, y0, size, pads):
    """The photo rgb (H x W x 3, row 0 north, over [x0, x0 + size]² of frame) with the pads painted as the paved roads
    (PHOTO_ROAD_RGBA, in the photo's own pavement tone under them: earthfast.paint_cover_toned, PHOTO_ROAD_TONE; the
    user, 2026-11-11: "utiluze and revfine this method of texturing the ground more"). Pure."""
    h, w = rgb.shape[:2]
    cov = _pads_px(frame, x0, y0, size, w, h, pads) if pads else None
    if cov is None:
        return rgb
    fast.require(231, "pads in the photo's own tone (its 9-value tone row)")
    radius = max(1, int(round(PHOTO_TONE_RADIUS_M * w / size)))
    return fast._ef.paint_cover_toned(np.ascontiguousarray(rgb, np.uint8), np.ascontiguousarray(cov, np.float32),
                                      np.array(PHOTO_ROAD_RGBA["paved"], np.float32),
                                      np.array(PHOTO_ROAD_TONE["paved"], np.float32), radius,
                                      PHOTO_TONE_SAMPLE_LUMA, PHOTO_TONE_SAMPLE_SAT, PHOTO_TONE_GREEN)


def photo_lots(rgb, frame, x0, y0, size, feats_list):
    """The photo with parking-lot polygons painted in the paved roads' colour (under the carriageways). Pure."""
    return photo_pads(rgb, frame, x0, y0, size, lot_pads(feats_list))


def photo_fields(rgb, frame, x0, y0, size, feats_list, roads=None, urban_fn=None, widths=None):
    """The photo with dirt furrows parallel to each field's long side. The photo stays between the furrows. A dirt
    road is left as the photo (its paint is translucent, so a stripe under it would show through). The phase is
    hashed from the feature id, so a field split across photo tiles lines up with the shrubs vegetation.farm_foliage
    plants between the furrows. Pure: rgb is not changed."""
    if rgb is None or size <= 0 or not feats_list:
        return rgb
    h, w = rgb.shape[:2]
    if h == 0 or w == 0:
        return rgb
    px_x, px_y = w / float(size), h / float(size)
    dirt = _dirt_road_mask(frame, x0, y0, size, h, w, roads or [], urban_fn, widths)
    out = None
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        sel = _farmland(feats)
        if sel is None:
            continue
        ids = getattr(feats, "ids", None)
        for i in np.flatnonzero(sel & (np.asarray(feats.kind) == 3)):
            parts = feats.parts(int(i))
            if not parts or not parts[0] or len(parts[0][0]) < 3:
                continue
            ring = np.asarray(parts[0][0], np.float64)
            e, n = mapgeom.enu_xy(feats, frame, ring)
            xy = np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)])
            out = _stripe_field(rgb if out is None else out, out is None, xy, x0, y0, size, px_x, px_y,
                                _field_phase(None if ids is None else ids[int(i)], xy), dirt)
    return rgb if out is None else out


def _dirt_road_mask(frame, x0, y0, size, h, w, feats_list, urban_fn, widths):
    """Bool h x w: pixels on a dirt carriageway and FARM_ROAD_CLEAR_M to either side (roads.road_specs kind "dirt").
    None when there is none. Furrows stop there, short of the road."""
    if not feats_list:
        return None
    px_x, px_y = w / float(size), h / float(size)
    im = draw = None
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        specs = roadsmod.road_specs(feats, urban_fn, widths)
        for i, sp in enumerate(specs):
            if sp is None or sp.kind != "dirt":
                continue
            # one pixel over: PIL's stroke sits a pixel short of the asked width on one side
            width_px = max(1, int(math.ceil((sp.width + 2 * PHOTO_ROAD_EDGE_M + 2 * FARM_ROAD_CLEAR_M)
                                            * max(px_x, px_y))) + 1)
            for ring in roadsmod.ground_parts(feats, i):
                ring = np.asarray(ring, np.float64)
                if len(ring) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, ring)
                c = (np.asarray(e, np.float64) - x0) * px_x
                r = (y0 + size - np.asarray(n, np.float64)) * px_y
                if c.max() < -width_px or c.min() > w + width_px or r.max() < -width_px or r.min() > h + width_px:
                    continue
                if im is None:
                    im = Image.new("L", (w, h), 0)
                    draw = ImageDraw.Draw(im)
                draw.line(list(zip(c.tolist(), r.tolist())), fill=255, width=width_px)
    return None if im is None else np.asarray(im) > 0


def _stripe_field(base, fresh, xy, x0, y0, size, px_x, px_y, phase, dirt=None):
    """base with this field's dirt furrows, or base itself when the field misses it (fresh: the first copy).
    dirt: pixels a dirt road covers, plus the clear strip beside it, left unpainted."""
    h, w = base.shape[:2]
    c = (xy[:, 0] - x0) * px_x
    r = (y0 + size - xy[:, 1]) * px_y
    if c.max() < 0 or c.min() > w or r.max() < 0 or r.min() > h:
        return None if fresh else base
    c0, r0 = max(0, int(math.floor(c.min()))), max(0, int(math.floor(r.min())))
    c1, r1 = min(w, int(math.ceil(c.max())) + 1), min(h, int(math.ceil(r.max())) + 1)
    if c1 <= c0 or r1 <= r0:
        return None if fresh else base
    im = Image.new("L", (c1 - c0, r1 - r0), 0)
    ImageDraw.Draw(im).polygon(list(zip((c - c0).tolist(), (r - r0).tolist())), fill=255)
    mask = np.asarray(im) > 0
    u = _long_axis(xy)
    v0, v1 = -float(u[1]), float(u[0])
    centre = xy.mean(axis=0)
    ee = x0 + (np.arange(c0, c1) + 0.5) / px_x
    nn = y0 + size - (np.arange(r0, r1) + 0.5) / px_y
    across = (nn[:, None] - centre[1]) * v1 + (ee[None, :] - centre[0]) * v0
    furrow = mask & (np.mod(across - phase, FARM_ROW_M) < FARM_FURROW_M)
    if dirt is not None:
        furrow = furrow & ~dirt[r0:r1, c0:c1]
    if not furrow.any():
        return None if fresh else base
    out = base.copy() if fresh else base
    sub = out[r0:r1, c0:c1]
    soil = np.array(FARM_SOIL, np.float32)
    mixed = sub[furrow].astype(np.float32) * (1.0 - FARM_MIX) + soil * FARM_MIX
    sub[furrow] = np.clip(np.round(mixed), 0, 255).astype(np.uint8)
    return out


def photo_ghosts(rgb, frame, x0, y0, size, feats_list, urban_fn=None, widths=None):
    """The photo rgb with its own pavement beside the drawn curbless paved roads (within PHOTO_GHOST_M of their edge,
    coloured like the photo on the road) replaced by the ground beyond it, mirrored (earthfast.photo_ghosts), before
    photo_roads paints the roads in: the map's carriageway is narrower than the photo's (its paved shoulders)
    or a metre or two off it, and the photo's strip showed as a pale band beside one edge (Woodland Park, US-24: the
    photo's pavement 17 m wide, the drawn road 11 m; the user, 2026-10-28). Pure (a new array)."""
    h, w = rgb.shape[:2]
    of = []
    got = _road_segments(frame, x0, y0, size, w, h, feats_list, urban_fn, widths, roads_of=of)
    if got is None:
        return rgb
    segs, hws, _ = got
    road = np.concatenate([a for a, _ in of])
    ghost = np.where(np.concatenate([b for _, b in of]), PHOTO_GHOST_M * w / size, 0.0)
    if not ghost.any():
        return rgb
    fast.require(92, "photo_ghosts")
    out, _ = fast._ef.photo_ghosts(np.ascontiguousarray(rgb, np.uint8), np.ascontiguousarray(segs, np.float64),
                                   np.ascontiguousarray(hws, np.float64), np.ascontiguousarray(ghost, np.float64),
                                   np.ascontiguousarray(road, np.int64), int(road.max()) + 1, PHOTO_GHOST_TOL)
    return out


def road_mask(frame, x0, y0, size, px, surface, widths=None):
    """The built carriageways' coverage (px x px uint8, 255 on the road, antialiased) of a tile photo's square
    [x0, x0 + size]² (its gutter included: the mask lines up with it on UV0), or None (texels coarser than
    ROAD_MASK_MAX_TEXEL, no roads, no cell cached)."""
    if surface is None or size / max(px, 1) > ROAD_MASK_MAX_TEXEL:
        return None
    pads = pads_mask(frame, x0, y0, size, px, _square_pads(frame, x0, y0, size, surface))
    lots = lots_mask(frame, x0, y0, size, px, _square_lots(frame, x0, y0, size, surface))
    feats = _square_roads(frame, x0, y0, size, surface)
    got = _road_segments(frame, x0, y0, size, px, px, feats, surface.urban, widths) if feats else None
    corners = corners_mask(frame, x0, y0, size, px, corner_pads(frame, x0, y0, size, feats, surface.urban, widths)) \
        if feats else None
    out = None
    if got is not None:
        colors = np.array([(255.0, 255.0, 255.0, 1.0 if k in ROAD_MASK_KINDS else 0.0) for k in PHOTO_ROAD_RGBA],
                          np.float32)
        out = fast._ef.paint_lines(np.zeros((px, px, 3), np.uint8), *got, colors)[..., 0]
    for extra in (pads, lots, corners):
        if extra is not None:
            out = extra if out is None else np.maximum(out, extra)
    return None if out is None or not out.any() else out
