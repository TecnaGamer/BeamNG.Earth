# Discord pitch (under 2000 characters; paste as is)

Hey! I've been building an Australia/Queensland look for BeamNG.Earth in a fork (github.com/XavierCraggs/BeamNG.Earth, branch `qld/main`). QLD came out looking very North American, so I worked through it region by region. Screenshots below (Yeppoon, Rockhampton, Brisbane).

**What it changes (AU/QLD only; all other regions stay byte-identical, digests checked):**
• Houses: Colorbond and terracotta roofs, deep eaves and gutters, high-set timber houses on stumps (brick gets a brick base), solar, whirlybirds, carports, awnings and tanks; shop footpath verandahs
• Streets: AU line markings, nature strips, Colorbond and paling fences, kerb channel, timber poles, grassed roundabout islands
• Signs: green direction boards with real OSM `destination` text, red Give Way, AU signals, guide posts, yellow curve warnings, roadside W-beam and wire-rope barriers
• Nature: eucalypts, drier grass, red inland soils, cane fields
• Region detection from real state borders (Natural Earth) instead of lat/lon boxes

I know new server logic should be Rust. The fork is Python so I could iterate on looks fast. Before upstreaming I'd port each piece to `earthfast` and send it as small PRs to `dev` (about 11, e.g. markings → vegetation → buildings → signs), each with screenshots.

**Questions before I start porting:**
1. Would you take this upstream at all? If so, which piece first?
2. Region-specific logic: in Rust, or would you rather have per-region data "packs" (JSON for markings, building kits, vegetation, signs) that others could write for their own countries?
3. Golden digests: should I rebuild the `.pyd` locally and re-record, or do you record them on your side?

Happy to adjust to however you want it structured. Cheers!

---

## Screenshots to attach (best 4–6)
1. The Yeppoon suburb overview: mixed roofs, high-set house, solar.
2. A high-set timber house with its stair, beside a brick low-set house.
3. Rockhampton signalised junction: AU signals, 55 sign, green "Westwood / Bajool" board.
4. Shop footpath verandahs (the Colorbond awning on posts).
5. The roundabout with the grass and shrub island.
6. A Give Way sign or a curve warning with barriers (once checked in game).

The strongest pitch is a before/after pair from the same spot. Run the old upstream launcher once, or switch to the backup mod at `C:\Users\xavec\beamng_earth_upstream_mod_backup_1.8.2`, and screenshot the same coordinates.
