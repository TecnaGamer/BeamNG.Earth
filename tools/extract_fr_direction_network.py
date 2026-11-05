"""Extract a compact road network from a local Geofabrik France PBF.

Development step, requires pyosmium and NumPy. A reference pass limits the
coordinate index to useful nodes. No part of this step runs in the game server.
"""

from array import array
from collections import Counter
import argparse
import json
from itertools import chain
from pathlib import Path

import numpy as np
import osmium

CLASSES = (
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "service",
)
PLACES = {"city", "town", "village", "hamlet", "suburb"}
POIS = {
    "townhall": "Mairie",
    "hospital": "Hôpital",
    "library": "Bibliothèque",
    "community_centre": "Salle communale",
    "sports_centre": "Complexe sportif",
    "station": "Gare",
    "museum": "Musée",
    "castle": "Château",
    "parking": "Parking",
}
SITE_KEYS = {
    "amenity": {"townhall", "hospital", "library", "community_centre", "parking"},
    "leisure": {"sports_centre"},
    "railway": {"station"},
    "tourism": {"museum"},
    "historic": {"castle"},
}
BARRIERS = {"bollard", "block", "gate", "lift_gate", "cycle_barrier", "bus_trap"}
PUBLIC = {"yes", "permissive", "designated", "official"}
RESTRICTED = {
    "no",
    "private",
    "destination",
    "customers",
    "delivery",
    "agricultural",
    "forestry",
    "permit",
}
ROUNDABOUT = 1
LINK = 2
SEPARATED = 4


def is_public(tags):
    access = next(
        (tags[k] for k in ("motorcar", "motor_vehicle", "vehicle", "access") if k in tags), ""
    )
    return access not in RESTRICTED and not any(
        key in tags
        for key in (
            "motorcar:conditional",
            "motor_vehicle:conditional",
            "vehicle:conditional",
            "access:conditional",
        )
    )


def direction(tags, highway):
    oneway = tags.get("oneway", "")
    if oneway in {"-1", "reverse"}:
        return -1
    if oneway in {"no", "false", "0"}:
        return 0
    return int(
        oneway in {"yes", "true", "1"}
        or highway == "motorway"
        or tags.get("junction") == "roundabout"
    )


def target(tags):
    name = tags.get("name", "").strip()
    if not name or len(name) > 100:
        return None
    place = tags.get("place")
    if place in PLACES:
        return place, name
    kind = next(
        (
            tags.get(key)
            for key in ("amenity", "leisure", "railway", "tourism", "historic")
            if tags.get(key) in POIS
        ),
        None,
    )
    if kind == "station" and (
        tags.get("station") in {"subway", "light_rail"} or tags.get("disused") == "yes"
    ):
        return None
    if kind == "parking" and not is_public(tags):
        return None
    if kind:
        if kind == "station":
            return kind, "Gare"
        if kind == "townhall":
            return kind, "Mairie"
        return kind, name
    return None


def destination_tags(tags):
    return {k: v for k, v in tags if k == "destination" or k.startswith("destination:")}


def _source_entities(source):
    filters = [("highway", c + suffix) for c in CLASSES for suffix in ("", "_link")]
    filters += [(key, value) for key, values in SITE_KEYS.items() for value in sorted(values)]
    filters += [("place", kind) for kind in sorted(PLACES)] + [
        ("barrier", kind) for kind in sorted(BARRIERS)
    ]
    filters += [
        ("traffic_sign", "destination"),
        ("type", "destination_sign"),
        ("type", "destinationsign"),
        ("type", "restriction"),
    ]
    pool = osmium.io.ThreadPool(6, 24)
    osm = osmium.osm
    track = osmium.IdTracker()
    ways = osmium.FileProcessor(source, entities=osm.WAY | osm.RELATION, thread_pool=pool)
    timestamp = ways.header.get("osmosis_replication_timestamp")
    for count, obj in enumerate(ways.with_filter(osmium.filter.TagFilter(*filters)), 1):
        track.add_references(obj)
        if count % 1_000_000 == 0:
            print(f"Indexed references for {count:,} OSM objects", flush=True)

    print("Reading coordinates of referenced nodes", flush=True)
    index = osmium.index.create_map("sparse_mem_array")
    locations = osmium.NodeLocationsForWays(index)
    locations.ignore_errors()
    # Apply both handlers in native code. The unreferenced nodes never enter
    # the in-memory coordinate array and the index stays sorted by node ID.
    with osmium.io.Reader(source, osm.NODE, thread_pool=pool) as reader:
        osmium.apply(reader, track.id_filter(), locations)
    print(f"Coordinate index uses {index.used_memory() / 2**20:,.0f} MiB", flush=True)
    del track

    # Tagged places and barriers can be outside the referenced node set.
    tagged_nodes = osmium.FileProcessor(source, entities=osm.NODE, thread_pool=pool).with_filter(
        osmium.filter.TagFilter(*filters)
    )
    roads = (
        osmium.FileProcessor(source, entities=osm.WAY | osm.RELATION, thread_pool=pool)
        .with_filter(osmium.filter.TagFilter(*filters))
        .with_filter(locations)
    )
    return timestamp, index, chain(tagged_nodes, roads)


def extract(source, output):
    output.mkdir(parents=True, exist_ok=True)
    nodes, xy, offsets = array("Q"), array("i"), array("I", [0])
    wayids, kinds, dirs, flags, refs = array("Q"), array("B"), array("b"), array("B"), array("I")
    ref_index = {"": 0}
    notes = {
        "targets": [],
        "tags": {},
        "mapped_nodes": [],
        "relations": [],
        "restrictions": [],
        "barriers": [],
    }
    counts = Counter()
    notes["timestamp"], location_index, entities = _source_entities(source)
    for obj in entities:
        tags = obj.tags
        if obj.is_node():
            if not obj.location.valid():
                continue
            if site := target(tags):
                notes["targets"].append([*site, obj.location.x, obj.location.y, obj.id])
            if tags.get("traffic_sign") == "destination":
                notes["mapped_nodes"].append([obj.id, obj.location.x, obj.location.y, dict(tags)])
            if tags.get("barrier") in BARRIERS and not any(
                tags.get(k) in PUBLIC for k in ("motorcar", "motor_vehicle", "vehicle", "access")
            ):
                notes["barriers"].append(obj.id)
            continue
        if obj.is_relation():
            if tags.get("type") in {"destination_sign", "destinationsign", "restriction"}:
                name = "restrictions" if tags.get("type") == "restriction" else "relations"
                notes[name].append(
                    [obj.id, dict(tags), [(m.type, m.ref, m.role) for m in obj.members]]
                )
            continue
        highway = tags.get("highway", "")
        base = highway.removesuffix("_link")
        if base not in CLASSES or not is_public(tags):
            # Named establishments are often polygons rather than POI nodes.
            site = target(tags)
            if site and len(obj.nodes) > 2:
                locs = [n.location for n in obj.nodes if n.location.valid()]
                if locs:
                    notes["targets"].append(
                        [
                            *site,
                            sum(p.x for p in locs) // len(locs),
                            sum(p.y for p in locs) // len(locs),
                            -obj.id,
                        ]
                    )
            continue
        if tags.get("oneway") in {"reversible", "alternating"} or tags.get("oneway:conditional"):
            continue
        if base == "service" and tags.get("service") in {
            "driveway",
            "parking_aisle",
            "drive-through",
            "emergency_access",
        }:
            continue
        locations = [(n.ref, n.location.x, n.location.y) for n in obj.nodes if n.location.valid()]
        if len(locations) != len(obj.nodes) or len(locations) < 2:
            continue
        for ident, x, y in locations:
            nodes.append(ident)
            xy.extend((x, y))
        offsets.append(len(nodes))
        wayids.append(obj.id)
        kinds.append(CLASSES.index(base))
        dirs.append(direction(tags, highway))
        flags.append(
            (ROUNDABOUT if tags.get("junction") == "roundabout" else 0)
            | (LINK if highway != base else 0)
            | (
                SEPARATED
                if tags.get("bridge", "no") != "no" or tags.get("tunnel", "no") != "no"
                else 0
            )
        )
        ref = tags.get("ref", "").split(";")[0].strip()
        refs.append(ref_index.setdefault(ref, len(ref_index)))
        if mapped := destination_tags(tags):
            notes["tags"][str(obj.id)] = mapped
            counts["destination:" + highway] += 1
        counts["road:" + highway] += 1
        if len(wayids) % 500_000 == 0:
            print(f"{len(wayids):,} roads extracted", flush=True)
    notes["relation_nodes"] = {}
    for ident in sorted(
        {ident for _, _, members in notes["relations"] for typ, ident, _ in members if typ == "n"}
    ):
        try:
            location = location_index.get(ident)
        except KeyError:
            continue
        if location.valid():
            notes["relation_nodes"][str(ident)] = [location.x, location.y]

    notes["refs"] = list(ref_index)
    notes["counts"] = dict(counts)
    np.savez(
        output / "roads.npz",
        nodes=np.asarray(nodes),
        xy=np.asarray(xy).reshape(-1, 2),
        offsets=np.asarray(offsets),
        wayids=np.asarray(wayids),
        kinds=np.asarray(kinds),
        dirs=np.asarray(dirs),
        flags=np.asarray(flags),
        refs=np.asarray(refs),
    )
    temporary = output / "notes.partial"
    temporary.write_text(json.dumps(notes, ensure_ascii=False), encoding="utf-8")
    temporary.replace(output / "notes.json")
    print(
        json.dumps(
            {
                "roads": len(wayids),
                "points": len(nodes),
                "targets": len(notes["targets"]),
                "relations": len(notes["relations"]),
                "destination_ways": len(notes["tags"]),
                "restrictions": len(notes["restrictions"]),
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    extract(args.source, args.output)
