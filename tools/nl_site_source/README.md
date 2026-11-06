# Optional catalogue build cache

Only `sources.json` is versioned here. It pins all source URLs and SHA-256 hashes.
The 796 downloaded source PNGs duplicate the already generated game textures and
are not needed to install or run the mod, import George data, or run the normal
regression tests.

To rebuild catalogue geometry/textures, restore this **development-only** cache:

```text
node tools/fetch_nl_catalog_source.mjs
npm install --prefix tools/nl_test_source
node tools/build_nl_site_catalog.mjs
node tools/generate_nl_testset.mjs
node tools/build_nl_size_profiles.mjs
```

The fetcher verifies pinned hashes and refuses changed content. It does not run
when the game starts. Downloaded PNGs are ignored by Git.

The catalogue builder writes RGB 512x512 textures with lossless compression and
26 material banks of at most 32 definitions. To losslessly repack an existing
generated catalogue and refresh its material references without downloading
sources, run `node tools/compact_nl_catalog.mjs`.
