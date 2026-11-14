"""Offline model preview and country STOP face contact sheet."""

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from preview_fr_road_details import append_model, assets, caption, FONT, fr, render, sm
import build_europe_stops as stops


def stop_faces(output):
    faces = json.loads((stops.ART / "sources.json").read_text(encoding="utf-8"))["faces"]
    cols, cell_w, cell_h = 8, 180, 190
    sheet = Image.new("RGB", (cols * cell_w, 78 + cell_h * math.ceil(len(faces) / cols)), "#eef2f4")
    draw = ImageDraw.Draw(sheet)
    heading = ImageFont.truetype(str(FONT), 29)
    label = ImageFont.truetype(str(FONT), 19)
    small = ImageFont.truetype(str(FONT), 15)
    draw.text((18, 10), "Faces STOP et DUR · Europe", font=heading, fill="#172d3b")
    draw.text(
        (18, 48),
        "Dessins utilisés dans les modèles · correspondances par territoire dans la notice",
        font=small,
        fill="#3c5363",
    )
    for i, code in enumerate(sorted(faces)):
        x, y = (i % cols) * cell_w, 78 + (i // cols) * cell_h
        im = Image.open(stops.OUT / f"bne_eu_stop_{code}.png").convert("RGB")
        face = im.resize((142, 142), Image.Resampling.LANCZOS)
        mask = Image.new("L", (568, 568))
        cut = round((1.0 - 1.0 / math.sqrt(2.0)) * 568)
        ImageDraw.Draw(mask).polygon(
            [
                (cut, 0),
                (568 - cut, 0),
                (567, cut),
                (567, 568 - cut),
                (568 - cut, 567),
                (cut, 567),
                (0, 568 - cut),
                (0, cut),
            ],
            fill=255,
        )
        sheet.paste(face, (x + 19, y + 28), mask.resize(face.size, Image.Resampling.LANCZOS))
        draw.text(
            (x + 19, y + 2),
            code.upper() if code != "vienna" else "B2a générique",
            font=label,
            fill="#172d3b",
        )
    sheet.save(output / "stops_europe.png")


def road_models(output):
    railing = sm.Mesh()
    append_model(railing, assets.module("a", "white_red"))
    rail_card = caption(
        render(railing, (800, 300), -20, 11),
        "Réflecteurs dans le creux",
        "Profil A · exemple de face bicolore",
    )
    j14 = sm.Mesh()
    append_model(j14, assets.j14(), (-1.40, 0, 0))
    append_model(j14, assets.j14(True), (1.0, 0, 0))
    gore_card = caption(
        render(j14, (800, 300), -12, 8),
        "Balises J14a de sortie",
        "Diamètres de 2 m et 1 m · vue avant",
    )
    plaques = sm.Mesh()
    # Wall and post displayed in one small illustrative scene.
    plaques.box(sm.METAL, (-1.22, 0.22, 1.56), (1.27, 0.22, 1.56))
    fr.street(plaques, (-1.22, -0.034, 2.535), (0, 1), "Rue des Rochers", centre_height=0.0)
    fr.street(plaques, (1.07, 0, 0), (0, 1), "Rue du Puits")
    fr.street(plaques, (1.07, 0, 0), (0, -1), "Rue du Puits", body=False)
    append_model(plaques, assets.street_post(), (1.07, 0, 0))
    plaque_card = caption(
        render(plaques, (800, 465), -9, 4),
        "Plaque murale et plaque sur poteau",
        "Assemblage illustratif · les deux types sont générés",
    )
    sheet = Image.new("RGB", (1600, rail_card.height + plaque_card.height + 76), "#eaeff2")
    title = ImageFont.truetype(str(FONT), 29)
    note = ImageFont.truetype(str(FONT), 17)
    draw = ImageDraw.Draw(sheet)
    draw.text((22, 10), "Corrections routes France et Europe", font=title, fill="#172d3b")
    draw.text((22, 48), "Maillages et faces du mod, rendus hors jeu", font=note, fill="#3c5363")
    sheet.paste(rail_card, (0, 76))
    sheet.paste(gore_card, (800, 76))
    sheet.paste(plaque_card, (0, 76 + rail_card.height))
    # Give the road props their scale beside a few actual sign faces.
    faces = Image.open(output / "stops_europe.png")
    clip = Image.new("RGB", (800, plaque_card.height), "#eef2f4")
    d = ImageDraw.Draw(clip)
    d.text(
        (22, 10),
        "Exemples de faces STOP et DUR",
        font=ImageFont.truetype(str(FONT), 23),
        fill="#172d3b",
    )
    d.text((22, 42), "La planche complète accompagne le ZIP", font=note, fill="#3c5363")
    codes = [
        c
        for c in ("fr", "de", "gb", "it", "es", "se", "no", "tr")
        if (stops.OUT / f"bne_eu_stop_{c}.png").exists()
    ]
    order = sorted(json.loads((stops.ART / "sources.json").read_text(encoding="utf-8"))["faces"])
    for i, code in enumerate(codes):
        j = order.index(code)
        x = (j % 8) * 180
        y = 78 + (j // 8) * 190
        clip.paste(faces.crop((x, y, x + 180, y + 190)), (40 + (i % 4) * 180, 74 + (i // 4) * 190))
    sheet.paste(clip, (800, 76 + rail_card.height))
    sheet.save(output / "apercus_routes.png")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    stop_faces(args.output)
    road_models(args.output)
