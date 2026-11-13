# BeamNG.Earth

A 1:1 scale, streamed Earth for [BeamNG.drive](https://www.beamng.com/): drive anywhere on the
planet on real elevation data and satellite imagery, with distance-based level of detail and the
real curvature of the Earth.

Status: beta (see VERSION). Tested on BeamNG.drive 0.40.4 on Linux (Vulkan) and 0.40.3 on the Steam Deck. See
[docs/ROADMAP.md](docs/ROADMAP.md) for what is coming.

**Community:** join the TGForever Discord, **[discord.gg/tgf](https://discord.gg/tgf)**, and head to the
#beamng-earth channel to chat about the mod, get help, report bugs, help with its development, and get an IP to join a
BeamMP.Earth server (see [Multiplayer](#multiplayer-beammp)).

> **Help with multiplayer comes from the TGForever Discord too.** BeamNG.Earth isn't like a normal BeamNG mod (it
> runs its own app next to the game), so if something isn't working with BeamMP, it's almost certainly a BeamNG.Earth
> problem, not a BeamMP one. Please **don't contact the BeamMP team or open BeamMP support tickets** about it.

You can help support development by reporting [issues](https://github.com/TecnaGamer/BeamNG.Earth/issues), contributing code and sending pull requests, or [donating](https://ko-fi.com/tecnagamer). Thank You!

## Features (working today)
**The world**
- The whole planet at 1:1 scale on real elevation: national lidar where it exists (Mapterhorn: much of
  Europe, the US and more; in Canada the distant terrain lined up with NRCan's HRDEM lidar ground),
  Copernicus 30 m elsewhere with buildings and small tree stands filtered out of it.
- Aerial and satellite imagery on the ground: open state and national orthophotos (Maryland's 15 cm photos,
  a dozen more US states, NAIP across the US, Switzerland, France, Portugal, Spain, the Netherlands, Germany's states,
  Austria, Poland, Czechia, Japan, parts of Australia and Canada, and more), Sentinel-2 cloudless everywhere else; shadows and
  colour seams taken out of the photos, the season's colours graded in, cliffs drawn as rock.
- A real view distance out to the horizon, with the Earth's curvature.
- Both poles: Antarctica's ice sheet up to the South Pole (2,835 m) from the Copernicus DEM, the Arctic's sea ice
  at the North Pole, the polar day and night of the real sky, and a minimap compass that points to true north.
- Opt-in (`BNE_PHOTO_SURFACE=1`): an untagged track, service or unclassified road is asphalt, grass or gravel by the
  colour of the regional photo under it (a lane as green as its verges is a grass lane, a pale neutral one gravel).
- Ground materials from land cover (grass, dirt, rock, sand, snow, mud, lawns, paved squares and parking
  lots) with 3D grass and bushes. Squares and pedestrian areas are painted into the ground with their own textures by
  their mapped surface and paving tags (grey stone slabs, red herringbone brick, cobbles; in the US red brick only
  where the aerial photo shows it), and cobbled streets show real setts. These stock textures are converted from the
  player's own game files when the server starts.
- Roads, parking lots and gas station forecourts are painted in the aerial photo's own pavement colour (old asphalt,
  concrete and patches keep their shade; parked cars and old markings are left out), and the asphalt of rounded street
  corners is painted to the curb.
- Trees and forests from a canopy height model, regional species (conifers, broadleaf, palms, desert
  shrubs, Mediterranean oaks and pines, Australia's eucalypts, riverside poplars in Europe, creosote bushes in North
  America's hot deserts), autumn colours in season. Far forests are drawn as the stock maps' tree clumps (fewer
  objects, lighter on the frame time); the lower tree settings bring the clumps nearer.
- Orchards and vineyards in rows along their fields, and round bales on a share of the fields after the harvest
  (straw on farmland, wrapped silage on meadows, in their season for each hemisphere).
- Forest and river sounds: the stock maps' wind in the trees (louder and fuller in dense, tall forest) and background
  sound, the set by climate (Köppen-Geiger zones: temperate, oceanic, Mediterranean, desert, tropical), running water along
  rivers and streams, waves along coasts and large lakes, the stock tunnel, under-bridge and parking garage echo, refinery
  machinery and power plants, and rain you can hear.
- Pump jacks at oil wells (OpenStreetMap), animated, with their sounds.
- Wind turbines at their mapped OpenStreetMap positions worldwide, with animated blades and distant streaming.
- High-voltage power lines along their mapped routes (OpenStreetMap), with towers, conductors and fenced substations
  (on wooden poles where the line's voltage is low or its mapped supports are poles).
- The satellite map with the real roads drawn over it on cars' own navigation screens.
- Night lighting: lit windows and street lamps after dusk, out to as far as buildings are drawn (about 3 miles),
  spaced as real street lamps are (North American design standards there, OpenStreetMap's mapped lamps elsewhere),
  and lit highways in towns and their ramps. Street lamps mapped in OpenStreetMap stand where they are mapped, as the
  post their mount names (high masts, straight masts, double arms), their arms toward the road
  (`BNE_MAPPED_LAMPS=off` leaves them out).
- Buildings from Overture / OpenStreetMap with regional styles (US siding and brick, Alpine chalets, Nordic
  wood, Mediterranean stucco, East Asian and Latin American looks), real heights, roofs, windows, shopfronts,
  porches, churches with steeples, glass towers. Roof shapes mapped on building parts are kept and sloped along their
  mapped direction (tower crowns, One World Trade Center's tapering corners), also on the far skyline. Buildings show
  out to about 3 miles: simpler versions past 1 mile, and a light skyline beyond that; bridges show out to about 3
  miles too.
- Roads painted into the ground itself (no floating road surface), with lane markings, sidewalks and curbs, curb ramps, storm drains, cobblestone streets,
  traffic islands, roundabouts (a granite truck apron and a planted, raised island, curbs round the ring into the entries,
  give-way lines at every entry, splitter islands where the map has a refuge), rural motorways with shoulders and toll booths; the ground graded under them, with
  retaining walls where a road is cut into a hill (dry stone in the mountains and towns, concrete along major roads)
  and stone parapets along mountain roads' drops; switchback mountain pass roads (the Stelvio) narrow, between their
  walls and parapets; metal guard rails, concrete and cable barriers where OpenStreetMap maps them; median
  barriers down divided highways (concrete walls, cable barriers or steel rails by the median's width and the
  country's practice, open at crossovers and ramps); street lights, utility poles with wires, traffic signals,
  stop signs, traffic lights and sidewalks where OpenStreetMap maps them (where traffic drives on the left, the
  signal poles stand on the left kerb with their arm over the road and the stop lines across the incoming lanes);
  railway lines as 3D track (ballast, sleepers and rails, rail bridges on piers, the ground cut and filled for the
  line; abandoned and disused lines overgrown and rusty; lines not built yet left out), level crossings with the rails
  flush in the road and crossbucks, flashing lights or gates (in the US as the FRA's crossing inventory lists them),
  and tram tracks (street track where trams run on the road). Opt-in (set `BNE_RAIL_XING=1`): a level crossing as
  one place (a barrier kit per approach, a rubber deck between the rails), crushed-stone ballast on tracks in use,
  parallel tracks sharing one bed, and buildings, lamps and poles kept off the railway's bed. Opt-in (`BNE_CATENARY=1`): electrified lines
  (OpenStreetMap `electrified`) get catenary masts, cantilevers and contact wire, concrete or steel by country.
  A track's bed comes from one profile per line, the same in every tile and grading cell (no steps at tile seams),
  smoothed along the line with its bends rounded; switches and crossovers meet the lines they join; a road crossing a
  track at grade lies flush with it; a road under a short rail bridge dips to clear it (to the height limit its sign
  gives, `maxheight`, when the map has one), and the bridges of a long rail line stand where the map puts them.
  Railway stations are built from the map: platforms at the country's height
  beside the rail head, roofs on columns, benches, bins and lamps, footbridges, walkable underpasses, boarded track
  crossings and name boards (`BNE_RAILSTATIONS=off` turns them off). The catenary runs on through way joints and
  switches, with portals over three tracks or more, droppers and per-class wire materials. Opt-in: a physics
  collider at the rails for riding them (`BNE_RAIL_COLLIDE=on`) and rail tunnels as tubes (`BNE_RAIL_TUNNELS=on`).
- Gas stations with pumps, canopies and forecourts in the region's style, where the stock refuel prompt fills the
  tank.
- Golf courses: mapped greens, fairways, rough and bunkers painted into the ground, and a flag at each mapped hole
  (from AnthonyGraceffa's contribution).
- Worn dirt and gravel roads: potholes, broken patches, the odd deep pit and a lean to one side on minor dirt roads,
  rougher forest tracks, smoother main gravel roads (from RiDexSS's contribution).
- Street trees, fire hydrants, benches and bins along sidewalks where the street is really built up (its buildings,
  shops and land use; from wyo-backroads' contribution).
- Highway signs on US freeways (from cody196's contribution): green exit signs at 1 mile, 1/2 mile and 1/4 mile and
  in the gore, overhead destination signs, Interstate / US / state route shields in each state's style, yellow merge
  warnings at entrance ramps and Interstate mile markers, from the destinations and milestones mapped in
  OpenStreetMap (rebuilt weekly). Settings > World > Highway signs turns them off.
- US state welcome and recreation boards (from wyo-backroads' contribution): a "Welcome to ..." board where an
  Interstate, US or numbered state route crosses into a state (the state's sign as photographed where a photo with
  clear reuse terms exists, else a lettered blue board), brown park guide boards before exits posted to a state or
  national park, and entrance boards where a road enters a named park, campground, national forest or grassland.
  `BNE_WELCOME_SIGNS=0` / `BNE_RECREATION_SIGNS=0` turn them off.
- Autobahn signs in Germany: the German chain at every exit, Kreuz and Dreieck, with the sign text as mapped in
  OpenStreetMap (rebuilt weekly). The 2000 / 1000 m announcement board with the junction's number and name, the 1000 /
  500 m advance boards with the destinations over their lanes, the A, B and E plates and pictograms, the 300 / 200 /
  100 m countdown boards, the direction sign at the exit and "Ausfahrt" in the gore. Overhead on carriageways with
  more than two lanes and at every Kreuz and Dreieck (gantries and cantilevers), beside the road on two lanes; boards
  move closer where junctions follow closely. Settings > World > Highway signs switches them too (the environment
  variable BNE_AUTOBAHN_SIGNS=off turns them off alone).
- Speed-limit signs everywhere, showing the limit the traffic drives (the mapped one, else the road class's default):
  where the limit changes, past major junctions and motorway entrances, and as reminders on long roads; the US
  "SPEED LIMIT" panel, Canada's "MAXIMUM" panel, and elsewhere the round red-ring sign (mph in the UK). The Highway
  signs setting switches them too.
- Road signs (off unless the server has `BNE_ROADSIGNS=on`; Settings > World > Road signs): no-entry signs where a one-way road ends, dead-end signs, pedestrian-crossing signs at marked crossings without signals, sharp-bend and double-bend warnings, roundabout signs, signal warnings, motorway and living-street signs, one-way signs, narrowing and side-road warnings, chevron boards, give way and stop, tunnel, bus stop, car park, pedestrian zone and cycle path signs, drawn from vectors on one atlas per sign system (Czechia, Germany, Poland, Ukraine so far); a sign stands only where the road data implies it.
  signs setting switches them too. In Germany, with the town signs on, the general limits are not posted, as there:
  no 50 at a town's entry sign (the sign itself sets the 50) or in town, no 100 outside towns, except where a lower
  limit ends.
- Town entry and exit signs (off by default for now; Settings > World > Town signs; from fuxau's contribution): each
  country's own sign where a road enters a town's built-up land, the exit sign on its back. In Germany the yellow
  Ortstafel (Zeichen 310 / 311) in DIN lettering, with the village, its Gemeinde ("Gemeinde Barbing", "Stadt
  Schrobenhausen", "Markt Donaustauf") and its Landkreis, the next place and its distance on the back, standing where
  OpenStreetMap has the real one mapped (rebuilt weekly), elsewhere where the land cover's built-up land begins.
  Where a town boundary runs through a fork, one pair stands on the road all its branches share; between two towns
  grown together only the through roads carry them.
- Direction signs (on by default; Settings > World > Direction signs): a blue, green
  or yellow advance board beside the road before junctions of major roads (not on German Autobahnen, which have the
  Autobahn signs above), with each way's road numbers (national
  and European), the places it leads to, and their distances; Ukraine, Czechia, Poland and Germany for now. Cities
  and towns are written in the local language with their English name under it, villages in the local language
  only. The boards are drawn from the road network and Overture's places and route numbers, so they are a good
  guess of a real board, not a copy: the distances are the straight ones scaled to a road's, and the colours and
  plates follow each country's practice as best known.
  In a town, a board stands only at an interchange, a way out or a fork of numbered routes: a small board with the local
  name, hung on a lamp or utility pole that stands there, else on a stand of its own.
- Opt-in (`BNE_REGIONAL_POLES=poles`): each European country's own utility poles, and steel or concrete street lamps
  with a bracket arm instead of the stock American post.
- German junction signs (off by default for now; Settings > World > German junction signs): Germany's right of way as its
  signs show it: Zeichen 306 / 307 along priority roads, 301 at single junctions, 205 where a road gives way and the
  map's 206 stops, with the 1002 plate where the priority road bends; no signs at right before left in residential
  streets, and 274.1 / 274.2 where a mapped 30 zone begins and ends. The Wartelinie at a 205, the Haltlinie at a stop
  and zebras only where mapped replace the US stop signs and crosswalks, and traffic gives way and stops by the same
  decision. Germany is found by its real border. The signs are worked out from the map (road classes and numbers,
  mapped priority roads, zones and stop and give-way points), so they are a good guess, not a copy of the real ones.
- Bridges drawn as their real type (beam, truss, arch, cable-stayed, suspension) with real heights and spans, piers
  that keep clear of the roads under them and 6 m (20 ft) of headroom over roads where the ground allows; a divided
  road's two decks joined across a narrow median, with mapped levels retained through multilevel interchanges;
  tunnels through hills, with portals, lights and low arches where little is over them; a tunnel mapped in several
  pieces (or as a network with junctions and roundabouts inside the mountain) kept at one level inside the hill, and
  city tunnels under level streets dug down under them, their portals where the ramps reach that depth
  (`BNE_TUNNEL_CHAINS=off`, `BNE_TUNNEL_BURY=off` for the old behaviour).
- Water: the sea with real depth (cars sink), lakes, and rivers kept beside the roads.
- Airports with graded, marked runways; racetracks (circuits, ovals and pit lanes, with their real banking);
  landmarks such as the pyramids of Giza.

**Time, weather and physics**
- **Real time**: the sun, moon and stars are where they really are for the place, the date and the clock
  (the real time zone); the clock runs in real time. Change the time in the pause menu and the mod hands it
  over to you (switch back to Real any time).
- **Real weather** at the player from Open-Meteo, updated as you travel: cloud cover and high cirrus, clouds
  drifting with the wind aloft, fog from the real visibility, ground wind with gusts that pushes the cars,
  rain and snow, the air temperature, and the sun dimming under overcast. It goes through the game's own
  weather system, so the Time & Weather menu shows the real values; a manual mode leaves it to you.
- **Seasons**: the photos' colours and the trees' leaves follow the time of year (autumn colours, bare
  trees in winter where the photos were taken leaf-off).
- **Real gravity**: the WGS84 gravity for your latitude and altitude plus the EGM2008 local anomalies, so
  gravity really is weaker on a mountain or at the equator.

**Driving and traffic**
- Drive anywhere: the physics ground follows the car at 100+ mph, the world origin moves with you
  (floating origin), and the world streams ahead of you; you never fall out of the world.
- The ground around the car is a 1 km terrain block at 1 m (3 ft) resolution, the stock maps' own detail, so road
  edges, ditches and the ground at walls follow the map closely (the Low and Medium presets use a lighter 2 m grid).
- Buildings, sidewalks, curbs and bridge decks are solid (lean collision meshes near the car keep the
  physics' collision rebuilds cheap).
- Real bridge and tunnel heights where open data measures them: in the US, bridge decks follow the USGS 3DEP lidar
  measured along each bridge, and so do the roads leading onto them up to where they are back on the ground
  (`BNE_APPROACH_LIDAR=off` turns that part off); in France (IGN BD TOPO) and Norway (NVDB), bridges and tunnels follow the national road
  databases' heights, and so do roads where the terrain data is coarse. Elsewhere, or where a source has nothing,
  the heights are estimated as before. The environment variables `BNE_ROADZ=off` (all of it) and
  `BNE_LIDAR_DECKS=off` (the US lidar only) turn it off.
- Measured buildings in France: each building's height to the eaves and roof rise, storeys, wall and roof materials
  (stone, brick, render; tiles, slate, zinc) and use (houses, shops, sheds, churches) come from IGN BD TOPO and are
  joined to the map's outlines, where the map itself does not say (Aix-en-Provence's old town: 4 000 of 4 800
  buildings measured, tiled pitched roofs instead of predicted flat blocks). Downloaded per map cell in the background
  beside the map data, about 0.1-0.6 MB once; `BNE_BDTOPO=off` turns it off.
- Measured massing in New York City: the setbacks, wedding-cake towers and crowns of the city's 3-D Building Model
  (NYC Open Data, 2014) as building parts on the map's outlines, downloaded per map cell from BeamNG.Earth's data site
  (`BNE_NYC_MASSING=off` turns it off). Rooftop water tanks on the city's mid-rise roofs use the stock water-tower model.
- Rooftops everywhere: a mechanical penthouse on towers, a stair / lift bulkhead on mid-rise blocks, and rooftop
  air-conditioning units spread by roof size on shops, offices and warehouses, each kept inside the roof's outline.
- Measured roofs from national LoD2 building models: each building's roof shape (flat, gabled, hipped, half-hipped,
  mansard, pyramidal, ...), height and roof rise fill what the map does not say, in Germany (the states' models) and,
  once their data is published, Switzerland and Liechtenstein (swissBUILDINGS3D), Estonia and Vienna. Where a model
  has no roof type, the shape is read from its roof faces when the data is built. A small file per ~25 km, fetched
  once per area; `BNE_LOD2_ROOFS=off` turns it off.
  `BNE_GRADE_GUARD=on` (off by default) adds a last pass over a cell's road plans: roads that meet at a node get one height
  and ease to it smoothly at a grade fit for their class, and the grades the earlier passes made stay under a ceiling per class.
- Smooth bends (on by default; `BNE_ROAD_SMOOTH=off` draws the roads as mapped): the map draws a bend as a few
  straight pieces, so roads had a visible corner at every vertex, and a banked racetrack's chords made a series of
  bumps. Each bend of a drivable road and racetrack is an arc tangent to both pieces (a motorway's gentle, a lane's
  tight; a bend of short pieces becomes one round curve, moving the line by under 0.75 m), joined by transition
  spirals so the curvature has no steps (`BNE_ROAD_G2=off`: the arcs alone). The road's ends and the points where
  another road joins it stay where the map has them. The road mesh, the ground's grading, the markings and the AI's
  routes all follow the same line. Driveways and parking aisles keep their mapped corners; railways are rounded only
  with `BNE_RAIL_CURVES=on`.
- Lane changes inside a highway stretch (from PR #880 by DerEifrige-f; `BNE_LANE_SECTIONS=off` turns it off): where
  the map's ways between two junctions hold different lane counts (an auxiliary lane opening before an exit, a lane
  dropping after an entrance), the road gets each part's own lane count and width. Where OpenStreetMap places the
  added lane beside the through lanes, the through lanes run straight and only the added lane's edge tapers; its lane
  line is a wide, short-dashed line, and the slip road leaves from that lane.
- Street widths measured against aerial photos: where the map gives a road no width, its default width comes from
  measurements of each road's visible paved width on high-resolution aerials in US towns and suburbs, European cities
  and Japan (narrower small-town streets in the US, one-way town streets as wide as their parking), no parking lanes
  are added on top of a mapped lane count, and winding rural roads (touge, alpine roads) are narrow
  (`BNE_WIDTH_MODEL=off` / `BNE_WINDING_ROADS=off`: the old widths).
- Fast teleports: picking a destination on the world map (its "Teleport here?" box or a search result) starts
  downloading the place's terrain and map data before you confirm. Right after a teleport, while you wait, the world server may borrow some of the game's
  CPU cores (at idle priority) to build the world faster (Linux; `--load-boost off` turns it off). It builds
  2 to 4 road pieces at once, by your computer's CPU threads and memory. While you drive and the world falls
  behind, it borrows a quarter of the game's cores until it has caught up (`BNGE_CATCHUP_LEND=0` turns that off,
  `0.5` borrows half).
- New places' roads, buildings and places come from Overture's own map tiles: one small download per area instead
  of reading the large map data files, a few seconds sooner on a first visit (the same data; road lines within about
  a quarter of a metre of the files'). Water, land use and the other map layers still come from the files, and the
  files stand in whenever the tiles do not answer (`BNGE_MAP_SOURCE=parquet` uses the files for everything, as before).
- A stand-in world while a new place loads: after a teleport to a place you have not visited, the car is set down on
  a first version of the ground before the roads are fully built. Its roads are laid flat into it from the map data
  as soon as that data is downloaded, and are built again in full detail a little later. Where the aerial photo is
  not downloaded yet, the ground is coloured by its land cover until the photo arrives. The environment variables
  `BNGE_L0_ROADS=0` (no flat roads in the first version), `BNGE_L0_REFRESH=0` (no rebuild when the map data
  arrives) and `BNGE_L0_GROUND=0` (wait for the photo as before) turn the parts off.
- Places you've been load much faster the next time: what was built around you (roads, buildings, the levelled
  ground, the distant land) is kept on disk, up to 2 GB (the oldest go first; the environment variable
  `BNE_REVISIT_MB` sets the size, `BNE_REVISIT=off` turns it off). It is kept across app updates unless the
  update changes how that part of the world is built.
- **AI support**: the mod builds BeamNG's road graph (the AI's map) from the real roads around you, with
  lanes, one-way streets, speed limits (mapped, or the country's defaults), the country's driving side and
  road rules, and traffic signals at real junctions. The stock AI, traffic, police and the AI modes in the
  vehicle menu work on it.
- **Traffic**: stock BeamNG traffic and police can be switched on, and parked cars line the streets and fill
  parking lots (stock BeamNG parking). On highways the traffic's road map reaches past the loaded ground, so cars
  ahead no longer see the road end and turn round, and a car put back near you joins at the road's speed instead of
  from a standstill (from ProxyFirewall202's contribution, PR #728). On highways of two or more lanes each way, traffic
  held behind a slower vehicle (a truck) moves over and passes it when the next lane is clear, then keeps right again
  (the game's AI does not overtake by itself; the map's Autodrive does not pass yet). On divided highways every other
  car put back near you comes on the other carriageway, towards you, so there is traffic both ways. The number of
  traffic cars is the game's own setting (Options > Gameplay > Traffic > Traffic amount; 0, automatic, is at most 10):
  a fast CPU can take more.
- Distant headlights and taillights move along the mapped roads at night while traffic is on, with density
  following the local time. Returning to a recently viewed road keeps established lights bright.

**Multiplayer (BeamMP, in testing)**
- Play the real world together on a BeamMP server (every player runs the BeamNG.Earth app, which starts BeamMP with
  the game): BeamNG.Earth servers are their own **BeamNG.Earth** category in BeamMP's server browser, players who join are
  sent to the others, cars show in the right place and with their crash damage in everyone's world, and other players
  show on the world map (see "Multiplayer" below).

**Map and navigation**
- A level select with famous places and a **Random location** button (it starts you on a road).
- World map (**M**): pan and zoom the globe, satellite or street map (EOX) with a roads & labels overlay,
  borders and place names (the **Layers** button), search for landmarks and places as you type and streets and
  addresses on Enter, in any language, or ask for "highest bridge in Switzerland", teleport anywhere. Works with a controller,
  laid out like the stock map. Layers > **Highways** (off by default) draws the US Interstates and US routes with their
  shields while zoomed out, so you can see which highways a GPS route takes (closer in, Roads & labels shows every road).
- **3D map** (experimental): a Google Earth style view in the game itself. Fly up from your car to see the whole
  globe with its photos, search for places, pick a spot with a click, set a GPS route or teleport there; its compass
  needle turns with the view. The map key opens the 2D or 3D map, whichever you used last, and each has a button for
  the other. Any camera more than about 150 miles up sees the Earth as a globe too.
- **Altitude** HUD app (hidden by default; add it from the apps menu, under BeamNG.Earth): height above sea level and
  above the ground, and how fast you climb or fall.
- GPS routes to any destination (OSRM on OpenStreetMap), with the stock route arrows on the road and the route
  on the game's minimap (bottom left), which shows the roads around you. The first 20 km (12 miles) of the
  route's map data and elevation download ahead while you drive, with a progress bar on the map (Settings >
  Storage > Download along the GPS route: off, the next 12 miles, or the whole route up to 300 miles).
  **Pre-cache route** on the GPS box keeps the whole route's data (a quarter mile to 2 miles each side, aerial photos
  included) like a pre-cached area below.
  The map shows the route's elevation profile under the GPS box and the local time and weather, has a Dark base
  map, and its right-click menu copies a spot's coordinates.
- A **Cache** overlay on the map (Layers > Cache, or LB / RB on a controller) shows what is downloaded on your
  computer and at what detail: a summary (some data / the far view / ready to drive), or the elevation, imagery or
  map data alone, or **World built** (the terrain, far view, roads and buildings already generated and kept).
- **Pre-cache an area**: the map's **Pre-cache area** button (or Shift + drag; on a controller A, A, then X) picks a box
  of up to 965 square miles; the confirm box shows its size, the download, the disk space and the time it takes
  (some hosts' daily limits stretch big areas over days). Its map data, elevation and imagery then download in the
  background, slower while you drive and never while the world around you loads, and carry on after a restart.
  Pre-cached areas (Places panel) show their progress, can be paused or deleted, and their data is kept when the cache
  is full or cleared. Tick **Build the world too** (areas and routes up to 155 square miles) to have the roads,
  sidewalks, bridges, buildings and street furniture built ahead as well, so they load at once when you arrive;
  otherwise the world itself is built when you get there. Tick **Fast** (or press **Fast** on a pre-cached area in the
  Places panel) to have it done in minutes instead: the game pauses and the download and building get every CPU core
  until it is done (each map host's own limits still apply); the game goes on by itself when it ends, or press
  **Stop fast**.
- In multiplayer the map has a **Players** list: follow a player, show everyone, or set the GPS to them. Teleport
  works without a car too (the free camera goes there).
- **Autodrive**: the car drives itself along the GPS route (in lane, legal speed, minding traffic).
- Saved places and recent places; "Where you left off" in the level select puts you back where you last drove (it
  is saved as you drive, at teleports and when the game closes).

**Interface and tools**
- A slim status bar (street, town, country, and under it the loading progress of the ground, roads & buildings, trees and
  distant land after a teleport; the names come from the map data the app downloads, your position is not sent to a
  lookup service), the Earth coordinates above the minimap, a connection banner when something is
  wrong and the data credits on screen, as BeamNG UI apps in a ".Earth" app layout (based on Freeroam's;
  move or resize them in the UI apps editor), and a one-time welcome page about the project.
- Settings in the pause menu, keybinds, in-game reports (notes and performance recordings to share), and a
  wireframe view (Ctrl+Alt+W) that shows how the ground, the distant terrain and the roads are built.
- A diagnostic log to send when something is slow: the world server's `logs/server.log` (in
  `%LOCALAPPDATA%\beamng-earth` on Windows, `~/.cache/beamng-earth` on Linux) records your hardware and settings, every
  hitch and frame-rate drop with where it happened and what was loading, and a summary every minute
  ([docs/QUICKSTART.md](docs/QUICKSTART.md#something-wrong-tell-us)).
- Easy setup: one download per system (Windows .exe, Linux AppImage) that installs the mod and starts the server
  and the game; from the source, `install.py` installs everything and `start.py` keeps the mod in sync.
- Runs on a Steam Deck (aim: 30 fps on the Handheld graphics preset).
- Computers with little memory: BeamNG.drive itself asks for 16 GB. On an 8 GB computer the app says so at start,
  the world server runs in fewer processes with smaller caches and a coarser distant land, and the first start picks
  the Low preset; expect some stutter while the world loads. With about 4 GB the game alone does not fit: the app
  warns clearly (in its window and in the game) and runs its smallest server. (`BNGE_LEAN=off` turns the low-memory
  server off.) The same low-memory server runs on any computer whose **Max RAM for BeamNG.Earth** is set low.
- Quality presets (Low / Medium / High / Ultra / Extreme) in BeamNG.Earth's own pause-menu tab (pause menu >
  BeamNG.Earth > Graphics; every settings page is listed down the side there), each fine-tunable; the first start
  picks Low, Medium or High for your hardware (Medium on a Steam Deck). **Extreme** puts every setting at its most,
  out to the real horizon, for the strongest PCs (it is never picked for you). (The "Even frame pacing" setting is gone:
  steady game speed is the default now, from the engine patch dtsmooth, at no frame-rate cost.) The
  Graphics page's settings:
  - **Terrain detail** (Low to Extreme): how far out each finer level of the distant land reaches (Extreme builds
    about twice as many distant ground pieces).
  - **Terrain pixel error** (0 = off, the default; 0.5 to 8 px; advanced): picks the distant ground's detail by what
    your screen shows: the largest jump in pixels allowed when a piece switches detail (your resolution and field
    of view count), so flat land stays coarser and mountains finer.
  - **Ground photo** (1024 / 2048 / 4096): the sharpness of the aerial photo and the painted roads on the
    ground around you; from the next level load.
  - **Ground grid** (1 m / 2 m): how fine the ground you drive on is. 2 m (the Low and Medium presets) loads a
    quarter of the data each time the ground moves with you: shorter hitches while driving, at the cost of softer
    small bumps and road edges; from the next start of BeamNG.Earth.
  - **Tree density** (Lowest to Extreme) and **Tree draw distance** (about 0.5 to 1.7 miles; 0.8 miles at Normal).
  - **Building draw distance**: the simple far buildings, the skyline and far bridges (Near to Max, twice Normal's 1
    and 3 miles).
  - **Object detail**: how far out objects keep their detailed models (Game setting = the game's Mesh Quality).
  - **Distant traffic lights**: moving headlights and taillights on distant roads (needs Roads and nighttime).
    **Distant lights without traffic** keeps them on even when game traffic is off; otherwise they follow traffic.
    Density follows road class, length and local hour, with quieter evenings and sparse overnight traffic.
    Cars persist around the player when turning or zooming;
    **Distant traffic quality** offers Low, Medium and High populations; lower levels reduce resource use.
    Off-screen motion updates less often. See [the implementation guide](docs/DISTANT_TRAFFIC.md) for bounds.
  - **Grass and shrubs**, **Grass draw distance**, **Rocks**, **Street lamp lights**, **View distance** (the real
    horizon or a fixed distance).
  - **Road surface detail** (Off / Basic / Full; Off on Low, Basic on Medium, Full above): wear on the paved roads
    from the game's own road decals, drawn with the road itself (no extra objects). Basic: tyre polish in the lanes'
    wheel paths on busier roads, a concrete road's slab and lane joints (OpenStreetMap surface=concrete) and a grassy
    asphalt edge on rural roads through grass, crops or woods (the land cover beside them). Full
    also sealed and alligator cracks, crack lines, repair patches, potholes on poor roads (OpenStreetMap smoothness
    where mapped), manhole covers on town streets, oil spots in parking lots and, in the autumn, fallen leaves
    along the edges where trees stand beside the road.
  - What loads around you reaches the same distance in every direction (a circle; before it was a square, whose
    corners reached about 40 % farther).
- Uses your graphics card for some of the world server's work (colour matching of the photos, compressing the distant
  textures) when it can, so the server needs less of your CPU; without a usable one the CPU does it as before.
- Offline play from the download cache (with a size cap and "clear cache").
- Overlapping physics blocks reuse raw land-cover classifications in a bounded native cache. Updated source
  data invalidates them; terrain slopes, underwater materials and road surfaces are still computed afresh.
- **Performance** settings page: the world server's CPU cores (Auto: a quarter of your cores, at most half) and its
  priority (Low: the game always comes first; Normal: the map loads faster on a busy CPU), from the next start; and
  **Full priority loading**: when you join the world and after a teleport, the app builds the place on every CPU core
  and the game keeps two until it has loaded.
- **Max RAM for BeamNG.Earth** (Performance settings page): the most memory the world server may hold, all its
  processes together (the game's own memory is not counted). **Auto** (the default) takes a third of your computer's
  memory below 24 GB (about 5 GB on a 16 GB PC, at least 1.5 GB) and 60 % of it on a bigger computer, at most 24 GB
  (about 19 GB on a 32 GB PC); or pick 3 to 32 GB. The
  page shows the memory in use against the limit. Near the limit the server drops its caches and restarts its busiest
  helper processes, and it uses less while your computer is short of memory; the caches and the number of helper
  processes are sized for the limit from the next start (the limit itself applies at once). A low limit makes the
  world load more slowly, not crash. (`--max-ram-gb` / `BNGE_MAX_RAM_GB` on the server; `BNGE_MEMWATCH=off` turns the
  watchdog off.)
- Works behind a proxy: the app's downloads use the HTTP/HTTPS proxy set on your computer (HTTPS_PROXY, HTTP_PROXY,
  NO_PROXY or the system's proxy setting; SOCKS proxies are not supported).
- Updates: the mod tells you in game (and on its settings page, with a link) when a newer version is on GitHub;
  with the downloaded app (.exe / AppImage), **Update now** downloads it next to your copy, checks it against
  GitHub's checksum, and the next time you start BeamNG.Earth the new version starts (your old file and shortcut
  keep working). Older copies the updater downloaded itself are removed at start, except the previous version and
  the one your shortcut starts; a copy you downloaded by hand is never touched.
- **Add-ons** settings page: mods and tools other people made for BeamNG.Earth (a short list we keep: for now
  Worldwide Trucking Career: Dangerous Contracts and BeamNG.Earth Live GPS), each with its description and changelog
  from its own GitHub page. Game mods install, update, turn off and on, and uninstall from the page, through the game's
  own mod manager, after a notice naming the author (they are not made by BeamNG.Earth); each download comes from the
  author's own GitHub releases and is checked against GitHub's checksum. Not while a BeamMP server is joined. Other
  mods can use BeamNG.Earth through its modding API (`beamngEarth_api`), and programs beside the game can read your
  position from `http://127.0.0.1:47615/v1/pose` (this computer only): see [docs/ADDONS.md](docs/ADDONS.md).

## How it works
- A **companion server** (`server/`: Python, with its heavy work in a Rust module, `native/earthfast`) downloads
  elevation, imagery and map data, builds terrain, roads, buildings and the rest in a local flat frame around a
  moving origin, and streams them to the game over a local socket. The game's Lua cannot reach the internet, so the
  server is required.
- The **mod** (`mod/beamng_earth/`) loads the tiles as meshes, keeps an invisible physics terrain
  under the car, moves the world origin as you drive (floating origin), and blends level-of-detail
  changes so nothing pops.
- The project's CDN, data builds and backend services, and how to run your own: [docs/HOSTING.md](docs/HOSTING.md).
- Design and measurements: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
  [docs/V1_4_SPEC.md](docs/V1_4_SPEC.md), research notes in [docs/research/](docs/research/).

## Install and play
**You need:** BeamNG.drive (tested with 0.40.x) on Steam, free disk space for the map download cache (it grows
as you visit places, up to 20 GB by default; Settings > Storage > "Cache size limit" takes any size in GB, 0 for no limit), and an internet
connection (the map is downloaded while you play).

1. **Download** the file for your system from the [Releases](https://github.com/TecnaGamer/BeamNG.Earth/releases)
   page:
   - **Windows:** `BeamNG.Earth-<version>.exe`. Windows may warn about an unknown app: **More info > Run anyway**.
   - **Linux:** `BeamNG.Earth-<version>-x86_64.AppImage`. Make it executable (right-click > Properties >
     "Allow executing", or `chmod +x`).
2. **Run it** (with Steam running). A console window opens (like BeamMP's launcher); it copies the mod into the
   game, starts the world server and starts BeamNG.drive. Closing the game stops the server.
   - **Linux, native game:** started with the engine patches (`tools/gcpatch`: grass kept across block moves,
     steady game speed (dtsmooth: no catch-up burst after a hitch), shorter ground block swaps (terfast), texkeep
     opt-in for testing) already applied, no Steam launch options needed. On Windows the launcher applies dtsmooth
     and terfast to the running game.
   - **Linux, Windows build under Proton:** the AppImage sees the Proton setting and starts that build through
     Steam (the server runs natively). The `.exe` also works inside the game's Proton prefix (for example with
     protontricks' launcher).
   - **Linux, Steam as a Flatpak (or snap):** the mod goes into the game's folder inside the Flatpak
     (`~/.var/app/com.valvesoftware.Steam/...`) and the game starts through Steam (without the grass patch).
   - The Windows build shows BeamNG's launch options dialog (renderer, safe mode) as usual; `--gfx dx11` /
     `--gfx vk` / `--gfx dx12` skip it with that renderer.
3. **Play:** in the game, go to **Freeroam** and pick the **BeamNG.Earth** level. A welcome page explains the
   basics the first time. Press **M** for the world map: search a place or an address, right-click to teleport,
   left-click to set a GPS route, and **Autodrive** to let the car drive it.

**From the source instead** (any system with [Python 3.10+](https://www.python.org/downloads/)): download this
project (the green **Code** button > **Download ZIP**) and unzip it. Install once (and after each update):
double-click `run_server.bat` (Windows) or run `python3 install.py` (Linux). Then start the server with
`run_server.bat` / `./run_server.sh`, keep its window open, and start the game yourself. Or run
`python3 launcher.py`, which also starts the game like the downloads do.

**Updating:** the mod tells you in game when a newer version is on GitHub. Download the new file and run it (from
the source: unzip it over the old folder and run the installer again).

**Development builds (pre-releases):** every change pushed to the `dev` branch is built automatically and published
as a GitHub **pre-release** named after the beta it follows, with a build number (for example
`1.7.2-beta.dev.37`). They have the newest fixes and features, but they are **built from the development branch
and not thoroughly tested**: expect bugs, and things may change or break from one build to the next. The beta
releases are the tested ones. A development build says so in game, and its update check offers the next development
build (or the next beta, if that is newer); a beta only ever offers betas. Only the newest three development builds
are kept on the releases page as their own pre-releases; older ones (the last 60) stay downloadable from one release,
**Development builds (archive)**, whose page lists them newest first with their Windows and Linux files.

**Uninstalling:** delete the `beamng_earth` folder in `<BeamNG user folder>/current/mods/unpacked/` and the
downloaded file (or this folder); the map cache is `.cache/beamng-earth` in your home folder (`~/.cache/beamng-earth` on Linux,
`C:\Users\<you>\.cache\beamng-earth` on Windows).

**The map cache on another drive:** in the game, settings page > Download cache > **Cache folder**: **Change...** opens
your system's folder window, or type or paste a folder and press **Use**. Or start the app once with `--cache-dir PATH`
(e.g. a shortcut to `BeamNG.Earth-<version>.exe --cache-dir "D:\BeamNG.Earth cache"`). It is used from the next start
and remembered; `default` goes back. Move the old cache folder there first to keep what is already downloaded.

**Deleting the whole map cache:** start the app once with `--clear-cache`. Its folders are moved aside at once and
deleted in the background while you play (no long wait for the many small files). The in-game **Clear cache** button
keeps the area around you.

**BeamNG.drive installed somewhere else** (not in a Steam library the app finds, e.g. another launcher): the app asks
where it is the first time (Enter opens your system's folder window, or type or paste the folder) and remembers it;
`--game-dir PATH` sets it too, `--game-dir default` forgets it. A game folder outside Steam is started directly. On
Linux, BeamMP's launcher finds the game only in Steam's libraries, so BeamMP isn't started with such a copy.

More detail (traffic, the status bar, reporting problems, troubleshooting): [docs/QUICKSTART.md](docs/QUICKSTART.md).

**Compute sharing (optional, for tinkerers):** another computer on your network can build part of the distant
landscape for you. On it, from a copy of the same version's source: `python3 server/peerwork.py serve` (port 47620,
`--workers N` processes; it downloads its own map data). On the computer you play on, set `BNGE_PEERS` to the
helpers before starting BeamNG.Earth, for example `BNGE_PEERS=192.168.1.20:47620,192.168.1.21:47620`. Helpers
running other code are left out, and anything a helper fails on is built on your own computer. Only use helpers on a
network you trust: the connection is not encrypted.

### Developer setup
Rail-crossing props can be regenerated with `python3 tools/build_rail_crossing_props.py`;
their lettering uses the bundled DejaVu Sans Bold font on every platform.

`python3 install.py` also works from a checkout (mod copy, `.venv`, then `python3 start.py`); by hand:
1. Python 3.10+ with the packages in `server/requirements.txt`. The server needs its Rust module: the checkout
   carries it built for Linux x86-64 (`server/earthfast.abi3.so`) and Windows (`server/earthfast.pyd`); after
   changing `native/earthfast`, or on another system, build it with `python3 tools/build_native.py` (Rust's cargo;
   `--windows` cross-builds the `.pyd` with cargo-zigbuild).
2. Copy (or sync) `mod/beamng_earth/` to
   `<BeamNG user folder>/current/mods/unpacked/beamng_earth/`.
3. Start the server: `python3 server/earthd.py`
4. Start BeamNG.drive and load the **BeamNG.Earth** level. The mod connects to the server and
   streams the world; the status bar at the top shows where you are and the loading, **M** opens the
   world map (search, teleport, GPS routes, Autodrive).

Player downloads: `python3 tools/build_app.py` builds this system's AppImage / .exe (PyInstaller; see its
docstring; for the .exe run `tools/build_bootloader.py` first: a bootloader built from source, fewer antivirus
false positives); GitHub Actions (`.github/workflows/release.yml`) builds both and publishes the release `v<VERSION>`
whenever `main` gets a `VERSION` without one. Release zip of the source: `python3 tools/package.py`.

Tests: `cd server && python3 -m unittest discover -s tests -t tests` and
`cd mod/tests && for f in test_*.lua; do luajit $f; done` (no luajit: `pip install lupa`, then
`python3 tools/run_lua_tests.py`).

For native thread-crash diagnostics without starting the game, see
[the isolated stress runner](docs/NATIVE_CRASH_REPRODUCTION.md).

## Multiplayer (BeamMP)

BeamNG.Earth runs on [BeamMP](https://beammp.com/) servers (in testing): drive the real world together.

> **Support for BeamNG.Earth on BeamMP comes from us, not from BeamMP.** For questions or help with joining or hosting
> a BeamNG.Earth server, join the TGForever Discord: **[discord.gg/tgf](https://discord.gg/tgf)**. BeamNG.Earth
> isn't like a normal BeamNG mod: it needs its own app running next to the game. If something isn't working, it's
> almost certainly a BeamNG.Earth problem, not a BeamMP one, so please **don't contact the BeamMP team or open BeamMP
> support tickets** when you run into trouble getting BeamNG.Earth to work with BeamMP.
>
> **BeamNG.Earth servers are hidden from players who don't run BeamNG.Earth.** BeamMP can't install BeamNG.Earth for
> a player who joins, because it needs its own app (a launcher and a world server that builds the world on each
> player's computer). So BeamMP's own server list never shows these servers. Players with the BeamNG.Earth app find
> them in the BeamNG.Earth category of BeamMP's server browser, or join by Direct Connect.

**Joining a server**
1. Start the BeamNG.Earth app (the same version as the server's). It starts BeamMP with the game: the official BeamMP
   launcher, unchanged (BeamMP's code is AGPL-3.0, [github.com/BeamMP](https://github.com/BeamMP)). If BeamMP isn't
   installed, the app asks once: **Yes / No / Don't ask again / Have it already** (the last one asks where your
   launcher is: Enter opens your system's file window, or type or paste the path; `--beammp-launcher PATH` sets it
   too, and it is kept for the next starts). To change it later: settings page > Multiplayer > **BeamMP launcher**.
   - **Windows:** Yes downloads BeamMP's official launcher from BeamMP's GitHub releases (checked against the checksum
     BeamMP publishes; Windows Firewall then asks once whether it may use the network: allow it).
   - **Linux and Steam Deck:** BeamMP publishes no Linux download, so Yes builds its launcher on your computer from
     BeamMP's source, the newest version that passed BeamMP's own tests, in an Ubuntu container (podman or docker:
     SteamOS has podman; about a minute on a Steam Deck). A launcher you built yourself is used when found
     (a `BeamMP-Launcher` folder in your home folder or one folder down, e.g. `~/Games/BeamMP-Launcher`, or
     `~/.local/share`; or answer "Have it already", or set `BEAMMP_LAUNCHER`).
   - The setting **Start BeamMP with BeamNG.Earth** (Pause menu > BeamNG.Earth > Multiplayer) turns this off or
     back on; `--no-beammp` starts the game without BeamMP once. If BeamMP is not ready within 30 seconds the app
     stops it and carries on in single player (`--beammp-wait SECONDS` waits longer, up to 600).
2. In the game: main menu > More > BeamMP, log in (an account or a guest).
3. Pick the **BeamNG.Earth** category in BeamMP's server browser: the servers running BeamNG.Earth (search,
   filters, favorites and details as for the other servers), the official server at the top. They are listed only there, for players who have BeamNG.Earth. A private server: Direct Connect with its address (the
   [TGForever Discord server](https://discord.gg/tgf) has some). Every player needs the BeamNG.Earth app running: each builds the
   world on their own computer.
   (Starting the BeamMP launcher yourself first still works: the app then starts just the world server.)

Leaving a server turns BeamNG.Earth back on (joining one switches your other mods off).

You are sent to where the others are. Cars show in the right place in everyone's world; other players show on the
world map (**M**) as green arrows; players far away (another country) are hidden until you meet. Crash damage: a crash
you watch keeps the damage your own game worked out; a car that crashed out of your sight takes on its owner's damage
(broken parts and dents) once it comes within about 0.6 miles.

Each player's app builds the world around them on their own computer. **Sharing the work** (in testing): the BeamMP
server keeps what its players build of the slowest part of the world, the road levelling plans of each map area
(compressed, a few hundred KB each), and sends it to every player who comes there later, this session or the next. A
player near the others gets much of it from the server instead of building it, which is quickest on slower
computers. It all goes through the BeamMP connection itself, nothing to set up or forward, and only between players
running the same BeamNG.Earth version with the same settings. A server can also hold baked packs of whole areas (put
`.bnge` files in `Resources/Server/BeamNGEarth/bake/`), and the machine running the server can build ahead of
the players itself (the share helper, below).

<!-- v1 shared world building: commented out 2026-10-27 (off since 2026-10-23; not coming back for a while).
**Sharing the work** (turned off for now: each player builds their own world)**:** players near each other share one
world origin and split the building of the distant landscape (each piece is built once and sent to the others; faster computers take more), while a player far from
everyone builds their own. For that the other players must reach your computer on a TCP port, 47621 unless the server
picks another (the server tells your app which): on the same network it just works; over the internet, forward that
port in your router, or you simply build everything yourself. A port of your own: set the environment variable
`BNGE_SHARE_PORT` before starting the app.
-->

**Hosting a server:** download `BeamNG.Earth-BeamMP-Server-<version>.zip` from the
[Releases](https://github.com/TecnaGamer/BeamNG.Earth/releases) page and follow the guide inside
(`BeamNG.Earth-README.md`). In short: unzip it into a BeamMP server's folder, set
`Map = "/levels/beamng_earth/info.json"` in `ServerConfig.toml`, forward the server's port (TCP and UDP) for players
over the internet, and start it (its log shows `[BeamNG.Earth] plugin loaded`). Which list it is on:
`Private = false` with your AuthKey puts it on the BeamNG.Earth list (the in-game category) and the plugin keeps it off
BeamMP's own public list, so players without BeamNG.Earth never see it; `Private = true`, or no AuthKey: on no list,
players use Direct Connect. The plugin lists the server once a minute with `curl` (Linux, and Windows 10 and later).
Update the server zip with every release: it carries the mod the server sends to players. The server's time and weather for everyone
(`G.TIME`: real or a fixed / running time such as `'20:30'`; `G.WEATHER`: real or clear, cloudy, rain, storm, snow,
fog) are set near the top of `Resources/Server/BeamNGEarth/main.lua`. The server needs no world data and
little CPU. From the source, `python3 tools/package.py --beammp` builds the same zip. Releases may also carry
ready-made host bundles (`BeamNG.Earth-Host-<version>-Windows.zip` / `-Linux-x86_64.zip`, built by
`tools/build_host.py`): the zip above plus BeamMP's own unmodified server program (v3.9.3, checked against the
checksum BeamMP publishes), its licence and source, and a `ServerConfig.toml` with an empty AuthKey for your own.
With Docker: `ghcr.io/tecnagamer/beamng.earth-server:<version>`, the Linux host bundle in a container, set up through
its environment (`BEAMMP_AUTH_KEY`, `BEAMMP_NAME`, `BNGE_WEATHER`, ...; see "Docker" in the server zip's guide,
[beammp/README.md](beammp/README.md#docker)).

**Shared road plans** (experimental, off by default): `BeamNG.Earth --world-cache export|serve|fetch|verify` packs
the road plans one machine has built into a checked bundle that other players can fetch and reuse, so their
companions skip planning those roads; see [docs/SHARED_WORLD.md](docs/SHARED_WORLD.md).

The server keeps its players' shared world building in `Resources/Server/BeamNGEarth/store` (up to `G.SHARE_STORE_MB`,
2 GB by default; the pieces used longest ago go first) and sends at most `G.SHARE_OUT_KBPS` (1 MB/s) of it, both near
the top of `main.lua`. **Share helper** (in testing): to let the server's machine build ahead of its players, run
BeamNG.Earth's world server next to it, from the source:
`python3 server/sharehelper.py --share-dir <BeamMP server>/Resources/Server/BeamNGEarth --cache <a folder for its map
data> --procs 4`. It needs the same BeamNG.Earth version as the players and a few GB of disk for each area.

## Usage statistics (telemetry)

To see how many people play and how hard we are pushing the free map-data services we rely on, the world server
sends a small anonymous report every 5 minutes: a random install id (created on your machine; no name, account or
hardware id), the app version and operating system, whether the game is connected, whether it is a BeamMP session,
and how many requests went to each data source (with their errors and rate limits). With **Performance data** ticked
it also sends the game's frame rate and hitches, how well the world keeps up, your CPU / GPU model and settings, and
error codes. No positions or places are sent.
The collector looks up your country from the connection's address and does not keep the address.

The public numbers (players online, peaks, history, countries with at least 5 players) are at
<https://earth.tgforever.net/players>, and the performance numbers (groups of at least 5 players) at
<https://earth.tgforever.net/performance>.

**Nothing is sent until you choose.** The welcome screen asks first, with a main switch and two parts (performance
data, network load); how the boxes start depends on your computer's time zone (nothing ticked in the EU and some
other places). Change it any time in BeamNG.Earth's settings page, Interface (from the next report on), or set the
environment variable `BNE_TELEMETRY=off` before starting BeamNG.Earth. Reports are never sent in offline mode
(`--offline`). Every field, why we want it and what happens to it: [TELEMETRY.md](TELEMETRY.md).

## Sending a report to the developers

The in-game reporter (Ctrl+Shift+E; Ctrl+Shift+Y records a 30 s performance profile) saves each report in your BeamNG
user folder (`beamngEarth/reports/`). After saving, **Send to the developers** opens a window that lists exactly what
would be sent, with the files to read, and nothing is sent until you click **Send** there:

- your title and description, the kind of report, and the screenshot (a box leaves it out);
- the location: exact, or rounded to about 1 km with a box (the map ids, street names and the roads and buildings near
  the spot are then left out too; the screenshot can still show where you are);
- the view: camera, heading, the point and object under the screen's centre, the car's position and speed;
- game and PC: game and mod versions, graphics settings, CPU / GPU / memory, BeamNG.Earth's settings, the names of
  enabled mods, the last 60 s of frame times, weather and time of day;
- the world server's state: its version and settings (`BNE_*` / `BNGE_*` overrides; secret ones only as "set"),
  memory, loading backlog, the data sources and the map features at the spot;
- logs (a box leaves them out): the world server's last 300 lines and the game's warnings and errors of the last 10
  minutes.

User names, home folders, e-mail and IP addresses are removed from all of it, also from the copy kept on your
computer. Reports are sent **privately** to the BeamNG.Earth developers (the project's own server,
`earth.tgforever.net`) and are **not published**; they are kept 90 days. A random install id (no name or account)
goes with each report so reports from one computer can be told apart. Sending is your choice each time, so it works
with the usage statistics off too. The send runs in the background, is retried, and a report not sent yet goes at the
next start; the window then shows a short report id (like `K7QD-3M9X`) to quote on Discord or GitHub. **Open GitHub
issue page** still opens a ready-to-post public issue if you prefer that. Details: [TELEMETRY.md](TELEMETRY.md#reports-you-send).

## Data sources and attribution
The repository contains only code; all map data is downloaded at runtime and cached locally.
- Imagery: state and national orthophoto services where they are open (Maryland, a dozen more US
  states, Switzerland, France, Portugal, Spain, the Netherlands, Germany's states, Poland (GUGiK), Japan, parts of Australia and
  Canada, and more), USDA NAIP and USGS The National Map (public domain; "Map services and data available from U.S. Geological Survey, National Geospatial Program") in the US, and Sentinel-2
  cloudless by EOX IT Services GmbH everywhere (https://s2maps.eu, contains modified Copernicus Sentinel
  data; 2016 release CC BY 4.0, 2023 release CC BY-NC-SA 4.0, non-commercial).
- Elevation: Mapterhorn (https://mapterhorn.com/attribution; zooms 0-12 served from BeamNG.Earth's own tile host,
  repacked from Mapterhorn's archive as Mapterhorn allows), AWS Terrain Tiles / Mapzen; the polar caps from
  85 deg on: Copernicus DEM GLO-30 (produced using Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and (c) Airbus
  Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA).
- Canada: HRDEM (contains information licensed under the Open Government Licence - Canada, Natural Resources
  Canada); British Columbia: LidarBC terrain, surface models (tree heights) and orthophotos (contains
  information licensed under the Open Government Licence - British Columbia).
- Roads, buildings, water and places: Overture Maps / OpenStreetMap contributors (lane counts, widths and speed limits from OpenStreetMap via OSM US Layercake); trees from a
  canopy height model; airports from OurAirports.
- Oil wells and refineries: OpenStreetMap contributors (ODbL; the bundled `oil_wells.npz` and `refineries.npz`).
- Highway signs: OpenStreetMap contributors (ODbL; exit and overhead destinations and mile references, rebuilt
  weekly from the planet by the BeamNG.Earth-data repo); lettering: the Roadgeek 2014 font (MIT, Sammi De Guzman);
  German Autobahn signs: the DINish font (SIL OFL 1.1) and the RWB/RWBA pictograms (public domain);
  US state outlines: U.S. Census Bureau TIGERweb (public domain).
- Direction signs: place names, populations and road route numbers from Overture Maps / OpenStreetMap contributors
  (ODbL); lettering: Overpass (SIL OFL 1.1) and Fira Sans Condensed (SIL OFL 1.1).
- German junction signs: the faces of Zeichen 205, 206, 301, 306, 307, 274.1, 274.2 and Zusatzzeichen 1002 after the
  Verkehrszeichenkatalog and the VwV-StVO (BMDV / BASt; amtliche Werke, § 5 UrhG); lettering: D-DIN (SIL OFL 1.1,
  Datto); priority roads, 30 zones and junction sign nodes: OpenStreetMap contributors (ODbL; rebuilt weekly from the
  planet by the BeamNG.Earth-data repo).
- Town signs: towns and villages from Overture Maps (divisions), Germany's mapped town signs from OpenStreetMap
  contributors (ODbL, rebuilt weekly), its Städte and Märkte from the Statistisches Bundesamt (Destatis,
  Gemeindeverzeichnis); German lettering: D-DIN (SIL OFL 1.1).
- Railways: OpenStreetMap contributors (ODbL; railway tags, rebuilt weekly from the planet by the [BeamNG.Earth-data](https://github.com/TecnaGamer/BeamNG.Earth-data) repo's GitHub Action); US level crossings: the Federal Railroad Administration's Highway-Rail Crossing Inventory (public domain).
- Climate zones (the forest sounds' set): Beck et al. (2023), High-resolution (1 km) Köppen-Geiger maps for
  1901–2099, Scientific Data 10, 724 (CC BY 4.0).
Each source's terms apply to the downloaded data; the in-game window shows the attribution line.
The full list of data sources, services, libraries, tools and references, with licences and the
credit each one asks for, is in [CREDITS.md](CREDITS.md). [SOURCES.md](SOURCES.md) explains what each source does in
the game, what it is sent, and what you see while it does not answer (the status line names such a source and what
it affects).

## License
Code: MIT, see [LICENSE](LICENSE). BeamNG.drive is a trademark of BeamNG GmbH; this is an unofficial
fan project and contains no BeamNG assets.
