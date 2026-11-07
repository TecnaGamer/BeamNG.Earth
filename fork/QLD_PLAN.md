# BeamNG.Earth — Queensland fork plan

Base: upstream `tecnagamer/beamng.earth` @ `v1.8.2-beta` (a4053d3a). Plan branch: `qld/plan`.
Goal: make Australia, and Queensland first, look and feel Australian rather than North American.
Real-world specs are in [QLD_REFERENCE.md](QLD_REFERENCE.md).

Research credits: roads/markings, signs, buildings and scenery code surveys by Sonnet Worker; architecture/workflow
survey by Haiku Runner; QLD standards reference by Sonnet Worker; synthesis and plan by Opus (main).

---

## 1. What QLD gets today

| Area | Today | Feels US because… |
|---|---|---|
| Country detection | Nearest airport's ISO code (`server/navgraph.py:618`) → "AU" | Fine for QLD; no state-level (QLD vs NSW) concept |
| Driving side | **Left-hand supported** (`navgraph.py:594`), passed to roads, signs, medians, traffic, roundabouts | — already good |
| Road markings | Own `_AU` style (`server/markings.py:61`): white, 0.10 m, 3/9 m dashes | No double-solid barrier lines (`roads.py:4831` only doubles yellow/SE/GB), no continuity lines, no yellow no-stopping lines; kerbs/gutters are stock US `curbs`/`gutter1` |
| Speed signs | Vienna C14 disc (`server/speedsigns.py:61`) | Close to AU R4-1 already; font and proportions off |
| Direction boards | **None**: AU not in `server/data/dirsigns_styles.json` (`dirsigns.py:311`) | — |
| Freeway/exit signs, shields | **None**: US-only (`signstream.py`, `exitsigns.py`) | — |
| Signals / stop / give way | Stock US/Italy models (`mod/.../trees.lua:174`) | **US-style signal heads** (AU done: see 2.5) |
| Street lamps, power | `furniture.py:32` lamps US/CA only; `BURIED_LV` has no AU | Generic |
| Buildings | One `oceania` style for AU+NZ via a lat/lon box (`server/buildings.py:387, 423`): hipped clay/metal roofs, stucco/brick | Stock US window sheets, US wooden fences (`trees.lua:178`), no verandahs/stumps/awnings, US gas stations (`gasstations.py:849`) |
| Trees | East Coast USA oak/beech/aspen + Douglas fir (`lib.rs:4791` regional rules are North America-shaped) | **Biggest single cause** |
| Seasons | Evergreen exemption only for \|lat\| < 23 (`server/season.py:54`, `seasonlook.lua:31`) | **Bug: Brisbane's oaks go rust in autumn and bare in winter** |
| Grass / ground | Hirochi green grass everywhere, 90% lawn in built-up areas, fixed stock soil textures | No dry winter brown, no red soils |
| Imagery | Brisbane falls to Sentinel-2 (~10 m); NSW source stops at −28.1° | A QLD ArcGIS source was tested upstream (`docs/research/2026-10-10-imagery-sources-world.md:126`) but **not wired in** |

The project already has a per-country style table for markings and a per-region style table for buildings. So most
of the QLD work is **filling in tables**, plus a handful of new assets and two new geometry rules.

---

## 2. Fork strategy (decide before coding)

Upstream averages **~80 commits/day** (2,311 commits since 2026-10-10). A fork that edits shared files freely will spend
its life resolving conflicts. The rules for this fork:

1. **Keep changes in data and region-keyed branches.** Add `"AU"`/`"queensland"` entries; don't rewrite general
   code paths. New code goes in new files where possible (`server/style_au.py`, `fork/`, new `.dae` assets).
2. **Pin to a tag, merge upstream deliberately.** Track `upstream/main` tags (not `dev`), merge each release into
   `qld/main`, and run tests before carrying on.
3. **Aim to upstream.** Almost all of this is "make AU correct", which upstream wants too (the project's rule is "general
   rules, no location-specific code"). Every phase below is shaped as a PR-able unit. Ask in the TGForever Discord
   (#beamng-earth) before big pieces, and check open PRs first (someone may already be on AU).
4. **Prefer Python/Lua/data over Rust for now.** Rust kernel changes need the native toolchain and version bumps. If
   upstreaming, note that upstream's policy is *new server logic in Rust*, so expect review asks to port.

---

## 2b. Asset sources: the Mount Coot-tha map

Inventory by Haiku Runner (read-only listing of the zips in the game's mods folder; nothing extracted).

**`mods/repo/mount_coottha.zip`** (B25Mitch, v1.0, "Lookout road near Brisbane", 258 MB) has **original Australian
assets** that fill several gaps in this plan:

| Asset | Path in zip (`levels/mount_coottha/art/...`) | Fills plan item |
|---|---|---|
| **26 eucalyptus meshes**: brown, dark, grey, thin, large a/b, saplings, dead, stumps, fallen branches, backdrops; own bark textures | `shapes/trees/eucalyptus/` | 1.2, 3.6 (real gums instead of West Coast USA eucalyptus) |
| **Hoop pine** (QLD native) | `shapes/trees/trees_pine/hoop_pine1.dae` | 1.2 |
| Palms a–g, tropical bushes, undergrowth (bush grass, twigs, shrub) | `shapes/trees/` | 1.2, 1.5 |
| **Reflector posts, cat's eyes** | `shapes/objects/` | 2.4 guide posts, raised pavement markers |
| **AU road-line textures** (`lines_australia_d/n/s`), kerbs, road paint | `shapes/roads/` | 1.4, 2.7 |
| Road signs (`roadsigns.dae`, `roadsigns`/`roadsigns2` textures) | `shapes/objects/` | 2.x signs (inspiration/reference) |
| Weatherboard buildings (`buildings_Q1–Q4`, `weatherboards_*.dds`) | `shapes/buildings/` | 1.3 siding textures, 3.1 reference |
| Wheelie bins (green/red), power poles, guardrails | `shapes/objects/` | 2.8, street clutter |

Fraser Island v3 and Glass House Mtns v1 (johnsy) are mostly **stock Jungle Rock Island / Utah** content under
Australian names. Their palms and tropical plants are stock, so we can reference the stock originals directly. Not useful beyond inspiration.

**Licence: none of the three states one.** So:
- **For your own build, reference the assets at runtime only.** That's the same way BeamNG.Earth already uses stock trees: preload
  `levels/mount_coottha/...` materials, and point `trees.lua` `CATALOG` groups at the meshes **when the mod is
  installed**, falling back to stock shapes when it isn't. Nothing is redistributed. Needs a small "optional asset pack"
  check in the mod (does the file exist?).
- **To ship them in the fork or upstream, first get written permission from B25Mitch** (BeamNG forum/mod repo page).
  Worth asking early: real gums plus reflector posts are a big part of the QLD look.
- In all cases, study them for inspiration (proportions, textures, how the eucalyptus LODs are built).

---

## 3. Phases

Effort: S ≈ ≤1 day, M ≈ 2–4 days, L ≈ a week or more (estimates from code reading, untested).

### Phase 0 — Fork hygiene and dev setup (must come first)

| # | Task | Why | Effort |
|---|---|---|---|
| 0.1 | Fork on GitHub, add it as `origin`, rename current remote to `upstream` | Keep upstream pullable | S |
| 0.2 | **Disable or rework `.github/workflows/release.yml`** in the fork | It publishes releases on every push to `main`/`dev` | S |
| 0.3 | **Repoint or disable self-update**: `server/updates.py:25`, `server/selfupdate.py`, `mod/.../core.lua:1819` (`RELEASES_URL`) | Otherwise the app **auto-updates itself back to upstream** and wipes the fork | S |
| 0.4 | Decide on shared-world/server-list (`earth.tgforever.net`, `earthd.py:155`) | Fork players may pollute the upstream shared world; keep it off, or keep it on only if upstreaming | S |
| 0.5 | Windows dev env: Python 3.12 venv, `pip install -r server/requirements.txt`, Rust toolchain, `python tools/build_native.py` | The committed `server/earthfast.pyd` is an upstream binary; rebuild from source to trust and change it | S–M |
| 0.6 | Run baseline tests: `python tools/run_tests.py`, `python tools/run_lua_tests.py` (`pip install lupa`) | Know what's green before touching anything | S |
| 0.7 | Pick **QLD benchmark spots** (below) and capture baseline screenshots in game | Before/after comparison | S |

**Phase 0 status (2026-11-06):**
- Done:
  - Fork at https://github.com/XavierCraggs/BeamNG.Earth. `origin` = fork; `upstream` = tecnagamer with pushes set
    to fail.
  - Branch `qld/main` from `v1.8.2-beta`.
  - `release.yml` is manual-only.
  - Update and self-update, the issue links and the User-Agent now point at the fork. A missing release is handled
    gracefully.
  - The BeamMP plugin no longer announces to earth.tgforever.net (`G.LISTED=false`).
  - The server-list fetch is opt-in (`BNE_SERVER_LIST=on`).
  - The data repo (`TecnaGamer/BeamNG.Earth-data`, read-only public data) is left as is.
  - Repo-local `core.autocrlf=false`, so files keep LF like upstream.
  - Dev venv `.venv` (Python 3.13) with requirements plus `lupa` and `fonttools`. **`install.py` was not run**, so your
    installed upstream mod is untouched.
- Baseline on clean `v1.8.2-beta`, Windows, prebuilt `earthfast.pyd` v149:
  - **Lua: 89/89 modules pass.**
  - **Server: 219/228 modules pass.**
  - The failures predate any fork change. Watch them, don't chase them:
    - Windows environment: `test_cache` (symlinks need Developer Mode), `test_gravity` and `test_seacarve` (Windows
      file locks), `test_apppaths` (path style), `test_credits` (asyncio event loop).
    - Timing: `test_colormatch` (10.11 vs a limit of 10.0).
    - Tool versions: `test_advance_sign_layout`, where the glyph atlas regenerated with fontTools differs from the
      committed one.
  - Output digests, **to look into before Phase 1.4**:
    - `test_bake_formats.test_plans_format`: the road-plan digest differs from the golden.
    - `test_imagery.test_dxt1_with_full_mip_chain`: the mip chain encoding differs.
    - Likely numpy/Pillow version or platform float differences. The plans golden is what guards `PLANS_FORMAT`, so
      a Windows-stable comparison is needed (or record our own Windows golden).
- Not done:
  - **Rust is not installed**, so the native module cannot be rebuilt yet.
  - Benchmark screenshots need you in game.

Suggested benchmark spots (each tests different things):
- **Paddington / Red Hill, Brisbane**: Queenslanders, steep streets, jacarandas
- **Pacific Motorway (M1), Gold Coast–Brisbane**: freeway markings, wire-rope median, green signs, M1 shields
- **Bruce Highway (A1) rural, near Gympie**: rural markings, guide posts, eucalypt bush, cane
- **A newer outer suburb (e.g. North Lakes)**: brick-veneer + Colorbond roofs, Colorbond fences, roundabouts
- **Toowoomba Range / Darling Downs**: dry grass, black and red soils, rural bush

### Phase 1 — Quick wins: tables only, no new assets, no Rust

**Batch 1 merged (2026-11-07):** 1.1 seasons, 1.3 Queensland buildings, 1.5 grass. **Check in game:**
- **Seasons (1.1):** Brisbane/Sydney/Melbourne with the PC date in May–August: leaves stay green, no rust patches in
  the ground photo. A northern city in October (e.g. London) should still show autumn colour.
- **Buildings (1.3):** Brisbane suburbs (Indooroopilly, Chermside, Sunnybank, Carindale) and Cairns (Edge Hill):
  - Mostly Colorbond roofs (dark grey, light grey, cream, green) with some terracotta or charcoal tile.
  - Lower-pitched hipped roofs.
  - Walls of cream, blond or red-brown brick, or white, cream or pale-grey weatherboard. No barn reds.
  - Light roofs come out a little darker than real (a texture limit).
- **Grass (1.5):** Brisbane should show clearly more pale, dry tufts and fewer lawns in suburbs (60% instead of 90%).
  Longreach should be drier still. In the game console, `beamngEarth_groundcover.status()` shows `dryShare`
  (Brisbane ≈ 0.5, rising to 0.6 in August). If it's too dry, lower `M.AU_DRY` in `groundcover.lua`. Watch the frame
  time when blocks move.


**Batch 2 merged (2026-11-07):** 1.2 trees, 1.4 markings, 1.6 gas stations. **Check in game:**
- **Trees (1.2):** Brisbane and the Gold Coast should be mostly eucalypts, with a few oaks and firs left as street
  trees, and palms in town areas. Cairns and the Daintree should have mostly rainforest trees in the tall canopy. London
  and Seattle should be unchanged. Tree cache v24 rebuilds by itself.
  - Variety is low: only 2 stock eucalyptus models.
  - The **Mount Coot-tha gums are wired in but off.** Its materials are a TorqueScript `materials.cs` that loads only
    with that map, so using them would mean our mod running that third-party script. **Your decision.**
- **Markings (1.4):** on a QLD no-overtaking section or an undivided road with 4+ lanes, the centre should be two solid
  white lines. Edge lines are 0.15 m. An ordinary two-lane road is still 3 m / 9 m dashed white.
  - Skipped: continuity lines (the code has no turn-lane concept yet) and yellow no-stopping lines (need new rules).
  - Open: QLD_REFERENCE says multi-lane lane lines are 9 m / 3 m; the code and research doc use 3 m / 9 m.
- **Gas stations (1.6):** AU/NZ use the West Coast flat-canopy kit under their own style name, `oceania` (the same
  look as before). A real Australian servo kit needs new models.

These give the biggest visual change for the least code.

| # | Task | Where | Notes | Effort |
|---|---|---|---|---|
| 1.1 | **Fix subtropical seasons**: no fall colour/bare trees where the climate is evergreen | `server/season.py:54`, `mod/.../seasonlook.lua:31` | Use `server/climate.py` `koppen_at()` (Cfa/Aw/Am…) instead of a flat \|lat\|<23 cut. General bug fix, very upstreamable | S |
| 1.2 | **Australian tree mix** with stock shapes: eucalyptus (West Coast USA `eucalyptus_double/_tall`), palms in built-up areas, Jungle Rock Island `tropical` for rainforest | `server/vegetation.py` (remap after `regional()`), `trees.lua` `CATALOG` | Do it as a Python post-remap first to avoid the Rust `regional_groups` kernel; bump `vegetation.VERSION` (23→24) | S–M |
| 1.3 | **New `queensland` building style** ahead of `oceania` in `region_style` | `server/buildings.py:340–429` | Higher brick/weatherboard share, Colorbond roof palette (Monument, Surfmist, Shale Grey, Woodland Grey…), AU brick colours, hipped share up; also update `MAT_MEAN` (:3919) | S–M |
| 1.4 | **AU marking rules**: double-solid barrier lines, 0.15 m edge lines, continuity lines at turn lanes | `server/markings.py:61` `_AU`, `roads.extras()` live block (~:4740; **ignore the dead copy after `return out`**) | Bump `FACTS_FORMAT` only if widths/lanes change; refresh goldens | M |
| 1.5 | Grass: drier GroundCover mix for AU (more West Coast USA dry grass, less lawn) | `mod/.../groundcover.lua:57`, `surface.lawns()` | Ideally seasonal: green summer, brown winter | S |
| 1.6 | Gas stations / plazas: stop AU falling to the US West Coast kit | `gasstations.py:849`, `plazas.py:76` | Pick the closest stock kit; real AU kits come in Phase 2 | S |

**Where we stopped (2026-11-07, usage limit):** merged and pushed: 1.1–1.7, 2.2 boards plus AU places, 2.4 guide posts,
the roof textures, gables, and 3.1 high-set houses. **Next up:** 3.1b house details (user chose ALL: eaves, soffit,
fascia and gutters; solar on the north slope; whirlybirds; carports; window awnings; water tanks), with no other worker
editing buildings.py. The real-sign-text research was stopped early. Partial finding from SE QLD OSM: 407
motorway_junction nodes, 194 with ref (exit numbers), only 1 with exit_to, so sign text must come from `destination=*`
on the link ways; those counts were not measured. Restart that research with a tight scope. Native module: use the
upstream committed `earthfast.pyd` (a source build changes geometry digests).

**From the in-game review and the reference photos (2026-11-07), still open:**
- **1.7 Nature strips (high impact):** Brisbane suburban streets have a grass verge between the kerb and the footpath,
  and often no footpath at all, just grass to the fence. Today we get concrete footpath right against the kerb
  (`roads.SIDEWALK`). Make it AU-aware: a grass verge ~2–4 m, the footpath set back or absent on minor residential
  streets, and roll-top (mountable) kerbs. See `fork/reference/contact_streets.jpg`.
- **Grass on asphalt (Bruce Hwy):** probably upstream (grass clump spread is larger than the 1.5 m verge). Confirm in
  game with the stock-mix console command, then cap `clumpRadius` in `groundcover.lua` `fieldsFor`.
- **Roof variety:** sheds, barns and OSM-tagged roof materials in QLD still use the stock zinc/slate.
- **Brisbane yards:** dark bare dirt with 60% lawn. Check whether this is too low.

### Phase 2 — New assets + data (moderate)

| # | Task | Where | Notes | Effort |
|---|---|---|---|---|
| 2.1 | **QLD imagery source**: wire in the tested QLD Globe/ArcGIS orthophoto | `server/imagery_regional.py` (beside NSW at :108) | **Check licence/terms first.** Biggest realism jump for the ground | S–M |
| 2.2 | **AU direction boards**: green road/freeway boards, white urban, blue services; M/A/B alphanumeric plates (green with yellow text) | `server/data/dirsigns_styles.json` (add `"AU"`) | Pure data! Check the places snapshot covers AU and that Overture `road_routes` carry AU refs | S–M |
| 2.3 | **AU speed sign** proportions + numeral font | `speedsigns.py:61` (add `"AU"` case), `speedsigns.rs:68, 749` | Needs Rust rebuild + version bumps; verify R4-1 dims against AS 1742.4 | M |
| 2.4 | **Guide posts** (white, red reflector on left, white on right; 150 m straights, closer on curves) | New `.dae` + placement module modelled on `kantstolpar.py` | New asset; very "Australian road" | M |
| 2.5 | **AU traffic signals** (300 mm 3-aspect, target boards), **red Give Way** sign | New `.dae`s, `trees.lua:174–176` group table | Stock US boom model must go; match height/orientation conventions. **Done (tools/build_au_traffic.py, `furniture.au_group`):** red Give Way R1-2 (`au_giveway`, 750 mm face on a galvanised post) wherever AU has a give-way and in place of the guessed stop signs at untagged minor junctions (stop signs only where OSM has `highway=stop`; AU now reads those nodes); signals `au_signal` (pole, 300 mm 3-aspect lantern, black target board with white border) and `au_signal_mast` (mast arm over approaches 9 m or wider), poles on the drivers' left kerb; switchable heads for the AI's junctions (`navgraph.lua` `SIGNAL_SHAPES`) | M |
| 2.6 | **Colorbond and timber paling fences** | New `.dae` (5.4 m segment, `FENCE_SEG_M`), `trees.lua:178` | Swap per region. **Done (tools/build_au_streetscape.py, `buildings.au_fence`):** Colorbond 60 % / paling 40 %, per-house hash, behind the verge's footpath; 8 models | S–M |
| 2.7 | Kerb & channel: AU Type D (layback) / Type E (barrier) look | New mod materials, `roads.lua` `MATERIAL_FILES`, name strings in `roads.py`/`junctions.py` | Requires mod-owned materials (stock ones are only referenced). **Done:** the kerb stays the stock curb sheet (layback 0.10 m on residential via `au_verge`, barrier 0.15 m elsewhere: `sidewalks.CURB_H`; the sweep is in Rust), the channel is `bne_au_channel` (`au_roads.materials.json`, 0.45 m, `roads._gutter`) instead of the US gutter decal | M |
| 2.8 | AU street lamps, overhead power on timber/concrete poles | `furniture.py:32` `LAMP_REGIONS`, `BURIED_LV` | Stock shapes first, custom later. **Done:** `LAMP_REGIONS["AU"] = "au"` (`LAMP_SPACING_AU`, a lamp on every second local pole), `BURIED_LV["AU"]` 0.25, concrete pole `au_pole_conc` on trunk/primary/secondary lines, stock timber pole and cobra-head arm elsewhere. **Roundabout centres done:** `surface.ISLAND_GRASS_AU` fill + `islands.au_garden_rows` (shrubs, one small tree) | S–M |
| 2.9 | Yellow diamond warning signs (curves, school zones), yellow no-stopping edge lines | New sign kind; `markings` rule + material | Placement logic is the hard bit (curvature, OSM tags) | M–L |
| 2.10 | Soil and ground textures: red/brown soils, cane fields | `terrain.lua:95` `LOOKS`, `surface.CLASS_MATERIAL` | Cane = new crop look on WorldCover class 40 in QLD coastal belt | M |

### Phase 3 — Big pieces

| # | Task | Notes | Effort |
|---|---|---|---|
| 3.1 | **Realism rules (user, 2026-11-07):** stumps/posts ONLY under timber houses (weatherboard/fibro: Queenslanders, post-war). Brick houses on sloping blocks get a brick base: an enclosed lower level, garage under, or split level ("highset brick"); never open stumps. Rendered houses follow the brick rule. Slope is the trigger for raising: steep block → high-set. Keep wall, gable, base and roof materials consistent per house. | — | — |
| 3.1a | **Queenslander houses**: raised floor on stumps (~2.8 m) with battened under-floor, wraparound verandah, steep corrugated roof, front stair | New geometry in `buildings.py` modelled on `_porch` (:1048) and `_roof` (:1173); fix door/stoop height logic (`doors()` :2967) and collision. Choose by hash share + era/suburb hints | L |
| 3.1b | **Australian house details** (queued after 3.1, which edits the same roof/base code): deep eaves (450–600 mm) with a lined soffit, fascia and Colorbond gutter plus downpipes; rooftop solar panels (about 1/3 of houses, north-facing slope); whirlybird vents; carports (Colorbond skillion on posts); window awnings/hoods; corrugated water tanks on rural/acreage blocks | User asked 2026-11-07 ("eaves… other australian architectural fashions"). Queensland only; other regions byte-identical | M |
| 3.2 | Footpath awnings on commercial strips | New box geometry next to `_signs`/`_cornice`; must avoid road/footpath meshes | M |
| 3.3 | AU freeway/exit/gantry signs + route shields (M1 etc.) | `exitsigns.py` is US-only and 915 lines of imperative layout; generalise or write `exitsigns_au.py`. Needs a sign-tag data source for AU | L |
| 3.4 | AS 1744-style font | Find a **freely licensed** Highway-Gothic-like face, then rebuild the atlas with `tools/build_sign_glyphs.py` | M |
| 3.5 | State-level region detection (border polygons) instead of airport/lat-lon boxes | Lets QLD differ from NSW cleanly; general improvement | M |
| 3.6 | Bespoke AU meshes: she-oaks, bottlebrush, grass trees, jacaranda in bloom, AU servo kit | Need artists or licence-clean sources (3dmr/CC0) | L |
| 3.7 | Optional "style pack" loader (JSON per region) for markings, buildings, vegetation, signs | Only worth it if several regions follow. Remember to hash pack files into the `revisit` cache key | L |

---

## 4. How agents will run each phase

| Role | Agent | Does |
|---|---|---|
| Plan, design calls, final review, in-game sign-off | **Opus (main)** | Picks the approach per task, reviews every diff, writes the PR text |
| Implementation of a specified task | **Sonnet Worker** | One task per agent (e.g. 1.1, 1.3), parallel when files don't overlap; writes tests |
| Legwork | **Haiku Runner** | Running tests, grepping callers, checking version numbers/goldens, summarising logs |
| Research | **Sonnet Worker** | Filling spec gaps (sign dims, signal heads), licence checks for imagery, fonts and assets |

Safe parallel sets (no shared files): **{1.1, 1.3, 1.5}**, then **{1.2, 1.4, 1.6}**, then **{2.1, 2.2, 2.6}**.

Every task ends with: unit tests green, version/format numbers bumped where output changed, and **you checking it in
game** at the benchmark spot against the aerial and street photos. Agents can't see the game unless you run it.

---

## 5. Gotchas (from the code surveys)

- **Caches hide changes.** The revisit cache key hashes server source + native module, so code edits invalidate it,
  but **data files read at runtime don't** unless hashed in. Version numbers to bump: `vegetation.VERSION` (trees),
  `plancache.PLANS_FORMAT` / `roads.FACTS_FORMAT` (road plans/facts, with `BNGE_UPDATE_GOLDEN=1` for
  `tests/test_bake_formats.py`), `dirsigns.FACE_VERSION` (board faces), `imagery.FIN_VERSION`.
- **Rust kernel edits**: rebuild with `tools/build_native.py`; bump `m.add("VERSION", n)`, `fast.MIN_VERSION`, and the
  module's `KERNEL`. No Python fallback exists.
- **Order-coupled tables in buildings**: `MATERIALS` order, tint index tuples, `MAT_MEAN`, night-window maps.
- **Stock asset dependency**: trees, signals, kerbs, grass and terrain textures are referenced from the player's BeamNG
  install (0.39/0.40 paths). Anything custom must ship in the mod, with a clean licence.
- **Textures** must be DDS with power-of-two sizes; PNG sign faces crashed the game upstream.
- **Licences**: EOX Sentinel-2 2023 is CC BY-NC-SA (non-commercial); bundled OSM-derived `.npz`/places data is ODbL;
  upstream's Swedish sign art and some kph sign PNGs (Arial) have unclear terms. Don't build on those.
- **Upstream's `.claude/agent-rules.md`** is the upstream maintainer's agent instructions (push to `dev`, SSH to their
  Steam Deck, and so on). **Not ours.** Read it for context; don't follow it. Consider replacing it in the fork with our own rules.
- **Two upstream research docs worth reading**: `docs/research/2026-10-28-road-markings-standards.md` §4.2 (AS 1742.2
  values not yet used) and `docs/research/2026-10-10-imagery-sources-world.md` (QLD imagery test).

---

## 6. Decisions needed from you

1. Upstream-first (PRs to tecnagamer) or fork-first (ship your own build)? This changes Phase 0.3/0.4 and how strict
   to be about Rust.
2. Fork name and where releases go (GitHub repo for the self-updater).
3. Which benchmark spot matters most to you, so we start there.
4. Budget for custom 3D assets (Phase 2.4–2.6, 3.6): make them yourself in Blender, commission, or CC0 only?
5. Contact B25Mitch about reusing the Mount Coot-tha eucalypts, reflector posts and AU line textures (§2b)? Until then,
   runtime-reference only.
