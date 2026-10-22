# Changelog

What changed in each release of BeamNG.Earth, newest first.

## 1.5.56 (KyzoFox build, on top of 1.5.5): speed

- **Windows: the game and the world server on separate cores.** On Linux the server already ran on its own
  cores (the last quarter) and the game on the rest; on Windows it only lowered its priority, and its five worker
  processes shared every core with the game (a 6-core laptop at 100 % CPU, the game at 17-20 fps). Now Windows
  splits them the same way: on a 6-core / 12-thread CPU the game gets 4 cores (8 threads) and the server 2 (4
  threads); after a teleport, while you wait for the world, the server also borrows half of the game's cores.
  `BNGE_SERVER_CORES=<n>` sets the server's cores (at most half); `--pin off` turns it off.

## 1.5.5 (KyzoFox build, on top of 1.5.4 beta)

Fixes for problems found driving I-20 through Louisiana and Mississippi on 1.5.4 beta.

### Overpasses
- **Overpasses over highways clear trucks.** Several bridges over I-20 had as little as 2-3 m under their beams. At
  the bridges reported (the clearance under the beams over the lowest lane, before → after): Thomas Road, West Monroe,
  LA 3.0 → 6.1 m; Stella Street, West Monroe 3.5 → 5.7 m; Nutland Road, Monroe, LA 2.1 → 5.0 m; US 65, Tallulah,
  LA 5.1 → 6.1 m; 31st Avenue South, Meridian, MS 5.0 → 6.0 m. What held them down:
  - On a divided road with twin bridges, each carriageway's ramp was capped by the other carriageway, as if it were
    a road beside it.
  - The ramp was measured from the ground, while the road's own smoothed height at the bridge end could be metres
    lower (the bridge cut out of the elevation data leaves a dip there).
  - Frontage roads joining by a bridge end, as high as it, held its ramp down as if they were low.
  - At the bridge end the ground was averaged with the highway and frontage roads around it and sagged under the
    raised road. A raised road now always stands on its fill.
- **Where the ground cannot be raised** (a frontage road right by the bridge end), a deck over a motorway or trunk
  road may climb up to 12 % instead of 6 %, only as far as the clearance needs. Bridges over streets are unchanged.
- **A hard minimum under every overpass: 5.0 m (16.4 ft) under the beams**, over every road a bridge crosses,
  anywhere in the world (20 ft stays the aim). Where the ramps cannot lift a bridge enough, its deck now climbs
  steeper to make it: up to 25 % on a street's bridge, 8 % on an interstate's (driven fast; there the road under
  dips instead). Checked on every bridge in the map data of the reported areas (Ruston, Monroe, West Monroe,
  Tallulah, Meridian, Newton, Tuscaloosa, Greene County AL: 94 map cells, 302 crossings): under 5 m, 138 on
  1.5.4 beta, 0 now but four in Monroe's I-20 / US 165 interchange, where ramps pass under I-20 right by its
  bridge's end (3.4-4.9 m).
- **Ramps no longer go missing when a map cell fails to load.** Building a bridge's ramps looks at the map cells
  around it; one that could not be fetched (offline, a network hiccup) threw away every ramp of the area, for
  the whole session: overpasses flat on the ground (Greene County, AL: 1.6 m under the beams, now 5.8 m). Now
  only that cell is skipped, and the area is planned again a minute later.
- **A short overpass is no longer taken for a junction.** A road under the middle of a 24 m bridge, within 12 m
  of both ends, counted as an intersection the bridge starts from, and the deck came down onto it.
- **Fell through a bridge after a rebase in West Monroe:** the ground around the player took 19-47 s to build
  there (1-3 s elsewhere), most of it the new underpass check testing every road against every interstate bridge
  in the I-20 / US 165 interchange. It now tests only the roads near each bridge: that area plans in 7.7 s here
  (22 s before the fix, 10 s on 1.5.4 beta), its interchange cell 10.9 → 2.4 s.
- **Roads under short interstate bridges dip under them.** Where I-20 crosses a local road on short bridges whose
  ends sit on the ground, the deck lay about 1.3 m over that road (Lincoln Parish / Ruston, LA). Interstates are never
  raised, so the road underneath now dips, as at real interchanges: up to 6 m, flat under the bridge and on smooth
  6 % slopes either side, along the same road only (not into side roads), and never cutting into a road beside it. The
  interstate keeps its own height at the bridge. At the reported spot: 1.3 → 5.6 m under the beams.
- **Higher clearance target:** 7.8 m to the road surface on the deck, 6.2 m (20 ft) under the beams, was 5.3 m.
- **Thinner decks.** The slab under the road is 0.6 m (was 1.2 m): with the beams 1.6 m deep, as on real overpasses.
  The old slab hung over the lanes like a wall.
- **No more ribbons of ground over the lanes.** A ramp leaving a bridge that curved back over the frontage road under
  that bridge built its fill straight across that road. Roads passing under a bridge now keep a ramp's fill off them
  beyond 30 m from the bridge end (by the bridge end the abutment stands between).
- **No bridge wall across a road beside it.** Where a ramp leaves a road side by side, the deck starts on that road's
  lanes; its side wall stood across them (Natchitoches Street, West Monroe). No wall is drawn over another road less
  than 1.5 m under the deck.

### Floating things (the Earth's curvature)
- **Traffic lights, AI traffic and parked cars no longer float.** The world is a flat map around a centre point that
  moves every ~15 km, and the ground curves away from it: about 10 m lower at 11 km. The terrain, roads and buildings
  allow for that, but the AI road graph (traffic and its switchable traffic lights) and the cars parked in lots took
  the ground's height as it is, so the further you were from the centre, the higher they floated (signals 10-11 m up
  in Ruston, LA). Now they use the same curved ground as everything else, anywhere in the world.

### Retaining walls
- **No more stone-and-soil strips standing between roads.** A stone retaining wall is built where a road is cut more
  than 2 m into the ground. Beside a road at another height (I-20's other carriageway, a ramp, a service road) the
  ground is shaped by both roads, not cut, but walls were still built there and stood free between the roads, strips
  that could burst tyres (Ruston, LA). Walls are now only built against natural ground: about 870 m of stray wall
  gone around one Ruston interchange, the real retaining walls kept.

### Street furniture
- **Fewer floating traffic lights and signs.** Signals and signs are placed up to 10 m past a junction, at the
  junction road's height; on a sloping or raised road, or beside a ramp, they hung in the air (Pecanland Mall Drive,
  Monroe, LA, and reports on the Discord). A prop more than 0.6 m off the ground at its own foot is now set on it
  (not on or by a bridge deck).

### Loading at speed
- **The ground's photos keep up better at highway speed.** The server had the ground ready in time, but the game
  loaded it at a 2 ms per frame budget and fell behind, so the ground ahead showed bare and beige. Above 45 mph,
  with work waiting, the budget may now grow to 3.5 ms. The engine-load check still holds it back when frames get
  heavy.

## 1.5.4 beta (2026-10-21)

Works with BeamNG.drive 0.40.

### Level select
- **A new level select.** BeamNG.Earth's card in the freeroam menu shows the whole Earth with its details, and lists
  every saved place as a starting location with its own aerial picture. Places you add, rename or delete show up
  there by themselves.
- **Random location** starts you on a road somewhere in the world.
- **The default places start you on a road.** Some of them used to start you inside a building or on a steep slope.
  Pécs, Hungary is a new default place, and the Kayenta solar plant is no longer one (it stays in your list if you
  have it).

### World map
- **Controller support, laid out like the stock map.** The right stick moves the map, the triggers zoom, A sets the
  GPS marker (press it again to clear it), X teleports, Y turns autodrive on or off and B goes back.
- **Reduced the chance of the car moving while you use the map.** The sticks and triggers no longer drive the car
  behind the map.
- **The credits box no longer covers the Places panel** or the game's info bar.

### Steam Deck and smaller computers
- **BeamNG.Earth can now run on a Steam Deck**, in Game Mode too. The launcher starts the game through Steam's
  runtime, and on computers with less than 20 GB of memory the world server uses smaller caches and fewer helper
  processes.
- **The world should build much faster on a Steam Deck.** In testing at Baltimore, the ground under the car was ready
  about 8 s after a teleport (before: 17.5 s), and everything around it in about 53 s (before: 85 s).
- **Reduced the chance of "Near the edge of the loaded ground" warnings on a Steam Deck.** Each new piece of ground's
  aerial photo is now made by a separate helper process, so the next piece is not held up behind the rest of the
  server's work.
- **The world server uses much less memory.** Street widths, map data and elevation tiles are now shared between its
  processes instead of each keeping its own copy (in testing with the Steam Deck settings: about 1.7 GB, before:
  4.6 GB).

### Driving and loading
- **The world now loads around the camera, not just the car.** With the free camera or a far chase camera, the
  ground, roads, buildings and trees load where you are looking, while collision stays around your car.
- **Roads ahead should load first at high speed** (GitHub #50). Above about 55 mph, road pieces along your path
  come before those beside or behind you.
- **Much smoother driving through dense forest** (GitHub #66). Each piece of the world now shows at most a few
  thousand trees (in testing near Bodø, Norway on a Steam Deck: 1.66 million trees down to 142,000, and about
  32-36 fps instead of 20 while driving).
- **Reduced the chance of a hitch the first time you reach a tunnel, a bridge or a new kind of road or building.**
  The game now sets up BeamNG.Earth's road and building materials while the map loads.
- **The world server uses less of your CPU while driving.** In testing on a desktop, a 4-minute city drive used about
  0.6 of a CPU core on average (before: 1.6).
- **Teleports should finish a little sooner.** In testing, Baltimore was fully loaded about 25 % sooner.
- **Faster loading in open country with very long straight roads**, such as the Bonneville Salt Flats.

### Bridges
- **Reduced the chance of driving into the ground at the end of a bridge.** A straight bridge whose deck rises toward
  its middle could be drawn flat, below the road it meets at the far end, and a deck meeting a steep embankment could
  end in a step.
- **Reduced the chance of falling through a bridge that loads late.** A bridge deck that appeared as you drove up to
  it was kept from turning solid until you had passed it.
- **Reduced the chance of hitting a low overpass** (GitHub #51). The roads leading onto a short bridge over a highway
  are now raised on gentle approach ramps, so the bridge clears the lanes (from KyzoFox's PR #64; in testing, the
  bridge over I-20 near Temple, GA went from about 4.5 m over the lanes to about 10 m).
- **Reduced the chance of a step where a road meets a bridge** whose two ends are at different heights.

### Roads and ground
- **Road surfaces now follow the ground's own shape**, and underpasses no longer block the road above them.
- **Reduced the chance of medians and traffic islands catching a wheel** (GitHub #55). Narrow curbed strips no longer
  fold back over themselves in their collision.
- **Rural motorways** have two lanes each way between paved shoulders (GitHub #43).
- **A bare verge beside roads without sidewalks**, and roads are painted centred and at least their full width.
- **Salt flats, playas and dry lake beds are bare now.** Rocks lie only where the ground slopes, and trees misread
  from bright ground are left out.
- **Fewer cars and trucks drawn as buildings** (GitHub #80). Machine-learned building outlines on public roads are
  left out.
- **Reduced the chance of buildings floating over driveways and loading docks** (GitHub #60: a church in Oklahoma
  City floated 10 m up).

### From contributors (PR #41 by Madderose, PR #64 by KyzoFox)
- **Reduced the chance of a trailer coming off** when the world re-centres around you.
- **Faster handling of large bursts of messages** from the world server, and fewer road pieces casting shadows.
- **Tiles without roads are no longer rebuilt again and again.**

### Other fixes
- **Linux: the launcher no longer mistakes Steam Linux Runtime for Proton** (GitHub #56). With that compatibility
  setting the mod was put into an old Proton folder the game no longer used.
- **Reduced the chance of terrain failing to load on Windows** when the world server's cache files are in use by
  another of its processes (GitHub #67, "Access is denied").
- **The mod now stays out of the way on other maps**, apart from keeping its connection to the world server.
- **"Degraded" no longer shows** for services the world does not need (weather, search, routing, the update check)
  (GitHub #23).
- **Reduced the chance of the server stopping writing files** if its session folder is deleted while it runs
  (GitHub #27, #54).

If you want to talk about this mod with me or other people, join my Discord server at
[discord.gg/tgf](https://discord.gg/tgf) and head to the #beamng-earth channel.

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
