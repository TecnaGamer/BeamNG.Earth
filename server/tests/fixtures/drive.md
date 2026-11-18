# Drivability replay inputs

Places reported as hard or impossible to drive, captured on 2026-11-11 with
`tools/capture_interchange_fixture.py --prefix drive` into a cache of their own: the first six with the
default `--cells-m 1200`, the GitHub issues' places with `--cells-m 400` (the measurement reaches 200 m; at
Südtiroler Platz 400 m and 1200 m measured the same, at a third of the size). Same format as
[interchanges.md](interchanges.md); `server/tests/drivability.py` measures them (with the interchange
replays) and `test_drivability.py` holds them to `drivability_baseline.json`.

| file | reported | point (lat, lon) |
|---|---|---|
| `drive_torbole_adige_tunnel.npz` | a part of the mountainside reaching onto the street | 45.86342, 10.91679 |
| `drive_riva_pontera_tunnel.npz` | a steep hill after a small tunnel on a major road | 45.87828, 10.88542 |
| `drive_riva_gardesana.npz` | something on top of the road | 45.87572, 10.87141 |
| `drive_casagranda_bridge.npz` | a large incline between tunnels | 45.86134, 10.83268 |
| `drive_casagranda_portal.npz` | a large incline before a tunnel | 45.85964, 10.83267 |
| `drive_a5_underpass.npz` | a gap in the autobahn | 49.35458, 8.63166 |
| `drive_compiegne_roundabout.npz` | a roundabout the railway crosses twice on bridges: "every version does a different kind of mess" | 49.422213, 2.806541 |
| `drive_perth_mount_henry_520.npz` | GitHub #520: Mount Henry Bridge's crests launch the car (in the game likely the imported 3D bridge model, which the replay does not load) | -32.033890, 115.858373 |
| `drive_tamar_bridge_1112.npz` | GitHub #1112: Tamar Bridge's ends droop, a drop and a bump onto the deck | 50.407633, -4.198455 |
| `drive_vienna_suedtiroler_1083.npz` | GitHub #1083 (1): Südtiroler Platz over its lid | 48.18609, 16.37368 |
| `drive_vienna_s1_rautenweg_1083.npz` | GitHub #1083 (2): Rautenweg over the S1's cover section | 48.26004, 16.47572 |
| `drive_vienna_s1_breitenleer_1083.npz` | GitHub #1083 (3): Breitenleer Straße over the S1's cover section | 48.25091, 16.47085 |
| `drive_vienna_a23_1083.npz` | GitHub #1083 (4): the A23 mapped on the ground over Erzherzog-Karl-Straße | 48.22963, 16.45307 |
| `drive_vienna_kennedy_1083.npz` | GitHub #1083 (5): Kennedybrücke mapped on the ground over its underpass | 48.18756, 16.30383 |
| `drive_vienna_mooslacken_1083.npz` | GitHub #1083 (6): Alois-Mock-Gasse over the Mooslackengasse lid | 48.24946, 16.36675 |
| `drive_paris_maillot_1083.npz` | GitHub #1083 (7): Avenue de Neuilly over the Périphérique | 48.87830, 2.28088 |
| `drive_paris_maillot_ax_1083.npz` | GitHub #1083 (8): Rue du Général Anselin over Voie AX | 48.87570, 2.27796 |
| `drive_dc_capitol_crossing_1083.npz` | GitHub #1083 (9): F Street over I-395 at Capitol Crossing | 38.89715, -77.0142 |

Road geometry: © OpenStreetMap contributors, Overture Maps Foundation, ODbL-1.0 (data from TomTom).
Elevation: Mapterhorn (see mapterhorn.com/attribution). See the project's CREDITS.md.

Add a place (downloads what CACHE_DIR lacks), list it in `drivability.SITES`, then write its baseline:

```text
python tools/capture_interchange_fixture.py NAME LAT LON CACHE_DIR --prefix drive --cells-m 400
python server/tests/drivability.py --update NAME
```
