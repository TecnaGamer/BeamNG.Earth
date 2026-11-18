# Norway tunnel NVDB v0.2.5 — measured grades and safe portal transitions

**Experimental Norway-only update, not an in-game verified fix.** This version
is specifically aimed at a tunnel road abruptly rising into the portal or a
portal face sitting on a terrain elevation spike. It builds on your working
v0.2.4 NVDB prefetch and keeps the rest of BeamNG.Earth as-is.

## What's different

- **Shared road/tunnel elevation:** A short, isolated DEM height spike at a
  road endpoint joined to a mapped tunnel may be corrected *in the original
  road grading plan*. The tunnel's normal endpoint pin follows that plan.
  No extra road, ramp, overhead road strip or colliding approach slab is made.
- **Driveable grade constraints:** Corrections are accepted only when the next
  40–95m give a stable reference grade, the initial spike fades within ~30m,
  the correction is at most **5.5m**, and the result stays below ~17% grade.
  Genuine steep roads and severe unknown anomalies are left unchanged.
- **NVDB 67 3-D tunnel axes:** The new cache stores properly georeferenced
  *LINESTRING Z* centreline elevations. When an official line closely matches
  *the whole mapped bore*, both endpoint heights agree with the graded road,
  the result is driveable and the vertical adjustment is <=3m, we can use its
  actual grade between those fixed endpoints. Parallel/ambiguous tunnels are
  rejected. No guessed height datum offsets or forced Aursjøvegen floor.
- **Conservative interior mouth relocation:** Where an existing mapped tunnel
  contains an open-to-covered boundary at a steep, thin terrain lip, it may
  shift **up to 14m inward along the same tunnel polyline**. The newly exposed
  ground must have <=2.2m cover; deep rock cuts and full-bore network ends are
  never moved. Its road, tunnel mesh and terrain hole use the *same* section
  boundary, rather than moving a portal prop independently.
- **Nationwide by design:** These rules are available for mapped tunnel roads
  anywhere inside Norway, without a hard-coded Molde/Ringebu/Aursjøvegen map
  location. NVDB V4 queries are asynchronous and locally cached.
- **Portal appearance retained:** Successful type-69 zero-object results choose
  a rock-cut entrance on an *already mapped* tunnel; verified portal sections
  may choose a box/arch. Errors and missing data keep the original fallback.
- **Old v0.2.3 ramps permanently off:** The obsolete
  `BNE_NORWAY_TUNNEL_EXTRA_GEOMETRY` environment flag is ignored. No synthetic
  24m approach ramps or extra terrain lip masks, even if that flag remains set.

## Installation from F:\BeamNG.Earth (PowerShell)

1. **Stop your BeamNG.Earth server.** Extract the ZIP directly into
   `F:\BeamNG.Earth`, so that `F:\BeamNG.Earth\norway_tunnels_nvdb_v025\apply.py`
   exists. Do not copy the ZIP contents into `server` manually.
2. If **v0.2.4 is installed**, restore its original module first:

   ```powershell
   py .\norway_tunnels_nvdb_v024\apply.py --undo
   ```

   If it refuses, stop and send its output. **Do not install v0.2.5 over an
   active v0.2.4 or any other modified source file.**
3. Check and install v0.2.5:

   ```powershell
   py .\norway_tunnels_nvdb_v025\apply.py --check
   py .\norway_tunnels_nvdb_v025\apply.py --install
   ```

   Only continue to `--install` if the check reports **Ready to install**.
   It verifies exact SHA-256 values for the previously supplied core scripts.
4. Prefetch the initial Norway test points:

   ```powershell
   py .\norway_tunnels_nvdb_v025\prefetch.py
   ```

   This produces a **new v0.2.5 3-D cache** at
   `server/data/norway_tunnel_nvdb_v025/`. Previous v0.2.4 cache files are
   retained and will work again if you roll back. Data fetched when the game
   first visits another tunnel will be used after that location's tiles are
   regenerated. No network request blocks road generation.
5. Restart server and game. Visit a fresh/rebuilt tunnel tile and check both
   ends *at driving height* as well as from above. Earlier generated road and
   terrain tiles may still be stale.

### Test a specific portal

```powershell
py .\norway_tunnels_nvdb_v025\prefetch.py --lat 61.549004 --lon 10.0062711 --force
```

Use your Molde Årø portal's exact coordinates in `--lat` and `--lon` if you
want to warm its cache before rebuilding. The API reports the actual number
of type-67 records containing usable 3-D centreline geometry. A count of zero
is not evidence that a tunnel is absent. Only **type 69** zero records, when
retrieved successfully, affects the rock-cut portal style.

## Settings for controlled comparisons

The following environment settings can disable each new feature independently
*before launching the server*. Setting any to `0` retains the corresponding
stock v0.2.4 behaviour:

| Variable | What it disables when `0` |
|---|---|
| `BNE_NORWAY_TUNNEL_APPROACH_SMOOTH` | Isolated DEM spike correction on a matched approach road |
| `BNE_NORWAY_TUNNEL_PORTAL_SLIDE` | Shallow internal mouth relocation |
| `BNE_NORWAY_TUNNEL_NVDB_HEIGHTS` | Type-67 tunnel-grade alignment |

Example:

```powershell
$env:BNE_NORWAY_TUNNEL_PORTAL_SLIDE='0'
```

This setting is for the current PowerShell server process only. To clear it
before a later launch use `Remove-Item Env:BNE_NORWAY_TUNNEL_PORTAL_SLIDE`.

## Safe rollback

Stop server, then:

```powershell
py .\norway_tunnels_nvdb_v025\apply.py --undo
```

The installer restores the **exact original** `server/norway_tunnels.py` from
`norway_tunnels_nvdb_v025_backup/`, moves the v0.2.5 NVDB helper into its
backup, and leaves all cached data and other Norway mods untouched. Restart
server; previously generated tiles may need rebuilding. Older v0.2.4 files
and its separate backup remain in place.

## What remains unproven

Unit tests validate the 3-D WKT interpretation, road/tunnel elevation
continuity conditions, bounded portal sliding, stock fallbacks and reversible
upgrade. **We cannot run the BeamNG.drive renderer against your live map from
here.** A portal built across very steep rock or a broken original road mesh
can still be undriveable. The engine deliberately refuses risky grade changes
or deep portal moves; some difficult sites may therefore look unchanged.

This package reads official NVDB V4 under the public NLOD terms. Attribution:
Contains data made available by Statens vegvesen / Nasjonal vegdatabank.
