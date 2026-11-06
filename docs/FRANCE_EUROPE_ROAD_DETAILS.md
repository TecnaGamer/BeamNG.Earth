# French signs and European motorway guardrails

Opt in with `BNE_EU_ROAD_DETAILS=on` and `--regional-poles europe`, or use
`tester_routes_europe.bat`. The default launch keeps the previous behaviour.
`tester_routes_et_plaques.bat` enables the same additions before calling the
existing international-plate launcher. It requires that launcher to be present.

## Signs and street names

French motorway guide signs reuse the existing worldwide OSM destination/exit
index, source road matching and bounded sign queue. France now receives French
blue/green/white faces, A/N red route cartouches, E green, D yellow, M blue, exit
numbers, metric distances, arrows and gantries. The existing source solver
places columns off pavement and preserves its vertical clearance. US shields,
mile markers and US merge diamonds are not emitted for French sign tiles.

Destination labels are source data. Gantries, sign stations and dimensions are
generated scenery estimates, not a reconstruction of every photographed sign.
The existing through-sign reader skips differing per-lane destinations; this
update does not claim exact lane assignment, exact grouping, all panels or all
supplementary text. Guide-sign coverage depends on the existing index.

The separate French offline snapshot supplies 2,257 usable direction relations,
using mapped colours when present and white when unspecified. Opposing faces share
one support; up to four arrow boards are stacked without overlapping lettering.
Unsupported/ambiguous mappings are skipped. Their provenance distinguishes
surveyed sign positions from junction-based estimates.

The priority directions catalogue uses mapped destinations first. Where those
are sparse, it compares legal turns at the same junction and can pair a
nearby village with a town or city farther along the route. These estimates
are specific to the approach and use distance along the road. The existing
checks for visible, unobstructed sign positions still apply.

Street plaques use actual road names at suitable urban junctions. Both wall
mounts and dedicated posts are supported, with up to 24 plaques per tile. The
whole plaque must clear rendered road ribbons and nearby building footprints.
Sight lines along the named street reject plaques hidden behind a building.
Where the rendered building has room on a facade facing the street, the plaque
can be fixed just above the first row of windows. Placement is inferred from
the generated scenery. Free-standing plaques and their posts have no collision.
Long names wrap onto two lines.

Junction STOP and give-way signs also test building footprints and sight lines
from their incoming approach. The search follows the road and keeps its traffic
side. If every candidate is blocked, the sign prop is omitted while the AI rule
and the road markings still apply.

## STOP faces and motorway exits

The European STOP props have a metal back, a rim, a post and two mesh LODs.
Their country-specific faces preserve the colours, borders, lettering outlines
and spacing of the attributed sign drawings. They do not substitute a general
road alphabet for the drawn word. `tools/sign_art/stop/sources.json` records the
exact source revision, checksums and shared or representative faces. The
individual art licenses are in `europe_stops/STOP_ART.NOTICE.md` beside the
models. The 3D dimensions are representative scenery sizes.

- France AB4 uses Roulex 45's Caractères drawing. [AB4 artwork](https://commons.wikimedia.org/wiki/File:France_road_sign_AB4.svg) and [French sign definitions](https://www.legifrance.gouv.fr/loda/id/JORFTEXT000000829916/2024-04-22/).
- Britain 601.1 follows the DfT sign drawing. [Working drawing P601.1](https://assets.publishing.service.gov.uk/media/5ed661a8e90e0754d4e62547/traffic-sign-drawing-schedule-09-part-02-item-01-p601x1.pdf).
- Sweden publishes the B2 drawing and the Tratex alphabet. [B2](https://www.transportstyrelsen.se/sv/vagtrafik/trafikregler-och-vagmarken/vagmarken/vajningspliktsmarken/stopplikt/) and [Tratex](https://www.transportstyrelsen.se/sv/vagtrafik/trafikregler-och-vagmarken/vagmarken/om-vagmarken/teckensnitt/).
- The Turkish face reads DUR. [KGM TT-2](https://www.kgm.gov.tr/SiteCollectionDocuments/KGMdocuments/Trafik/IsaretlerElKitabi/TrafikIsaretleriElKitabi2015.pdf).

French J14a beacons use a curved green body with two outward white arrows. The
1 m and 2 m models use heights of 1.20 m and 1.70 m from the manufacturer's
examples. A connected one-way exit must open a wide enough gap between the
through lane and the ramp. The generator places one marker in that gap, facing
approaching traffic. The road geometry determines the position independently
of the destination text. [J14a definition](https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000025041343/) and [Sodilor dimensions](https://sodilor.fr/wp-content/uploads/2025/10/CATALOGUE-SODILOR-2023_A34.pdf).

## Other French lettering

Black capitals use the existing Typoroute 2002 L1 digitization. On blue/green
faces its white rendering is a visual substitute for L2, not an exact L2 font.
Mixed case and metric distances use D-DIN; exact L4 italics are not included.
Existing town signs, fonts and their notices remain in place. No photographic
textures from the supplied reference screenshots are distributed.

## European guardrails and French SOS phones

Mapped steel rails and generated steel median rails use country profiles. The
profile table follows the supplied community map as a visual reference:

| Profile | Countries explicitly distinguished on that map |
| --- | --- |
| B | Denmark, Croatia, Poland, Serbia |
| Thin B | Czechia, Slovakia |
| Mixed A/B | Germany, Ireland, Montenegro, Turkey |
| A | Other mapped countries |

Reflectors follow its country colours, separately from the steel profile:
white in France, Sweden and Iceland; none in Germany, Switzerland, Portugal,
the UK, Ireland, the Netherlands, Norway, Denmark, Finland and Estonia; yellow
in Belgium and Spain; orange in Lithuania; orange/red in Italy; white/red in
Latvia; red in the other explicitly coloured countries. Unspecified small
territories and countries use representative presets, not a surveyed inventory.
Mixed A/B selection is stable, not rerolled when a tile loads. Country lookup
uses the actual object's position and the existing country polygons.

Paved, one-way motorway mainlines can receive estimated rails on the outside
edge of each carriageway throughout the supported European countries. The
placement follows the local driving side. Mapped barriers take precedence on
their own side, including those just across a tile boundary. Short openings
between two facing mapped terminals stay open. Ramp carriageways, crossings
and parallel bridge decks are excluded by the placement checks. Bridge and
median generation follow their own rules.

Autostrade per l'Italia reports about 4,161 km of replaced or upgraded lateral
barriers on its network. The Norwegian NVDB and Dutch Rijkswaterstaat maintain
geographic inventories. These sources have different network scopes, and the
scenery uses a deterministic default where the loaded map has no local
evidence. Some real open verges can still receive a generated rail.
[ASPI Carta dei Servizi 2026](https://www.autostrade.it/documents/10279/48940298/0163BV6_Carta_servizi_Maggio29%401-82_v2_%2B29_05.pdf?item=pedaggio),
[Norwegian NVDB](https://dataut.vegvesen.no/dataset/nasjonal-vegdatabank),
[Rijkswaterstaat inventory](https://data.overheid.nl/en/dataset/80483-geografische-areaalregistratie-geleideconstructie).

Italian motorway shoulders clear of mapped barriers can receive delineators
with a yellow reflector facing traffic. The spacing is estimated. The shared
Italian and Albanian rural model has a white body and a solid black sloped cap.

63 original steel assets cover the profiles, reflectors and sloped ends. The
reflectors fit into the middle corrugation of the steel face. The rails
have two LODs and simple rigid collision meshes. Forest instancing reuses the
existing continuous-barrier handling; these are not deformable restraint-system
simulations. A separate SOS phone asset also has two LODs and rigid collision.

The compact offline SOS snapshot has 13,447 mapped nodes across metropolitan
France and Corsica. Runtime filtering emits only compatible phones beside a
ground motorway/trunk. It never invents regularly spaced phones. Nearby steel
rails bend outward and receive two sloped ends with an access opening. The
dimensions are visual estimates. Concrete/cable and rails at another elevation
are untouched. No emergency stopping lane or lay-by is invented from a phone
node alone. Data provenance and refresh instructions are in
`server/data/fr_road_details.NOTICE.md`.

## Rebuild and validation

`tools/build_europe_guardrails.py` regenerates the 67 DAE assets for the rails,
SOS phone, plaque post and J14a beacons. `tools/build_europe_stops.py` rebuilds
the STOP faces and meshes using the local SVGs, CairoSVG, numpy and Pillow.
`tools/preview_road_corrections.py OUTPUT` renders the corrected meshes and
prints the STOP face artwork on an offline contact sheet.

The source tests cover outer-edge selection, left-hand traffic, borders,
deterministic tile ownership, mapped-barrier suppression, crossing clearance,
SOS projection and access, street names, French sign streaming, material
serialization, asset budgets and snapshot attribution. Installer tests exercise
LF/CRLF, branch additions, dry runs, backups, idempotence and rollback.
BeamNG's Windows importer, collision behaviour, visual transitions and FPS must
still be verified in game; they cannot be established by these offline tests.
