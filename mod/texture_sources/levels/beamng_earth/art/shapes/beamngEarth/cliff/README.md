# Dynamic cliffs texture sources

Bundled lossless WebP sources use **2K color and 1K normal/scalar maps** (smaller
maps retain their authored size). The standard installer makes BC7 sRGB color,
BC5 normals and BC4 scalar DDS files with mipmaps. The game only reads DDS.
Authored PBR maps are preserved; no runtime color-derived substitute normals are
introduced. The physical repeat remains 91.44 m for natural/road-cut rock and
182.88 m vertically for the canyon chart. Lower resolution changes detail, not
that physical scale.

## Optional original detail

With `BNE_CLIFFS=1`, the cliff settings offer bundled textures or optional 4K.
Bundled is the default, and graphics presets do not opt into a download.
The choice takes effect on a world-server restart. Sources are fetched through
`netfetch`, checked against a versioned manifest (path, byte size, SHA-256),
validated as images and cached under `cliff-textures-v1` in the selected download
cache. Changed source hashes invalidate only their DDS entries. A missing or
failed download uses the bundled map; a verified cached pack also works offline.
The current base URL comes from the `cliffs` entry in the project's `cdn.json`:
`{"cliffs": {"enabled": true, "base": "https://host/versioned-pack/"}}`.
The client appends `manifest.json`; no production pack URL is hard-coded.
An absent/disabled entry or unavailable pack leaves bundled detail usable.

For testing before publication, set `BNE_CLIFFS_PACK_URL` to a base HTTPS URL,
a loopback HTTP URL (for example `http://127.0.0.1:8123/`), or an absolute local
pack folder. The override bypasses CDN discovery but still verifies the manifest,
file sizes and SHA-256 hashes. Local transports require this explicit override.
The folder/server must contain `manifest.json` with `format: 1` and a `files`
array containing `path`, `size` and `sha256` for each `<set>/<name>.webp`.
The entire feature must be enabled and the player must select 4K; bundled/off
settings never discover or download the pack.

The pack contains individual `<set>/<name>.webp` files (`natural`, `limestone`,
`arid`, `canyon`, `shared`), with the same names as the bundled sources. It contains
no DDS, stock game assets or stock derivatives. The maintainer generates the
published `manifest.json`; `inventory.json` is only a local handoff checksum list.
Do not put the optional originals in the repository or normal player package.

`tools/prepare_cliff_pack.py OUTPUT --originals INPUT` exports an original source
folder in that layout and makes the bundled maps. Once OUTPUT holds the original
pack, reruns use those files. It refuses to treat the bundled 2K colors as 4K
originals. Normal resampling renormalizes vectors; scalar resampling averages
without ringing. Artwork generation itself is not reproducible from missing
prompts; the retained sources are the inputs to the reproducible install pipeline.

## Provenance

See the per-set notices in `server/data/` and the Dynamic cliffs table in
`CREDITS.md`: `cliff_natural.NOTICE.md`, `cliff_limestone.NOTICE.md`,
`cliff_arid.NOTICE.md`, `cliff_canyon.NOTICE.md`, and
`cliff_references.NOTICE.md`. They distinguish CC0 scans, generated artwork,
procedural geometry references and numerical transition data. Exact AI model
versions were not retained and are explicitly identified as unknown. On
2026-11-23 the contributor confirmed no third-party reference images were used
for the generated limestone/arid/canyon sets and dedicated those sets to CC0.
Natural scan attribution and numeric/code licences remain separately documented.

The limestone original normal contains the fine-relief pass implemented in
`tools/enhance_limestone_normal.py` (seed 1847). Run it on an unmodified source,
never repeatedly on its own output; the bundled normal is then filtered to 1K.

## Footprint

Decimal MB, all 27 maps, measured 2026-11-20. Source size is the sum of the
individual downloadable WebP files, excluding HTTP/archive overhead. Both installed sizes were measured after converting every source with the native DDS encoder and checking the resulting formats, dimensions and mip chains.

| Component | Bundled | Optional original set |
| --- | ---: | ---: |
| Lossless source/download payload | 56.02 MB | 303.37 MB |
| Installed DDS including mipmaps | 53.13 MB | 378.19 MB |
| Compressed blocks if every mip of every map is resident | 53.13 MB | 378.19 MB |

The last row is a texture allocation estimate, **not measured game RAM or VRAM**.
It excludes engine overhead, geometry and locally prepared stock maps. Optional
sources remain in the disk cache in addition to installed DDS. Every player gets
the bundled sources; disabling cliffs skips their normal install-time conversion.
Both categories share prepared maps; disabling both skips startup preparation.
Neither texture resolution enters geometry cache identity.

## Private stock detail

BeamNG-owned rocky-dirt maps, stock rock meshes and their derivatives are not
redistributed. `cliffstock.py` reads the player's installation before streaming.
Private versioned caches live under the game user folder's
`temp/beamngEarth/shapes/cliff_contact_v1`, `cliff_ground_v1` and `cliff_talus_v4`.
Missing stock data skips the dependent decoration with a diagnostic. Standalone
installation accepts `install.py --game-dir`; the launcher uses its selected
installation. Neither disabled category triggers private preparation, and
`--dry-run` does not write files.
