# Perimeter regression map fixtures

These offline fixtures contain mapped facility boundaries, infrastructure, roads and buildings used by perimeter tests. OSM XML captures retain their source IDs, versions and timestamps. `perimeter_fixture_context/capture.json` records the Overture release and geographic cells; its NPZ layers consolidate the original captures without changing their features. Tests use synthetic terrain unless explicitly stated otherwise.

Map data: © OpenStreetMap contributors, available under the Open Database License (ODbL): https://www.openstreetmap.org/copyright and https://opendatacommons.org/licenses/odbl/1-0/. Overture-derived map features retain their source metadata and the attribution described in the repository's README and CREDITS. No BeamNG game assets are included.

Run `python probes/test_warren_authoritative_suite.py` with the server test dependencies installed. Lua client tests require `lupa`. The two external map-cache cases are intentionally skipped by this isolated runner.
