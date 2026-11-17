# truth_*: the measured shape of real junctions

`truth_NAME.json`: swisstopo's swissTLM3D road and railway lines round a place, each a 3D line with what it is, whether
it is on a bridge or in a tunnel, and its level (tools/capture_truth_fixture.py; tests/truth.py reads them).
`truth_NAME.npz`: the map and the elevation Earth reads at the same place (tools/capture_interchange_fixture.py NAME
LAT LON CACHE --prefix truth, then --repack FILE --keep-m 1000), in the same frame.

Source: Federal Office of Topography swisstopo, swissTLM3D 2026-02 (free use with the source named). The country's
file (3.6 GB) is not in the repository and no test downloads anything: only these cuts are kept.

| name | place | half (m) |
|---|---|---|
| zurich_brunau | 47.344339, 8.519291: the A3's viaducts over the Sihl, the Uetliberg tunnel's portals, a railway tunnel under them | 600 |
| zurich_europabruecke | 47.393704, 8.493146: a road bridge over a motorway, its loop ramp, a railway yard and underpasses | 400 |
| zurich_duttweilerbruecke | 47.389179, 8.510263: a road bridge over a railway yard, a railway viaduct over the bridge | 400 |

`python server/tests/truth.py [NAME ...]` prints a place's measurements; tests/test_truth.py holds them.
