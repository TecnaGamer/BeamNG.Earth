# Shield artwork and data provenance

## Nationwide library

`artwork_sources.json` lists each of the 88 imported numberless designs,
its original Wikimedia Commons file, creator, license, source URL/SHA-1,
downloaded-input SHA-256 and reviewed number layout. The derived blank images
are in `art/`; locally obtained original vector inputs are retained in `svg/`.
All 88 entries have explicit reuse permission recorded in that catalog.

Eighty-seven imported sources are identified as **public domain** by their
file license metadata. They include official-standard drawings and authors'
public-domain dedications. Their creators and original file pages remain in
the catalog. Changes include resizing, number compositing, number-slot layout
and deriving mesh cutout contours. Original numberless SVGs remain linked;
cached 960-pixel blank previews were used when original downloads were throttled.

### Wide Minnesota design — CC BY-SA 4.0

`art/mn_wide.png` derives from **MN-blank wide.svg**, by **Taniya Wilburn**:
https://commons.wikimedia.org/wiki/File:MN-blank_wide.svg

Source and this adapted image are licensed **Creative Commons
Attribution-ShareAlike 4.0 International**:
https://creativecommons.org/licenses/by-sa/4.0/

Changes: resampled from the source's cached blank preview into the shared
texture layout; generated route faces add Roadgeek numbers and are encoded as
mipmapped DDS. These derivative Minnesota-wide graphics are distributed under
CC BY-SA 4.0 as well. Retain this attribution, source link, license link and
change notice when redistributing them. No endorsement by the source creator
is implied. The separately licensed source code, OSM databases, font assets and
other public-domain designs are identified independently in this package.

### Nationwide route database

`route_relations.sqlite.gz` is derived from **© OpenStreetMap contributors**,
licensed **ODbL 1.0**: https://www.openstreetmap.org/copyright

The accompanying manifest identifies the 2026-11-08T10:34:50Z snapshot,
checksums, route/way counts and network breakdown. This editable SQLite
database stores relation tags and way memberships, not a surveyed sign inventory.
The source snapshot and reproducible build commands are documented in
`docs/nationwide-highway-shields.md`.

## Wyoming pilot artwork

`wy.svg` and `wy.png` are numberless adaptations of **File:WY-210.svg**,
by Wikimedia Commons contributor **Vishwin60**, retrieved 2026-11-08.
The source file's Commons metadata identifies it as **public domain**, with
no attribution requirement (PD MUTCD WY). The drawing includes the Wyoming
legend and bucking horse and rider; only the numbered `text1938` path was removed.

- Source and license description: https://commons.wikimedia.org/wiki/File:WY-210.svg
- Original: https://upload.wikimedia.org/wikipedia/commons/f/f3/WY-210.svg
- Rebuild: `python tools/build_wy_shield_template.py WY-210.svg`
- Build-only SVG rasterizer: `resvg-py`; no SVG library is needed by the server.

`lettering.png` / `lettering.json` contain a small subset of the existing
Roadgeek 2014 Series EEM 3.1 atlas. Sammi De Guzman's Roadgeek font distribution
is MIT licensed; the existing distribution's notice is included as
`Roadgeek-LICENSE.txt`. Source: https://github.com/sammdot/roadgeek-fonts/releases/tag/3.1

The network registry is project configuration. OpenStreetMap Americana was
investigated as a CC0 source, but its simplified Wyoming map marker does not
contain the horse. No Americana artwork was copied into this pilot.

- Americana source/license: https://github.com/osm-americana/openstreetmap-americana
- Route network conventions: https://wiki.openstreetmap.org/wiki/United_States_roads_tagging/Routes
- Texas subtypes: https://wiki.openstreetmap.org/wiki/Texas/State_Route_Relations

`../wyoming_route_shields.json` is derived from **© OpenStreetMap contributors**,
licensed **ODbL 1.0** (https://www.openstreetmap.org/copyright).
Its header records the OSM timestamp, source SHA-256, bounds and panel counts.
It is an inferred station inventory, not a surveyed inventory of installed signs.
The editable database is supplied in JSON. See `docs/highway-shields.md` for
the source queries, rebuilding, scope and validation limitations.

The ordinary federal shield meshes and their lettering remain the existing
project implementation. Public-domain artwork attribution does not imply that
all state insignia or future third-party shield assets have the same license.
