# German junctions: right-of-way signs and markings (design)

Date: 2026-11-06. Status: design, approved by the user 2026-11-06; built 2026-11-07 (what changed from the approved
design: "As built" below). Branch: `feature/de-junctions` (fork Yukuhu/BeamNG.Earth), one PR to upstream `dev`. The
line numbers below are those of `dev` 741354ad, which the design was written against.

This is the first of a series that makes Germany look German. The series' order, agreed with the user:

1. **Foundation + junctions** (this document, one PR)
2. Town entry and exit signs: review, test and improve PR #597 (fuxau) rather than a parallel version
3. Traffic lights: German signal heads on poles beside the road
4. Roadside, regulatory and warning signs: Leitpfosten, one-way / no-entry, pedestrian crossings, warning signs,
   Autobahn exit signs
5. Rail crossings and bus stops: Andreaskreuz on its own post, Baken, single red light, half barriers, "H" sign
6. Street-name signs

## As built: changes from the approved design

The sections below describe the code as built. Where it departs from the design the user approved:

- **One PR, not two** (the user's decision): the foundation (part A) and the junctions (part B) go in together. The
  shards' new data come with the data repo's first weekly run after the merge; until then A3 holds.
- **The border is upstream's `countrybounds`** (PR #505, merged into `dev` meanwhile: Overture's divisions, ~10 m),
  not an outline of this branch's own: the same source, already in `dev`, and no data file in the PR (A1).
- **301 stands immediately before the junction**, in town and out of town (VwV-StVO zu Zeichen 301 Rn. 1). The
  "150-250 m before" is no rule for 301.
- **The 1002 plate goes only with a Vorfahrtstraße**: at a junction whose priority pair is 306, under its 306 and
  under that junction's 205 / 206; never with 301 (StVO Anlage 3 lfd. Nr. 2.1; Anlage 2 zu Nr. 2 und 3; VwV zu
  Zeichen 301 Rn. 3).
- **307 only out of town** (zu den Zeichen 306 und 307 Rn. 8: in town "entbehrlich"), and never where a 306 goes on
  past a 205 / 206 (Rn. 7).
- **A mapped priority road and a stretch by class are one step** (the approved steps 2 and 3): the pair is picked by
  rank, then straightness. At a junction where two or more arms are mapped `priority_road=designated` /
  `yes_unposted`, the arms that are stretches by class only and rank no higher are not stretches there, so the mapped
  road wins (the design's order: mapped first). A single mapped arm, or an untagged higher road crossing a mapped one,
  keeps the class stretch (partial tagging).
- **A mapped 205 / 206 on an arm coming in ends right before left at its junction, in a 30 zone too**: the other arms
  take a pair (301) and the rest 205 (VwV zu den Zeichen 205 und 206 Rn. 6: "sowohl positiv als auch negativ zu
  beschildern"; zu § 45 Abs. 1 bis 1e Rn. 41: in a Tempo 30 zone by Zeichen 301).
- **In a 30 zone a pair is always 301** (every arm coming in in the zone): never 306, so no 1002 plate, no 306 after
  the junction and no 307, for a mapped `priority_road=designated` and a stretch by class alike (StVO § 45 Abs. 1c
  Satz 2: a zone extends over no Vorfahrtstraße; the 301: VwV zu § 45 Abs. 1 bis 1e Rn. 41). Plain zone streets with
  nothing mapped stay right before left.
- **Arms that are no street** (service roads, tracks, paths; a living street where another arm is none) are not part
  of the derivation: they get no sign, and their drivers yield on leaving (StVO § 10).
- **Some nodes are left undecided** (no signs, no US stop, no AI change): fewer than three street arms, the Autobahn's
  own carriageway (StVO § 18 Abs. 3) or ramps among themselves, a diverge, a carriageway split. None of them is a
  junction where anyone must be told to yield.
- **A ramp's end gives way to the road it meets** (205), whatever its class: German Anschlussstellen are signed so.
- **"In town" is the land cover's town of most of a junction's street arms**, not the road drawing's: the drawing
  makes some town roads rural for its own reasons (a short roundabout approach, a road built up on one side). Most,
  not every (the user's review, 2026-11-07): a village's main road is mapped in segments, and one with fields on one
  side (Barbing's Regensburger Straße past its sports ground, 38 % built up) made both its junctions rural, their 306
  after the junction among side streets all in town. Driveways and service roads do not vote.
- **A mapped stop or give-way point belongs to one arm**, the nearest, not by its direction: Overture's points have
  none, and the shards' forward / backward run along the OSM way, which the arms do not carry.
- **A roundabout's entry gets Zeichen 205 over Zeichen 215** (the user's review, 2026-11-07; the approved design
  left 215 out): StVO § 8 Abs. 1a gives the ring the right of way where 215 stands under 205, as German roundabouts
  are signed. The 215 is 600 mm (750 over 100 km/h; VwV-StVO zu den §§ 39 bis 43 Rn. 13 and 15, round signs), its
  blue the VzKat picture's (#003ABD), its arrows measured off Wikimedia Commons' public-domain rendering.
- **Every tertiary is a stretch, with a ref or without** (the user's review, 2026-11-07): as approved only a tertiary
  with a ref was, so Barbing's Regensburger Straße (tertiary, 50 km/h, no ref) got 301 at four junctions in a row,
  where zu Zeichen 301 Rn. 4 wants 306 on a town's main roads. Out of town a tertiary without a ref (a
  Gemeindeverbindungsstraße) now gets 306 after its junctions and 307 where it ends; 301 stays for unclassified roads
  and for pairs a mapped sign leaves.
- **A stretch is each road's own property** (class, ref, `priority_road`), read at each junction, with no pass across
  tiles; mapped 307 nodes are not read (`priority_road=end` is).
- **In town a stretch's arm shows 306 where the derivation gives 301** (zu Zeichen 301 Rn. 4: not on main roads,
  "Dort ist das Zeichen 306 zu verwenden"), and a 301 where no arm gives way is left out (zu Zeichen 301 Rn. 2).
- **Zebras (Z 293) only in town, on roads of at most 50 km/h** (zu § 26 Rn. 1), and never near signals (Rn. 5).
- **The sizes follow each arm's limit** (zu den §§ 39 bis 43 Rn. 15), the lower edge is 2.00 m in and out of town
  (Rn. 42), and Z 206 at Größe 1 is 900 mm (Rn. 14: special forms per the VzKat, which has 900 and 1050).
- **The zone signs stand on the side of the zone's edge with a single arm** (both at a zone street's mouth, as German
  streets have them) and replace only the boundary's own 274 (B2).

### Not covered

- No "at most three 301 in a row in town" (zu Zeichen 301 Rn. 4: else 306), no advance 205 with a distance plate (zu
  den Zeichen 205 und 206 Rn. 2: only where the rule would not be seen in time).
- Unpaved and cobbled arms get their 205 but no Wartelinie, as they get none of the road's own lines (zu Zeichen 341
  allows it where a 205 stands; it does not require it).
- The AI's road graph groups a junction's arms by Overture's connector, the road tiles by a 1 m point. Where the two
  differ (12 of 416 sampled German junctions had one more arm in the graph; none changed the decision), the AI's
  priority could differ from the signs.
- Where no zone sign finds a clear spot at a zone's edge, the 274 there goes anyway.
- One drivability per edge, as at roundabout entries: along a chain of give-way junctions the third tertiary 205
  (0.84) takes the minimap's low colour, and an edge that is one junction's 205 and the next one's priority arm makes
  that pair unequal (0.98 / 1.0).
- Driving the other way off a mapped `priority_road=end` section onto its Vorfahrtstraße, there is no 306 where it
  begins; the first is at the next junction.

## Intent

In Germany today the mod shows American junctions. A US stop sign (`/levels/east_coast_usa/art/shapes/signs_usa/
sign_stop`) goes on every urban minor road meeting a major one, and a hashed coin flip puts all-way stops on half
of the minor four-ways (`server/furniture.py` `junction_controls`, 320-364). Give-way signs appear only at
roundabouts. There are no priority-road signs at all. Germany's mapped stop and give-way nodes are never read: the
read is gated to Sweden (`blockworker.py` 551 and 836, `earthd.py` 1761 and 2253).

German junctions follow a different system. A priority road (Vorfahrtstraße, Zeichen 306) runs through and the
side roads give way (Zeichen 205). Stop signs (Zeichen 206) are rare, kept for blind or dangerous junctions.
Residential streets and 30 zones have no signs at all and run on right before left. Getting this right changes
what a driver sees at almost every junction in Germany.

The user's standard is "no compromises": signs a German driver would expect, where German rules put them.
Real sign positions are rarely mapped, even in Germany. A sample block of central Freiburg (z14 cell 8549/5695) has
140 OSM `traffic_sign` elements, but only 32 of them are sign nodes, plus 2 `highway=stop|give_way` nodes; the rest
are regulations along ways (DE:244.1, DE:242.1, DE:239). Overture's infrastructure layer kept 3 of the 140. So
signs are **placed by German rules applied to the real road data**, and mapped nodes override the rules where they
exist. The user agreed to this; Mapillary's detected signs are not used.

## Benchmarks

The places the user knows in person and will judge:

- **Schrobenhausen**, the whole town (Upper Bavaria, about 48.561 N, 11.262 E): old town, residential 30 zones,
  priority roads through, villages around it.
- **The route Schrobenhausen → Ingolstadt** (about 48.765 N, 11.424 E): country roads, villages, a federal road
  and the city at the end.
- **Border towns, for the country lookup only:** Kehl / Strasbourg, Lörrach / Basel, Freilassing / Salzburg.

Per the project's rules, every check starts from the real place: aerials (`tools/ortho_hi.py`) and Mapillary photos
(`tools/streetview.py`, a developer reference only). The game screenshot is compared against them.

## Goals

- At every junction in Germany, the game shows the signs and markings German rules require (situations below).
- Junction priority comes from the real roads: mapped stop / give-way nodes, `priority_road` ways, speed zones, and
  road class when nothing is mapped.
- Traffic AI behaves consistently with the signs.
- The country is known from real borders, so a German junction near a border is German and a French one is not.

## Non-goals (in this project)

- **Signalised junctions stay as they are.** The US mast arm and the backup 306 / 205 signs under German lights
  come with the traffic-light project (3). The mockup's traffic-light card is that project's target, not this PR's.
- One-way, no-entry, pedestrian-crossing and warning signs (project 4). Town signs (project 2).
- **Any country other than Germany.** The sign code is gated on exactly `"DE"`. `markings.STYLES` maps AT, CH, LI
  and LU to `_DE`, but the sign code must not inherit that mapping.
- Reproducing each real sign post's exact position (see Intent).

## Anti-goals (a failure even if it works)

- **Any change outside Germany.** Junction props and markings in every other country stay byte-identical, and a
  test shows it.
- **AI traffic that crashes or stalls more than today.** Removing the US stop signs from German residential streets
  without giving the AI anything in their place is the most likely way this ships worse than it started.
- **Noticeably longer loading, or more memory on the Steam Deck** (aim: 30 fps on the Handheld preset). The
  extended data shards are budgeted in A2, the new sign props in B3.
- **Code for particular places.** Benchmarks are for checking; every rule is general and deterministic.
- **Signs that contradict the markings or the AI** (a 205 with no waiting line, or the reverse).

## Constraints (project rules)

- New server logic goes in Rust (`native/earthfast`); Python is thin glue, and the mod stays Lua. If a signature
  changes: bump `m.add("VERSION", n)`, call it through `fast.require(n, …)`, raise `fast.MIN_VERSION`. Commit the
  Rust source, not the built `server/earthfast.abi3.so` / `.pyd`: the maintainer rebuilds at merge.
- Rules cite their source (StVO, VwV-StVO, RMS, VzKat), the way `markings.py` and `speedsigns.rs` cite theirs.
- **Format numbers:** any change to grading plans or road facts output bumps `plancache.PLANS_FORMAT` /
  `roads.FACTS_FORMAT` in the same commit, with `BNGE_UPDATE_GOLDEN=1` for `tests/test_bake_formats.py`. A bump
  would force a worldwide rebuild even though the output outside Germany is unchanged. So the builder first works
  out which outputs junction props and junction markings belong to, and designs to stay out of facts and plans if
  that's possible. The PR states which way it went and why. (The revisit cache is keyed on the code hash and
  rebuilds after any server change anyway: `revisit.py` 13.)
- New features land behind a setting, the way direction signs (#754) and town signs (#597) did (see Settings).
- README (features, settings) and CREDITS.md (new data, sign artwork sources) are updated with the change.
- The quality gate (`tools/quality_gate.py`) passes. Contributor PRs target `dev`.

## Decided: part A, the foundation

Part A changes nothing visible on its own: the country from real borders, the extended shards and their reading,
which part B uses. Both parts go in one PR, part A's commits first. The `build_lane_tags.py` extension goes live with
the data repo's first weekly run after the merge; until then the junctions work as A3 says (road class and ref, and
Overture's stop and give-way points), and the shards' sign nodes, mapped priority roads and the zone signs have no
data yet.

### A1. Country from real borders

Today the country is the ISO code of the nearest airport (`navgraph.country_near`, 742-750; its own comment calls
this "a stand-in until a border map"). That gets border towns wrong.

**Requirements:**
- The country of each junction, or at least of each road tile with a junction-level fallback near a border, comes
  from real borders.
- Correct at the three border pairs above.
- Works with no extra online service.
- Cheap in memory and time on a Steam Deck (about 9 server processes).
- Unknown (sea, a gap or a disputed edge) falls back to today's behaviour.

**Options (the builder's call, with these constraints):**
- **Overture `division_area` geometry**, `subtype=country`, read per map cell as a new layer read. The existing
  `division_areas` layer reads codes only (`overture.py` 186, `geometry=False`), so this is a new read, cached like
  the other layers.
- **Port `server/countrybounds.py` from PR #505** (exencendre). That needs a full security read of its diff first
  (project rule), a commit credit ("from PR #505 by exencendre"), and a memory check. Its snapshot is 5.7 M vertices,
  and with its LRU cache it must not cost a Deck hundreds of MB across processes.

*As built:* the second option, with no port: PR #505 was merged into `dev` meanwhile. `server/germany.py` is a thin
DE-only API (`contains`, `de_at`, `effective_country`, `signature`) over upstream's `server/countrybounds.py`
(Overture's divisions, the territories with their waters, simplified to ~10 m and looked up natively by
`countrymap.rs`; the band file fetched once from BeamNG.Earth-data, the project's own data host, by `regionaldata`
and kept on disk). A point is in Germany where `countrybounds.bundled().codes_at` says "DE".
- `germany.BOX` (5.81-15.10 E, 47.22-55.11 N; A2) is a quick reject: outside it nothing is looked up or fetched. The
  setting is on by default the world over, and a player in Baltimore downloads no borders.
- No border data (not downloaded yet, offline, a failed fetch or checksum), or no single territory at the point, is
  not Germany: today's output, never an error.
- The cache keys of German output gain the border data's signature (`germany.signature()`, marked `:absent` where
  there is none) only with the setting on and the tile's reach meeting `germany.BOX`, so a tile built without the
  data is built again once it is there. (The road tiles' keys also carry the setting's own bool, False when off.)
- A lookup that raises marks the builds running then incomplete (`revisit.fault`: none of them is kept), drops
  countrybounds' cached index and is not tried again for 60 s, doubled with each failure in a row up to 30 min.
- A box's read gate (whether a road tile or the road graph reads the map's stop and give-way points, crossings and
  sign nodes, and a speed-sign tile builds zone signs: `designs.reads_here`) asks the box's corners and centre, then
  a 25 m grid over it. A strip of Germany narrower than about 35 m inside the box can be missed (`designs.GRID_M`).

**How far it reaches (decided):**
- The new lookup decides "this is Germany" for every Germany-specific output of this project:
  - the junction signs and markings, each by the junction's own point;
  - the AI at those junctions (the road graph takes the road tiles' decision);
  - the 274.1 / 274.2 swap in the speed signs;
  - the line style (`markings.style`, by the road tile's middle).
  Otherwise a junction at Kehl could get German 205 signs but French lines.
- "Outside Germany", for the byte-identical test, means places that **neither** lookup calls DE. Where the two
  disagree, that's the border fix this part exists for, and the PR lists those places' changed output.
- Other users of the country (driving side, mph, signal phasing) are left alone in this project.

### A2. Germany's road-rule data in the weekly OpenStreetMap extract

Overture drops these tags, and Layercake's `highways.parquet` has a fixed set of 38 columns without them (checked
2026-11-06). The project's weekly extract is built by `tools/build_lane_tags.py`, which the data repo's Action
(TecnaGamer/BeamNG.Earth-data `rail.yml`) downloads from upstream `dev` and runs on the planet each week. Extending
that tool goes live the week after the maintainer merges it, with no change to the data repo.

**What is added, normalised at build time so the shards stay small:**
- **Ways:**
  - `priority_road` (`designated` / `end` / `yes_unposted` → a small enum).
  - A German zone limit (`DE:30` / `DE:20` / `DE:10`) from `zone:maxspeed=DE:30`, `maxspeed:type=DE:zone30` or
    `source:maxspeed=DE:zone:30` (and their 20 / 10 variants). A bare `maxspeed=30` is **not** a zone.
  - Way-level `traffic_sign` only as `priority_road`: a way's `DE:306` (Vorfahrtstraße) counts as
    `priority_road=designated`. No other way-level sign code is kept.
  - Only tags that part B reads; no raw strings carried "just in case".
- **Nodes:**
  - `traffic_sign=DE:205|206|301|306|307|274.1|274.2` (with their `direction` / `traffic_sign:direction`), carried
    the way `build_rail_tags.py` carries level-crossing nodes. Also the other forms mappers use: a code with its
    variant (`DE:274.1:30`, `DE:274.1[30]`, `DE:274.1-20`, kept whole so the reader can take the zone's limit), a
    code without `DE:` (written with it), and `traffic_sign:forward` / `:backward` (a record each, with that
    direction unless the node maps one). Part B reads the 205 / 206 nodes; the direction keys stay in the shards
    for later (B1).
  - `highway=stop` and `highway=give_way` are **not** in the shards: they come from Overture's infrastructure
    layer, which `secrossings.nodes` already reads for Sweden (position and stop or give way; no direction).

**The shard layout (`tools/build_lane_tags.py`):**
- `"ways"`: unchanged, exactly what the tool wrote before: the lane-tagged ways and the roundabout rings. A way
  with only road rules is not in it, and a lane way's entry carries no rule keys. Today's server copies a way's
  `"ways"` entry into the lane parts the markings read (Sweden's 2+1 roads among them), so a rule key there would
  change their output outside Germany.
- `"rules"`: `{"priority_road": {"designated": [way ids], "end": [...], "yes_unposted": [...]}, "zone:maxspeed":
  {"DE:30": [...], "DE:20": [...], "DE:10": [...]}}`, a way in every shard its nodes fall in, as with `"ways"`;
  left out where a shard has none. Each id list is sorted and delta-coded (the first id, then each minus the one
  before: 100, 105, 230 is `[100, 5, 125]`). The server decodes it with a cumulative sum and keeps sorted id arrays
  (`railtags.Rules`), never a dict per way.
- `"nodes"`: `[[id, lon, lat, {"traffic_sign", "direction"?, "traffic_sign:direction"?}]]`, the sign nodes above.
- **Only around Germany** (the user's decision, 2026-11-06, on the measured growth: Sweden's ~41k `priority_road`
  ways, none of them used, grew its shards 29.9 %). `GERMANY_BOX = (5.81, 47.22, 15.10, 55.11)` (lon_min, lat_min,
  lon_max, lat_max) is the server's `germany.BOX`: every point the border data calls Germany, its territorial waters
  included (5.870 .. 15.035 E, 47.275 .. 55.095 N), lies inside it with 0.015 degrees or more to spare. It is a
  constant in the tool because the Action curls the tool alone (`test_lane_rules` keeps the two equal). A rule way is
  written when at least one of its first / middle / last nodes lies in the box (a way without any node place is
  dropped, as in `"ways"`); a sign node when it lies in the box. Both filters run in `build_lane_tags.py`;
  `build_rail_tags.py`'s rail path is unchanged. A shard outside the box holds what the tool wrote before the road
  rules; only a shard touched by a rule way that also reaches inside the box differs (a way across the box's edge,
  with a node inside, can put a rules-only shard in the tile next door, which a reader of `"ways"` reads like no
  shard).

**Budget:** `railtags.Ways` already notes 57 MB per dense shard and 1–2 GB across a Deck's processes. The added
columns are measured on a dense German shard (the Munich z7 tile) and on a dense non-German one. The PR reports
the growth, and it has to stay small; this is why the values are normalised. Measured with the box and the delta
coding (Geofabrik extracts, against `dev`'s tool): Oberbayern, 19,176 rule ways and 1,030 sign nodes (all inside
the box, as before it); the Munich shard 68/44 grows from 308,333 to 353,102 B gzipped (+14.5 %; +22.5 % without
the delta coding), all four Oberbayern shards +15.6 %. Sweden: every one of the 37 shards decodes identical to
`dev`'s, the total stays 430,799 B (+0 %; +29.9 % without the box), and no rule or sign node is written.

**Testing before it's live:** build the shards locally from a Geofabrik extract (Oberbayern, or all of Bavaria)
with the same tool, serve them locally, and point `BNE_DATA_PAGES` at them.

### A3. Works without the new data

The junctions must work, and be reviewable in game, **before** the extended shards exist. With no tags, German
junctions still get German rules from road class and ref alone (B1, the last step), and Overture's mapped stop and
give-way points still count. Tags refine the result; they don't enable it.

## Decided: part B, the junctions

### B1. Who has priority at a junction (the derivation, in order)

For each junction in Germany (its own point inside the border) that has no signal and is no roundabout. The rules
are the kernel's (`native/earthfast/src/dejunction.rs`, whose module doc maps each rule to its source);
`server/designs.py` hands it the arms.

**The arms it is given:** streets only, living_street to motorway, a link ranking as the road it leads to.
- An arm that is no street (a service road, a track, a path), or a living street where another arm is none, gets
  no sign: its drivers yield on leaving (StVO § 10; VwV-StVO zu den Zeichen 205 und 206 Rn. 6 leaves the exits of
  verkehrsberuhigte Bereiche and field and forest tracks unsigned), and the AI gives way there (B4).
- A ramp or slip road (any link) meeting a road that is no link gives way there: it ranks below every such road and
  counts as a mapped give-way, never as a stretch.

**Nodes that are no junction** are left undecided, with no German sign, no US stop and no AI change:
- fewer than three street arms (a road split at a connector, a driveway on a road);
- a motorway arm that is no link (the Autobahn's own carriageway has the right of way: StVO § 18 Abs. 3), or every
  arm of motorway class (ramps merging or parting among themselves);
- only one arm coming in (a diverge: nobody there must yield);
- a carriageway split: a two-way road and its two one-way carriageways, all of one class.

**The steps:**

1. **The map's signs:** a mapped stop or give-way (Overture's `highway=stop` / `give_way` points, the shards'
   `traffic_sign=DE:206|205` nodes) belongs to one arm: the one whose road it is nearest, within 30 m of the junction
   and half the road's length along it, and within the road's half width + 5 m beside it, the drivers' side breaking
   a tie. Its direction is not used (Overture's points have none). That arm gets 206 / 205. When exactly two unmapped
   arms are left, at least one of them coming in, they are the priority pair.
2. **A Vorfahrtstraße:** two or more remaining arms on a stretch (defined below): the best pair of them, by the
   ranking below. At a junction where two or more arms are mapped `priority_road=designated` / `yes_unposted`, the
   arms that are stretches by class only and rank no higher are not stretches there, so the mapped priority road wins.
   One stretch arm alone is no pair: on to 3.
3. **Right before left:** no arm coming in has a mapped sign, and every arm coming in is in a 30 zone, or every one
   is of a residential-type class (living_street, residential, or unclassified in town): the junction has **no signs
   and no lines**. A mapped 205 / 206 on an arm coming in rules this step out, in a 30 zone too: a junction that
   departs from right before left is signed on both roads (VwV-StVO zu den Zeichen 205 und 206 Rn. 6), in a Tempo 30
   zone by Zeichen 301 (zu § 45 Abs. 1 bis 1e Rn. 41). Step 4 then gives the other arms their pair and the rest 205.
4. **Road class, when nothing above applies:** the best pair of the remaining arms; a lone arm coming in (every other
   one mapped or leaving) is the priority road by itself.
   - The ranking: class first; then the ref's rank when classes tie: Bundesstraße (B) > Staats- / Landesstraße (St,
     S, L) > Kreisstraße (K, or a district code of one to three letters, umlauts included, and a number: ND 15,
     PAF 3, MÜ 20; "A n" counts as a district code only on a road below trunk) > none; then, of the pairs of the two
     top ranks, the straightest through the junction (within a small slack the one with fewer one-ways leaving: a
     divided road's carriageway coming in pairs with the road ahead), then the arms' order.
   - Two equal roads above residential type (two Kreisstraßen in a village, two secondaries out of town) are
     **never** left as right before left; the tie-break decides. German practice signs these crossings.

The pair gets **306** when both its arms are on a stretch (from step 4 also a lone arm on a stretch), else **301**.
In a 30 zone (every arm coming in in it) the pair is always 301, never 306, whether its arms are a mapped
`priority_road=designated` or stretches by class: no 1002 plate, no 306 after the junction and no 307 stand there.
The statute keeps Vorfahrtstraßen out of zones (StVO § 45 Abs. 1c Satz 2: "Die Zonen-Anordnung darf sich weder auf
Straßen des überörtlichen Verkehrs (Bundes-, Landes- und Kreisstraßen) noch auf weitere Vorfahrtstraßen (Zeichen
306) erstrecken"), and inside a zone the VwV gives priority by Zeichen 301 (zu § 45 Abs. 1 bis 1e Rn. 41). Plain zone
streets with nothing mapped stay right before left (step 3). The other unmapped arms coming in get **205**; an arm
leaving (a one-way out) gets nothing. A 301 at a junction where no arm gives way is left out (zu Zeichen 301 Rn. 2:
the other road must have a 205 or 206). In town a stretch's arm shows 306 where this gives 301 (zu Zeichen 301 Rn. 4:
301 is not for main roads, "Dort ist das Zeichen 306 zu verwenden"), outside a 30 zone. "In town" is most of the
junction's street arms' roads in town by the land cover, before the road drawing's own overrides (a short roundabout
approach, a road built up on one side).

**A Vorfahrtstraße is a property of a road, not of one junction** (VwV-StVO zu den Zeichen 306 und 307 Rn. 1: in
town the Bundes-, Landes- and Kreisstraßen and the main roads get 306). A road is on a stretch when:
- it is mapped `priority_road=designated` or `yes_unposted` (a way's `traffic_sign=DE:306` reads as designated);
- else, unless it is mapped `priority_road=end`: it is of class trunk / primary / secondary / tertiary, a tertiary
  with a ref or without (a Kreisstraße, or a town's main road or Gemeindeverbindungsstraße that OpenStreetMap ranks
  with them). The ref ranks two stretches against each other (above), not whether a road is one.

Each road tile reads the flag of every road (`de_stretch`) and uses it at each junction; there is no pass across
tiles. A stretch so ends, for the signs, where its road drops to a class that is none, at a roundabout or a
signalised junction (which keep their own signs), or where it meets a stretch that takes the priority. A mapped 307
node is not read.

**Signs on the priority side:**
- On a stretch: **306** at each of its junctions outside a 30 zone (above), on the pair's arms (VwV-StVO zu den
  Zeichen 306 und 307 Rn. 2): before the junction in town; out of town after it, on the arm the drivers leave on
  (facing them, where that arm is a stretch; before the junction where it is not), and after it too for drivers who
  start on the stretch there. Where the priority bends, always before the junction (Rn. 4).
- **307** out of town only (Rn. 8), on a post of its own 10 m farther out than the junction's sign:
  - on a stretch's arm coming in that gets 205 / 206 / 301 here, where the stretch goes on nowhere; never where it
    goes on past a 205 / 206, that is where another stretch arm gives way here too or a stretch one-way leaves
    straight on (Rn. 7);
  - where a mapped `priority_road=end` way starts, after the node on that way;
  - before a roundabout's entry on a stretch, unless another stretch (no link) leaves the roundabout.
- Any other junction where step 1 or step 4 gives one road priority: **301** ("priority at the next junction") on
  the pair's arms, immediately before the junction, in town and out of town (VwV-StVO zu Zeichen 301 Rn. 1).
- **Bent priority:** where a 306 pair is not the straightest pair through the junction, its 306 arms and that
  junction's 205 / 206 arms get the **Zusatzzeichen 1002** plate showing the bend, each from its own approach (the
  other roads ahead, left or right by the nearest 90 degrees). Never with 301 (VwV-StVO zu Zeichen 301 Rn. 3): a pair
  found by rank keeps 301 and its junction has no plates. No plate at a junction of more than four arms, where two
  roads fall in one place of the picture or one runs back beside the approach, or where the ten 1002 pictures have
  none for the shape.
- **Roundabouts:** keep today's give-way at the entries, as Zeichen 205 over Zeichen 215 on one post (without a
  plate) at today's spot: the ring has the right of way where 215 stands under 205 (StVO § 8 Abs. 1a).
- **Stop signs** only from step 1 (mapped). Rule-placed stops are not invented; the US stop signs and the all-way
  coin flip are off in Germany.

### B2. What it looks like: the six situations

The user reviewed six mockups (the brainstorm session, 2026-11-06) and flagged none. In this project:

| Situation | Signs | Markings |
|---|---|---|
| Priority road through a town | 306 on the main road before each junction; 205 on side roads | Wartelinie (Z 341, broken) at 205 |
| The priority road turns | 306 + 1002 plate on its arms; 205 + 1002 plate on the others | Wartelinie at 205 |
| Stop where mapped | 206 on that arm | Haltlinie (Z 294, solid) |
| Country road, single junction (not a Vorfahrtstraße) | 301 immediately before; 205 on the side road | Wartelinie; edge lines as today's `_DE` style |
| Residential / 30 zone | none at the junction (where a 205 / 206 is mapped there: 301 and 205, B1 step 3); 274.1 where the zone begins, 274.2 where it ends | none at the junction |
| Traffic lights | **out of scope** (project 3) | unchanged |

**Zone signs and speed signs:** `speedsigns.py` places a round 274 wherever the limit changes. A zone's edge is a
connector in Germany where a road of a mapped 30 / 20 / 10 zone (A2) meets one outside it.
- There a 274.1 faces the traffic going in and a 274.2 the traffic leaving, both on the side of the edge with a single
  arm: at a zone street's mouth both in the zone street; where one street outside meets the zone's streets, both on
  that street; on the zone's arms where neither side is single; at a node of two roads, the 274.1 in and the 274.2
  out. Each stands 5 m from the edge (at a junction, from its widest other road), on the drivers' side, clear of the
  pavements.
- A 274.2 only where the traffic can leave into a street outside every zone that is no living street (VwV-StVO zu den
  Zeichen 274.1 und 274.2 Rn. 3: entering a verkehrsberuhigter Bereich, that area's own sign stands instead; by
  analogy, none where another zone's 274.1 does).
- Their size follows the round 274's (zu den §§ 39 bis 43 Rn. 17a, Rn. 15). They are drawn in the speed-sign tiles,
  as the 274s are.
- They replace only the boundary's own 274, so there is never a 274 and a 274.1 at the same boundary. A 274 on a zone
  road goes (inside the zone its limit is the zone sign's). So does a 274 within 60 m of the edge that faces the
  traffic leaving the zone and shows the limit posted there, on the road that traffic goes straight on to, the
  outer road of a node of two roads, or the one street the zone pair stands on. Every other 274 stays: the road
  through a zone street's mouth keeps its own.

**Markings:** Germany gets the Wartelinie (Z 341) at 205, a roundabout's entries included, and the Haltlinie (Z 294)
at 206, with their RMS dimensions. The Swedish M14 teeth and M13 line (`markings.give_way_lines` / `stop_lines`, today
Sweden only) are not used in Germany. The road's own lines end at them, the way `stop_lines` already cuts them.

**The US crosswalk and stop line go in Germany.** Today every town junction arm with sidewalks on both corners gets
a West Coast USA `crossing_white` crosswalk and a 0.45 m stop line, in every country but Sweden (`junctions.py`
around 562-592; Sweden's replacement is the `country == "SE"` block of `roads.build_tile`, around 8938-8960). Left
in place, they would contradict every 205 and every right-before-left junction. In Germany, at junctions without
signals:
- **Crosswalk paint only where OpenStreetMap maps a marked crossing.** A zebra crossing (Fußgängerüberweg) gets
  Z 293: `crossing=zebra`, `crossing_ref=zebra` or `crossing:markings=zebra`, or a marked crossing without signals
  (`crossing:markings=yes`, `crossing=marked` or `uncontrolled`: an unsignalised marked crossing is a Fußgängerüberweg
  in Germany). Only in town, on a road of at most 50 km/h (VwV-StVO zu § 26 Rn. 1), and with no traffic signal near
  (Rn. 5). Unmarked crossings, refuges without markings and signalised crossings get none. They are placed as
  Sweden's (`secrossings.place`), with Germany's own reading of the tags (`secrossings.de_paint`).
- **Stop lines only at 206** (the Haltlinie above, at the map's stop line where it has one). 205 arms get the
  Wartelinie; right-before-left arms, exits and undecided nodes get nothing.
- **Curb ramps stay at the corners**, as German streets have lowered kerbs at junctions with or without paint; a
  mapped crossing's ramps are added to them. Where a crosswalk goes, its line cut goes with it.
- **Signalised junctions keep today's paint** until the traffic-light project (a crosswalk belongs to its nearest
  junction within 50 m).

### B3. The signs themselves

- **Faces:** per the official catalogue (VzKat). Their images are official works without copyright in Germany
  (§ 5 UrhG); the source used is credited in CREDITS.md. They are drawn by `tools/build_de_signs.py` from their
  construction (the VwV-StVO's and the VzKat's sizes, the VzKat's colours), Zeichen 206's lettering in D-DIN.
- **Sizes:** the VwV-StVO size classes by the speed limit of the arm the sign stands on (zu den §§ 39 bis 43 Rn. 13,
  15): Größe 1 under 50 km/h, Größe 2 from 50 to 100 (and where no limit is known), Größe 3 above 100. The octagon of
  Zeichen 206 is not in the VwV's table: it takes the VzKat's 900 mm (1050 at Größe 3), Größe 1 too (Rn. 14: special
  forms per the VzKat).
- **Posts:** galvanised grey round posts (60.3 mm). The lower edge is 2.00 m over the road, in and out of town
  (Rn. 42: "in der Regel 2 m über Straßenniveau"). A sign stands where today's US signs stand, off every carriageway
  (`furniture.signal_spot`). The back of a plate is grey.
- **Built like Sweden's:** the meshes generated by a tool (`tools/build_median_props.py`, as Sweden's), placed as
  `trees.lua` items: 36 assemblies (205, 206, 301, 306, 307, 205, 206 and 306 over each of the ten 1002 plates, and
  205 over 215) in three sizes, 108 groups (`designsigns.GROUPS`), each with its collision box. They show out to 300 m (`GROUP_MAX_M`)
  and, as Sweden's signs, are not thinned until a tile holds more items than its cap (`trees.lua` `isBarrier`). The
  zone signs are drawn in the speed-sign tiles' mesh, as the 274s.
- **Budget (my call):**
  - One sign assembly (plates and post) is one mesh item, so a post with 306 and a 1002 plate is one draw, not
    three.
  - Assemblies are culled at the same distance as the US stop and give-way signs (300 m).
  - On the B4 A/B, average fps and 1 % low stay within 3 % of before, on this PC at the Low and High presets, in
    Schrobenhausen and on the route.
  - The PR reports the new items per road tile, set against today's US stop signs. If the 3 % doesn't hold, the
    plan adds LODs or a placement cap.

### B4. Traffic AI

The stock AI ignores the "yield" and "slow" controller actions: `ai.lua` never compares actions 4 and 5
(`docs/research/2026-10-10-navgraph-traffic-signals.md` §4.4, §5.1). The research's method for give-way is small
drivability steps on the yielding approaches (its §5: "1.0 / 0.98 / 0.96 …"). Today only roundabout entries use
it (`ENTRY_DRIVABILITY`, `navgraph.py` 515-516); a non-ring `G_YIELD` is dropped at 680, with nothing in its place.
This project builds the per-arm step. The road graph takes the road tiles' decision of each junction (the same
`designs.controls` over the same arms, in the same order). So in Germany:
- **205** → a drivability step on that approach's edge, as the ring code does: `DE_YIELD_STEP` (0.02) below the
  lowest of the arms it gives way to (the priority pair's, or the right-before-left arms'). The pass repeats on the
  values it leaves, at most four times, so a side road that is the next junction's priority arm steps once more
  there. An exit (an arm that is no street: StVO § 10) takes the same step. This is also how a mapped
  `priority_road` that disagrees with road class reaches the AI.
- **206** → `signStop`, as today's stop junctions (`navgraph.lua` 316-318), with no stop-sign head (the 206 is the
  road tiles' sign).
- **Right before left** → an **invisible `signStop` for the AI only** on every arm (my call), in town and out of
  town. Equal drivability gives the stock AI nothing to yield on, and today's coin flip already stops it at half of
  these junctions. A brief stop at an unsigned residential junction is close to how people really approach
  right-before-left, and it keeps crashes down. If the A/B shows it stalls traffic, the builder tries alternatives
  and says which was chosen and why:
  - only the arms that must yield to a right-hand arm stop;
  - a slow-down patch to the AI in the mod.
- **A node left undecided** (B1) → nothing: no stop, no step, no US stop. Roundabouts and signalised junctions as
  today.
- **The minimap** colours a road from drivability 0.85 instead of 0.9 (`minimaproads.lua`), so a 205 step keeps its
  road's colour; no drivability lay between the two. An approach stepped below 0.5 (an exit at a living street, a
  mapped 205 among living streets) spawns no random traffic but is still driven, as a track is.
- **Acceptance:**
  - An autodrive A/B on the Schrobenhausen → Ingolstadt route and inside Schrobenhausen, with traffic on
    (`tools/autodrive_perf.py --keep-going`).
  - It compares crash, stop and off-route events before and after, plus average and 1 % low fps.
  - It passes with no more crashes or stalls than before.

### B5. Settings

- **Germany's signs and markings sit behind one setting**, "German junction signs" under Settings > World, and
  `BNE_DEJUNCTIONS=on` / `off` forces it (the `BNE_DIRSIGNS` style). It is on by default and acts only in Germany;
  the default is the maintainer's choice. With it off, Germany is exactly as today.
- **The country lookup (A1)** takes effect only where the setting is on, until the maintainer decides otherwise.

## Verification

- **Tests:**
  - Rust and Python unit tests for the derivation (each B1 step).
  - The zone and 274 interplay.
  - The 1002 plate choice.
  - One decision: the same roads through the road tiles (`roads.build_tile`) and the road graph (`navgraph.build`)
    give each junction the same signs.
  - "Byte-identical outside DE": the same junction rows before and after, for a set of non-German cells.
  - `tools/quality_gate.py` green.
- **Benchmarks, in game:**
  - Schrobenhausen: at least ten junctions of every kind in B2, each a screenshot next to its aerial and Mapillary
    photo, judged by the user.
  - The route to Ingolstadt, driven live.
  - The three border pairs for the country.
- **Performance:**
  - The A/B in B4 (fps and events) on this PC at the Low and High presets.
  - Shard growth (A2).
  - A Steam Deck run is requested from the maintainer in the PR; there's no Deck here.

## Left to the builder

- The mechanism of A1, within its constraints (as built: upstream's countrybounds, A1).
- How the Vorfahrtstraße stretches (B1) are found and shared across tiles (as built: each road's own flag, read at
  each junction; no pass).
- The exact rankings in B1 step 4 and the "in town" test (as built: the land cover's town of every arm).
- The repeat spacing of 306 (from VwV-StVO), sign sizes, heights and offsets (from VwV-StVO / RMS), and the 1002
  variants (as built: 306 at every junction of a stretch; B1, B3).
- Whether props and markings fall under a format number (Constraints) and the design that follows from it (as built:
  neither does; the signs and the paint are road-tile output, outside facts and plans, and their cache keys are the
  revisit keys of A1).
- How the work splits into commits inside the PR.

## For the maintainer to decide (in the PR)

- The setting's default.
- Merging the `tools/build_lane_tags.py` extension, which changes the weekly shards for everyone.
- Whether other users of the country switch to the new lookup.
- A Steam Deck check.
