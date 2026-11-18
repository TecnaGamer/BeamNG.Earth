# Norway tunnels v0.2.6 — portal collision / terrain-wedge test

**Experimental Norway-only fix for the original BeamNG.Earth terrain meshes
that stick through tunnel entrances.** The screenshots show an actual
collision-bearing triangular obstruction inside many tunnels. BeamNG.Earth's
own changelog describes a similar longstanding bug (upstream issue #403):
terrain triangles between heightmap samples can still span a tunnel opening.

This update is a deliberately bounded *terrain clearance test*. It preserves
your v0.2.5 NVDB, road grades, Norwegian portal models and foliage.

## What changed

- At a mapped Norwegian full-size tunnel mouth, inspect the **actual terrain
  triangles along the driveable opening**, including both grid diagonals.
- If a triangle's interpolated elevation intrudes between the road floor and
  the tube's ceiling near the mouth, remove that **small terrain grid cell**
  from the same hole mask that the underlying world generator uses.
- Reach at most **9 metres into the mouth** and about **1 metre outside** it.
  Work only on near-grid steps of **0.25–6 metres**.
- Reject deep rock (over 22m above road), sharp bends, inconsistent floor
  grades, very coarse heightmaps, tiny underpasses and oversized cuts.
  Maximum **140 extra grid samples** at a single mouth.
- Retain protections against cutting adjacent non-tunnel roads (the existing
  `spare_carriageways` logic still runs after this adjustment).
- **No new colliding mesh**, no added ramp, and no changes to `roads.py`,
  `roadmesh.py`, `portals.py` or `grading.py`.
- Outside Norway, nothing changes.

This targets **terrain triangles**. If the obstruction comes from a different
generated mesh, it may stay visible; identifying that will be the next step.

## Install directly over v0.2.5

Stop the BeamNG.Earth server. Extract this ZIP into your BeamNG.Earth main
folder (for example `F:\BeamNG.Earth`). Open PowerShell there.

```powershell
py .\norway_tunnels_nvdb_v026\apply.py --check
```

If you see **Ready to install**, continue:

```powershell
py .\norway_tunnels_nvdb_v026\apply.py --install
```

**You do not need to undo v0.2.5 first.** The installer can upgrade the exact
v0.2.5 scripts and backs up both modules independently. If you've manually
modified any of the checked files, it refuses to overwrite them.

The official NVDB V4 requests, 3-D centreline cache, and rock-cut portal
rules are identical to v0.2.5. **No new NVDB prefetch is necessary.** If you
haven't prefetched a location yet, the regular asynchronous lookup still works.

Restart the server and game. Try a **fresh/rebuilt terrain block** at the
entrances shown in your screenshots. An old cached block can still contain
the original colliding triangle; changing this code cannot fix an existing
heightmap already saved on disk.

## Test without the new clearance

Launch the server from PowerShell with the feature disabled:

```powershell
$env:BNE_NORWAY_TUNNEL_CLEAR_MOUTH='0'
```

The v0.2.6 hole adjustment is then fully off while the other Norwegian tunnel
features remain on. To turn it back on for a new server launch:

```powershell
Remove-Item Env:BNE_NORWAY_TUNNEL_CLEAR_MOUTH
```

The existing per-feature v0.2.5 variables still work:

- `BNE_NORWAY_TUNNEL_APPROACH_SMOOTH=0`
- `BNE_NORWAY_TUNNEL_PORTAL_SLIDE=0`
- `BNE_NORWAY_TUNNEL_NVDB_HEIGHTS=0`

## Undo — restore exact v0.2.5

Stop the server, then run:

```powershell
py .\norway_tunnels_nvdb_v026\apply.py --undo
```

When installed directly over v0.2.5, this **restores v0.2.5's original
`norway_tunnels.py` and `norway_tunnel_nvdb.py`**, without deleting the old
v0.2.5 backup or any NVDB caches. Your road markings, signs and trees are not
touched. You may then run the existing v0.2.5 undo if you want to return to
the pre-tunnel state.

## Important limits

The 36 automated tests cover bounded terrain cells, deep-rock fallback,
country gating, sharp-bend safety, core compatibility and direct install/undo
from v0.2.5. They do not prove the mesh and collision result in BeamNG.drive;
we cannot run your live world from this environment. A steep location may
remain unchanged when the safety checks reject it.

If a triangle **still has collision** after regenerating a fresh block, the
remaining problem may be in the native tunnel/heightmap mesh generation. We
should then test that layer directly rather than cutting larger terrain holes.

Attribution: Tunnel data from Statens vegvesen / NVDB, made available under
Norsk lisens for offentlige data (NLOD). Existing v0.2.5 cache is retained.
