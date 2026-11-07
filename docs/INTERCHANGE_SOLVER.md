# Interchange solver: notes, known issues and roadmap

Branch `feat/interchange-refine` (yahms), on top of the merged interchange solver (#671). Everything here is
opt-in: `BNE_INTERCHANGE=1` (with #482's `BNE_JUNCTION_PAD=1`, `BNE_NEIGHBOURS=1`, `BNE_RAIL_FLAT_SEP=1` for the
full set). With the switch off nothing changes, except that the roads cells (layer 9) now carry the
`maxheight_m` rule: each cell is rebuilt once (from the cached Overture rows where present). Test place: Perth, the Mitchell Freeway at Hay / Murray /
Wellington Streets (-31.9505, 115.8492).

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

1. **Ramp grades by speed.** On- and off-ramps use one grade limit along their length. They should allow more
   grade change near the slow road they join and less as they leave the freeway (a design speed falling along
   the ramp).
2. **Headroom over railways.** Decks over tracks keep the road headroom; real ones are often higher (overhead
   wires), and railway generation itself lays the tracks higher than expected. Left as is for now.
3. **Untagged streets keep 6 m.** Where `maxheight` is mapped on only part of a street (Murray Street under the
   east carriageway), the untagged part needs 6 m and is dug up to ~0.6 m.
4. Being worked on (Perth, in game 2026-11-07):
   - a slight step where a freeway deck meets the terrain;
   - George Street between Wellington and Murray rises slightly and falls back before the Murray intersection;
   - George Street from Murray to Hay: a lump in the Murray intersection, a dip, then steep mounds up to a car
     park entrance, lumpy after;
   - Murray Street west of the freeway held flat for ~50 m, then rising sharply (the natural climb is gentler).

## Roadmap

1. Fix the anomalies above (item 4).
2. Ramp grade limits by design speed along the ramp (known issue 1).
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
