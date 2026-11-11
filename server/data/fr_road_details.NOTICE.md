# French road-detail snapshots

© OpenStreetMap contributors, licensed under the Open Database License 1.0:
https://www.openstreetmap.org/copyright
https://opendatacommons.org/licenses/odbl/1-0/

Source: Geofabrik's France extract, https://download.geofabrik.de/europe/france.html
OSM replication timestamp: 2026-11-03T21:24:23Z.
Input PBF SHA-256: `8073ddca0c3db34ff3f37daa9f7faf15a068f0ee752ab2bec6745700f2360b95`.

`fr_sos_phones.json.gz` contains 13,447 public `emergency=phone` nodes, filtered
to France's polygon. It includes roads and other locations; runtime placement
only accepts phones beside a compatible ground motorway/trunk, outside every
nearby carriageway. This is a mapping snapshot, not an exhaustive national
inventory. A retained record does not guarantee an in-game object.

`fr_road_direction_signs.json.gz` contains 2,257 usable `destination_sign`
relations: 2,076 white, 104 green, 77 blue. Labels, colours and outgoing geometry
are mapped attributes. A relation without a sign node uses its mapped junction
as a position estimate. Hiking-only routes, unknown colours, missing targets
and ambiguous geometry are omitted. Metropolitan France and Corsica are covered;
overseas territories are not included in this extract.

`fr_local_directions.zip` covers city streets, villages and rural junctions in
the same geographic area. Its `index.json` records the source snapshot, counts
and grid cells. `relation` means a mapped destination-sign relation, `road` a
destination tag on a road, and `route` a scenic estimate from a directed route
to a named place or public facility. Estimated distances are along that route
and rounded for display on some rural signs. Diagram signs use connected exits
of the actual drawn roundabout. Placement at every candidate depends on the
live road geometry, safe shoulder space and visibility.

`fr_priority_directions-*.zip` and `fr_priority_directions.json` are the
priority junction catalogue for the same extract. Routes are computed across
the directed graph using tagged access and supported turn restrictions. The
catalogue separates nearby places from regional destinations and tests each
inferred name against the junction's competing exits. The `checked_route` flag
identifies these estimates. The manifest lists the SHA-256 of the full archive
and of each distributed part. Once a catalogue built this way (its index's
`routing` 6) is the published one, `frlocaldirections.nearby` keeps only the
checked route estimates of both catalogues; an older catalogue keeps every face.

Rebuild offline (developer dependency: pyosmium; players do not need it):

```sh
python3 tools/build_fr_road_data.py france-latest.osm.pbf server/data/fr_sos_phones.json.gz --directions-output server/data/fr_road_direction_signs.json.gz
```

Rebuild the local network and directional-sign archive with pyosmium, NumPy and
SciPy. The extraction keeps a coordinate index for referenced nodes in memory.

```sh
python3 tools/extract_fr_direction_network.py france-latest.osm.pbf build/fr_direction_network
python3 tools/build_fr_local_directions.py build/fr_direction_network server/data/fr_local_directions.zip
python3 tools/extract_fr_direction_network.py --priority france-latest.osm.pbf build/fr_priority_network
python3 tools/build_fr_priority_directions.py build/fr_priority_network build/fr_priority_directions.zip
python3 tools/split_fr_priority_directions.py build/fr_priority_directions.zip server/data
```

Set `directionsigns.ROAD_DATA_SHA256` to its printed checksum; the local and
priority archives go to BeamNG.Earth-data through `tools/build_regional_data.py`
(below), whose manifest holds their checksums. Compressed JSON retains IDs,
coordinates and source provenance and is distributed under ODbL.
No public Overpass query is performed during gameplay. Motorway destinations
come separately from the mod's existing worldwide highway-sign index.

## Distribution (merge of PR #505, 2026-11-06)

This archive is no longer shipped inside BeamNG.Earth. `tools/build_regional_data.py` converts it (the borders
as latitude band files simplified to about 10 m, the cell archives as one gzip file per cell) for
BeamNG.Earth-data's GitHub Pages (`regional/`), and `server/regionaldata.py` fetches only what a player needs,
only when the regional scenery is on. The source, licence and attribution above are unchanged.
