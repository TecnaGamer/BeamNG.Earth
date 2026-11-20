import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def storefront(name):
    W, H = 1200, 480
    img = Image.new("RGB", (W, H), "#aaa9a4")
    d = ImageDraw.Draw(img)

    def label(x, y, s, size, fill="white", font="DejaVuSans-Bold.ttf"):
        d.text(
            (x, y),
            s,
            font=ImageFont.truetype(
                str(Path(__file__).resolve().parents[1] / "fonts" / font), size
            ),
            anchor="mm",
            fill=fill,
            stroke_width=1,
            stroke_fill="#444850",
        )

    def glazing(left, right, top, bottom):
        d.rectangle((left, top, right, bottom), fill="#252f32")
        panes = max(2, round((right - left) / 125))
        for index in range(panes):
            x = left + round(index * (right - left) / panes) + 4
            edge = left + round((index + 1) * (right - left) / panes) - 4
            d.rectangle((x, top + 5, edge, bottom - 5), fill="#68808a")
            d.polygon(
                [(x, bottom - 6), (edge, top + 40), (edge, bottom - 6)], fill="#475e68"
            )
        middle = (left + right) // 2
        d.rectangle(
            (middle - 8, (top + bottom) // 2, middle - 4, (top + bottom) // 2 + 36),
            fill="#bdc7c8",
        )

    if name.startswith("Mc"):
        d.rectangle((0, 0, W, H), fill="#5c5854")
        for x in range(0, W, 14):
            d.line((x, 0, x, H), fill="#726b63", width=2)
        d.rectangle((55, 15, 238, H), fill="#ac2825")
        for x in (89, 144):
            points = [
                (
                    x + 28 + 28 * math.cos(a * math.pi / 100),
                    154 - 100 * math.sin(a * math.pi / 100),
                )
                for a in range(101)
            ]
            d.line(points, fill="#ffc930", width=12)
        label(713, 109, name, 86)
        glazing(290, 1110, 225, 450)
        d.rectangle((250, 192, 1154, 220), fill="#222728")
    elif name == "Kroger":
        d.rectangle((0, 0, W, H), fill="#a97760")
        for y in range(0, H, 12):
            d.line((0, y, W, y), fill="#bf9681")
            for x in range((y // 12 % 2) * 18, W, 36):
                d.line((x, y, x, y + 11), fill="#bc907b")
        d.polygon([(170, 155), (365, 50), (835, 50), (1030, 155)], fill="#e0d5bd")
        d.ellipse((423, 38, 777, 167), fill="#15549b", outline="#ece4d5", width=5)
        label(600, 93, name, 77, font="D-DIN-Bold.otf")
        glazing(180, 1020, 225, 460)
        d.rectangle((116, 187, 1084, 218), fill="#35544f")
        for x in (142, 1030):
            d.rectangle((x, 217, x + 30, H), fill="#e1d2b8")
    elif name in {"The Home Depot", "Lowe's", "Ace Hardware"}:
        d.rectangle((0, 0, W, H), fill="#cabc9f")
        for y in range(12, H, 18):
            d.line((0, y, W, y), fill="#bcaf95", width=1)
        if name == "Lowe's":
            d.polygon(
                [(290, 114), (600, 17), (910, 114), (910, 183), (290, 183)],
                fill="#173961",
            )
            label(600, 119, "Lowe's", 105)
            trim = "#264971"
        elif name == "The Home Depot":
            d.rectangle((150, 52, 1050, 160), fill="#be5017")
            label(600, 103, "THE HOME DEPOT", 76)
            trim = "#b54b18"
        else:
            label(600, 102, "Ace Hardware", 93, fill="#ad2630")
            trim = "#932925"
        glazing(360, 840, 245, 474)
        d.rectangle((196, 207, 1004, 242), fill=trim)
        for x in (220, 960):
            d.rectangle((x, 242, x + 20, H), fill="#958975")
    elif name in {"Chick-fil-A", "Wendy's"}:
        d.rectangle((0, 0, W, H), fill="#996950")
        for y in range(0, H, 16):
            d.line((0, y, W, y), fill="#b1917c", width=2)
            for x in range((y // 16 % 2) * 22, W, 44):
                d.line((x, y, x, y + 16), fill="#b1917c", width=2)
        d.rectangle((130, 36, 1070, 176), fill="#f1e8d7")
        label(600, 102, name, 97, fill="#a82235", font="D-DIN-Bold.otf")
        glazing(265, 935, 235, 477)
    elif name == "Walmart":
        d.rectangle((0, 0, W, H), fill="#888f94")
        d.rectangle((82, 12, 1118, H), fill="#185689")
        label(520, 94, "Walmart", 90)
        for a in range(0, 360, 60):
            u, v = math.sin(math.radians(a)), math.cos(math.radians(a))
            d.line(
                [(765 + u * 18, 92 + v * 18), (765 + u * 48, 92 + v * 48)],
                fill="#ffbd32",
                width=12,
            )

        glazing(155, 1045, 265, 464)
        d.rectangle((120, 235, 1080, 257), fill="#c5cace")
    else:
        colors = {
            "ALDI": "#223d67",
            "Publix": "#41694b",
            "Costco": "#a33335",
            "Sam's Club": "#206592",
            "The Home Depot": "#ba4c12",
            "Lowe's": "#194a83",
            "Ace Hardware": "#ad2630",
            "Burger King": "#9c3123",
            "Wendy's": "#ac2030",
            "Taco Bell": "#65428e",
            "Chick-fil-A": "#ae2035",
            "Subway": "#176c39",
            "Sonic Drive-In": "#ad253b",
            "Arby's": "#a82332",
            "KFC": "#ae2732",
            "Popeyes": "#b34c18",
            "Dunkin'": "#ae4716",
            "Starbucks": "#165741",
            "Domino's": "#16658c",
            "Pizza Hut": "#a3232b",
            "Target": "#ad1c29",
            "Dollar General": "#39352b",
            "CVS": "#ad2630",
            "Walgreens": "#a42330",
        }
        d.rectangle((0, 0, W, 168), fill=colors[name])
        label(W / 2, 84, name, 94)
        glazing(75, 1125, 210, 466)
    return img
