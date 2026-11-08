"""build_au_roadsigns.py: Australia's chevron alignment marker (AS 1742.2 D4-6), Wrong Way Go Back and gravel road signs
(mod/.../art/shapes/beamngEarth/bne_au_chevron_l.dae, bne_au_chevron_r.dae, bne_au_wrongway.dae, bne_au_gravel.dae, their
textures and au_roadsigns.materials.json), placed by server/auroadsigns.py (fork: Queensland).

    python tools/build_au_roadsigns.py

Each sign is a flat plate on galvanised posts behind it (sunk 0.3 m), its face a texture drawn here (unbranded, in the
speed signs' font, tools/fonts) and quantized to 16 colours: the chevron's one sheet holds two cells, the chevron pointing
left and right (bne_au_chevron_l / _r take one each); the other two are one sheet each. The face looks towards -Y, the
traffic it faces, the sign centred on the origin (the layout of build_au_tboard.py, so the server's rows are alike).
"""
import json
import os
import uuid

from PIL import Image, ImageDraw, ImageFont

from build_rail_crossing_props import OUT, Mesh, write_dae

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, "fonts", "RG2014EEM.ttf")
TEX = "/levels/beamng_earth/art/shapes/beamngEarth/"
POST_SINK = 0.3
FRONT, BACK = -0.045, -0.025        # m: the plate's front and back faces' Y (the posts just behind it)
SS = 4                              # supersampling
COLOURS = 16
YELLOW, BLACK, WHITE, RED = (255, 205, 0), (20, 20, 20), (255, 255, 255), (196, 30, 38)

# name: (width m, height m, lower edge m over the ground, post x positions, post radius m, texture px, material)
SIGNS = {
    "chevron": (0.45, 0.60, 1.0, (0.0,), 0.04, (144, 192), "bne_au_chevron_face"),
    "wrongway": (1.2, 0.90, 1.2, (-0.4, 0.4), 0.045, (384, 288), "bne_au_wrongway_face"),
    "gravel": (1.2, 1.50, 1.0, (-0.4, 0.4), 0.045, (320, 400), "bne_au_gravel_face"),
}
STEEL = "bne_au_roadsign_steel"
STEEL_PBR = ((0.62, 0.64, 0.65), 0.7, 0.45)         # (base colour, metallic, roughness)
FACE_PBR = (0.0, 0.3)                               # (metallic, roughness) of the printed faces


def plate(key, cell=(0.0, 1.0)):
    """One sign's mesh; cell: the sheet's u range the face takes."""
    w, h, low, posts, r, _, face = SIGNS[key]
    m = Mesh(6)
    x, z0, z1 = w / 2, low, low + h
    u0, u1 = cell
    m.face(face, [(-x, FRONT, z0), (x, FRONT, z0), (x, FRONT, z1), (-x, FRONT, z1)],
           [(u0, 0.0), (u1, 0.0), (u1, 1.0), (u0, 1.0)])
    m.face(STEEL, [(x, BACK, z0), (-x, BACK, z0), (-x, BACK, z1), (x, BACK, z1)])
    for a, b in (((-x, z0), (x, z0)), ((x, z0), (x, z1)), ((x, z1), (-x, z1)), ((-x, z1), (-x, z0))):
        m.face(STEEL, [(a[0], BACK, a[1]), (b[0], BACK, b[1]), (b[0], FRONT, b[1]), (a[0], FRONT, a[1])])
    for px in posts:
        m.cylinder(STEEL, px, BACK + r, -POST_SINK, z1, r, caps=False)
    return m


def fit(draw_im, text, cx, cy, cap, room, colour):
    """Text centred at (cx, cy) px, capitals cap px high, squeezed to room px wide if longer."""
    font = ImageFont.truetype(FONT, 200)
    x0, y0, x1, y1 = font.getbbox(text)
    font = ImageFont.truetype(FONT, max(round(200 * cap / (y1 - y0)), 4))
    x0, y0, x1, y1 = font.getbbox(text)
    layer = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(layer).text((-x0, -y0), text, font=font, fill=255)
    if layer.width > room:
        layer = layer.resize((round(room), layer.height), Image.LANCZOS)
    draw_im.paste(colour, (round(cx - layer.width / 2), round(cy - layer.height / 2)), layer)
    return layer.width, layer.height


def finish(im, w, h, path):
    im.resize((w, h), Image.LANCZOS).quantize(COLOURS, dither=Image.Dither.NONE).save(path, optimize=True)


def chevron_cell(w, h, pointing):
    """One cell, w x h px (supersampled): a yellow plate, black border, one bold chevron pointing left or right."""
    im = Image.new("RGB", (w, h), BLACK)
    d = ImageDraw.Draw(im)
    b = round(0.07 * w)
    d.rounded_rectangle((b, b, w - 1 - b, h - 1 - b), radius=round(0.05 * w), fill=YELLOW)
    # (a '<': its tip left, the arms 0.22 of the plate high, a bar the arrow-head of the standard's drawing)
    pts = [(0.12, 0.5), (0.88, 0.12), (0.88, 0.34), (0.56, 0.5), (0.88, 0.66), (0.88, 0.88)]
    if pointing == "right":
        pts = [(1.0 - u, v) for u, v in pts]
    d.polygon([(u * w, v * h) for u, v in pts], fill=BLACK)
    return im


def chevron_png(path):
    cw, ch = SIGNS["chevron"][5]
    sheet = Image.new("RGB", (2 * cw * SS, ch * SS), BLACK)
    sheet.paste(chevron_cell(cw * SS, ch * SS, "left"), (0, 0))
    sheet.paste(chevron_cell(cw * SS, ch * SS, "right"), (cw * SS, 0))
    finish(sheet, 2 * cw, ch, path)


def wrongway_png(path):
    """WRONG / WAY (underlined) / GO BACK, white capitals on red, a white border."""
    w, h = SIGNS["wrongway"][5]
    W, H = w * SS, h * SS
    im = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(im)
    b = round(0.03 * H)
    d.rounded_rectangle((b, b, W - 1 - b, H - 1 - b), radius=round(0.06 * H), fill=RED)
    room, cap = 0.84 * W, 0.20 * H
    fit(im, "WRONG", W / 2, 0.22 * H, cap, room, WHITE)
    tw, _ = fit(im, "WAY", W / 2, 0.50 * H, cap, room, WHITE)
    y = 0.50 * H + cap / 2 + 0.04 * H                    # (the underline, as wide as the word)
    d.rectangle((W / 2 - tw / 2, y, W / 2 + tw / 2, y + 0.035 * H), fill=WHITE)
    fit(im, "GO BACK", W / 2, 0.79 * H, cap, room, WHITE)
    finish(im, w, h, path)


def gravel_png(path):
    """A yellow diamond GRAVEL ROAD over white DRIVE TO / PREVAILING ROAD / CONDITIONS, on red with a white border."""
    w, h = SIGNS["gravel"][5]
    W, H = w * SS, h * SS
    im = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(im)
    b = round(0.02 * W)
    d.rounded_rectangle((b, b, W - 1 - b, H - 1 - b), radius=round(0.04 * W), fill=RED)
    dia = 0.86 * W                                       # the diamond's diagonal
    cx, cy = W / 2, 0.03 * H + dia / 2
    for r, col in ((dia / 2, BLACK), (dia / 2 - 0.025 * W, YELLOW)):
        d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=col)
    fit(im, "GRAVEL", cx, cy - 0.09 * dia, 0.13 * dia, 0.58 * dia, BLACK)
    fit(im, "ROAD", cx, cy + 0.09 * dia, 0.13 * dia, 0.58 * dia, BLACK)
    top = 0.03 * H + dia + 0.035 * H
    cap, gap = 0.058 * H, 0.022 * H
    for k, line in enumerate(("DRIVE TO", "PREVAILING ROAD", "CONDITIONS")):
        fit(im, line, W / 2, top + cap / 2 + k * (cap + gap), cap, 0.9 * W, WHITE)
    finish(im, w, h, path)


def materials():
    out = {}
    pid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + n))
    col, met, rough = STEEL_PBR
    out[STEEL] = {"name": STEEL, "mapTo": STEEL, "class": "Material", "version": 1.5, "persistentId": pid(STEEL),
                  "Stages": [{"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough}, {}, {}, {}]}
    for key, (*_, face) in SIGNS.items():
        out[face] = {"name": face, "mapTo": face, "class": "Material", "version": 1.5, "persistentId": pid(face),
                     "Stages": [{"baseColorMap": f"{TEX}bne_au_{key}.png", "metallicFactor": FACE_PBR[0],
                                 "roughnessFactor": FACE_PBR[1]}, {}, {}, {}]}
    with open(os.path.join(OUT, "au_roadsigns.materials.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)


def main():
    chevron_png(os.path.join(OUT, "bne_au_chevron.png"))
    wrongway_png(os.path.join(OUT, "bne_au_wrongway.png"))
    gravel_png(os.path.join(OUT, "bne_au_gravel.png"))
    materials()
    for name, key, cell in (("bne_au_chevron_l", "chevron", (0.0, 0.5)), ("bne_au_chevron_r", "chevron", (0.5, 1.0)),
                            ("bne_au_wrongway", "wrongway", (0.0, 1.0)), ("bne_au_gravel", "gravel", (0.0, 1.0))):
        path = os.path.join(OUT, name + ".dae")
        lods = [(32, plate(key, cell)), (4, plate(key, cell))]
        write_dae(path, name, lods, Mesh(4))        # (no collision mesh: a plate on posts, nothing to hit)
        w, h, low, *_ = SIGNS[key]
        print(path, os.path.getsize(path), "bytes", round(low + h, 2), "m")
    for k in ("chevron", "wrongway", "gravel"):
        p = os.path.join(OUT, f"bne_au_{k}.png")
        print(p, os.path.getsize(p), "bytes")


if __name__ == "__main__":
    main()
