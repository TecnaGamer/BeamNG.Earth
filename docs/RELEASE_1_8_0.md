# 1.8.0 release record

Version 1.8.0 is the requested version and packaging bump from 1.7.4-beta. It contains the same
gameplay implementation, reliability fixes and experimental shared road-plan support. Changing the
version does not complete the [modernization roadmap](PLATFORM_STATUS.md) or add runtime acceptance.

The preceding release passed both Windows and Linux quality gates and player build/startup checks.
Its final local gate passed 446 Python cases (five skipped), 72 Lua modules, two JavaScript checks and
four Rust tests. The new version is rebuilt and passes the release workflow before publication;
the [release page](https://github.com/redthepro/Beam-Earth/releases/tag/v1.8.0) records final build evidence
and publishes SHA-256 checksums for the actual downloads.

Windows player compilation runs locally with the custom MSVC bootloader and native earthfast 116;
Linux AppImage compilation runs in CI. Windows and Linux hosting bundles include unchanged official
BeamMP 3.9.3 binaries, licenses, corresponding source and empty-key configuration. Those BeamMP binaries
are not compiled by this project. Players need BeamNG.drive; hosts provide their own BeamMP AuthKey.

The [shared-plan measurements](research/2026-11-02-world-bundle.md) are unchanged: consistent heights
and fewer source queries were verified, but the tiny synthetic cold-load path was slower. No new
loading-speed or FPS claim is made. Shared plans remain opt-in, bounded and incomplete as a full-world
representation. Actual two-client driving, long-session stability and network-fault recovery remain
unverified; embedded voice, Steam parties, radio and downloaded-model placement remain unfinished.
