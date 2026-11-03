# Archived fork validation — 1.8.1

This historical record concerns `redthepro/Beam-Earth`, not an upstream release published by
`TecnaGamer/BeamNG.Earth`. It preserves review findings and validation evidence for the contribution;
the current PR's checks remain authoritative for the adapted upstream branch.

Fork version 1.8.1 followed a release review of the fork's 1.7.4-beta implementation. The pending 1.8.0 publication
was cancelled in favor of that version. It retained the gameplay implementation, reliability fixes and
experimental shared road-plan support, with the review fixes listed in the changelog. This does not
complete the [modernization roadmap](PLATFORM_STATUS.md) or add runtime acceptance.

## Review findings addressed

| Finding | Correction and regression coverage |
|---|---|
| Duplicate Earth license/credit members in hosting ZIPs | Retain one copy from the plugin ZIP; both platform package tests assert unique members and preserved notices. |
| Update notice could announce an unpublished main-branch version | Query the same published full-release endpoint as the downloader; test unpublished, draft, prerelease, malformed and foreign responses. |
| Numeric plan feature IDs could pass validation and crash grading | Require nonempty bounded string IDs before a bundle is activated. |
| Slow response headers or bodies could outlive the request budget | Interrupt the active HTTP transport at the deadline and clean up its watchdog; system DNS resolution remains OS-managed. |
| Export could relabel old plans with current generation settings | Bind generation-time identity, source versions and cell metadata to each complete plan; reject mismatched or legacy markers. |
| Cached rail/lane releases were invisible during pin validation | Read bounded cached manifest metadata without downloads, and clean up resources on failed startup compatibility checks. |

Older hash-only export markers needed regeneration for fork 1.8.1. This affects the experimental bundle
export workflow; ordinary gameplay does not require a bundle. See [shared-plan setup](SHARED_WORLD.md).

The provenance/integration review passed 176 focused cases with four existing skips. A two-iteration
[generation/export/transfer fixture](research/2026-11-02-release-1.8.1-world-bundle.json) confirmed equal
geographic heights, zero consumer source queries and zero warm requests. Its median cold path remained
slower with sharing (125.96 ms versus 22.45 ms); this is a small synthetic regression fixture, not gameplay.

The preceding fork release passed both Windows and Linux quality gates and player build/startup checks.
Its final local gate passed 446 Python cases (five skipped), 72 Lua modules, two JavaScript checks and
four Rust tests. The [fork's published 1.8.1 release](https://github.com/redthepro/Beam-Earth/releases/tag/v1.8.1)
records final build evidence and SHA-256 checksums for those downloads. These are historical fork artifacts,
not upstream downloads or evidence that the upstream PR has already been released.

Windows player compilation runs locally with the custom MSVC bootloader and native earthfast 116;
Linux AppImage compilation runs in CI. Windows and Linux hosting bundles include unchanged official
BeamMP 3.9.3 binaries, licenses, corresponding source and empty-key configuration. Those BeamMP binaries
are not compiled by this project. Players need BeamNG.drive; hosts provide their own BeamMP AuthKey.

The historical [shared-plan measurements](research/2026-11-02-world-bundle.md) remain available: consistent heights
and fewer source queries were verified in 1.7.4-beta, but the tiny synthetic cold-load path was slower. No new
loading-speed or FPS claim is made. Shared plans remain opt-in, bounded and incomplete as a full-world
representation. Actual two-client driving, long-session stability and network-fault recovery remain
unverified; embedded voice, Steam parties, radio and downloaded-model placement remain unfinished.
