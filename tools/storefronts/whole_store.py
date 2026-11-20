"""Generate a complete exterior elevation for each wall, at a consistent physical scale."""

import math
import warnings
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

CHAINS = {"Walmart", "The Home Depot", "Lowe's", "Chick-fil-A"}
ACCENTS = {
    "Walmart": "#244d75",
    "The Home Depot": "#ac5426",
    "Lowe's": "#233e63",
    "Chick-fil-A": "#54443d",
}


# wall finish, panel color, sign background, sign lettering
DINING = {
    "KFC": ("stripe", "#ebe5d8", "#b92531", "#fff5ef"),
    "Wendy's": ("timber", "#a97453", "#a72638", "#fff4e7"),
    "Burger King": ("timber", "#a87958", "#e7c897", "#97382d"),
    "Taco Bell": ("siding", "#e1d7c6", "#564164", "#fff8e9"),
    "Starbucks": ("panel", "#e2dfd6", "#24463d", "#fff8ed"),
    "Arby's": ("siding", "#bdb0a1", "#952a32", "#fff9ef"),
}
DINING.update(
    {
        "Subway": ("panel", "#c7bba4", "#236347", "#f1e644"),
        "Sonic Drive-In": ("panel", "#e7dbbf", "#cf352c", "#ffdc55"),
        "Popeyes": ("masonry", "#dac5ae", "#f2dfc8", "#bb4f13"),
        "Dunkin'": ("panel", "#eee3d1", "#f7ead8", "#b65319"),
        "Domino's": ("panel", "#ece0cf", "#a72535", "#ffffff"),
        "Pizza Hut": ("panel", "#dedbd4", "#333436", "#ffffff"),
    }
)
MARKETS = {
    "Kroger": "#275691",
    "Target": "#be2532",
    "Dollar General": "#e5b832",
    "Ace Hardware": "#9d2b2e",
    "CVS": "#b22336",
    "Walgreens": "#a82634",
    "ALDI": "#223d67",
    "Publix": "#41694b",
    "Costco": "#a33335",
    "Sam's Club": "#206592",
}
CHAINS.update(DINING, MARKETS)
ACCENTS.update(MARKETS)

# Height share of the upper wall, fascia paint, and narrow lower-edge trim.
FASCIA = {
    "ALDI": (0.24, "#183d71", "#ed9938"),
    "Costco": (0.16, "#b52b36", "#245380"),
    "Sam's Club": (0.29, "#156397", "#64aec0"),
    "Kroger": (0.16, "#254f87", "#d9ded8"),
    "Target": (0.22, "#bd2634", "#e5d9c8"),
    "Walmart": (0.20, "#245b87", "#e6b442"),
    "Dollar General": (0.18, "#343330", "#edbf30"),
    "Ace Hardware": (0.16, "#a93035", "#593b36"),
    "CVS": (0.10, "#b32a3c", "#e0c9ba"),
    "Walgreens": (0.09, "#9c3440", "#d2b59e"),
    "Burger King": (0.13, "#a33a28", "#dd943b"),
    "Wendy's": (0.13, "#ae303d", "#58423b"),
    "Taco Bell": (0.11, "#674973", "#ba9b80"),
    "Chick-fil-A": (0.085, "#973140", "#58483c"),
    "KFC": (0.14, "#b62431", "#eee3d3"),
    "Starbucks": (0.13, "#285446", "#484c43"),
    "Arby's": (0.14, "#ac343c", "#503f39"),
    "Subway": (0.15, "#236144", "#e5be38"),
    "Sonic Drive-In": (0.12, "#bc3834", "#eabf3b"),
    "Popeyes": (0.17, "#be5423", "#943831"),
    "Dunkin'": (0.13, "#d9792a", "#bd4678"),
    "Domino's": (0.15, "#246088", "#b83642"),
    "Pizza Hut": (0.15, "#a12733", "#363633"),
}


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


# span, sill, head height, maximum distance around a public corner (metres)
CAFE_GLAZING = {
    "Starbucks": (5.6, 0.22, 3.05, 6.4),
    "Dunkin'": (3.45, 0.36, 2.76, 0),
    "Domino's": (4.25, 0.67, 2.65, 0),
    "Burger King": (4.4, 0.58, 2.85, 3.8),
    "Wendy's": (5.25, 0.35, 2.95, 5.1),
    "Subway": (3.3, 0.69, 2.67, 0),
    "Sonic Drive-In": (2.5, 0.85, 2.5, 0),
    "Popeyes": (3.25, 0.52, 2.7, 3.2),
    "Pizza Hut": (4.75, 0.35, 2.88, 0),
    "KFC": (4.15, 0.38, 2.85, 3.2),
    "Arby's": (3.9, 0.54, 2.8, 3.9),
}


def shopfront_glass(name, width, doors, at_start, at_end):
    """Fit glass beside actual doors first. Keep space for jambs and solid service walls."""
    span, sill, head, corner = CAFE_GLAZING[name]
    bays = []
    if doors:
        edges = [(0.35, width - 0.35)]
        for door in sorted(doors):
            edges = [
                piece
                for a, b in edges
                for piece in ((a, min(b, door - 1.85)), (max(a, door + 1.85), b))
                if piece[1] > piece[0]
            ]
        for a, b in edges:
            fitted = min(span, b - a)
            if fitted < 1.5:
                continue
            center = min(
                (a + fitted / 2, b - fitted / 2),
                key=lambda c: min(abs(c - d) for d in doors),
            )
            bays.append((center, fitted, sill, head))
        if name == "Domino's" and len(bays) > 1:
            bays = [max(bays, key=lambda bay: bay[1])]
    elif corner and (at_start or at_end):
        fitted = min(width - 0.85, corner - 0.8, span)
        if fitted >= 1.5:
            center = 0.4 + fitted / 2 if at_start else width - 0.4 - fitted / 2
            bays.append((center, fitted, sill, head))
    return bays


def elevation(
    name,
    width,
    height,
    entries,
    docks,
    dining,
    shop_bays=(),
    photo_color=None,
    observed_brick=False,
):
    restaurant = name == "Chick-fil-A" or name in DINING
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
    if name in DINING:
        finish, field, _, _ = DINING[name]
        rect(0, base if finish == "masonry" else 0.42, width, height, field)
        if finish in {"timber", "siding"}:
            for y in np.arange(0.48, height, 0.23 if finish == "timber" else 0.35):
                rect(
                    0,
                    y,
                    width,
                    y + 0.025,
                    "#6f5848" if finish == "timber" else "#ac9e90",
                )
    if (name == "Walgreens" or observed_brick) and not photo_color:
        # Warm masonry continues around the public and service sides.
        rect(0, 0, width, height, "#aa8876" if observed_brick else "#b48a72")
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
    if name in DINING:
        _, _, accent, _ = DINING[name]
        if finish == "stripe" and (entries or dining):
            for x in np.arange(0, min(width, 5.6), 1.05):
                rect(x, 0.42, min(x + 0.5, width), height, accent)
        if name == "Wendy's" and entries:
            x = entries[0]
            rect(max(0, x - 1.9), 0, min(width, x + 1.9), height, accent)
    if not restaurant and name not in {"CVS", "Walgreens"}:
        if not observed_brick:
            for x in np.arange(3.6, width, 4.8):
                rect(x, base, x + 0.045, height, "#998f82")
        rect(0, height * 0.77, width, height * 0.80, ACCENTS[name])
    if name in {"The Home Depot", "Lowe's"}:
        paint = "#dc671e" if name == "The Home Depot" else "#174b85"
        rect(0, height * 0.70, width, height - 0.22, paint)
        for x in entries:
            rect(x - 4.4, base, x + 4.4, height - 0.22, paint)
    if entries and name in {
        "Target",
        "Sam's Club",
        "ALDI",
        "Walgreens",
        "CVS",
        "Dollar General",
    }:
        color = {
            "Target": "#c92b37",
            "Sam's Club": "#266394",
            "ALDI": "#ae784c",
            "Walgreens": "#985e4a",
            "CVS": "#b48670",
            "Dollar General": "#534f48",
        }[name]
        for x in entries:
            rect(x - 4.2, 0, x + 4.2, height, color)
    if name in {"Pizza Hut", "Domino's"}:
        rect(0, height - 0.62, width, height, DINING[name][2])
    if name == "Costco" and not observed_brick:
        # Corrugated warehouse siding reads as vertical lines at street distance.
        rect(0, 1.15, width, height, "#d4cbb9")
        for x in np.arange(0.22, width, 0.46):
            rect(x, 1.15, x + 0.025, height, "#b9b1a1")
    if name == "Publix" and not observed_brick:
        rect(0, 0.3, width, height * 0.64, "#ac8170")
        for row in range(math.ceil((height * 0.64 - 0.3) / 0.18)):
            for col in range(-1, math.ceil(width / 0.50)):
                x = (col + (0.5 if row % 2 else 0)) * 0.50
                y = 0.3 + row * 0.18
                rect(
                    x,
                    y,
                    x + 0.48,
                    y + 0.165,
                    "#956958" if (row + col) % 3 else "#9f715e",
                )
    if name == "Burger King":
        for x in entries:
            rect(max(0, x - 2.05), 0, min(width, x + 2.05), 3.22, "#a53229")
    if name == "Starbucks":
        rect(0, height - 0.52, width, height, "#353736")
    if name == "Arby's" and (entries or dining):
        rect(0, 2.90, width, 3.13, "#af3233")
    if name in FASCIA:
        share, paint, trim = FASCIA[name]
        bottom = height * (1 - share) - 0.14
        rect(0, bottom, width, height - 0.14, paint)
        rect(0, bottom - 0.08, width, bottom, trim)
    rect(0, height - 0.15, width, height, "#514e49")

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
            if name in CAFE_GLAZING:
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

    for center, span, sill, head in shop_bays:
        glass(center, span, sill, head, False)
    for x in dining:
        if name == "Walgreens":
            glass(x, 3.15, 0.48, height * 0.76, False)
        elif name == "CVS":
            glass(x, 2.25, 0.85, 3.15, False)
        else:
            glass(x, 3, 0.66, 2.78, False)
    for x in entries:
        glass(
            x,
            min(3.2 if restaurant else 6, width * 0.9),
            0.02,
            2.65 if restaurant else 3.45,
            True,
        )
    if name in {"Kroger", "Ace Hardware", "Target"}:
        for x in entries:
            for offset in (-4.9, 4.9):
                if 1.5 < x + offset < width - 1.5:
                    glass(x + offset, 3.0, 0.46, 3.36, False)
    if name == "ALDI":
        for x in entries:
            for offset in (-4.6, 4.6):
                if 1.45 < x + offset < width - 1.45:
                    glass(x + offset, 2.8, 0.32, height * 0.79, False)
    if name == "Kroger" and entries and width > 16:
        # A fanlight above the public entrance gives the grocery bay its arched profile.
        mid = width / 2
        for offset in np.linspace(-3, 3, 120):
            arc = 1.30 * math.sqrt(max(0, 1 - (offset / 3) ** 2))
            rect(mid + offset, 3.85, mid + offset + 0.055, 3.88 + arc, "#617b84")
        for offset in (-2, -1, 0, 1, 2):
            rect(
                mid + offset,
                3.85,
                mid + offset + 0.05,
                3.87 + 1.30 * math.sqrt(1 - (offset / 3) ** 2),
                "#393e3f",
            )
    for x in docks:
        span = min(3.1, width * 0.8)
        rect(x - span / 2, 0, x + span / 2, 3.6, "#494944")
        for y in np.arange(0.35, 3.45, 0.22):
            rect(x - span / 2 + 0.14, y, x + span / 2 - 0.14, y + 0.18, "#99998e")
    return img


def sign_image(name, aspect=4.5):
    colors = {
        "McDonald's": ("#ac2825", "#ffcc32"),
        "Chick-fil-A": ("#ede6d7", "#ad2039"),
        "Walmart": ("#24527d", "#ffffff"),
        "Lowe's": ("#233e63", "#ffffff"),
        "The Home Depot": ("#d06529", "#fff5e8"),
    }
    colors.update({k: v[2:] for k, v in DINING.items()})
    colors.update({k: (v, "#ffffff") for k, v in MARKETS.items()})
    colors.update(
        {
            "Costco": ("#eae4d9", "#b32732"),
            "Publix": ("#f0e9d7", "#28613b"),
            "CVS": ("#f3ece1", "#b92337"),
            "Walgreens": ("#eee6d7", "#aa2034"),
        }
    )
    colors["Dollar General"] = ("#e7bd34", "#242620")
    bg, fg = colors[name]
    width = max(12, round(200 * aspect))
    img = Image.new("RGB", (width, 200), bg)
    d = ImageDraw.Draw(img)
    face = Path(__file__).resolve().parents[1] / "fonts" / "DejaVuSans-Bold.ttf"
    left, right = width * 0.035, width * 0.965
    if name == "Target":
        left = width * 0.245
    if name == "Walmart":
        right = width * 0.763
    base = ImageFont.truetype(face, 160)
    x0, y0, x1, y1 = base.getbbox(name)
    size = max(1, int(160 * min((right - left) / (x1 - x0), 168 / (y1 - y0))))
    font = ImageFont.truetype(face, size)
    x0, y0, x1, y1 = font.getbbox(name)
    d.text(
        ((left + right - x0 - x1) / 2, (200 - y0 - y1) / 2), name, fill=fg, font=font
    )
    if name == "Costco":
        d.rectangle((width * 0.18, 184, width * 0.82, 189), fill="#244c78")
    if name == "Target":
        for r, c in ((67, "white"), (44, bg), (22, "white")):
            radius = min(1., width / 900) * r
            cx = width * .15
            d.ellipse((cx - radius, 100 - radius, cx + radius, 100 + radius), fill=c)
    if name == "Walmart":
        for angle in range(0, 360, 60):
            a = math.radians(angle)
            d.line(
                [(width * .87 + math.sin(a) * r, 100 + math.cos(a) * r) for r in (min(21, width*.025), min(54, width*.065))],
                fill="#ffc83d",
                width=13,
            )
    return img


def build(
    name, ring, street, height, points, mesh, texture, mapped_brick=False, photos=()
):
    restaurant = name == "Chick-fil-A" or name in DINING
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
                f"Skipped {name} photo observation because its wall endpoints no longer match the footprint"
            )
    for i in conflicting_walls:
        # Both observations support brick. Use the default brick tone when their colors disagree.
        photo_colors[i] = None
    edges = np.roll(ring, -1, axis=0) - ring
    lengths = np.linalg.norm(edges, axis=1)
    axes = edges / lengths[:, None]
    normals = np.column_stack((axes[:, 1], -axes[:, 0]))
    score = np.maximum(0, normals @ street) ** 2 * np.minimum(lengths, 45)
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
    front = int(np.argmax(score))
    if any(entries):
        front = max(
            (i for i in range(len(ring)) if entries[i]),
            key=lambda i: (len(entries[i]), lengths[i]),
        )
    if not any(entries):
        # A long unobstructed customer wall supports a paired warehouse entrance layout.
        fractions = (0.25, 0.75) if not restaurant and lengths[front] >= 42 else (0.5,)
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

        def block(left, right, depth, bottom, top, material):
            a = ring[i] + axes[i] * left
            b = ring[i] + axes[i] * right
            p, q = a + normals[i] * depth, b + normals[i] * depth
            face(left, right, depth, bottom, top, material)
            for c, e in ((a, p), (q, b), (b, a)):
                mesh(
                    [
                        (*c, bottom),
                        (*e, bottom),
                        (*e, top),
                        (*c, bottom),
                        (*e, top),
                        (*c, top),
                    ],
                    material,
                )
            for z in (top, bottom):
                mesh([(*a, z), (*b, z), (*q, z), (*a, z), (*q, z), (*p, z)], material)

        door = entries[i]
        public = bool(door) or (restaurant and normals[i] @ normals[front] >= -0.15)
        dining = (
            [
                x
                for x in np.arange(2.25, width - 1.6, 4.4)
                if all(abs(x - e) > 3.6 for e in door)
            ]
            if public and restaurant
            else []
        )
        shop_bays = []
        if name in CAFE_GLAZING:
            dining = []
            if not docks[i]:
                shop_bays = shopfront_glass(
                    name,
                    width,
                    door,
                    i == (front + 1) % len(ring),
                    i == (front - 1) % len(ring),
                )
        if name in {"CVS", "Walgreens"} and not docks[i]:
            # Keep windows on the customer approach and the near end of an adjoining wall.
            positions = np.arange(
                2.05, width - 1.65, 3.6 if name == "Walgreens" else 4.7
            )
            near_start = i == (front + 1) % len(ring)
            near_end = i == (front - 1) % len(ring)
            dining = [
                x
                for x in positions
                if (
                    door
                    or i == front
                    or near_start
                    and x < 6
                    or near_end
                    and width - x < 6
                )
                and all(abs(x - e) > 4.9 for e in door)
            ]
        if i == front:
            # Model the entrance as a roof volume extending back into the footprint.
            # Width share, maximum width, interior depth, rise and roof pitch vary by format.
            profiles = {
                "Starbucks": (0.47, 10, 5.8, 1.05, 0.07),
                "Burger King": (0.34, 7.2, 4, 1.22, 0),
                "Taco Bell": (0.28, 5.6, 4.3, 2.10, 0),
                "Wendy's": (0.31, 6.4, 3.6, 1.50, 0),
                "Chick-fil-A": (0.56, 12.5, 6.7, 1.25, 0.075),
                "KFC": (0.39, 8, 4.5, 1.3, 0),
                "CVS": (0.68, 17, 3.6, 0.72, 0),
                "Walgreens": (0.38, 12, 5, 1.65, 0),
                "Subway": (0.74, 16, 6, 0.32, 0),
                "Lowe's": (0.78, 29, 8, 0.70, 0),
                "Kroger": (0.62, 24, 7, 0.80, 0),
                "Sonic Drive-In": (0.48, 9, 3, 0.36, 0),
            }
            shallow_front = name in {"Taco Bell", "Chick-fil-A"}
            if shallow_front:
                span = min(width * 0.91, 7.8)
                left = (width - span) / 2
                top = height + (2.1 if name == "Taco Bell" else 1.15)
            else:
                share, limit, depth, rise, pitch = profiles.get(
                    name, (0.44, 9, 4, 0.78, 0) if restaurant else (0.52, 23, 6, 1.1, 0)
                )
                span = min(
                    width * 0.94, max(min(3.0, width), min(width * share, limit))
                )
                anchor = door[0] if len(door) == 1 else width / 2
                left = float(np.clip(anchor - span / 2, 0, width - span))
                top = height + rise
                # Limit the extrusion against every intersecting footprint edge, including setbacks.
                relative = ring - ring[i]
                along = relative @ axes[i]
                inward = -(relative @ normals[i])
                for j in range(len(ring)):
                    if j == i:
                        continue
                    k = (j + 1) % len(ring)
                    lo, hi = sorted((along[j], along[k]))
                    start, end = max(lo, left + 0.03), min(hi, left + span - 0.03)
                    if end < start:
                        continue
                    if hi - lo < 1e-8:
                        depths = [inward[j], inward[k]]
                    else:
                        depths = [
                            inward[j]
                            + (v - along[j])
                            * (inward[k] - inward[j])
                            / (along[k] - along[j])
                            for v in (start, end)
                        ]
                    if max(depths) > 0.01:
                        depth = min(depth, max(0, min(depths) - 0.02))

                def roof_point(x, d, z):
                    return (*(ring[i] + axes[i] * x + normals[i] * d), z)

                def quad(a, b, c, d, material):
                    mesh([a, b, c, a, c, d], material)

                rear = top + depth * pitch
                for x in (left, left + span):
                    quad(
                        roof_point(x, -depth, height),
                        roof_point(x, 0, height),
                        roof_point(x, 0, top),
                        roof_point(x, -depth, rear),
                        0,
                    )
                quad(
                    roof_point(left, -depth, height),
                    roof_point(left + span, -depth, height),
                    roof_point(left + span, -depth, rear),
                    roof_point(left, -depth, rear),
                    0,
                )
                quad(
                    roof_point(left, 0, top),
                    roof_point(left + span, 0, top),
                    roof_point(left + span, -depth, rear),
                    roof_point(left, -depth, rear),
                    2,
                )
            if name in {"Taco Bell", "Wendy's"}:
                # A narrow entrance tower rises through the horizontal dining-room volume.
                tower_width = min(3.8, width * 0.65)
                x = max(
                    0,
                    min(
                        width - tower_width,
                        (door[0] if door else width / 2) - tower_width / 2,
                    ),
                )
                finish = Image.new(
                    "RGB", (144, 256), "#a12638" if name == "Wendy's" else "#87664d"
                )
                brush = ImageDraw.Draw(finish)
                for y in range(4, 256, 15):
                    brush.line(
                        (0, y, 144, y),
                        fill="#8f2030" if name == "Wendy's" else "#72523e",
                    )
                # Leave the door and its clear opening visible below the canopy.
                face(x, x + tower_width, 0.60, 2.99, top + 0.42, texture(finish), True)
                block(x, x + tower_width, 0.58, 2.99, top + 0.42, 0)
            if name in {"Subway", "Kroger", "Lowe's"}:
                # Capped gable on the projecting entrance bay.
                rise = min(1.8, span * 0.19)
                tip = (left + span / 2, top + rise)

                def point(x, z, depth):
                    return (*(ring[i] + axes[i] * x + normals[i] * depth), z)

                for gable_end in (-depth, 0.77):
                    mesh(
                        [
                            point(left, top, gable_end),
                            point(left + span, top, gable_end),
                            point(*tip, gable_end),
                        ],
                        0,
                    )
                for x in (left, left + span):
                    mesh(
                        [
                            point(x, top, -depth),
                            point(x, top, 0.77),
                            point(*tip, 0.77),
                            point(x, top, -depth),
                            point(*tip, 0.77),
                            point(*tip, -depth),
                        ],
                        2,
                    )
            block(left, left + span, 0.76, height - 0.9, top, 0)
            sign_h = top - height + 0.72
            sign_width = min(span - 0.06, sign_h * 4.5)
            if name in {"CVS", "Walgreens", "Costco", "Publix", "ALDI", "Sam's Club"}:
                sign_width = span * 0.94
            start = left + (span - sign_width) / 2
            sign_mid = (height - 0.82 + top - 0.10) / 2
            sign_half_height = (
                sign_h / 2
                if shallow_front
                or name
                in {"CVS", "Walgreens", "Costco", "Publix", "ALDI", "Sam's Club"}
                else sign_width / 9
            )  # 900 x 200 artwork
            face(
                start,
                start + sign_width,
                0.78,
                sign_mid - sign_half_height,
                sign_mid + sign_half_height,
                texture(sign_image(name, sign_width / (2 * sign_half_height))),
                True,
            )
            block(left - 0.08, left + span + 0.08, 0.84, top - 0.1, top + 0.03, 2)
        if i == street_front and width >= 10 and not docks[i]:
            # A bracket on the approach wall carries the sign above head height. Its two faces
            # read in opposite directions along the street, including from a shallow approach.
            reach = min(2.8, width * .13)
            bottom = max(3.5, height - .35)
            top = bottom + min(1.65, reach * .58)
            along = width * .19
            block(along - .09, along + .09, reach, bottom - .12, bottom + .08, 2)
            anchor = ring[i] + axes[i] * along
            art = texture(sign_image(name, reach / (top - bottom)))
            for side in (-1, 1):
                base = anchor + side * .085 * axes[i]
                inner, outer = base + .12 * normals[i], base + (reach + .12) * normals[i]
                a, b = (inner, outer) if side == -1 else (outer, inner)
                mesh([(*a,bottom), (*b,bottom), (*b,top), (*a,bottom), (*b,top), (*a,top)], art, uv)
        if i == secondary:
            sign_width = min(width * 0.64, 7.2)
            sign_height = min(1.35, sign_width / 4.5, height * 0.21)
            left = (width - sign_width) / 2
            top = height - 0.24
            face(left, left + sign_width, 0.12, top - sign_height, top,
                 texture(sign_image(name, sign_width / sign_height)), True)
        painting = elevation(
            name,
            width,
            height,
            door,
            docks[i],
            dining,
            shop_bays,
            photo_color=photo_colors.get(i),
            observed_brick=mapped_brick or i in photo_colors,
        )
        face(0, width, 0.025, 0, height, texture(painting), True)
        block(0, width, 0.09, height - 0.13, height + 0.04, 2)
        for x in door:
            half = min(2.25 if restaurant else 6.5, width * 0.49)
            center = np.clip(x, half, width - half)
            z = 2.92 if restaurant else 3.8
            reach = 1.25 if restaurant else 2.4
            if name in {"Starbucks", "Arby's"}:
                half = min(width * 0.475, 5.6 if name == "Starbucks" else 4.1)
                center = float(np.clip(x, half, width - half))
                reach = 1.7
                fabric = {
                    "Starbucks": "#284b40",
                    "Chick-fil-A": "#464339",
                    "Arby's": "#a72b33",
                }[name]
                shade = texture(Image.new("RGB", (8, 8), fabric))
                a, b = (
                    ring[i] + axes[i] * (center - half),
                    ring[i] + axes[i] * (center + half),
                )
                p, q = a + normals[i] * reach, b + normals[i] * reach
                mesh(
                    [
                        (*a, z + 0.62),
                        (*b, z + 0.62),
                        (*q, z + 0.18),
                        (*a, z + 0.62),
                        (*q, z + 0.18),
                        (*p, z + 0.18),
                    ],
                    shade,
                    uv,
                )
                face(center - half, center + half, reach, z, z + 0.18, shade, True)
                continue
            if name == "Sonic Drive-In":
                # Arch the attached canopy without assuming a separate drive-in lane.
                half = min(5.5, width * 0.47)
                center = np.clip(x, half, width - half)
                reach = 2.35
                red = texture(Image.new("RGB", (8, 8), "#b53236"))
                for step in range(12):
                    l, r = step / 12, (step + 1) / 12
                    u, v = center + half * (2 * l - 1), center + half * (2 * r - 1)
                    zu, zv = (
                        z + 0.7 * math.sin(math.pi * l),
                        z + 0.7 * math.sin(math.pi * r),
                    )

                    def vertex(pos, depth, h):
                        return (*(ring[i] + axes[i] * pos + normals[i] * depth), h)

                    a, b = vertex(u, reach, zu), vertex(v, reach, zv)
                    c, d = vertex(v, 0, zv), vertex(u, 0, zu)
                    mesh([a, b, c, a, c, d], 2)
                    e, f = vertex(u, reach, zu + 0.2), vertex(v, reach, zv + 0.2)
                    mesh([a, b, f, a, f, e], red, uv)
            block(center - half, center + half, reach, z, z + 0.25, 2)
            awning = {
                "Popeyes": "#ae3234",
                "Ace Hardware": "#aa3236",
                "Pizza Hut": "#b92e34",
                "Dunkin'": "#d97627",
                "Sonic Drive-In": "#edb72c",
            }.get(name)
            if awning:
                face(
                    center - half,
                    center + half,
                    reach + 0.015,
                    z,
                    z + 0.25,
                    texture(Image.new("RGB", (8, 8), awning)),
                    True,
                )
            if not restaurant and width > 9:
                # A pair of masonry piers and a raised head frame each vestibule.
                # Confine supports to the building face to keep the approach clear.
                for post in (center - half, center + half - 0.55):
                    block(post, post + 0.55, 0.48, 0, height + 0.34, 0)
                block(
                    center - half, center + half, 0.55, height - 0.52, height + 0.36, 0
                )
                block(
                    center - half - 0.08,
                    center + half + 0.08,
                    0.66,
                    height + 0.35,
                    height + 0.46,
                    2,
                )
                # Separate entrance signs remain visible when the middle of a big box
                # is set back or screened by another mapped structure.
                if i == front and len(door) >= 2 and width >= 42 and half >= 4:
                    sign_width = min(2 * half - 1.2, 11)
                    sign_height = min(1.95, sign_width / 4.5, height - z - .35)
                    if sign_height > .65:
                        top = height - .55
                        face(center - sign_width / 2, center + sign_width / 2, .69,
                             top - sign_height, top, texture(sign_image(name, sign_width / sign_height)), True)

        if restaurant:
            for x in dining:
                block(x - 1.56, x + 1.56, 0.38, 2.83, 2.98, 2)

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
