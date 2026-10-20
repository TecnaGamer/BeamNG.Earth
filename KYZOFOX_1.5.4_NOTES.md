# 1.5.4 (KyzoFox build): what changed over 1.5.3 beta

This is TecnaGamer's 1.5.3-beta with fixes from a community build on top. Where 1.5.3 already fixed the same
problem, 1.5.3's code was kept and the community version dropped.

## Kept from 1.5.3 as is (the community build's own version was removed)
- Urgent collision rebuilds and `nextWalk` mesh ordering (`mod/.../roads.lua`). These replace the community build's
  deck-first queue, deck radius and `RELOAD_DECK_S`.
- Road tiles along the path at speed (`wanted_tiles` / `path_key`, `server/roads.py`). These replace `ahead_tiles`;
  `--road-ahead` now only makes 1.5.3's path longer (`AHEAD_M`).
- The ground first (`ground_busy`), the edge pause, and failed tiles asked for again.

## Added on top
| Area | Files |
|---|---|
| Overpass approach ramps (deterministic per road and bridge; motorway and trunk never raised) | `server/grading.py`, `tests/test_approaches.py` |
| Bridge clearance and grade settings (`--bridge-clearance` 7.5, `--bridge-max-grade` 0.12) | `server/roads.py`, `server/earthd.py` |
| Re-sent tile drops its stale queued meshes (fixes floating buildings from reused pooled slots) | `mod/.../roads.lua` `dropTile`, `q.tile` identity check |
| Misplaced footprints on roads left out (only big slabs lifted) | `server/buildings.py` |
| Road tiles never wait for building data (partial tile, rebuilt later at 300 m or more) | `server/blockworker.py`, `roads.Tracker.partial/ready`, `earthd.roads_data_ready`, `tests/test_blockworker_partial.py` |
| Empty squares not rebuilt every pose; failed tiles retried after 3 s; `roads_empty` message | `server/roads.py` Tracker |
| Road guard: the game pauses until the next 4 s of road (and its decks) are loaded and solid | `mod/.../roadguard.lua`, `roads.squareState/onEmpty`, `core.lua`, `tests/test_roadguard.lua` |
| Trailer re-hitched after a rebase | `mod/.../rebase.lua`, `core.lua`, `tests/test_recouple.lua` |
| Roads first: trees and far work wait while near road tiles are missing | `server/earthd.py` |
| Look-ahead downloads include the roads and water layers, and reach further | `server/earthd.py` |
| Land-cover noise about 4x faster, bit-identical output | `server/surface.py` |
| Larger governor budget (4/8 ms, engine tick free to 5 ms) | `mod/.../governor.lua` (tests pin the old values for their timing maths) |
| BALANCED profile; launcher and .exe default to it | `Play BALANCED.bat`, `launcher.py`, `BUILD EXE.bat` |

## Tests
- `server/` (pytest): 1091 passed. The ones that fail here fail the same way on unmodified 1.5.3-beta in the same
  sandbox: no network to the map servers (`test_integration_drive`, `test_credits` session), Linux CA bundle
  (`test_launcher_linux`), `test_v131_default`, and one timing check (`test_colormatch` per-tile cost, 10.4 ms vs 10).
  Updated for 1.5.4's settings: `test_roadmesh` (deck grade now `DECK_MAX_GRADE`), `test_download_ahead` (full look-ahead
  distance; the roads layer fetched even without buildings).
- `mod/tests/*.lua` (luajit): all pass except `test_sky.lua`, which fails in this environment on 1.5.3 too.
  `test_governor` checks the new budgets, then pins the old values its timing maths uses.
