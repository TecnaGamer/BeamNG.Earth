import itertools
from xml.etree import ElementTree

import numpy as np
from polygon import contains_points

BRANDS = {"Q38076": "McDonald's", "Q153417": "Kroger", "Q483551": "Walmart"}
HEIGHTS = {"McDonald's": 4.3, "Kroger": 7, "Walmart": 9}

# Supported businesses, grouped by the OSM business type.
CHAIN_GROUPS = [
    (
        ("amenity", {"fast_food", "restaurant", "cafe"}),
        [
            "McDonald's",
            "Burger King",
            "Wendy's",
            "Taco Bell",
            "Chick-fil-A",
            "Subway",
            "Sonic Drive-In",
            "Arby's",
            "KFC",
            "Popeyes",
            "Dunkin'",
            "Starbucks",
            "Domino's",
            "Pizza Hut",
        ],
    ),
    (
        (
            "shop",
            {
                "supermarket",
                "department_store",
                "hypermarket",
                "variety_store",
                "wholesale",
            },
        ),
        [
            "Kroger",
            "Walmart",
            "Target",
            "Dollar General",
            "ALDI",
            "Publix",
            "Costco",
            "Sam's Club",
        ],
    ),
    (
        ("shop", {"doityourself", "hardware"}),
        ["The Home Depot", "Lowe's", "Ace Hardware"],
    ),
    (("amenity", {"pharmacy"}), ["CVS", "Walgreens"]),
]
for _, names in CHAIN_GROUPS:
    for name in names:
        HEIGHTS.setdefault(name, 4.3 if names[0] == "McDonald's" else 6)


def brand(tags):
    labels = {
        tags.get(k, "").casefold().replace("\u2019", "'") for k in ("brand", "name")
    }
    labels.add(BRANDS.get(tags.get("brand:wikidata"), "").casefold())
    if "cvs pharmacy" in labels:
        labels.add("cvs")
    for alias, canonical in {
        "publix super market": "publix",
        "costco wholesale": "costco",
        "home depot": "the home depot",
        "lowe's home improvement": "lowe's",
    }.items():
        if alias in labels:
            labels.add(canonical)
    for (key, types), names in CHAIN_GROUPS:
        if tags.get(key) in types or (
            names[0] == "CVS" and tags.get("shop") == "chemist"
        ):
            for name in names:
                if name.casefold() in labels:
                    return name


def matches(path):
    tree = ElementTree.parse(path)

    def tags(element):
        return {t.get("k"): t.get("v") for t in element.findall("tag")}

    nodes = {n.get("id"): n for n in tree.findall("node")}

    def point(n):
        return [float(n.get("lon")), float(n.get("lat"))]

    parking = []
    buildings = []
    units = []
    for way in tree.findall("way"):
        refs = [n.get("ref") for n in way.findall("nd")]
        if any(ref not in nodes for ref in refs):
            continue  # Partial extracts may omit nodes outside the download boundary.
        if tags(way).get("service") == "parking_aisle":
            parking.extend(point(nodes[r]) for r in refs)
        if len(refs) < 4 or refs[0] != refs[-1]:
            continue
        props = tags(way)
        coords = np.array([point(nodes[r]) for r in refs[:-1]])
        if not np.isfinite(coords).all():
            continue
        distinct = np.any(coords != np.roll(coords, 1, axis=0), axis=1)
        coords = coords[distinct]
        refs = [r for r, keep in zip(refs[:-1], distinct) if keep]
        if len(coords) < 3:
            continue
        local = coords - coords[0]
        area2 = np.sum(
            local[:, 0] * np.roll(local[:, 1], -1)
            - local[:, 1] * np.roll(local[:, 0], -1)
        )
        if abs(area2) < 1e-16:
            continue
        refs.append(refs[0])
        if props.get("amenity") == "parking":
            parking.append(coords.mean(0))
        if brand(props) and not props.get("building") and props.get("level") == "0":
            units.append((way.get("id"), refs, brand(props)))
        if props.get("building") and props["building"] != "no":
            buildings.append((way.get("id"), coords, props, refs))
    for ident, coords, props, refs in buildings:
        lo, hi = coords.min(0), coords.max(0)
        name = brand(props)
        # A point inside a footprint identifies a tenant, but cannot establish its frontage.
        exterior = {
            frozenset((a, b)): i for i, (a, b) in enumerate(itertools.pairwise(refs))
        }
        has_unit = False
        for unit_id, unit_refs, unit_name in units:
            shared = [
                exterior[edge]
                for a, b in itertools.pairwise(unit_refs)
                if (edge := frozenset((a, b))) in exterior
            ]
            if not shared:
                continue
            interior = [point(nodes[r]) for r in unit_refs[:-1] if r not in refs]
            if not interior or not all(contains_points(coords, interior)):
                continue
            has_unit = True
            if len(shared) != 1:
                continue
            i = shared[0]
            span = coords[[i, (i + 1) % len(coords)]]
            # Host ring winding determines which side faces outside.
            ring = coords - coords[0]
            signed_area = sum(
                a[0] * b[1] - b[0] * a[1]
                for a, b in zip(ring, np.roll(ring, -1, axis=0))
            )
            if signed_area < 0:
                span = span[::-1]
            yield unit_name, unit_id, span, None, [], None
        if (
            not name
            or has_unit
            or props.get("building")
            in {"apartments", "office", "mixed_use", "residential", "hotel"}
        ):
            continue
        center = (lo + hi) / 2
        scale = np.array([111320 * np.cos(np.radians(center[1])), 111132])
        doors = [
            point(nodes[r])
            for r in refs[:-1]
            if tags(nodes[r]).get("entrance") in {"main", "yes"}
            and tags(nodes[r]).get("access") not in {"private", "no"}
        ]
        targets = doors or parking
        if not targets:
            continue
        offset = min(
            ((target - center) * scale for target in targets), key=np.linalg.norm
        )
        if (
            np.linalg.norm(offset) > np.linalg.norm((hi - lo) * scale) / 2 + 100
            or np.linalg.norm(offset) < 1
        ):
            continue
        fixtures = []
        for ref in refs[:-1]:
            attributes = tags(nodes[ref])
            dock = attributes.get("amenity") == "loading_dock"
            if dock or (
                attributes.get("entrance") in {"yes", "main"}
                and attributes.get("access") not in {"private", "no"}
            ):
                fixtures.append((point(nodes[ref]), dock))
        yield (
            name,
            ident,
            coords,
            offset / np.linalg.norm(offset),
            fixtures,
            props.get("building:material"),
        )
