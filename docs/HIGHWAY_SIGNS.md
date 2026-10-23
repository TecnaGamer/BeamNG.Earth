# Highway sign port

Ported from the modified 1.5.5 build onto 1.6.0-beta, starting at `18f131c` on `feature-highway-signs`.

## Player controls and coverage

**Mod options → World** has independent, enabled-by-default visibility switches for **Highway exit signs** and **Interstate mile markers**. Switching them changes only the sign objects and saves the preference. Signs travel with the existing road tiles, so the Roads option also controls their visibility.

Exit panels use OSM junction references and destination tags. Advance panels use 1 mile, 1/2 mile and 1/4 mile where the approach is long enough; metric roads retain the 500 m advance panel. The small panel is placed in a clear gore between the highway and ramp. Rest-area and welcome-center destinations use blue panels. Positions are chosen from a complete source neighbourhood before selecting the owning tile, preventing duplicate small panels at tile boundaries.

Mile markers have the posted cardinal direction, Interstate shield, MILE label and whole-mile number. The bundled snapshot contains Michigan and Kentucky references. Additional coverage uses OSM milestones on identified Interstate routes. Gaps are filled only between compatible references on an unambiguous connected route. State resets, ambiguous branches and unsupported mileage are not extrapolated. Some inventory points are calculated references rather than surveyed physical sign locations; see `CREDITS.md` and the snapshot's source metadata.

Panels are placed outside the current road pavement. Locations without a matching carriageway or safe clearance are skipped, including bridge decks. These are render-only objects, not new physical obstacles.

## Integration with 1.6.0

- `highwaysigns.build_tile` is used by both the process-worker and direct-server road builders.
- `signroads` uses the current `roads.road_specs` widths, lane tags and source geometry. It does not modify road geometry, markings, bridge profiles or terrain.
- `signcontext` retains a bounded, deterministic neighbourhood for sign placement across adjacent tiles.
- The two optional road-tile fields, `exits` and `mileposts`, use 1.6.0's existing shape-slot pooling, replacement, anchor transforms and ghost retirement. Hidden signs retain their slots until the tile is removed.
- An index becoming ready marks existing tiles for replacement at the normal road-build priority. Visible roads remain until replacement tiles arrive; outstanding builds are also refreshed.
- Existing connection/protocol, multiplayer, compute-sharing, terrain generation and minimap code paths are retained. The normal build fingerprint includes the new server modules.

## Data and packaging

Install the current `server/requirements.txt`; `osmium>=4` is the new dependency for background PBF indexing. `osmregions` downloads a regional Geofabrik extract through the current `netfetch.Fetcher.session` API. Requests keep the existing admission, offline, timeout and accounting policies. Downloads are resumable, limited to 2 GiB, and redirected only within the HTTPS extract host. Workers share process locks for downloading/indexing.

Cached extracts and sign indexes reuse `mapdata/raw/osm-lanes/` under the existing cache directory. The folder name is retained for compatibility with previous downloads; the old lane importer and lane database are not used. Existing compatible `.objects.v4.sqlite` sign indexes remain readable. New indexes use `.signs.v1.sqlite`, leaving the older build's lights and other roadside objects intact. OSM sign indexes are built atomically in the background. First-time regional downloads can be large; roads continue while they prepare. Offline play uses bundled references and already cached sign/road data.

The build includes `exit_glyphs.json`, `mile_markers_us.json`, sign materials and the opacity atlas. `tools/build_mile_markers.py` is the retained snapshot refresh tool. The snapshot records source URLs, dates and calibrated rows; updating it is separate from running the game.

## Validation

- 289 targeted Python tests passed, covering signs, road/bridge geometry, map data, OSM tags, networking, workers, shape slots, protocols, compute-sharing, process pools and terrain patches.
- Feature-specific Lua tests cover independent saved toggles, queued signs, render-only meshes, pooled slots, duplicate replacement, anchor transforms and ghost retirement.
- The wider Lua run passes 54 of 55 executed suites. Two timing assertions in the existing `test_roads.lua` also fail on the untouched `18f131c` version under this Windows/Lupa harness. `test_scoping.lua` requires the Linux `find`/LuaJIT CLI tooling and was excluded from that run.
- Worker and server outputs are checked byte-for-byte. Removing signs leaves road mesh files and non-sign tile metadata unchanged.
- Packaging checks exercise the glyph/reference files in the frozen data layout. A further 32 tests passed using the sign modules and data extracted from the actual EXE; the EXE's server entry point starts successfully with `--server --help`.

Before merging, drive a mapped interchange and a covered Interstate in-game, confirm advance signs and whole-mile panels, toggle each feature, teleport/rebase and reconnect. Check a first-time region after its background index completes, plus a BeamMP session with matching builds. These visual/game-runtime checks have not been performed for this port.
