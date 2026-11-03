# 1.7.4-beta build and validation record

The release contains the foundation reliability work and opt-in pinned road-plan bundles. It does not
complete the full modernization initiative. [Feature status](PLATFORM_STATUS.md) records remaining work.

Local machine: Windows 11, Ryzen 7 7700X, Python 3.14.7, native earthfast 116. The source-native build and
PyInstaller 6.22.3 custom MSVC bootloader build succeeded. Player executable compilation succeeded;
packaged `--server --help` and `--world-cache --help` both returned zero. These are startup/interface
checks, not game or audio/installer acceptance.

Local gate: 30 Python modules, 450 cases (445 pass, five documented skips); all 72 Lua unit/stub modules;
two Node map-math modules; four Rust discovery-service tests. Focused shared-world, worker, plan-cache,
host packaging and update checks also passed. Skips retain the foundation reasons: disabled automatic
sharing, two optional real-cache road cases, Windows FIFO absence and a standalone LuaJIT executable
check (the separate Lua gate uses Lupa).

The shared-plan test uses actual grading and local HTTP transfer with synthetic source roads and two
separate client directories/anchors. Five benchmark runs agree exactly on all 256 geographic height
samples. Source road-cell queries fall 38 to 19, elevation queries 8 to 4, with zero queries in pinned
clients. Warm fetch makes zero requests. Cold total increased from 27.10 to 234.79 ms; no loading-speed
or FPS improvement is claimed. [Full measurement](research/2026-11-02-world-bundle.md).

Both hosting ZIPs use pinned, digest-verified official BeamMP 3.9.3 binaries; these are not locally compiled
BeamMP builds. Windows `--help` passed locally. Linux hosting execution and AppImage compilation require
Linux CI, because this Windows host has no WSL/container runtime. The GitHub run attached to the release
is the authority for remote build results; do not infer them from this local record.

No actual BeamNG/BeamMP server session was launched for this validation. Users still supply their own
BeamNG installation and BeamMP AuthKey. Embedded voice, parties, imported models and radio remain absent.
This is a prerelease, with bounded road-plan sharing disabled by default and real gameplay acceptance pending.
