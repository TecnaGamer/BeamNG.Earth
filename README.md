# Norway Signs v0.7.3.1 — Missing Facing Module Hotfix

Your test failed with:

```
ImportError: cannot import name 'facing' from 'norway_signs_v073'
```

The **v0.7.3 ZIP omitted `server/norway_signs_v073/facing.py`**, even though the
new `server/norway_nvdb_destinations_v07.py` imports it. This is a narrowly
scoped repair: it adds that one module plus an offline regression test.

## Installation

Keep v0.7.3 installed. Do **not** undo it first.

1. Close BeamNG.Earth.
2. Extract this ZIP and copy `apply_norway_facing_hotfix_v0731.py`,
   `test_nvdb_sign_facing_v0731.py` and the included `norway_signs_v073`
   directory into `F:\BeamNG.Earth` (merge the directory; do not delete the
   existing `catalog.py` and `coverage.py`).
3. In PowerShell, from the repository root, run:

   ```powershell
   py apply_norway_facing_hotfix_v0731.py --check
   py apply_norway_facing_hotfix_v0731.py --apply
   py -m unittest discover -s server/tests -p test_nvdb_signs_modular_v073.py -v
   py -m unittest discover -s server/tests -p test_nvdb_sign_facing_v0731.py -v
   ```

4. If all tests pass, run your existing v0.7.3 NVDB coverage survey:

   ```powershell
   py nvdb_sign_coverage_v073.py --lat 63.11 --lon 7.73 --radius-m 450
   ```

This hotfix only restores the sign-facing function from the prior v0.7.2
behavior (and supports two-sided route shields). It does **not** solve all
physical sign-facing uncertainties, nor does it expand nationwide coverage.
It will not change your road markings, bridges, terrain, speed signs, or
existing sign textures.

## Undo

```powershell
py apply_norway_facing_hotfix_v0731.py --undo
```

The installer refuses to overwrite pre-existing files or undo edited files.
