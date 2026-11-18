# Norway foliage v0.7 — denser distant hillside forest (experimental)

**Reversible Lua-only addon for the existing Norway foliage v0.6.**

The v0.6 screenshot of distant hillsides showed scattered dark tree dots rather
than an overlapping forest canopy. The server already builds cheap, collisonless
forest groups from Norwegian trees, but normal distance-based thinning removes
many of those groups. This experiment retains more of those *existing* grouped
models without changing the expensive individual 3D roadside trees.

## What changes

- **Norway only:** The boost is applied exclusively to the pre-existing
  `norway_conifer_backdrop` and `norway_deciduous_backdrop` groups, which v0.6
  creates from off-road Norwegian woodland. Other countries are unchanged.
- **Near detail untouched:** Within 300 m of the player/tile distance there is
  no added density. Individual spruce, pine and birch models, including the
  existing 45 m road-detail band, retain their v0.6 selection.
- **Bounded fuller hills:** At normal graphics quality, the client may show up
  to **72 extra grouped forest objects per tile** between 300–700 m, **104**
  between 700–1500 m, and **72** farther away *if a tile is already loaded*.
  Existing global caps and tree budgets for non-backdrop objects stay intact.
- **Performance:** These are unchanged stock BeamNG models with their native
  billboard/impostor LODs and no collision. Extra-item allowances shrink on
  lower vegetation quality levels and expand on higher ones. There is no new
  texture, mesh, server request, cache generation, or terrain work.
- **Can be toggled** in the Lua file with `M.NORWAY_DENSE_FOREST = false`, or
  completely undone using the installer.

## Limitations

This is a **controlled mid-distance density test**, not a finished distant
mountain forest system. It does not create new distant vegetation tiles; at
normal tree distance v0.6's server generally stops sending new tiles beyond
about 1.28 km. Very far slopes therefore may still look bare. A separate
NIBIO/canopy-height forest-cover terrain layer would be the logical next
experiment if those slopes are the main issue. We have *not* added such a layer
in this pack.

FPS impact has **not** been measured in BeamNG.drive. The extra object count is
bounded and uses stock impostors, but may still lower FPS on some hardware.

## Install from your BeamNG.Earth main folder

**Requires v0.6 already installed.** Extract `norway_foliage_v07.zip` into the
main BeamNG.Earth folder so `norway_foliage_v07` is alongside `server` and
`mod`. Open PowerShell in that **main folder** and run:

```powershell
py .\norway_foliage_v07\apply.py --check
py .\norway_foliage_v07\apply.py --install
```

The script checks the SHA-256 hashes of your existing v0.6 `trees.lua` and
`server/vegetation.py` and **refuses modified or unfamiliar files**. It backs up
only `trees.lua`, which is the only file it changes. Your existing v0.6 backups
remain untouched. Restart **BeamNG.drive** to reload the changed Lua (no server
restart or vegetation cache purge is needed).

## Undo the experiment

With PowerShell in the main BeamNG.Earth folder:

```powershell
py .\norway_foliage_v07\apply.py --undo
```

This restores the exact pre-v0.7 `trees.lua` (v0.6) and preserves user edits
made after installation by refusing to erase them. Restart BeamNG.drive.
If desired, you can then undo v0.6 separately using its own installer.

## How to compare

1. Take a screenshot and note FPS on the same Norwegian hillside in v0.6.
2. Install v0.7; restart BeamNG.drive and revisit the same view/time of day.
3. Compare whether the tree crowns now merge better and FPS remains acceptable.
4. If the *very far* hill is still bare, give the location; that likely needs
   forest-cover texture work, not more individual trees.

## Offline verification

- `python -m unittest discover -s norway_foliage_v07/tests -v` — installer
  installs, refuses unknown edits, and restores exact byte-for-byte originals.
- `luatex --luaonly norway_foliage_v07/tests/lua_backdrop_test.lua` — loads the
  actual Lua module in a test harness and checks distance, quality presets,
  Norway-only matching, collision flags, and strict bonus quotas.

The experiment changes **no NVDB roads, signs, markings, bridges, grading,
SR16 classification, or terrain textures**.
