# French motorway signs: composition and supports

The initial correction resized faces independently after support planning. A
short blue face could sit below the beam without a connection; route-only
fallbacks also produced nearly empty square A6 boards. The new compositor fixes
the underlying geometry/layout, rather than only increasing sign frequency.

## Composition and placement

`frhighwaysigns` supplies the same measured envelopes and lateral offsets to
placement, overlap handling and rendering. Blue through panels have downward
lane arrows; exits have a numbered SE2-style header and downward exit arrow,
with compact separate colour registers. Cartouches and S-itinerary insets are
separate backed panels. Rear rails/clamps connect every register to the beam,
including a short face beside a taller stack. Posts follow sampled ground.

Advance boards remain on the right shoulder at 1000/500 m when the source
approach permits, independently of the mapped branch side. Branch arrows and
overhead panels still use that actual exit direction. Sloping ground is sampled
at each foot: the panel stays at least 0.45 m above the highest foot and 2.2 m
above the road, with posts embedded 0.22 m. Longitudinal overlap moves preserve
the road-height reference and the right-hand mounting side.
Motorway decision assemblies try 35/75/130/200 m before the split when a bridge
or support conflict blocks a nearer station. Failed overhead planning retains
roadside information. Asymmetric French panel bounds determine support spans;
reviewed cantilevers use one right-hand column. Pavement/deck exclusions, the
existing six-metre underside clearance and one-owner-per-tile rule remain.
Explicit kilometre distances can produce a blue D61-style roadside confirmation
without lane arrows. Unknown distances and through destinations are not guessed;
a route number alone no longer becomes an empty blue destination board.

## Data and reviewed examples

Optional exact-OSM-ID lookup now reads posted mainline destinations/international
references without fetching geometry; failures are cached/throttled. Lane
ambiguity or conflicting references do not become through-city claims. New
shards retain extra French names/distances while keeping the legacy three-name
contract used by other renderers.

`fr_motorway_reference_signs.json` transcribes the supplied Street View examples
at A6 exit 26 northbound, A4 exit 18 westbound and A355 exit 2 northeastbound.
Matching requires exit number, local junction position and incoming direction;
opposite carriageways and distant same-numbered exits are unaffected. Elsewhere,
explicit colour registers take precedence. Other exits split exact destination
matches from the official 2025 green-pole list into a green register above local
white names. Exit branches explicitly identified as motorways retain blue.
Green-pole status is not determined by prefecture/subprefecture status alone,
and this fallback cannot reconstruct every signed itinerary: mapped or reviewed
colours override it. See `server/data/fr_green_poles.NOTICE.md` for provenance.
No extra destination is invented when it is absent from the source labels.

The supplied SVG is a visual reference for proportions/lane-arrow arrangements,
not redistributed artwork. Junction coordinates derive from the 2026-11-07 OSM
shards: (c) OpenStreetMap contributors, ODbL 1.0. General design reference:
Cerema IISR part 5, consolidated 2024 text:
https://equipementsdelaroute.cerema.fr/IMG/pdf/2025_09_03_iisr_5epartie_vc_20240315_relu_jcb_cle28888d.pdf

## Lettering and limits

French blue/green materials are separate from the regional palette. The shared
atlas preserves L1 town-letter metrics and adds a lighter raster illustration
derived from the bundled OFL Typoroute L1 source. It is not an official L2 font;
secondary italic text is a geometric slant, not an official L4 font. The source
TTF is unmodified; its attribution/licence remains bundled. See
`tools/fonts/town/Typoroute-SOURCE.md` and `NOTICE.md`. Install the atlas and its
JSON metrics together; `start.py` redeploys changed mod assets on the next launch.

Unicode superscript letters in destination names (for example `Clermont-Fᵈ`)
reuse ordinary atlas glyphs, reduced and raised in the mesh. Their measured
envelope follows the same shaping, so a missing modifier-letter glyph no longer
renders as `?`; no shared atlas repack is needed.

Supports/stations remain estimated game scenery, not surveyed reproductions or
certified manufacturing drawings. Regional activation, town signs, roads,
physics, fences and plate development are not changed by this correction.

## Validation

126 Python tests passed in 14 modules; one optional pyosmium builder test was
skipped. The infrastructure module separately passed 25 tests. Four Lua stub
modules passed 251 assertions. Checks cover front-facing arrow heads, downward
exit arrows, header depth, unequal-stack attachment, supports/envelopes,
cantilevers, bridge fallback, confirmation placement, tile ownership, duplicates,
country isolation, metadata failures and texture/atlas assets.

`python tools/preview_fr_motorway_signs.py` reproduces seven front/rear previews
from production placement, triangles, UVs and atlas on representative test roads.
They were visually inspected. BeamNG is unavailable here; the installed result
still needs an in-game check at the supplied locations.

### Right shoulder and destination-register follow-up

`test_fr_exit_legends` adds coverage for both exit-36 source labels, raised D
UVs/alignment, green-pole aliases and exact matches, explicit-colour precedence,
actual left-branch arrows with right-side announcements, independently grounded
feet on a bank, and collision moves preserving the road-height reference.
`tools/preview_fr_exit_corrections.py` renders the production face and a synthetic
sloping-road cross-section. These previews are not BeamNG captures.
