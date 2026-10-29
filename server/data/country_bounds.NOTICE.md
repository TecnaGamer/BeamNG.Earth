# Country boundary database — ODbL 1.0

`country_bounds.zip` is a repackaged database from Overture Maps Foundation,
`divisions/division_area`, release **2026-09-23.1**. Selection:
`subtype IN ('country','dependency') AND is_territorial = true`, with no country
whitelist: 272 territorial records, 1,493 polygon parts, 5,758,924 vertices.

**© OpenStreetMap contributors, Overture Maps Foundation.**
This database is available under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
[OpenStreetMap copyright and attribution](https://www.openstreetmap.org/copyright).
The repository's MIT licence does not relicense this database.

All selected records identify OpenStreetMap as their geometry source. The ZIP
contains the complete derivative database in editable JSON form: one polygon's
GeoJSON coordinate rings per member, plus `index.json` with codes, retained source
records, IDs, versions and index bounds. Unused names/translations and their
provenance were removed. Original geometry coordinates, holes and islands were
retained without simplification or rounding. The source snapshot is dated
2026-09-16T00:00:00Z; extraction/build: 29 October 2026.

These codes include dependencies, overseas areas and special/disputed territories;
they are not a list of sovereign countries. Overseas codes such as GF are distinct
from FR. Extents, territorial waters, political perspectives and polar coverage
follow the source. Exact edges/contradictory overlaps return unknown in the reader.

Sources:

- https://stac.overturemaps.org/2026-09-23.1/divisions/division_area/collection.json
- https://docs.overturemaps.org/guides/divisions/
- https://docs.overturemaps.org/attribution/

Build scripts: `tools/fetch_country_bounds.py`, `tools/build_country_bounds.py`.
SHA-256: `9ef7540081e29fbb72dd69b74a44ec37cf87f5814866f7a97b85330ba9f8d846`.
The old eleven-country `country_bounds.geojson.gz` is superseded and unused.
