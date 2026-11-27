# Running BeamNG.Earth on macOS with CrossOver

> **Status: community-tested, experimental.** Verified on an Apple M1 Pro Mac (macOS 27.0.1) with
> CrossOver 26.3 (Wine 11.0), BeamNG.drive 0.40.6.0 from Steam, and BeamNG.Earth 1.10.1-beta.
> Intel Macs are untested.

## How it works

The world server runs **natively on macOS**. BeamNG.drive runs inside a **CrossOver bottle**. They talk over
localhost (the server listens on `127.0.0.1:47613`).

Don't run the Windows build of the server under CrossOver. Its Python worker processes crash on an
unimplemented Windows function (`PssCaptureSnapshot`), and the GPU texture encoder fails under Wine.

## Requirements

- Apple Silicon Mac with Xcode Command Line Tools (`xcode-select --install`)
- [Homebrew](https://brew.sh)
- CrossOver with a **Windows 10 64-bit** bottle that has Steam and BeamNG.drive installed.
  Launch BeamNG once in the bottle so its user folder exists.
- Disk space for the map cache (`~/.cache/beamng-earth`, capped at 20 GB by default)

## 1. Install the tools

```
brew install python@3.12 rust git
```

## 2. Get the source and set up Python

```
git clone https://github.com/TecnaGamer/BeamNG.Earth.git
cd BeamNG.Earth
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt
```

## 3. Build the native module

The build needs one extra linker flag on macOS, otherwise it fails with many
`Undefined symbols ... "_Py..."` errors:

```
export RUSTFLAGS="-C link-arg=-undefined -C link-arg=dynamic_lookup"
python3 tools/build_native.py
```

It ends with `built .../server/earthfast.abi3.so`. The warnings about unused variables and
"built for newer macOS version" are harmless.

## 4. Point the server's BeamNG folder at the bottle

The server writes tile files under `~/.local/share/BeamNG/BeamNG.drive/current/`, but the game inside
CrossOver reads from the bottle's own copy of that folder. Link them. Without this the game connects but the
ground never renders.

Set `BOTTLE` to your bottle's name:

```
BOTTLE="Steam"
BOTTLE_CUR="$HOME/Library/Application Support/CrossOver/Bottles/$BOTTLE/drive_c/users/crossover/AppData/Local/BeamNG/BeamNG.drive/current"
mkdir -p "$HOME/.local/share/BeamNG/BeamNG.drive"
# if a real "current" folder already exists there, move it aside first
[ -d "$HOME/.local/share/BeamNG/BeamNG.drive/current" ] && [ ! -L "$HOME/.local/share/BeamNG/BeamNG.drive/current" ] \
  && mv "$HOME/.local/share/BeamNG/BeamNG.drive/current" "$HOME/.local/share/BeamNG/BeamNG.drive/current.bak"
ln -s "$BOTTLE_CUR" "$HOME/.local/share/BeamNG/BeamNG.drive/current"
```

## 5. Install the mod

```
python3 install.py
```

Don't copy `mod/beamng_earth` by hand. The installer also generates the mod's DDS textures from
`mod/texture_sources`. Without them the game logs many `Failed to create resource ... .dds` errors and signs,
dirt and some materials are missing. You can check with:

```
find "$BOTTLE_CUR/mods/unpacked/beamng_earth" -name "*.dds" | wc -l
```

(a successful install gives well over 100).

## 6. Run

Start the server and leave the terminal open:

```
./run_server.sh
```

Always use `run_server.sh` (or `python3 start.py`), not `python3 server/earthd.py` directly. The launcher
passes the recommended options, including `--buildings on`. Without it there are no buildings.

Then start BeamNG.drive from **Steam inside the bottle** with the **D3D11** renderer
(Steam launch option `-gfx dx11`, or choose "Launch (D3D11 renderer)" in BeamNG's launcher window).
Open **Freeroam > BeamNG.Earth**.

Don't run `BeamNG.Earth-<version>.exe`. That's the Windows launcher this guide replaces.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Build fails with `Undefined symbols ... "_Py..."` | Set `RUSTFLAGS` as in step 3 |
| Game connects but the ground never renders | The folder link from step 4 is missing or points at the wrong bottle |
| Many `Failed to create resource ... .dds` lines in `beamng.log` | Run `python3 install.py` (step 5) |
| Roads and terrain but no buildings | Server was started with `earthd.py` instead of `run_server.sh` |
| Everything see-through, no trees or car body; `beamng.log` shows `d3d12 ... failed to create draw-indexed command signature` | The game is on the D3D12 renderer. Use D3D11 |
| Spawned inside a building | Open the map (M) and teleport to a road, or wait for loading to finish before moving |

`beamng.log` is at
`~/Library/Application Support/CrossOver/Bottles/<bottle>/drive_c/users/crossover/AppData/Local/BeamNG/BeamNG.drive/current/beamng.log`.

## Performance

Expect roughly 30 to 35 fps with default settings under CrossOver. In my testing, shadows were the biggest cost.
Lowering shadow quality, reflections, motion blur and SSAO helps the most.