# Data sources

BeamNG.Earth builds the world around you from open data that the BeamNG.Earth server (the program the app starts
on your PC) downloads as you drive. This page explains what each source does, when it is used, and what you see if
it does not answer. Licences and the exact attribution each one asks for are in [CREDITS.md](CREDITS.md).

A few things hold for all of them:

- **Your PC talks to them directly.** There is no BeamNG.Earth middleman server. Requests carry only what the source
  needs to answer (a map tile number, a range of a file, or for search, weather and routing, the place asked about).
  Your e-mail, Steam name or any account details are never sent.
- **Everything is kept on disk** in the cache folder (Settings: the cache folder). A place you have visited loads
  from there the next time, and it keeps working offline.
- **Each source has its own speed limit and back-off.** If one stops answering, the server waits and tries again
  later instead of hammering it. While a source is down, the status line at the top of the screen names it and
  says what it affects, for example:
  `Server OK, some internet data sources not answering: imagery.nationalmap.gov unreachable (detailed US aerial
  photos (NAIP); coarser photos meanwhile)`.
- **Optional sources are not named in the status line.** The world builds without them (weather, search, routing,
  updates, regional photo services with a worldwide fallback, the extra road and railway details), so a source
  blocked in your country does not leave a warning on screen for good.

## Map data: roads, buildings, water, land use

| Source | Address | What it builds | If it does not answer | Status line |
|---|---|---|---|---|
| Overture Maps (map data releases) | `overturemaps-us-west-2.s3.us-west-2.amazonaws.com` | Every road and railway line, every building (footprint, height, roof shape where mapped), water areas, rivers, land use (parks, farmland, industrial areas), bridges and tunnels as mapped, gas stations, airports' surroundings. Read in small ranges of large files: only the part around you. | Places you have not visited before come without roads, buildings or water until it answers. Places you have visited load from disk. | named |
| Overture Maps (second copy, on Azure) | `overturemapswestus2.blob.core.windows.net` | The same data, used when the first copy does not answer. | Only matters while the first copy is down too. | named |
| Overture Maps file index | `stac.overturemaps.org` | The list of which file holds which part of the world. | The app carries its own copy of the list, which is used instead. | named |
| OpenStreetMap road details (OSM US Layercake) | `data.openstreetmap.us` | OpenStreetMap's own road tags matched to the roads: lanes, widths, sidewalks, surfaces, speed limits, roads under construction. | Roads keep their width, lane and sidewalk estimates from their class and the land around them. | optional |

## Terrain heights (elevation)

| Source | Address | What it builds | If it does not answer | Status line |
|---|---|---|---|---|
| Mapterhorn terrain tiles | `tiles.mapterhorn.com` | The shape of the ground everywhere: hills, valleys, road cuttings. Mapterhorn combines the best national elevation data (as fine as 1 m in parts of Europe and the US) with global data elsewhere. | New places use the coarser backup below. | named |
| Mapterhorn coverage list | `download.mapterhorn.com` | Which areas have detailed heights, so the server does not ask for tiles that do not exist. Fetched about once a week. | New places may load a little slower. | named |
| AWS Terrain Tiles (backup) | `s3.amazonaws.com` | Coarser heights (SRTM and similar) where Mapterhorn has none or does not answer. | Only matters while Mapterhorn is down too; with both down, new places have no terrain. | named |
| Copernicus DEM GLO-30 | `copernicus-dem-30m.s3.amazonaws.com` | Heights near the North and South Poles, where the other sources stop. | No terrain past about 85° north or south. | named |
| NRCan HRDEM mosaic (CanElevation) | `canelevation-dem.s3.ca-central-1.amazonaws.com` | Canada's lidar ground heights: the distant terrain is lined up with them (and, opt-in, the near terrain read from them). | Canada's distant terrain may stand on the forest tops. | optional |
| USGS 3DEP lidar point clouds | `s3-us-west-2.amazonaws.com` | In the US, the real height of bridge decks, measured along each bridge from the lidar points (about 13 MB per map cell full of bridges, once). Off with `BNE_LIDAR_DECKS=off`. | Bridge decks get the estimated heights. | optional |
| NVDB (Statens vegvesen) | `nvdbapiles.atlas.vegvesen.no` | In Norway, the measured heights of bridges and tunnels (and roads where the terrain is coarse). Off with `BNE_ROADZ=off`. | Estimated heights. | optional |
| IGN BD TOPO roads | `data.geopf.fr` (WFS) | In France, the measured heights of bridges and tunnels (and roads where the terrain is coarse). Off with `BNE_ROADZ=off`. | Estimated heights. | optional |
| LidarBC (Province of British Columbia) | `nrs.objectstore.gov.bc.ca` | British Columbia's 10-15 cm aerial photos (around Vancouver and Victoria); opt-in, its lidar terrain and measured tree heights. | Those places get the next photo source. | optional |

## Aerial and satellite photos (what the ground looks like)

The ground's texture comes from the best photo available at each spot, in this order: a regional service, the US
national photos, then worldwide satellite photos. If one does not answer, the next one fills in.

| Source | Address | Where | If it does not answer | Status line |
|---|---|---|---|---|
| Regional aerial photos (state and national mapping agencies) | see the list below | the regions below, mostly 10-30 cm per pixel | Coarser photos from the next source (US national photos or Sentinel-2). | optional |
| Maryland six-inch photos (MD iMAP) | `mdgeodata.md.gov` | Maryland, 15 cm per pixel | Coarser photos meanwhile. | named |
| USGS / USDA NAIP aerial photos | `imagery.nationalmap.gov` | the lower 48 US states, 30-60 cm per pixel, near you | Coarser photos meanwhile. | named |
| USDA NAIP aerial photos (second source) | `apps.geo.fpac.usda.gov` | the same photos, from the US Department of Agriculture | Coarser photos meanwhile. | named |
| USGS The National Map photos | `basemap.nationalmap.gov` | the US, the middle distance and the distant land | Sentinel-2 satellite photos meanwhile. | named |
| Sentinel-2 cloudless (EOX) | `tiles.maps.eox.at` | the whole world, 10 m per pixel; the distant land everywhere and the near ground outside the regions above. Also the world map's base layers. | New ground outside the regions above may stay plain (the land cover's colours). | named |

Regional services, each used only inside its own area:
Connecticut (`cteco.uconn.edu`), Massachusetts (`tiles.arcgis.com`), Washington DC (`imagery.dcgis.dc.gov`),
Indiana (`di-ingov.img.arcgis.com`), Ohio (`maps.ohio.gov`), North Carolina (`services.nconemap.gov`),
Pennsylvania (`imagery.pasda.psu.edu`), Virginia (`vginmaps.vdem.virginia.gov`), New York State
(`orthos.its.ny.gov`), New Jersey (`maps.nj.gov`), Delaware (`imagery.firstmap.delaware.gov`), Vermont
(`maps.vcgi.vermont.gov`), Switzerland (`wmts.geo.admin.ch`), France (`data.geopf.fr`), Spain
(`tms-pnoa-ma.idee.es`), the Netherlands (`service.pdok.nl`), Luxembourg (`wmts1.geoportail.lu`), Austria
(`maps.wien.gv.at`), South Tyrol (`geoservices.buergernetz.bz.it`), Italy (`wms.pcn.minambiente.it`), Poland
(`mapy.geoportal.gov.pl`), Czechia (`ags.cuzk.gov.cz`), Estonia (`tiles.maaamet.ee`), Flanders
(`geo.api.vlaanderen.be`), Bavaria (`geoservices.bayern.de`), North Rhine-Westphalia (`www.wms.nrw.de`),
Baden-Wurttemberg (`owsproxy.lgl-bw.de`), Lower Saxony (`opendata.lgln.niedersachsen.de`), Berlin
(`gdi.berlin.de`), Hesse (`www.gds-srv.hessen.de`), Japan (`cyberjapandata.gsi.go.jp`), New South Wales
(`maps.six.nsw.gov.au`), Tasmania (`services.thelist.tas.gov.au`) and Ontario (`ws.geoservices.lrc.gov.on.ca`).

## Land cover, trees and water

| Source | Address | What it builds | If it does not answer | Status line |
|---|---|---|---|---|
| ESA WorldCover (10 m land cover) | `esa-worldcover.s3.eu-central-1.amazonaws.com` | What covers the ground: forest, grass, fields, bare rock, sand, built-up land. It picks the ground materials (and their grip), rocks, which streets count as town streets, and the sounds of the landscape. | Places not visited before get a plain default ground until it answers; they are painted again once it does. | named |
| Meta / WRI canopy heights | `dataforgood-fb-data.s3.amazonaws.com` | The trees: where each one stands and how tall it is, from a map of tree heights metre by metre (forests, gaps, clearings, street trees). | Places not visited before have no trees until it answers. | named |
| NOAA NCEI bathymetry | `gis.ngdc.noaa.gov` | The depth of the sea along coasts (down to about 3 m detail along US coasts): the sea floor you can drive or sink into. | New coasts get a plain, evenly sloping sea floor. | named |

## Railways, airports and other details

| Source | Address | What it builds | If it does not answer | Status line |
|---|---|---|---|---|
| BeamNG.Earth's weekly data (GitHub Pages) | `tecnagamer.github.io` | Built every week from the whole OpenStreetMap planet by this project: railway details (track gauge, electrification, disused and abandoned lines, level crossings and their barriers) and, in the US, the Federal Railroad Administration's list of the gates, lights and crossbucks at each level crossing. | Railways keep their basic class; level crossings get equipment by road size. | optional |
| OurAirports | `davidmegginson.github.io` | Airports and their runways (position, length, width, surface), for paved runways and levelled airfields. Fetched about once a week. | The copy on disk is used. | named |

## Live services (used only when you use the feature)

| Source | Address | What it does | What is sent | If it does not answer | Status line |
|---|---|---|---|---|---|
| Open-Meteo | `api.open-meteo.com` | Real weather (clouds, rain, snow, wind, fog) and the local time zone. | The position you are at. | The weather stays as it is. | optional |
| Photon (komoot) | `photon.komoot.io` | The map's address and place search, and the town name in the status bar. | What you type, and your position (to rank nearby results first). | Search says it is unavailable for a moment. | optional |
| OSRM routing (FOSSGIS) | `routing.openstreetmap.de` | GPS routes over long distances on the map. | The start and the destination of the route. | No new route until it answers. | optional |
| EGM2008 gravity model (SourceForge) | `downloads.sourceforge.net` | Real gravity, which differs slightly from place to place (74 MB, downloaded once). | Nothing but the download. | Standard gravity is used. | optional |

## The app itself

| Source | Address | What it does | If it does not answer | Status line |
|---|---|---|---|---|
| GitHub (update check) | `raw.githubusercontent.com` | Checks a few times a day whether a newer version is out. | No update notice. | optional |
| GitHub (one-click update) | `api.github.com`, `github.com` | Downloads the new version when you press the update button. | The update waits. | optional |
| BeamNG.Earth server list | `earth.tgforever.net` | The BeamNG.Earth tab in BeamMP's server browser, when you open it or press Refresh. | The list stays empty. | optional |

## Data that comes with the app (no download)

- **Country and state borders, big town names** (Natural Earth): the world map.
- **Climate zones** (Beck et al. Köppen-Geiger map): which trees grow where and which landscape sounds play.
- **Oil wells and refineries** (OpenStreetMap, exported once): pump jacks, refinery sounds.
- **Bridge types** (the US National Bridge Inventory and Wikidata): trusses, arches, suspension and cable-stayed
  bridges, and their real spans and heights.
- **The Overture Maps file index**: a copy for when its server does not answer.

## Never contacted

The server never asks OpenStreetMap's own servers (the Overpass API, OSM's vector tiles) or Protomaps at runtime: they
are run by volunteers for other uses, and the data above already covers what they would give.
