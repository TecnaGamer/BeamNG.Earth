# German Autobahn guide signs: announcement, advance and direction signs at exits and interchanges (design)

Date: 2026-11-08. Status: design, for the user's approval. Branch: `feature/de-autobahn-signs` (fork
Yukuhu/BeamNG.Earth), from upstream `dev` d5941c7c, one PR to upstream `dev`. File references are to that commit.

Part of the series that makes Germany look German (the series and its order: `2026-11-06-german-junctions-design.md`).
The user brought this project forward from the series' project 4 ("Autobahn exit signs") on 2026-11-08.

## Intent

The user, driving the game's Autobahn: "there simply are no overhead signs, especially on interchanges and exits,
this ruins immersion." The user's standard: the signs a German driver expects, with the right information on them,
where German rules put them.

Why the game shows none today:
- **The highway guide signs (PR #363) are US-only.** They are exit panels, overhead gantries and gore signs.
  `signstream.build_tile` builds them only where `signstates.states` finds a US state (or France with
  `BNE_EU_ROAD_DETAILS`); `test_highway_signs.py` pins that.
- **The direction boards (PR #754, on by default) are the only boards near German motorways.** They stand beside the
  road about 1000 m before diverges, merges and inside interchanges. Their content is inferred: Overture places in
  a 30 degree cone, distances and an English line under city names. They have no junction numbers and no exit names.
  Round Barbing they put 14 such boards in 7 x 5 km.

What makes a faithful version possible: German mappers record the sign text itself in OpenStreetMap
(`destination` = the text on the sign, in sign order; wiki `DE:Key:destination`), and they do it densely:
- Germany-wide, 97 % of the 6,528 `motorway_junction` nodes have a name, and about 94 % of the real exits (rest areas
  excluded) have a number.
- 87 % of exit links have `destination`.
- 77-86 % of exits have the 500 m advance sign's content as `destination:lanes` on the carriageway before them.
- `turn:lanes` (the lane arrows) is mapped before 96-100 % of exits.

Overpass, 2026-11-08, samples round Regensburg, Ingolstadt and the Ruhr. The weekly sign shards already carry these
exits, planet-wide, but keep only `{ref, names[:3], routes[:3]}` per exit.

The real signs at the user's places were read from open street-level imagery and compared with OSM: in 22 of 26 sign
and OSM pairs, the same places appear in the same order. The places are the A3 at Neutraubling and Regensburg-Ost,
Kreuz Regensburg, Dreieck Holledau and the A9 at Lenting and Ingolstadt-Nord. The imagery is KartaView sequences
1371954 (2019) and 7991305 (2023), CC BY-SA 4.0, and Panoramax 889978ac and bb565635 (2025).

## Benchmarks

The places the user knows and will judge:
- **The A3 east of Regensburg**, the exits Neutraubling (102, nearest Barbing), Regensburg-Ost (101) and Rosenhof
  (103). The stretch was widened to three lanes and resigned overhead about 2023.
- **Kreuz Regensburg** (A3 junction 99 / A93 junction 44), from all four directions.
- **The A9 at Ingolstadt** (Lenting 60, Ingolstadt-Nord 61, Ingolstadt-Süd 62) and **Dreieck Holledau** (A9 65 / A93
  55), on the user's route Schrobenhausen to Ingolstadt.

Every check starts from the real place: the photos above, `tools/streetview.py` when a Mapillary token is available,
and `tools/ortho_hi.py`.

## Goals

- Every exit, Autobahnkreuz and Autobahndreieck on a German Autobahn gets the German chain of guide signs, with the
  text that is posted there (as mapped in OSM).
- Overhead where German rules put signs overhead; beside the road elsewhere.
- Faithful look: blue panels, white DIN-style lettering, the A / B / E number plates, the junction number ring,
  pictograms, lane arrows, round distances, grey gantries.

## Non-goals (in this project)

- **Autobahn-like Bundesstraßen** (the same chain in black on yellow, RWB instead of RWBA). They come later, as their
  own step.
- **Signs whose content is not in OSM:**
  - the Entfernungstafel (Z 453, km to the Fernziele);
  - Autohof boards (Z 448.1);
  - Bedarfsumleitung plates ("U 57");
  - the 2000 m "Kreuz ... 3 km" double announcements that only stack text.
- **Rest areas** (Parkplatz, Rastplatz, Raststätte announcements).
- **The blue Autobahn start and end signs** (Z 330.1 / 330.2): open PR #882 (Robitnik, road signs) has them.
- **Signs leading to the Autobahn on ordinary roads** (Z 430 / 440, yellow with blue inserts).
- Variable message signs, LED gantries, lane control signals.
- Any country other than Germany.

## Anti-goals (a failure even if it works)

- **A guessed place on a sign.** A German driver spots a wrong Fernziel at once. Text comes from what is mapped for that
  junction; where nothing is mapped, the board is left out, never filled from nearest towns (the existing guide signs
  already refuse city guesses: `exitsigns.py`, `throughsigns.py` docstring).
- **Any change outside Germany.** Sign tiles, direction boards and the weekly shards outside Germany stay
  byte-identical, and a test shows it.
- **Two boards for the same junction saying different things:** the direction boards must step back where these
  signs stand.
- **More memory with every kilometre driven, or noticeably longer loading, on the Steam Deck** (aim: 30 fps on the
  Handheld preset). A texture or Material per distinct board, as the direction boards do (never freed in a session),
  is ruled out.
- **Code for particular places.** Benchmarks are for checking; every rule is general.
- **Gantries in the way:** standing on bridge decks, in tunnels, on the carriageway or through other roads.

## Constraints (project rules)

As in the junctions design ("Constraints (project rules)"). In short:
- **Code placement:**
  - New server logic in Rust (`native/earthfast`), Python as thin glue, the mod in Lua.
  - A signature change bumps `m.add("VERSION", n)`, `fast.require(n, ...)` and `fast.MIN_VERSION`.
  - Commit the Rust source, not the built `.so` / `.pyd`.
- **Citations:** rules cite their source (StVO Anlage 3, VwV-StVO, RWBA, VzKat, ZTV-ING), as `markings.py` and
  `speedsigns.rs` do.
- **Gating and data:**
  - German output gated on exactly `"DE"`, by real borders (`countrybounds`, PR #505, in `dev`), not the nearest
    airport.
  - Data and artwork licensable for the repo, credited in CREDITS.md; README updated.
- **Format numbers:** a change to grading plans or road facts bumps their format numbers. Sign tiles are built
  separately (`signstream.py`); the builder confirms these signs stay out of facts and plans.
- **Quality gate:** `tools/quality_gate.py` passes; one PR to `dev` (one PR per feature).
- **Overlap with open PRs:** #878 (German junctions), #892 (town signs) and #882 / #883 (Robitnik) change
  `signstream.build_tile` / `KINDS` or the direction boards. The branch merges with whichever lands first; kernel
  VERSION numbers are renumbered by whichever PR lands later.

## Decided by the user (2026-11-08)

1. **Overhead or side by the German rule:** overhead on carriageways with more than two lanes and at every Kreuz and
   Dreieck; beside the road on two-lane carriageways. This is how the real A3 was signed before and after its
   widening.
2. **Autobahnen only** (`highway=motorway`) in this version.
3. **On by default**, switched by the existing Settings > World > Highway signs.
4. **The look of the rebuilt Bavarian A3 / A9** (the 2023-2025 photos): portal gantries with two panels, a
   cantilever for the 1000 m board at an exit.
5. **The junction name is printed as mapped** ("Kreuz Regensburg", "Dreieck Holledau"), chosen on the mockup screen.
6. **The countdown boards (Z 450) as in the current StVO drawings** 450-50 / 51 / 52 (blue, distance printed on top,
   white bars rising to the right). The user corrected the first mockup.
7. Mockups (local, not in the repo: `.superpowers/sdd/2026-11-08-de-autobahn-signs/mockups/preview/` `exit.png` /
   `kreuz.png` with their HTML, and the analysis `analysis.md` beside `mockups/`; they are the visual reference for
   the build):
   - **The exit chain** at A3 Neutraubling (102, eastbound) was accepted ("the rest looks good" after the Z 450 fix).
   - **The Kreuz chain** at Kreuz Regensburg (A3 westbound) was shown: lane arrows, plates, two-panel gantries, fork
     boards on the ramp, no Z 333. The user answered its naming question (decision 5) and flagged nothing else.
8. Accepted as stated (the builder's calls the user heard and did not object to):
   - Dense junctions: boards move forward with a round shortened distance.
   - A board only where its text is mapped.
   - OSM typos printed as mapped.
   - The non-goals above.
   - Grey gantries.
   - The direction boards step back on German Autobahns.
   - The signs appear for players with the first weekly data run after the merge.
   - Text from a font atlas: one fixed texture set instead of a picture per board (after the builder check: the
     German boards' own atlas, see F).

## Decided: the design

### A. Where it applies

- **Gate:** the sign tile is checked by real borders (`countrybounds.bundled().codes_at`, the French path's pattern in
  `signstream.build_tile`), and each assembly is kept only if its own position is in `"DE"` (as `highwaysigns.build_tile`
  filters French assemblies by `euguardrails.country_at`). Tiles without Germany run exactly as today.
- **Roads:** junctions on `highway=motorway` main carriageways. A `motorway_junction` node whose link leaves to a
  `motorway` / `motorway_link` chain that joins another motorway is a Kreuz or Dreieck. The node name ("Kreuz ...",
  "Dreieck ...", or an adjective form such as "Kamener Kreuz") confirms it, and so does `destination:ref` naming
  another A-number. Every other exit is an Anschlussstelle. Junction nodes named as rest areas (Rastplatz, Raststätte,
  Rasthof, Parkplatz, Tank- und Rast, Autohof), or rest-area links with `noref=yes` and no destination, are skipped.
  The builder settles the exact classifier and tests it on the benchmarks.
- **Setting:**
  - Under `highwaySigns` (Settings > World > Highway signs, on by default), like the US guide signs.
  - Plus an environment override for testing, `BNE_AUTOBAHN_SIGNS=on|off`, as the series' other features have.
  - The mod side toggles the new tile field with the others in `roads.lua`.

### B. Data: what the weekly sign shards must carry

`tools/build_sign_tags.py` (run weekly over the planet by the data repo's Action; format in `server/signshards.py`) keeps
every motorway junction and motorway/trunk(+link) way already. What it drops for German signs must be added to the
exit record's `d`, **only for records inside Germany**, so shards elsewhere stay byte-identical:

| Field | From | For |
|---|---|---|
| junction `ref`, `name` raw (both kept even when destinations exist) | the junction node | Z 448 |
| the full positional families of the exit link chain up to its first fork: `destination`, `destination:ref`, `:to`, `:ref:to`, `:symbol`, `:colour`, `:country`, `:arrow`; and per fork branch | link ways | Wegweiser at the exit; Kreuz fork boards |
| the approach's per-lane families: `destination:lanes`, `destination:ref:lanes`, `:symbol:lanes`, `:colour:lanes`, `:country:lanes`, `turn:lanes`, `lanes`, from the carriageway way ending at the junction (or the last way within 1 km that has them) | approach ways | Z 449 panels and lane arrows |
| the straight-on way's `destination` family; the mainline `ref`, `int_ref`, `lanes` | through way | through panels |
| the next junction downstream on the same carriageway: its name and ref | junction chain | through-panel fallback |
| the previous junction upstream on the same carriageway: where its entry merges (the end of its merge lane) | junction chain | dense junctions (C) |
| a longer approach: at least 2.3 km back from the junction (the 2000 m Kreuz station measured from 125 m before the gore, plus margin), against 1.8 km today (`exitsigns.APPROACH_M`); German records only | approach ways | the 2000 m and 1000 m stations |

No cap on the number of destinations.

**Where the shard-side code lives (decided after the builder check):**
- The data repo's weekly job (TecnaGamer/BeamNG.Earth-data, `.github/workflows/rail.yml`, job `signs`) downloads a
  fixed list of files from `dev`: `tools/build_sign_tags.py`, `tools/build_rail_tags.py`, `server/exitsigns.py`,
  `server/throughsigns.py`, `server/milemarkers.py`, `server/routeshields.py` and `server/signshards.py`. It runs
  them with numpy only: no native module, no downloaded data.
- So the German extraction lives in those files. No new module or data file is imported there, because that would
  break the weekly run until the data repo changes.
- "Inside Germany" is decided by a simplified German outline written inline in `signshards.py` (pure numpy
  point-in-polygon), not by `countrybounds`. PR #878's `GERMANY_BOX` would add the fields to Czech, Austrian and other
  border records, and change two z7 shards with no German territory (69/43, 69/44).
- The outline keeps every German motorway junction inside it. The builder checks that against the bundled
  `countrybounds` border data in a test, and checks that no z7 shard without German territory changes.
- The job has a 340-minute limit. Its time and peak memory with the change are measured on a Germany extract (and the
  growth of the kept tag sets estimated for the planet) before the PR.

**Reading rules** (from the measured data; each gets a test):
- Positional lists keep empty slots. In `Regensburg-Ost;;Landshut` the empty slot carries the ship and GVZ pictograms
  in `:symbol`. Empty slots are dropped only when printing.
- A single `:country` / `:symbol` value is never broadcast to a list of several names (way 4077228: `country=A` belongs
  to Passau only). On a length mismatch the entries stay unmarked.
- Never split on an internal " / " ("Ingolstadt-Nord / Eichstätt" is one target). `|` separates lanes, `;` entries.
- A junction `ref` may hold two numbers ("8;7") or a letter ("100a").
- White inserts (`destination:colour=white`) print black on a white field inside the blue panel.

**Old data:** the reader treats a shard without these fields as "no German data". Then nothing changes against today:
no Autobahn signs, and the direction boards stay as they are. Players get the signs with the first weekly run after
the merge. The in-game review uses locally built shards (the de-townsigns proxy setup).

**What each board takes, and what happens when it is missing:**

| Board | Source | If missing |
|---|---|---|
| Z 448 announcement | node `name` + `ref` | no name: no board; `noref=yes`: name without ring |
| Z 449 advance, through part | through lanes of the approach's `destination:lanes` | the straight-on way's `destination`; else the next junction's name (the RWBA 2000 rule "next Anschlussstelle", seen on the real westbound A3); else no through panel (the exit panel stands alone) |
| Z 449 / Wegweiser, exit part | exit lanes of `destination:lanes` | the link chain's `destination`; else the junction name (allowed by the DE tagging convention when no Wegweiser exists); else no exit panel (the Z 333 stays) |
| Kreuz fork boards | the fork branches' `destination` | no fork boards |

OSM errors ("Gaimershaim", "Kehlheim") are printed as mapped; the fix belongs in OSM.

### C. The chain of signs

Distances are measured back from the **reference point, 125 m before the painted gore** (Hessen LWBA 4.2.1, "im
Regelfall"; the federal definition is in the unread RWBA 2023 4.2.1). The gore comes from the existing gore search
(`exitsigns` gore helpers). Sources: StVO Anlage 3 (Zeichen 333, 406, 448, 449, 450), the pre-2009 § 42 Abs. 8 StVO
distances, VzKat 2017 examples, BASt dWiSta 2022 figure 14, and the Hessen Regelpläne 1/2/5/6.

| Junction | Station | Sign | Overhead (> 2 lanes) | Side (2 lanes) |
|---|---|---|---|---|
| Anschlussstelle | 1000 m | Z 448: exit pictogram, ring with the number, name (two lines with a leading hyphen when the name has a district suffix: "Regensburg" / "-Ost"), "1000 m" | cantilever from the right verge, panel centred over the right lane | two posts |
| | 500 m | Z 449 | portal; through panel over the through lanes, exit panel to the right of the right edge line (RWBA 2023, ARS 15/2024 III) with "500 m" | diagram board: straight stem with the through text, branch arrow to the right with the exit text, "500 m" |
| | 300 / 200 / 100 m | Z 450 boards 450-52 / 51 / 50 | right verge, own post | same |
| | 0 m | Wegweiser (Z 332) | portal; through panel as at 500 m, exit panel over the exit lane, diagonal arrow, no distance | two posts, exit text and diagonal arrow |
| | gore | Z 333 "Ausfahrt" | in the gore | same |
| Kreuz / Dreieck | 2000 m | Z 448: crossing pictogram, ring with the junction's number on this Autobahn (99 on the A3, 44 on the A93 for Kreuz Regensburg), the name as mapped, "2000 m" | portal, panel centred over the carriageway | two posts |
| | 1000 m, 500 m | Z 449 | portal; through panel over the through lanes; one panel per exit direction over its lanes (curved arrow, the other Autobahn's plate, the distance) | diagram board with one branch per direction |
| | 300 / 200 / 100 m | Z 450 | as above | |
| | split | Wegweiser | portal; through panel; exit panel with the diagonal arrow left of its lines and its plate centred in a bottom row (the A93 gantries, 2023; the 2019 A3 gantry had the plate at the top) | two posts |
| | ramp fork | Gabelungswegweiser | two ground boards in the fork, one per branch, arrow from the branch's direction | |
| | gore | none | | |

**Overhead or side**, per junction and per station: overhead when the main carriageway has more than two lanes at that
station (`lanes`; the exit lane not counted), and always at a Kreuz or Dreieck. Otherwise side, with two posts 1.5 m
from the edge and the board bottom 2 m up (VwV-StVO general rule).

**Dense junctions:** when a station falls behind the previous junction's merge, it moves forward to the first spot
at least 100 m after that merge. Its printed distance is the true one rounded down to 50 m, as the A93 boards at Kreuz
Regensburg read 400 m and 250 m. A station that would come within 150 m of the next station is dropped, the one
nearer the junction kept. Boards of two junctions never share a structure in this version.

**Station spots:** like the existing stations (`exitsigns.place`), along the walked approach. They avoid bridge decks
and tunnels (moved upstream to the first clear spot, as `exitsigns._on_deck` keeps ground signs off decks) and overlaps
with other assemblies (`signlayout.arrange`).

### D. What the panels look like

| Element | Rule | Source |
|---|---|---|
| Lettering | DIN 1451 Mittelschrift look: mixed case, post-1981 glyphs (straight 6 / 9, t with a hooked foot); cap height 420 mm overhead, 350 mm side; line pitch cap + 4/7 cap | VwV-StVO zu §§ 39-43 Rn. 19; BASt dWiSta 2022 Abb. 22-24 |
| Font | DINish Medium (OFL 1.1) and a condensed face, in the German boards' own atlas (see F); OFL.txt shipped with it | measured closest weight and glyph era among open fonts |
| Panel | blue, white rim, rounded corners; text block centred, lines left-aligned in their block (as the photos) | photos; Commons Z 449 drawing (1976 grid) |
| Through panel | its lines on top; at the bottom one short up-arrow centred over each lane it serves, the A-plate between the arrows | photos A3 2023, A9 2025; dWiSta |
| Exit panel | its lines; bottom row with the arrow (diagonal at an exit and at the split, curved on a Kreuz Vorwegweiser), B- or A-plate, distance | photos |
| Line packing | two short names may share a line ("Hof  Weiden", "Linz (A) Passau") when the line fits the panel; otherwise one per line. A district suffix takes its own line ("Regensburg" / "-Ost") | photos |
| Plates | A (Z 405): gabled outline, white on blue, digits 0.75 cap; B (Z 401): black on yellow; E (Z 410): white on green | Commons drawings (PD) |
| Number ring (Z 406) | white ring on blue, circle for 1-2 characters, stadium for 3 or more | StVO Anl. 3; dWiSta 5.4 |
| Country codes | white oval with the code after the name ("Linz (A)") | photos |
| White inserts | black text on a white field ("GVZ", "-Zentrum", "Audi") | `destination:colour`; photos |
| Pictograms | those that occur in German `destination:symbol` (airport, harbour / ferry, industrial, train station, rail freight terminal, centre, and the like), drawn from the public-domain RWBA 2000 set on Wikimedia Commons; unknown symbols are left out, no placeholder | Commons category "RWBA 2000" (PD-VzKat / PD-GermanGov) |
| Too long | a line wider than the panel may be: the panel grows up to the width of the lanes it serves, then the line uses the condensed face. No abbreviations | builder's call (real narrow panels abbreviate, "R.-Ost"; OSM has the full names) |
| Colours | sRGB chosen against the photos (published RAL conversions disagree: #005B8C vs #154889), one value each for blue, white, yellow, green | builder's call |

### E. Structures

| Structure | Rule | Source |
|---|---|---|
| Portal (Schilderbrücke) | two posts outside the carriageway, behind the barrier, on rounded concrete plinths; a box girder spanning the carriageway, a walkway with railing on top; grey | ZTV-ING (now Teil 8 Abschnitt 3, 2025-08); photos |
| Cantilever (Kragarm) | one post on the right verge, arm over the right lane(s), at most 12 m reach; grey | ZTV-ING ("soll 12 m nicht überschreiten") |
| Clearance | panel bottom at least 5.0 m above the carriageway | ZTV-ING |
| Spans | one carriageway each (as the existing gantries) | photos |
| Side boards | two posts, 1.5 m from the edge, bottom 2 m | VwV-StVO |
| Z 450 | own post on the right verge | StVO; photo |
| Z 333 | low, in the gore | StVO |

### F. Rendering and performance

- Render-only meshes in the sign tiles, one batch per tile, as the existing guide signs.
- **Text from the German boards' own atlas** (decided after the builder check):
  - It holds DINish, its condensed face and the pictograms.
  - It has one fixed material pair, built by the same tooling as the shared atlas (`tools/build_town_fonts.py`
    pattern; `server/signmesh.py` metrics).
  - No texture or Material per board.
  - The shared atlas (`sign_font.json`, 4096 x 2048, full to row 1936) stays untouched. Adding to it would move every
    glyph and double its size, which would change French guide-sign tiles and town-sign cache keys
    (`font_signature` in `poleprofiles.py`) and add memory for every player.
  - The builder checks in game that the new textures are loaded only where German boards are drawn.
- Gantries and posts reuse the guide-sign metal material.
- **Cost per board is vertices only:**
  - a US exit with a gantry is about 4,700 vertices today;
  - the regional sign tile budget is 60,000 vertices (`signmesh.py`).
- **The Rust kernel** (`native/earthfast/src/deautobahn.rs`) holds the rules above with their citations:
  - stations and dense-junction handling;
  - overhead or side;
  - panel layout;
  - structures;
  - the mesh.
- **The Python glue** (`server/deautobahn.py`):
  - reads the shard records;
  - reuses the existing approach, gore and pavement helpers (`exitsigns`, `signlayout`, `signroads`) as the French path
    does;
  - hands arrays to the kernel.
- **Measured before the PR:**
  - Tile build time on the Regensburg and Ingolstadt tiles.
  - Vertex counts per junction.
  - An fps A/B driving the A3 Rosenhof to Kreuz Regensburg with the setting on and off (the maintainer has taken
    features off by default after an A/B before).

### G. Direction boards step back

Where German Autobahn signs are built (a tile in Germany whose shards carry the new fields), `dirsigns` drops its
boards that stand on `highway=motorway` in Germany. Boards of the kernel's `freeway` kind on trunk roads stay
(`dirsigns.rs` counts rural one-way trunks as `freeway` too), because Autobahn-like Bundesstraßen get no new signs in
this version (decided after the builder check). Rural and urban boards stay. With old shards nothing changes.

The test is on the board's road point (`b[4:6]` in the `dirsigns.py` board loop) against the road class, done on the
Python side, so the `dirsign_place` kernel and its VERSION are untouched; open PR #883 changes that kernel.

## Verification

- **Tests (Python and `cargo test`):**
  - **Reading rules:** positional lists with empty slots, no broadcasting, " / " inside names, "8;7" refs.
  - **The classifier:** exit, Kreuz, Dreieck and rest area.
  - **Each fallback** in B.
  - **The station table:** including dense junctions and decks.
  - **Overhead or side** by lane count.
  - **Line packing.**
- **Golden content for the benchmark junctions**, from small frozen tag fixtures (ODbL, attributed):
  - Neutraubling both ways;
  - Kreuz Regensburg from the A3 and the A93;
  - Lenting and Ingolstadt-Nord;
  - Dreieck Holledau.

  For each sign, its text, plates, arrows and mounting, checked against the photos listed in Intent.
- **Outside Germany byte-identical:** sign tiles of a US, a French and a Czech sample tile; the weekly shard of a
  non-German z7 tile.
- **In game** (BeamNG.drive on this PC, locally built shards):
  - drive the benchmarks;
  - screenshots next to the photos;
  - `get_instability` 0, no errors in the log;
  - the fps A/B from F.
- `tools/quality_gate.py`.

## Left to the builder

- The exact Kreuz / Dreieck / rest-area classifier and how it handles odd mappings.
- Where the new shard fields go in `d`, and whether `signshards.FORMAT` stays 1 (additive fields) or is bumped. The
  constraint is that old readers and other countries are unaffected.
- Panel dimensions within the rules in D, the condensed-face threshold, colour values from the photos.
- Gantry geometry details (girder section, walkway, plinths) within E.
- The kernel's interface and how much of the placement stays in the existing Python helpers versus moves into Rust.
  Logic that is new to this feature goes in Rust.
- The pictogram list (from the German `destination:symbol` counts) and their atlas rendering.
- Whether the fork boards need their own placement or reuse the gore search on the ramp.

## For the maintainer to decide (in the PR)

- On by default under Highway signs (the user's choice); the maintainer may prefer opt-in after the fps A/B.
- The weekly shard builder change (Germany only, inside the files the data repo's job already fetches) and the new
  German sign atlas (generated PNGs, DINish under OFL 1.1, RWBA pictograms in the public domain).
- The direction boards stepping back on German motorways.
