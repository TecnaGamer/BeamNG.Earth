# 3D solve: design constraints

The rules the 3D solve (`server/grade3d`) is held to. Check every change against this page before writing it. A change that needs a rule here to move gets the user's decision first and an update here, never a quiet exception in code.

Where a rule was decided is given in brackets: an issue number, or the user's own words.

## 1. Goal

A drivable world: every block of road has heights, and none of them contradicts the map. A block with no answer leaves its roads with no heights, so the game shows no roads there. That is a failure, not an acceptable outcome. The user: "you're responsible to give me a driveable world" (#135).

## 2. Process rules

These apply to every change.

- **The data is read right; the laws are never eased.** When a block has no answer, the cause is how the input was built from the map, until shown otherwise. The user: "no special cases this isn't that complicated we just need to be careful with how data is processed" (#117).
- **Contradictions are reported, never relaxed.** No penalty in place of a hard law, no clearance dropped because it does not fit, no retry with looser laws (#119).
- **No special cases.** No rule keyed on a place, a way id, a road name or a single tag combination seen at one site. A rule says what a kind of map data means, and holds everywhere.
- **No fallbacks, no alternatives, no switches, no second path.** One solve. If it has no answer, it stops and names the contradiction. The user: "I specifically asked no alternatives and no fallbacks" (#119).
- **No duplicate or prototype files** in the repository, and no work kept in side folders. Changes go in place, in a worktree, through a pull request.
- **Every rule is measured** before it merges:
  - on the failing blocks (their irreducible core, `core_classify`);
  - on drivability against the baseline;
  - on the junction eval.

  A rule that does not change the result is not merged.

## 3. What is known and hard

The map gives these; the solve must meet every one exactly (#119).

1. **Joins.** Roads the map joins meet at one height. A join is a map node both roads share, at that node's place (#119), never two lines merely lying near each other.
   - The node's place is read from where the lines meet: the end of one on the other nearest the node, within 6 m (`NODE_M`). The node positions come from the map's connector fractions, which can place one node metres apart on two short ways (#135).
   - Any node of both counts, not only the one nearest the end. Example: Barcelona's bus depot, where an aisle meets a ramp loop at its second-to-last node, 0.45 m from its end.
2. **Clearances.** Where two roads cross at different levels, the upper clears the lower by at least its headroom plus the upper's structure (`gradeshapes.clearance_need`):
   - The headroom is the lower road's mapped `maxheight` where it has one. A car park aisle (`service=parking_aisle`, Overture's subclass `parking_aisle`) without a sign has a car's 2.3 m (the user, #135: car parks are signed 2.0–2.3 m). Otherwise 4.0 m for a road and 6.0 m for a track. Each gets a 0.15 m margin on top.
   - The structure is a deck's depth by its span, a rail deck's 1.68 m, or a tunnel's 1.0 m cover.
3. **Grading laws.** Each road keeps its class's maximum grade and minimum vertical curve. A major road's vertical curve is the one it is driven over at its class's driving speed (`laws.DRIVE_KMH`, at most 0.2 g of lift). Each stretch is held to the laws of the road it is on, not to those of the fastest road its path joins (#133).

## 4. What is reconstructed

**The earthwork** is each road's height off the terrain (its cut or fill). The heightmap does not show it; it is rebuilt from the constraints above (#119).

**The cost:** least earthwork. It is linear (|z − terrain| × length × width), weighted by (speed / 50 km/h)². The user: "the higher the speed, the least it should move" (#119).
- Decks and tunnels cost nothing.
- Stacked samples cost nothing: where two lines cross at two levels, the heightmap has one height and does not say whose.

**Away from structures a road lies on the ground.** The user: "earthworks are to meet non ground layers" (#119).

## 5. How the map is read

Each rule below reads a kind of map data; none is tied to a place.

### Structures

- **A bridge or tunnel is the map's flag** (`bridge=*` / `tunnel=*`, Overture's `is_bridge` / `is_tunnel`).
- **A `layer` or `level` with no bridge or tunnel tag is an order for drawing, not a structure.** The road stays on the ground (#135). Examples:
  - a tram along its street at `layer=1` (Birmingham's Bull Street);
  - car park and depot floors (Barcelona, Perth);
  - approaches in cuttings or on embankments.

  **Not in the code yet.** Today a level alone still makes a deck (above 0) or a tunnel (below 0). Reading it as drawing order solves Barcelona's depot, Perth's car park and Birmingham (1,−8), but it leaves 2–5 m ledges where the S1's tunnels at Rautenweg and Breitenleer open onto their cuttings, which are mapped `layer=-1` with no tunnel tag. It merges once those portals are smooth.
- **The levels order stacked structures of one kind:** decks by their level, tunnels by theirs (`gradeshapes._over`).

### Pieces and paths

- **Each piece of a road is cut by its ranges** (ground, deck, tunnel) and holds only the map nodes on its own stretch (#133).
- **Two pieces of one road at different levels meet end to end only,** never one's end on the other's middle (#133). Examples: a loop ramp over its own start (Pregerson); a spiral (Kawazu).
- **A link's ground part is a link,** like its deck and tunnel (#133).

### Crossings

**A crossing with no clearance** is one of these (`layers.py`):
- **"grade":** the two are at one level and the map has no node of both there, so it says nothing of their heights.
- **"apart":** two tunnels at one level; the map does not say which is under.
- **"merge":** ordered by levels alone, within 20 m of a path's end. This is a ramp merging, mapped as crossing.
- **"open":** within 20 m of where a road ends that the map joins to nothing driven (no road, only a footpath or nothing; for a track, no track) (#135, the user). The road goes on off the map there, and the map says nothing of its height. Example: La Défense's car park spiral rises from a node only a footway touches, its car park not mapped, and 2 m on the map has it over the Tour Légende's delivery tunnel.
  - An end outside the area the block read may go on to what was not read, so it is not one.
- **"joined":** no route between the two places, through the map's joins, can part them by the clearance (#135). Examples: a raceway's deck crossing its own ground approach 14 m from where they meet (Vienna's Südtiroler Platz); a service tunnel at La Défense passing 5.25 m under a junction 9 m from the node it starts at. Along a route, a stretch climbs at most its length × its steepest member's grade. Across a join it climbs nothing.
  - The map then says both "they meet" and "one passes over the other". Its joins (topology, which every router reads) outrank its layer (drawing order).
  - The search is bounded by the clearance divided by the grade: about 40–160 m for roads and about 250 m for track. It never leaves the block.

## 6. Runtime limits

- **A height depends only on map data in a fixed window.** Blocks are on a grid fixed in the planning frame:
  - a 1.5 km core;
  - heights blended with the neighbouring block over 100 m;
  - 300 m of margin, and 300 m more read.

  A block is solved once and comes out the same whichever cell asks.
- **A road under the car never moves.** Nothing beyond the block's window, and nothing about where the car is or what is loaded, may change a height (the user, 2026-11-28).
- **A block is solved before the car can see it**, within the distance the game preloads. Today the look-ahead fetches the map data only, up to 4 km; the 3D blocks are solved when the tiles are asked for. **Open:** solve the blocks ahead too.
- **A block solves in seconds**, and a contradiction is named quickly. Pregerson's unsolved block took 6193 s of the eval's 3-hour budget. **Open.**

## 7. Open questions for the user

1. **A link's vertical curve.** A motorway link is driven at its motorway's 130 km/h, a 665 m curve. Is that the law for links, or do links get their own driving speed? (Pregerson, #133.) Links at their own law or at 80 km/h do not alone solve Pregerson's block (#135).
2. **`tunnel=building_passage`.** It is a road through a building at street level, which Overture gives as a plain tunnel. Read the OSM value per way through the tag join, so a passage is ground? Bowker's block now solves without it.
3. **Solving the blocks ahead** of the car within the preload distance (section 6).
