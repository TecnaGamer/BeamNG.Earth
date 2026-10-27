# Combined custom build — 1.6.3-beta

This build combines `fix/bridge-underpass-overpass` and
`port/road-infrastructure-1.6.3` on `integration/bridges-road-infrastructure`.
Both original branches remain separate for their individual pull requests.

## Combined v6 — automatic roadside lamps and signs

- Added OSM street-lamp poles and high-mast lighting assemblies, including
  `lamp_mount=high_mast` and lighting towers. Mapped heights, light counts and
  light directions are respected; default poles are 9 m and high masts 35 m.
  Meshes include tapered steel poles, arms/rings and emissive lamp faces.
- Added white, black-lettered speed panels, with the taller `TRUCKS` layout
  when OSM supplies a truck limit. Explicit sign nodes and changed way limits
  take priority; highway repeaters follow connected source roads every 2 miles.
  Missing numeric limits use the existing game road-class defaults. Variable
  or unknown posted limits are not replaced with invented fixed values.
- Added Interstate and US route shields, with directional plaques, about every
  3 miles. Route relations supply numbers/directions when mapped, otherwise
  numbered route orientation and the road's travel direction supply the plaque.
- Added Michigan-only Adopt-a-Highway assemblies on Interstate, US and state
  highway corridors about every 2 miles. White panels, blue lettering/borders,
  evergreen/road artwork and `MDOT` follow the reference. The lower panel says
  `NEXT 2 MILES / COMMUNITY / VOLUNTEERS`; no real sponsor is invented.
- Posts stand beyond the generated paved shoulders, face approaching traffic,
  and belong to exactly one tile. Unsafe placements and bridge decks are skipped.
  These render-only objects use the existing background sign stream and toggle;
  lamp faces glow, but this update does not add dynamic pavement illumination.
- The sign cache upgrades atomically to `.signs.v3.sqlite`; old signs stay visible
  during the rebuild. An online run with OSM downloads enabled builds the new
  inventory automatically. No manual cache deletion is needed.
- Carried forward the existing `integration/bridges-road-infrastructure` tree
  (435018f), including every combined v5 median, Exit 76 ramp and earlier fix.
  The new app is packaged separately; previous combined downloads are preserved.

Validation: Python regression suite, actual mesh/atlas visual preview, Michigan
PBF import and placement replay on cached road geometry, plus frozen-app package
and offline startup checks. Full-route in-game driving is not yet verified.

OSM tag references: [street lamps and high-mast mounting](https://wiki.openstreetmap.org/wiki/Tag:highway%3Dstreet_lamp),
[speed limits](https://wiki.openstreetmap.org/wiki/Key:maxspeed),
[truck limits](https://wiki.openstreetmap.org/wiki/Key:maxspeed:hgv).

## Combined v5 — I-65 cable medians and Indiana Exit 9 extension

- Replaced the added Jersey median north of the US 62 bridge in the first
  screenshot (near 37.67836, -85.84646) with four steel cables on galvanized
  posts along the northbound median shoulder. The posts have amber reflectors.
- Switched the cable to the southbound median shoulder at the overpass at
  37.844624, -85.715595. Cables stop at the requested Louisville restart near
  Exit 116 / 38.09602, -85.70071; they do not cross the median at the switch.
- Resumed Jersey barriers and duplex blue 0.2-mile markers through Louisville
  and into Indiana, ending at the Exit 9 overpass. Both Ohio River spans are
  explicitly excluded. Existing bridge geometry and mapped crossover openings
  are retained; the added median treatment remains on ground road sections.
- Added 45 Indiana stations (0.2–9.0) using
  [INDOT reference-post labels](https://gis.indot.in.gov/ro/rest/services/DOT/ReferencePosts/MapServer/0)
  and connected OSM geometry. Indiana numbering is separate from Kentucky.
  The inventory is bundled, with no new download during driving.
- The explicit Louisville/Indiana extension includes wider paired medians and
  can use overlapping inner shoulders while retaining clearance to travel lanes.
  The original under-15 m rule remains elsewhere. Cable barriers use the existing
  colliding mesh stream, with open space between their four visible cables.
- Retained all combined v4 changes, including the single-lane Exit 76 ramp,
  I-75 median treatment, earlier road/bridge fixes, and multiplayer features.

Downloads: `BeamNG.Earth-1.6.3-beta-combined-v5.exe` and
`BeamNG.Earth-BeamMP-Server-1.6.3-beta-combined-v5.zip`. Restart the app/game with
v5 to refresh generated geometry; old downloads and player saves are preserved.

Validation: 296 Python tests and 81 Lua assertions passed. Six cached corridor
replays verified the start, both cable sides, switch and Jersey restart. An OSM
geometry replay mounted 43 of 45 Indiana reference posts: mile 6.2 is on a bridge,
and overlapping generated travel lanes near mile 4.2 leave insufficient room for
a median wall. Those locations remain gaps. A mesh preview verified the cables
and posts; full-route in-game driving remains unverified.

## Combined v4 — I-65 southbound Exit 76 ramp

- Corrected the off-ramp to KY 224 at Upton from three lanes to one. The cached
  OSM way 16257189 has an incorrect `lanes=3` tag; a source- and location-specific
  correction now feeds pavement width, markings, terrain and AI navigation.
- The ramp uses the normal single-lane ramp width, 8.3 m including shoulders,
  instead of 17.9 m. Both adjacent I-65 mainlines retain three lanes. Downloaded
  map data is preserved, and the new build fingerprint refreshes generated caches.
- Retained all combined v3 changes, including Kentucky median barriers and blue
  0.2-mile markers, plus the earlier road, bridge and multiplayer changes.

Downloads: `BeamNG.Earth-1.6.3-beta-combined-v4.exe` and the matching
`BeamNG.Earth-BeamMP-Server-1.6.3-beta-combined-v4.zip`. Earlier builds and player
saves are preserved. Fully close and restart the app/game using v4.

Validation: 220 Python tests passed. A replay of 243 road features across six
cached cells changed only the reported ramp, with all 21 checked ramp AI edges
using one lane. Mesh, ground texture and terrain-material checks passed; in-game
driving at this exit remains unverified.

## Combined v3 — Kentucky median barriers and blue reference signs

- Added tall concrete Jersey barriers to paired I-65 and I-75 ground carriageways
  in Kentucky where the paved-edge median gap is less than 15 m. The nominal wall
  is 1.37 m tall, 0.90 m across the base and 0.24 m across the top; its base narrows
  where necessary to fit a tight median. Mapped crossovers and ramps keep openings.
- Added blue barrier-mounted reference panels every 0.2 route miles on eligible
  wall sections. Each post has NORTH and SOUTH faces, an Interstate shield,
  MILE label, whole-mile number, and a separate .0/.2/.4/.6/.8 box.
- Bundled 1,645 calibrated reference stations: I-65 mile 0–137 and I-75 mile
  0–191.6. Geometry follows connected OSM mainlines between existing Kentucky DOT
  mile references; state-line tails extend the nearby calibration by at most one
  mile. No tile resets mileage or downloads this inventory during driving.
- Walls share the existing colliding deck stream and cache invalidation, while
  sign panels retain the existing Highway Signs / mile-marker visibility controls.
  Bridge structures retain the previous combined build's geometry and clearance
  fixes; this new wall pass applies to ground carriageways, with gaps at bridges,
  tunnels, mapped openings, wide medians, and unpaired or missing road data.
- Retained all combined v2 features, including yellow edge corrections, bridge
  continuity and truck clearance, OSM lanes and signs, BeamMP autosave/resume,
  tree/grass density settings, loading improvements, and HTTPS fixes.

Downloads: `BeamNG.Earth-1.6.3-beta-combined-v3.exe` and the matching
`BeamNG.Earth-BeamMP-Server-1.6.3-beta-combined-v3.zip`. Earlier downloads and
player saves are preserved. Restart the app/game to load the new assets.

Validation includes threshold/opening/collision/seam/direction/decimal tests,
both sign faces rendered from the actual mesh and glyph atlas, and cached I-65
replays at miles 26, 46, 60 and 100. The mile 46 marker was checked against the
actual road-tile wall. Full-route in-game appearance and driving remain unverified.

## Combined v2 — marking corrections and completeness audit

- Fixed white sections in the yellow/orange driver-left boundary at left-side
  ramp merges. The outside edge now keeps its color through the taper; lane
  separators and the shared inner boundary of right-side ramps remain white.
- Restored yellow left edges on one-way ramps classified as primary, secondary,
  or tertiary roads, and on mapped divided carriageways of those classes.
- Preserved yellow edge paint around continuous highway corners and used the
  same color rule for lane transitions, bridges, and tunnels.
- Audited all **133 changed files** from both source branches: **123 match a
  branch exactly**, and **10 contain reviewed integration/marking changes**.
  No feature files or modified Python functions were omitted. Autosave, tree
  and grass density, signs, road infrastructure, and bridge fixes are retained.
- Added regression cases for road classifications, both travel directions,
  entrance/exit ramps, reversed map geometry, bridge/tunnel output, and corners.

## Included changes

- Bridge junction continuity, extended approach decks, collision surfaces, and
  clearance checks across the full width of stacked bridges.
- Road and bridge widths, lane transitions, ramp merges, roundabouts, intersection
  pavement, regional markings, highway signs, and mile markers.
- BeamMP per-player autosave and resume, including account and guest identity,
  saved position, heading, road height, and supported vehicle configuration.
- Tree-density presets from very low through maximum, plus grass-density settings.
- Road-loading optimizations, CPU worker budgets, improved approach terrain detail,
  HTTPS packaging fixes, and invalidation of outdated generated geometry.

## Integration fixes

- Promoted bridge approaches retain lane counts and road widths as well as their
  deck and collision mesh.
- The final bridge continuity pass preserves flush short overpasses, avoiding
  reintroducing small crests removed by the road-infrastructure changes.
- The app and BeamMP server ZIP are built from the same combined source tree.

## Updating BeamMP

Use `BeamNG.Earth-1.6.3-beta-combined-v5.exe` with
`BeamNG.Earth-BeamMP-Server-1.6.3-beta-combined-v5.zip` from this build.

1. Stop the BeamMP server and replace its plugin and client ZIP using the server
   archive. Reapply any customized plugin settings.
2. Keep `ServerConfig.toml`, both `data/bnge_player_positions*.json` files, and
   clients' existing guest-token settings. The archive contains no player saves.
3. Restart the server, fully close and reopen BeamNG using the combined app, then
   reconnect. Drive, allow the default ten-second save interval to pass, and
   disconnect/rejoin to check restoration.

The clean bridge-only app did not include this custom multiplayer resume client.
See [BeamMP setup and persistence details](beammp/README.md) for configuration,
older-save migration, and diagnostic messages. Damage, fuel, and attached trailers
are not part of the existing saved vehicle configuration.

## Validation

- 679 Python tests passed, with 2 platform/tool-dependent skips, across 49 modules.
- 5,476 Lua assertions passed across 66 test files, including server checkpoint
  persistence/restart, client restoration, guest identity, settings, and trees.
  The separate scoping check requires Unix `find` and a standalone LuaJIT binary
  and could not run in this Windows test environment.
- The previous integration's six cached Seattle road/elevation replays completed with no height mismatch at
  shared bridge endpoints. The reported stacked crossing at
  47.57282, -122.31967 retained 6.15 m of modeled usable clearance after merging.
- Some short or inconsistent mapped approaches still produce steep grades or
  insufficient headroom elsewhere in those networks. These are reported by the
  solver; this build does not guarantee truck clearance at every crossing.
- Live multiplayer reconnection and in-game driving still need confirmation.
