# Changelog

What changed in each release of BeamNG.Earth, newest first.

## 1.5.4 (KyzoFox build, on top of 1.5.3 beta)

Everything in 1.5.3 beta, plus these fixes. Where 1.5.3 already fixed something (bridge and building collision at
high speed, road tiles built along your path, the ground first, the edge pause, failed road tiles asked for again),
its fix is kept as it is.

### Bridges
- **Overpasses are high enough for trucks.** A short bridge over a road had both ends on the ground (the elevation
  data is too coarse to show the real approach embankments), so its deck could climb only a little before it was over
  the lanes: about 2.5 m on the I-20 overpass near Temple, GA, and trucks hit it. The roads leading onto such a bridge
  now rise on a smooth ramp (at most 5 %) to the height the deck needs, like real approach fills, so the deck clears
  the road under it by 7.5 m (about 5.3 m under the girders). Long viaducts, bridges over water and ground that is
  already high enough are left as they were. Turn the ramps off with the environment variable
  `BNE_APPROACH_RAMPS=off`; `BNE_APPROACH_GRADE` sets their grade.
- **Motorways and trunk roads are never raised**, only the smaller roads crossing over them. The raise depends only on
  the road and the bridge it leads to, so it is the same in every map area (no road drawn under raised ground at an
  area's edge); it follows the road, and its fill slope never reaches a road beside it.
- **Bridge clearance and deck steepness are settings:** `--bridge-clearance` (default 7.5 m over a road, was 6.5) and
  `--bridge-max-grade` (default 12 %, was 6 %).

### Buildings
- **No more buildings floating in the sky.** When the server sent a road tile again, meshes of the first copy still
  waiting to be made were made anyway. Their shape files had already been handed back to the server and reused for
  another tile, so that tile's buildings appeared here at its ground height: groups of houses hanging in the air
  (near Cleburne, AL). Those leftover meshes were also never deleted. A re-sent tile now drops everything still
  queued from its first copy.
- **Fewer blocks floating over highways.** A building outline that a road runs through is usually just misplaced (a
  machine-learned outline, or one a few metres off). Only a really big one (an expressway deck or a station slab) is
  still lifted over the road; smaller ones and machine-learned ones a road crosses are left out. The far buildings
  follow the same rule.

### Loading at speed
- **Roads never wait for buildings.** A road tile used to wait until its buildings' map data had downloaded, which
  held the road and its bridge deck in front of a fast car for 15-19 s. The road tile is now built and sent at once
  (about 3x faster) and its buildings follow once their data is in. It is only rebuilt with them when you are at least
  300 m away, never with you on it.
- **Roads further ahead.** `--road-ahead` (3 km in the BALANCED profile) extends 1.5.3's road tiles along your path
  from about 15 km/h up, in the same order you reach them.
- **Downloads further ahead.** The look-ahead downloads now include the road and water layers and reach past your road
  reach (1.5 km beyond it at any speed).
- **Roads first.** While road tiles near you are missing, trees, far buildings and far terrain tiles start nothing new
  (far terrain tiles one at a time), leaving the CPU and network to the roads.
- **Squares with no roads are not built again and again**, and a failed road tile waits 3 s before it is tried again
  instead of on every update.
- **Terrain blocks build faster.** Most of a block's build time was the land-cover noise math, hashed again for each
  of a block's million samples. It now hashes each lattice point once, shares the work between the two warp
  directions, and works out lawns only on built-up land. Same terrain, bit for bit; that step is about 4x faster on
  rural roads (2.5x in towns).
- **A larger per-frame loading budget in the game** (4 ms, up to 8 ms; was 2 and 4), so roads and tiles appear sooner
  on mid-range PCs.

### Missing roads
- **The game waits for the road ahead instead of letting you fall.** While you drive, the next 4 s of road must be
  loaded, with its bridge decks and tunnels solid. If they are not, the game pauses itself with "waiting for the road
  ahead to load" and carries on by itself once they are (at most 25 s). Resuming by hand still works. Turn it off with
  the `roadGuard` setting.

### Trailers
- **The trailer stays hitched when the world re-centres.** Every ~15 km the world moves back to the origin
  ("Loading n/222"). The truck and its trailer were moved separately, the coupling could break for that moment, and
  the fifth wheel would not latch again by itself. Couplings are now re-attached right after the move.

### Launcher
- **BALANCED profile:** `Play BALANCED.bat`, and the launcher and the built .exe start with the same settings
  (roads 3 km ahead and 768 m around, roads first, trees 1 km, far buildings 1.2 km; no photo touch-up, horizon,
  airports or sea). Anything after `--` still overrides them. `BUILD EXE.bat` builds the .exe.

## 1.5.3 beta (2026-10-19)

Works with BeamNG.drive 0.40.

### High-speed driving
- **Reduced the chance of driving onto bridges and into buildings before they have collision at high speed.** Their
  collision is now made solid as soon as the car is a few seconds away instead of waiting for the next periodic
  rebuild. In testing at about 185 mph, collision was ready at least 480 m ahead of the car (before: as little as
  32 m).
- **Road surfaces should keep up better at high speed.** Above about 55 mph the world server builds the road tiles
  along your path, up to 14 seconds of driving ahead, in the order you will reach them. Things beside and behind you
  come later.
- **The ground under the car now comes first.** The terrain is built ahead of road tiles and far-away scenery when the
  server is busy.
- **Reduced the chance of falling off the edge of the loaded ground.** If the ground ahead is ever late (a slow first
  visit, a busy computer), the game now pauses at the edge with "Waiting for collision to load here" instead of letting
  the car drive off, and carries on by itself once it has loaded.

### Teleporting (drop the car at the camera)
- **Reduced the chance of falling through bridges and buildings after a teleport.** The pause now also waits for the
  bridges, tunnels, sidewalks and buildings around the car to have collision, then sets the car down on them.
- **Teleports to places that are already partly loaded pause too** if their bridges and buildings are not ready yet.
- **Dropping the car under a bridge** now lands it on the road below instead of on the bridge deck.

### Traffic and streets (thanks to hashk014, PR #35)
- **Roundabouts:** traffic now drives one way round in the right direction (the mapped one, else your country's
  driving side), cars on the roundabout have priority, and entries get give-way signs.
- **Traffic slows down for tight bends and hairpins**, braking in time, and follows speed limits that change partway
  along a road. Straight roads keep their posted limit.
- **Narrow old-town streets** between close buildings are drawn as narrow as 3 m wide with narrower sidewalks,
  instead of a wider default that ran into the buildings. Traffic takes narrow two-way streets more slowly.

### BeamNG.drive 0.40
- **Shingle roofs and runway markings should show their textures again.** 0.40 moved some of the game textures
  BeamNG.Earth uses.

### Installing from source (thanks to hashk014, PR #34)
- **Clearer help on Windows when a Python package cannot be installed.** With a Python too new for DuckDB's
  ready-made package, the installer now stops at once and explains how to use 64-bit Python 3.12 or the standalone
  .exe instead of failing after a long build and suggesting a network problem. Only affects installing from the source
  code; the .exe is unchanged.

### Other fixes
- **Grass should come back after starting the BeamNG.Earth map again in the same game session** (for example after
  going back to the main menu).

### Known issues
- Bridges are not perfect yet, and long sessions get more stuttery until you restart the game.
- A very fast drive through a dense city on a first visit can still outrun the road surfaces.

## 1.5.2 beta (2026-10-19)

Works with BeamNG.drive 0.40. Tested on 0.40.0: loading the world, driving in Baltimore, the Golden Gate, and terrain
blocks loading while you drive.

### BeamNG.drive 0.40
- **The map, welcome and feedback screens now open the way 0.40 expects.** 0.40 changed how mods switch UI
  screens; BeamNG.Earth uses the new way and falls back to the old one.
- **Parked cars follow 0.40's renamed parked-traffic setting.** 0.40 renamed the option that sets how many parked
  cars there are; BeamNG.Earth reads the new name first.
- **The Linux grass patch (gcpatch) now works on 0.40.** It finds its place in the new game version on its own, so
  grass should stay when terrain blocks move.

### Tips after updating the game to 0.40
- **0.40 turns all mods off after the update.** Turn mods back on from the main menu, then check that BeamNG.Earth
  is on under Mods > Manage.
- **0.40 may reset your units.** If the speedometer shows km/h, switch it back to mph in the game's options.

### Known issues
- Same as 1.5.0: bridges are not perfect yet, high-speed driving still stutters while the world loads, and long
  sessions get more stuttery until you restart the game.
- 0.40's new weather has not been tested together with BeamNG.Earth's real weather yet.

## 1.5.1 beta (2026-10-19)

Works with BeamNG.drive 0.39.4.

### Buildings
- **Reduced the chance for buildings to spawn floating.** Buildings far from where the map was last centred were
  placed without the curve of the Earth, so they could float by several metres (about 5 m at 8 km, up to 18 m at
  15 km). They now use the same ground height as the terrain.
- **Reduced the chance of gaps under distant buildings.** Their walls now reach a few metres below the ground,
  which hides gaps where the distant terrain is less detailed.

### Update notice
- **The "new version available" notice now stays on screen** in the top-left corner until you choose **Open
  releases page** or **Dismiss**. Dismiss hides it until the next version comes out.
- **Open releases page should now open your browser on Linux too.** The world server opens it, because the game's
  own browser call does nothing in the native Linux version.

### Game updates
- **The launcher now tells you when a game update has turned your mods off**, and how to turn BeamNG.Earth back
  on.
- **The Linux grass patch (gcpatch) is more likely to keep working after a game update.** It can now find its place
  in a new game version on its own.

### Known issues
- Same as 1.5.0: bridges are not perfect yet, high-speed driving still stutters while the world loads, and long
  sessions get more stuttery until you restart the game.

## 1.5.0 (2026-10-18)

The first release out of beta. Big themes: real bridges and tunnels, smoother driving, and credits for every data
source.

### Bridges
- **Real bridge types.** Truss, arch, suspension, cable-stayed and movable bridges are built with their real
  structure, from the US National Bridge Inventory (FHWA) and Wikidata (worldwide). Examples: the Queensboro, the
  Golden Gate, Sydney Harbour, the Rainbow Bridge, Tsing Ma, Tower Bridge.
- **Real heights and shapes.** Deck heights use the inventory's clearance over shipping channels. Decks no longer
  sag between supports. Anchor piers stand at the ends. Twin carriageways of one bridge sit at the same height.
  Double-deck bridges keep the right gap between decks.
- **Painted bridges** in their mapped colour (Golden Gate red, for example). Mapped towers and walkways are drawn as
  part of the bridge, not as buildings with windows.
- **Clearer roads on bridges.** Roads pass through bridge towers and gate buildings. Machine-detected "buildings" on
  bridges and on water (often barges or decks) are removed.
- Ramps start flush with their intersections.

### Tunnels
- **Mapped tunnels follow what's over them.** Depending on that, a tunnel becomes open road, a low stone arch or a
  full tube.
- **Inside.** Lamps on the walls and a darker inner wall.
- **At the mouths.** The portal faces reach the ground. Buildings over tunnels sit above the road. Dark gaps at
  arch mouths are fixed.
- Central Park's sunken roads and park bridges.
- Faster loading near long tunnels (for example the Gotthard).

### Streets and cities
- Street trees along sidewalks, medians on divided roads, and paved plazas and squares.
- Parking garages you can drive into, with ramps.
- Stoops and front steps start from the ground at the door.
- Buildings use their mapped wall material (stone, brick, glass).
- One broken white centre line outside the Americas.
- Props were mirrored (doors backwards on some walls); fixed.
- Rocks and plants sit on the graded ground beside roads.
- Italian orthophotos. Bare scree instead of false snow on mountain passes.

### Performance
- **Smaller hitch each time the terrain moves under you.** Each terrain block now loads only the ground materials it
  uses. In tests the stall dropped from about 300–440 ms to under 100 ms.
- **Buildings have a level of detail.** About a third of a mile away they switch to a simpler version: no window
  bays, roof clutter or steps.
- **What you're looking at loads first.** Road, tree and building tiles in front of the camera are built before
  the ones behind you.
- **Faster world building.** Road tiles build faster, and more of the server's terrain work runs in Rust.
- **Disk space fix.** The game's terrain texture cache no longer grows by about 550 MB a day; a session's entries
  are removed with it. Old entries from earlier versions stay in `temp/art/terrainmaterialcache` until you delete
  them.

### Gameplay
- **Dropping the car far away with the free camera is safe now.** The game pauses, with a banner saying it's waiting
  for collision to load, until the ground under the car has loaded. Then it resumes, instead of the car falling
  through the world.
- **At the edge of the loaded ground the game pauses instead of braking the car.** This happens when the server can't
  move the terrain (for example if it has stopped). It resumes once the ground follows; you can also resume yourself
  and drive back.
- **Credits show every data source** used around you: imagery, elevation, map data (OpenStreetMap, Overture,
  Microsoft, TomTom, and others), land cover, trees, sea depth, bridges, airports, gravity and weather. Each has a
  short name; the full wording stays on the settings page.
- The connection banner says whether the world server or a data provider is the problem.
- The launcher window is easier to read.
- The Windows .exe triggers fewer antivirus false positives.

### Known issues
- Bridges are much better but not perfect yet.
- Houses and buildings float above the ground in many parts of the world.
- High-speed driving still stutters while the world loads around you.
- A long session (about an hour of driving and teleporting) gets steadily more stuttery. Restarting the game
  clears it. The cause is being investigated (GPU memory fragmentation is the main suspect).
