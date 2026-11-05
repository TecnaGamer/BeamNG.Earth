# European locality names — ODbL 1.0

`europe_places.zip` is an editable derivative of Overture Maps
`divisions/division`, release **2026-09-23.1**, extracted 29 October 2026.
It contains **370,721 named localities**, in 2,689 one-degree cells, for 55
country/territory codes. France retains its separate OSM sign/locality snapshot.
Selection: city/town/village class, plus the Vatican city-state point.
Names are the source's primary names, not invented translations.

**© OpenStreetMap contributors; Overture Maps Foundation.** Selected records
also identify Esri Community Maps and Maps Entity Variant Names (CC0).

- https://www.openstreetmap.org/copyright
- https://docs.overturemaps.org/attribution/
- https://opendatacommons.org/licenses/odbl/1-0/
- https://creativecommons.org/publicdomain/zero/1.0/

The derivative is distributed under ODbL 1.0; the repository's MIT licence does
not relicense it. Complete editable JSON is supplied inside the ZIP, with source
IDs, primary names, classes, country codes and positions. `index.json` records
the release, selection, counts and raw extraction hash. Unused translations,
population and geometry metadata are omitted. These points are NOT surveyed
traffic-sign positions or legal built-up boundaries.

Pinned source item:
https://stac.overturemaps.org/2026-09-23.1/divisions/division/00000/00000.json

Builders: `tools/fetch_european_places.py`, `tools/build_european_places.py`.
The fetcher accepts a local copy of the Parquet linked by the pinned source item.
Players use the offline ZIP, without downloads or API keys.

ZIP SHA-256: `de04ae47eac96d7172fc9091219af0d742f8ec015bda04aec486aea6493372e3`.
Raw extraction SHA-256: `80e050818eea8d6e605de382ce370a438652a3760d82f6fb7b6e156e3a42720a`.

Naming and country assignment follow the source snapshot. Missing, duplicated,
historical or disputed names remain possible. Scenery placement uses sustained
built-up transitions; it is not an inventory of actual signs.
