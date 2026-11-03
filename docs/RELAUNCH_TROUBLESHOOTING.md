# Startup and relaunch troubleshooting

## Waiting at "Multiplayer starting BeamMP"

The world companion is running, but the optional BeamMP launcher has not finished
starting. Its own update or download can stall before the game opens.

Close the waiting Earth window, then start single-player immediately:

```powershell
& ".\BeamNG.Earth-<VERSION>.exe" --no-beammp
```

Linux: `./BeamNG.Earth-<VERSION>-x86_64.AppImage --no-beammp`. Replace `<VERSION>`
with the downloaded filename's version. This skips BeamMP for one launch without
uninstalling it or changing saved settings.

The relaunch fixes limit the normal wait for an installed BeamMP launcher to 30
seconds, then stop that owned attempt and continue to the game. Use
`--beammp-wait 120` for a slower update. Initial installation or a missing launcher's
Linux build remains a separate operation. The console shows the limit and log paths.

On Windows, BeamMP's detailed output is usually `Launcher.log` beside its executable;
redirected `beammp-launcher.log` can be empty. Earth's companion log is
`%LOCALAPPDATA%\beamng-earth\logs\server.log`.

## The world server is already running

Close the previous Earth window first. The companion normally listens on loopback
port 47613. A port conflict alone does not prove its owner is safe to kill: the
launcher does not terminate arbitrary processes or take over a live companion.

Windows uses a short game-exit grace period; Linux retains its longer grace period
for Steam/container process handoffs. Windows CEF helpers with explicit `--type=`
arguments do not count as the main game. If command-line inspection is unavailable,
process detection remains conservative.

A game that never starts no longer leaves Earth waiting indefinitely. The startup
deadline is three minutes without a game or BeamNG launcher dialog; an open dialog
can remain open. `--no-game` intentionally runs a persistent companion.

If the actual game is stuck after a crash, save diagnostics and close it through
Task Manager. Earth does not force-close a running game or reset Steam, caches,
mods, or graphics settings.

## Acceptance checks

1. Start with `--no-beammp`, reach the game menu and connect to Earth.
2. Close the game, wait for Earth to exit, and start again. Verify the port is
   released and only one companion runs.
3. Cancel the BeamNG launch dialog and verify Earth stops after its startup deadline.
4. With BeamMP unavailable, verify its displayed timeout and single-player fallback.
   Test successful multiplayer separately when BeamMP can finish starting.
5. Close Earth while its companion prepares data and verify owned workers exit.

Automated tests use mocked clocks for startup decisions and isolated real process
trees with ephemeral ports for shutdown. They do not certify multiplayer gameplay
or diagnose native game crashes.
