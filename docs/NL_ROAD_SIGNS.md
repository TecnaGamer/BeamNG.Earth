# Dutch road-sign pilot

This is a small, opt-in addition to the current dev road-sign renderer. It does
not contain the assets, private NDW store or client-side replacement logic of
the earlier Dutch signs PR. BGT and traffic lights are outside this change.

## Enable

Start the server with both `BNE_ROADSIGNS=on` and `BNE_NL_ROADSIGNS=on` in its
environment. Keep **Road signs** enabled in the game's settings. Restart the
server after changing either environment variable. Both server flags default
to off; enabling ordinary road signs alone does not enable the Dutch pilot.

The pilot uses the road and infrastructure data already streamed by Earth:

| RVV sign | Atlas role | Placement input |
| --- | --- | --- |
| B6 | `prio.give_way` | Mapped give-way infrastructure |
| B7 | `prio.stop` | Mapped stop infrastructure |
| C2 | `reg.no_entry` | An eligible wrong-way entrance to a one-way road |
| D1 | `mand.roundabout` | An approach to a detected roundabout |

The last two are inferred scenery from the existing server rules, not a survey
of individual sign positions. This is not nationwide NDW coverage. Unsupported
types are excluded by an allowlist, including newly added global roles until
reviewed for the Netherlands. Existing signs and speed-limit signs are left
alone; there is no country-wide hide/remove operation. The pilot therefore
does not promise deduplication against every other sign source or custom mod.

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

## Follow-up: publicly available source positions

An NDW implementation should be a separate, reviewable dataset producer for
BeamNG.Earth-data and its normal tile host, with a reader using Earth's cached
tile fetching. It must not require the contributor's local store or contact the
George API from gameplay. Only identifiers, RVV codes, coordinates, direction
and applicable qualifiers should be exported; vendor pictures and photographs
must not enter the atlas or dataset.

[NDW's general data terms](https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden/)
state CC0 unless a dataset or product states otherwise. Verify the specific
traffic-sign export's terms and record provenance before publishing it; that
general statement alone is not evidence of rights to third-party pictures.
Hosting and dataset publication require coordination with the data repository's
maintainer. This pilot needs neither that publication nor a private data folder.

## Validation

`python -m unittest discover -s server/tests -p test_roadsigns.py` checks the
country switches, supported roles, existing-country behaviour and shared mesh.
`python tools/build_sign_atlas.py --check` checks drawing/role consistency.
Visual in-game review of Dutch approaches remains necessary before treating
this experimental placement as complete.
