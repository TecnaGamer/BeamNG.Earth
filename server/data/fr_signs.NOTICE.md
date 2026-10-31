# French sign snapshots — ODbL 1.0

**© OpenStreetMap contributors.** These derivative databases are available under
the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
[OpenStreetMap copyright and attribution](https://www.openstreetmap.org/copyright).
They are separate from the MIT code and original mesh assets.

`fr_town_signs.json.gz` contains 43,114 mapped sign candidates and 40,917
settlement centres (cities, towns, villages and the hamlets referenced by signs).
`fr_direction_signs.json.gz` contains 2,061 candidates derived from white/black
`type=destination_sign` relations with explicit destination text and usable
outgoing geometry. Counts are source candidates, not a promise every candidate
will render: runtime country, road, orientation and clearance filters still apply.

These are metropolitan France/Corsica snapshots from Overpass on 29 October 2026,
not a global sign inventory. The town requests have several source timestamps;
the compressed JSON metadata retains them and the direction snapshot's timestamp.
Nodes/relations keep their OSM IDs; road references are retained when supplied.
Sources are restricted to FR by the separate country database. Stored JSON is the
complete editable derivative database. The fetch/build tools document queries,
selection and omissions; `fr_sign_sources.json` records source response hashes.

Names come from OSM. An unnamed mapped city-limit node may take the nearest
unambiguous named settlement within 1,500 m; these records carry `name_estimated`.
Missing town-sign orientations can be estimated from a matching settlement
centre. A separate runtime fallback may add estimated
entries at sustained built-up transitions on otherwise unmapped approaches.
These estimates are not surveyed sign positions or administrative town boundaries.
Direction arrows never invent destination names or routing distances. Unsupported
colours and ambiguous geometry are omitted; most tourist/hiking signs are excluded.

Tools: `tools/fetch_fr_sign_data.py`, `tools/build_fr_sign_data.py`,
`tools/build_fr_direction_data.py`. Requests use public Overpass only during
explicit developer acquisition; players never query it for these bundled signs.
Refreshing OSM data changes content; inspect it and update the runtime SHA pins.

- https://wiki.openstreetmap.org/wiki/Tag:traffic_sign=city_limit
- https://wiki.openstreetmap.org/wiki/Key:direction
- https://wiki.openstreetmap.org/wiki/Relation:destination_sign
- https://overpass-api.de/
