# Shared road-plan bundles (experimental)

This opt-in feature prepares geographic road-grading plans once and distributes the same verified
plans to multiple companions. It uses the existing anchor-independent planning regions, so clients
with different floating origins can reuse the same road heights. It does **not** share the entire
terrain, bridge meshes, buildings, vehicle physics or imagery. Automatic BeamMP peer sharing remains off.

## How it works

1. A companion generates complete road plans through the existing grading pipeline. Only files with
   matching version 2 provenance markers may be exported. Each marker binds the bytes, geographic cell,
   settings, generation-time code identity and source versions. Plans produced while retrieval failed,
   older hash-only markers, mixed source versions and relabeled generator settings are rejected.
2. Export produces a deterministic manifest and content-addressed NPZ artifacts. The manifest records
   generator/native compatibility, geographic cell keys, settings, locally known source versions,
   an operator-declared input snapshot digest, attribution, byte sizes and SHA256 hashes.
3. An operator distributes the manifest ID out of band. Clients fetch the manifest and unique artifacts,
   enforce limits and verify hashes/array schemas before atomically installing a complete directory.
   Repeated cells with identical artifacts download only one blob. A warm installed bundle uses no network.
4. For an explicitly pinned session, `BNGE_WORLD_BUNDLE` names that directory. Grading consumes its plans;
   missing cells, incompatible settings/source versions or corrupt data fail instead of silently generating
   different plans. Restart the companion to change bundles. There is no live bundle-switch feature.

This is prepared-data distribution, not an online job scheduler. There is no download or HTTP request on
the driving thread. The input snapshot digest is an operator assertion, not automatic proof that every
DEM sample or collision asset was captured. The local DEM and other world assets still matter.

## Operator workflow

Commands work from source as `python tools/world_cache.py ...`. The player executable also exposes
the same tool as `BeamNG.Earth-<VERSION>.exe --world-cache ...` (Linux: AppImage `--world-cache`).
Replace `<VERSION>` with the version from the build's `VERSION` file. Use a build containing this contribution.

First drive or pre-build a bounded region with the same generator code, grading enabled, and no bundle pinned.
Find the resulting `plans/<run>/r10/w0s0` (or the actual width/water signature) in the companion cache.
Regenerate plans written with older hash-only markers using this implementation to obtain version 2
`.npz.complete` provenance. Existing markers must match the producer's code, settings and source versions.
For an export preparation run, set `BNE_REVISIT=off` before starting the producer so an older prepared
cache cannot skip generation of the new provenance; restore the usual setting for normal play.
Export with the same code and generator settings used for that run. Include neighboring planning cells needed
by the route; pinned mode rejects missing cells instead of extending a pin automatically.

```sh
python tools/world_cache.py source-versions PATH_TO_COMPANION_CACHE > sources.json
python tools/world_cache.py export PATH_TO_PLAN_SIGNATURE_DIR prepared-region --sources sources.json --input-digest YOUR_SNAPSHOT_SHA256 --credit "Actual provider attribution and license"
python tools/world_cache.py verify prepared-region
python tools/world_cache.py serve prepared-region --port 47622
```

The source-versions command reads local metadata offline, including bounded cached rail/lane manifests.
Missing source metadata stays unknown; malformed metadata is rejected. The exported source values must
match the generation-time records in every selected plan. Use the producer's versions and record the
actual input snapshot/digest and licensing separately; do not invent them or use the examples as provenance.
Export only data you are allowed to redistribute and retain all required notices.
Plans of Hong Kong cells made with its measured road heights (the `hk` source of `server/roadz.py`) or its LiDAR
terrain (`server/hkdtm.py`), both on by default, get no provenance marker, so they cannot be exported; BeamMP sharing
and baked packs leave them out too. Produce a Hong Kong region with `BNE_ROADZ_HK=off` and `BNE_HKDTM=off` to export
it.

The included server binds **127.0.0.1 only** and serves just the selected manifest and hashes, without
directory listings or arbitrary file paths. It limits concurrent handlers and request duration. For a
remote service, publish the immutable bundle using an operator-managed HTTPS server/reverse proxy with
appropriate access controls. The included stdlib HTTP server is a local tool, not a public production service.
BeamMP gameplay still uses its own server and transport; do not send these artifacts through vehicle events.

On each client, obtain the expected manifest ID from the operator and fetch before launching:

```sh
python tools/world_cache.py fetch https://YOUR_CACHE_HOST/region client-worlds --manifest-id EXPECTED_SHA256
```

The result names `client-worlds/<manifest-id>`. In PowerShell, set the returned absolute path only for the
desired session, then launch the Earth player app:

```powershell
$env:BNGE_WORLD_BUNDLE = 'C:\absolute\client-worlds\EXPECTED_SHA256'
try { & '.\BeamNG.Earth-<VERSION>.exe' } finally { Remove-Item Env:BNGE_WORLD_BUNDLE }
```

Keep grading enabled and retain the producer's compatible width/water settings and source metadata.
Clients need actual matching local provider manifests when the bundle records known rail/lane releases;
missing or differing releases fail compatibility checks. Acquiring those source snapshots automatically
is outside this experimental workflow. Changing source metadata or bundles requires a companion restart.
For the local proof, fetch `http://127.0.0.1:47622`; plaintext HTTP is refused for non-loopback hosts.
Redirects, embedded URL credentials and query-string tokens are refused. Trust comes from the separately
obtained manifest hash plus HTTPS, not an unauthenticated discovery announcement.

## Failure and resource behavior

- Up to 256 artifacts, 32 MiB per artifact, 256 MiB declared bundle data and 1 MiB manifest; strict numeric,
  array/ZIP and metadata validation runs before plans enter native grading. No pickle deserialization.
- Fetch uses four bounded workers and a 512 MiB default local storage budget (`--max-cache-mb`). It refuses
  excess capacity; it never evicts a potentially active bundle. Remove unused versions explicitly.
- The destination permits one installer at a time. `.fetch.lock` reports concurrent installation; after a
  confirmed crashed installer, remove that lock and abandoned `.stage-*` directory before retrying.
- Interrupted/corrupt/mismatched fetches never replace an existing bundle. Activation is explicit for the
  next session. A valid warm bundle remains usable without the server.
- Pinned mode disables incompatible revisit reuse and provisional ungraded first blocks. Its stricter
  safety checks may increase startup latency; it is not the default recommendation for normal driving yet.
- Invalid bundles are rejected before cache maintenance starts. A later source compatibility failure
  closes the temporary source reader and network accounting resources before returning the error.
- Missing region or damaged pinned data reports a build error. Retaining previous collision-safe ground
  through that error still requires real BeamNG acceptance; offline tests cannot certify engine behavior.

## Verification and boundaries

`test_worldbundle.py` exercises real grading over synthetic roads with different anchors and verifies
no new source fetches by consumers, strict plan data validation and integration failure paths.
`test_world_cache.py` uses a real loopback HTTP service and isolated client directories, covering cold/warm
reuse, duplicate content, hash mismatch, quota, traversal, changed server data and competing installers.

Run `python tools/benchmark_world_bundle.py` for a reproducible synthetic before/after report. It separates
producer generation, export, network transfer, consumer application and warm reuse. Small-fixture transfer
overhead can exceed saved generation time; use the measured totals rather than assuming a speedup.
The result does not measure a real city, FPS, BeamMP stress, or full terrain equivalence.
The [recorded five-run experiment](research/2026-11-02-world-bundle.md) halved source queries but was
slower cold on its small synthetic input (234.79 ms shared versus 27.10 ms independent).

Next steps are provider/input snapshot automation, complete terrain/asset dependency closure, authenticated
server coordination and a real two-client drive. Redis and SIMD remain contingent on measured bottlenecks.
