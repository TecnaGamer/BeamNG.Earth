"""Opt-in regional French house models (BNE_FR_HOUSES=on).

Known non-house types always retain the upstream model. As requested, untyped
small buildings may be inferred as houses only within mapped residential land.
The regional shares below are visual presets, not measured housing statistics.
"""
import math
import os

import numpy as np

import house_regions

ON = os.environ.get("BNE_FR_HOUSES", "off").lower() in {"on", "true", "1"}
INFER = os.environ.get("BNE_FR_HOUSES_INFER", "on").lower() not in {"off", "false", "0"}
VERSION = "fr-houses-v1"
MATERIALS = tuple("bne_fr_house_" + k for k in
                  ("stucco", "stone", "brick", "wood", "roller", "tile", "canal", "slate", "trim", "glass", "glass_lit", "metal"))
HOUSE_CLASSES = {"house", "detached", "dwelling_house", "semidetached_house", "semi", "bungalow", "terrace"}
UNTYPED = {"", "yes", "building", "residential"}
NONHOUSE_NAMES = ("école", "ecole", "mairie", "chapelle", "église", "eglise", "mosquée", "collège", "lycée",
                  "hôpital", "clinique", "gymnase", "entrepôt", "usine", "supermarché", "restaurant", "hôtel", "hotel")

# Family, relative weight. All are varied again in roof, shutters and palette.
FAMILIES = {
    "alsace": (("render", 40), ("halfhip", 20), ("timber", 40)),
    "brittany": (("granite", 55), ("render", 45)),
    "normandy": (("render", 55), ("stone", 25), ("timber", 20)),
    "north": (("brick", 75), ("render", 25)),
    "paris_basin": (("render", 68), ("meuliere", 32)),
    "toulouse": (("brick", 65), ("render", 35)),
    "basque": (("render", 70), ("basque_frame", 30)),
    "mediterranean": (("render", 75), ("stone", 25)),
    "alpine": (("chalet", 60), ("render", 35), ("stone", 5)),
    "pyrenees": (("stone", 55), ("render", 45)),
    "southwest": (("render", 75), ("stone", 25)),
    "loire": (("render", 65), ("limestone", 35)),
    "east": (("render", 70), ("stone", 30)),
    "central": (("render", 55), ("stone", 45)),
}
NEUTRALS = ((.96, .94, .88), (.88, .85, .76), (.94, .90, .78), (.88, .87, .83), (.95, .94, .91))
ALSACE = NEUTRALS + ((.92, .79, .69), (.79, .88, .95), (.90, .84, .76), (.82, .92, .79), (.98, .91, .68), (.97, .83, .85))
ALSACE_ROSY = ((.92, .79, .69), (.97, .83, .85))
SOUTH = ((.96, .92, .82), (.93, .84, .70), (.94, .87, .77), (.90, .78, .65), (.96, .95, .90))
WOOD = ((.30, .20, .14), (.43, .31, .21), (.34, .28, .23))
SHUTTERS = ((.88, .88, .84), (.29, .39, .42), (.27, .36, .26), (.37, .26, .19), (.42, .24, .22), (.26, .32, .41))
BASE_BASQUE = ((.41, .12, .10), (.14, .29, .21), (.17, .28, .39))


def _number(value):
    try:
        return float(value) if math.isfinite(float(value)) else None
    except (ValueError, TypeError):
        return None


def eligible(spec, attrs):
    import buildings as b
    cls = str(attrs.get("class") or "").lower()
    if cls not in HOUSE_CLASSES | UNTYPED or attrs.get("_fr_part") or attrs.get("building_height"):
        return False
    if any(k in str(attrs.get("name") or "").lower() for k in NONHOUSE_NAMES):
        return False
    if spec.shop or spec.steeple or spec.deck or (attrs.get("min_height") or 0) > .5:
        return False
    if str(attrs.get("roof_shape") or "").lower().replace("_", "-") not in {"", "gabled", "hipped", "half-hipped",
                                                                          "pyramidal", "flat"}:
        return False
    if str(attrs.get("facade_material") or "").lower() in {"glass", "metal", "steel", "aluminium", "copper"}:
        return False
    area = abs(b._area2(spec.ring)) / 2
    _, _, hl, hw = b.oriented_box(spec.ring)
    floors, height = _number(attrs.get("num_floors")), _number(attrs.get("height"))
    if height is not None and height < 2.5:
        return False
    if not (hw >= 2.2 and hl <= 16 and area / (4 * hl * hw) >= .78):
        return False
    if cls in HOUSE_CLASSES:
        return 35 <= area <= 450 and (floors is None or floors <= 3) and (height is None or height <= 14)
    return (INFER and (cls == "residential" or attrs.get("_fr_residential") is True)
            and 45 <= area <= 220 and hw <= 7.5 and hl <= 12 and hl / hw <= 3.5
            and (floors is None or floors <= 2) and (height is None or height <= 9)
            and str(attrs.get("roof_shape") or "").lower() != "flat")


def adapt(spec, attrs, lat, lon):
    if not ON or not eligible(spec, attrs):
        return spec
    import buildings as b
    lat, lon = attrs.get("_fr_location", (lat, lon))
    region = house_regions.profile(lat, lon, attrs.get("_fr_altitude"))
    if region is None:
        return spec
    h = lambda key: b._hash01(spec.seed, "fr-house-v1", key)
    pick = lambda values, key: values[min(int(h(key) * len(values)), len(values) - 1)]
    choice = h("family") * 100
    family = FAMILIES[region][-1][0]
    for name, weight in FAMILIES[region]:
        choice -= weight
        if choice < 0:
            family = name
            break
    floors = _number(attrs.get("num_floors"))
    if floors is None:
        floors = 1 if attrs.get("class") == "bungalow" or h("floors") < .30 else 2
    floors = max(1, min(3, int(round(floors))))
    if family in {"timber", "basque_frame", "chalet"} and floors == 1:
        family = "render"
    fm = str(attrs.get("facade_material") or "").lower()
    if fm in {"brick", "bricks"}:
        family = "brick"
    elif fm in b.STONE_FACADES:
        family = "granite" if fm == "granite" else "stone"
    elif fm in {"wood", "timber"}:
        family = "wood"
    elif fm and family in {"timber", "basque_frame", "chalet"}:
        family = "render"                    # explicit full facade material wins
    palette = ALSACE if region == "alsace" else SOUTH if region in {"basque", "mediterranean", "southwest", "toulouse"} else NEUTRALS
    color = pick(palette, "wall")
    # Keep existing non-rosy facades stable. Retain only 15% of the two rosy
    # entries: about 2.7% of default Alsatian wall colours, instead of 18.2%.
    # This is a visual preset, not a measured housing share; mapped colours win below.
    if region == "alsace" and color in ALSACE_ROSY and h("rare rosy wall") >= .15:
        color = pick(NEUTRALS, "neutral wall")
    if region == "basque":
        color = (.97, .96, .92)
    mat = "stucco"
    if family in {"stone", "granite", "limestone", "meuliere"}:
        mat = "stone"
        color = (.67, .67, .63) if family == "granite" else (.89, .82, .65) if family == "meuliere" else (.94, .90, .75) if family == "limestone" else pick(((.88, .84, .72), (.76, .75, .68), (.91, .85, .71)), "stone")
    if family == "brick":
        mat = "brick"
        color = pick(((.82, .45, .29), (.78, .40, .26), (.84, .48, .32)), "brick") if region == "toulouse" else pick(((.53, .29, .22), (.62, .32, .23), (.49, .29, .24)), "brick")
    if family == "wood":
        mat, color = "wood", pick(WOOD, "wood")
    color = b.parse_color(attrs.get("facade_color")) or color
    roof_given = str(attrs.get("roof_shape") or "").lower().replace("_", "-")
    roof = roof_given or ("half-hipped" if family == "halfhip" else "hipped" if family == "render" and h("hip") < .18 else "gabled")
    if roof == "pyramidal":
        roof = "hipped"
    pitch = 27. if region in {"alpine", "basque"} else 24. if region in {"mediterranean", "southwest", "toulouse"} else 44. if region in {"brittany", "normandy", "pyrenees"} else 40.
    if region == "alsace":
        pitch = 48. if family == "timber" else 44. if family == "halfhip" else 40.
    _, _, hl, hw = b.oriented_box(spec.ring)
    # Preserve the original outline when its oriented box would add much area.
    if b._area2(spec.ring) / (8 * hl * hw) < .86:
        roof = "flat"
        family = "render" if family in {"timber", "halfhip"} else family
    rise = 0. if roof == "flat" else hw * math.tan(math.radians(pitch))
    rh = _number(attrs.get("roof_height"))
    if rh is not None and rh > 8.:
        return spec                          # implausible/special geometry stays upstream
    if rh is not None and rh > 0 and roof != "flat":
        rise = rh
    total_h = _number(attrs.get("height"))
    if total_h is not None and _number(attrs.get("num_floors")) is None:
        floors = max(1, min(3, int(round(max(2.5, total_h - rise) / 2.85))))
        if floors == 1 and family in {"timber", "basque_frame", "chalet"}:
            family = "render"
    if total_h is not None:
        rise = min(rise, max(0., total_h - 2.3 * floors))
        wall_h = total_h - rise
    else:
        wall_h = floors * 2.85 + .2
    if roof != "flat":
        if rise < .10:
            return spec
        pitch = math.degrees(math.atan2(rise, hw))
    roof_kind = "canal" if region in {"basque", "mediterranean", "southwest", "toulouse"} else "slate" if region in {"brittany", "pyrenees"} else "tile"
    if region in {"loire", "normandy", "paris_basin", "alpine", "central"} and h("slate") < .65:
        roof_kind = "slate"
    rmat = str(attrs.get("roof_material") or "").lower()
    if rmat in {"slate", "stone"}:
        roof_kind = "slate"
    elif rmat in {"tiles", "tile", "clay", "roof_tiles", "terracotta"}:
        roof_kind = "canal" if region in {"basque", "mediterranean", "southwest", "toulouse"} else "tile"
    roof_rgb = pick(((.75, .42, .28), (.64, .35, .25), (.69, .43, .30), (.54, .38, .30)), "roof")
    if roof_kind == "slate":
        roof_rgb = pick(((.27, .30, .32), (.32, .35, .37), (.25, .27, .29)), "slate")
    roof_rgb = b.parse_color(attrs.get("roof_color")) or roof_rgb
    roof_look = b.custom_look("bne_fr_house_" + roof_kind, roof_rgb)
    if roof == "flat" or rmat in {"metal", "zinc", "steel", "copper", "glass", "concrete", "bitumen", "asphalt", "shingle", "shingles"}:
        roof_look = spec.roof_mat             # retain mapped specialist materials
    trim = pick(BASE_BASQUE if region == "basque" else SHUTTERS, "shutters")
    timber_rgb = trim if family == "basque_frame" else pick(WOOD, "timber")
    roller = region == "alsace" and family in {"render", "halfhip"} and h("roller") < .70
    shutter_rgb = (.88, .88, .85) if roller and h("white roller") < .75 else trim
    spec.fr_house = dict(region=region, family=family, inferred=str(attrs.get("class") or "").lower() in UNTYPED,
                         wall=b.custom_look("bne_fr_house_" + mat, color),
                         trim=b.custom_look("bne_fr_house_trim", (.91, .90, .85)),
                         shutter=b.custom_look("bne_fr_house_roller" if roller else "bne_fr_house_wood", shutter_rgb),
                         timber=b.custom_look("bne_fr_house_wood", timber_rgb), roller=roller,
                         panes=0 if roller else 2 if family == "timber" or (region in {"brittany", "loire", "pyrenees", "central"} and h("panes") < .65) else 0,
                         plinth=b.custom_look("bne_fr_house_stucco", (.63, .48, .43) if region == "alsace" else (.63, .61, .56)),
                         balcony=(floors >= 2 and not attrs.get("_fr_neighbours")
                                  and (family == "chalet" or h("balcony") < (.32 if region == "basque" else .16))),
                         chimney=h("chimney") < .85, dormer=(roof == "gabled" and pitch >= 35 and h("dormer") < .28),
                         eave=.65 if family == "chalet" else .08 if attrs.get("_fr_neighbours") else .28)
    spec.floors, spec.height, spec.floor_h = floors, wall_h, wall_h / floors
    spec.roof, spec.pitch, spec.roof_mat = roof, pitch, roof_look
    spec.facade = spec.wall = spec.fr_house["wall"]
    spec.band, spec.plain, spec.porch, spec.stoop, spec.shop = None, True, False, None, False
    return spec


class ResidentialLand:
    """Mapped residential land, with institutional/commercial exclusions on top."""
    DENY = {"industrial", "commercial", "retail", "education", "school", "university", "college", "hospital",
            "healthcare", "religious", "military", "cemetery", "railway", "prison", "construction"}

    def __init__(self, layers):
        self.areas = []             # (label, outer (n, 2) lon/lat, [holes], bbox)
        seen = set()
        for f in layers:
            if f is None or "class" not in f.attrs:
                continue
            for i in range(len(f)):
                cls = str(f.attrs["class"][i])
                sub = str(f.attrs["subtype"][i]) if "subtype" in f.attrs else ""
                label = False if cls in self.DENY or sub in self.DENY else True if cls == "residential" or sub == "residential" else None
                if label is None or str(f.ids[i]) in seen or int(f.kind[i]) != 3:
                    continue
                seen.add(str(f.ids[i]))
                for p in f.parts(i):
                    rings = [np.asarray(r, np.float64) for r in p]
                    if not rings or any(r.ndim != 2 or r.shape[1] != 2 or len(r) < 4 or not np.isfinite(r).all()
                                        for r in rings):
                        continue             # malformed land use cannot establish a house
                    o = rings[0]
                    self.areas.append((label, o, rings[1:], (*o.min(0), *o.max(0))))

    def at_many(self, lats, lons):
        """Per point: inside mapped residential land and no excluded land (the native ring test, PR #505's
        per-point Python rings before)."""
        import polygon
        lats, lons = np.asarray(lats, np.float64).ravel(), np.asarray(lons, np.float64).ravel()
        res = np.zeros(len(lats), bool)
        deny = np.zeros(len(lats), bool)
        pts = np.column_stack((lons, lats))
        for label, outer, holes, (bx0, by0, bx1, by1) in self.areas:
            near = np.flatnonzero((lons >= bx0) & (lons <= bx1) & (lats >= by0) & (lats <= by1))
            if not len(near):
                continue
            q = pts[near]
            inside = polygon.contains_points(outer, q)
            for h in holes:
                if inside.any():
                    inside &= ~polygon.contains_points(h, q)
            (res if label else deny)[near[inside]] = True
        return res & ~deny

    def at(self, lat, lon):
        return bool(self.at_many([lat], [lon])[0])


def needs_residential_land(frame, box):
    if not ON or not INFER:
        return False
    import countrybounds
    x0, y0, x1, y1 = box
    x = np.array([x0, x0, x1, x1, (x0 + x1) / 2])
    y = np.array([y0, y1, y0, y1, (y0 + y1) / 2])
    lat, lon, _ = frame.enu_to_geodetic(x, y, 0.)
    pts = [(float(la), float(lo)) for la, lo in zip(lat, lon) if -5.3 < lo < 9.8 and 41.3 < la < 51.2]
    return bool(pts) and "FR" in countrybounds.bundled().codes_at(pts)
