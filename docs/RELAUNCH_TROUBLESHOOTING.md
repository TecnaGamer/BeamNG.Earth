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

## The starting map stays at "Loading the ground"

The loading bar shows the current preparation phase when the companion supports
it. A cold location can take longer than a cached one. The mod allows five minutes
in total, including retries. It retries reported preparation failures up to five
attempts with backoff. If the game cannot import files the companion already
prepared, it tries those same files up to three times, without rebuilding them.

After the limit, the loading bar and a game message explain the failure. Save the
companion log (and the game log for an import failure), then select the location
again on the map. A timeout does not start more generation jobs automatically;
the server may still be finishing the abandoned request. Selecting an uncached
place in offline mode requires downloading its data first.

If the companion fails before the game opens, the launcher distinguishes an exit
from a startup timeout and displays the last startup step and error when present,
plus the full log's path. A semaphore cleanup warning alone does not identify why
the process stopped. See [initial-loading validation](INITIAL_LOADING_VALIDATION.md)
for the automated coverage and remaining game checks.

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
