# Bridge headroom policy (1.5.4)

Road crossings target **6 m of usable space below the lowest deck girder**, plus a
0.15 m tolerance. The top of the bridge is therefore 8.35 m above the lower road
with the current 1.2 m slab and 1 m girders. `DECK_ROAD_CLEAR` is derived from the
mesh dimensions rather than an unrelated deck-height number. This provides an
explicit envelope for tall road vehicles such as semi-trucks and trailers; it is
not a promise for arbitrarily tall modifications or raised equipment.

The solver keeps full height for 20 m along the bridge in either direction from
a modelled road crossing before tapering the requirement. This covers a road
corridor rather than a single point; it is a fixed allowance, not a surveyed
footprint calculation for every skew angle and road width. Both vertices around
off-sample crossings/corridor edges receive the requirement. Profile smoothing
and overlapping-deck merging cannot remove an already feasible clearance floor.
Existing approach grading uses the same derived deck clearance automatically.
Explicit water/navigation requirements retain their own clearances.

## Signed height limits (OpenStreetMap `maxheight`)

Where the road under a deck carries a height limit (OpenStreetMap's `maxheight`, read from Overture's
height restrictions into the roads layer's `maxheight_m` rule, roads layer 9), that limit replaces the 6 m
as the headroom under the girders (still plus the 0.15 m tolerance). A signed limit is usually a little
under the real clearance, so it errs low. Only the stretches where the tagged road is on the ground count:
a limit mapped along a stretch that is itself a bridge describes what is over it. Limits outside 2-9 m are
ignored as mapping errors. On with the interchange solver (`BNE_INTERCHANGE=1`); `BNE_MAXHEIGHT=on/off`
overrides that. The rule is read into every roads cell regardless (it changes no heights by itself). (Perth: the Mitchell Freeway is
signed 4.5 m and 4.9 m under Hay Street, Murray Street 4.5 m under the freeway.)

## Constraints and diagnostics

Abutment heights, genuine at-grade ramp junctions and the existing maximum grade
remain authoritative. Forcing an otherwise impossible span upward would recreate
the steep wedges and approach obstructions that 1.5.4 already fixed. Major-road
approach fills remain subject to the existing exclusions. If a modelled road
crossing cannot meet the target, `earthd.roads` logs `bridge clearance below 6.0 m
target` with the bridge part and its modelled headroom. It does not silently claim
success, remove the bridge, or steepen the approaches to make a numerical check pass.

The diagnostic checks the final road-crossing profile corridor, excluding the
existing near-abutment at-grade junction cases. It is not a spatial collision
audit of every triangle, pier, truss, terrain object, missing crossing or AI route.
The lower-road datum is the graded elevation sampled at the crossing. Sloped
lower roads, very wide/skewed crossings, additional structures and incomplete map
data still require spatial and in-game validation. Closed/unresolved bridge chains
also cannot provide a universal clearance guarantee.

## Verification

`server/tests/test_bridge_clearance.py` covers the derived policy, interpolation
between deck samples, a full clearance corridor, actual generated slab/girder
vertices and triangle indices, overlap merging, stacked decks, existing approach
grading, water requirements, real at-grade junctions, bounded short-span slopes
and diagnostics for an impossible crossing. The existing bridge, grading, tunnel,
navigation and worker tests accompany these checks in the proposed CI workflow.

These are automated geometry checks. Before treating this as a release guarantee,
drive a stock semi-truck with a tall box trailer beneath and across representative
overpasses and stacked interchanges, at both approaches and after streaming/rebase.
Check all travel lanes, slope transitions, piers/trusses and collision activation;
compare against the unchanged 1.5.4 build. This PR does not modify or rebuild the
existing 1.5.4-beta executable: using the change requires a build of the patched source.
