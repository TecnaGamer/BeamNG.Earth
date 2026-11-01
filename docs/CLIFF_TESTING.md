# Cliff rocks testing

This experimental feature adds terrain-fitted cliff faces, forest-instanced broken rocks,
and terrain-projected transition decals. Natural cliffs and road cuts use different materials,
with regional rock colors that avoid copying vegetation and satellite shadows directly.

## Install and controls

Use the installation steps in [QUICKSTART.md](QUICKSTART.md). Install both the mod and server
from the same build. Restart the server and game after replacing an earlier testing build so
loaded meshes and materials cannot mask a change. Normal driving streams cliffs automatically.

In the existing **BeamNG.Earth settings** UI, switch these independently:

- **Cliff faces**: fitted meshes and their near-vehicle collision.
- **Scattered cliff rocks**: broken rock collections around steep cliff transitions.
- **Cliff transition decals**: rock textures projected over the terrain at cliff edges.

All three default to enabled and can be changed during play. The ordinary **Rocks** option
controls the existing rock scatter separately. No launcher flag or environment variable is needed.
These controls disable client rendering; the server still prepares cliff geometry for streamed tiles.

## Suggested checks

- California coast, Cowell-Purisima Trail: `37.41159, -122.43018` and
  `37.41692, -122.43404`. Check continuous cliff coverage, nearly vertical faces,
  terrain transitions, natural color, and broken-rock grounding.
- Austin, South Capital of Texas Highway: `30.29941, -97.82788` and
  `30.30035, -97.82782`. Check terraced road cuts, low ends tapering toward road
  height, fitted bases, and cliff sections with a road above the crest.
- Austin, North Capital of Texas Highway: `30.30705, -97.82551`. Check the
  sustained roadside bank near the buildings and junction. A major road's urban
  classification must not suppress a cut backed by the original terrain;
  pavement and built-up crests must remain excluded. From `30.30565, -97.82529`,
  check for exposed wedges along the rounded foot and crest. Keep the simulation
  unpaused while waiting for newly streamed cliff meshes to appear.
- Drive away until tiles unload and return. Repeat after restarting. Coverage and
  materials should be consistent; there should be no duplicate surfaces or flicker.
- Approach a cliff slowly to check collision, then disable cliff faces and confirm
  the invisible mesh no longer blocks the vehicle. Roads and shoulders should remain clear.
- Check flat cities, small isolated elevation spikes, bridges, and stacked interchanges.
  A road elevation difference alone must not generate a rock wall. Road cuts require
  supporting terrain profiles; natural cliffs require sustained steep terrain and area.

Detection is heuristic. Please report missing cliffs as well as false positives, including
coordinates, screenshots, build/commit, and whether they persist after restarting.

## Performance comparison

Keep the camera, resolution, graphics settings, and traffic identical. Wait for terrain and
textures to finish streaming before each measurement. Compare all three cliff controls off,
then enable each one alone, then all three together. Record CPU/GPU frame times and FPS;
milliseconds are easier to compare across different baseline frame rates.

Decals have distance and count limits and are created over multiple frames. Broken rocks use
the forest system and asset LODs. Cliff collision is activated near the vehicle. These limits
reduce costs but are not a guarantee of a particular frame rate. Include hardware and graphics
settings with performance reports.

## Assets and development

Shipped scan-derived assets are CC0; the road-cut material uses an AI-restored source.
See [CREDITS.md](../CREDITS.md) for provenance. Small-rock collections and decal textures
are copied and tinted from the user's installed BeamNG assets at runtime, not redistributed.

The source checkout includes offline preparation instructions in
`tools/assets/cliff/SCANS.md` and `tools/assets/roadcut_generated/RESTORATION.md`.
These tools and source images are not required to run the packaged mod.

## Detection regression tests

Run the focused suite from the repository root (PowerShell):

```powershell
$env:BNGE_ROADGEO_REAL = '0'
python -m unittest server.tests.test_cliffcuts server.tests.test_cliffrocks server.tests.test_cliffdecals server.tests.test_clifftalus server.tests.test_cdae server.tests.test_roadgeo
```

The deterministic fixtures cover coastal cliffs, terraces, upper roads, tapering
ends, fitted toes, near-vertical faces, and pavement clearance. The North Capital
scenario also tests the urban-road selection path, original terrain evidence,
nearby buildings, and rejection of artificial fill, missing source data, and
minor urban roads. Rounded-edge coverage is checked against exported triangles
along an oblique urban bank; bounded crest fitting must stop at terrace benches,
gentle hills, excluded land, and missing height samples. These tests run without
downloading terrain or launching the game; use the locations above for visual
checks. Add both a valid cliff case and
nearby false-positive cases when refining detection.
