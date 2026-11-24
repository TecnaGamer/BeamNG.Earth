"""Procedural storefronts for fictional identities on mapped footprints."""
import math
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

import numpy as np
import entrance_designs
from PIL import Image, ImageDraw, ImageFont
from storeidentity import DINING_STYLES, STYLES


def simplify(ring):
    """Merge straight wall segments, retaining entrance coordinates separately."""
    keep = []
    for i, p in enumerate(ring):
        u, v = p - ring[i - 1], ring[(i + 1) % len(ring)] - p
        if u @ v <= 0 or abs(u[0] * v[1] - u[1] * v[0]) > 0.02 * np.linalg.norm(
            u
        ) * np.linalg.norm(v):
            keep.append(p)
    return np.asarray(keep) if len(keep) >= 3 else ring


def window_bays(profile, width, doors):
    count = int(width / profile["spacing"])
    centers = ((bay + .5) * width / count for bay in range(count))
    return [x for x in centers if all(abs(x - door) >= profile["span"] / 2 + 1.7 for door in doors)]


def elevation(identity, width, height, entries, docks, windows, photo_color=None, observed_brick=False, finish=None):
    restaurant = identity.style in DINING_STYLES
    w, h = max(8, min(1536, round(width * 30))), 192
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)

    def rect(x, y, r, t, color):
        d.rectangle(
            (
                round(x * w / width),
                round(h - t * h / height),
                round(r * w / width),
                round(h - y * h / height),
            ),
            fill=color,
        )

    rect(0, 0, width, height, "#d7cbbb" if restaurant else "#c2b7a3")
    # Running-bond brick on restaurants, expansion joints on warehouse panels.
    brick_h = 0.13 if restaurant else 0.23
    base = height * 0.57 if restaurant else 0.95
    palette = (
        ["#8c5845", "#925c47", "#98644f", "#865540"]
        if restaurant
        else ["#9b9183", "#a29889", "#aca18e", "#a29786"]
    )
    for row in range(math.ceil(base / brick_h)):
        brick_w = 0.39 if restaurant else 0.62
        y = row * brick_h
        for col in range(-1, math.ceil(width / brick_w)):
            x = (col + (row % 2) * 0.5) * brick_w
            rect(
                x,
                y,
                x + brick_w - 0.012,
                min(base, y + brick_h - 0.012),
                palette[(col * 17 + row * 7 + col * row) % 4],
            )
    if finish and not (photo_color or observed_brick):
        # The family cladding starts above a short masonry plinth, including on service walls.
        plinth = min(.65, base)
        if finish == "ribbed_metal":
            rect(0, plinth, width, height, "#a9b2af")
            for x in np.arange(.24, width, .48):
                rect(x, plinth, x + .055, height, "#7e8986")
                rect(x + .055, plinth, x + .09, height, "#c6cbc4")
        elif finish == "timber_siding":
            tones = ("#967252", "#a17b59", "#9b7756")
            for i, y in enumerate(np.arange(plinth, height, .27)):
                rect(0, y, width, min(height, y + .27), tones[i % 3])
                rect(0, y, width, y + .035, "#705740")
        elif finish == "metal_panels":
            rect(0, plinth, width, height, "#b6c0c1")
            for x in np.arange(2.1, width, 2.1):
                rect(x, plinth, x + .055, height, "#808e90")
            for y in np.arange(plinth + 1.05, height, 1.05):
                rect(0, y, width, y + .045, "#899799")
        elif finish == "stone_courses":
            rect(0, plinth, width, height, "#d2c8b7")
            for i, y in enumerate(np.arange(plinth, height, .85)):
                rect(0, y, width, y + .045, "#aa9f8d")
                for x in np.arange((i % 2) * 1.1, width, 2.2):
                    rect(x, y, x + .045, y + .85, "#b6aa97")
    if observed_brick and not photo_color:
        # Warm masonry continues around the public and service sides.
        rect(0, 0, width, height, "#aa8876")
        for row in range(math.ceil(height / 0.17)):
            y = row * 0.17
            for col in range(-1, math.ceil(width / 0.49)):
                x = (col + 0.5 * (row % 2)) * 0.49
                rect(
                    x,
                    y,
                    x + 0.47,
                    min(height, y + 0.155),
                    ("#945f4d", "#a16853", "#965e4c")[(row + col * 3) % 3],
                )
    if photo_color:
        import PIL.ImageColor

        rgb = np.array(PIL.ImageColor.getrgb(photo_color))
        mortar = tuple(np.clip(rgb * 0.7 + 75, 0, 255).astype(int))
        rect(0, 0, width, height, mortar)
        for row in range(math.ceil(height / 0.12)):
            for col in range(-1, math.ceil(width / 0.32)):
                x = (col + 0.5 * (row % 2)) * 0.32
                tone = 0.92 + 0.035 * ((row * 13 + col * 7) % 5)
                rect(
                    x,
                    row * 0.12,
                    x + 0.304,
                    min(height, (row + 1) * 0.12 - 0.012),
                    tuple(np.clip(rgb * tone, 0, 255).astype(int)),
                )
    def glass(center, span, sill, top, door):
        left, right = center - span / 2, center + span / 2
        rect(left, sill, right, top, "#393c39")
        count = max(2, round(span / 1.18))
        for k in range(count):
            x = left + k * span / count + 0.055
            end = left + (k + 1) * span / count - 0.055
            for band in range(18):
                low = sill + 0.09 + (top - sill - 0.18) * band / 18
                tone = int(52 + band * 3)
                rect(
                    x,
                    low,
                    end,
                    low + (top - sill - 0.18) / 18,
                    (tone - 9, tone + 8, tone + 12),
                )
            if restaurant:
                # One shared sky reflection runs across the panes at a shallow angle.
                def pixel(x, y):
                    return (round(x * w / width), round(h - y * h / height))

                def horizon(x):
                    return sill + (top - sill) * (0.54 + 0.16 * (x - left) / span)

                d.polygon(
                    [
                        pixel(x, horizon(x)),
                        pixel(end, horizon(end)),
                        pixel(end, horizon(end) + 0.12),
                        pixel(x, horizon(x) + 0.12),
                    ],
                    fill="#849799",
                )
            if door and k in (count // 2 - 1, count // 2):
                rect(x, 0.17, end, 0.29, "#9b9c94")
                rect(x + 0.09, 1.00, end - 0.09, 1.05, "#c1c5bb")
        rect(left, top - 0.38, right, top - 0.32, "#444744")
        if sill:
            rect(left - 0.06, sill - 0.09, right + 0.06, sill, "#d6c5ad")

    if entries or windows:
        rect(0, height * .80, width, height * .93, identity.paint)
    profile = STYLES[identity.style]["windows"]
    for x in windows:
        half = min(profile["span"] / 2, x, width - x)
        left, right = x - half, x + half
        top = min(profile["head"], height * .79)
        sill = min(profile["sill"], top * .75)
        glass(x, 2 * half, sill, top, False)
        trim = profile["trim"]
        if trim == "awning":
            rect(left - .12, top, right + .12, top + .36, identity.paint)
            for stripe in np.arange(left, right - .12, .55):
                rect(stripe, top, stripe + .13, top + .36, identity.ink)
        elif trim == "counter":
            rect(left - .15, sill - .18, right + .15, sill, identity.paint)
            rect(left - .15, sill - .05, right + .15, sill + .04, "#dad5c9")
        elif trim == "timber":
            for jamb in (left, right - .17):
                rect(jamb, sill - .12, jamb + .17, top + .17, "#624934")
            rect(left - .13, top, right + .13, top + .2, "#765a40")
        elif trim == "pilaster":
            for jamb in (left - .13, right):
                rect(jamb, 0, jamb + .13, top + .27, "#e2dccf")
            rect(left - .18, top + .15, right + .18, top + .27, "#d0c6b8")
        elif trim == "transom":
            rect(left, top - (top - sill) * .28, right, top - (top - sill) * .28 + .07, "#384746")
        elif trim == "rail":
            rect(left, sill + .16, right, sill + .23, "#c6c8bc")
        elif trim == "lintel":
            rect(left - .11, top, right + .11, top + .18, "#c8b69f")
    for x in entries:
        glass(x, min(2.8, width * .72), .02, min(2.65, height*.76), True)
    for x in docks:
        span = min(3.1, width * 0.8)
        rect(x - span / 2, 0, x + span / 2, 3.6, "#494944")
        for y in np.arange(0.35, 3.45, 0.22):
            rect(x - span / 2 + 0.14, y, x + span / 2 - 0.14, y + 0.18, "#99998e")
    return img


SIGN_FONT = Path(__file__).resolve().parents[1] / "fonts" / "DejaVuSans-Bold.ttf"
_SIGN_FONTS = ContextVar("storefront_sign_fonts", default=None)


@contextmanager
def sign_font_scope():
    """Reuse sized faces only within one synchronous storefront export."""
    load = lru_cache(maxsize=64)(lambda size: ImageFont.truetype(SIGN_FONT, size))
    token = _SIGN_FONTS.set(load)
    try:
        yield
    finally:
        _SIGN_FONTS.reset(token)


def _sign_font(size):
    load = _SIGN_FONTS.get()
    return ImageFont.truetype(SIGN_FONT, size) if load is None else load(size)


def sign_image(identity, aspect=4.5, height_m=1., surface="facade"):
    height = 128
    width = max(64, min(1200, round(height * aspect)))
    image = Image.new("RGB", (width, height), identity.paint)
    draw = ImageDraw.Draw(image)
    margin = min(16, width // 12)
    if identity.layout == "frame":
        draw.rounded_rectangle((3, 3, width-4, height-4), radius=9, outline=identity.ink, width=3)
    elif identity.layout == "rail":
        draw.rectangle((0, height-11, width-1, height-6), fill=identity.ink)
    elif identity.layout == "cap":
        draw.rectangle((0, 0, width-1, 7), fill=identity.ink)
        draw.rectangle((margin, 10, width//3, 12), fill=identity.ink)
    left = margin
    if identity.mark and width >= 180:
        cell = min(15, width // 36)
        top = (height - 5 * cell) // 2
        for row, pattern in enumerate(identity.mark.split("/")):
            for col, pixel in enumerate(pattern):
                if pixel == "#":
                    x, y = left + col*cell, top + row*cell
                    draw.rectangle((x, y, x+cell-1, y+cell-1), fill=identity.ink)
        left += 5 * cell + margin
        if identity.layout == "split":
            draw.line((left, height//5, left, 4*height//5), fill=identity.ink, width=3)
            left += margin
    base = _sign_font(90)
    size = max(1, int(90 * min(1, (width-left-margin) / max(1,base.getlength(identity.label)))))
    draw.text(((left+width-margin)/2, height/2), identity.label,
              font=_sign_font(size), anchor="mm", fill=identity.ink)
    return image


def tenant_banner(identity, aspect, height):
    """A mall advertising panel distinguishes an indoor tenant from a dedicated external entrance."""
    art = sign_image(identity, aspect*1.3, height)
    footer = max(18,round(art.width/aspect)-art.height)
    image = Image.new("RGB",(art.width,art.height+footer),"#253139")
    image.paste(art,(0,0))
    ImageDraw.Draw(image).text((image.width/2,art.height+footer/2),"INSIDE THE MALL",
                               font=_sign_font(max(9,int(footer*.66))),
                               fill="white",anchor="mm")
    return image


def directory_image(rows):
    image = Image.new("RGB",(512,256),"#263338")
    draw = ImageDraw.Draw(image)
    draw.text((16,8),"DIRECTORY",font=_sign_font(28),fill="white")
    line = (image.height-52)//len(rows)
    for i,(unit,name) in enumerate(rows):
        y = 50+i*line
        draw.line((16,y-2,496,y-2),fill="#829194")
        label = f"{unit}   {name}"
        font = _sign_font(min(line-5,38))
        size = max(1, int(font.size*min(1,478/max(1,font.getlength(label)))))
        draw.text((16,y+2),label,font=_sign_font(size),fill="white")
    return image


def carhop_row(ring, approach, identity, mesh, texture):
    """Open parking canopy, with an order screen on the driver side of each pull-in bay."""
    from buildings import oriented_box
    center, axis, length, depth = oriented_box(ring)
    forward = np.array([-axis[1], axis[0]])
    if forward @ approach > 0:
        axis, forward = -axis, -forward
    colors = {}
    def box(left, right, near, far, bottom, top, color):
        if color not in colors:
            colors[color] = texture(Image.new("RGB", (4,4), color))
        vertices = [(*(center+x*axis+y*forward), z) for z in (bottom,top)
                    for y in (near,far) for x in (left,right)]
        for corners in ((0,2,3,1),(4,5,7,6),(0,1,5,4),(2,6,7,3),(0,4,6,2),(1,3,7,5)):
            mesh([vertices[corners[j]] for j in (0,1,2,0,2,3)], colors[color], [(0,0)]*6)
    box(-length,length,-depth,depth,3.12,3.34,"#e6dfce")
    box(-length,length,-depth,depth,3.34,3.57,identity.paint)
    count = max(1, int(2*length/3.35))
    pitch = (2*length-.30)/count
    for i in range(count+1):
        x = -length+.15+i*pitch
        box(x-.05,x+.05,-depth,depth,.015,.027,"#ece8ce")
        if i == count:
            break
        box(x-.09,x+.09,.30,.50,0,3.12,"#aab3b4")
        box(x-.09,x+.09,-depth+.08,depth-.08,2.97,3.12,identity.ink)
        for direction in ((1, -1) if depth >= 4.6 else (1,)):
            # Opposing cars have their drivers on opposite sides of the bay.
            post = x if direction == 1 else x+pitch
            y = -direction * (depth/2 if depth >= 4.6 else .9)
            box(post-.07,post+.07,y-.07,y+.07,0,1.72,"#73848a")
            box(post-.12,post+.12,y-.38,y+.38,1.10,1.96,"#253f52")
            face = post+direction*.14
            box(face-.01,face+.01,y-.31,y+.31,1.23,1.86,"#eae3c9")
            box(face-.018,face+.018,y-.31,y+.31,1.71,1.86,identity.paint)


def wall_fixtures(ring, points):
    """Project mapped doors and docks onto wall-local distances in metres."""
    edges = np.roll(ring, -1, axis=0) - ring
    lengths = np.linalg.norm(edges, axis=1)
    axes = edges / lengths[:, None]
    entries = [[] for _ in ring]
    docks = [[] for _ in ring]
    for point, service in points:
        along = np.clip(np.sum((point - ring) * axes, axis=1), 0, lengths)
        distance = np.linalg.norm(ring + along[:, None] * axes - point, axis=1)
        choices = np.flatnonzero(distance <= distance.min() + 0.08)
        i = max(
            choices,
            key=lambda j: min(along[j], lengths[j] - along[j]) + 0.001 * lengths[j],
        )
        (docks if service else entries)[i].append(along[i])
    return entries, docks


def pharmacy_entrance(front, lengths, normals, docks, arrival, street):
    """Infer one public entrance from a clipped corner or two sides facing the approach."""
    def turn(a, b):
        return a[0]*b[1] - a[1]*b[0]

    chamfers = []
    for i, width in enumerate(lengths):
        before, after = (i-1) % len(lengths), (i+1) % len(lengths)
        if docks[i] or not 3.5 <= width <= 12 or min(lengths[before], lengths[after]) < 1.4*width:
            continue
        if all(.30 < turn(a, b) < .96 and a @ b > 0 for a,b in
               ((normals[before], normals[i]), (normals[i], normals[after]))):
            alignment = normals[i] @ arrival
            if alignment > .55:
                chamfers.append((alignment, i))
    chamfers.sort(reverse=True)
    if chamfers and (len(chamfers) == 1 or chamfers[0][0]-chamfers[1][0] > .15):
        wall = chamfers[0][1]
        return wall, lengths[wall]/2

    # A square corner supports a near-end doorway when a second public face
    # distinguishes that end. Symmetric approaches retain a centred entrance.
    ends = []
    for previous in (True, False):
        side = (front + (-1 if previous else 1)) % len(lengths)
        a,b = (normals[side],normals[front]) if previous else (normals[front],normals[side])
        score = max(normals[side] @ arrival, normals[side] @ street)
        if not docks[front] and not docks[side] and lengths[side] >= 7 and turn(a,b) > .55 and score > .25:
            ends.append((score, previous))
    ends.sort(reverse=True)
    if lengths[front] >= 12 and ends and (len(ends)==1 or ends[0][0]-ends[1][0] > .15):
        inset = min(5., lengths[front]*.20)
        return front, inset if ends[0][1] else lengths[front]-inset
    return front, lengths[front]/2


def build(
    identity, variant, ring, street, height, points, mesh, texture, mapped_brick=False, photos=(), entrance_direction=None, *, use_family_finish=True
):
    restaurant = identity.style in DINING_STYLES
    ring = simplify(ring)
    photo_colors = {}
    conflicting_walls = set()
    for ends, color in photos:
        for i, a in enumerate(ring):
            segment = np.array([a, ring[(i + 1) % len(ring)]])
            if any(
                np.max(np.linalg.norm(segment - order, axis=1)) < 0.5
                for order in (ends, ends[::-1])
            ):
                if (
                    i in photo_colors
                    and color
                    and photo_colors[i]
                    and photo_colors[i] != color
                ):
                    conflicting_walls.add(i)
                if color or i not in photo_colors:
                    photo_colors[i] = color
                break
        else:
            warnings.warn(
                f"Skipped {identity.label} photo observation because its wall endpoints no longer match the footprint"
            )
    for i in conflicting_walls:
        # Both observations support brick. Use the default brick tone when their colors disagree.
        photo_colors[i] = None
    edges = np.roll(ring, -1, axis=0) - ring
    lengths = np.linalg.norm(edges, axis=1)
    axes = edges / lengths[:, None]
    normals = np.column_stack((axes[:, 1], -axes[:, 0]))
    score = np.maximum(0, normals @ street) ** 2 * np.minimum(lengths, 45)
    entries, docks = wall_fixtures(ring, points)
    front = int(np.argmax(score if entrance_direction is None else
                          np.maximum(0, normals @ entrance_direction) ** 2 * np.minimum(lengths, 45)))
    if any(entries):
        front = max(
            (i for i in range(len(ring)) if entries[i]),
            key=lambda i: (len(entries[i]), score[i], lengths[i]),
        )
    if not any(entries) and identity.style == "corner_shop":
        front, along = pharmacy_entrance(front, lengths, normals, docks,
                                         street if entrance_direction is None else entrance_direction, street)
        entries[front] = [along]
    elif not any(entries):
        # A long unobstructed customer wall supports a paired warehouse entrance layout.
        fractions = (0.25, 0.75) if STYLES[identity.style].get("wide_frontage") and lengths[front] >= 42 else (0.5,)
        entries[front] = [f * lengths[front] for f in fractions]
    # The entrance tower already has a sign. Choose one substantial second wall visible
    # from the addressed street or an approach around the public corner.
    street_front = int(np.argmax(score))
    secondary = None
    if street_front != front and lengths[street_front] >= 7 and not docks[street_front]:
        secondary = street_front
    elif restaurant:
        sides = [i for i in range(len(ring)) if i != front and lengths[i] >= 7
                 and not docks[i] and abs(normals[i] @ normals[front]) < 0.55]
        if sides:
            secondary = max(sides, key=lambda i: (round(float(normals[i] @ street), 2),
                                                   lengths[i], -np.linalg.norm(ring[i] - ring[front])))
    uv = [(0, 1), (1, 1), (1, 0), (0, 1), (1, 0), (0, 0)]

    def paint_wall(i):
        width = lengths[i]

        def face(left, right, depth, bottom, top, material, paint=False):
            a = ring[i] + axes[i] * left + normals[i] * depth
            b = ring[i] + axes[i] * right + normals[i] * depth
            mesh(
                [
                    (*a, bottom),
                    (*b, bottom),
                    (*b, top),
                    (*a, bottom),
                    (*b, top),
                    (*a, top),
                ],
                material,
                uv if paint else None,
            )

        door = entries[i]
        design = entrance_designs.Entrance(variant, identity.paint, identity.ink, STYLES[identity.style]["span"])
        fitted = []
        if i == front and door and not docks[i]:
            fitted = [(x, entrance_designs.fitted_span(design, width, x, door)) for x in dict.fromkeys(door)]
            if not all(span for _, span in fitted):
                fitted = []
        for center, span in fitted:
            def vertex(x, depth, z):
                return (*(ring[i] + axes[i]*(center+x) + normals[i]*depth), z)
            entrance_designs.draw(design, span, height, vertex, mesh, texture, lambda aspect, h: sign_image(identity, aspect, h))

        public = bool(door) or (restaurant and normals[i] @ normals[front] >= -0.15)
        profile = STYLES[identity.style]["windows"]
        windows = window_bays(profile, width, door) if public and not docks[i] else []
        # Add shallow window furniture only in open bays outside the fitted entrance.
        trim = profile["trim"]
        if windows and trim in {"awning", "counter"}:
            half_span = profile["span"] / 2 + .1
            detail_bays = [x for x in windows if all(
                abs(x - center) > half_span + span / 2 for center, span in fitted)]
            if detail_bays:
                swatch = Image.new("RGB", (32, 4), identity.paint)
                if trim == "awning":
                    brush = ImageDraw.Draw(swatch)
                    for stripe in range(0, 32, 8):
                        brush.rectangle((stripe, 0, stripe + 2, 3), fill=identity.ink)
                art = texture(swatch)
                high = profile["head"] + .36 if trim == "awning" else profile["sill"]
                reach, drop = (.72, .28) if trim == "awning" else (.32, 0.)
                for x in detail_bays:
                    left, right = x - half_span, x + half_span
                    near = ring[i] + axes[i] * left + normals[i] * .03
                    far = near + normals[i] * reach
                    across = axes[i] * (right - left)
                    a, b = (*near, high), (*(near + across), high)
                    c, d = (*(far + across), high - drop), (*far, high - drop)
                    mesh([d, c, b, d, b, a], art, uv)
                    face(left, right, .03 + reach, high - drop - .12, high - drop, art, True)
        image = elevation(identity, width, height, door, docks[i], windows,
                          photo_colors.get(i), mapped_brick or i in photo_colors,
                          STYLES[identity.style].get("wall_finish") if use_family_finish else None)
        face(0, width, .018, 0, height, texture(image), True)
        if (i == front and not fitted) or i == secondary:
            span = min(width*.84, 12.)
            center = width/2
            tall = min(height*.25, span/4.5)
            top = height*.97
            face(center-span/2, center+span/2, .075, top-tall, top,
                 texture(sign_image(identity, span/tall, tall)), True)

    paint_wall(front)
    for wall_index in range(len(ring)):
        if wall_index != front:
            paint_wall(wall_index)


def pack_elevations(images):
    """One atlas per store so walls share a material. Duplicate border pixels for filtering."""
    pad = 4
    positions = [None] * len(images)
    x = y = row_height = used_width = 0
    for i in sorted(range(len(images)), key=lambda i: images[i].height, reverse=True):
        w, h = images[i].size
        if x and x + w + 2 * pad > 2048:
            y += row_height
            x = row_height = 0
        positions[i] = (x + pad, y + pad, w, h)
        x += w + 2 * pad
        used_width = max(used_width, x)
        row_height = max(row_height, h + 2 * pad)
    atlas = Image.new("RGB", (used_width, y + row_height))
    for img, (x, y, w, h) in zip(images, positions):
        border = np.pad(np.asarray(img), ((pad, pad), (pad, pad), (0, 0)), mode="edge")
        atlas.paste(Image.fromarray(border), (x - pad, y - pad))
    return atlas, positions
