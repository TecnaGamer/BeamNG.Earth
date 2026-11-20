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
        "walmart supercenter": "walmart",
        "walmart neighborhood market": "walmart",
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


def parking_frontage(coords, point, parking):
    """Estimate a short sign panel on a wall supported by both a tenant point and parking in front."""
    if not len(parking):
        return None
    scale = np.array([111320 * np.cos(np.radians(point[1])), 111132])
    ring = (coords - point) * scale
    edges = np.roll(ring, -1, axis=0) - ring
    widths = np.linalg.norm(edges, axis=1)
    along = np.clip(-np.sum(ring * edges, axis=1) / widths**2, 0, 1)
    closest = ring + along[:, None] * edges
    distances = np.linalg.norm(closest, axis=1)
    first, second = distances.argsort()[:2]
    if distances[first] > 15 or distances[second] < distances[first] + 5:
        return None
    axis = edges[first] / widths[first]
    if np.sum(ring[:, 0] * edges[:, 1] - ring[:, 1] * edges[:, 0]) < 0:
        axis = -axis
    normal = np.array([axis[1], -axis[0]])
    ahead = (parking - point) * scale - closest[first]
    depth = ahead @ normal
    if not np.any((depth > 2) & (depth < 45) & (np.abs(ahead @ axis) < 20)):
        return None
    half = min(10., min(along[first], 1-along[first]) * widths[first] - 1)
    if half < 3:
        return None
    return point + np.array([closest[first] - half * axis, closest[first] + half * axis]) / scale


def matches(path):
    tree = ElementTree.parse(path)

    def tags(element):
        return {t.get("k"): t.get("v") for t in element.findall("tag")}

    nodes = {n.get("id"): n for n in tree.findall("node")}

    def point(n):
        return [float(n.get("lon")), float(n.get("lat"))]

    address_roads = {}
    parking = []
    buildings = []
    units = []
    for way in tree.findall("way"):
        refs = [n.get("ref") for n in way.findall("nd")]
        if any(ref not in nodes for ref in refs):
            continue  # Partial extracts may omit nodes outside the download boundary.
        attributes = tags(way)
        if attributes.get("name") and attributes.get("highway") in {
            "primary", "secondary", "tertiary", "residential", "unclassified", "living_street"
        } and len(refs) >= 2:
            road = np.array([point(nodes[r]) for r in refs])
            address_roads.setdefault(attributes["name"].casefold(), []).append(road)
        if attributes.get("service") == "parking_aisle":
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
    # A tagged advertising totem has its own location. Keep ordinary advertising billboards
    # on the existing billboard path; their operator need not be the advertised business.
    labels = {name.casefold(): name for _, names in CHAIN_GROUPS for name in names}
    for node in nodes.values():
        props = tags(node)
        if props.get("advertising") != "totem":
            continue
        name = labels.get((props.get("brand") or props.get("name") or "").casefold())
        if not name:
            continue
        try:
            height = float(props.get("height", 6))
            facing = float(props["direction"]) if "direction" in props else None
        except ValueError:
            continue
        if not 2 <= height <= 20:
            continue
        location = np.array(point(node))
        scale = np.array([111320*np.cos(np.radians(location[1])), 111132])
        if facing is None:
            # Pole signs read along the road. Use the tangent of its nearest segment.
            best = 100.
            for roads in address_roads.values():
                for road in roads:
                    starts = (road[:-1] - location) * scale
                    edges = np.diff(road, axis=0) * scale
                    along = np.clip(-np.sum(starts*edges, axis=1) /
                                    np.maximum(np.sum(edges**2, axis=1), 1e-12), 0, 1)
                    dist = np.linalg.norm(starts + along[:, None]*edges, axis=1)
                    k = int(dist.argmin())
                    if dist[k] < best and np.linalg.norm(edges[k]) > 0:
                        best = dist[k]
                        facing = np.degrees(np.arctan2(edges[k, 0], edges[k, 1]))
        if facing is None or not np.isfinite(facing):
            continue
        angle = np.radians(facing)
        half = np.array([np.cos(angle), -np.sin(angle)]) * min(2.5, height*.32) / scale
        yield (name, "totem_" + node.get("id"), np.array([location-half, location+half]),
               None, [], None, height)

    tenant_points = [(n.get("id"), name, np.array(point(n))) for n in nodes.values()
                     if (name := brand(tags(n))) and tags(n).get("level", "0") == "0"]
    parking = np.asarray(parking).reshape(-1, 2)
    for ident, coords, props, refs in buildings:
        lo, hi = coords.min(0), coords.max(0)
        name = brand(props)
        # Prefer a mapped tenant wall over an inferred parking-facing panel.
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
        if not name and not has_unit and props.get("building") == "retail":
            for node_id, tenant, location in tenant_points:
                if not (np.all(location > lo) and np.all(location < hi) and contains_points(coords, [location])[0]):
                    continue
                panel = parking_frontage(coords, location, parking)
                if panel is not None:
                    yield tenant, "poi_" + node_id, panel, None, [], None
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
        # When the door is unmapped, a store's addressed street identifies its public approach.
        road_points = []
        for road in address_roads.get(props.get("addr:street", "").casefold(), []):
            starts = (road[:-1] - center) * scale
            segments = np.diff(road, axis=0) * scale
            along = np.clip(-np.sum(starts * segments, axis=1) /
                            np.maximum(np.sum(segments**2, axis=1), 1e-12), 0, 1)
            nearest = starts + along[:, None] * segments
            offset = nearest[np.argmin(np.linalg.norm(nearest, axis=1))]
            if 1 <= np.linalg.norm(offset) <= np.linalg.norm((hi - lo) * scale) / 2 + 100:
                road_points.append(center + offset / scale)
        targets = road_points or doors or parking
        if len(targets) == 0:
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
