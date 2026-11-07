# AU sign text: where it comes from and how to use OSM destination tags

Measured 2026-11-07 via Overpass (osm base 00:45Z) in the SE QLD box (-28.2,152.6,-26.4,153.6).

## Counts

| Set | Count |
|---|---|
| motorway_junction nodes (known) | 407 (194 with ref, 1 with exit_to) |
| motorway_link / trunk_link / primary_link ways | 5132 |
| ... with `destination` | 1545 (30%) |
| ... with `destination:ref` | 1273 (25%) |
| ... with `destination:street` | 1629 (32%) |
| ... with `destination:symbol` | 104 (2%) |
| ... with both `destination` and `destination:ref` | 979 (19%) |

Coverage is good for text boards: roughly a third of link ways carry real posted text, and `destination:street` is the most common key (it often names the road, e.g. "Pacific Motorway"). Symbols are rare. Not measured: the share on motorway_link only, and mainline `destination:*` (tools/build_sign_tags.py already handles those as the "through" kind).

## Samples (motorway_link, QLD)

| way | destination | destination:ref | destination:street | other |
|---|---|---|---|---|
| 5135872 | Gold Coast | M1 | Pacific Motorway | ref M1 |
| 8012944 | Logan;Gold Coast | M3 | Pacific Highway | |
| 8039131 | City Centre;Toowong | M3 | Pacific Highway | |
| 23615426 | Sunshine Coast | (`destination:ref:to` M7;A3) | (`destination:street:to` Airport Link Tunnel;Lutwyche Road) | `destination:symbol=airport` |
| 26157696 | Sunshine Coast | M1 | Gateway Motorway | |
| 28186612 | Tugun | | | `junction:ref=95`, name "Exit 95 to Tugun" |
| 28186646 | Currumbin | | | name "Exit 95 to Currumbin" |

Note the `:to` variants (`destination:ref:to`, `destination:street:to`) occur in AU. exitsigns.py reads `destination:ref:to` but not `destination:street:to`.

## How current code chooses text

**Direction boards (server/dirsigns.py)** do not read OSM destination tags at all.
- Destinations are place names from a population-ranked snapshot: `Places` (dirsigns.py:55) built from `places_2026-10-21.0.json.gz` plus the fork's AU extra (`PLACES_EXTRA`, dirsigns.py:41, Overture AU localities).
- The Rust kernel `earthfast.dirsign_place` (dirsigns.py:303) picks, per junction arm, up to two place indices and distances (row columns 5-8). `_row_content` (dirsigns.py:222-243) turns them into `(name, English name, km)`; the English line shows only for cities.
- Route plates come from `road_routes` / Overture `routes` (dirsigns.py:104, 188).

**Exit guide signs (server/exitsigns.py:57 `labels`)** do read OSM: `destination` (or `:forward`/`:backward`), then `destination:street`, `destination:lanes`, node `exit_to`, node `name` (lines 72-73); routes from `destination:ref`, `:ref:to`, `:ref:lanes` (line 74); exit number from `junction:ref` or node `ref`. `EXIT_KEYS` (exitsigns.py:29) lists the tags kept.

**Shard contents.** tools/build_sign_tags.py writes weekly z7 shards (dataset "signs"). Each is `{"objects": [{"id", "k": exit|through|mile, "lon", "lat", "d": {...}}]}` (railtags.py:312 `SignTags`; `signshards.KINDS`, signshards.py:14). `exit` = a motorway/trunk link with its posted destinations and approach; `through` = directed mainline destination tags; `mile` = Interstate mileposts. `BNE_SIGNS_DIR` reads a local shard set (railtags.py:310).

**US gating (server/signstream.py).** `us = signstates.states(la, lo) != ''` for the tile (line 64); `if not (WORLD or us)` (line 79) returns only speed signs and direction boards. Exits, mileposts and merge signs are therefore built only where signstates finds a US state (or `BNE_SIGNS_WORLD=1`). Every face is drawn in MUTCD style (signstream.py header).

## Recommendation

Two separate problems; do the cheap one first.

1. **Smallest first step (text only, no Rust):** build an AU destination shard set in `tools/` (a thin variant of build_sign_tags.py restricted to AU, with `destination:street:to` added, writing `exit`-kind records to a local folder via `BNE_SIGNS_DIR`) and a small Python lookup in dirsigns.py. When a board stands at a junction whose arm is a link way with OSM `destination`, prefer those names (and `destination:ref` for plates) over the nearest-place names in `_row_content`; otherwise keep the place snapshot. Effort: about 1 to 2 days, plus a day of QLD verification.
   - Caveat to check before starting: whether the kernel's output row carries the arm's OSM way id. If it does not, matching arm to link way is a geometric nearest-way join in Python (doable, adds roughly half a day). If a kernel change is needed to return the way id, then Rust does change, but only a column added to the row.
2. **Showing exit guide signs in AU (larger):** needs the `us` gate relaxed for AU in signstream.py:64/79 and an AU face style (Austroads green/white, different typeface and shields, left-hand placement). That is the real work: roughly 1 to 2 weeks, mostly sign art and layout, and exitsigns.py/signlayout.py assume MUTCD dimensions. Rust is not required for the data, but the face drawing here is Python/atlas based; check `dirsign_face` only if boards (not exits) need new styling.

**Does Rust have to change?** Not for step 1 if the way id can be recovered in Python; possibly a one-column kernel change otherwise. Not for step 2's data side.
