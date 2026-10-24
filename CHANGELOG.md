# Changelog

What changed in each release of BeamNG.Earth, newest first.

## 1.6.2 beta (2026-10-24)

Works with BeamNG.drive 0.40.3.

### Multiplayer
- **Find BeamNG.Earth servers in BeamMP's server browser.** Main menu > More > BeamMP has a new **BeamNG.Earth** tab:
  the servers running BeamNG.Earth, with their players and a Join button. They only show there, for players who have
  BeamNG.Earth: they no longer appear in BeamMP's normal list.
- **The app starts BeamMP for you.** No more "start the BeamMP launcher first": the BeamNG.Earth app starts BeamMP's
  official launcher (unchanged) with the game. If BeamMP isn't installed, the app asks: **Yes / No / Don't ask again**.
  - **Windows:** Yes downloads BeamMP's official launcher from BeamMP's GitHub, checked against the checksum BeamMP
    publishes.
  - **Linux and Steam Deck:** Yes builds BeamMP's launcher on your computer from BeamMP's own source, from the newest
    version that passed BeamMP's own tests (about a minute on a Steam Deck; it needs podman or docker, which SteamOS
    already has).
  - A new setting, **Start BeamMP with BeamNG.Earth** (Pause menu > Mods > BeamNG.Earth, Multiplayer), turns this off
    or back on.
- **BeamNG.Earth stays on after you leave a server.** Joining a BeamMP server switches your other mods off, and leaving
  left BeamNG.Earth switched off until the game was restarted.
- **Crash damage shows on the other players' screens, dents included.** With BeamNG 0.40, BeamMP's full damage
  message failed before it was sent, so other players only saw the parts that broke off. Now a crash you watch keeps
  the damage your own game worked out, and a car that crashed out of your sight takes on its owner's damage once, as it
  comes within about 0.6 miles: the broken parts at once, the dents growing in over a second. The damage message is
  about 6 times smaller than BeamMP's own. In testing with three players, copies of crashed cars ended within 0-24
  broken beams of the owner's car (they had been 16-120 off, or showed no damage). A car repaired or reset while far
  from you no longer comes back as a wreck.
- **Reduced the chance of your car vanishing in multiplayer.** Right after a spawn or a teleport, the guard that stops
  you driving off the edge of the ground could hold the car and let it go every frame, and BeamMP hides a held car.
  The mod now also holds your car without switching it off, so it stays visible.
- **Reduced the chance of other players' cars glitching or being removed** after one of you teleports across the world
  and back: position updates with broken numbers, and the last updates from where they were, are now ignored, and
  their car shows again only once there is ground under it.
- **Reduced the chance of other players' cars looking like they float, and of bumpy ground under them,** when they are
  far from you: cars beyond your drivable ground now hover where their owner says they are instead of falling.
- **Reduced the chance of long freezes on Windows when other cars are far away** (multiplayer, and AI traffic). The
  ground built under far cars is now many small pieces: Windows took 4-26 seconds to take in one big piece, and takes a
  fraction of a second for the same ground in pieces.
- **Shared world building is turned off for now:** each player builds their own world again. Cars still show in the
  right place.

### Smoother driving
- **Fewer stutters while driving** (all systems, most on Windows and the Steam Deck). Each time the drivable ground
  moved along with you, the game redrew all of the sun's shadows, which took up to 1/6 s on a Steam Deck; the ground
  no longer triggers that. In testing, shadow stutters on a Windows PC highway drive went from 14 to none, and on a
  Steam Deck city drive from 34 to 18.
- **Fewer stutters while driving on Windows.** The game's first look at each new file the world server writes (a piece
  of road, trees, distant land) could hold a frame, up to half a second. In testing on a 6-minute highway drive with
  DirectX 11, frames slower than 1/10 s went from 15 to 6 and frames slower than 1/30 s from 192 to 126.
- **Fewer stutters from parked cars.** The parking spot list was rebuilt every couple of seconds from every spot within
  about half a mile, up to 1/10 s near big parking lots; it now keeps only the nearest spots. In testing on a highway
  past Phoenix, frames held up by it went from about 25 to 1 in 4 minutes.
- **Reduced the chance of the distant land stopping to load on Windows** ("distant land 5/514"): its workers ran at
  Windows' lowest priority and got no CPU time while the other workers were busy.
- **Reduced the chance of the ground at your start point staying unfinished** (no aerial photo, sand showing through
  the road) until you drove off.

### Windows
- **Fixed starting the game with DirectX 12 from the app** (`--gfx dx12`: "There is no valid rendering device
  available").
- **Steam Controller works on Windows** (GitHub #218): the app starts the game through Steam, so Steam Input and the
  overlay reach it.
- **Reduced the chance of the world server staying open after the game closed** when the game took long to start.
- **Reduced the chance of a piece of distant land failing** when two of the world server's processes used the same
  downloaded photo at once.

### Roads
- **Reduced the chance of missing road pieces near long bridges and tunnels** (all systems): in testing, a downtown
  Chicago piece failed in 7 of 10 tries; now in none.

### Hosting a BeamMP server
- **Which list your server is on** is set in `ServerConfig.toml`: `Private = false` with your AuthKey puts it on the
  BeamNG.Earth list only (the plugin keeps it off BeamMP's own list); `Private = true`, or no AuthKey, keeps it off
  both, and players join with Direct Connect. See the server zip's README. Update the server zip with each release:
  it carries the mod your server sends to players.

### Which file do I need?
- **`BeamNG.Earth-1.6.2-beta.exe`: Windows players.** The whole app in one file: run it and it installs the mod into
  BeamNG.drive, starts the world server, BeamMP and the game (Windows may warn about an unknown app: More info > Run
  anyway). Steam must be running.
- **`BeamNG.Earth-1.6.2-beta-x86_64.AppImage`: Linux and Steam Deck players** (on the Deck, in Desktop Mode). The same
  app for Linux: make it executable (right-click > Properties, or `chmod +x`) and run it.
- **Joining a BeamMP server with BeamNG.Earth?** Run the app above, then in the game: More > BeamMP, the
  **BeamNG.Earth** tab.
- **`BeamNG.Earth-BeamMP-Server-1.6.2-beta.zip`: only for people hosting a BeamMP server** (unzip it into the server's
  folder; see its README). Players don't need it.
- **`Source code (zip)` and `Source code (tar.gz)`: for developers.** Added by GitHub to every release; not needed to
  play.

If you want to talk about this mod with me or other people, join my Discord server at
[discord.gg/tgf](https://discord.gg/tgf) and head to the #beamng-earth channel.

## 1.6.1 beta (2026-10-23)

Works with BeamNG.drive 0.40.3.

### Smoother driving
- **Reduced the chance of hitches on Windows when the ground moves with you.** Each time the ground under the car moved
  to its next piece, Windows rebuilt the ground's textures a second time. In testing on a Steam Deck, that frame got
  about 30 % shorter without it (a player's GTX 1060 showed 0.25-0.34 s frames there).
- **Reduced the chance of freezes in new places on Windows.** The world server's graphics card work added in 1.6.0
  (colour matching and texture compression) is off by default on Windows for now, as in 1.5.5, and the world server
  keeps to its own CPU cores while the game has the others, as on Linux (from KyzoFox's PR #112).
- **Reduced the chance of stutters while driving.** The traffic map, parked cars and collision updates no longer pile
  into the same frame as the road and building loading. In testing on a Steam Deck in Baltimore, frames slower than
  1/10 s went from 10-12 to 4 on the same 2.4-mile drive.
- **Fewer stutters from the ground moving under you in town.** The drivable ground moves ahead in longer steps at
  town speeds (about 4 times a mile instead of 8-12).
- **New ground is ready sooner while driving.** The aerial photo under each new piece of ground, the slowest part of
  building it, is made about 25 % faster on a Steam Deck and about twice as fast on a desktop, with the same result.
- **Better at very high speed.** Above about 157 mph a new piece of ground no longer waits for its aerial photo; the
  photo follows a moment later. In testing on a Steam Deck at 600 mph, the waits for the ground went from 11 a minute
  (7.6 s) to 4 (1.2 s).

### Faster loading
- **Faster teleports to new places.** On a first visit the car is placed in about 4 seconds on a Steam Deck (it took
  23-32 seconds); the roads are painted into the ground's photo a few seconds later.
- **Less downloading and less CPU on a first visit.** The world server's helper processes no longer download the same
  map data at the same time or work out the same roads' levelling. In testing, a first visit to Spokane downloaded
  about 260 MB instead of 400 MB, and a teleport on a Steam Deck used about 35 % less CPU.
- **Trees are placed about 9 times faster** (the same trees), and several other slow parts of the world server now run
  as fast native code.
- **Reduced the chance of very slow loading after many satellite photos in a day** (GitHub #116). In testing on a
  Steam Deck the car was placed in 10-11 seconds instead of 37.
- **Aerial photos in the US download faster:** more are fetched from the USGS at once.

### Loading line and minimap
- **A clearer loading line.** It shows the moment you teleport (F7 included), on its own line under the place name,
  with each part and its progress: the ground, roads & buildings, trees and the distant land. It stays until
  everything around you is in, including the ground's final photo with the roads painted in.
- **The minimap shows your Earth coordinates** (latitude and longitude) above it, instead of the town.

### Multiplayer (BeamMP)
- **Reduced the chance of multiplayer freezing.** While the ground under you was still loading (after a teleport, or
  BeamMP's teleport to a player), the mod paused the game, which in BeamMP froze every car on your screen, the other
  players' too. Now only your own car waits.
- **Reduced the chance of a freeze when players join.** Every join sent the full damage state of every nearby car to
  everyone, undamaged ones too; now only damaged cars' state is sent.
- **Another player's car you removed stays removed** (it came back at once with BeamMP's "Trying to respawn" notice).
- **The server picks the sharing port.** The TCP port players reach each other on to share world building (47621)
  is set at the top of the server plugin's `main.lua` (`G.SHARE_PORT`); every player who joins gets it.
- **Shared world building stays consistent:** a helper building for another player builds the roads the same way that
  player's game does (from hashk014's PR #187).

### Fixes
- **Reduced the chance of roads and water missing in new places.** 1.5.5 and 1.6.0 came with a newer version of a
  library the world server reads map data with, and on a first visit some of its reads came out corrupted, so those
  areas lost their road details or their water. This version goes back to the one 1.5.4 used.
- **Reduced the chance of roads missing their lane counts and widths for good** when the OpenStreetMap road details
  could not be fetched ahead of time; they are now fetched again when the area is needed.
- **Reduced the chance of the autodrive getting stuck** (GitHub #180). On divided roads its next target could fall on
  the other side of the highway, and the car tried to turn around and crawled at 4 mph. Targets now stay on the car's
  own side, and a car that stops making progress plans its path again, then is put back on the road ahead.
- **The download cache keeps to its size limit.** Several of its folders grew outside it, run after run (35 GB on a
  test machine set to 20 GB); they are now trimmed when the world server starts, without stalling the ground's
  building.
- **The mod loads in a game that was already running.** When the BeamNG.Earth app starts while the game runs (started
  by the BeamMP launcher, for example) and the mod was just installed, updated or turned back on, the app has the game
  reload its Lua, so the mod loads without restarting the game; a level that was open is started again.

### For bug reports
- **A log that helps us find what's slow.** The world server's log (`server.log` in the `beamng-earth\logs` folder)
  now also records your computer and graphics settings, every hitch and frame-rate drop in the game with where it
  happened, how fast you were going and what was loading, and a summary every minute. If something stutters or
  freezes, send us that file right after it happens.

### Which file do I need?
- **`BeamNG.Earth-1.6.1-beta.exe`: Windows players.** The whole app in one file: run it and it installs the mod into
  BeamNG.drive, starts the world server and starts the game (Windows may warn about an unknown app: More info > Run
  anyway). Steam must be running.
- **`BeamNG.Earth-1.6.1-beta-x86_64.AppImage`: Linux and Steam Deck players** (on the Deck, in Desktop Mode). The same
  app for Linux: make it executable (right-click > Properties, or `chmod +x`) and run it.
- **Joining a BeamMP server with BeamNG.Earth?** You need the app above (the same version as the server's). The best
  way: start the **BeamMP launcher first** and wait until the game has fully started, **then** start the BeamNG.Earth
  app, then join a server running the BeamMP.Earth plugin (the [Discord](https://discord.gg/tgf) has the IPs).
- **`BeamNG.Earth-BeamMP-Server-1.6.1-beta.zip`: only for people hosting a BeamMP server.** The drop-in server
  package (unzip it into the BeamMP server's folder, see the README's Multiplayer section). Players joining a server
  don't need it: they run the app above.
- **`Source code (zip)` and `Source code (tar.gz)`: for developers.** Added by GitHub to every release: the project's
  code as it was for this version, the same files in two archive formats (zip for Windows, tar.gz for Linux and
  macOS). Only needed to read or change the code, or to run from source (`install.py`, then `start.py`); not needed to
  play.

If you want to talk about this mod with me or other people, join my Discord server at
[discord.gg/tgf](https://discord.gg/tgf) and head to the #beamng-earth channel.

## 1.6.0 beta (2026-10-23)

Works with BeamNG.drive 0.40.3.

### Multiplayer (BeamMP, in testing)
- **Want to play together? Join the [Discord](https://discord.gg/tgf) to get an IP to join a BeamMP.Earth server.**
- **BeamNG.Earth on BeamMP servers.** A server runs it from the drop-in server package (see the README). Every player needs the BeamNG.Earth app running; a player who joins is sent to where the others are, and each player's car shows in the right place in everyone's world (within a few centimetres in testing), with its dents. Players far apart don't see each other's cars until they meet.
- **Players near each other share the work of building the world.** Players close together use one shared world origin, and each piece of distant landscape is built once by one of them and sent to the others; a player far from everyone builds their own, and faster computers take a bigger share. In testing, a Steam Deck joining a desktop that had already loaded the area got most of its distant landscape from it (69 seconds instead of 110-150). Over the internet this needs TCP port 47621 open; otherwise each player builds their own.
- **Other players on the world map**, as green arrows with their names.
- **Reduced the chance of other players' cars showing as dark spheres** after one of you teleported far away and back. A car that ended up thousands of miles from your world could break and be removed by the game; far cars are now hidden until they come back, and a removed car comes back once its player is near.
- **Reduced the chance of the BeamNG.Earth level going missing after playing on a BeamMP server.** Joining any BeamMP server turns off your other mods and leaves them off; the BeamNG.Earth launcher now turns BeamNG.Earth back on before the game starts.
- **Your other cars stay where you left them.** A car of yours more than about 1.5 miles away is saved with its damage and taken out, and comes back when you return to it; other cars (yours, AI traffic, other players') now have collision with the roads, buildings and trees around them, not only the car you drive.

### Faster loading
- **Faster teleports to new places.** The car is placed as soon as the ground under it is there, in about 5 seconds on a first visit (it took 30-60 seconds); the levelled roads, walls and river beds are swapped in a little later. The distant landscape of a first visit also loads in about half the time.
- **Your graphics card now takes some of the world server's work** (the colour matching of the aerial photos and the compression of the distant textures), so the server needs much less of your CPU. Without a usable graphics card the server does the same work on the CPU as before.
- **Distant terrain is lighter on your graphics card:** flat land far away is drawn with far fewer triangles (75-85 % fewer over plains and cities, 25-35 % fewer in the mountains), while cliffs keep their full detail.
- **Compute sharing (experimental, for tinkerers):** another computer on your network can build distant landscape for you (see the README). In testing, a Steam Deck with a mini PC and a desktop helping loaded the distant landscape of a first visit in 40-55 seconds instead of 110-150.

### Look
- **Reduced the chance of red and blue stripes over the ground** (GitHub #115). Since 1.5.0 the distant ground could be drawn grey-white with red and neon-blue stripes over it, worst at Low texture quality: the smaller versions of the ground's photo the game uses in the distance were made wrong. They now match the photo (in testing on a Steam Deck, the stripes were gone at the same spot).
- **Roads are now part of the ground** instead of a road surface laid over it: the asphalt, cobbles and gravel are painted into the ground's own texture, with the lane markings, curbs and sidewalks on top. It also ends white diamond shapes that showed on some mountain roads (GitHub #135, the Stelvio Pass).
- **Real lane counts and road widths** from OpenStreetMap where they are mapped (four-lane streets are drawn four lanes wide, with their lane lines).
- **Racetracks are drawn** (GitHub #143): circuits, ovals and pit lanes from OpenStreetMap, with their real banking from the elevation data (in testing: Daytona's 31° turns, the Nürburgring). Roads and tunnels crossing a track no longer cut into it.
- **Sidewalks follow the ground** (from FluffyProgramming's PR #82), and **sett and paving-stone streets stay cobbled, concrete streets are concrete** (from erkinalp's PR #119).
- **Sharper ground in Poland** (the national aerial photos, about 25 cm) and **across northern Europe**, where the national aerial photos were skipped for the ground right around you north of about 47°.
- **Sunken city roads get full-height concrete walls** instead of a 4 m wall with a bare earth cliff above it, and **reduced the chance of white "tents" along walls beside sunken roads.**
- **Reduced the chance of bridges sitting on the road they cross** (GitHub #138, #142): where the elevation data has a short bridge filled in at deck height, the road under it now dips to pass beneath.
- **Reduced the chance of paved areas layered and smeared over each other** (GitHub #127: overlapping ones are paved once) and **of tunnels showing through rivers and harbours** (GitHub #128: tunnels under water are left out).
- **You can drive to the poles.** Teleporting near either pole used to freeze the world server. Antarctica's ice sheet now has its real heights up to the South Pole (2,835 m), and everything past 85 degrees is snow and ice.

### Map and interface
- **Minimap:** the .Earth layout now has BeamNG 0.40's round minimap at the bottom left, with the roads around you and your GPS route; its label shows the town you are in, and its N points to true north.
- **Update now button** (GitHub #140): when a new version is out, the in-game notice can download it for you; the next time you start BeamNG.Earth, even from your old shortcut, the new version starts.
- **Reduced the chance of Autodrive circling on steep roads** (GitHub #131).
- **Wireframe view** (Ctrl+Alt+W): shows the world as triangle edges.

If you want to talk about this mod with me or other people, join my Discord server at [discord.gg/tgf](https://discord.gg/tgf) and head to the #beamng-earth channel.

## 1.5.5 beta (2026-10-22)

Works with BeamNG.drive 0.40.1.

### Smoother driving and loading
- **Less of a pulse in the game's flow while driving.** Each new piece of road or distant landscape that loaded in
  made the game redraw the sun's shadows, many times a second, and the game's clock sped up and slowed down around
  each one. In testing at 80 mph, slow frames went from about 17 a second to about 2.
- **Reduced the chance of freezes of a few seconds after an update.** Updating the mod made the game rebuild every
  ground texture again, one at a time as each new kind of ground came into view (2.5-3 s each). Files that did not
  change now keep their old time.
- **Reduced the chance of a stutter as new ground loads.** Each piece of ground sets its aerial photo before it swaps
  in, so the game rebuilds its ground textures once instead of twice (from Alfao's PR #71; in testing on a desktop,
  the frame where new ground appears went from about 40 ms to about 22 ms).
- **Faster ground loading in cities with short tunnels and underpasses.** Working out where a road passes over an
  underpass's entrance took up to two minutes for one piece of ground in Oslo; it now takes a fraction of a second.
- **Reduced the chance of roads no longer loading for the rest of a session** (GitHub #76, #91). When one of the world
  server's helper processes crashed, the roads and buildings it builds stopped coming while the ground went on
  loading; the server now starts the helper again.
- **Reduced the chance of "Too many open files" errors on Linux** (GitHub #99). The world server now raises its limit
  at start.
- **Reduced the chance of driving onto a bridge or into a tunnel before it is solid on slower computers.** Their
  collision now loads before sidewalks' and buildings' (in testing on a Steam Deck at 80 mph into Phoenix, an overpass
  deck had turned solid only as the car reached it).
- **Reduced the chance of low-detail distant ground showing through the ground around you** (GitHub #46), mostly on
  slower computers at speed.

### Terrain and roads
- **Sharper terrain along roads cut into hills.** The elevation data blurs steep road cuttings into gentle ramps.
  Where the ground rises steeply beside a road, the road now sits in a real cutting with a retaining wall: dry stone
  walls in the mountains and towns, concrete along motorways and major roads, and low stone parapets along the drops
  of mountain roads. Walls stop before tight hairpin bends and are at most about 4 m tall, with the slope above them.
- **More detailed ground where the best elevation data exists** (most of Europe and much of the US): the near terrain
  and the road profiles use elevation tiles twice as detailed as before.
- **Reduced the chance of roads at different heights pulling each other up or down** (GitHub #92). A road beside a
  street in a cutting, or up on a bank, pulled the street's edge up its slope, and sunken roads blended into the
  streets above them; each road now stays level across its own lanes in more places (in testing: Munich's sunken
  McGraw-Graben, a cutting in Pullach). Tight mountain hairpins, such as on the Stelvio Pass, still need work: the
  ground between their levels can still be uneven.
- **Reduced the chance of patches of ground showing through the road** on long mountain roads and highways.
- **Reduced the chance of sidewalks hanging down walls and slopes** beside sunken or raised roads.
- **Motorway ramps should now have one proper lane** (GitHub #107). A ramp took the main road's wide shoulders, so its
  edge lines stood about 1.6 m apart and the traffic AI saw two lanes; ramps now get one 3.7 m lane between narrower
  shoulders.
- **Reduced the chance of a ledge where a ramp leaves an elevated motorway** (I-83 in Baltimore had a step across the
  lanes that stopped cars).
- **Squares and pedestrian areas are paved as they are mapped** (GitHub #70): asphalt, stone setts or brick, and sand
  or gravel ones are left to the aerial photo, where every square used to be red brick. Fountains, lawns and buildings
  inside a square are no longer paved over.
- **Reduced the chance of traffic signal poles standing in the road.** The game's working traffic lights now stand
  where the mod's signal poles do, clear of the lanes and of bridge decks.
- **Reduced the chance of yard fences standing in the street** (GitHub #105). Fence pieces now keep 2 m from every
  road.

### Bridges and tunnels
- **Reduced the chance of crashing into a wall of ground at the end of a bridge** (GitHub #108). Where a street passed
  under a bridge right by its end, the ground at the bridge end was pulled down toward the street, and the deck ended
  at a 2-3 m wall (Meppen, Germany).
- **Reduced the chance of a dip and a step where a bridge meets its road.** On the D1 motorway near Prešov, Slovakia
  the deck sank 3.6 m and met a 1.4 m step, and a car flipped there at 55 mph; the road now keeps its grade to the
  bridge.
- **Cable-stayed and arch bridges are drawn as they stand today** (GitHub #65). The Port Mann Bridge near Vancouver
  was drawn as the arch bridge it replaced in 2012. Stays and arches also keep clear of the lanes.
- **Reduced the chance of bridge walls standing across a road** (GitHub #96), as on the Pitt River Bridge's ramps over
  Lougheed Highway.
- **Reduced the chance of retaining walls standing in the way under a bridge deck** (one went through the Bruce
  Highway's deck in Australia).
- **Twin tunnels share one tube** (GitHub #84). Two carriageways side by side in a tunnel each had their own tube,
  standing through each other's walls and portals.
- **Reduced the chance of ridges or dams across rivers and harbours over underwater tunnels** (found by storm-ace,
  GitHub #85), such as across the Maas in Rotterdam and along Baltimore's Fort McHenry tunnel.

### Water
- **Reduced the chance of water flooding towns and polders in the Netherlands** (GitHub #8, #53). Lakes now keep their
  own level, no higher than the ground round them, and water no longer covers land well below it (in testing at
  Maarssen the streets are dry).
- **Reduced the chance of deep holes in harbours on lakes** (GitHub #45). A harbour or bay on a lake was carved down
  as if it were the sea, with holes nearly 180 m deep along Chicago's lakeshore.

### Buildings and places
- **Reduced the chance of buildings standing across roads** (GitHub #48). Narrow shelters and walkways a road passes
  under are raised over it, and shelters are drawn as low canopies.
- **Reduced the chance of houses standing between a road's two carriageways** (GitHub #90).
- **The pyramids of Giza are drawn as stone pyramids** (GitHub #47), not as large buildings with windows.
- **Grain elevators in small US towns are drawn as plain concrete towers** instead of brick apartment blocks with
  windows.
- **Fewer flat boxes drawn as buildings.** Footprints mapped less than 0.1 m tall (plazas, pads) are left out.
- **Toll plazas show toll booths**, using West Coast USA's toll booth.

### Trees
- **Fewer palm trees where they don't belong** (GitHub #110). Palms are now common only in South Florida, the Rio
  Grande Valley, coastal Southern California and the tropics, and the Southeast's conifers look like pines.
- **Tropical trees' vines and hanging roots are textured again.** They showed as bright orange "no texture" stripes in
  the tropics.

### Traffic
- **Reduced the chance of AI traffic turning around right after it appears** (GitHub #72). In testing in Bathurst,
  Australia: 3 turnarounds in 79 appearances before, none in 129 after.

### Other
- **Fewer "data sources not answering" warnings.** A regional aerial-photo service being down no longer shows the
  warning while the worldwide imagery fills in; ground left with no imagery at all still says so.
- **In-game reports name the release version** (they showed the mod's internal version number).

If you want to talk about this mod with me or other people, join my Discord server at
[discord.gg/tgf](https://discord.gg/tgf) and head to the #beamng-earth channel.

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
