# Sidewalk decoration development-context observations

Captured read-only on 2026-11-01 from the available local mapdata v2 cache, Overture release
`2026-09-23.1`. Capture and replay make no network requests and do not modify the download cache.
`sidewalk_development.npz` contains original complete feature geometry, source attributes and
applicable rules. It contains no synthesized buildings, land-use masks, POI classifications, or
sidewalk eligibility evidence.

The companion JSON manifest records every contributing cell, its relative source-cache path and
original provenance, plus the exact road feature and sample query. Selection keeps all source
features whose bounding boxes intersect a 900 m square around each reference point. Large polygons
remain complete. Cross-cell copies of whole features remain present deliberately, so replay also
exercises source-ID deduplication. The source-cell land-use pieces retain their original geometry.

| Case | Reference latitude, longitude | Original road sampled | Expected local pattern |
|---|---|---|---|
| Cheyenne downtown | 41.1335, -104.8150 | Capitol Avenue | Rich commercial context |
| Cheyenne residential | 41.1477, -104.8290 | Bent Avenue | Limited residential context |
| Laramie downtown | 41.3110, -105.5940 | South 2nd Street | Rich commercial context |
| Horse Creek | 41.2183589, -104.8330190 | Horse Creek Road | Sparse; no procedural decoration |
| Round Top | 41.2182404, -104.9002486 | Roundtop Road | Sparse; no procedural decoration |
| Happy Jack | 41.1931691, -105.2014728 | Happy Jack Road | Sparse agricultural/residential; no procedural decoration |

The sample query is the closest point on the named source road to the reference point. The tool
uses a unit perpendicular to that actual source segment to query the frontage on both sides.
The source road's ordinary carriageway half-width (recorded in the manifest) offsets frontage
probes from this road-centre query; it does not create sidewalk geometry or assert that a sidewalk
exists. These fixtures test decoration context only. Road
specification, bridge, banking, and rural-classification regression fixtures remain separate.

The rural locations intersect source residential land-use polygons, demonstrating why such a
polygon cannot establish development by itself. The downtown data includes buildings without
`class`; their `subtype`, footprint geometry, commercial land use and POIs provide additional
evidence. The residential Bent Avenue sample has no nearby POI concentration. These are samples
of the stated roads, not assertions about every point along those roads or the named cities.

Reproduction from a local cache (writes only the selected output fixture and manifest):

```powershell
python tools/audit_sidewalk_development.py --capture --capture-only --cache-dir C:/Users/chenk/.cache/beamng-earth
```

Portable replay using only repository fixtures:

```powershell
python tools/audit_sidewalk_development.py --repeats 100 --output build/sidewalk-development-audit.json
```

Add `--source-cache --cache-dir <cache-folder>` to measure complete source-cell decoding and
index construction through the production cache-only reader, using the same geographic queries.

`--radius` permits comparisons; the default is 100 m. The captured selection safely supports
the default radius and these central sample queries. Capture a larger `--extent` before using
a radius beyond the fixture's source coverage. The reported construction/sample timings cover
the scoring work; fixture decompression is outside those measurements.

Source attribution: Overture Maps Foundation and OpenStreetMap contributors (ODbL 1.0), with
additional building/place source licences recorded in the manifest's original provenance.
Retain the project's [CREDITS.md](../../../CREDITS.md) and
[OpenStreetMap attribution](https://www.openstreetmap.org/copyright). The places dataset also
includes Meta/Microsoft data under CDLA Permissive 2.0; consult the recorded source provenance.
