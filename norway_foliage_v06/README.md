# Norway foliage v0.6 — fuller birch, natural undergrowth patches

Norway-only successor to v0.5. The screenshots at Fv28 (62.4144326, 11.437733)
showed the important distinction: **the existing SR16 species classes were
largely plausible**, but the forest looked pale, disconnected and bare.
This pass improves stock tree variant choice and low vegetation without
changing the mapped species or inventing costly custom meshes.

## Changes

1. **Spruce remains spruce, pine remains pine, birch remains broadleaf.** Norway's
   SR16-to-group table is unchanged. The user's full-branch stock Douglas-fir
   silhouette still represents Norwegian pines and spruces. The catalog now
   includes more variants of that same conifer family.
2. **Fuller taller birches:** 60% of Norwegian birches whose mapped canopy is
   at least 11 m high now use the existing East Coast aspen *forest* crown
   instead of small, thin crowns scaled upwards. The tree keeps the right
   height. Colour still uses BeamNG's stock leaf material—no texture override.
3. **Clustered undergrowth:** Norwegian forest shrubs and roadside-edge bushes
   use smoothly interpolated, world-fixed patches instead of independent,
   equally spaced placement probabilities. The average item count stays
   near v0.5's.
4. **Roadside groundcover:** 36% of eligible existing shrub rows within
   approximately 22 m of a road become low green stock clover patches.
   These are *replacement rows*, not additional geometry. No collision;
   visible only within 180 m.
5. **Performance retained:** v0.5's 45-m roadside detailed-tree band and
   lower-cost distant grouped trees/imposter LODs remain. Its reduced
   artificial sapling density also remains. No new DAE or PNG textures.
6. **Norway only:** All new rules are gated by the exact Norway land outline.
   Sweden and Finland retain their previous logic. Vegetation cache **v34**.

**Limits:** This does not repaint bare grey terrain materials or cover distant
mountains with grass. Groundcover shapes appear near roads. The pack has passed
offline tests, but its in-game appearance and actual FPS remain to be tested.
No changes to NVDB road widths, markings, signs, bridges, tunnels or grading.

## Install from F:\BeamNG.Earth

Extract `norway_foliage_v06.zip` into the main directory so the
`norway_foliage_v06` folder sits beside `server` and `mod`. Open PowerShell
**in the main directory** and run:

```powershell
py .\norway_foliage_v06\apply.py --check
py .\norway_foliage_v06\apply.py --install
```

You can **upgrade directly from v0.5 or v0.4**; their backups are preserved.
The installer only overwrites recognized hashes and refuses unknown edits.
If v0.3 is still installed, undo it first.

Fully restart the BeamNG.Earth companion server **and** BeamNG.drive to
regenerate vegetation cache v34 and reload the game's Lua asset mapping.

## Revert

```powershell
py .\norway_foliage_v06\apply.py --undo
```

This restores your exact pre-v0.6 files—including v0.5 if it was installed.
Older installers' backup folders remain unchanged.

For the same Fv28 SR16 test location:

```powershell
py .\norway_foliage_v06\probe_sr16.py --lat 62.4144326 --lon 11.437733
```

Our observed z14 NIBIO map tile: approximately 68% broadleaf, 3.5% pine,
0.8% spruce, 6.1% mixed, 21.4% unclassified. The precise road coordinate
itself was unclassified (roads need not have a forest class). A successful
probe **does not prove** the game is running the new server version.

Offline checks: `py -m unittest discover -s .\norway_foliage_v06\tests -v`

Sources:
- [NIBIO SR16](https://www.nibio.no/tema/skog/kart-over-skogressurser/skogressurskart-sr16)
- [BeamNG forest and imposters](https://documentation.beamng.com/modding/levels/level_classes/forest/)
