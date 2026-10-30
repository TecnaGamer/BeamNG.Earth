# Changelog

What changed in each release of BeamNG.Earth, newest first.

## 1.7.2 beta (2026-10-29)

> **Join the TGForever Discord, [discord.gg/tgf](https://discord.gg/tgf), to chat, get help, report bugs or help
> develop the mod.** For help, that includes multiplayer:
> BeamNG.Earth isn't like a normal BeamNG mod (it runs its own app next to the game), so if something isn't working
> with BeamMP, it's almost certainly a BeamNG.Earth problem, not a BeamMP one. Please don't contact the BeamMP team or
> open BeamMP support tickets about it. BeamNG.Earth servers are hidden from players who don't run BeamNG.Earth
> (BeamMP can't install it for them: it needs its own app), so they only show in the BeamNG.Earth tab of BeamMP's
> server browser.

Works with BeamNG.drive 0.40.4.

### New
- **See what's downloaded on the map**: Layers > Cache colours the map by what is on your disk (some data, the far
  view's data, or everything needed to drive there), with views for elevation, aerial photos and map data, and
  **World built**: where the terrain, far view, roads and buildings are already generated and kept on disk.
- **Pre-cache an area**: drag a box on the map (the Pre-cache area button or Shift+drag) to download every bit of
  elevation, aerial photo and map data it needs, with the size, download and time shown first. It downloads in the
  background when the game isn't loading anything, picks up where it left off after a restart, and Clear cache keeps
  your pre-cached areas.
- **Pre-cache a GPS route**: Pre-cache route on the map's GPS box downloads everything along the planned route (a quarter
  mile to 2 miles each side) the same way as a pre-cached area, starting from the route's start, and keeps it.
- **Build the world ahead**: tick "Build the world too" when pre-caching an area or a route (up to 155 square miles) and
  its roads, sidewalks, bridges, buildings and street furniture are built in the background as well, so arriving
  there is much faster (Annapolis: the roads round the car were ready 4 seconds after a teleport instead of 23).
  After a BeamNG.Earth update they are built again in the background.
- **Download along the GPS route** (Settings > Storage): what a planned route downloads ahead by itself: the next 12
  miles (as before), the whole route (up to 300 miles), or nothing.
- **Players list on the map in multiplayer**: follow another player, show everyone, or set the GPS to them (thanks
  iambobab, #459).
- **Teleport without a car**: with no car the free camera goes to the place, and Go waits for the ground to load
  (thanks iambobab, #460).

### Improved
- The download cache's size limit (Settings > Storage) is now typed in GB (any size; 0 for no limit) instead of
  picked from a list.
- **Steadier game speed**: the game's time no longer speeds up and slows down as much while the world loads around
  you. A new setting, **Even frame pacing** (Graphics, on by default), keeps the time between frames steady, which
  costs up to about a tenth of your frame rate; turn it off for the highest frame rate. It stays off below 30 fps.
  Some of the mod's own work each frame was also cut (the far buildings' checks, new tree groups made while driving).
- **Smoother, faster rebases** (the world's origin moving with you every 9 miles): roads, buildings, bridges,
  street furniture and trees no longer disappear and get rebuilt around you; they are built once per place, kept,
  and only the road surface is laid again (Fells Point: the roads within 300 m exact again after 0.4 s instead of
  2.2 s, about 6x less work; Los Angeles 1.3 s instead of 4.7 s). Teleporting near a place you visited reuses it too.
- **Faster far view after a rebase or teleport**: the aerial photos' shadow removal, colour matching and tone are
  now done once per photo tile and kept on disk, so the far view's ground no longer redoes them for every new
  origin (Kern River: the far view ready after a rebase in 8.7 s instead of 11.8 s, with about a quarter less
  CPU; first visits are a bit faster too). It uses up to about 60 MB of the download cache per place.
- Building colours, roofs and all-way stops are now chosen from each building's and junction's own map data, so they
  stay the same wherever you start from (some buildings and junctions look different once).
- **Bridges and tunnels at their real heights where measured data exists**: in the US, bridge decks follow the USGS
  lidar survey of the bridge itself (the LA Four Level's ramps came out within about 2 inches of the survey, from
  about 2.5 ft off); in France (IGN BD TOPO) and Norway (the national road database), bridges, tunnels and roads on
  coarse terrain take the countries' own measured road heights (the Millau viaduct within an inch, from about 8 ft).
  Elsewhere bridges are worked out from the terrain as before.

### Fixed
- Reduced the chance of speed-limit and highway signs appearing only when you are right next to them while driving:
  they now come as soon as the roads under them are in (about 700 m ahead instead of about 300).
- Turn arrows painted on the road now show the arrow the right way up (with game 0.40 straight-ahead lanes showed an
  upside-down "EXIT" instead).
- Reduced the chance of a freeway exit sign standing in a street's lane where an exit ramp comes down (Baltimore's
  Guilford Avenue exit), and made one-destination exit signs their real size (they stood about 20 ft tall).
- Reduced the chance of rivers that run underground in pipes being drawn as open water and carved into the ground
  (thanks iknlm, #474).
- Reduced the chance of large roundabouts getting a too-small island, entry stubs, or the aerial photo's road showing
  through the island (thanks soifaxa, #468).
- Reduced the chance of sidewalks, street lamps and stone bridge walls on rural divided highways (thanks
  wyo-backroads, #458).
- Reduced the chance of traffic cars being respawned again and again in a loop (thanks OmdevShastri, #493).
- Reduced the chance of far-away trees staying summer green while the trees near you have their autumn colours: the
  flat far versions of oaks, beeches, birches and aspens now take the season's colours too.
- Reduced the chance of elevated ramps and viaduct stretches the map marks as ground being drawn at street level in
  the US: the lidar survey along them decides (the Springfield Interchange: no more ramps under 15 ft over a road,
  the Backlick Road bridge with its real clearance).
- Reduced the chance of autodrive and traffic driving a few feet off their lanes where a ramp joins a motorway.
- Reduced the chance of a hole or the end of a bridge wall in your way where a street leaves a bridge at an angle
  (Charles Street over I-83 in Baltimore): such junctions get a paved pad with a railing only over the drop.
- Reduced the chance of a viaduct ending in a drop to the street where the map starts a branch a few feet off its
  split (Baltimore's Jones Falls Expressway had a 26 ft drop across the lanes; the Springfield Interchange a 44 ft
  step), and of short stretches between two bridges being drawn at street level.
- Reduced the chance of autodrive and traffic swerving or running off the road right after the world's origin moves
  (every 9 miles): the AI's road map now moves with the world at once.
- Reduced the chance of other players' cars in multiplayer standing a few yards off the road on high ground when
  your world origins are far apart.

Questions, bugs or just want to chat? Join the TGForever Discord: https://discord.gg/tgf

## 1.7.1 beta (2026-10-29)

> **Join the TGForever Discord, [discord.gg/tgf](https://discord.gg/tgf), to chat, get help, report bugs or help
> develop the mod.** For help, that includes multiplayer:
> BeamNG.Earth isn't like a normal BeamNG mod (it runs its own app next to the game), so if something isn't working
> with BeamMP, it's almost certainly a BeamNG.Earth problem, not a BeamMP one. Please don't contact the BeamMP team or
> open BeamMP support tickets about it. BeamNG.Earth servers are hidden from players who don't run BeamNG.Earth
> (BeamMP can't install it for them: it needs its own app), so they only show in the BeamNG.Earth tab of BeamMP's
> server browser.

Works with BeamNG.drive 0.40.3.

### New
- **Settings in a tab of their own in the pause menu** (next to BeamMP's), in the game's own style, every page listed
  down the side: Graphics, World, Interface, Multiplayer, Storage, Feedback, About, Developer.
- **Quality presets and many more graphics settings**: Low / Medium / High / Ultra presets, plus an **Extreme**
  preset that puts everything at its highest out to the real horizon (for strong PCs; never picked for you). New
  settings: terrain detail (how far out the distant ground stays finely detailed), ground photo (1024 /
  2048 / 4096, from the next level load), tree density and tree draw distance, building draw distance, object detail
  (how far out models keep their finest detail, in BeamNG.Earth only; your game's own setting comes back when you
  leave), grass draw distance, and the view distance. The first start picks a preset for your hardware (Medium on a
  Steam Deck); settings you changed before are kept.
- **What loads around you reaches the same distance in every direction**: roads, trees and buildings used to load in
  a square around the car, so they reached farther towards its corners; they now load in a circle.
- **Guard rails** (MAKAKAIglitch's pull request #205): guard rails mapped in OpenStreetMap are placed as the game's own
  metal rail sections, following the road's slope, with flared end pieces. Bridges whose edges have mapped rails get
  a low concrete curb with a rail on top instead of the full concrete wall, and rails leading onto a bridge join its rail.
- **Canada** (spacedroogz-max's pull request #206): British Columbia's 10-15 cm aerial photos on the ground around
  Vancouver and Victoria, and the distant terrain across southern Canada lined up with Natural Resources Canada's
  lidar ground, so it no longer stands on the forest tops. The full lidar terrain and tree heights are there too, off
  by default for now (they made first visits about a minute slower).
- **Parking lots** (AnthonyGraceffa's pull request #442): mapped parking lots are paved in the ground photo and get
  painted stall lines where the parked cars stand.
- **Building colours** (hudsontadams's pull request #167): buildings with a mapped wall or roof colour or material
  (brick, glass, a red roof ...) now show it, part by part.
- **Highway signs on US freeways** (from cody196's contribution): exit signs at 1 mile, 1/2 mile and 1/4 mile and in
  the gore, overhead destination signs with route shields, merge warnings and Interstate mile markers, from the signs
  mapped in OpenStreetMap. Settings > World > Highway signs.
- **Speed-limit signs everywhere**: the posted limit the AI drives, after junctions where it changes, past motorway
  entrances and as reminders; US, Canadian ("MAXIMUM", GitHub #454) and European styles (mph in the UK).
- **Median barriers on divided highways**: concrete walls, cable barriers or steel rails by the median's width and
  the country's practice, open at crossovers and ramps; mapped concrete and cable barriers too.
- **Rounded corners where country roads meet** (from cody196's contribution): the asphalt curves round the corner and
  the gravel edge and edge lines follow it.

### Improved
- **Sharper road edges and ground** (top-down shots of Woodland Park, Colorado): the ground photo under the
  car has twice the pixels per side, with the roads painted into it at about 20 inches a pixel and lined up exactly
  with the road's lines (they sat about 3 ft to the south-west), and roads without
  curbs get a gravel shoulder edge, so their edges are crisp instead of a blurred, stepped band.
- **The road's colour, grip and lines line up**: the asphalt in the ground sat up to about 5 ft to one side of the
  painted lines on some roads, and near one edge the tyres could be on dirt (and dirt's texture showed on the
  asphalt) while still inside the lines. The asphalt's colour and its grip now lie exactly under the road's lines.
- **Road markings by country** (from the national road marking manuals): line colours, widths and dash lengths now
  follow the country (for example the US's 10 ft dashes every 40 ft, Germany's 6 m dashes on country roads, Britain's
  shorter town patterns); US-style yellow left edge lines on divided roads and ramps; rural two-lane roads in the US
  get a broken centre line, town streets a double yellow; dashes are real dashes now, without the grey smear between
  them.
- **Painted gores at freeway exits and entrances**: the wedge where a ramp splits from or joins a freeway is now
  paved, with the wide white lines and the white chevrons pointing at oncoming traffic, as on the real roads.
- **Bridges look and drive like the real ones** (jonathonbarton's PR #409): decks climb smoothly to what they cross
  instead of humping, river bridges hold their real clearance over the water, lift and bascule bridges get their towers
  and control houses, lens trusses span the river, and the parapets are solid concrete barriers.
- **Motorway curves without banking bumps** (wyo-backroads' PR #410, with our fix): banking follows the whole curve.
- **No street lamps along empty rural interstates** (wyo-backroads' PR #404); OpenStreetMap's lit tags are used.
- **Lane counts from the map** (jonathonbarton's PR #405): a street keeps its lanes where one piece of it was mapped
  without a count, and one-way streets join a two-way street on their own side of it.
- **Smoother roads and junctions** (mpsteppe's PRs #289, #290): side roads meet the road they join at its height, and
  mid-size and dirt roads follow fewer small bumps in the elevation data.
- **Gas stations in Japan, and painted lots after a teleport** (FluffyProgramming's PR #401); big stations no longer
  cause a hitch when they appear.
- **Railways still under construction are no longer drawn** (OmdevShastri's PR #392).
- **Country roads the right width**: small rural roads were drawn about 6 ft too wide (measured on official aerial
  photos in Pennsylvania and Colorado); they now match, and in the US they get a centre line without edge lines, as
  most of them have.
- **Turn arrows and ladder crosswalks**: lanes mapped as turn lanes get their arrows before the junction, and US
  crosswalks get the ladder style (the bars with a line along each side).
- **Lane lines follow the map's lanes** where OpenStreetMap maps them: uneven lanes each way move the centre line,
  centre turn lanes get their yellow lines, no-passing stretches get solid lines, and the traffic drives in the
  lanes that are painted.
- **Reduced the chance of a pale strip beside one edge of a road**: where the aerial photo's pavement is wider than
  the mapped road or a little off it, that strip of the photo is now replaced with the ground beside it.
- **Fewer trees on roads and sidewalks** (GitHub #424, #425): trees and bushes keep clear of the road's full drawn
  width (parking lanes included) and of its sidewalks; street trees stand by the curb.
- **Traffic lights no longer light every colour at once** where they don't switch (GitHub #415): those now show dark
  lenses.
- **Office towers at night look like a city at night**: Manhattan's glass towers lit up white from street to roof
  (TGForever Discord). Their windows are now dimmer and about half of them are dark.

### Fixed
- Reduced the chance of concrete retaining walls standing beside country roads in front of natural banks (a low rocky
  bank along US-163 in Utah had a long concrete wall): walls now stand only where the road was actually cut into the
  ground.
- Reduced the chance of the road's painted lines lying a few feet to one side of its asphalt and grip far from where
  you started, on high ground (on US-163 in Utah, 8 miles out at 5,400 ft, they were about 7 ft apart): the lines,
  shorelines, and the positions of cars across a rebase now follow the ground's own positions.
- Reduced the chance of side streets humping up where they meet a road that dips under a bridge (Haven Street,
  Baltimore).
- Reduced the chance of a fixed view distance (the Low preset's 25 miles) being replaced by the horizon's distance
  while driving or flying; switching back to the real horizon now brings the far view back at once.
- Reduced the chance of the game speed seeming to pulse (speeding up and slowing down every fraction of a second) in
  wooded places: the trees' updates no longer run when nothing changed.
- Reduced the collision rebuild cost along railway tracks (the rails and sleepers no longer collide; cars ride on the
  ballast) (from Madderose's contribution).
- Road surfaces no longer cast hidden sun shadows (a little faster rendering) (from Madderose's contribution).
- Reduced the chance of world server processes staying behind after closing on Windows (from Madderose's
  contribution).
- Reduced the chance of autodrive and traffic crawling at 30 km/h on narrow country roads with a higher limit
  (GitHub #451, the Netherlands' 60 km/h roads).
- **Underpasses are open roads under a bridge** (GitHub #450, the A-7 near Málaga; #228, #243, the A7 in
  Germany): the map often marks the road under a highway as a short tunnel, and it was built as a tube in an
  embankment, its vault and openings in the highway's lanes. A short road tunnel (under about 260 ft, counting all of
  its pieces) with little ground over it is now a road under a bridge, as are streets over a railway the map marks
  as a short tunnel (Chicago's streets over the Union Station tracks).
- Reduced the chance of a highway dipping where a longer tunnel runs under it (about 9 ft at a culvert under the A-7
  near Málaga), and of a tunnel's portal ending inside the lanes of a road crossing over it.
- Reduced the chance of a hole in the road just outside a low tunnel's entrance (cars could fall through it),
  depending on which way the entrance faced.
- Reduced the chance of long mountain tunnels being left out because of a lake or stream far above them: the
  Gotthard road tunnel (10.5 miles) has its entrances at Göschenen and Airolo again (TGForever Discord report).
- Reduced the chance of roads still under construction being built as finished ones when the map's road details
  missed them (the Gotthard road tunnel's second bore, due in 2029).
- Reduced the chance of a tunnel entrance standing in the lanes of a road running alongside it (a service gallery's
  arch and mound in the A2's lanes at Göschenen).
- Motorway and main-road tunnels under shallow ground start where the map puts their entrance, as a covered
  concrete section tall enough for trucks, instead of an open cut further in.
- Motorway and main-road tunnels under shallow ground get concrete entrances instead of the stone arches of park
  underpasses.
- Reduced the chance of a street being dug into a walled trench under a railway where only coarse elevation data
  exists (Perm, Russia, GitHub #414): the railway now rises on its embankment and the street stays at grade.
- Reduced the chance of roads and buildings never loading after a road worker stopped answering ("roads & buildings
  0/26"): the worker is restarted and its tiles built again (KyzoFox's pull request #444).
- Reduced the chance of a dirt edge strip across a parking lot's asphalt along its aisles.
- Reduced the chance of a suspension or cable-stayed bridge's tower leg standing in the lanes of the other direction
  (jonathonbarton's pull request #441).
- **Reduced the chance of tunnel entrances blocked by the hillside** (GitHub #403, Malibu Canyon Road's tunnel in
  California): where a road was cut into a cliff right at a tunnel's mouth, wedges of ground stood across the
  opening from the road up to the roof. The ground is now cut away wherever it reaches into the opening. Pittsburgh's
  Fort Pitt Tunnel's upper portal was nearly closed the same way and is open now.
- **Fewer street lamps at interchanges in the countryside** (TGForever Discord): every ramp was lit end to end, even
  out in Wyoming. A ramp out of town now has lamps only where it leaves the highway and where it meets the road at its
  end, as most rural interchanges are lit; ramps in towns and cities stay lit along their length.
- **Smoother banked curves** (TGForever Discord: "a series of bumps where the bank would be"): the map draws a curve
  as straight pieces that turn every few hundred feet, and the bank came and went at each turn. Curves now lean
  evenly from start to end, easing in and out over about 200 ft.
- **Faster first visits on slower connections** (GitHub #241): the ground under the car read the elevation along
  each river and stream one after another, a trip to the elevation server each; it now asks for all of them at once.
  On a Windows PC on Wi-Fi a new town's rivers came in about twice as fast.
- **A slow internet connection is named as such** (GitHub #241): when the map data comes in very slowly, the status
  line says so ("slow internet connection: new places take a few minutes to load") instead of looking stuck.
- **Roads build faster**: the roads, bridges and props around you take about a quarter less computer time to build
  (more of the server's work now runs in its fast native code).
- **Reduced the chance of roads taking minutes on PCs with few cores** (GitHub #412): after a teleport the app lends
  the game's spare cores to the world builder while you wait; driving off ended that at once, even with no roads
  built yet. It now lasts until the roads around you are there (two minutes at most).
- **Fewer city-sized roofs** (GitHub #413): a mapping error drew one "building" over a whole city in Egypt, its
  concrete roof over every street. Footprints larger than any real building (about 0.8 square miles) are now left out.
- **Fewer power-line towers on the road** (TGForever Discord): where a mapped tower sat on a highway's shoulder, the
  tower now stands just off the road.
- **A quieter console** (GitHub #391): "bridge clearance below 6.0 m target" no longer fills the app's window.

Questions, bugs or just want to chat? Join the TGForever Discord: https://discord.gg/tgf

## 1.7.0 beta (2026-10-27)

> **Join the TGForever Discord, [discord.gg/tgf](https://discord.gg/tgf), to chat, get help, report bugs or help
> develop the mod.** For help, that includes multiplayer:
> BeamNG.Earth isn't like a normal BeamNG mod (it runs its own app next to the game), so if something isn't working
> with BeamMP, it's almost certainly a BeamNG.Earth problem, not a BeamMP one. Please don't contact the BeamMP team or
> open BeamMP support tickets about it. BeamNG.Earth servers are hidden from players who don't run BeamNG.Earth
> (BeamMP can't install it for them: it needs its own app), so they only show in the BeamNG.Earth tab of BeamMP's
> server browser.

Works with BeamNG.drive 0.40.3.

### New
- **Where to get help, shown where you'll see it**: the app's window, the BeamMP server's console when it starts and
  both READMEs now say plainly that help with BeamNG.Earth, multiplayer included, is on the TGForever Discord
  (discord.gg/tgf), not from the BeamMP team (BeamNG.Earth isn't like a normal mod: problems with it on BeamMP
  are almost certainly ours), and why BeamNG.Earth servers are hidden from players without BeamNG.Earth.
- **Forest sounds**: wind in the trees and a background sound for the landscape, using the stock
  maps' own ambient sounds: louder and fuller in dense, tall forest, quiet in open land. The set follows the real
  climate zone (a Köppen-Geiger climate map): temperate forest as East Coast USA, oceanic as West Coast USA,
  Mediterranean as Italy, deserts and polar as Utah and Johnson Valley, tropics as Jungle Rock Island.
- **River sounds**: rivers and streams have the stock maps' running-water sound, louder as you get closer (as loud
  as East Coast USA's river at the same distance in testing). Canals, lakes and the sea are quiet for now.
- **Tunnel, bridge and parking garage sounds**: inside a tunnel the outside sounds go quiet and the stock maps' tunnel
  echo is used (in short tunnels and under low arches the outside is only muffled), under bridges and overpasses the
  stock under-bridge echo, and in the parking garages you can drive into the stock parking garage echo. Outside sounds
  fade out over the last stretch before a tunnel's mouth (and back in as you leave) instead of cutting off.
- **Shore sounds**: waves along the coasts and large lakes (a sound in the game's files that no stock map uses).
- **Pump jacks at oil wells**: West Coast USA's animated pump jack, with its engine and squeak sounds, at the oil wells
  OpenStreetMap maps (gas wells and closed wells left out).
- **Night lighting** (GitHub #3): after dusk about half the buildings' windows light up (warm, now and then cool),
  offices and shopfronts glow softly, and gas station forecourts and tunnels are lit. Street lamps glow as points of
  light out to as far as buildings are drawn (about 3 miles), and light the streets in view out to about half a mile
  (the 128 nearest, favouring the ones ahead of you), their light reaching across the street more than behind the
  pole, as real street lamps' does. The distant skyline's windows light up too. In testing at night in a city about 0.1 ms
  a frame on a PC, about 1.3 ms on a Steam Deck (about 38 to 36 fps).
- **More streets have street lamps**: town streets without a mapped sidewalk now have lamps too (before, only about a
  quarter of a Baltimore neighbourhood's streets had any), on the utility poles where the lines run overhead, on lamp
  posts where they are buried. Highways in towns and every ramp get tall lamp posts along their outer edge. The
  spacing follows real practice: in North America the cities' and state DOTs' design standards (about 230-250 ft
  apart on main roads, every other utility pole on side streets, closer in dense city cores), elsewhere the street
  lamps OpenStreetMap maps in European cities (about 100 ft apart, mostly on one side). US states differ as they do in
  real life: the Mountain West and Oklahoma light their side streets sparsely, and California, Oregon, Washington and
  Virginia light their freeways only at the interchanges.
- **Fewer utility poles where power lines are underground**: in countries where most low-voltage lines are buried
  (the Netherlands, the UK, Germany, Denmark and others, in proportion) that share of the roads has no poles and wires;
  town streets there get street lamps instead.
- **A Rocks setting** (GitHub #5, after PR #321 by MihneaTeodorStoica): turn the rocks and boulders off.
- **Satellite map on car navigation screens** (GitHub #44, PR #334 by MihneaTeodorStoica): cars with a navigation
  screen show BeamNG.Earth's satellite map there, turning with the car (Settings > Interface to turn it off).
- **High-voltage power lines and substations from the map**: the transmission lines OpenStreetMap maps now have
  lattice towers at their mapped tower spots with the conductors hung between them (underground cables left out).
  Where a line reaches a mapped substation it ends on the gantries of a switchyard laid out as West Coast USA's (bays of
  gantries, busbars on their supports, breakers, transformers and lightning masts), and the rest of the substation is
  filled with more of them, on gravel behind a chain-link fence.
- **Refinery and power plant sounds**: West Coast USA's refinery machinery sounds inside refineries and petrochemical
  plants, and its power plant sound (quieter) at fuel-burning and nuclear power plants.
- **Rain sound**: rain now has a sound (the game's own rain loop), louder in heavier rain, fading in and out with the
  weather, and quiet inside tunnels.
- **Buildings and bridges much farther away**: a light skyline of buildings out to about 3 miles past the far
  buildings (about 1.4 miles before), and bridge decks out to about 3 miles (they used to vanish past half a mile),
  for a small frame cost (about 8 % in the busiest view tested).
- **The route's elevation profile on the map** (PR #379 by SwissCheese26): a small chart in its own box under the GPS
  box, with where you are on it and the route's total climb and descent (smoothed, so a bridge over a valley is no
  spike, and a flat drive draws flat), **Copy coordinates** on the map's right-click menu, a **Dark** base map, and the local time and
  weather next to the map's title.
- **Banked motorway curves**: motorways, expressways and their ramps now lean into their curves (up to 8 %, more on
  tighter bends), easing in and out over about 60 ft.
- **Railroad crossing gates, lights and signs**: level crossings now have crossbucks, flashing lights or gates (raised),
  the crossbuck in the Americas and the St Andrew's cross elsewhere. In the US they match what the Federal Railroad
  Administration's crossing inventory lists for each crossing; elsewhere they follow the road's size.
- **Old railways look old**: abandoned and disused lines are drawn worn, with weeds or grass in the ballast, rusty
  rails and missing sleepers.
- **BeamMP servers no longer have a sharing port**: shared world building (players near each other splitting the work
  of building the world) has been switched off for a while and stays off for now, so its setting (`G.SHARE_PORT`) and
  the TCP port players had to open for it are gone. Each player's app builds their own world, as before.

### Faster world building
- **Much faster first visits near high-voltage power lines**: each piece of road read the ground under every tower
  of every power line touching it, the whole line, often 60 miles of it. One spot in rural Pennsylvania downloaded
  about 6,500 elevation tiles for its first piece of road (5 minutes); now about 290 (30 seconds).
- **Shorter tree hitches**: the trees' work is spread over frames with a time budget, one new forest object a frame,
  and tree tiles arrive as one flat list of numbers (fewer, shorter garbage collections). In the contributor's tests the
  worst tree frame went from 53-87 ms to 15-17 ms (PRs #318 and #329 by Paul-CodeGH).
- The hidden HUD window no longer builds its view every frame (PR #315 by Paul-CodeGH); railway pieces far from a tile
  are skipped before their heights are looked up (PR #341 by teddante).
- **Faster first visits than 1.6.3**: the elevation is looked up without copying its tiles on every lookup, the road
  levelling (where the ground meets the roads) is about a third quicker, and more of the building code (walls,
  footprints, roof outlines) is native code. In testing on fresh places with empty caches: on a PC (Linz, Austria) the
  first roads came in about 26 s after a teleport instead of 31, and all nearby roads in about 33 s instead of 37; on a
  Steam Deck (Brasov, Romania) the first roads in about 55 s instead of 66, all nearby roads in about 78 s instead of
  89, and everything around you, distant land included, in about 105 s instead of 118.

### Fixes
- **Reduced the chance of sidewalks without collision** (driving in town): a sidewalk's collision is held back while the
  car is on it, so it never turns solid around the wheels, and that held back the whole area's sidewalks whenever the
  car came within about 13 ft of any curb there. Now only a car actually over a sidewalk holds them.
- **The status line says what a data source that is not answering affects**: for example "imagery.nationalmap.gov
  unreachable (detailed US aerial photos (NAIP); coarser photos meanwhile)" instead of the address alone. The new
  [SOURCES.md](SOURCES.md) explains every source the game uses, what it is sent, and what you see while it is down.
- **The map's GPS distance follows the game's units** (GitHub #327): km / m with metric units, mi / ft with imperial.
- **Parked cars follow the game's 'parked vehicles' setting** (GitHub #320, #145): they came even with it off.
- **Linux: hybrid-graphics laptops** start the game on the dedicated GPU, a Flatpak Steam's game starts directly,
  and your Steam launch options are used for the direct start (PR #351 by joshuaeroman).
- **Reduced the chance of no roads or buildings when Overture's index server is down** (GitHub #357): the app now
  carries the map data's file index itself (it was unreachable for about an hour on Oct 26).
- **Reduced the chance of 'Address search unavailable' after the first search** (GitHub #350): the status bar's
  town lookups can no longer shut the search out.
- **Roads still under construction are left out** (GitHub #184) in newly visited places.
- **Reduced the chance of the app stopping with WinError 1392** (GitHub #282): a damaged BeamMP launcher folder now
  only skips multiplayer for that start, with a hint.
- **Reduced the chance of a force-feedback wheel locking fully right after opening the map** (GitHub #284): the map
  no longer touches the steering when it opens.
- **Reduced the chance of a railway lying across the road under a rail bridge** (GitHub #311, #345, #364: New Britain,
  the Netherlands, Calgary's Deerfoot Trail): roads now dip under rail bridges as they do under road overpasses
  (Calgary: about 3-6 ft of room before, about 20 ft now), and a line over a short road underpass mapped only on the
  road gets a bridge (PR #370 by doichoiboi).
- **Reduced the chance of falling through the world after a teleport** (found at a tunnel near Grenoble, GitHub #339):
  a car set down over a hole, or falling fast right after a teleport, is put back on the nearest ground.
- **Reduced the chance of 'stac.overturemaps.org unreachable' and other HTTPS errors on Windows** (GitHub #373, PR #371
  by akariiirl): the app checks certificates with Windows' own certificate store, and ships the Overture map index
  itself.
- **Reduced the chance of bare dirt everywhere on a first visit** (the Harz mountains, GitHub #316): ground that was
  built before the land cover data came is painted again once it is there.
- **Runways are paved end to end** (GitHub #337, Tallinn): the runway itself is now painted into the ground, not
  only its markings.
- **Reduced the chance of a dry riverbed where a tributary joins a river** (GitHub #323, Vitebsk): the bigger
  river keeps its water at the confluence.
- **Bridges and tunnels as wide as their road** (GitHub #378, diagnosed by RiDexSS): decks now use OpenStreetMap's
  lane count and width (a 4-lane bridge in Perm was drawn with one lane each way).
- **More traffic ahead of you on motorways** (GitHub #353): traffic cars leaving the drivable area now come back
  ahead of you when you drive faster than 45 mph, not behind (Albany test drive: about 6 cars within 820 ft ahead
  instead of 2).
- **Reduced the chance of a wall at the start of a bridge** (found on test drives on the New York Thruway): a deck
  no longer climbs steeper than 15 % off its end over lower ground.
- **Smoother main roads where the elevation data is coarse** (GitHub #335): primary roads now get the same long,
  gentle profile as motorways, so a main road across flat forest no longer rides every bump of the 30 m data.
- **Reduced the chance of humps on short overpasses** (PR #374 by wyo-backroads: I-80 over Otto Road, Wyoming):
  short motorway decks no longer crest over the road under them, and that road's dip carries on through its
  junctions.
- **Reduced the chance of crashing at freeway exits on bridges** (found on a test drive: US-101's exit to Mission
  Road over the Los Angeles River): an exit ramp leaving an elevated freeway no longer drops below the freeway while
  still beside it, and it runs down on a fill to the street.
- **Reduced the chance of a sudden step on a road dipping under a bridge** (found on a test drive: US-101 at Los
  Angeles' Union Station): every piece of the world now works out the same dip, and the road there no longer
  crosses phantom tracks.
- **Railways that aren't built yet are no longer drawn**: proposed lines and lines under construction were drawn as
  real track across roads.
- **Smoother level crossings**: the rails now lie level with the road where a railway crosses it (they stood about 2 ft
  over the road at its edge), the track easing down to them over the last 100 ft or so.
- **Reduced the chance of a steep hump where a bridge meets a side road's bridge** (found on a test drive, US-24
  near Buena Vista, Colorado: the deck climbed about 33 ft in 65 ft and flipped the car at 52 mph).
- **Reduced the chance of a road bridge lying on the tracks it crosses** (GitHub #324, Vitebsk): road decks and
  their approach ramps now rise over railways as over roads (Vitebsk: about 7 ft under the rails before, up to
  about 17 ft of room now; a track right at a deck's mapped end can still be tight).
- **Reduced the chance of old cached places after an update**: the app's cache of visited places now notices every
  code change in the packaged app (from PR #368 by cody196).
- **Gas stations**: a shop mapped as a house beside its canopy, canopies that aren't neat rectangles, and stations whose
  pin sits on the road (PR #309 by FluffyProgramming).
- **Reduced the chance of overpasses sitting too low with the road under them dug out** (GitHub #306, BR-343 at
  Teresina): 1.6.3 carries a bridge's deck a few feet onto the next road segment, and that piece was not treated as
  part of the bridge, so the overpass lost its approach ramp on that side (about 15 ft too low there).
- **Reduced the chance of grass and gravel on roads (and their grip gone) on fast first visits** (Discord, mountain
  roads in Japan): the ground under a road is now painted again as soon as its map data arrives, instead of staying
  grass until the ground there was built again for another reason.
- **Reduced the chance of gas stations on the wrong side of the road** (GitHub #308, Rio de Janeiro): a station's lot
  mapped as one big building is now its forecourt with a canopy (it was drawn as a block), a roof counts as a canopy
  only when shaped like one (pumps stood on train tracks under a platform roof), and never one across the street.
- **Reduced the chance of steep drops on interchange ramps** (Dallas' High Five): a ramp held up beside its twin
  dropped back to its own height in a few car lengths where the two parted (35 % over 30 ft); it now comes down at
  most at the 6 % a deck climbs, and a raised joint no longer leaves a short ramp beyond it to drop to the ground
  (Birmingham's Spaghetti Junction: 40 % down a 160 ft ramp, now 6 %).

### Which file do I need?
- **`BeamNG.Earth-1.7.0-beta.exe`: Windows players.** The whole app in one file: run it and it installs the mod into
  BeamNG.drive, starts the world server, BeamMP and the game (Windows may warn about an unknown app: More info > Run
  anyway). Steam must be running.
- **`BeamNG.Earth-1.7.0-beta-x86_64.AppImage`: Linux and Steam Deck players** (on the Deck, in Desktop Mode). Make it
  executable (right-click > Properties, or `chmod +x`) and run it.
- **Joining a BeamMP server with BeamNG.Earth?** Run the app above, then in the game: More > BeamMP, the
  **BeamNG.Earth** category.
- **`BeamNG.Earth-BeamMP-Server-1.7.0-beta.zip`: only for people hosting a BeamMP server** (unzip it into the server's
  folder; see its README). Players don't need it.
- **`Source code (zip)` and `Source code (tar.gz)`: for developers.** Added by GitHub to every release; not needed to
  play.

If you want to talk about this mod with me or other people, join my Discord server, TGForever, at
[discord.gg/tgf](https://discord.gg/tgf) and head to the #beamng-earth channel.

## 1.6.3 beta (2026-10-25)

Works with BeamNG.drive 0.40.3.

### New
- **Places you've been load much faster the next time.** What was built around you (roads, buildings, the levelled
  ground, the distant land and its aerial photos) is kept on disk. A teleport back, starting the game where you left
  it, or driving a road you've driven before uses it instead of building it all again. In testing on a 6-core PC,
  coming back to a city loaded all nearby roads in about 7 seconds instead of 27, and the distant land in about 10
  instead of 35-45. To make that work, the map's centre (after a teleport, and when it moves along with you on a long
  drive) now sits on a fixed grid near you, within about half a mile. The kept places use at most 2 GB; the oldest go
  first.
- **Gas stations with refuelling** (FluffyProgramming, PR #220): real stations with pumps, canopies and forecourts in
  the region's stock style, where the stock refuel prompt fills the tank.
- **Railway lines are real 3D track** (mpsteppe, PR #287): a ballast bed with sleepers and rails you can drive on, rail
  bridges on concrete piers, and the ground cut and filled for the line. Trams, and the rails across roads at level
  crossings, stay drawn in the road.
- **Traffic lights only where the map has them** (mpsteppe, PR #288): no more lights at every town junction of two main
  roads; mapped lights at country junctions too.
- **Sidewalks follow OpenStreetMap's sidewalk tag** where a road has one (mpsteppe, PR #292).
- **GPS routes download ahead** (SwissCheese26, PR #275): the first 12 miles of a planned route's roads, buildings and
  elevation download at low priority, with a progress bar on the map.
- **The map's place names stay visible when zoomed in** past street level (PR #275).
- **Address search and the status bar's town names in the local language** in German-, French- and Italian-speaking
  countries (from PR #275's German names).
- **Bridges span the road under them** with its shoulders and the slopes up to the road above, so the road under runs
  through at its full width instead of between wedges of ground (the Baltimore-Washington Parkway over a local road).
- **Simple buildings first after a teleport:** the town around the car shows as simple buildings within seconds, and
  each street's detailed buildings, trees and props replace them as its roads arrive.
- **"Where you left off" saves your place as you drive** (every minute at most, at teleports, and when the game
  closes), so a restart, or a crash, puts you back where you were.
- **South Asian towns look more like themselves** (OmdevShastri, PR #146): plastered flat-roofed buildings in India,
  Pakistan, Bangladesh, Sri Lanka and Nepal, and local road surface names.
- **European speed limit signs** (cristivasile, PR #195): round km/h signs from 30 to 150 and Germany's end-of-limit
  sign, where limits are posted in km/h.
- **Roads mapped as part of a route relation get their real lanes, width and surface** (Misnik1337, PR #213; GitHub
  #261).
- **Earlier pull requests now in a release:** railway and tram tracks on the ground (Misnik1337, PR #214), divided
  roads' bridges joined across the median (veilednet, PR #207), 20 ft of headroom under overpasses where the ground
  allows (amccoy-crypto, PR #95), and no more curb bulb-outs at every corner (focalfury, PR #216; GitHub #208, #253).
  Thank you all!

The map's road data downloads again once after the update.

### Faster world building
- **Roads and buildings appear sooner on a first visit** (GitHub #241): elevation tiles that don't exist are no longer
  asked for, and on computers with more than 8 CPU threads a place's map data downloads all at once instead of one
  piece after another. In testing on a 16-thread PC, the first roads came 3 to 14 seconds sooner.
- **Less work to build the roads, buildings and levelled ground of a new place.** Much of the road and grading code
  was made faster (a good part of it rewritten as native code), and the server's processes now share what they work
  out about each map area instead of each doing it again. In testing, a busy city's nearby roads took about a third
  less computer time than this morning's build (Baltimore's Penn Station: 24 to 16 seconds of work for 16 road
  pieces), and distant land about half the computer time of the last version.
- **Short teleports are quick too:** a teleport less than about 12 miles away (across town, a saved place nearby) now
  places the car as fast as a long one. In testing in Manhattan the car was placed after about 2 seconds instead of
  26, and the first roads came after 12 seconds instead of 39.
- **Roads, sidewalks and building collision keep up better right after a teleport** on PCs with 12 or more CPU threads
  and 24 GB of memory or more: the world server now builds 4 road pieces at once there (3 with 8 or more threads,
  still 2 on smaller machines). Driving off at 50 mph right after a teleport into Manhattan, the road under the car was
  there after about 6 seconds instead of 15, at the same frame rate.
- **Reduced the chance of roads not loading at all after a few teleports on PCs with few cores** (found on a 6-core
  Windows PC): building the traffic's road network for a new place could keep the world server busy for minutes, and
  the roads, sidewalks and buildings around the car waited behind it.

### Multiplayer
- **BeamNG.Earth servers are a category of BeamMP's own server browser** (search, filters, favorites, details), in
  their own colour, with the official server at the top and each server's country flag.
- **Server owners can set the time and weather for everyone** (the plugin's settings): the real time and weather of
  the place (as before), a fixed or running time of day, and clear, cloudy, rain, storm, snow or fog weather.
- **The app finds a BeamMP launcher you built or put somewhere yourself** more often (a `BeamMP-Launcher` folder in your
  home folder or one folder down, or `~/.local/share`). The install question has a new answer, **Have it already**,
  which opens your system's file window to pick your launcher (or you type its path) and remembers it
  (`--beammp-launcher PATH` does the same). The settings page's **BeamMP launcher** changes it later.
- **No more spawning high above the ground after joining:** the free camera kept its old height next to the other
  players, and a car spawned from it dropped from there.
- **Reduced the chance of another player's car going unstable** (and BeamMP stopping it with an error) when it comes
  back into your world after its player teleported away and back.
- **Fixed an error in other players' cars' damage** when the damage arrived just as their car came back.
- **No more name tags floating on an empty road** where another player's car is hidden.

### Fixes
- **Reduced the chance of ramps and dips where roads meet** (GitHub #266, BR-381 in Brazil): the pieces of a road are
  now smoothed together through a junction, instead of each on its own (they met up to about 7 ft apart on slopes).
- **Reduced the chance of an overpass sitting right on top of the highway under it:** when an overpass can't be raised
  enough on its ramps, the road under it now dips under the bridge, as real roads in a cutting do.
- **Reduced the chance of buildings floating beside roads in cuttings** (idea from hashk014's PR #69): buildings next
  to the road stand on the same shaped ground as the road.
- **Reduced the chance of humps in roads that pass under a bridge** (GitHub #271).
- **Autodrive: reduced the chance of the car stopping and turning around on winding roads** (GitHub #269).
- **Fewer footpaths, bins and hydrants along highways in the middle of nowhere** (GitHub #267, the Nullarbor).
- **Reduced the chance of the car not responding to any controls after closing the map** (GitHub #252).
- **Reduced the chance of a crash on BeamNG 0.40.3 when the window is minimized while driving** (Misnik1337, PR #213):
  the world no longer moves its ground while the game window is minimized.
- **Reduced the chance of freezes of several seconds that keep coming back after using walking mode** (GitHub #240).
- **Reduced the chance of AI traffic, parked cars, traffic lights and far-away cars floating over the road** far from
  where you started: their positions ignored the Earth's curve (up to about 60 ft near the rebase distance). Thanks to
  KyzoFox (PR #112) for finding it.
- **Reduced the chance of truck-shaped "buildings" standing on motorway lanes** (GitHub #239, #246).
- **Reduced the chance of bridge walls standing across a road** where a bridge ends at a junction (GitHub #258).
- **Reduced the chance of boxes on steep bare mountainsides** (GitHub #242, Mount Fuji).
- **Fixed black strips and blocks on the ground in New York City** (GitHub #251).
- **Reduced the chance of the world never loading when its first piece of ground fails** (a damaged download, GitHub
  #286): it is tried again by itself.
- **The minimap's and the navigation app's distance to your destination counts the whole route** (a long trip showed
  only the next 1-2 miles).
- **If the ground under you takes very long to build, the server log now says what it is waiting for** (GitHub #263).
- **Linux: the setting "Start BeamMP with BeamNG.Earth" is now followed** (the app always started BeamMP).
- **Reduced the chance of map data being downloaded again on Windows** when another part of the app was saving the
  same file at that moment.

### Other
- **The map download cache can go on another drive** (GitHub #254): settings page > Download cache > **Cache folder**
  opens your system's folder window (or type or paste the folder), used from the next start. `--cache-dir PATH` when
  starting the app does the same.
- **BeamNG.drive installed outside Steam's libraries** (another launcher, a folder of its own): the app asks where it is
  (your system's folder window, or type the path) and remembers it; `--game-dir PATH` does the same.

### Hosting a BeamMP server
- Update the server zip with this release: it carries the mod your server sends to players. Which list your server is
  on is set in `ServerConfig.toml` as before (`Private = false` with your AuthKey: the BeamNG.Earth category; see the
  server zip's README).

### Which file do I need?
- **`BeamNG.Earth-1.6.3-beta.exe`: Windows players.** The whole app in one file: run it and it installs the mod into
  BeamNG.drive, starts the world server, BeamMP and the game (Windows may warn about an unknown app: More info > Run
  anyway). Steam must be running.
- **`BeamNG.Earth-1.6.3-beta-x86_64.AppImage`: Linux and Steam Deck players** (on the Deck, in Desktop Mode). Make it
  executable (right-click > Properties, or `chmod +x`) and run it.
- **Joining a BeamMP server with BeamNG.Earth?** Run the app above, then in the game: More > BeamMP, the
  **BeamNG.Earth** category.
- **`BeamNG.Earth-BeamMP-Server-1.6.3-beta.zip`: only for people hosting a BeamMP server** (unzip it into the server's
  folder; see its README). Players don't need it.
- **`Source code (zip)` and `Source code (tar.gz)`: for developers.** Added by GitHub to every release; not needed to
  play.

If you want to talk about this mod with me or other people, join my Discord server at
[discord.gg/tgf](https://discord.gg/tgf) and head to the #beamng-earth channel.

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
