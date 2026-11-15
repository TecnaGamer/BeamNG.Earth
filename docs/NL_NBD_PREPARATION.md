# NBd direction-sign preparation

This change adds offline inspection tools only. It does not load data in the
game, alter generated signs or depend on the Dutch signals PR or BGT branch.

`tools/audit_nbd.py` joins the public `wwxy.csv`, `doelen_nbd.csv` and `WW.TXT`
exports by junction/object identifiers, converts RD coordinates with development
dependency `pyproj`, and writes source-hashed GeoJSON and a quality report.
It keeps raw destinations, raw object type and missing direction explicitly.
The generic `soort=N` category alone never establishes an automobile sign.

```
python tools/audit_nbd.py SOURCE_DIRECTORY OUTPUT_DIRECTORY --bbox 4.52 51.90 4.58 51.97
python tools/nbd_control_map.py OUTPUT_DIRECTORY/objects.geojson OUTPUT_DIRECTORY/control.html
```

The self-contained HTML inspection plot has search, object-type filtering and
source details, without remote map tiles or third-party scripts. Optional BGT
observations already present in input are shown as observations; no BGT loader
is imported. An anchor can describe a panel, not its actual mast foot.

The previously acquired Capelle/Rotterdam export sample has 573 local rows,
441 with raw destination strings, 108 bicycle objects, 450 road-guide objects
and 15 unclassified objects. None provides a populated heading. This is a
quality observation, not evidence that 450 automobile panels can be rendered.
The national coordinate file has 93,695 rows; 913 fail the coordinate/envelope
check. Source export hashes are recorded afresh by each audit.

Public source locations:

- https://bewegwijzeringsdienst.nl/home/databeheer/
- https://downloads.rijkswaterstaatdata.nl/bewegwijzering/
- https://data.overheid.nl/en/dataset/43337-nationale-bewegwijzering---052016

The archived Data.overheid metadata identifies the RWS distribution as Public
Domain Mark 1.0 (metadata dated 2016). Source images, photographs, logos and
fonts are not redistributed by these tools. Current asset-specific terms and
dataset currency must be checked before a game dataset is published.

## Subsequent small PRs

1. A reviewed face schema and deterministic tile producer: explicit face normal,
   panel dimensions, ordered destinations, arrows, mounting height and support
   geometry, each with source and measured/transcribed/inferred status.
2. One complete supported panel type using existing shared panel/lettering
   meshes, with source-backed road-facing orientation and native server placement.
3. Opt-in streaming and successful-equivalent replacement only, with tile-boundary,
   reload, clearance and frame-time tests. Unknown pose remains an audit record.

The earlier local integration prototype includes experimental rendering and BGT
clearance code. It is deliberately not copied here: new server placement belongs
in Rust, and the ordinary game must not acquire a new dependency or update loop.
The existing BGT worktree remains independent and unchanged.
