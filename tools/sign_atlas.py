#!/usr/bin/env python3
"""The street furniture's texture atlas (mod/.../beamngEarth/bne_signs.png, the material bne_signs): Dutch road signs
(Wikimedia Commons' SVGs of the RVV 1990 signs, public domain, rendered by Commons; drawn here where it cannot be
reached) and the faces of traffic lights, on a 4 x 4 grid of 256 px cells. tools/kunstwerk_props.py
maps its plates to the cells by CELLS.

    python3 tools/sign_atlas.py
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                   "bne_signs3.png")
C = 256
GRID = (4, 16)           # cells across, down (the route signs take 2 x 2 cells each: SPAN)
CELLS = {"B6": 0, "G11": 1, "D2": 2, "L2": 3, "light": 4, "back": 5, "stripes": 6, "route": 7, "black": 8,
         "light_bike": 9, "button": 10, "grey": 11, "lens_red": 12, "lens_amber": 13, "lens_green": 14, "white": 15,
         "route_havens": 16, "route_centrum": 18, "route_a13": 24,
         "route_maastunnel": 26, "route_delfshaven": 32, "route_ringa20": 34,
         "finger_centrum": 40, "finger_westzeedijk": 41, "finger_maastunnel": 42, "route_small_s100": 43,
         "route_small_a20": 44, "route_small_s114": 45}
SPAN = {"route_havens": 2, "route_centrum": 2, "route_a13": 2, "route_maastunnel": 2, "route_delfshaven": 2,
        "route_ringa20": 2}
# the gantry's route signs over the Droogleever Fortuynplein's east arm, as on them (Street View, 2026-10-21): each its
# size (m) and its lines; drawn in the RVV's colours (the ANWB face is not ours: Arial Bold stands in)
ROUTE = {
    "route_havens": ((3.0, 2.4), [("panel", "Havens 200-500"), ("brown", "Euromast"), ("text", "S 114"),
                                  ("arrow", "left")]),
    "route_centrum": ((3.6, 3.7), [("panel", "Centrumring", "S100"), ("green", "Erasmus MC", "H"),
                                   ("green", "Museumpark", "P"), ("text", "Ring A20"), ("box", "route", "P"),
                                   ("arrow", "left")]),
    "route_a13": ((3.5, 2.0), [("text", "A13  A20", "truck"), ("text", "route"), ("text", "S 114")]),
    # the cantilever on the 's-Gravendijkwal's median nose, its two signs over the two ways (Street View B221)
    "route_maastunnel": ((4.0, 3.2), [("panel", "Maastunnel", "S102"), ("text", "Ring A15"), ("text", "S 120"),
                                      ("green", "Erasmus MC", "H"), ("green", "Westzeedijk", "P"),
                                      ("arrow", "up")]),
    "route_delfshaven": ((6.0, 2.6), [("text", "Havens 200-210"), ("brown", "Euromast"),
                                      ("text", "Delfshaven"), ("text", "Lloydkwartier"), ("arrow", "left")]),
    "route_ringa20": ((3.0, 1.6), [("text", "Ring A20"), ("text", "S 100"), ("arrow", "right")]),
    # the fingers on the tram verge (Street View B130) and the small route plates by the zebras (A269, B20)
    "finger_centrum": ((2.2, 0.55), [("text", "Centrumring  S100")]),
    "finger_westzeedijk": ((2.2, 0.55), [("text", "Westzeedijk")]),
    "finger_maastunnel": ((2.2, 0.55), [("text", "Maastunnel")]),
    "route_small_s100": ((1.2, 0.5), [("text", "Centrumring")]),
    "route_small_a20": ((1.2, 0.5), [("text", "Ring A20")]),
    "route_small_s114": ((1.2, 0.5), [("text", "S 114")]),
}
COMMONS = {"B6": "Nederlands verkeersbord B6.svg", "G11": "Nederlands verkeersbord G11.svg",
           "D2": "Nederlands verkeersbord D2.svg", "L2": "Nederlands verkeersbord L2.svg"}
CACHE = os.path.join(HERE, "..", "..", "design", "kunstwerken", "_textures", "signs")
RED, BLUE, WHITE, BLACK = (200, 16, 30), (0, 72, 150), (250, 250, 248), (18, 18, 18)
SIGN_BLUE = (0, 66, 158)        # the direction signs' blue (RAL 5017's, a little deeper: the game's haze lifts it)


def cell(d, k):
    return (k % GRID[0]) * C, (k // GRID[0]) * C


def _font(size, bold=True):
    """The signs' face: Bahnschrift (DIN 1451, the nearest Windows has to the ANWB's), bold; Arial Bold else."""
    from PIL import ImageFont
    try:
        f = ImageFont.truetype("bahnschrift.ttf", size)
        f.set_variation_by_name("Bold" if bold else "SemiBold")
        return f
    except Exception:
        return ImageFont.truetype("arialbd.ttf", size)


def _arrow(d, cx, top, bottom, kind, w, col):
    """A Dutch direction arrow: a thick stem up from the bottom, its head as the sign draws it (up, bending left or
    right, or diagonal right)."""
    st = w * 0.16
    head = w * 0.42
    if kind == "up":
        d.rectangle([cx - st / 2, top + head * 0.8, cx + st / 2, bottom], fill=col)
        d.polygon([(cx, top), (cx - head / 2, top + head), (cx + head / 2, top + head)], fill=col)
        return
    if kind in ("left", "right"):          # up, then bending to the side and up (the sign's "turn" arrow)
        sg = -1 if kind == "left" else 1
        bend = top + (bottom - top) * 0.45
        d.rectangle([cx - st / 2, bend, cx + st / 2, bottom], fill=col)
        tip = (cx + sg * head * 0.95, top)
        base = (cx, bend + st / 2)
        dx, dy = tip[0] - base[0], tip[1] - base[1]
        L = (dx * dx + dy * dy) ** 0.5
        ux, uy = dx / L, dy / L
        nx, ny = -uy, ux
        p1 = (tip[0] - ux * head * 0.75, tip[1] - uy * head * 0.75)
        d.polygon([(base[0] + nx * st / 2, base[1] + ny * st / 2), (p1[0] + nx * st / 2, p1[1] + ny * st / 2),
                   (p1[0] + nx * head / 2, p1[1] + ny * head / 2), tip, (p1[0] - nx * head / 2, p1[1] - ny * head / 2),
                   (p1[0] - nx * st / 2, p1[1] - ny * st / 2), (base[0] - nx * st / 2, base[1] - ny * st / 2)],
                  fill=col)
        return
    # diagonal up-right
    d.line([(cx - head * 0.6, bottom), (cx + head * 0.4, top + head * 0.6)], fill=col, width=int(st))
    d.polygon([(cx + head * 0.7, top), (cx + head * 0.05, top + head * 0.25), (cx + head * 0.62, top + head * 0.82)],
              fill=col)


def route_sign(size, lines, span=2):
    """A Dutch direction sign's face as the ANWB draws them (Street View, 2026-10-22): blue, a thin white border with
    rounded corners, its lines filling its width, left aligned behind a common margin: a destination on a white panel
    (black text) or in a green-bordered white panel (blue text, an icon square before it), a brown panel for a sight,
    white text on the blue for the rest, the arrow centred at the bottom. Drawn at its own proportions, then fitted to
    its span x span cells (the plate's UV stretches it back)."""
    w, h = size
    W = 1400
    H = int(W * h / w)
    im = Image.new("RGB", (W, H), SIGN_BLUE)
    d = ImageDraw.Draw(im)
    b = max(int(W * 0.018), 6)
    d.rounded_rectangle([b // 2, b // 2, W - 1 - b // 2, H - 1 - b // 2], radius=int(W * 0.03), outline=WHITE, width=b)
    pad = W * 0.06
    n_text = sum(1 for it in lines if it[0] != "arrow")
    has_arrow = any(it[0] == "arrow" for it in lines)
    row = (H - 2 * b - pad * 0.6) / (n_text + (1.6 if has_arrow else 0) + 0.2)
    fs = int(min(row * 0.72, W / 9.5))
    font, small = _font(fs), _font(int(fs * 0.72))
    y = b + pad * 0.3
    for item in lines:
        kind, text = item[0], item[1]
        extra = item[2] if len(item) > 2 else None
        if kind == "arrow":
            _arrow(d, W / 2, y + row * 0.1, y + row * 1.5, text, row * 1.6, WHITE)
            y += row * 1.6
            continue
        x0, x1 = pad, W - pad
        yc = y + row / 2
        fg = WHITE
        if kind in ("panel", "green", "brown", "box"):
            bg = {"panel": WHITE, "green": WHITE, "brown": (122, 64, 34), "box": WHITE}[kind]
            fg = {"panel": BLACK, "green": SIGN_BLUE, "brown": WHITE, "box": SIGN_BLUE}[kind]
            tw = d.textlength(text, font=font)
            right = x1 if kind != "box" else x0 + row * 0.95 + tw + row * 0.5
            d.rounded_rectangle([x0, y + row * 0.07, right, y + row * 0.93], radius=int(row * 0.12), fill=bg,
                                outline=(0, 128, 64) if kind == "green" else None, width=int(row * 0.07))
        tx = x0 + row * 0.25
        if extra and extra not in ("S100", "S102", "S114"):   # an icon square before the text
            sq = row * 0.62
            d.rounded_rectangle([tx, yc - sq / 2, tx + sq, yc + sq / 2], radius=int(sq * 0.12),
                                fill=SIGN_BLUE if kind in ("panel", "green", "box") else WHITE)
            ic = WHITE if kind in ("panel", "green", "box") else SIGN_BLUE
            if extra == "truck":                        # a lorry from the side: box, cab, wheels
                s_ = sq * 0.36
                cx_ = tx + sq / 2
                d.rectangle([cx_ - s_, yc - s_ * 0.55, cx_ + s_ * 0.25, yc + s_ * 0.35], fill=ic)
                d.polygon([(cx_ + s_ * 0.32, yc - s_ * 0.2), (cx_ + s_ * 0.75, yc - s_ * 0.2), (cx_ + s_, yc + s_ * 0.1),
                           (cx_ + s_, yc + s_ * 0.35), (cx_ + s_ * 0.32, yc + s_ * 0.35)], fill=ic)
                for wx in (-0.6, 0.65):
                    d.ellipse([cx_ + (wx - 0.17) * s_, yc + s_ * 0.3, cx_ + (wx + 0.17) * s_, yc + s_ * 0.64],
                              fill=ic)
            elif extra == "plane":
                d.text((tx + sq / 2, yc), "✈", font=_font(int(sq * 0.8)), fill=ic, anchor="mm")
            else:
                d.text((tx + sq / 2, yc), extra, font=_font(int(sq * 0.85)), fill=ic, anchor="mm")
            tx += sq + row * 0.25
        room = x1 - tx - row * 0.2 - (row * 1.9 if extra in ("S100", "S102", "S114") else 0)
        f_line = font
        while d.textlength(text, font=f_line) > room and f_line.size > 10:     # (a long name: smaller, in its panel)
            f_line = _font(int(f_line.size * 0.93))
        d.text((tx, yc), text, font=f_line, fill=fg, anchor="lm")
        font_used = f_line
        if extra in ("S100", "S102", "S114"):               # a city route's number in its box after the name
            nx_ = tx + d.textlength(text, font=font_used) + row * 0.35
            nw = d.textlength(extra.replace("S", "s "), font=small) + row * 0.3
            d.rounded_rectangle([nx_, yc - row * 0.3, nx_ + nw, yc + row * 0.3], radius=int(row * 0.08),
                                fill=WHITE, outline=BLACK, width=max(2, int(row * 0.05)))
            d.text((nx_ + nw / 2, yc), extra.replace("S", "s "), font=small, fill=BLACK, anchor="mm")
        y += row
    return im.resize((span * C, span * C), Image.LANCZOS)


def bicycle(d, x, y, s, col):
    """A bicycle as the signs draw it: two wheels, the frame, the bars (s: the cell's scale)."""
    w = max(2, int(10 * s))
    r = 34 * s
    for cx in (x - 50 * s, x + 50 * s):
        d.ellipse([cx - r, y + 10 * s - r, cx + r, y + 10 * s + r], outline=col, width=w)
    pts = [(x - 50 * s, y + 10 * s), (x - 12 * s, y - 32 * s), (x + 32 * s, y - 32 * s), (x + 50 * s, y + 10 * s)]
    d.line(pts, fill=col, width=w)
    d.line([(x - 50 * s, y + 10 * s), (x + 8 * s, y + 10 * s), (x + 32 * s, y - 32 * s)], fill=col, width=w)
    d.line([(x - 12 * s, y - 32 * s), (x - 18 * s, y - 48 * s), (x - 32 * s, y - 48 * s)], fill=col, width=w)
    d.line([(x + 32 * s, y - 32 * s), (x + 26 * s, y - 52 * s), (x + 40 * s, y - 54 * s)], fill=col, width=w)


def commons(title):
    """The sign's PNG as Commons renders its SVG (cached), RGBA; None when it cannot be had."""
    import json
    import urllib.parse
    import urllib.request
    os.makedirs(CACHE, exist_ok=True)
    dst = os.path.join(CACHE, title.replace(" ", "_").replace(".svg", ".png"))
    if not os.path.exists(dst):
        try:
            q = urllib.parse.urlencode({"action": "query", "titles": "File:" + title, "prop": "imageinfo",
                                        "iiprop": "url|extmetadata", "iiurlwidth": 512, "format": "json"})
            hdr = {"User-Agent": "beamng-earth-tools/1.0"}
            j = json.load(urllib.request.urlopen(urllib.request.Request(
                "https://commons.wikimedia.org/w/api.php?" + q, headers=hdr), timeout=30))
            ii = next(iter(j["query"]["pages"].values()))["imageinfo"][0]
            if ii["extmetadata"].get("LicenseShortName", {}).get("value") != "Public domain":
                return None
            with urllib.request.urlopen(urllib.request.Request(ii["thumburl"], headers=hdr), timeout=30) as f:
                open(dst, "wb").write(f.read())
        except Exception as e:      # (offline: the drawn one)
            print(f"  {title}: {e}")
            return None
    return Image.open(dst).convert("RGBA")


def metal(im):
    """The poles' cells as metal, not flat paint: hot-dip galvanised grey (mottled spangle, faint streaks along the
    pole) and black powder coat (a fine orange peel); and the atlas's roughness and metallic maps beside it
    (bne_signs3_r.png, bne_signs3_m.png: per cell, the plates' faces smooth and not metal, the galvanising metal)."""
    import numpy as np
    from scipy.ndimage import gaussian_filter
    a = np.asarray(im).astype(np.float32)
    rng = np.random.default_rng(7)
    rough = np.full(a.shape[:2], 150, np.float32)       # (0.59: painted metal, a sign's back)
    metal_ = np.zeros(a.shape[:2], np.float32)

    def region(name):
        x, y = cell(None, CELLS[name])
        sp = SPAN.get(name, 1) * C
        return slice(y, y + sp), slice(x, x + sp)
    for name in ("grey", "back"):
        r = region(name)
        n = (gaussian_filter(rng.normal(0, 1, (C, C)), 6) * 9 + gaussian_filter(rng.normal(0, 1, (C, C)), 1.2) * 5
             + gaussian_filter(rng.normal(0, 1, (C, C)), (40, 0.8)) * 4)     # spangle, grain, streaks along v
        base = np.array([150, 154, 152] if name == "grey" else [140, 143, 142], np.float32)
        a[r] = np.clip(base + n[..., None] * np.array([1, 1, 1.05]), 0, 255)
        rough[r] = 105 + gaussian_filter(rng.normal(0, 1, (C, C)), 5) * 18
        metal_[r] = 215 if name == "grey" else 160
    for name in ("black", "stripes", "button"):
        r = region(name)
        n = gaussian_filter(rng.normal(0, 1, (C, C)), 1.5) * 3
        a[r] = np.clip(a[r] + n[..., None], 0, 255)
        rough[r] = 120 + n * 4                            # powder coat: satin
    for name in ("B6", "G11", "D2", "L2", "light", "light_bike"):
        rough[region(name)] = 80                          # the sign film and the lenses: smooth
    for name in [n for n in CELLS if n.startswith(("route", "finger"))]:
        rough[region(name)] = 185                         # the big signs' film: matte (no sky in it, round 7)
    im.paste(Image.fromarray(a.astype(np.uint8)), (0, 0))
    Image.fromarray(np.clip(rough, 0, 255).astype(np.uint8)).save(OUT.replace(".png", "_r.png"))
    Image.fromarray(metal_.astype(np.uint8)).save(OUT.replace(".png", "_m.png"))


def main():
    im = Image.new("RGB", (GRID[0] * C, GRID[1] * C), (128, 128, 128))
    d = ImageDraw.Draw(im)
    # B6: give way, a triangle point down, red border
    x, y = cell(d, CELLS["B6"])
    d.rectangle([x, y, x + C, y + C], fill=(150, 152, 150))
    d.polygon([(x + 8, y + 24), (x + C - 8, y + 24), (x + C / 2, y + C - 10)], fill=RED)
    d.polygon([(x + 48, y + 48), (x + C - 48, y + 48), (x + C / 2, y + C - 66)], fill=WHITE)
    # G11: cycle path, blue disc, white bicycle
    x, y = cell(d, CELLS["G11"])
    d.rectangle([x, y, x + C, y + C], fill=(150, 152, 150))
    d.ellipse([x + 6, y + 6, x + C - 6, y + C - 6], fill=WHITE)
    d.ellipse([x + 14, y + 14, x + C - 14, y + C - 14], fill=BLUE)
    bicycle(d, x + C / 2, y + C / 2 + 8, 1.0, WHITE)
    # D2: keep right, blue disc, white arrow down to the right
    x, y = cell(d, CELLS["D2"])
    d.rectangle([x, y, x + C, y + C], fill=(150, 152, 150))
    d.ellipse([x + 6, y + 6, x + C - 6, y + C - 6], fill=WHITE)
    d.ellipse([x + 14, y + 14, x + C - 14, y + C - 14], fill=BLUE)
    d.line([(x + 76, y + 76), (x + 162, y + 162)], fill=WHITE, width=30)
    d.polygon([(x + 190, y + 190), (x + 110, y + 182), (x + 182, y + 110)], fill=WHITE)
    # L2: zebra crossing, blue square, white triangle, a walker on stripes
    x, y = cell(d, CELLS["L2"])
    d.rectangle([x + 6, y + 6, x + C - 6, y + C - 6], fill=BLUE)
    d.polygon([(x + C / 2, y + 30), (x + C - 30, y + C - 40), (x + 30, y + C - 40)], fill=WHITE)
    for i in range(5):
        d.rectangle([x + 66 + i * 26, y + C - 70, x + 78 + i * 26, y + C - 50], fill=BLACK)
    d.ellipse([x + 118, y + 92, x + 138, y + 112], fill=BLACK)
    d.line([(x + 128, y + 112), (x + 124, y + 150), (x + 108, y + 176)], fill=BLACK, width=8)
    d.line([(x + 124, y + 150), (x + 146, y + 176)], fill=BLACK, width=8)
    d.line([(x + 110, y + 128), (x + 146, y + 132)], fill=BLACK, width=7)
    # a traffic light's face: black, three lenses (red lit, amber and green dark), visors' shadows
    for name, bike in (("light", False), ("light_bike", True)):
        x, y = cell(d, CELLS[name])
        d.rectangle([x, y, x + C, y + C], fill=BLACK)
        d.rectangle([x + 70, y + 4, x + C - 70, y + C - 4], outline=(235, 235, 235), width=6)
        for j, (on, off) in enumerate((((255, 40, 30), (90, 10, 10)), ((255, 170, 0), (90, 60, 0)),
                                       ((40, 255, 120), (0, 70, 30)))):
            cy = y + 44 + j * 84
            col = on if j == 0 else off
            d.ellipse([x + C / 2 - 34, cy - 34, x + C / 2 + 34, cy + 34], fill=col)
            if bike:
                bicycle(d, x + C / 2, cy + 4, 0.32, BLACK)
    # a sign's back and a plain pole: galvanised grey
    x, y = cell(d, CELLS["back"])
    d.rectangle([x, y, x + C, y + C], fill=(150, 152, 150))
    x, y = cell(d, CELLS["grey"])
    d.rectangle([x, y, x + C, y + C], fill=(165, 167, 165))
    # a signal pole's band: black and white rings, 8 per cell height (the UV's v runs up the pole)
    x, y = cell(d, CELLS["stripes"])
    for i in range(8):
        d.rectangle([x, y + i * 32, x + C, y + i * 32 + 32], fill=BLACK if i % 2 else WHITE)
    # a route sign: blue, white border, a destination's lines and an arrow
    x, y = cell(d, CELLS["route"])
    d.rectangle([x, y, x + C, y + C], fill=WHITE)
    d.rectangle([x + 6, y + 6, x + C - 6, y + C - 6], fill=BLUE)
    for i, w in enumerate((150, 120, 170)):
        d.rectangle([x + 26, y + 40 + i * 50, x + 26 + w, y + 64 + i * 50], fill=WHITE)
    d.polygon([(x + 200, y + 200), (x + 230, y + 170), (x + 230, y + 230)], fill=WHITE)
    d.rectangle([x + 150, y + 192, x + 205, y + 208], fill=WHITE)
    x, y = cell(d, CELLS["black"])
    d.rectangle([x, y, x + C, y + C], fill=BLACK)
    # a push-button box: yellow with a black disc (the cyclists' and walkers' request)
    x, y = cell(d, CELLS["button"])
    d.rectangle([x, y, x + C, y + C], fill=(235, 190, 20))
    d.ellipse([x + 88, y + 88, x + 168, y + 168], fill=BLACK)
    # lenses: a lit red, a dark amber and a dark green (a light at red as it stands), a soft glow to the rim
    for name, (cc, rim) in {"lens_red": ((255, 60, 40), (140, 10, 5)), "lens_amber": ((95, 62, 10), (60, 40, 5)),
                            "lens_green": ((10, 70, 40), (5, 40, 25))}.items():
        x, y = cell(d, CELLS[name])
        d.rectangle([x, y, x + C, y + C], fill=rim)
        for i in range(40):
            f = i / 40
            col = tuple(int(rim[j] + (cc[j] - rim[j]) * f) for j in range(3))
            r = (C / 2 - 4) * (1 - f * 0.85)
            d.ellipse([x + C / 2 - r, y + C / 2 - r, x + C / 2 + r, y + C / 2 + r], fill=col)
    x, y = cell(d, CELLS["white"])
    d.rectangle([x, y, x + C, y + C], fill=(236, 236, 232))
    for name, title in COMMONS.items():
        img = commons(title)
        if img is not None:
            x, y = cell(d, CELLS[name])
            back = Image.new("RGB", (C, C), (150, 152, 150))
            img.thumbnail((C - 8, C - 8))
            back.paste(img, ((C - img.width) // 2, (C - img.height) // 2), img)
            im.paste(back, (x, y))
    for name, (size, lines) in ROUTE.items():
        im.paste(route_sign(size, lines, SPAN.get(name, 1)), cell(d, CELLS[name]))
    metal(im)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    im.save(OUT)
    print(OUT)


if __name__ == "__main__":
    main()
