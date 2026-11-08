# Getting the AU work into BeamNG.Earth (rewritten 2026-11-08)

## Where things stand
- **Fork:** 79 commits, 162 files, about 12.6k lines on top of upstream `a4053d3a`. Roughly 25% is server code, 28% generated shapes, 17% tests, 11% tools and 5% `fork/` docs.
- **Upstream `dev`:** 184 commits ahead of our base. It touches six of our files: buildings, roads, surface, vegetation, furniture and blockworker.
- **What changed upstream, and why it matters:**
  - **PR #505, European regional scenery,** was merged as one large squashed commit. It is opt-in and off by default (`--regional-poles europe`, `BNE_FR_*` env flags), and was mostly Python. The maintainer then did their own speed and memory pass and ported the hot path (the country lookup) to Rust: `countrymap.rs`, `country_codes`.
    - This is our template: one regional-scenery feature behind an opt-in switch, with an A/B timing table. A dozen tiny PRs is not the shape they merge.
    - It also already places **chevrons and delineators** in Europe. Our chevron markers (CAMs) and guide posts must reuse its placement code and only add an AU look. A second system would be the first thing a reviewer rejects.
  - **PR #597** (town signs) and **PR #843** (highway shields, ramp tapers) both touch the same sign and road code we do.
  - **Add-on system** (`docs/ADDONS.md`): a registry of third-party mods. This is a fallback route if parts are refused: AU *assets* could ship as an add-on, but server logic can't.
- **Policy** (`.claude/agent-rules.md`):
  - New server logic belongs in Rust, and PRs target `dev`.
  - In practice (#505) Python was accepted and the maintainer ported the hot spots. **Ask before porting anything.**

## Phases
Each phase ends with one full test run; during work, only targeted tests are run.

### A. Decided from #505's precedent (no need to ask)
1. One opt-in switch, default off, sent as a few themed PRs.
2. Python first, with an A/B table; the maintainer ports the hot spots.
3. The NVIS grid (1.1 MB) is bundled, like #505's 0.7 MB `fr_house_*.bin`. Only data of tens of MB goes to BeamNG.Earth-data.

### B. Sync with upstream `dev`
- Merge `upstream/dev` into `qld/main`, resolve the six overlapping files, and re-run the install.
- Then build every AU feature on top of the shared parts:
  - country from `countrymap` instead of our own polygons;
  - #505's chevron and delineator placement;
  - #843's ramp geometry for Wrong Way signs.

### C. Fit the upstream shape
1. **One switch.**
   - All AU behaviour goes behind a single setting/flag in #505's style (e.g. `--regional-scenery au`, or extend `--regional-poles` to take `au`). Off means byte-identical tiles to upstream.
   - Today it is gated on "is in Australia" only, which changes default output for every AU player. A reviewer will want it opt-in at first.
2. **One region lookup.**
   - Country comes from `countrymap.country_codes`. Keep `climate.au_state` (Natural Earth admin_1) only if states are still needed: QLD vs other building styles.
   - Ask whether admin_1 can join the countrymap band files.
3. **Cache keys:** the switch and the AU data files go into the format/revisit keys, so turning it on or off rebuilds tiles.

### D. Strip
- **Fork-only material** (never upstream):
  - `fork/`;
  - tester release notes;
  - the update REPO pointing at the fork;
  - the release workflow and `LISTED` edits;
  - the Mt Coot-tha hook;
  - `fork/reference/` photos;
  - the parked LiDAR branch.
- **Dead code sweep** (one Haiku pass): functions, constants, materials and shapes nothing references. Examples: unused speed-sign values (70/130 cells), superseded T-board meshes, leftover debug env flags.
- **Assets:**
  - Every PNG quantized; every `.dae` built by its `tools/build_au_*.py`.
  - The builders merged into one `tools/build_au_assets.py`, so a reviewer can regenerate everything with one command.
- **Docs:** one `docs/AUSTRALIA.md`, like #505's docs: what it adds, the data sources, the standards followed (AS 1742) and the switch. Credits go in `CREDITS.md`:
  - NVIS (CC BY 4.0);
  - Natural Earth;
  - the Roadgeek 2014 font — check its licence permits redistribution in the app.

### E. Trim tests
- Keep **behaviour tests**: placement happens or doesn't, side and facing, gating off outside AU, switch off means unchanged output.
- Drop tests that only pin tuning constants: exact sizes, colours, counts.
- Merge the per-feature AU test files into a few: `test_au_roads`, `test_au_buildings`, `test_au_vegetation`, `test_au_amenities`.
- Fix the two failures that predate our work (`test_bake_formats.test_plans_format`, and the `test_roads` import in `test_speed_signs`), or confirm they also fail on clean `upstream/dev`.
- Re-record digests only for AU cases; check that the non-AU digests are untouched.
- Target: about half the current test lines.

### F. Measure (the maintainer always does this, so pre-empt it)
- A/B over 16 road tiles in Brisbane and 16 rural tiles near Rockhampton, with buildings on and revisit off:
  - switch off vs upstream (should be noise);
  - switch on: time and peak memory.
- Profile anything over +15% and fix or port the hot spot. Likely candidates: building geometry (1.3k lines) and the vegclass lookup.

### G. Split into PRs, each opt-in under the same switch and each with screenshots plus the A/B table

| # | PR | Main parts | Note |
|---|---|---|---|
| 1 | AU switch, region lookup, roads and signs | markings, speed signs and AU limits, Give Way, signals, guide posts (via #505 delineators), curve warnings, CAMs (via #505 chevrons), T boards, Wrong Way, gravel, no-stopping lines, direction board style | Most visible, lowest risk; sets up the switch the rest reuse |
| 2 | AU vegetation | NVIS grid, palettes, eucalypt/rainforest/mallee groups, dry long grass, wet/dry season | Data hosting decided in A |
| 3 | AU streetscape | nature strips, fences, kerb channel, poles and lamps, roundabout gardens, crash barriers (`aurails`) | |
| 4 | AU houses and shops | high-set houses, Colorbond and tile roofs, eaves, solar, carports, tanks, awnings | Largest diff; may need splitting or porting |
| 5 | AU street amenities and servos | payphones, post boxes, servo kit | Could be made generic (any country's phone box or post box from OSM) |

Mechanics for each PR:
- Cut a fresh branch from `upstream/dev` and re-apply by hand, not by cherry-pick.
- Normalise line endings to upstream's.
- Write commit messages in upstream's long descriptive style.
- Add the screenshots and A/B table to the PR body.
- Send them one at a time, waiting for each to merge.

### H. Fallback
If a part is refused as too regional, ship its assets and Lua as an **add-on** through the registry (`docs/ADDONS.md`), and keep only generic hooks upstream.

## Order and cost
1. A, then B (needed regardless).
2. C and D together in one pass.
3. E.
4. F.
5. G, one PR at a time.

B and C are the expensive steps: conflicts, plus rewiring onto #505's code. D and E are cheap, mechanical Haiku/Sonnet passes.
