# v0.8.3 — sign-generation performance

Requires unmodified v0.8.2. Stop the generator, extract into F:\BeamNG.Earth and run from that folder:

```powershell
py apply_norway_signs_v083.py --check
py apply_norway_signs_v083.py --apply
py start.py
```

Keep existing sign textures and NVDB caches. No cache clearing or area regeneration is required solely for this performance update. Its benefit applies when the generator processes signs. Earlier visual changes still require regeneration if they have not yet been generated.

## Optimizations
Previously, every texture request rendered the complete Pillow image before checking whether the DDS output already existed. This update checks the content-keyed DDS header and dimensions first and reuses matching files without rendering. Missing files, invalid headers, mismatched dimensions and header-only files are regenerated. This is a lightweight header check, not a complete DDS corruption validator.

Font objects are reused through a bounded 128-entry cache. Sign-dimension calculations are reused through a bounded 2,048-entry cache keyed by the fields that affect dimensions. Material indices use a dictionary instead of repeated list searches. Appearance, resolution, sign content and mesh layout are unchanged. Restart the generator after changing installed fonts.

Existing v0.8.2 texture keys and NVDB cache versions are deliberately preserved. Cache hits require the DDS to remain in the requested output directory. If the parent generator deletes that directory, it will need to render textures again.

## Validation
57 offline tests passed, including all 52 earlier regressions. Added checks cover bypassing rendering on disk hits, rebuilding invalid/empty/header-only files, changed-content keys, dimension-cache invalidation and bounded font reuse. LF/CRLF installation, exact undo and refusal to overwrite local modifications passed.

An isolated benchmark of seven representative signs measured first-pass texture preparation at 1.129 seconds before and 0.998 seconds after. Ten repeated passes (70 cached requests) took 8.664 seconds before and 0.00083 seconds after. The benchmark replaced DDS encoding with an identical lightweight writer in both versions, and ran on this workspace rather than the user's PC. It excludes network downloads, terrain, mesh generation, actual DDS compression and BeamNG loading. These are sign-texture-path measurements, not whole-map loading speedups. Pixel hashes of all seven rendered images matched between versions.

This optimization does not claim a measured improvement to BeamNG's loading of an already exported map. No full game runtime is available here. Nationwide 703 diagram signs remain pending.

The installer updates server/norway_nvdb_destinations_v07.py and server/norway_signs_v073/shields_v074.py. Other bundled dependencies are for offline tests and are not overwritten by the installer.

Undo with the generator stopped:
`py apply_norway_signs_v083.py --undo`
Keep .norway_signs_backup_v083 for rollback.

Run offline tests (NumPy and Pillow required):
`py -m unittest discover -s . -p test_signs_v083.py -v`
