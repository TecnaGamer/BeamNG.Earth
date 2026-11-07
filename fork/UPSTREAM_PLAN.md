# Getting the QLD work upstream, and making it reusable worldwide

## 1. How to pitch
1. Post on the BeamNG.Earth Discord (or open an issue): before and after screenshots of Yeppoon, Rockhampton, Brisbane and the M1, plus this split list. Ask the maintainer:
   - which PRs they want first;
   - whether they want new behaviour in Rust or Python;
   - whether a "region pack" design (section 3) is welcome.
2. Then send PRs one at a time, each small enough to review in one sitting.

## 2. PR split
Each PR goes on its own branch, cut from a fresh `upstream/main`. Re-apply the change by hand rather than cherry-picking the fork merges, because upstream moves about 80 commits a day. Leave out all fork-only material:
- Phase 0 setup (repo URLs, user agent, release workflow, BeamMP `LISTED`)
- `fork/` docs
- reference photos
- the Mt Coot-tha hook

| # | PR | Main files | Risk |
|---|---|---|---|
| 1 | **Region polygons**: `climate.au_state` from Natural Earth admin_1, which replaces the lat/lon boxes. It lays the groundwork for everything below and is worth having upstream on its own. | climate.py, data, tool | low |
| 2 | AU road markings: `_AU`, `_NZ` split, double barrier lines, 0.15 m edges | markings.py | low |
| 3 | AU vegetation: eucalypt group, dry grass share, evergreen seasons | vegetation.py, season.py, groundcover.lua | low |
| 4 | QLD building style: tints, Colorbond and tile roofs, gables continuing the wall, even brick UVs | buildings.py, materials | med |
| 5 | QLD houses: high-set houses, eaves, gutters, solar, whirlybirds, carports, awnings, tanks, storey share | buildings.py | med to high (big diff, so this could become 2 PRs) |
| 6 | Shop footpath awnings | buildings.py | med |
| 7 | AU streetscape: nature strips, fences, kerb channel, poles and lamps, roundabout gardens | roads.py, surface.py, furniture.py, islands.py, shapes | med |
| 8 | AU signs, part 1: direction boards style plus places data, real OSM destination text shard | dirsigns.py, data | low to med |
| 9 | AU signs, part 2: Give Way, AU signals, guide posts, curve warnings, crash barriers, no-stopping lines | furniture.py, warnsigns.py, kantstolpar.py, navgraph.lua, shapes | med |
| 10 | AU ground: red soils, cane fields | surface.py, terrain.lua | low |
| 11 | AU servos (gas station kit) | — | low |

**Before each PR:**
- Run the full test suite once.
- Re-record digests only for AU cases.
- Check that the non-AU digests are unchanged.
- Normalise line endings to match upstream; some fork merges changed CRLF to LF and back.

## 3. Rust: upstream policy (.claude/agent-rules.md, the maintainer, 2026-10-21)
- New server logic goes in Rust (`native/earthfast`). Python is only thin glue: argument marshalling and calls.
- Touching a Python algorithm means porting it, with no Python twin left behind (`fast.require()`, no fallback).
- The mod stays Lua.
- PRs target `dev`, not `main`.
- The fork wrote everything in Python, to iterate fast without rebuilding the native module. **Every PR therefore needs a port before submission.** Sort the fork code three ways:
  - **Data and tables** (markings styles, tints, shares, dirsigns_styles.json, places and destination shards, the state polygons, region packs): stays data, with no port needed.
  - **Algorithms** (high-set house geometry, eaves, carports, awnings, brick UVs, fence and verge placement, curve detection, barrier rules, no-stopping lines, destination matching, the point-in-polygon lookup, soil and cane classification): port to Rust, keeping the Python tests as behaviour tests.
  - **Lua** (groundcover, trees catalog, signals, terrain looks): stays Lua.
  - Generated assets (.dae, materials) stay as they are, along with their `tools/` builders. Check whether upstream wants the builders in Python or Rust; tools may be exempt.
- **Build:** a local MSVC build gives float digests that differ from upstream's committed `.pyd`. Before porting, rebuild the native module on a clean `upstream/dev` and re-record the goldens locally, so only our own diffs show. Or ask the maintainer how they record digests (CI or their machine).
- **Workflow per PR:**
  1. Branch from `upstream/dev`.
  2. Port the module to Rust.
  3. Test that the AU behaviour matches the fork version bit for bit.
  4. Check the non-AU digests are unchanged.
  5. Open the PR to `dev` with in-game screenshots (the maintainer insists on checking real-place imagery).

## 4. Making it worldwide: "region packs" (plan item 3.7)
The fork shows what a region needs. Today it is hard-coded per country in each module, as `if region == "AU"` branches. The generic version:

1. **Region resolver**: one function, `region.at(lat, lon) -> ("AU", "QLD")`, backed by admin_0 and admin_1 polygons (PR 1, generalised to every country). Every module asks it, instead of keeping its own box or airport lookup.
2. **Region pack**: `server/data/regions/<code>.json`. More specific packs inherit from broader ones: `AU-QLD` → `AU` → `oceania` → `default`. Keys:
   - `markings`: line widths, colours, centre and edge rules (today `markings.STYLES`)
   - `buildings`: tint palette, roof materials and shares, storey shares, house kits (high-set, eaves, solar, carport, awning) with probabilities
   - `vegetation`: tree groups and shares, lawn and dry shares, evergreen flag
   - `ground`: soil look rules (by Köppen class), crop looks (by WorldCover class plus a polygon)
   - `streetscape`: verge type, fence kinds and shares, kerb and channel, pole and lamp models, island planting
   - `signs`: board style (dirsigns_styles), speed sign face, give-way and stop rules, signal model, warning-sign thresholds, guide-post spacing, barrier rules
   - `data`: optional shards (places, destinations)
3. **Asset builders as templates**: `tools/build_au_*.py` already generate signs, posts, fences and signals procedurally from a few numbers. Turn them into one `tools/build_region_assets.py <pack>`, so a creator can make (for example) Japanese guide posts or UK fences by editing JSON, without needing modelling skills.
4. **Cache keys**: hash the pack files into the `revisit` cache key and the format numbers. Otherwise edits to a pack won't show.
5. **Docs for creators**: a "Make your region" guide covering:
   - copying `AU.json`;
   - gathering reference photos and the local standards (like `fork/QLD_REFERENCE.md`);
   - tuning the numbers in game with the in-game test checklist.

**Order:** do the resolver (step 1) first. Then move the AU branches into `AU.json` one module at a time, with digests unchanged, and only then advertise it to other creators.
