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

## 3. Rust
- Upstream keeps heavy geometry kernels in `native/earthfast` and ships a prebuilt `.pyd`. We kept everything in Python on purpose, because a local MSVC build changes the float digests.
- Good candidates to move to Rust once upstream agrees:
  - the brick UV re-mapping (`_qld_brick_scale`, which rewrites UVs the kernel already made; better done in the kernel's bay logic);
  - the curve-sign curvature scan;
  - the point-in-polygon state lookup, if it shows up in profiles.
- Let upstream CI build the `.pyd`. Never commit a locally built one.

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
