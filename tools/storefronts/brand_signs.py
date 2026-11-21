"""Street-facing sign artwork. Catalog names remain stable for map matching and metadata."""
from PIL import Image, ImageDraw, ImageFont


LETTERING = {
    "AMC Theatres": "amc", "Regal Cinemas": "REGAL", "Cinemark": "CINEMARK",
    "Sonic Drive-In": "SONIC", "Dick's Sporting Goods": "DICK’S",
    "Burlington": "BURLINGTON", "Ross Dress for Less": "ROSS",
    "Sprouts Farmers Market": "SPROUTS", "Jersey Mike's Subs": "Jersey Mike’s",
    "Harbor Freight Tools": "HARBOR FREIGHT",
    "T.J. Maxx": "tj·maxx", "Old Navy": "OLD NAVY", "Gap": "GAP",
    "Uniqlo": "UNIQLO", "Zara": "ZARA", "Sephora": "SEPHORA",
    "Hollister": "HOLLISTER", "Athleta": "ATHLETA", "Lululemon": "lululemon",
    "Meijer": "meijer", "The LEGO Store": "LEGO",
    "The Fresh Market": "THE FRESH MARKET", "Ulta Beauty": "ULTA",
    "Petco": "petco", "Lidl": "LIDL",
    "Smith's Food and Drug": "Smith’s", "Fry's Food Stores": "Fry’s",
    "Freddy's Frozen Custard & Steakburgers": "Freddy’s",
}


def curve(start, segments):
    """Sample connected cubic curves in artwork coordinates."""
    points = [start]
    for control1, control2, end in segments:
        x, y = points[-1]
        for i in range(1, 17):
            t = i / 16
            a = 1 - t
            points.append((a**3*x + 3*a*a*t*control1[0] + 3*a*t*t*control2[0] + t**3*end[0],
                           a**3*y + 3*a*a*t*control1[1] + 3*a*t*t*control2[1] + t**3*end[1]))
    return points


def apple():
    mark = Image.new("L", (128, 148))
    pen = ImageDraw.Draw(mark)
    body = curve((64, 39), [
        ((50,39),(43,27),(25,35)), ((6,42),(2,62),(5,82)),
        ((8,104),(24,137),(38,139)), ((49,142),(54,132),(66,132)),
        ((79,131),(84,142),(95,135)), ((105,129),(114,111),(119,98)),
        ((96,89),(90,65),(113,50)), ((105,37),(93,31),(82,33)),
        ((74,34),(68,39),(64,39)),
    ])
    pen.polygon(body, fill=255)
    pen.polygon(curve((64,33), [((63,17),(74,5),(91,2)), ((92,16),(79,32),(64,33))]), fill=255)
    return mark


def freddys():
    # Joined strokes keep the short word readable at a distance, including the two tall d's.
    mask = Image.new("L", (211, 92))
    pen = ImageDraw.Draw(mask)

    def stroke(start, *segments, width=5):
        pen.line(curve(start, segments), fill=255, width=width, joint="curve")

    stroke((5,32), ((1,17),(36,11),(55,14)), ((61,16),(60,22),(57,24)))
    stroke((39,16), ((31,25),(25,49),(16,64)), width=7)
    stroke((11,44), ((20,37),(37,38),(44,36)), width=4)
    stroke((31,58), ((39,52),(43,41),(46,36)), ((50,31),(52,39),(47,43)),
           ((55,37),(61,33),(58,42)), ((55,49),(49,58),(56,56)))
    stroke((56,56), ((70,52),(77,38),(70,38)), ((58,38),(54,66),(74,55)))
    for x in (76,101):
        stroke((x,54), ((x+4,35),(x+16,34),(x+15,43)), ((x+7,62),(x-1,60),(x,54)),
               ((x+10,47),(x+22,20),(x+26,13)), ((x+29,22),(x+13,47),(x+13,55)),
               ((x+14,60),(x+21,52),(x+25,48)))
    stroke((128,41), ((121,57),(124,65),(140,45)), ((150,27),(145,42),(137,63)),
           ((126,90),(111,84),(121,73)), ((134,62),(148,58),(158,48)))
    stroke((159,30), ((164,24),(165,21),(163,18)), width=4)
    stroke((155,56), ((160,52),(170,40),(175,34)), ((172,43),(185,49),(169,57)),
           ((165,60),(159,58),(161,55)), ((175,61),(187,51),(197,43)))
    return mask


def centered_mark(canvas, mask, color):
    mask = mask.crop(mask.getbbox())
    scale = min(canvas.width*.94/mask.width, canvas.height*.91/mask.height)
    mask = mask.resize((max(1,round(mask.width*scale)), max(1,round(mask.height*scale))), Image.Resampling.LANCZOS)
    canvas.paste(color, ((canvas.width-mask.width)//2,(canvas.height-mask.height)//2,
                         (canvas.width-mask.width)//2+mask.width,(canvas.height-mask.height)//2+mask.height), mask)


def domino_tile():
    tile = Image.new("RGB", (88, 176), "#f7f6f0")
    d = ImageDraw.Draw(tile)
    d.rectangle((2,2,85,85), fill="#bd2335")
    d.rectangle((2,90,85,173), fill="#17649a")
    for x,y in ((44,44),(24,112),(64,150)):
        d.ellipse((x-8,y-8,x+8,y+8), fill="#ffffff")
    return tile.rotate(-42, Image.Resampling.BICUBIC, expand=True, fillcolor="#f7f6f0")


def symbol_and_name(symbol, label, font_path, width, height_m, surface, ink):
    """Fit a horizontal or stacked mark, with a symbol fallback for small panels."""
    paper = symbol.getpixel((0, 0))
    font = ImageFont.truetype(font_path, 112)
    box = font.getbbox(label)
    word = Image.new("RGB", (box[2]-box[0], box[3]-box[1]), paper)
    ImageDraw.Draw(word).text((-box[0], -box[1]), label, font=font, fill=ink)
    gap = 14
    candidates = [
        ((symbol.width + gap + word.width, max(symbol.height, word.height)), False),
        ((max(symbol.width, word.width), symbol.height + gap + word.height), True),
    ]
    def fit(size):
        return min(width * .94 / size[0], 184 / size[1])

    size, stacked = max(candidates, key=lambda choice: fit(choice[0]))
    # Estimated letter height on the actual panel. Roadside signs are read from farther away.
    minimum = .22 if surface == "roadside" else .14
    if surface == "projecting" or word.height * fit(size) * height_m / 200 < minimum:
        master = symbol
    else:
        master = Image.new("RGB", size, paper)
        if stacked:
            master.paste(symbol, ((size[0]-symbol.width)//2, 0))
            master.paste(word, ((size[0]-word.width)//2, symbol.height+gap))
        else:
            master.paste(symbol, (0, (size[1]-symbol.height)//2))
            master.paste(word, (symbol.width+gap, (size[1]-word.height)//2))
    scale = fit(master.size)
    art = master.resize((max(1, round(master.width*scale)), max(1, round(master.height*scale))),
                        Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, 200), paper)
    canvas.paste(art, ((width-art.width)//2, (200-art.height)//2))
    return canvas


def burger_king(font_path):
    """A compact two-line restaurant badge that retains its proportions on wide fascias."""
    paper, bun, ink = "#fff1db", "#f5a122", "#c62b20"
    badge = Image.new("RGB", (220, 200), paper)
    pen = ImageDraw.Draw(badge)
    pen.polygon(curve((21, 51), [
        ((22, -9), (195, -9), (199, 51)),
        ((153, 47), (65, 47), (21, 51)),
    ]), fill=bun)
    pen.polygon(curve((19, 154), [
        ((70, 157), (151, 157), (201, 154)),
        ((192, 205), (28, 205), (19, 154)),
    ]), fill=bun)
    font = ImageFont.truetype(font_path, 92)
    for label, top, width, height in (("BURGER", 54, 194, 43), ("KING", 101, 186, 49)):
        left, upper, right, lower = font.getbbox(label)
        mask = Image.new("L", (right-left, lower-upper))
        ImageDraw.Draw(mask).text((-left, -upper), label, font=font, fill=255)
        mask = mask.resize((width, height), Image.Resampling.LANCZOS)
        x = (badge.width-width)//2
        badge.paste(ink, (x, top, x+width, top+height), mask)
    return badge


# Catalog keys stay the same across the matcher, facade, tenant and roadside paths.
SYMBOL_ONLY = {"Apple Store": (apple, "#34383a", "#f9faf8")}
SCRIPT_MARKS = {"Freddy's Frozen Custard & Steakburgers": (freddys, "#fff4df", "#af1d2d")}
SYMBOL_AND_NAME = {"Domino's": (domino_tile, "#17649a")}


def artwork(name, width, font_path, height_m, surface):
    """Return custom artwork when the storefront uses a symbol or a distinctive lockup."""
    if name in SYMBOL_AND_NAME:
        symbol, ink = SYMBOL_AND_NAME[name]
        return symbol_and_name(symbol(), LETTERING.get(name, name), font_path, width, height_m, surface, ink)
    mark = SYMBOL_ONLY.get(name) or SCRIPT_MARKS.get(name)
    if mark:
        mask, paper, ink = mark
        img = Image.new("RGB", (width, 200), paper)
        centered_mark(img, mask(), ink)
        return img
    if name == "In-N-Out Burger":
        master = Image.new("RGB", (480,200), "#fff1da")
        pen = ImageDraw.Draw(master)
        pen.polygon([(18,100),(18,67),(335,67),(364,39),(339,34),(411,9),(403,73),
                     (387,56),(350,100)], fill="#f7be26", outline="#b5262e", width=5)
        pen.text((240,103), "IN-N-OUT", anchor="mt", font=ImageFont.truetype(font_path,86), fill="#ad2028")
    elif name == "Burger King":
        master = burger_king(font_path)
    elif name == "Best Buy":
        master = Image.new("RGB", (340,200), "#174579")
        d = ImageDraw.Draw(master)
        font = ImageFont.truetype(font_path, 99)
        d.text((22,2), "BEST", font=font, fill="white", anchor="lt")
        d.text((22,89), "BUY", font=font, fill="white", anchor="lt")
        d.polygon([(246,152),(260,138),(316,138),(316,171),(260,171)], fill="#ffda30")
        d.ellipse((255,152,259,156),fill="#174579")
    else:
        return None
    factor = min(width*.96/master.width, 196/master.height)
    resized = master.resize((max(1,round(master.width*factor)),max(1,round(master.height*factor))), Image.Resampling.LANCZOS)
    result = Image.new("RGB",(width,200),master.getpixel((0,0)))
    result.paste(resized,((width-resized.width)//2,(200-resized.height)//2))
    return result
