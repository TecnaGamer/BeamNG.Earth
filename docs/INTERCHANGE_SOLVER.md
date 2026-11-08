# Interchange solver: notes, known issues and roadmap

Branch `feat/interchange-refine` (yahms), on top of the merged interchange solver (#671). Everything here is
opt-in: `BNE_INTERCHANGE=1` (with #482's `BNE_JUNCTION_PAD=1`, `BNE_NEIGHBOURS=1`, `BNE_RAIL_FLAT_SEP=1` for the
full set; see "How to turn it on" below). With the switch off nothing changes, except that the roads cells (layer 10) now carry the
`maxheight_m` rule: each cell is rebuilt once (from the cached Overture rows where present). Test place: Perth, the Mitchell Freeway at Hay / Murray /
Wellington Streets (-31.9505, 115.8492).

## How to turn it on

The solver is read from environment variables when BeamNG.Earth starts, so set them in the window you start it from
(they last until that window is closed). Plans made with it on are cached apart from the default ones, so it can be
turned on and off freely.

- **Windows, the download** (`BeamNG.Earth-<version>.exe`): open a Command Prompt in the folder with the file
  (type `cmd` in Explorer's address bar there), then
  ```
  set BNE_INTERCHANGE=1
  BeamNG.Earth-<version>.exe
  ```
  In PowerShell: `$env:BNE_INTERCHANGE = "1"; .\BeamNG.Earth-<version>.exe`
- **Linux, the AppImage:** `BNE_INTERCHANGE=1 ./BeamNG.Earth-<version>-x86_64.AppImage`
- **From the source:** the same `set` (Windows) before `run_server.bat` or `python start.py`, or
  `BNE_INTERCHANGE=1 ./run_server.sh` (Linux).

For the full set (junction pads, neighbouring cells planned together, rail level crossings flattened), add
`BNE_JUNCTION_PAD=1`, `BNE_NEIGHBOURS=1` and `BNE_RAIL_FLAT_SEP=1` the same way. They cost load time (Perth: the ground
under the car about 15 s later than with `BNE_INTERCHANGE` alone).

Settings worth knowing, all optional:

| Variable | Default | What it does |
|---|---|---|
| `BNE_CARRIAGEWAYS` | `on` | `off`: crossings only, no freeway profiles |
| `BNE_MAXHEIGHT` | `on` | `off`: 6 m headroom everywhere, OSM `maxheight` ignored |
| `BNE_CW_MARGIN` | `700` | metres past the cell a freeway is solved over |
| `BNE_CW_FALLBACK` | `py` | `off`: no Python solve where the Rust kernel finds a level infeasible |
| `BNE_CW_HELP` | `on` | solve the regions a cell needs side by side, on idle workers |
| `BNE_CLEAR_AIM` | `0.5` | metres a deck aims above the minimum headroom |

### Suggested release-notes text

> **New, opt-in: interchange solver.** Freeways, their ramps and the streets crossing them are now graded together
> as one height problem, with bridge headroom from OpenStreetMap's `maxheight`, walls under freeway decks, and
> smoother freeway profiles at speed. It is off by default while it is tested. To try it on Windows, open a Command
> Prompt in the folder with the download and run `set BNE_INTERCHANGE=1` and then `BeamNG.Earth-<version>.exe`
> (Linux: `BNE_INTERCHANGE=1 ./BeamNG.Earth-<version>-x86_64.AppImage`). Around big interchanges the ground under
> the car takes about 35 % longer to load with it on (Perth CBD: 30 s to 41 s). Found something off? Comment on the
> pull request, #PR, with the place (coordinates) and a screenshot.

## What it does now

- **Carriageway profiles.** A freeway's carriageways and ramps (its "family") are solved together over a region
  (the home cell + `BNE_CW_MARGIN`, 700 m), smooth for their speed, moving up and down to fit the terrain and
  the roads crossing them.
- **Clearance (headroom).** Every deck keeps the headroom of the road under it, measured to the underside of the
  girders plus 0.15 m: OpenStreetMap's `maxheight` on that road where it has one (Overture's height restriction,
  the cells' `maxheight_m` rule; only where the tagged road is on the ground), else 6 m. A carriageway deck aims
  `BNE_CLEAR_AIM` (0.5 m) higher where nothing pulls against it. See docs/BRIDGE_CLEARANCE.md.
- **Lift before dig.** A street under a freeway deck pulls the deck up (weight `BNE_CW_PREF_W`, 10) before it is
  dug, and is dug `BNE_CW_STREET_DIG` (1.5 m) at most. The dig is judged over the street's own width, not 20 m
  of the deck either side.
- **Street decks over a freeway** clear it at its solved profile; the freeway dips under them (as far as
  `CW_MAX_DIG_M`) where the street is at its own ground level.
- **Walls under decks.** Beside a street under a freeway deck (span <= 45 m, >= 2.5 m from street to the
  deck's underside) the embankment slope is replaced by a vertical wall at the road's edge, its top kept
  under the deck.
- **Speed.** A region is solved once per process and shared between planners (Perth pair of cells 56 s -> 33 s).

Perth in game (2026-11-07): Murray and Wellington under the freeway clean, walls under the decks, the freeway's
flow over and under the street bridges smooth at 80 km/h.

## Known issues (for the PR)

1. **Ramp grades by speed.** A slip road now eases from its link limits (7 %, K 12) to a street's (10 %, K 5) over
   the last `BNE_RAMP_EASE` (60 m) before the street it meets. Still to do: stricter limits (the freeway's) as it
   leaves the freeway.
2. **Headroom over railways.** Decks over tracks keep the road headroom; real ones are often higher (overhead
   wires), and railway generation itself lays the tracks higher than expected. Left as is for now.
3. **Partly mapped height limits.** A limit is carried 30 m on along its road either side (`BNE_MAXHEIGHT_EXTEND`)
   to cover the whole underpass when it is mapped under one deck only; a street with no limit at all keeps 6 m.
4. **Banked roads merging.** Where a slip road merges into a banked carriageway, each is banked on its own, and the
   ground under the overlap follows one of them: the other's edge can be rough.

Fixed after the in-game check of 2026-11-07 (offline, to be checked in game):
- Slip roads on the ground had the freeway's limits (5 %, K 30): they were classed as "motorway", not links.
- A slip road meeting a street at grade is held to the ground at the junction itself (`BNE_CW_JUNCTION_W` 500),
  not to its smoothed ground: George Street was dug up to 3.4 m with mounds (max grade 49 % -> 18 %, no dig over
  1 m), Murray Street west of the freeway held flat 50 m then climbing 21 % (now on its ground, max 8 %).
- Junction pads (BNE_JUNCTION_PAD) skip a freeway's own merges and diverges: a flat pad there stood out of the
  low side of the banked lanes as a disc of dirt.
- The fill behind the walls under a freeway deck takes the lowest deck over it, from that deck's own profile, 0.3 m
  under its underside: under the twin decks at Murray Street the higher one's fill stood through the lower deck in
  2 m steps.
- A street under a freeway deck is dug `BNE_CW_STREET_DIG` (0.2 m) at most before the deck rises (was 1.5 m).

## Roadmap

1. Banked merges (known issue 4).
2. Ramp limits leaving the freeway (known issue 1).
3. **Interchange control points** (prototype outside the repo, `wip_tools/icp`): build the interchange as a
   frame of points from the OSM layer (crossings with their clearances, junctions shared, edges where it joins
   the network locked to the terrain), bound each point by the grade limits from the most to the least
   constrained, solve all roads jointly for the flattest profiles, then blend the terrain in with distance from
   the control points (a ground road under a crossing keeps its full hold on the terrain; digging costs more than
   lifting). Perth: all 64 crossings >= 7.4 m road over road, digs > 1 m 220 -> 60 points, in ~1.5 s for the
   box. Next: the real headroom rule and maxheight in it, the edge pins, a hump / dig report, then use it as the
   region solve's first pass.
4. Walls for decks over a freeway (Hay Street) and steep approach fills, not only streets under a freeway.
5. Batch the terrain reads per region (they are most of a region's solve time).
