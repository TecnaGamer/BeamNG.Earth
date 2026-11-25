# weathersynth

BeamNG.Earth's weather sounds made from physics: thunder from a tortuous lightning channel and rain from drop impacts
and bubbles. It renders the mod's `.ogg` assets offline (deterministic, seeded). Behind the `audio` feature it also has
a live engine for the world server. The commands are listed at the top of `src/main.rs`.

## Builds

The offline renderer needs only `libm` (pinned, pure Rust):

```
cargo build --release --locked
```

The live audio output (`--features audio`) adds cpal 0.17.3 with default features only: ALSA on Linux, and WASAPI
shared mode on Windows (windows 0.62.2). Every crate in that set was read before it came in (2026-11-25), and
`Cargo.lock` pins each one's crates.io checksum. Any fetch therefore gets those exact sources or fails.

### Normal and CI builds (no vendor folder)

```
cargo fetch --locked --target x86_64-unknown-linux-gnu      # only that target's crates (the reviewed set)
cargo build --release --locked --features audio
```

On Windows, fetch with `--target x86_64-pc-windows-gnu` (or msvc). Fetching without `--target` would also download
cpal's macOS, iOS, Android and wasm dependencies. They are never compiled for our targets, but they were not reviewed.

### Offline builds from the reviewed crates

`vendor/` is not committed (it is about 126 MB, mostly the windows crate). Rebuild it from cargo's download cache:

```
python3 -I tools/vendor_reviewed.py Cargo.lock vendor
cargo build --release --offline --locked --features audio --config vendor/config.toml
```

The tool does the following:
- **Reviewed crates:** checks each tarball's sha256 against the crates.io index and extracts the crates unmodified,
  with their `.cargo-checksum.json`.
- **Other packages in the lock:** writes stubs for cpal's other-platform dependencies, which cargo resolves across
  every platform. Each stub has the real crate's name, version, dependencies and features, a lib that is a
  `compile_error!`, and a `build.rs` that panics when the crate declares `links`. A stub can never be built by
  accident.
- **`vendor/config.toml`:** writes the source replacement, passed explicitly with `--config`. Nothing in the
  repository forces it.

### Release targets

- **Linux, glibc 2.35 or older** (like earthfast and the AppImage):

  ```
  CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C link-arg=-Wl,--allow-shlib-undefined" \
    cargo zigbuild --release --locked --features audio --target x86_64-unknown-linux-gnu.2.35
  ```

  - alsa-sys links `libasound.so.2` dynamically, so at runtime the binary uses the system's own libasound.
  - `--allow-shlib-undefined` is needed only because the build machine's libasound was built against a newer glibc
    than the 2.35 stubs (`__isoc23_vsscanf@GLIBC_2.38`). The binary itself needs at most GLIBC_2.34
    (`objdump -T`).
  - Building needs alsa-lib's development files (`pkg-config alsa`).
- **Windows:** `cargo zigbuild --release --locked --features audio --target x86_64-pc-windows-gnu`. The binary imports
  only system DLLs. No WinRT activation function is imported, and cpal uses Win32 COM (`CoCreateInstance`) only.

## Live weather sounds (`live-serve`)

`weathersynth live-serve [--thunder procedural|files:DIR]` is started by the world server (server/liveaudio.py) when
the Weather page's "Weather sounds" is Live.
- **Input:** the mod's `weather_audio` messages on stdin, one JSON line each (src/proto.rs; server/protocol_table.json).
- **Output:** a state line on stdout every 0.5 s (`live` with the output latency and the thunder counters), or one
  `failed` line before a non-zero exit. The server forwards these as `audio_state`.
- **Fallback:** the mod plays its pre-recorded files whenever the state lines stop.
- **Quiet:** when muted, when the input stops for 1 s, or on Pre-recorded.
- **Exit:** fades out and exits on EOF.

Thunder (src/thunderlive.rs):
- **Two messages per strike.** The mod sends a look-ahead hint up to 3 s early, rendered at once on its own thread,
  and the final one at the strike. Only finals play, at their UTC arrival time minus the output latency.
- **The renderer is a trait.** `ThunderRenderer::render(&Spec { seed, d, kind }) -> mono f32 at 44.1 kHz`.
  `procedural` is thunder.rs's model, set to each class's loudness target. `files:DIR` plays
  `bne_thunder_<kind><n>.wav` picked by the seed. Another method only needs a new impl and a `--thunder` name.

Output latency (src/audio.rs, serve.rs):
- **Device buffer:** cpal is asked for a fixed buffer (`--buffer`; 512 frames on Linux, 256 elsewhere), clamped to the
  device's range. WASAPI shared mode ignores the request and calls back with its own ~22 ms buffer.
- **Fill target:** one callback plus max(one callback, 512 frames), and half a callback more per ring underrun.
- **Trouble:** a stream error (ALSA POLLERR after an unrecovered xrun, a device lost), or 3 xruns within 60 s, reopens
  the default device with twice the buffer, up to 2048 frames. After 5 quiet minutes the buffer halves again, never
  below the first size. Only failures at 2048 frames count toward `failed` (3 a minute).

Ten-minute soaks (2026-11-25; silent, volumes 0; rain at 4 Hz, a hinted thunder every 30 s):

| Machine | Priority, load | Device | Latency min / p50 / p95 / max | Underruns | xruns |
|---|---|---|---|---|---|
| PC (CachyOS, PipeWire) | nice 19, a 13-core job at nice -4 | ALSA, 512 frames | 34 / 50 / 56 / 56 ms | 0 | 0 |
| Windows 11 test PC | normal | WASAPI 48 kHz, 1056-frame callbacks | 44 / 54 / 54 / 54 ms | 0 | 0 |
| Deck, Game Mode, broker game running (it at nice 10) | nice 19, load 8-10 on 8 threads | ALSA (PipeWire); 512, grew to 1024 | 50 / 99 / 132 / 134 ms | 0 | 7 |
| Deck, same | nice 0, load 7-9 | ALSA, 512 frames | 51 / 71 / 77 / 77 ms | 0 | 0 |

On the Deck a nice-19 process gets little CPU next to a nice-10 game at load 10, and it cannot raise its own priority
(RLIMIT_NICE 0). A player's server, and so the synth, runs at normal priority.

The thunder renderer (procedural) took up to 1.9 s per strike on the PC and Windows, and up to 3.5-4.3 s on the loaded
Deck. It runs on its own thread, so the output never waits for it. On the Deck, 2 of 20 thunders started late
(their renders took longer than the 3 s look-ahead). Not yet checked: the Deck in Desktop
Mode, and anything with the game in the loop (the broker).

## Thunder from the grain bank (src/grainbank.rs; method A, the user's pick on 2026-11-25)

The thunder is built from 118 short CC0 field recordings ("grains": bodies, cracks, claps and rolls, from 42
recordings), layered and spread in time by a random lightning channel. The behaviour follows
`~/bnge-tests/weather/procedural/grainbank/v1/SPEC.md`, ported from its Python reference renderer. Over 10 seeds per
class the port passes the spec's acceptance checks (`~/bnge-tests/weather/procedural/finalA/accept.py`).

- **The sources (committed):** `assets/grains/` is the cutting tool's output as it comes: `manifest.json`,
  `grains/*.ogg` (2.2 MB, Vorbis q5) and `CREDITS.md` (the 42 recordists). A re-cut from the original recordings
  replaces that folder.
- **The bank (built, gitignored):** `assets/thunder_grains.bin`, raw i16 PCM with a small header and an FNV checksum,
  so no Vorbis decoder is involved at run time. Build it with `weathersynth grainbank-pack`, run in this folder (the
  defaults are `--manifest assets/grains/manifest.json --out assets/thunder_grains.bin`).
- **How the pack decodes:**
  - ffmpeg's own Vorbis decoder, named explicitly (`-c:a vorbis`, not libvorbis);
  - its float output taken as is (`-f f32le`; the grains are mono 44.1 kHz, so nothing is remixed or resampled);
  - the conversion to 16 bits done in Rust (x 32767, rounded half away from zero, clamped).
- **The decoder as the only variable:** `assets/grains/thunder_grains.bin.sha256` is the bank packed with ffmpeg
  n9.0.2. The release workflow packs once (Ubuntu 22.04's ffmpeg) and warns when its sha256 differs; both platforms'
  apps then bundle that one file. A different decoder build moves samples by at most a few LSB (the finalA renders
  moved by at most -70 dB re peak between the two pack paths).
- **Offline:** `weathersynth grains-set --bank assets/thunder_grains.bin --out DIR --ogg` renders `bne_thunder_<class>1/2`
  (seeds 1 and 2), each levelled to its class target with a sample peak at most -1 dBFS.
  `grains-render ... --class C --seed N [--dist M] [--live]` renders a single strike.
- **Live:** `live-serve --thunder grains` (the default) reads the bank from next to the program, or from `assets/` in a
  source tree. A strike renders in 66-120 ms on the PC, against 0.8-1.9 s for the procedural model, which is the
  fallback when the bank file is missing.
- **Determinism:**
  - one named `Rng` stream per strike (`thunder-grains`);
  - libm maths and f64 mixing in a fixed order;
  - absorption through minimum-phase kernels designed per distance in 1 % steps. A kernel depends only on its key, so
    the cache never changes the output.

## Audio rules (src/audio.rs)

- **Output streams only.** On WASAPI, an input stream opened on an output device is loopback capture of the system's
  audio. A unit test fails if the input-stream builder is ever named in `audio.rs`.
- **The callback only copies.** On Windows, cpal's audio thread runs at THREAD_PRIORITY_TIME_CRITICAL. The synthesis
  runs on a worker thread into a lock-free single-producer / single-consumer ring; on an underrun the callback plays
  zeros and never waits.
- **No WinRT classes.** windows-core's registration-free WinRT fallback would search for `<Namespace>.dll`; it is
  linked but never reached.

## Checks

- `cargo test --release --locked --features audio`: the ISO 9613-1 table, the N-wave spectrum, output-only streams,
  the audio ring, the JSON line reader, the thunder bus (hint / final / late / kind change) and the live mix.
- `weathersynth storm-dump` against `tools/storm_dump.lua`, run under LuaJIT on the weather branch's storm.lua. The
  strike lattice must match byte for byte.
- `weathersynth live-bench` and `thunder-bench`: the live engine's CPU cost, with no device.
