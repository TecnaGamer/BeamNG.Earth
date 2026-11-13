# Dutch road signs and public NDW tile pipeline

This opt-in integration extends the current dev shared road-sign renderer with
public NDW attribute tiles and a reproducible dataset producer. The earlier
storefront faces, per-face DAE catalogue, private store and blanket Dutch sign
replacement are excluded. Groenpol signals are included separately in the same
PR; see `NL_VRI.md`. BGT remains outside this change.

## Enable

Start the server with both `BNE_ROADSIGNS=on` and `BNE_NL_ROADSIGNS=on` in its
environment. Keep **Road signs** enabled in the game's settings. Restart the
server after changing either environment variable. Both server flags default
to off; enabling ordinary road signs alone does not enable Dutch NDW signs.
Groenpol signals have their own opt-in setting in the game.

When the public dataset is unavailable, four roles use the road and
infrastructure data already streamed by Earth:

| RVV sign | Atlas role | Placement input |
| --- | --- | --- |
| B6 | `prio.give_way` | Mapped give-way infrastructure |
| B7 | `prio.stop` | Mapped stop infrastructure |
| C2 | `reg.no_entry` | An eligible wrong-way entrance to a one-way road |
| D1 | `mand.roundabout` | An approach to a detected roundabout |

The last two are inferred scenery, not surveyed sign positions. The public NDW
reader additionally supports the RVV-to-atlas mappings in `server/nlrvvdata.py`
and numeric A1 speed limits. Complete plain-text underboards are preserved in
order and rendered with shared plates and the existing lettering atlas. Unknown
artwork, zones, symbol underboards, time conditions and incomplete qualifiers
are rejected rather than turned into an unconditional restriction. Qualified
NDW signs never suppress unconditional inferred signs.
This does not reproduce the earlier 827-face catalogue. A matching nearby
inferred sign is omitted only after its same-facing, same-meaning NDW counterpart
has successfully produced a mesh. Missing data never clears existing signs.
There is no country-wide hide/remove operation or promise of deduplication
against arbitrary custom mods.

## Assets and rendering

These four designs reuse the repository's existing vector drawings in
`tools/build_sign_atlas.py`: `give_way`, `stop`, `no_entry` and `d_round`.
STOP lettering is drawn from strokes, without an external font. No downloaded
sign face, photograph, vendor image or new per-sign model is included. The
existing `vienna_w` atlas and its metal material are shared with other countries.
The atlas generator remains reproducible with `python tools/build_sign_atlas.py`.

The RVV identifiers refer to [RVV 1990, Annex 1](https://wetten.overheid.nl/BWBR0004825/#Bijlage1).
The artwork is simplified game scenery, not certified fabrication artwork.
Existing placement dimensions are provisional game defaults, not a claim of
compliance with all Dutch siting requirements.

The existing native server kernels place and batch all panels, backs and poles
into the tile's single `rsigns` mesh, with two shared materials. No per-sign
Lua update loop or separate DAE catalogue is added. The existing pavement and
bridge checks, elevation sampling, optional sign queue, tile unload and settings
switch remain in charge. Missing map inputs produce no pilot signs.

## Public NDW dataset and hosting

`python tools/build_nl_rvv.py verkeersborden_actueel_beeld.csv.gz output/nl-rvv`
compiles the official CSV into zoom-12 gzip JSON tiles. The manifest records
the input hash, source, license, coordinate/bearing convention, rejection counts
and duplicate count. A build requires an empty output folder and writes its
manifest last. The included manually triggered workflow builds a downloadable
artifact; it has no deployment permissions and publishes no raw CSV or images.

`server/nlrvv.py` reads `<BNE_DATA_PAGES>/nl-rvv/manifest.json` and its shards using
the existing `railtags.Shards` disk cache, manifest data-host support, failure
backoff and build invalidation. Decoded NDW shards are limited to 16 in memory.
Only a road tile's records are rendered, in its existing optional sign queue.
No game request downloads the national CSV or contacts the George API. Pole feet
must clear drawn pavements and bridge footprints; the source compass heading is
transformed into the current frame. Runtime records carry source IDs only,
coordinates, heading, RVV code, supported role/speed and complete supported
underboard text; photo URLs are removed. Format 2 prevents older readers from
silently discarding qualifiers. Build IDs include the producer revision.

[NDW's general data terms](https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden/)
state CC0 unless a dataset or product states otherwise. Verify the specific
traffic-sign export's terms and record provenance before publishing it; that
general statement alone is not evidence of rights to third-party pictures.
Hosting and dataset publication still require the BeamNG.Earth-data maintainer
to deploy the generated `nl-rvv` directory through the existing site/data host.
That external deployment is not performed by this PR. Until then the reader
fails open and the four-role fallback remains. `BNE_DATA_PAGES` can select a test
host with the same layout; it must include the other normal Earth datasets too.

Validation on the first 10,000 official CSV records produced 4,704 normalized
records in 865 tiles, collapsed one duplicate, and rejected 2,462 unsupported
artwork variants, 2,698 qualified/zone records, 125 non-roadside mounts and four
invalid locations/headings. Text that cannot fit legibly or lacks font glyphs
is additionally rejected during placement. This sample is not the national dataset
and does not measure placement acceptance against generated roads.

## Validation

`python -m unittest discover -s server/tests -p test_roadsigns.py` checks the
country switches, supported roles, existing-country behaviour and shared mesh.
`python tools/build_sign_atlas.py --check` checks drawing/role consistency.
`python -m unittest discover -s server/tests -p test_nlrvv.py` additionally checks
public normalization, qualifiers, deduplication, native batching, safe fallback,
public tile reads and persistent caching. Visual in-game review remains needed.
